from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_leader_election_elects_single_leader_with_failover():
    """Only one instance leads at a time; when the leader stops, another takes over.

    This is what makes running multiple API instances safe — the schedulers and
    startup recovery run only on the leader.
    """
    from core.leader import LeaderElection
    from core.redis import redis_client

    key = f"test:leader:{uuid.uuid4().hex}"
    acquired: list[str] = []

    a = LeaderElection(
        redis_client, key, ttl_seconds=5, poll_seconds=1,
        on_acquire=lambda: acquired.append("a"),
    )
    b = LeaderElection(
        redis_client, key, ttl_seconds=5, poll_seconds=1,
        on_acquire=lambda: acquired.append("b"),
    )

    await a.start()
    try:
        assert a.is_leader is True
        assert acquired == ["a"]
        # While A holds the lock, B cannot acquire it.
        await b._tick()
        assert b.is_leader is False
    finally:
        await a.stop()  # releases the lock

    # With A stood down, B acquires on its next election step.
    await b._tick()
    assert b.is_leader is True
    assert acquired == ["a", "b"]

    await b._release_lock()


@pytest.mark.asyncio
async def test_leader_renew_only_extends_lock_owned_by_self():
    """A renew never resurrects a lock owned by someone else (no split-brain)."""
    from core.leader import LeaderElection
    from core.redis import redis_client

    key = f"test:leader:{uuid.uuid4().hex}"
    a = LeaderElection(redis_client, key, ttl_seconds=5, poll_seconds=1)
    b = LeaderElection(redis_client, key, ttl_seconds=5, poll_seconds=1)

    assert await a._try_acquire() is True
    # B thinks it might be leader but does not own the lock → renew must fail.
    assert await b._try_renew() is False
    # A owns it → renew succeeds.
    assert await a._try_renew() is True

    await a._release_lock()


def test_leader_election_rejects_ttl_not_exceeding_poll():
    from core.leader import LeaderElection
    from core.redis import redis_client

    with pytest.raises(ValueError):
        LeaderElection(redis_client, "k", ttl_seconds=5, poll_seconds=5)
