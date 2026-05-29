"""scheduled_tasks table — proactive task triggers

Revision ID: 0021_scheduled_tasks
Revises: 0020_project_sources
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_scheduled_tasks"
down_revision = "0020_project_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        # schedule_kind: 'interval' (interval_seconds) or 'cron' (cron string)
        sa.Column("schedule_kind", sa.Text(), nullable=False, server_default="interval"),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron", sa.Text(), nullable=True),
        sa.Column("persona_id", sa.UUID(), nullable=True),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_task_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_tasks_due",
        "scheduled_tasks",
        ["enabled", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_due", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
