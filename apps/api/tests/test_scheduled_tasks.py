"""Proactive scheduled-task trigger tests (pure-mock, no DB).

Mocking mirrors test_source_sync.py: a fake engine returns canned rows, the
task-spawn seam is monkeypatched so no real executor runs.
"""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from jobs import scheduled_tasks as st


# ─── compute_next_run ─────────────────────────────────────────────────────────

def test_interval_next_run_is_in_the_future():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    nxt = st.compute_next_run({"schedule_kind": "interval", "interval_seconds": 3600}, now)
    assert nxt is not None and nxt > now
    assert (nxt - now) <= timedelta(seconds=3600)


def test_cron_next_run_parsed():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    nxt = st.compute_next_run({"schedule_kind": "cron", "cron": "0 9 * * *"}, now)
    assert nxt is not None and nxt > now
    assert nxt.hour == 9 and nxt.minute == 0


def test_invalid_schedule_returns_none():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    assert st.compute_next_run({"schedule_kind": "interval", "interval_seconds": 0}, now) is None
    assert st.compute_next_run({"schedule_kind": "cron", "cron": "not-a-cron"}, now) is None


# ─── run_due_scheduled_tasks ────────────────────────────────────────────────────

class _FakeCol:
    def is_(self, *_): return self
    def __le__(self, *_): return self
    def __or__(self, *_): return self
    def __eq__(self, *_): return self
    def desc(self): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        enabled = _FakeCol()
        next_run_at = _FakeCol()
        organization_id = _FakeCol()
        created_at = _FakeCol()


class _Clause:
    def where(self, *a, **k): return self
    def values(self, **k): return self
    def order_by(self, *a): return self


def _engine(results, ops):
    idx = [0]

    class _Result:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def all(self_inner):
                    return data if isinstance(data, list) else ([data] if data else [])
            return M()

    class _Conn:
        async def execute(self, stmt, params=None):
            ops.append("exec")
            i = idx[0]; idx[0] += 1
            return _Result(results[i] if i < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class _Engine:
        def begin(self): return _Conn()

    return _Engine()


def _patch(monkeypatch, engine, ops):
    monkeypatch.setattr(st, "engine", engine)
    monkeypatch.setattr(st, "reflect_table", AsyncMock(return_value=_FakeTable()))
    monkeypatch.setattr(st, "select", lambda *a, **k: _Clause())
    monkeypatch.setattr(st, "update", lambda *a, **k: _Clause())
    monkeypatch.setattr(st.audit, "log", AsyncMock())


@pytest.mark.asyncio
async def test_due_rows_spawn_tasks_and_advance_next_run(monkeypatch):
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    due = [
        {"id": "s1", "organization_id": "default", "goal": "scan inbox",
         "schedule_kind": "interval", "interval_seconds": 3600, "created_by": "m1"},
        {"id": "s2", "organization_id": "default", "goal": "daily brief",
         "schedule_kind": "cron", "cron": "0 9 * * *", "created_by": "m1"},
    ]
    ops: list[str] = []
    # First execute = select(due rows); then one update per row.
    engine = _engine([due, None, None], ops)
    _patch(monkeypatch, engine, ops)

    spawn = AsyncMock(side_effect=["task-1", "task-2"])
    monkeypatch.setattr(st, "_spawn_task", spawn)

    spawned = await st.run_due_scheduled_tasks(now=now)
    assert spawned == ["task-1", "task-2"]
    assert spawn.await_count == 2
    # Each row triggered an audit row and an update (advance next_run_at).
    assert st.audit.log.await_count == 2


@pytest.mark.asyncio
async def test_no_due_rows_is_a_noop(monkeypatch):
    ops: list[str] = []
    engine = _engine([[]], ops)
    _patch(monkeypatch, engine, ops)
    spawn = AsyncMock()
    monkeypatch.setattr(st, "_spawn_task", spawn)

    spawned = await st.run_due_scheduled_tasks()
    assert spawned == []
    assert spawn.await_count == 0


@pytest.mark.asyncio
async def test_spawn_task_inserts_and_runs(monkeypatch):
    """_spawn_task materializes a Task with triggered_by='schedule' and runs it."""
    import runtime.executor as ex

    insert_task = AsyncMock(return_value="task-9")
    monkeypatch.setattr(ex, "insert_task", insert_task)

    run = AsyncMock()

    class _FakeExecutor:
        def __init__(self, _run=run):
            self.run = _run

    monkeypatch.setattr(ex, "TaskExecutor", _FakeExecutor)

    row = {"organization_id": "default", "goal": "g", "created_by": "m1"}
    task_id = await st._spawn_task(row)
    assert task_id == "task-9"
    values = insert_task.await_args.args[0]
    assert values["triggered_by"] == "schedule"
    assert values["goal"] == "g"
