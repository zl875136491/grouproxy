import logging
import re
from ipaddress import ip_address, ip_network

from ..models import SourceBlacklist

logger = logging.getLogger(__name__)

# Source-domain rules are resolved by each monitor.  Restrict them to the
# portable ASCII hostname subset so the control plane and monitor cannot
# disagree about whether an input is a hostname, URL, wildcard, or IP literal.
_HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_ADDRESS_LABEL = re.compile(r"(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)")


def normalize_cidr(value: str) -> str:
    return str(ip_network(value, strict=False))


def normalize_source_ip(value: str) -> str:
    return str(ip_address(value))


def normalize_source_domain(value: str) -> str:
    """Return a canonical ASCII hostname suitable for monitor DNS lookup.

    A domain source rule is not a URL matcher.  It must be a conventional DNS
    hostname/FQDN, without whitespace, wildcard syntax, paths, ports, or IP
    literals (including common legacy numeric IPv4 spellings).
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid_source_blacklist_domain")
    if not value.isascii():
        raise ValueError("invalid_source_blacklist_domain")

    pattern = value.lower()
    if pattern.endswith("."):
        pattern = pattern[:-1]
    if not pattern or len(pattern) > 253:
        raise ValueError("invalid_source_blacklist_domain")

    try:
        ip_address(pattern)
    except ValueError:
        pass
    else:
        raise ValueError("invalid_source_blacklist_domain")

    labels = pattern.split(".")
    if not all(_HOSTNAME_LABEL.fullmatch(label) for label in labels):
        raise ValueError("invalid_source_blacklist_domain")
    # Resolver libraries may interpret an all-numeric label sequence as a
    # non-canonical IPv4 address (for example 127.1 or 0x7f.0.0.1).  A source
    # domain rule must never be an alternate spelling of an IP rule.
    if all(_NUMERIC_ADDRESS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("invalid_source_blacklist_domain")
    return pattern


def normalize_source_blacklist_pattern(kind: str, value: str) -> str:
    """Normalize one source deny pattern before it is persisted or bundled."""

    if not isinstance(kind, str) or not isinstance(value, str):
        raise ValueError("invalid_source_blacklist_pattern")
    pattern = value.strip()
    if not pattern:
        raise ValueError("empty_source_blacklist_pattern")
    if kind == "ip":
        return normalize_source_ip(pattern)
    if kind == "network":
        return normalize_cidr(pattern)
    if kind == "domain":
        return normalize_source_domain(value)
    raise ValueError("invalid_source_blacklist_kind")


async def effective_source_blacklist(target_site_id: str) -> list[dict[str, str | None]]:
    """Return enabled global rules plus enabled rules for one site.

    The returned flat shape is the stable bundle contract consumed by the
    monitor.  Querying the site scope explicitly also prevents a malformed
    legacy row with a missing ``site_id`` from affecting unrelated sites.
    """

    entries = await SourceBlacklist.find(
        {
            "enabled": True,
            "$or": [
                {"scope": "global"},
                {"scope": "site", "site_id": target_site_id},
            ],
        }
    ).to_list()
    result: list[dict[str, str | None]] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for item in entries:
        scope = getattr(item, "scope", None)
        raw_site_id = getattr(item, "site_id", None)
        if scope == "global":
            # A global rule with a site target is not canonical. Do not let a
            # malformed row widen itself into an all-site deny rule.
            if raw_site_id is not None:
                continue
            site_id: str | None = None
        elif scope == "site":
            if not isinstance(raw_site_id, str) or raw_site_id != target_site_id:
                continue
            site_id = raw_site_id
        else:
            continue
        kind = getattr(item, "kind", None)
        try:
            pattern = normalize_source_blacklist_pattern(kind, getattr(item, "pattern", None))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring malformed source blacklist rule id=%s",
                getattr(item, "id", "unknown"),
            )
            continue
        key = (scope, kind, pattern, site_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "scope": scope,
                "site_id": site_id,
                "kind": kind,
                "pattern": pattern,
            }
        )
    result.sort(key=lambda value: (str(value["scope"]), str(value["kind"]), str(value["pattern"])))
    return result


def match_source_blacklist(source_ip: str, rules: list[dict[str, str | None]]) -> str | None:
    """Return the matching IP/network rule, if any.

    Domain rules are intentionally not evaluated here because a CIDR preview
    contains only a source address. The monitor materializes them into source
    IP rules when it applies the bundle.
    """

    address = ip_address(source_ip)
    for rule in rules:
        if rule.get("kind") not in {"ip", "network"}:
            continue
        pattern = str(rule.get("pattern") or "")
        try:
            if address in ip_network(pattern, strict=False):
                return pattern
        except ValueError:
            continue
    return None


def preview_source_blacklist(
    source_ip: str,
    rules: list[dict[str, str | None]],
    *,
    site_shutdown: bool = False,
) -> dict[str, object]:
    """Build a conservative source-rule preview for a source IP.

    The control plane cannot determine whether an IP belongs to a domain rule:
    domain rules are resolved by the monitor with its local DNS configuration at
    apply time.  Consequently, an otherwise-unmatched source is *indeterminate*
    whenever an enabled domain rule is present, rather than being reported as
    allowed.  A concrete IP/network match or a site shutdown remains definitive.
    """

    match = match_source_blacklist(source_ip, rules)
    unresolved_domain_patterns = sorted(
        {
            str(rule.get("pattern") or "")
            for rule in rules
            if str(rule.get("kind") or "").lower() == "domain"
            and rule.get("enabled", True) is not False
            and str(rule.get("pattern") or "")
        }
    )
    if site_shutdown:
        outcome = "blocked"
        reason = "shutdown"
        allowed: bool | None = False
    elif match is not None:
        outcome = "blocked"
        reason = "source_blacklisted"
        allowed = False
    elif unresolved_domain_patterns:
        outcome = "indeterminate"
        reason = "domain_resolution_required"
        allowed = None
    else:
        outcome = "allowed"
        reason = "allowed"
        allowed = True
    return {
        "allowed": allowed,
        "matched_pattern": match,
        "reason": reason,
        "outcome": outcome,
        "unresolved_domain_patterns": unresolved_domain_patterns,
    }
