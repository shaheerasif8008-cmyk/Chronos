from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.ssrf import UnsafeURLError, assert_safe_url
from jobs.monitor_polling import MonitorPollError, run_monitor_now, validate_read_only_tool

router = APIRouter(prefix="/monitors", tags=["monitors"])

_MONITOR_TYPES = {"website", "source", "connector", "inbox", "news", "digest"}
_MONITOR_STATUSES = {"active", "paused"}
_CONDITION_OPERATORS = {"changed", "contains", "always"}


class MonitorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    monitor_type: str = Field(pattern="^(website|source|connector|inbox|news|digest)$")
    target: str = Field(min_length=1, max_length=2_000)
    condition: dict[str, Any] = Field(default_factory=lambda: {"operator": "changed"})
    source_config: dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int = Field(default=900, ge=60, le=86_400)
    max_attempts: int = Field(default=5, ge=1, le=10)
    alert_cooldown_seconds: int = Field(default=300, ge=0, le=86_400)
    schedule_id: str | None = None
    workflow_id: str | None = None
    status: str = Field(default="active", pattern="^(active|paused)$")


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    target: str | None = Field(default=None, min_length=1, max_length=2_000)
    condition: dict[str, Any] | None = None
    source_config: dict[str, Any] | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    alert_cooldown_seconds: int | None = Field(default=None, ge=0, le=86_400)
    schedule_id: str | None = None
    workflow_id: str | None = None
    status: str | None = Field(default=None, pattern="^(active|paused)$")


class MonitorEvaluateRequest(BaseModel):
    """Compatibility body. Observations are never trusted from callers."""

    observed: dict[str, Any] | None = None


@router.get("/")
async def list_monitors(
    status: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_monitors", member.organization_id)
    if status and status not in _MONITOR_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported monitor status")
    monitors = await reflect_table("monitors")
    stmt = select(monitors).where(monitors.c.organization_id == member.organization_id)
    if status:
        stmt = stmt.where(monitors.c.status == status)
    stmt = stmt.order_by(monitors.c.created_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_serialize_monitor(dict(row)) for row in rows]


@router.post("/")
async def create_monitor(
    req: MonitorRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_monitor", member.organization_id)
    _validate_spec(req.monitor_type, req.target, req.condition, req.source_config)
    monitors = await reflect_table("monitors")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        count = await conn.scalar(
            select(func.count()).select_from(monitors).where(
                monitors.c.organization_id == member.organization_id
            )
        )
        if int(count or 0) >= settings.monitor_max_per_org:
            raise HTTPException(
                status_code=409,
                detail=f"Monitor quota reached ({settings.monitor_max_per_org} per organization)",
            )
        row = (
            await conn.execute(
                insert(monitors)
                .values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    name=req.name.strip(),
                    monitor_type=req.monitor_type,
                    target=req.target.strip(),
                    condition=req.condition,
                    source_config=req.source_config,
                    interval_seconds=req.interval_seconds,
                    max_attempts=req.max_attempts,
                    alert_cooldown_seconds=req.alert_cooldown_seconds,
                    schedule_id=req.schedule_id,
                    workflow_id=req.workflow_id,
                    status=req.status,
                    next_run_at=now if req.status == "active" else None,
                    created_by=member.id,
                )
                .returning(monitors)
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=500, detail="Monitor could not be created")
    await audit.log(
        "monitor_created",
        member.id,
        "monitors.create",
        organization_id=member.organization_id,
        resource_type="monitors",
        resource_id=str(row["id"]),
        payload={
            "monitor_type": req.monitor_type,
            "interval_seconds": req.interval_seconds,
            "status": req.status,
        },
    )
    return _serialize_monitor(dict(row))


@router.patch("/{monitor_id}")
async def update_monitor(
    monitor_id: str,
    req: MonitorUpdate,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "update_monitor", monitor_id)
    current = await _require_monitor(member, monitor_id)
    values = req.model_dump(exclude_unset=True)
    if not values:
        return _serialize_monitor(current)

    monitor_type = str(current.get("monitor_type") or "")
    target = str(values.get("target", current.get("target") or ""))
    condition = values.get("condition", current.get("condition") or {})
    source_config = values.get("source_config", current.get("source_config") or {})
    _validate_spec(monitor_type, target, condition, source_config)
    now = datetime.now(timezone.utc)
    reset_baseline = bool({"target", "source_config", "condition"}.intersection(values))
    if values.get("name") is not None:
        values["name"] = str(values["name"]).strip()
    if values.get("target") is not None:
        values["target"] = str(values["target"]).strip()
    if values.get("status") == "active" and current.get("status") != "active":
        values.update(next_run_at=now, backoff_until=None, consecutive_failures=0)
    elif values.get("status") == "paused":
        values.update(next_run_at=None, backoff_until=None)
    elif values.get("interval_seconds") is not None and current.get("status") == "active":
        values["next_run_at"] = now
    if reset_baseline:
        values.update(
            content_hash=None,
            last_etag=None,
            last_modified=None,
            next_run_at=now if values.get("status", current.get("status")) == "active" else None,
        )

    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(monitors)
                .where(
                    monitors.c.id == monitor_id,
                    monitors.c.organization_id == member.organization_id,
                )
                .values(**values)
                .returning(monitors)
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await audit.log(
        "monitor_updated",
        member.id,
        "monitors.update",
        organization_id=member.organization_id,
        resource_type="monitors",
        resource_id=monitor_id,
        payload={"fields": sorted(values.keys())},
    )
    return _serialize_monitor(dict(row))


@router.post("/{monitor_id}/run")
async def run_monitor(
    monitor_id: str,
    member: Member = Depends(get_current_member),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    await permissions.check(member, "evaluate_monitor", monitor_id)
    await _require_monitor(member, monitor_id)
    try:
        return await run_monitor_now(
            monitor_id,
            member.organization_id,
            actor_id=member.id,
            idempotency_key=idempotency_key,
        )
    except MonitorPollError as exc:
        status_code = 409 if exc.code in {"already_running", "duplicate_run"} else 422
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/{monitor_id}/evaluate")
async def evaluate_monitor(
    monitor_id: str,
    _req: MonitorEvaluateRequest | None = None,
    member: Member = Depends(get_current_member),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Compatibility alias that performs a real collection instead of trusting supplied evidence."""

    return await run_monitor(monitor_id, member, idempotency_key)


@router.get("/{monitor_id}/runs")
async def list_monitor_runs(
    monitor_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_monitors", member.organization_id)
    await _require_monitor(member, monitor_id)
    runs = await reflect_table("monitor_runs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(runs)
                .where(
                    runs.c.organization_id == member.organization_id,
                    runs.c.monitor_id == monitor_id,
                )
                .order_by(runs.c.created_at.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [_serialize_run(dict(row)) for row in rows]


@router.get("/alerts")
async def list_monitor_alerts(
    status: str | None = Query(default=None),
    monitor_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_monitor_alerts", member.organization_id)
    alerts = await reflect_table("monitor_alerts")
    stmt = select(alerts).where(alerts.c.organization_id == member.organization_id)
    if status:
        stmt = stmt.where(alerts.c.status == status)
    if monitor_id:
        stmt = stmt.where(alerts.c.monitor_id == monitor_id)
    stmt = stmt.order_by(alerts.c.created_at.desc()).limit(200)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_serialize_alert(dict(row)) for row in rows]


async def _require_monitor(member: Member, monitor_id: str) -> dict[str, Any]:
    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(monitors).where(
                    monitors.c.id == monitor_id,
                    monitors.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return dict(row)


def _validate_spec(
    monitor_type: str,
    target: str,
    condition: dict[str, Any],
    source_config: dict[str, Any],
) -> None:
    if monitor_type not in _MONITOR_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported monitor type")
    operator = str((condition or {}).get("operator") or "changed")
    if operator not in _CONDITION_OPERATORS:
        raise HTTPException(status_code=422, detail="Unsupported monitor condition")
    if operator == "contains" and not str((condition or {}).get("value") or "").strip():
        raise HTTPException(status_code=422, detail="Contains monitors require a condition value")
    if monitor_type == "website":
        try:
            assert_safe_url(target.strip())
        except UnsafeURLError as exc:
            raise HTTPException(status_code=422, detail=f"Unsafe monitor target: {exc}") from exc
    elif monitor_type in {"source", "connector", "inbox"}:
        tool = str((source_config or {}).get("tool") or "")
        args = (source_config or {}).get("args") or {}
        if not isinstance(args, dict):
            raise HTTPException(status_code=422, detail="Monitor source arguments must be an object")
        try:
            validate_read_only_tool(tool, args)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    elif not target.strip():
        raise HTTPException(status_code=422, detail="Monitor query is required")


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _public_source_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("tool", "workspace_id", "max_results")
        if value.get(key) not in (None, "")
    }


def _serialize_monitor(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "name": row.get("name"),
        "monitor_type": row.get("monitor_type"),
        "target": row.get("target"),
        "condition": row.get("condition") or {},
        "source_config": _public_source_config(row.get("source_config")),
        "interval_seconds": row.get("interval_seconds"),
        "max_attempts": row.get("max_attempts"),
        "alert_cooldown_seconds": row.get("alert_cooldown_seconds"),
        "schedule_id": str(row.get("schedule_id")) if row.get("schedule_id") else None,
        "workflow_id": row.get("workflow_id"),
        "status": row.get("status"),
        "last_checked_at": _iso(row.get("last_checked_at")),
        "last_run_at": _iso(row.get("last_run_at")),
        "last_success_at": _iso(row.get("last_success_at")),
        "last_failure_at": _iso(row.get("last_failure_at")),
        "next_run_at": _iso(row.get("next_run_at")),
        "backoff_until": _iso(row.get("backoff_until")),
        "last_run_status": row.get("last_run_status"),
        "last_error_code": row.get("last_error_code"),
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "alert_count": int(row.get("alert_count") or 0),
        "last_evidence": row.get("last_evidence") or {},
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "monitor_id": str(row.get("monitor_id")),
        "trigger_source": row.get("trigger_source"),
        "status": row.get("status"),
        "attempt": int(row.get("attempt") or 1),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "next_attempt_at": _iso(row.get("next_attempt_at")),
        "error_code": row.get("error_code"),
        "error_summary": row.get("error_summary"),
        "observation": row.get("observation") or {},
        "alert_id": str(row.get("alert_id")) if row.get("alert_id") else None,
        "workflow_run_id": row.get("workflow_run_id"),
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_alert(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "monitor_id": str(row.get("monitor_id")) if row.get("monitor_id") else None,
        "run_id": str(row.get("run_id")) if row.get("run_id") else None,
        "severity": row.get("severity"),
        "summary": row.get("summary"),
        "evidence": row.get("evidence") or {},
        "status": row.get("status"),
        "created_at": _iso(row.get("created_at")),
    }
