"""merge artifact versioning and attachment parsing heads

Revision ID: 0018_artifact_attachment_merge
Revises: 0017_artifact_versions, 0017_attachment_parsing
Create Date: 2026-05-28
"""

revision = "0018_artifact_attachment_merge"
down_revision = ("0017_artifact_versions", "0017_attachment_parsing")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
