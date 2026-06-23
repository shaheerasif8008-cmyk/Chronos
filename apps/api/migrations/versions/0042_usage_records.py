"""usage_records: persistent monthly usage ledger for billing (W4.3)."""
from alembic import op
import sqlalchemy as sa

revision = "0042_usage_records"
down_revision = "0041_domain_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("period", sa.Text(), nullable=False),          # 'YYYY-MM'
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("uq_usage_records_org_period", "usage_records", ["organization_id", "period"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_usage_records_org_period", table_name="usage_records")
    op.drop_table("usage_records")
