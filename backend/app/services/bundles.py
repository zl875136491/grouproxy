from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..models import (
    DesiredRelease,
    DestinationBlacklist,
    Node,
    Site,
    SiteSubscription,
    SubscriptionVersion,
)
from .cidr import effective_cidrs
from .crypto import sign_bundle
from .proxy_credentials import proxy_auth_bundle


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


async def build_signed_bundle(
    *,
    site: Site,
    node: Node,
    desired_version: int,
    release_id: str,
    settings: Settings,
) -> dict[str, Any]:
    allow_cidrs, sources = await effective_cidrs(str(site.id))
    blacklist = await DestinationBlacklist.find(
        DestinationBlacklist.enabled == True,  # noqa: E712 - Beanie expression
    ).to_list()
    now = datetime.now(timezone.utc)
    subscription = await selected_subscription_bundle(site_id=str(site.id), settings=settings)
    proxy_auth = await proxy_auth_bundle(
        site_id=str(site.id), required=site.proxy_auth_required, settings=settings
    )
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "release_id": release_id,
        "desired_version": desired_version,
        # HTTP Basic authentication must never be silently ignored by an old
        # monitor, so credential-bearing bundles require the matching parser.
        "min_monitor_version": "0.3.0",
        "site_id": str(site.id),
        "node_id": node.agent_id,
        "shutdown": site.shutdown,
        "listen": {"http_port": site.http_port},
        "allow_cidrs": allow_cidrs,
        "deny_destinations": [{"pattern": item.pattern, "kind": item.kind} for item in blacklist],
        "proxy_auth": proxy_auth,
        "subscription": subscription,
        "acl_note": sources,
        "issued_at": iso(now),
        "expires_at": iso(now + timedelta(days=settings.bundle_ttl_days)),
    }
    return sign_bundle(bundle, settings.bundle_hmac_secret)


async def create_desired_release(
    *,
    site: Site,
    nodes: list[Node],
    settings: Settings,
    created_by: str,
    previous_release_id: str | None = None,
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
