"""project_sources and project_source_chunks tables

Revision ID: 0020_project_sources
Revises: 0019_attachment_parsing
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020_project_sources"
down_revision = "0019_attachment_parsing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_sources",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("parse_status", sa.Text(), nullable=True, server_default="pending"),
        sa.Column("index_status", sa.Text(), nullable=True, server_default="pending"),
        sa.Column("connector_id", sa.UUID(), nullable=True),
        sa.Column(
            "permissions",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_project_sources_project_id",
        ),
    )
    op.create_index(
        "ix_project_sources_org_project",
        "project_sources",
        ["organization_id", "project_id"],
    )

    op.create_table(
        "project_source_chunks",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        # VECTOR(1536) applied below via ALTER (mirrors 0002_memory; avoids a
        # hard dependency on the pgvector python package at migration import).
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["project_sources.id"], ondelete="CASCADE",
            name="fk_project_source_chunks_source_id",
        ),
    )
    op.execute(
        "ALTER TABLE project_source_chunks "
        "ALTER COLUMN embedding TYPE VECTOR(1536) USING embedding::vector"
    )
    op.execute(
        "CREATE INDEX ix_project_source_chunks_embedding ON project_source_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.drop_index("ix_project_source_chunks_embedding", table_name="project_source_chunks")
    op.drop_table("project_source_chunks")
    op.drop_index("ix_project_sources_org_project", table_name="project_sources")
    op.drop_table("project_sources")
