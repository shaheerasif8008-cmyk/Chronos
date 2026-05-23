"""connector framework registry, actions, permissions, logs

Revision ID: 0008_connector_framework
Revises: 0007_context_suggestions_guard
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_connector_framework"
down_revision = "0007_context_suggestions_guard"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.add_column("connectors", sa.Column("name", sa.Text(), nullable=False, server_default="Unnamed connector"))
    op.add_column("connectors", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column("connectors", sa.Column("type", sa.Text(), nullable=False, server_default="native"))
    op.add_column("connectors", sa.Column("auth_type", sa.Text(), nullable=False, server_default="none"))
    op.add_column("connectors", sa.Column("actions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("connectors", sa.Column("mcp_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("connectors", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")))
    op.add_column("connectors", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")))

    op.create_table(
        "connector_actions",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameters_schema", postgresql.JSONB(), nullable=False),
        sa.Column("output_schema", postgresql.JSONB()),
        sa.Column("required_permissions", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("connector_id", "name", name="uq_connector_actions_connector_name"),
    )
    op.create_index("ix_connector_actions_connector", "connector_actions", ["connector_id"])

    op.create_table(
        "connector_credentials",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("employee_id", sa.Text()),
        sa.Column("user_id", sa.Text()),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("vault_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_connector_credentials_scope", "connector_credentials", ["organization_id", "workspace_id", "employee_id", "user_id", "connector_id"])

    op.create_table(
        "connector_permissions",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("allowed_scopes", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_connector_permissions_actor", "connector_permissions", ["organization_id", "workspace_id", "employee_id", "connector_id", "action_name"])

    op.create_table(
        "connector_execution_logs",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("employee_id", sa.Text()),
        sa.Column("user_id", sa.Text()),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("arguments_redacted", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_status", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_connector_execution_logs_org_created", "connector_execution_logs", ["organization_id", "created_at"])

    op.create_table(
        "connector_installations",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("installed_by", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="installed"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_connector_installations_scope", "connector_installations", ["organization_id", "workspace_id", "connector_id"])


def downgrade() -> None:
    op.drop_index("ix_connector_installations_scope", table_name="connector_installations")
    op.drop_table("connector_installations")
    op.drop_index("ix_connector_execution_logs_org_created", table_name="connector_execution_logs")
    op.drop_table("connector_execution_logs")
    op.drop_index("ix_connector_permissions_actor", table_name="connector_permissions")
    op.drop_table("connector_permissions")
    op.drop_index("ix_connector_credentials_scope", table_name="connector_credentials")
    op.drop_table("connector_credentials")
    op.drop_index("ix_connector_actions_connector", table_name="connector_actions")
    op.drop_table("connector_actions")
    for column in ["updated_at", "created_at", "mcp_config", "actions", "auth_type", "type", "description", "name"]:
        op.drop_column("connectors", column)
