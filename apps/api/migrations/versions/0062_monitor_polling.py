"""Durable production monitor polling.

Revision ID: 0062_monitor_polling
Revises: 0061_admin_lifecycle
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0062_monitor_polling"
down_revision = "0061_admin_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "monitors",
        sa.Column("source_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("monitors", sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="900"))
    op.add_column("monitors", sa.Column("next_run_at", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("last_run_at", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("last_success_at", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("last_failure_at", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("last_run_status", sa.Text()))
    op.add_column("monitors", sa.Column("last_error_code", sa.Text()))
    op.add_column("monitors", sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("monitors", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"))
    op.add_column("monitors", sa.Column("backoff_until", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("content_hash", sa.Text()))
    op.add_column("monitors", sa.Column("last_etag", sa.Text()))
    op.add_column("monitors", sa.Column("last_modified", sa.Text()))
    op.add_column("monitors", sa.Column("lease_token", sa.Text()))
    op.add_column("monitors", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("monitors", sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("monitors", sa.Column("alert_cooldown_seconds", sa.Integer(), nullable=False, server_default="300"))
    op.execute("UPDATE monitors SET next_run_at = NOW() WHERE status = 'active' AND next_run_at IS NULL")
    op.create_index(
        "ix_monitors_due_polling",
        "monitors",
        ["status", "next_run_at", "backoff_until", "lease_expires_at"],
    )

    op.create_table(
        "monitor_runs",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("monitor_id", sa.UUID(), nullable=False),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("trigger_source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("observation", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_hash", sa.Text()),
        sa.Column("alert_id", sa.UUID()),
        sa.Column("workflow_run_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("monitor_id", "run_key", name="uq_monitor_runs_monitor_key"),
    )
    op.create_index(
        "ix_monitor_runs_scope",
        "monitor_runs",
        ["organization_id", "monitor_id", "created_at"],
    )
    op.create_index(
        "ix_monitor_runs_retry",
        "monitor_runs",
        ["status", "next_attempt_at"],
    )

    op.add_column("monitor_alerts", sa.Column("run_id", sa.UUID()))
    op.add_column("monitor_alerts", sa.Column("dedupe_key", sa.Text()))
    op.create_index(
        "uq_monitor_alerts_dedupe",
        "monitor_alerts",
        ["organization_id", "monitor_id", "dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_monitor_alerts_dedupe", table_name="monitor_alerts")
    op.drop_column("monitor_alerts", "dedupe_key")
    op.drop_column("monitor_alerts", "run_id")
    op.drop_index("ix_monitor_runs_retry", table_name="monitor_runs")
    op.drop_index("ix_monitor_runs_scope", table_name="monitor_runs")
    op.drop_table("monitor_runs")
    op.drop_index("ix_monitors_due_polling", table_name="monitors")
    for column in (
        "alert_cooldown_seconds",
        "alert_count",
        "lease_expires_at",
        "lease_token",
        "last_modified",
        "last_etag",
        "content_hash",
        "backoff_until",
        "max_attempts",
        "consecutive_failures",
        "last_error_code",
        "last_run_status",
        "last_failure_at",
        "last_success_at",
        "last_run_at",
        "next_run_at",
        "interval_seconds",
        "source_config",
    ):
        op.drop_column("monitors", column)
