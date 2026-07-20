"""Durable malware-scan evidence for untrusted file ingress.

Revision ID: 0065_file_security
Revises: 0064_agent_publications
"""

from alembic import op
import sqlalchemy as sa


revision = "0065_file_security"
down_revision = "0064_agent_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column(
            "malware_scan_status",
            sa.Text(),
            nullable=False,
            server_default="not_required",
        ),
    )
    op.add_column("artifacts", sa.Column("malware_scan_engine", sa.Text()))
    op.add_column("artifacts", sa.Column("malware_scan_engine_version", sa.Text()))
    op.add_column("artifacts", sa.Column("malware_scan_signature", sa.Text()))
    op.add_column("artifacts", sa.Column("malware_scanned_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_artifacts_malware_scan_status",
        "artifacts",
        "malware_scan_status IN ('not_required','clean','infected','error')",
    )
    op.create_index(
        "ix_artifacts_malware_scan_status",
        "artifacts",
        ["organization_id", "malware_scan_status", "created_at"],
    )

    op.create_table(
        "file_security_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("artifact_id", sa.UUID()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text()),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text()),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text()),
        sa.Column("signature", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_by", sa.Text()),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "source IN ('attachment','browser_download','browser_upload')",
            name="ck_file_security_event_source",
        ),
        sa.CheckConstraint(
            "verdict IN ('clean','infected','error')",
            name="ck_file_security_event_verdict",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_file_security_event_size"),
    )
    op.create_index(
        "ix_file_security_events_scope",
        "file_security_events",
        ["organization_id", "scanned_at"],
    )
    op.create_index(
        "ix_file_security_events_verdict",
        "file_security_events",
        ["organization_id", "verdict", "scanned_at"],
    )
    op.create_index(
        "ix_file_security_events_sha256",
        "file_security_events",
        ["organization_id", "sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_file_security_events_sha256", table_name="file_security_events")
    op.drop_index("ix_file_security_events_verdict", table_name="file_security_events")
    op.drop_index("ix_file_security_events_scope", table_name="file_security_events")
    op.drop_table("file_security_events")
    op.drop_index("ix_artifacts_malware_scan_status", table_name="artifacts")
    op.drop_constraint("ck_artifacts_malware_scan_status", "artifacts", type_="check")
    for column in (
        "malware_scanned_at",
        "malware_scan_signature",
        "malware_scan_engine_version",
        "malware_scan_engine",
        "malware_scan_status",
    ):
        op.drop_column("artifacts", column)
