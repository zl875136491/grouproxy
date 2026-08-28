from datetime import datetime, timedelta, timezone
from uuid import uuid4

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
    task.locked_by = worker_id
    task.locked_at = current
    task.heartbeat_at = current
    task.lease_expires_at = current + timedelta(seconds=lease_seconds)
    task.started_at = getattr(task, "started_at", None) or current
    await task.save()
    return task
