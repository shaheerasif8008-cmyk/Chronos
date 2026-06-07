from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from connectors.framework.audit import redact_arguments
from connectors.framework.models import ConnectorDef


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorRepository(Protocol):
    async def upsert_connector_definition(self, connector: ConnectorDef, tenant_id: str = "default") -> None:
        ...

    async def install_connector(self, connector_id: str, *, tenant_id: str, workspace_id: str, installed_by: str | None = None) -> dict[str, Any]:
        ...

    async def disable_connector(self, connector_id: str, *, tenant_id: str) -> None:
        ...

    async def list_connectors(self, *, tenant_id: str) -> list[dict[str, Any]]:
        ...

    async def get_connector(self, connector_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        ...

    async def get_action(self, connector_id: str, action_name: str) -> dict[str, Any] | None:
        ...

    async def list_actions(self, connector_id: str) -> list[dict[str, Any]]:
        ...

    async def grant_permission(self, **values: Any) -> dict[str, Any]:
        ...

    async def revoke_permission(self, *, tenant_id: str, workspace_id: str, employee_id: str, connector_id: str, action_name: str) -> None:
        ...

    async def get_permission(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str, action_name: str) -> dict[str, Any] | None:
        ...

    async def list_permitted_actions(self, *, tenant_id: str, workspace_id: str, employee_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        ...

    async def load_credentials(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str) -> dict[str, Any]:
        ...

    async def log_execution(self, **values: Any) -> dict[str, Any]:
        ...

    async def list_execution_logs(self, *, tenant_id: str, connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        ...


class InMemoryConnectorRepository:
    def __init__(self) -> None:
        self.connectors: dict[str, dict[str, Any]] = {}
        self.actions: dict[tuple[str, str], dict[str, Any]] = {}
        self.permissions: list[dict[str, Any]] = []
        self.installations: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.approval_events: list[dict[str, Any]] = []
        self.health: dict[tuple[str, str], dict[str, Any]] = {}
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self.mcp_logs: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.trace_steps: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tool_plans: list[dict[str, Any]] = []
        self.policies: list[dict[str, Any]] = []
        self.workflows: dict[str, dict[str, Any]] = {}
        self.workflow_runs: dict[str, dict[str, Any]] = {}
        self.workflow_steps: dict[tuple[str, str], dict[str, Any]] = {}
        self.workflow_dependencies: list[dict[str, Any]] = []
        self.workflow_states: dict[str, dict[str, Any]] = {}
        self.workflow_artifacts: list[dict[str, Any]] = []
        self.workflow_triggers: list[dict[str, Any]] = []
        self.workflow_run_events: list[dict[str, Any]] = []

    async def upsert_connector_definition(self, connector: ConnectorDef, tenant_id: str = "default") -> None:
        existing = self.connectors.get(connector.id, {})
        self.connectors[connector.id] = {
            "id": connector.id,
            "name": connector.name,
            "provider": connector.provider,
            "description": connector.description,
            "type": connector.type,
            "status": existing.get("status", "available"),
            "auth_type": connector.auth_type,
            "scopes": connector.scopes,
            "actions": [action.name for action in connector.actions],
            "organization_id": tenant_id,
            "workspace_id": existing.get("workspace_id"),
            "created_at": existing.get("created_at", now_iso()),
            "updated_at": now_iso(),
            "mcp_config": connector.mcp_config or {},
        }
        for action in connector.actions:
            self.actions[(connector.id, action.name)] = {
                "id": f"{connector.id}:{action.name}",
                "connector_id": connector.id,
                "name": action.name,
                "description": action.description,
                "parameters_schema": action.parameters_schema,
                "output_schema": action.output_schema,
                "required_permissions": action.required_permissions,
                "risk_level": action.risk_level,
                "approval_required": action.approval_required,
            }

    async def install_connector(self, connector_id: str, *, tenant_id: str, workspace_id: str, installed_by: str | None = None) -> dict[str, Any]:
        connector = self.connectors[connector_id]
        connector.update({"status": "installed", "workspace_id": workspace_id, "updated_at": now_iso()})
        self.installations.append(
            {
                "id": f"inst_{len(self.installations) + 1}",
                "connector_id": connector_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "installed_by": installed_by,
            }
        )
        return dict(connector)

    async def disable_connector(self, connector_id: str, *, tenant_id: str) -> None:
        self.connectors[connector_id]["status"] = "disabled"
        self.connectors[connector_id]["updated_at"] = now_iso()

    async def list_connectors(self, *, tenant_id: str) -> list[dict[str, Any]]:
        return list(self.connectors.values())

    async def get_connector(self, connector_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        connector = self.connectors.get(connector_id)
        return dict(connector) if connector else None

    async def get_action(self, connector_id: str, action_name: str) -> dict[str, Any] | None:
        action = self.actions.get((connector_id, action_name))
        return dict(action) if action else None

    async def list_actions(self, connector_id: str) -> list[dict[str, Any]]:
        return [dict(action) for (cid, _), action in self.actions.items() if cid == connector_id]

    async def grant_permission(self, **values: Any) -> dict[str, Any]:
        await self.revoke_permission(
            tenant_id=values["tenant_id"],
            workspace_id=values["workspace_id"],
            employee_id=values["employee_id"],
            connector_id=values["connector_id"],
            action_name=values["action_name"],
        )
        row = {"id": f"perm_{len(self.permissions) + 1}", "created_at": now_iso(), **values}
        self.permissions.append(row)
        return dict(row)

    async def revoke_permission(self, *, tenant_id: str, workspace_id: str, employee_id: str, connector_id: str, action_name: str) -> None:
        self.permissions = [
            permission
            for permission in self.permissions
            if not (
                permission["tenant_id"] == tenant_id
                and permission["workspace_id"] == workspace_id
                and permission["employee_id"] == employee_id
                and permission["connector_id"] == connector_id
                and permission["action_name"] == action_name
            )
        ]

    async def get_permission(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str, action_name: str) -> dict[str, Any] | None:
        for permission in self.permissions:
            if (
                permission["tenant_id"] == tenant_id
                and permission["connector_id"] == connector_id
                and permission["action_name"] == action_name
                and permission.get("workspace_id") == workspace_id
                and (permission.get("employee_id") == employee_id or permission.get("user_id") == user_id)
            ):
                return dict(permission)
        return None

    async def list_permitted_actions(self, *, tenant_id: str, workspace_id: str, employee_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        rows = []
        for permission in self.permissions:
            if permission["tenant_id"] != tenant_id or permission["workspace_id"] != workspace_id or permission["employee_id"] != employee_id:
                continue
            connector = self.connectors.get(permission["connector_id"])
            action = self.actions.get((permission["connector_id"], permission["action_name"]))
            if connector and connector["status"] == "installed" and action:
                rows.append((dict(connector), dict(action), dict(permission)))
        return rows

    async def load_credentials(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str) -> dict[str, Any]:
        return {}

    async def log_execution(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"log_{len(self.logs) + 1}", "created_at": now_iso(), **values}
        self.logs.insert(0, row)
        return dict(row)

    async def list_execution_logs(self, *, tenant_id: str, connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [log for log in self.logs if log["tenant_id"] == tenant_id and (connector_id is None or log["connector_id"] == connector_id)]
        return rows[:limit]

    async def create_approval_request(self, **values: Any) -> dict[str, Any]:
        row = {
            "id": f"appr_{len(self.approvals) + 1}",
            "status": "pending",
            "approval_count": 0,
            "created_at": now_iso(),
            "resolved_at": None,
            "resumed_job_id": None,
            **values,
        }
        self.approvals.insert(0, row)
        self.approval_events.insert(0, {"id": f"appevt_{len(self.approval_events) + 1}", "approval_request_id": row["id"], "event_type": "created", "created_at": now_iso()})
        return dict(row)

    async def list_approval_requests(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.approvals if row["tenant_id"] == tenant_id and (status is None or row["status"] == status)]
        return [dict(row) for row in rows[:limit]]

    async def resolve_approval_request(self, approval_id: str, *, tenant_id: str, actor_id: str, status: str, note: str | None = None) -> dict[str, Any]:
        for row in self.approvals:
            if row["id"] == approval_id and row["tenant_id"] == tenant_id:
                row["status"] = status
                row["resolved_at"] = now_iso()
                row["approval_count"] = row.get("required_approvals", 1) if status == "approved" else row.get("approval_count", 0)
                self.approval_events.insert(
                    0,
                    {
                        "id": f"appevt_{len(self.approval_events) + 1}",
                        "approval_request_id": approval_id,
                        "actor_id": actor_id,
                        "event_type": status,
                        "note": note,
                        "created_at": now_iso(),
                    },
                )
                return dict(row)
        raise ValueError("Approval request not found")

    async def mark_approval_resumed(self, approval_id: str, *, tenant_id: str, job_id: str) -> dict[str, Any]:
        for row in self.approvals:
            if row["id"] == approval_id and row["tenant_id"] == tenant_id:
                row["resumed_job_id"] = job_id
                self.approval_events.insert(
                    0,
                    {
                        "id": f"appevt_{len(self.approval_events) + 1}",
                        "approval_request_id": approval_id,
                        "actor_id": None,
                        "event_type": "resumed",
                        "note": job_id,
                        "created_at": now_iso(),
                    },
                )
                return dict(row)
        raise ValueError("Approval request not found")

    async def upsert_connector_health(self, **values: Any) -> dict[str, Any]:
        key = (values["tenant_id"], values["connector_id"])
        row = {**self.health.get(key, {}), **values, "updated_at": now_iso()}
        self.health[key] = row
        return dict(row)

    async def get_connector_health(self, *, tenant_id: str, connector_id: str) -> dict[str, Any] | None:
        row = self.health.get((tenant_id, connector_id))
        return dict(row) if row else None

    async def list_connector_health(self, *, tenant_id: str) -> list[dict[str, Any]]:
        return [dict(row) for (tid, _), row in self.health.items() if tid == tenant_id]

    async def register_mcp_server(self, *, tenant_id: str, name: str, transport: str, command: str | None = None, server_url: str | None = None) -> dict[str, Any]:
        row = {
            "id": f"mcp_{len(self.mcp_servers) + 1}",
            "tenant_id": tenant_id,
            "name": name,
            "transport": transport,
            "command": command,
            "server_url": server_url,
            "status": "available",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self.mcp_servers[row["id"]] = row
        return dict(row)

    async def get_mcp_server(self, server_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        row = self.mcp_servers.get(server_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    async def list_mcp_servers(self, *, tenant_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.mcp_servers.values() if row["tenant_id"] == tenant_id]

    async def log_mcp_discovery(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"mcplog_{len(self.mcp_logs) + 1}", "created_at": now_iso(), **values}
        self.mcp_logs.insert(0, row)
        return dict(row)

    async def list_mcp_discovery_logs(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.mcp_logs if row["tenant_id"] == tenant_id]
        return [dict(row) for row in rows[:limit]]

    async def create_execution_trace(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"trace_{len(self.traces) + 1}", "started_at": now_iso(), "completed_at": None, "duration_ms": 0, **values}
        self.traces.insert(0, row)
        return dict(row)

    async def finish_execution_trace(self, trace_id: str, *, status: str) -> dict[str, Any]:
        for row in self.traces:
            if row["id"] == trace_id:
                row["status"] = status
                row["completed_at"] = now_iso()
                return dict(row)
        raise ValueError("Execution trace not found")

    async def create_trace_step(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"tracestep_{len(self.trace_steps) + 1}", "created_at": now_iso(), **values}
        self.trace_steps.append(row)
        return dict(row)

    async def list_execution_traces(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.traces if row["tenant_id"] == tenant_id]
        return [dict(row) for row in rows[:limit]]

    async def list_trace_steps(self, trace_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.trace_steps if row["trace_id"] == trace_id]

    async def create_execution_job(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"job_{len(self.jobs) + 1}", "status": "queued", "attempts": 0, "created_at": now_iso(), "updated_at": now_iso(), **values}
        self.jobs[row["id"]] = row
        return dict(row)

    async def finish_execution_job(self, job_id: str, **values: Any) -> dict[str, Any]:
        row = self.jobs.get(job_id, {"id": job_id})
        row.update(values)
        row["updated_at"] = now_iso()
        self.jobs[job_id] = row
        return dict(row)

    async def get_execution_job(self, job_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        row = self.jobs.get(job_id)
        return dict(row) if row and row.get("tenant_id") == tenant_id else None

    async def list_execution_jobs(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.jobs.values() if row["tenant_id"] == tenant_id and (status is None or row["status"] == status)]
        return [dict(row) for row in rows[:limit]]

    async def cancel_execution_job(self, job_id: str, *, tenant_id: str) -> dict[str, Any]:
        row = self.jobs[job_id]
        if row["tenant_id"] != tenant_id:
            raise ValueError("Execution job not found")
        row["status"] = "cancelled"
        row["updated_at"] = now_iso()
        return dict(row)

    async def create_tool_execution_plan(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"toolplan_{len(self.tool_plans) + 1}", "created_at": now_iso(), **values}
        self.tool_plans.insert(0, row)
        return dict(row)

    async def create_policy(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"policy_{len(self.policies) + 1}", "created_at": now_iso(), "updated_at": now_iso(), **values}
        self.policies.insert(0, row)
        return dict(row)

    async def list_policies(self, *, tenant_id: str, enabled: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [row for row in self.policies if row["tenant_id"] == tenant_id and (enabled is None or bool(row.get("enabled", True)) == enabled)]
        return [dict(row) for row in rows[:limit]]

    async def delete_policy(self, policy_id: str, *, tenant_id: str) -> None:
        self.policies = [row for row in self.policies if not (row["id"] == policy_id and row["tenant_id"] == tenant_id)]

    async def create_workflow(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"workflow_{len(self.workflows) + 1}", "status": "pending", "created_at": now_iso(), "updated_at": now_iso(), **values}
        self.workflows[row["id"]] = row
        return dict(row)

    async def get_workflow(self, workflow_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        row = self.workflows.get(workflow_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    async def list_workflows(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.workflows.values() if row["tenant_id"] == tenant_id and (status is None or row["status"] == status)]
        return [dict(row) for row in rows[:limit]]

    async def create_workflow_run(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"run_{len(self.workflow_runs) + 1}", "status": "pending", "created_at": now_iso(), "updated_at": now_iso(), **values}
        self.workflow_runs[row["id"]] = row
        return dict(row)

    async def get_workflow_run(self, run_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        row = self.workflow_runs.get(run_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    async def update_workflow_run(self, run_id: str, *, tenant_id: str, **values: Any) -> dict[str, Any]:
        row = self.workflow_runs[run_id]
        if row["tenant_id"] != tenant_id:
            raise ValueError("Workflow run not found")
        row.update(values)
        row["updated_at"] = now_iso()
        return dict(row)

    async def list_workflow_runs(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.workflow_runs.values() if row["tenant_id"] == tenant_id and (status is None or row["status"] == status)]
        return [dict(row) for row in rows[:limit]]

    async def create_workflow_step(self, **values: Any) -> dict[str, Any]:
        row = {"status": "queued", "attempts": 0, "created_at": now_iso(), "updated_at": now_iso(), **values}
        self.workflow_steps[(row["run_id"], row["id"])] = row
        return dict(row)

    async def list_workflow_steps(self, run_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
        rows = [row for (rid, _), row in self.workflow_steps.items() if rid == run_id and row["tenant_id"] == tenant_id]
        return [dict(row) for row in rows]

    async def update_workflow_step(self, run_id: str, step_id: str, *, tenant_id: str, **values: Any) -> dict[str, Any]:
        row = self.workflow_steps[(run_id, step_id)]
        if row["tenant_id"] != tenant_id:
            raise ValueError("Workflow step not found")
        if row.get("status") in {"success", "failed", "skipped"} and values.get("status") == "running":
            return dict(row)
        row.update(values)
        row["updated_at"] = now_iso()
        return dict(row)

    async def create_workflow_dependency(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"wfdep_{len(self.workflow_dependencies) + 1}", **values}
        self.workflow_dependencies.append(row)
        return dict(row)

    async def list_workflow_dependencies(self, run_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.workflow_dependencies if row["run_id"] == run_id and row["tenant_id"] == tenant_id]

    async def upsert_workflow_state(self, **values: Any) -> dict[str, Any]:
        row = {**self.workflow_states.get(values["run_id"], {}), **values, "updated_at": now_iso()}
        row.setdefault("id", f"wfstate_{len(self.workflow_states) + 1}")
        row.setdefault("checkpoint_version", 1)
        self.workflow_states[values["run_id"]] = row
        return dict(row)

    async def get_workflow_state(self, run_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        row = self.workflow_states.get(run_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    async def create_workflow_artifact(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"wfartifact_{len(self.workflow_artifacts) + 1}", "created_at": now_iso(), **values}
        self.workflow_artifacts.insert(0, row)
        return dict(row)

    async def create_workflow_trigger(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"wftrigger_{len(self.workflow_triggers) + 1}", "status": "active", "created_at": now_iso(), **values}
        self.workflow_triggers.insert(0, row)
        return dict(row)

    async def list_workflow_triggers(self, workflow_id: str | None = None, *, tenant_id: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            row for row in self.workflow_triggers
            if row["tenant_id"] == tenant_id
            and (workflow_id is None or row["workflow_id"] == workflow_id)
            and (status is None or row.get("status") == status)
        ]
        return [dict(row) for row in rows[:limit]]

    async def record_workflow_run_event(self, **values: Any) -> dict[str, Any]:
        row = {"id": f"wfevent_{len(self.workflow_run_events) + 1}", "created_at": now_iso(), **values}
        self.workflow_run_events.append(row)
        return dict(row)

    async def list_workflow_run_history(self, run_id: str, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = [row for row in self.workflow_run_events if row["tenant_id"] == tenant_id and row["run_id"] == run_id]
        return [dict(row) for row in rows[-limit:]]


class DatabaseConnectorRepository:
    async def upsert_connector_definition(self, connector: ConnectorDef, tenant_id: str = "default") -> None:
        from sqlalchemy import text

        from core.config import settings
        from core.db import engine

        actions = [action.name for action in connector.actions]
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO connectors (
                        id, organization_id, region, name, provider, description, type, status,
                        auth_type, scopes, actions, vault_ref, mcp_config, created_at, updated_at
                    )
                    VALUES (
                        :id, :organization_id, :region, :name, :provider, :description, :type,
                        COALESCE((SELECT status FROM connectors WHERE id = :id), 'available'),
                        :auth_type, :scopes, CAST(:actions AS jsonb), 'internal:none', CAST(:mcp_config AS jsonb), NOW(), NOW()
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        provider = EXCLUDED.provider,
                        description = EXCLUDED.description,
                        type = EXCLUDED.type,
                        auth_type = EXCLUDED.auth_type,
                        scopes = EXCLUDED.scopes,
                        actions = EXCLUDED.actions,
                        mcp_config = EXCLUDED.mcp_config,
                        updated_at = NOW()
                    """
                ),
                {
                    "id": connector.id,
                    "organization_id": tenant_id,
                    "region": settings.region,
                    "name": connector.name,
                    "provider": connector.provider,
                    "description": connector.description,
                    "type": connector.type,
                    "auth_type": connector.auth_type,
                    "scopes": connector.scopes,
                    "actions": json.dumps(actions),
                    "mcp_config": json.dumps(connector.mcp_config or {}),
                },
            )
            for action in connector.actions:
                await conn.execute(
                    text(
                        """
                        INSERT INTO connector_actions (
                            connector_id, name, description, parameters_schema, output_schema,
                            required_permissions, risk_level, approval_required, created_at, updated_at
                        )
                        VALUES (
                            :connector_id, :name, :description, CAST(:parameters_schema AS jsonb), CAST(:output_schema AS jsonb),
                            :required_permissions, :risk_level, :approval_required, NOW(), NOW()
                        )
                        ON CONFLICT (connector_id, name) DO UPDATE SET
                            description = EXCLUDED.description,
                            parameters_schema = EXCLUDED.parameters_schema,
                            output_schema = EXCLUDED.output_schema,
                            required_permissions = EXCLUDED.required_permissions,
                            risk_level = EXCLUDED.risk_level,
                            approval_required = EXCLUDED.approval_required,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "connector_id": connector.id,
                        "name": action.name,
                        "description": action.description,
                        "parameters_schema": json.dumps(action.parameters_schema),
                        "output_schema": json.dumps(action.output_schema) if action.output_schema else None,
                        "required_permissions": action.required_permissions,
                        "risk_level": action.risk_level,
                        "approval_required": action.approval_required,
                    },
                )

    async def install_connector(self, connector_id: str, *, tenant_id: str, workspace_id: str, installed_by: str | None = None) -> dict[str, Any]:
        from sqlalchemy import insert, text, update

        from core.config import settings
        from core.db import engine, reflect_table

        connectors = await reflect_table("connectors")
        installations = await reflect_table("connector_installations")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(connectors)
                    .where(connectors.c.id == connector_id, connectors.c.organization_id == tenant_id)
                    .values(status="installed", updated_at=text("NOW()"))
                    .returning(connectors)
                )
            ).mappings().first()
            if row is None:
                raise ValueError("Connector not found")
            await conn.execute(
                insert(installations).values(
                    organization_id=tenant_id,
                    region=settings.region,
                    connector_id=connector_id,
                    workspace_id=workspace_id,
                    installed_by=installed_by,
                    status="installed",
                )
            )
        return dict(row)

    async def disable_connector(self, connector_id: str, *, tenant_id: str) -> None:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        connectors = await reflect_table("connectors")
        async with engine.begin() as conn:
            await conn.execute(
                update(connectors)
                .where(connectors.c.id == connector_id, connectors.c.organization_id == tenant_id)
                .values(status="disabled", updated_at=text("NOW()"))
            )

    async def list_connectors(self, *, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        connectors = await reflect_table("connectors")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(select(connectors).where(connectors.c.organization_id == tenant_id).order_by(connectors.c.name.asc()))
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get_connector(self, connector_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        connectors = await reflect_table("connectors")
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(connectors).where(connectors.c.id == connector_id, connectors.c.organization_id == tenant_id))
            ).mappings().first()
        return dict(row) if row else None

    async def get_action(self, connector_id: str, action_name: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        actions = await reflect_table("connector_actions")
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(actions).where(actions.c.connector_id == connector_id, actions.c.name == action_name))
            ).mappings().first()
        return dict(row) if row else None

    async def list_actions(self, connector_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        actions = await reflect_table("connector_actions")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(select(actions).where(actions.c.connector_id == connector_id).order_by(actions.c.name.asc()))
            ).mappings().all()
        return [dict(row) for row in rows]

    async def grant_permission(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        permissions = await reflect_table("connector_permissions")
        await self.revoke_permission(
            tenant_id=values["tenant_id"],
            workspace_id=values["workspace_id"],
            employee_id=values["employee_id"],
            connector_id=values["connector_id"],
            action_name=values["action_name"],
        )
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(permissions)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        employee_id=values["employee_id"],
                        user_id=values.get("user_id"),
                        connector_id=values["connector_id"],
                        action_name=values["action_name"],
                        allowed_scopes=values.get("allowed_scopes", []),
                        approval_required=values.get("approval_required", False),
                    )
                    .returning(permissions)
                )
            ).mappings().first()
        return dict(row)

    async def get_execution_job(self, job_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_jobs")
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(table.c.id == job_id, table.c.organization_id == tenant_id))).mappings().first()
        return dict(row) if row else None

    async def list_execution_jobs(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_jobs")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if status:
            stmt = stmt.where(table.c.status == status)
        stmt = stmt.order_by(table.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def cancel_execution_job(self, job_id: str, *, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_jobs")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == job_id, table.c.organization_id == tenant_id, table.c.status.in_(["queued", "running"]))
                    .values(status="cancelled", completed_at=text("NOW()"), updated_at=text("NOW()"))
                    .returning(table)
                )
            ).mappings().first()
        if not row:
            raise ValueError("Execution job not found or cannot be cancelled")
        return dict(row)

    async def revoke_permission(self, *, tenant_id: str, workspace_id: str, employee_id: str, connector_id: str, action_name: str) -> None:
        from sqlalchemy import text

        from core.db import engine, reflect_table

        permissions = await reflect_table("connector_permissions")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DELETE FROM connector_permissions
                    WHERE organization_id = :tenant_id
                      AND workspace_id = :workspace_id
                      AND employee_id = :employee_id
                      AND connector_id = :connector_id
                      AND action_name = :action_name
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "employee_id": employee_id,
                    "connector_id": connector_id,
                    "action_name": action_name,
                },
            )

    async def get_permission(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str, action_name: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        permissions = await reflect_table("connector_permissions")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(permissions).where(
                        permissions.c.organization_id == tenant_id,
                        permissions.c.workspace_id == workspace_id,
                        permissions.c.connector_id == connector_id,
                        permissions.c.action_name == action_name,
                        (permissions.c.employee_id == employee_id) | (permissions.c.user_id == user_id),
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_permitted_actions(self, *, tenant_id: str, workspace_id: str, employee_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        from sqlalchemy import text

        from core.db import engine

        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT c.*, a.name AS action_name, a.description AS action_description,
                               a.parameters_schema, a.output_schema, a.required_permissions,
                               a.risk_level, a.approval_required AS action_approval_required,
                               p.allowed_scopes, p.approval_required AS permission_approval_required
                        FROM connector_permissions p
                        JOIN connectors c ON c.id = p.connector_id
                        JOIN connector_actions a ON a.connector_id = p.connector_id AND a.name = p.action_name
                        WHERE p.organization_id = :tenant_id
                          AND p.workspace_id = :workspace_id
                          AND p.employee_id = :employee_id
                          AND c.status = 'installed'
                        ORDER BY c.id, a.name
                        """
                    ),
                    {"tenant_id": tenant_id, "workspace_id": workspace_id, "employee_id": employee_id},
                )
            ).mappings().all()
        triples = []
        for row in rows:
            data = dict(row)
            connector = {key: data[key] for key in data.keys() if not key.startswith("action_") and key not in {"parameters_schema", "output_schema", "required_permissions", "risk_level", "allowed_scopes", "permission_approval_required"}}
            action = {
                "connector_id": data["id"],
                "name": data["action_name"],
                "description": data["action_description"],
                "parameters_schema": data["parameters_schema"],
                "output_schema": data["output_schema"],
                "required_permissions": data["required_permissions"],
                "risk_level": data["risk_level"],
                "approval_required": data["action_approval_required"],
            }
            permission = {
                "allowed_scopes": data["allowed_scopes"],
                "approval_required": data["permission_approval_required"],
            }
            triples.append((connector, action, permission))
        return triples

    async def load_credentials(self, *, tenant_id: str, workspace_id: str | None, employee_id: str | None, user_id: str | None, connector_id: str) -> dict[str, Any]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        credentials = await reflect_table("connector_credentials")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(credentials).where(
                        credentials.c.organization_id == tenant_id,
                        credentials.c.connector_id == connector_id,
                        credentials.c.status == "active",
                    )
                )
            ).mappings().first()
        if not row:
            return {}
        from connectors import vault

        return await vault.get(row["vault_ref"])

    async def log_execution(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core import audit
        from core.config import settings
        from core.db import engine, reflect_table

        logs = await reflect_table("connector_execution_logs")
        redacted = redact_arguments(values.get("arguments") or {})
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(logs)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values.get("workspace_id"),
                        employee_id=values.get("employee_id"),
                        user_id=values.get("user_id"),
                        connector_id=values["connector_id"],
                        action_name=values["action_name"],
                        arguments_redacted=redacted,
                        result_status=values["result_status"],
                        error_message=values.get("error_message"),
                        duration_ms=values.get("duration_ms", 0),
                    )
                    .returning(logs)
                )
            ).mappings().first()
        await audit.log(
            "connector_execution",
            values.get("user_id") or values.get("employee_id"),
            f"connector.{values['connector_id']}.{values['action_name']}",
            organization_id=values["tenant_id"],
            resource_type="connectors",
            resource_id=values["connector_id"],
            payload={
                "workspace_id": values.get("workspace_id"),
                "employee_id": values.get("employee_id"),
                "user_id": values.get("user_id"),
                "arguments": redacted,
                "duration_ms": values.get("duration_ms", 0),
                "error_message": values.get("error_message"),
            },
            decision=values["result_status"],
        )
        return dict(row)

    async def list_execution_logs(self, *, tenant_id: str, connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        logs = await reflect_table("connector_execution_logs")
        stmt = select(logs).where(logs.c.organization_id == tenant_id)
        if connector_id:
            stmt = stmt.where(logs.c.connector_id == connector_id)
        stmt = stmt.order_by(logs.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def create_approval_request(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("approval_requests")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        employee_id=values["employee_id"],
                        user_id=values.get("user_id"),
                        connector_id=values["connector_id"],
                        action_name=values["action_name"],
                        risk_level=values["risk_level"],
                        arguments_redacted=values.get("arguments_redacted") or {},
                        justification=values.get("justification") or "",
                        approval_mode=values.get("approval_mode") or "single",
                        required_approvals=values.get("required_approvals") or 1,
                        expires_at=values.get("expires_at"),
                        execution_payload=values.get("execution_payload") or {},
                    )
                    .returning(table)
                )
            ).mappings().first()
        await self._log_approval_event(row["id"], tenant_id=values["tenant_id"], actor_id=values.get("user_id"), event_type="created")
        return dict(row)

    async def _log_approval_event(self, approval_request_id: str, *, tenant_id: str, actor_id: str | None, event_type: str, note: str | None = None) -> None:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("approval_events")
        async with engine.begin() as conn:
            await conn.execute(
                insert(table).values(
                    organization_id=tenant_id,
                    region=settings.region,
                    approval_request_id=approval_request_id,
                    actor_id=actor_id,
                    event_type=event_type,
                    note=note,
                )
            )

    async def list_approval_requests(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("approval_requests")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if status:
            stmt = stmt.where(table.c.status == status)
        stmt = stmt.order_by(table.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def resolve_approval_request(self, approval_id: str, *, tenant_id: str, actor_id: str, status: str, note: str | None = None) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("approval_requests")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == approval_id, table.c.organization_id == tenant_id)
                    .values(status=status, resolved_at=text("NOW()"), approval_count=text("required_approvals") if status == "approved" else text("approval_count"))
                    .returning(table)
                )
            ).mappings().first()
        if not row:
            raise ValueError("Approval request not found")
        await self._log_approval_event(approval_id, tenant_id=tenant_id, actor_id=actor_id, event_type=status, note=note)
        return dict(row)

    async def mark_approval_resumed(self, approval_id: str, *, tenant_id: str, job_id: str) -> dict[str, Any]:
        from sqlalchemy import update

        from core.db import engine, reflect_table

        table = await reflect_table("approval_requests")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == approval_id, table.c.organization_id == tenant_id)
                    .values(resumed_job_id=job_id)
                    .returning(table)
                )
            ).mappings().first()
        if not row:
            raise ValueError("Approval request not found")
        await self._log_approval_event(approval_id, tenant_id=tenant_id, actor_id=None, event_type="resumed", note=job_id)
        return dict(row)

    async def upsert_connector_health(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import text

        from core.config import settings
        from core.db import engine

        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO connector_health (
                            organization_id, region, connector_id, status, latency_ms, failure_rate, timeout_rate,
                            failure_count, timeout_count, total_count, rate_limit_count, last_success_at, last_failure_at, updated_at
                        )
                        VALUES (
                            :tenant_id, :region, :connector_id, :status, :latency_ms, :failure_rate, :timeout_rate,
                            :failure_count, :timeout_count, :total_count, :rate_limit_count,
                            CASE WHEN :last_success THEN NOW() ELSE NULL END,
                            CASE WHEN :last_success THEN NULL ELSE NOW() END,
                            NOW()
                        )
                        ON CONFLICT (organization_id, connector_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            latency_ms = EXCLUDED.latency_ms,
                            failure_rate = EXCLUDED.failure_rate,
                            timeout_rate = EXCLUDED.timeout_rate,
                            failure_count = EXCLUDED.failure_count,
                            timeout_count = EXCLUDED.timeout_count,
                            total_count = EXCLUDED.total_count,
                            rate_limit_count = EXCLUDED.rate_limit_count,
                            last_success_at = CASE WHEN :last_success THEN NOW() ELSE connector_health.last_success_at END,
                            last_failure_at = CASE WHEN :last_success THEN connector_health.last_failure_at ELSE NOW() END,
                            updated_at = NOW()
                        RETURNING *
                        """
                    ),
                    {**values, "region": settings.region},
                )
            ).mappings().first()
        return dict(row)

    async def get_connector_health(self, *, tenant_id: str, connector_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_health")
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(table).where(table.c.organization_id == tenant_id, table.c.connector_id == connector_id))
            ).mappings().first()
        return dict(row) if row else None

    async def list_connector_health(self, *, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_health")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.organization_id == tenant_id))).mappings().all()
        return [dict(row) for row in rows]

    async def register_mcp_server(self, *, tenant_id: str, name: str, transport: str, command: str | None = None, server_url: str | None = None) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("mcp_servers")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(organization_id=tenant_id, region=settings.region, name=name, transport=transport, command=command, server_url=server_url)
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def get_mcp_server(self, server_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("mcp_servers")
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(table.c.id == server_id, table.c.organization_id == tenant_id))).mappings().first()
        return dict(row) if row else None

    async def list_mcp_servers(self, *, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("mcp_servers")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.organization_id == tenant_id).order_by(table.c.created_at.desc()))).mappings().all()
        return [dict(row) for row in rows]

    async def log_mcp_discovery(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("mcp_discovery_logs")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        server_id=values["server_id"],
                        status=values["status"],
                        message=values["message"],
                        tools_discovered=values.get("tools_discovered", 0),
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def list_mcp_discovery_logs(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("mcp_discovery_logs")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.organization_id == tenant_id).order_by(table.c.created_at.desc()).limit(limit))).mappings().all()
        return [dict(row) for row in rows]

    async def create_execution_trace(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_traces")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        connector_id=values["connector_id"],
                        action_name=values["action_name"],
                        status=values["status"],
                        graph=values.get("graph") or {},
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def finish_execution_trace(self, trace_id: str, *, status: str) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_traces")
        async with engine.begin() as conn:
            row = (
                await conn.execute(update(table).where(table.c.id == trace_id).values(status=status, completed_at=text("NOW()")).returning(table))
            ).mappings().first()
        return dict(row)

    async def create_trace_step(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_trace_steps")
        async with engine.begin() as conn:
            row = (await conn.execute(insert(table).values(**values).returning(table))).mappings().first()
        return dict(row)

    async def list_execution_traces(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_traces")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.organization_id == tenant_id).order_by(table.c.started_at.desc()).limit(limit))).mappings().all()
        return [dict(row) for row in rows]

    async def list_trace_steps(self, trace_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_trace_steps")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.trace_id == trace_id).order_by(table.c.created_at.asc()))).mappings().all()
        return [dict(row) for row in rows]

    async def create_execution_job(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_jobs")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        employee_id=values["employee_id"],
                        user_id=values.get("user_id"),
                        connector_id=values["connector_id"],
                        action_name=values["action_name"],
                        arguments_redacted=redact_arguments(values.get("arguments") or {}),
                        max_attempts=values.get("max_attempts", 1),
                        timeout_ms=values.get("timeout_ms", 15000),
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def finish_execution_job(self, job_id: str, **values: Any) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("connector_execution_jobs")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == job_id)
                    .values(
                        status=values["status"],
                        result=values.get("result"),
                        error_message=values.get("error_message"),
                        attempts=values.get("attempts", 1),
                        completed_at=text("NOW()"),
                        updated_at=text("NOW()"),
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def create_tool_execution_plan(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("tool_execution_plans")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        employee_id=values["employee_id"],
                        goal=values["goal"],
                        steps=values.get("steps") or [],
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def create_policy(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("connector_policies")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values.get("workspace_id"),
                        employee_id=values.get("employee_id"),
                        role=values.get("role"),
                        connector_id=values.get("connector_id"),
                        action_name=values.get("action_name"),
                        risk_level=values.get("risk_level"),
                        decision=values["decision"],
                        approval_mode=values.get("approval_mode") or "single",
                        conditions=values.get("conditions") or {},
                        priority=values.get("priority") or 0,
                        enabled=values.get("enabled", True),
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def list_policies(self, *, tenant_id: str, enabled: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("connector_policies")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if enabled is not None:
            stmt = stmt.where(table.c.enabled == enabled)
        stmt = stmt.order_by(table.c.priority.desc(), table.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def delete_policy(self, policy_id: str, *, tenant_id: str) -> None:
        from sqlalchemy import delete

        from core.db import engine, reflect_table

        table = await reflect_table("connector_policies")
        async with engine.begin() as conn:
            await conn.execute(delete(table).where(table.c.id == policy_id, table.c.organization_id == tenant_id))

    async def create_workflow(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflows")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workspace_id=values["workspace_id"],
                        employee_id=values["employee_id"],
                        user_id=values.get("user_id"),
                        name=values["name"],
                        description=values.get("description") or "",
                        definition=values.get("definition") or {},
                        status=values.get("status") or "pending",
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def get_workflow(self, workflow_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflows")
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(table.c.id == workflow_id, table.c.organization_id == tenant_id))).mappings().first()
        return dict(row) if row else None

    async def list_workflows(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflows")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if status:
            stmt = stmt.where(table.c.status == status)
        stmt = stmt.order_by(table.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def create_workflow_run(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert, text

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_runs")
        payload = {
            "organization_id": values["tenant_id"],
            "region": settings.region,
            "workflow_id": values["workflow_id"],
            "workspace_id": values["workspace_id"],
            "employee_id": values["employee_id"],
            "user_id": values.get("user_id"),
            "status": values.get("status") or "pending",
            "correlation_id": values["correlation_id"],
            "started_at": text("NOW()") if values.get("status") == "running" else None,
        }
        for key in ("trigger_source", "trigger_event_type", "trigger_payload"):
            if key in table.c and key in values:
                payload[key] = values.get(key)
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(**payload)
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def get_workflow_run(self, run_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_runs")
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(table.c.id == run_id, table.c.organization_id == tenant_id))).mappings().first()
        return dict(row) if row else None

    async def update_workflow_run(self, run_id: str, *, tenant_id: str, **values: Any) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_runs")
        payload = {key: value for key, value in values.items() if key in table.c}
        payload["updated_at"] = text("NOW()")
        if payload.get("status") in {"completed", "failed", "cancelled"}:
            payload["completed_at"] = text("NOW()")
        async with engine.begin() as conn:
            row = (
                await conn.execute(update(table).where(table.c.id == run_id, table.c.organization_id == tenant_id).values(**payload).returning(table))
            ).mappings().first()
        if not row:
            raise ValueError("Workflow run not found")
        return dict(row)

    async def list_workflow_runs(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_runs")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if status:
            stmt = stmt.where(table.c.status == status)
        stmt = stmt.order_by(table.c.updated_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def create_workflow_step(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_steps")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        id=f"{values['run_id']}:{values['id']}",
                        step_key=values["id"],
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workflow_id=values["workflow_id"],
                        run_id=values["run_id"],
                        tool_name=values["tool_name"],
                        arguments=values.get("arguments") or {},
                        status=values.get("status") or "queued",
                        max_attempts=values.get("max_attempts") or 1,
                        parallel_safe=values.get("parallel_safe", False),
                        condition=values.get("condition") or {},
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def list_workflow_steps(self, run_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_steps")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.run_id == run_id, table.c.organization_id == tenant_id).order_by(table.c.queued_at.asc()))).mappings().all()
        result = []
        for row in rows:
            data = dict(row)
            data["id"] = data.get("step_key") or data["id"]
            result.append(data)
        return result

    async def update_workflow_step(self, run_id: str, step_id: str, *, tenant_id: str, **values: Any) -> dict[str, Any]:
        from sqlalchemy import text, update

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_steps")
        existing = await self._workflow_step_row(run_id, step_id, tenant_id=tenant_id)
        if existing and existing.get("status") in {"success", "failed", "skipped"} and values.get("status") == "running":
            return existing
        payload = {key: value for key, value in values.items() if key in table.c}
        payload["updated_at"] = text("NOW()")
        if payload.get("status") == "running":
            payload["started_at"] = text("NOW()")
        if payload.get("status") in {"success", "failed", "skipped"}:
            payload["completed_at"] = text("NOW()")
        async with engine.begin() as conn:
            row = (
                await conn.execute(update(table).where(table.c.run_id == run_id, table.c.id == step_id, table.c.organization_id == tenant_id).values(**payload).returning(table))
            ).mappings().first()
            if not row and "step_key" in table.c:
                row = (
                    await conn.execute(update(table).where(table.c.run_id == run_id, table.c.step_key == step_id, table.c.organization_id == tenant_id).values(**payload).returning(table))
                ).mappings().first()
        if not row:
            raise ValueError("Workflow step not found")
        return dict(row)

    async def _workflow_step_row(self, run_id: str, step_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_steps")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(table).where(table.c.run_id == run_id, table.c.id == step_id, table.c.organization_id == tenant_id)
                )
            ).mappings().first()
            if not row and "step_key" in table.c:
                row = (
                    await conn.execute(
                        select(table).where(table.c.run_id == run_id, table.c.step_key == step_id, table.c.organization_id == tenant_id)
                    )
                ).mappings().first()
        return dict(row) if row else None

    async def create_workflow_dependency(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_step_dependencies")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workflow_id=values["workflow_id"],
                        run_id=values["run_id"],
                        step_id=values["step_id"],
                        depends_on_step_id=values["depends_on_step_id"],
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def list_workflow_dependencies(self, run_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_step_dependencies")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.run_id == run_id, table.c.organization_id == tenant_id))).mappings().all()
        return [dict(row) for row in rows]

    async def upsert_workflow_state(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import text

        from core.config import settings
        from core.db import engine

        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO workflow_state (organization_id, region, workflow_id, run_id, snapshot, checkpoint_version, created_at, updated_at)
                        VALUES (:tenant_id, :region, :workflow_id, :run_id, CAST(:snapshot AS jsonb), 1, NOW(), NOW())
                        ON CONFLICT (organization_id, run_id) DO UPDATE SET
                            snapshot = EXCLUDED.snapshot,
                            checkpoint_version = workflow_state.checkpoint_version + 1,
                            updated_at = NOW()
                        RETURNING *
                        """
                    ),
                    {
                        "tenant_id": values["tenant_id"],
                        "region": settings.region,
                        "workflow_id": values["workflow_id"],
                        "run_id": values["run_id"],
                        "snapshot": json.dumps(values.get("snapshot") or {}, default=str),
                    },
                )
            ).mappings().first()
        return dict(row)

    async def get_workflow_state(self, run_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_state")
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(table.c.run_id == run_id, table.c.organization_id == tenant_id))).mappings().first()
        return dict(row) if row else None

    async def create_workflow_artifact(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_artifacts")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workflow_id=values["workflow_id"],
                        run_id=values["run_id"],
                        step_id=values.get("step_id"),
                        kind=values["kind"],
                        metadata=values.get("metadata") or {},
                        content_compressed=values.get("content_compressed") or {},
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def create_workflow_trigger(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_triggers")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workflow_id=values["workflow_id"],
                        trigger_type=values["trigger_type"],
                        config=values.get("config") or {},
                        status=values.get("status") or "active",
                    )
                    .returning(table)
                )
            ).mappings().first()
        data = dict(row)
        data["source"] = (data.get("config") or {}).get("source")
        data["event_type"] = (data.get("config") or {}).get("event_type")
        return data

    async def list_workflow_triggers(self, workflow_id: str | None = None, *, tenant_id: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_triggers")
        stmt = select(table).where(table.c.organization_id == tenant_id)
        if workflow_id:
            stmt = stmt.where(table.c.workflow_id == workflow_id)
        if status:
            stmt = stmt.where(table.c.status == status)
        stmt = stmt.order_by(table.c.created_at.desc()).limit(limit)
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        result = []
        for row in rows:
            data = dict(row)
            data["source"] = (data.get("config") or {}).get("source")
            data["event_type"] = (data.get("config") or {}).get("event_type")
            result.append(data)
        return result

    async def record_workflow_run_event(self, **values: Any) -> dict[str, Any]:
        from sqlalchemy import insert

        from core.config import settings
        from core.db import engine, reflect_table

        table = await reflect_table("workflow_run_events")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=values["tenant_id"],
                        region=settings.region,
                        workflow_id=values["workflow_id"],
                        run_id=values["run_id"],
                        event_type=values["event_type"],
                        actor_id=values.get("actor_id"),
                        payload=values.get("payload") or {},
                    )
                    .returning(table)
                )
            ).mappings().first()
        return dict(row)

    async def list_workflow_run_history(self, run_id: str, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("workflow_run_events")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table)
                    .where(table.c.run_id == run_id, table.c.organization_id == tenant_id)
                    .order_by(table.c.created_at.asc())
                    .limit(limit)
                )
            ).mappings().all()
        return [dict(row) for row in rows]
