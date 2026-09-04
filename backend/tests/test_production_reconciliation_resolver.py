from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.jobs import JobRegistry
from cti_app.application.model_gateway import ModelGatewayError
from cti_app.application.production_jobs import (
    ProductionReconciliationProbeParameters,
    ProductionStageChain,
    production_reconciliation_probe_job_kind,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.application.production_reconciliation_resolver import (
    ProductionReconciliationResolver,
    ReconciliationOutcome,
)
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
)
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.integrations.models import BridgeTransportError


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.items = {run.id: run}

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run


class _Models:
    def __init__(self, model: ModelRun) -> None:
        self.items = {model.id: model}

    async def get(self, run_id: UUID) -> ModelRun | None:
        return self.items.get(run_id)


class _BatchItems:
    async def get_by_run(self, run_id: UUID) -> None:
        del run_id
        return None


class _Uow:
    def __init__(self, run: SubjectProductionRun, model: ModelRun) -> None:
        self.subject_production_runs = _Runs(run)
        self.model_runs = _Models(model)
        self.edition_production_batch_items = _BatchItems()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Bridge:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        self.calls.append(response_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _ConversationService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, bool, UUID | None]] = []

    async def reconcile(
        self,
        conversation_id: UUID,
        *,
        available: bool,
        context_subject_id: UUID | None = None,
    ) -> object:
        self.calls.append((conversation_id, available, context_subject_id))
        return object()


class _Gateway:
    def __init__(self, model: ModelRun) -> None:
        self.model = model
        self.calls: list[dict[str, object]] = []

    async def adopt_recovery_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        provenance: str,
        actor_id: str,
        external_turn_id: str | None = None,
    ) -> ModelRun:
        if run_id != self.model.id:
            raise ModelGatewayError("wrong model run")
        self.calls.append(
            {
                "provenance": provenance,
                "actor_id": actor_id,
                "external_turn_id": external_turn_id,
            }
        )
        self.model.status = ModelRunStatus.SUCCEEDED
        self.model.raw_output_sha256 = hashlib.sha256(content).hexdigest()
        self.model.raw_output_reference = "blob://automatic-recovery"
        return self.model


def _fixture(
    bridge_result: dict[str, Any] | Exception,
    *,
    with_conversation: bool = True,
) -> tuple[
    ProductionReconciliationResolver,
    SubjectProductionRun,
    ModelRun,
    _Bridge,
    _ConversationService,
    _Gateway,
]:
    subject_id = uuid4()
    model_id = uuid4()
    conversation_id = uuid4() if with_conversation else None
    model = ModelRun(
        id=model_id,
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.RESEARCH,
        requested_model="chatgpt-web",
        prompt_template_id="production",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
        status=ModelRunStatus.NEEDS_REVIEW,
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        error_code="active_signal_stalled",
    )
    run = SubjectProductionRun(
        id=uuid4(),
        subject_id=subject_id,
        edition_id=uuid4(),
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.REFERENCES,
        references_conversation_id=conversation_id,
        error_code=PRODUCTION_RECONCILIATION_ERROR_CODE,
        error_details={"bridge_request_id": "bridge-request:a1"},
        reconciliation=ProductionSubmissionReconciliation(
            production_run_id=uuid4(),
            model_run_id=model_id,
            stage=SubjectProductionStage.REFERENCES,
            bridge_response_id=None,
            submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
            phase="reconciliation",
        ),
    )
    run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=model_id,
        stage=SubjectProductionStage.REFERENCES,
        bridge_response_id=None,
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )
    uow = _Uow(run, model)
    bridge = _Bridge(bridge_result)
    conversations = _ConversationService()
    gateway = _Gateway(model)
    resolver = ProductionReconciliationResolver(
        cast(Any, lambda: uow),
        bridge,
        cast(Any, gateway),
        cast(Any, conversations),
    )
    return resolver, run, model, bridge, conversations, gateway


@pytest.mark.asyncio
async def test_retryable_bridge_error_stays_undecided() -> None:
    resolver, run, _, bridge, conversations, gateway = _fixture(
        BridgeTransportError(
            "bridge_timeout",
            "timeout",
            retryable=True,
        )
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.UNDECIDED
    assert bridge.calls == ["bridge-request:a1"]
    assert run.requires_reconciliation
    assert conversations.calls == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_bridge_404_releases_and_marks_conversation_unavailable() -> None:
    resolver, run, _, bridge, conversations, gateway = _fixture(
        BridgeTransportError(
            "bridge_protocol_error",
            "not found",
            retryable=False,
            status_code=404,
        )
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.RELEASED
    assert bridge.calls == ["bridge-request:a1"]
    assert run.requires_reconciliation is False
    assert run.error_code == "bridge_run_unavailable"
    assert conversations.calls == [(run.references_conversation_id, False, run.subject_id)]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_terminal_success_adopts_non_empty_output_and_resumes() -> None:
    resolver, run, model, bridge, conversations, gateway = _fixture(
        {"id": "resp_123", "status": "completed", "output_text": "# answer"}
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.RESUMED
    assert bridge.calls == ["bridge-request:a1"]
    assert run.status is SubjectProductionStatus.RUNNING
    assert run.reconciliation is not None
    assert run.reconciliation.output_sha256 == hashlib.sha256(b"# answer").hexdigest()
    assert run.reconciliation.provenance == "automatic_bridge_retrieval"
    assert model.status is ModelRunStatus.SUCCEEDED
    assert gateway.calls[0]["provenance"] == "automatic_bridge_retrieval"
    assert conversations.calls == [(run.references_conversation_id, True, run.subject_id)]


@pytest.mark.asyncio
async def test_terminal_failure_releases_without_adopting_output() -> None:
    resolver, run, model, _, conversations, gateway = _fixture(
        {"id": "resp_123", "status": "failed", "error": {"code": "bridge_server_error"}}
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.RELEASED
    assert run.requires_reconciliation is False
    assert model.status is ModelRunStatus.NEEDS_REVIEW
    assert gateway.calls == []
    assert conversations.calls == [(run.references_conversation_id, False, run.subject_id)]


@pytest.mark.asyncio
async def test_failed_bridge_transport_result_releases_even_if_http_is_retryable() -> None:
    resolver, run, model, _, conversations, gateway = _fixture(
        BridgeTransportError(
            "bridge_server_error",
            "the bridge recorded a failed run",
            retryable=True,
            status_code=503,
            bridge_status="failed",
        )
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.RELEASED
    assert run.requires_reconciliation is False
    assert model.status is ModelRunStatus.NEEDS_REVIEW
    assert gateway.calls == []
    assert conversations.calls == [(run.references_conversation_id, False, run.subject_id)]


@pytest.mark.asyncio
async def test_in_progress_bridge_run_stays_undecided() -> None:
    resolver, run, model, _, conversations, gateway = _fixture(
        {"id": "resp_123", "status": "running"}
    )

    assert await resolver.resolve(run.id) is ReconciliationOutcome.UNDECIDED
    assert run.requires_reconciliation
    assert model.status is ModelRunStatus.NEEDS_REVIEW
    assert gateway.calls == []
    assert conversations.calls == []


@pytest.mark.asyncio
async def test_declared_lost_releases_and_records_the_analyst_claim(tmp_path: Path) -> None:
    resolver, run, _, _, conversations, gateway = _fixture(
        BridgeTransportError("bridge_timeout", "timeout", retryable=True)
    )
    resolver._diagnostics = DiagnosticsLog.from_env(tmp_path)

    assert (
        await resolver.release_declared_lost(
            run.id, "Chrome a été fermé et la conversation est introuvable", actor_id="analyst-1"
        )
        is ReconciliationOutcome.RELEASED
    )
    assert run.requires_reconciliation is False
    assert run.error_code == "production_reconciliation_declared_lost"
    assert conversations.calls == [(run.references_conversation_id, False, run.subject_id)]
    assert gateway.calls == []
    event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert event == {
        "at": event["at"],
        "event": "production.reconciliation_declared_lost",
        "run_id": str(run.id),
        "pid": event["pid"],
        "subject_id": str(run.subject_id),
        "stage": "references",
        "bridge_run_id": "bridge-request:a1",
        "actor_id": "analyst-1",
        "reason": "Chrome a été fermé et la conversation est introuvable",
    }


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[SimpleNamespace] = []

    async def submit(self, **kwargs: Any) -> SimpleNamespace:
        job = SimpleNamespace(id=uuid4(), **kwargs)
        self.submitted.append(job)
        return job


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.dispatched.append(job_id)


class _ProbeContext:
    job_id = uuid4()

    async def correlation_id(self) -> str:
        return "probe-test"

    async def check_cancelled(self) -> None:
        return None


@pytest.mark.asyncio
async def test_probe_404_restarts_the_same_production_stage_without_posting() -> None:
    resolver, run, _, bridge, _, _ = _fixture(
        BridgeTransportError(
            "bridge_protocol_error",
            "not found",
            retryable=False,
            status_code=404,
        ),
        with_conversation=False,
    )
    run.current_stage = SubjectProductionStage.SOURCES
    run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=run.reconciliation.model_run_id,
        stage=SubjectProductionStage.SOURCES,
        bridge_response_id=None,
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    chain = ProductionStageChain()
    chain.bind(cast(Any, jobs), cast(Any, dispatcher))
    registry = JobRegistry()
    register_production_jobs(
        registry,
        cast(Any, resolver._uow_factory),
        chain=chain,
        bridge_transport=bridge,
        model_gateway=cast(Any, resolver._model_gateway),
    )

    handler = registry.handler(production_reconciliation_probe_job_kind())
    result = await handler(
        ProductionReconciliationProbeParameters(run_id=run.id),
        _ProbeContext(),  # type: ignore[arg-type]
    )

    assert result.endswith("#released")
    assert run.status is SubjectProductionStatus.RUNNING
    assert run.pipeline_generation == 1
    assert [job.kind for job in jobs.submitted] == [
        stage_job_kind(SubjectProductionStage.SOURCES)
    ]
    assert bridge.calls == ["bridge-request:a1"]


@pytest.mark.asyncio
async def test_probe_logs_and_reschedules_an_undecided_bridge_result(
    tmp_path: Path,
) -> None:
    resolver, run, _, bridge, _, gateway = _fixture(
        BridgeTransportError("bridge_timeout", "timeout", retryable=True)
    )
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    chain = ProductionStageChain()
    chain.bind(cast(Any, jobs), cast(Any, dispatcher))
    registry = JobRegistry()
    diagnostics = DiagnosticsLog.from_env(tmp_path)
    register_production_jobs(
        registry,
        cast(Any, resolver._uow_factory),
        chain=chain,
        bridge_transport=bridge,
        model_gateway=cast(Any, gateway),
        diagnostics=diagnostics,
    )

    handler = registry.handler(production_reconciliation_probe_job_kind())
    result = await handler(
        ProductionReconciliationProbeParameters(run_id=run.id, attempt=0),
        _ProbeContext(),  # type: ignore[arg-type]
    )

    assert result.endswith("#retry")
    assert len(jobs.submitted) == 1
    assert jobs.submitted[0].kind == production_reconciliation_probe_job_kind()
    assert jobs.submitted[0].input_parameters["attempt"] == 1
    event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert event["event"] == "production.reconciliation_probe"
    assert event["run_id"] == str(run.id)
    assert event["subject_id"] == str(run.subject_id)
    assert event["bridge_run_id"] == "bridge-request:a1"
    assert event["attempt"] == 0
    assert event["outcome"] == ReconciliationOutcome.UNDECIDED.value
