"""artifact + attachment merge (applied directly to DB, stub for alembic)

Revision ID: 0018_artifact_attachment_merge
Revises: 0017_attachment_parsing
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_artifact_attachment_merge"
down_revision = "0017_attachment_parsing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
