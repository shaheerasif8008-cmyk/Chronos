"""settings documents and tool policy

Revision ID: 0006_settings
Revises: 0005_audit_log_append_only
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_settings"
down_revision = "0005_audit_log_append_only"
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.create_table(
        "settings_documents",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "scope", "scope_id", "section", name="uq_settings_documents_scope_section"),
    )
    op.create_index(
        "ix_settings_documents_org_scope",
        "settings_documents",
        ["organization_id", "scope", "scope_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_settings_documents_org_scope", table_name="settings_documents")
    op.drop_table("settings_documents")
