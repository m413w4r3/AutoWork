from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_conversations import (
    ConversationNeedsReviewError,
    ConversationPolicyError,
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
from cti_app.application.production_reconciliation import _verified_external_turn_id
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRunStatus, ModelUsage
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


# --------------------------------------------------------------------------- #
# Recovery identity: only a verified external assistant turn id may become
# `ModelConversationTurn.external_turn_id`. It is never derived from the bridge
# run id, `ModelRun.response_id`, the ModelRun id or a browser target id.
# --------------------------------------------------------------------------- #
_AMBIGUOUS_BRIDGE_RUN_ID = "bridge-response-DIFFERENT"


class _AmbiguousThenAnsweringAdapter(_ScriptedBridgeAdapter):
    """Fails the first submission ambiguously, then answers normally."""

    def __init__(self, answer: str, *, turn_id: str = "dom-turn-continue") -> None:
        super().__init__(answer, turn_id=turn_id)
        self.fail_next = True

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        if self.fail_next:
            self.fail_next = False
            self.calls.append(request)
            raise BridgeTransportError(
                "bridge_ui_timeout",
                "La confirmation du bridge est ambiguë.",
                retryable=True,
                phase="submission_confirmation",
                bridge_run_id=_AMBIGUOUS_BRIDGE_RUN_ID,
                submission_state="submission_attempted",
            )
        return await super().invoke(request, role=role, output_schema=output_schema)


async def _ambiguous_first_turn(
    service: ModelConversationService,
    conversation: ModelConversation,
) -> None:
    with pytest.raises(ModelGatewayError):
        await service.add_turn(
            conversation.id,
            message="Question récupérable",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="identity-recovery-key",
            correlation_id="corr-identity",
        )


async def test_visible_recovery_persists_the_real_external_turn_id(tmp_path: Path) -> None:
    """J + L: the DOM turn id survives adoption; response_id never stands in."""
    adapter = _AmbiguousThenAnsweringAdapter("Suite", turn_id="dom-turn-2")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    await _ambiguous_first_turn(service, conversation)

    turn = next(iter(state.turns.values()))
    model_run = state.model_runs[turn.model_run_id]
    # The bridge response id is deliberately different from the DOM turn id.
    assert model_run.response_id == _AMBIGUOUS_BRIDGE_RUN_ID

    await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        model_run.id,
        b"Reponse recuperee",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="dom-turn-1",
    )

    adopted = state.turns[turn.id]
    assert adopted.status is ConversationTurnStatus.SUCCEEDED
    assert adopted.external_turn_id == "dom-turn-1"
    assert adopted.external_turn_id != model_run.response_id
    assert adopted.external_turn_id != str(model_run.id)
    assert adopted.external_turn_id != str(conversation.id)


async def test_continue_after_visible_recovery_routes_on_the_real_turn_id(
    tmp_path: Path,
) -> None:
    """K: the next CONTINUE carries the captured DOM turn id, not response_id."""
    adapter = _AmbiguousThenAnsweringAdapter("Suite", turn_id="dom-turn-2")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    await _ambiguous_first_turn(service, conversation)

    turn = next(iter(state.turns.values()))
    await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        state.model_runs[turn.model_run_id].id,
        b"Reponse recuperee",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="dom-turn-1",
    )

    second = await service.add_turn(
        conversation.id,
        message="Et ensuite ?",
        mode=ConversationMode.CONTINUE,
        external_llm_allowed=True,
        idempotency_key="continue-after-recovery",
        correlation_id="corr-continue-recovery",
    )

    assert second.status is ConversationTurnStatus.SUCCEEDED
    context = adapter.calls[-1].conversation
    assert context is not None
    assert context.expected_turn_id == "dom-turn-1"
    assert context.expected_turn_id != _AMBIGUOUS_BRIDGE_RUN_ID


async def test_manual_import_invents_no_external_turn_id_and_blocks_continue(
    tmp_path: Path,
) -> None:
    """M: a Markdown import has no verified external identity, and says so."""
    adapter = _AmbiguousThenAnsweringAdapter("Suite")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    await _ambiguous_first_turn(service, conversation)

    turn = next(iter(state.turns.values()))
    model_run = state.model_runs[turn.model_run_id]
    await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        model_run.id,
        b"Contenu colle a la main",
        provenance="manual_import",
        actor_id="analyst",
    )

    adopted = state.turns[turn.id]
    assert adopted.status is ConversationTurnStatus.SUCCEEDED
    assert adopted.external_turn_id is None

    with pytest.raises(ConversationPolicyError, match="tour externe vérifié"):
        await service.add_turn(
            conversation.id,
            message="Et ensuite ?",
            mode=ConversationMode.CONTINUE,
            external_llm_allowed=True,
            idempotency_key="continue-after-manual-import",
            correlation_id="corr-continue-manual",
        )
    # The refusal happened before any submission: the adapter was called once.
    assert len(adapter.calls) == 1


async def test_blank_external_turn_id_is_not_replaced_by_a_look_alike(
    tmp_path: Path,
) -> None:
    """O: an unusable capture identity degrades to `None`, never to a fallback."""
    adapter = _AmbiguousThenAnsweringAdapter("Suite")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    await _ambiguous_first_turn(service, conversation)

    turn = next(iter(state.turns.values()))
    await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        state.model_runs[turn.model_run_id].id,
        b"Reponse recuperee",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id=_verified_external_turn_id("   "),
    )

    assert state.turns[turn.id].external_turn_id is None


async def test_double_adoption_creates_no_second_run_or_turn(tmp_path: Path) -> None:
    """P: replaying the exact same adoption is a join, never a new identity."""
    adapter = _AmbiguousThenAnsweringAdapter("Suite")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    await _ambiguous_first_turn(service, conversation)

    turn = next(iter(state.turns.values()))
    model_run_id = state.model_runs[turn.model_run_id].id
    first = await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        model_run_id,
        b"Reponse recuperee",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="dom-turn-1",
    )
    second = await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        model_run_id,
        b"Reponse recuperee",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="dom-turn-1",
    )

    assert first.id == second.id == model_run_id
    assert len(state.model_runs) == 1
    assert len(state.turns) == 1
    assert len(state.conversations) == 1
    assert state.turns[turn.id].external_turn_id == "dom-turn-1"


class _AlwaysAmbiguousStatelessAdapter:
    """Stateless bridge run: no ConversationContext is ever attached."""

    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web"
    is_external = True

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        assert request.conversation is None
        raise BridgeTransportError(
            "bridge_ui_timeout",
            "La confirmation du bridge est ambiguë.",
            retryable=True,
            phase="submission_confirmation",
            bridge_run_id="bridge-response-STATELESS",
            submission_state="submission_attempted",
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        raise ModelGatewayError("stateless adapter does not resume")


async def test_stateless_recovery_creates_no_conversational_identity(
    tmp_path: Path,
) -> None:
    """N: adopting a stateless run never fabricates a conversation or a turn."""
    adapter = _AlwaysAmbiguousStatelessAdapter()
    service, state = _build_service(adapter, tmp_path)  # type: ignore[arg-type]
    gateway = service._gateway  # type: ignore[attr-defined]
    run_id = uuid4()

    with pytest.raises(ModelGatewayError):
        await gateway.research(
            ModelRequest(
                text="Recherche sans conversation",
                prompt_template_id="analyst-conversation",
                prompt_template_version="1",
                evidence_pack_hash="a" * 64,
                external_llm_allowed=True,
                routing_hint=ModelRoutingHint.WEB_RESEARCH,
                run_id=run_id,
            )
        )

    await gateway.adopt_recovery_output(
        run_id,
        b"Reponse recuperee sans conversation",
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="dom-turn-stateless",
    )

    assert state.turns == {}
    assert state.conversations == {}
    assert state.model_runs[run_id].status.value == "succeeded"


class _StalledVisibleAnswerAdapter(_ScriptedBridgeAdapter):
    """Reproduit l'incident : ChatGPT a répondu à l'écran, le bridge rend
    `needs_review` avec `active_signal_stalled` et un candidat récupérable."""

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        return AdapterResult(
            status=AdapterResultStatus.NEEDS_REVIEW,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            response_id="resp_65a707c50a5549a582b2fc3f",
            usage=ModelUsage(input_tokens=3, output_tokens=0, total_tokens=3),
            metadata={
                "reason": "active_signal_stalled",
                "completion_signal": "streaming",
                "completion_confidence": "high",
                "output_chars": len(self._answer),
                "candidate_output_present": True,
                "recovery_preview_available": True,
                "submission_state": "post_submission",
            },
        )


async def test_native_needs_review_is_not_collapsed_into_a_generic_error(
    tmp_path: Path,
) -> None:
    """L'incident : un needs_review natif devenait « pas de réponse finale ».

    Le motif du bridge, le ModelRun exact, l'identité de la réponse bridge et la
    disponibilité d'une récupération doivent survivre au passage par la
    conversation, sinon Production enregistre une erreur terminale pour un sujet
    dont la réponse est à l'écran.
    """
    adapter = _StalledVisibleAnswerAdapter("Réponse visible mais jamais conclue")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)

    with pytest.raises(ConversationNeedsReviewError) as raised:
        await service.add_turn(
            conversation.id,
            message="Question de recherche",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="stalled-visible-key",
            correlation_id="corr-stalled",
        )

    error = raised.value
    turn = next(iter(state.turns.values()))
    model_run = state.model_runs[turn.model_run_id]

    assert error.code == "model_submission_reconciliation_required"
    assert error.reason == "active_signal_stalled"
    assert error.model_run_id == model_run.id
    assert error.bridge_response_id == "resp_65a707c50a5549a582b2fc3f"
    assert error.recovery_available is True
    assert error.details["output_chars"] == len("Réponse visible mais jamais conclue")
    assert error.details["submission_state"] == "submitted_or_unknown"
    assert "n'a pas produit de réponse finale" not in str(error)
    # Le ModelRun garde le vrai motif du bridge : c'est le diagnostic.
    assert model_run.status is ModelRunStatus.NEEDS_REVIEW
    assert model_run.error_code == "active_signal_stalled"
    assert model_run.response_id == "resp_65a707c50a5549a582b2fc3f"
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert len(adapter.calls) == 1


class _UnpersistedCandidateAdapter(_StalledVisibleAnswerAdapter):
    """Le texte a été vu, mais l'aperçu n'a jamais atteint le registre."""

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        result = await super().invoke(request, role=role, output_schema=output_schema)
        result.metadata["recovery_preview_available"] = False
        return result


async def test_explicit_unavailable_recovery_is_never_upgraded_by_output_chars(
    tmp_path: Path,
) -> None:
    """`recovery_preview_available=False` est une affirmation, pas une absence.

    Le bridge ne la pose qu'après l'échec de la persistance durable : la
    réécrire en `True` parce que `output_chars > 0` enverrait un humain adopter
    un aperçu qui n'existe pas. Et un échec de persistance ne redevient jamais
    un replay implicite du modèle.
    """
    adapter = _UnpersistedCandidateAdapter("Réponse visible mais jamais persistée")
    service, _state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)

    with pytest.raises(ConversationNeedsReviewError) as raised:
        await service.add_turn(
            conversation.id,
            message="Question de recherche",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="unpersisted-candidate-key",
            correlation_id="corr-unpersisted",
        )

    error = raised.value
    assert error.reason == "active_signal_stalled"
    assert error.details["output_chars"] > 0
    assert error.details["candidate_output_present"] is True
    assert error.recovery_available is False
    assert error.details["recovery_available"] is False
    assert len(adapter.calls) == 1


async def test_adopting_the_stalled_candidate_never_sends_a_second_prompt(
    tmp_path: Path,
) -> None:
    adapter = _StalledVisibleAnswerAdapter("Réponse visible mais jamais conclue")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)

    with pytest.raises(ConversationNeedsReviewError):
        await service.add_turn(
            conversation.id,
            message="Question de recherche",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="stalled-adoption-key",
            correlation_id="corr-stalled-adopt",
        )

    turn = next(iter(state.turns.values()))
    await service._gateway.adopt_recovery_output(  # type: ignore[attr-defined]
        turn.model_run_id,
        "Réponse visible mais jamais conclue".encode(),
        provenance="visible_recovery",
        actor_id="reviewer",
        external_turn_id="assistant-42",
    )

    replay = await service.add_turn(
        conversation.id,
        message="Question de recherche",
        mode=ConversationMode.FRESH,
        external_llm_allowed=True,
        idempotency_key="stalled-adoption-key",
        correlation_id="corr-stalled-adopt-2",
    )

    assert replay.status is ConversationTurnStatus.SUCCEEDED
    assert replay.external_turn_id == "assistant-42"
    assert len(adapter.calls) == 1
    assert (await service.turns(conversation.id))[0].output_text == (
        "Réponse visible mais jamais conclue"
    )


async def test_placeholder_turn_id_is_rejected_as_a_durable_identity() -> None:
    placeholder = "request-placeholder-request-WEB:822ff1a2-6c1f-49a1-b10e-3143f7ca53b3-0"

    assert _verified_external_turn_id(placeholder) is None
    assert _verified_external_turn_id("assistant-42") == "assistant-42"
