from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

from ..models import CrossSiteAllow, SiteCIDR, TravelException


def normalize_cidr(value: str) -> str:
    return str(ip_network(value, strict=False))


def normalize_source_ip(value: str) -> str:
    return str(ip_address(value))


async def effective_cidrs(target_site_id: str) -> tuple[list[str], dict[str, list[str]]]:
    now = datetime.now(timezone.utc)
    sources: dict[str, list[str]] = {
        "site": [],
        "cross_site": [],
        "travel_exception": [],
    }

    own = await SiteCIDR.find(
        SiteCIDR.site_id == target_site_id,
        SiteCIDR.enabled == True,  # noqa: E712 - Beanie expression
    ).to_list()
    sources["site"] = [normalize_cidr(item.cidr) for item in own]

    cross = await CrossSiteAllow.find(
        CrossSiteAllow.to_site_id == target_site_id,
        CrossSiteAllow.enabled == True,  # noqa: E712 - Beanie expression
    ).to_list()
    for relation in cross:
        entries = await SiteCIDR.find(
            SiteCIDR.site_id == relation.from_site_id,
            SiteCIDR.enabled == True,  # noqa: E712 - Beanie expression
        ).to_list()
        sources["cross_site"].extend(normalize_cidr(item.cidr) for item in entries)

    exceptions = await TravelException.find(
        TravelException.enabled == True,  # noqa: E712 - Beanie expression
        TravelException.expires_at > now,
    ).to_list()
    sources["travel_exception"] = [normalize_cidr(item.cidr) for item in exceptions]

    merged = sorted(set(value for values in sources.values() for value in values))
    return merged, sources


def match_source_ip(source_ip: str, cidrs: list[str]) -> str | None:
    address = ip_address(source_ip)
    for cidr in cidrs:
        if address in ip_network(cidr, strict=False):
            return cidr
    return None
