from __future__ import annotations

import time
from typing import Any

from connectors.framework.approvals import ApprovalService
from connectors.framework.health import ConnectorHealthService
from connectors.framework.models import ConnectorResult
from connectors.framework.policy import PolicyEngine
from connectors.framework.schema import SchemaValidationError, validate_json_schema
from core.models import AgentContext


class QueuedConnectorExecutionService:
    def __init__(
        self,
        repo: Any,
        queue: Any,
        *,
        approval_service: ApprovalService | None = None,
        policy_engine: PolicyEngine | None = None,
        health_service: ConnectorHealthService | None = None,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.approval_service = approval_service or ApprovalService(repo)
        self.policy_engine = policy_engine or PolicyEngine()
        self.health_service = health_service or ConnectorHealthService(repo)

    async def enqueue(
        self,
        *,
        connector_id: str,
        action_name: str,
        arguments: dict[str, Any],
        context: AgentContext,
        max_attempts: int = 1,
        timeout_ms: int = 15000,
        metadata: dict[str, Any] | None = None,
    ) -> ConnectorResult:
        started = time.perf_counter()
        tenant_id = context.org_id
        workspace_id = context.workspace_id or "default"
        employee_id = context.id
        user_id = context.member_id

        async def finish(result: ConnectorResult) -> ConnectorResult:
            result.duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
            await self.repo.log_execution(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                employee_id=employee_id,
                user_id=user_id,
                connector_id=connector_id,
                action_name=action_name,
                arguments=arguments,
                result_status=result.status,
                error_message=result.error,
                duration_ms=result.duration_ms,
            )
            if result.status != "queued":
                await self.health_service.record_execution(tenant_id, connector_id, result.status, duration_ms=result.duration_ms)
            return result

        connector = await self.repo.get_connector(connector_id, tenant_id=tenant_id)
        if not connector:
            return await finish(ConnectorResult(status="failure", error="Connector not found"))
        if connector.get("status") != "installed":
            return await finish(ConnectorResult(status="failure", error=f"Connector is {connector.get('status')}, not installed"))

        action = await self.repo.get_action(connector_id, action_name)
        if not action:
            return await finish(ConnectorResult(status="failure", error="Connector action not found"))

        try:
            validate_json_schema(arguments, action["parameters_schema"])
        except SchemaValidationError as exc:
            return await finish(ConnectorResult(status="validation_error", error=str(exc)))

        permission = await self.repo.get_permission(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            connector_id=connector_id,
            action_name=action_name,
        )
        allowed_scopes = set((permission or {}).get("allowed_scopes") or [])
        required_permissions = set(action.get("required_permissions") or [])
        if not permission or not required_permissions.issubset(allowed_scopes):
            return await finish(ConnectorResult(status="permission_denied", error="Connector action is not permitted for this actor"))

        policy_engine = await PolicyEngine.from_repository(self.repo, tenant_id=tenant_id)
        if not policy_engine.policies:
            policy_engine = self.policy_engine
        policy = await policy_engine.evaluate(
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "employee_id": employee_id,
                "user_id": user_id,
                "roles": permission.get("roles") or [],
            },
            connector,
            action,
            permission,
        )
        if policy.decision == "deny":
            return await finish(ConnectorResult(status="permission_denied", error=policy.reason))
        if policy.decision == "require_approval":
            approval = await self.approval_service.request_approval(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                employee_id=employee_id,
                user_id=user_id,
                connector_id=connector_id,
                action_name=action_name,
                risk_level=action["risk_level"],
                arguments=arguments,
                justification=policy.reason,
                approval_mode=policy.approval_mode,
                execution_payload={
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "employee_id": employee_id,
                    "user_id": user_id,
                    "connector_id": connector_id,
                    "action_name": action_name,
                    "arguments": arguments,
                    "max_attempts": max_attempts,
                    "timeout_ms": timeout_ms,
                    "actor": context.model_dump(),
                },
            )
            return await finish(
                ConnectorResult(
                    status="approval_required",
                    output={"approval_request_id": approval["id"], "approval_mode": approval["approval_mode"]},
                    error=policy.reason,
                )
            )

        job = await self.repo.create_execution_job(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            connector_id=connector_id,
            action_name=action_name,
            arguments=arguments,
            max_attempts=max_attempts,
            timeout_ms=timeout_ms,
        )
        payload = {
            "id": job["id"],
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "employee_id": employee_id,
            "user_id": user_id,
            "connector_id": connector_id,
            "action_name": action_name,
            "arguments": arguments,
            "max_attempts": max_attempts,
            "timeout_ms": timeout_ms,
            "actor": context.model_dump(),
            **(metadata or {}),
        }
        await self.queue.enqueue(payload)
        return await finish(ConnectorResult(status="queued", output={"job_id": job["id"]}))
