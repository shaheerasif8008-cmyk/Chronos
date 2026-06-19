"""graduated autonomy: trust ledger + learned policies

Revision ID: 0036_graduated_autonomy
Revises: 0035_agent_cmd_profiles
Create Date: 2026-06-19

Adds the three tables behind Graduated Autonomy:

* ``trust_levels``   — current earned standing per (workspace scope x action_class).
                       Mutable: the EWMA trust_score and auto_threshold evolve.
* ``trust_events``   — append-only evidence trail feeding the score. Immutable,
                       like ``audit_log`` (REVOKE UPDATE/DELETE + reject trigger).
* ``learned_policies`` — guardrails synthesized from approval rejections. Mutable
                       (admins ratify / disable), human-attributed.

Trust accumulates at the *workspace* scope so autonomy never leaks across teams.
Every table carries organization_id + region per RULES 4 & 5.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0036_graduated_autonomy"
down_revision = "0035_agent_cmd_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trust_levels",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("scope", sa.Text(), nullable=False),          # workspace:<id> | org
        sa.Column("action_class", sa.Text(), nullable=False),   # tool[:partition]
        sa.Column("trust_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("incidents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_threshold", sa.Float(), nullable=True),  # NULL = never auto
        sa.Column("graduated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("graduated_by", sa.Text(), nullable=True),     # member id | 'system' | 'seed'
        sa.Column("demoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "scope", "action_class",
                            name="uq_trust_levels_scope_class"),
    )
    op.create_index("ix_trust_levels_lookup", "trust_levels",
                    ["organization_id", "scope", "action_class"])

    op.create_table(
        "trust_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("action_class", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),        # auto_success|approved|rejected|incident|reverted
        sa.Column("approval_id", sa.dialects.postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_trust_events_lookup", "trust_events",
                    ["organization_id", "scope", "action_class", "created_at"])

    op.create_table(
        "learned_policies",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("action_class", sa.Text(), nullable=False),
        sa.Column("matcher", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("decision", sa.Text(), nullable=False),       # deny | require_approval
        sa.Column("source_approval_id", sa.dialects.postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("derived_from_note", sa.Text(), nullable=True),
        sa.Column("ratified_by", sa.Text(), nullable=True),     # NULL = proposed, not enforced
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_learned_policies_lookup", "learned_policies",
                    ["organization_id", "action_class", "enabled"])

    # trust_events is immutable evidence — same posture as audit_log (RULE 6 pattern).
    op.execute("GRANT SELECT, INSERT ON trust_events TO app_user")
    op.execute("REVOKE UPDATE, DELETE ON trust_events FROM app_user")
    op.execute("DROP TRIGGER IF EXISTS trust_events_append_only ON trust_events")
    op.execute(
        """
        CREATE TRIGGER trust_events_append_only
        BEFORE UPDATE OR DELETE ON trust_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trust_events_append_only ON trust_events")
    op.drop_index("ix_learned_policies_lookup", table_name="learned_policies")
    op.drop_table("learned_policies")
    op.drop_index("ix_trust_events_lookup", table_name="trust_events")
    op.drop_table("trust_events")
    op.drop_index("ix_trust_levels_lookup", table_name="trust_levels")
    op.drop_table("trust_levels")
