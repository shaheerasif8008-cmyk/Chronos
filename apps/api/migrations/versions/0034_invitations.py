"""member invitations

Revision ID: 0034_invitations
Revises: 0033_desktop_sessions
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa


revision = "0034_invitations"
down_revision = "0033_desktop_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("invited_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_invitations_token"),
    )
    op.create_index("ix_invitations_org_email_status", "invitations", ["organization_id", "email", "status"])


def downgrade() -> None:
    op.drop_index("ix_invitations_org_email_status", table_name="invitations")
    op.drop_table("invitations")
