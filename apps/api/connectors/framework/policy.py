from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal


PolicyDecisionValue = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True)
class ConnectorPolicy:
    id: str
    decision: PolicyDecisionValue
    role: str | None = None
    workspace_id: str | None = None
    connector_id: str | None = None
    action_name: str | None = None
    risk_level: str | None = None
    employee_id: str | None = None
    tenant_id: str | None = None
    approval_mode: str = "single"
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyDecisionValue
    reason: str
    approval_mode: str = "single"
    policy_id: str | None = None


class PolicyEngine:
    def __init__(
        self,
        policies: list[ConnectorPolicy] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.policies = sorted(policies or [], key=lambda policy: policy.priority, reverse=True)
        self.clock = clock or (lambda: datetime.now().strftime("%H:%M"))

    @classmethod
    async def from_repository(cls, repo: Any, *, tenant_id: str) -> "PolicyEngine":
        rows = await repo.list_policies(tenant_id=tenant_id, enabled=True)
        policies = [
            ConnectorPolicy(
                id=row["id"],
                decision=row["decision"],
                role=row.get("role"),
                workspace_id=row.get("workspace_id"),
                connector_id=row.get("connector_id"),
                action_name=row.get("action_name"),
                risk_level=row.get("risk_level"),
                employee_id=row.get("employee_id"),
                tenant_id=row.get("tenant_id") or row.get("organization_id"),
                approval_mode=row.get("approval_mode") or "single",
                conditions=row.get("conditions") or {},
                priority=row.get("priority") or 0,
                enabled=bool(row.get("enabled", True)),
            )
            for row in rows
        ]
        return cls(policies)

    async def evaluate(
        self,
        actor_context: dict[str, Any],
        connector: dict[str, Any],
        action: dict[str, Any],
        permission: dict[str, Any] | None,
    ) -> PolicyDecision:
        if not permission:
            return PolicyDecision("deny", "No connector permission grants this action")

        for policy in self.policies:
            if self._matches(policy, actor_context, connector, action):
                if not self._conditions_pass(policy.conditions):
                    if policy.decision == "deny":
                        return PolicyDecision("deny", "Denied by policy outside allowed business hours", policy.approval_mode, policy.id)
                    continue
                return PolicyDecision(policy.decision, f"Matched policy {policy.id}", policy.approval_mode, policy.id)

        if action.get("risk_level") in {"write", "destructive", "financial", "external_message"}:
            return PolicyDecision("require_approval", f"{action['risk_level']} actions require approval", "single")
        if permission.get("approval_required") or action.get("approval_required"):
            return PolicyDecision("require_approval", "Connector permission or action requires approval", "single")
        return PolicyDecision("allow", "Allowed by connector permission")

    def _matches(
        self,
        policy: ConnectorPolicy,
        actor_context: dict[str, Any],
        connector: dict[str, Any],
        action: dict[str, Any],
    ) -> bool:
        if not policy.enabled:
            return False
        checks = {
            "tenant_id": actor_context.get("tenant_id"),
            "workspace_id": actor_context.get("workspace_id"),
            "employee_id": actor_context.get("employee_id"),
            "connector_id": connector.get("id"),
            "action_name": action.get("name"),
            "risk_level": action.get("risk_level"),
        }
        for attr, value in checks.items():
            expected = getattr(policy, attr)
            if expected is not None and expected != value:
                return False
        if policy.role and policy.role not in set(actor_context.get("roles") or []):
            return False
        return True

    def _conditions_pass(self, conditions: dict[str, Any]) -> bool:
        if not conditions.get("business_hours_only"):
            return True
        hour = int(self.clock().split(":", 1)[0])
        return 9 <= hour < 17
