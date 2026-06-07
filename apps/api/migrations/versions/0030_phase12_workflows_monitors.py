"""phase 12 scheduled workflows monitors revision alias

Revision ID: 0030_phase12_workflows_monitors
Revises: 0030_phase12_sched_wf_monitors
Create Date: 2026-06-07

Existing local databases may be stamped with this earlier revision id. The
actual DDL lives in ``0030_phase12_sched_wf_monitors``; this revision is a
no-op bridge so those databases can continue upgrading.
"""

revision = "0030_phase12_workflows_monitors"
down_revision = "0030_phase12_sched_wf_monitors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
