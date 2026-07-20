"""Production agent publications, provider bindings, and durable delivery.

Revision ID: 0064_agent_publications
Revises: 0063_connector_write_ledger
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0064_agent_publications"
down_revision = "0063_connector_write_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_publication_bindings",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text()),
        sa.Column("external_tenant_id", sa.Text(), nullable=False),
        sa.Column("external_channel_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("provider_status", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("provider IN ('slack','teams')", name="ck_agent_publication_binding_provider"),
        sa.CheckConstraint("status IN ('active','disabled','revoked')", name="ck_agent_publication_binding_status"),
        sa.UniqueConstraint("organization_id", "provider", "external_tenant_id", "external_channel_id", name="uq_agent_publication_binding_channel"),
    )
    op.create_index("ix_agent_publication_bindings_scope", "agent_publication_bindings", ["organization_id", "provider", "status"])

    op.add_column("agent_publications", sa.Column("binding_id", sa.UUID()))
    op.add_column("agent_publications", sa.Column("secret_vault_ref", sa.Text()))
    op.add_column("agent_publications", sa.Column("secret_fingerprint", sa.Text()))
    op.add_column("agent_publications", sa.Column("secret_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_publications", sa.Column("allowed_origins", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("agent_publications", sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("agent_publications", sa.Column("provider_status", sa.Text(), nullable=False, server_default="needs_rotation"))
    op.add_column("agent_publications", sa.Column("last_error_code", sa.Text()))
    op.add_column("agent_publications", sa.Column("last_inbound_at", sa.DateTime(timezone=True)))
    op.add_column("agent_publications", sa.Column("last_outbound_at", sa.DateTime(timezone=True)))
    op.add_column("agent_publications", sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")))
    op.add_column("agent_publications", sa.Column("unpublished_at", sa.DateTime(timezone=True)))
    op.add_column("agent_publications", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    # Legacy plaintext publication tokens cannot be safely re-keyed in SQL.
    # Existing publications are explicitly degraded and must be rotated once.
    op.drop_column("agent_publications", "inbound_token")
    op.create_foreign_key("fk_agent_publication_binding", "agent_publications", "agent_publication_bindings", ["binding_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint("ck_agent_publication_rate", "agent_publications", "rate_limit_per_minute BETWEEN 1 AND 600")
    op.create_check_constraint("ck_agent_publication_provider_status", "agent_publications", "provider_status IN ('ready','degraded','needs_rotation','revoked')")
    op.create_index("ix_agent_publications_binding", "agent_publications", ["binding_id"])
    op.create_index(
        "uq_agent_publications_active_binding",
        "agent_publications",
        ["binding_id"],
        unique=True,
        postgresql_where=sa.text("binding_id IS NOT NULL AND status = 'active'"),
    )
    op.create_index(
        "uq_agent_publications_active_email",
        "agent_publications",
        ["organization_id", "external_channel_id"],
        unique=True,
        postgresql_where=sa.text("target = 'email' AND status = 'active'"),
    )

    op.create_table(
        "agent_publication_links",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("external_conversation_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("last_task_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["publication_id"], ["agent_publications.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("publication_id", "external_conversation_id", name="uq_agent_publication_external_conversation"),
    )
    op.create_index("ix_agent_publication_links_conversation", "agent_publication_links", ["organization_id", "conversation_id"])

    op.create_table(
        "agent_publication_inbound_events",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("external_conversation_id", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text()),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="received"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["publication_id"], ["agent_publications.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("publication_id", "external_event_id", name="uq_agent_publication_event_replay"),
        sa.CheckConstraint("status IN ('received','queued','rejected')", name="ck_agent_publication_inbound_status"),
    )
    op.create_index("ix_agent_publication_inbound_scope", "agent_publication_inbound_events", ["organization_id", "publication_id", "created_at"])

    op.drop_constraint("ck_notification_delivery_kind", "notification_delivery_receipts", type_="check")
    op.drop_constraint("ck_notification_delivery_channel", "notification_delivery_receipts", type_="check")
    op.add_column("notification_delivery_receipts", sa.Column("publication_id", sa.UUID()))
    op.add_column("notification_delivery_receipts", sa.Column("binding_id", sa.UUID()))
    op.add_column("notification_delivery_receipts", sa.Column("task_id", sa.Text()))
    op.add_column("notification_delivery_receipts", sa.Column("external_conversation_id", sa.Text()))
    op.add_column("notification_delivery_receipts", sa.Column("approval_id", sa.Text()))
    op.add_column("notification_delivery_receipts", sa.Column("provider_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_foreign_key("fk_notification_delivery_publication", "notification_delivery_receipts", "agent_publications", ["publication_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_notification_delivery_binding", "notification_delivery_receipts", "agent_publication_bindings", ["binding_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_notification_delivery_approval", "notification_delivery_receipts", "approvals", ["approval_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint("ck_notification_delivery_kind", "notification_delivery_receipts", "delivery_kind IN ('notification','weekly_digest','agent_response')")
    op.create_check_constraint("ck_notification_delivery_channel", "notification_delivery_receipts", "channel IN ('email','slack','teams','web','api')")
    op.drop_constraint("ck_notification_delivery_status", "notification_delivery_receipts", type_="check")
    op.create_check_constraint(
        "ck_notification_delivery_status",
        "notification_delivery_receipts",
        "status IN ('approval_pending','pending','processing','retry','delivered','dead_letter')",
    )
    op.create_index("ix_notification_delivery_publication", "notification_delivery_receipts", ["organization_id", "publication_id", "status", "created_at"])
    op.create_index("ix_notification_delivery_approval", "notification_delivery_receipts", ["organization_id", "approval_id"])


def downgrade() -> None:
    op.execute("DELETE FROM notification_delivery_receipts WHERE delivery_kind = 'agent_response' OR channel <> 'email'")
    op.drop_index("ix_notification_delivery_approval", table_name="notification_delivery_receipts")
    op.drop_index("ix_notification_delivery_publication", table_name="notification_delivery_receipts")
    op.execute(
        "UPDATE notification_delivery_receipts SET status = 'dead_letter', "
        "last_error_code = COALESCE(last_error_code, 'approval_state_removed') "
        "WHERE status = 'approval_pending'"
    )
    op.drop_constraint("ck_notification_delivery_status", "notification_delivery_receipts", type_="check")
    op.create_check_constraint(
        "ck_notification_delivery_status",
        "notification_delivery_receipts",
        "status IN ('pending','processing','retry','delivered','dead_letter')",
    )
    op.drop_constraint("ck_notification_delivery_channel", "notification_delivery_receipts", type_="check")
    op.drop_constraint("ck_notification_delivery_kind", "notification_delivery_receipts", type_="check")
    op.drop_constraint("fk_notification_delivery_binding", "notification_delivery_receipts", type_="foreignkey")
    op.drop_constraint("fk_notification_delivery_approval", "notification_delivery_receipts", type_="foreignkey")
    op.drop_constraint("fk_notification_delivery_publication", "notification_delivery_receipts", type_="foreignkey")
    for column in ("provider_payload", "approval_id", "external_conversation_id", "task_id", "binding_id", "publication_id"):
        op.drop_column("notification_delivery_receipts", column)
    op.create_check_constraint("ck_notification_delivery_channel", "notification_delivery_receipts", "channel = 'email'")
    op.create_check_constraint("ck_notification_delivery_kind", "notification_delivery_receipts", "delivery_kind IN ('notification','weekly_digest')")
    op.drop_index("ix_agent_publication_inbound_scope", table_name="agent_publication_inbound_events")
    op.drop_table("agent_publication_inbound_events")
    op.drop_index("ix_agent_publication_links_conversation", table_name="agent_publication_links")
    op.drop_table("agent_publication_links")
    op.drop_index("ix_agent_publications_binding", table_name="agent_publications")
    op.drop_index("uq_agent_publications_active_email", table_name="agent_publications")
    op.drop_index("uq_agent_publications_active_binding", table_name="agent_publications")
    op.drop_constraint("ck_agent_publication_provider_status", "agent_publications", type_="check")
    op.drop_constraint("ck_agent_publication_rate", "agent_publications", type_="check")
    op.drop_constraint("fk_agent_publication_binding", "agent_publications", type_="foreignkey")
    op.add_column("agent_publications", sa.Column("inbound_token", sa.Text(), nullable=False, server_default=""))
    for column in ("revoked_at", "unpublished_at", "published_at", "last_outbound_at", "last_inbound_at", "last_error_code", "provider_status", "rate_limit_per_minute", "allowed_origins", "secret_version", "secret_fingerprint", "secret_vault_ref", "binding_id"):
        op.drop_column("agent_publications", column)
    op.drop_index("ix_agent_publication_bindings_scope", table_name="agent_publication_bindings")
    op.drop_table("agent_publication_bindings")
