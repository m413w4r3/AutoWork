"""Unify editorial selection and production around one article pipeline."""

from alembic import op
import sqlalchemy as sa


revision = "0017_unified_article_production"
down_revision = "0016_edition_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Analyst stages were never part of the new pipeline. Refuse to silently
    # strand an active legacy run; terminal evidence remains readable after the
    # profile column is removed.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM subject_production_runs
                    WHERE status NOT IN ('ready', 'needs_review', 'failed', 'cancelled')
                      AND (
                          profile = 'major_assisted'
                          OR current_stage IN ('analyst_research', 'analyst_note')
                      )
                ) THEN
                    RAISE EXCEPTION
                        'cannot unify production: active major_assisted or analyst-stage run exists';
                END IF;
            END $$;
            """
        )
    )

    op.add_column("editions", sa.Column("target_articles", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE editions SET target_articles = target_major_articles + target_briefs"
        )
    )
    op.alter_column("editions", "target_articles", nullable=False)
    op.create_check_constraint(
        "ck_editions_articles", "editions", "target_articles BETWEEN 0 AND 120"
    )
    op.drop_constraint("ck_editions_major", "editions", type_="check")
    op.drop_constraint("ck_editions_briefs", "editions", type_="check")
    op.drop_column("editions", "target_major_articles")
    op.drop_column("editions", "target_briefs")

    op.drop_constraint("ck_editorial_groups_type", "editorial_groups", type_="check")
    op.drop_column("editorial_groups", "editorial_type")

    op.drop_constraint("ck_run_profile", "subject_production_runs", type_="check")
    op.drop_column("subject_production_runs", "profile")
    op.drop_constraint("ck_batch_profile", "edition_production_batches", type_="check")
    op.drop_column("edition_production_batches", "profile")

    op.drop_constraint("ck_artifact_stage", "production_artifacts", type_="check")
    op.create_check_constraint(
        "ck_artifact_stage",
        "production_artifacts",
        "stage IN ('references', 'extraction', 'synthesis', 'publication', 'brief')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM production_artifacts WHERE stage = 'publication') THEN
                    RAISE EXCEPTION
                        'cannot downgrade unified production while PUBLICATION artifacts exist';
                END IF;
            END $$;
            """
        )
    )
    op.drop_constraint("ck_artifact_stage", "production_artifacts", type_="check")
    op.create_check_constraint(
        "ck_artifact_stage",
        "production_artifacts",
        "stage IN ('references', 'extraction', 'synthesis', 'brief')",
    )
    op.add_column(
        "edition_production_batches",
        sa.Column("profile", sa.String(length=32), nullable=True),
    )
    op.execute(
        sa.text("UPDATE edition_production_batches SET profile = 'brief_auto'")
    )
    op.alter_column("edition_production_batches", "profile", nullable=False)
    op.create_check_constraint(
        "ck_batch_profile",
        "edition_production_batches",
        "profile IN ('brief_auto', 'major_assisted')",
    )

    op.add_column(
        "subject_production_runs", sa.Column("profile", sa.String(length=32), nullable=True)
    )
    op.execute(sa.text("UPDATE subject_production_runs SET profile = 'brief_auto'"))
    op.alter_column("subject_production_runs", "profile", nullable=False)
    op.create_check_constraint(
        "ck_run_profile",
        "subject_production_runs",
        "profile IN ('brief_auto', 'major_assisted')",
    )

    op.add_column("editorial_groups", sa.Column("editorial_type", sa.String(length=32)))
    op.create_check_constraint(
        "ck_editorial_groups_type",
        "editorial_groups",
        "editorial_type IS NULL OR editorial_type IN ('brief', 'major')",
    )

    op.add_column("editions", sa.Column("target_major_articles", sa.BigInteger(), nullable=True))
    op.add_column("editions", sa.Column("target_briefs", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE editions
            SET target_major_articles = LEAST(target_articles, 20),
                target_briefs = target_articles - LEAST(target_articles, 20)
            """
        )
    )
    op.alter_column("editions", "target_major_articles", nullable=False)
    op.alter_column("editions", "target_briefs", nullable=False)
    op.create_check_constraint(
        "ck_editions_major", "editions", "target_major_articles BETWEEN 0 AND 20"
    )
    op.create_check_constraint(
        "ck_editions_briefs", "editions", "target_briefs BETWEEN 0 AND 100"
    )
    op.drop_constraint("ck_editions_articles", "editions", type_="check")
    op.drop_column("editions", "target_articles")
