"""connectors and credential vault

Revision ID: 0003_connectors
Revises: 0002_memory
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_connectors"
down_revision = "0002_memory"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("persona_id", sa.Text()),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_handle", sa.Text()),
        sa.Column("vault_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("scopes", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_connectors_org_provider",
        "connectors",
        ["organization_id", "provider", "status"],
    )

    op.create_table(
        "vault_entries",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("vault_ref", sa.Text(), nullable=False, unique=True),
        # encrypted_data: nonce+ciphertext hex (AES-256-GCM). Never log this column.
        sa.Column("encrypted_data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("vault_entries")
    op.drop_index("ix_connectors_org_provider", table_name="connectors")
    op.drop_table("connectors")
