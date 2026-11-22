"""PostgreSQL oracle for the M24-X1 transfer flow.

One disposable database runs the real SQLAlchemy UoW through the transfer
service: the workspace scan/ingest commits canonical revisions, the human
assignment goes through the task engine, the new handoff version is appended
with canonical refs and a freshness diff against its predecessor, and an
idempotent replay appends nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command

from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    InvestigationHandoffService,
)
from cti_app.application.investigation_transfers import (
    InvestigationTransferService,
    TransferTaskRequest,
)
from cti_app.application.workspace_agent import (
    EventsPageV1,
    EventV1,
    FileStateV1,
    ScanResultV1,
    SnapshotPageV1,
    WorkspaceAgentPort,
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
)
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_handoff import HandoffActor, HandoffActorKind
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
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

HEAD = "0024_work_contributions"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=10)
NOTE_PATH = "notes/humaines/analyse.md"
WORKSPACE_ID = "ws-" + "b" * 32
HUMAN_ACTOR = HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test")
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
CLAUDE_ID = "operator:claude_code"
OLD_SHA = "1" * 64
NEW_SHA = "2" * 64


class _ScannedWorkspace:
    """A page feed with one newer revision of the human note."""

    async def scan_workspace(
        self,
        workspace_id: str,
        *,
        full: bool = False,
        idempotency_key: str | None = None,
    ) -> ScanResultV1:
        assert workspace_id == WORKSPACE_ID
        return ScanResultV1(generation=1, latest_cursor=1, emitted=1, policy_violations=0)

    async def events(
        self, workspace_id: str, *, after: int | None = None, limit: int = 500
    ) -> EventsPageV1:
        assert workspace_id == WORKSPACE_ID
        if after is not None:
            return EventsPageV1(
                generation=1, events=[], next_cursor=after, oldest_retained=0, resync_required=False
            )
        return EventsPageV1(
            generation=1,
            events=[
                EventV1(
                    event_id="evt-transfer-1",
                    seq=1,
                    generation=1,
                    kind="file_modified",
                    path=NOTE_PATH,
                    revision_id=workspace_revision_id(
                        workspace_id=WORKSPACE_ID, path=NOTE_PATH, sha256=NEW_SHA
                    ),
                    sha256=NEW_SHA,
                    size=256,
                    origin="ui",
                    observed_at=LATER,
                )
            ],
            next_cursor=1,
            oldest_retained=0,
            resync_required=False,
        )

    async def snapshot(
        self, workspace_id: str, *, page_token: str | None = None, limit: int = 500
    ) -> SnapshotPageV1:
        assert workspace_id == WORKSPACE_ID
        return SnapshotPageV1(
            generation=1,
            files=[
                FileStateV1(
                    path=NOTE_PATH,
                    revision_id=workspace_revision_id(
                        workspace_id=WORKSPACE_ID, path=NOTE_PATH, sha256=NEW_SHA
                    ),
                    sha256=NEW_SHA,
                    size=256,
                    mode="0644",
                    mtime_ns=0,
                    origin="ui",
                    observed_at=LATER,
                    policy_class="human_editable",
                )
            ],
            next_page_token=None,
        )


async def _seed_investigation(session_factory) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Transfer fixture {suffix}",
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
            title="Transfer fixture",
            slug=f"transfer-fixture-{suffix}",
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


async def _seed_workspace(
    session_factory, investigation: AnalystInvestigation
) -> WorkspaceFileRevisionV1:
    binding = WorkspaceBindingV1(
        workspace_id=WORKSPACE_ID,
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        delivery_code="IR202611",
        article_slug="loader-article",
        synthesis_sha256="e" * 64,
        created_at=NOW,
    )
    old = WorkspaceFileRevisionV1(
        workspace_id=WORKSPACE_ID,
        revision_id=workspace_revision_id(
            workspace_id=WORKSPACE_ID, path=NOTE_PATH, sha256=OLD_SHA
        ),
        path=NOTE_PATH,
        sha256=OLD_SHA,
        size=128,
        mode="0644",
        mtime_ns=0,
        observed_at=NOW,
        origin="ui",
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.workspace_bindings.add_if_absent(binding)
        stored, _created = await uow.workspace_file_revisions.append_if_absent(old)
        await uow.commit()
    return stored


async def _seed_task(session_factory, investigation_id: UUID) -> InvestigationTaskV1:
    task = InvestigationTaskV1(
        id=uuid4(),
        investigation_id=investigation_id,
        task_key="lead-loader",
        title="Inspect the loader",
        question="Which network gate does the loader use?",
        objective="Identify the first-stage C2 gate.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=HUMAN,
        created_at=NOW,
        updated_at=NOW,
        allowed_workspace_paths=("notes/humaines", "results"),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await uow.investigation_tasks.add(task)
        await uow.commit()
    return stored


async def _seed_decision(session_factory, investigation_id: UUID, task_id: UUID):
    decision = AnalystDecision(
        investigation_id=investigation_id,
        decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
        target_type=AnalystDecisionTargetType.TASK,
        target_id=task_id,
        actor_id="analyst:test",
        reason="Accept the interim result for the transfer",
        correlation_id=f"transfer-{uuid4()}",
        occurred_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.analyst_decisions.append(decision)
        await uow.commit()
    return decision


def _service(session_factory) -> InvestigationTransferService:
    return InvestigationTransferService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        workspace_agent=cast(WorkspaceAgentPort, _ScannedWorkspace()),
        clock=lambda: NOW,
    )


def test_transfer_flow_ingests_reassigns_handoff_and_replays(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            old_revision = await _seed_workspace(factory, investigation)
            task = await _seed_task(factory, investigation.id)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored_task = await uow.investigation_tasks.get(task.id)
                stored_investigation = await uow.analyst_investigations.get(investigation.id)
            assert stored_task is not None and stored_investigation is not None
            assert stored_task.investigation_id == stored_investigation.id, (
                stored_task.investigation_id,
                stored_investigation.id,
                investigation.id,
            )
            decision = await _seed_decision(factory, investigation.id, task.id)

            previous = await InvestigationHandoffService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory), clock=lambda: NOW
            ).create(
                investigation.id,
                actor=HUMAN_ACTOR,
                idempotency_key="seed-handoff",
                request=HandoffCreateRequest(
                    task_id=task.id,
                    to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=CLAUDE_ID),
                    file_revision_ids=(old_revision.revision_id,),
                    next_action="Continue from the previous analysis.",
                ),
            )
            assert previous.handoff.version == 1

            service = _service(factory)
            request = TransferTaskRequest(
                to_actor_kind="operator",
                to_adapter_kind=OperatorAdapterKind.CLAUDE_CODE,
                instruction="Deepen the protocol check.",
                expected_status_version=1,
            )
            result = await service.transfer(
                investigation.id,
                task_id=task.id,
                actor=HUMAN,
                idempotency_key="pg-transfer-1",
                request=request,
            )

            assert result.replayed is False
            assert result.sync is not None
            assert result.sync.inserted_events == 1
            assert result.sync.inserted_revisions == 1
            assert result.task.status is TaskStatus.ASSIGNED
            assert result.task.assigned_actor_id == CLAUDE_ID
            handoff = result.handoff.handoff
            assert handoff.version == 2
            assert handoff.next_action == "Deepen the protocol check."
            assert handoff.to_actor.actor_id == CLAUDE_ID
            assert [item.file_relpath for item in handoff.files] == [NOTE_PATH]
            assert handoff.files[0].workspace_revision_id != old_revision.revision_id
            assert handoff.files[0].file_sha256 == NEW_SHA
            assert {item.analyst_decision_id for item in handoff.refs.decisions} == {
                str(decision.id)
            }
            assert result.previous_handoff_id == previous.handoff.handoff_id
            assert result.previous_freshness is not None
            assert result.previous_freshness.state == "stale"
            assert [item.kind.value for item in result.previous_freshness.diffs] == ["file"]
            assert result.previous_freshness.diffs[0].recorded == OLD_SHA
            assert result.previous_freshness.diffs[0].current == NEW_SHA

            replay = await service.transfer(
                investigation.id,
                task_id=task.id,
                actor=HUMAN,
                idempotency_key="pg-transfer-1",
                request=request,
            )
            assert replay.replayed is True
            assert replay.handoff.handoff.handoff_id == handoff.handoff_id
            assert replay.sync is None

            async with SqlAlchemyUnitOfWork(factory) as uow:
                rows = await uow.investigation_handoffs.list_for_investigation(
                    investigation.id, task_id=task.id
                )
                revisions = await uow.workspace_file_revisions.list_for_workspace(WORKSPACE_ID)
            assert [record.handoff.version for record in rows] == [1, 2]
            assert len(revisions) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())

    async def version() -> str:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                assert isinstance(value, str)
                return value
        finally:
            await engine.dispose()

    assert asyncio.run(version()) == HEAD
