"""tenant-scoped custom HTTP connectors and signed inbound webhooks

Revision ID: 0059_custom_integrations
Revises: 0058_repo_workspaces

Credential material and webhook payloads are stored only in the encrypted
credential vault.  These tables contain tenant-scoped configuration, bounded
delivery metadata, and idempotency evidence.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0059_custom_integrations"
down_revision = "0058_repo_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_http_connectors",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("last_health_status", sa.Text()),
        sa.Column("last_health_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_custom_http_connectors_status"),
        sa.UniqueConstraint("organization_id", "connector_id", name="uq_custom_http_org_connector"),
    )
    op.create_index(
        "ix_custom_http_connectors_org_status",
        "custom_http_connectors",
        ["organization_id", "status", "created_at"],
    )

    op.create_table(
        "custom_http_actions",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("custom_http_connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("request_schema", JSONB(), nullable=False, server_default=sa.text("'{\"type\": \"object\"}'::jsonb")),
        sa.Column("response_schema", JSONB()),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(
            ["custom_http_connector_id"],
            ["custom_http_connectors.id"],
            name="fk_custom_http_actions_connector",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')", name="ck_custom_http_actions_method"),
        sa.CheckConstraint("risk_level IN ('read', 'write', 'destructive')", name="ck_custom_http_actions_risk"),
        sa.UniqueConstraint(
            "organization_id",
            "custom_http_connector_id",
            "action_name",
            name="uq_custom_http_action_name",
        ),
    )
    op.create_index(
        "ix_custom_http_actions_org_connector",
        "custom_http_actions",
        ["organization_id", "custom_http_connector_id"],
    )

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("public_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("secret_vault_ref", sa.Text(), nullable=False),
        sa.Column("secret_fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_webhook_endpoints_status"),
        sa.CheckConstraint("rate_limit_per_minute BETWEEN 1 AND 600", name="ck_webhook_endpoints_rate"),
        sa.UniqueConstraint("public_id", name="uq_webhook_endpoints_public_id"),
    )
    op.create_index(
        "ix_webhook_endpoints_org_status",
        "webhook_endpoints",
        ["organization_id", "status", "created_at"],
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("endpoint_id", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("payload_bytes", sa.Integer(), nullable=False),
        sa.Column("payload_vault_ref", sa.Text(), nullable=False),
        sa.Column("untrusted_scan", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="received"),
        sa.Column("workflow_run_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            name="fk_webhook_events_endpoint",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('received', 'processing', 'processed', 'failed')", name="ck_webhook_events_status"),
        sa.UniqueConstraint("endpoint_id", "external_event_id", name="uq_webhook_event_external_id"),
    )
    op.create_index(
        "ix_webhook_events_org_received",
        "webhook_events",
        ["organization_id", "received_at"],
    )

    op.add_column("workflow_runs", sa.Column("trigger_idempotency_key", sa.Text()))
    op.create_index(
        "uq_workflow_runs_trigger_idempotency",
        "workflow_runs",
        ["organization_id", "trigger_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("trigger_idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_workflow_runs_trigger_idempotency", table_name="workflow_runs")
    op.drop_column("workflow_runs", "trigger_idempotency_key")
    op.drop_index("ix_webhook_events_org_received", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_webhook_endpoints_org_status", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
    op.drop_index("ix_custom_http_actions_org_connector", table_name="custom_http_actions")
    op.drop_table("custom_http_actions")
    op.drop_index("ix_custom_http_connectors_org_status", table_name="custom_http_connectors")
    op.drop_table("custom_http_connectors")
