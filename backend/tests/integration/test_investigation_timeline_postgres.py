"""PostgreSQL oracle for the investigation timeline union (M24-04B3, D8/P3).

One investigation is seeded with every canonical source the timeline unions —
a task event, a human decision plus its verification event, a canonical
observation, a Bridge consultation with its two advisory observations, and a
plan version — then the projection is read through ``SqlAlchemyUnitOfWork``.

Disposable database only: ``make test-integration
INTEGRATION_TEST_PATH=tests/integration/test_investigation_timeline_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_plans import InvestigationPlanService
from cti_app.application.investigation_task_events import (
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_timeline import (
    InvestigationTimelineService,
    TimelineActorKind,
    TimelineItemType,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProseAuthority,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
SYSTEM = TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test")


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Timeline fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Timeline fixture",
            slug=f"timeline-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="e" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
            prose_authority=ProseAuthority.BRIDGE_ONLY,
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _build_blob_observation(
    session_factory: async_sessionmaker[AsyncSession],
    blob_root: Path,
    investigation_id: UUID,
    *,
    source: str,
    object_type: str,
    observed_at: datetime,
    invocation_ref: UUID | None = None,
) -> EvidenceObservation:
    blob_store = FilesystemBlobStore(blob_root)
    catalog = BlobCatalogService(blob_store, lambda: SqlAlchemyUnitOfWork(session_factory))
    blob = await catalog.ingest(
        BytesIO(json.dumps({"schema": "timeline-fixture.v1"}).encode()),
        logical_bucket="model-outputs",
        mime_type="application/json",
    )
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=blob.descriptor.sha256,
        source=source,
        source_version="timeline-fixture.v1",
        observed_at=observed_at,
        object_type=object_type,
        object_id=f"fixture-{uuid4()}",
        invocation_ref=invocation_ref,
    )


async def _insert_observation(
    session_factory: async_sessionmaker[AsyncSession], observation: EvidenceObservation
) -> EvidenceObservation:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return observation


async def _seed_task_with_events(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
) -> tuple[InvestigationTaskV1, UUID, AnalystDecision]:
    task = InvestigationTaskV1(
        investigation_id=investigation_id,
        task_key=f"lead-{uuid4().hex[:12]}",
        title="Inspect the loader",
        question="Which gate contacts the C2?",
        objective="Identify the second-stage downloader.",
        priority=TaskPriority.NORMAL,
        status=TaskStatus.PROPOSED,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=SYSTEM,
        created_at=NOW,
        updated_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.investigation_tasks.add(task)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await append_task_event(
            uow.investigation_tasks,
            TaskEventDraft(
                investigation_id=investigation_id,
                task_id=task.id,
                event_type=TaskEventType.TASK_CREATED,
                actor=SYSTEM,
                idempotency_key="created",
                to_status=TaskStatus.PROPOSED,
            ),
            occurred_at=NOW,
        )
        await uow.commit()

    decision = AnalystDecision(
        investigation_id=investigation_id,
        decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
        target_type=AnalystDecisionTargetType.TASK,
        target_id=task.id,
        actor_id=HUMAN.actor_id,
        reason="result verified",
        correlation_id="timeline:fixture",
        occurred_at=NOW + timedelta(seconds=10),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.analyst_decisions.append(decision)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        event, _ = await append_task_event(
            uow.investigation_tasks,
            TaskEventDraft(
                investigation_id=investigation_id,
                task_id=task.id,
                event_type=TaskEventType.VERIFICATION_RECORDED,
                actor=HUMAN,
                idempotency_key="verified",
                analyst_decision_id=decision.id,
            ),
            occurred_at=NOW + timedelta(seconds=10),
        )
        await uow.commit()
    return task, event.id, decision


async def _seed_bridge_consultation(
    session_factory: async_sessionmaker[AsyncSession],
    blob_root: Path,
    investigation_id: UUID,
) -> tuple[ToolInvocation, list[UUID]]:
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"advisor-{uuid4()}",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters={"consultation_type": "plan_investigation"},
        purpose="Ask Bridge for an investigation plan",
        status=ToolInvocationStatus.SUCCEEDED,
        created_at=NOW + timedelta(seconds=20),
        updated_at=NOW + timedelta(seconds=20),
    )
    call = await _build_blob_observation(
        session_factory,
        blob_root,
        investigation_id,
        source="advisor.consult",
        object_type="advisor_call",
        observed_at=NOW + timedelta(seconds=20),
        invocation_ref=invocation.id,
    )
    response = await _build_blob_observation(
        session_factory,
        blob_root,
        investigation_id,
        source="advisor.consult.response",
        object_type="advisor_response_received",
        observed_at=NOW + timedelta(seconds=21),
        invocation_ref=invocation.id,
    )
    invocation = ToolInvocation(
        investigation_id=invocation.investigation_id,
        operation_key=invocation.operation_key,
        tool_name=invocation.tool_name,
        schema_version=invocation.schema_version,
        normalized_parameters=invocation.normalized_parameters,
        purpose=invocation.purpose,
        status=invocation.status,
        response_refs=(call.id, response.id),
        id=invocation.id,
        created_at=invocation.created_at,
        updated_at=invocation.updated_at,
    )
    # The invocation must exist before observations may reference it.
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.tool_invocations.add_if_absent(invocation)
        await uow.commit()
    await _insert_observation(session_factory, call)
    await _insert_observation(session_factory, response)
    return invocation, [call.id, response.id]


async def _seed_plan_version(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    invocation: ToolInvocation,
    response_observation_id: UUID,
) -> UUID:
    uow = SqlAlchemyUnitOfWork(session_factory)
    await uow.__aenter__()
    try:
        service = InvestigationPlanService(
            plans=uow.investigation_plans,
            invocations=uow.tool_invocations,
            observations=uow.evidence_observations,
            investigations=uow.analyst_investigations,
        )
        try:
            version = await service.create_version(
                InvestigationPlanV1(
                    investigation_id=investigation_id,
                    version=1,
                    leads=(InvestigationLeadV1(title="Inspect the loader"),),
                ),
                parse_status=PlanParseStatus.PARSED,
                source_invocation_id=invocation.id,
                source_observation_id=response_observation_id,
                now=NOW + timedelta(seconds=22),
            )
        except VersionConflictError:  # pragma: no cover - fixture invariant
            raise
        await uow.commit()
        return version.id
    finally:
        await uow.__aexit__(None, None, None)


def test_timeline_union_deduplicates_and_labels_on_postgres(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            blob_root = tmp_path / "timeline-blobs"
            canonical = await _insert_observation(
                factory,
                await _build_blob_observation(
                    factory,
                    blob_root,
                    investigation.id,
                    source="virustotal",
                    object_type="vt_file_report",
                    observed_at=NOW + timedelta(seconds=5),
                ),
            )
            task, verification_event_id, decision = await _seed_task_with_events(
                factory, investigation.id
            )
            invocation, advisory_observation_ids = await _seed_bridge_consultation(
                factory, blob_root, investigation.id
            )
            plan_version_id = await _seed_plan_version(
                factory,
                investigation.id,
                invocation=invocation,
                response_observation_id=advisory_observation_ids[1],
            )

            async with SqlAlchemyUnitOfWork(factory) as uow:
                service = InvestigationTimelineService(
                    tasks=uow.investigation_tasks,
                    decisions=uow.analyst_decisions,
                    observations=uow.evidence_observations,
                    invocations=uow.tool_invocations,
                    plans=uow.investigation_plans,
                )
                first = await service.page(investigation.id, limit=3)
                second = await service.page(investigation.id, cursor=first.next_cursor, limit=20)

            items = (*first.items, *second.items)
            by_id = {item.object_id: item for item in items}
            assert first.has_more is True
            assert second.has_more is False
            assert len({item.object_id for item in items}) == len(items)
            keys = [(item.occurred_at, item.object_id) for item in items]
            assert keys == sorted(keys)

            assert by_id[canonical.id].evidence is True
            assert by_id[canonical.id].item_type is TimelineItemType.OBSERVATION
            assert by_id[canonical.id].task_id is None

            decision_item = by_id[decision.id]
            assert decision_item.item_type is TimelineItemType.DECISION
            assert decision_item.task_id == task.id
            assert decision_item.actor_kind is TimelineActorKind.HUMAN
            assert decision_item.linked_event_ids == (verification_event_id,)
            assert verification_event_id not in by_id

            consultation = by_id[invocation.id]
            assert consultation.item_type is TimelineItemType.BRIDGE_CONSULTATION
            assert consultation.linked_observation_ids == tuple(advisory_observation_ids)
            assert consultation.evidence is False
            for observation_id in advisory_observation_ids:
                assert observation_id not in by_id

            plan = by_id[plan_version_id]
            assert plan.item_type is TimelineItemType.PLAN_VERSION
            assert plan.evidence is False
        finally:
            await engine.dispose()

    asyncio.run(exercise())
