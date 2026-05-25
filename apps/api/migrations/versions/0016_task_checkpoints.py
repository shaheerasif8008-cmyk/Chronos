"""task_checkpoints — named state snapshots for long-running tasks

Revision ID: 0016_task_checkpoints
Revises: 0015_artifacts
Create Date: 2026-05-25

Category 5 (State Management), Step 3. Per-step working state is already persisted
on the tasks row (result + agent_state), so this table is NOT a duplicate of that —
it is the inspectable/named-snapshot artifact the roadmap asks for: a plan step can
declare `"checkpoint": "<name>"` to freeze a labelled copy of the execution context
at that point. No `execution_context` column is added because result + agent_state
already cover Step 1.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_task_checkpoints"
down_revision = "0015_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_name", sa.Text(), nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_task_checkpoints_task", "task_checkpoints", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_checkpoints_task", "task_checkpoints")
    op.drop_table("task_checkpoints")
