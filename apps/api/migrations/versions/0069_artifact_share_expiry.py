"""Expire public artifact bearer links by default.

Revision ID: 0069_artifact_share_expiry
Revises: 0068_publication_reconcile

Public share tokens are credentials. Historical rows were revocable but had no
finite lifetime, so this forward migration gives every existing active link a
seven-day window from migration time and records an explicit expiry for all new
links. Revoked historical rows remain nullable because they can never become
active again.
"""

from alembic import op
import sqlalchemy as sa


revision = "0069_artifact_share_expiry"
down_revision = "0068_publication_reconcile"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("artifact_shares")
    }


def _index_names() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("artifact_shares")
        if index.get("name")
    }


def _constraint_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("artifact_shares")
        if constraint.get("name")
    }


def upgrade() -> None:
    if "expires_at" not in _column_names():
        op.add_column(
            "artifact_shares",
            sa.Column(
                "expires_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                # Keep old application instances safe during a rolling deploy:
                # inserts that do not yet send expires_at still get a finite TTL.
                server_default=sa.text("(NOW() + INTERVAL '7 days')"),
            ),
        )
    else:
        # This also repairs databases that ran an earlier development revision
        # of 0069 before the finite server default was added.
        op.alter_column(
            "artifact_shares",
            "expires_at",
            server_default=sa.text("(NOW() + INTERVAL '7 days')"),
        )
    op.execute(
        """
        UPDATE artifact_shares
           SET expires_at = NOW() + INTERVAL '7 days'
         WHERE status = 'active'
           AND expires_at IS NULL
        """
    )
    if "ck_artifact_shares_active_expires" not in _constraint_names():
        op.create_check_constraint(
            "ck_artifact_shares_active_expires",
            "artifact_shares",
            "status <> 'active' OR expires_at IS NOT NULL",
        )
    if "ix_artifact_shares_expiry" not in _index_names():
        op.create_index(
            "ix_artifact_shares_expiry",
            "artifact_shares",
            ["status", "expires_at"],
        )


def downgrade() -> None:
    # Removing expiry would silently make bearer links permanent again. Keep the
    # safety column and index on downgrade instead of widening public access.
    pass
