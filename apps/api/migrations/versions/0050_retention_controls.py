"""tenant retention controls and legal holds

Revision ID: 0050_retention_controls
Revises: 0049_collaboration_acl

Legal holds are deliberately separate from mutable settings JSON.  An active
hold can cover an organization or a specific memory/artifact, is attributable
to the administrator who created it, and is released rather than deleted so
the audit trail remains explainable.
"""

from alembic import op
import sqlalchemy as sa


revision = "0050_retention_controls"
down_revision = "0049_collaboration_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retention_holds",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column(
            "organization_id", sa.Text(), nullable=False, server_default="default"
        ),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("released_by", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resource_type IN ('organization', 'memory', 'artifact')",
            name="ck_retention_holds_resource_type",
        ),
    )
    op.create_index(
        "ix_retention_holds_org_active",
        "retention_holds",
        ["organization_id", "released_at"],
    )
    op.create_index(
        "uq_retention_holds_active_resource",
        "retention_holds",
        ["organization_id", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_memory_entries_retention",
        "memory_entries",
        ["organization_id", "is_deleted", "updated_at"],
    )
    op.create_index(
        "ix_artifacts_retention",
        "artifacts",
        ["organization_id", "is_deleted", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_retention", table_name="artifacts")
    op.drop_index("ix_memory_entries_retention", table_name="memory_entries")
    op.drop_index(
        "uq_retention_holds_active_resource", table_name="retention_holds"
    )
    op.drop_index("ix_retention_holds_org_active", table_name="retention_holds")
    op.drop_table("retention_holds")
