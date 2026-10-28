from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main
from app.routing import RoutingPolicy, RoutingPublish, RoutingRule, RoutingUpdate
from app.services import bundles


@pytest.mark.parametrize(
    "kind, pattern, expected",
    [
        ("domain", "WWW.Example.COM.", "www.example.com"),
        ("domain_suffix", ".example.com", "example.com"),
        ("domain", "例子.测试", "xn--fsqu00a.xn--0zwm56d"),
        ("ip_cidr", "10.2.3.4/8", "10.0.0.0/8"),
        ("ip_cidr", "2001:db8::1/32", "2001:db8::/32"),
    ],
)
def test_normalizes_routing_patterns(kind, pattern, expected):
    assert RoutingRule(kind=kind, pattern=pattern, action="direct").pattern == expected


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "domain", "pattern": "https://example.com/", "action": "direct"},
        {"kind": "domain", "pattern": "*.example.com", "action": "direct"},
        {"kind": "domain", "pattern": "127.0.0.1", "action": "direct"},
        {"kind": "ip_cidr", "pattern": "bad", "action": "direct"},
        {"kind": "domain", "pattern": "example.com", "action": "shell"},
        {"kind": "domain", "pattern": "example.com", "action": "proxy", "outbound": "GLOBAL"},
    ],
)
def test_rejects_invalid_or_arbitrary_rules(rule):
    with pytest.raises(ValidationError):
        RoutingRule.model_validate(rule)


def test_only_rule_mode_and_bounded_policy():
    with pytest.raises(ValidationError):
        RoutingPolicy(mode="global")
    with pytest.raises(ValidationError):
        RoutingPolicy(rules=[{"kind": "domain", "pattern": "example.com", "action": "proxy"}] * 201)
    assert RoutingPolicy().mode == "rule"
    assert RoutingPolicy().private_direct is True


@pytest.mark.asyncio
async def test_get_defaults_and_save_revision_conflict(monkeypatch):
    site = SimpleNamespace(id="site", routing_revision=0, routing_policy=None)
    monkeypatch.setattr(main.Site, "get", AsyncMock(return_value=site))
    response = await main.get_site_routing("site", "admin")
    assert response.policy.mode == "rule"
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=0))
    )
    monkeypatch.setattr(main.Site, "get_motor_collection", lambda: collection)
    with pytest.raises(HTTPException) as raised:
        await main.update_site_routing(
            "site", RoutingUpdate(expected_revision=1, policy=RoutingPolicy()), "admin"
        )
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_save_is_cas_and_does_not_publish(monkeypatch):
    site = SimpleNamespace(id="site", routing_revision=0, routing_policy=None)
    monkeypatch.setattr(main.Site, "get", AsyncMock(return_value=site))
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1))
    )
    monkeypatch.setattr(main.Site, "get_motor_collection", lambda: collection)
    monkeypatch.setattr(main, "append_audit", AsyncMock())
    release = AsyncMock()
    monkeypatch.setattr(main, "_create_release_from_draft", release)
    response = await main.update_site_routing(
        "site", RoutingUpdate(expected_revision=0, policy=RoutingPolicy()), "admin"
    )
    assert response.revision == 1
    release.assert_not_called()
    query, update = collection.update_one.call_args.args
    assert query["_id"] == "site" and "$or" in query
    assert update["$set"]["routing_policy"]["mode"] == "rule"


@pytest.mark.asyncio
async def test_publish_requires_upgraded_nodes(monkeypatch):
    site = SimpleNamespace(
        id="site", routing_revision=1, routing_policy=RoutingPolicy().model_dump()
    )
    monkeypatch.setattr(main.Site, "get", AsyncMock(return_value=site))
    monkeypatch.setattr(
        main,
        "Node",
        SimpleNamespace(
            site_id="site",
            find=lambda *_: SimpleNamespace(
                to_list=AsyncMock(return_value=[SimpleNamespace(monitor_version="0.7.0")])
            ),
        ),
    )
    with pytest.raises(HTTPException) as raised:
        await main.publish_site_routing("site", RoutingPublish(expected_revision=1), None, "admin")
    assert raised.value.detail == "routing_monitor_upgrade_required"


@pytest.mark.asyncio
async def test_bundle_signs_routing_policy_and_requires_new_monitor(monkeypatch):
    monkeypatch.setattr(bundles, "effective_blacklist", AsyncMock(return_value=[]))
    monkeypatch.setattr(bundles, "effective_access_control", AsyncMock(return_value=[]))
    monkeypatch.setattr(bundles, "selected_subscription_bundle", AsyncMock(return_value=None))
    policy = RoutingPolicy(
        final="direct",
        rules=[RoutingRule(kind="domain_suffix", pattern="example.com", action="reject")],
    )
    result = await bundles.build_signed_bundle(
        site=SimpleNamespace(id="site", shutdown=False, routing_policy=policy.model_dump()),
        node=SimpleNamespace(agent_id="agent"),
        desired_version=1,
        release_id="release",
        settings=SimpleNamespace(bundle_hmac_secret="test-secret", bundle_ttl_days=1),
        routing_policy=policy.model_dump(),
    )
    assert result["routing_policy"] == policy.model_dump()
    assert result["min_monitor_version"] == "0.8.0"
    assert result["mac"]


@pytest.mark.asyncio
async def test_saved_draft_is_not_implicitly_published(monkeypatch):
    monkeypatch.setattr(bundles, "effective_blacklist", AsyncMock(return_value=[]))
    monkeypatch.setattr(bundles, "effective_access_control", AsyncMock(return_value=[]))
    monkeypatch.setattr(bundles, "selected_subscription_bundle", AsyncMock(return_value=None))
    result = await bundles.build_signed_bundle(
        site=SimpleNamespace(
            id="site", shutdown=False, routing_policy=RoutingPolicy().model_dump()
        ),
        node=SimpleNamespace(agent_id="agent"),
        desired_version=1,
        release_id="release",
        settings=SimpleNamespace(bundle_hmac_secret="test-secret", bundle_ttl_days=1),
    )
    assert "routing_policy" not in result


def test_routing_routes_require_management():
    routes = {route.path: route for route in main.app.routes}
    for path in ("/api/v1/sites/{site_id}/routing", "/api/v1/sites/{site_id}/routing/publish"):
        assert any(
            dependency.call == main.require_management
            for dependency in routes[path].dependant.dependencies
        )


@pytest.mark.asyncio
async def test_other_releases_inherit_published_policy_not_saved_draft(monkeypatch):
    published = RoutingPolicy(final="direct").model_dump()
    previous = SimpleNamespace(
        desired_version=4, release_id="previous", bundle={"routing_policy": published}
    )
    monkeypatch.setattr(bundles, "latest_release", AsyncMock(return_value=previous))
    builder = AsyncMock(return_value={"bundle_hash": "hash", "expires_at": "2026-10-29T00:00:00Z"})
    monkeypatch.setattr(bundles, "build_signed_bundle", builder)
    monkeypatch.setattr(
        bundles, "DesiredRelease", lambda **kwargs: SimpleNamespace(**kwargs, insert=AsyncMock())
    )
    site = SimpleNamespace(
        id="site",
        config_revision=4,
        set=AsyncMock(),
        routing_policy=RoutingPolicy(final="proxy").model_dump(),
    )
    await bundles.create_desired_release(
        site=site, nodes=[SimpleNamespace(agent_id="agent")], settings=None, created_by="admin"
    )
    assert builder.call_args.kwargs["routing_policy"] == published
    replacement = RoutingPolicy(final="direct", cn_direct=False).model_dump()
    await bundles.create_desired_release(
        site=site,
        nodes=[SimpleNamespace(agent_id="agent")],
        settings=None,
        created_by="admin",
        routing_policy=replacement,
    )
    assert builder.call_args.kwargs["routing_policy"] == replacement
