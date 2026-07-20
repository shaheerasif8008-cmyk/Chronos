from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

ACTION_EVENT_TYPES = {
    "tool_call",
    "tool_result",
    "tool_error",
    "awaiting_approval",
    "approval_decided",
    "approval_rejected",
    "artifact",
    "sub_agent_spawned",
    "sub_agent_complete",
    "sub_agent_event",
    "browser_action",
    "browser_session_created",
    "browser_takeover_requested",
    "browser_takeover_released",
    "browser_sensitive_site_approved",
    "browser_session_revoked",
    "browser_session_closed",
    "task_complete",
    "task_failed",
    "task_cancelled",
    "task_cleanup_requested",
}


def normalize_tool_name(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("__", ".", 1)


def normalize_audit_event(
    row: dict[str, Any],
    *,
    tasks_by_id: dict[str, dict[str, Any]],
    approvals_by_id: dict[str, dict[str, Any]],
    artifacts_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    event_type = str(payload.get("type") or row.get("action") or row.get("event_type") or "event")
    task_id = str(payload.get("task_id") or row.get("resource_id") or "")
    task = tasks_by_id.get(task_id, {})
    approval_ids = [str(item) for item in payload.get("approval_ids") or []]
    approval_id = str(payload.get("approval_id") or (approval_ids[0] if approval_ids else "") or "")
    approval = approvals_by_id.get(approval_id, {})
    artifact_id = str(payload.get("artifact_id") or "")
    artifact = artifacts_by_id.get(artifact_id, {})
    tool = normalize_tool_name(str(payload.get("tool") or row.get("action") or "")) if event_type.startswith("tool_") else None
    created_at = row.get("created_at")

    status = _status_for(event_type, task, approval, payload)
    summary = _summary_for(event_type, payload, task, approval, artifact, tool)

    return {
        "id": str(row.get("id")),
        "type": event_type,
        "status": status,
        "summary": summary,
        "actor_id": row.get("actor_id"),
        "task_id": task_id or None,
        "task_goal": task.get("goal"),
        "task_status": task.get("status"),
        "tool": tool,
        "approval_id": approval_id or None,
        "approval_status": approval.get("status"),
        "artifact_id": artifact_id or None,
        "artifact_title": artifact.get("title") or payload.get("title"),
        "artifact_kind": artifact.get("kind") or payload.get("kind"),
        "error": payload.get("error"),
        "created_at": _iso(created_at),
        "payload": payload,
        "links": {
            "task": f"/activity?task={task_id}" if task_id else None,
            "approval": f"/approvals?approval={approval_id}" if approval_id else None,
            "artifact": f"/artifacts/{artifact_id}" if artifact_id else None,
        },
    }


async def list_task_events(task_id: str, org_id: str, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    from core.db import engine, reflect_table

    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(audit_log)
                .where(
                    audit_log.c.organization_id == org_id,
                    audit_log.c.event_type == "activity",
                    audit_log.c.resource_id == task_id,
                )
                .order_by(audit_log.c.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
    raw_rows = [dict(row) for row in rows]
    raw_rows = [row for row in raw_rows if _row_type(row) in ACTION_EVENT_TYPES]
    raw_rows.extend(await _synthetic_task_rows(task_id, org_id, raw_rows))
    return await _normalize_rows(raw_rows, org_id=org_id, ascending=True)


async def list_activity_actions(
    org_id: str,
    *,
    member_id: str | None = None,
    include_org_wide: bool = False,
    event_type: str | None = None,
    status: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    from core.db import engine, reflect_table
    from core.models import Member
    from core.task_access import visibility_clause
    from sqlalchemy import or_

    audit_log = await reflect_table("audit_log")
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        stmt = (
            select(audit_log)
            .where(audit_log.c.organization_id == org_id, audit_log.c.event_type == "activity")
            .order_by(audit_log.c.created_at.desc())
            .limit(max(limit * 4, limit))
            .offset(offset)
        )
        if task_id:
            stmt = stmt.where(audit_log.c.resource_id == task_id)
        if member_id and not include_org_wide:
            scoped_member = Member(
                id=member_id,
                organization_id=org_id,
                email="activity-reader@chronos.invalid",
                role="user",
            )
            own_tasks = select(tasks.c.id).where(
                tasks.c.organization_id == org_id,
                visibility_clause(tasks, scoped_member),
            )
            stmt = stmt.where(
                or_(
                    audit_log.c.resource_id.in_(own_tasks),
                    audit_log.c.actor_id == member_id,
                )
            )
        rows = (await conn.execute(stmt)).mappings().all()

    normalized = await _normalize_rows(
        [dict(row) for row in rows if _row_type(dict(row)) in ACTION_EVENT_TYPES],
        org_id=org_id,
        ascending=False,
    )

    if event_type:
        normalized = [event for event in normalized if event["type"] == event_type]
    if status:
        normalized = [event for event in normalized if event["status"] == status]
    if tool:
        normalized = [event for event in normalized if event.get("tool") == normalize_tool_name(tool)]
    if query:
        needle = query.lower()
        normalized = [
            event for event in normalized
            if needle in str(event.get("summary") or "").lower()
            or needle in str(event.get("task_goal") or "").lower()
            or needle in str(event.get("tool") or "").lower()
        ]
    return normalized[:limit]


async def _normalize_rows(rows: list[dict[str, Any]], *, org_id: str, ascending: bool) -> list[dict[str, Any]]:
    task_ids = sorted({str((row.get("payload") or {}).get("task_id") or row.get("resource_id")) for row in rows if ((row.get("payload") or {}).get("task_id") or row.get("resource_id"))})
    approval_ids = sorted(_approval_ids_from_rows(rows))
    artifact_ids = sorted({str((row.get("payload") or {}).get("artifact_id")) for row in rows if (row.get("payload") or {}).get("artifact_id")})
    tasks_by_id, approvals_by_id, artifacts_by_id = await _load_enrichment(org_id, task_ids, approval_ids, artifact_ids)
    events = [
        normalize_audit_event(
            row,
            tasks_by_id=tasks_by_id,
            approvals_by_id=approvals_by_id,
            artifacts_by_id=artifacts_by_id,
        )
        for row in rows
    ]
    return sorted(events, key=lambda event: event.get("created_at") or "", reverse=not ascending)


async def _load_enrichment(
    org_id: str,
    task_ids: list[str],
    approval_ids: list[str],
    artifact_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    from core.db import engine, reflect_table

    tasks_by_id: dict[str, dict[str, Any]] = {}
    approvals_by_id: dict[str, dict[str, Any]] = {}
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    async with engine.begin() as conn:
        if task_ids:
            tasks = await reflect_table("tasks")
            rows = (
                await conn.execute(
                    select(tasks).where(tasks.c.organization_id == org_id, tasks.c.id.in_(task_ids))
                )
            ).mappings().all()
            tasks_by_id = {str(row["id"]): dict(row) for row in rows}
        if approval_ids:
            approvals = await reflect_table("approvals")
            rows = (
                await conn.execute(
                    select(approvals).where(approvals.c.organization_id == org_id, approvals.c.id.in_(approval_ids))
                )
            ).mappings().all()
            approvals_by_id = {str(row["id"]): dict(row) for row in rows}
        if artifact_ids:
            artifacts = await reflect_table("artifacts")
            rows = (
                await conn.execute(
                    select(artifacts).where(artifacts.c.organization_id == org_id, artifacts.c.id.in_(artifact_ids))
                )
            ).mappings().all()
            artifacts_by_id = {str(row["id"]): dict(row) for row in rows}
    return tasks_by_id, approvals_by_id, artifacts_by_id


async def _synthetic_task_rows(task_id: str, org_id: str, existing_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from core.db import engine, reflect_table

    existing_artifacts = {str((row.get("payload") or {}).get("artifact_id")) for row in existing_rows if (row.get("payload") or {}).get("artifact_id")}
    synthetic: list[dict[str, Any]] = []
    async with engine.begin() as conn:
        approvals = await reflect_table("approvals")
        approval_rows = (
            await conn.execute(
                select(approvals).where(approvals.c.organization_id == org_id, approvals.c.task_id == task_id)
            )
        ).mappings().all()
        if approval_rows and not any(_row_type(row) == "awaiting_approval" for row in existing_rows):
            first = dict(approval_rows[0])
            synthetic.append({
                "id": f"approval:{first['id']}",
                "event_type": "activity",
                "action": "awaiting_approval",
                "actor_id": "chronos",
                "resource_id": task_id,
                "payload": {"type": "awaiting_approval", "task_id": task_id, "approval_ids": [str(row["id"]) for row in approval_rows]},
                "created_at": first.get("requested_at"),
            })
        artifacts = await reflect_table("artifacts")
        artifact_rows = (
            await conn.execute(
                select(artifacts).where(artifacts.c.organization_id == org_id, artifacts.c.task_id == task_id)
            )
        ).mappings().all()
        for row in artifact_rows:
            row_dict = dict(row)
            artifact_id = str(row_dict["id"])
            if artifact_id in existing_artifacts:
                continue
            synthetic.append({
                "id": f"artifact:{artifact_id}",
                "event_type": "activity",
                "action": "artifact",
                "actor_id": "chronos",
                "resource_id": task_id,
                "payload": {
                    "type": "artifact",
                    "task_id": task_id,
                    "artifact_id": artifact_id,
                    "title": row_dict.get("title"),
                    "kind": row_dict.get("kind"),
                },
                "created_at": row_dict.get("created_at"),
            })
    return synthetic


def _approval_ids_from_rows(rows: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("approval_id"):
            ids.add(str(payload["approval_id"]))
        for approval_id in payload.get("approval_ids") or []:
            ids.add(str(approval_id))
    return ids


def _row_type(row: dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    if isinstance(payload, dict) and payload.get("type"):
        return str(payload["type"])
    return str(row.get("action") or row.get("event_type") or "")


def _status_for(event_type: str, task: dict[str, Any], approval: dict[str, Any], payload: dict[str, Any]) -> str:
    if event_type in {"tool_call", "sub_agent_spawned", "browser_action"}:
        return "running"
    if event_type in {"tool_result", "sub_agent_complete", "artifact", "task_complete", "browser_session_created", "browser_takeover_released", "browser_sensitive_site_approved", "browser_session_closed"}:
        return "complete"
    if event_type in {"tool_error", "task_failed"}:
        return "error"
    if event_type == "awaiting_approval":
        return "approval_pending"
    if event_type == "approval_rejected":
        return "rejected"
    if event_type == "approval_decided":
        return str(payload.get("decision") or approval.get("status") or "complete")
    return str(task.get("status") or "recorded")


def _summary_for(
    event_type: str,
    payload: dict[str, Any],
    task: dict[str, Any],
    approval: dict[str, Any],
    artifact: dict[str, Any],
    tool: str | None,
) -> str:
    if event_type == "tool_call":
        return f"Calling {tool or 'tool'}"
    if event_type == "tool_result":
        return str(payload.get("summary") or f"{tool or 'Tool'} completed")
    if event_type == "tool_error":
        return str(payload.get("error") or f"{tool or 'Tool'} failed")
    if event_type == "awaiting_approval":
        return "Waiting for approval"
    if event_type == "approval_decided":
        return f"Approval {payload.get('decision') or approval.get('status') or 'decided'}"
    if event_type == "approval_rejected":
        return "Approval rejected"
    if event_type == "artifact":
        return f"Created artifact {artifact.get('title') or payload.get('title') or ''}".strip()
    if event_type == "sub_agent_spawned":
        return f"Spawned sub-agent: {payload.get('goal') or ''}".strip()
    if event_type == "sub_agent_complete":
        return "Sub-agent completed"
    if event_type == "sub_agent_event":
        nested = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        return f"Sub-agent event: {nested.get('type') or 'event'}"
    if event_type == "task_complete":
        return "Task completed"
    if event_type == "task_failed":
        return str(payload.get("error") or "Task failed")
    if event_type == "browser_action":
        action = payload.get("action") or "action"
        suffix = payload.get("current_url") or payload.get("selector") or payload.get("filename") or ""
        return f"Browser {action}: {suffix}".strip()
    if event_type == "browser_session_created":
        return "Browser session created"
    if event_type == "browser_takeover_requested":
        return f"Browser takeover requested: {payload.get('reason') or 'user input required'}"
    if event_type == "browser_takeover_released":
        return f"Browser hand-back: {payload.get('summary') or 'released'}"
    if event_type == "browser_sensitive_site_approved":
        return f"Browser sensitive site approved: {payload.get('domain') or ''}".strip()
    if event_type == "browser_session_revoked":
        return "Browser session revoked"
    if event_type == "browser_session_closed":
        return "Browser session closed"
    return str(task.get("goal") or event_type)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)
