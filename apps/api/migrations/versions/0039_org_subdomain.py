"""Add subdomain + onboarding columns to organizations (W1 Phase 1).

Tenant resolution maps a request's subdomain label to an org via
``organizations.subdomain``. Backfills existing rows from ``slug`` so current
tenants (incl. the seeded ``default`` org) resolve immediately.

Revision ID: 0039_org_subdomain
Revises: 0038_sso_scim
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa


revision = "0039_org_subdomain"
down_revision = "0038_sso_scim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("subdomain", sa.Text(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column("onboarding_state", sa.Text(), nullable=False, server_default="new"),
    )
    op.add_column(
        "organizations",
        sa.Column("owner_member_id", sa.Text(), nullable=True),
    )
    op.execute("UPDATE organizations SET subdomain = slug WHERE subdomain IS NULL")
    op.create_index(
        "uq_organizations_subdomain",
        "organizations",
        ["subdomain"],
        unique=True,
        postgresql_where=sa.text("subdomain IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_organizations_subdomain", table_name="organizations")
    op.drop_column("organizations", "owner_member_id")
    op.drop_column("organizations", "onboarding_state")
    op.drop_column("organizations", "subdomain")
