"""Start a new private conversation after a Bridge session is lost."""

from __future__ import annotations

from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.editorial_thread_common import (
    _binding_from_resolution,
    _new_session_thread_id,
    _resolve_current,
    is_valid_editorial_idempotency_key,
)
from cti_app.application.editorial_thread_types import (
    EditorialThreadApplicationError,
    EditorialThreadSessionStartResult,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
)
from cti_app.domain.model_conversations import ConversationPurpose, ConversationTransport
from cti_app.domain.model_runs import ModelProvider


class EditorialThreadSessions:
    """Own deterministic conversation recovery and new-session creation."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        model_conversations: ModelConversationService | None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._model_conversations = model_conversations

    async def start_new_session(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        idempotency_key: str,
    ) -> EditorialThreadSessionStartResult:
        """Create a fresh private conversation only after the current session is lost."""
        if not is_valid_editorial_idempotency_key(idempotency_key):
            raise EditorialThreadApplicationError("idempotency_key_invalid")
        resolution = await _resolve_current(self._workspace, subject_id, occurrence_id)
        binding = _binding_from_resolution(resolution)
        async with self._uow_factory() as uow:
            current = await uow.editorial_threads.get_current_by_occurrence(
                subject_id, binding.production_run_id, occurrence_id
            )
            if current is None:
                raise EditorialThreadApplicationError("editorial_thread_session_not_lost")
            previous = current.thread
            if previous.bridge_status is not EditorialBridgeStatus.SESSION_LOST:
                if previous.session_number > 1:
                    replay_id = _new_session_thread_id(
                        previous, previous.session_number - 1, idempotency_key
                    )
                    replay = await uow.editorial_threads.get(replay_id)
                    if (
                        replay is not None
                        and replay.thread.session_number == previous.session_number
                    ):
                        return EditorialThreadSessionStartResult(
                            thread_id=replay.thread.id,
                            session_number=replay.thread.session_number,
                        )
                raise EditorialThreadApplicationError("editorial_thread_session_not_lost")
            if previous.session_number > 1:
                replay_id = _new_session_thread_id(
                    previous, previous.session_number - 1, idempotency_key
                )
                replay = await uow.editorial_threads.get(replay_id)
                if replay is not None and replay.thread.session_number == previous.session_number:
                    return EditorialThreadSessionStartResult(
                        thread_id=replay.thread.id,
                        session_number=replay.thread.session_number,
                    )
            next_session_number = previous.session_number + 1
            thread_id = _new_session_thread_id(previous, previous.session_number, idempotency_key)
            existing = await uow.editorial_threads.get(thread_id)
            if existing is not None:
                return EditorialThreadSessionStartResult(
                    thread_id=existing.thread.id,
                    session_number=existing.thread.session_number,
                )

        if self._model_conversations is None:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable")
        thread = previous.start_new_session(
            origin=previous.origin,
            model_conversation_id=uuid5(
                NAMESPACE_URL, f"editorial-thread-conversation:{thread_id}"
            ),
        )
        if thread.id != thread_id or thread.session_number != next_session_number:
            thread = replace(thread, id=thread_id, session_number=next_session_number)
        conversation_id = thread.model_conversation_id
        if conversation_id is None:
            raise EditorialThreadApplicationError("editorial_thread_conversation_missing")
        try:
            conversation = await self._model_conversations.get_or_create(
                conversation_id,
                provider=ModelProvider.OPENAI,
                transport=ConversationTransport.CHATGPT_BRIDGE,
                purpose=ConversationPurpose.EDITORIAL_DISCUSSION,
                title="Discussion éditoriale",
                edition_id=None,
                subject_id=subject_id,
                expected_profile=None,
                requested_model=None,
                _editorial_thread_id=thread.id,
                _allow_missing_editorial_thread_recovery=True,
            )
        except Exception:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable") from None
        if (
            conversation.id != thread.model_conversation_id
            or conversation.subject_id != subject_id
            or conversation.purpose is not ConversationPurpose.EDITORIAL_DISCUSSION
            or conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
        ):
            raise EditorialThreadApplicationError("thread_not_found")
        async with self._uow_factory() as uow:
            try:
                created = await uow.editorial_threads.create_if_absent(thread)
                await uow.commit()
            except Exception:
                raise EditorialThreadApplicationError(
                    "editorial_thread_service_unavailable"
                ) from None
        return EditorialThreadSessionStartResult(
            thread_id=created.id,
            session_number=created.session_number,
        )
