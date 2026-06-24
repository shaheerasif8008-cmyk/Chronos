"""notifications: durable in-app notification feed (W5.2).

Tenant-scoped. ``member_id`` NULL means an org-wide notification visible to every
member of the org (e.g. an approval awaiting any admin); a set ``member_id``
targets one recipient. ``emailed_at`` supports W5.3 delivery tracking.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_notifications"
down_revision = "0042_usage_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False, server_default="info"),
        sa.Column("resource_type", sa.Text(), nullable=True),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_notifications_org_created",
        "notifications",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_org_member",
        "notifications",
        ["organization_id", "member_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_org_member", table_name="notifications")
    op.drop_index("ix_notifications_org_created", table_name="notifications")
    op.drop_table("notifications")
