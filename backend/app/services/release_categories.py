from typing import Any

CATEGORY_FIELDS = (
    ("access_rules", {"blacklist", "access_control", "source_blacklist"}),
    ("routing_rules", {"routing_policy"}),
    ("outbound_service", {"proxy_selection"}),
    ("subscription", {"subscription"}),
)


def release_change_categories(diff: Any) -> list[str]:
    if not isinstance(diff, dict):
        return []
    return [category for category, fields in CATEGORY_FIELDS if fields.intersection(diff)]


def legacy_release_categories(idempotency_key: str) -> list[str]:
    prefixes = (
        ("source-blacklist:", "access_rules"),
        ("routing:", "routing_rules"),
        ("proxy-selection:", "outbound_service"),
        ("subscription.publish:", "subscription"),
        ("subscription.rollback:", "subscription"),
    )
    for prefix, category in prefixes:
        if idempotency_key.startswith(prefix):
            return [category]
    return []
