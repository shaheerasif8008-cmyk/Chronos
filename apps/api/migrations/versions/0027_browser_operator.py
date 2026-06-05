"""browser operator sessions and events

Revision ID: 0027_browser_operator
Revises: 0026_research_runs
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_browser_operator"
down_revision = "0026_research_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("current_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("screenshot_object_path", sa.Text(), nullable=True),
        sa.Column("screenshot_data_url", sa.Text(), nullable=True),
        sa.Column("cookies_ref", sa.Text(), nullable=True),
        sa.Column("storage_state", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("takeover_state", sa.Text(), nullable=False, server_default="none"),
        sa.Column("takeover_reason", sa.Text(), nullable=True),
        sa.Column("takeover_summary", sa.Text(), nullable=True),
        sa.Column("consent", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sensitive_site_approvals", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("downloads", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_browser_sessions_org_status", "browser_sessions", ["organization_id", "status"])
    op.create_index("ix_browser_sessions_org_task", "browser_sessions", ["organization_id", "task_id"])
    op.create_index("ix_browser_sessions_org_updated", "browser_sessions", ["organization_id", "updated_at"])

    op.create_table(
        "browser_session_events",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("screenshot_ref", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_browser_session_events_session_seq",
        "browser_session_events",
        ["organization_id", "session_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_session_events_session_seq", table_name="browser_session_events")
    op.drop_table("browser_session_events")
    op.drop_index("ix_browser_sessions_org_updated", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_org_task", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_org_status", table_name="browser_sessions")
    op.drop_table("browser_sessions")
