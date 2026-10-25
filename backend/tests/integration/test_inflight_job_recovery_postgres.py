"""PostgreSQL recovery oracle for a worker lost during an external submission."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    ModelConversationTurnTransport,
    make_investigation_conversation_id,
)
from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.investigation_jobs import INVESTIGATION_RUN_JOB_KIND
from cti_app.application.jobs import JobExecutor, SynchronousJobDispatcher
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelCapabilities,
    ModelGateway,
    ModelRouter,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    InvestigationExecutionMode,
)
from cti_app.domain.investigation_state import StopReason
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_conversations import (
    ConversationStatus,
    ConversationTurnStatus,
)
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
    ModelTransport,
    ModelUsage,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.investigation_audit_reader import (
    read_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import BlobModelOutputStore
from tests.workflow_emulator.investigation_support import (
    approve_envelope_http,
    build_investigation_app,
    investigation_settings,
    seed_verified_article,
    start_investigation_http,
)

pytestmark = pytest.mark.integration


class _SimulatedWorkerDeath(BaseException):
    """A process-loss signal that JobExecutor must not convert to job failure."""


class _ProviderSideFake:
    """Shared provider state, separate from either constructed application runtime."""

    def __init__(self) -> None:
        self.send_count = 0
        self.request_ids: list[str] = []
        self.crash_on_first_send = True

    async def send(self, request: Any) -> AdapterResult:
        self.send_count += 1
        request_id = request.request_id
        assert isinstance(request_id, str) and request_id
        self.request_ids.append(request_id)
        if self.crash_on_first_send:
            self.crash_on_first_send = False
            raise _SimulatedWorkerDeath("worker stopped after provider accepted the send")
        return AdapterResult(
            status=AdapterResultStatus.NEEDS_REVIEW,
            provider=ModelProvider.OPENAI,
            requested_model="m05c-synthetic-crash-recovery",
            actual_model_version="m05c-synthetic-crash-recovery",
            usage=ModelUsage(),
            metadata={
                "reason": "unexpected_second_send_in_recovery_oracle",
                "submission_state": ModelSubmissionState.EXTERNAL_STATE_UNKNOWN.value,
            },
        )


class _SimulatedModelAdapter:
    provider = ModelProvider.OPENAI
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    requested_model = "m05c-synthetic-crash-recovery"
    is_external = True
    is_configured = True
    capabilities = ModelCapabilities(conversation=True)

    def __init__(self, provider_fake: _ProviderSideFake) -> None:
        self._provider_fake = provider_fake

    async def invoke(
        self,
        request: Any,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        return await self._provider_fake.send(request)

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("The recovery path must not resume or resubmit a provider request.")


@dataclass(slots=True)
class _Runtime:
    engine: Any
    session_factory: Any
    uow_factory: Any
    artifact_store: ProductionArtifactStore
    model_conversations: ModelConversationService
    app: Any
    registry: Any
    executor: JobExecutor
    dispatcher: SynchronousJobDispatcher

    @property
    def job_service(self) -> Any:
        return self.app.state.job_service


def _build_runtime(
    database_url: str,
    blob_root: Path,
    provider_fake: _ProviderSideFake,
) -> _Runtime:
    engine = create_postgres_engine(database_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = FilesystemBlobStore(blob_root)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    adapter = _SimulatedModelAdapter(provider_fake)
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        openai_drafting=adapter,
        openai_critic=adapter,
        qwen=adapter,
        fake=adapter,
    )
    gateway = ModelGateway(
        router,
        uow_factory,
        BlobModelOutputStore(BlobCatalogService(blob_store, uow_factory)),
    )
    model_conversations = ModelConversationService(uow_factory, gateway, blob_store)
    composition_factory = InvestigationCompositionFactory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_conversations=model_conversations,
        settings=investigation_settings(providers=False),
        turn_transport=ModelConversationTurnTransport(model_conversations),
    )
    app, registry, _deferred_dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=composition_factory,
    )
    executor = JobExecutor(
        uow_factory,
        registry,
        # A killed worker does not live long enough to heartbeat. Recovery ages
        # the durable heartbeat directly to simulate elapsed process downtime.
        heartbeat_interval_seconds=3600,
    )
    return _Runtime(
        engine=engine,
        session_factory=session_factory,
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_conversations=model_conversations,
        app=app,
        registry=registry,
        executor=executor,
        dispatcher=SynchronousJobDispatcher(executor),
    )


async def _read_model_records(uow_factory: Any, investigation_id: UUID):
    conversation_id = make_investigation_conversation_id(investigation_id)
    async with uow_factory() as uow:
        conversation = await uow.model_conversations.get(conversation_id)
        assert conversation is not None
        turns = tuple(await uow.model_conversation_turns.list_for_conversation(conversation_id))
        runs = []
        for turn in turns:
            run = await uow.model_runs.get(turn.model_run_id)
            assert run is not None
            runs.append(run)
        return conversation, turns, tuple(runs)


@pytest.mark.asyncio
async def test_ambiguous_inflight_emission_recovers_without_second_provider_send(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    provider_fake = _ProviderSideFake()
    first = _build_runtime(migrated_postgres_url, tmp_path / "shared-blobs", provider_fake)
    fresh: _Runtime | None = None
    try:
        seeded = await seed_verified_article(
            first.uow_factory,
            first.artifact_store,
            suffix=uuid4().hex[:10],
        )
        investigation_id = seeded.investigation.id
        async with httpx.AsyncClient(
            transport=ASGITransport(app=first.app), base_url="http://test"
        ) as client:
            started = await start_investigation_http(client, seeded)
            job_id = UUID(started["job_id"])
            approved = await approve_envelope_http(
                client,
                investigation_id,
                providers=(),
                budget_limits={
                    "bridge_turns": 4,
                    "vt_units": 0,
                    "shodan_credits": 0,
                    "downloads": 0,
                    "tool_seconds": 20_000,
                },
                allow_acquisition=False,
            )
            assert approved.status_code == 200, approved.text

        # InvestigationService.run returns immediately in operator_led mode;
        # this job recovery path uses the default bridge-led runner to reach the
        # durable model submission claim.
        async with first.uow_factory() as uow:
            initial_state = await uow.investigation_states.get(investigation_id)
        assert initial_state is not None
        assert initial_state.execution_mode is InvestigationExecutionMode.BRIDGE_LED

        with pytest.raises(_SimulatedWorkerDeath):
            await first.dispatcher.dispatch(job_id)
        assert provider_fake.send_count == 1

        async with first.uow_factory() as uow:
            interrupted_job = await uow.jobs.get(job_id)
            assert interrupted_job is not None
            assert interrupted_job.status is JobStatus.RUNNING
            assert interrupted_job.attempt == 1
            assert interrupted_job.heartbeat_at is not None
            interrupted_job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
            await uow.jobs.save(interrupted_job)
            await uow.commit()

        interrupted_conversation, interrupted_turns, interrupted_runs = await _read_model_records(
            first.uow_factory, investigation_id
        )
        assert interrupted_conversation.status is ConversationStatus.BUSY
        assert len(interrupted_turns) == 1
        interrupted_turn = interrupted_turns[0]
        assert interrupted_turn.status is ConversationTurnStatus.RUNNING
        assert interrupted_turn.idempotency_key
        assert len(interrupted_runs) == 1
        interrupted_run = interrupted_runs[0]
        assert interrupted_run.status is ModelRunStatus.RUNNING
        assert interrupted_run.submission_state is ModelSubmissionState.EXTERNAL_STATE_UNKNOWN
        assert interrupted_run.submission_attempt == 1
        assert interrupted_run.bridge_request_id == provider_fake.request_ids[0]
        assert interrupted_turn.model_run_id == interrupted_run.id
        assert interrupted_turn.input_sha256 and len(interrupted_turn.input_sha256) == 64
        assert len(interrupted_run.authorized_input_hash) == 64
        assert len(interrupted_run.evidence_pack_hash) == 64

        audit_at_interruption = await read_investigation_audit_snapshot(
            first.session_factory, investigation_id, first.artifact_store
        )
        assert audit_at_interruption.envelope is not None
        interrupted_reservations = tuple(
            reservation
            for reservation in audit_at_interruption.envelope.budget.reservations
            if reservation.amounts.get(BudgetDimension.BRIDGE_TURNS, 0) > 0
        )
        assert len(interrupted_reservations) == 1
        interrupted_reservation = interrupted_reservations[0]
        assert interrupted_reservation.status is BudgetReservationStatus.RESERVED
        assert interrupted_reservation.amounts[BudgetDimension.BRIDGE_TURNS] == 1
        assert interrupted_reservation.committed_amounts.get(BudgetDimension.BRIDGE_TURNS, 0) == 0
        assert audit_at_interruption.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 0

        fresh = _build_runtime(migrated_postgres_url, tmp_path / "shared-blobs", provider_fake)
        assert fresh.engine is not first.engine
        assert fresh.session_factory is not first.session_factory
        assert fresh.uow_factory is not first.uow_factory
        assert fresh.job_service is not first.job_service
        assert (
            fresh.app.state.investigation_composition_factory
            is not first.app.state.investigation_composition_factory
        )
        assert fresh.model_conversations is not first.model_conversations
        assert fresh.executor is not first.executor
        assert fresh.dispatcher is not first.dispatcher
        fresh.app.state.job_dispatcher = fresh.dispatcher
        assert fresh.registry.resumes_after_worker_loss(INVESTIGATION_RUN_JOB_KIND)

        timeout = timedelta(seconds=30)
        async with fresh.uow_factory() as uow:
            stale_job = await uow.jobs.get(job_id)
        assert stale_job is not None
        assert stale_job.status is JobStatus.RUNNING
        assert stale_job.heartbeat_at is not None
        assert stale_job.heartbeat_at < datetime.now(UTC) - timeout

        recovered_jobs = await fresh.job_service.recover_abandoned(timeout)
        assert [item.id for item in recovered_jobs] == [job_id]
        async with fresh.uow_factory() as uow:
            queued_job = await uow.jobs.get(job_id)
        assert queued_job is not None
        assert queued_job.status is JobStatus.QUEUED
        assert queued_job.attempt == 0
        assert queued_job.idempotency_key == interrupted_job.idempotency_key
        assert queued_job.error_code == "worker_interrupted"
        assert queued_job.next_retry_at is not None
        recovery_events = await fresh.job_service.history(job_id)
        assert any(
            event.event_type == "job.heartbeat_recovered"
            and event.from_status is JobStatus.RUNNING
            and event.to_status is JobStatus.QUEUED
            for event in recovery_events
        )

        await fresh.dispatcher.dispatch(job_id)
        assert provider_fake.send_count == 1
        assert provider_fake.request_ids == [interrupted_run.bridge_request_id]

        async with fresh.uow_factory() as uow:
            final_job = await uow.jobs.get(job_id)
            state = await uow.investigation_states.get(investigation_id)
        assert final_job is not None and state is not None
        assert final_job.status is JobStatus.SUCCEEDED
        assert final_job.attempt == 1
        assert final_job.idempotency_key == interrupted_job.idempotency_key
        assert state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
        assert state.version >= 2
        assert len(state.sha256) == 64
        assert state.sha256 == hashlib.sha256(state.canonical_bytes()).hexdigest()

        final_conversation, final_turns, final_runs = await _read_model_records(
            fresh.uow_factory, investigation_id
        )
        assert final_conversation.status is ConversationStatus.NEEDS_REVIEW
        assert len(final_turns) == 1
        final_turn = final_turns[0]
        assert final_turn.id == interrupted_turn.id
        assert final_turn.idempotency_key == interrupted_turn.idempotency_key
        assert final_turn.model_run_id == interrupted_run.id
        assert final_turn.status is ConversationTurnStatus.NEEDS_REVIEW
        assert final_turn.error_code == "orphaned_running_turn"
        assert len(final_runs) == 1
        final_run = final_runs[0]
        assert final_run.id == interrupted_run.id
        assert final_run.status is ModelRunStatus.NEEDS_REVIEW
        assert final_run.submission_state is ModelSubmissionState.EXTERNAL_STATE_UNKNOWN
        assert final_run.submission_attempt == 1
        assert final_run.bridge_request_id == interrupted_run.bridge_request_id
        assert final_run.error_code == "orphaned_running_turn"
        assert final_run.error_details is not None
        assert final_run.error_details["recovery"] == "distinct_attempt_after_terminal_owner"
        assert final_run.prompt_template_id and final_run.prompt_template_version
        assert final_run.requested_model == _SimulatedModelAdapter.requested_model
        assert len(final_run.authorized_input_hash) == 64
        assert len(final_run.evidence_pack_hash) == 64

        final_audit = await read_investigation_audit_snapshot(
            fresh.session_factory, investigation_id, fresh.artifact_store
        )
        assert final_audit.hypotheses_state == state
        # The audit projection resolves a model conversation only through the
        # P10 pivot binding (`analyst_investigations.pivot_conversation_id`).
        # A Bridge-led investigation turn has no such binding, so the projection
        # exposes no conversation here (identical to the E2E-0 and M05-D
        # snapshots). The turn/run audit is asserted directly through the model
        # repositories above; the projection itself is checked through state,
        # source versions and the G2 envelope below.
        assert final_audit.pivot_conversation_id is None
        assert final_audit.conversation is None
        assert final_audit.conversation_turns == ()
        assert final_audit.model_runs == ()
        assert final_audit.source_versions["investigation_state"] == state.SCHEMA
        assert (
            final_audit.source_versions["model_conversation"] == "model-conversation.repository.v1"
        )
        assert final_audit.envelope is not None
        final_reservations = tuple(
            reservation
            for reservation in final_audit.envelope.budget.reservations
            if reservation.operation_key == interrupted_reservation.operation_key
        )
        assert len(final_reservations) == 1
        final_reservation = final_reservations[0]
        assert final_reservation.amounts == interrupted_reservation.amounts
        assert final_reservation.status is BudgetReservationStatus.COMMITTED
        assert final_reservation.committed_amounts[BudgetDimension.BRIDGE_TURNS] == 1
        assert final_audit.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1
        assert final_audit.envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 0

        # The recovery seals the uncertain durable operation. It does not grant
        # permission to allocate another submission attempt or conversation turn.
        assert provider_fake.send_count == 1
    finally:
        if fresh is not None:
            await fresh.engine.dispose()
        await first.engine.dispose()
