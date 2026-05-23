"""approval execution payload for continuation

Revision ID: 0010_approval_execution_payload
Revises: 0009_connector_operations
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_approval_execution_payload"
down_revision = "0009_connector_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("execution_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("approval_requests", sa.Column("resumed_job_id", sa.Text()))


def downgrade() -> None:
    op.drop_column("approval_requests", "resumed_job_id")
    op.drop_column("approval_requests", "execution_payload")
