from __future__ import annotations

import time
import uuid
from typing import Any

from connectors.framework.approvals import ApprovalService
from connectors.framework.health import ConnectorHealthService
from connectors.framework.models import ConnectorResult
from connectors.framework.policy import PolicyEngine
from connectors.framework.schema import SchemaValidationError, validate_json_schema
from core.models import AgentContext
from core.connector_write_ledger import (
    ConnectorWriteLedger,
    WriteOperationConflict,
    provider_supports_idempotency,
    secret_sha256,
    utcnow,
)


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
        metadata = dict(metadata or {})
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
        if policy.decision == "require_approval" and not metadata.get("approval_id"):
            approval = await self.approval_service.request_approval(
                tenant_id=tenant_id,
                task_id=context.task_id,
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
                    "task_id": context.task_id,
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

        job_id = str(uuid.uuid4())
        task_id = str(
            context.task_id
            or (
                f"connector-request:{secret_sha256(str(metadata['idempotency_key']))}"
                if metadata.get("idempotency_key")
                else f"connector-job:{job_id}"
            )
        )
        payload = {
            "id": job_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "workspace_id": workspace_id,
            "employee_id": employee_id,
            "user_id": user_id,
            "connector_id": connector_id,
            "action_name": action_name,
            "arguments": arguments,
            "max_attempts": max_attempts,
            "timeout_ms": timeout_ms,
            "actor": context.model_dump(),
            **metadata,
        }
        write_operation: dict[str, Any] | None = None
        approval_id = str(metadata.get("approval_id") or "")
        if action.get("risk_level") != "read":
            capabilities = (
                await self.repo.get_write_capabilities(
                    connector_id, action_name, tenant_id=tenant_id
                )
                if hasattr(self.repo, "get_write_capabilities")
                else {"provider": connector.get("provider") or connector_id}
            )
            provider = str(capabilities.get("provider") or connector_id)
            idempotency_key = str(
                metadata.get("idempotency_key")
                or approval_id
                or (
                    f"workflow:{metadata['workflow_run_id']}:{metadata['workflow_step_id']}"
                    if metadata.get("workflow_run_id")
                    and metadata.get("workflow_step_id")
                    else job_id
                )
            )
            try:
                write_operation = await ConnectorWriteLedger(self.repo).prepare(
                    organization_id=tenant_id,
                    member_id=str(user_id or employee_id),
                    task_id=task_id,
                    channel="framework",
                    tool=f"{connector_id}.{action_name}",
                    provider=provider,
                    risk_level=str(action.get("risk_level") or "write"),
                    payload=arguments,
                    approval_binding=approval_id or "autonomy-policy",
                    idempotency_key=idempotency_key,
                    connector_job_id=job_id,
                    provider_idempotency=provider_supports_idempotency(
                        provider,
                        header=capabilities.get("idempotency_header"),
                    ),
                    supports_reconciliation=bool(
                        capabilities.get("supports_reconciliation")
                    ),
                    outbox_payload=payload,
                )
            except WriteOperationConflict as exc:
                return await finish(ConnectorResult(status="failure", error=str(exc)))

            if not write_operation.get("_created"):
                status = str(write_operation.get("status") or "")
                if status == "complete":
                    durable = write_operation.get("result") or {}
                    return await finish(
                        ConnectorResult(
                            status="success",
                            output=durable.get("output") or durable,
                        )
                    )
                if status in {"manual_review", "failed", "cancelled"}:
                    return await finish(
                        ConnectorResult(
                            status="manual_review" if status == "manual_review" else "failure",
                            error=write_operation.get("last_error")
                            or f"Connector write is {status}",
                        )
                    )
                return await finish(
                    ConnectorResult(
                        status="queued",
                        output={
                            "job_id": write_operation.get("connector_job_id"),
                            "write_operation_id": str(write_operation["id"]),
                            "idempotency_replayed": True,
                        },
                    )
                )
            payload["write_operation_id"] = str(write_operation["id"])
            payload["provider_idempotency_key"] = write_operation[
                "provider_idempotency_key"
            ]
            payload["provider_supports_idempotency"] = bool(
                write_operation["provider_supports_idempotency"]
            )
            payload["idempotency_header"] = capabilities.get("idempotency_header")

        job = await self.repo.create_execution_job(
            id=job_id,
            tenant_id=tenant_id,
            task_id=task_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            connector_id=connector_id,
            action_name=action_name,
            arguments=arguments,
            max_attempts=max_attempts,
            timeout_ms=timeout_ms,
            write_operation_id=str(write_operation["id"]) if write_operation else None,
            approval_id=approval_id or None,
        )
        try:
            await self.queue.enqueue(payload)
        except Exception:
            # The encrypted Postgres outbox is authoritative.  Recovery will
            # repopulate Redis after a transient queue outage or total loss.
            if not write_operation:
                raise
            return await finish(
                ConnectorResult(
                    status="queued",
                    output={
                        "job_id": job["id"],
                        "write_operation_id": str(write_operation["id"])
                        if write_operation
                        else None,
                        "durable_outbox": bool(write_operation),
                    },
                )
            )
        if write_operation:
            await self.repo.mark_write_operation_enqueued(
                str(write_operation["id"]),
                organization_id=tenant_id,
                enqueued_at=utcnow(),
            )
        return await finish(
            ConnectorResult(
                status="queued",
                output={
                    "job_id": job["id"],
                    "write_operation_id": str(write_operation["id"])
                    if write_operation
                    else None,
                },
            )
        )
