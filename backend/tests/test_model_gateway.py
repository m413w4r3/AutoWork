from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import (
    BinaryModelInputError,
    ExternalModelBlockedError,
    ModelGateway,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    sanitize_model_request,
)
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRunStatus
from cti_app.integrations.models import (
    FakeModelAdapter,
    InMemoryModelOutputStore,
    OpenAIResearchAdapter,
    OpenAIStructuredAdapter,
    QwenAdapter,
)
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


class SequencedResponsesTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.create_calls = 0
        self.retrieve_calls = 0

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self.create_calls += 1
        return self._responses[0]

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        assert response_id == "resp_background"
        self.retrieve_calls += 1
        return self._responses[min(self.retrieve_calls, len(self._responses) - 1)]


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


def request(
    *,
    external_llm_allowed: bool,
    background: bool = False,
    routing_hint: ModelRoutingHint = ModelRoutingHint.WEB_RESEARCH,
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
    )


def gateway_with_transport(
    transport: SequencedResponsesTransport,
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

    assert router.select(bulk, ModelRole.STRUCTURED_EXTRACTION).provider is ModelProvider.QWEN
    assert router.select(premium, ModelRole.DRAFTING).provider is ModelProvider.OPENAI


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
