"""versioned project instructions

Revision ID: 0052_project_instruction_hist
Revises: 0051_desktop_device_bridge

Project instructions affect every model turn in a project, so production edits
need durable history rather than only the latest mutable value.  Existing
projects are backfilled as version 1.
"""

from alembic import op
import sqlalchemy as sa


revision = "0052_project_instruction_hist"
down_revision = "0051_desktop_device_bridge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "instructions_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_projects_instructions_version",
        "projects",
        "instructions_version >= 1",
    )

    op.create_table(
        "project_instruction_versions",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name="fk_project_instruction_versions_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "version",
            name="uq_project_instruction_versions_project_version",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_project_instruction_versions_version",
        ),
    )
    op.create_index(
        "ix_project_instruction_versions_org_project",
        "project_instruction_versions",
        ["organization_id", "project_id", "version"],
    )
    op.execute(
        """
        INSERT INTO project_instruction_versions
            (organization_id, region, project_id, version, instructions, changed_by)
        SELECT organization_id, region, id, 1, instructions, created_by
        FROM projects
        """
    )
def downgrade() -> None:
    op.drop_index(
        "ix_project_instruction_versions_org_project",
        table_name="project_instruction_versions",
    )
    op.drop_table("project_instruction_versions")
    op.drop_constraint(
        "ck_projects_instructions_version",
        "projects",
        type_="check",
    )
    op.drop_column("projects", "instructions_version")
