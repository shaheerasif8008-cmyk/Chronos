"""task reliability: attempts, failure taxonomy, dead-letter state

Revision ID: 0025_task_dead_letter
Revises: 0024_memory_parity
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_task_dead_letter"
down_revision = "0024_memory_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 1 reliability: record attempt count, a final failure taxonomy, and a
    # terminal dead-letter flag for tasks that exhausted retries / timed out.
    op.add_column("tasks", sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("tasks", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("dead_letter", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_index("ix_tasks_dead_letter", "tasks", ["organization_id", "dead_letter"])


def downgrade() -> None:
    op.drop_index("ix_tasks_dead_letter", "tasks")
    op.drop_column("tasks", "dead_letter")
    op.drop_column("tasks", "failure_reason")
    op.drop_column("tasks", "attempts")
