from __future__ import annotations

from connectors.framework.queue import RedisExecutionQueue
from core.redis import redis_client


def connector_execution_queue() -> RedisExecutionQueue:
    return RedisExecutionQueue(redis_client)
