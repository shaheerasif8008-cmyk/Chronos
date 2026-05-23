"""connector operations infrastructure

Revision ID: 0009_connector_operations
Revises: 0008_connector_framework
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_connector_operations"
down_revision = "0008_connector_framework"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("arguments_redacted", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("justification", sa.Text(), nullable=False, server_default=""),
        sa.Column("approval_mode", sa.Text(), nullable=False, server_default="single"),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("approval_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approval_requests_scope_status", "approval_requests", ["organization_id", "workspace_id", "status"])

    op.create_table(
        "approval_events",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("approval_request_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_approval_events_request", "approval_events", ["approval_request_id", "created_at"])

    op.create_table(
        "connector_policies",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("employee_id", sa.Text()),
        sa.Column("role", sa.Text()),
        sa.Column("connector_id", sa.Text()),
        sa.Column("action_name", sa.Text()),
        sa.Column("risk_level", sa.Text()),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("approval_mode", sa.Text(), nullable=False, server_default="single"),
        sa.Column("conditions", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    op.create_table(
        "connector_execution_jobs",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("arguments_redacted", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="15000"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_connector_execution_jobs_status", "connector_execution_jobs", ["organization_id", "status", "created_at"])

    op.create_table(
        "connector_health",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="healthy"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("timeout_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "connector_id", name="uq_connector_health_org_connector"),
    )

    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("command", sa.Text()),
        sa.Column("server_url", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="available"),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    op.create_table(
        "mcp_discovery_logs",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("tools_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    op.create_table(
        "connector_execution_traces",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("graph", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "connector_execution_trace_steps",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("step_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_redacted", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    op.create_table(
        "tool_execution_plans",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("tool_execution_plans")
    op.drop_table("connector_execution_trace_steps")
    op.drop_table("connector_execution_traces")
    op.drop_table("mcp_discovery_logs")
    op.drop_table("mcp_servers")
    op.drop_table("connector_health")
    op.drop_index("ix_connector_execution_jobs_status", table_name="connector_execution_jobs")
    op.drop_table("connector_execution_jobs")
    op.drop_table("connector_policies")
    op.drop_index("ix_approval_events_request", table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_index("ix_approval_requests_scope_status", table_name="approval_requests")
    op.drop_table("approval_requests")
