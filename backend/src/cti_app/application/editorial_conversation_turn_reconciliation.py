"""Reconcile only the exact persisted editorial conversation turn."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from cti_app.application.editorial_conversation_turns import (
    EditorialTurnReconciliation,
    has_persisted_not_submitted_proof,
)
from cti_app.application.editorial_thread_common import is_editorial_turn_objective
from cti_app.domain.editorial_thread import EditorialThreadReconciliationOutcome
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
)
from cti_app.domain.model_runs import ModelRunStatus

if TYPE_CHECKING:
    from cti_app.application.editorial_conversation_turns import (
        EditorialConversationTurns,
        _ConversationTurnService,
    )


async def reconcile_editorial_turn(
    turns: EditorialConversationTurns,
    service: _ConversationTurnService,
    conversation_id: UUID,
    *,
    thread_id: UUID,
    turn_idempotency_key: str,
    context_subject_id: UUID,
) -> EditorialTurnReconciliation:
    """Read and reconcile only the exact persisted editorial message turn."""
    from cti_app.application.model_conversations import ConversationNotFoundError

    async with service._uow_factory() as uow:
        conversation = await uow.model_conversations.get(conversation_id)
        service._ensure_visible(conversation, context_subject_id)
        if conversation is None:
            raise ConversationNotFoundError("Conversation introuvable")
        await service._require_editorial_thread_path(uow, conversation, thread_id)
        if (
            conversation.purpose is not ConversationPurpose.EDITORIAL_DISCUSSION
            or conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
        ):
            raise ConversationNotFoundError("Conversation introuvable")
        turn = await uow.model_conversation_turns.get_by_idempotency_key(turn_idempotency_key)
        if turn is None or turn.conversation_id != conversation_id:
            return EditorialTurnReconciliation(EditorialThreadReconciliationOutcome.STILL_UNKNOWN)
        if not is_editorial_turn_objective(turn.objective):
            return EditorialTurnReconciliation(
                EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                turn_id=turn.id,
            )
        run = await uow.model_runs.get(turn.model_run_id)
        if run is None or run.backend.value != "chatgpt_bridge":
            return EditorialTurnReconciliation(
                EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                turn_id=turn.id,
            )

    if run.status is ModelRunStatus.NEEDS_REVIEW:
        # This gateway operation only reads the exact persisted Bridge run.
        # A provider 404 is deliberately left as unknown by the gateway.
        await service._gateway.reconcile_bridge_submission(run.id)
        async with service._uow_factory() as uow:
            conversation = await uow.model_conversations.get(conversation_id)
            service._ensure_visible(conversation, context_subject_id)
            if conversation is None:
                raise ConversationNotFoundError("Conversation introuvable")
            await service._require_editorial_thread_path(uow, conversation, thread_id)
            turn = await uow.model_conversation_turns.get_by_idempotency_key(turn_idempotency_key)
            if turn is None or turn.conversation_id != conversation_id:
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN
                )
            run = await uow.model_runs.get(turn.model_run_id)
            if run is None:
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                    turn_id=turn.id,
                )

    if run.status is ModelRunStatus.SUCCEEDED:
        reply = await turns._finish_verified_editorial_output(
            service,
            conversation_id,
            thread_id=thread_id,
            turn_idempotency_key=turn_idempotency_key,
            run_id=run.id,
            context_subject_id=context_subject_id,
        )
        if reply is not None:
            return EditorialTurnReconciliation(
                EditorialThreadReconciliationOutcome.RECONCILED_REPLY,
                turn_id=turn.id,
                reply=reply,
            )

    if has_persisted_not_submitted_proof(turn, run):
        lost_codes = {"conversation_unavailable", "bridge_tab_closed"}
        outcome = (
            EditorialThreadReconciliationOutcome.SESSION_LOST
            if turn.error_code in lost_codes
            else EditorialThreadReconciliationOutcome.NOT_SUBMITTED
        )
        async with service._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            service._ensure_visible(conversation, context_subject_id)
            if conversation is None:
                raise ConversationNotFoundError("Conversation introuvable")
            await service._require_editorial_thread_path(uow, conversation, thread_id)
            turn = await uow.model_conversation_turns.get_by_idempotency_key(turn_idempotency_key)
            run = await uow.model_runs.get_for_update(run.id)
            if turn is None or run is None:
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN
                )
            if (
                turn.conversation_id != conversation_id
                or not is_editorial_turn_objective(turn.objective)
                or not has_persisted_not_submitted_proof(turn, run)
            ):
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                    turn_id=turn.id,
                )
            if (
                outcome is EditorialThreadReconciliationOutcome.SESSION_LOST
                and turn.error_code not in lost_codes
            ):
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                    turn_id=turn.id,
                )
            if turn.status in {
                ConversationTurnStatus.RUNNING,
                ConversationTurnStatus.NEEDS_REVIEW,
            }:
                turn.mark_pre_submission_retryable(
                    model_run_id=run.id,
                    submission_attempt=run.submission_attempt,
                )
                await uow.model_conversation_turns.save(turn)
                if conversation.status is not ConversationStatus.UNAVAILABLE:
                    conversation.mark_problem(uncertain=False)
                await uow.model_conversations.save(conversation)
                await uow.commit()
            elif turn.status is not ConversationTurnStatus.FAILED:
                return EditorialTurnReconciliation(
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
                    turn_id=turn.id,
                )
        return EditorialTurnReconciliation(outcome, turn_id=turn.id)

    return EditorialTurnReconciliation(
        EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
        turn_id=turn.id,
    )
