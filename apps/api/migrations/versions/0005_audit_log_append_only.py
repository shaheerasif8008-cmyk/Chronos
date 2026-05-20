"""enforce audit log append-only trigger

Revision ID: 0005_audit_log_append_only
Revises: 0004_tasks
Create Date: 2026-05-20
"""
from alembic import op

revision = "0005_audit_log_append_only"
down_revision = "0004_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user LOGIN PASSWORD 'chronos_app';
            ELSE
                ALTER ROLE app_user LOGIN PASSWORD 'chronos_app';
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
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
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
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM app_user")
