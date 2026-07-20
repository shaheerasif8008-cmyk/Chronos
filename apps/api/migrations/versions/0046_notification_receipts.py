"""isolate notification read and dismiss state per member.

Revision ID: 0046_notification_receipts
Revises: 0045_sso_invites

``notifications`` remains the shared, tenant-scoped content record. A receipt is
keyed by tenant, notification, and member, so acting on an org-wide notification
can never change another member's feed state.
"""

from alembic import op
import sqlalchemy as sa


revision = "0046_notification_receipts"
down_revision = "0045_sso_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite tenant keys let both foreign keys enforce that a receipt cannot
    # associate a notification or member from another organization.
    op.create_unique_constraint(
        "uq_notifications_org_id",
        "notifications",
        ["organization_id", "id"],
    )
    op.create_unique_constraint(
        "uq_members_org_id",
        "members",
        ["organization_id", "id"],
    )
    op.create_table(
        "notification_receipts",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("notification_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "organization_id",
            "notification_id",
            "member_id",
            name="pk_notification_receipts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "notification_id"],
            ["notifications.organization_id", "notifications.id"],
            name="fk_notification_receipts_notification_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "member_id"],
            ["members.organization_id", "members.id"],
            name="fk_notification_receipts_member_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_notification_receipts_org_member_state",
        "notification_receipts",
        ["organization_id", "member_id", "dismissed_at", "read_at"],
    )

    # Preserve all unambiguous targeted state. For legacy org-wide rows the old
    # schema did not record who acted, so applying the existing state to current
    # members is the only backward-compatible snapshot. Future actions are fully
    # isolated in the receipt table.
    op.execute(
        """
        INSERT INTO notification_receipts (
            organization_id,
            region,
            notification_id,
            member_id,
            read_at,
            dismissed_at,
            created_at,
            updated_at
        )
        SELECT
            n.organization_id,
            n.region,
            n.id,
            m.id,
            n.read_at,
            n.dismissed_at,
            COALESCE(n.read_at, n.dismissed_at, n.created_at, NOW()),
            GREATEST(
                COALESCE(n.read_at, '-infinity'::timestamptz),
                COALESCE(n.dismissed_at, '-infinity'::timestamptz),
                COALESCE(n.created_at, '-infinity'::timestamptz)
            )
        FROM notifications AS n
        JOIN members AS m
          ON m.organization_id = n.organization_id
         AND (n.member_id IS NULL OR m.id = n.member_id)
        WHERE n.read_at IS NOT NULL OR n.dismissed_at IS NOT NULL
        ON CONFLICT (organization_id, notification_id, member_id) DO NOTHING
        """
    )
    op.execute(
        "COMMENT ON TABLE notification_receipts IS "
        "'Per-member state for shared notification content'"
    )
    op.execute(
        "COMMENT ON COLUMN notifications.read_at IS "
        "'Deprecated: member state lives in notification_receipts'"
    )
    op.execute(
        "COMMENT ON COLUMN notifications.dismissed_at IS "
        "'Deprecated: member state lives in notification_receipts'"
    )


def downgrade() -> None:
    # Targeted notifications have exactly one recipient, so their latest receipt
    # can be represented faithfully in the legacy shared columns.
    op.execute(
        """
        UPDATE notifications AS n
        SET read_at = r.read_at,
            dismissed_at = r.dismissed_at
        FROM notification_receipts AS r
        WHERE n.organization_id = r.organization_id
          AND n.id = r.notification_id
          AND n.member_id = r.member_id
        """
    )
    op.execute("COMMENT ON COLUMN notifications.read_at IS NULL")
    op.execute("COMMENT ON COLUMN notifications.dismissed_at IS NULL")
    op.drop_index(
        "ix_notification_receipts_org_member_state",
        table_name="notification_receipts",
    )
    op.drop_table("notification_receipts")
    op.drop_constraint("uq_members_org_id", "members", type_="unique")
    op.drop_constraint("uq_notifications_org_id", "notifications", type_="unique")
