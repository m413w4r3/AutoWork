from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_conversations import (
    ModelConversationError,
    ModelConversationService,
)
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationContext,
    ConversationResult,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    SafeModelRequest,
    sanitize_model_request,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelUsage
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore, BridgeTransportError, FakeModelAdapter
from tests.conversation_support import InMemoryConversationUnitOfWorkFactory


def _request(context: ConversationContext) -> ModelRequest:
    return ModelRequest(
        text="Quel pivot faut-il vérifier ?",
        prompt_template_id="analyst-conversation",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=context,
    )


def test_authorized_hash_binds_conversation_parent_head_message_and_prompt() -> None:
    conversation_id = uuid4()
    parent_id = uuid4()
    first = ConversationContext(
        mode="continue",
        id=conversation_id,
        expected_turn_id="turn-a",
        parent_turn_id=parent_id,
        previous_head_hash="b" * 64,
    )
    other_head = ConversationContext(
        mode="continue",
        id=conversation_id,
        expected_turn_id="turn-a",
        parent_turn_id=parent_id,
        previous_head_hash="c" * 64,
    )

    assert (
        sanitize_model_request(_request(first)).authorized_input_hash
        != sanitize_model_request(_request(other_head)).authorized_input_hash
    )
    assert sanitize_model_request(_request(first)).conversation == first


def test_authorized_hash_binds_the_explicit_web_search_choice() -> None:
    context = ConversationContext(
        mode="continue",
        id=uuid4(),
        expected_turn_id="turn-a",
    )
    request = _request(context)

    assert sanitize_model_request(request).web_search is False
    assert (
        sanitize_model_request(request).authorized_input_hash
        != sanitize_model_request(replace(request, web_search=True)).authorized_input_hash
    )


def test_research_and_drafting_scopes_can_continue() -> None:
    analyst = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.ANALYST_ASSISTANCE,
        title="Analyse",
        external_locator="https://chatgpt.com/opaque/a",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    analyst.start_turn(mode=ConversationMode.CONTINUE)
    assert analyst.status is ConversationStatus.BUSY

    drafting = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.DRAFTING,
        title="Synthèse",
        external_locator="https://chatgpt.com/opaque/s",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    drafting.start_turn(mode=ConversationMode.CONTINUE)
    assert drafting.status is ConversationStatus.BUSY

    research = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.SUBJECT_RESEARCH,
        title="Recherche",
        external_locator="https://chatgpt.com/opaque/r",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    research.start_turn(mode=ConversationMode.CONTINUE)
    assert research.status is ConversationStatus.BUSY

    discovery = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.DISCOVERY,
        title="Découverte",
        external_locator="https://chatgpt.com/opaque/d",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    with pytest.raises(ValueError, match="requires fresh"):
        discovery.start_turn(mode=ConversationMode.CONTINUE)


def test_model_request_has_no_lifecycle_field() -> None:
    """Temporary Chat is enforced by the transport for every fresh bridge
    session; a dormant application-side deletion spec is no longer the
    authority, so ModelRequest carries no conversation_lifecycle field."""
    conversation_id = uuid4()

    request = ModelRequest(
        text="Extract data",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
    )
    assert request.conversation is None
    assert not hasattr(request, "conversation_lifecycle")

    fresh_context = ConversationContext(mode="fresh", id=conversation_id)
    fresh_request = ModelRequest(
        text="Analyze data",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=fresh_context,
    )
    assert fresh_request.conversation == fresh_context

    continue_context = ConversationContext(
        mode="continue",
        id=conversation_id,
        expected_turn_id="turn-a",
    )
    continue_request = ModelRequest(
        text="Continue analysis",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=continue_context,
    )
    assert continue_request.conversation == continue_context


def test_conversation_context_continue_requires_expected_turn_id() -> None:
    with pytest.raises(ValueError, match="expected_turn_id"):
        ConversationContext(mode="continue", id=uuid4())


def test_conversation_context_fresh_forbids_expected_turn_id() -> None:
    with pytest.raises(ValueError, match="expected_turn_id"):
        ConversationContext(mode="fresh", id=uuid4(), expected_turn_id="turn-a")


def test_bridge_payload_carries_expected_turn_id_not_locator() -> None:
    context = ConversationContext(mode="continue", id=uuid4(), expected_turn_id="turn-a")
    payload = context.bridge_payload()
    assert payload == {
        "mode": "continue",
        "id": str(context.id),
        "expected_turn_id": "turn-a",
    }
    assert "external_locator" not in payload


class _ScriptedBridgeAdapter:
    """Answers with a fixed text and a diagnostic-only external_locator,
    exactly once per call — never used as routing identity."""

    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web"
    is_external = True

    def __init__(self, answer: str, *, turn_id: str = "bridge-turn-1") -> None:
        self._answer = answer
        self._turn_id = turn_id
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
                external_locator="https://chatgpt.com/?temporary-chat=true",
                turn_id=self._turn_id,
                verified=True,
            ),
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        raise ModelGatewayError("Scripted bridge adapter does not support background resume")


class _RecordingSessionCloser:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def archive_conversation(self, conversation_id: Any) -> None:
        self.calls.append(conversation_id)


class _FailingSessionCloser:
    async def archive_conversation(self, conversation_id: Any) -> None:
        raise RuntimeError("simulated close failure")


class _RecordingFailingSessionCloser:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def archive_conversation(self, conversation_id: Any) -> None:
        self.calls.append(conversation_id)
        raise RuntimeError("simulated close failure")


def _build_service(
    adapter: _ScriptedBridgeAdapter,
    tmp_path: Path,
    *,
    conversation_session_closer: Any = None,
) -> tuple[ModelConversationService, InMemoryConversationUnitOfWorkFactory]:
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
    gateway = ModelGateway(router, conversation_uow, output_store)  # type: ignore[arg-type]
    service = ModelConversationService(
        conversation_uow,  # type: ignore[arg-type]
        gateway,
        blob_store,
        conversation_session_closer=conversation_session_closer,
    )
    return service, conversation_uow


async def _fresh_conversation(service: ModelConversationService) -> ModelConversation:
    return await service.create(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.SUBJECT_RESEARCH,
        title="Recherche",
        edition_id=None,
        subject_id=None,
        expected_profile=None,
        requested_model=None,
    )


async def test_continue_uses_parent_external_turn_id_not_external_locator(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedBridgeAdapter("Première réponse", turn_id="external-turn-1")
    service, _ = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)

    first = await service.add_turn(
        conversation.id,
        message="Première question",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="continue-key-fresh",
        correlation_id="corr-fresh",
    )
    assert first.status is ConversationTurnStatus.SUCCEEDED
    assert first.external_turn_id == "external-turn-1"

    adapter._answer = "Deuxième réponse"
    adapter._turn_id = "external-turn-2"
    second = await service.add_turn(
        conversation.id,
        message="Deuxième question",
        mode=ConversationMode.CONTINUE,
        external_llm_allowed=True,
        idempotency_key="continue-key-continue",
        correlation_id="corr-continue",
    )
    assert second.status is ConversationTurnStatus.SUCCEEDED
    # The routing precondition was the parent's external_turn_id, never a
    # locator — the request-side ConversationContext carries no such field.
    assert adapter.calls[1].conversation is not None
    assert adapter.calls[1].conversation.expected_turn_id == "external-turn-1"
    assert not hasattr(adapter.calls[1].conversation, "external_locator")


async def test_keep_leaves_conversation_ready_and_never_closes_session(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    closer = _RecordingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    turn = await service.add_turn(
        conversation.id,
        message="Question",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="keep-key",
        correlation_id="corr-keep",
        lifecycle_policy=ConversationPolicy.KEEP,
    )
    assert turn.status is ConversationTurnStatus.SUCCEEDED

    updated = await service.get(conversation.id)
    assert updated.status is ConversationStatus.READY
    assert closer.calls == []


async def test_delete_on_success_archives_and_closes_session_exactly_once(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    closer = _RecordingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    turn = await service.add_turn(
        conversation.id,
        message="Question",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="delete-on-success-key",
        correlation_id="corr-dos",
        lifecycle_policy=ConversationPolicy.DELETE_ON_SUCCESS,
    )
    assert turn.status is ConversationTurnStatus.SUCCEEDED

    updated = await service.get(conversation.id)
    assert updated.status is ConversationStatus.ARCHIVED
    assert closer.calls == [conversation.id]


async def test_delete_on_success_does_not_close_on_failure_or_needs_review(
    tmp_path: Path,
) -> None:
    class _FailingAdapter(_ScriptedBridgeAdapter):
        async def invoke(
            self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
        ) -> AdapterResult:
            del role, output_schema
            self.calls.append(request)
            raise ModelGatewayError("simulated bridge failure")

    adapter = _FailingAdapter("unused")
    closer = _RecordingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    with pytest.raises(ModelGatewayError):
        await service.add_turn(
            conversation.id,
            message="Question",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="delete-on-success-fail-key",
            correlation_id="corr-dos-fail",
            lifecycle_policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

    assert closer.calls == []


async def test_duplicate_delete_on_success_returns_succeeded_when_retry_close_fails(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    closer = _RecordingFailingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    first = await service.add_turn(
        conversation.id,
        message="Question",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="delete-on-success-replay-key",
        correlation_id="corr-dos-replay",
        lifecycle_policy=ConversationPolicy.DELETE_ON_SUCCESS,
    )
    replay = await service.add_turn(
        conversation.id,
        message="Question",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="delete-on-success-replay-key",
        correlation_id="corr-dos-replay-2",
        lifecycle_policy=ConversationPolicy.DELETE_ON_SUCCESS,
    )

    assert first.status is ConversationTurnStatus.SUCCEEDED
    assert replay.id == first.id
    assert replay.status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1
    assert closer.calls == [conversation.id, conversation.id]


async def test_adopted_model_run_closes_exact_failed_turn_without_resubmission(
    tmp_path: Path,
) -> None:
    class _AmbiguousAdapter(_ScriptedBridgeAdapter):
        async def invoke(
            self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
        ) -> AdapterResult:
            self.calls.append(request)
            del role, output_schema
            raise BridgeTransportError(
                "bridge_ui_timeout",
                "La confirmation du bridge est ambiguë.",
                retryable=True,
                phase="submission_confirmation",
                bridge_run_id="bridge-recovery-1",
                submission_state="submission_attempted",
            )

    adapter = _AmbiguousAdapter("unused")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)

    with pytest.raises(ModelGatewayError):
        await service.add_turn(
            conversation.id,
            message="Question récupérable",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="recovery-turn-key",
            correlation_id="corr-recovery",
        )

    turn = next(iter(state.turns.values()))
    model_run = state.model_runs[turn.model_run_id]
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    recovered = await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        model_run.id,
        b"Recovered answer",
        provenance="visible_recovery",
        actor_id="reviewer",
    )
    assert recovered.status.value == "succeeded"
    assert state.turns[turn.id].status is ConversationTurnStatus.SUCCEEDED

    replay = await service.add_turn(
        conversation.id,
        message="Question récupérable",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="recovery-turn-key",
        correlation_id="corr-recovery-replay",
    )
    assert replay.status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1
    assert (await service.turns(conversation.id))[0].output_text == "Recovered answer"


async def test_explicit_archive_closes_bridge_session(tmp_path: Path) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    closer = _RecordingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    archived = await service.archive(conversation.id)
    assert archived.status is ConversationStatus.ARCHIVED
    assert closer.calls == [conversation.id]


async def test_explicit_archive_stays_archived_even_if_close_fails(tmp_path: Path) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    service, uow_factory = _build_service(
        adapter, tmp_path, conversation_session_closer=_FailingSessionCloser()
    )
    conversation = await _fresh_conversation(service)

    with pytest.raises(ModelConversationError):
        await service.archive(conversation.id)

    async with uow_factory() as uow:
        persisted = await uow.model_conversations.get(conversation.id)
    assert persisted is not None
    assert persisted.status is ConversationStatus.ARCHIVED


async def test_repeat_archive_is_safe_and_retries_closing(tmp_path: Path) -> None:
    adapter = _ScriptedBridgeAdapter("Réponse")
    closer = _RecordingSessionCloser()
    service, _ = _build_service(adapter, tmp_path, conversation_session_closer=closer)
    conversation = await _fresh_conversation(service)

    first = await service.archive(conversation.id)
    second = await service.archive(conversation.id)
    assert first.status is ConversationStatus.ARCHIVED
    assert second.status is ConversationStatus.ARCHIVED
    # Both calls retry closing the exact external tab.
    assert closer.calls == [conversation.id, conversation.id]
