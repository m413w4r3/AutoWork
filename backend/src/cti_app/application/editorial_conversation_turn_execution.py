"""Execute and finalize a persisted model conversation turn."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING

from cti_app.application.model_gateway import ModelRequest
from cti_app.domain.model_conversations import (
    ConversationPolicy,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import (
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
)

if TYPE_CHECKING:
    from cti_app.application.editorial_conversation_turns import (
        EditorialConversationTurns,
        EditorialTurnPreparation,
        _ConversationTurnService,
    )

logger = logging.getLogger("cti_app.model_conversations")


async def execute_editorial_turn(
    turns: EditorialConversationTurns,
    service: _ConversationTurnService,
    *,
    conversation: ModelConversation,
    turn: ModelConversationTurn,
    run: ModelRun,
    request: ModelRequest,
    stateless_external: bool,
    lifecycle_policy: ConversationPolicy,
    editorial_turn: EditorialTurnPreparation | None,
) -> ModelConversationTurn:
    from cti_app.application.model_conversations import (
        ConversationTurnFailedError,
        ModelConversationError,
        _needs_review_error,
        _role,
        conversation_close_failure_fields,
    )

    try:
        execution = await service._gateway.execute(request, _role(conversation.purpose))
        if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
            # A native provider needs_review is already a reviewable,
            # non-replayable outcome with its own reason. Keep it typed.
            raise _needs_review_error(execution)
        if execution.run.status is not ModelRunStatus.SUCCEEDED or not execution.output_text:
            raise ModelConversationError("Le modèle n'a pas produit de réponse finale")
        metadata = execution.conversation
        if (
            not stateless_external
            and conversation.transport is ConversationTransport.CHATGPT_BRIDGE
            and (
                metadata is None
                or metadata.id != str(conversation.id)
                or not metadata.verified
                or not metadata.turn_id
            )
        ):
            raise ModelConversationError("Le bridge n'a pas vérifié la conversation cible")
        output_reference = execution.run.output_references[0]
        output_sha256 = hashlib.sha256(execution.output_text.encode()).hexdigest()
        should_close_session = (
            lifecycle_policy is ConversationPolicy.DELETE_ON_SUCCESS
            and conversation.transport is ConversationTransport.CHATGPT_BRIDGE
        )
        async with service._uow_factory() as uow:
            persisted_conversation = await uow.model_conversations.get_for_update(conversation.id)
            persisted_turn = await uow.model_conversation_turns.get(turn.id)
            if persisted_conversation is None or persisted_turn is None:
                raise ModelConversationError("Le tour conversationnel a disparu")
            persisted_turn.succeed(
                output_blob_reference=output_reference,
                output_sha256=output_sha256,
                # No verified bridge metadata means no verified external
                # identity: leave it unset rather than passing off the
                # ModelRun response id as a continuation handle.
                external_turn_id=(
                    metadata.turn_id if metadata is not None and not stateless_external else None
                ),
            )
            persisted_conversation.finish_turn(
                persisted_turn.id,
                external_locator=(
                    metadata.external_locator
                    if metadata is not None and not stateless_external
                    else None
                ),
            )
            # KEEP: durable output first, conversation stays READY, browser
            # session stays alive for a future CONTINUE. DELETE_ON_SUCCESS:
            # durable output first, then archive the application conversation
            # — the browser session itself is only closed after this commit,
            # never while holding the row lock.
            if should_close_session:
                persisted_conversation.archive()
            await uow.model_conversation_turns.save(persisted_turn)
            await uow.model_conversations.save(persisted_conversation)
            await uow.commit()

        if should_close_session and service._conversation_session_closer is not None:
            try:
                await service._conversation_session_closer.archive_conversation(conversation.id)
            except Exception as exc:
                # A close failure never rewrites the already-successful model
                # output as failed — it is only logged.
                failure = conversation_close_failure_fields(exc)
                logger.warning(
                    "conversation_session_close_failed conversation_id=%s error_code=%s "
                    "cause_code=%s retryable=%s phase=%s reason=%s details=%s",
                    conversation.id,
                    failure["error_code"],
                    failure["cause_code"],
                    failure.get("retryable"),
                    failure.get("phase"),
                    failure["reason"],
                    failure["details"],
                    exc_info=True,
                )
        return persisted_turn
    except asyncio.CancelledError:
        try:
            await asyncio.shield(
                service._seal_interrupted_turn(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    run_id=run.id,
                )
            )
        except Exception:
            logger.exception(
                "conversation_turn_interruption_finalization_failed turn_id=%s",
                turn.id,
            )
        raise

    except Exception as exc:
        pre_submission = getattr(exc, "submission_state", None) == "pre_submission"
        completed_response = getattr(exc, "completed_response", False) is True
        safe_message = turns.failure_message() if editorial_turn is not None else str(exc)
        uncertain = (
            not pre_submission
            and not completed_response
            and (
                bool(getattr(exc, "retryable", False))
                or getattr(exc, "code", "")
                in {
                    "bridge_server_error",
                    "bridge_timeout",
                    "bridge_ui_timeout",
                    "model_submission_reconciliation_required",
                }
            )
        )
        blocked = getattr(exc, "code", "") == "external_llm_blocked"
        async with service._uow_factory() as uow:
            persisted_conversation = await uow.model_conversations.get_for_update(conversation.id)
            persisted_turn = await uow.model_conversation_turns.get(turn.id)
            if persisted_conversation and persisted_turn:
                persisted_turn.fail(
                    code=str(getattr(exc, "code", "conversation_turn_failed")),
                    message=safe_message,
                    uncertain=uncertain,
                    blocked=blocked,
                )
                persisted_conversation.mark_problem(uncertain=uncertain)
                await uow.model_conversation_turns.save(persisted_turn)
                await uow.model_conversations.save(persisted_conversation)
            # The gateway is the primary authority over the ModelRun's provider
            # lifecycle. This only guards a narrow, locally-certain case: the
            # ModelRun this turn pre-persisted (for the turn's FK) never left
            # RUNNING/NOT_SUBMITTED, meaning the gateway failed before ever
            # claiming it for submission. Left alone that run would stay RUNNING
            # forever. Any run the gateway did touch (SUCCEEDED, NEEDS_REVIEW,
            # FAILED, or EXTERNAL_STATE_UNKNOWN) is left untouched.
            persisted_run = await uow.model_runs.get_for_update(run.id)
            if (
                persisted_run is not None
                and persisted_run.status is ModelRunStatus.RUNNING
                and persisted_run.submission_state is ModelSubmissionState.NOT_SUBMITTED
            ):
                persisted_run.fail(
                    str(getattr(exc, "code", "conversation_turn_failed")),
                    "Le tour conversationnel a échoué avant toute soumission au fournisseur.",
                )
                await uow.model_runs.save(persisted_run)
            await uow.commit()
        # Le résultat d'un POST bridge est inconnu : ce tour est scellé en
        # NEEDS_REVIEW et ne doit pas consommer les retries du job avec la
        # même clé d'idempotence.
        if uncertain:
            if getattr(exc, "code", "") == "model_submission_reconciliation_required":
                if editorial_turn is not None:
                    raise ConversationTurnFailedError(
                        safe_message,
                        code=str(getattr(exc, "code", "model_submission_reconciliation_required")),
                        status=ConversationTurnStatus.NEEDS_REVIEW,
                    ) from None
                raise exc
            raise ConversationTurnFailedError(
                safe_message,
                code=str(getattr(exc, "code", "conversation_turn_failed")),
                status=ConversationTurnStatus.NEEDS_REVIEW,
            ) from exc
        if editorial_turn is not None:
            retry_after = turns.retry_after_seconds(getattr(exc, "retry_after", None))
            raise ConversationTurnFailedError(
                safe_message,
                code=str(getattr(exc, "code", "conversation_turn_failed")),
                status=(
                    ConversationTurnStatus.BLOCKED if blocked else ConversationTurnStatus.FAILED
                ),
                submission_state="pre_submission" if pre_submission else None,
                retryable=bool(getattr(exc, "retryable", False)),
                retry_after_seconds=retry_after,
                completed_response=completed_response,
            ) from None
        raise
