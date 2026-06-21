"""email_domain_claims: domain -> org mapping for self-serve signup (W1 Phase 2A).

The first signup from a work-email domain creates an org and soft-claims the
domain here; later same-domain signups resolve their org through this table.
``email_domain_claims`` is the canonical domain registry (sso_connections will
be reconciled against it in Phase 3).
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_email_domain_claims"
down_revision = "0039_org_subdomain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_domain_claims",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.Text(), nullable=False, server_default="soft_email"),
        sa.Column("join_policy", sa.Text(), nullable=False, server_default="auto"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("uq_email_domain_claims_domain", "email_domain_claims", ["domain"], unique=True)
    op.create_index("ix_email_domain_claims_org", "email_domain_claims", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_email_domain_claims_org", table_name="email_domain_claims")
    op.drop_index("uq_email_domain_claims_domain", table_name="email_domain_claims")
    op.drop_table("email_domain_claims")
