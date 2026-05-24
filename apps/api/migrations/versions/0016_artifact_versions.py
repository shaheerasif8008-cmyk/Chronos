"""artifact version history

Adds a stable logical key + current-version flag so artifacts can be versioned:
re-writing the same key (within a conversation/task scope) supersedes the prior
current row and bumps the version, instead of creating a duplicate artifact.

Revision ID: 0016_artifact_versions
Revises: 0015_artifacts
Create Date: 2026-05-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_artifact_versions"
down_revision = "0015_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("artifact_key", sa.Text(), nullable=True))
    op.add_column(
        "artifacts",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Backfill: existing artifacts are each their own latest version, self-keyed.
    op.execute("UPDATE artifacts SET artifact_key = id::text WHERE artifact_key IS NULL")
    op.alter_column("artifacts", "artifact_key", nullable=False)

    # At most one current row per (org, conversation, key) — lets concurrent
    # writers serialize on the constraint instead of both inserting version 1.
    op.execute(
        "CREATE UNIQUE INDEX ix_artifacts_current_key "
        "ON artifacts (organization_id, conversation_id, artifact_key) "
        "WHERE is_current AND conversation_id IS NOT NULL"
    )
    op.create_index("ix_artifacts_key", "artifacts", ["organization_id", "artifact_key"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_key", "artifacts")
    op.execute("DROP INDEX IF EXISTS ix_artifacts_current_key")
    op.drop_column("artifacts", "is_current")
    op.drop_column("artifacts", "artifact_key")
