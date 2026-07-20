"""persist tenant-scoped Stripe billing state and webhook idempotency

Revision ID: 0055_billing_accounts
Revises: 0054_dataset_project_scope
"""

import sqlalchemy as sa
from alembic import op


revision = "0055_billing_accounts"
down_revision = "0054_dataset_project_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_accounts",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True),
        sa.Column("subscription_status", sa.Text(), nullable=True),
        sa.Column("plan", sa.Text(), nullable=False, server_default="trial"),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_created", sa.BigInteger(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_billing_accounts_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_billing_accounts"),
        sa.UniqueConstraint("stripe_customer_id", name="uq_billing_accounts_customer"),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_billing_accounts_subscription"
        ),
    )
    op.create_index(
        "ix_billing_accounts_status",
        "billing_accounts",
        ["subscription_status", "updated_at"],
    )

    op.create_table(
        "billing_webhook_events",
        sa.Column("stripe_event_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_created", sa.BigInteger(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_billing_webhook_events_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("stripe_event_id", name="pk_billing_webhook_events"),
    )
    op.create_index(
        "ix_billing_webhook_events_org_processed",
        "billing_webhook_events",
        ["organization_id", "processed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_webhook_events_org_processed",
        table_name="billing_webhook_events",
    )
    op.drop_table("billing_webhook_events")
    op.drop_index("ix_billing_accounts_status", table_name="billing_accounts")
    op.drop_table("billing_accounts")
