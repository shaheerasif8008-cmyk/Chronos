from __future__ import annotations

import time
from typing import Any


class ExecutionTracer:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def start_trace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        connector_id: str,
        action_name: str,
        graph: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.repo.create_execution_trace(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            connector_id=connector_id,
            action_name=action_name,
            status="running",
            graph=graph or {"nodes": [f"{connector_id}.{action_name}"], "edges": []},
        )

    async def record_step(
        self,
        trace_id: str,
        *,
        step_name: str,
        status: str,
        attempt: int,
        started_at: float,
        input_redacted: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return await self.repo.create_trace_step(
            trace_id=trace_id,
            step_name=step_name,
            status=status,
            attempt=attempt,
            input_redacted=input_redacted or {},
            output_summary=output_summary or {},
            error=error,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
        )

    async def finish_trace(self, trace_id: str, *, status: str) -> dict[str, Any]:
        return await self.repo.finish_execution_trace(trace_id, status=status)
