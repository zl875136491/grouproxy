"""MongoDB task worker for control-plane backup and restore operations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from ..config import Settings
from ..models import BackupRecord, Task, utcnow
from .alerts import set_alert
from .audit import append_audit
from .backups import (
    BACKUP_READY_STATUSES,
    BackupError,
    backup_schedule_key,
    create_backup_artifact,
    delete_backup_artifact,
    rehearsal_schedule_key,
    restore_backup,
    retained_scheduled_backup_ids,
    verify_backup,
)
from .tasks import (
    claim_due_task,
    complete_task,
    create_task,
    fail_task,
    heartbeat_task,
    reclaim_expired_tasks,
)

BACKUP_TASK_TYPES = ("backup.create", "backup.restore")


class BackupWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker_id = f"backup-worker:{socket.gethostname()}:{os.getpid()}"
        self.stop_event = asyncio.Event()
        self._next_maintenance_at = 0.0

    async def run(self) -> None:
        while not self.stop_event.is_set():
            did_work = False
            try:
                await self._run_maintenance_if_due()
                for task_type in BACKUP_TASK_TYPES:
                    await reclaim_expired_tasks(task_type=task_type)
                    task = await claim_due_task(
                        task_type=task_type,
                        worker_id=self.worker_id,
                        lease_seconds=60,
                    )
                    if task is not None:
                        did_work = True
                        await self.execute(task)
                        break
            except Exception:
                # A failed backup must not take down the API; the task lease is
                # recovered on the next pass and the error remains queryable.
                pass
            if did_work:
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self.stop_event.set()

    async def _run_maintenance_if_due(self) -> None:
        """Run opt-in scheduling and retention outside request handlers."""

        if not self.settings.backup_auto_enabled:
            return
        monotonic_now = asyncio.get_running_loop().time()
        if monotonic_now < self._next_maintenance_at:
            return
        self._next_maintenance_at = (
            monotonic_now + self.settings.backup_maintenance_interval_seconds
        )
        try:
            await self._schedule_automatic_backup()
            await self._schedule_automatic_rehearsal()
            await self._apply_retention()
            await set_alert(
                fingerprint="backup:scheduler",
                category="backup",
                title="Backup maintenance failed",
                detail="",
                severity="critical",
                active=False,
            )
        except Exception:
            # Scheduler failure must be visible, but never stop workers from
            # handling an operator-initiated backup or restore task.
            await set_alert(
                fingerprint="backup:scheduler",
                category="backup",
                title="Backup maintenance failed",
                detail="The automatic backup maintenance cycle did not complete.",
                severity="critical",
                active=True,
            )

    async def _schedule_automatic_backup(self) -> None:
        current = utcnow()
        idempotency_key = backup_schedule_key(
            scope="control_plane",
            interval_seconds=self.settings.backup_auto_interval_seconds,
            at=current,
        )
        if await Task.find_one(Task.idempotency_key == idempotency_key):
            return
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        backup_id = f"bkp_{digest[:32]}"
        record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
        record_created = False
        if record is None:
            record = BackupRecord(
                backup_id=backup_id,
                scope="control_plane",
                origin="scheduled",
                status="queued",
                created_by="scheduler",
            )
            try:
                await record.insert()
                record_created = True
            except DuplicateKeyError:
                record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
                if record is None:
                    raise
        try:
            task, created = await create_task(
                task_type="backup.create",
                target_type="backup",
                target_id=backup_id,
                payload={"backup_id": backup_id, "scope": "control_plane"},
                idempotency_key=idempotency_key,
                created_by="scheduler",
                request_id=idempotency_key,
            )
        except Exception:
            if record_created and record.status == "queued" and not record.storage_ref:
                await record.delete()
            raise
        if created:
            await append_audit(
                action="backup.schedule",
                target_type="backup",
                target_id=backup_id,
                actor="scheduler",
                actor_role="system",
                request_id=idempotency_key,
                after={"scope": "control_plane", "task_id": task.task_id},
            )

    async def _schedule_automatic_rehearsal(self) -> None:
        records = (
            await BackupRecord.find(
                {
                    "storage_ref": {"$ne": ""},
                    "status": {"$in": list(BACKUP_READY_STATUSES)},
                }
            )
            .sort(-BackupRecord.created_at)
            .limit(1)
            .to_list()
        )
        if not records:
            return
        record = records[0]
        current = utcnow()
        if (
            record.last_rehearsed_at is not None
            and current - record.last_rehearsed_at
            < timedelta(seconds=self.settings.backup_rehearsal_interval_seconds)
        ):
            return
        active = await Task.find_one(
            {
                "task_type": "backup.restore",
                "target_id": record.backup_id,
                "active": True,
            }
        )
        if active is not None:
            return
        idempotency_key = rehearsal_schedule_key(
            backup_id=record.backup_id,
            interval_seconds=self.settings.backup_rehearsal_interval_seconds,
            at=current,
        )
        task, created = await create_task(
            task_type="backup.restore",
            target_type="backup",
            target_id=record.backup_id,
            payload={"backup_id": record.backup_id, "confirm": False},
            idempotency_key=idempotency_key,
            created_by="scheduler",
            request_id=idempotency_key,
        )
        if not created:
            return
        record.restore_task_id = task.task_id
        await record.save()
        await append_audit(
            action="backup.rehearsal.schedule",
            target_type="backup",
            target_id=record.backup_id,
            actor="scheduler",
            actor_role="system",
            request_id=idempotency_key,
            after={"task_id": task.task_id},
        )

    async def _apply_retention(self) -> None:
        records = await BackupRecord.find(
            {"origin": "scheduled", "storage_ref": {"$ne": ""}}
        ).to_list()
        retained = retained_scheduled_backup_ids(
            records,
            daily_days=self.settings.backup_retention_daily_days,
            weekly_weeks=self.settings.backup_retention_weekly_weeks,
            monthly_months=self.settings.backup_retention_monthly_months,
        )
        active_restores = await Task.find(
            {"task_type": "backup.restore", "active": True}
        ).to_list()
        protected_ids = {task.target_id for task in active_restores}
        for record in records:
            if record.backup_id in retained or record.backup_id in protected_ids:
                continue
            if record.status not in BACKUP_READY_STATUSES and record.status != "expired":
                continue
            before = {
                "storage_ref": record.storage_ref,
                "checksum": record.checksum,
                "status": record.status,
            }
            if record.status != "expired":
                record.status = "expired"
                record.error = ""
                await record.save()
            try:
                delete_backup_artifact(settings=self.settings, record=record)
            except BackupError as exc:
                record.error = exc.code
                await record.save()
                await self._set_backup_alert(record, active=True, detail=exc.code)
                continue
            await append_audit(
                action="backup.retention.delete",
                target_type="backup",
                target_id=record.backup_id,
                actor="scheduler",
                actor_role="system",
                request_id=f"backup-retention:{record.backup_id}",
                before=before,
                after={"status": "expired"},
            )
            await record.delete()
            await self._set_backup_alert(record, active=False, detail="")

    async def _set_backup_alert(
        self, record: BackupRecord, *, active: bool, detail: str
    ) -> None:
        try:
            await set_alert(
                fingerprint=f"backup:{record.backup_id}",
                category="backup",
                title="Backup verification failed",
                detail=detail,
                severity="critical",
                active=active,
            )
        except Exception:
            # Alert persistence must not hide the primary task result.
            return

    async def execute(self, task: Task) -> None:
        backup_id = str(task.payload.get("backup_id", ""))
        record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
        if record is None:
            await fail_task(task, error="backup_record_not_found", retryable=False)
            return
        record.status = "running"
        record.error = ""
        await record.save()
        await heartbeat_task(task, lease_seconds=60)
        try:
            if task.task_type == "backup.create":
                artifact = await create_backup_artifact(
                    backup_id=record.backup_id,
                    scope=record.scope,
                    settings=self.settings,
                )
                record.artifact_paths = [artifact.filename]
                record.storage_ref = artifact.filename
                record.format = "tar.gz.enc" if artifact.encrypted else "tar.gz"
                record.checksum = artifact.checksum
                record.encrypted = artifact.encrypted
                record.size_bytes = artifact.size_bytes
                record.manifest = artifact.manifest
                # Creation is only considered successful after reading the
                # archive back and checking every member hash.
                verification = await verify_backup(settings=self.settings, record=record)
                record.status = "verified"
                record.verified_at = utcnow()
                await record.save()
                await complete_task(
                    task,
                    result={"backup_id": record.backup_id, **verification},
                    message="Backup created and verified",
                )
                await append_audit(
                    action="backup.create",
                    target_type="backup",
                    target_id=record.backup_id,
                    actor=task.created_by,
                    request_id=task.request_id,
                    after={
                        "status": record.status,
                        "checksum": record.checksum,
                        "encrypted": record.encrypted,
                    },
                )
                await self._set_backup_alert(record, active=False, detail="")
                return

            apply_changes = bool(task.payload.get("confirm", False))
            summary = await restore_backup(
                settings=self.settings,
                record=record,
                apply_changes=apply_changes,
            )
            record.status = "restored" if apply_changes else "rehearsed"
            if not apply_changes:
                record.last_rehearsed_at = utcnow()
            record.error = ""
            await record.save()
            await complete_task(
                task,
                result=summary,
                message=(
                    "Backup restore rehearsal completed"
                    if not apply_changes
                    else "Backup restored"
                ),
            )
            await append_audit(
                action="backup.restore" if apply_changes else "backup.restore.rehearsal",
                target_type="backup",
                target_id=record.backup_id,
                actor=task.created_by,
                request_id=task.request_id,
                after={"status": record.status, **summary},
            )
            await self._set_backup_alert(record, active=False, detail="")
        except BackupError as exc:
            record.status = "failed"
            record.error = exc.code
            await record.save()
            updated = await fail_task(task, error=exc.code, retryable=False)
            await append_audit(
                action="backup.failed",
                target_type="backup",
                target_id=record.backup_id,
                actor=task.created_by,
                request_id=task.request_id,
                result="failed",
                error=exc.code,
                after={"task_status": updated.status},
            )
            await self._set_backup_alert(record, active=True, detail=exc.code)
        except Exception:
            record.status = "failed"
            record.error = "backup_operation_failed"
            await record.save()
            updated = await fail_task(task, error=record.error, retryable=True)
            await append_audit(
                action="backup.failed",
                target_type="backup",
                target_id=record.backup_id,
                actor=task.created_by,
                request_id=task.request_id,
                result="failed",
                error=record.error,
                after={"task_status": updated.status},
            )
            await self._set_backup_alert(record, active=True, detail=record.error)
