"""scope datasets to projects

Revision ID: 0054_dataset_project_scope
Revises: 0053_source_feed_parent
"""

import sqlalchemy as sa
from alembic import op


revision = "0054_dataset_project_scope"
down_revision = "0053_source_feed_parent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("project_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_datasets_project_id",
        "datasets",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_datasets_org_project_created",
        "datasets",
        ["organization_id", "project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_datasets_org_project_created", table_name="datasets")
    op.drop_constraint("fk_datasets_project_id", "datasets", type_="foreignkey")
    op.drop_column("datasets", "project_id")
