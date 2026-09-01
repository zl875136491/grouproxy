import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from .config import Settings
from .models import DOCUMENT_MODELS

logger = logging.getLogger(__name__)

# These collections predate the unique idempotency indexes introduced for
# telemetry upload. Beanie's default naming is intentionally retained so the
# cleanup runs against the same existing collections before init_beanie builds
# their indexes.
TELEMETRY_BATCH_COLLECTION = "TelemetryBatch"
TELEMETRY_CURSOR_COLLECTION = "TelemetryCursor"


async def _deduplicate_markers(
    collection: Any,
    *,
    keys: list[str],
    sort: dict[str, int],
) -> int:
    """Keep the newest marker for each future unique-index key.

    Marker documents only record an already-accepted telemetry batch or cursor;
    the access logs, connection snapshots, and probe data live in separate
    collections. Removing duplicate markers is therefore a conservative
    compatibility migration, not a deletion of operational data.
    """

    group_key = {key: f"${key}" for key in keys}
    cursor = collection.aggregate(
        [
            {"$sort": sort},
            {
                "$group": {
                    "_id": group_key,
                    "ids": {"$push": "$_id"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]
    )
    removed = 0
    async for group in cursor:
        ids = group.get("ids", [])
        duplicates = ids[1:]
        if not duplicates:
            continue
        result = await collection.delete_many({"_id": {"$in": duplicates}})
        removed += result.deleted_count
    return removed


async def _prepare_telemetry_indexes(database: Any) -> None:
    """Reconcile legacy telemetry markers before Beanie creates unique indexes."""

    batches = database[TELEMETRY_BATCH_COLLECTION]
    duplicate_batches = await _deduplicate_markers(
        batches,
        keys=["node_id", "kind", "batch_id"],
        sort={"received_at": -1, "_id": -1},
    )
    duplicate_sequences = await _deduplicate_markers(
        batches,
        keys=["node_id", "kind", "sequence"],
        sort={"received_at": -1, "_id": -1},
    )
    cursors = database[TELEMETRY_CURSOR_COLLECTION]
    duplicate_cursors = await _deduplicate_markers(
        cursors,
        keys=["node_id", "kind"],
        sort={"last_sequence": -1, "updated_at": -1, "_id": -1},
    )
    if duplicate_batches or duplicate_sequences or duplicate_cursors:
        logger.warning(
            "Reconciled legacy telemetry markers before unique indexes: "
            "batches=%d sequences=%d cursors=%d",
            duplicate_batches,
            duplicate_sequences,
            duplicate_cursors,
        )


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: AsyncIOMotorClient | None = None

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(
            self.settings.mongodb_url,
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
        )
        await self.client.admin.command("ping")
        database = self.client[self.settings.mongodb_database]
        await _prepare_telemetry_indexes(database)
        await init_beanie(
            database=database,
            document_models=DOCUMENT_MODELS,
        )

    async def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


@asynccontextmanager
async def database_lifespan(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
