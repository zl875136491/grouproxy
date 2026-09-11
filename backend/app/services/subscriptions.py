"""Subscription ingestion and validation.

The control plane fetches upstream content once, turns it into immutable
versions, and only sends a selected version to monitors.  This module keeps
untrusted URLs and payloads at that boundary: no browser response or audit
payload needs the upstream URL, credential, or raw subscription content.
"""

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

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
SINGLE_NODE_MAX_BYTES = 16_384


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


def _single_node_payload(outbound: dict[str, Any]) -> bytes:
    return json.dumps(
        {"outbounds": [outbound]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _single_node_scheme(value: str) -> str:
    match = re.match(r"^\s*([A-Za-z][A-Za-z0-9+.-]*)://", value)
    return match.group(1).lower() if match else ""


def _normalize_vless_node(raw: str) -> bytes:
    """Convert a VLESS URI into one canonical sing-box outbound."""

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SubscriptionError("single_node_invalid_uri") from exc
    if parsed.scheme.lower() != "vless" or not parsed.hostname or port is None:
        raise SubscriptionError("single_node_invalid_uri")
    if parsed.password is not None:
        raise SubscriptionError("single_node_invalid_uri")
    try:
        node_uuid = str(uuid.UUID(unquote(parsed.username or "")))
    except (ValueError, AttributeError) as exc:
        raise SubscriptionError("single_node_invalid_uuid") from exc
    if port < 1 or port > 65_535:
        raise SubscriptionError("single_node_invalid_port")
    host = parsed.hostname.strip()
    if not host or any(character.isspace() for character in host):
        raise SubscriptionError("single_node_invalid_host")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise SubscriptionError("single_node_invalid_parameters") from exc

    allowed_keys = {
        "encryption",
        "security",
        "type",
        "sni",
        "fp",
        "pbk",
        "sid",
        "flow",
        "packetEncoding",
    }
    unknown = set(query) - allowed_keys
    if unknown:
        raise SubscriptionError("single_node_parameter_unsupported")

    def parameter(name: str, *, required: bool = False) -> str:
        values = query.get(name, [])
        if len(values) > 1 or (required and (not values or not values[0].strip())):
            raise SubscriptionError("single_node_invalid_parameters")
        result = unquote(values[0]).strip() if values else ""
        if len(result) > MAX_FIELD_LENGTH:
            raise SubscriptionError("single_node_invalid_parameters")
        return result

    encryption = parameter("encryption") or "none"
    if encryption.lower() != "none":
        raise SubscriptionError("single_node_encryption_unsupported")
    security = (parameter("security") or "none").lower()
    if security not in {"none", "tls", "reality"}:
        raise SubscriptionError("single_node_security_unsupported")
    transport = (parameter("type") or "tcp").lower()
    if transport != "tcp":
        raise SubscriptionError("single_node_transport_unsupported")

    tag = unquote(parsed.fragment).strip() or "vless-node"
    if len(tag) > 255:
        raise SubscriptionError("single_node_name_too_long")
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": host,
        "server_port": port,
        "uuid": node_uuid,
    }
    flow = parameter("flow")
    if flow:
        outbound["flow"] = flow
    packet_encoding = parameter("packetEncoding")
    if packet_encoding:
        outbound["packet_encoding"] = packet_encoding

    if security != "none":
        server_name = parameter("sni", required=security == "reality")
        tls: dict[str, Any] = {"enabled": True}
        if server_name:
            tls["server_name"] = server_name
        fingerprint = parameter("fp")
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        if security == "reality":
            public_key = parameter("pbk", required=True)
            short_id = parameter("sid", required=True)
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", public_key):
                raise SubscriptionError("single_node_invalid_reality_key")
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", short_id):
                raise SubscriptionError("single_node_invalid_reality_short_id")
            tls["reality"] = {
                "enabled": True,
                "public_key": public_key,
                "short_id": short_id,
            }
        outbound["tls"] = tls

    return _single_node_payload(outbound)


def _vmess_payload_document(raw: str) -> dict[str, Any]:
    """Decode the standard ``vmess://<base64-json>`` representation.

    Subscription applications emit both padded standard Base64 and unpadded
    URL-safe Base64.  Decode strictly after restoring padding so malformed or
    unexpectedly large values cannot be accepted as a node definition.
    """

    _, separator, payload = raw.partition("://")
    if not separator:
        raise SubscriptionError("single_node_invalid_uri")
    payload = unquote(payload.strip())
    if not payload:
        raise SubscriptionError("single_node_invalid_vmess")
    if payload.lstrip().startswith("{"):
        decoded = payload.encode("utf-8")
    else:
        compact = re.sub(r"\s+", "", payload)
        if not compact or len(compact.encode("ascii", errors="ignore")) > SINGLE_NODE_MAX_BYTES:
            raise SubscriptionError("single_node_too_large")
        compact += "=" * (-len(compact) % 4)
        try:
            decoded = base64.b64decode(compact, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SubscriptionError("single_node_invalid_vmess") from exc
    if len(decoded) > SINGLE_NODE_MAX_BYTES:
        raise SubscriptionError("single_node_too_large")
    try:
        document = json.loads(decoded.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubscriptionError("single_node_invalid_vmess") from exc
    if not isinstance(document, dict):
        raise SubscriptionError("single_node_invalid_vmess")
    return document


def _vmess_text(
    document: dict[str, Any],
    *keys: str,
    default: str = "",
    required: bool = False,
) -> str:
    value: Any = None
    present = False
    for key in keys:
        if key in document:
            value = document[key]
            present = True
            break
    if not present or value is None:
        result = default
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        result = unquote(str(value)).strip()
    else:
        raise SubscriptionError("single_node_invalid_parameters")
    if (required and not result) or len(result) > MAX_FIELD_LENGTH:
        raise SubscriptionError("single_node_invalid_parameters")
    return result


def _vmess_int(
    document: dict[str, Any],
    *keys: str,
    default: int | None = None,
) -> int:
    value: Any = None
    present = False
    for key in keys:
        if key in document:
            value = document[key]
            present = True
            break
    if not present or value is None or value == "":
        if default is None:
            raise SubscriptionError("single_node_invalid_parameters")
        return default
    if isinstance(value, bool):
        raise SubscriptionError("single_node_invalid_parameters")
    try:
        result = int(str(value).strip(), 10)
    except (TypeError, ValueError) as exc:
        raise SubscriptionError("single_node_invalid_parameters") from exc
    return result


def _vmess_bool(document: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        raise SubscriptionError("single_node_invalid_parameters")
    return False


def _vmess_alpn(document: dict[str, Any]) -> list[str]:
    value = document.get("alpn", "")
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        values = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise SubscriptionError("single_node_invalid_parameters")
            values.append(item.strip())
    else:
        raise SubscriptionError("single_node_invalid_parameters")
    if len(values) > 16 or any(len(item) > MAX_FIELD_LENGTH for item in values):
        raise SubscriptionError("single_node_invalid_parameters")
    return values


def _normalize_vmess_node(raw: str) -> bytes:
    document = _vmess_payload_document(raw)
    host = _vmess_text(document, "add", "server", required=True)
    if not host or any(character.isspace() for character in host):
        raise SubscriptionError("single_node_invalid_host")
    port = _vmess_int(document, "port", "server_port")
    if port < 1 or port > 65_535:
        raise SubscriptionError("single_node_invalid_port")
    try:
        node_uuid = str(uuid.UUID(_vmess_text(document, "id", "uuid", required=True)))
    except (ValueError, AttributeError) as exc:
        raise SubscriptionError("single_node_invalid_uuid") from exc

    tag = _vmess_text(document, "ps", "name", "remark", default="vmess-node") or "vmess-node"
    if len(tag) > 255:
        raise SubscriptionError("single_node_name_too_long")
    security = _vmess_text(document, "scy", "security", default="auto").lower() or "auto"
    if security not in {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}:
        raise SubscriptionError("single_node_security_unsupported")
    alter_id = _vmess_int(document, "aid", "alterId", "alter_id", default=0)
    if alter_id < 0 or alter_id > 65_535:
        raise SubscriptionError("single_node_invalid_parameters")

    network = _vmess_text(document, "net", "network", default="tcp").lower() or "tcp"
    camouflage = _vmess_text(document, "type", default="none").lower() or "none"
    if network in {"raw", "none"}:
        network = "tcp"
    if network in {"tcp", "http"} and camouflage in {"http", "h2"}:
        network = "http"
    if network not in {"tcp", "ws", "websocket", "http", "h2", "grpc", "httpupgrade"}:
        raise SubscriptionError("single_node_transport_unsupported")

    transport_host = _vmess_text(document, "host", "Host", default="")
    if any(character in transport_host for character in "\r\n"):
        raise SubscriptionError("single_node_invalid_parameters")
    path = _vmess_text(document, "path", default="")
    transport: dict[str, Any] | None = None
    if network in {"ws", "websocket"}:
        transport = {"type": "ws", "path": path or "/"}
        if transport_host:
            transport["headers"] = {"Host": transport_host.split(",", 1)[0].strip()}
    elif network in {"http", "h2"}:
        transport = {"type": "http", "path": path or "/"}
        if transport_host:
            transport["host"] = [item.strip() for item in transport_host.split(",") if item.strip()]
    elif network == "grpc":
        service_name = path.lstrip("/") or _vmess_text(
            document, "serviceName", "service_name", default=""
        )
        transport = {"type": "grpc"}
        if service_name:
            transport["service_name"] = service_name
    elif network == "httpupgrade":
        transport = {"type": "httpupgrade", "path": path or "/"}
        if transport_host:
            transport["host"] = transport_host.split(",", 1)[0].strip()

    outbound: dict[str, Any] = {
        "type": "vmess",
        "tag": tag,
        "server": host,
        "server_port": port,
        "uuid": node_uuid,
        "security": security,
        "alter_id": alter_id,
    }
    packet_encoding = _vmess_text(
        document, "packetEncoding", "packet_encoding", default=""
    )
    if packet_encoding:
        outbound["packet_encoding"] = packet_encoding
    if transport is not None:
        outbound["transport"] = transport

    tls_value = document.get("tls", "")
    if isinstance(tls_value, bool):
        tls_enabled = tls_value
    elif isinstance(tls_value, (int, float)) and not isinstance(tls_value, bool):
        tls_enabled = tls_value != 0
    elif isinstance(tls_value, str):
        tls_enabled = tls_value.strip().lower() not in {"", "none", "false", "0", "off"}
    else:
        raise SubscriptionError("single_node_invalid_parameters")
    if tls_enabled:
        server_name = _vmess_text(document, "sni", "servername", "server_name", default="")
        tls: dict[str, Any] = {"enabled": True}
        if server_name:
            tls["server_name"] = server_name
        fingerprint = _vmess_text(document, "fp", "fingerprint", default="")
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        alpn = _vmess_alpn(document)
        if alpn:
            tls["alpn"] = alpn
        if _vmess_bool(document, "allowInsecure", "allow_insecure", "skip-cert-verify"):
            tls["insecure"] = True
        outbound["tls"] = tls

    return _single_node_payload(outbound)


def normalize_single_node(value: str) -> bytes:
    """Convert a VLESS or VMess URI into one canonical sing-box outbound."""

    raw = value.strip()
    if not raw or len(raw.encode("utf-8")) > SINGLE_NODE_MAX_BYTES:
        raise SubscriptionError("single_node_too_large")
    scheme = _single_node_scheme(raw)
    if scheme == "vless":
        return _normalize_vless_node(raw)
    if scheme == "vmess":
        return _normalize_vmess_node(raw)
    raise SubscriptionError("single_node_invalid_uri")


def single_node_source_name(value: str) -> str:
    """Derive an operator-facing source label for a VLESS or VMess URI."""

    raw = value.strip()
    scheme = _single_node_scheme(raw)
    if scheme == "vmess":
        try:
            document = _vmess_payload_document(raw)
            label = _vmess_text(document, "ps", "name", "remark", default="")
            if label:
                return label[:120]
            host = _vmess_text(document, "add", "server", default="node")
            port = _vmess_int(document, "port", "server_port", default=0)
            return (f"vmess-{host}:{port}" if port else f"vmess-{host}")[:120]
        except SubscriptionError:
            return "vmess-node"
    parsed = urlsplit(raw)
    label = unquote(parsed.fragment).strip()
    if not label:
        host = parsed.hostname or "node"
        try:
            port = parsed.port
        except ValueError:  # pragma: no cover - normalize_single_node validates first
            port = None
        label = f"vless-{host}:{port}" if port else f"vless-{host}"
    return label[:120]


def _canonical_subscription_content(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content
    if _single_node_scheme(text) in {"vless", "vmess"}:
        return normalize_single_node(text)
    return content


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
    Host header. Redirects are validated to prevent DNS rebinding attacks:
    - Each redirect's hostname is independently DNS-resolved and validated
    - Only public IPs are allowed (no private/local addresses)
    - Cached validation prevents attackers from rebinding DNS mid-fetch
    """

    current = source.url
    max_body_bytes = source.max_body_bytes
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    # Track validated hosts to prevent DNS rebinding
    validated_hosts: dict[str, list[str]] = {}
    
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        for redirect_index in range(source.redirect_limit + 1):
            parsed, port = _validate_url_shape(current)
            hostname = parsed.hostname or ""
            
            # Use cached addresses if we've already validated this host
            if hostname in validated_hosts:
                addresses = validated_hosts[hostname]
            else:
                # First time seeing this host - resolve and validate
                addresses = await _resolve_public_addresses(hostname, port)
                # Cache validated addresses to prevent DNS rebinding
                validated_hosts[hostname] = addresses
            
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
                        # Construct absolute redirect URL
                        next_url = urljoin(current, location)
                        # Pre-validate the redirect target before following
                        try:
                            next_parsed, next_port = _validate_url_shape(next_url)
                            next_hostname = next_parsed.hostname or ""
                            # Resolve and validate redirect target immediately
                            # This prevents DNS rebinding between redirect construction
                            # and the next loop iteration
                            if next_hostname not in validated_hosts:
                                next_addresses = await _resolve_public_addresses(
                                    next_hostname, next_port
                                )
                                validated_hosts[next_hostname] = next_addresses
                        except SubscriptionError as e:
                            # Redirect target is invalid (private IP, etc.)
                            raise SubscriptionError("subscription_redirect_rejected") from e
                        current = next_url
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
    if _single_node_scheme(text) in {"vless", "vmess"}:
        normalize_single_node(text)
        return ParsedSubscription("sing-box", 1)
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


def subscription_outbound_tags(content: bytes, format: str) -> set[str]:
    """Return the selector tags a monitor will derive from a version.

    This is deliberately a small, read-only projection.  It lets bundle
    construction discard a stale proxy selection before it reaches older
    monitors, while the monitor remains the authority that parses and applies
    the endpoint payload.
    """

    try:
        text = content.decode("utf-8-sig")
        if format == "sing-box":
            value = json.loads(text)
            items = value if isinstance(value, list) else value.get("outbounds", [])
        elif format == "sip008":
            value = json.loads(text)
            items = value.get("servers", []) if isinstance(value, dict) else []
        elif format == "clash":
            value = yaml.safe_load(text)
            items = value.get("proxies", []) if isinstance(value, dict) else []
        else:
            return set()
    except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, AttributeError):
        return set()

    if not isinstance(items, list):
        return set()
    tags: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if format == "sing-box":
            raw_tag = item.get("tag")
        elif format == "sip008":
            raw_tag = item.get("remarks")
        else:
            raw_tag = item.get("name")
        tag = str(raw_tag).strip() if raw_tag is not None else ""
        tags.add(tag or f"subscription-{index + 1}")
    return tags


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
    content = _canonical_subscription_content(content)
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
    version, changed = await record_subscription_version(
        source,
        _canonical_subscription_content(content),
    )
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
