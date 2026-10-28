import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .routing import RoutingPolicy, RoutingRule

MAX_IMPORT_BYTES = 1024 * 1024
MATCH_FIELDS = ("domain", "domain_suffix", "ip_cidr")
OUTBOUND_ACTIONS = {"direct": "direct", "subscription": "proxy", "proxy": "proxy"}


class RoutingImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)
    mode: Literal["append", "replace"] = "append"
    policy: RoutingPolicy


class RoutingImportOut(BaseModel):
    policy: RoutingPolicy
    source_rules: int
    imported_rules: int


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _outbound_action(value: object, location: str) -> str:
    if not isinstance(value, str) or value not in OUTBOUND_ACTIONS:
        raise ValueError(location + ": outbound must be direct, subscription or proxy")
    return OUTBOUND_ACTIONS[value]


def import_singbox_routing(payload: RoutingImport) -> RoutingImportOut:
    if len(payload.content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("File exceeds 1 MiB")
    try:
        document = json.loads(payload.content.lstrip("\ufeff"), object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid JSON: use a UTF-8 sing-box JSON configuration") from error
    route = document.get("route", document) if isinstance(document, dict) else document
    rules = route.get("rules") if isinstance(route, dict) else route
    if not isinstance(rules, list):
        raise ValueError("Expected route.rules, a route object with rules, or a rules array")
    if len(rules) > 200:
        raise ValueError("At most 200 source rules can be imported")
    converted: list[RoutingRule] = []
    for index, rule in enumerate(rules):
        location = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise ValueError(location + ": expected an object")
        unsupported = set(rule) - {*MATCH_FIELDS, "action", "outbound", "type"}
        if unsupported:
            raise ValueError(location + ": unsupported fields: " + ", ".join(sorted(unsupported)))
        if rule.get("type", "default") != "default":
            raise ValueError(location + ": only default destination rules are supported")
        action = rule.get("action", "route")
        if action == "route":
            target_action = _outbound_action(rule.get("outbound"), location)
        elif action == "reject" and "outbound" not in rule:
            target_action = "reject"
        else:
            raise ValueError(location + ": use route with outbound, or reject without outbound")
        match_count = 0
        for kind in MATCH_FIELDS:
            if kind not in rule:
                continue
            patterns = rule[kind]
            if isinstance(patterns, str):
                patterns = [patterns]
            if not isinstance(patterns, list) or not patterns:
                raise ValueError(location + "." + kind + ": expected a string or non-empty array")
            for pattern in patterns:
                if not isinstance(pattern, str):
                    raise ValueError(location + "." + kind + ": patterns must be strings")
                try:
                    converted.append(RoutingRule(kind=kind, pattern=pattern, action=target_action))
                except ValidationError as error:
                    raise ValueError(location + "." + kind + ": invalid domain or CIDR") from error
                if len(converted) > 200:
                    raise ValueError("Expanded rules exceed the 200-rule limit")
                match_count += 1
        if not match_count:
            raise ValueError(location + ": a destination matcher is required; use route.final")
    updated = payload.policy.model_dump()
    updated["rules"] = payload.policy.rules + converted if payload.mode == "append" else converted
    if len(updated["rules"]) > 200:
        raise ValueError("Combined rules exceed the 200-rule limit")
    if isinstance(route, dict) and "final" in route:
        updated["final"] = _outbound_action(route["final"], "route.final")
    return RoutingImportOut(
        policy=RoutingPolicy.model_validate(updated),
        source_rules=len(rules),
        imported_rules=len(converted),
    )
