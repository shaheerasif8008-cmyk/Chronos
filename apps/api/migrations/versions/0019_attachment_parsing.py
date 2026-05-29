"""attachment parsing: parent link + parse status on artifacts

Revision ID: 0019_attachment_parsing
Revises: 0018_projects
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_attachment_parsing"
down_revision = "0018_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("parent_artifact_id", sa.UUID(), nullable=True))
    op.add_column("artifacts", sa.Column("parse_status", sa.Text(), nullable=True))
    op.create_index("ix_artifacts_parent", "artifacts", ["parent_artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_parent", "artifacts")
    op.drop_column("artifacts", "parse_status")
    op.drop_column("artifacts", "parent_artifact_id")
