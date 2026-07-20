"""make connector credential ownership explicit.

Revision ID: 0047_connector_member_scope
Revises: 0046_notification_receipts
"""

from alembic import op
import sqlalchemy as sa


revision = "0047_connector_member_scope"
down_revision = "0046_notification_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column("member_id", sa.Text(), nullable=True),
    )
    # Historical per-member OAuth rows use provider:org:member IDs. Explicit
    # org-scoped and registry rows intentionally remain NULL and are shared.
    op.execute(
        """
        UPDATE connectors
        SET member_id = substring(
            id FROM char_length(provider || ':' || organization_id || ':') + 1
        )
        WHERE id LIKE provider || ':' || organization_id || ':%'
        """
    )
    op.create_foreign_key(
        "fk_connectors_member_id",
        "connectors",
        "members",
        ["member_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_connectors_org_member_provider_status",
        "connectors",
        ["organization_id", "member_id", "provider", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connectors_org_member_provider_status", table_name="connectors"
    )
    op.drop_constraint("fk_connectors_member_id", "connectors", type_="foreignkey")
    op.drop_column("connectors", "member_id")
