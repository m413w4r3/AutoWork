import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import CheckConstraint, MetaData, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from cti_app.infrastructure.database.models import (  # noqa: F401
    agent_sessions,
    bridge_generation_slots,
    collection,
    core,
    discovery,
    edition_publication,
    edition_render,
    editions,
    editorial,
    editorial_drafts,
    editorial_proposal_impacts,
    editorial_proposals,
    editorial_threads,
    editorial_workspace,
    external_effects,
    invariants,
    investigation,
    investigation_coordination,
    investigation_handoffs,
    investigation_tasks,
    jobs,
    media_assets,
    model_execution,
    opinion_note_proposals,
    osint_open_policy,
    production,
    production_routing,
    publication_render,
    publication_review,
    retrohunt,
    selection,
    source_media,
    work_contributions,
    workspace,
    workspace_note_annotations,
)
from cti_app.infrastructure.database.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_EXTERNAL_EFFECT_TABLES = frozenset(
    {
        "external_effect_claim",
        "external_effect_use",
        "effect_acquisition_dispatch_links",
        "sample_acquisition_dispatch_attempts",
    }
)
_OSINT_POLICY_TABLES = frozenset(
    {
        "osint_open_policies",
        "osint_open_policy_events",
        "osint_open_inheritances",
    }
)
_OSINT_ENVELOPE_COLUMNS = frozenset(
    {
        "authority_kind",
        "inherited_policy_id",
        "inherited_policy_version",
        "inherited_policy_event_id",
    }
)
_SAMPLE_ACQUISITION_DISPATCH_COLUMNS = frozenset({"dispatch_attempt_id"})
_ROUTING_DECISION_TABLES = frozenset({"production_routing_decisions"})
_EDITORIAL_WORKSPACE_TABLES = frozenset({"editorial_workspace_snapshots"})
_EDITORIAL_THREAD_TABLES = frozenset(
    {"editorial_threads", "editorial_thread_events", "editorial_thread_jobs"}
)
_EDITORIAL_BRIDGE_GENERATION_SLOT_TABLES = frozenset({"bridge_generation_slots"})
_EDITORIAL_PROPOSAL_TABLES = frozenset(
    {
        "editorial_proposals",
        "editorial_proposal_model_runs",
        "editorial_proposal_events",
        "editorial_proposal_impacts",
    }
)
_EDITORIAL_DRAFT_TABLES = frozenset({"editorial_drafts"})
_WORKSPACE_TABLES = frozenset(
    {
        "workspace_bindings",
        "workspace_file_revisions",
        "workspace_file_events",
        "workspace_sync_cursors",
    }
)
_INVESTIGATION_PLAN_TABLES = frozenset({"investigation_plans", "investigation_plan_versions"})
_INVESTIGATION_TASK_TABLES = frozenset(
    {
        "investigation_tasks",
        "investigation_task_dependencies",
        "investigation_task_events",
    }
)
_INVESTIGATION_HANDOFF_TABLES = frozenset({"investigation_handoffs"})
_AGENT_SESSION_TABLES = frozenset({"agent_sessions", "agent_session_instructions"})
_WORK_CONTRIBUTION_TABLES = frozenset({"work_contributions"})
_NOTE_PROPOSAL_TABLES = frozenset({"investigation_note_proposals"})
_NOTE_ANNOTATION_TABLES = frozenset({"workspace_note_annotations"})
_TOOL_INVOCATION_ATTRIBUTION_COLUMNS = frozenset({"task_id", "actor_kind", "actor_id"})
_TOOL_INVOCATION_ATTRIBUTION_CHECKS = frozenset(
    {"ck_tool_invocations_actor_kind", "ck_tool_invocations_actor_pair"}
)
_TOOL_INVOCATION_AGENT_SESSION_COLUMNS = frozenset({"agent_session_id"})
_TOOL_INVOCATION_AGENT_SESSION_CHECKS = frozenset({"ck_tool_invocations_agent_session_pair"})
_PROSE_AUTHORITY_COLUMN = "prose_authority"
_PROSE_AUTHORITY_CHECK = "ck_analyst_investigation_prose_authority"
_BASELINE_DECISION_TYPE_VALUES = (
    "'member_validate', 'member_reject', 'feature_validate', 'feature_reject', "
    "'pivot_approve', 'pivot_reject', 'note_approve', 'note_changes_requested', "
    "'detection_approve', 'detection_reject'"
)
_BASELINE_DECISION_TARGET_VALUES = (
    "'member', 'feature', 'tool', 'invariant', 'pivot', 'corpus', 'detection', 'note'"
)
_ROUTING_RUN_COLUMNS = frozenset({"routing_decision_id"})
_COLLECTION_ATTEMPT_ADDITIONS = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "duration_ms",
        "acquisition_method",
        "source_equivalence_class",
        "evidence_class",
        "observation_kind",
        "quality_score",
        "quality_assessment",
        "archive_snapshot_time",
        "model_run_id",
        "model_generated",
        "primary_source_verified",
    }
)
_COLLECTION_ATTEMPT_ADDITION_CHECKS = frozenset(
    {
        "ck_collection_attempts_strategy_metadata",
        "ck_collection_attempts_duration_ms",
        "ck_collection_attempts_quality_score",
    }
)
_EDITORIAL_THREAD_TURN_COLUMNS = frozenset({"objective"})
_BASELINE_CONVERSATION_PURPOSE_SQL = (
    "'discovery', 'analyst_assistance', 'pivot_research', 'drafting', 'critic', 'subject_research'"
)


def _metadata_for_frozen_baseline() -> MetaData:
    """Keep post-baseline additive objects out of the frozen 0001 shape.

    The baseline intentionally predates these additive features, but its
    frozen migration derives its table list from ``Base.metadata``. Present
    that migration with its pre-feature shape while retaining complete
    metadata for current schema inspection and later additive revisions.
    """
    metadata = MetaData()
    for source_table in target_metadata.tables.values():
        if (
            source_table.name in _OSINT_POLICY_TABLES
            or source_table.name in _EXTERNAL_EFFECT_TABLES
            or source_table.name in _ROUTING_DECISION_TABLES
            or source_table.name in _EDITORIAL_WORKSPACE_TABLES
            or source_table.name in _EDITORIAL_THREAD_TABLES
            or source_table.name in _EDITORIAL_BRIDGE_GENERATION_SLOT_TABLES
            or source_table.name in _EDITORIAL_PROPOSAL_TABLES
            or source_table.name in _EDITORIAL_DRAFT_TABLES
            or source_table.name in _WORKSPACE_TABLES
            or source_table.name in _INVESTIGATION_PLAN_TABLES
            or source_table.name in _INVESTIGATION_TASK_TABLES
            or source_table.name in _INVESTIGATION_HANDOFF_TABLES
            or source_table.name in _AGENT_SESSION_TABLES
            or source_table.name in _WORK_CONTRIBUTION_TABLES
            or source_table.name in _NOTE_PROPOSAL_TABLES
            or source_table.name in _NOTE_ANNOTATION_TABLES
        ):
            continue
        table = source_table.to_metadata(metadata)
        if table.name == "investigation_envelopes":
            for constraint in tuple(table.constraints):
                if constraint.name in {
                    "ck_investigation_envelopes_authority_lineage",
                    "fk_investigation_envelopes_inherited_policy",
                    "fk_investigation_envelopes_inherited_event",
                }:
                    table.constraints.discard(constraint)
            for foreign_key in tuple(table.foreign_keys):
                if (
                    foreign_key.parent is not None
                    and foreign_key.parent.name in _OSINT_ENVELOPE_COLUMNS
                ):
                    table.foreign_keys.discard(foreign_key)
                    if foreign_key.constraint is not None:
                        table.constraints.discard(foreign_key.constraint)
            for column_name in _OSINT_ENVELOPE_COLUMNS:
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "sample_acquisition_attempts":
            for constraint in tuple(table.constraints):
                if constraint.name == "fk_sample_acquisition_attempts_dispatch":
                    table.constraints.discard(constraint)
            for foreign_key in tuple(table.foreign_keys):
                if (
                    foreign_key.parent is not None
                    and foreign_key.parent.name in _SAMPLE_ACQUISITION_DISPATCH_COLUMNS
                ):
                    table.foreign_keys.discard(foreign_key)
                    if foreign_key.constraint is not None:
                        table.constraints.discard(foreign_key.constraint)
            for index in tuple(table.indexes):
                if any(
                    column.name in _SAMPLE_ACQUISITION_DISPATCH_COLUMNS for column in index.columns
                ):
                    table.indexes.discard(index)
            for column_name in _SAMPLE_ACQUISITION_DISPATCH_COLUMNS:
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "production_runs":
            for constraint in tuple(table.constraints):
                if constraint.name == "fk_production_runs_routing_decision":
                    table.constraints.discard(constraint)
            for foreign_key in tuple(table.foreign_keys):
                if (
                    foreign_key.parent is not None
                    and foreign_key.parent.name in _ROUTING_RUN_COLUMNS
                ):
                    table.foreign_keys.discard(foreign_key)
                    if foreign_key.constraint is not None:
                        table.constraints.discard(foreign_key.constraint)
            for column_name in _ROUTING_RUN_COLUMNS:
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "collection_attempts":
            baseline_outcome_constraint: CheckConstraint | None = None
            for constraint in tuple(table.constraints):
                if constraint.name == "ck_collection_attempts_outcome":
                    table.constraints.discard(constraint)
                    baseline_outcome_constraint = CheckConstraint(
                        "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', "
                        "'error', 'interrupted')",
                        name="ck_collection_attempts_outcome",
                    )
                    continue
                constraint_columns = getattr(constraint, "columns", ())
                if constraint.name in _COLLECTION_ATTEMPT_ADDITION_CHECKS or any(
                    column.name in _COLLECTION_ATTEMPT_ADDITIONS for column in constraint_columns
                ):
                    table.constraints.discard(constraint)
            if baseline_outcome_constraint is not None:
                table.append_constraint(baseline_outcome_constraint)
            for foreign_key in tuple(table.foreign_keys):
                if (
                    foreign_key.parent is not None
                    and foreign_key.parent.name in _COLLECTION_ATTEMPT_ADDITIONS
                ):
                    table.foreign_keys.discard(foreign_key)
                    if foreign_key.constraint is not None:
                        table.constraints.discard(foreign_key.constraint)
            for index in tuple(table.indexes):
                if any(column.name in _COLLECTION_ATTEMPT_ADDITIONS for column in index.columns):
                    table.indexes.discard(index)
            for column_name in _COLLECTION_ATTEMPT_ADDITIONS:
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "model_conversations":
            for constraint in tuple(table.constraints):
                if constraint.name == "ck_model_conversations_purpose":
                    table.constraints.discard(constraint)
            table.append_constraint(
                CheckConstraint(
                    f"purpose IN ({_BASELINE_CONVERSATION_PURPOSE_SQL})",
                    name="ck_model_conversations_purpose",
                )
            )
        if table.name == "model_conversation_turns":
            for constraint in tuple(table.constraints):
                if constraint.name == "ck_model_conversation_turns_objective":
                    table.constraints.discard(constraint)
            for column_name in _EDITORIAL_THREAD_TURN_COLUMNS:
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "analyst_investigations":
            for constraint in tuple(table.constraints):
                if constraint.name == _PROSE_AUTHORITY_CHECK or any(
                    column.name == _PROSE_AUTHORITY_COLUMN
                    for column in getattr(constraint, "columns", ())
                ):
                    table.constraints.discard(constraint)
            if _PROSE_AUTHORITY_COLUMN in table.c:
                table._columns.remove(table.c[_PROSE_AUTHORITY_COLUMN])
        if table.name == "tool_invocations":
            # 0021 adds execution attribution; the frozen 0001 shape predates it.
            # 0023 adds the agent-session attribution pair for the same reason.
            for constraint in tuple(table.constraints):
                if (
                    constraint.name
                    in _TOOL_INVOCATION_ATTRIBUTION_CHECKS | _TOOL_INVOCATION_AGENT_SESSION_CHECKS
                    or any(
                        column.name
                        in _TOOL_INVOCATION_ATTRIBUTION_COLUMNS
                        | _TOOL_INVOCATION_AGENT_SESSION_COLUMNS
                        for column in getattr(constraint, "columns", ())
                    )
                ):
                    table.constraints.discard(constraint)
            for foreign_key in tuple(table.foreign_keys):
                if (
                    foreign_key.parent is not None
                    and foreign_key.parent.name
                    in _TOOL_INVOCATION_ATTRIBUTION_COLUMNS | _TOOL_INVOCATION_AGENT_SESSION_COLUMNS
                ):
                    table.foreign_keys.discard(foreign_key)
                    if foreign_key.constraint is not None:
                        table.constraints.discard(foreign_key.constraint)
            for index in tuple(table.indexes):
                if any(
                    column.name
                    in _TOOL_INVOCATION_ATTRIBUTION_COLUMNS | _TOOL_INVOCATION_AGENT_SESSION_COLUMNS
                    for column in index.columns
                ):
                    table.indexes.discard(index)
            for column_name in (
                _TOOL_INVOCATION_ATTRIBUTION_COLUMNS | _TOOL_INVOCATION_AGENT_SESSION_COLUMNS
            ):
                if column_name in table.c:
                    table._columns.remove(table.c[column_name])
        if table.name == "analyst_decisions":
            for constraint in tuple(table.constraints):
                if constraint.name == "ck_analyst_decisions_type":
                    table.constraints.discard(constraint)
                    table.append_constraint(
                        CheckConstraint(
                            f"decision_type IN ({_BASELINE_DECISION_TYPE_VALUES})",
                            name="ck_analyst_decisions_type",
                        )
                    )
                elif constraint.name == "ck_analyst_decisions_target":
                    table.constraints.discard(constraint)
                    table.append_constraint(
                        CheckConstraint(
                            f"target_type IN ({_BASELINE_DECISION_TARGET_VALUES})",
                            name="ck_analyst_decisions_target",
                        )
                    )
    return metadata


def _run_migrations_with_frozen_baseline_metadata() -> None:
    original_metadata = Base.metadata
    Base.metadata = _metadata_for_frozen_baseline()
    try:
        context.run_migrations()
    finally:
        Base.metadata = original_metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        _run_migrations_with_frozen_baseline_metadata()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        _run_migrations_with_frozen_baseline_metadata()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
