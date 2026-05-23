"""workflow step runtime references

Revision ID: 0012_workflow_step_runtime_refs
Revises: 0011_workflow_runtime
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_workflow_step_runtime_refs"
down_revision = "0011_workflow_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_steps", sa.Column("execution_job_id", sa.Text()))
    op.add_column("workflow_steps", sa.Column("approval_request_id", sa.Text()))


def downgrade() -> None:
    op.drop_column("workflow_steps", "approval_request_id")
    op.drop_column("workflow_steps", "execution_job_id")
