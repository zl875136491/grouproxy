import re
from datetime import datetime, timedelta, timezone

import pytest

import main as main_module
from app.db import _migrate_connection_snapshot_retention
from app.models import ConnectionSnapshot


def test_connection_routes_expose_live_and_history_contracts() -> None:
    routes = {
        (route.path, method)
        for route in main_module.app.routes
        for method in (route.methods or set())
    }

    assert ("/api/v1/connections/live", "GET") in routes
    assert ("/api/v1/connections/history", "GET") in routes


@pytest.mark.asyncio
async def test_connection_history_query_combines_audit_dimensions() -> None:
    since = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    until = since + timedelta(hours=1)

    query = await main_module._connection_history_query(
        site_id="site-north",
        node_id=None,
        since=since,
        until=until,
        source_ip="192.0.2.44",
        destination="example.com",
        network="tcp",
        outbound="edge-a",
        search="rule-1",
    )

    assert query["site_id"] == "site-north"
    assert query["sampled_at"] == {"$gte": since, "$lte": until}
    connection_match = query["connections"]["$elemMatch"]
    assert connection_match["src_ip"]["$regex"] == "192\\.0\\.2\\.44"
    assert connection_match["network"]["$regex"] == "^tcp$"
    assert connection_match["outbound_chain"]["$regex"] == re.escape("edge-a")
    assert {
        next(iter(item.values()))["$regex"]
        for item in connection_match["$or"]
    } == {re.escape("example.com")}
    assert any("connections.rule" in item for item in query["$or"])


def test_connection_snapshot_default_retention_is_audit_friendly() -> None:
    now = datetime.now(timezone.utc)
    factory = ConnectionSnapshot.model_fields["expires_at"].default_factory

    assert factory is not None
    assert factory() >= now + timedelta(days=89)


@pytest.mark.asyncio
async def test_connection_snapshot_retention_migration_uses_sample_time() -> None:
    class Result:
        modified_count = 3

    class Collection:
        def __init__(self) -> None:
            self.query = None
            self.update = None

        async def update_many(self, query, update):
            self.query = query
            self.update = update
            return Result()

    collection = Collection()

    class Database:
        def __getitem__(self, name: str):
            assert name == "ConnectionSnapshot"
            return collection

        async def list_collection_names(self) -> list[str]:
            return ["ConnectionSnapshot"]

    settings = type("Settings", (), {"connection_history_retention_days": 30})()
    assert await _migrate_connection_snapshot_retention(Database(), settings) == 3
    assert collection.query["sampled_at"] == {"$type": "date"}
    deadline = collection.update[0]["$set"]["expires_at"]["$add"]
    assert deadline[0] == "$sampled_at"
    assert deadline[1] == 30 * 24 * 60 * 60 * 1_000
