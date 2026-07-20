from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from runtime import leases


# Uvicorn gets a separate graceful connection-drain window. Once application
# shutdown starts, task cleanup must leave time for the leader lock and the
# remaining lifespan resources to close before the container stop deadline.
_SHUTDOWN_TASK_WAIT_SECONDS = 10.0


RunTask = Callable[[str], Awaitable[Any]]
MarkCancelled = Callable[[str, str], Awaitable[Any]]
MarkFailed = Callable[[str, str], Awaitable[Any]]
MarkPaused = Callable[[str, str], Awaitable[Any]]


async def _default_run_task(task_id: str) -> Any:
    from runtime.executor import TaskExecutor

    return await TaskExecutor().run(task_id)


def classify_failure(error: str) -> str:
    """Map a runner error string to the final failure taxonomy.

    Returns one of: ``timeout`` (task-level timeout), ``cancelled`` (user/system
    cancellation), or ``error`` (an exception exhausted retries).
    """
    normalized = (error or "").strip().lower()
    if normalized == "task_timeout":
        return "timeout"
    if normalized in {"user_cancelled", "cancelled", "system_cancelled"}:
        return "cancelled"
    return "error"


async def _persist_terminal(task_id: str, **values: Any) -> None:
    from runtime.agent_loop import save_task

    await save_task(task_id, completed_at=datetime.now(timezone.utc), **values)


async def _default_mark_cancelled(task_id: str, reason: str) -> None:
    from runtime.agent_loop import emit_activity

    await _persist_terminal(task_id, status="cancelled", error=reason, failure_reason="cancelled")
    await emit_activity(task_id, {"type": "task_cancelled", "reason": reason})


async def _default_mark_failed(task_id: str, error: str) -> None:
    from runtime.agent_loop import emit_activity

    reason = classify_failure(error)
    await _persist_terminal(task_id, status="failed", error=error, failure_reason=reason, dead_letter=True)
    await emit_activity(task_id, {"type": "task_failed", "error": error, "failure_reason": reason, "dead_letter": True})
    # Alert the org that a task failed permanently (best-effort; gated by the
    # org's notification settings).
    try:
        from core import notifications
        from runtime.agent_loop import get_task

        task = await get_task(task_id)
        if task:
            await notifications.emit(
                organization_id=task["organization_id"],
                type="task_failure",
                title="Task failed",
                body=(task.get("goal") or "A task failed.") + f" ({reason})",
                severity="critical",
                resource_type="task",
                resource_id=str(task_id),
                created_by="chronos",
            )
    except Exception:
        pass


class TaskRunner:
    """Small in-process priority runner used until the platform grows workers.

    The interface is intentionally queue-like so it can later be backed by Redis
    leases or a worker pool without changing routers or startup recovery.
    """

    def __init__(
        self,
        *,
        run_task: RunTask | None = None,
        mark_cancelled: MarkCancelled | None = None,
        mark_failed: MarkFailed | None = None,
        mark_paused: MarkPaused | None = None,
        max_concurrency: int = 4,
        max_attempts: int = 1,
        task_timeout_seconds: float | None = None,
    ) -> None:
        self._run_task = run_task or _default_run_task
        self._mark_cancelled = mark_cancelled or self._builtin_mark_cancelled
        self._mark_failed = mark_failed or self._builtin_mark_failed
        self._mark_paused = mark_paused or self._builtin_mark_paused
        self._last_attempts: dict[str, int] = {}
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_attempts = max(1, int(max_attempts))
        self._task_timeout_seconds = task_timeout_seconds
        self._queue: list[tuple[int, int, str]] = []
        self._sequence = itertools.count()
        self._queued: set[str] = set()
        self._cancelled: dict[str, str] = {}
        self._paused: dict[str, str] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._worker_task: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._closed = False

    async def _builtin_mark_cancelled(self, task_id: str, reason: str) -> None:
        from runtime.agent_loop import emit_activity

        attempts = self._last_attempts.pop(task_id, 0)
        await _persist_terminal(
            task_id, status="cancelled", error=reason, failure_reason="cancelled", attempts=attempts
        )
        await emit_activity(task_id, {"type": "task_cancelled", "reason": reason})

    async def _builtin_mark_failed(self, task_id: str, error: str) -> None:
        from runtime.agent_loop import emit_activity

        reason = classify_failure(error)
        attempts = self._last_attempts.pop(task_id, 1)
        await _persist_terminal(
            task_id,
            status="failed",
            error=error,
            failure_reason=reason,
            dead_letter=True,
            attempts=attempts,
        )
        await emit_activity(
            task_id,
            {"type": "task_failed", "error": error, "failure_reason": reason,
             "dead_letter": True, "attempts": attempts},
        )

    async def _builtin_mark_paused(self, task_id: str, reason: str) -> None:
        """Persist a cooperative pause when the runner is used directly.

        The HTTP intervention path persists and emits before cancelling a local
        coroutine.  This fallback keeps direct/queued runner use equally safe
        and is idempotent when that intervention already happened.
        """
        from runtime.agent_loop import emit_activity, get_task, save_task

        task = await get_task(task_id)
        if task and task.get("status") == "paused":
            return
        state = dict((task or {}).get("agent_state") or {})
        state["pause"] = {
            "reason": reason,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        await save_task(
            task_id,
            status="paused",
            agent_state=state,
            error=None,
            completed_at=None,
        )
        await emit_activity(task_id, {"type": "task_paused", "reason": reason})

    async def requeue(self, task_id: str, *, priority: int = 10) -> bool:
        """Revive a dead-lettered/failed task: clear terminal state and re-enqueue."""
        from runtime.agent_loop import save_task

        await save_task(
            task_id, status="queued", dead_letter=False, failure_reason=None, error=None
        )
        self._last_attempts.pop(task_id, None)
        await self.enqueue(task_id, priority=priority)
        return True

    async def enqueue(self, task_id: str, *, priority: int = 10) -> None:
        if task_id in self._queued or task_id in self._running:
            return
        heapq.heappush(self._queue, (priority, next(self._sequence), task_id))
        self._queued.add(task_id)
        self._wake.set()

    def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> bool:
        if task_id in self._running:
            self._running[task_id].cancel()
            self._cancelled[task_id] = reason
            return True
        if task_id in self._queued:
            self._cancelled[task_id] = reason
            self._wake.set()
            return True
        return False

    def pause(self, task_id: str, *, reason: str = "operator_pause") -> bool:
        """Stop queued work or cancel a local coroutine at its checkpoint.

        A running coroutine is cancelled only after the caller has persisted
        ``status=paused``.  The executor therefore leaves the durable checkpoint
        intact, and the lease is released by ``_run_with_policy``'s ``finally``.
        Workers in another process observe the persisted status at the agent
        loop's safe boundaries.
        """
        if task_id in self._running:
            self._paused[task_id] = reason
            self._running[task_id].cancel()
            return True
        if task_id in self._queued:
            self._paused[task_id] = reason
            self._wake.set()
            return True
        return False

    def clear_pause(self, task_id: str) -> None:
        self._paused.pop(task_id, None)

    async def drain_once(self) -> bool:
        while self._queue:
            _, _, task_id = heapq.heappop(self._queue)
            self._queued.discard(task_id)
            pause_reason = self._paused.pop(task_id, None)
            if pause_reason:
                await self._mark_paused(task_id, pause_reason)
                return True
            reason = self._cancelled.pop(task_id, None)
            if reason:
                await self._mark_cancelled(task_id, reason)
                return True
            await self._run_with_policy(task_id)
            return True
        return False

    def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._closed = False
        self._worker_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._closed = True
        self._wake.set()
        # A worker shutdown is resumable, not a user cancellation. Persist the
        # queued state before cancelling local coroutines so another replica or
        # the next process can claim the checkpoint exactly once.
        if self._running:
            from runtime.agent_loop import save_task

            for task_id in list(self._running):
                await save_task(task_id, status="queued")
        running_tasks = list(self._running.values())
        for task in running_tasks:
            task.cancel()
        worker_task = self._worker_task
        self._worker_task = None
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        if running_tasks:
            # Cancellation runs each task's ``finally`` block, which stops its
            # lease heartbeat and releases the Redis lease. Await that cleanup
            # so a replacement replica can recover immediately instead of
            # waiting for the lease TTL. Keep the wait bounded: third-party
            # model/tool code can mishandle cancellation, and container shutdown
            # must still complete before the orchestrator sends SIGKILL.
            done, pending = await asyncio.wait(
                running_tasks,
                timeout=_SHUTDOWN_TASK_WAIT_SECONDS,
            )
            for task in done:
                if not task.cancelled():
                    task.exception()  # retrieve/loggable exception; do not leak it
            if pending:
                import logging

                logging.getLogger(__name__).warning(
                    "task runner shutdown timed out waiting for %d task(s)",
                    len(pending),
                )

    async def _run_forever(self) -> None:
        while not self._closed:
            while len(self._running) < self._max_concurrency and self._queue:
                _, _, task_id = heapq.heappop(self._queue)
                self._queued.discard(task_id)
                pause_reason = self._paused.pop(task_id, None)
                if pause_reason:
                    await self._mark_paused(task_id, pause_reason)
                    continue
                reason = self._cancelled.pop(task_id, None)
                if reason:
                    await self._mark_cancelled(task_id, reason)
                    continue
                self._running[task_id] = asyncio.create_task(self._run_one(task_id))
            self._wake.clear()
            if not self._queue:
                await self._wake.wait()
            else:
                await asyncio.sleep(0)

    async def _run_one(self, task_id: str) -> None:
        try:
            await self._run_with_policy(task_id)
        except asyncio.CancelledError:
            pause_reason = self._paused.pop(task_id, None)
            if pause_reason:
                await self._mark_paused(task_id, pause_reason)
            elif self._closed:
                # ``stop`` already persisted ``queued`` for restart recovery.
                pass
            else:
                reason = self._cancelled.pop(task_id, "user_cancelled")
                await self._mark_cancelled(task_id, reason)
        finally:
            self._running.pop(task_id, None)
            self._wake.set()

    async def _run_with_policy(self, task_id: str) -> None:
        # Durable lease: claim the task before running. A peer worker (or a
        # startup/reaper requeue) that already owns it is refused, so a task is
        # never executed twice concurrently. The lease is renewed on a heartbeat
        # and released when the task finishes — if this worker dies, the lease
        # expires and the reaper re-queues the task.
        if not await leases.acquire_task_lease(task_id):
            return
        heartbeat = asyncio.create_task(self._heartbeat(task_id))
        try:
            last_error = "task_failed"
            for attempt in range(1, self._max_attempts + 1):
                self._last_attempts[task_id] = attempt
                try:
                    if self._task_timeout_seconds is None:
                        await self._run_task(task_id)
                    else:
                        await asyncio.wait_for(self._run_task(task_id), timeout=self._task_timeout_seconds)
                    return
                except asyncio.TimeoutError:
                    last_error = "task_timeout"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = str(exc) or type(exc).__name__
                    if attempt >= self._max_attempts:
                        break
            await self._mark_failed(task_id, last_error)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await leases.release_task_lease(task_id)

    async def _heartbeat(self, task_id: str) -> None:
        """Renew the task lease periodically so a live worker keeps ownership."""
        from core.config import settings

        interval = max(1, int(settings.task_lease_heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            await leases.renew_task_lease(task_id)


def _configured_runner() -> TaskRunner:
    from core.config import settings

    return TaskRunner(
        max_concurrency=settings.task_runner_max_concurrency,
        max_attempts=settings.task_runner_max_attempts,
        task_timeout_seconds=settings.task_runner_timeout_seconds,
    )


runner = _configured_runner()


async def enqueue_task(task_id: str, priority: int = 10) -> None:
    await runner.enqueue(task_id, priority=priority)


async def _active_task_ids() -> list[str]:
    from sqlalchemy import select

    from core.db import engine, reflect_table

    tasks_table = await reflect_table("tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tasks_table.c.id).where(tasks_table.c.status.in_(["planning", "running"]))
            )
        ).all()
    return [str(row[0]) for row in rows]


async def reap_orphaned_tasks(task_ids: list[str] | None = None) -> list[str]:
    """Re-queue active tasks whose owning worker died (lease expired).

    A task in ``planning``/``running`` always has a live lease while its worker is
    alive (claimed before execution). If the lease is gone, the worker crashed —
    revive the task so another worker resumes it from its last checkpoint. This is
    crash recovery between restarts, complementing the startup-time recovery scan.
    """
    from runtime.agent_loop import save_task

    ids = task_ids if task_ids is not None else await _active_task_ids()
    reaped: list[str] = []
    for task_id in ids:
        if await leases.task_lease_held(task_id):
            continue  # a live worker still owns it
        await save_task(task_id, status="queued")
        await runner.enqueue(task_id, priority=5)  # slightly ahead of fresh work
        reaped.append(task_id)
    return reaped


def cancel_task(task_id: str, reason: str = "user_cancelled") -> bool:
    return runner.cancel(task_id, reason=reason)


async def pause_task(task_id: str, reason: str = "operator_pause") -> bool:
    """Persist a pause request and stop local execution if this worker owns it."""
    from sqlalchemy import select, update

    from core.db import engine, reflect_table
    from runtime.agent_loop import emit_activity

    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        task = (
            await conn.execute(
                select(tasks.c.status, tasks.c.agent_state)
                .where(tasks.c.id == task_id)
                .with_for_update()
            )
        ).mappings().first()
        if not task or task.get("status") in {"complete", "failed", "cancelled", "paused"}:
            return False
        state = dict(task.get("agent_state") or {})
        state["pause"] = {
            "reason": reason,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        updated = (
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id, tasks.c.status == task["status"])
                .values(
                    status="paused",
                    agent_state=state,
                    error=None,
                    completed_at=None,
                )
                .returning(tasks.c.id)
            )
        ).first()
    if not updated:
        return False
    await emit_activity(task_id, {"type": "task_paused", "reason": reason})
    runner.pause(task_id, reason=reason)
    return True


async def resume_task(task_id: str, priority: int = 5) -> bool:
    """Atomically resume a paused task exactly once across API replicas."""
    from sqlalchemy import select, update

    from core.db import engine, reflect_table
    from runtime.agent_loop import emit_activity

    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks.c.agent_state).where(
                    tasks.c.id == task_id,
                    tasks.c.status == "paused",
                )
            )
        ).mappings().first()
        if not row:
            return False
        state = dict(row.get("agent_state") or {})
        pause = dict(state.pop("pause", {}) or {})
        updated = (
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id, tasks.c.status == "paused")
                .values(
                    status="queued",
                    agent_state=state,
                    error=None,
                    completed_at=None,
                )
                .returning(tasks.c.id)
            )
        ).first()
    if not updated:
        return False
    runner.clear_pause(task_id)
    await emit_activity(
        task_id,
        {"type": "task_resumed", "pause_reason": pause.get("reason")},
    )
    await runner.enqueue(task_id, priority=priority)
    return True


async def requeue_task(task_id: str, priority: int = 10) -> bool:
    return await runner.requeue(task_id, priority=priority)


def start_runner() -> None:
    runner.start()


async def stop_runner() -> None:
    await runner.stop()
