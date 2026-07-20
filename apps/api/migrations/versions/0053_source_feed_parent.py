"""bind synchronized connector documents to their source feed

Revision ID: 0053_source_feed_parent
Revises: 0052_project_instruction_hist
"""

from alembic import op


revision = "0053_source_feed_parent"
down_revision = "0052_project_instruction_hist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Connector document rows belong to one durable feed. Without this parent
    # link a refresh cannot remove upstream-deleted documents safely when a
    # project has multiple feeds backed by the same connector.
    op.execute(
        "ALTER TABLE project_sources ADD COLUMN IF NOT EXISTS parent_source_id UUID"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_project_sources_parent_source'
            ) THEN
                ALTER TABLE project_sources
                ADD CONSTRAINT fk_project_sources_parent_source
                FOREIGN KEY (parent_source_id) REFERENCES project_sources(id)
                ON DELETE CASCADE;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_project_sources_parent_uri
        ON project_sources (organization_id, parent_source_id, uri)
        WHERE parent_source_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("uq_project_sources_parent_uri", table_name="project_sources")
    op.drop_constraint(
        "fk_project_sources_parent_source",
        "project_sources",
        type_="foreignkey",
    )
    op.drop_column("project_sources", "parent_source_id")
