"""native organization administration lifecycle

Revision ID: 0061_admin_lifecycle
Revises: 0060_task_cleanup

The lifecycle tables keep every ownership and destructive mutation tenant
scoped. Workspace deletion is a delayed tombstone so retention evidence is not
silently destroyed, while API key material is represented only by an indexed
lookup id and a peppered digest.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0061_admin_lifecycle"
down_revision = "0060_task_cleanup"
branch_labels = None
depends_on = None


def _tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.create_table(
        "native_groups",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "name", name="uq_native_groups_org_name"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="ck_native_groups_name"),
    )
    op.create_index("ix_native_groups_org_created", "native_groups", ["organization_id", "created_at"])

    op.create_table(
        "native_group_members",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant_columns(),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["group_id"], ["native_groups.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "group_id", "member_id", name="uq_native_group_member"),
    )
    op.create_index("ix_native_group_members_org_member", "native_group_members", ["organization_id", "member_id"])

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("legacy_key", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_by", sa.Text(), nullable=True),
        sa.Column("deletion_execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "slug", name="uq_workspaces_org_slug"),
        sa.UniqueConstraint("organization_id", "legacy_key", name="uq_workspaces_org_legacy_key"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="ck_workspaces_name"),
        sa.CheckConstraint("status IN ('active', 'archived', 'deletion_pending', 'deleted')", name="ck_workspaces_status"),
    )
    op.create_index("ix_workspaces_org_status", "workspaces", ["organization_id", "status", "created_at"])
    op.create_index("ix_workspaces_deletion_due", "workspaces", ["status", "deletion_execute_after"])

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant_columns(),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "workspace_id", "member_id", name="uq_workspace_member"),
        sa.CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_workspace_member_role"),
    )
    op.create_index("ix_workspace_members_org_member", "workspace_members", ["organization_id", "member_id"])

    # Reconcile the historically implicit `default` workspace into the native
    # lifecycle without rewriting mixed Text/UUID legacy foreign keys. The
    # alias is resolved at authorization time; all active existing members keep
    # their former access and every organization receives at least one owner.
    op.execute(
        """
        INSERT INTO workspaces (
            id, organization_id, region, name, slug, legacy_key, status,
            created_by, created_at, updated_at
        )
        SELECT
            gen_random_uuid()::text,
            organization.id,
            organization.region,
            COALESCE(NULLIF(organization.name, ''), 'Default workspace'),
            'default',
            'default',
            'active',
            COALESCE((
                SELECT member.id FROM members AS member
                WHERE member.organization_id = organization.id
                  AND member.status = 'active'
                ORDER BY CASE WHEN member.role = 'owner' THEN 0 ELSE 1 END,
                         member.created_at, member.id
                LIMIT 1
            ), 'system'),
            NOW(),
            NOW()
        FROM organizations AS organization
        ON CONFLICT (organization_id, legacy_key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO workspace_members (
            id, organization_id, region, workspace_id, member_id, role,
            added_by, created_at, updated_at
        )
        SELECT
            gen_random_uuid()::text,
            member.organization_id,
            member.region,
            workspace.id,
            member.id,
            CASE
                WHEN member.role IN ('owner', 'admin') THEN 'owner'
                WHEN member.id = (
                    SELECT first_member.id FROM members AS first_member
                    WHERE first_member.organization_id = member.organization_id
                      AND first_member.status = 'active'
                    ORDER BY first_member.created_at, first_member.id
                    LIMIT 1
                ) THEN 'owner'
                ELSE 'editor'
            END,
            workspace.created_by,
            NOW(),
            NOW()
        FROM members AS member
        JOIN workspaces AS workspace
          ON workspace.organization_id = member.organization_id
         AND workspace.legacy_key = 'default'
        WHERE member.status = 'active'
        ON CONFLICT (organization_id, workspace_id, member_id) DO NOTHING
        """
    )

    op.create_table(
        "organization_api_keys",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lookup_id", sa.Text(), nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("ARRAY['read']::text[]")),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip_hash", sa.Text(), nullable=True),
        sa.Column("created_by_member_id", sa.Text(), nullable=False),
        sa.Column("rotated_from_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["rotated_from_id"], ["organization_api_keys.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_organization_api_keys_status"),
        sa.CheckConstraint("rate_limit_per_minute BETWEEN 1 AND 6000", name="ck_organization_api_keys_rate"),
        sa.CheckConstraint("cardinality(scopes) BETWEEN 1 AND 3 AND scopes <@ ARRAY['read','write','admin']::text[]", name="ck_organization_api_keys_scopes"),
    )
    op.create_index("ix_organization_api_keys_org_status", "organization_api_keys", ["organization_id", "status", "created_at"])

    op.drop_constraint("ck_retention_holds_resource_type", "retention_holds", type_="check")
    op.create_check_constraint(
        "ck_retention_holds_resource_type",
        "retention_holds",
        "resource_type IN ('organization', 'memory', 'artifact', 'workspace')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_retention_holds_resource_type", "retention_holds", type_="check")
    op.create_check_constraint(
        "ck_retention_holds_resource_type",
        "retention_holds",
        "resource_type IN ('organization', 'memory', 'artifact')",
    )
    op.drop_index("ix_organization_api_keys_org_status", table_name="organization_api_keys")
    op.drop_table("organization_api_keys")
    op.drop_index("ix_workspace_members_org_member", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index("ix_workspaces_deletion_due", table_name="workspaces")
    op.drop_index("ix_workspaces_org_status", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index("ix_native_group_members_org_member", table_name="native_group_members")
    op.drop_table("native_group_members")
    op.drop_index("ix_native_groups_org_created", table_name="native_groups")
    op.drop_table("native_groups")
