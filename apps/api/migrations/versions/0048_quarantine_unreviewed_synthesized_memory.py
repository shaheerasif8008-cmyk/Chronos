"""quarantine legacy unreviewed synthesized organization memory.

Revision ID: 0048_quarantine_synth_memory
Revises: 0047_connector_member_scope

Historical profile synthesis scanned organization-wide conversation transcripts
and directly persisted model output as shared memory.  New code stages context
changes for explicit review; this migration makes pre-existing unreviewed rows
invisible while preserving them and their provenance for audit/rollback.
"""

from alembic import op


revision = "0048_quarantine_synth_memory"
down_revision = "0047_connector_member_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_entries
        ADD COLUMN IF NOT EXISTS is_quarantined BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute(
        """
        INSERT INTO audit_log (
            organization_id,
            region,
            event_type,
            actor_id,
            action,
            resource_type,
            payload,
            decision
        )
        SELECT
            organization_id,
            COALESCE(MIN(region), 'us'),
            'memory_quarantine',
            'migration:0048',
            'memory.quarantine_unreviewed_synthesis',
            'memory_entries',
            json_build_object('quarantined_count', COUNT(*)),
            'privacy_boundary_enforced'
        FROM memory_entries
        WHERE source = 'synthesized'
          AND is_deleted = FALSE
          AND is_quarantined = FALSE
        GROUP BY organization_id
        """
    )
    op.execute(
        """
        UPDATE memory_entries
        SET is_quarantined = TRUE,
            is_deleted = TRUE
        WHERE source = 'synthesized'
          AND is_deleted = FALSE
          AND is_quarantined = FALSE
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_memory_entries_org_quarantined
        ON memory_entries (organization_id, is_quarantined)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO audit_log (
            organization_id,
            region,
            event_type,
            actor_id,
            action,
            resource_type,
            payload,
            decision
        )
        SELECT
            organization_id,
            COALESCE(MIN(region), 'us'),
            'memory_quarantine_reverted',
            'migration:0048',
            'memory.restore_quarantined_synthesis',
            'memory_entries',
            json_build_object('restored_count', COUNT(*)),
            'migration_downgrade'
        FROM memory_entries
        WHERE is_quarantined = TRUE
        GROUP BY organization_id
        """
    )
    # Only rows marked by this migration are restored; memories that were
    # already deleted before upgrade never received the marker.
    op.execute(
        """
        UPDATE memory_entries
        SET is_deleted = FALSE
        WHERE is_quarantined = TRUE
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_memory_entries_org_quarantined")
    op.execute("ALTER TABLE memory_entries DROP COLUMN IF EXISTS is_quarantined")
