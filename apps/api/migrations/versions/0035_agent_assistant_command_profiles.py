"""agent assistant command profiles

Revision ID: 0035_agent_cmd_profiles
Revises: 0034_invitations
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0035_agent_cmd_profiles"
down_revision = "0034_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_profiles", sa.Column("profile_kind", sa.Text(), nullable=False, server_default="agent"))
    op.add_column("agent_profiles", sa.Column("personality", sa.Text(), nullable=False, server_default=""))
    op.add_column("agent_profiles", sa.Column("workflows", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("agent_profiles", sa.Column("connected_accounts", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.create_index("ix_agent_profiles_org_kind_status", "agent_profiles", ["organization_id", "profile_kind", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_profiles_org_kind_status", table_name="agent_profiles")
    op.drop_column("agent_profiles", "connected_accounts")
    op.drop_column("agent_profiles", "workflows")
    op.drop_column("agent_profiles", "personality")
    op.drop_column("agent_profiles", "profile_kind")
