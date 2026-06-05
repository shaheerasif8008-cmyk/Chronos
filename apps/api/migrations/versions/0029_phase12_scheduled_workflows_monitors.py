"""phase 12 scheduled workflows monitors

Revision ID: 0029_phase12_scheduled_workflows_monitors
Revises: 0028_computer_phase10
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_phase12_scheduled_workflows_monitors"
down_revision = "0028_computer_phase10"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("status", sa.Text(), nullable=False, server_default="active"))
    op.add_column("scheduled_tasks", sa.Column("run_at", sa.DateTime(timezone=True)))
    op.add_column("scheduled_tasks", sa.Column("time_of_day", sa.Text()))
    op.add_column("scheduled_tasks", sa.Column("day_of_week", sa.Text()))
    op.add_column("scheduled_tasks", sa.Column("day_of_month", sa.Integer()))
    op.add_column("scheduled_tasks", sa.Column("trigger_source", sa.Text()))
    op.add_column("scheduled_tasks", sa.Column("trigger_event_type", sa.Text()))

    op.create_table(
        "scheduled_task_runs",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        *tenant_columns(),
        sa.Column("schedule_id", sa.UUID()),
        sa.Column("task_id", sa.UUID()),
        sa.Column("workflow_run_id", sa.Text()),
        sa.Column("monitor_id", sa.UUID()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger_source", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_task_runs_scope", "scheduled_task_runs", ["organization_id", "schedule_id", "created_at"])

    op.add_column("workflow_runs", sa.Column("trigger_source", sa.Text(), nullable=False, server_default="manual"))
    op.add_column("workflow_runs", sa.Column("trigger_event_type", sa.Text()))
    op.add_column("workflow_runs", sa.Column("trigger_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))

    op.create_table(
        "workflow_run_events",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_workflow_run_events_run", "workflow_run_events", ["organization_id", "run_id", "created_at"])

    op.create_table(
        "monitors",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        *tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("monitor_type", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("condition", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schedule_id", sa.UUID()),
        sa.Column("workflow_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_monitors_scope", "monitors", ["organization_id", "status", "monitor_type"])

    op.create_table(
        "monitor_alerts",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        *tenant_columns(),
        sa.Column("monitor_id", sa.UUID(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="info"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_monitor_alerts_scope", "monitor_alerts", ["organization_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_monitor_alerts_scope", table_name="monitor_alerts")
    op.drop_table("monitor_alerts")
    op.drop_index("ix_monitors_scope", table_name="monitors")
    op.drop_table("monitors")
    op.drop_index("ix_workflow_run_events_run", table_name="workflow_run_events")
    op.drop_table("workflow_run_events")
    op.drop_column("workflow_runs", "trigger_payload")
    op.drop_column("workflow_runs", "trigger_event_type")
    op.drop_column("workflow_runs", "trigger_source")
    op.drop_index("ix_scheduled_task_runs_scope", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_column("scheduled_tasks", "trigger_event_type")
    op.drop_column("scheduled_tasks", "trigger_source")
    op.drop_column("scheduled_tasks", "day_of_month")
    op.drop_column("scheduled_tasks", "day_of_week")
    op.drop_column("scheduled_tasks", "time_of_day")
    op.drop_column("scheduled_tasks", "run_at")
    op.drop_column("scheduled_tasks", "status")
