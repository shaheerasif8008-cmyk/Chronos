from __future__ import annotations

import asyncio
import time
from typing import Any

from connectors.framework.adapters import ConnectorAdapter
from connectors.framework.approvals import ApprovalService
from connectors.framework.health import ConnectorHealthService
from connectors.framework.models import ConnectorResult
from connectors.framework.policy import PolicyEngine
from connectors.framework.repository import ConnectorRepository
from connectors.framework.schema import SchemaValidationError, validate_json_schema
from core.models import AgentContext


class ConnectorExecutionService:
    def __init__(
        self,
        repo: ConnectorRepository,
        adapters: dict[str, ConnectorAdapter],
        timeout_seconds: float = 15.0,
        approval_service: ApprovalService | None = None,
        policy_engine: PolicyEngine | None = None,
        health_service: ConnectorHealthService | None = None,
    ) -> None:
        self.repo = repo
        self.adapters = adapters
        self.timeout_seconds = timeout_seconds
        self.approval_service = approval_service or ApprovalService(repo)
        self.policy_engine = policy_engine or PolicyEngine()
        self.health_service = health_service or ConnectorHealthService(repo)

    async def execute(
        self,
        *,
        connector_id: str,
        action_name: str,
        arguments: dict[str, Any],
        context: AgentContext,
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
        if not permission:
            return await finish(ConnectorResult(status="permission_denied", error="Connector action is not permitted for this actor"))

        allowed_scopes = set(permission.get("allowed_scopes") or [])
        required_permissions = set(action.get("required_permissions") or [])
        if not required_permissions.issubset(allowed_scopes):
            return await finish(ConnectorResult(status="permission_denied", error="Connector permission scopes are insufficient"))

        policy = await self.policy_engine.evaluate(
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
                execution_payload={},
            )
            return await finish(
                ConnectorResult(
                    status="approval_required",
                    output={"approval_request_id": approval["id"], "approval_mode": approval["approval_mode"]},
                    error=policy.reason,
                )
            )

        adapter = self.adapters.get(connector_id)
        if not adapter:
            return await finish(ConnectorResult(status="failure", error="No adapter registered for connector"))

        credentials = await self.repo.load_credentials(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            connector_id=connector_id,
        )
        if not await adapter.validate_credentials(credentials):
            return await finish(ConnectorResult(status="failure", error="Connector credentials are invalid"))

        try:
            result = await asyncio.wait_for(
                adapter.execute(action_name, arguments, {"credentials": credentials, "actor": context.model_dump()}),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return await finish(ConnectorResult(status="timeout", error="Connector execution timed out"))
        except Exception as exc:
            return await finish(ConnectorResult(status="failure", error=str(exc)))

        return await finish(result)
