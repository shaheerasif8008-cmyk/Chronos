"""agents personas and workspace publishing

Revision ID: 0029_agents_phase13
Revises: 0028_computer_phase10
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0029_agents_phase13"
down_revision = "0028_computer_phase10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("template_id", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tool_grants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("connector_grants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("project_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("memory_scopes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("autonomy_level", sa.Text(), nullable=False, server_default="supervised"),
        sa.Column("approval_policy", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schedule_permissions", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_profiles_org_status", "agent_profiles", ["organization_id", "status"])

    op.create_table(
        "agent_publications",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("external_channel_id", sa.Text(), nullable=True),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("approval_policy", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("inbound_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_publications_org_agent", "agent_publications", ["organization_id", "agent_profile_id"])
    op.create_index("ix_agent_publications_org_target", "agent_publications", ["organization_id", "target"])

    op.create_table(
        "agent_profile_events",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("publication_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_profile_events_agent_created", "agent_profile_events", ["organization_id", "agent_profile_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_profile_events_agent_created", table_name="agent_profile_events")
    op.drop_table("agent_profile_events")
    op.drop_index("ix_agent_publications_org_target", table_name="agent_publications")
    op.drop_index("ix_agent_publications_org_agent", table_name="agent_publications")
    op.drop_table("agent_publications")
    op.drop_index("ix_agent_profiles_org_status", table_name="agent_profiles")
    op.drop_table("agent_profiles")
