"""messages_rich — add metadata columns to messages, conversations, tasks

Revision ID: 0017_messages_rich
Revises: 0016_task_checkpoints
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_messages_rich"
down_revision = "0016_task_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── messages ─────────────────────────────────────────────────────────────
    op.add_column("messages", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("mode", sa.Text(), nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "citations",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "tool_traces",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "memory_refs",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "artifact_refs",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("messages", sa.Column("approval_state", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("runtime_status", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("parent_message_id", sa.UUID(), nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )

    # ── conversations ─────────────────────────────────────────────────────────
    op.add_column("conversations", sa.Column("project_id", sa.UUID(), nullable=True))

    # ── tasks ────────────────────────────────────────────────────────────────
    op.add_column("tasks", sa.Column("project_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("mode", sa.Text(), nullable=True))


def downgrade() -> None:
    # ── tasks ────────────────────────────────────────────────────────────────
    op.drop_column("tasks", "mode")
    op.drop_column("tasks", "project_id")

    # ── conversations ─────────────────────────────────────────────────────────
    op.drop_column("conversations", "project_id")

    # ── messages ─────────────────────────────────────────────────────────────
    op.drop_column("messages", "pinned")
    op.drop_column("messages", "parent_message_id")
    op.drop_column("messages", "runtime_status")
    op.drop_column("messages", "approval_state")
    op.drop_column("messages", "artifact_refs")
    op.drop_column("messages", "memory_refs")
    op.drop_column("messages", "tool_traces")
    op.drop_column("messages", "citations")
    op.drop_column("messages", "mode")
    op.drop_column("messages", "model")
