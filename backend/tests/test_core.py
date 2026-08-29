import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import AgentAck
from app.services.audit import redact
from app.services.cidr import match_source_ip, normalize_cidr, normalize_source_ip
from app.services.crypto import calculate_bundle_hash, sign_bundle, verify_bundle
from app.services.subscriptions import (
    SubscriptionError,
    fetch_source_bytes,
    inspect_subscription,
    normalize_source_url,
    refresh_subscription_source,
)


def test_bundle_signature_round_trip_is_deterministic() -> None:
    bundle = {
        "schema_version": 1,
        "node_id": "codedev",
        "allow_cidrs": ["10.32.12.0/24"],
        "issued_at": datetime(2026, 8, 28, tzinfo=timezone.utc).isoformat(),
    }
    signed = sign_bundle(bundle, "test-secret")

    assert verify_bundle(signed, "test-secret") == (True, "")
    assert signed["bundle_hash"] == calculate_bundle_hash(signed)

    signed["allow_cidrs"] = ["192.0.2.0/24"]
    assert verify_bundle(signed, "test-secret") == (False, "bundle_hash_mismatch")


def test_cidr_normalization_and_preview_match() -> None:
    cidrs = [normalize_cidr("10.32.12.9/24"), normalize_cidr("2001:db8::1/64")]

    assert cidrs == ["10.32.12.0/24", "2001:db8::/64"]
    assert normalize_source_ip("10.32.12.111") == "10.32.12.111"
    assert match_source_ip("10.32.12.111", cidrs) == "10.32.12.0/24"
    assert match_source_ip("192.0.2.1", cidrs) is None


def test_audit_redaction_covers_nested_node_secrets() -> None:
    value = {
        "token": "one-time-token",
        "nested": {"agent_token": "node-token", "username": "operator"},
    }

    assert redact(value) == {
        "token": "[REDACTED]",
        "nested": {"agent_token": "[REDACTED]", "username": "operator"},
    }


def test_agent_ack_retains_last_good_version() -> None:
    field = AgentAck.model_fields["last_good_version"]

    assert field.annotation is int
    assert field.default == 0


def test_subscription_parsing_supports_required_input_formats() -> None:
    samples = [
        (
            b'{"outbounds":[{"type":"shadowsocks","tag":"edge-a","server":"198.51.100.20","server_port":8388,"method":"aes-256-gcm","password":"secret"}]}',
            "sing-box",
        ),
        (
            b'{"version":1,"servers":[{"server":"198.51.100.20","server_port":8388,"method":"aes-256-gcm","password":"secret"}]}',
            "sip008",
        ),
        (
            b"proxies:\n"
            b"  - name: edge-a\n"
            b"    type: ss\n"
            b"    server: 198.51.100.20\n"
            b"    port: 8388\n"
            b"    cipher: aes-256-gcm\n"
            b"    password: secret\n",
            "clash",
        ),
    ]

    for content, expected_format in samples:
        parsed = inspect_subscription(content)
        assert parsed.format == expected_format
        assert parsed.node_count == 1
        assert len(hashlib.sha256(content).hexdigest()) == 64


def test_subscription_url_rejects_tls_and_embedded_credentials() -> None:
    assert normalize_source_url("http://example.com/subscription") == "http://example.com/subscription"
    with pytest.raises(SubscriptionError, match="subscription_url_scheme_not_allowed"):
        normalize_source_url("https://example.com/subscription")
    with pytest.raises(SubscriptionError, match="subscription_url_credentials_not_supported"):
        normalize_source_url("http://operator:secret@example.com/subscription")


@pytest.mark.asyncio
async def test_subscription_fetch_blocks_loopback_before_request() -> None:
    source = SimpleNamespace(
        url="http://127.0.0.1/subscription",
        max_body_bytes=2_000_000,
        redirect_limit=3,
    )

    with pytest.raises(SubscriptionError, match="subscription_ssrf_blocked"):
        await fetch_source_bytes(source)


@pytest.mark.asyncio
async def test_uploaded_source_is_not_refreshable() -> None:
    source = SimpleNamespace(enabled=True, url="")

    with pytest.raises(SubscriptionError, match="subscription_source_not_refreshable"):
        await refresh_subscription_source(source, SimpleNamespace())
