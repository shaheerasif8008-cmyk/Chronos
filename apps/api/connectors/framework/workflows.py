from __future__ import annotations

import uuid
from typing import Any

from connectors.framework.compression import compress_connector_output
from connectors.framework.queued_runtime import QueuedConnectorExecutionService
from core.models import AgentContext


TERMINAL_STEP_STATES = {"success", "failed", "skipped"}
INTERRUPTED_RUN_STATES = {"running", "retrying", "recovering"}


def tenant_of(row: dict[str, Any]) -> str:
    return row.get("tenant_id") or row.get("organization_id") or "default"


class WorkflowRuntime:
    def __init__(self, repo: Any, queue: Any) -> None:
        self.repo = repo
        self.queue = queue

    async def create_workflow(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        employee_id: str,
        user_id: str | None,
        name: str,
        steps: list[dict[str, Any]],
        description: str = "",
        triggers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        workflow = await self.repo.create_workflow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            employee_id=employee_id,
            user_id=user_id,
            name=name,
            description=description,
            definition={"steps": steps},
        )
        for trigger in triggers or []:
            config = dict(trigger.get("config") or {})
            config.setdefault("source", trigger.get("source"))
            config.setdefault("event_type", trigger.get("event_type"))
            await self.repo.create_workflow_trigger(
                tenant_id=tenant_id,
                workflow_id=workflow["id"],
                trigger_type=trigger["trigger_type"],
                config=config,
                status=trigger.get("status") or "active",
            )
        return workflow

    async def start_run(
        self,
        workflow_id: str,
        *,
        tenant_id: str,
        trigger_source: str = "manual",
        trigger_event_type: str | None = None,
        trigger_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow = await self.repo.get_workflow(workflow_id, tenant_id=tenant_id)
        if not workflow:
            raise ValueError("Workflow not found")
        run = await self.repo.create_workflow_run(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            workspace_id=workflow["workspace_id"],
            employee_id=workflow["employee_id"],
            user_id=workflow.get("user_id"),
            status="running",
            correlation_id=f"wf_{uuid.uuid4().hex}",
            trigger_source=trigger_source,
            trigger_event_type=trigger_event_type,
            trigger_payload=trigger_payload or {},
        )
        await self._record_run_event(run, "run_started", {"trigger_source": trigger_source, "trigger_event_type": trigger_event_type})
        for step in workflow["definition"].get("steps", []):
            await self.repo.create_workflow_step(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                run_id=run["id"],
                id=step["id"],
                tool_name=step["tool_name"],
                arguments=step.get("arguments") or {},
                status="queued",
                max_attempts=step.get("max_attempts", 1),
                parallel_safe=bool(step.get("parallel_safe")),
                condition=step.get("condition") or {},
            )
            for dependency in step.get("dependencies") or []:
                await self.repo.create_workflow_dependency(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    run_id=run["id"],
                    step_id=step["id"],
                    depends_on_step_id=dependency,
                )
        await self._checkpoint(run["id"], tenant_id=tenant_id)
        return run

    async def dispatch_event(
        self,
        *,
        tenant_id: str,
        source: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        dispatched: list[dict[str, Any]] = []
        triggers = await self.repo.list_workflow_triggers(tenant_id=tenant_id, status="active")
        for trigger in triggers:
            config = trigger.get("config") or {}
            if config.get("source") != source or config.get("event_type") != event_type:
                continue
            workflow = await self.repo.get_workflow(trigger["workflow_id"], tenant_id=tenant_id)
            if not workflow:
                continue
            if not self._workflow_conditions_match(workflow, payload):
                continue
            run = await self.start_run(
                trigger["workflow_id"],
                tenant_id=tenant_id,
                trigger_source=source,
                trigger_event_type=event_type,
                trigger_payload=payload,
            )
            dispatched.append(run)
        return dispatched

    async def tick(self, run_id: str, *, tenant_id: str) -> dict[str, Any]:
        run = await self.repo.get_workflow_run(run_id, tenant_id=tenant_id)
        if not run:
            raise ValueError("Workflow run not found")
        if run["status"] in {"paused", "cancelled", "completed", "failed", "waiting_for_approval"}:
            return {"run_id": run_id, "status": run["status"], "ready_step_ids": []}
        steps = await self.repo.list_workflow_steps(run_id, tenant_id=tenant_id)
        dependencies = await self.repo.list_workflow_dependencies(run_id, tenant_id=tenant_id)
        completed = {step["id"] for step in steps if step["status"] == "success"}
        blocked_by: dict[str, set[str]] = {}
        for dep in dependencies:
            blocked_by.setdefault(dep["step_id"], set()).add(dep["depends_on_step_id"])
        ready = [
            step
            for step in steps
            if step["status"] == "queued" and blocked_by.get(step["id"], set()).issubset(completed)
        ]
        for step in ready:
            await self._schedule_step(run, step)
        await self._checkpoint(run_id, tenant_id=tenant_id)
        return {"run_id": run_id, "status": "running", "ready_step_ids": [step["id"] for step in ready]}

    async def complete_step(
        self,
        run_id: str,
        step_id: str,
        *,
        tenant_id: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        run = await self.repo.get_workflow_run(run_id, tenant_id=tenant_id)
        if not run:
            raise ValueError("Workflow run not found")
        artifact_ref = None
        if output is not None:
            artifact = await self.repo.create_workflow_artifact(
                tenant_id=tenant_id,
                workflow_id=run["workflow_id"],
                run_id=run_id,
                step_id=step_id,
                kind="connector_output",
                metadata={"step_id": step_id},
                content_compressed=compress_connector_output(output, metadata={"run_id": run_id, "step_id": step_id}),
            )
            artifact_ref = artifact["id"]
        await self.repo.update_workflow_step(
            run_id,
            step_id,
            tenant_id=tenant_id,
            status=status,
            output_ref=artifact_ref,
            error_message=error,
        )
        steps = await self.repo.list_workflow_steps(run_id, tenant_id=tenant_id)
        if all(step["status"] in TERMINAL_STEP_STATES for step in steps):
            await self.repo.update_workflow_run(run_id, tenant_id=tenant_id, status="completed" if all(step["status"] != "failed" for step in steps) else "failed")
        await self._checkpoint(run_id, tenant_id=tenant_id)
        return await self.repo.get_workflow_state(run_id, tenant_id=tenant_id)

    async def pause_run(self, run_id: str, *, tenant_id: str, reason: str) -> dict[str, Any]:
        run = await self.repo.update_workflow_run(run_id, tenant_id=tenant_id, status="paused", pause_reason=reason)
        await self._record_run_event(run, "run_paused", {"reason": reason})
        await self._checkpoint(run_id, tenant_id=tenant_id)
        return run

    async def resume_run(self, run_id: str, *, tenant_id: str) -> dict[str, Any]:
        run = await self.repo.update_workflow_run(run_id, tenant_id=tenant_id, status="running")
        await self._record_run_event(run, "run_resumed", {})
        await self._checkpoint(run_id, tenant_id=tenant_id)
        return run

    async def cancel_run(self, run_id: str, *, tenant_id: str) -> dict[str, Any]:
        run = await self.repo.update_workflow_run(run_id, tenant_id=tenant_id, status="cancelled")
        await self._record_run_event(run, "run_cancelled", {})
        await self._checkpoint(run_id, tenant_id=tenant_id)
        return run

    async def recover_interrupted_runs(self, *, tenant_id: str) -> list[str]:
        recovered: list[str] = []
        candidates: dict[str, dict[str, Any]] = {}
        for status in INTERRUPTED_RUN_STATES:
            for run in await self.repo.list_workflow_runs(tenant_id=tenant_id, status=status):
                candidates[run["id"]] = run
        for run in candidates.values():
            await self.repo.update_workflow_run(run["id"], tenant_id=tenant_id, status="recovering")
            refreshed = await self.repo.get_workflow_run(run["id"], tenant_id=tenant_id)
            if refreshed:
                await self._record_run_event(refreshed, "run_recovered", {})
            await self._checkpoint(run["id"], tenant_id=tenant_id)
            recovered.append(run["id"])
        return recovered

    async def _schedule_step(self, run: dict[str, Any], step: dict[str, Any]) -> None:
        connector_id, action_name = step["tool_name"].split("__", 1)
        result = await QueuedConnectorExecutionService(self.repo, self.queue).enqueue(
            connector_id=connector_id,
            action_name=action_name,
            arguments=step.get("arguments") or {},
            context=AgentContext(id=run["employee_id"], org_id=tenant_of(run), member_id=run.get("user_id"), workspace_id=run["workspace_id"]),
            metadata={"workflow_run_id": run["id"], "workflow_step_id": step["id"], "correlation_id": run["correlation_id"]},
        )
        if result.status == "queued":
            await self.repo.update_workflow_step(run["id"], step["id"], tenant_id=tenant_of(run), status="running", execution_job_id=result.output["job_id"])
        elif result.status == "approval_required":
            await self.repo.update_workflow_step(run["id"], step["id"], tenant_id=tenant_of(run), status="waiting approval", approval_request_id=result.output["approval_request_id"])
            await self.repo.update_workflow_run(run["id"], tenant_id=tenant_of(run), status="waiting_for_approval")
        else:
            await self.repo.update_workflow_step(run["id"], step["id"], tenant_id=tenant_of(run), status="failed", error_message=result.error)
            await self.repo.update_workflow_run(run["id"], tenant_id=tenant_of(run), status="failed")

    async def _checkpoint(self, run_id: str, *, tenant_id: str) -> dict[str, Any]:
        run = await self.repo.get_workflow_run(run_id, tenant_id=tenant_id)
        steps = await self.repo.list_workflow_steps(run_id, tenant_id=tenant_id)
        dependencies = await self.repo.list_workflow_dependencies(run_id, tenant_id=tenant_id)
        snapshot = {
            "run": run,
            "steps": {step["id"]: step for step in steps},
            "dependencies": dependencies,
        }
        return await self.repo.upsert_workflow_state(
            tenant_id=tenant_id,
            workflow_id=run["workflow_id"],
            run_id=run_id,
            snapshot=snapshot,
        )

    def _workflow_conditions_match(self, workflow: dict[str, Any], payload: dict[str, Any]) -> bool:
        steps = (workflow.get("definition") or {}).get("steps") or []
        conditions = [condition for step in steps for condition in (step.get("conditions") or [])]
        if not conditions:
            return True
        return all(self._condition_matches(condition, payload) for condition in conditions)

    def _condition_matches(self, condition: dict[str, Any], payload: dict[str, Any]) -> bool:
        field = str(condition.get("field") or "")
        if field.startswith("payload."):
            field = field[len("payload."):]
        value: Any = payload
        for part in field.split("."):
            if not part:
                continue
            if not isinstance(value, dict):
                return False
            value = value.get(part)
        operator = condition.get("operator") or "equals"
        expected = condition.get("value")
        if operator == "equals":
            return value == expected
        if operator == "contains":
            return str(expected) in str(value)
        if operator == "exists":
            return value is not None
        return False

    async def _record_run_event(self, run: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        if not hasattr(self.repo, "record_workflow_run_event"):
            return
        await self.repo.record_workflow_run_event(
            tenant_id=tenant_of(run),
            workflow_id=run["workflow_id"],
            run_id=run["id"],
            event_type=event_type,
            actor_id=run.get("user_id"),
            payload=payload,
        )
