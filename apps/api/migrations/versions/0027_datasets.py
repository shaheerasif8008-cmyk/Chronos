"""datasets table for data analysis workspace

Revision ID: 0027_datasets
Revises: 0026_research_runs
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_datasets"
down_revision = "0026_research_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("source_artifact_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("schema", JSONB(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_datasets_org_created",
        "datasets",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_datasets_org_created", table_name="datasets")
    op.drop_table("datasets")
