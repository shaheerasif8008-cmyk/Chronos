from __future__ import annotations
"""
Durable runtime coordination — Redis leases + a distributed lock.

The in-process TaskRunner is fine for a single worker, but the platform runs
multiple API workers. Two coordination primitives make that safe:

* **Task lease** — a worker claims ``task_lease:{id}`` (SET NX EX) before it runs
  a task and renews it on a heartbeat. A second worker (or a startup-recovery
  scan) that tries to claim a held task is refused, so a task never runs twice
  concurrently. If the owning worker dies, the lease expires and the reaper
  re-queues the task — crash recovery without a full fleet restart.

* **Single-holder lock** — ``acquire_lock``/``release_lock`` (token-guarded) so a
  periodic job like the scheduler poll fires on exactly one worker per tick.

Everything degrades safely: if Redis is unavailable the primitives behave as a
single-process system (claims succeed, locks are granted) so the runtime keeps
working — coordination is a multi-worker safety layer, not a hard dependency.
"""
import logging
import os
import socket
import uuid

from core.config import settings
from core.redis import redis_client

log = logging.getLogger(__name__)

# Stable-ish identifier for this worker process, for lease ownership + debugging.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_LEASE_PREFIX = "task_lease:"

# Release a lock only if we still own it (token match) — never drop a peer's lock.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""
# Renew a lease only if we still own it.
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


def _lease_key(task_id: str) -> str:
    return f"{_LEASE_PREFIX}{task_id}"


async def acquire_task_lease(task_id: str, *, ttl: int | None = None) -> bool:
    """Claim the lease for a task. Returns True if this worker now owns it.

    Degrades to True (claim granted) when Redis is unavailable so a single
    process keeps running.
    """
    ttl = ttl or settings.task_lease_ttl_seconds
    try:
        ok = await redis_client.set(_lease_key(task_id), WORKER_ID, nx=True, ex=ttl)
        return bool(ok)
    except Exception as exc:
        log.debug("task lease acquire degraded (granting): %s", exc)
        return True


async def renew_task_lease(task_id: str, *, ttl: int | None = None) -> bool:
    ttl = ttl or settings.task_lease_ttl_seconds
    try:
        result = await redis_client.eval(_RENEW_LUA, 1, _lease_key(task_id), WORKER_ID, str(ttl))
        return bool(result)
    except Exception as exc:
        log.debug("task lease renew degraded: %s", exc)
        return True


async def release_task_lease(task_id: str) -> None:
    try:
        await redis_client.eval(_RELEASE_LUA, 1, _lease_key(task_id), WORKER_ID)
    except Exception as exc:
        log.debug("task lease release degraded: %s", exc)


async def task_lease_held(task_id: str) -> bool:
    """True when *some* live worker holds the lease (key exists)."""
    try:
        return bool(await redis_client.exists(_lease_key(task_id)))
    except Exception as exc:
        log.debug("task lease check degraded (assuming unheld): %s", exc)
        return False


async def acquire_lock(name: str, *, ttl: int) -> str | None:
    """Acquire a single-holder lock. Returns an ownership token, or None if held.

    Degrades to a token (granted) when Redis is unavailable.
    """
    token = uuid.uuid4().hex
    try:
        ok = await redis_client.set(f"lock:{name}", token, nx=True, ex=ttl)
        return token if ok else None
    except Exception as exc:
        log.debug("lock acquire degraded (granting): %s", exc)
        return token


async def release_lock(name: str, token: str) -> None:
    try:
        await redis_client.eval(_RELEASE_LUA, 1, f"lock:{name}", token)
    except Exception as exc:
        log.debug("lock release degraded: %s", exc)
