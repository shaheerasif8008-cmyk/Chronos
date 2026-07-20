"""durable task cancellation cleanup

Revision ID: 0060_task_cleanup
Revises: 0059_custom_integrations
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0060_task_cleanup"
down_revision = "0059_custom_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_cleanup_requests",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("task_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default="user_cancelled"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "task_id", name="uq_task_cleanup_org_task"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry', 'complete')",
            name="ck_task_cleanup_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_task_cleanup_attempts"),
    )
    op.create_index(
        "ix_task_cleanup_due",
        "task_cleanup_requests",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_task_cleanup_org_created",
        "task_cleanup_requests",
        ["organization_id", "created_at"],
    )

    op.add_column("connector_execution_jobs", sa.Column("task_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_connector_jobs_org_task_status",
        "connector_execution_jobs",
        ["organization_id", "task_id", "status"],
    )

    op.add_column("approval_requests", sa.Column("task_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_connector_approvals_org_task_status",
        "approval_requests",
        ["organization_id", "task_id", "status"],
    )

    op.add_column("desktop_commands", sa.Column("task_id", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE desktop_commands AS dc
        SET task_id = lcg.task_id
        FROM local_computer_grants AS lcg
        WHERE dc.grant_id = lcg.id
          AND dc.organization_id = lcg.organization_id
          AND lcg.task_id IS NOT NULL
        """
    )
    op.create_index(
        "ix_desktop_commands_org_task_status",
        "desktop_commands",
        ["organization_id", "task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_commands_org_task_status", table_name="desktop_commands")
    op.drop_column("desktop_commands", "task_id")
    op.drop_index("ix_connector_approvals_org_task_status", table_name="approval_requests")
    op.drop_column("approval_requests", "task_id")
    op.drop_index("ix_connector_jobs_org_task_status", table_name="connector_execution_jobs")
    op.drop_column("connector_execution_jobs", "task_id")
    op.drop_index("ix_task_cleanup_org_created", table_name="task_cleanup_requests")
    op.drop_index("ix_task_cleanup_due", table_name="task_cleanup_requests")
    op.drop_table("task_cleanup_requests")
