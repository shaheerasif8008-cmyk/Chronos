from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ConnectorType = Literal["native", "mcp", "oauth", "api_key", "internal"]
ConnectorStatus = Literal["available", "installed", "disabled", "error"]
RiskLevel = Literal["read", "write", "destructive", "financial", "external_message"]
ExecutionStatus = Literal[
    "success",
    "failure",
    "timeout",
    "validation_error",
    "permission_denied",
    "approval_required",
    "queued",
    "cancelled",
]


@dataclass(frozen=True)
class ConnectorActionDef:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    required_permissions: list[str]
    risk_level: RiskLevel
    approval_required: bool = False


@dataclass(frozen=True)
class ConnectorDef:
    id: str
    name: str
    provider: str
    description: str
    type: ConnectorType
    auth_type: str
    scopes: list[str]
    actions: list[ConnectorActionDef]
    mcp_config: dict[str, Any] | None = None


@dataclass
class ConnectorResult:
    status: ExecutionStatus
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
