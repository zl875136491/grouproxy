import logging
import re
from ipaddress import ip_address, ip_network

from ..models import SourceBlacklist

logger = logging.getLogger(__name__)

# Domain rules are resolved (source) or suffix-matched (destination) by each
# monitor. Restrict them to the portable ASCII hostname subset so the control
# plane and monitor cannot disagree about whether an input is a hostname, URL,
# wildcard, or IP literal.
_HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_ADDRESS_LABEL = re.compile(r"(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)")


def canonicalize_kind(kind: str) -> str:
    if kind == "network":
        return "cidr"
    if kind in {"ip", "cidr", "domain"}:
        return kind
    raise ValueError("invalid_source_blacklist_kind")


def canonicalize_direction(direction: str) -> str:
    if direction in {"source", "destination"}:
        return direction
    raise ValueError("invalid_blacklist_direction")


def normalize_cidr(value: str) -> str:
    return str(ip_network(value, strict=False))


def normalize_source_ip(value: str) -> str:
    return str(ip_address(value))


def normalize_source_domain(value: str) -> str:
    """Return a canonical ASCII hostname suitable for monitor DNS lookup.

    A domain rule is not a URL matcher.  It must be a conventional DNS
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
    """Normalize one deny pattern before it is persisted or bundled."""

    if not isinstance(kind, str) or not isinstance(value, str):
        raise ValueError("invalid_source_blacklist_pattern")
    pattern = value.strip()
    if not pattern:
        raise ValueError("empty_source_blacklist_pattern")
    canonical_kind = canonicalize_kind(kind)
    if canonical_kind == "ip":
        return normalize_source_ip(pattern)
    if canonical_kind == "cidr":
        return normalize_cidr(pattern)
    if canonical_kind == "domain":
        return normalize_source_domain(value)
    raise ValueError("invalid_source_blacklist_kind")


def bundle_blacklist_entry(item: SourceBlacklist) -> dict[str, str]:
    return {
        "id": str(item.id),
        "direction": canonicalize_direction(getattr(item, "direction", "source")),
        "kind": canonicalize_kind(item.kind),
        "pattern": normalize_source_blacklist_pattern(item.kind, item.pattern),
    }


async def effective_blacklist(node_id: str) -> list[dict[str, str]]:
    """Return enabled rules that belong to one node."""

    entries = await SourceBlacklist.find(
        {"enabled": True, "node_id": node_id}
    ).to_list()
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in entries:
        if getattr(item, "node_id", None) != node_id:
            continue
        try:
            direction = canonicalize_direction(getattr(item, "direction", "source"))
            kind = canonicalize_kind(getattr(item, "kind", None))
            pattern = normalize_source_blacklist_pattern(kind, getattr(item, "pattern", None))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring malformed blacklist rule id=%s",
                getattr(item, "id", "unknown"),
            )
            continue
        key = (direction, kind, pattern)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "id": str(getattr(item, "id", "")),
                "direction": direction,
                "kind": kind,
                "pattern": pattern,
            }
        )
    result.sort(key=lambda value: (value["direction"], value["kind"], value["pattern"]))
    return result


async def effective_source_blacklist(node_id: str) -> list[dict[str, str]]:
    """Compatibility alias used by older call sites; source rules only."""

    return [
        rule
        for rule in await effective_blacklist(node_id)
        if rule["direction"] == "source"
    ]


def match_source_blacklist(source_ip: str, rules: list[dict[str, str | None]]) -> str | None:
    """Return the matching IP/CIDR source rule, if any.

    Domain source rules are intentionally not evaluated here because a preview
    contains only a source address. The monitor materializes them into source
    IP rules when it applies the bundle.
    """

    address = ip_address(source_ip)
    for rule in rules:
        direction = str(rule.get("direction") or "source")
        if direction != "source":
            continue
        if rule.get("kind") not in {"ip", "cidr", "network"}:
            continue
        pattern = str(rule.get("pattern") or "")
        try:
            if address in ip_network(pattern, strict=False):
                return pattern
        except ValueError:
            continue
    return None


def match_destination_blacklist(dest_host: str, rules: list[dict[str, str | None]]) -> str | None:
    """Return the matching destination rule for an IP, CIDR, or domain host."""

    host = dest_host.strip().lower().rstrip(".")
    if not host:
        return None
    address = None
    try:
        address = ip_address(host)
    except ValueError:
        pass
    for rule in rules:
        if str(rule.get("direction") or "source") != "destination":
            continue
        kind = str(rule.get("kind") or "")
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        if kind in {"ip", "cidr", "network"} and address is not None:
            try:
                if address in ip_network(pattern, strict=False):
                    return pattern
            except ValueError:
                continue
        if kind == "domain":
            if host == pattern or host.endswith("." + pattern):
                return pattern
    return None


def preview_source_blacklist(
    source_ip: str,
    rules: list[dict[str, str | None]],
    *,
    site_shutdown: bool = False,
    dest_host: str | None = None,
) -> dict[str, object]:
    """Build a conservative blacklist preview for a source IP and optional host.

    The control plane cannot determine whether an IP belongs to a source-domain
    rule: those are resolved by the monitor with its local DNS configuration at
    apply time.  Consequently, an otherwise-unmatched source is *indeterminate*
    whenever an enabled source-domain rule is present, rather than being
    reported as allowed.
    """

    source = (source_ip or "").strip()
    match = match_source_blacklist(source, rules) if source else None
    dest_match = match_destination_blacklist(dest_host, rules) if dest_host else None
    unresolved_domain_patterns = sorted(
        {
            str(rule.get("pattern") or "")
            for rule in rules
            if source
            and str(rule.get("direction") or "source") == "source"
            and str(rule.get("kind") or "").lower() == "domain"
            and rule.get("enabled", True) is not False
            and str(rule.get("pattern") or "")
        }
    )
    if site_shutdown:
        outcome = "blocked"
        reason = "shutdown"
        allowed: bool | None = False
        matched = match or dest_match
    elif match is not None:
        outcome = "blocked"
        reason = "source_blacklisted"
        allowed = False
        matched = match
    elif dest_match is not None:
        outcome = "blocked"
        reason = "dest_blacklisted"
        allowed = False
        matched = dest_match
    elif unresolved_domain_patterns:
        outcome = "indeterminate"
        reason = "domain_resolution_required"
        allowed = None
        matched = None
    else:
        outcome = "allowed"
        reason = "allowed"
        allowed = True
        matched = None
    return {
        "allowed": allowed,
        "matched_pattern": matched,
        "reason": reason,
        "outcome": outcome,
        "unresolved_domain_patterns": unresolved_domain_patterns,
    }
