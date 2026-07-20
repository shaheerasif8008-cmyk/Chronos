"""Operator quarantine review and content-disarm evidence.

Revision ID: 0067_file_quarantine_review
Revises: 0066_conversation_workspaces
"""

from alembic import op
import sqlalchemy as sa


revision = "0067_file_quarantine_review"
down_revision = "0066_conversation_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_file_security_event_source", "file_security_events", type_="check"
    )
    op.create_check_constraint(
        "ck_file_security_event_source",
        "file_security_events",
        "source IN ('attachment','browser_download','browser_upload','connector_sync')",
    )
    op.add_column(
        "file_security_events",
        sa.Column(
            "content_disarm_status",
            sa.Text(),
            nullable=False,
            server_default="not_applicable",
        ),
    )
    op.add_column("file_security_events", sa.Column("content_disarm_reason", sa.Text()))
    op.add_column(
        "file_security_events",
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("file_security_events", sa.Column("review_note", sa.Text()))
    op.add_column("file_security_events", sa.Column("reviewed_by", sa.Text()))
    op.add_column(
        "file_security_events", sa.Column("reviewed_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_file_security_event_disarm_status",
        "file_security_events",
        "content_disarm_status IN ('not_applicable','not_run','safe','sanitized','rejected','error')",
    )
    op.create_check_constraint(
        "ck_file_security_event_review_status",
        "file_security_events",
        "review_status IN ('pending','acknowledged','false_positive','closed')",
    )
    op.execute(
        "UPDATE file_security_events SET review_status = 'closed' "
        "WHERE verdict = 'clean' AND content_disarm_status IN "
        "('not_applicable','safe','sanitized')"
    )
    op.create_index(
        "ix_file_security_events_review_queue",
        "file_security_events",
        ["organization_id", "review_status", "scanned_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_file_security_events_review_queue", table_name="file_security_events"
    )
    op.drop_constraint(
        "ck_file_security_event_review_status",
        "file_security_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_file_security_event_disarm_status",
        "file_security_events",
        type_="check",
    )
    for column in (
        "reviewed_at",
        "reviewed_by",
        "review_note",
        "review_status",
        "content_disarm_reason",
        "content_disarm_status",
    ):
        op.drop_column("file_security_events", column)
    op.drop_constraint(
        "ck_file_security_event_source", "file_security_events", type_="check"
    )
    op.create_check_constraint(
        "ck_file_security_event_source",
        "file_security_events",
        "source IN ('attachment','browser_download','browser_upload')",
    )
