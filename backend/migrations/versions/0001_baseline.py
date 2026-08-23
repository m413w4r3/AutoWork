"""Baseline schema, squashed from the former 0001-0023 migration chain.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-22

[R07a] This single migration reproduces the exact schema that the former
23-migration chain (0001..0023) produced on ``upgrade head``. The historical
equivalence was verified before the schema entered its new canonical phase.
It is a *squash*, not a cleanup: every drift, legacy shape and inconsistency
the old chain accumulated (unnamed vs. named constraints, JSON vs. JSONB,
uuid[] vs. JSONB, historically-added-nullable-then-altered columns, etc.)
is deliberately preserved as-is. No schema decision was made here; see the
individual (now-deleted) migrations' history for *why* each shape looks the
way it does.

Tables are created in an order chosen to satisfy foreign keys directly
wherever possible. Three foreign keys are genuinely circular (each side
needs the other table to exist first) and are therefore added via a
deferred ``ALTER TABLE`` after both sides exist, exactly as the original
chain did:

- ``source_collections.latest_attempt_id`` <-> ``collection_attempts.collection_id``
- ``discovery_merge_runs.parent_snapshot_id`` <-> ``discovery_snapshots.merge_run_id``
- ``model_conversations.head_turn_id`` <-> ``model_conversation_turns.conversation_id``
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TLP_CHECK = "tlp IN ('CLEAR', 'GREEN', 'AMBER', 'AMBER+STRICT', 'RED')"
JOB_STATUS_VALUES = "'queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled'"

# Guard functions, in their final (post 0013 / 0019) form.
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
)

# (table, trigger_name, function_name, trigger_kind) -- trigger_kind is
# "tlp" (BEFORE UPDATE OF tlp) or "append_only" (BEFORE UPDATE OR DELETE).
_TRIGGERS: tuple[tuple[str, str, str, str], ...] = (
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
        "brief_evidence_packs",
        "trg_brief_evidence_packs_append_only",
        "reject_evidence_mutation",
        "append_only",
    ),
    ("brief_drafts", "trg_brief_drafts_append_only", "reject_evidence_mutation", "append_only"),
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
)

# Tables in the exact order they are created (and, for downgrade, the exact
# reverse order they must be dropped in to satisfy foreign keys).
_TABLES_IN_CREATION_ORDER: tuple[str, ...] = (
    "blobs",
    "subjects",
    "samples",
    "provenance_events",
    "jobs",
    "editions",
    "edition_audit_events",
    "job_events",
    "model_runs",
    "discovery_batches",
    "discovery_intakes",
    "discovery_merge_runs",
    "discovery_subject_identities",
    "discovery_snapshots",
    "editorial_groups",
    "human_decisions",
    "subject_merge_events",
    "subject_contributions",
    "collection_policy_snapshots",
    "source_documents",
    "derived_artifacts",
    "source_collections",
    "collection_attempts",
    "claims",
    "indicators",
    "rejected_model_proposals",
    "brief_evidence_packs",
    "brief_drafts",
    "model_conversations",
    "model_conversation_turns",
    "model_output_rejections",
    "subject_production_runs",
    "production_artifacts",
    "edition_production_batches",
    "edition_production_batch_items",
    "conversation_lifecycles",
)


def upgrade() -> None:
    for _function_name, definition in _GUARD_FUNCTIONS:
        op.execute(definition)

    _create_blobs()
    _create_subjects()
    _create_samples()
    _create_provenance_events()
    _create_jobs()
    _create_editions()
    _create_edition_audit_events()
    _create_job_events()
    _create_model_runs()
    _create_discovery_batches()
    _create_discovery_intakes()
    _create_discovery_merge_runs()
    _create_discovery_subject_identities()
    _create_discovery_snapshots()
    op.create_foreign_key(
        "fk_discovery_merge_runs_parent_snapshot",
        "discovery_merge_runs",
        "discovery_snapshots",
        ["parent_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_editorial_groups()
    _create_human_decisions()
    _create_subject_merge_events()
    _create_subject_contributions()
    _create_collection_policy_snapshots()
    _create_source_documents()
    _create_derived_artifacts()
    _create_source_collections()
    _create_collection_attempts()
    op.create_foreign_key(
        "fk_source_collections_latest_attempt",
        "source_collections",
        "collection_attempts",
        ["latest_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_claims()
    _create_indicators()
    _create_rejected_model_proposals()
    _create_brief_evidence_packs()
    _create_brief_drafts()
    _create_model_conversations()
    _create_model_conversation_turns()
    op.create_foreign_key(
        "fk_model_conversations_head_turn",
        "model_conversations",
        "model_conversation_turns",
        ["head_turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_model_output_rejections()
    _create_subject_production_runs()
    _create_production_artifacts()
    _create_edition_production_batches()
    _create_edition_production_batch_items()
    _create_conversation_lifecycles()

    for table, trigger_name, function_name, kind in _TRIGGERS:
        _install_trigger(table, trigger_name, function_name, kind)


def downgrade() -> None:
    op.drop_constraint(
        "fk_source_collections_latest_attempt", "source_collections", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_discovery_merge_runs_parent_snapshot", "discovery_merge_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_model_conversations_head_turn", "model_conversations", type_="foreignkey"
    )
    for table in reversed(_TABLES_IN_CREATION_ORDER):
        op.drop_table(table)
    for function_name, _definition in reversed(_GUARD_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {function_name}()")


def _install_trigger(table: str, trigger_name: str, function_name: str, kind: str) -> None:
    if kind == "tlp":
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OF tlp ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function_name}()
            """
        )
    else:
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function_name}()
            """
        )


# ---------------------------------------------------------------------------
# Table definitions, in creation order
# ---------------------------------------------------------------------------


def _create_blobs() -> None:
    op.create_table(
        "blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("logical_bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size >= 0", name="ck_blobs_size_non_negative"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_blobs_sha256_length"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blobs_sha256_format"),
        sa.CheckConstraint(
            "logical_bucket ~ '^[a-z0-9][a-z0-9._-]{0,62}$'",
            name="ck_blobs_logical_bucket_format",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("logical_bucket", "sha256", name="uq_blobs_bucket_sha256"),
        sa.UniqueConstraint("object_key", name="uq_blobs_object_key"),
    )


def _create_subjects() -> None:
    op.create_table(
        "subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_subjects_tlp"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_subjects_slug_format"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
        sa.UniqueConstraint("slug"),
    )


def _create_samples() -> None:
    op.create_table(
        "samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("license_restriction", sa.Text(), nullable=True),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("do_not_submit", sa.Boolean(), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_samples_tlp"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_samples_subject_id", "samples", ["subject_id"])
    op.create_index("ix_samples_blob_id", "samples", ["blob_id"])


def _create_provenance_events() -> None:
    op.create_table(
        "provenance_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_provenance_events_tlp"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provenance_events_aggregate",
        "provenance_events",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )
    op.create_index("ix_provenance_events_subject_id", "provenance_events", ["subject_id"])


def _create_jobs() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_current", sa.BigInteger(), nullable=False),
        sa.Column("progress_total", sa.BigInteger(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("max_attempts", sa.BigInteger(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("input_parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_reference", sa.Text(), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(f"status IN ({JOB_STATUS_VALUES})", name="ck_jobs_status"),
        sa.CheckConstraint("progress_current >= 0", name="ck_jobs_progress_current"),
        sa.CheckConstraint("progress_total >= 0", name="ck_jobs_progress_total"),
        sa.CheckConstraint(
            "progress_total = 0 OR progress_current <= progress_total",
            name="ck_jobs_progress_bounds",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_jobs_max_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_status_next_retry", "jobs", ["status", "next_retry_at"])
    op.create_index("ix_jobs_running_heartbeat", "jobs", ["status", "heartbeat_at"])
    op.create_index("ix_jobs_aggregate", "jobs", ["aggregate_type", "aggregate_id"])


def _create_editions() -> None:
    op.create_table(
        "editions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_major_articles", sa.BigInteger(), nullable=False),
        sa.Column("target_briefs", sa.BigInteger(), nullable=False),
        sa.Column("previous_edition_id", sa.Uuid(), nullable=True),
        sa.Column("source_profile", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_editions_tlp"),
        sa.CheckConstraint(
            "status IN ('draft', 'discovery', 'selection', 'production', 'review', "
            "'assembling', 'published', 'archived')",
            name="ck_editions_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_editions_version"),
        sa.CheckConstraint("target_major_articles BETWEEN 0 AND 20", name="ck_editions_major"),
        sa.CheckConstraint("target_briefs BETWEEN 0 AND 100", name="ck_editions_briefs"),
        sa.CheckConstraint("period_start <= period_end", name="ck_editions_period_order"),
        sa.CheckConstraint(
            "period_start = date_trunc('month', period_start)::date "
            "AND period_end = (date_trunc('month', period_start) + "
            "interval '1 month - 1 day')::date",
            name="ck_editions_complete_month",
        ),
        sa.CheckConstraint("jsonb_typeof(languages) = 'array'", name="ck_editions_languages"),
        sa.ForeignKeyConstraint(["previous_edition_id"], ["editions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "country_code", "period_start", "period_end", name="uq_editions_country_period"
        ),
    )
    op.create_index("ix_editions_country_status", "editions", ["country_code", "status"])
    op.create_index("ix_editions_period", "editions", ["period_start", "period_end"])


def _create_edition_audit_events() -> None:
    op.create_table(
        "edition_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_edition_audit_edition", "edition_audit_events", ["edition_id", "occurred_at"]
    )


def _create_job_events() -> None:
    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"to_status IN ({JOB_STATUS_VALUES})", name="ck_job_events_to_status"),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({JOB_STATUS_VALUES})",
            name="ck_job_events_from_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_events_job", "job_events", ["job_id", "occurred_at"])


def _create_model_runs() -> None:
    op.create_table(
        "model_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_role", sa.String(length=32), nullable=False),
        sa.Column("requested_model", sa.String(length=128), nullable=False),
        sa.Column("actual_model_version", sa.String(length=255), nullable=True),
        sa.Column("prompt_template_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=64), nullable=False),
        sa.Column("authorized_input_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_pack_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=True),
        sa.Column("output_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_output_reference", sa.Text(), nullable=True),
        sa.Column("raw_output_sha256", sa.String(64), nullable=True),
        sa.Column("raw_output_chars", sa.BigInteger(), nullable=True),
        sa.Column("normalized_output_reference", sa.Text(), nullable=True),
        sa.Column("normalized_output_sha256", sa.String(64), nullable=True),
        sa.Column("parser_stage", sa.String(64), nullable=True),
        sa.Column("serializer_version", sa.String(64), nullable=True),
        sa.Column("normalization_version", sa.String(64), nullable=True),
        sa.Column("json_error_line", sa.BigInteger(), nullable=True),
        sa.Column("json_error_column", sa.BigInteger(), nullable=True),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "transformations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "citation_count", sa.BigInteger(), nullable=False, server_default=sa.text("'0'")
        ),
        sa.Column(
            "extracted_url_count", sa.BigInteger(), nullable=False, server_default=sa.text("'0'")
        ),
        sa.Column(
            "visible_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.CheckConstraint("provider IN ('openai', 'qwen', 'fake')", name="ck_model_runs_provider"),
        sa.CheckConstraint(
            "model_role IN ('research', 'structured_extraction', 'drafting', 'critic')",
            name="ck_model_runs_role",
        ),
        sa.CheckConstraint(
            "status IN ('running','waiting_background','needs_review','succeeded','failed',"
            "'blocked')",
            name="ck_model_runs_status",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_model_runs_duration"
        ),
        sa.CheckConstraint(
            "char_length(authorized_input_hash) = 64",
            name="ck_model_runs_input_hash_length",
        ),
        sa.CheckConstraint(
            "char_length(evidence_pack_hash) = 64",
            name="ck_model_runs_evidence_hash_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameters) = 'object'", name="ck_model_runs_parameters_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(output_references) = 'array'",
            name="ck_model_runs_output_references_array",
        ),
        sa.CheckConstraint(
            "(raw_output_chars IS NULL OR raw_output_chars >= 0) "
            "AND citation_count >= 0 AND extracted_url_count >= 0",
            name="ck_model_runs_output_diagnostic_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_id", name="uq_model_runs_response_id"),
    )
    op.create_index("ix_model_runs_status", "model_runs", ["status", "updated_at"])
    op.create_index("ix_model_runs_evidence", "model_runs", ["evidence_pack_hash", "started_at"])


def _create_discovery_batches() -> None:
    op.create_table(
        "discovery_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("complementary_axis", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("discovery_model_run_id", sa.Uuid(), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("sensitivity", sa.String(length=64), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_discovery_batches_tlp"),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_discovery_batches_request_hash",
        ),
        sa.CheckConstraint("status = 'completed'", name="ck_discovery_batches_status"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_discovery_payload_object"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["discovery_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "request_hash", name="uq_discovery_batches_request"),
    )
    op.create_index(
        "ix_discovery_batches_edition", "discovery_batches", ["edition_id", "created_at"]
    )


def _create_discovery_intakes() -> None:
    op.create_table(
        "discovery_intakes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("input_mode", sa.String(length=32), nullable=False),
        sa.Column("raw_report_hash", sa.String(length=64), nullable=False),
        sa.Column("parsed_report_hash", sa.String(length=64), nullable=False),
        sa.Column("intake_hash", sa.String(length=64), nullable=False),
        sa.Column("research_model_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("complementary_axis", sa.String(length=500), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_discovery_intakes_sequence"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["research_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["discovery_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "sequence", name="uq_discovery_intakes_sequence"),
        sa.UniqueConstraint("edition_id", "intake_hash", name="uq_discovery_intakes_hash"),
        sa.UniqueConstraint("batch_id", name="uq_discovery_intakes_batch"),
    )
    op.create_index("ix_discovery_intakes_edition", "discovery_intakes", ["edition_id", "sequence"])


def _create_discovery_merge_runs() -> None:
    op.create_table(
        "discovery_merge_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("planner_kind", sa.String(length=32), nullable=False),
        sa.Column("merge_model_run_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("blocking_version", sa.String(length=64), nullable=False),
        sa.Column("merge_input_hash", sa.String(length=64), nullable=False),
        sa.Column("handle_map", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("included_subject_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded_subject_count", sa.Integer(), nullable=False),
        sa.Column("raw_output_reference", sa.Text(), nullable=True),
        sa.Column("normalized_output_reference", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rebase_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "review_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("supersedes_merge_run_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("rebase_count BETWEEN 0 AND 2", name="ck_discovery_merge_runs_rebase"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merge_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_merge_run_id"],
            ["discovery_merge_runs.id"],
            name="fk_discovery_merge_runs_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merge_input_hash", name="uq_discovery_merge_runs_input_hash"),
    )
    op.create_index(
        "ix_discovery_merge_runs_edition", "discovery_merge_runs", ["edition_id", "created_at"]
    )


def _create_discovery_subject_identities() -> None:
    op.create_table(
        "discovery_subject_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("origin_key", sa.Text(), nullable=False),
        sa.Column("cross_edition_lineage_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'merged')", name="ck_discovery_subject_status"),
        sa.CheckConstraint(
            "(status = 'active' AND merged_into_id IS NULL) OR "
            "(status = 'merged' AND merged_into_id IS NOT NULL)",
            name="ck_discovery_subject_merge_projection",
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["created_by_merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "origin_key", name="uq_discovery_subject_origin"),
    )
    op.create_index(
        "ix_discovery_subject_identities_edition",
        "discovery_subject_identities",
        ["edition_id", "status"],
    )


def _create_discovery_snapshots() -> None:
    op.create_table(
        "discovery_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("planner_kind", sa.String(length=32), nullable=False),
        sa.Column("lineage", sa.String(length=16), nullable=False),
        sa.Column("replay_run_id", sa.Uuid(), nullable=True),
        sa.Column("subjects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_discovery_snapshots_version"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id"], ["discovery_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "edition_id", "lineage", "version", name="uq_discovery_snapshots_version"
        ),
        sa.UniqueConstraint("intake_id", "lineage", name="uq_discovery_snapshots_intake"),
        sa.UniqueConstraint("merge_run_id", name="uq_discovery_snapshots_merge_run"),
    )
    op.create_index(
        "ix_discovery_snapshots_edition",
        "discovery_snapshots",
        ["edition_id", "lineage", "version"],
    )
    op.create_index(
        "uq_discovery_snapshots_active_operational",
        "discovery_snapshots",
        ["edition_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND lineage = 'operational'"),
    )


def _create_editorial_groups() -> None:
    op.create_table(
        "editorial_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_relationship_status", sa.String(length=32), nullable=False),
        sa.Column("needs_source_verification", sa.Boolean(), nullable=False),
        sa.Column("needs_source_expansion", sa.Boolean(), nullable=False),
        sa.Column("grouping_confidence", sa.String(length=32), nullable=False),
        sa.Column("grouping_justification", sa.Text(), nullable=False),
        sa.Column("potential_historical_group_id", sa.Uuid(), nullable=True),
        sa.Column("editorial_type", sa.String(length=32), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_subject_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'rejected', 'selected', 'superseded')",
            name="ck_editorial_groups_status",
        ),
        sa.CheckConstraint(
            "outcome IN ('new_subject', 'duplicate_same_publication', "
            "'update_previous_subject', 'non_independent_reprint', 'ambiguous_review')",
            name="ck_editorial_groups_outcome",
        ),
        sa.CheckConstraint(
            "editorial_type IS NULL OR editorial_type IN ('brief', 'major')",
            name="ck_editorial_groups_type",
        ),
        sa.CheckConstraint(
            "source_relationship_status IN ('provisional', 'verified')",
            name="ck_editorial_groups_relationship",
        ),
        sa.CheckConstraint(
            "grouping_confidence IN ('low', 'medium', 'high')",
            name="ck_editorial_groups_confidence",
        ),
        sa.CheckConstraint("version > 0", name="ck_editorial_groups_version"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_editorial_payload_object"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["potential_historical_group_id"],
            ["editorial_groups.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["discovery_subject_id"],
            ["discovery_subject_identities.id"],
            name="fk_editorial_groups_discovery_subject",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_editorial_groups_edition", "editorial_groups", ["edition_id", "status", "created_at"]
    )
    op.create_index(
        "ix_editorial_groups_discovery_subject", "editorial_groups", ["discovery_subject_id"]
    )


def _create_human_decisions() -> None:
    op.create_table(
        "human_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("group_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_type IN ('merge', 'split', 'reject', 'select', 'claim_validate', "
            "'claim_correct', 'claim_reject', 'indicator_validate', 'indicator_correct', "
            "'indicator_reject', 'source_relationship_validate', "
            "'source_relationship_correct', 'brief_changes_requested', 'brief_approve', "
            "'brief_promote')",
            name="ck_human_decisions_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(group_ids) = 'array'", name="ck_human_decisions_groups_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_human_decisions_payload_object"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_human_decisions_edition", "human_decisions", ["edition_id", "occurred_at"])


def _create_subject_merge_events() -> None:
    op.create_table(
        "subject_merge_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("from_subject_id", sa.Uuid(), nullable=False),
        sa.Column("into_subject_id", sa.Uuid(), nullable=False),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_subject_id <> into_subject_id", name="ck_subject_merge_events_distinct"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["from_subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["into_subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subject_merge_events_edition", "subject_merge_events", ["edition_id", "created_at"]
    )


def _create_subject_contributions() -> None:
    op.create_table(
        "subject_contributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_key", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_version", sa.Integer(), nullable=False),
        sa.Column("contributed_title", sa.String(length=1000), nullable=False),
        sa.Column("contributed_summary", sa.Text(), nullable=False),
        sa.Column(
            "contributed_source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "contributed_provisional_ioc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("merge_group_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("first_seen_version > 0", name="ck_subject_contributions_version"),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["first_seen_snapshot_id"], ["discovery_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "intake_id", "candidate_key", name="uq_subject_contributions_candidate"
        ),
    )
    op.create_index(
        "ix_subject_contributions_subject", "subject_contributions", ["subject_id", "created_at"]
    )


def _create_collection_policy_snapshots() -> None:
    op.create_table(
        "collection_policy_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("max_redirects", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_download_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_expanded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_decompression_ratio", sa.Float(), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=False),
        sa.Column("allowed_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocked_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("extraction_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(id) = 64 AND id ~ '^[0-9a-f]{64}$'",
            name="ck_collection_policy_snapshots_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_source_documents() -> None:
    op.create_table(
        "source_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("license_restriction", sa.Text(), nullable=True),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("do_not_submit", sa.Boolean(), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("logical_filename", sa.Text(), nullable=True),
        sa.Column("source_collection_id", sa.Uuid(), nullable=True),
        sa.Column("source_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("decoded_blob_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("declared_mime_type", sa.String(255), nullable=True),
        sa.Column("detected_mime_type", sa.String(255), nullable=True),
        sa.Column("encoded_sha256", sa.String(64), nullable=True),
        sa.Column("decoded_sha256", sa.String(64), nullable=True),
        sa.Column("encoded_size", sa.BigInteger(), nullable=True),
        sa.Column("decoded_size", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(TLP_CHECK, name="ck_source_documents_tlp"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["decoded_blob_id"],
            ["blobs.id"],
            name="fk_source_documents_decoded_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_documents_subject_id", "source_documents", ["subject_id"])
    op.create_index("ix_source_documents_blob_id", "source_documents", ["blob_id"])


def _create_derived_artifacts() -> None:
    op.create_table(
        "derived_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("text_blob_id", sa.Uuid(), nullable=False),
        sa.Column("parser_name", sa.String(length=128), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("text_length", sa.BigInteger(), nullable=False),
        sa.Column("publication_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("text_length >= 0", name="ck_derived_artifacts_text_length"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["text_blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_derived_artifacts_source",
        "derived_artifacts",
        ["source_document_id", "created_at"],
    )


def _create_source_collections() -> None:
    op.create_table(
        "source_collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("source_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("proposed_role", sa.String(length=32), nullable=False),
        sa.Column("relationship_status", sa.String(length=32), nullable=False),
        sa.Column("relationship_evidence", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("latest_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decoded_blob_id", sa.Uuid(), nullable=True),
        sa.Column("fetch_job_id", sa.Uuid(), nullable=True),
        sa.Column("fetch_policy_snapshot_id", sa.String(64), nullable=True),
        sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetch_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin_kind", sa.String(32), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.Column("source_tlp", sa.String(32), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("do_not_submit", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','queued','fetching','archived','extracted','completed',"
            "'unavailable','blocked','failed_retryable','failed_terminal')",
            name="ck_source_collections_state",
        ),
        sa.CheckConstraint(
            "proposed_role IN ('primary', 'independent', 'relay', 'aggregator', 'social', "
            "'unknown')",
            name="ck_source_collections_role",
        ),
        sa.CheckConstraint(
            "relationship_status IN ('provisional', 'verified')",
            name="ck_source_collections_relationship",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_source_collections_attempt_count"),
        sa.CheckConstraint(
            "relationship_status <> 'verified' OR "
            "relationship_evidence LIKE 'human:%' OR "
            "relationship_evidence LIKE 'deterministic:%'",
            name="ck_source_collections_verified_evidence",
        ),
        sa.CheckConstraint(
            "origin_kind IN ('discovery', 'reference_research', 'manual')",
            name="ck_source_collections_origin_kind",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["discovery_batches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"],
            ["derived_artifacts.id"],
            name="fk_source_collections_derived_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decoded_blob_id"],
            ["blobs.id"],
            name="fk_source_collections_decoded_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fetch_job_id"],
            ["jobs.id"],
            name="fk_source_collections_fetch_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fetch_policy_snapshot_id"],
            ["collection_policy_snapshots.id"],
            name="fk_source_collections_fetch_policy_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_id", "source_candidate_id", name="uq_source_collections_subject_candidate"
        ),
        sa.UniqueConstraint(
            "subject_id", "canonical_url", name="uq_source_collections_subject_canonical_url"
        ),
    )
    op.create_index(
        "ix_source_collections_subject_state", "source_collections", ["subject_id", "state"]
    )


def _create_collection_attempts() -> None:
    op.create_table(
        "collection_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.String(length=64), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("redirect_chain", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("declared_content_type", sa.String(length=255), nullable=True),
        sa.Column("detected_content_type", sa.String(length=255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("allowed_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("policy_snapshot_id", sa.String(64), nullable=False),
        sa.Column("encoded_size", sa.BigInteger(), nullable=True),
        sa.Column("encoded_sha256", sa.String(64), nullable=True),
        sa.Column("decoded_size", sa.BigInteger(), nullable=True),
        sa.Column("decoded_sha256", sa.String(64), nullable=True),
        sa.Column("content_encoding", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error', "
            "'interrupted')",
            name="ck_collection_attempts_outcome",
        ),
        sa.CheckConstraint("size IS NULL OR size >= 0", name="ck_collection_attempts_size"),
        sa.CheckConstraint(
            "sha256 IS NULL OR (char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_sha256",
        ),
        sa.CheckConstraint(
            "encoded_size IS NULL OR encoded_size >= 0",
            name="ck_collection_attempts_encoded_size",
        ),
        sa.CheckConstraint(
            "decoded_size IS NULL OR decoded_size >= 0",
            name="ck_collection_attempts_decoded_size",
        ),
        sa.CheckConstraint(
            "encoded_sha256 IS NULL OR "
            "(char_length(encoded_sha256) = 64 AND encoded_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_encoded_sha256",
        ),
        sa.CheckConstraint(
            "decoded_sha256 IS NULL OR "
            "(char_length(decoded_sha256) = 64 AND decoded_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_decoded_sha256",
        ),
        sa.ForeignKeyConstraint(["collection_id"], ["source_collections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["policy_snapshot_id"],
            ["collection_policy_snapshots.id"],
            name="fk_collection_attempts_policy_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collection_attempts_collection",
        "collection_attempts",
        ["collection_id", "attempted_at"],
    )
    op.create_index("ix_collection_attempts_job", "collection_attempts", ["job_id"])


def _create_claims() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("extraction_method", sa.String(length=128), nullable=False),
        sa.Column("extraction_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("span_start", sa.BigInteger(), nullable=False),
        sa.Column("span_end", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("local_span_start", sa.BigInteger(), nullable=True),
        sa.Column("local_span_end", sa.BigInteger(), nullable=True),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('name', 'date', 'ioc', 'cve', 'fact', 'assessment', 'uncertainty', "
            "'infection_chain', 'ttp', 'victimology')",
            name="ck_claims_kind",
        ),
        sa.CheckConstraint(
            "span_start >= 0 AND span_end > span_start", name="ck_claims_span"
        ),
        sa.CheckConstraint(
            "(local_span_start IS NULL AND local_span_end IS NULL) OR "
            "(local_span_start >= 0 AND local_span_end > local_span_start)",
            name="ck_claims_local_span",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"], ["derived_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["model_run_id"],
            ["model_runs.id"],
            name="fk_claims_model_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claims_subject", "claims", ["subject_id", "created_at"])
    op.create_index("ix_claims_source", "claims", ["source_document_id"])


def _create_indicators() -> None:
    op.create_table(
        "indicators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("span_start", sa.BigInteger(), nullable=False),
        sa.Column("span_end", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('hash', 'domain', 'ip', 'url', 'cve', 'attack_id', 'email')",
            name="ck_indicators_kind",
        ),
        sa.CheckConstraint(
            "span_start >= 0 AND span_end > span_start", name="ck_indicators_span"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"], ["derived_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_indicators_subject", "indicators", ["subject_id", "created_at"])
    op.create_index("ix_indicators_source", "indicators", ["source_document_id"])


def _create_rejected_model_proposals() -> None:
    op.create_table(
        "rejected_model_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("requested_kind", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(proposal_hash) = 64 AND proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_rejected_model_proposals_hash",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"], ["derived_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rejected_model_proposals_source",
        "rejected_model_proposals",
        ["source_document_id", "created_at"],
    )


def _create_brief_evidence_packs() -> None:
    op.create_table(
        "brief_evidence_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("object_hashes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_entities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("human_decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_from_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("built_from_snapshot_version", sa.Integer(), nullable=True),
        sa.Column(
            "covered_contribution_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "scope",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'full'::character varying"),
        ),
        sa.Column("base_pack_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_brief_evidence_packs_version"),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_evidence_packs_hash",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["built_from_snapshot_id"],
            ["discovery_snapshots.id"],
            name="fk_brief_evidence_packs_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_pack_id"],
            ["brief_evidence_packs.id"],
            name="fk_brief_evidence_packs_base",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "version", name="uq_brief_evidence_packs_version"),
        sa.UniqueConstraint(
            "subject_id", "content_hash", name="uq_brief_evidence_packs_content_hash"
        ),
    )
    op.create_index(
        "ix_brief_evidence_packs_subject",
        "brief_evidence_packs",
        ["subject_id", "version"],
    )


def _create_brief_drafts() -> None:
    op.create_table(
        "brief_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        sa.Column("pack_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parent_draft_id", sa.Uuid(), nullable=True),
        sa.Column("regenerated_block_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_brief_drafts_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'changes_requested', 'approved', 'promoted')",
            name="ck_brief_drafts_status",
        ),
        sa.CheckConstraint(
            "char_length(pack_hash) = 64 AND pack_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_drafts_pack_hash",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pack_id"], ["brief_evidence_packs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_draft_id"], ["brief_drafts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "version", name="uq_brief_drafts_version"),
    )
    op.create_index("ix_brief_drafts_subject", "brief_drafts", ["subject_id", "version"])


def _create_model_conversations() -> None:
    op.create_table(
        "model_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("external_locator", sa.Text(), nullable=True),
        sa.Column("expected_profile", sa.String(255), nullable=True),
        sa.Column("requested_model", sa.String(255), nullable=True),
        sa.Column("head_turn_id", sa.Uuid(), nullable=True),
        sa.Column("turn_count", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai','qwen','fake')", name="ck_model_conversations_provider"
        ),
        sa.CheckConstraint(
            "transport IN ('chatgpt_bridge','openai_responses','application_managed')",
            name="ck_model_conversations_transport",
        ),
        sa.CheckConstraint(
            "purpose IN ('discovery','analyst_assistance','pivot_research','drafting',"
            "'critic','subject_production')",
            name="ck_model_conversations_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready','busy','needs_review','unavailable','archived')",
            name="ck_model_conversations_status",
        ),
        sa.CheckConstraint(
            "turn_count >= 0 AND version >= 1", name="ck_model_conversations_counters"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_conversations_subject", "model_conversations", ["subject_id", "updated_at"]
    )
    op.create_index(
        "ix_model_conversations_edition", "model_conversations", ["edition_id", "updated_at"]
    )


def _create_model_conversation_turns() -> None:
    op.create_table(
        "model_conversation_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("parent_turn_id", sa.Uuid(), nullable=True),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("input_blob_reference", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_blob_reference", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_turn_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="ck_model_conversation_turns_sequence"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','needs_review','blocked')",
            name="ck_model_conversation_turns_status",
        ),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_model_conversation_turns_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["model_conversations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_turn_id"], ["model_conversation_turns.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_model_conversation_turn_sequence"
        ),
        sa.UniqueConstraint("model_run_id", name="uq_model_conversation_turn_model_run"),
        sa.UniqueConstraint("idempotency_key", name="uq_model_conversation_turn_idempotency"),
    )
    op.create_index(
        "ix_model_conversation_turns_conversation",
        "model_conversation_turns",
        ["conversation_id", "sequence"],
    )
    op.create_index(
        "uq_model_conversation_turn_running",
        "model_conversation_turns",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def _create_model_output_rejections() -> None:
    op.create_table(
        "model_output_rejections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=False),
        sa.Column("value_sha256", sa.String(64), nullable=False),
        sa.Column("raw_output_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "value_sha256 ~ '^[0-9a-f]{64}$'", name="ck_model_output_rejections_hash"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_output_rejections_run",
        "model_output_rejections",
        ["model_run_id", "created_at"],
    )


def _create_subject_production_runs() -> None:
    op.create_table(
        "subject_production_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_stage", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("research_date", sa.Date(), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_run_version"),
        sa.CheckConstraint("run_number >= 1", name="ck_run_number"),
        sa.CheckConstraint(
            "status IN ('queued','running','ready','needs_review','failed','cancelled')",
            name="ck_run_status",
        ),
        sa.CheckConstraint(
            "current_stage IN ('sources','references','extraction','synthesis','assembly')",
            name="ck_run_stage",
        ),
        sa.CheckConstraint("profile IN ('brief_auto','major_assisted')", name="ck_run_profile"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["model_conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "run_number", name="uq_subject_run_number"),
    )
    op.create_index(
        "ix_subject_production_runs_subject_id_created_at",
        "subject_production_runs",
        ["subject_id", "created_at"],
    )
    op.create_index(
        "ix_subject_production_runs_edition_id_status",
        "subject_production_runs",
        ["edition_id", "status"],
    )


def _create_production_artifacts() -> None:
    op.create_table(
        "production_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="verified"),
        sa.Column("raw_blob_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_blob_id", sa.Uuid(), nullable=True),
        sa.Column("rendered_blob_id", sa.Uuid(), nullable=True),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_turn_id", sa.Uuid(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version >= 1", name="ck_artifact_version"),
        sa.CheckConstraint(
            "stage IN ('references','extraction','synthesis','brief')", name="ck_artifact_stage"
        ),
        sa.CheckConstraint(
            "status IN ('verified','stale','needs_review')", name="ck_artifact_status"
        ),
        sa.CheckConstraint("LENGTH(input_hash) = 64", name="ck_artifact_input_hash"),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["raw_blob_id"], ["blobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["canonical_blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rendered_blob_id"], ["blobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["conversation_turn_id"], ["model_conversation_turns.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_run_id", "stage", "version", name="uq_run_stage_version"),
    )
    op.create_index(
        "ix_production_artifacts_run_stage_version",
        "production_artifacts",
        ["production_run_id", "stage", "version"],
    )


def _create_edition_production_batches() -> None:
    op.create_table(
        "edition_production_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','completed_with_issues','cancelled')",
            name="ck_batch_status",
        ),
        sa.CheckConstraint("profile IN ('brief_auto','major_assisted')", name="ck_batch_profile"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_edition_production_batches_edition_id_status",
        "edition_production_batches",
        ["edition_id", "status"],
    )


def _create_edition_production_batch_items() -> None:
    op.create_table(
        "edition_production_batch_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("position >= 1", name="ck_batch_item_position"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["edition_production_batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "position", name="uq_batch_position"),
        sa.UniqueConstraint("batch_id", "subject_id", name="uq_batch_subject"),
    )


def _create_conversation_lifecycles() -> None:
    op.create_table(
        "conversation_lifecycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("release_outcome", sa.String(length=32), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_cleanup_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cleanup_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "policy IN ('keep', 'delete_on_success')",
            name="ck_conv_lifecycle_policy",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'delete_pending', 'deleting', 'deleted', "
            "'cleanup_failed', 'retained')",
            name="ck_conv_lifecycle_status",
        ),
        sa.CheckConstraint(
            "release_outcome IS NULL OR release_outcome IN ('success', 'failure', "
            "'needs_review', 'cancelled')",
            name="ck_conv_lifecycle_outcome",
        ),
        sa.CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_conv_lifecycle_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_conv_lifecycle_conversation_id"),
    )
    op.create_index(
        "ix_conversation_lifecycles_conversation_id",
        "conversation_lifecycles",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_lifecycles_status",
        "conversation_lifecycles",
        ["status"],
    )
    op.create_index(
        "ix_conversation_lifecycles_created_at",
        "conversation_lifecycles",
        ["created_at"],
    )
