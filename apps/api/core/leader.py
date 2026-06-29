"""Redis-backed leader election.

The API process runs in-process APScheduler schedulers (profile synthesis,
context update, scheduled tasks) and the startup recovery sweep. Those must run
on exactly ONE instance — otherwise scaling the web service to N replicas fires
every schedule N times and recovers every interrupted run N times.

This module elects a single leader via a Redis lock with a TTL. The leader
renews the lock on a heartbeat; if it dies, the lock expires and another
instance acquires it on its next poll. Acquisition/loss invoke caller-supplied
callbacks (e.g. resume/pause the schedulers).

The lock value is the instance id, and renew/release are guarded by a
compare-and-act Lua script so an instance can never renew or delete a lock it no
longer owns (avoids the classic "expired then someone else acquired, original
owner deletes it" race).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# Extend the TTL only if we still own the lock.
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Delete the lock only if we still own it.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

Callback = Callable[[], Awaitable[None] | None]


class LeaderElection:
    def __init__(
        self,
        redis,
        key: str,
        *,
        instance_id: str | None = None,
        ttl_seconds: int = 30,
        poll_seconds: int = 10,
        on_acquire: Callback | None = None,
        on_release: Callback | None = None,
    ) -> None:
        if ttl_seconds <= poll_seconds:
            raise ValueError("ttl_seconds must exceed poll_seconds so the lock is renewed before it expires")
        self._redis = redis
        self._key = key
        self.instance_id = instance_id or uuid.uuid4().hex
        self._ttl = ttl_seconds
        self._poll = poll_seconds
        self._on_acquire = on_acquire
        self._on_release = on_release
        self.is_leader = False
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def _try_acquire(self) -> bool:
        return bool(await self._redis.set(self._key, self.instance_id, nx=True, ex=self._ttl))

    async def _try_renew(self) -> bool:
        result = await self._redis.eval(_RENEW_SCRIPT, 1, self._key, self.instance_id, self._ttl)
        return bool(result)

    async def _release_lock(self) -> None:
        try:
            await self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self.instance_id)
        except Exception:
            log.warning("leader: lock release failed", exc_info=True)

    async def _fire(self, callback: Callback | None) -> None:
        if callback is None:
            return
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("leader: callback failed")

    async def _tick(self) -> None:
        """One election step: renew if leader, otherwise try to take the lock."""
        if self.is_leader:
            if not await self._try_renew():
                # Lost the lock (e.g. a GC pause let it expire). Stand down.
                self.is_leader = False
                log.warning("leader: lost leadership for %s", self._key)
                await self._fire(self._on_release)
        else:
            if await self._try_acquire():
                self.is_leader = True
                log.info("leader: acquired leadership for %s (instance=%s)", self._key, self.instance_id)
                await self._fire(self._on_acquire)

    async def _loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await self._tick()
                except Exception:
                    log.warning("leader: election tick failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self.is_leader:
                self.is_leader = False
                await self._fire(self._on_release)
                await self._release_lock()

    async def start(self) -> None:
        """Begin the election loop. Acquires immediately if the lock is free."""
        self._stopped.clear()
        await self._tick()  # immediate attempt so a sole instance leads without waiting a poll
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop, release the lock if held, and run on_release once."""
        self._stopped.set()
        if self._task is not None:
            await self._task
            self._task = None
