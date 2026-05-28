"""projects and project_members tables

Revision ID: 0018_projects
Revises: 0017_messages_rich
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_projects"
down_revision = "0017_messages_rich"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("visibility", sa.Text(), nullable=True, server_default="private"),
        sa.Column(
            "default_tools",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("memory_policy", sa.Text(), nullable=True, server_default="default"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_org_created", "projects", ["organization_id", "created_at"])

    op.create_table(
        "project_members",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True, server_default="member"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "member_id", name="uq_project_members_proj_member"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_project_members_project_id",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["members.id"], ondelete="CASCADE",
            name="fk_project_members_member_id",
        ),
    )
    op.create_index("ix_project_members_member_id", "project_members", ["member_id"])
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_members_project_id", "project_members")
    op.drop_index("ix_project_members_member_id", "project_members")
    op.drop_table("project_members")
    op.drop_index("ix_projects_org_created", "projects")
    op.drop_table("projects")
