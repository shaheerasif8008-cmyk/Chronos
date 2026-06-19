"""Proof tests for durable, multi-worker task coordination.

Covers the guarantees that make the runtime safe across >1 worker:
1. A leased task runs once and the lease is released afterward.
2. A peer worker that doesn't hold the lease does not run (no double-execution).
3. A crashed worker's lease expires → the reaper revives the task (crash recovery).
4. The scheduler poll fires on exactly one worker per tick (single-holder lock).

Leases are simulated with an in-memory store so the tests don't need Redis; this
exercises the runner/reaper/scheduler logic, not the Redis client itself.
"""
from __future__ import annotations

import pytest

from runtime import leases, task_runner
from runtime.task_runner import TaskRunner


class _FakeLeaseStore:
    """Minimal Redis-like lease store with single-owner semantics."""

    def __init__(self):
        self.owner: dict[str, str] = {}

    def patch(self, monkeypatch, worker_id: str = "worker-A"):
        async def acquire(task_id, *, ttl=None):
            if task_id in self.owner:
                return False
            self.owner[task_id] = worker_id
            return True

        async def renew(task_id, *, ttl=None):
            return self.owner.get(task_id) == worker_id

        async def release(task_id):
            if self.owner.get(task_id) == worker_id:
                del self.owner[task_id]

        async def held(task_id):
            return task_id in self.owner

        monkeypatch.setattr(leases, "acquire_task_lease", acquire)
        monkeypatch.setattr(leases, "renew_task_lease", renew)
        monkeypatch.setattr(leases, "release_task_lease", release)
        monkeypatch.setattr(leases, "task_lease_held", held)


@pytest.mark.asyncio
async def test_leased_task_runs_once_and_releases(monkeypatch):
    store = _FakeLeaseStore()
    store.patch(monkeypatch)
    ran: list[str] = []

    async def run_task(task_id):
        ran.append(task_id)

    runner = TaskRunner(run_task=run_task)
    await runner.enqueue("t1")
    await runner.drain_once()

    assert ran == ["t1"]
    assert "t1" not in store.owner  # lease released after completion


@pytest.mark.asyncio
async def test_peer_worker_skips_task_it_does_not_own(monkeypatch):
    store = _FakeLeaseStore()
    store.owner["t1"] = "worker-B"  # already held by a peer
    store.patch(monkeypatch, worker_id="worker-A")
    ran: list[str] = []
    failed: list[str] = []

    async def run_task(task_id):
        ran.append(task_id)

    async def mark_failed(task_id, error):
        failed.append(task_id)

    runner = TaskRunner(run_task=run_task, mark_failed=mark_failed)
    await runner.enqueue("t1")
    await runner.drain_once()

    assert ran == []      # peer owns it → not executed here
    assert failed == []   # and not marked failed either


@pytest.mark.asyncio
async def test_reaper_revives_orphaned_task(monkeypatch):
    # Task t1 is "running" but its worker died → no live lease.
    store = _FakeLeaseStore()
    store.patch(monkeypatch)  # owner empty → held() returns False for t1
    saved: list[tuple[str, dict]] = []
    enqueued: list[str] = []

    async def fake_save(task_id, **values):
        saved.append((task_id, values))

    async def fake_enqueue(task_id, *, priority=10):
        enqueued.append(task_id)

    monkeypatch.setattr("runtime.agent_loop.save_task", fake_save)
    monkeypatch.setattr(task_runner.runner, "enqueue", fake_enqueue)

    reaped = await task_runner.reap_orphaned_tasks(task_ids=["t1"])

    assert reaped == ["t1"]
    assert enqueued == ["t1"]
    assert saved and saved[0][1]["status"] == "queued"


@pytest.mark.asyncio
async def test_reaper_leaves_live_task_alone(monkeypatch):
    store = _FakeLeaseStore()
    store.owner["t1"] = "worker-B"  # a live worker still holds it
    store.patch(monkeypatch, worker_id="worker-A")
    enqueued: list[str] = []

    async def fake_enqueue(task_id, *, priority=10):
        enqueued.append(task_id)

    monkeypatch.setattr(task_runner.runner, "enqueue", fake_enqueue)

    reaped = await task_runner.reap_orphaned_tasks(task_ids=["t1"])

    assert reaped == []
    assert enqueued == []


@pytest.mark.asyncio
async def test_scheduler_poll_single_holder(monkeypatch):
    from jobs import scheduled_tasks

    held = {"locked": False}

    async def acquire_lock(name, *, ttl):
        if held["locked"]:
            return None
        held["locked"] = True
        return "token"

    async def release_lock(name, token):
        held["locked"] = False

    ran = {"count": 0}

    async def fake_inner(now=None):
        ran["count"] += 1
        return ["task-x"]

    monkeypatch.setattr(leases, "acquire_lock", acquire_lock)
    monkeypatch.setattr(leases, "release_lock", release_lock)
    monkeypatch.setattr(scheduled_tasks, "_run_due_scheduled_tasks", fake_inner)

    # First worker gets the lock and runs.
    first = await scheduled_tasks.run_due_scheduled_tasks()
    assert first == ["task-x"]
    assert ran["count"] == 1

    # While the lock is held, a second worker's tick is a no-op.
    held["locked"] = True
    second = await scheduled_tasks.run_due_scheduled_tasks()
    assert second == []
    assert ran["count"] == 1
