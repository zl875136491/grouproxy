from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..config import PROXY_LISTEN_PORT, Settings
from ..models import (
    DesiredRelease,
    Node,
    Site,
    SiteSubscription,
    SubscriptionVersion,
)
from .cidr import effective_blacklist
from .crypto import sign_bundle
from .subscriptions import subscription_outbound_tags

RETIRED_POLICY_FIELDS = frozenset(
    {
        "allow_cidrs",
        "deny_destinations",
        "deny_sources",
        "acl_note",
        "acl_sources",
        "effective_cidrs",
        "source_blacklist",
    }
)


def strip_retired_policy_fields(value: Any) -> Any:
    """Return JSON-shaped data without policy fields removed by this release.

    Older drafts and desired bundles can remain in MongoDB across an upgrade.
    Keeping the cleanup recursive prevents those fields from leaking through a
    draft/desired response or being carried into a newly signed retry.
    """

    if isinstance(value, dict):
        return {
            key: strip_retired_policy_fields(item)
            for key, item in value.items()
            if key not in RETIRED_POLICY_FIELDS
        }
    if isinstance(value, list):
        return [strip_retired_policy_fields(item) for item in value]
    return value


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def latest_release(site_id: str, node_id: str | None = None) -> DesiredRelease | None:
    query = {"site_id": site_id}
    if node_id:
        query["node_id"] = node_id
    return await DesiredRelease.find_one(query, sort=[("desired_version", -1)])


async def selected_subscription_bundle(
    *, site_id: str, settings: Settings
) -> dict[str, Any] | None:
    """Return only the immutable subscription version selected for a site."""

    selected = await SiteSubscription.find_one(SiteSubscription.site_id == site_id)
    if selected is None:
        return None
    version = await SubscriptionVersion.get(selected.subscription_version_id)
    if version is None or not version.parse_ok:
        # A broken historical row must never produce an unsigned or incomplete
        # Desired Bundle. The publish API prevents this normal path.
        raise ValueError("selected_subscription_version_invalid")
    try:
        content = version.content.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - guarded at ingestion
        raise ValueError("selected_subscription_content_invalid") from exc
    payload: dict[str, Any] = {
        "version": version.version,
        "version_id": str(version.id),
        "hash": version.content_hash,
        "format": version.format,
        "size_bytes": version.size_bytes,
    }
    if version.size_bytes <= settings.subscription_inline_max_bytes:
        payload["content"] = content
    else:
        payload["blob_url"] = (
            f"{settings.backend_public_url.rstrip('/')}/agent/v1/blobs/{version.content_hash}"
        )
    return payload


async def _selected_subscription_tags(site_id: str) -> set[str]:
    """Resolve tags without exposing subscription content in the bundle."""

    selected = await SiteSubscription.find_one(SiteSubscription.site_id == site_id)
    if selected is None:
        return set()
    version = await SubscriptionVersion.get(selected.subscription_version_id)
    if version is None or not version.parse_ok:
        return set()
    return subscription_outbound_tags(version.content, version.format)


async def _bundle_subscription_tags(bundle: dict[str, Any]) -> set[str]:
    """Resolve the immutable outbound tags carried by an existing bundle.

    Desired bundles may inline small subscriptions or point at an immutable
    blob.  Reading the bundle's own version is important here: the selected
    site subscription can already have moved on while an older desired release
    is still being retried by a monitor.
    """

    subscription = bundle.get("subscription")
    if not isinstance(subscription, dict):
        return set()
    content = subscription.get("content")
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        content_hash = str(subscription.get("hash", ""))
        if not content_hash:
            return set()
        version = await SubscriptionVersion.find_one(
            SubscriptionVersion.content_hash == content_hash
        )
        if version is None:
            return set()
        raw = version.content
    return subscription_outbound_tags(raw, str(subscription.get("format", "")))


async def repair_stale_proxy_selection(
    desired: DesiredRelease,
    settings: Settings,
) -> bool:
    """Remove a selector preference that cannot exist in the desired bundle.

    A subscription publication can replace dozens of old outbounds while a
    queued release still carries a selection from the previous version.  The
    monitor must be able to apply that release using its normal first-outbound
    fallback, including when the bundle was created by an older backend.
    """

    repaired = strip_retired_policy_fields(desired.bundle)
    changed = repaired != desired.bundle
    selection = repaired.get("proxy_selection")
    if not isinstance(selection, dict):
        if not changed:
            return False
        signed = sign_bundle(repaired, settings.bundle_hmac_secret)
        desired.bundle = signed
        desired.bundle_hash = signed["bundle_hash"]
        await desired.save()
        return True
    group = str(selection.get("group", "")).strip()
    outbound = str(selection.get("outbound", "")).strip()
    tags = await _bundle_subscription_tags(repaired)
    if group == "subscription" and outbound and outbound in tags:
        if not changed:
            return False
    else:
        repaired = dict(repaired)
        repaired.pop("proxy_selection", None)
        changed = True
    if not changed:
        return False
    signed = sign_bundle(repaired, settings.bundle_hmac_secret)
    desired.bundle = signed
    desired.bundle_hash = signed["bundle_hash"]
    await desired.save()
    return True


async def build_signed_bundle(
    *,
    site: Site,
    node: Node,
    desired_version: int,
    release_id: str,
    settings: Settings,
    proxy_selection: dict[str, str] | None = None,
) -> dict[str, Any]:
    blacklist = await effective_blacklist(node.agent_id)
    now = datetime.now(timezone.utc)
    subscription = await selected_subscription_bundle(site_id=str(site.id), settings=settings)
    selected_tags = (
        await _selected_subscription_tags(str(site.id)) if subscription is not None else set()
    )
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "release_id": release_id,
        "desired_version": desired_version,
        "min_monitor_version": "0.6.0",
        "site_id": str(site.id),
        "node_id": node.agent_id,
        "shutdown": site.shutdown,
        "listen": {"http_port": PROXY_LISTEN_PORT},
        # Access is allow-all by default. Only this node's blacklist is
        # carried to the monitor and rendered as deny rules.
        "blacklist": blacklist,
        "subscription": subscription,
        "issued_at": iso(now),
        "expires_at": iso(now + timedelta(days=settings.bundle_ttl_days)),
    }
    if proxy_selection:
        # The selection is metadata for the monitor's generated selector. It
        # never contains endpoint credentials or a user supplied URL.
        group = str(proxy_selection.get("group", "subscription"))[:255]
        outbound = str(proxy_selection.get("outbound", ""))[:255]
        # A subscription refresh can remove the tag selected in an older
        # release.  Omitting the stale preference lets both current and
        # pre-0.4 monitors use their normal first-outbound fallback.
        if group != "subscription" or outbound in selected_tags:
            bundle["proxy_selection"] = {"group": group, "outbound": outbound}
    return sign_bundle(bundle, settings.bundle_hmac_secret)


async def create_desired_release(
    *,
    site: Site,
    nodes: list[Node],
    settings: Settings,
    created_by: str,
    previous_release_id: str | None = None,
    proxy_selection: dict[str, str] | None = None,
) -> tuple[str, list[DesiredRelease]]:
    release_id = str(uuid4())
    previous = await latest_release(str(site.id))
    desired_version = max(site.config_revision, previous.desired_version if previous else 0) + 1
    site.config_revision = desired_version
    await site.save()
    releases: list[DesiredRelease] = []
    for node in nodes:
        bundle = await build_signed_bundle(
            site=site,
            node=node,
            desired_version=desired_version,
            release_id=release_id,
            settings=settings,
            proxy_selection=(
                proxy_selection
                if proxy_selection and proxy_selection.get("node_id") in {None, node.agent_id}
                else None
            ),
        )
        item = DesiredRelease(
            release_id=release_id,
            node_id=node.agent_id,
            site_id=str(site.id),
            desired_version=desired_version,
            source_revision=site.config_revision,
            bundle_hash=bundle["bundle_hash"],
            bundle=bundle,
            previous_release_id=previous_release_id or (previous.release_id if previous else None),
            status="queued",
            expires_at=datetime.fromisoformat(bundle["expires_at"].replace("Z", "+00:00")),
            created_by=created_by,
        )
        await item.insert()
        releases.append(item)
    return release_id, releases
