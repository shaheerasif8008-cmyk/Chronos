"""cloud and local computer sessions

Revision ID: 0028_computer_phase10
Revises: 521e52e86de7
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0028_computer_phase10"
down_revision = "521e52e86de7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "computer_sessions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("browser_session_id", sa.Text(), nullable=True),
        sa.Column("editor_state", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("network_policy", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("resource_limits", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("environment", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_computer_sessions_org_status", "computer_sessions", ["organization_id", "status"])
    op.create_index("ix_computer_sessions_org_task", "computer_sessions", ["organization_id", "task_id"])

    op.create_table(
        "computer_session_events",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_computer_session_events_session_seq", "computer_session_events", ["organization_id", "session_id", "seq"])

    op.create_table(
        "local_computer_grants",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("folder_path", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("allowed_commands", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("allowed_apps", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_local_computer_grants_org_status", "local_computer_grants", ["organization_id", "status"])
    op.create_index("ix_local_computer_grants_org_task", "local_computer_grants", ["organization_id", "task_id"])

    op.create_table(
        "local_computer_events",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("grant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_local_computer_events_grant_seq", "local_computer_events", ["organization_id", "grant_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_local_computer_events_grant_seq", table_name="local_computer_events")
    op.drop_table("local_computer_events")
    op.drop_index("ix_local_computer_grants_org_task", table_name="local_computer_grants")
    op.drop_index("ix_local_computer_grants_org_status", table_name="local_computer_grants")
    op.drop_table("local_computer_grants")
    op.drop_index("ix_computer_session_events_session_seq", table_name="computer_session_events")
    op.drop_table("computer_session_events")
    op.drop_index("ix_computer_sessions_org_task", table_name="computer_sessions")
    op.drop_index("ix_computer_sessions_org_status", table_name="computer_sessions")
    op.drop_table("computer_sessions")
