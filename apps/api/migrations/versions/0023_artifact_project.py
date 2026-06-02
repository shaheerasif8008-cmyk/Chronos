"""artifact project linkage: direct project_id for move-to-project

Revision ID: 0023_artifact_project
Revises: 0022_artifact_workspace
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_artifact_project"
down_revision = "0022_artifact_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Direct artifact -> project link so artifacts can be MOVED into a project
    # (Phase 5 `move`). Prior linkage was indirect via conversation/task; this
    # column lets an artifact belong to a project explicitly and be re-homed.
    op.add_column("artifacts", sa.Column("project_id", sa.UUID(), nullable=True))
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_project_id", "artifacts")
    op.drop_column("artifacts", "project_id")
