"""Document the already-clean Article schema at the migration head.

The repository is greenfield: ``0001_baseline`` creates the target schema
directly, so this revision intentionally performs no data migration or
legacy-state inspection.
"""

from alembic import op


revision = "0017_unified_article_production"
down_revision = "0016_edition_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0003 keeps the independent AnalystInvestigation tables but temporarily
    # widened the SubjectProductionRun stage check.  The current pipeline is
    # deliberately narrower; this changes only the target schema and never
    # reads or rewrites existing rows.
    op.drop_constraint("ck_run_stage", "subject_production_runs", type_="check")
    op.create_check_constraint(
        "ck_run_stage",
        "subject_production_runs",
        "current_stage IN ('sources', 'references', 'extraction', 'synthesis', 'assembly')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_stage", "subject_production_runs", type_="check")
    op.create_check_constraint(
        "ck_run_stage",
        "subject_production_runs",
        "current_stage IN ('sources', 'references', 'extraction', 'synthesis', "
        "'analyst_research', 'analyst_note', 'assembly')",
    )
