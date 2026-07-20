"""durable outbound notification delivery receipts

Revision ID: 0056_notification_delivery
Revises: 0055_billing_accounts
"""

import sqlalchemy as sa
from alembic import op


revision = "0056_notification_delivery"
down_revision = "0055_billing_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_delivery_receipts",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("notification_id", sa.Text(), nullable=True),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("delivery_kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default="email"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "notification_id"],
            ["notifications.organization_id", "notifications.id"],
            name="fk_notification_delivery_notification_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "member_id"],
            ["members.organization_id", "members.id"],
            name="fk_notification_delivery_member_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "dedupe_key",
            name="uq_notification_delivery_org_dedupe",
        ),
        sa.CheckConstraint(
            "delivery_kind IN ('notification', 'weekly_digest')",
            name="ck_notification_delivery_kind",
        ),
        sa.CheckConstraint(
            "channel = 'email'",
            name="ck_notification_delivery_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'delivered', 'dead_letter')",
            name="ck_notification_delivery_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_notification_delivery_attempts",
        ),
    )
    op.create_index(
        "ix_notification_delivery_claim",
        "notification_delivery_receipts",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_notification_delivery_org_notification",
        "notification_delivery_receipts",
        ["organization_id", "notification_id", "status"],
    )
    op.create_index(
        "ix_notification_delivery_org_kind_claim",
        "notification_delivery_receipts",
        [
            "organization_id",
            "delivery_kind",
            "status",
            "next_attempt_at",
            "created_at",
        ],
    )
    op.create_index(
        "ix_notification_delivery_org_member_kind",
        "notification_delivery_receipts",
        ["organization_id", "member_id", "delivery_kind", "created_at"],
    )
    op.create_index(
        "ix_notifications_pending_email",
        "notifications",
        ["organization_id", "created_at"],
        postgresql_where=sa.text("emailed_at IS NULL"),
    )
    op.execute(
        "COMMENT ON TABLE notification_delivery_receipts IS "
        "'Per-recipient email attempts, claims, retries, and terminal outcomes'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_pending_email")
    op.drop_index(
        "ix_notification_delivery_org_member_kind",
        table_name="notification_delivery_receipts",
    )
    op.execute("DROP INDEX IF EXISTS ix_notification_delivery_org_kind_claim")
    op.drop_index(
        "ix_notification_delivery_org_notification",
        table_name="notification_delivery_receipts",
    )
    op.drop_index(
        "ix_notification_delivery_claim",
        table_name="notification_delivery_receipts",
    )
    op.drop_table("notification_delivery_receipts")
