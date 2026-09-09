import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from .config import PROXY_LISTEN_PORT, Settings
from .models import DOCUMENT_MODELS
from .services.crypto import sign_bundle

logger = logging.getLogger(__name__)

# These collections predate the unique idempotency indexes introduced for
# telemetry upload. Beanie's default naming is intentionally retained so the
# cleanup runs against the same existing collections before init_beanie builds
# their indexes.
TELEMETRY_BATCH_COLLECTION = "TelemetryBatch"
TELEMETRY_CURSOR_COLLECTION = "TelemetryCursor"
SITE_COLLECTION = "Site"
SUBSCRIPTION_SOURCE_COLLECTION = "SubscriptionSource"
LEGACY_PROXY_CREDENTIAL_COLLECTION = "ProxyCredential"
DESIRED_RELEASE_COLLECTION = "DesiredRelease"
CONFIG_DRAFT_COLLECTION = "ConfigDraft"


async def _migrate_retired_proxy_state(database: Any, settings: Settings) -> None:
    """Erase retired proxy Basic-auth state and pin bundles to port 1080.

    This runs before Beanie opens collections so no handler can expose a
    retired credential. Legacy desired bundles are re-signed after the field
    is stripped, allowing a still-pending release to apply without restoring
    a removed authentication configuration.
    """

    sites = database[SITE_COLLECTION]
    updated = await sites.update_many(
        {},
        {
            "$unset": {"http_port": "", "proxy_auth_required": ""},
        },
    )
    credentials = database[LEGACY_PROXY_CREDENTIAL_COLLECTION]
    dropped = await credentials.drop()
    scrubbed_releases = 0
    desired_releases = database[DESIRED_RELEASE_COLLECTION]
    async for release in desired_releases.find({}):
        bundle = release.get("bundle")
        if not isinstance(bundle, dict):
            continue
        legacy_auth = "proxy_auth" in bundle
        listen = bundle.get("listen")
        current_port = listen.get("http_port") if isinstance(listen, dict) else None
        if not legacy_auth and current_port == PROXY_LISTEN_PORT:
            continue
        bundle.pop("proxy_auth", None)
        bundle["listen"] = {"http_port": PROXY_LISTEN_PORT}
        signed = sign_bundle(bundle, settings.bundle_hmac_secret)
        await desired_releases.update_one(
            {"_id": release["_id"]},
            {"$set": {"bundle": signed, "bundle_hash": signed["bundle_hash"]}},
        )
        scrubbed_releases += 1
    drafts = database[CONFIG_DRAFT_COLLECTION]
    scrubbed_drafts = await drafts.update_many(
        {
            "$or": [
                {"diff.proxy_auth": {"$exists": True}},
                {"validation.proxy_auth": {"$exists": True}},
            ]
        },
        {"$unset": {"diff.proxy_auth": "", "validation.proxy_auth": ""}},
    )
    if updated.modified_count or dropped or scrubbed_releases or scrubbed_drafts.modified_count:
        logger.info(
            "Removed retired proxy state; migrated_sites=%d bundles=%d drafts=%d",
            updated.modified_count,
            scrubbed_releases,
            scrubbed_drafts.modified_count,
        )


async def _migrate_subscription_source_types(database: Any) -> None:
    """Classify local imports created before source types were persisted."""

    sources = database[SUBSCRIPTION_SOURCE_COLLECTION]
    http = await sources.update_many(
        {"source_type": {"$exists": False}, "url": {"$ne": ""}},
        {"$set": {"source_type": "http"}},
    )
    local = await sources.update_many(
        {"source_type": {"$exists": False}},
        {"$set": {"source_type": "upload"}},
    )
    if http.modified_count or local.modified_count:
        logger.info(
            "Classified legacy subscription sources; http=%d local=%d",
            http.modified_count,
            local.modified_count,
        )


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
        await _migrate_retired_proxy_state(database, self.settings)
        await _migrate_subscription_source_types(database)
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
