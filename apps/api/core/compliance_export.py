"""Tenant-scoped, tamper-evident compliance evidence exports."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from core import audit, evidence
from core.artifacts import save_artifact
from core.audit_redaction import redact
from core.db import engine, reflect_table
from core.models import Member


SCHEMA_VERSION = "chronos.compliance.v1"
ALLOWED_CATEGORIES = frozenset(
    {"audit", "connector_access", "memory_access", "approvals", "task_execution"}
)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    canonical = json.dumps(_json_value(redact(value)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _selected(table: Any, names: Iterable[str]) -> list[Any]:
    return [table.c[name] for name in names if name in table.c]


def _time_filter(stmt: Any, table: Any, column: str, since: datetime | None, until: datetime | None) -> Any:
    if column not in table.c:
        return stmt
    if since is not None:
        stmt = stmt.where(table.c[column] >= since)
    if until is not None:
        stmt = stmt.where(table.c[column] < until)
    return stmt


def _audit_category(row: dict[str, Any]) -> str:
    haystack = " ".join(
        str(row.get(key) or "").lower()
        for key in ("event_type", "action", "resource_type")
    )
    if "memory" in haystack:
        return "memory_access"
    if any(marker in haystack for marker in ("connector", "tool_call", "tool_result", "oauth")):
        return "connector_access"
    if "approval" in haystack:
        return "approvals"
    if any(marker in haystack for marker in ("task", "workflow", "schedule", "monitor")):
        return "task_execution"
    return "audit"


async def _load_records(
    organization_id: str,
    *,
    since: datetime | None,
    until: datetime | None,
    categories: frozenset[str],
) -> list[dict[str, Any]]:
    audit_log = await reflect_table("audit_log")
    connectors = await reflect_table("connectors")
    approvals = await reflect_table("approvals")
    tasks = await reflect_table("tasks")

    audit_stmt = select(audit_log).where(audit_log.c.organization_id == organization_id)
    audit_stmt = _time_filter(audit_stmt, audit_log, "created_at", since, until).order_by(
        audit_log.c.created_at.asc(), audit_log.c.id.asc()
    )

    connector_columns = _selected(
        connectors,
        (
            "id",
            "member_id",
            "provider",
            "account_handle",
            "name",
            "type",
            "auth_type",
            "status",
            "scopes",
            "connected_at",
            "last_used_at",
            "created_at",
            "updated_at",
        ),
    )
    connector_stmt = select(*connector_columns).where(
        connectors.c.organization_id == organization_id
    )
    connector_stmt = _time_filter(connector_stmt, connectors, "connected_at", since, until)

    approval_columns = _selected(
        approvals,
        (
            "id",
            "task_id",
            "step_id",
            "action_type",
            "action_payload",
            "requested_at",
            "expires_at",
            "status",
            "decided_by",
            "decided_at",
        ),
    )
    approval_stmt = select(*approval_columns).where(
        approvals.c.organization_id == organization_id
    )
    approval_stmt = _time_filter(approval_stmt, approvals, "requested_at", since, until)

    task_columns = _selected(
        tasks,
        (
            "id",
            "parent_task_id",
            "project_id",
            "workspace_id",
            "triggered_by",
            "triggered_by_member_id",
            "assignee_member_id",
            "status",
            "goal",
            "mode",
            "current_step",
            "iteration_count",
            "attempts",
            "failure_reason",
            "dead_letter",
            "depth",
            "token_count",
            "cost_estimate",
            "created_at",
            "started_at",
            "completed_at",
        ),
    )
    task_stmt = select(*task_columns).where(tasks.c.organization_id == organization_id)
    task_stmt = _time_filter(task_stmt, tasks, "created_at", since, until)

    async with engine.begin() as conn:
        audit_rows = (await conn.execute(audit_stmt)).mappings().all()
        connector_rows = (
            (await conn.execute(connector_stmt)).mappings().all()
            if "connector_access" in categories
            else []
        )
        approval_rows = (
            (await conn.execute(approval_stmt)).mappings().all()
            if "approvals" in categories
            else []
        )
        task_rows = (
            (await conn.execute(task_stmt)).mappings().all()
            if "task_execution" in categories
            else []
        )

    records: list[dict[str, Any]] = []
    for source in audit_rows:
        row = dict(source)
        category = _audit_category(row)
        if category not in categories:
            continue
        records.append(
            {
                "category": category,
                "record_type": "audit_event",
                "id": str(row.get("id")),
                "event_type": row.get("event_type"),
                "actor_id": row.get("actor_id"),
                "action": row.get("action"),
                "resource_type": row.get("resource_type"),
                "resource_id": row.get("resource_id"),
                "decision": row.get("decision"),
                "payload_sha256": _digest(row.get("payload")),
                "occurred_at": _json_value(row.get("created_at")),
            }
        )

    for source in connector_rows:
        row = _json_value(redact(dict(source)))
        records.append(
            {
                "category": "connector_access",
                "record_type": "connection_snapshot",
                "occurred_at": row.get("last_used_at") or row.get("connected_at") or row.get("created_at"),
                **row,
            }
        )

    for source in approval_rows:
        row = dict(source)
        action_payload = row.pop("action_payload", None)
        normalized = _json_value(redact(row))
        records.append(
            {
                "category": "approvals",
                "record_type": "approval_snapshot",
                "occurred_at": normalized.get("decided_at") or normalized.get("requested_at"),
                "action_payload_sha256": _digest(action_payload),
                **normalized,
            }
        )

    for source in task_rows:
        row = dict(source)
        goal = row.pop("goal", None)
        normalized = _json_value(redact(row))
        records.append(
            {
                "category": "task_execution",
                "record_type": "task_snapshot",
                "occurred_at": normalized.get("completed_at") or normalized.get("started_at") or normalized.get("created_at"),
                "goal_sha256": _digest(goal),
                **normalized,
            }
        )

    records.sort(
        key=lambda row: (
            str(row.get("occurred_at") or ""),
            str(row.get("category") or ""),
            str(row.get("id") or ""),
        )
    )
    return records


async def create_export(
    member: Member,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    categories: Iterable[str] | None = None,
) -> dict[str, Any]:
    selected_categories = frozenset(categories or ALLOWED_CATEGORIES)
    unknown = selected_categories - ALLOWED_CATEGORIES
    if unknown:
        raise ValueError(f"Unsupported compliance categories: {', '.join(sorted(unknown))}")
    if not selected_categories:
        raise ValueError("Select at least one compliance category")
    if since is not None and until is not None and since >= until:
        raise ValueError("since must be earlier than until")

    records = await _load_records(
        member.organization_id,
        since=since,
        until=until,
        categories=selected_categories,
    )
    chained, head = evidence.chain(records)
    counts = Counter(record["category"] for record in records)
    generated_at = datetime.now(timezone.utc)
    bundle = {
        "manifest": {
            "schema_version": SCHEMA_VERSION,
            "organization_id": member.organization_id,
            "generated_by": member.id,
            "generated_at": generated_at.isoformat(),
            "filters": {
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "categories": sorted(selected_categories),
            },
            "record_count": len(chained),
            "category_counts": {key: counts.get(key, 0) for key in sorted(selected_categories)},
            "chain_head": head,
            "signature": evidence.sign(head),
            "algorithm": "sha256-chain + hmac-sha256",
        },
        "events": chained,
    }
    content = json.dumps(bundle, sort_keys=True, indent=2, ensure_ascii=False)
    artifact_id = await save_artifact(
        content,
        kind="data",
        title=f"Chronos compliance export {generated_at.date().isoformat()}",
        org_id=member.organization_id,
        mime_type="application/json",
        parse_status="parsed",
        created_by=f"member:{member.id}",
    )
    await audit.log(
        "compliance",
        member.id,
        "export_compliance",
        organization_id=member.organization_id,
        resource_type="artifact",
        resource_id=artifact_id,
        payload={
            "record_count": len(chained),
            "category_counts": bundle["manifest"]["category_counts"],
            "chain_head": head,
        },
    )
    return {
        "artifact_id": artifact_id,
        "download_path": f"/artifacts/{artifact_id}/content",
        "manifest": bundle["manifest"],
    }


def verify_bundle(bundle: dict[str, Any]) -> bool:
    manifest = bundle.get("manifest") or {}
    events = bundle.get("events") or []
    raw = [{key: value for key, value in event.items() if key not in {"_hash", "_prev"}} for event in events]
    _, head = evidence.chain(raw)
    return (
        head == manifest.get("chain_head")
        and evidence.sign(head) == manifest.get("signature")
        and len(events) == manifest.get("record_count")
    )
