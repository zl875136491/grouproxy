import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main
from app.routing import RoutingPolicy, RoutingRule
from app.routing_import import MAX_IMPORT_BYTES, RoutingImport, import_singbox_routing


def convert(document, *, policy=None, mode="append"):
    return import_singbox_routing(
        RoutingImport(content=json.dumps(document), policy=policy or RoutingPolicy(), mode=mode)
    )


def test_import_template_expands_ordered_arrays_and_maps_proxy():
    template = Path(__file__).parents[2] / "frontend/public/templates/sing-box-routing.json"
    result = import_singbox_routing(
        RoutingImport(content=template.read_text(), policy=RoutingPolicy())
    )
    assert result.source_rules == 4
    assert result.imported_rules == 6
    assert [rule.action for rule in result.policy.rules] == [
        "direct",
        "direct",
        "direct",
        "direct",
        "proxy",
        "reject",
    ]
    assert result.policy.final == "proxy"
    assert result.policy.rules[-1].pattern == "blocked.example"


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda rules: rules,
        lambda rules: {"rules": rules},
        lambda rules: {"route": {"rules": rules}, "outbounds": [{"password": "never-return"}]},
    ],
)
def test_accepts_array_route_and_full_config_without_importing_credentials(wrapper):
    result = convert(
        wrapper(
            [
                {"domain": ["EXAMPLE.COM.", "例子.测试"], "outbound": "proxy"},
                {"ip_cidr": "2001:db8::1/32", "action": "route", "outbound": "direct"},
            ]
        )
    )
    assert [rule.pattern for rule in result.policy.rules] == [
        "example.com",
        "xn--fsqu00a.xn--0zwm56d",
        "2001:db8::/32",
    ]
    assert "never-return" not in result.model_dump_json()


def test_append_replace_final_and_preservation_of_builtins():
    original = RoutingPolicy(
        private_direct=False,
        cn_direct=False,
        final="proxy",
        rules=[RoutingRule(kind="domain", pattern="existing.example", action="reject")],
    )
    route = {"rules": [{"domain_suffix": "new.example", "outbound": "direct"}], "final": "direct"}
    appended = convert(route, policy=original)
    replaced = convert(route, policy=original, mode="replace")
    assert [rule.pattern for rule in appended.policy.rules] == ["existing.example", "new.example"]
    assert [rule.pattern for rule in replaced.policy.rules] == ["new.example"]
    assert replaced.policy.final == "direct"
    assert not replaced.policy.cn_direct and not replaced.policy.private_direct
    assert original.final == "proxy" and len(original.rules) == 1
    assert convert({"rules": []}, policy=original, mode="replace").policy.rules == []
    assert len(convert([], policy=original).policy.rules) == 1


def test_combined_destination_fields_expand_as_ordered_alternatives():
    result = convert(
        [
            {
                "type": "default",
                "domain": ["one.example", "two.example"],
                "domain_suffix": "suffix.example",
                "ip_cidr": "10.1.2.3/8",
                "outbound": "direct",
            },
            {"domain": "after.example", "outbound": "subscription"},
        ]
    )
    assert [rule.kind for rule in result.policy.rules] == [
        "domain",
        "domain",
        "domain_suffix",
        "ip_cidr",
        "domain",
    ]
    assert result.policy.rules[3].pattern == "10.0.0.0/8"
    assert result.policy.rules[-1].action == "proxy"


@pytest.mark.parametrize(
    "rule, reason",
    [
        ({"domain": "example.com", "port": 443, "outbound": "direct"}, "port"),
        ({"rule_set": ["cn"], "outbound": "direct"}, "rule_set"),
        ({"type": "logical", "outbound": "direct"}, "default destination"),
        ({"domain": "example.com", "invert": True, "outbound": "direct"}, "invert"),
        ({"domain_regex": ".*", "action": "reject"}, "domain_regex"),
        ({"domain": "example.com", "outbound": "unknown-tag"}, "outbound must"),
        ({"domain": "example.com", "action": "hijack-dns"}, "use route"),
        ({"domain": "example.com", "action": "reject", "outbound": "direct"}, "use route"),
        ({"outbound": "direct"}, "destination matcher"),
        ({"domain": [], "action": "reject"}, "non-empty array"),
        ({"domain": [True], "action": "reject"}, "must be strings"),
        ({"domain": "*.example.com", "action": "reject"}, "invalid domain"),
        ({"ip_cidr": "garbage", "outbound": "direct"}, "invalid domain or CIDR"),
        (None, "expected an object"),
    ],
)
def test_rejects_unsupported_semantics_without_partial_import(rule, reason):
    with pytest.raises(ValueError, match=reason):
        convert([{"domain": "valid.example", "outbound": "direct"}, rule])


@pytest.mark.parametrize("content", ["null", "{}", '{"route":null}', '{"rules":{}}', "[", "true"])
def test_rejects_invalid_json_and_document_shapes(content):
    with pytest.raises(ValueError):
        import_singbox_routing(RoutingImport(content=content, policy=RoutingPolicy()))


def test_duplicate_keys_bom_and_limits():
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        import_singbox_routing(
            RoutingImport(content='{"rules":[],"rules":[]}', policy=RoutingPolicy())
        )
    assert (
        import_singbox_routing(
            RoutingImport(content='\ufeff{"rules":[]}', policy=RoutingPolicy())
        ).source_rules
        == 0
    )
    many = {"domain": ["example.com"] * 201, "outbound": "direct"}
    with pytest.raises(ValueError, match="Expanded rules"):
        convert([many])
    with pytest.raises(ValueError, match="200 source rules"):
        convert([{"domain": "example.com", "outbound": "direct"}] * 201)
    full = RoutingPolicy(
        rules=[RoutingRule(kind="domain", pattern="example.com", action="direct")] * 200
    )
    with pytest.raises(ValueError, match="Combined rules"):
        convert([{"domain": "example.com", "outbound": "direct"}], policy=full)
    with pytest.raises(ValidationError):
        RoutingImport(content=" " * (MAX_IMPORT_BYTES + 1), policy=RoutingPolicy())
    with pytest.raises(ValueError, match="1 MiB"):
        import_singbox_routing(
            RoutingImport(
                content='{"ignored":"' + "中" * (MAX_IMPORT_BYTES // 2) + '"}',
                policy=RoutingPolicy(),
            )
        )


@pytest.mark.asyncio
async def test_preview_endpoint_is_readonly_and_requires_management(monkeypatch):
    save = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(main, "update_site_routing", save)
    monkeypatch.setattr(main, "_create_release_from_draft", publish)
    result = await main.preview_routing_import(
        RoutingImport(
            content='[{"domain":"example.com","outbound":"direct"}]', policy=RoutingPolicy()
        ),
        "admin",
    )
    assert result.imported_rules == 1
    save.assert_not_called()
    publish.assert_not_called()
    route = next(route for route in main.app.routes if route.path == "/api/v1/routing/import")
    assert any(dep.call == main.require_management for dep in route.dependant.dependencies)
    with pytest.raises(HTTPException) as error:
        await main.preview_routing_import(
            RoutingImport(
                content='[{"rule_set":["cn"],"outbound":"direct"}]', policy=RoutingPolicy()
            ),
            "admin",
        )
    assert error.value.status_code == 422
    assert "rules[0]" in error.value.detail
