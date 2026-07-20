"""durable isolated repository workspaces

Revision ID: 0058_repo_workspaces
Revises: 0057_browser_operator_remote

Repository code and credentials never live in this table. It stores only the
tenant/task binding, opaque E2B sandbox id, S3 snapshot pointer, bounded quota
metadata, and a short operation lease for replica-safe resume.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0058_repo_workspaces"
down_revision = "0057_browser_operator_remote"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_workspaces",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("runtime_provider", sa.Text(), nullable=False, server_default="e2b"),
        sa.Column("sandbox_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("snapshot_object_key", sa.Text(), nullable=True),
        sa.Column("snapshot_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("snapshot_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('creating', 'active', 'recovering', 'expired', 'failed', 'closed')", name="ck_repo_workspaces_status"),
        sa.CheckConstraint("snapshot_bytes >= 0", name="ck_repo_workspaces_snapshot_bytes"),
        sa.CheckConstraint("snapshot_version >= 0", name="ck_repo_workspaces_snapshot_version"),
        sa.UniqueConstraint(
            "organization_id",
            "task_id",
            "repo_path",
            name="uq_repo_workspaces_org_task_path",
        ),
    )
    op.create_index(
        "ix_repo_workspaces_org_status",
        "repo_workspaces",
        ["organization_id", "status", "last_used_at"],
    )
    op.create_index(
        "ix_repo_workspaces_org_task",
        "repo_workspaces",
        ["organization_id", "task_id", "repo_path"],
    )
    op.create_index(
        "ix_repo_workspaces_sandbox",
        "repo_workspaces",
        ["sandbox_id"],
        unique=True,
        postgresql_where=sa.text("sandbox_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_repo_workspaces_sandbox", table_name="repo_workspaces")
    op.drop_index("ix_repo_workspaces_org_task", table_name="repo_workspaces")
    op.drop_index("ix_repo_workspaces_org_status", table_name="repo_workspaces")
    op.drop_table("repo_workspaces")
