"""Control-plane backup artifacts with integrity verification.

Backups are deliberately produced from MongoDB collections instead of shelling
out to ``mongodump``.  Each archive contains a manifest and per-collection
hashes, is written atomically with restrictive permissions, and can optionally
be encrypted with an operator-provided key.
"""

from __future__ import annotations

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

BACKUP_SCHEMA_VERSION = 1
MAX_MEMBER_BYTES = 128 * 1024 * 1024
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


def _json_lines(documents: list[dict[str, Any]]) -> bytes:
    lines = [
        json_util.dumps(document, sort_keys=True, separators=(",", ":"))
        for document in documents
    ]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


async def _collect_collections() -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    for model in DOCUMENT_MODELS:
        collection = model.get_motor_collection()
        name = collection.name
        if name in files:
            raise BackupError("backup_duplicate_collection")
        documents: list[dict[str, Any]] = []
        async for document in collection.find({}):
            documents.append(document)
        documents.sort(key=lambda document: str(document.get("_id", "")))
        payload = _json_lines(documents)
        if len(payload) > MAX_MEMBER_BYTES:
            raise BackupError("backup_collection_too_large")
        files[name] = payload
        counts[name] = len(documents)
    return files, counts


def _tar_member(name: str, payload: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    return info


def _archive_bytes(
    files: dict[str, bytes], counts: dict[str, int], scope: str
) -> tuple[bytes, dict[str, Any]]:
    collections = {
        name: {
            "documents": counts[name],
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(files.items())
    }
    manifest: dict[str, Any] = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "scope": scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "collections": collections,
    }
    manifest_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, payload in sorted(files.items()):
            archive.addfile(
                _tar_member(f"collections/{name}.jsonl", payload), io.BytesIO(payload)
            )
        archive.addfile(
            _tar_member("manifest.json", manifest_payload), io.BytesIO(manifest_payload)
        )
    return stream.getvalue(), manifest


async def create_backup_artifact(
    *, backup_id: str, scope: str, settings: Settings
) -> BackupArtifact:
    if scope != "control_plane":
        raise BackupError("backup_scope_not_supported")
    encryption_key = _secret(settings)
    if settings.environment not in {"development", "test"} and not encryption_key:
        raise BackupError("backup_encryption_required")
    files, counts = await _collect_collections()
    archive, manifest = _archive_bytes(files, counts, scope)
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


def _read_archive(
    settings: Settings, record: BackupRecord
) -> tuple[dict[str, Any], dict[str, bytes]]:
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
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            total_size = 0
            for member in members:
                is_manifest = member.name == "manifest.json"
                is_collection = (
                    member.name.startswith("collections/")
                    and member.name.endswith(".jsonl")
                    and "/" not in member.name.removeprefix("collections/")
                )
                if not member.isreg() or not (is_manifest or is_collection):
                    raise BackupError("backup_member_invalid")
                if member.name in files:
                    raise BackupError("backup_member_duplicate")
                if member.size > MAX_MEMBER_BYTES:
                    raise BackupError("backup_member_too_large")
                total_size += member.size
                if total_size > MAX_MEMBER_BYTES * 8:
                    raise BackupError("backup_archive_too_large")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup_member_invalid")
                files[member.name] = extracted.read(MAX_MEMBER_BYTES + 1)
                if len(files[member.name]) > MAX_MEMBER_BYTES:
                    raise BackupError("backup_member_too_large")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError("backup_archive_invalid") from exc
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("backup_manifest_invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError("backup_manifest_invalid")
    collection_manifest = manifest.get("collections")
    if not isinstance(collection_manifest, dict):
        raise BackupError("backup_manifest_invalid")
    expected_members = {f"collections/{name}.jsonl" for name in collection_manifest}
    if set(files) != expected_members:
        raise BackupError("backup_collection_set_mismatch")
    for name, metadata in collection_manifest.items():
        member_name = f"collections/{name}.jsonl"
        content = files.get(member_name)
        if content is None or not isinstance(metadata, dict):
            raise BackupError("backup_collection_missing")
        if hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
            raise BackupError("backup_collection_checksum_mismatch")
        if metadata.get("size_bytes") != len(content):
            raise BackupError("backup_collection_size_mismatch")
        expected_documents = metadata.get("documents")
        if not isinstance(expected_documents, int) or expected_documents < 0:
            raise BackupError("backup_manifest_invalid")
        actual_documents = sum(1 for line in content.splitlines() if line)
        if actual_documents != expected_documents:
            raise BackupError("backup_collection_count_mismatch")
    return manifest, files


async def verify_backup(*, settings: Settings, record: BackupRecord) -> dict[str, Any]:
    manifest, files = _read_archive(settings, record)
    return {
        "scope": manifest.get("scope", ""),
        "schema_version": manifest.get("schema_version"),
        "collections": len(files),
        "documents": sum(
            int(item.get("documents", 0))
            for item in manifest.get("collections", {}).values()
            if isinstance(item, dict)
        ),
    }


async def restore_backup(
    *, settings: Settings, record: BackupRecord, apply_changes: bool
) -> dict[str, Any]:
    manifest, files = _read_archive(settings, record)
    model_by_collection = {
        model.get_motor_collection().name: model for model in DOCUMENT_MODELS
    }
    summary: dict[str, Any] = {
        "mode": "restore" if apply_changes else "rehearsal",
        "collections": len(files),
        "documents": 0,
        "applied": 0,
        "skipped": [],
    }
    for member_name, content in sorted(files.items()):
        collection_name = member_name.removeprefix("collections/").removesuffix(".jsonl")
        model = model_by_collection.get(collection_name)
        if model is None:
            raise BackupError("backup_collection_unknown")
        documents: list[dict[str, Any]] = []
        for line in content.splitlines():
            if not line:
                continue
            value = json_util.loads(line)
            if not isinstance(value, dict) or "_id" not in value:
                raise BackupError("backup_document_invalid")
            documents.append(value)
        summary["documents"] += len(documents)
        if not apply_changes:
            continue
        # Do not overwrite the live task or backup record that is executing the
        # restore. Other collections are upserted by their stable Mongo _id;
        # this is intentionally non-destructive and leaves newer documents
        # available for an operator to reconcile.
        if collection_name in {TaskCollectionName, BackupCollectionName}:
            summary["skipped"].append(collection_name)
            continue
        collection = model.get_motor_collection()
        operations = [
            ReplaceOne({"_id": document["_id"]}, document, upsert=True)
            for document in documents
        ]
        if operations:
            result = await collection.bulk_write(operations, ordered=False)
            summary["applied"] += result.matched_count + result.upserted_count
    summary["manifest_scope"] = manifest.get("scope", "")
    return summary


# Collection names are resolved at runtime by Beanie; these defaults match its
# snake-case naming and keep restore safeguards readable before initialization.
TaskCollectionName = "task"
BackupCollectionName = "backup_record"
