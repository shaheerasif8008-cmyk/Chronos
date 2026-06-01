"""message structured_response — canonical structured envelope per assistant message

Revision ID: 0024_message_structured_response
Revises: 0023_merge_heads
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_message_structured_response"
down_revision = "0023_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("structured_response", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "structured_response")
