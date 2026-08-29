"""Subscription ingestion and validation.

The control plane fetches upstream content once, turns it into immutable
versions, and only sends a selected version to monitors.  This module keeps
untrusted URLs and payloads at that boundary: no browser response or audit
payload needs the upstream URL, credential, or raw subscription content.
"""

import asyncio
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml

from ..config import Settings
from ..models import SubscriptionSource, SubscriptionVersion, utcnow

SUPPORTED_FORMATS = {"clash", "sip008", "sing-box"}
SUPPORTED_OUTBOUND_TYPES = {
    "anytls",
    "http",
    "hysteria2",
    "shadowsocks",
    "socks",
    "ssh",
    "trojan",
    "tuic",
    "vless",
    "vmess",
    "wireguard",
}
MAX_SUBSCRIPTION_NODES = 128
MAX_FIELD_LENGTH = 8_192
MAX_VALUE_DEPTH = 12


class SubscriptionError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ParsedSubscription:
    format: str
    node_count: int


@dataclass(frozen=True)
class RefreshResult:
    version: SubscriptionVersion
    changed: bool


def normalize_source_url(value: str) -> str:
    """Accept only HTTP upstreams while the deployment is explicitly HTTP-only."""

    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except ValueError as exc:
        raise SubscriptionError("invalid_subscription_url") from exc
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise SubscriptionError("subscription_url_scheme_not_allowed")
    if parsed.username is not None or parsed.password is not None:
        # Credential handling needs encrypted secret storage; do not silently
        # accept a URL whose auth component would be lost during safe fetching.
        raise SubscriptionError("subscription_url_credentials_not_supported")
    return urlunsplit(("http", parsed.netloc, parsed.path or "/", parsed.query, ""))


def source_url_hint(value: str) -> str:
    """A stable display value which intentionally excludes path and credentials."""

    if not value:
        return "uploaded"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port and parsed.port != 80:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}"
    except ValueError:
        return "http://[invalid]"


def _validate_url_shape(value: str) -> tuple[Any, int]:
    normalized = normalize_source_url(value)
    parsed = urlsplit(normalized)
    port = parsed.port or 80
    if port < 1 or port > 65535:
        raise SubscriptionError("invalid_subscription_url")
    return parsed, port


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # ``is_global`` excludes loopback, RFC1918, link-local, documentation,
    # multicast, unspecified, and cloud metadata addresses.
    return address.is_global


async def _resolve_public_addresses(host: str, port: int) -> list[str]:
    if host.lower() == "localhost":
        raise SubscriptionError("subscription_ssrf_blocked")
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise SubscriptionError("subscription_dns_failed", retryable=True) from exc
    addresses = sorted({record[4][0] for record in records})
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise SubscriptionError("subscription_ssrf_blocked")
    return addresses


def _request_url(parsed: Any, address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    netloc = host if port == 80 else f"{host}:{port}"
    return urlunsplit(("http", netloc, parsed.path or "/", parsed.query, ""))


def _host_header(parsed: Any, port: int) -> str:
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return host if port == 80 else f"{host}:{port}"


async def fetch_source_bytes(source: SubscriptionSource) -> bytes:
    """Fetch an HTTP subscription with redirect-by-redirect SSRF validation.

    Requests are connected to a validated address while retaining the original
    Host header.  That prevents a second DNS lookup inside the HTTP client from
    defeating the address check through DNS rebinding.
    """

    current = source.url
    max_body_bytes = source.max_body_bytes
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        for redirect_index in range(source.redirect_limit + 1):
            parsed, port = _validate_url_shape(current)
            addresses = await _resolve_public_addresses(parsed.hostname or "", port)
            endpoint = _request_url(parsed, addresses[0], port)
            try:
                async with client.stream(
                    "GET",
                    endpoint,
                    headers={"Host": _host_header(parsed, port), "Accept": "*/*"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location or redirect_index >= source.redirect_limit:
                            raise SubscriptionError("subscription_redirect_rejected")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise SubscriptionError(
                            "subscription_upstream_http_error",
                            retryable=response.status_code >= 500,
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > max_body_bytes:
                            raise SubscriptionError("subscription_response_too_large")
                        data.extend(chunk)
                    if not data:
                        raise SubscriptionError("subscription_response_empty")
                    return bytes(data)
            except SubscriptionError:
                raise
            except httpx.TimeoutException as exc:
                raise SubscriptionError("subscription_fetch_timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise SubscriptionError("subscription_fetch_failed", retryable=True) from exc
    raise SubscriptionError("subscription_redirect_rejected")


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SubscriptionError(code)
    return value


def _string(value: Any, code: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise SubscriptionError(code)
    result = value.strip()
    if (required and not result) or len(result) > MAX_FIELD_LENGTH:
        raise SubscriptionError(code)
    return result


def _port(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise SubscriptionError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SubscriptionError(code) from exc
    if result < 1 or result > 65_535:
        raise SubscriptionError(code)
    return result


def _validate_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_VALUE_DEPTH:
        raise SubscriptionError("subscription_value_too_deep")
    if isinstance(value, str):
        if len(value) > MAX_FIELD_LENGTH:
            raise SubscriptionError("subscription_field_too_long")
        return
    if isinstance(value, (int, float, bool)) or value is None:
        return
    if isinstance(value, list):
        if len(value) > MAX_SUBSCRIPTION_NODES:
            raise SubscriptionError("subscription_value_too_large")
        for item in value:
            _validate_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_SUBSCRIPTION_NODES:
            raise SubscriptionError("subscription_value_too_large")
        for key, item in value.items():
            _string(key, "subscription_invalid_field")
            _validate_value(item, depth + 1)
        return
    raise SubscriptionError("subscription_invalid_value")


def _validate_outbounds(items: Any) -> int:
    if not isinstance(items, list) or not items or len(items) > MAX_SUBSCRIPTION_NODES:
        raise SubscriptionError("subscription_outbounds_invalid")
    tags: set[str] = set()
    for index, item in enumerate(items):
        outbound = _mapping(item, "subscription_outbound_invalid")
        kind = _string(outbound.get("type"), "subscription_outbound_type_invalid").lower()
        if kind not in SUPPORTED_OUTBOUND_TYPES:
            raise SubscriptionError("subscription_outbound_type_unsupported")
        tag = _string(outbound.get("tag", f"subscription-{index + 1}"), "subscription_tag_invalid")
        if tag in {"direct", "block", "subscription"} or tag in tags:
            raise SubscriptionError("subscription_tag_invalid")
        tags.add(tag)
        _validate_value(outbound)
    return len(items)


def _parse_singbox(value: Any) -> ParsedSubscription:
    if isinstance(value, list):
        items = value
    else:
        document = _mapping(value, "subscription_json_invalid")
        items = document.get("outbounds")
    return ParsedSubscription("sing-box", _validate_outbounds(items))


def _parse_sip008(value: Any) -> ParsedSubscription:
    document = _mapping(value, "subscription_sip008_invalid")
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers or len(servers) > MAX_SUBSCRIPTION_NODES:
        raise SubscriptionError("subscription_sip008_invalid")
    for server in servers:
        item = _mapping(server, "subscription_sip008_invalid")
        _string(item.get("server"), "subscription_sip008_invalid")
        _port(item.get("server_port"), "subscription_sip008_invalid")
        _string(item.get("method"), "subscription_sip008_invalid")
        _string(item.get("password"), "subscription_sip008_invalid")
        _validate_value(item)
    return ParsedSubscription("sip008", len(servers))


def _parse_clash(value: Any) -> ParsedSubscription:
    document = _mapping(value, "subscription_clash_invalid")
    proxies = document.get("proxies")
    if not isinstance(proxies, list) or not proxies or len(proxies) > MAX_SUBSCRIPTION_NODES:
        raise SubscriptionError("subscription_clash_invalid")
    names: set[str] = set()
    supported = {"ss", "shadowsocks", "trojan", "vmess", "vless"}
    for proxy in proxies:
        item = _mapping(proxy, "subscription_clash_invalid")
        kind = _string(item.get("type"), "subscription_clash_invalid").lower()
        if kind not in supported:
            raise SubscriptionError("subscription_clash_type_unsupported")
        name = _string(item.get("name"), "subscription_clash_invalid")
        if name in names:
            raise SubscriptionError("subscription_tag_invalid")
        names.add(name)
        _string(item.get("server"), "subscription_clash_invalid")
        _port(item.get("port"), "subscription_clash_invalid")
        if kind in {"ss", "shadowsocks"}:
            _string(item.get("cipher"), "subscription_clash_invalid")
            _string(item.get("password"), "subscription_clash_invalid")
        elif kind in {"trojan", "vmess", "vless"}:
            _string(item.get("password") or item.get("uuid"), "subscription_clash_invalid")
        _validate_value(item)
    return ParsedSubscription("clash", len(proxies))


def inspect_subscription(content: bytes) -> ParsedSubscription:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubscriptionError("subscription_content_not_utf8") from exc
    if not text.strip():
        raise SubscriptionError("subscription_response_empty")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if decoded is not None:
        if isinstance(decoded, dict) and "servers" in decoded:
            return _parse_sip008(decoded)
        return _parse_singbox(decoded)
    try:
        yaml_value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SubscriptionError("subscription_format_unrecognized") from exc
    return _parse_clash(yaml_value)


async def _next_version(source_id: str) -> int:
    previous = await SubscriptionVersion.find_one(
        SubscriptionVersion.source_id == source_id,
        sort=[("version", -1)],
    )
    return (previous.version if previous else 0) + 1


async def record_subscription_version(
    source: SubscriptionSource,
    content: bytes,
    *,
    fetched_at: datetime | None = None,
) -> tuple[SubscriptionVersion, bool]:
    content_hash = hashlib.sha256(content).hexdigest()
    source_id = str(source.id)
    existing = await SubscriptionVersion.find_one(
        SubscriptionVersion.source_id == source_id,
        SubscriptionVersion.content_hash == content_hash,
    )
    if existing is not None:
        return existing, False
    parse_ok = False
    parse_error = ""
    format_name = "unknown"
    node_count = 0
    try:
        parsed = inspect_subscription(content)
        parse_ok = True
        format_name = parsed.format
        node_count = parsed.node_count
    except SubscriptionError as exc:
        parse_error = exc.code
    version = SubscriptionVersion(
        source_id=source_id,
        version=await _next_version(source_id),
        content_hash=content_hash,
        size_bytes=len(content),
        format=format_name,
        content=content,
        fetched_at=fetched_at or utcnow(),
        parse_ok=parse_ok,
        parse_error=parse_error,
        node_count=node_count,
    )
    await version.insert()
    return version, True


async def mark_refresh_failure(source: SubscriptionSource, code: str) -> None:
    source.last_refresh_attempt_at = utcnow()
    source.last_refresh_error = code[:160]
    source.consecutive_failures += 1
    source.updated_at = utcnow()
    await source.save()


async def refresh_subscription_source(
    source: SubscriptionSource,
    settings: Settings,
) -> RefreshResult:
    if not source.enabled:
        raise SubscriptionError("subscription_source_disabled")
    if not source.url:
        raise SubscriptionError("subscription_source_not_refreshable")
    try:
        content = await fetch_source_bytes(source)
        if len(content) > settings.subscription_max_body_bytes:
            raise SubscriptionError("subscription_response_too_large")
        version, changed = await record_subscription_version(source, content)
    except SubscriptionError as exc:
        await mark_refresh_failure(source, exc.code)
        raise
    source.last_refresh_attempt_at = utcnow()
    source.updated_at = utcnow()
    if not version.parse_ok:
        source.last_refresh_error = version.parse_error
        source.consecutive_failures += 1
        await source.save()
        raise SubscriptionError(version.parse_error or "subscription_parse_failed")
    source.last_refresh_at = utcnow()
    source.last_refresh_error = ""
    source.consecutive_failures = 0
    await source.save()
    return RefreshResult(version=version, changed=changed)


async def record_uploaded_subscription(
    source: SubscriptionSource,
    content: bytes,
    settings: Settings,
) -> tuple[SubscriptionVersion, bool]:
    if not content:
        raise SubscriptionError("subscription_response_empty")
    if len(content) > min(source.max_body_bytes, settings.subscription_max_body_bytes):
        raise SubscriptionError("subscription_response_too_large")
    version, changed = await record_subscription_version(source, content)
    source.last_refresh_attempt_at = utcnow()
    source.updated_at = utcnow()
    if version.parse_ok:
        source.last_refresh_at = utcnow()
        source.last_refresh_error = ""
        source.consecutive_failures = 0
    else:
        source.last_refresh_error = version.parse_error
        source.consecutive_failures += 1
    await source.save()
    return version, changed
