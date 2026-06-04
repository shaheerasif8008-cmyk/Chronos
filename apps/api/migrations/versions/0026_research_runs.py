"""research_runs, research_citations, research_events tables

Revision ID: 0026_research_runs
Revises: 0025_task_dead_letter
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_research_runs"
down_revision = "0025_task_dead_letter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("persona_id", sa.UUID(), nullable=True),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("depth", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("source_scopes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("citation_policy", sa.Text(), nullable=True, server_default="required"),
        sa.Column("time_budget_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("plan", JSONB(), nullable=True),
        sa.Column("findings", JSONB(), nullable=True),
        sa.Column("limitations", sa.Text(), nullable=True),
        sa.Column("report_artifact_id", sa.UUID(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("cost_estimate", sa.Float(), nullable=True, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_runs_org_created",
        "research_runs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_research_runs_org_status",
        "research_runs",
        ["organization_id", "status"],
    )

    op.create_table(
        "research_citations",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("marker", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("distance", sa.Float(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_citations_run",
        "research_citations",
        ["organization_id", "run_id"],
    )

    op.create_table(
        "research_events",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_events_run_seq",
        "research_events",
        ["organization_id", "run_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_events_run_seq", table_name="research_events")
    op.drop_table("research_events")
    op.drop_index("ix_research_citations_run", table_name="research_citations")
    op.drop_table("research_citations")
    op.drop_index("ix_research_runs_org_status", table_name="research_runs")
    op.drop_index("ix_research_runs_org_created", table_name="research_runs")
    op.drop_table("research_runs")
