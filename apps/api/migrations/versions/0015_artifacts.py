"""artifacts table

Revision ID: 0015_artifacts
Revises: 0014_task_agent_loop_state
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_artifacts"
down_revision = "0014_task_agent_loop_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),  # 'markdown'|'code'|'file'|'doc'|'data'
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_conversation_id", "artifacts", ["conversation_id"])
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
    op.create_index("ix_artifacts_org_created", "artifacts", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_org_created", "artifacts")
    op.drop_index("ix_artifacts_task_id", "artifacts")
    op.drop_index("ix_artifacts_conversation_id", "artifacts")
    op.drop_table("artifacts")
