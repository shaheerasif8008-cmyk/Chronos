from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from connectors.framework.audit import redact_arguments
from connectors.framework.health import ConnectorHealthService
from connectors.framework.models import ConnectorResult
from core.connector_write_ledger import (
    ConnectorWriteLedger,
    ManualReviewRequired,
    WriteOperationBusy,
    WriteOperationTerminal,
)


class ConnectorWorker:
    def __init__(
        self,
        repo: Any,
        adapters: dict[str, Any],
        queue: Any,
        *,
        tracer: Any | None = None,
        health: ConnectorHealthService | None = None,
    ) -> None:
        self.repo = repo
        self.adapters = adapters
        self.queue = queue
        self.tracer = tracer
        self.health = health or ConnectorHealthService(repo)

    async def run_once(self) -> dict[str, Any] | None:
        job = await self.queue.dequeue(timeout_seconds=0.1)
        if not job:
            return None
        stored_job = await self.repo.get_execution_job(job["id"], tenant_id=job["tenant_id"]) if hasattr(self.repo, "get_execution_job") else None
        if stored_job and stored_job.get("write_operation_id"):
            job.setdefault("write_operation_id", str(stored_job["write_operation_id"]))
        if stored_job and stored_job.get("status") == "cancelled":
            if stored_job.get("write_operation_id"):
                await self.repo.update_write_operation(
                    str(stored_job["write_operation_id"]),
                    organization_id=str(job["tenant_id"]),
                    expected_statuses=["pending", "retry"],
                    status="cancelled",
                    last_error="Connector write was cancelled before provider dispatch",
                    claim_owner=None,
                    claim_expires_at=None,
                )
            job.update({"status": "cancelled", "attempts": 0, "result": None, "error_message": "Execution job was cancelled before worker start", "duration_ms": 0})
            await self.repo.log_execution(
                tenant_id=job["tenant_id"],
                workspace_id=job.get("workspace_id"),
                employee_id=job.get("employee_id"),
                user_id=job.get("user_id"),
                connector_id=job["connector_id"],
                action_name=job["action_name"],
                arguments=job.get("arguments") or {},
                result_status="cancelled",
                error_message=job["error_message"],
                duration_ms=0,
            )
            return job

        ledger: ConnectorWriteLedger | None = None
        write_operation: dict[str, Any] | None = None
        preflight_final: ConnectorResult | None = None
        operation_id = str(job.get("write_operation_id") or "")
        if operation_id:
            ledger = ConnectorWriteLedger(self.repo)
            try:
                claim = await ledger.claim(
                    operation_id,
                    organization_id=str(job["tenant_id"]),
                    owner=f"connector-worker:{uuid.uuid4()}",
                )
            except WriteOperationBusy:
                # A duplicate Redis entry must never become a duplicate provider
                # call. Leave the durable job untouched; outbox recovery will
                # retry it after the active lease expires if needed.
                return {**job, "status": "queued", "attempts": 0}
            except ManualReviewRequired as exc:
                preflight_final = ConnectorResult(
                    status="manual_review", error=str(exc)
                )
            except WriteOperationTerminal as exc:
                preflight_final = ConnectorResult(status="failure", error=str(exc))
            else:
                write_operation = claim.operation
                job["provider_idempotency_key"] = write_operation.get(
                    "provider_idempotency_key"
                )
                job["provider_supports_idempotency"] = bool(
                    write_operation.get("provider_supports_idempotency")
                )
                if claim.kind == "replay":
                    durable = claim.result or {}
                    preflight_final = ConnectorResult(
                        status="success", output=durable.get("output") or durable
                    )
        trace = None
        if self.tracer:
            trace = await self.tracer.start_trace(
                tenant_id=job["tenant_id"],
                workspace_id=job.get("workspace_id") or "default",
                connector_id=job["connector_id"],
                action_name=job["action_name"],
            )

        attempts = 0
        max_attempts = int(job.get("max_attempts") or 1)
        timeout_ms = int(job.get("timeout_ms") or 15000)
        final = preflight_final or ConnectorResult(status="failure", error="Connector execution did not run")
        deferred_retry = False
        provider_response_recorded = False
        while preflight_final is None and attempts < max_attempts:
            attempts += 1
            started = time.perf_counter()
            try:
                final = await self._execute_adapter_controlled(
                    job, timeout_seconds=timeout_ms / 1000
                )
            except Exception as exc:
                final = ConnectorResult(
                    status="ambiguous" if ledger else "failure",
                    error=(
                        "Connector raised after provider dispatch; outcome is unknown"
                        if ledger
                        else str(exc)
                    ),
                )
            final.duration_ms = final.duration_ms or int((time.perf_counter() - started) * 1000)

            retry_now = False
            if ledger and operation_id:
                # This is the first await after the adapter returns. Provider
                # success or ambiguity is durable before tracing, health, or
                # cancellation bookkeeping can crash the worker.
                if final.status == "success":
                    await ledger.record_provider_response(
                        operation_id,
                        organization_id=str(job["tenant_id"]),
                        result={"output": final.output or {}},
                        evidence={
                            "connector_id": job["connector_id"],
                            "action_name": job["action_name"],
                            "status": "success",
                            "duration_ms": final.duration_ms,
                        },
                    )
                    provider_response_recorded = True
                elif final.status in {"ambiguous", "timeout", "cancelled"}:
                    operation = await ledger.mark_ambiguous(
                        operation_id,
                        organization_id=str(job["tenant_id"]),
                        error=final.error or "Provider outcome is ambiguous",
                    )
                    if operation and operation.get("status") == "retry":
                        if attempts < max_attempts and final.status != "cancelled":
                            claim = await ledger.claim(
                                operation_id,
                                organization_id=str(job["tenant_id"]),
                                owner=f"connector-worker:{uuid.uuid4()}",
                            )
                            job["provider_idempotency_key"] = claim.operation.get(
                                "provider_idempotency_key"
                            )
                            retry_now = True
                        else:
                            deferred_retry = True
                            final = ConnectorResult(
                                status="queued",
                                error=(
                                    "Provider outcome was ambiguous; retry retained "
                                    "the same provider idempotency key"
                                ),
                            )
                    else:
                        final = ConnectorResult(
                            status="manual_review",
                            error=(
                                "Provider outcome is ambiguous; automatic retry is disabled"
                            ),
                        )
                else:
                    await ledger.mark_failed(
                        operation_id,
                        organization_id=str(job["tenant_id"]),
                        error=final.error or f"Connector returned {final.status}",
                    )
            if self.tracer and trace:
                await self.tracer.record_step(
                    trace["id"],
                    step_name=f"{job['connector_id']}.{job['action_name']}",
                    status=final.status,
                    attempt=attempts,
                    started_at=started,
                    input_redacted=redact_arguments(job.get("arguments") or {}),
                    output_summary=final.output or {},
                    error=final.error,
                )
            if retry_now:
                continue
            if not ledger and final.status == "timeout" and attempts < max_attempts:
                continue
            break

        # Close the tiny race between the last polling tick and persistence.
        if not provider_response_recorded and await self._cancel_requested(job):
            if operation_id and attempts:
                # The controlled adapter path already committed ambiguity before
                # reaching this check. Preserve that truthful terminal/retry
                # state rather than relabeling it as a clean cancellation.
                if final.status not in {"manual_review", "queued"}:
                    operation = await ledger.mark_ambiguous(
                        operation_id,
                        organization_id=str(job["tenant_id"]),
                        error="Execution was cancelled while the provider outcome was unknown",
                    )
                    final = ConnectorResult(
                        status=(
                            "queued"
                            if operation and operation.get("status") == "retry"
                            else "manual_review"
                        ),
                        error="Execution was cancelled while the provider outcome was unknown",
                    )
            else:
                final = ConnectorResult(
                    status="cancelled", error="Execution job was cancelled"
                )

        if provider_response_recorded and ledger and operation_id:
            await ledger.complete(operation_id, organization_id=str(job["tenant_id"]))

        job.update(
            {
                "status": final.status,
                "attempts": attempts,
                "result": final.output,
                "error_message": final.error,
                "duration_ms": final.duration_ms,
            }
        )
        await self.repo.log_execution(
            tenant_id=job["tenant_id"],
            workspace_id=job.get("workspace_id"),
            employee_id=job.get("employee_id"),
            user_id=job.get("user_id"),
            connector_id=job["connector_id"],
            action_name=job["action_name"],
            arguments=job.get("arguments") or {},
            result_status=final.status,
            error_message=final.error,
            duration_ms=final.duration_ms,
        )
        await self.health.record_execution(job["tenant_id"], job["connector_id"], final.status, duration_ms=final.duration_ms)
        if hasattr(self.repo, "finish_execution_job"):
            await self.repo.finish_execution_job(job["id"], status=final.status, result=final.output, error_message=final.error, attempts=attempts)
        if self.tracer and trace:
            await self.tracer.finish_trace(trace["id"], status=final.status)
        if (
            not deferred_retry
            and job.get("workflow_run_id")
            and job.get("workflow_step_id")
        ):
            from connectors.framework.workflows import WorkflowRuntime

            runtime = WorkflowRuntime(self.repo, self.queue)
            await runtime.complete_step(
                job["workflow_run_id"],
                job["workflow_step_id"],
                tenant_id=job["tenant_id"],
                status="success" if final.status == "success" else "failed",
                output=final.output,
                error=final.error,
            )
            await runtime.tick(job["workflow_run_id"], tenant_id=job["tenant_id"])
        return job

    async def _cancel_requested(self, job: dict[str, Any]) -> bool:
        if not hasattr(self.repo, "get_execution_job"):
            return False
        try:
            stored = await self.repo.get_execution_job(
                job["id"], tenant_id=job["tenant_id"]
            )
        except Exception:
            return False
        return bool(stored and stored.get("status") == "cancelled")

    async def _execute_adapter_controlled(
        self, job: dict[str, Any], *, timeout_seconds: float
    ) -> ConnectorResult:
        """Cancel an in-flight adapter coroutine when durable state is cancelled."""

        execution = asyncio.create_task(self._execute_adapter(job))
        deadline = asyncio.get_running_loop().time() + max(0.001, timeout_seconds)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    return ConnectorResult(
                        status="timeout", error="Connector execution timed out"
                    )
                done, _ = await asyncio.wait(
                    {execution}, timeout=min(0.25, remaining)
                )
                if done:
                    return await execution
                if await self._cancel_requested(job):
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    return ConnectorResult(
                        status="cancelled", error="Execution job was cancelled"
                    )
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    async def _execute_adapter(self, job: dict[str, Any]) -> ConnectorResult:
        adapter = self.adapters.get(job["connector_id"])
        if not adapter:
            return ConnectorResult(status="failure", error="No adapter registered for connector")
        credentials = await self.repo.load_credentials(
            tenant_id=job["tenant_id"],
            workspace_id=job.get("workspace_id"),
            employee_id=job.get("employee_id"),
            user_id=job.get("user_id"),
            connector_id=job["connector_id"],
        )
        if not await adapter.validate_credentials(credentials):
            return ConnectorResult(status="failure", error="Connector credentials are invalid")
        return await adapter.execute(
            job["action_name"],
            job.get("arguments") or {},
            {
                "credentials": credentials,
                "actor": job.get("actor") or {},
                "write_operation_id": job.get("write_operation_id"),
                "provider_idempotency_key": job.get("provider_idempotency_key"),
                "provider_supports_idempotency": bool(
                    job.get("provider_supports_idempotency")
                ),
                "idempotency_header": job.get("idempotency_header"),
            },
        )
