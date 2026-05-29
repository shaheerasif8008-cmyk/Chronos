"""artifact version history

Revision ID: 0017_artifact_versions
Revises: 0016_task_checkpoints
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_artifact_versions"
down_revision = "0016_task_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("artifact_key", sa.Text(), nullable=True))
    op.add_column(
        "artifacts",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE artifacts SET artifact_key = id::text WHERE artifact_key IS NULL")
    op.alter_column("artifacts", "artifact_key", nullable=False)
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
