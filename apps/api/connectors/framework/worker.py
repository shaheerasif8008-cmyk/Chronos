from __future__ import annotations

import asyncio
import time
from typing import Any

from connectors.framework.audit import redact_arguments
from connectors.framework.health import ConnectorHealthService
from connectors.framework.models import ConnectorResult


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
        if stored_job and stored_job.get("status") == "cancelled":
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
        final = ConnectorResult(status="failure", error="Connector execution did not run")
        while attempts < max_attempts:
            attempts += 1
            started = time.perf_counter()
            try:
                final = await asyncio.wait_for(self._execute_adapter(job), timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                final = ConnectorResult(status="timeout", error="Connector execution timed out")
            except Exception as exc:
                final = ConnectorResult(status="failure", error=str(exc))
            final.duration_ms = final.duration_ms or int((time.perf_counter() - started) * 1000)
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
            if final.status != "timeout":
                break

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
        if job.get("workflow_run_id") and job.get("workflow_step_id"):
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
            {"credentials": credentials, "actor": job.get("actor") or {}},
        )
