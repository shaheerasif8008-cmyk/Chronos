"""domain_verifications: DNS-TXT proof of domain ownership (W1 Phase 3)."""
from alembic import op
import sqlalchemy as sa

revision = "0041_domain_verifications"
down_revision = "0040_email_domain_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domain_verifications",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("txt_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_domain_verifications_org_domain", "domain_verifications",
                    ["organization_id", "domain"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_domain_verifications_org_domain", table_name="domain_verifications")
    op.drop_table("domain_verifications")
