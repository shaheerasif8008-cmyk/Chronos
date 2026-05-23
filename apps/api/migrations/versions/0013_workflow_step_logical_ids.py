"""workflow step logical ids

Revision ID: 0013_workflow_step_logical_ids
Revises: 0012_workflow_step_runtime_refs
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_workflow_step_logical_ids"
down_revision = "0012_workflow_step_runtime_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_steps", sa.Column("step_key", sa.Text()))
    op.execute("UPDATE workflow_steps SET step_key = id WHERE step_key IS NULL")
    op.alter_column("workflow_steps", "step_key", nullable=False)
    op.create_index("ix_workflow_steps_run_step_key", "workflow_steps", ["organization_id", "run_id", "step_key"])


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_run_step_key", table_name="workflow_steps")
    op.drop_column("workflow_steps", "step_key")
