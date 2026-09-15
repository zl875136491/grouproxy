import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import _deduplicate_markers, _migrate_source_blacklist_state
from app.models import AgentAck
from app.schemas import SourceBlacklistCreate, SourceBlacklistPreviewResponse
from app.services import alerts, backups, cidr
from app.services.access import access_profile, load_linux_setup_script, load_windows_setup_script
from app.services.audit import redact
from app.services.backups import (
    BACKUP_SCHEMA_VERSION,
    BackupError,
    backup_schedule_key,
    create_backup_artifact,
    rehearsal_schedule_key,
    restore_backup,
    retained_scheduled_backup_ids,
    verify_backup,
)
from app.services.cidr import (
    match_source_blacklist,
    normalize_source_blacklist_pattern,
    normalize_source_domain,
    normalize_source_ip,
    preview_source_blacklist,
)
from app.services.crypto import calculate_bundle_hash, sign_bundle, verify_bundle
from app.services.subscriptions import (
    SubscriptionError,
    fetch_source_bytes,
    inspect_subscription,
    normalize_single_node,
    normalize_source_url,
    refresh_subscription_source,
    single_node_source_name,
    source_url_hint,
    subscription_outbound_tags,
)
from main import _safe_probe_target


def test_access_assets_are_selected_from_immutable_environment_profiles() -> None:
    test_settings = SimpleNamespace(environment="test")
    production_settings = SimpleNamespace(environment="production")

    test_profile = access_profile(test_settings)
    production_profile = access_profile(production_settings)
    test_linux = load_linux_setup_script(test_settings)
    production_linux = load_linux_setup_script(production_settings)
    test_windows = load_windows_setup_script(test_settings)
    production_windows = load_windows_setup_script(production_settings)

    assert test_profile.fqdn == "test-proxy.1oa.com.cn"
    assert production_profile.fqdn == "proxy.1oa.com.cn"
    assert test_profile.macos_shortcut_url == "/shortcuts/grouproxy-macos-test.shortcut"
    assert production_profile.macos_shortcut_url == "/shortcuts/grouproxy-macos-production.shortcut"
    assert "test-proxy.1oa.com.cn" in test_linux
    assert "proxy.1oa.com.cn" in production_linux
    assert "test-proxy.1oa.com.cn" in test_windows
    assert "proxy.1oa.com.cn" in production_windows
    assert test_linux.replace("test-proxy.1oa.com.cn", "proxy.1oa.com.cn") == production_linux
    assert test_windows.replace("test-proxy.1oa.com.cn", "proxy.1oa.com.cn") == production_windows


def test_access_scripts_are_parameterless_proxy_toggles() -> None:
    script = load_linux_setup_script(SimpleNamespace(environment="test"))
    windows = load_windows_setup_script(SimpleNamespace(environment="test"))

    assert "if proxy_is_enabled; then" in script
    assert "gsettings set org.gnome.system.proxy mode manual" in script
    assert "gsettings set org.gnome.system.proxy mode none" in script
    assert "kwriteconfig" in script
    assert "This proxy toggle does not accept parameters" in script
    assert "--uninstall" not in script
    assert "ca-certificates" not in script.lower()
    assert "update-ca-certificates" not in script.lower()
    assert "ProxyEnable" in windows
    assert "ProxyServer" in windows
    assert "$proxyEnabled" in windows
    assert "InternetSetOption" in windows
    assert "$Disable" not in windows
    assert "param(" not in windows
    assert "ipinfo.io" not in windows
    assert "Read-Host" not in windows
    assert "proxy-backup" not in windows
    assert "HTTP_PROXY" not in windows


def test_linux_access_script_toggles_managed_user_files(tmp_path) -> None:
    script_path = access_profile(SimpleNamespace(environment="test")).linux_script_path
    config_root = tmp_path / "config"
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config_root),
    }

    first = subprocess.run(
        ["bash", str(script_path)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    shell_file = config_root / "grouproxy" / "proxy.env"
    environment_file = config_root / "environment.d" / "90-grouproxy-proxy.conf"
    assert "Grouproxy proxy is now enabled" in first.stdout
    assert shell_file.is_file()
    assert environment_file.is_file()

    second = subprocess.run(
        ["bash", str(script_path)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "Grouproxy proxy is now disabled" in second.stdout
    assert not shell_file.exists()
    assert not environment_file.exists()


def test_bundle_signature_round_trip_is_deterministic() -> None:
    bundle = {
        "schema_version": 1,
        "node_id": "codedev",
        "marker": "stable",
        "issued_at": datetime(2026, 8, 28, tzinfo=timezone.utc).isoformat(),
    }
    signed = sign_bundle(bundle, "test-secret")

    assert verify_bundle(signed, "test-secret") == (True, "")
    assert signed["bundle_hash"] == calculate_bundle_hash(signed)

    signed["marker"] = "changed"
    assert verify_bundle(signed, "test-secret") == (False, "bundle_hash_mismatch")


def test_source_network_normalization_and_match() -> None:
    networks = [
        normalize_source_blacklist_pattern("network", "10.32.12.9/24"),
        normalize_source_blacklist_pattern("network", "2001:db8::1/64"),
    ]

    assert networks == ["10.32.12.0/24", "2001:db8::/64"]
    assert normalize_source_ip("10.32.12.111") == "10.32.12.111"
    assert match_source_blacklist(
        "10.32.12.111",
        [{"direction": "source", "kind": "network", "pattern": networks[0]}],
    ) == "10.32.12.0/24"
    assert match_source_blacklist(
        "2001:db8::42",
        [{"direction": "source", "kind": "cidr", "pattern": networks[1]}],
    ) == "2001:db8::/64"
    assert match_source_blacklist(
        "192.0.2.1",
        [{"direction": "source", "kind": "network", "pattern": networks[0]}],
    ) is None


def test_source_blacklist_normalization_and_match() -> None:
    assert normalize_source_blacklist_pattern("ip", " 192.0.2.10 ") == "192.0.2.10"
    assert normalize_source_blacklist_pattern("network", "10.0.0.8/24") == "10.0.0.0/24"
    assert normalize_source_blacklist_pattern("domain", "Blocked.Example.") == "blocked.example"
    rules = [
        {"direction": "source", "kind": "ip", "pattern": "192.0.2.10"},
        {"direction": "source", "kind": "cidr", "pattern": "10.0.0.0/24"},
        {"direction": "source", "kind": "domain", "pattern": "blocked.example"},
    ]
    assert match_source_blacklist("192.0.2.10", rules) == "192.0.2.10"
    assert match_source_blacklist("10.0.0.12", rules) == "10.0.0.0/24"
    assert match_source_blacklist("198.51.100.5", rules) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Blocked.Example.", "blocked.example"),
        ("localhost", "localhost"),
        ("xn--mnich-kva.example", "xn--mnich-kva.example"),
        ("service-1.internal.example", "service-1.internal.example"),
    ],
)
def test_source_domain_normalization_accepts_ascii_hostnames(value: str, expected: str) -> None:
    assert normalize_source_domain(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        " https://blocked.example",
        "https://blocked.example",
        "blocked.example/path",
        "blocked.example:443",
        "*.blocked.example",
        "blocked example",
        "blocked\\texample",
        "m\u00fcnich.example",
        "-blocked.example",
        "blocked-.example",
        "blocked..example",
        "192.0.2.1",
        "2001:db8::1",
        "127.1",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "2130706433",
    ],
)
def test_source_domain_normalization_rejects_non_hostnames(value: str) -> None:
    with pytest.raises(ValueError, match="invalid_source_blacklist_domain"):
        normalize_source_domain(value)


def test_source_blacklist_create_normalizes_and_rejects_domain_patterns() -> None:
    rule = SourceBlacklistCreate(node_ids=["node-a"], kind="domain", pattern="Blocked.Example.")
    assert rule.pattern == "blocked.example"

    with pytest.raises(ValueError, match="invalid_source_blacklist_pattern"):
        SourceBlacklistCreate(node_ids=["node-a"], kind="domain", pattern="https://blocked.example")


def test_source_blacklist_preview_is_indeterminate_for_domain_rules() -> None:
    response = SourceBlacklistPreviewResponse(
        allowed=True,
        matched_pattern=None,
        reason="allowed",
        source_blacklist=[
            {"direction": "source", "kind": "ip", "pattern": "192.0.2.1"},
            {"direction": "source", "kind": "domain", "pattern": "blocked.example"},
        ],
    )

    assert response.allowed is None
    assert response.reason == "domain_resolution_required"
    assert response.outcome == "indeterminate"
    assert response.unresolved_domain_patterns == ["blocked.example"]


def test_source_blacklist_preview_stays_blocked_when_ip_rule_matches() -> None:
    response = SourceBlacklistPreviewResponse(
        allowed=False,
        matched_pattern="192.0.2.1",
        reason="source_blacklisted",
        source_blacklist=[
            {"direction": "source", "kind": "ip", "pattern": "192.0.2.1"},
            {"direction": "source", "kind": "domain", "pattern": "blocked.example"},
        ],
    )

    assert response.allowed is False
    assert response.reason == "source_blacklisted"
    assert response.outcome == "blocked"
    assert response.unresolved_domain_patterns == ["blocked.example"]


def test_source_blacklist_preview_helper_reports_domain_resolution_required() -> None:
    result = preview_source_blacklist(
        "198.51.100.5",
        [{"direction": "source", "kind": "domain", "pattern": "blocked.example"}],
    )

    assert result == {
        "allowed": None,
        "matched_pattern": None,
        "reason": "domain_resolution_required",
        "outcome": "indeterminate",
        "unresolved_domain_patterns": ["blocked.example"],
    }


def test_source_blacklist_preview_helper_keeps_definitive_results() -> None:
    rules = [
        {"direction": "source", "kind": "ip", "pattern": "192.0.2.1"},
        {"direction": "source", "kind": "domain", "pattern": "blocked.example"},
    ]
    assert preview_source_blacklist("192.0.2.1", rules)["allowed"] is False
    assert preview_source_blacklist("192.0.2.1", rules)["reason"] == "source_blacklisted"
    assert preview_source_blacklist("198.51.100.5", [], site_shutdown=True) == {
        "allowed": False,
        "matched_pattern": None,
        "reason": "shutdown",
        "outcome": "blocked",
        "unresolved_domain_patterns": [],
    }


class _MigrationCursor:
    def __init__(self, documents: list[dict[str, object]]) -> None:
        self.documents = list(documents)

    def __aiter__(self):
        async def values():
            for document in self.documents:
                yield document

        return values()


class _MigrationCollection:
    def __init__(self, documents: list[dict[str, object]]) -> None:
        self.documents = documents

    def find(self, _: dict[str, object]) -> _MigrationCursor:
        return _MigrationCursor(self.documents)

    async def drop_index(self, _: str) -> None:
        return None

    async def delete_one(self, query: dict[str, object]) -> None:
        self.documents[:] = [
            document for document in self.documents if document["_id"] != query["_id"]
        ]

    async def update_one(self, query: dict[str, object], update: dict[str, object]) -> None:
        for document in self.documents:
            if document["_id"] == query["_id"]:
                for field in update.get("$unset", {}):
                    document.pop(field, None)
                document.update(update.get("$set", {}))  # type: ignore[arg-type]
                return
        raise AssertionError(f"missing document {query!r}")

    async def insert_many(self, documents: list[dict[str, object]]) -> None:
        self.documents.extend(documents)


def _legacy_blacklist_documents(created_at: datetime) -> list[dict[str, object]]:
    return [
        {
            "_id": "global",
            "scope": "global",
            "site_id": None,
            "kind": "ip",
            "pattern": "192.0.2.10",
            "comment": "global",
            "enabled": True,
            "created_by": "admin",
            "created_at": created_at,
        },
        {
            "_id": "site",
            "scope": "site",
            "site_id": "site-a",
            "kind": "network",
            "pattern": "198.51.100.42/24",
            "comment": "network",
            "enabled": True,
            "created_by": "admin",
            "created_at": created_at,
        },
        {
            "_id": "wrong-global-target",
            "scope": "global",
            "site_id": "site-a",
            "kind": "ip",
            "pattern": "192.0.2.10",
            "comment": "global",
            "enabled": True,
            "created_by": "admin",
            "created_at": created_at,
        },
        {
            "_id": "duplicate",
            "scope": "global",
            "site_id": None,
            "kind": "ip",
            "pattern": "192.0.2.10",
            "comment": "duplicate",
            "enabled": True,
            "created_by": "admin",
            "created_at": created_at,
        },
        {
            "_id": "invalid-kind",
            "scope": "global",
            "site_id": None,
            "kind": "bogus",
            "pattern": "192.0.2.0/24",
            "enabled": True,
        },
        {
            "_id": "orphan-site",
            "scope": "site",
            "site_id": "missing-site",
            "kind": "ip",
            "pattern": "192.0.2.11",
            "enabled": True,
        },
        {
            "_id": "invalid-enabled",
            "scope": "global",
            "site_id": None,
            "kind": "ip",
            "pattern": "192.0.2.12",
            "enabled": "true",
        },
    ]


@pytest.mark.asyncio
async def test_source_blacklist_startup_migration_drops_legacy_rules_without_nodes() -> None:
    created_at = datetime(2026, 9, 14, tzinfo=timezone.utc)
    source_rules = _MigrationCollection(_legacy_blacklist_documents(created_at))
    collections = {
        "Site": _MigrationCollection([{"_id": "site-a"}]),
        "SourceBlacklist": source_rules,
    }

    class Database:
        def __getitem__(self, name: str) -> _MigrationCollection:
            return collections[name]

        async def list_collection_names(self) -> list[str]:
            return list(collections)

    updated, removed = await _migrate_source_blacklist_state(Database())

    assert (updated, removed) == (0, 7)
    assert source_rules.documents == []


@pytest.mark.asyncio
async def test_source_blacklist_startup_migration_expands_legacy_rules_onto_nodes() -> None:
    created_at = datetime(2026, 9, 14, tzinfo=timezone.utc)
    source_rules = _MigrationCollection(_legacy_blacklist_documents(created_at))
    collections = {
        "Site": _MigrationCollection([{"_id": "site-a"}, {"_id": "site-b"}]),
        "Node": _MigrationCollection(
            [
                {"_id": "node-a", "agent_id": "node-a", "site_id": "site-a"},
                {"_id": "node-b", "agent_id": "node-b", "site_id": "site-b"},
            ]
        ),
        "SourceBlacklist": source_rules,
    }

    class Database:
        def __getitem__(self, name: str) -> _MigrationCollection:
            return collections[name]

        async def list_collection_names(self) -> list[str]:
            return list(collections)

    updated, removed = await _migrate_source_blacklist_state(Database())

    assert (updated, removed) == (3, 5)
    remaining = sorted(
        (
            (
                document.get("node_id"),
                document.get("direction"),
                document.get("kind"),
                document.get("pattern"),
            )
            for document in source_rules.documents
        )
    )
    assert remaining == [
        ("node-a", "source", "cidr", "198.51.100.0/24"),
        ("node-a", "source", "ip", "192.0.2.10"),
        ("node-b", "source", "ip", "192.0.2.10"),
    ]
    for document in source_rules.documents:
        assert "scope" not in document
        assert "site_id" not in document


@pytest.mark.asyncio
async def test_effective_blacklist_ignores_malformed_rules_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        SimpleNamespace(
            id="ip",
            node_id="node-a",
            direction="source",
            kind="ip",
            pattern="192.0.2.10",
        ),
        SimpleNamespace(
            id="cidr",
            node_id="node-a",
            direction="source",
            kind="network",
            pattern="198.51.100.0/24",
        ),
        SimpleNamespace(
            id="dest",
            node_id="node-a",
            direction="destination",
            kind="domain",
            pattern="ads.example",
        ),
        SimpleNamespace(
            id="other-node",
            node_id="node-b",
            direction="source",
            kind="ip",
            pattern="192.0.2.11",
        ),
        SimpleNamespace(
            id="bad-kind",
            node_id="node-a",
            direction="source",
            kind="bogus",
            pattern="192.0.2.0/24",
        ),
        SimpleNamespace(
            id="bad-pattern",
            node_id="node-a",
            direction="source",
            kind="domain",
            pattern="https://bad.example",
        ),
    ]

    class Query:
        async def to_list(self) -> list[SimpleNamespace]:
            return entries

    class SourceBlacklistModel:
        @classmethod
        def find(cls, query: dict[str, object]) -> Query:
            assert query["enabled"] is True
            assert query["node_id"] == "node-a"
            return Query()

    monkeypatch.setattr(cidr, "SourceBlacklist", SourceBlacklistModel)

    assert await cidr.effective_blacklist("node-a") == [
        {
            "id": "dest",
            "direction": "destination",
            "kind": "domain",
            "pattern": "ads.example",
        },
        {
            "id": "cidr",
            "direction": "source",
            "kind": "cidr",
            "pattern": "198.51.100.0/24",
        },
        {
            "id": "ip",
            "direction": "source",
            "kind": "ip",
            "pattern": "192.0.2.10",
        },
    ]


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


@pytest.mark.asyncio
async def test_resolve_open_alerts_marks_a_recovered_category_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2026, 9, 2, tzinfo=timezone.utc)

    class Record:
        def __init__(self) -> None:
            self.status = "open"
            self.resolved_at = None
            self.last_seen_at = None
            self.saved = False

        async def save(self) -> None:
            self.saved = True

    records = [Record(), Record()]

    class Query:
        async def to_list(self) -> list[Record]:
            return records

    class AlertModel:
        @classmethod
        def find(cls, query: dict[str, str]) -> Query:
            assert query == {"category": "backup", "status": "open"}
            return Query()

    monkeypatch.setattr(alerts, "Alert", AlertModel)
    monkeypatch.setattr(alerts, "utcnow", lambda: current)

    assert await alerts.resolve_open_alerts(category="backup") == 2
    assert all(record.status == "resolved" for record in records)
    assert all(record.resolved_at == current for record in records)
    assert all(record.last_seen_at == current and record.saved for record in records)


class _BackupCursor:
    def __init__(self, documents: list[dict[str, object]]) -> None:
        self.documents = documents

    def sort(self, *_: object) -> "_BackupCursor":
        return self

    def __aiter__(self):
        async def values():
            for document in self.documents:
                yield document

        return values()


class _BackupCollection:
    def __init__(self, name: str, documents: list[dict[str, object]]) -> None:
        self.name = name
        self.documents = documents

    def find(self, _: dict[str, object]) -> _BackupCursor:
        return _BackupCursor(self.documents)


def _backup_settings(tmp_path, *, member_limit: int = 4_096) -> SimpleNamespace:
    return SimpleNamespace(
        environment="test",
        backup_encryption_key=None,
        backup_directory=str(tmp_path),
        backup_member_max_bytes=member_limit,
        backup_archive_max_bytes=member_limit * 32,
    )


@pytest.mark.asyncio
async def test_backup_splits_large_collections_and_rehearses_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    collection = _BackupCollection(
        "large_collection",
        [{"_id": str(index), "payload": "x" * 3_000} for index in range(6)],
    )

    class Model:
        @classmethod
        def get_motor_collection(cls) -> _BackupCollection:
            return collection

    monkeypatch.setattr(backups, "DOCUMENT_MODELS", [Model])
    settings = _backup_settings(tmp_path)

    artifact = await create_backup_artifact(
        backup_id="bkp_chunked", scope="control_plane", settings=settings
    )
    metadata = artifact.manifest["collections"]["large_collection"]
    record = SimpleNamespace(
        storage_ref=artifact.filename,
        artifact_paths=[],
        checksum=artifact.checksum,
        encrypted=False,
    )

    assert artifact.manifest["schema_version"] == BACKUP_SCHEMA_VERSION
    assert len(metadata["chunks"]) > 1
    assert await verify_backup(settings=settings, record=record) == {
        "scope": "control_plane",
        "schema_version": BACKUP_SCHEMA_VERSION,
        "collections": 1,
        "documents": 6,
    }
    rehearsal = await restore_backup(settings=settings, record=record, apply_changes=False)
    assert rehearsal["mode"] == "rehearsal"
    assert rehearsal["collections"] == 1
    assert rehearsal["documents"] == 6


@pytest.mark.asyncio
async def test_backup_rejects_a_single_document_larger_than_one_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    collection = _BackupCollection("large_document", [{"_id": "one", "payload": "x" * 8_192}])

    class Model:
        @classmethod
        def get_motor_collection(cls) -> _BackupCollection:
            return collection

    monkeypatch.setattr(backups, "DOCUMENT_MODELS", [Model])

    with pytest.raises(BackupError, match="backup_document_too_large"):
        await create_backup_artifact(
            backup_id="bkp_large_document",
            scope="control_plane",
            settings=_backup_settings(tmp_path),
        )


@pytest.mark.asyncio
async def test_backup_verifies_the_legacy_single_member_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    collection_name = "legacy_collection"
    content = b'{"_id":"legacy","value":1}\n'
    manifest = {
        "schema_version": 1,
        "scope": "control_plane",
        "created_at": "2026-09-02T00:00:00+00:00",
        "collections": {
            collection_name: {
                "documents": 1,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "legacy.tar.gz"
    with tarfile.open(path, mode="w:gz") as archive:
        for name, payload in (
            (f"collections/{collection_name}.jsonl", content),
            ("manifest.json", manifest_payload),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    payload = path.read_bytes()
    record = SimpleNamespace(
        storage_ref=path.name,
        artifact_paths=[],
        checksum=hashlib.sha256(payload).hexdigest(),
        encrypted=False,
    )
    collection = _BackupCollection(collection_name, [])

    class Model:
        @classmethod
        def get_motor_collection(cls) -> _BackupCollection:
            return collection

    monkeypatch.setattr(backups, "DOCUMENT_MODELS", [Model])
    settings = _backup_settings(tmp_path)

    assert await verify_backup(settings=settings, record=record) == {
        "scope": "control_plane",
        "schema_version": 1,
        "collections": 1,
        "documents": 1,
    }
    rehearsal = await restore_backup(settings=settings, record=record, apply_changes=False)
    assert rehearsal["documents"] == 1


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


def test_single_vless_reality_node_is_normalized_to_a_singbox_subscription() -> None:
    content = normalize_single_node(
        "vless://f128b39b-fcaa-46fd-adf6-c1f746956645@64.110.114.73:39465?"
        "encryption=none&security=reality&type=tcp&sni=www.microsoft.com&fp=chrome&"
        "pbk=4hfFy1eGI-ePouORmeIKSGq8DDY3Q9T2J3xc1UkJqSQ&sid=6ba85179e30d4fc2&"
        "flow=xtls-rprx-vision#VLESS_Reality_Vision"
    )

    parsed = inspect_subscription(content)
    document = json.loads(content)
    outbound = document["outbounds"][0]
    assert parsed.format == "sing-box"
    assert parsed.node_count == 1
    assert outbound["type"] == "vless"
    assert outbound["tls"]["reality"] == {
        "enabled": True,
        "public_key": "4hfFy1eGI-ePouORmeIKSGq8DDY3Q9T2J3xc1UkJqSQ",
        "short_id": "6ba85179e30d4fc2",
    }


def test_single_vless_reality_requires_handshake_parameters() -> None:
    with pytest.raises(SubscriptionError, match="single_node_invalid_parameters"):
        normalize_single_node(
            "vless://f128b39b-fcaa-46fd-adf6-c1f746956645@64.110.114.73:39465?"
            "encryption=none&security=reality&type=tcp&sni=www.microsoft.com&"
            "pbk=4hfFy1eGI-ePouORmeIKSGq8DDY3Q9T2J3xc1UkJqSQ"
        )


def test_single_vmess_uri_supports_urlsafe_base64_and_common_transport_fields() -> None:
    document = {
        "v": "2",
        "ps": "VMess edge",
        "add": "edge.example.com",
        "port": "443",
        "id": "f128b39b-fcaa-46fd-adf6-c1f746956645",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "cdn.example.com",
        "path": "/gateway",
        "tls": "tls",
        "sni": "cdn.example.com",
        "fp": "chrome",
        "alpn": "h2,http/1.1",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(document, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")

    content = normalize_single_node(f"vmess://{encoded}")
    parsed = inspect_subscription(content)
    outbound = json.loads(content)["outbounds"][0]

    assert parsed.format == "sing-box"
    assert parsed.node_count == 1
    assert outbound["type"] == "vmess"
    assert outbound["uuid"] == document["id"]
    assert outbound["security"] == "auto"
    assert outbound["transport"] == {
        "headers": {"Host": "cdn.example.com"},
        "path": "/gateway",
        "type": "ws",
    }
    assert outbound["tls"]["server_name"] == "cdn.example.com"
    assert single_node_source_name(f"vmess://{encoded}") == "VMess edge"


def test_single_vmess_uri_rejects_invalid_payload_and_unsupported_transport() -> None:
    with pytest.raises(SubscriptionError, match="single_node_invalid_vmess"):
        normalize_single_node("vmess://not-base64")

    document = {
        "add": "edge.example.com",
        "port": 443,
        "id": "f128b39b-fcaa-46fd-adf6-c1f746956645",
        "net": "kcp",
    }
    encoded = base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")
    with pytest.raises(SubscriptionError, match="single_node_transport_unsupported"):
        normalize_single_node(f"vmess://{encoded}")


def test_single_vmess_uri_accepts_boolean_tls_and_httpupgrade_transport() -> None:
    document = {
        "ps": "upgrade",
        "add": "edge.example.com",
        "port": 443,
        "id": "f128b39b-fcaa-46fd-adf6-c1f746956645",
        "net": "httpupgrade",
        "path": "/upgrade",
        "host": "edge.example.com",
        "tls": True,
    }
    encoded = base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")
    outbound = json.loads(normalize_single_node(f"vmess://{encoded}"))["outbounds"][0]
    assert outbound["transport"] == {
        "type": "httpupgrade",
        "path": "/upgrade",
        "host": "edge.example.com",
    }
    assert outbound["tls"] == {"enabled": True}


def test_subscription_outbound_tags_match_monitor_fallback_names() -> None:
    assert subscription_outbound_tags(
        b'{"outbounds":[{"type":"vmess","server":"edge.example"},{"type":"vless","tag":"edge-b"}]}',
        "sing-box",
    ) == {"subscription-1", "edge-b"}
    assert subscription_outbound_tags(
        b"proxies:\n  - type: vmess\n    name: edge-a\n    server: edge.example\n    port: 443\n",
        "clash",
    ) == {"edge-a"}
    assert subscription_outbound_tags(
        b'{"version":1,"servers":[{"server":"edge.example","server_port":443,"method":"x","password":"y"}]}',
        "sip008",
    ) == {"subscription-1"}


def test_full_singbox_client_config_keeps_only_leaf_outbounds() -> None:
    content = json.dumps(
        {
            "log": {"level": "info"},
            "outbounds": [
                {"type": "urltest", "tag": "auto", "outbounds": ["edge-a"]},
                {"type": "selector", "tag": "manual", "outbounds": ["edge-a"]},
                {"type": "direct", "tag": "direct"},
                {
                    "type": "vless",
                    "tag": "edge-a",
                    "server": "198.51.100.20",
                    "server_port": 443,
                    "uuid": "f128b39b-fcaa-46fd-adf6-c1f746956645",
                },
            ],
        }
    ).encode()
    parsed = inspect_subscription(content)
    assert parsed.format == "sing-box"
    assert parsed.node_count == 1
    assert subscription_outbound_tags(content, "sing-box") == {"edge-a"}


def test_clash_meta_profile_without_inline_proxies_is_rejected() -> None:
    content = (
        b"proxy-providers:\n"
        b"  p:\n"
        b"    type: http\n"
        b"    url: https://example.com/nodes\n"
        b"proxy-groups:\n"
        b"  - name: g\n"
        b"    type: select\n"
        b"    use: [p]\n"
    )
    with pytest.raises(SubscriptionError, match="subscription_clash_profile_unsupported"):
        inspect_subscription(content)


def test_subscription_url_accepts_http_and_https_and_rejects_credentials() -> None:
    assert normalize_source_url("http://example.com/subscription") == "http://example.com/subscription"
    assert (
        normalize_source_url("https://example.com/subscription")
        == "https://example.com/subscription"
    )
    assert (
        normalize_source_url("example.com/subscription", scheme="https")
        == "https://example.com/subscription"
    )
    assert (
        normalize_source_url("http://example.com:8080/sub", scheme="https")
        == "https://example.com:8080/sub"
    )
    assert (
        normalize_source_url("https://example.com/sub", scheme="http")
        == "http://example.com/sub"
    )
    assert source_url_hint("https://example.com:8443/subscription") == "https://example.com:8443/subscription"
    assert source_url_hint("https://example.com/subscription") == "https://example.com/subscription"
    with pytest.raises(SubscriptionError, match="subscription_url_scheme_not_allowed"):
        normalize_source_url("ftp://example.com/subscription")
    with pytest.raises(SubscriptionError, match="subscription_url_credentials_not_supported"):
        normalize_source_url("http://operator:secret@example.com/subscription")


@pytest.mark.asyncio
async def test_subscription_fetch_disables_tls_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr("app.services.subscriptions.httpx.AsyncClient", _FakeClient)
    source = SimpleNamespace(
        url="https://127.0.0.1/subscription",
        max_body_bytes=2_000_000,
        redirect_limit=3,
    )
    with pytest.raises(SubscriptionError, match="subscription_ssrf_blocked"):
        await fetch_source_bytes(source)
    assert captured.get("verify") is False


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


class _TelemetryField:
    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> tuple[str, object]:  # type: ignore[override]
        return self.name, other


class _FakeUpdateResult:
    def __init__(self, matched_count: int) -> None:
        self.matched_count = matched_count


class _FakeTelemetryCursorCollection:
    def __init__(self, last_sequence: int) -> None:
        self.last_sequence = last_sequence
        self.filters: list[dict[str, object]] = []

    async def update_one(self, query: dict[str, object], _: dict[str, object]) -> _FakeUpdateResult:
        self.filters.append(query)
        expected = query.get("last_sequence")
        if not isinstance(expected, dict):
            return _FakeUpdateResult(0)
        if "$lt" in expected and self.last_sequence < int(expected["$lt"]):
            self.last_sequence = int(expected["$lt"])
            return _FakeUpdateResult(1)
        if "$gt" in expected and self.last_sequence > int(expected["$gt"]):
            self.last_sequence = int(expected["$gt"])
            return _FakeUpdateResult(1)
        return _FakeUpdateResult(0)


class _FakeTelemetryBatchDoc:
    def __init__(self, **values: object) -> None:
        self.values = values
        self.batch_id = str(values.get("batch_id") or "")
        self.sequence = int(values.get("sequence") or 0)

    async def insert(self) -> None:
        raise AssertionError("insert should be patched in tests")

    async def delete(self) -> None:
        return None


@pytest.mark.asyncio
async def test_telemetry_sequence_restart_purges_old_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    from pymongo.errors import DuplicateKeyError

    from main import _accept_telemetry_batch
    import main as main_module

    cursor = _FakeTelemetryCursorCollection(last_sequence=50_000)
    deleted: list[dict[str, object]] = []
    inserts: list[dict[str, object]] = []
    existing_by_batch: dict[str, _FakeTelemetryBatchDoc] = {
        "old-1": _FakeTelemetryBatchDoc(batch_id="old-1", sequence=12),
    }
    existing_by_sequence: dict[int, _FakeTelemetryBatchDoc] = {
        12: existing_by_batch["old-1"],
    }

    class _FakeBatchModel:
        node_id = _TelemetryField("node_id")
        kind = _TelemetryField("kind")
        batch_id = _TelemetryField("batch_id")
        sequence = _TelemetryField("sequence")

        def __init__(self, **values: object) -> None:
            self.doc = _FakeTelemetryBatchDoc(**values)

        async def insert(self) -> None:
            sequence = self.doc.sequence
            batch_id = self.doc.batch_id
            if sequence in existing_by_sequence and existing_by_sequence[sequence].batch_id != batch_id:
                raise DuplicateKeyError("sequence")
            inserts.append(self.doc.values)
            existing_by_sequence[sequence] = self.doc
            existing_by_batch[batch_id] = self.doc

        @classmethod
        async def find_one(cls, *predicates: object) -> _FakeTelemetryBatchDoc | None:
            values = {
                name: value
                for predicate in predicates
                if isinstance(predicate, tuple) and len(predicate) == 2
                for name, value in [predicate]
            }
            if "batch_id" in values:
                return existing_by_batch.get(str(values["batch_id"]))
            if "sequence" in values:
                return existing_by_sequence.get(int(values["sequence"]))
            return None

        @classmethod
        def get_motor_collection(cls) -> SimpleNamespace:
            async def delete_many(query: dict[str, object]) -> None:
                deleted.append(query)
                existing_by_batch.clear()
                existing_by_sequence.clear()

            return SimpleNamespace(delete_many=delete_many)

    monkeypatch.setattr(main_module.TelemetryCursor, "get_motor_collection", staticmethod(lambda: cursor))
    monkeypatch.setattr(main_module, "TelemetryBatch", _FakeBatchModel)

    node = SimpleNamespace(agent_id="codedev")
    accepted = await _accept_telemetry_batch(
        node=node,
        kind="connection_snapshot",
        batch_id="new-12",
        sequence=12,
        item_count=1,
    )

    assert accepted is True
    assert deleted == [{"node_id": "codedev", "kind": "connection_snapshot"}]
    assert inserts[-1]["batch_id"] == "new-12"
    assert cursor.last_sequence == 12


@pytest.mark.asyncio
async def test_telemetry_sequence_collision_purges_after_cursor_already_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymongo.errors import DuplicateKeyError

    from main import _accept_telemetry_batch
    import main as main_module

    cursor = _FakeTelemetryCursorCollection(last_sequence=11)
    deleted: list[dict[str, object]] = []
    existing_by_sequence = {12: _FakeTelemetryBatchDoc(batch_id="old-12", sequence=12)}

    class _FakeBatchModel:
        node_id = _TelemetryField("node_id")
        kind = _TelemetryField("kind")
        batch_id = _TelemetryField("batch_id")
        sequence = _TelemetryField("sequence")
        attempts = 0

        def __init__(self, **values: object) -> None:
            self.doc = _FakeTelemetryBatchDoc(**values)

        async def insert(self) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise DuplicateKeyError("sequence")
            existing_by_sequence[self.doc.sequence] = self.doc

        @classmethod
        async def find_one(cls, *predicates: object) -> _FakeTelemetryBatchDoc | None:
            values = {
                name: value
                for predicate in predicates
                if isinstance(predicate, tuple) and len(predicate) == 2
                for name, value in [predicate]
            }
            if "batch_id" in values:
                return None
            if "sequence" in values:
                return existing_by_sequence.get(int(values["sequence"]))
            return None

        @classmethod
        def get_motor_collection(cls) -> SimpleNamespace:
            async def delete_many(query: dict[str, object]) -> None:
                deleted.append(query)
                existing_by_sequence.clear()

            return SimpleNamespace(delete_many=delete_many)

    monkeypatch.setattr(main_module.TelemetryCursor, "get_motor_collection", staticmethod(lambda: cursor))
    monkeypatch.setattr(main_module, "TelemetryBatch", _FakeBatchModel)

    accepted = await _accept_telemetry_batch(
        node=SimpleNamespace(agent_id="codedev"),
        kind="connection_snapshot",
        batch_id="live-12",
        sequence=12,
        item_count=1,
    )

    assert accepted is True
    assert deleted == [{"node_id": "codedev", "kind": "connection_snapshot"}]
    assert cursor.last_sequence == 12


def test_subscription_version_id_from_bundle_reads_desired_payload() -> None:
    import main as main_module

    assert (
        main_module._subscription_version_id_from_bundle(
            {"subscription": {"version_id": "6aa8e7018a60e479f3dca9f2", "hash": "abc"}}
        )
        == "6aa8e7018a60e479f3dca9f2"
    )
    assert main_module._subscription_version_id_from_bundle({"subscription": {}}) == ""
    assert main_module._subscription_version_id_from_bundle({"blacklist": []}) == ""
    assert main_module._subscription_version_id_from_bundle(None) == ""


def test_release_out_includes_subscription_labels() -> None:
    import main as main_module

    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    out = main_module._release_out(
        SimpleNamespace(
            release_id="rel-1",
            site_id="site-1",
            node_ids=["codedev"],
            desired_release_id="desired-1",
            previous_release_id=None,
            task_id="task-1",
            status="succeeded",
            stage="succeeded",
            progress=100,
            error="",
            rollback_reason="",
            started_at=now,
            finished_at=now,
            created_at=now,
        ),
        subscription_name="ssdnote",
        subscription_source_id="src-1",
    )
    assert out.subscription_name == "ssdnote"
    assert out.subscription_source_id == "src-1"


@pytest.mark.asyncio
async def test_subscription_labels_for_releases_join_desired_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as main_module

    desired = SimpleNamespace(
        release_id="rel-1",
        bundle={"subscription": {"version_id": "ver-1"}},
    )
    version = SimpleNamespace(id="ver-1", source_id="src-1")
    source = SimpleNamespace(id="src-1", name="ssdnote")

    class _Desired:
        @staticmethod
        def find(_query: object) -> SimpleNamespace:
            async def to_list() -> list[SimpleNamespace]:
                return [desired]

            return SimpleNamespace(to_list=to_list)

    async def fake_version_get(item_id: str) -> SimpleNamespace | None:
        return version if item_id == "ver-1" else None

    async def fake_source_get(item_id: str) -> SimpleNamespace | None:
        return source if item_id == "src-1" else None

    monkeypatch.setattr(main_module, "DesiredRelease", _Desired)
    monkeypatch.setattr(main_module.SubscriptionVersion, "get", fake_version_get)
    monkeypatch.setattr(main_module.SubscriptionSource, "get", fake_source_get)

    labels = await main_module._subscription_labels_for_releases(
        [SimpleNamespace(release_id="rel-1")]
    )
    assert labels == {"rel-1": ("ssdnote", "src-1")}
