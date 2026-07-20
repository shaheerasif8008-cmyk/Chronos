"""Durable, tenant-bound cleanup for cancelled tasks.

The task row is the cancellation signal observed by model loops on every API
replica.  This module owns the second half of cancellation: terminating only
the external/local runtimes bound to that tenant and task.  A durable request
and expiring claim make cleanup restart-safe; every cleaner is idempotent so an
expired claim or duplicate request can be retried without touching peer tasks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from runtime import leases, task_runner


log = logging.getLogger(__name__)
_CLAIM_SECONDS = 300
_MAX_ERROR_LENGTH = 2000
_BACKGROUND: set[asyncio.Task[Any]] = set()
Cleaner = Callable[[str, Sequence[str], str], Awaitable[dict[str, Any]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "status": str(row["status"]),
        "attempts": int(row.get("attempts") or 0),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_error": row.get("last_error"),
        "summary": dict(row.get("summary") or {}),
        "completed_at": row.get("completed_at"),
    }


def _safe_error(exc: BaseException) -> str:
    """Keep retry evidence useful without persisting provider URLs or secrets."""

    message = (str(exc) or "cleanup operation failed")[:1000]
    message = re.sub(r"https?://\S+", "[redacted-url]", message, flags=re.IGNORECASE)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_ -]?key|token|secret)\b\s*[:=]\s*\S+",
        r"\1=[redacted]",
        message,
    )
    return f"{type(exc).__name__}: {message}"[:500]


async def _task_tree(
    conn: Any,
    tasks: Any,
    *,
    organization_id: str,
    root_task_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = (
        await conn.execute(
            select(tasks)
            .where(
                tasks.c.id == root_task_id,
                tasks.c.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).mappings().first()
    if root is None:
        return None, []
    ids = [root_task_id]
    seen = {root_task_id}
    frontier = [root_task_id]
    while frontier:
        rows = (
            await conn.execute(
                select(tasks.c.id)
                .where(
                    tasks.c.organization_id == organization_id,
                    tasks.c.parent_task_id.in_(frontier),
                )
                .with_for_update()
            )
        ).all()
        frontier = []
        for child_id, in rows:
            value = str(child_id)
            if value not in seen:
                seen.add(value)
                ids.append(value)
                frontier.append(value)
    return dict(root), ids


async def request_task_cancellation(
    *,
    organization_id: str,
    task_id: str,
    actor_id: str,
    reason: str = "user_cancelled",
) -> dict[str, Any]:
    """Persist cancellation and one cleanup request for the task subtree.

    The tenant predicate is present on every lookup/update.  A duplicate call
    reuses the unique cleanup request and never broadens its task scope.
    """

    tasks = await reflect_table("tasks")
    cleanups = await reflect_table("task_cleanup_requests")
    now = _now()
    reason = (reason or "user_cancelled").strip()[:500] or "user_cancelled"
    created = False
    status_changed = False
    async with engine.begin() as conn:
        root, task_ids = await _task_tree(
            conn,
            tasks,
            organization_id=organization_id,
            root_task_id=task_id,
        )
        if root is None:
            raise KeyError(task_id)

        cancellable = {"queued", "pending", "planning", "running", "awaiting_approval", "paused"}
        status_changed = str(root.get("status")) in cancellable
        task_values: dict[str, Any] = {
            "status": "cancelled",
            "error": reason,
            "completed_at": now,
        }
        if "failure_reason" in tasks.c:
            task_values["failure_reason"] = "cancelled"
        await conn.execute(
            update(tasks)
            .where(
                tasks.c.organization_id == organization_id,
                tasks.c.id.in_(task_ids),
                tasks.c.status.in_(sorted(cancellable)),
            )
            .values(**task_values)
        )

        existing = (
            await conn.execute(
                select(cleanups)
                .where(
                    cleanups.c.organization_id == organization_id,
                    cleanups.c.task_id == task_id,
                )
                .with_for_update()
            )
        ).mappings().first()
        if existing is None:
            row = (
                await conn.execute(
                    pg_insert(cleanups)
                    .values(
                        organization_id=organization_id,
                        region=str(root.get("region") or settings.region),
                        task_id=task_id,
                        task_ids=task_ids,
                        requested_by=actor_id,
                        reason=reason,
                        status="pending",
                        next_attempt_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["organization_id", "task_id"]
                    )
                    .returning(cleanups)
                )
            ).mappings().first()
            created = row is not None
            if row is None:
                row = (
                    await conn.execute(
                        select(cleanups).where(
                            cleanups.c.organization_id == organization_id,
                            cleanups.c.task_id == task_id,
                        )
                    )
                ).mappings().one()
        else:
            row = existing

    for scoped_task_id in task_ids:
        task_runner.cancel_task(scoped_task_id, reason=reason)

    try:
        from runtime.agent_loop import emit_activity

        await emit_activity(
            task_id,
            {
                "type": "task_cleanup_requested",
                "cleanup_request_id": str(row["id"]),
                "task_count": len(task_ids),
                "reason": reason,
            },
            actor_id=actor_id,
        )
    except Exception:
        log.warning("Could not emit task cleanup request activity", exc_info=True)
    try:
        await audit.log(
            "task_cleanup_requested",
            actor_id,
            "tasks.cancel",
            organization_id=organization_id,
            resource_type="tasks",
            resource_id=task_id,
            decision="allowed",
            payload={
                "cleanup_request_id": str(row["id"]),
                "task_ids": task_ids,
                "duplicate": not created,
                "reason": reason,
            },
        )
    except Exception:
        log.warning("Could not audit task cleanup request", exc_info=True)

    _spawn_cleanup(str(row["id"]))
    return {
        "task_id": task_id,
        "status": "cancelled",
        "cancelled": status_changed,
        "cleanup": _public(dict(row)),
    }


def _spawn_cleanup(request_id: str) -> None:
    task = asyncio.create_task(run_cleanup_request(request_id))
    _BACKGROUND.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _BACKGROUND.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            log.warning(
                "Task cleanup background attempt failed: %s",
                type(completed.exception()).__name__,
            )

    task.add_done_callback(done)


async def get_task_cleanup(
    *, organization_id: str, task_id: str
) -> dict[str, Any] | None:
    table = await reflect_table("task_cleanup_requests")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    table.c.organization_id == organization_id,
                    table.c.task_id == task_id,
                )
            )
        ).mappings().first()
    return _public(dict(row)) if row else None


async def _claim_cleanup(request_id: str) -> dict[str, Any] | None:
    table = await reflect_table("task_cleanup_requests")
    now = _now()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(
                    table.c.id == request_id,
                    table.c.status != "complete",
                    table.c.next_attempt_at <= now,
                    or_(
                        table.c.status.in_(["pending", "retry"]),
                        table.c.lease_expires_at.is_(None),
                        table.c.lease_expires_at <= now,
                    ),
                )
                .values(
                    status="running",
                    attempts=table.c.attempts + 1,
                    lease_owner=leases.WORKER_ID,
                    lease_expires_at=now + timedelta(seconds=_CLAIM_SECONDS),
                    updated_at=now,
                )
                .returning(table)
            )
        ).mappings().first()
    return dict(row) if row else None


async def _finish_cleanup(
    request: dict[str, Any],
    *,
    summary: dict[str, Any],
    errors: dict[str, str],
) -> dict[str, Any] | None:
    table = await reflect_table("task_cleanup_requests")
    now = _now()
    attempts = int(request.get("attempts") or 1)
    if errors:
        delay = min(300, 2 ** min(attempts, 8))
        status = "retry"
        next_attempt = now + timedelta(seconds=delay)
        completed_at = None
        last_error = "; ".join(
            f"{name}: {message}" for name, message in sorted(errors.items())
        )[:_MAX_ERROR_LENGTH]
    else:
        status = "complete"
        next_attempt = now
        completed_at = now
        last_error = None
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(
                    table.c.id == request["id"],
                    table.c.lease_owner == leases.WORKER_ID,
                )
                .values(
                    status=status,
                    next_attempt_at=next_attempt,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=last_error,
                    summary=summary,
                    updated_at=now,
                    completed_at=completed_at,
                )
                .returning(table)
            )
        ).mappings().first()
    return dict(row) if row else None


async def run_cleanup_request(request_id: str) -> dict[str, Any] | None:
    request = await _claim_cleanup(request_id)
    if request is None:
        return None
    org_id = str(request["organization_id"])
    actor_id = str(request.get("requested_by") or "chronos")
    task_ids = [str(value) for value in list(request.get("task_ids") or [])]
    if str(request["task_id"]) not in task_ids:
        task_ids.insert(0, str(request["task_id"]))

    summary: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, cleaner in _resource_cleaners():
        try:
            summary[name] = await cleaner(org_id, task_ids, actor_id)
        except Exception as exc:  # noqa: BLE001 - every provider gets a retry record
            errors[name] = _safe_error(exc)
            summary[name] = {"status": "retry", "error": errors[name]}
            log.warning(
                "Task cleanup resource %s failed: %s", name, type(exc).__name__
            )

    row = await _finish_cleanup(request, summary=summary, errors=errors)
    event_type = "task_cleanup_retry" if errors else "task_cleanup_complete"
    try:
        await audit.log(
            event_type,
            actor_id,
            "tasks.cleanup",
            organization_id=org_id,
            resource_type="tasks",
            resource_id=str(request["task_id"]),
            decision="retry" if errors else "allowed",
            payload={
                "cleanup_request_id": str(request["id"]),
                "attempt": int(request.get("attempts") or 1),
                "summary": summary,
                "errors": errors,
            },
        )
    except Exception:
        log.warning("Could not audit task cleanup result", exc_info=True)
    return _public(row) if row else None


async def reap_pending_task_cleanups(*, limit: int = 100) -> list[str]:
    table = await reflect_table("task_cleanup_requests")
    now = _now()
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table.c.id)
                .where(
                    table.c.status != "complete",
                    table.c.next_attempt_at <= now,
                    or_(
                        table.c.status.in_(["pending", "retry"]),
                        table.c.lease_expires_at.is_(None),
                        table.c.lease_expires_at <= now,
                    ),
                )
                .order_by(table.c.next_attempt_at, table.c.created_at)
                .limit(max(1, min(limit, 500)))
            )
        ).all()
    request_ids = [str(row[0]) for row in rows]
    for request_id in request_ids:
        await run_cleanup_request(request_id)
    return request_ids


def _resource_cleaners() -> tuple[tuple[str, Cleaner], ...]:
    return (
        ("task_runtime", _cleanup_task_runtime),
        ("sub_agents", _cleanup_sub_agents),
        ("browser_sessions", _cleanup_browser_sessions),
        ("computer_sessions", _cleanup_computer_sessions),
        ("repo_workspaces", _cleanup_repo_workspaces),
        ("desktop_sessions", _cleanup_desktop_sessions),
        ("paired_device_commands", _cleanup_paired_device_commands),
        ("connector_jobs", _cleanup_connector_jobs),
        ("approvals", _cleanup_approvals),
    )


async def _cleanup_task_runtime(
    _organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    locally_signalled = 0
    for task_id in task_ids:
        if task_runner.cancel_task(task_id, reason="user_cancelled"):
            locally_signalled += 1
        # Token-guarded release only removes a lease owned by this process.
        await leases.release_task_lease(task_id)
    return {"status": "complete", "tasks": len(task_ids), "locally_signalled": locally_signalled}


async def _cleanup_sub_agents(
    organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        remaining = (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.organization_id == organization_id,
                    tasks.c.parent_task_id.in_(list(task_ids)),
                    tasks.c.status.not_in(["complete", "failed", "cancelled"]),
                )
            )
        ).all()
        if remaining:
            await conn.execute(
                update(tasks)
                .where(
                    tasks.c.organization_id == organization_id,
                    tasks.c.id.in_([str(row[0]) for row in remaining]),
                )
                .values(
                    status="cancelled",
                    error="parent_task_cancelled",
                    completed_at=_now(),
                )
            )
    for row in remaining:
        task_runner.cancel_task(str(row[0]), reason="parent_task_cancelled")
    return {"status": "complete", "cancelled": len(remaining)}


async def _cleanup_browser_sessions(
    organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    from connectors.browser_operator import browser_operator

    sessions: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        for session in await browser_operator.list_sessions(
            organization_id=organization_id, task_id=task_id
        ):
            sessions[str(session["id"])] = session
    failures: list[str] = []
    closed = 0
    for session_id in sessions:
        try:
            await browser_operator.close_session(
                session_id, organization_id=organization_id
            )
            closed += 1
        except Exception:
            failures.append(session_id)
    if failures:
        raise RuntimeError(f"browser provider cleanup failed for {len(failures)} session(s)")
    return {"status": "complete", "closed": closed}


async def _cleanup_computer_sessions(
    organization_id: str, task_ids: Sequence[str], actor_id: str
) -> dict[str, Any]:
    from connectors.computer import computer_connector

    return await computer_connector.cancel_task_sessions(
        organization_id=organization_id,
        task_ids=list(task_ids),
        member_id=actor_id,
    )


async def _cleanup_repo_workspaces(
    organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    from connectors.repo_workspace_remote import production_repo_workspace_connector

    return await production_repo_workspace_connector.close_task_workspaces(
        organization_id=organization_id,
        task_ids=list(task_ids),
    )


async def _cleanup_desktop_sessions(
    organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    from connectors.desktop import desktop_connector

    sessions: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        for session in await desktop_connector.list_sessions(
            organization_id=organization_id, task_id=task_id
        ):
            sessions[str(session["id"])] = session
    for session_id in sessions:
        await desktop_connector.close_session(
            session_id, organization_id=organization_id
        )
    return {"status": "complete", "closed": len(sessions)}


async def _cleanup_paired_device_commands(
    organization_id: str, task_ids: Sequence[str], actor_id: str
) -> dict[str, Any]:
    from core.desktop_bridge import desktop_bridge

    return await desktop_bridge.cancel_task_commands(
        organization_id=organization_id,
        task_ids=list(task_ids),
        actor_id=actor_id,
    )


async def _cleanup_connector_jobs(
    organization_id: str, task_ids: Sequence[str], _actor_id: str
) -> dict[str, Any]:
    table = await reflect_table("connector_execution_jobs")
    now = _now()
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.organization_id == organization_id,
                table.c.task_id.in_(list(task_ids)),
                table.c.status.in_(["queued", "running"]),
            )
            .values(
                status="cancelled",
                error_message="Task was cancelled",
                completed_at=now,
                updated_at=now,
            )
        )
    return {"status": "complete", "cancelled": int(result.rowcount or 0)}


async def _cleanup_approvals(
    organization_id: str, task_ids: Sequence[str], actor_id: str
) -> dict[str, Any]:
    now = _now()
    approvals = await reflect_table("approvals")
    connector_approvals = await reflect_table("approval_requests")
    async with engine.begin() as conn:
        native = await conn.execute(
            update(approvals)
            .where(
                approvals.c.organization_id == organization_id,
                approvals.c.task_id.in_(list(task_ids)),
                approvals.c.status == "pending",
            )
            .values(
                status="rejected",
                decided_by=actor_id,
                decided_at=now,
                decision_note="Task was cancelled",
            )
        )
        connector = await conn.execute(
            update(connector_approvals)
            .where(
                connector_approvals.c.organization_id == organization_id,
                connector_approvals.c.task_id.in_(list(task_ids)),
                connector_approvals.c.status == "pending",
            )
            .values(status="rejected", resolved_at=now)
        )
    return {
        "status": "complete",
        "native_rejected": int(native.rowcount or 0),
        "connector_rejected": int(connector.rowcount or 0),
    }
