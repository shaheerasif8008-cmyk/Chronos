"""memory system

Revision ID: 0002_memory
Revises: 0001_sprint1_core
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_memory"
down_revision = "0001_sprint1_core"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("scope", sa.Text(), nullable=False, server_default="org"),
        sa.Column("scope_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_conversation_id", sa.Text()),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("created_by", sa.Text()),
    )
    op.execute("ALTER TABLE memory_entries ALTER COLUMN embedding TYPE VECTOR(1536) USING embedding::vector")
    op.execute(
        "CREATE INDEX ix_memory_entries_embedding ON memory_entries "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.create_index(
        "ix_memory_entries_org_scope",
        "memory_entries",
        ["organization_id", "scope", "scope_id"],
    )

    op.create_table(
        "context_suggestions",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("suggested_patch", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="context_update_job"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("context_suggestions")
    op.drop_index("ix_memory_entries_org_scope", table_name="memory_entries")
    op.drop_index("ix_memory_entries_embedding", table_name="memory_entries")
    op.drop_table("memory_entries")
