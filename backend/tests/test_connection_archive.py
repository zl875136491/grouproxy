from datetime import datetime, timezone

import pytest

from app.services.connection_archive import ConnectionArchiveWorker, archive_cutoff_day, archive_day_bounds


def test_archive_day_bounds_are_utc_full_day() -> None:
    start, end = archive_day_bounds(datetime(2026, 9, 17).date())
    assert start == datetime(2026, 9, 17, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert archive_cutoff_day(datetime(2026, 9, 24, 12, tzinfo=timezone.utc)) == datetime(2026, 9, 17).date()


@pytest.mark.asyncio
async def test_archive_worker_does_not_repeat_completed_day(monkeypatch, tmp_path) -> None:
    class Result:
        deleted_count = 2

    class Cursor:
        def sort(self, *_):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class EmptyAggregate:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Collection:
        def aggregate(self, *_):
            return EmptyAggregate()

        def find(self, *_):
            return Cursor()

        async def delete_many(self, *_):
            return Result()

    monkeypatch.setattr(
        "app.services.connection_archive.ConnectionSnapshot.get_motor_collection",
        lambda: Collection(),
    )
    settings = type(
        "Settings",
        (),
        {
            "connection_history_archive_directory": str(tmp_path),
            "connection_history_archive_interval_seconds": 3600,
        },
    )()
    worker = ConnectionArchiveWorker(settings)
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    result = await worker.run_once(now=now)
    assert result is not None and result.exists()
    assert await worker.run_once(now=now) is None


@pytest.mark.asyncio
async def test_archive_worker_exports_backlog_before_cutoff(monkeypatch, tmp_path) -> None:
    deleted: list[dict] = []

    class Result:
        deleted_count = 1

    class Cursor:
        def __init__(self) -> None:
            self._done = False

        def sort(self, *_):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return {"sampled_at": "old"}

    class Aggregate:
        def __init__(self) -> None:
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return {"_id": "2026-09-01"}

    class Collection:
        def aggregate(self, *_):
            return Aggregate()

        def find(self, *_):
            return Cursor()

        async def delete_many(self, query):
            deleted.append(query)
            return Result()

    monkeypatch.setattr(
        "app.services.connection_archive.ConnectionSnapshot.get_motor_collection",
        lambda: Collection(),
    )
    settings = type(
        "Settings",
        (),
        {"connection_history_archive_directory": str(tmp_path)},
    )()
    worker = ConnectionArchiveWorker(settings)
    result = await worker.run_once(now=datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    assert result == tmp_path / "connections-2026-09-17.jsonl.gz"
    assert (tmp_path / "connections-2026-09-01.jsonl.gz").exists()
    assert [item["sampled_at"]["$gte"].date().isoformat() for item in deleted] == [
        "2026-09-01",
        "2026-09-17",
    ]
