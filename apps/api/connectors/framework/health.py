from __future__ import annotations

from typing import Any


class ConnectorHealthService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def record_execution(self, tenant_id: str, connector_id: str, status: str, *, duration_ms: int) -> dict[str, Any]:
        current = await self.repo.get_connector_health(tenant_id=tenant_id, connector_id=connector_id)
        total = int(current.get("total_count", 0)) + 1 if current else 1
        failures = int(current.get("failure_count", 0)) + (1 if status == "failure" else 0) if current else (1 if status == "failure" else 0)
        timeouts = int(current.get("timeout_count", 0)) + (1 if status == "timeout" else 0) if current else (1 if status == "timeout" else 0)
        rate_limits = int(current.get("rate_limit_count", 0)) + (1 if status == "rate_limited" else 0) if current else (1 if status == "rate_limited" else 0)
        previous_latency = float(current.get("latency_ms", 0)) if current else 0
        latency = int(((previous_latency * (total - 1)) + duration_ms) / total)
        failure_rate = failures / total
        timeout_rate = timeouts / total
        if status == "rate_limited":
            health_status = "rate_limited"
        elif timeout_rate >= 0.25 or failure_rate >= 0.25:
            health_status = "degraded"
        else:
            health_status = "healthy"
        return await self.repo.upsert_connector_health(
            tenant_id=tenant_id,
            connector_id=connector_id,
            status=health_status,
            latency_ms=latency,
            failure_rate=failure_rate,
            timeout_rate=timeout_rate,
            failure_count=failures,
            timeout_count=timeouts,
            total_count=total,
            rate_limit_count=rate_limits,
            last_success=status == "success",
        )
