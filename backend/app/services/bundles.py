from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..models import (
    DesiredRelease,
    DestinationBlacklist,
    Node,
    Site,
)
from .cidr import effective_cidrs
from .crypto import sign_bundle


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def latest_release(site_id: str, node_id: str | None = None) -> DesiredRelease | None:
    query = {"site_id": site_id}
    if node_id:
        query["node_id"] = node_id
    return await DesiredRelease.find_one(query, sort=[("desired_version", -1)])


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
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "release_id": release_id,
        "desired_version": desired_version,
        "min_monitor_version": "0.1.0",
        "site_id": str(site.id),
        "node_id": node.agent_id,
        "shutdown": site.shutdown,
        "listen": {
            "http_port": site.http_port,
            "https_port": None,
        },
        "allow_cidrs": allow_cidrs,
        "deny_destinations": [{"pattern": item.pattern, "kind": item.kind} for item in blacklist],
        "proxy_auth": {"required": False, "users": []},
        "subscription": None,
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
