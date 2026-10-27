import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.model_conversations import router as model_conversation_router
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    ModelConversationTurnTransport,
    TurnSubmissionStatus,
    make_investigation_conversation_id,
    make_investigation_turn_idempotency_key,
)
from cti_app.application.identity import Identity
from cti_app.application.model_conversations import (
    ConversationNeedsReviewError,
    ConversationPolicyError,
    ConversationSessionCloseError,
    ModelConversationReconciliationError,
    ModelConversationService,
    _needs_review_error,
)
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationContext,
    ConversationResult,
    ModelCapabilities,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    SafeModelRequest,
    sanitize_model_request,
)
from cti_app.application.production_reconciliation import _verified_external_turn_id
from cti_app.domain.investigation import AgendaItem, InvestigationAgenda
from cti_app.domain.investigation_state import (
    InvestigationWorkingState,
    StopReason,
    investigation_bridge_attempt,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelTransport,
    ModelUsage,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore, BridgeTransportError, FakeModelAdapter
from tests.conversation_support import (
    InMemoryConversationUnitOfWorkFactory,
    InMemoryModelConversationTurnRepository,
)


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
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    capabilities = ModelCapabilities(web_search=True, background=True, conversation=True)
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


class _FailOnceBeforeSubmissionAdapter(_ScriptedBridgeAdapter):
    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        if not self.calls:
            self.calls.append(request)
            raise BridgeTransportError(
                "bridge_server_error",
                "Composer payload was not ready before submission.",
                retryable=True,
                retry_after=23,
                submission_state="pre_submission",
            )
        del role, output_schema
        self.calls.append(request)
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            output_text=self._answer,
        )


class _CompletedRateLimitAdapter(_ScriptedBridgeAdapter):
    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        raise BridgeTransportError(
            "bridge_rate_limited",
            "Le bridge limite temporairement les requêtes.",
            retryable=True,
            retry_after=4,
            phase="generation",
            status_code=429,
            submission_state="post_submission",
            completed_response=True,
        )


class _FailNTimesBeforeSubmissionAdapter(_ScriptedBridgeAdapter):
    def __init__(self, answer: str, *, failures: int) -> None:
        super().__init__(answer)
        self._failures_remaining = failures

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        self.calls.append(request)
        if self._failures_remaining:
            self._failures_remaining -= 1
            raise BridgeTransportError(
                "bridge_server_error",
                "Composer payload was not ready before submission.",
                retryable=True,
                submission_state="pre_submission",
            )
        del role, output_schema
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            output_text=self._answer,
        )


@pytest.mark.asyncio
async def test_model_transport_recovers_persisted_confirmed_pre_submission_failure(
    tmp_path: Path,
) -> None:
    adapter = _FailOnceBeforeSubmissionAdapter(
        "@@DECISION@@\nGOAL: Finish the synthetic investigation.\n"
        "SUMMARY: The retry was submitted once.\nNEXT: finish\n@@END@@"
    )
    subject_id = uuid4()
    service, conversation_uow = _build_service(
        adapter,
        tmp_path,
        known_subject_ids={subject_id},
    )
    transport = ModelConversationTurnTransport(service)
    investigation_id = uuid4()
    context_sha256 = hashlib.sha256(b"persisted pre-submit prompt").hexdigest()
    args = {
        "investigation_id": investigation_id,
        "subject_id": subject_id,
        "turn_index": 6,
        "context_sha256": context_sha256,
        "prompt": "A stable synthetic investigation prompt.",
        "external_llm_allowed": True,
    }

    first = await transport.submit(**args)
    replay = await transport.submit(**args)
    assert first.status is TurnSubmissionStatus.UNAVAILABLE
    assert first.submission_state == "pre_submission"
    assert first.retryable is True
    assert first.retry_after_seconds == 23
    assert replay.status is TurnSubmissionStatus.UNAVAILABLE
    assert replay.submission_state == "pre_submission"
    assert replay.retryable is True
    assert replay.retry_after_seconds == 23
    assert len(adapter.calls) == 1

    resumed = await transport.submit_with_attempt(**args, attempt=1)

    assert resumed.status is TurnSubmissionStatus.SUCCEEDED
    assert resumed.idempotency_key.endswith(":attempt=1")
    assert resumed.idempotency_key != first.idempotency_key
    assert len(adapter.calls) == 2
    stored_turns = tuple(conversation_uow.turns.values())
    assert {item.status for item in stored_turns} == {
        ConversationTurnStatus.FAILED,
        ConversationTurnStatus.SUCCEEDED,
    }


@pytest.mark.asyncio
async def test_completed_rate_limit_replay_preserves_rejection_without_resubmission(
    tmp_path: Path,
) -> None:
    adapter = _CompletedRateLimitAdapter("unused")
    subject_id = uuid4()
    service, state = _build_service(
        adapter,
        tmp_path,
        known_subject_ids={subject_id},
    )
    transport = ModelConversationTurnTransport(service)
    args = {
        "investigation_id": uuid4(),
        "subject_id": subject_id,
        "turn_index": 1,
        "context_sha256": hashlib.sha256(b"completed 429 prompt").hexdigest(),
        "prompt": "A stable synthetic investigation prompt.",
        "external_llm_allowed": True,
    }

    first = await transport.submit(**args)
    replay = await transport.submit(**args)

    assert first.status is TurnSubmissionStatus.UNAVAILABLE
    assert first.error_code == "bridge_rate_limited"
    assert first.completed_response is True
    assert first.submission_state == "post_submission"
    assert first.retry_after_seconds == 4
    assert replay.status is TurnSubmissionStatus.UNAVAILABLE
    assert replay.error_code == "bridge_rate_limited"
    assert replay.completed_response is True
    assert replay.submission_state == "post_submission"
    assert replay.retry_after_seconds == 4
    assert len(adapter.calls) == 1
    assert len(state.turns) == 1


@pytest.mark.asyncio
async def test_model_transport_resume_after_three_confirmed_failures_sends_attempt_four(
    tmp_path: Path,
) -> None:
    adapter = _FailNTimesBeforeSubmissionAdapter(
        "@@DECISION@@\nGOAL: Finish the synthetic investigation.\n"
        "SUMMARY: The fourth distinct attempt succeeded.\nNEXT: finish\n@@END@@",
        failures=3,
    )
    subject_id = uuid4()
    service, conversation_uow = _build_service(
        adapter,
        tmp_path,
        known_subject_ids={subject_id},
    )
    transport = ModelConversationTurnTransport(service)
    investigation_id = uuid4()
    context_sha256 = hashlib.sha256(b"persisted retry-exhaustion prompt").hexdigest()
    args = {
        "investigation_id": investigation_id,
        "subject_id": subject_id,
        "turn_index": 6,
        "context_sha256": context_sha256,
        "prompt": "A stable synthetic investigation prompt.",
        "external_llm_allowed": True,
    }

    exhausted = [
        await transport.submit(**args),
        await transport.submit_with_attempt(**args, attempt=1),
        await transport.submit_with_attempt(**args, attempt=2),
    ]

    assert all(item.status is TurnSubmissionStatus.UNAVAILABLE for item in exhausted)
    assert all(item.submission_state == "pre_submission" for item in exhausted)
    assert len({item.idempotency_key for item in exhausted}) == 3
    assert (
        await service.get(make_investigation_conversation_id(investigation_id))
    ).status is ConversationStatus.UNAVAILABLE

    resumed = await transport.submit_with_attempt(**args, attempt=3)

    assert resumed.status is TurnSubmissionStatus.SUCCEEDED
    assert resumed.idempotency_key.endswith(":attempt=3")
    assert len(adapter.calls) == 4
    assert resumed.conversation_id is not None
    turns = await service.turns(resumed.conversation_id)
    assert len({item.turn.idempotency_key for item in turns}) == 4
    assert all(
        item.status in {ConversationTurnStatus.FAILED, ConversationTurnStatus.SUCCEEDED}
        for item in conversation_uow.turns.values()
    )


class _RecordingSessionCloser:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def archive_conversation(self, conversation_id: Any) -> None:
        self.calls.append(conversation_id)


class _FailingSessionCloser:
    async def archive_conversation(self, conversation_id: Any) -> None:
        raise BridgeTransportError(
            "bridge_extension_disconnected",
            "Extension Chrome non connectée.",
            retryable=True,
            phase="conversation_archive",
            conversation_id=str(conversation_id),
            diagnostics={"tab_id": 7, "window_id": 8},
        )


class _RecordingFailingSessionCloser:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def archive_conversation(self, conversation_id: Any) -> None:
        self.calls.append(conversation_id)
        raise BridgeTransportError(
            "bridge_extension_disconnected",
            "Extension Chrome non connectée.",
            retryable=True,
            phase="conversation_archive",
            conversation_id=str(conversation_id),
            diagnostics={"tab_id": 7, "window_id": 8},
        )


def _build_service(
    adapter: _ScriptedBridgeAdapter,
    tmp_path: Path,
    *,
    conversation_session_closer: Any = None,
    visible_recovery_transport: Any = None,
    known_subject_ids: set[Any] | None = None,
) -> tuple[ModelConversationService, InMemoryConversationUnitOfWorkFactory]:
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        qwen=FakeModelAdapter(),
        fake=FakeModelAdapter(),
    )
    conversation_uow = InMemoryConversationUnitOfWorkFactory(known_subject_ids=known_subject_ids)
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
        visible_recovery_transport=visible_recovery_transport,
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


@pytest.mark.asyncio
async def test_cancelled_bridge_submission_is_persisted_as_needs_review(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class _BlockingAdapter(_ScriptedBridgeAdapter):
        async def invoke(
            self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
        ) -> AdapterResult:
            del role, output_schema
            self.calls.append(request)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    adapter = _BlockingAdapter("unused")
    service, state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    task = asyncio.create_task(
        service.add_turn(
            conversation.id,
            message="A synthetic investigation question",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="synthetic-cancelled-turn",
            correlation_id="synthetic-cancelled-correlation",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turn = next(iter(state.turns.values()))
    run = state.model_runs[turn.model_run_id]
    persisted_conversation = await service.get(conversation.id)
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert turn.error_code == "operator_interrupted"
    assert run.status is ModelRunStatus.NEEDS_REVIEW
    assert persisted_conversation.status is ConversationStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_orphaned_running_turn_is_reviewed_before_distinct_retry(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class _FirstCallInterruptedAdapter(_ScriptedBridgeAdapter):
        async def invoke(
            self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
        ) -> AdapterResult:
            del role, output_schema
            self.calls.append(request)
            if len(self.calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            context = request.conversation
            return AdapterResult(
                status=AdapterResultStatus.COMPLETED,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                output_text="synthetic retry response",
                conversation=(
                    ConversationResult(
                        id=str(context.id),
                        mode=context.mode,
                        external_locator=None,
                        turn_id="synthetic-response",
                        verified=True,
                    )
                    if context is not None
                    else None
                ),
            )

    investigation_id = uuid4()
    subject_id = uuid4()
    context_sha256 = "e" * 64
    adapter = _FirstCallInterruptedAdapter("unused")
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    conversation_id = make_investigation_conversation_id(investigation_id)
    conversation = await service.get_or_create(
        conversation_id,
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.PIVOT_RESEARCH,
        title="Synthetic investigation",
        edition_id=None,
        subject_id=subject_id,
        expected_profile=None,
        requested_model=None,
    )
    state.investigation_states[investigation_id] = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            (
                AgendaItem("Synthetic question", 0.8, 1),
                AgendaItem("Second synthetic question", 0.7, 2),
            )
        ),
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256=context_sha256,
        stop_reason=StopReason.USER_INTERRUPT,
    )
    key = make_investigation_turn_idempotency_key(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
    )
    task = asyncio.create_task(
        service.add_turn(
            conversation.id,
            message="Canonical synthetic investigation prompt",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            stateless_external=True,
            idempotency_key=key,
            correlation_id=f"investigation:{investigation_id}:turn:0:{key[:16]}",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Model process death skips the cancellation finalizer. Recreate the
    # durable running rows before the recovered job re-enters this key.
    orphaned_turn = next(iter(state.turns.values()))
    orphaned_turn.status = ConversationTurnStatus.RUNNING
    orphaned_turn.error_code = None
    orphaned_turn.error_message = None
    orphaned_turn.error_details = None
    orphaned_turn.finished_at = None
    orphaned_run = state.model_runs[orphaned_turn.model_run_id]
    orphaned_run.status = ModelRunStatus.RUNNING
    orphaned_run.error_code = None
    orphaned_run.error_message = None
    orphaned_run.error_details = None
    orphaned_run.finished_at = None
    state.conversations[conversation.id].status = ConversationStatus.BUSY
    stale_replay = await ModelConversationTurnTransport(service).submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical synthetic investigation prompt",
        external_llm_allowed=True,
    )
    assert stale_replay.status.value == "needs_review"
    sealed_turn = state.turns[orphaned_turn.id]
    assert sealed_turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert sealed_turn.error_code == "orphaned_running_turn"
    assert len(adapter.calls) == 1

    recovered = await service.reconcile_orphaned_investigation_turn(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
        attempt=0,
        owner_job_terminal=True,
    )
    retry_attempt = investigation_bridge_attempt(
        state.investigation_states[investigation_id].recent_actions, 0
    )
    calls_before_retry = len(adapter.calls)
    retried = await ModelConversationTurnTransport(service).submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical synthetic investigation prompt",
        external_llm_allowed=True,
        attempt=retry_attempt,
    )
    turns = await service.turns(conversation.id, context_subject_id=subject_id)

    assert recovered == "orphaned_running_turn"
    assert retry_attempt == 1
    assert turns[0].turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert turns[0].turn.error_code == "orphaned_running_turn"
    assert turns[1].turn.idempotency_key != key
    assert retried.status.value == "succeeded"
    assert len(adapter.calls) - calls_before_retry == 1
    assert len(adapter.calls) == 2
    explicitly_abandoned = await service.abandon_investigation_turn(
        conversation.id,
        turns[0].turn.id,
        reason="Operator reviewed the orphaned synthetic attempt",
        actor_id="operator:synthetic",
        context_subject_id=subject_id,
    )
    assert explicitly_abandoned.error_code == "operator_abandoned"
    assert (
        investigation_bridge_attempt(state.investigation_states[investigation_id].recent_actions, 0)
        == 2
    )


@pytest.mark.asyncio
async def test_model_unavailable_without_running_turn_is_not_orphaned(tmp_path: Path) -> None:
    investigation_id = uuid4()
    subject_id = uuid4()
    adapter = _ScriptedBridgeAdapter("unused")
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    conversation_id = make_investigation_conversation_id(investigation_id)
    conversation = await service.get_or_create(
        conversation_id,
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.PIVOT_RESEARCH,
        title="Synthetic unavailable investigation",
        edition_id=None,
        subject_id=subject_id,
        expected_profile=None,
        requested_model=None,
    )
    pending_sha256 = "f" * 64
    state.investigation_states[investigation_id] = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            (
                AgendaItem("Synthetic question", 0.8, 1),
                AgendaItem("Second synthetic question", 0.7, 2),
            )
        ),
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256=pending_sha256,
        stop_reason=StopReason.MODEL_UNAVAILABLE,
    )

    disposition = await service.reconcile_orphaned_investigation_turn(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=pending_sha256,
        attempt=0,
        owner_job_terminal=True,
    )

    assert disposition == "none"
    assert state.conversations[conversation.id].status is ConversationStatus.PENDING
    assert state.investigation_states[investigation_id].recent_actions == ()


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


class _StatelessInvestigationAdapter(_ScriptedBridgeAdapter):
    def __init__(self, answer: str) -> None:
        super().__init__(answer)

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        assert request.conversation is None
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            output_text=self._answer,
            conversation=None,
        )


async def test_investigation_turns_keep_local_chain_without_external_identity(
    tmp_path: Path,
) -> None:
    investigation_id = uuid4()
    subject_id = uuid4()
    adapter = _StatelessInvestigationAdapter("A valid final answer")
    service, state = _build_service(adapter, tmp_path)
    state.known_subject_ids.add(subject_id)
    transport = ModelConversationTurnTransport(service)

    first = await transport.submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256="a" * 64,
        prompt="Full context for turn zero",
        external_llm_allowed=True,
    )
    second = await transport.submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=1,
        context_sha256="b" * 64,
        prompt="Full context for turn one",
        external_llm_allowed=True,
    )

    local_conversation_id = first.conversation_id
    assert first.status.value == "succeeded"
    assert second.status.value == "succeeded"
    assert local_conversation_id is not None
    turns = await service.turns(local_conversation_id)
    assert [item.turn.sequence for item in turns] == [1, 2]
    assert turns[0].turn.parent_turn_id is None
    assert turns[1].turn.parent_turn_id == turns[0].turn.id
    assert [item.turn.external_turn_id for item in turns] == [None, None]
    assert [item.turn.idempotency_key for item in turns] == [
        first.idempotency_key,
        second.idempotency_key,
    ]
    assert [item.turn.correlation_id for item in turns] == [
        f"investigation:{investigation_id}:turn:0:{first.idempotency_key[:16]}",
        f"investigation:{investigation_id}:turn:1:{second.idempotency_key[:16]}",
    ]
    assert all(call.conversation is None for call in adapter.calls)
    assert state.turns[turns[0].turn.id].external_turn_id is None
    conversation = await service.get(local_conversation_id)
    assert conversation.head_turn_id == turns[1].turn.id
    assert conversation.turn_count == 2


@pytest.mark.asyncio
async def test_resume_reuses_captured_bridge_output_without_provider_replay(tmp_path: Path) -> None:
    investigation_id = uuid4()
    subject_id = uuid4()
    context_sha256 = "f" * 64
    adapter = _StatelessInvestigationAdapter("Synthetic captured response")
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    state.investigation_states[investigation_id] = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            (
                AgendaItem("Synthetic question", 0.8, 1),
                AgendaItem("Second synthetic question", 0.7, 2),
            )
        ),
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256=context_sha256,
        stop_reason=StopReason.USER_INTERRUPT,
    )
    transport = ModelConversationTurnTransport(service)
    first = await transport.submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical synthetic investigation prompt",
        external_llm_allowed=True,
    )

    disposition = await service.reconcile_orphaned_investigation_turn(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
        attempt=0,
        owner_job_terminal=True,
    )
    replay = await transport.submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical synthetic investigation prompt",
        external_llm_allowed=True,
    )

    assert first.status.value == "succeeded"
    assert disposition == "captured_output"
    assert replay.output_text == "Synthetic captured response"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_orphan_with_provider_response_identity_stays_in_review(tmp_path: Path) -> None:
    class _KnownResponseAdapter(_ScriptedBridgeAdapter):
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
                response_id="synthetic-response-identity",
                usage=ModelUsage(input_tokens=1, output_tokens=0, total_tokens=1),
                metadata={"reason": "synthetic_ambiguous_response", "output_chars": 0},
            )

    investigation_id = uuid4()
    subject_id = uuid4()
    context_sha256 = "9" * 64
    adapter = _KnownResponseAdapter("unused")
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    conversation_id = make_investigation_conversation_id(investigation_id)
    await service.get_or_create(
        conversation_id,
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.PIVOT_RESEARCH,
        title="Synthetic investigation",
        edition_id=None,
        subject_id=subject_id,
        expected_profile=None,
        requested_model=None,
    )
    state.investigation_states[investigation_id] = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            (
                AgendaItem("Synthetic question", 0.8, 1),
                AgendaItem("Second synthetic question", 0.7, 2),
            )
        ),
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256=context_sha256,
        stop_reason=StopReason.USER_INTERRUPT,
    )
    transport = ModelConversationTurnTransport(service)
    first = await transport.submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical synthetic investigation prompt",
        external_llm_allowed=True,
    )

    disposition = await service.reconcile_orphaned_investigation_turn(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
        attempt=0,
        owner_job_terminal=True,
    )

    assert first.status.value == "needs_review"
    assert disposition == "needs_review"
    assert state.investigation_states[investigation_id].recent_actions == ()
    assert len(adapter.calls) == 1


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
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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
    assert "bridge_extension_disconnected" in caplog.text


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

    with pytest.raises(ConversationSessionCloseError) as caught:
        await service.archive(conversation.id)

    assert caught.value.cause_code == "bridge_extension_disconnected"
    assert caught.value.retryable is True
    assert caught.value.phase == "conversation_archive"
    assert caught.value.details == {"tab_id": 7, "window_id": 8}
    assert isinstance(caught.value.__cause__, BridgeTransportError)
    assert caught.value.__cause__.code == "bridge_extension_disconnected"

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
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    capabilities = ModelCapabilities(web_search=True, background=True, conversation=True)
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


def test_external_turn_identity_review_message_acknowledges_visible_answer() -> None:
    run = SimpleNamespace(
        error_details={
            "reason": "external_turn_identity_unavailable",
            "output_chars": 1_610,
            "recovery_preview_available": False,
        },
        error_code="external_turn_identity_unavailable",
        error_message="ChatGPT s'est arrêté sans produire de réponse finale",
        id=uuid4(),
        response_id="bridge-run-1",
        submission_state=SimpleNamespace(value="result_obtained"),
    )

    error = _needs_review_error(SimpleNamespace(run=run, metadata={}))

    assert error.reason == "external_turn_identity_unavailable"
    assert "réponse finale visible" in str(error).lower()
    assert "identité de tour externe non vérifiable" in str(error).lower()
    assert "s'est arrêté sans produire" not in str(error)


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
    assert error.details["submission_state"] == "external_state_unknown"
    assert "n'a pas produit de réponse finale" not in str(error)
    # Le ModelRun garde le vrai motif du bridge : c'est le diagnostic.
    assert model_run.status is ModelRunStatus.NEEDS_REVIEW
    assert model_run.error_code == "active_signal_stalled"
    assert model_run.response_id == "resp_65a707c50a5549a582b2fc3f"
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_targeted_turn_content_lookup_does_not_load_conversation_history(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = _ScriptedBridgeAdapter("Persisted answer")
    service, _state = _build_service(adapter, tmp_path)
    conversation = await _fresh_conversation(service)
    target_turn = None
    for index in range(4):
        target_turn = await service.add_turn(
            conversation.id,
            message=f"Historical consultation {index}",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key=f"history-turn-{index}",
            correlation_id=f"history-correlation-{index}",
        )
    assert target_turn is not None

    async def reject_full_history(_self, _conversation_id):
        raise AssertionError("Targeted lookup must not enumerate historical turns")

    monkeypatch.setattr(
        InMemoryModelConversationTurnRepository,
        "list_for_conversation",
        reject_full_history,
    )
    content = await service.turn_content_by_idempotency_key(conversation.id, "history-turn-3")

    assert content is not None
    assert content.turn.id == target_turn.id
    assert content.output_text == "Persisted answer"


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


class _VisibleTurnRecovery:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]:
        self.calls.append(bridge_run_id)
        if self.payload is None:
            raise RuntimeError("visible recovery unavailable")
        return {"bridge_run_id": bridge_run_id, **self.payload}


async def _create_needs_review_turn(
    service: ModelConversationService,
    conversation: ModelConversation,
    *,
    key: str = "visible-turn-key",
) -> ModelConversationTurn:
    with pytest.raises(ConversationNeedsReviewError):
        await service.add_turn(
            conversation.id,
            message="Question récupérable",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key=key,
            correlation_id="corr-visible-turn",
            context_subject_id=conversation.subject_id,
        )
    return cast(ModelConversationTurn, next(iter(service._uow_factory.turns.values())))  # type: ignore[attr-defined]


async def test_visible_turn_preview_and_adoption_preserve_missing_external_identity(
    tmp_path: Path,
) -> None:
    text = "Réponse Bridge déjà visible"
    recovery = _VisibleTurnRecovery({"text": text, "turn_id": "placeholder-request-0"})
    adapter = _StalledVisibleAnswerAdapter(text)
    service, state = _build_service(adapter, tmp_path, visible_recovery_transport=recovery)
    conversation = await _fresh_conversation(service)
    turn = await _create_needs_review_turn(service, conversation)

    preview = await service.preview_visible_recovery(
        conversation.id, turn.id, context_subject_id=None
    )

    assert preview == {
        "turn_id": turn.id,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "characters": len(text),
        "text": text,
    }
    adopted = await service.adopt_visible_recovery(
        conversation.id,
        turn.id,
        expected_sha256=preview["sha256"],
        actor_id="operator:test",
        context_subject_id=None,
    )
    run = state.model_runs[turn.model_run_id]

    assert adopted.status is ConversationTurnStatus.SUCCEEDED
    assert adopted.external_turn_id is None
    assert adopted.output_sha256 == preview["sha256"]
    assert run.status is ModelRunStatus.SUCCEEDED
    assert run.submission_state.value == "result_obtained"
    assert run.error_details["recovery"] == {
        "provenance": "visible_recovery",
        "actor_id": "operator:test",
        "reason": "operator_confirmed_visible_sha256",
        "sha256": preview["sha256"],
        "source_model_run_id": None,
        "adopted_at": run.error_details["recovery"]["adopted_at"],
    }
    assert len(adapter.calls) == 1


async def test_visible_turn_adoption_rejects_wrong_hash_and_is_idempotent(
    tmp_path: Path,
) -> None:
    text = "Réponse exacte"
    recovery = _VisibleTurnRecovery({"text": text})
    adapter = _StalledVisibleAnswerAdapter(text)
    service, state = _build_service(adapter, tmp_path, visible_recovery_transport=recovery)
    conversation = await _fresh_conversation(service)
    turn = await _create_needs_review_turn(service, conversation, key="visible-idempotent-key")

    with pytest.raises(ModelConversationReconciliationError) as mismatch:
        await service.adopt_visible_recovery(
            conversation.id,
            turn.id,
            expected_sha256="0" * 64,
            actor_id="operator:test",
            context_subject_id=None,
        )
    assert mismatch.value.code == "conversation_reconciliation_hash_mismatch"
    assert state.turns[turn.id].status is ConversationTurnStatus.NEEDS_REVIEW

    digest = hashlib.sha256(text.encode()).hexdigest()
    first = await service.adopt_visible_recovery(
        conversation.id,
        turn.id,
        expected_sha256=digest,
        actor_id="operator:test",
        context_subject_id=None,
    )
    second = await service.adopt_visible_recovery(
        conversation.id,
        turn.id,
        expected_sha256=digest,
        actor_id="operator:replay",
        context_subject_id=None,
    )

    assert first.id == second.id == turn.id
    assert state.turns[turn.id].status is ConversationTurnStatus.SUCCEEDED
    assert len(state.model_runs[turn.model_run_id].output_references) == 1
    assert len(recovery.calls) == 2  # mismatch and first adoption; replay is local only


async def test_visible_turn_preview_refuses_unavailable_recovery_without_mutation(
    tmp_path: Path,
) -> None:
    recovery = _VisibleTurnRecovery(None)
    adapter = _StalledVisibleAnswerAdapter("Visible text, no durable preview")
    service, state = _build_service(adapter, tmp_path, visible_recovery_transport=recovery)
    conversation = await _fresh_conversation(service)
    turn = await _create_needs_review_turn(service, conversation, key="preview-unavailable-key")

    with pytest.raises(ModelConversationReconciliationError) as unavailable:
        await service.preview_visible_recovery(conversation.id, turn.id, context_subject_id=None)

    assert unavailable.value.code == "conversation_reconciliation_visible_unavailable"
    assert state.turns[turn.id].status is ConversationTurnStatus.NEEDS_REVIEW
    assert state.model_runs[turn.model_run_id].status is ModelRunStatus.NEEDS_REVIEW
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


async def test_abandoning_investigation_turn_records_next_attempt_without_reusing_key(
    tmp_path: Path,
) -> None:
    class _StatelessAmbiguousAdapter(_ScriptedBridgeAdapter):
        async def invoke(
            self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
        ) -> AdapterResult:
            self.calls.append(request)
            del role, output_schema
            assert request.conversation is None
            return AdapterResult(
                status=AdapterResultStatus.NEEDS_REVIEW,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                response_id="resp_retry_attempt",
                usage=ModelUsage(input_tokens=3, output_tokens=0, total_tokens=3),
                metadata={
                    "reason": "external_turn_identity_unavailable",
                    "output_chars": 24,
                    "recovery_preview_available": True,
                    "submission_state": "post_submission",
                },
            )

    investigation_id = uuid4()
    subject_id = uuid4()
    context_sha256 = "c" * 64
    adapter = _StatelessAmbiguousAdapter("unused")
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    conversation_id = make_investigation_conversation_id(investigation_id)
    conversation = await service.get_or_create(
        conversation_id,
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.PIVOT_RESEARCH,
        title=f"Investigation {investigation_id}",
        edition_id=None,
        subject_id=subject_id,
        expected_profile=None,
        requested_model=None,
    )
    agenda = InvestigationAgenda(
        (
            AgendaItem("First question", 0.9, 1),
            AgendaItem("Second question", 0.8, 2),
        )
    )
    state.investigation_states[investigation_id] = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=agenda,
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256=context_sha256,
        stop_reason=StopReason.MODEL_UNAVAILABLE,
    )
    abandoned_key = make_investigation_turn_idempotency_key(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
    )
    with pytest.raises(ConversationNeedsReviewError):
        await service.add_turn(
            conversation.id,
            message="Canonical investigation prompt",
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key=abandoned_key,
            correlation_id=f"investigation:{investigation_id}:turn:0:{abandoned_key[:16]}",
            context_subject_id=subject_id,
            stateless_external=True,
        )
    turn = next(iter(state.turns.values()))
    turn.error_code = "operator_interrupted"
    state.model_runs[turn.model_run_id].error_code = "operator_interrupted"

    class _IdentityProvider:
        async def current(self) -> Identity:
            return Identity("operator:synthetic")

    app = FastAPI()
    app.include_router(model_conversation_router)
    app.state.model_conversation_service = service
    app.state.identity_provider = _IdentityProvider()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/model-conversations/{conversation.id}/turns/{turn.id}"
            f"/reconciliation/abandon?subject_id={subject_id}",
            json={"reason": "Operator confirmed no execution occurred"},
        )
    assert response.status_code == 200, response.text
    abandoned = state.turns[turn.id]
    persisted_state = state.investigation_states[investigation_id]
    next_attempt = investigation_bridge_attempt(persisted_state.recent_actions, 0)
    retry_key = make_investigation_turn_idempotency_key(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=context_sha256,
        attempt=next_attempt,
    )
    retry = await ModelConversationTurnTransport(service).submit(
        investigation_id=investigation_id,
        subject_id=subject_id,
        turn_index=0,
        context_sha256=context_sha256,
        prompt="Canonical investigation prompt",
        external_llm_allowed=True,
        attempt=next_attempt,
    )
    replay = await service.abandon_investigation_turn(
        conversation.id,
        turn.id,
        reason="Operator confirmed no execution occurred",
        actor_id="operator:synthetic",
        context_subject_id=subject_id,
    )

    assert abandoned.status is ConversationTurnStatus.FAILED
    assert abandoned.error_code == "operator_abandoned"
    assert state.model_runs[turn.model_run_id].status is ModelRunStatus.FAILED
    assert state.model_runs[turn.model_run_id].submission_state.value == "external_state_unknown"
    assert next_attempt == 1
    assert retry_key != abandoned_key
    assert retry_key.endswith(":attempt=1")
    assert retry.status.value == "needs_review", retry
    assert retry.idempotency_key == retry_key
    persisted_turns = await service.turns(conversation.id, context_subject_id=subject_id)
    assert len(persisted_turns) == 2
    assert persisted_turns[1].turn.sequence == 2
    assert persisted_turns[1].turn.parent_turn_id == abandoned.id
    assert len(adapter.calls) == 2
    assert replay.id == abandoned.id


async def test_visible_turn_reconciliation_routes_use_local_preview_and_actor(
    tmp_path: Path,
) -> None:
    text = "Route-level visible response"
    recovery = _VisibleTurnRecovery({"text": text})
    adapter = _StalledVisibleAnswerAdapter(text)
    service, state = _build_service(adapter, tmp_path, visible_recovery_transport=recovery)
    conversation = await _fresh_conversation(service)
    turn = await _create_needs_review_turn(service, conversation, key="api-visible-turn-key")
    subject_id = uuid4()

    class _IdentityProvider:
        async def current(self) -> Identity:
            return Identity("operator:route-test")

    app = FastAPI()
    app.include_router(model_conversation_router)
    app.state.model_conversation_service = service
    app.state.identity_provider = _IdentityProvider()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview_response = await client.post(
            f"/api/model-conversations/{conversation.id}/turns/{turn.id}"
            f"/reconciliation/visible/preview?subject_id={subject_id}"
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        adopt_response = await client.post(
            f"/api/model-conversations/{conversation.id}/turns/{turn.id}"
            f"/reconciliation/visible/adopt?subject_id={subject_id}",
            json={"expected_sha256": preview["sha256"]},
        )

    assert adopt_response.status_code == 200
    adopted = adopt_response.json()
    assert adopted["status"] == "succeeded"
    assert adopted["external_turn_id"] is None
    assert adopted["output_text"] == text
    assert adopted["recovery"]["actor_id"] == "operator:route-test"
    assert adopted["recovery"]["provenance"] == "visible_recovery"
    assert adopted["recovery"]["sha256"] == preview["sha256"]
    assert state.turns[turn.id].status is ConversationTurnStatus.SUCCEEDED
    assert len(adapter.calls) == 1
