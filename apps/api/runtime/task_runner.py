from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any


RunTask = Callable[[str], Awaitable[Any]]
MarkCancelled = Callable[[str, str], Awaitable[Any]]
MarkFailed = Callable[[str, str], Awaitable[Any]]


async def _default_run_task(task_id: str) -> Any:
    from runtime.executor import TaskExecutor

    return await TaskExecutor().run(task_id)


async def _default_mark_cancelled(task_id: str, reason: str) -> None:
    from runtime.agent_loop import emit_activity, save_task

    await save_task(
        task_id,
        status="cancelled",
        error=reason,
        completed_at=datetime.now(timezone.utc),
    )
    await emit_activity(task_id, {"type": "task_cancelled", "reason": reason})


async def _default_mark_failed(task_id: str, error: str) -> None:
    from runtime.agent_loop import emit_activity, save_task

    await save_task(
        task_id,
        status="failed",
        error=error,
        completed_at=datetime.now(timezone.utc),
    )
    await emit_activity(task_id, {"type": "task_failed", "error": error})


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
        max_concurrency: int = 4,
        max_attempts: int = 1,
        task_timeout_seconds: float | None = None,
    ) -> None:
        self._run_task = run_task or _default_run_task
        self._mark_cancelled = mark_cancelled or _default_mark_cancelled
        self._mark_failed = mark_failed or _default_mark_failed
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_attempts = max(1, int(max_attempts))
        self._task_timeout_seconds = task_timeout_seconds
        self._queue: list[tuple[int, int, str]] = []
        self._sequence = itertools.count()
        self._queued: set[str] = set()
        self._cancelled: dict[str, str] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._worker_task: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._closed = False

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

    async def drain_once(self) -> bool:
        while self._queue:
            _, _, task_id = heapq.heappop(self._queue)
            self._queued.discard(task_id)
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
        for task in list(self._running.values()):
            task.cancel()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while not self._closed:
            while len(self._running) < self._max_concurrency and self._queue:
                _, _, task_id = heapq.heappop(self._queue)
                self._queued.discard(task_id)
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
            reason = self._cancelled.pop(task_id, "user_cancelled")
            await self._mark_cancelled(task_id, reason)
        finally:
            self._running.pop(task_id, None)
            self._wake.set()

    async def _run_with_policy(self, task_id: str) -> None:
        last_error = "task_failed"
        for attempt in range(1, self._max_attempts + 1):
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


def cancel_task(task_id: str, reason: str = "user_cancelled") -> bool:
    return runner.cancel(task_id, reason=reason)


def start_runner() -> None:
    runner.start()


async def stop_runner() -> None:
    await runner.stop()
