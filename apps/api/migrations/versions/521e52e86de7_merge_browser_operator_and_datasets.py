"""merge browser operator and datasets

Revision ID: 521e52e86de7
Revises: 0027_browser_operator, 0027_datasets
Create Date: 2026-06-05 11:21:23.789807
"""
from alembic import op
import sqlalchemy as sa


revision = '521e52e86de7'
down_revision = ('0027_browser_operator', '0027_datasets')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
