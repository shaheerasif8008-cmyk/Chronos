"""task agent loop state

Revision ID: 0014_task_agent_loop_state
Revises: 0013_workflow_step_logical_ids
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_task_agent_loop_state"
down_revision = "0013_workflow_step_logical_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("iteration_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("agent_state", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.drop_column("tasks", "agent_state")
    op.drop_column("tasks", "iteration_count")
