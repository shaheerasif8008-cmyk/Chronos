"""comments: collaboration layer — comments + @mentions on projects/tasks/artifacts.

Tenant-scoped. A comment is attached to a target entity (``target_type`` in
{project, task, artifact}) and may mention members; ``mentions`` holds the
resolved member ids (a mention only resolves to a member who can already see the
target, so the column never leaks cross-scope identity). Soft-deleted via
``deleted_at`` so threads keep their lineage.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0044_comments"
down_revision = "0043_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("author_member_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_comments_org_target",
        "comments",
        ["organization_id", "target_type", "target_id", "created_at"],
    )
    op.create_index(
        "ix_comments_org_author",
        "comments",
        ["organization_id", "author_member_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_comments_org_author", table_name="comments")
    op.drop_index("ix_comments_org_target", table_name="comments")
    op.drop_table("comments")
