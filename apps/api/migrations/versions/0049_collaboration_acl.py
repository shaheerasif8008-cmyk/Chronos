"""Shared-conversation ACLs and durable task handoffs.

Revision ID: 0049_collaboration_acl
Revises: 0048_quarantine_synth_memory

Conversations remain private by default: the creator is the sole owner until an
owner explicitly adds an ``editor`` or ``viewer`` ACL row.  A trigger creates
the owner ACL in the same database transaction as every future conversation,
while the backfill covers existing rows.

Tasks keep their immutable creator/owner and gain one current assignee plus an
append-only assignment event stream.  Reassignment and handoff therefore do not
erase the accountability chain.
"""

from alembic import op
import sqlalchemy as sa


revision = "0049_collaboration_acl"
down_revision = "0048_quarantine_synth_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_members",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_member_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')",
            name="ck_conversation_members_role",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_members_conversation_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "conversation_id",
            "member_id",
            name="uq_conversation_members_org_conversation_member",
        ),
    )
    op.create_index(
        "ix_conversation_members_org_member",
        "conversation_members",
        ["organization_id", "member_id", "conversation_id"],
    )
    op.create_index(
        "ix_conversation_members_org_conversation",
        "conversation_members",
        ["organization_id", "conversation_id"],
    )

    # Existing conversations preserve their exact private-owner semantics.
    op.execute(
        """
        INSERT INTO conversation_members (
            organization_id,
            region,
            conversation_id,
            member_id,
            role,
            granted_by_member_id
        )
        SELECT
            organization_id,
            region,
            id,
            member_id,
            'owner',
            member_id
        FROM conversations
        ON CONFLICT (organization_id, conversation_id, member_id) DO NOTHING
        """
    )

    # Keep the owner ACL atomic with conversation creation across every caller,
    # including scheduled/internal paths that do not go through the HTTP router.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION chronos_seed_conversation_owner_acl()
        RETURNS trigger AS $$
        BEGIN
            INSERT INTO conversation_members (
                organization_id,
                region,
                conversation_id,
                member_id,
                role,
                granted_by_member_id
            ) VALUES (
                NEW.organization_id,
                NEW.region,
                NEW.id,
                NEW.member_id,
                'owner',
                NEW.member_id
            )
            ON CONFLICT (organization_id, conversation_id, member_id) DO NOTHING;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER conversations_seed_owner_acl
        AFTER INSERT ON conversations
        FOR EACH ROW EXECUTE FUNCTION chronos_seed_conversation_owner_acl()
        """
    )

    op.add_column("tasks", sa.Column("assignee_member_id", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("assigned_by_member_id", sa.Text(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_org_assignee_status",
        "tasks",
        ["organization_id", "assignee_member_id", "status"],
    )

    op.create_table(
        "task_assignment_events",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("from_member_id", sa.Text(), nullable=True),
        sa.Column("to_member_id", sa.Text(), nullable=True),
        sa.Column("actor_member_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "event_type IN ('assigned', 'reassigned', 'handoff', 'unassigned')",
            name="ck_task_assignment_events_type",
        ),
        sa.CheckConstraint(
            "from_member_id IS NOT NULL OR to_member_id IS NOT NULL",
            name="ck_task_assignment_events_has_party",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_assignment_events_task_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_task_assignment_events_org_task_created",
        "task_assignment_events",
        ["organization_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_task_assignment_events_org_recipient",
        "task_assignment_events",
        ["organization_id", "to_member_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_assignment_events_org_recipient",
        table_name="task_assignment_events",
    )
    op.drop_index(
        "ix_task_assignment_events_org_task_created",
        table_name="task_assignment_events",
    )
    op.drop_table("task_assignment_events")
    op.drop_index("ix_tasks_org_assignee_status", table_name="tasks")
    op.drop_column("tasks", "assigned_at")
    op.drop_column("tasks", "assigned_by_member_id")
    op.drop_column("tasks", "assignee_member_id")

    op.execute("DROP TRIGGER IF EXISTS conversations_seed_owner_acl ON conversations")
    op.execute("DROP FUNCTION IF EXISTS chronos_seed_conversation_owner_acl")
    op.drop_index(
        "ix_conversation_members_org_conversation",
        table_name="conversation_members",
    )
    op.drop_index(
        "ix_conversation_members_org_member",
        table_name="conversation_members",
    )
    op.drop_table("conversation_members")
