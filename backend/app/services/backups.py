"""Control-plane backup artifacts with integrity verification.

Backups are deliberately produced from MongoDB collections instead of shelling
out to ``mongodump``.  Each archive contains a manifest and per-collection
hashes, is written atomically with restrictive permissions, and can optionally
be encrypted with an operator-provided key.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bson import json_util
from pymongo import ReplaceOne

from ..config import Settings
from ..models import DOCUMENT_MODELS, BackupRecord

BACKUP_SCHEMA_VERSION = 2
SUPPORTED_BACKUP_SCHEMA_VERSIONS = frozenset({1, BACKUP_SCHEMA_VERSION})
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_MEMBER_BYTES * 8
RESTORE_BATCH_SIZE = 1_000
_BACKUP_KEY_ENV = "GROUPROXY_INTERNAL_BACKUP_KEY"
BACKUP_READY_STATUSES = frozenset({"verified", "rehearsed", "restored"})


class BackupError(Exception):
    """A safe, user-facing backup failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BackupArtifact:
    filename: str
    checksum: str
    encrypted: bool
    size_bytes: int
    manifest: dict[str, Any]


def backup_schedule_key(*, scope: str, interval_seconds: int, at: datetime) -> str:
    """Return a deterministic scheduler key for one backup interval.

    MongoDB's idempotency index is the durable scheduler state: restarting the
    backend in the same interval cannot enqueue another automatic archive.
    """

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    current = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    slot = int(current.timestamp() // interval_seconds)
    return f"backup.schedule:{scope}:{slot}"


def rehearsal_schedule_key(
    *, backup_id: str, interval_seconds: int, at: datetime
) -> str:
    """Return an idempotency key for a non-destructive restore rehearsal."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    current = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    slot = int(current.timestamp() // interval_seconds)
    return f"backup.rehearsal.schedule:{backup_id}:{slot}"


def retained_scheduled_backup_ids(
    records: list[BackupRecord],
    *,
    daily_days: int,
    weekly_weeks: int,
    monthly_months: int,
) -> set[str]:
    """Choose the newest automatic archive for each configured time bucket.

    Manual archives are intentionally excluded: an operator-created snapshot
    must never disappear because of an automatic retention job. The caller is
    responsible for excluding active restore tasks before deleting candidates.
    """

    candidates = [
        record
        for record in records
        if record.origin == "scheduled"
        and record.storage_ref
        and record.status in BACKUP_READY_STATUSES
    ]
    candidates.sort(
        key=lambda record: (
            record.created_at
            if record.created_at.tzinfo is not None
            else record.created_at.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )
    retained: set[str] = set()

    def retain_distinct(limit: int, bucket: Callable[[datetime], object]) -> None:
        if limit <= 0:
            return
        seen: set[object] = set()
        for record in candidates:
            current = record.created_at
            current = (
                current
                if current.tzinfo is not None
                else current.replace(tzinfo=timezone.utc)
            )
            key = bucket(current.astimezone(timezone.utc))
            if key in seen:
                continue
            seen.add(key)
            retained.add(record.backup_id)
            if len(seen) >= limit:
                return

    retain_distinct(daily_days, lambda current: current.date())
    retain_distinct(weekly_weeks, lambda current: current.isocalendar()[:2])
    retain_distinct(monthly_months, lambda current: (current.year, current.month))
    return retained


def backup_root(settings: Settings) -> Path:
    """Resolve and create the configured backup directory."""

    configured = settings.backup_directory.strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "grouproxy-backups"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        # Some mounted filesystems do not support chmod; the file-level mode is
        # still enforced below and the caller can surface the deployment issue.
        pass
    return root.resolve()


def _secret(settings: Settings) -> str:
    value = settings.backup_encryption_key
    return value.get_secret_value() if value is not None else ""


def _member_limit(settings: Settings) -> int:
    """Return the bounded member size while supporting pre-setting runtimes."""

    return int(getattr(settings, "backup_member_max_bytes", MAX_MEMBER_BYTES))


def _archive_limit(settings: Settings) -> int:
    """Return a whole-archive expansion limit no smaller than one member."""

    return max(
        _member_limit(settings),
        int(getattr(settings, "backup_archive_max_bytes", MAX_ARCHIVE_BYTES)),
    )


def _crypt(data: bytes, key: str, *, decrypt: bool = False) -> bytes:
    if not shutil.which("openssl"):
        raise BackupError("backup_openssl_unavailable")
    if not key:
        raise BackupError("backup_encryption_key_missing")
    command = [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "-iter",
        "100000",
        "-salt",
        "-pass",
        f"env:{_BACKUP_KEY_ENV}",
    ]
    if decrypt:
        command.insert(2, "-d")
    environment = os.environ.copy()
    environment[_BACKUP_KEY_ENV] = key
    try:
        result = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("backup_encryption_failed") from exc
    if result.returncode != 0:
        raise BackupError("backup_decryption_failed" if decrypt else "backup_encryption_failed")
    return result.stdout


def _tar_member(name: str, payload: bytes | int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = payload if isinstance(payload, int) else len(payload)
    info.mode = 0o600
    info.mtime = 0
    return info


def _chunk_member_name(collection_name: str, index: int) -> str:
    return f"collections/{collection_name}/{index:08d}.jsonl"


def _document_line(document: dict[str, Any]) -> bytes:
    return (
        json_util.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


async def _write_collection_chunks(
    archive: tarfile.TarFile,
    *,
    name: str,
    collection: Any,
    member_limit: int,
) -> dict[str, Any]:
    """Add a collection as bounded JSONL members and return its manifest entry."""

    chunk_index = 0
    chunk_size = 0
    chunk_documents = 0
    document_count = 0
    total_size = 0
    whole_hash = hashlib.sha256()
    chunk_hash = hashlib.sha256()
    chunks: list[dict[str, Any]] = []

    def new_chunk_file() -> tempfile.SpooledTemporaryFile[bytes]:
        # Keep small collections in memory and spill large telemetry chunks to
        # disk before archive compression. This prevents collection growth from
        # multiplying the backend's resident memory usage.
        return tempfile.SpooledTemporaryFile(
            max_size=min(member_limit, 8 * 1024 * 1024), mode="w+b"
        )

    chunk_file = new_chunk_file()

    def flush_chunk() -> None:
        nonlocal chunk_documents, chunk_file, chunk_hash, chunk_index, chunk_size
        if not chunk_documents:
            return
        member_name = _chunk_member_name(name, chunk_index)
        chunk_file.seek(0)
        archive.addfile(_tar_member(member_name, chunk_size), chunk_file)
        chunks.append(
            {
                "member": member_name,
                "documents": chunk_documents,
                "size_bytes": chunk_size,
                "sha256": chunk_hash.hexdigest(),
            }
        )
        chunk_file.close()
        chunk_file = new_chunk_file()
        chunk_index += 1
        chunk_size = 0
        chunk_documents = 0
        chunk_hash = hashlib.sha256()

    try:
        cursor = collection.find({}).sort("_id", 1)
        async for document in cursor:
            line = _document_line(document)
            if len(line) > member_limit:
                raise BackupError("backup_document_too_large")
            if chunk_documents and chunk_size + len(line) > member_limit:
                flush_chunk()
            chunk_file.write(line)
            chunk_hash.update(line)
            whole_hash.update(line)
            chunk_size += len(line)
            chunk_documents += 1
            document_count += 1
            total_size += len(line)
        flush_chunk()
        if not chunks:
            member_name = _chunk_member_name(name, 0)
            archive.addfile(_tar_member(member_name, b""), io.BytesIO())
            chunks.append(
                {
                    "member": member_name,
                    "documents": 0,
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            )
    finally:
        chunk_file.close()

    return {
        "documents": document_count,
        "size_bytes": total_size,
        "sha256": whole_hash.hexdigest(),
        "chunks": chunks,
    }


async def _archive_bytes(*, scope: str, settings: Settings) -> tuple[bytes, dict[str, Any]]:
    collections: dict[str, dict[str, Any]] = {}
    member_limit = _member_limit(settings)
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for model in DOCUMENT_MODELS:
                collection = model.get_motor_collection()
                name = collection.name
                if name in collections:
                    raise BackupError("backup_duplicate_collection")
                collections[name] = await _write_collection_chunks(
                    archive,
                    name=name,
                    collection=collection,
                    member_limit=member_limit,
                )
            manifest: dict[str, Any] = {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "scope": scope,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "collections": collections,
            }
            manifest_payload = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            archive.addfile(
                _tar_member("manifest.json", manifest_payload),
                io.BytesIO(manifest_payload),
            )
        stream.seek(0)
        return stream.read(), manifest


async def create_backup_artifact(
    *, backup_id: str, scope: str, settings: Settings
) -> BackupArtifact:
    if scope != "control_plane":
        raise BackupError("backup_scope_not_supported")
    encryption_key = _secret(settings)
    if settings.environment not in {"development", "test"} and not encryption_key:
        raise BackupError("backup_encryption_required")
    archive, manifest = await _archive_bytes(scope=scope, settings=settings)
    encrypted = bool(encryption_key)
    output = _crypt(archive, encryption_key) if encrypted else archive
    root = backup_root(settings)
    suffix = ".tar.gz.enc" if encrypted else ".tar.gz"
    filename = f"{backup_id}{suffix}"
    destination = root / filename
    temporary = root / f".{filename}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(destination)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BackupError("backup_write_failed") from exc
    return BackupArtifact(
        filename=filename,
        checksum=hashlib.sha256(output).hexdigest(),
        encrypted=encrypted,
        size_bytes=len(output),
        manifest=manifest,
    )


def _artifact_path(settings: Settings, record: BackupRecord) -> Path:
    root = backup_root(settings)
    reference = record.storage_ref or (record.artifact_paths[0] if record.artifact_paths else "")
    if not reference:
        raise BackupError("backup_artifact_missing")
    candidate = (root / reference).resolve()
    if candidate.parent != root or candidate.name != reference:
        raise BackupError("backup_artifact_path_invalid")
    return candidate


def delete_backup_artifact(*, settings: Settings, record: BackupRecord) -> None:
    """Delete one verified artifact after the retention decision is durable.

    ``_artifact_path`` rejects traversal and references outside the configured
    backup root, so the scheduler cannot be tricked into deleting arbitrary
    files through a corrupted database record.
    """

    path = _artifact_path(settings, record)
    try:
        path.unlink()
    except FileNotFoundError:
        # A previous interrupted retention pass already removed the payload.
        return
    except OSError as exc:
        raise BackupError("backup_retention_delete_failed") from exc


def _collection_name_from_member(member_name: str) -> str | None:
    if not member_name.startswith("collections/") or not member_name.endswith(".jsonl"):
        return None
    parts = member_name.removeprefix("collections/").split("/")
    if len(parts) == 1:
        name = parts[0].removesuffix(".jsonl")
        return name or None
    if len(parts) == 2:
        name, chunk = parts
        chunk_index = chunk.removesuffix(".jsonl")
        if name and len(chunk_index) == 8 and chunk_index.isdecimal():
            return name
    return None


def _validate_collection_metadata(
    metadata: Any, *, documents: int, size_bytes: int, digest: str
) -> None:
    if not isinstance(metadata, dict):
        raise BackupError("backup_manifest_invalid")
    expected_documents = metadata.get("documents")
    if not isinstance(expected_documents, int) or expected_documents < 0:
        raise BackupError("backup_manifest_invalid")
    if expected_documents != documents:
        raise BackupError("backup_collection_count_mismatch")
    if metadata.get("size_bytes") != size_bytes:
        raise BackupError("backup_collection_size_mismatch")
    if metadata.get("sha256") != digest:
        raise BackupError("backup_collection_checksum_mismatch")


def _validate_v1_archive(
    collection_manifest: dict[str, Any], files: dict[str, bytes]
) -> dict[str, list[str]]:
    expected_members: set[str] = set()
    members_by_collection: dict[str, list[str]] = {}
    for name, metadata in collection_manifest.items():
        if not isinstance(name, str) or not name or "/" in name:
            raise BackupError("backup_manifest_invalid")
        member_name = f"collections/{name}.jsonl"
        expected_members.add(member_name)
        content = files.get(member_name)
        if content is None:
            raise BackupError("backup_collection_missing")
        _validate_collection_metadata(
            metadata,
            documents=sum(1 for line in content.splitlines() if line),
            size_bytes=len(content),
            digest=hashlib.sha256(content).hexdigest(),
        )
        members_by_collection[name] = [member_name]
    if set(files) != expected_members:
        raise BackupError("backup_collection_set_mismatch")
    return members_by_collection


def _validate_v2_archive(
    collection_manifest: dict[str, Any], files: dict[str, bytes]
) -> dict[str, list[str]]:
    expected_members: set[str] = set()
    members_by_collection: dict[str, list[str]] = {}
    for name, metadata in collection_manifest.items():
        if not isinstance(name, str) or not name or "/" in name or not isinstance(metadata, dict):
            raise BackupError("backup_manifest_invalid")
        chunks = metadata.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise BackupError("backup_manifest_invalid")
        member_names: list[str] = []
        collection_hash = hashlib.sha256()
        collection_size = 0
        collection_documents = 0
        for index, chunk_metadata in enumerate(chunks):
            if not isinstance(chunk_metadata, dict):
                raise BackupError("backup_manifest_invalid")
            member_name = _chunk_member_name(name, index)
            if chunk_metadata.get("member") != member_name or member_name in expected_members:
                raise BackupError("backup_manifest_invalid")
            content = files.get(member_name)
            if content is None:
                raise BackupError("backup_collection_missing")
            documents = sum(1 for line in content.splitlines() if line)
            _validate_collection_metadata(
                chunk_metadata,
                documents=documents,
                size_bytes=len(content),
                digest=hashlib.sha256(content).hexdigest(),
            )
            expected_members.add(member_name)
            member_names.append(member_name)
            collection_hash.update(content)
            collection_size += len(content)
            collection_documents += documents
        _validate_collection_metadata(
            metadata,
            documents=collection_documents,
            size_bytes=collection_size,
            digest=collection_hash.hexdigest(),
        )
        members_by_collection[name] = member_names
    if set(files) != expected_members:
        raise BackupError("backup_collection_set_mismatch")
    return members_by_collection


def _read_archive(
    settings: Settings, record: BackupRecord
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, list[str]]]:
    path = _artifact_path(settings, record)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BackupError("backup_artifact_missing") from exc
    if record.checksum and hashlib.sha256(payload).hexdigest() != record.checksum:
        raise BackupError("backup_checksum_mismatch")
    if record.encrypted:
        payload = _crypt(payload, _secret(settings), decrypt=True)
    files: dict[str, bytes] = {}
    member_limit = _member_limit(settings)
    archive_limit = _archive_limit(settings)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            total_size = 0
            for member in members:
                is_manifest = member.name == "manifest.json"
                is_collection = _collection_name_from_member(member.name) is not None
                if not member.isreg() or not (is_manifest or is_collection):
                    raise BackupError("backup_member_invalid")
                if member.name in files:
                    raise BackupError("backup_member_duplicate")
                if member.size > member_limit:
                    raise BackupError("backup_member_too_large")
                total_size += member.size
                if total_size > archive_limit:
                    raise BackupError("backup_archive_too_large")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup_member_invalid")
                files[member.name] = extracted.read(member_limit + 1)
                if len(files[member.name]) > member_limit:
                    raise BackupError("backup_member_too_large")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError("backup_archive_invalid") from exc
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("backup_manifest_invalid") from exc
    schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if (
        not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_BACKUP_SCHEMA_VERSIONS
    ):
        raise BackupError("backup_manifest_invalid")
    collection_manifest = manifest.get("collections")
    if not isinstance(collection_manifest, dict):
        raise BackupError("backup_manifest_invalid")
    if schema_version == 1:
        members_by_collection = _validate_v1_archive(collection_manifest, files)
    else:
        members_by_collection = _validate_v2_archive(collection_manifest, files)
    return manifest, files, members_by_collection


async def verify_backup(*, settings: Settings, record: BackupRecord) -> dict[str, Any]:
    manifest, _, members_by_collection = await asyncio.to_thread(
        _read_archive, settings, record
    )
    return {
        "scope": manifest.get("scope", ""),
        "schema_version": manifest.get("schema_version"),
        "collections": len(members_by_collection),
        "documents": sum(
            int(item.get("documents", 0))
            for item in manifest.get("collections", {}).values()
            if isinstance(item, dict)
        ),
    }


async def _apply_restore_batch(collection: Any, operations: list[ReplaceOne]) -> int:
    result = await collection.bulk_write(operations, ordered=False)
    return result.matched_count + result.upserted_count


async def restore_backup(
    *, settings: Settings, record: BackupRecord, apply_changes: bool
) -> dict[str, Any]:
    manifest, files, members_by_collection = await asyncio.to_thread(
        _read_archive, settings, record
    )
    model_by_collection = {
        model.get_motor_collection().name: model for model in DOCUMENT_MODELS
    }
    summary: dict[str, Any] = {
        "mode": "restore" if apply_changes else "rehearsal",
        "collections": len(members_by_collection),
        "documents": 0,
        "applied": 0,
        "skipped": [],
    }
    for collection_name, member_names in sorted(members_by_collection.items()):
        model = model_by_collection.get(collection_name)
        if model is None:
            raise BackupError("backup_collection_unknown")
        # Do not overwrite the live task or backup record that is executing the
        # restore. Other collections are upserted by their stable Mongo _id;
        # this is intentionally non-destructive and leaves newer documents
        # available for an operator to reconcile.
        should_apply = apply_changes and collection_name not in {
            TaskCollectionName,
            BackupCollectionName,
        }
        if apply_changes and not should_apply:
            summary["skipped"].append(collection_name)
        collection = model.get_motor_collection() if should_apply else None
        operations: list[ReplaceOne] = []

        for member_name in member_names:
            for line in files[member_name].splitlines():
                if not line:
                    continue
                value = json_util.loads(line)
                if not isinstance(value, dict) or "_id" not in value:
                    raise BackupError("backup_document_invalid")
                summary["documents"] += 1
                if should_apply:
                    operations.append(ReplaceOne({"_id": value["_id"]}, value, upsert=True))
                    if len(operations) >= RESTORE_BATCH_SIZE:
                        summary["applied"] += await _apply_restore_batch(collection, operations)
                        operations.clear()
                elif summary["documents"] % RESTORE_BATCH_SIZE == 0:
                    # A rehearsal parses potentially millions of documents
                    # without database writes. Yield between batches so agent
                    # heartbeats and API requests stay responsive.
                    await asyncio.sleep(0)
        if operations and collection is not None:
            summary["applied"] += await _apply_restore_batch(collection, operations)
    summary["manifest_scope"] = manifest.get("scope", "")
    return summary


# Collection names are resolved at runtime by Beanie; these defaults match its
# snake-case naming and keep restore safeguards readable before initialization.
TaskCollectionName = "task"
BackupCollectionName = "backup_record"
