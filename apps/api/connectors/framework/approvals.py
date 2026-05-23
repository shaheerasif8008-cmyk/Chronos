from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from connectors.framework.audit import redact_arguments


class ApprovalService:
    def __init__(self, repo: Any, default_ttl_minutes: int = 60) -> None:
        self.repo = repo
        self.default_ttl_minutes = default_ttl_minutes

    async def request_approval(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        employee_id: str,
        user_id: str | None,
        connector_id: str,
        action_name: str,
        risk_level: str,
        arguments: dict[str, Any],
        justification: str,
        approval_mode: str = "single",
        execution_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        required = 2 if approval_mode == "multi" else 1
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.default_ttl_minutes)
        return await self.repo.create_approval_request(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            connector_id=connector_id,
            action_name=action_name,
            risk_level=risk_level,
            arguments_redacted=redact_arguments(arguments),
            justification=justification,
            approval_mode=approval_mode,
            required_approvals=required,
            expires_at=expires_at,
            execution_payload=execution_payload or {},
        )

    async def resolve(
        self,
        approval_id: str,
        *,
        tenant_id: str,
        actor_id: str,
        approved: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        status = "approved" if approved else "rejected"
        return await self.repo.resolve_approval_request(
            approval_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            status=status,
            note=note,
        )

    async def approve_and_enqueue(
        self,
        approval_id: str,
        *,
        tenant_id: str,
        actor_id: str,
        queue: Any,
        note: str | None = None,
    ) -> dict[str, Any]:
        approval = await self.resolve(approval_id, tenant_id=tenant_id, actor_id=actor_id, approved=True, note=note)
        payload = approval.get("execution_payload") or {}
        if not payload:
            return {"status": "approved", "approval_request_id": approval_id, "job_id": None}
        job = await self.repo.create_execution_job(**payload)
        queue_payload = {"id": job["id"], **payload, "actor": payload.get("actor") or {}}
        await queue.enqueue(queue_payload)
        if hasattr(self.repo, "mark_approval_resumed"):
            await self.repo.mark_approval_resumed(approval_id, tenant_id=tenant_id, job_id=job["id"])
        return {"status": "queued", "approval_request_id": approval_id, "job_id": job["id"]}
