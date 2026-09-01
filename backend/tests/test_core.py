import hashlib
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import _deduplicate_markers
from app.models import AgentAck
from app.services.access import render_linux_setup_script
from app.services.audit import redact
from app.services.backups import (
    backup_schedule_key,
    rehearsal_schedule_key,
    retained_scheduled_backup_ids,
)
from app.services.cidr import match_source_ip, normalize_cidr, normalize_source_ip
from app.services.crypto import calculate_bundle_hash, sign_bundle, verify_bundle
from app.services.subscriptions import (
    SubscriptionError,
    fetch_source_bytes,
    inspect_subscription,
    normalize_source_url,
    refresh_subscription_source,
)
from main import _safe_probe_target


def test_linux_setup_script_is_rendered_from_the_http_only_template() -> None:
    script = render_linux_setup_script(
        SimpleNamespace(proxy_access_fqdn="proxy.example.internal", proxy_access_port=18080)
    )

    assert 'PROXY_HOST="${GROUPROXY_PROXY_HOST:-proxy.example.internal}"' in script
    assert 'PROXY_PORT="${GROUPROXY_PROXY_PORT:-18080}"' in script
    assert "--uninstall" in script
    assert "gsettings set org.gnome.system.proxy mode manual" in script
    assert "kwriteconfig" in script
    assert "HTTPS transport is intentionally disabled." in script
    assert "ca-certificates" not in script.lower()
    assert "update-ca-certificates" not in script.lower()


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


def test_backup_schedule_keys_are_stable_within_one_interval() -> None:
    start = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    assert backup_schedule_key(
        scope="control_plane", interval_seconds=300, at=start
    ) == backup_schedule_key(
        scope="control_plane", interval_seconds=300, at=start + timedelta(seconds=299)
    )
    assert backup_schedule_key(
        scope="control_plane", interval_seconds=300, at=start
    ) != backup_schedule_key(
        scope="control_plane", interval_seconds=300, at=start + timedelta(seconds=300)
    )
    assert rehearsal_schedule_key(
        backup_id="bkp_fixture", interval_seconds=3_600, at=start
    ) == rehearsal_schedule_key(
        backup_id="bkp_fixture", interval_seconds=3_600, at=start + timedelta(seconds=1)
    )


def test_scheduled_backup_retention_keeps_time_buckets_and_excludes_manual_records() -> None:
    def record(
        backup_id: str,
        created_at: datetime,
        *,
        origin: str = "scheduled",
        status: str = "verified",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            backup_id=backup_id,
            origin=origin,
            storage_ref=f"{backup_id}.tar.gz",
            status=status,
            created_at=created_at,
        )

    records = [
        record("today-newest", datetime(2026, 8, 30, 12, tzinfo=timezone.utc)),
        record("today-older", datetime(2026, 8, 30, 9, tzinfo=timezone.utc)),
        record("yesterday", datetime(2026, 8, 29, 12, tzinfo=timezone.utc)),
        record("previous-week", datetime(2026, 8, 22, 12, tzinfo=timezone.utc)),
        record("previous-month", datetime(2026, 7, 15, 12, tzinfo=timezone.utc)),
        record(
            "manual-operator-snapshot",
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            origin="manual",
        ),
        record(
            "failed-scheduled-snapshot",
            datetime(2026, 8, 31, 12, tzinfo=timezone.utc),
            status="failed",
        ),
    ]

    retained = retained_scheduled_backup_ids(
        records,
        daily_days=2,
        weekly_weeks=2,
        monthly_months=2,
    )

    assert retained == {"today-newest", "yesterday", "previous-week", "previous-month"}


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


def test_probe_target_allows_a_public_address_and_normalizes_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def public_dns(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr("main.socket.getaddrinfo", public_dns)

    assert _safe_probe_target("https://example.com/ncr") == "https://example.com/ncr"


def test_probe_target_rejects_private_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def private_dns(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]

    monkeypatch.setattr("main.socket.getaddrinfo", private_dns)

    with pytest.raises(HTTPException, match="probe_target_private_network"):
        _safe_probe_target("https://example.com/ncr")


def test_probe_target_rejects_queries_before_dns_resolution() -> None:
    with pytest.raises(HTTPException, match="probe_target_query_not_allowed"):
        _safe_probe_target("https://example.com/ncr?unexpected=value")


class _FakeDeleteResult:
    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


class _FakeMarkerCollection:
    def __init__(self) -> None:
        self.deleted_filters: list[dict[str, object]] = []

    def aggregate(self, _: list[dict[str, object]]):
        async def groups():
            yield {"ids": ["newest", "older", "oldest"], "count": 3}
            yield {"ids": ["only"], "count": 1}

        return groups()

    async def delete_many(self, query: dict[str, object]) -> _FakeDeleteResult:
        self.deleted_filters.append(query)
        return _FakeDeleteResult(len(query["_id"]["$in"]))  # type: ignore[index]


@pytest.mark.asyncio
async def test_legacy_telemetry_deduplication_keeps_the_newest_marker() -> None:
    collection = _FakeMarkerCollection()

    removed = await _deduplicate_markers(
        collection,
        keys=["node_id", "kind", "sequence"],
        sort={"received_at": -1, "_id": -1},
    )

    assert removed == 2
    assert collection.deleted_filters == [{"_id": {"$in": ["older", "oldest"]}}]
