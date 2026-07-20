"""Durable connector mutation ledger and outbox.

Revision ID: 0063_connector_write_ledger
Revises: 0062_monitor_polling
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0063_connector_write_ledger"
down_revision = "0062_monitor_polling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_write_operations",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("connector_job_id", sa.Text()),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("approval_binding", sa.Text(), nullable=False),
        sa.Column("idempotency_sha256", sa.Text(), nullable=False),
        sa.Column("provider_idempotency_key", sa.Text(), nullable=False),
        sa.Column("provider_supports_idempotency", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("supports_reconciliation", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_owner", sa.Text()),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("encrypted_payload", sa.Text()),
        sa.Column("provider_evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("provider_responded_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW() + INTERVAL '30 days'")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','claimed','provider_confirmed','complete','retry','manual_review','failed','cancelled')",
            name="ck_connector_write_operations_status",
        ),
        sa.CheckConstraint("channel IN ('broker','framework')", name="ck_connector_write_operations_channel"),
        sa.UniqueConstraint(
            "organization_id",
            "member_id",
            "tool",
            "idempotency_sha256",
            name="uq_connector_write_operation_identity",
        ),
    )
    op.create_index(
        "ix_connector_write_operations_due",
        "connector_write_operations",
        ["status", "next_attempt_at", "claim_expires_at", "last_enqueued_at"],
    )
    op.create_index(
        "ix_connector_write_operations_scope",
        "connector_write_operations",
        ["organization_id", "member_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_connector_write_operations_job",
        "connector_write_operations",
        ["connector_job_id"],
        unique=True,
        postgresql_where=sa.text("connector_job_id IS NOT NULL"),
    )
    op.create_index(
        "ix_connector_write_operations_expiry",
        "connector_write_operations",
        ["expires_at"],
    )

    op.add_column("connector_execution_jobs", sa.Column("write_operation_id", sa.UUID()))
    op.add_column("connector_execution_jobs", sa.Column("approval_id", sa.Text()))
    op.create_index(
        "ix_connector_execution_jobs_write_operation",
        "connector_execution_jobs",
        ["write_operation_id"],
        unique=True,
        postgresql_where=sa.text("write_operation_id IS NOT NULL"),
    )

    op.add_column("custom_http_actions", sa.Column("idempotency_header", sa.Text()))


def downgrade() -> None:
    op.drop_column("custom_http_actions", "idempotency_header")
    op.drop_index("ix_connector_execution_jobs_write_operation", table_name="connector_execution_jobs")
    op.drop_column("connector_execution_jobs", "approval_id")
    op.drop_column("connector_execution_jobs", "write_operation_id")
    op.drop_index("ix_connector_write_operations_expiry", table_name="connector_write_operations")
    op.drop_index("ix_connector_write_operations_job", table_name="connector_write_operations")
    op.drop_index("ix_connector_write_operations_scope", table_name="connector_write_operations")
    op.drop_index("ix_connector_write_operations_due", table_name="connector_write_operations")
    op.drop_table("connector_write_operations")
