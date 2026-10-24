"""Read-only snapshot loader for the investigation audit projection.

Offline projection tests do not execute this PostgreSQL adapter; its runtime
behavior remains UNKNOWN until exercised against a PostgreSQL test database.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.evidence_recorder import MAX_EVIDENCE_JSON_BYTES
from cti_app.application.investigation_audit_export import (
    AuditSourceSnapshot,
    project_advisor_call_blob_metadata,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation_experiment import EXPERIMENT_SCHEMA_VERSION
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.infrastructure.database.models.investigation import AnalystOpinionVersionRow
from cti_app.infrastructure.database.models.production import AnalystInvestigationRow
from cti_app.infrastructure.database.repositories.investigation import (
    SqlAlchemyAnalystOpinionRepository,
    SqlAlchemyDetectionCandidateRepository,
    SqlAlchemyEvidenceObservationRepository,
    SqlAlchemyFindingRepository,
    SqlAlchemyInvestigationBudgetRepository,
    SqlAlchemyInvestigationEnvelopeRepository,
    SqlAlchemyInvestigationExperimentRepository,
    SqlAlchemyInvestigationStateRepository,
    SqlAlchemySampleAssociationRepository,
    SqlAlchemyToolInvocationRepository,
)
from cti_app.infrastructure.database.repositories.model_conversations import (
    SqlAlchemyModelConversationRepository,
    SqlAlchemyModelConversationTurnRepository,
)
from cti_app.infrastructure.database.repositories.model_runs import SqlAlchemyModelRunRepository
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyAnalystDecisionRepository,
    SqlAlchemyAnalystInputPackRepository,
)


async def load_investigation_audit_snapshot(
    session: AsyncSession, investigation_id: UUID
) -> AuditSourceSnapshot:
    """Read existing repositories and rows without writes or external blob reads."""
    investigation_row = await session.scalar(
        select(AnalystInvestigationRow).where(AnalystInvestigationRow.id == investigation_id)
    )
    if investigation_row is None:
        raise LookupError("Investigation does not exist")

    invocations = await SqlAlchemyToolInvocationRepository(session).list_for_investigation(
        investigation_id
    )
    observations = await SqlAlchemyEvidenceObservationRepository(session).list_for_investigation(
        investigation_id
    )
    findings = await SqlAlchemyFindingRepository(session).list_for_investigation(investigation_id)
    experiments = await SqlAlchemyInvestigationExperimentRepository(session).list_for_investigation(
        investigation_id
    )
    state = await SqlAlchemyInvestigationStateRepository(session).get(investigation_id)
    envelope = await SqlAlchemyInvestigationEnvelopeRepository(session).get(investigation_id)
    budget = await SqlAlchemyInvestigationBudgetRepository(session).get(investigation_id)
    candidates = await SqlAlchemyDetectionCandidateRepository(session).list_for_investigation(
        investigation_id
    )
    associations = await SqlAlchemySampleAssociationRepository(session).list_for_investigation(
        investigation_id
    )
    decisions = await SqlAlchemyAnalystDecisionRepository(session).list_for_investigation(
        investigation_id
    )
    input_pack_row = await SqlAlchemyAnalystInputPackRepository(session).get_for_investigation(
        investigation_id
    )
    input_pack = {
        "blob_id": str(investigation_row.input_pack_blob_id)
        if investigation_row.input_pack_blob_id is not None
        else None,
        "sha256": investigation_row.input_sha256,
        "synthesis_artifact_id": str(investigation_row.synthesis_artifact_id),
        "investigation_version": investigation_row.version,
        "investigation_status": investigation_row.status,
        "schema_version": input_pack_row.schema_version
        if input_pack_row is not None
        and input_pack_row.blob_id == investigation_row.input_pack_blob_id
        and input_pack_row.sha256 == investigation_row.input_sha256
        else None,
        "input_pack_record_id": str(input_pack_row.id)
        if input_pack_row is not None
        and input_pack_row.blob_id == investigation_row.input_pack_blob_id
        and input_pack_row.sha256 == investigation_row.input_sha256
        else None,
        "created_at": input_pack_row.created_at if input_pack_row is not None else None,
    }

    opinion_repository = SqlAlchemyAnalystOpinionRepository(session)
    opinion_keys = await session.execute(
        select(AnalystOpinionVersionRow.opinion_id, AnalystOpinionVersionRow.version)
        .where(AnalystOpinionVersionRow.investigation_id == investigation_id)
        .order_by(AnalystOpinionVersionRow.opinion_id, AnalystOpinionVersionRow.version)
    )
    opinions = []
    for opinion_id, version in opinion_keys:
        opinion = await opinion_repository.get(opinion_id, version=version)
        if opinion is not None:
            opinions.append(opinion)

    pivot_conversation_id = investigation_row.pivot_conversation_id
    conversation = None
    turns = ()
    model_runs = []
    if pivot_conversation_id is not None:
        conversation = await SqlAlchemyModelConversationRepository(session).get(
            pivot_conversation_id
        )
        if conversation is not None:
            turns = await SqlAlchemyModelConversationTurnRepository(session).list_for_conversation(
                conversation.id
            )
            model_run_repository = SqlAlchemyModelRunRepository(session)
            seen_run_ids: set[UUID] = set()
            for turn in turns:
                if turn.model_run_id in seen_run_ids:
                    continue
                seen_run_ids.add(turn.model_run_id)
                model_run = await model_run_repository.get(turn.model_run_id)
                if model_run is not None:
                    model_runs.append(model_run)

    return AuditSourceSnapshot(
        investigation_id=str(investigation_id),
        invocations=invocations,
        observations=observations,
        findings=findings,
        experiments=experiments,
        hypotheses_state=state,
        envelope=envelope,
        budget_snapshot=budget,
        opinions=tuple(opinions),
        analyst_decisions=tuple(decisions),
        input_pack=input_pack,
        detection_candidates=candidates,
        sample_associations=associations,
        pivot_conversation_id=str(pivot_conversation_id) if pivot_conversation_id else None,
        conversation=conversation,
        conversation_turns=turns,
        model_runs=tuple(model_runs),
        source_versions={
            "investigation_state": InvestigationWorkingState.SCHEMA,
            "investigation_experiment": EXPERIMENT_SCHEMA_VERSION,
            "analyst_opinion": "analyst-opinion.v1",
            "model_conversation": "model-conversation.repository.v1",
            "advisor_call_payload": "operator-advisor-call.v1",
            "audit_projection": "autowork.investigation-audit-event.v1",
        },
    )


async def read_investigation_audit_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    artifact_store: ProductionArtifactStore | None = None,
) -> AuditSourceSnapshot:
    """Capture one PostgreSQL repeatable-read, read-only database snapshot."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            snapshot = await load_investigation_audit_snapshot(session, investigation_id)

    projections = []
    for observation in snapshot.observations:
        source = getattr(observation, "source", None)
        object_type = getattr(observation, "object_type", None)
        source = source.value if hasattr(source, "value") else source
        object_type = object_type.value if hasattr(object_type, "value") else object_type
        if source != "advisor.consult" or object_type != "advisor_call":
            continue
        if artifact_store is None:
            projections.append(
                project_advisor_call_blob_metadata(
                    observation, None, read_error="artifact_reader_unconfigured"
                )
            )
            continue
        blob_id = getattr(observation, "blob_id", None)
        try:
            if not isinstance(blob_id, UUID):
                raise ValueError("advisor observation does not identify a blob")
            raw = await artifact_store.read_bytes(blob_id, max_bytes=MAX_EVIDENCE_JSON_BYTES)
        except Exception as exc:
            error_code = (
                "blob_read_limit"
                if type(exc).__name__ == "BlobReadLimitExceededError"
                else "blob_read_failed"
            )
            projections.append(
                project_advisor_call_blob_metadata(observation, None, read_error=error_code)
            )
        else:
            projections.append(project_advisor_call_blob_metadata(observation, raw))
    return replace(snapshot, advisor_call_metadata=tuple(projections))
