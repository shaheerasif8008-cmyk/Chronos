"""merge 0019_artifact_workspace and 0022_artifact_workspace heads

Revision ID: 0023_merge_heads
Revises: 0019_artifact_workspace, 0022_artifact_workspace
Create Date: 2026-06-01
"""
from alembic import op

revision = "0023_merge_heads"
down_revision = ("0019_artifact_workspace", "0022_artifact_workspace")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
