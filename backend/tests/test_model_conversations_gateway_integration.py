"""Real ModelConversationService -> real ModelGateway, for P23.6.

P23.4 only exercised the real gateway for Q2 (structured extraction); Q1/Q4
conversational turns kept going through `_FakeConversations`, a duck-typed
stand-in that never touches `ModelGateway._execute`. That gap is exactly
what let the P23.5 regression through: `ModelConversationService.add_turn`
pre-persists its `ModelRun` as RUNNING/NOT_SUBMITTED (for the turn's FK)
*before* ever calling the gateway, and `_execute` treated every RUNNING
existing run as a dangerous replay regardless of `submission_state` --
raising "Model run needs reconciliation before resubmission" on the very
first call.

This module drives `add_turn` end to end against a real `ModelGateway`
(backed by the in-memory `ModelRun`/conversation repositories in
`tests.conversation_support`) and a scripted fake OpenAI/bridge adapter --
never mocking `gateway.execute` itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationResult,
    ModelGateway,
    ModelGatewayError,
    ModelRouter,
    SafeModelRequest,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelUsage
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore, FakeModelAdapter
from tests.conversation_support import InMemoryConversationUnitOfWorkFactory


class _ScriptedBridgeAdapter:
    """Stands in for the ChatGPT bridge adapter: verifies the conversation and
    answers with a fixed text, exactly once per call."""

    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web"
    is_external = True

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls: list[SafeModelRequest] = []

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del output_schema
        self.calls.append(request)
        context = request.conversation
        assert context is not None
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            output_text=self._answer,
            conversation=ConversationResult(
                id=str(context.id),
                mode=context.mode,
                external_locator="https://chatgpt.com/opaque/scripted",
                turn_id="bridge-turn-1",
                verified=True,
            ),
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        raise ModelGatewayError("Scripted bridge adapter does not support background resume")


def _build_service(
    adapter: _ScriptedBridgeAdapter, tmp_path: Path
) -> tuple[ModelConversationService, InMemoryConversationUnitOfWorkFactory]:
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        qwen=FakeModelAdapter(),
        fake=FakeModelAdapter(),
    )
    conversation_uow = InMemoryConversationUnitOfWorkFactory()
    blob_store = FilesystemBlobStore(tmp_path)
    # Production wires the gateway's output store through the blob catalog
    # (`blob://...` references) rather than the plain in-memory store used
    # by the Q2-only gateway tests, because `ModelConversationService.turns`
    # reads a turn's output back through `uow.blobs` -- an in-memory-only
    # output store would silently desync from that read path.
    output_store = BlobModelOutputStore(
        BlobCatalogService(blob_store, conversation_uow)  # type: ignore[arg-type]
    )
    gateway = ModelGateway(router, conversation_uow, output_store)  # type: ignore[arg-type]
    service = ModelConversationService(
        conversation_uow,  # type: ignore[arg-type]
        gateway,
        blob_store,
    )
    return service, conversation_uow


async def test_q1_fresh_conversation_reaches_the_adapter_exactly_once(tmp_path: Path) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse Q1")
    service, _ = _build_service(adapter, tmp_path)
    conversation = await service.create(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.SUBJECT_RESEARCH,
        title="Recherche Q1",
        edition_id=None,
        subject_id=None,
        expected_profile=None,
        requested_model=None,
    )

    turn = await service.add_turn(
        conversation.id,
        message="Quelles sont les sources sur ce sujet ?",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        web_search=True,
        idempotency_key="q1-fresh-key",
        correlation_id="corr-q1",
    )

    assert turn.status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1
    assert adapter.calls[0].web_search is True

    updated = await service.get(conversation.id)
    assert updated.status is ConversationStatus.READY

    # The exact read path _ask_with_format_repair uses in production.
    contents = await service.turns(conversation.id)
    matching = [item for item in contents if item.turn.id == turn.id]
    assert len(matching) == 1
    assert matching[0].output_text == "Réponse Q1"

    # Replaying the same idempotency key must not touch the adapter again.
    replay = await service.add_turn(
        conversation.id,
        message="Quelles sont les sources sur ce sujet ?",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        web_search=True,
        idempotency_key="q1-fresh-key",
        correlation_id="corr-q1-replay",
    )
    assert replay.id == turn.id
    assert replay.status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1


async def test_q4_drafting_fresh_conversation_reaches_the_adapter_exactly_once(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse Q4")
    service, _ = _build_service(adapter, tmp_path)
    conversation = await service.create(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.DRAFTING,
        title="Synthèse Q4",
        edition_id=None,
        subject_id=None,
        expected_profile=None,
        requested_model=None,
    )

    turn = await service.add_turn(
        conversation.id,
        message="Rédige la synthèse",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="q4-fresh-key",
        correlation_id="corr-q4",
    )

    assert turn.status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1


class _CrashesBeforeGatewayClaimsTheRun(ModelGateway):
    """Simulates a failure between `add_turn` pre-persisting its ModelRun and
    `ModelGateway._execute` ever taking its lock on it -- `build_run` (used
    to construct the pre-persisted row) is untouched; only `execute` is
    short-circuited."""

    async def execute(self, request: Any, role: ModelRole) -> Any:
        del request, role
        raise RuntimeError("simulated crash before the gateway claimed the run")


async def test_pre_submission_crash_does_not_leave_the_model_run_running(
    tmp_path: Path,
) -> None:
    """Regression guard for P23.6 part B: a turn that fails before the
    gateway ever claims its pre-persisted ModelRun must not leave that run
    RUNNING/NOT_SUBMITTED forever -- it is transitioned to FAILED, still
    provably never submitted."""
    adapter = _ScriptedBridgeAdapter("unused")
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        qwen=FakeModelAdapter(),
        fake=FakeModelAdapter(),
    )
    conversation_uow = InMemoryConversationUnitOfWorkFactory()
    blob_store = FilesystemBlobStore(tmp_path)
    output_store = BlobModelOutputStore(
        BlobCatalogService(blob_store, conversation_uow)  # type: ignore[arg-type]
    )
    gateway = _CrashesBeforeGatewayClaimsTheRun(
        router, conversation_uow, output_store  # type: ignore[arg-type]
    )
    service = ModelConversationService(
        conversation_uow,  # type: ignore[arg-type]
        gateway,
        blob_store,
    )

    conversation = await service.create(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.SUBJECT_RESEARCH,
        title="Recherche",
        edition_id=None,
        subject_id=None,
        expected_profile=None,
        requested_model=None,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await service.add_turn(
            conversation.id,
            message="Question",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="crash-key",
            correlation_id="corr-crash",
        )

    assert len(conversation_uow.model_runs) == 1
    run = next(iter(conversation_uow.model_runs.values()))
    assert run.status.value == "failed"
    assert run.submission_state.value == "not_submitted"

    updated = await service.get(conversation.id)
    assert updated.status is ConversationStatus.UNAVAILABLE
