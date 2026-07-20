"""SSO replay protection and truthful invitation delivery metadata.

Revision ID: 0045_sso_invites
Revises: 0044_comments
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0045_sso_invites"
down_revision = "0044_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "invitations",
        sa.Column(
            "delivery_channel",
            sa.Text(),
            nullable=False,
            server_default="manual_link",
        ),
    )
    op.add_column("invitations", sa.Column("delivery_error", sa.Text(), nullable=True))
    op.add_column(
        "invitations",
        sa.Column("last_delivery_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "invitations",
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invitations", "sent_at")
    op.drop_column("invitations", "last_delivery_attempt_at")
    op.drop_column("invitations", "delivery_error")
    op.drop_column("invitations", "delivery_channel")
    op.drop_column("invitations", "delivery_status")
