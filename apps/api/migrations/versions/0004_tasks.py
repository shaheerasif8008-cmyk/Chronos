"""tasks, approvals, and autonomous runtime state

Revision ID: 0004_tasks
Revises: 0003_connectors
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_tasks"
down_revision = "0003_connectors"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("parent_task_id", sa.Text()),
        sa.Column("persona_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.Column("triggered_by_member_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text()),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_estimate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], name="fk_tasks_parent_task_id"),
    )
    op.create_index("ix_tasks_org_status", "tasks", ["organization_id", "status"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("action_payload", postgresql.JSONB(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_approvals_task_id"),
    )
    op.create_index("ix_approvals_org_status", "approvals", ["organization_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_approvals_org_status", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_index("ix_tasks_org_status", table_name="tasks")
    op.drop_table("tasks")
