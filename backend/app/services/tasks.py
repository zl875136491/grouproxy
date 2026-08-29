from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..models import Task


def now() -> datetime:
    return datetime.now(timezone.utc)


async def create_task(
    *,
    task_type: str,
    target_type: str,
    target_id: str,
    payload: dict,
    idempotency_key: str,
    created_by: str,
    request_id: str = "",
) -> tuple[Task, bool]:
    existing = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing:
        return existing, False
    task = Task(
        task_id=str(uuid4()),
        task_type=task_type,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        idempotency_key=idempotency_key,
        created_by=created_by,
        request_id=request_id,
        next_run_at=now(),
    )
    try:
        await task.insert()
    except DuplicateKeyError:
        existing = await Task.find_one(Task.idempotency_key == idempotency_key)
        if existing:
            return existing, False
        raise
    return task, True


async def mark_task_running(task: Task, worker_id: str, lease_seconds: int = 60) -> Task:
    current = now()
    task.status = "running"
    task.active = True
    task.locked_by = worker_id
    task.locked_at = current
    task.heartbeat_at = current
    task.lease_expires_at = current + timedelta(seconds=lease_seconds)
    task.started_at = getattr(task, "started_at", None) or current
    await task.save()
    return task


async def claim_due_task(
    *, task_type: str, worker_id: str, lease_seconds: int = 60
) -> Task | None:
    """Atomically claim one due task so concurrent backend workers cannot race."""

    current = now()
    collection = Task.get_motor_collection()
    document = await collection.find_one_and_update(
        {
            "task_type": task_type,
            "status": "queued",
            "$or": [{"active": True}, {"active": {"$exists": False}}],
            "next_run_at": {"$lte": current},
        },
        {
            "$set": {
                "status": "running",
                "locked_by": worker_id,
                "locked_at": current,
                "heartbeat_at": current,
                "lease_expires_at": current + timedelta(seconds=lease_seconds),
                "started_at": current,
                "stage": "running",
                "progress_message": "Worker claimed task",
            }
        },
        sort=[("next_run_at", 1), ("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return await Task.get(document["_id"])


async def heartbeat_task(task: Task, lease_seconds: int = 60) -> Task:
    current = now()
    task.heartbeat_at = current
    task.lease_expires_at = current + timedelta(seconds=lease_seconds)
    await task.save()
    return task


async def complete_task(task: Task, *, result: dict[str, Any], message: str) -> Task:
    current = now()
    task.status = "cancelled" if task.cancel_requested else "succeeded"
    task.active = False
    task.progress = 100
    task.stage = "cancelled" if task.cancel_requested else "succeeded"
    task.progress_message = "Cancellation acknowledged" if task.cancel_requested else message
    task.result = result
    task.finished_at = current
    task.locked_by = ""
    task.lease_expires_at = None
    await task.save()
    return task


async def fail_task(
    task: Task,
    *,
    error: str,
    retryable: bool,
    backoff_base_seconds: int = 5,
) -> Task:
    """Record a recoverable failure or move a task to the dead-letter state."""

    current = now()
    safe_error = " ".join(error.split())[:512]
    next_retry = task.retry_count + 1
    task.error = safe_error
    task.locked_by = ""
    task.locked_at = None
    task.heartbeat_at = current
    task.lease_expires_at = None
    if task.cancel_requested:
        task.status = "cancelled"
        task.active = False
        task.stage = "cancelled"
        task.progress_message = "Cancellation acknowledged"
        task.finished_at = current
    elif not retryable or next_retry >= task.max_retries:
        task.retry_count = next_retry
        task.status = "dead_letter"
        task.active = False
        task.stage = "dead_letter"
        task.progress_message = "Non-recoverable error" if not retryable else "Retry limit reached"
        task.finished_at = current
    else:
        task.retry_count = next_retry
        task.status = "queued"
        task.active = True
        task.stage = "retry_scheduled"
        task.progress_message = "Retry scheduled"
        delay = backoff_base_seconds * (2 ** (next_retry - 1))
        task.next_run_at = current + timedelta(seconds=delay)
    await task.save()
    return task


async def reclaim_expired_tasks(*, task_type: str) -> list[Task]:
    """Recover interrupted workers after their MongoDB lease expires."""

    current = now()
    expired = await Task.find(
        {
            "task_type": task_type,
            "status": {"$in": ["running", "cancel_requested"]},
            "lease_expires_at": {"$lt": current},
        }
    ).to_list()
    recovered: list[Task] = []
    for task in expired:
        if task.cancel_requested:
            task.status = "cancelled"
            task.active = False
            task.stage = "cancelled"
            task.progress_message = "Cancellation acknowledged after lease expiry"
            task.finished_at = current
            task.locked_by = ""
            task.lease_expires_at = None
            await task.save()
            recovered.append(task)
            continue
        await fail_task(task, error="task_lease_expired", retryable=True)
        recovered.append(task)
    return recovered
