from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_cleanup_provider_failure_is_redacted_retried_and_idempotent(monkeypatch):
    from runtime import cancellation

    request = {
        "id": "cleanup-1",
        "organization_id": "org-a",
        "task_id": "task-a",
        "task_ids": ["task-a"],
        "requested_by": "member-a",
        "attempts": 1,
    }
    claims = [dict(request), {**request, "attempts": 2}]
    calls = {"stable": 0, "provider": 0}
    provider_healthy = False
    finishes: list[tuple[dict, dict]] = []

    async def claim(_request_id):
        return claims.pop(0)

    async def stable(_org, _tasks, _actor):
        calls["stable"] += 1
        return {"status": "complete"}

    async def provider(_org, _tasks, _actor):
        calls["provider"] += 1
        if not provider_healthy:
            raise RuntimeError(
                "POST https://provider.example/session Authorization: Bearer super-secret"
            )
        return {"status": "complete"}

    async def finish(_request, *, summary, errors):
        finishes.append((summary, errors))
        return {
            **_request,
            "status": "retry" if errors else "complete",
            "summary": summary,
            "last_error": next(iter(errors.values()), None),
            "next_attempt_at": None,
            "completed_at": None if errors else cancellation._now(),
        }

    async def no_audit(*_args, **_kwargs):
        return "audit"

    monkeypatch.setattr(cancellation, "_claim_cleanup", claim)
    monkeypatch.setattr(
        cancellation,
        "_resource_cleaners",
        lambda: (("stable", stable), ("provider", provider)),
    )
    monkeypatch.setattr(cancellation, "_finish_cleanup", finish)
    monkeypatch.setattr(cancellation.audit, "log", no_audit)

    first = await cancellation.run_cleanup_request("cleanup-1")
    assert first["status"] == "retry"
    serialized = str(finishes[0])
    assert "provider.example" not in serialized
    assert "super-secret" not in serialized
    assert "[redacted-url]" in serialized

    # Simulate the leader reaper claiming the durable row after a process restart.
    provider_healthy = True
    second = await cancellation.run_cleanup_request("cleanup-1")
    assert second["status"] == "complete"
    # The successful cleaner is intentionally safe to replay when a peer failed.
    assert calls == {"stable": 2, "provider": 2}


@pytest.mark.asyncio
async def test_running_connector_job_is_cooperatively_cancelled():
    from connectors.framework.models import ConnectorResult
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker

    class SlowAdapter:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def validate_credentials(self, _credentials):
            return True

        async def execute(self, _action, _arguments, _context):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return ConnectorResult(status="success", output={"unsafe": True})

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    adapter = SlowAdapter()
    job = await repo.create_execution_job(
        tenant_id="org-a",
        task_id="task-a",
        workspace_id="default",
        employee_id="task:task-a",
        user_id="member-a",
        connector_id="slow",
        action_name="send",
        arguments={},
        max_attempts=1,
        timeout_ms=10_000,
    )
    await queue.enqueue(
        {
            "id": job["id"],
            "tenant_id": "org-a",
            "task_id": "task-a",
            "workspace_id": "default",
            "employee_id": "task:task-a",
            "user_id": "member-a",
            "connector_id": "slow",
            "action_name": "send",
            "arguments": {},
            "max_attempts": 1,
            "timeout_ms": 10_000,
        }
    )
    worker = ConnectorWorker(repo, {"slow": adapter}, queue)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    await repo.cancel_execution_job(job["id"], tenant_id="org-a")
    result = await asyncio.wait_for(running, timeout=2)

    assert result["status"] == "cancelled"
    assert result["result"] is None
    assert adapter.cancelled.is_set()
    stored = await repo.get_execution_job(job["id"], tenant_id="org-a")
    assert stored["status"] == "cancelled"


@pytest.mark.asyncio
async def test_computer_task_cleanup_never_kills_sibling_scope(monkeypatch):
    import connectors.computer as computer_module
    from connectors.computer import ComputerConnector

    class Runtime:
        def __init__(self):
            self.killed: list[str] = []

        async def resume(self, sandbox_id, **_kwargs):
            return sandbox_id

        async def kill(self, sandbox_id):
            self.killed.append(sandbox_id)

    connector = ComputerConnector()
    runtime = Runtime()
    connector._runtime = runtime
    now = computer_module._now()

    def session(session_id, org_id, task_id, sandbox_id):
        return {
            "id": session_id,
            "organization_id": org_id,
            "region": "us",
            "task_id": task_id,
            "member_id": "member-a",
            "status": "active",
            "environment": {
                "sandbox_id": sandbox_id,
                "metadata": {
                    "chronos_tenant": computer_module._tenant_marker(org_id),
                    "chronos_session": session_id,
                },
            },
            "resource_limits": {},
            "network_policy": {},
            "editor_state": {},
            "history": [],
            "created_at": now,
            "updated_at": now,
        }

    connector._sessions = {
        "owned": session("owned", "org-a", "task-a", "sandbox-owned"),
        "sibling-task": session("sibling-task", "org-a", "task-b", "sandbox-task-b"),
        "sibling-org": session("sibling-org", "org-b", "task-a", "sandbox-org-b"),
    }

    async def storage_unavailable(_name):
        raise RuntimeError("test fallback")

    async def no_save(_session):
        return None

    async def no_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(computer_module, "reflect_table", storage_unavailable)
    monkeypatch.setattr(connector, "_save_session", no_save)
    monkeypatch.setattr(connector, "_record_event", no_event)
    result = await connector.cancel_task_sessions(
        organization_id="org-a", task_ids=["task-a"], member_id="member-a"
    )

    assert result == {"status": "complete", "cancelled": 1}
    assert runtime.killed == ["sandbox-owned"]


def test_task_cleanup_migration_and_leader_reaper_are_wired():
    root = Path(__file__).parents[1]
    migration = (root / "migrations/versions/0060_task_cleanup.py").read_text()
    main = (root / "main.py").read_text()
    scheduler = (root / "jobs/task_cleanup.py").read_text()

    assert 'down_revision = "0059_custom_integrations"' in migration
    assert '"task_cleanup_requests"' in migration
    assert '"connector_execution_jobs", sa.Column("task_id"' in migration
    assert '"desktop_commands", sa.Column("task_id"' in migration
    assert "task_cleanup_jobs.scheduler" in main
    assert '"Task cancellation cleanup", task_cleanup_jobs.reap_task_cleanups' in main
    assert "reap_pending_task_cleanups" in scheduler


@pytest.mark.asyncio
async def test_duplicate_cancel_and_cross_tenant_rejection(monkeypatch):
    """Database proof for the unique request and tenant-bound task lookup."""

    from sqlalchemy import delete, func, insert, select

    from core.db import engine, reflect_table
    from runtime import cancellation

    tasks = await reflect_table("tasks")
    try:
        cleanups = await reflect_table("task_cleanup_requests")
    except Exception:
        pytest.skip("0060_task_cleanup migration is not applied")

    org = f"cancel-{uuid.uuid4().hex[:8]}"
    task_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(tasks),
            [
                {
                    "id": task_id,
                    "organization_id": org,
                    "parent_task_id": None,
                    "goal": "root",
                    "triggered_by": "test",
                    "status": "running",
                },
                {
                    "id": child_id,
                    "organization_id": org,
                    "parent_task_id": task_id,
                    "goal": "child",
                    "triggered_by": "test",
                    "status": "running",
                },
            ],
        )

    async def no_audit(*_args, **_kwargs):
        return "audit"

    async def no_activity(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cancellation, "_spawn_cleanup", lambda _request_id: None)
    monkeypatch.setattr(cancellation.audit, "log", no_audit)
    monkeypatch.setattr("runtime.agent_loop.emit_activity", no_activity)
    try:
        first = await cancellation.request_task_cancellation(
            organization_id=org,
            task_id=task_id,
            actor_id="member-a",
        )
        second = await cancellation.request_task_cancellation(
            organization_id=org,
            task_id=task_id,
            actor_id="member-a",
        )
        assert first["cleanup"]["id"] == second["cleanup"]["id"]
        assert first["cancelled"] is True
        assert second["cancelled"] is False

        with pytest.raises(KeyError):
            await cancellation.request_task_cancellation(
                organization_id="foreign-org",
                task_id=task_id,
                actor_id="foreign-member",
            )

        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.status).where(
                        tasks.c.id.in_([task_id, child_id])
                    )
                )
            ).all()
            count = (
                await conn.execute(
                    select(func.count()).select_from(cleanups).where(
                        cleanups.c.organization_id == org,
                        cleanups.c.task_id == task_id,
                    )
                )
            ).scalar_one()
        assert {str(row[0]): row[1] for row in rows} == {
            task_id: "cancelled",
            child_id: "cancelled",
        }
        assert count == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                delete(cleanups).where(cleanups.c.organization_id == org)
            )
            await conn.execute(delete(tasks).where(tasks.c.id.in_([task_id, child_id])))


@pytest.mark.asyncio
async def test_reaper_recovers_expired_cleanup_claim_after_restart(monkeypatch):
    from datetime import timedelta

    from sqlalchemy import delete, insert, select

    from core.db import engine, reflect_table
    from runtime import cancellation

    tasks = await reflect_table("tasks")
    try:
        cleanups = await reflect_table("task_cleanup_requests")
    except Exception:
        pytest.skip("0060_task_cleanup migration is not applied")
    org = f"reaper-{uuid.uuid4().hex[:8]}"
    task_id = str(uuid.uuid4())
    cleanup_id = str(uuid.uuid4())
    now = cancellation._now()
    async with engine.begin() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id,
                organization_id=org,
                goal="restart cleanup",
                triggered_by="test",
                status="cancelled",
            )
        )
        await conn.execute(
            insert(cleanups).values(
                id=cleanup_id,
                organization_id=org,
                region="us",
                task_id=task_id,
                task_ids=[task_id],
                requested_by="member-a",
                reason="user_cancelled",
                status="running",
                attempts=1,
                next_attempt_at=now - timedelta(minutes=2),
                lease_owner="dead-worker",
                lease_expires_at=now - timedelta(minutes=1),
            )
        )

    async def cleaner(_org, task_ids, _actor):
        assert task_ids == [task_id]
        return {"status": "complete", "closed": 1}

    async def no_audit(*_args, **_kwargs):
        return "audit"

    monkeypatch.setattr(
        cancellation, "_resource_cleaners", lambda: (("provider", cleaner),)
    )
    monkeypatch.setattr(cancellation.audit, "log", no_audit)
    try:
        reaped = await cancellation.reap_pending_task_cleanups()
        assert cleanup_id in reaped
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(cleanups).where(cleanups.c.id == cleanup_id)
                )
            ).mappings().one()
        assert row["status"] == "complete"
        assert row["attempts"] == 2
        assert row["lease_owner"] is None
        assert row["completed_at"] is not None
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(cleanups).where(cleanups.c.id == cleanup_id))
            await conn.execute(delete(tasks).where(tasks.c.id == task_id))
