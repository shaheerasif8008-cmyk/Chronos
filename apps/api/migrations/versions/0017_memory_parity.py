"""memory parity controls

Revision ID: 0017_memory_parity
Revises: 0016_task_checkpoints
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_memory_parity"
down_revision = "0016_task_checkpoints"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.add_column("memory_entries", sa.Column("confidence_score", sa.Float(), nullable=False, server_default="1.0"))
    op.add_column("memory_entries", sa.Column("status", sa.Text(), nullable=False, server_default="active"))
    op.add_column("memory_entries", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")))
    op.add_column("memory_entries", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")))
    op.add_column("memory_entries", sa.Column("is_sensitive", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")))
    op.add_column("memory_entries", sa.Column("staleness", sa.Text(), nullable=False, server_default="fresh"))
    op.add_column("memory_entries", sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("memory_entries", sa.Column("conflict_group_id", sa.Text()))
    op.add_column("memory_entries", sa.Column("supersedes_memory_id", sa.Text()))
    op.create_index("ix_memory_entries_status", "memory_entries", ["organization_id", "status", "is_archived"])
    op.create_index("ix_memory_entries_conflict_group", "memory_entries", ["organization_id", "conflict_group_id"])

    op.create_table(
        "memory_access_logs",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False, server_default="api"),
        sa.Column("request_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_memory_access_logs_memory", "memory_access_logs", ["organization_id", "memory_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_access_logs_memory", table_name="memory_access_logs")
    op.drop_table("memory_access_logs")
    op.drop_index("ix_memory_entries_conflict_group", table_name="memory_entries")
    op.drop_index("ix_memory_entries_status", table_name="memory_entries")
    for column in [
        "supersedes_memory_id",
        "conflict_group_id",
        "provenance",
        "staleness",
        "is_sensitive",
        "is_archived",
        "is_pinned",
        "status",
        "confidence_score",
    ]:
        op.drop_column("memory_entries", column)
