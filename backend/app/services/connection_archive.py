"""Daily, durable export and cleanup for historical connection snapshots."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import socket
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import ConnectionSnapshot, utcnow

logger = logging.getLogger(__name__)


def archive_day_bounds(day) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def archive_cutoff_day(now: datetime) -> date:
    """Return the newest UTC day that is eligible for export.

    On a given calendar day, that is the day seven days earlier. Everything
    from the following midnight onward stays in the database.
    """

    return (now.astimezone(timezone.utc) - timedelta(days=7)).date()


class ConnectionArchiveWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker_id = f"connection-archive:{socket.gethostname()}:{os.getpid()}"
        self.stop_event = asyncio.Event()
        self._last_archived_day = None

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("Connection history archive failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.settings.connection_history_archive_interval_seconds,
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self.stop_event.set()

    async def run_once(self, *, now: datetime | None = None) -> Path | None:
        current = (now or utcnow()).astimezone(timezone.utc)
        cutoff_day = archive_cutoff_day(current)
        if self._last_archived_day == cutoff_day:
            return None
        directory = Path(self.settings.connection_history_archive_directory)
        directory.mkdir(parents=True, exist_ok=True)
        last_path: Path | None = None
        for target_day in await self._days_to_archive(cutoff_day):
            last_path = await self._export_day(directory, target_day)
        self._last_archived_day = cutoff_day
        return last_path

    async def _days_to_archive(self, cutoff_day: date) -> list[date]:
        _, cutoff_end = archive_day_bounds(cutoff_day)
        found: set[date] = set()
        cursor = ConnectionSnapshot.get_motor_collection().aggregate(
            [
                {"$match": {"sampled_at": {"$lt": cutoff_end}}},
                {
                    "$group": {
                        "_id": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$sampled_at",
                                "timezone": "UTC",
                            }
                        }
                    }
                },
            ]
        )
        async for row in cursor:
            raw_day = str(row.get("_id") or "")
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                continue
            if day <= cutoff_day:
                found.add(day)
        # Always record the cutoff day, including an empty archive, so a quiet
        # day is still durable evidence that the export ran.
        found.add(cutoff_day)
        return sorted(found)

    async def _export_day(self, directory: Path, target_day: date) -> Path:
        start, end = archive_day_bounds(target_day)
        final_path = directory / f"connections-{target_day.isoformat()}.jsonl.gz"
        temp_path = directory / f".connections-{target_day.isoformat()}.{os.getpid()}.tmp"
        count = 0
        try:
            with gzip.open(temp_path, "wt", encoding="utf-8") as output:
                cursor = ConnectionSnapshot.get_motor_collection().find(
                    {"sampled_at": {"$gte": start, "$lt": end}}
                ).sort("sampled_at", 1)
                async for document in cursor:
                    output.write(json.dumps(document, default=str, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
            os.replace(temp_path, final_path)
            result = await ConnectionSnapshot.get_motor_collection().delete_many(
                {"sampled_at": {"$gte": start, "$lt": end}}
            )
            logger.info(
                "Archived connection history day=%s exported=%d deleted=%d path=%s",
                target_day,
                count,
                result.deleted_count,
                final_path,
            )
            return final_path
        finally:
            if temp_path.exists():
                temp_path.unlink()
