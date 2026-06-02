"""memory parity: control-center flags, supersession, usage log

Revision ID: 0024_memory_parity
Revises: 0023_artifact_project
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_memory_parity"
down_revision = "0023_artifact_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- memory_entries: control-center + conflict/staleness flags ---
    op.add_column("memory_entries", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("memory_entries", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("memory_entries", sa.Column("is_sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    # When an entry is superseded by a newer/merged one, point at the survivor.
    # Non-null => stale; excluded from retrieval, surfaced in the control center.
    op.add_column("memory_entries", sa.Column("superseded_by", sa.Text(), nullable=True))
    op.create_index("ix_memory_entries_supersession", "memory_entries", ["organization_id", "superseded_by"])

    # --- memory_usage_log: append-only record of which memories were used ---
    op.create_table(
        "memory_usage_log",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("used_by", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_usage_memory", "memory_usage_log", ["organization_id", "memory_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_usage_memory", "memory_usage_log")
    op.drop_table("memory_usage_log")
    op.drop_index("ix_memory_entries_supersession", "memory_entries")
    op.drop_column("memory_entries", "superseded_by")
    op.drop_column("memory_entries", "is_sensitive")
    op.drop_column("memory_entries", "is_pinned")
    op.drop_column("memory_entries", "is_archived")
