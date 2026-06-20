"""enterprise SSO (OIDC) + SCIM 2.0 provisioning

Revision ID: 0038_sso_scim
Revises: 0037_risk_overrides
Create Date: 2026-06-19

Adds the tables behind enterprise identity:

* ``sso_connections``  — per-org OIDC identity providers (Okta, Entra ID, Google,
                         Auth0, Ping, …). Login is routed by email domain or org.
* ``scim_tokens``      — per-org bearer tokens that an IdP uses to provision users
                         and groups via SCIM 2.0. Only the token hash is stored.
* ``scim_groups``      — IdP groups mapped to a Chronos role.
* ``group_memberships``— group ↔ member links (SCIM Group members).

Plus member columns for external identity + lifecycle:
* ``external_id``  — the IdP/SCIM id for the user (stable across email changes).
* ``status``       — active | deactivated (SCIM deprovisioning sets deactivated).
* ``sso_subject``  — the OIDC ``sub`` claim, bound on first SSO login.
"""
from alembic import op
import sqlalchemy as sa


revision = "0038_sso_scim"
down_revision = "0037_risk_overrides"
branch_labels = None
depends_on = None


def _tenant():
    return (
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
    )


def upgrade() -> None:
    # Member identity + lifecycle columns.
    op.add_column("members", sa.Column("external_id", sa.Text(), nullable=True))
    op.add_column("members", sa.Column("status", sa.Text(), nullable=False, server_default="active"))
    op.add_column("members", sa.Column("sso_subject", sa.Text(), nullable=True))
    # One IdP user per org (SCIM externalId is unique within a tenant).
    op.create_index(
        "uq_members_org_external_id",
        "members",
        ["organization_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "sso_connections",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant(),
        sa.Column("protocol", sa.Text(), nullable=False, server_default="oidc"),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False, server_default=""),
        sa.Column("authorize_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("jwks_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("userinfo_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="openid email profile"),
        sa.Column("email_domain", sa.Text(), nullable=True),   # routes login by domain
        sa.Column("default_role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_sso_connections_org", "sso_connections", ["organization_id", "enabled"])
    op.create_index(
        "uq_sso_connections_domain", "sso_connections", ["email_domain"],
        unique=True, postgresql_where=sa.text("email_domain IS NOT NULL AND enabled"),
    )

    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant(),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_prefix", sa.Text(), nullable=False),     # first chars, for display
        sa.Column("token_hash", sa.Text(), nullable=False),       # sha256 of the secret
        sa.Column("default_role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_scim_tokens_hash"),
    )
    op.create_index("ix_scim_tokens_org", "scim_tokens", ["organization_id", "enabled"])

    op.create_table(
        "scim_groups",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant(),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),  # role this group grants
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("organization_id", "display_name", name="uq_scim_groups_org_name"),
    )
    op.create_index("ix_scim_groups_org", "scim_groups", ["organization_id"])

    op.create_table(
        "group_memberships",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        *_tenant(),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("group_id", "member_id", name="uq_group_memberships"),
    )
    op.create_index("ix_group_memberships_group", "group_memberships", ["group_id"])
    op.create_index("ix_group_memberships_member", "group_memberships", ["member_id"])


def downgrade() -> None:
    op.drop_table("group_memberships")
    op.drop_index("ix_scim_groups_org", table_name="scim_groups")
    op.drop_table("scim_groups")
    op.drop_index("ix_scim_tokens_org", table_name="scim_tokens")
    op.drop_table("scim_tokens")
    op.drop_index("uq_sso_connections_domain", table_name="sso_connections")
    op.drop_index("ix_sso_connections_org", table_name="sso_connections")
    op.drop_table("sso_connections")
    op.drop_index("uq_members_org_external_id", table_name="members")
    op.drop_column("members", "sso_subject")
    op.drop_column("members", "status")
    op.drop_column("members", "external_id")
