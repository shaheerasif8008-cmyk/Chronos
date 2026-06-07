"""object storage s3 only

Revision ID: 0031_object_storage_s3_only
Revises: 0030_phase12_workflows_monitors
Create Date: 2026-06-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_object_storage_s3_only"
down_revision = "0030_phase12_workflows_monitors"
branch_labels = None
depends_on = None

_LEGACY_OBJECT_PATH = "mini" + "o_path"


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if _has_column("artifacts", _LEGACY_OBJECT_PATH) and not _has_column("artifacts", "object_path"):
        op.alter_column("artifacts", _LEGACY_OBJECT_PATH, new_column_name="object_path")
    if _has_column("artifact_versions", _LEGACY_OBJECT_PATH) and not _has_column("artifact_versions", "object_path"):
        op.alter_column("artifact_versions", _LEGACY_OBJECT_PATH, new_column_name="object_path")


def downgrade() -> None:
    if _has_column("artifacts", "object_path") and not _has_column("artifacts", _LEGACY_OBJECT_PATH):
        op.alter_column("artifacts", "object_path", new_column_name=_LEGACY_OBJECT_PATH)
    if _has_column("artifact_versions", "object_path") and not _has_column("artifact_versions", _LEGACY_OBJECT_PATH):
        op.alter_column("artifact_versions", "object_path", new_column_name=_LEGACY_OBJECT_PATH)
