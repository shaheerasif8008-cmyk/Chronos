"""artifact workspace: versions, shares, artifact metadata columns

Revision ID: 0018_artifact_workspace
Revises: 0017_attachment_parsing
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_artifact_workspace"
down_revision = "0017_attachment_parsing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- artifacts: workspace metadata ---
    op.add_column("artifacts", sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")))
    op.add_column("artifacts", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("artifacts", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # --- artifact_versions: one row per saved version, version-addressed bytes ---
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("minio_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("edit_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
    )
    op.create_index("ix_artifact_versions_artifact", "artifact_versions", ["artifact_id", "version"])

    # --- artifact_shares: signed-token public links with revocation ---
    op.create_table(
        "artifact_shares",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="public_link"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_artifact_share_token"),
    )
    op.create_index("ix_artifact_shares_artifact", "artifact_shares", ["artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_shares_artifact", "artifact_shares")
    op.drop_table("artifact_shares")
    op.drop_index("ix_artifact_versions_artifact", "artifact_versions")
    op.drop_table("artifact_versions")
    op.drop_column("artifacts", "is_deleted")
    op.drop_column("artifacts", "created_by")
    op.drop_column("artifacts", "updated_at")
