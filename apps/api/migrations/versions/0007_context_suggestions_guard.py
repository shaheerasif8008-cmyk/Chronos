"""ensure context suggestions table exists

Revision ID: 0007_context_suggestions_guard
Revises: 0006_settings
Create Date: 2026-05-22
"""
from alembic import op

revision = "0007_context_suggestions_guard"
down_revision = "0006_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS context_suggestions (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            organization_id TEXT NOT NULL DEFAULT 'default',
            region TEXT NOT NULL DEFAULT 'us',
            status TEXT NOT NULL DEFAULT 'pending',
            suggested_patch TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'context_update_job',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    # The original table is owned by 0002_memory; this guard migration should
    # not remove it on downgrade.
    pass
