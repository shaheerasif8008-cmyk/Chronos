"""move browser operator state to encrypted remote contexts

Revision ID: 0057_browser_operator_remote
Revises: 0056_notification_delivery

Legacy Playwright storage_state contained plaintext cookies and local-storage
tokens. Remote Browserbase Contexts encrypt that data at rest, so this migration
purges the old payloads and stores only non-secret provider identifiers needed
to reconnect from another API replica.
"""

import sqlalchemy as sa
from alembic import op


revision = "0057_browser_operator_remote"
down_revision = "0056_notification_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "browser_sessions",
        sa.Column("runtime_provider", sa.Text(), nullable=True),
    )
    op.add_column(
        "browser_sessions",
        sa.Column("remote_session_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "browser_sessions",
        sa.Column("remote_context_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_browser_sessions_remote_session",
        "browser_sessions",
        ["remote_session_id"],
        unique=True,
        postgresql_where=sa.text("remote_session_id IS NOT NULL"),
    )

    # Do not retain credentials from the former process-local implementation.
    # Existing live sessions cannot be safely rehydrated without those cookies,
    # so close them explicitly instead of presenting a false active state.
    op.execute(
        sa.text(
            """
            UPDATE browser_sessions
               SET storage_state = '{}'::jsonb,
                   cookies_ref = NULL,
                   screenshot_data_url = NULL,
                   runtime_provider = COALESCE(runtime_provider, 'legacy-local'),
                   status = CASE
                       WHEN status IN ('active', 'degraded') THEN 'closed'
                       ELSE status
                   END,
                   takeover_state = CASE
                       WHEN status IN ('active', 'degraded') THEN 'none'
                       ELSE takeover_state
                   END,
                   updated_at = NOW()
            """
        )
    )


def downgrade() -> None:
    # Purged credential material is intentionally unrecoverable.
    op.drop_index("ix_browser_sessions_remote_session", table_name="browser_sessions")
    op.drop_column("browser_sessions", "remote_context_id")
    op.drop_column("browser_sessions", "remote_session_id")
    op.drop_column("browser_sessions", "runtime_provider")
