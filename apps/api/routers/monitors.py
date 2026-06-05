from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from jobs.scheduled_tasks import evaluate_monitor_result

router = APIRouter(prefix="/monitors", tags=["monitors"])


class MonitorRequest(BaseModel):
    name: str
    monitor_type: str = Field(pattern="^(website|source|connector|inbox|news|digest)$")
    target: str
    condition: dict[str, Any] = Field(default_factory=lambda: {"operator": "changed"})
    schedule_id: str | None = None
    workflow_id: str | None = None
    status: str = "active"


class MonitorUpdate(BaseModel):
    name: str | None = None
    target: str | None = None
    condition: dict[str, Any] | None = None
    schedule_id: str | None = None
    workflow_id: str | None = None
    status: str | None = None


class MonitorEvaluateRequest(BaseModel):
    observed: dict[str, Any]


@router.get("/")
async def list_monitors(status: str | None = Query(default=None), member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_monitors", member.organization_id)
    monitors = await reflect_table("monitors")
    stmt = select(monitors).where(monitors.c.organization_id == member.organization_id)
    if status:
        stmt = stmt.where(monitors.c.status == status)
    stmt = stmt.order_by(monitors.c.created_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_serialize_monitor(dict(row)) for row in rows]


@router.post("/")
async def create_monitor(req: MonitorRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_monitor", member.organization_id)
    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(monitors)
                .values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    name=req.name,
                    monitor_type=req.monitor_type,
                    target=req.target,
                    condition=req.condition,
                    schedule_id=req.schedule_id,
                    workflow_id=req.workflow_id,
                    status=req.status,
                    created_by=member.id,
                )
                .returning(monitors)
            )
        ).mappings().first()
    await audit.log(
        "monitor_created",
        member.id,
        "monitors.create",
        organization_id=member.organization_id,
        resource_type="monitors",
        resource_id=str(row["id"]),
        payload={"monitor_type": req.monitor_type, "target": req.target},
    )
    return _serialize_monitor(dict(row))


@router.patch("/{monitor_id}")
async def update_monitor(monitor_id: str, req: MonitorUpdate, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "update_monitor", monitor_id)
    await _require_monitor(member, monitor_id)
    monitors = await reflect_table("monitors")
    values = {key: value for key, value in req.model_dump(exclude_unset=True).items()}
    if not values:
        return await _require_monitor(member, monitor_id)
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(monitors)
                .where(monitors.c.id == monitor_id, monitors.c.organization_id == member.organization_id)
                .values(**values)
                .returning(monitors)
            )
        ).mappings().first()
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


@router.post("/{monitor_id}/evaluate")
async def evaluate_monitor(monitor_id: str, req: MonitorEvaluateRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "evaluate_monitor", monitor_id)
    monitor = await _require_monitor(member, monitor_id)
    if monitor.get("status") == "paused":
        return {"monitor_id": monitor_id, "status": "paused", "alert": None}
    alert_payload = evaluate_monitor_result(monitor, req.observed)
    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        await conn.execute(
            update(monitors)
            .where(monitors.c.id == monitor_id, monitors.c.organization_id == member.organization_id)
            .values(last_checked_at=datetime.now(timezone.utc), last_evidence=req.observed)
        )
    if not alert_payload:
        return {"monitor_id": monitor_id, "status": "checked", "alert": None}
    alerts = await reflect_table("monitor_alerts")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(alerts)
                .values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    monitor_id=monitor_id,
                    severity=alert_payload["severity"],
                    summary=alert_payload["summary"],
                    evidence=alert_payload["evidence"],
                    status="open",
                )
                .returning(alerts)
            )
        ).mappings().first()
    await audit.log(
        "monitor_alert_created",
        member.id,
        "monitors.evaluate",
        organization_id=member.organization_id,
        resource_type="monitor_alerts",
        resource_id=str(row["id"]),
        payload={"monitor_id": monitor_id, "severity": row["severity"], "evidence_url": (row["evidence"] or {}).get("url")},
    )
    return {"monitor_id": monitor_id, "status": "alert_created", "alert": _serialize_alert(dict(row))}


@router.get("/alerts")
async def list_monitor_alerts(status: str | None = Query(default=None), member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_monitor_alerts", member.organization_id)
    alerts = await reflect_table("monitor_alerts")
    stmt = select(alerts).where(alerts.c.organization_id == member.organization_id)
    if status:
        stmt = stmt.where(alerts.c.status == status)
    stmt = stmt.order_by(alerts.c.created_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_serialize_alert(dict(row)) for row in rows]


async def _require_monitor(member: Member, monitor_id: str) -> dict[str, Any]:
    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(monitors).where(monitors.c.id == monitor_id, monitors.c.organization_id == member.organization_id)
            )
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return dict(row)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize_monitor(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "name": row.get("name"),
        "monitor_type": row.get("monitor_type"),
        "target": row.get("target"),
        "condition": row.get("condition") or {},
        "schedule_id": str(row.get("schedule_id")) if row.get("schedule_id") else None,
        "workflow_id": row.get("workflow_id"),
        "status": row.get("status"),
        "last_checked_at": _iso(row.get("last_checked_at")),
        "last_evidence": row.get("last_evidence") or {},
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_alert(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "monitor_id": str(row.get("monitor_id")) if row.get("monitor_id") else None,
        "severity": row.get("severity"),
        "summary": row.get("summary"),
        "evidence": row.get("evidence") or {},
        "status": row.get("status"),
        "created_at": _iso(row.get("created_at")),
    }
