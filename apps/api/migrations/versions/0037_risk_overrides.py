"""risk registry overrides for graduated autonomy

Revision ID: 0037_risk_overrides
Revises: 0036_graduated_autonomy
Create Date: 2026-06-19

Admin-editable per-org overrides for the Risk Pricer's base factors. Inference
(core/risk.py) handles the long tail; this table captures the exceptions an org
wants to tune, without touching the model-facing tool schemas.
"""
from alembic import op
import sqlalchemy as sa


revision = "0037_risk_overrides"
down_revision = "0036_graduated_autonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_overrides",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("blast_radius", sa.Float(), nullable=False),
        sa.Column("irreversibility", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "tool", name="uq_risk_overrides_tool"),
    )
    op.create_index("ix_risk_overrides_org", "risk_overrides", ["organization_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_risk_overrides_org", table_name="risk_overrides")
    op.drop_table("risk_overrides")
