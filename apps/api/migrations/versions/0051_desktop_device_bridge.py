"""authenticated desktop-device bridge

Revision ID: 0051_desktop_device_bridge
Revises: 0050_retention_controls

The API never stores a device bearer token or a Mac path.  Device tokens and
pairing codes are persisted only as keyed/one-way digests; the per-device
command signing key is AES-256-GCM encrypted with tenant-bound associated data.
Folder bookmarks stay exclusively on the paired device behind an opaque
``client_grant_id``.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0051_desktop_device_bridge"
down_revision = "0050_retention_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "desktop_pair_codes",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "member_id"],
            ["members.organization_id", "members.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("code_hash", name="uq_desktop_pair_codes_hash"),
    )
    op.create_index(
        "ix_desktop_pair_codes_org_member",
        "desktop_pair_codes",
        ["organization_id", "member_id", "expires_at"],
    )
    op.create_index(
        "ix_desktop_pair_codes_expiry", "desktop_pair_codes", ["expires_at"]
    )

    op.create_table(
        "desktop_devices",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("client_version", sa.Text(), nullable=True),
        sa.Column("capabilities", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("encrypted_command_secret", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "member_id"],
            ["members.organization_id", "members.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_desktop_devices_token_hash"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_desktop_devices_status"),
    )
    op.create_index(
        "ix_desktop_devices_org_member_status",
        "desktop_devices",
        ["organization_id", "member_id", "status"],
    )

    op.add_column("local_computer_grants", sa.Column("device_id", sa.UUID(), nullable=True))
    op.add_column("local_computer_grants", sa.Column("client_grant_id", sa.Text(), nullable=True))
    op.add_column("local_computer_grants", sa.Column("folder_display_name", sa.Text(), nullable=True))
    op.alter_column("local_computer_grants", "folder_path", existing_type=sa.Text(), nullable=True)
    # Pre-bridge grants pointed at the API host and are invalid in production.
    # Revoke and scrub them during upgrade so an old absolute path is not kept
    # after the deployment moves to client-owned security-scoped bookmarks.
    op.execute(
        """
        UPDATE local_computer_grants
        SET status = 'revoked',
            folder_path = NULL,
            folder_display_name = 'Legacy local folder',
            revoked_at = COALESCE(revoked_at, NOW()),
            updated_at = NOW()
        WHERE device_id IS NULL
        """
    )
    op.create_foreign_key(
        "fk_local_computer_grants_device",
        "local_computer_grants",
        "desktop_devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_local_computer_grants_device_status",
        "local_computer_grants",
        ["organization_id", "device_id", "status"],
    )
    op.create_index(
        "uq_local_computer_grants_device_client",
        "local_computer_grants",
        ["device_id", "client_grant_id"],
        unique=True,
        postgresql_where=sa.text("device_id IS NOT NULL AND client_grant_id IS NOT NULL"),
    )

    op.create_table(
        "desktop_commands",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("grant_id", sa.UUID(), nullable=True),
        sa.Column("command_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_nonce", sa.Text(), nullable=True),
        sa.Column("result_status", sa.Text(), nullable=True),
        sa.Column("result_error_code", sa.Text(), nullable=True),
        sa.Column("result_payload", sa.LargeBinary(), nullable=True),
        sa.Column("result_sha256", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["device_id"], ["desktop_devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grant_id"], ["local_computer_grants.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("device_id", "nonce", name="uq_desktop_commands_device_nonce"),
        sa.UniqueConstraint("device_id", "result_nonce", name="uq_desktop_commands_device_result_nonce"),
        sa.CheckConstraint(
            "command_type IN ('list_files', 'read_file', 'exec', 'open_app', 'revoke_grant', 'notify')",
            name="ck_desktop_commands_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed', 'expired', 'cancelled')",
            name="ck_desktop_commands_status",
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts BETWEEN 1 AND 10", name="ck_desktop_commands_attempts"),
    )
    op.create_index(
        "ix_desktop_commands_device_queue",
        "desktop_commands",
        ["device_id", "status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_desktop_commands_org_member",
        "desktop_commands",
        ["organization_id", "member_id", "created_at"],
    )
    op.create_index(
        "ix_desktop_commands_lease_expiry",
        "desktop_commands",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_desktop_commands_retention", "desktop_commands", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_commands_retention", table_name="desktop_commands")
    op.drop_index("ix_desktop_commands_lease_expiry", table_name="desktop_commands")
    op.drop_index("ix_desktop_commands_org_member", table_name="desktop_commands")
    op.drop_index("ix_desktop_commands_device_queue", table_name="desktop_commands")
    op.drop_table("desktop_commands")

    op.drop_index("uq_local_computer_grants_device_client", table_name="local_computer_grants")
    op.drop_index("ix_local_computer_grants_device_status", table_name="local_computer_grants")
    op.drop_constraint("fk_local_computer_grants_device", "local_computer_grants", type_="foreignkey")
    # Bridge grants deliberately have no server-side path and cannot be
    # represented by the pre-0051 NOT NULL schema.
    op.execute("DELETE FROM local_computer_grants WHERE device_id IS NOT NULL")
    op.execute("UPDATE local_computer_grants SET folder_path = '' WHERE folder_path IS NULL")
    op.alter_column("local_computer_grants", "folder_path", existing_type=sa.Text(), nullable=False)
    op.drop_column("local_computer_grants", "folder_display_name")
    op.drop_column("local_computer_grants", "client_grant_id")
    op.drop_column("local_computer_grants", "device_id")

    op.drop_index("ix_desktop_devices_org_member_status", table_name="desktop_devices")
    op.drop_table("desktop_devices")
    op.drop_index("ix_desktop_pair_codes_org_member", table_name="desktop_pair_codes")
    op.drop_index("ix_desktop_pair_codes_expiry", table_name="desktop_pair_codes")
    op.drop_table("desktop_pair_codes")
