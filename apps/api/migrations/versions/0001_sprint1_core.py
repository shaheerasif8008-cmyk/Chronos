"""sprint1 core schema

Revision ID: 0001_sprint1_core
Revises:
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_sprint1_core"
down_revision = None
branch_labels = None
depends_on = None


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "organizations",
        sa.Column("id", sa.Text(), primary_key=True, server_default="default"),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False, server_default="trial"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("name", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "email", name="uq_members_org_email"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *tenant_columns(),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text()),
        sa.Column("resource_id", sa.Text()),
        sa.Column("payload", sa.JSON()),
        sa.Column("decision", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_audit_log_org_created", "audit_log", ["organization_id", "created_at"])

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user LOGIN PASSWORD 'chronos_app';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user")
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM app_user")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_log_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_log_mutation")
    op.execute("DROP ROLE IF EXISTS app_user")
    op.drop_table("audit_log")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("members")
    op.drop_table("organizations")
