from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import main as main_module
from app.schemas import SourceBlacklistCreate


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/source-blacklist"})


def _release(site_id: str, node_ids: list[str], release_id: str = "release-1"):
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    return main_module.ReleaseOut(
        release_id=release_id,
        site_id=site_id,
        node_ids=node_ids,
        desired_release_id=f"desired-{release_id}",
        previous_release_id=None,
        task_id=f"task-{release_id}",
        status="applying",
        stage="applying",
        progress=10,
        error="",
        rollback_reason="",
        started_at=now,
        finished_at=None,
        created_at=now,
    )


def _distribution(*targets: main_module.SourceBlacklistDistributionOut):
    return main_module._SourceBlacklistDistribution(
        releases=[],
        draft_ids=[],
        no_node_site_ids=[],
        targets=list(targets),
    )


class _Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other: object) -> tuple[str, object]:  # type: ignore[override]
        return self.name, other


class _FakeRule:
    node_id = _Field("node_id")
    direction = _Field("direction")
    kind = _Field("kind")
    pattern = _Field("pattern")
    existing: object | None = None
    current: "_FakeRule | None" = None

    def __init__(self, **values: object):
        self.id = "rule-1"
        self.node_id = str(values.get("node_id") or "node-a")
        self.direction = str(values.get("direction") or "source")
        self.kind = str(values["kind"])
        self.pattern = str(values["pattern"])
        self.comment = str(values["comment"])
        self.enabled = bool(values["enabled"])
        self.created_by = str(values["created_by"])
        self.created_at = datetime(2026, 9, 14, tzinfo=timezone.utc)
        self.inserted = False
        self.deleted = False
        type(self).current = self

    @classmethod
    async def find_one(cls, *_: object) -> object | None:
        return cls.existing

    @classmethod
    async def get(cls, _: str) -> "_FakeRule | None":
        return cls.current

    async def insert(self) -> None:
        self.inserted = True

    async def delete(self) -> None:
        self.deleted = True


def _patch_route_dependencies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def audit(**_: object) -> None:
        calls.append("audit")

    monkeypatch.setattr(main_module, "SourceBlacklist", _FakeRule)
    monkeypatch.setattr(main_module, "append_audit", audit)
    monkeypatch.setattr(main_module, "_actor", lambda: "admin")
    monkeypatch.setattr(main_module, "_request_id", lambda _: "request-1")
    monkeypatch.setattr(main_module, "_request_source_ip", lambda _: "127.0.0.1")
    return calls


@pytest.mark.asyncio
async def test_enabled_source_blacklist_create_preflights_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRule.current = None
    calls = _patch_route_dependencies(monkeypatch)

    async def reject_preflight(**_: object) -> list[object]:
        calls.append("preflight")
        raise HTTPException(409, {"code": "source_blacklist_release_in_progress"})

    monkeypatch.setattr(main_module, "_source_blacklist_release_plans", reject_preflight)

    with pytest.raises(HTTPException) as exc_info:
        await main_module.add_source_blacklist(
            SourceBlacklistCreate(node_ids=["node-a"], kind="ip", pattern="192.0.2.10"),
            _request(),
            "admin",
        )

    assert exc_info.value.status_code == 409
    assert _FakeRule.current is None
    assert calls == ["preflight"]


@pytest.mark.asyncio
async def test_enabled_source_blacklist_create_returns_release_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRule.current = None
    calls = _patch_route_dependencies(monkeypatch)
    target = main_module.SourceBlacklistDistributionOut(
        site_id="site-north",
        node_ids=["node-a", "node-b"],
        state="released",
        release=_release("site-north", ["node-a", "node-b"]),
    )
    seen_plans: list[object] = []

    async def plans(**_: object) -> list[object]:
        calls.append("preflight")
        plan = object()
        seen_plans.append(plan)
        return seen_plans

    async def distribute(**kwargs: object):
        calls.append("distribute")
        assert kwargs["operation"] == "created"
        assert kwargs["plans"] == seen_plans
        return _distribution(target)

    monkeypatch.setattr(main_module, "_source_blacklist_release_plans", plans)
    monkeypatch.setattr(main_module, "_distribute_source_blacklist_change", distribute)

    result = await main_module.add_source_blacklist(
        SourceBlacklistCreate(node_ids=["node-a"], kind="ip", pattern="192.0.2.11"),
        _request(),
        "admin",
    )

    assert result.operation == "created"
    assert result.rule.id == "rule-1"
    assert result.distribution == [target]
    assert _FakeRule.current is not None and _FakeRule.current.inserted is True
    assert calls == ["preflight", "distribute", "audit"]


@pytest.mark.asyncio
async def test_disabled_source_blacklist_create_is_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRule.current = None
    calls = _patch_route_dependencies(monkeypatch)
    target = main_module.SourceBlacklistDistributionOut(
        site_id="site-north", node_ids=["node-a"], state="no_effect", release=None
    )

    async def unexpected(**_: object) -> object:
        raise AssertionError("disabled rules must not create or preflight releases")

    async def no_effect(**_: object) -> list[main_module.SourceBlacklistDistributionOut]:
        calls.append("no_effect")
        return [target]

    monkeypatch.setattr(main_module, "_source_blacklist_release_plans", unexpected)
    monkeypatch.setattr(main_module, "_distribute_source_blacklist_change", unexpected)
    monkeypatch.setattr(main_module, "_source_blacklist_no_effect_distribution", no_effect)

    result = await main_module.add_source_blacklist(
        SourceBlacklistCreate(
            node_ids=["node-a"], kind="ip", pattern="192.0.2.12", enabled=False
        ),
        _request(),
        "admin",
    )

    assert result.rule.enabled is False
    assert result.distribution == [target]
    assert _FakeRule.current is not None and _FakeRule.current.inserted is True
    assert calls == ["no_effect", "audit"]


@pytest.mark.asyncio
async def test_enabled_source_blacklist_delete_preflights_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = _FakeRule(
        node_id="node-a",
        direction="source",
        kind="ip",
        pattern="192.0.2.13",
        comment="",
        enabled=True,
        created_by="admin",
    )
    calls = _patch_route_dependencies(monkeypatch)

    async def reject_preflight(**_: object) -> list[object]:
        calls.append("preflight")
        raise HTTPException(409, {"code": "source_blacklist_release_in_progress"})

    monkeypatch.setattr(main_module, "_source_blacklist_release_plans", reject_preflight)

    with pytest.raises(HTTPException) as exc_info:
        await main_module.delete_source_blacklist("rule-1", _request(), "admin")

    assert exc_info.value.status_code == 409
    assert rule.deleted is False
    assert calls == ["preflight"]


@pytest.mark.asyncio
async def test_source_blacklist_distribution_releases_each_populated_site_and_skips_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Draft:
        created: list["Draft"] = []

        def __init__(self, **values: object):
            self.__dict__.update(values)
            self.id = f"draft-{len(type(self).created) + 1}"
            self.status = "draft"
            self.updated_at = None

        async def insert(self) -> None:
            type(self).created.append(self)

        async def save(self) -> None:
            return None

    north = SimpleNamespace(id="site-north", config_revision=4)
    east = SimpleNamespace(id="site-east", config_revision=9)
    east.saved = 0

    async def save_empty() -> None:
        east.saved += 1

    east.save = save_empty
    north_nodes = [
        SimpleNamespace(id="north-a", agent_id="north-a"),
        SimpleNamespace(id="north-b", agent_id="north-b"),
    ]
    calls: list[dict[str, object]] = []

    async def effective(_: str) -> list[dict[str, str]]:
        return [{"direction": "source", "kind": "ip", "pattern": "192.0.2.14"}]

    async def create_release(**kwargs: object):
        calls.append(kwargs)
        site = kwargs["site"]
        return (
            _release(str(site.id), [node.agent_id for node in north_nodes], "release-north"),
            False,
        )

    monkeypatch.setattr(main_module, "ConfigDraft", Draft)
    monkeypatch.setattr(main_module, "effective_blacklist", effective)
    monkeypatch.setattr(main_module, "_create_release_from_draft", create_release)

    rule = SimpleNamespace(
        id="rule-14",
        node_id="north-a",
        direction="source",
        kind="ip",
        pattern="192.0.2.14",
        enabled=True,
    )
    result = await main_module._distribute_source_blacklist_change(
        rule=rule,
        operation="created",
        plans=[
            main_module._SourceBlacklistReleasePlan(site=north, nodes=north_nodes),
            main_module._SourceBlacklistReleasePlan(site=east, nodes=[]),
        ],
        actor="admin",
        request_id="request-1",
    )

    assert len(Draft.created) == 1
    assert Draft.created[0].node_ids == ["north-a", "north-b"]
    assert calls[0]["requested_node_ids"] == ["north-a", "north-b"]
    assert east.config_revision == 10
    assert east.saved == 1
    assert [(item.site_id, item.state) for item in result.targets] == [
        ("site-north", "released"),
        ("site-east", "no_nodes"),
    ]


@pytest.mark.asyncio
async def test_blacklist_plans_only_selected_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    north = SimpleNamespace(id="site-north", slug="north")
    east = SimpleNamespace(id="site-east", slug="east")
    nodes = {
        "north-a": SimpleNamespace(id="north-a", agent_id="north-a", site_id="site-north"),
        "east-a": SimpleNamespace(id="east-a", agent_id="east-a", site_id="site-east"),
        "north-b": SimpleNamespace(id="north-b", agent_id="north-b", site_id="site-north"),
    }

    async def find_node(node_id: str):
        return nodes[node_id]

    async def get_site(site_id: str):
        return north if site_id == "site-north" else east

    checked: list[list[str]] = []

    async def no_active(items: list[object]) -> None:
        checked.append([item.agent_id for item in items])
        return None

    monkeypatch.setattr(main_module, "_find_node_reference", find_node)
    monkeypatch.setattr(main_module, "Site", SimpleNamespace(get=get_site))
    monkeypatch.setattr(main_module, "_active_config_release_for_nodes", no_active)

    plans = await main_module._source_blacklist_release_plans(
        node_ids=["north-a", "east-a"]
    )

    assert [(plan.site.id, [node.agent_id for node in plan.nodes]) for plan in plans] == [
        ("site-north", ["north-a"]),
        ("site-east", ["east-a"]),
    ]
    assert checked == [["north-a"], ["east-a"]]
