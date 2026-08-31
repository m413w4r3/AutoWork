"""Current PostgreSQL schema baseline.

The database is reset before this migration is applied. The current ORM
metadata is therefore the complete target schema: it creates every table,
column, constraint and index in one operation, with no historical revisions
or data conversions involved.
"""

from collections.abc import Sequence

from alembic import op

from cti_app.infrastructure.database.models import (  # noqa: F401
    collection,
    core,
    discovery,
    edition_publication,
    editions,
    editorial,
    invariants,
    jobs,
    model_execution,
    production,
    publication_review,
)
from cti_app.infrastructure.database.models.base import Base

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GUARD_FUNCTIONS: tuple[tuple[str, str], ...] = (
    (
        "prevent_tlp_downgrade",
        """
        CREATE FUNCTION prevent_tlp_downgrade() RETURNS trigger AS $$
        DECLARE
            old_rank integer;
            new_rank integer;
        BEGIN
            old_rank := CASE OLD.tlp
                WHEN 'CLEAR' THEN 0 WHEN 'GREEN' THEN 1 WHEN 'AMBER' THEN 2
                WHEN 'AMBER+STRICT' THEN 3 WHEN 'RED' THEN 4 END;
            new_rank := CASE NEW.tlp
                WHEN 'CLEAR' THEN 0 WHEN 'GREEN' THEN 1 WHEN 'AMBER' THEN 2
                WHEN 'AMBER+STRICT' THEN 3 WHEN 'RED' THEN 4 END;
            IF new_rank < old_rank THEN
                RAISE EXCEPTION 'TLP downgrade from % to % is forbidden', OLD.tlp, NEW.tlp
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_provenance_mutation",
        """
        CREATE FUNCTION reject_provenance_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'provenance_events is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_audit_mutation",
        """
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit journals are append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_human_decision_mutation",
        """
        CREATE FUNCTION reject_human_decision_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'human_decisions is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_evidence_mutation",
        """
        CREATE FUNCTION reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_discovery_intakes_mutation",
        """
        CREATE FUNCTION reject_discovery_intakes_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'discovery_intakes is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_subject_merge_events_mutation",
        """
        CREATE FUNCTION reject_subject_merge_events_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'subject_merge_events is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "reject_subject_contributions_mutation",
        """
        CREATE FUNCTION reject_subject_contributions_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'subject_contributions is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
    ),
    (
        "forbid_reference_mutation",
        """
        CREATE FUNCTION forbid_reference_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'reference corpus rows are append-only';
        END;
        $$;
        """,
    ),
)

# ``kind`` is ``tlp`` for the four monotonic classification guards and
# ``append_only`` for all immutable evidence/audit tables.
_TRIGGERS: tuple[tuple[str, str, str, str], ...] = (
    (
        "publication_review_decisions",
        "trg_publication_review_decisions_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "publication_manifests",
        "trg_publication_manifests_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "publication_manifest_entries",
        "trg_publication_manifest_entries_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "publication_manifest_exclusions",
        "trg_publication_manifest_exclusions_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "edition_releases",
        "trg_edition_releases_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "production_input_snapshots",
        "trg_production_input_snapshots_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "production_reuse_invalidations",
        "trg_production_reuse_invalidations_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "virustotal_observations",
        "trg_vt_observations_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "virustotal_file_views",
        "trg_vt_file_views_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    ("subjects", "trg_subjects_prevent_tlp_downgrade", "prevent_tlp_downgrade", "tlp"),
    (
        "source_documents",
        "trg_source_documents_prevent_tlp_downgrade",
        "prevent_tlp_downgrade",
        "tlp",
    ),
    ("samples", "trg_samples_prevent_tlp_downgrade", "prevent_tlp_downgrade", "tlp"),
    ("editions", "trg_editions_prevent_tlp_downgrade", "prevent_tlp_downgrade", "tlp"),
    (
        "provenance_events",
        "trg_provenance_events_append_only",
        "reject_provenance_mutation",
        "append_only",
    ),
    (
        "edition_audit_events",
        "trg_edition_audit_events_append_only",
        "reject_audit_mutation",
        "append_only",
    ),
    ("job_events", "trg_job_events_append_only", "reject_audit_mutation", "append_only"),
    (
        "human_decisions",
        "trg_human_decisions_append_only",
        "reject_human_decision_mutation",
        "append_only",
    ),
    (
        "analyst_decisions",
        "trg_analyst_decisions_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "analyst_input_packs",
        "trg_analyst_input_packs_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "collection_attempts",
        "trg_collection_attempts_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "derived_artifacts",
        "trg_derived_artifacts_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    ("claims", "trg_claims_append_only", "reject_evidence_mutation", "append_only"),
    ("indicators", "trg_indicators_append_only", "reject_evidence_mutation", "append_only"),
    (
        "collection_policy_snapshots",
        "trg_collection_policy_snapshots_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "rejected_model_proposals",
        "trg_rejected_model_proposals_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    (
        "discovery_intakes",
        "trg_discovery_intakes_append_only",
        "reject_discovery_intakes_mutation",
        "append_only",
    ),
    (
        "subject_merge_events",
        "trg_subject_merge_events_append_only",
        "reject_subject_merge_events_mutation",
        "append_only",
    ),
    (
        "subject_contributions",
        "trg_subject_contributions_append_only",
        "reject_subject_contributions_mutation",
        "append_only",
    ),
    (
        "reference_members",
        "reference_members_immutable",
        "forbid_reference_mutation",
        "append_only",
    ),
    (
        "reference_member_disputes",
        "reference_member_disputes_immutable",
        "forbid_reference_mutation",
        "append_only",
    ),
)


def upgrade() -> None:
    # This is intentionally a fresh-schema operation. No revision checks,
    # ALTER/backfill path, or old-state inspection belongs in the reset.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for _function_name, definition in _GUARD_FUNCTIONS:
        op.execute(definition)
    for table, trigger_name, function_name, kind in _TRIGGERS:
        if kind == "tlp":
            op.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OF tlp ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )
        else:
            op.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )


def downgrade() -> None:
    for table, trigger_name, _function_name, _kind in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {trigger_name} ON {table}")
    # The current metadata intentionally contains a small mutual-FK cycle
    # between source_collections and collection_attempts. PostgreSQL can drop
    # the fresh schema deterministically when the tables are removed with
    # CASCADE; this is only the inverse of the empty-database baseline.
    for table in reversed(tuple(Base.metadata.tables.values())):
        op.execute(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
    for function_name, _definition in reversed(_GUARD_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
