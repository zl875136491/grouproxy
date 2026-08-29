"""MongoDB-backed execution loop for subscription refresh tasks."""

import asyncio
import os
import socket
from datetime import timedelta
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from ..config import Settings
from ..models import SubscriptionSource, Task, utcnow
from .audit import append_audit
from .subscriptions import SubscriptionError, refresh_subscription_source
from .tasks import (
    claim_due_task,
    complete_task,
    create_task,
    fail_task,
    heartbeat_task,
    reclaim_expired_tasks,
)

REFRESH_TASK_TYPE = "subscription.refresh"


async def enqueue_refresh_task(
    *,
    source: SubscriptionSource,
    created_by: str,
    idempotency_key: str | None = None,
    request_id: str = "",
) -> tuple[Task, bool]:
    """Return an active refresh task instead of starting concurrent fetches."""

    source_id = str(source.id)
    active = await Task.find_one(
        {
            "task_type": REFRESH_TASK_TYPE,
            "target_id": source_id,
            "status": {"$in": ["queued", "running", "cancel_requested"]},
        }
    )
    if active is not None:
        return active, True
    key = (
        f"subscription.refresh:{source_id}:{idempotency_key}"
        if idempotency_key
        else f"subscription.refresh:{source_id}:{uuid4()}"
    )
    try:
        task, created = await create_task(
            task_type=REFRESH_TASK_TYPE,
            target_type="subscription_source",
            target_id=source_id,
            payload={"source_id": source_id},
            idempotency_key=key,
            created_by=created_by,
            request_id=request_id,
        )
    except DuplicateKeyError:
        # Task.Settings enforces a partial unique index for active work. A
        # simultaneous request with another idempotency key must join the
        # in-flight refresh instead of queueing a second fetch.
        active = await Task.find_one(
            {
                "task_type": REFRESH_TASK_TYPE,
                "target_id": source_id,
                "status": {"$in": ["queued", "running", "cancel_requested"]},
            }
        )
        if active is not None:
            return active, True
        raise
    return task, not created


class SubscriptionWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker_id = f"subscription-worker:{socket.gethostname()}:{os.getpid()}"
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                await reclaim_expired_tasks(task_type=REFRESH_TASK_TYPE)
                await self.schedule_due_sources()
                task = await claim_due_task(
                    task_type=REFRESH_TASK_TYPE,
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.subscription_task_lease_seconds,
                )
                if task is not None:
                    await self.execute(task)
                    continue
            except Exception:
                # The next scheduler pass can recover an abandoned lease. Do
                # not let a single bad upstream stop all future refreshes.
                pass
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(self.settings.subscription_worker_poll_seconds, 0.1),
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self.stop_event.set()

    async def schedule_due_sources(self) -> None:
        current = utcnow()
        sources = await SubscriptionSource.find(
            SubscriptionSource.enabled == True,  # noqa: E712 - Beanie expression
            SubscriptionSource.url != "",
        ).to_list()
        for source in sources:
            last_attempt = source.last_refresh_attempt_at or source.created_at
            if current - last_attempt < timedelta(seconds=source.fetch_interval_sec):
                continue
            slot = int(current.timestamp() // source.fetch_interval_sec)
            await enqueue_refresh_task(
                source=source,
                created_by="scheduler",
                idempotency_key=f"subscription.refresh:{source.id}:{slot}",
            )

    async def execute(self, task: Task) -> None:
        source = await SubscriptionSource.get(task.target_id)
        if source is None:
            await fail_task(task, error="subscription_source_not_found", retryable=False)
            return
        if not source.enabled:
            task.cancel_requested = True
            await complete_task(
                task,
                result={"source_id": str(source.id)},
                message="Source disabled",
            )
            return
        await heartbeat_task(task, lease_seconds=self.settings.subscription_task_lease_seconds)
        try:
            result = await refresh_subscription_source(source, self.settings)
            await complete_task(
                task,
                result={
                    "source_id": str(source.id),
                    "version_id": str(result.version.id),
                    "content_hash": result.version.content_hash,
                    "changed": result.changed,
                    "parse_ok": result.version.parse_ok,
                },
                message="Subscription refresh completed",
            )
            await append_audit(
                action="subscription.refresh",
                target_type="subscription_source",
                target_id=str(source.id),
                actor=task.created_by,
                request_id=task.request_id,
                after={
                    "version_id": str(result.version.id),
                    "content_hash": result.version.content_hash,
                    "changed": result.changed,
                },
            )
        except SubscriptionError as exc:
            updated = await fail_task(task, error=exc.code, retryable=exc.retryable)
            await append_audit(
                action="subscription.refresh.failed",
                target_type="subscription_source",
                target_id=str(source.id),
                actor=task.created_by,
                request_id=task.request_id,
                after={"task_status": updated.status, "retry_count": updated.retry_count},
                result="failed",
                error=exc.code,
            )
        except Exception:
            updated = await fail_task(
                task,
                error="subscription_refresh_unexpected_error",
                retryable=True,
            )
            await append_audit(
                action="subscription.refresh.failed",
                target_type="subscription_source",
                target_id=str(source.id),
                actor=task.created_by,
                request_id=task.request_id,
                after={"task_status": updated.status, "retry_count": updated.retry_count},
                result="failed",
                error="subscription_refresh_unexpected_error",
            )
