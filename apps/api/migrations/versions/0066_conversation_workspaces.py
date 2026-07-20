"""Bind every conversation to one native tenant workspace.

Revision ID: 0066_conversation_workspaces
Revises: 0065_file_security
"""

from alembic import op
import sqlalchemy as sa


revision = "0066_conversation_workspaces"
down_revision = "0065_file_security"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(constraint["name"])
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    }


def upgrade() -> None:
    # Some early 0061 deployments created the workspace lifecycle before the
    # legacy alias column was added to that migration. Reconcile those already-
    # stamped databases here; fresh databases already have this exact schema.
    if "legacy_key" not in _column_names("workspaces"):
        op.add_column("workspaces", sa.Column("legacy_key", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE workspaces AS workspace
        SET legacy_key = 'default'
        WHERE workspace.legacy_key IS NULL
          AND workspace.slug = 'default'
          AND NOT EXISTS (
              SELECT 1
              FROM workspaces AS existing
              WHERE existing.organization_id = workspace.organization_id
                AND existing.legacy_key = 'default'
          )
        """
    )
    if "uq_workspaces_org_legacy_key" not in _constraint_names("workspaces"):
        op.create_unique_constraint(
            "uq_workspaces_org_legacy_key",
            "workspaces",
            ["organization_id", "legacy_key"],
        )

    if "workspace_id" not in _column_names("conversations"):
        op.add_column(
            "conversations", sa.Column("workspace_id", sa.Text(), nullable=True)
        )

    # Defensive compatibility for databases whose historical conversation org
    # predates the organizations backfill in 0061. Each such tenant gets exactly
    # one native default workspace before conversation rows are made non-null.
    op.execute(
        """
        INSERT INTO workspaces (
            id, organization_id, region, name, slug, legacy_key, status,
            created_by, created_at, updated_at
        )
        SELECT
            gen_random_uuid()::text,
            conversation.organization_id,
            COALESCE(MAX(conversation.region), 'us'),
            'Default workspace',
            'default',
            'default',
            'active',
            COALESCE(MAX(conversation.member_id), 'system'),
            NOW(),
            NOW()
        FROM conversations AS conversation
        LEFT JOIN workspaces AS workspace
          ON workspace.organization_id = conversation.organization_id
         AND workspace.legacy_key = 'default'
        WHERE workspace.id IS NULL
        GROUP BY conversation.organization_id
        ON CONFLICT (organization_id, legacy_key) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE conversations AS conversation
        SET workspace_id = workspace.id
        FROM workspaces AS workspace
        WHERE workspace.organization_id = conversation.organization_id
          AND workspace.legacy_key = 'default'
          AND conversation.workspace_id IS NULL
        """
    )
    op.alter_column(
        "conversations", "workspace_id", existing_type=sa.Text(), nullable=False
    )

    # The composite key prevents a conversation from ever pointing at another
    # tenant's workspace, even if an application-layer check regresses.
    if "uq_workspaces_org_id" not in _constraint_names("workspaces"):
        op.create_unique_constraint(
            "uq_workspaces_org_id", "workspaces", ["organization_id", "id"]
        )
    op.create_foreign_key(
        "fk_conversations_workspace_tenant",
        "conversations",
        "workspaces",
        ["organization_id", "workspace_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_conversations_org_workspace_updated",
        "conversations",
        ["organization_id", "workspace_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_org_workspace_updated", table_name="conversations")
    op.drop_constraint(
        "fk_conversations_workspace_tenant", "conversations", type_="foreignkey"
    )
    op.drop_constraint("uq_workspaces_org_id", "workspaces", type_="unique")
    op.drop_column("conversations", "workspace_id")
