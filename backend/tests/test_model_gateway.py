from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel

from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    BinaryModelInputError,
    ExternalModelBlockedError,
    ModelCapabilities,
    ModelCapabilityError,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
    sanitize_model_request,
)
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelTransport,
    ModelUsage,
)
from cti_app.integrations.models import (
    BridgeTransportError,
    ChatCompletionsTransport,
    ChatCompletionsTransportError,
    ChatGPTBridgeClient,
    FakeModelAdapter,
    HttpChatCompletionsTransport,
    InMemoryModelOutputStore,
    OpenAICompatibleChatAdapter,
    OpenAIResearchAdapter,
    OpenAIStructuredAdapter,
    QwenAdapter,
    ResponsesTransport,
)
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


def test_structured_output_capability_is_opt_in() -> None:
    assert ModelCapabilities().structured_output is False
    assert OpenAIStructuredAdapter.capabilities.structured_output is True
    assert (
        QwenAdapter(
            FixedChatTransport(), model="Qwen3-32B", is_external=False
        ).capabilities.structured_output
        is True
    )
    assert (
        OpenAICompatibleChatAdapter(
            FixedChatTransport(),
            provider=ModelProvider.GEMINI,
            backend=ModelBackend.GEMINI_WEBAI,
            model="gemini-3-flash",
            is_external=True,
        ).capabilities.structured_output
        is False
    )


class SequencedResponsesTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.create_calls = 0
        self.retrieve_calls = 0
        self.idempotency_keys: list[str | None] = []

    async def create(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        del payload
        self.create_calls += 1
        self.idempotency_keys.append(idempotency_key)
        return self._responses[0]

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        assert response_id == "resp_background"
        self.retrieve_calls += 1
        return self._responses[min(self.retrieve_calls, len(self._responses) - 1)]


class FailingResponsesTransport:
    async def create(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        del payload, idempotency_key
        raise BridgeTransportError(
            "bridge_auth_failed",
            "L'authentification auprès du bridge a échoué.",
            retryable=False,
            attempts=1,
            phase="generation",
        )

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        del response_id
        raise AssertionError("not used")


class SubmissionAwareResponsesTransport:
    def __init__(self, *, submission_state: str) -> None:
        self.submission_state = submission_state
        self.calls = 0
        self.idempotency_keys: list[str | None] = []

    async def create(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        del payload
        self.calls += 1
        self.idempotency_keys.append(idempotency_key)
        if self.calls == 1:
            raise BridgeTransportError(
                "bridge_ui_timeout",
                "bridge failure",
                retryable=True,
                attempts=1,
                phase="submission_confirmation",
                submission_state=self.submission_state,
                diagnostics={
                    "user_turns_before": 1,
                    "composer_text": "must not persist",
                },
            )
        return {
            "id": "resp_recovered",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": "recovered",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        del response_id
        raise AssertionError("not used")


class NoCallChatTransport:
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise AssertionError("Qwen transport should not be called")


class FixedChatTransport:
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "qwen-local",
            "model": str(payload["model"]),
            "choices": [{"message": {"content": "Traitement local"}}],
        }


class FailingChatTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self.calls += 1
        raise ModelGatewayError("Qwen outcome is unknown")


class NeedsReviewAdapter:
    provider = ModelProvider.OPENAI
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    capabilities = ModelCapabilities(web_search=True, background=True, conversation=True)
    requested_model = "chatgpt-web"
    is_external = True

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(
        self, request: Any, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del request, role, output_schema
        self.calls += 1
        return AdapterResult(
            status=AdapterResultStatus.NEEDS_REVIEW,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(),
            metadata={
                "reason": "active_signal_stalled",
                "completion_signal": "streaming",
            },
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("not used")


def request(
    *,
    external_llm_allowed: bool,
    background: bool = False,
    routing_hint: ModelRoutingHint = ModelRoutingHint.WEB_RESEARCH,
    run_id: UUID | None = None,
    provider: ModelProvider | None = None,
    backend: ModelBackend | None = None,
) -> ModelRequest:
    return ModelRequest(
        text="Analyse token=super-secret /home/analyst/private/report.txt",
        prompt_template_id="research-monthly",
        prompt_template_version="1.0",
        evidence_pack_hash="e" * 64,
        external_llm_allowed=external_llm_allowed,
        routing_hint=routing_hint,
        metadata={
            "publisher": "example",
            "api_key": "must-disappear",
            "actor_id": "internal-user-id",
            "note": "Authorization: Bearer metadata-secret",
        },
        parameters={"reasoning": {"effort": "high"}},
        background=background,
        run_id=run_id,
        provider=provider,
        backend=backend,
    )


def gateway_with_transport(
    transport: ResponsesTransport,
) -> tuple[ModelGateway, InMemoryModelRunUnitOfWorkFactory, InMemoryModelOutputStore]:
    openai_research = OpenAIResearchAdapter(transport, model="chatgpt-web")
    openai_structured = OpenAIStructuredAdapter(transport, model="chatgpt-web")
    qwen = QwenAdapter(NoCallChatTransport(), model="Qwen3-32B", is_external=False)
    fake = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    router = ModelRouter(
        openai_research=openai_research,
        openai_structured=openai_structured,
        qwen=qwen,
        fake=fake,
    )
    return ModelGateway(router, model_uow, output_store), model_uow, output_store


async def test_external_llm_policy_blocks_before_transport() -> None:
    transport = SequencedResponsesTransport([])
    gateway, model_uow, _ = gateway_with_transport(transport)

    with pytest.raises(ExternalModelBlockedError):
        await gateway.research(request(external_llm_allowed=False))

    assert transport.create_calls == 0
    run = next(iter(model_uow.state.values()))
    assert run.status is ModelRunStatus.BLOCKED
    assert run.error_code == "external_llm_blocked"


async def test_typed_bridge_error_details_are_persisted_safely() -> None:
    gateway, model_uow, _ = gateway_with_transport(FailingResponsesTransport())

    with pytest.raises(BridgeTransportError):
        await gateway.research(request(external_llm_allowed=True))

    run = next(iter(model_uow.state.values()))
    assert run.error_code == "bridge_auth_failed"
    assert run.error_details == {
        "provider": "openai_chatgpt_bridge",
        "phase": "generation",
        "retryable": False,
        "attempts": 1,
    }


async def test_first_submission_uses_attempt_one_bridge_identity() -> None:
    transport = SequencedResponsesTransport(
        [
            {
                "id": "resp_first",
                "status": "completed",
                "model": "chatgpt-web",
                "output_text": "first",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        ]
    )
    gateway, model_uow, _ = gateway_with_transport(transport)
    model_request = request(external_llm_allowed=True, run_id=uuid4())

    await gateway.research(model_request)

    assert model_request.run_id is not None
    run = model_uow.state[model_request.run_id]
    assert run.submission_attempt == 1
    assert transport.idempotency_keys == [f"{run.id}:a1"]


async def test_attempted_bridge_failure_is_reconciliation_only_and_keeps_diagnostics() -> None:
    transport = SubmissionAwareResponsesTransport(submission_state="submission_attempted")
    gateway, model_uow, _ = gateway_with_transport(transport)
    model_request = request(external_llm_allowed=True, run_id=uuid4())

    with pytest.raises(ModelSubmissionReconciliationRequiredError) as caught:
        await gateway.research(model_request)
    with pytest.raises(ModelSubmissionReconciliationRequiredError):
        await gateway.research(model_request)

    run = model_uow.state[model_request.run_id]
    assert transport.calls == 1
    assert caught.value.code == "model_submission_reconciliation_required"
    assert caught.value.retryable is False
    assert caught.value.phase == "reconciliation"
    assert run.status is ModelRunStatus.NEEDS_REVIEW
    assert run.error_code == "model_submission_reconciliation_required"
    assert run.submission_state.value == "submitted_or_unknown"
    assert run.submission_attempt == 1
    assert run.error_details == {
        "provider": "openai_chatgpt_bridge",
        "phase": "submission_confirmation",
        "retryable": True,
        "attempts": 1,
        "submission_state": "submission_attempted",
        "bridge_request_id": f"{run.id}:a1",
        "reconciliation_phase": "reconciliation",
        "bridge_diagnostics": {"user_turns_before": 1},
    }


async def test_proven_pre_submission_bridge_failure_can_be_explicitly_retried() -> None:
    transport = SubmissionAwareResponsesTransport(submission_state="pre_submission")
    gateway, model_uow, _ = gateway_with_transport(transport)
    model_request = request(external_llm_allowed=True, run_id=uuid4())

    with pytest.raises(BridgeTransportError):
        await gateway.research(model_request)

    run = model_uow.state[model_request.run_id]
    assert run.submission_state.value == "not_submitted"
    assert run.submission_attempt == 1
    assert transport.idempotency_keys == [f"{run.id}:a1"]

    replay = request(
        external_llm_allowed=True,
        run_id=model_request.run_id,
    )
    replay = replace(replay, allow_failed_resubmit=True)
    execution = await gateway.research(replay)

    assert execution.output_text == "recovered"
    assert transport.calls == 2
    assert execution.run.submission_attempt == 2
    assert transport.idempotency_keys == [f"{run.id}:a1", f"{run.id}:a2"]


async def test_qwen_trusted_gateway_runs_when_external_llm_is_forbidden() -> None:
    qwen = QwenAdapter(FixedChatTransport(), model="Qwen3-32B", is_external=False)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    router = ModelRouter(
        openai_research=FakeModelAdapter(),
        openai_structured=FakeModelAdapter(),
        qwen=qwen,
        fake=FakeModelAdapter(),
    )
    gateway = ModelGateway(router, model_uow, InMemoryModelOutputStore())

    execution = await gateway.draft(
        request(
            external_llm_allowed=False,
            routing_hint=ModelRoutingHint.STANDARD_DRAFT,
        )
    )

    assert execution.run.provider is ModelProvider.QWEN
    assert execution.run.status is ModelRunStatus.SUCCEEDED
    assert execution.output_text == "Traitement local"


def test_sanitizer_removes_secrets_paths_and_internal_metadata() -> None:
    cleaned = sanitize_model_request(request(external_llm_allowed=True))

    assert "super-secret" not in cleaned.text
    assert "/home/analyst" not in cleaned.text
    assert cleaned.metadata == {
        "publisher": "example",
        "note": "Authorization: [REDACTED]",
    }
    assert "metadata-secret" not in str(cleaned.metadata)
    assert len(cleaned.authorized_input_hash) == 64


def test_router_prefers_qwen_for_bulk_and_openai_for_premium_drafting() -> None:
    transport = SequencedResponsesTransport([])
    gateway, _, _ = gateway_with_transport(transport)
    router = gateway._router

    bulk = request(
        external_llm_allowed=False,
        routing_hint=ModelRoutingHint.BULK_EXTRACTION,
    )
    premium = request(
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.PREMIUM_SYNTHESIS,
    )
    discovery_merge = request(
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.DISCOVERY_MERGE,
    )

    assert router.select(bulk, ModelRole.STRUCTURED_EXTRACTION).provider is ModelProvider.QWEN
    assert router.select(premium, ModelRole.DRAFTING).provider is ModelProvider.OPENAI
    assert router.select(discovery_merge, ModelRole.DRAFTING).provider is ModelProvider.OPENAI


async def test_gemini_route_persists_provider_backend_and_transport() -> None:
    gemini = OpenAICompatibleChatAdapter(
        FixedChatTransport(),
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model="gemini-3-flash",
        is_external=True,
    )
    router = ModelRouter(
        openai_research=FakeModelAdapter(),
        openai_structured=FakeModelAdapter(),
        qwen=FakeModelAdapter(),
        gemini=gemini,
        fake=FakeModelAdapter(),
    )
    gateway = ModelGateway(router, InMemoryModelRunUnitOfWorkFactory(), InMemoryModelOutputStore())

    execution = await gateway.draft(
        request(
            external_llm_allowed=True,
            routing_hint=ModelRoutingHint.PREMIUM_SYNTHESIS,
            backend=ModelBackend.GEMINI_WEBAI,
        )
    )

    assert execution.run.provider is ModelProvider.GEMINI
    assert execution.run.backend is ModelBackend.GEMINI_WEBAI
    assert execution.run.transport is ModelTransport.OPENAI_CHAT_COMPLETIONS


async def test_gemini_rejects_unsupported_capabilities_before_transport() -> None:
    transport = NoCallChatTransport()
    gemini = OpenAICompatibleChatAdapter(
        transport,
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model="gemini-3-flash",
        is_external=True,
    )
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            gemini=gemini,
            fake=FakeModelAdapter(),
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )

    with pytest.raises(ModelGatewayError, match="web_search"):
        await gateway.research(
            replace(
                request(
                    external_llm_allowed=True,
                    backend=ModelBackend.GEMINI_WEBAI,
                ),
                web_search=True,
            )
        )


def test_binary_values_are_rejected_by_typed_request() -> None:
    with pytest.raises(BinaryModelInputError):
        ModelRequest(
            text="payload",
            prompt_template_id="binary",
            prompt_template_version="1",
            evidence_pack_hash="f" * 64,
            external_llm_allowed=False,
            routing_hint=ModelRoutingHint.BULK_EXTRACTION,
            metadata={"payload": b"MZ"},
        )


async def test_succeeded_run_reloads_persisted_output_without_network_call() -> None:
    fake = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            fake=fake,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    run_id = uuid4()
    model_request = request(
        external_llm_allowed=False,
        routing_hint=ModelRoutingHint.STANDARD_DRAFT,
        provider=ModelProvider.FAKE,
        run_id=run_id,
    )

    first = await gateway.draft(model_request)
    second = await gateway.draft(model_request)

    assert first.run.id == second.run.id == run_id
    assert second.output_text == first.output_text
    assert len(fake.calls) == 1
    assert second.run.submission_attempt == 1
    assert second.metadata["checkpoint"] == "hit"


async def test_needs_review_run_is_never_resubmitted() -> None:
    adapter = NeedsReviewAdapter()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=FakeModelAdapter(),
            fake=FakeModelAdapter(),
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )
    model_request = request(external_llm_allowed=True, run_id=uuid4())

    first = await gateway.research(model_request)
    with pytest.raises(ModelGatewayError, match="reconciliation"):
        await gateway.research(model_request)

    assert first.run.status is ModelRunStatus.NEEDS_REVIEW
    assert first.run.error_code == "active_signal_stalled"
    assert first.run.error_message == ("ChatGPT s'est arrêté sans produire de réponse finale.")
    assert first.run.error_details == first.metadata
    assert first.metadata["reason"] == "active_signal_stalled"
    assert first.output_text is None
    assert adapter.calls == 1


async def test_running_not_submitted_run_is_claimed_exactly_once() -> None:
    """Regression for P23.6: ModelConversationService pre-persists the ModelRun
    (RUNNING/NOT_SUBMITTED) before ever calling the gateway, so this must be a
    legitimate first submission rather than a rejected replay."""
    fake = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            fake=fake,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    model_request = request(
        external_llm_allowed=False,
        routing_hint=ModelRoutingHint.STANDARD_DRAFT,
        provider=ModelProvider.FAKE,
        run_id=uuid4(),
    )
    assert model_request.run_id is not None
    pre_persisted = gateway.build_run(model_request, ModelRole.DRAFTING)
    assert pre_persisted.status is ModelRunStatus.RUNNING
    assert pre_persisted.submission_state.value == "not_submitted"
    model_uow.state[pre_persisted.id] = pre_persisted

    execution = await gateway.draft(model_request)

    assert execution.run.status is ModelRunStatus.SUCCEEDED
    assert len(fake.calls) == 1

    # Replaying the same call now hits the persisted checkpoint, not the adapter.
    replay = await gateway.draft(model_request)
    assert replay.run.status is ModelRunStatus.SUCCEEDED
    assert len(fake.calls) == 1


async def test_running_submitted_or_unknown_run_is_never_resubmitted() -> None:
    """A run that made it past the initial-submission claim is a possible
    duplicate-in-flight and must never be reposted."""
    fake = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            fake=fake,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    model_request = request(
        external_llm_allowed=False,
        routing_hint=ModelRoutingHint.STANDARD_DRAFT,
        provider=ModelProvider.FAKE,
        run_id=uuid4(),
    )
    assert model_request.run_id is not None
    pre_persisted = gateway.build_run(model_request, ModelRole.DRAFTING)
    pre_persisted.begin_submission_attempt()
    assert pre_persisted.submission_state.value == "submitted_or_unknown"
    model_uow.state[pre_persisted.id] = pre_persisted

    with pytest.raises(ModelSubmissionReconciliationRequiredError):
        await gateway.draft(model_request)

    assert len(fake.calls) == 0
    assert model_uow.state[pre_persisted.id].status is ModelRunStatus.NEEDS_REVIEW
    assert model_uow.state[pre_persisted.id].submission_attempt == 1


async def test_qwen_unknown_failure_is_not_resubmitted() -> None:
    transport = FailingChatTransport()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=QwenAdapter(transport, model="Qwen3-32B", is_external=False),
            fake=FakeModelAdapter(),
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    model_request = request(
        external_llm_allowed=False,
        routing_hint=ModelRoutingHint.STANDARD_DRAFT,
        run_id=uuid4(),
    )
    assert model_request.run_id is not None

    with pytest.raises(ModelSubmissionReconciliationRequiredError):
        await gateway.draft(model_request)
    with pytest.raises(ModelSubmissionReconciliationRequiredError):
        await gateway.draft(model_request)

    run = model_uow.state[model_request.run_id]
    assert run.status is ModelRunStatus.NEEDS_REVIEW
    assert run.error_code == "model_submission_reconciliation_required"
    assert run.submission_state.value == "submitted_or_unknown"
    assert run.submission_attempt == 1
    assert transport.calls == 1


async def test_bridge_recovery_output_is_adopted_and_reused() -> None:
    gateway, model_uow, _ = gateway_with_transport(FailingResponsesTransport())
    model_request = request(external_llm_allowed=True, run_id=uuid4())
    assert model_request.run_id is not None

    with pytest.raises(BridgeTransportError):
        await gateway.research(model_request)
    recovered = await gateway.adopt_recovery_output(
        model_request.run_id,
        b"Recovered bridge answer",
        provenance="visible_recovery",
        actor_id="reviewer",
    )
    execution = await gateway.research(model_request)

    assert recovered.status is ModelRunStatus.SUCCEEDED
    assert execution.output_text == "Recovered bridge answer"
    assert model_uow.state[model_request.run_id].status is ModelRunStatus.SUCCEEDED


async def test_background_openai_response_is_resumed_by_job_polling() -> None:
    transport = SequencedResponsesTransport(
        [
            {
                "id": "resp_background",
                "status": "queued",
                "model": "chatgpt-web",
            },
            {
                "id": "resp_background",
                "status": "in_progress",
                "model": "chatgpt-web",
            },
            {
                "id": "resp_background",
                "status": "completed",
                "model": "chatgpt-web",
                "output_text": "Recherche terminée",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        ]
    )
    gateway, model_uow, output_store = gateway_with_transport(transport)
    execution = await gateway.research(request(external_llm_allowed=True, background=True))
    assert execution.run.status is ModelRunStatus.WAITING_BACKGROUND

    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway)
    job_service = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry, retry_base_seconds=0.001))
    job = await job_service.submit(
        kind="model.openai.background.poll",
        aggregate_type="model_run",
        aggregate_id=execution.run.id,
        idempotency_key=f"poll-{uuid4()}",
        correlation_id="background-test",
        input_parameters={"model_run_id": str(execution.run.id), "poll_number": 1},
        max_attempts=3,
    )

    await dispatcher.dispatch(job.id)

    completed_job = await job_service.get(job.id)
    completed_run = model_uow.state[execution.run.id]
    assert completed_job.status is JobStatus.SUCCEEDED
    assert completed_job.attempt == 2
    assert completed_run.status is ModelRunStatus.SUCCEEDED
    assert completed_run.response_id == "resp_background"
    assert completed_run.output_references[0].startswith("memory://model-outputs/")
    assert list(output_store.objects.values()) == [b"Recherche termin\xc3\xa9e"]


class _CountingChatTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self.calls += 1
        raise AssertionError("Gemini transport must not be called")


class _Extraction(BaseModel):
    title: str


def _gemini_gateway(
    transport: ChatCompletionsTransport,
    *,
    routing: dict[ModelRoutingHint, ModelBackend] | None = None,
) -> tuple[ModelGateway, InMemoryModelRunUnitOfWorkFactory]:
    gemini = OpenAICompatibleChatAdapter(
        transport,
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model="gemini-3-flash",
        is_external=True,
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    router = ModelRouter(
        openai_research=FakeModelAdapter(),
        openai_structured=FakeModelAdapter(),
        qwen=FakeModelAdapter(),
        gemini=gemini,
        fake=FakeModelAdapter(),
        routing=routing,
    )
    return ModelGateway(router, model_uow, InMemoryModelOutputStore()), model_uow


@pytest.mark.parametrize("routed", [False, True], ids=["explicit_backend", "configured_route"])
async def test_gemini_structured_extraction_fails_closed_before_transport(routed: bool) -> None:
    transport = _CountingChatTransport()
    gateway, model_uow = _gemini_gateway(
        transport,
        routing={ModelRoutingHint.BULK_EXTRACTION: ModelBackend.GEMINI_WEBAI} if routed else None,
    )
    model_request = request(
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.BULK_EXTRACTION,
        backend=None if routed else ModelBackend.GEMINI_WEBAI,
    )

    with pytest.raises(ModelCapabilityError) as caught:
        await gateway.extract(model_request, _Extraction)

    assert caught.value.backend is ModelBackend.GEMINI_WEBAI
    assert caught.value.capability == "structured_output"
    assert transport.calls == 0
    assert model_uow.state == {}


async def test_production_factory_builds_gemini_fail_closed_for_structured_output() -> None:
    from typing import cast

    from cti_app.application.persistence import UnitOfWorkFactory
    from cti_app.config import Settings
    from cti_app.integrations.model_factory import create_model_gateway

    def no_persistence() -> Any:
        raise AssertionError("A capability refusal must not open a unit of work")

    settings = Settings(_env_file=None, model_route_bulk_extraction="gemini_webai")
    gateway = create_model_gateway(settings, cast(UnitOfWorkFactory, no_persistence))
    gemini = gateway._router.by_backend(ModelBackend.GEMINI_WEBAI, ModelRole.STRUCTURED_EXTRACTION)

    assert settings.webai_model == "gemini-3-flash"
    assert gemini.requested_model == "gemini-3-flash"
    assert gemini.capabilities.structured_output is False
    with pytest.raises(ModelCapabilityError):
        await gateway.extract(
            request(external_llm_allowed=True, routing_hint=ModelRoutingHint.BULK_EXTRACTION),
            _Extraction,
        )


async def test_bridge_detail_pre_submission_fails_without_reconciliation() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "bridge_extension_disconnected",
                    "message": "Extension Chrome non connectée : ouvre un onglet chatgpt.com.",
                    "retryable": True,
                    "phase": "pre_submission",
                    "submission_state": "pre_submission",
                }
            },
        )

    run_id = uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway, model_uow, _ = gateway_with_transport(
            ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        )
        with pytest.raises(BridgeTransportError) as caught:
            await gateway.research(request(external_llm_allowed=True, run_id=run_id))

    run = model_uow.state[run_id]
    assert caught.value.submission_state == "pre_submission"
    assert calls == 1
    assert run.status is ModelRunStatus.FAILED
    assert run.error_code == "bridge_extension_disconnected"
    assert run.submission_state.value == "not_submitted"
    assert run.error_details["submission_state"] == "pre_submission"


async def test_gemini_http_refusals_are_typed_and_only_proven_ones_skip_reconciliation() -> None:
    statuses = iter([401, 400])
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses)
        calls.append(status)
        return httpx.Response(status, json={"detail": "refused"})

    def gemini_request(run_id: UUID) -> ModelRequest:
        return request(
            external_llm_allowed=True,
            routing_hint=ModelRoutingHint.PREMIUM_SYNTHESIS,
            backend=ModelBackend.GEMINI_WEBAI,
            run_id=run_id,
        )

    auth_run_id, bad_request_run_id = uuid4(), uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway, model_uow = _gemini_gateway(
            HttpChatCompletionsTransport(
                "http://web_ai.test/v1", api_key=None, provider="gemini", client=client
            )
        )
        with pytest.raises(ChatCompletionsTransportError) as refused:
            await gateway.draft(gemini_request(auth_run_id))
        with pytest.raises(ModelSubmissionReconciliationRequiredError):
            await gateway.draft(gemini_request(bad_request_run_id))
        # A replay of the unproven failure is sealed: no second POST.
        with pytest.raises(ModelSubmissionReconciliationRequiredError):
            await gateway.draft(gemini_request(bad_request_run_id))

    assert calls == [401, 400]
    assert refused.value.code == "provider_auth_failed"
    auth_run = model_uow.state[auth_run_id]
    assert auth_run.status is ModelRunStatus.FAILED
    assert auth_run.error_code == "provider_auth_failed"
    assert auth_run.submission_state.value == "not_submitted"
    bad_request_run = model_uow.state[bad_request_run_id]
    assert bad_request_run.status is ModelRunStatus.NEEDS_REVIEW
    assert bad_request_run.submission_state.value == "submitted_or_unknown"
    diagnostics = bad_request_run.error_details["bridge_diagnostics"]
    assert diagnostics["error_code"] == "provider_bad_request"
    assert diagnostics["http_status"] == 400
