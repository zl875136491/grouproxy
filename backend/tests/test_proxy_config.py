from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import main as main_module
from app.schemas import AgentProxyConfigBatch, NodeNameUpdate, ProxyGroupSnapshot
from app.services import bundles


def _request() -> Request:
    return Request({"type": "http", "method": "PATCH", "path": "/api/v1/nodes/a", "headers": []})


def test_single_node_subscription_route_is_registered() -> None:
    routes = {
        (route.path, method)
        for route in main_module.app.routes
        for method in (route.methods or set())
    }

    assert ("/api/v1/subscriptions/single-node", "POST") in routes
    assert (
        "/api/v1/subscriptions/{source_id}/versions/{version_id}/content",
        "GET",
    ) in routes


@pytest.mark.asyncio
async def test_single_node_handler_reports_validation_errors_after_route_match() -> None:
    payload = main_module.SubscriptionSingleNodeCreate(name="probe", uri="vmess://invalid")

    with pytest.raises(HTTPException) as exc_info:
        await main_module.create_single_node_subscription(payload, "admin")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "single_node_invalid_vmess"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected_outbound, expected_selection",
    [
        ("missing", None),
        ("current", {"group": "subscription", "outbound": "current"}),
    ],
)
async def test_bundle_drops_proxy_selection_removed_by_subscription_refresh(
    monkeypatch: pytest.MonkeyPatch,
    selected_outbound: str,
    expected_selection: dict[str, str] | None,
) -> None:
    class EmptyBlacklistQuery:
        async def to_list(self) -> list[object]:
            return []

    class EmptyBlacklist:
        enabled = object()

        @classmethod
        def find(cls, *_: object) -> EmptyBlacklistQuery:
            return EmptyBlacklistQuery()

    async def no_cidrs(_: str) -> tuple[list[str], list[str]]:
        return [], []

    async def current_subscription(**_: object) -> dict[str, object]:
        return {
            "version": 1,
            "version_id": "version-1",
            "hash": "a" * 64,
            "format": "sing-box",
            "size_bytes": 64,
            "content": '{"outbounds":[{"type":"vmess","tag":"current"}]}',
        }

    async def current_tags(_: str) -> set[str]:
        return {"current"}

    monkeypatch.setattr(bundles, "DestinationBlacklist", EmptyBlacklist)
    monkeypatch.setattr(bundles, "effective_cidrs", no_cidrs)
    monkeypatch.setattr(bundles, "selected_subscription_bundle", current_subscription)
    monkeypatch.setattr(bundles, "_selected_subscription_tags", current_tags)

    site = SimpleNamespace(id="site-1", shutdown=False)
    node = SimpleNamespace(agent_id="node-1")
    settings = SimpleNamespace(
        bundle_hmac_secret="bundle-secret-for-test",
        bundle_ttl_days=30,
        subscription_inline_max_bytes=128_000,
        backend_public_url="http://127.0.0.1:8000",
    )
    result = await bundles.build_signed_bundle(
        site=site,
        node=node,
        desired_version=2,
        release_id="release-1",
        settings=settings,
        proxy_selection={"group": "subscription", "outbound": selected_outbound},
    )

    assert result.get("proxy_selection") == expected_selection


@pytest.mark.asyncio
async def test_repair_stale_proxy_selection_resigns_existing_bundle() -> None:
    saved = False

    async def save() -> None:
        nonlocal saved
        saved = True

    desired = SimpleNamespace(
        bundle={
            "schema_version": 1,
            "subscription": {
                "format": "sing-box",
                "content": '{"outbounds":[{"type":"vmess","tag":"current"}]}',
            },
            "proxy_selection": {"group": "subscription", "outbound": "removed"},
        },
        bundle_hash="old",
        save=save,
    )
    settings = SimpleNamespace(bundle_hmac_secret="bundle-secret")

    assert await bundles.repair_stale_proxy_selection(desired, settings) is True
    assert saved is True
    assert "proxy_selection" not in desired.bundle
    assert desired.bundle_hash != "old"


def test_latest_ack_per_node_ignores_historical_failures() -> None:
    older = SimpleNamespace(
        node_id="codedev",
        sequence=10,
        received_at=datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc),
    )
    newer = SimpleNamespace(
        node_id="codedev",
        sequence=11,
        received_at=datetime(2026, 9, 7, 18, 1, tzinfo=timezone.utc),
    )
    other = SimpleNamespace(
        node_id="nuc",
        sequence=1,
        received_at=datetime(2026, 9, 7, 18, 2, tzinfo=timezone.utc),
    )

    result = main_module._latest_ack_per_node([older, newer, other])

    assert [(item.node_id, item.sequence) for item in result] == [
        ("codedev", 11),
        ("nuc", 1),
    ]


def test_proxy_projection_contains_only_safe_group_metadata() -> None:
    group = ProxyGroupSnapshot.model_validate(
        {
            "name": " GLOBAL ",
            "type": "Fallback",
            "now": "edge-a",
            "all": ["edge-a", " edge-a ", "edge-b"],
            "server": "198.51.100.20",
            "password": "must-not-persist",
            "nodes": [
                {
                    "name": "edge-a",
                    "type": "Trojan",
                    "udp": True,
                    "alive": True,
                    "delay_ms": 123,
                    "server": "198.51.100.20",
                    "uuid": "must-not-persist",
                }
            ],
        }
    )

    projected = main_module._sanitize_proxy_group(group)

    assert projected is not None
    assert projected.name == "GLOBAL"
    assert projected.all == ["edge-a", "edge-b"]
    assert projected.nodes[0].model_dump() == {
        "name": "edge-a",
        "type": "Trojan",
        "udp": True,
        "alive": True,
        "delay_ms": 123,
        "history": [],
    }
    serialized = str(projected.model_dump())
    assert "198.51.100.20" not in serialized
    assert "must-not-persist" not in serialized
    assert "password" not in projected.model_dump()


def test_proxy_config_batch_normalizes_legacy_null_groups() -> None:
    payload = AgentProxyConfigBatch.model_validate(
        {
            "node_id": "codedev",
            "batch_id": "proxy-config-legacy",
            "sequence": 1,
            "sampled_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "api_available": False,
            "groups": None,
            "error": "clash_api_unavailable",
        }
    )

    assert payload.groups == []


@pytest.mark.asyncio
async def test_latest_proxy_config_queries_the_most_recent_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(node_id="codedev")

    class SnapshotField:
        def __eq__(self, _: object) -> "SnapshotField":  # type: ignore[override]
            return self

        def __neg__(self) -> "SnapshotField":
            return self

    class Query:
        def __init__(self) -> None:
            self.sort_called = False

        def sort(self, _: object) -> "Query":
            self.sort_called = True
            return self

        async def first_or_none(self) -> object:
            return snapshot

    query = Query()

    class SnapshotModel:
        node_id = SnapshotField()
        sampled_at = SnapshotField()

        @classmethod
        def find(cls, _: object) -> Query:
            return query

    monkeypatch.setattr(main_module, "ProxyConfigSnapshot", SnapshotModel)

    result = await main_module._latest_proxy_config("codedev")

    assert result is snapshot
    assert query.sort_called is True


def test_proxy_selection_allows_only_the_rendered_subscription_selector() -> None:
    assert main_module._proxy_selection_group(" subscription ") == "subscription"

    with pytest.raises(HTTPException) as exc_info:
        main_module._proxy_selection_group("GLOBAL")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "proxy_group_not_selectable"


def test_proxy_output_sanitizes_legacy_raw_snapshot() -> None:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    snapshot = SimpleNamespace(
        id="snapshot-1",
        node_id="codedev",
        site_id="site-north",
        sampled_at=now,
        api_available=True,
        groups=[
            {
                "name": "subscription",
                "type": "Selector",
                "now": "edge-a",
                "all": ["edge-a"],
                "nodes": [
                    {
                        "name": "edge-a",
                        "type": "Trojan",
                        "server": "10.0.0.1",
                        "password": "secret",
                    }
                ],
                "uuid": "secret",
            }
        ],
        error="",
        received_at=now,
    )

    output = main_module._proxy_config_out(snapshot).model_dump()

    assert output["groups"][0]["nodes"][0]["name"] == "edge-a"
    assert "server" not in str(output)
    assert "password" not in str(output)
    assert "uuid" not in str(output)


@pytest.mark.asyncio
async def test_unavailable_proxy_api_retains_last_known_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    existing = SimpleNamespace(
        node_id="codedev",
        site_id="old-site",
        batch_id="old-batch",
        sampled_at=now,
        api_available=True,
        groups=[{"name": "subscription", "type": "Selector", "all": ["edge-a"], "nodes": []}],
        error="",
        received_at=now,
        expires_at=now,
        saved=False,
    )

    async def save_snapshot() -> None:
        existing.saved = True

    existing.save = save_snapshot

    class SnapshotField:
        def __eq__(self, _: object) -> "SnapshotField":  # type: ignore[override]
            return self

    class SnapshotModel:
        node_id = SnapshotField()

        @classmethod
        async def find_one(cls, *_: object) -> object:
            return existing

    async def accept(**_: object) -> bool:
        return True

    monkeypatch.setattr(main_module, "ProxyConfigSnapshot", SnapshotModel)
    monkeypatch.setattr(main_module, "_accept_telemetry_batch", accept)
    node = SimpleNamespace(agent_id="codedev", site_id="site-north")
    payload = AgentProxyConfigBatch(
        node_id="codedev",
        batch_id="proxy-config-2",
        sequence=2,
        sampled_at=now,
        api_available=False,
        groups=[],
        error="clash_api_unavailable",
    )

    result = await main_module.agent_proxy_config(payload, node)

    assert result.accepted is True
    assert existing.api_available is False
    assert existing.error == "clash_api_unavailable"
    assert existing.groups == [
        {"name": "subscription", "type": "Selector", "all": ["edge-a"], "nodes": []}
    ]


@pytest.mark.asyncio
async def test_node_rename_changes_only_display_name(monkeypatch: pytest.MonkeyPatch) -> None:
    node = SimpleNamespace(
        id="node-document-id",
        site_id="site-north",
        name="codedev",
        agent_id="codedev",
        advertise_ip="127.0.0.1",
        monitor_version="0.3.0",
        singbox_version="unknown",
        last_seen_at=None,
        desired_version=1,
        applied_version=1,
        applied_hash="hash",
        liveness_status="online",
        config_status="in_sync",
        service_status="healthy",
        subscription_status="current",
        probe_status="unknown",
        last_error="",
        saved=False,
    )

    async def find_node(_: str) -> object:
        return node

    async def save() -> None:
        node.saved = True

    async def audit(**_: object) -> None:
        return None

    node.save = save
    monkeypatch.setattr(main_module, "_find_node_reference", find_node)
    monkeypatch.setattr(main_module, "append_audit", audit)
    monkeypatch.setattr(main_module, "_actor", lambda: "admin")

    result = await main_module.update_node(
        "codedev", NodeNameUpdate(name="  North gateway  "), _request(), "admin"
    )

    assert result.name == "North gateway"
    assert node.name == "North gateway"
    assert node.agent_id == "codedev"
    assert node.site_id == "site-north"
    assert node.saved is True


@pytest.mark.asyncio
async def test_node_rename_rejects_blank_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "_find_node_reference",
        lambda _: _async_value(SimpleNamespace(name="codedev")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await main_module.update_node(
            "codedev", NodeNameUpdate(name=" "), _request(), "admin"
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "node_name_required"


async def _async_value(value: object) -> object:
    return value
