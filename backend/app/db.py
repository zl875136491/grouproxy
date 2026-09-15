import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from .config import PROXY_LISTEN_PORT, Settings
from .models import DOCUMENT_MODELS
from .services.cidr import normalize_source_blacklist_pattern
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
SOURCE_BLACKLIST_COLLECTION = "SourceBlacklist"
RETIRED_POLICY_COLLECTIONS = (
    "SiteCIDR",
    "TravelException",
    "CrossSiteAllow",
    "DestinationBlacklist",
)
RETIRED_POLICY_FIELDS = frozenset(
    {
        "allow_cidrs",
        "deny_destinations",
        "deny_sources",
        "acl_note",
        "acl_sources",
        "effective_cidrs",
    }
)


def _strip_retired_policy_fields(value: Any) -> Any:
    """Remove the policy model retired in favour of source blacklists."""

    if isinstance(value, dict):
        return {
            key: _strip_retired_policy_fields(item)
            for key, item in value.items()
            if key not in RETIRED_POLICY_FIELDS
        }
    if isinstance(value, list):
        return [_strip_retired_policy_fields(item) for item in value]
    return value


def _normalized_source_blacklist_document(
    document: dict[str, Any], *, known_site_ids: set[str]
) -> dict[str, Any] | None:
    """Return a canonical source rule, or drop an unsafe legacy document.

    Beanie validates ``SourceBlacklist`` documents when it reads them. A row
    written by an older build or directly into MongoDB can otherwise make an
    entire policy query fail before the request handler has a chance to reject
    the bad value. Source access is intentionally allow-all by default, so an
    unrecognised deny record must not become an implicit blocker.
    """

    scope = document.get("scope")
    kind = document.get("kind")
    if scope not in {"global", "site"} or kind not in {"ip", "network", "domain"}:
        return None
    try:
        pattern = normalize_source_blacklist_pattern(kind, document.get("pattern"))
    except (TypeError, ValueError):
        return None

    raw_site_id = document.get("site_id")
    if scope == "global":
        # The two scopes have mutually exclusive targets. Do not guess whether
        # an inconsistent row was intended to be global or site-scoped: source
        # policy must not widen into an all-site deny during migration.
        if raw_site_id is not None:
            return None
        site_id: str | None = None
    else:
        if not isinstance(raw_site_id, str):
            return None
        site_id = raw_site_id.strip()
        if not site_id or site_id not in known_site_ids:
            return None

    raw_enabled = document.get("enabled", True)
    if not isinstance(raw_enabled, bool):
        return None
    raw_comment = document.get("comment", "")
    comment = raw_comment.strip()[:512] if isinstance(raw_comment, str) else ""
    raw_created_by = document.get("created_by", "system")
    created_by = raw_created_by.strip()[:128] if isinstance(raw_created_by, str) else "system"
    if not created_by:
        created_by = "system"
    created_at = document.get("created_at")
    if not isinstance(created_at, datetime):
        created_at = datetime.now(timezone.utc)
    return {
        "scope": scope,
        "site_id": site_id,
        "kind": kind,
        "pattern": pattern,
        "comment": comment,
        "enabled": raw_enabled,
        "created_by": created_by,
        "created_at": created_at,
    }


async def _migrate_source_blacklist_state(database: Any) -> tuple[int, int]:
    """Normalize valid source rules and remove malformed/orphaned duplicates.

    This runs before Beanie opens the collection, which makes the modern flat
    blacklist contract resilient to corrupted historical documents. The first
    valid rule for a canonical key is retained; later duplicates are removed
    before Beanie attempts to create its unique index.
    """

    collections = set(await database.list_collection_names())
    if SOURCE_BLACKLIST_COLLECTION not in collections:
        return 0, 0

    known_site_ids = {
        str(site["_id"])
        async for site in database[SITE_COLLECTION].find({})
        if site.get("_id") is not None
    }
    rules = database[SOURCE_BLACKLIST_COLLECTION]
    normalized_count = 0
    removed_count = 0
    seen: set[tuple[str, str | None, str, str]] = set()
    async for document in rules.find({}):
        normalized = _normalized_source_blacklist_document(
            document, known_site_ids=known_site_ids
        )
        document_id = document.get("_id")
        if normalized is None:
            await rules.delete_one({"_id": document_id})
            removed_count += 1
            continue
        key = (
            str(normalized["scope"]),
            normalized["site_id"],
            str(normalized["kind"]),
            str(normalized["pattern"]),
        )
        if key in seen:
            await rules.delete_one({"_id": document_id})
            removed_count += 1
            continue
        seen.add(key)
        changes = {
            field: value
            for field, value in normalized.items()
            if document.get(field) != value
        }
        if changes:
            await rules.update_one({"_id": document_id}, {"$set": changes})
            normalized_count += 1
    if normalized_count or removed_count:
        logger.warning(
            "Normalized source blacklist state before startup; updated=%d removed=%d",
            normalized_count,
            removed_count,
        )
    return normalized_count, removed_count


async def _migrate_retired_policy_state(database: Any, settings: Settings) -> None:
    """Erase retired proxy and policy state before handlers can expose it.

    This runs before Beanie opens collections. Existing desired bundles are
    normalized and re-signed so an installation upgraded from the old CIDR,
    exception, cross-site, or destination-policy model cannot restore that
    model through a pending bundle or a monitor restart.
    """

    sites = database[SITE_COLLECTION]
    updated = await sites.update_many(
        {},
        {
            "$unset": {"http_port": "", "proxy_auth_required": ""},
        },
    )
    collections = set(await database.list_collection_names())
    dropped_collections: list[str] = []
    if LEGACY_PROXY_CREDENTIAL_COLLECTION in collections:
        await database[LEGACY_PROXY_CREDENTIAL_COLLECTION].drop()
        dropped_collections.append(LEGACY_PROXY_CREDENTIAL_COLLECTION)
    for collection_name in RETIRED_POLICY_COLLECTIONS:
        if collection_name in collections:
            await database[collection_name].drop()
            dropped_collections.append(collection_name)
    scrubbed_releases = 0
    desired_releases = database[DESIRED_RELEASE_COLLECTION]
    async for release in desired_releases.find({}):
        bundle = release.get("bundle")
        if not isinstance(bundle, dict):
            continue
        normalized = _strip_retired_policy_fields(bundle)
        normalized.pop("proxy_auth", None)
        listen = normalized.get("listen")
        if not isinstance(listen, dict) or listen.get("http_port") != PROXY_LISTEN_PORT:
            normalized["listen"] = {"http_port": PROXY_LISTEN_PORT}
        # The modern bundle contract has a single flat source blacklist. An
        # old bundle becomes an explicit empty blacklist, which is allow-all.
        if not isinstance(normalized.get("source_blacklist"), list):
            normalized["source_blacklist"] = []
        normalized["min_monitor_version"] = "0.5.0"
        if normalized == bundle:
            continue
        signed = sign_bundle(normalized, settings.bundle_hmac_secret)
        await desired_releases.update_one(
            {"_id": release["_id"]},
            {"$set": {"bundle": signed, "bundle_hash": signed["bundle_hash"]}},
        )
        scrubbed_releases += 1
    drafts = database[CONFIG_DRAFT_COLLECTION]
    scrubbed_drafts = 0
    async for draft in drafts.find({}):
        diff = _strip_retired_policy_fields(draft.get("diff", {}))
        validation = _strip_retired_policy_fields(draft.get("validation", {}))
        if isinstance(diff, dict):
            diff.pop("proxy_auth", None)
        if isinstance(validation, dict):
            validation.pop("proxy_auth", None)
        if diff == draft.get("diff", {}) and validation == draft.get("validation", {}):
            continue
        await drafts.update_one(
            {"_id": draft["_id"]},
            {"$set": {"diff": diff, "validation": validation}},
        )
        scrubbed_drafts += 1
    if updated.modified_count or dropped_collections or scrubbed_releases or scrubbed_drafts:
        logger.info(
            "Removed retired policy state; migrated_sites=%d bundles=%d drafts=%d collections=%s",
            updated.modified_count,
            scrubbed_releases,
            scrubbed_drafts,
            ",".join(dropped_collections) or "none",
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
        await _migrate_retired_policy_state(database, self.settings)
        await _migrate_source_blacklist_state(database)
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
