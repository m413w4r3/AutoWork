"""PostgreSQL oracle for human plan acceptance and task materialisation replay.

Runs against a disposable database at head ``0021_investigation_tasks``.  The
acceptance decision and the materialised tasks/events/dependencies are written
in one unit of work; replaying the same plan version is a no-op and never
rewrites an existing task (ADR-0011 D2/E6).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_plan_materialization import (
    InvestigationPlanMaterializationService,
    plan_accept_decision_id,
    plan_task_key,
)
from cti_app.application.investigation_plans import InvestigationPlanService
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
    ToolInvocationActorKind,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.domain.investigation_task import TaskActorKind, TaskStatus
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
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
SHA = "a" * 64


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Plan accept fixture {suffix}",
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
            title="Plan accept fixture",
            slug=f"plan-accept-fixture-{suffix}",
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
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _advisor_exchange(
    session_factory: async_sessionmaker[AsyncSession],
    blob_root: Path,
    investigation_id: UUID,
) -> tuple[UUID, UUID]:
    blob_store = FilesystemBlobStore(blob_root)
    catalog = BlobCatalogService(blob_store, lambda: SqlAlchemyUnitOfWork(session_factory))
    blob = await catalog.ingest(
        BytesIO(json.dumps({"schema": "advisor-response-link.v1"}).encode()),
        logical_bucket="model-outputs",
        mime_type="application/json",
    )
    observation_id = uuid4()
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"advisor-plan-{uuid4()}",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters={
            "consultation_type": "plan_investigation",
            "question": "Build an investigation plan",
        },
        purpose="Ask Bridge for an investigation plan",
        status=ToolInvocationStatus.SUCCEEDED,
        response_refs=(observation_id,),
        actor_kind=ToolInvocationActorKind.HUMAN,
        actor_id="analyst:test",
        created_at=NOW,
        updated_at=NOW,
    )
    observation = EvidenceObservation(
        id=observation_id,
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=blob.descriptor.sha256,
        source="advisor.consult.response",
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type="advisor_response_received",
        object_id=f"advisor-plan-{uuid4()}",
        invocation_ref=invocation.id,
        limitations=("Raw Bridge text is untrusted advisory material.",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.tool_invocations.add_if_absent(invocation)
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return invocation.id, observation.id


async def _materialise(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    investigation_id: UUID,
    version_id: UUID,
    decision_id: UUID,
):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        version = await uow.investigation_plans.get_version(version_id)
        assert version is not None
        materializer = InvestigationPlanMaterializationService(tasks=uow.investigation_tasks)
        plan = await materializer.prepare(version)
        outcome = await materializer.apply(plan, decision_id=decision_id, now=NOW)
        await uow.commit()
        return outcome


def test_plan_accept_materialisation_replay_is_idempotent_postgres(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            session_factory = create_session_factory(engine)
            investigation = await _seed_investigation(session_factory)
            invocation_id, observation_id = await _advisor_exchange(
                session_factory, tmp_path / "plan-accept-blobs", investigation.id
            )
            lead_a = InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur")
            lead_b = InvestigationLeadV1(
                lead_id="lead-b",
                title="Cartographier le C2",
                depends_on=("lead-a",),
            )
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                service = InvestigationPlanService(
                    plans=uow.investigation_plans,
                    invocations=uow.tool_invocations,
                    observations=uow.evidence_observations,
                    investigations=uow.analyst_investigations,
                )
                version = await service.create_version(
                    InvestigationPlanV1(
                        investigation_id=investigation.id,
                        version=1,
                        leads=(lead_a, lead_b),
                    ),
                    parse_status=PlanParseStatus.PARSED,
                    source_invocation_id=invocation_id,
                    source_observation_id=observation_id,
                    now=NOW,
                )
                decision_id = plan_accept_decision_id(investigation.id, "accept-key")
                await uow.analyst_decisions.append(
                    AnalystDecision(
                        id=decision_id,
                        investigation_id=investigation.id,
                        decision_type=AnalystDecisionType.PLAN_ACCEPT,
                        target_type=AnalystDecisionTargetType.PLAN_VERSION,
                        target_id=version.id,
                        actor_id="analyst:test",
                        reason="Accept the investigation plan",
                        correlation_id="accept-key",
                    )
                )
                await uow.commit()
                version_id = version.id

            first = await _materialise(
                session_factory,
                investigation_id=investigation.id,
                version_id=version_id,
                decision_id=decision_id,
            )
            assert len(first.created_task_ids) == 2

            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                tasks = await uow.investigation_tasks.list_for_investigation(investigation.id)
                assert len(tasks) == 2
                by_key = {task.task_key: task for task in tasks}
                task_a = by_key[plan_task_key(investigation.id, lead_a)]
                task_b = by_key[plan_task_key(investigation.id, lead_b)]
                assert task_a.status is TaskStatus.READY
                assert task_b.status is TaskStatus.PROPOSED
                assert task_a.created_by.kind is TaskActorKind.SYSTEM
                assert task_a.created_by.actor_id == f"plan_accept:{decision_id}"
                assert await uow.investigation_tasks.list_dependencies(task_b.id) == (task_a.id,)
                events_a = await uow.investigation_tasks.list_events(task_a.id)
                events_b = await uow.investigation_tasks.list_events(task_b.id)
                assert [event.idempotency_key for event in events_a] == [
                    f"plan:{version_id}:{task_a.task_key}"
                ]
                assert [event.idempotency_key for event in events_b] == [
                    f"plan:{version_id}:{task_b.task_key}"
                ]
                assert events_a[0].analyst_decision_id == decision_id

            replay = await _materialise(
                session_factory,
                investigation_id=investigation.id,
                version_id=version_id,
                decision_id=decision_id,
            )
            assert replay.created_task_ids == ()
            assert len(replay.tasks) == 2

            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                tasks = await uow.investigation_tasks.list_for_investigation(investigation.id)
                assert len(tasks) == 2
                task_a = next(
                    task
                    for task in tasks
                    if task.task_key == plan_task_key(investigation.id, lead_a)
                )
                assert len(await uow.investigation_tasks.list_events(task_a.id)) == 1
                assert (
                    len(
                        await uow.investigation_tasks.list_dependencies(
                            next(
                                task.id
                                for task in tasks
                                if task.task_key == plan_task_key(investigation.id, lead_b)
                            )
                        )
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
