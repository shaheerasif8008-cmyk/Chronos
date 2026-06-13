"""desktop GUI operator sessions

Revision ID: 0033_desktop_sessions
Revises: 0032_skills_persistence
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0033_desktop_sessions"
down_revision = "0032_skills_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "desktop_sessions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("consent", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("display", sa.Text(), nullable=True),
        sa.Column("screen", sa.Text(), nullable=True),
        sa.Column("degraded_reason", sa.Text(), nullable=True),
        sa.Column("screenshot_object_path", sa.Text(), nullable=True),
        sa.Column("history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_desktop_sessions_org_status", "desktop_sessions", ["organization_id", "status"])
    op.create_index("ix_desktop_sessions_org_task", "desktop_sessions", ["organization_id", "task_id"])

    op.create_table(
        "desktop_session_events",
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
    op.create_index(
        "ix_desktop_session_events_session_seq",
        "desktop_session_events",
        ["organization_id", "session_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_session_events_session_seq", table_name="desktop_session_events")
    op.drop_table("desktop_session_events")
    op.drop_index("ix_desktop_sessions_org_task", table_name="desktop_sessions")
    op.drop_index("ix_desktop_sessions_org_status", table_name="desktop_sessions")
    op.drop_table("desktop_sessions")
