from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from app.services.release_categories import legacy_release_categories, release_change_categories


@pytest.mark.parametrize(
    "diff, expected",
    [
        ({"blacklist": {}}, ["access_rules"]),
        ({"access_control": []}, ["access_rules"]),
        ({"routing_policy": {}}, ["routing_rules"]),
        ({"proxy_selection": {}}, ["outbound_service"]),
        ({"subscription": {}}, ["subscription"]),
        (
            {"routing_policy": {}, "subscription": {}, "note": "note"},
            ["routing_rules", "subscription"],
        ),
        ({"note": "subscription"}, []),
        (None, []),
    ],
)
def test_categories_follow_changed_fields(diff, expected):
    assert release_change_categories(diff) == expected


@pytest.mark.parametrize(
    "key, expected",
    [
        ("routing:site:2:random", ["routing_rules"]),
        ("source-blacklist:updated:rule:site:random", ["access_rules"]),
        ("proxy-selection:node:subscription:auto:latest", ["outbound_service"]),
        ("subscription.publish:version:site:random", ["subscription"]),
        ("subscription.rollback:version:site:random", ["subscription"]),
        ("operator-custom-key", []),
    ],
)
def test_legacy_task_keys(key, expected):
    assert legacy_release_categories(key) == expected


@pytest.mark.asyncio
async def test_history_enrichment_preserves_explicit_categories_and_batches_legacy_tasks(
    monkeypatch,
):
    finder = AsyncMock(
        return_value=[
            SimpleNamespace(task_id="old-task", idempotency_key="routing:site:2:random"),
            SimpleNamespace(task_id="unknown-task", idempotency_key="custom"),
        ]
    )
    monkeypatch.setattr(main.Task, "find", lambda query: SimpleNamespace(to_list=finder))
    categories = await main._release_categories_for_history(
        [
            SimpleNamespace(
                release_id="new", task_id="new-task", change_categories=["subscription"]
            ),
            SimpleNamespace(release_id="old", task_id="old-task"),
            SimpleNamespace(release_id="unknown", task_id="unknown-task"),
        ]
    )
    assert categories == {"new": ["subscription"], "old": ["routing_rules"], "unknown": []}
    finder.assert_awaited_once()
