from __future__ import annotations

import asyncio
from typing import Any, Protocol


class ExecutionQueue(Protocol):
    async def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        ...

    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        ...


class InMemoryExecutionQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        await self._queue.put(job)
        return job

    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return None


class RedisExecutionQueue:
    def __init__(self, redis: Any, queue_name: str = "chronos:connector_jobs") -> None:
        self.redis = redis
        self.queue_name = queue_name

    async def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        import json

        await self.redis.lpush(self.queue_name, json.dumps(job))
        return job

    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        import json

        item = await self.redis.brpop(self.queue_name, timeout=max(1, int(timeout_seconds)))
        if not item:
            return None
        _, payload = item
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)
