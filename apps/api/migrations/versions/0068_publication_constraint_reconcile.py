"""Reconcile publication delivery checks on already-stamped databases.

Revision ID: 0068_publication_reconcile
Revises: 0067_file_quarantine_review

Migration 0064 originally shipped before ``web``/``api`` publication delivery
and approval-gated replies were added to its source. Databases that had already
applied that revision kept the narrower checks even though Alembic reported
head. This forward-only reconciliation deliberately repairs those installations
without rewriting their recorded migration history.
"""

from alembic import op
import sqlalchemy as sa


revision = "0068_publication_reconcile"
down_revision = "0067_file_quarantine_review"
branch_labels = None
depends_on = None


_TABLE = "notification_delivery_receipts"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
    }


def _check_constraint_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
        if constraint.get("name")
    }


def _foreign_key_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(_TABLE)
        if constraint.get("name")
    }


def _index_names() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name")
    }


def _replace_check(name: str, condition: str) -> None:
    # Inspect on every replacement so the function is safe both on a fresh
    # chain and on an already-stamped database carrying the historical check.
    if name in _check_constraint_names():
        op.drop_constraint(name, _TABLE, type_="check")
    op.create_check_constraint(name, _TABLE, condition)


def upgrade() -> None:
    # The approval linkage was added to the 0064 source after some databases
    # had already applied that revision. Reconcile the full contract here as
    # well as the two check constraints that exposed the drift first.
    if "approval_id" not in _column_names():
        op.add_column(_TABLE, sa.Column("approval_id", sa.Text()))
    if "fk_notification_delivery_approval" not in _foreign_key_names():
        op.create_foreign_key(
            "fk_notification_delivery_approval",
            _TABLE,
            "approvals",
            ["approval_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_notification_delivery_approval" not in _index_names():
        op.create_index(
            "ix_notification_delivery_approval",
            _TABLE,
            ["organization_id", "approval_id"],
        )
    _replace_check(
        "ck_notification_delivery_channel",
        "channel IN ('email','slack','teams','web','api')",
    )
    _replace_check(
        "ck_notification_delivery_status",
        "status IN ('approval_pending','pending','processing','retry','delivered','dead_letter')",
    )


def downgrade() -> None:
    # The canonical 0064-0067 source already declares this expanded contract.
    # Re-introducing the historical checks would make those revisions unable to
    # serve their own web/API and approval-pending rows, so downgrade is a
    # forward-compatible no-op by design.
    pass
