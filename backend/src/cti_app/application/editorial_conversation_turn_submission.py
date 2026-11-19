"""Prepare, persist and dispatch editorial conversation message turns."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from cti_app.application.editorial_conversation_turns import (
    EditorialDiscussionContext,
    EditorialDiscussionObjective,
    EditorialTurnContractError,
)
from cti_app.application.model_gateway import (
    BRIDGE_SOURCE_PROVENANCE_KEY,
    BridgeExternalEffectBinding,
    ConversationContext,
    ModelRequest,
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
    EDITORIAL_BRIDGE_TURN_ID_PARAMETER,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
)

if TYPE_CHECKING:
    from cti_app.application.editorial_conversation_turns import (
        EditorialConversationTurns,
        _ConversationTurnService,
    )

logger = logging.getLogger("cti_app.model_conversations")


async def submit_editorial_turn(
    turns: EditorialConversationTurns,
    service: _ConversationTurnService,
    conversation_id: UUID,
    *,
    message: str,
    mode: ConversationMode,
    external_llm_allowed: bool,
    web_search: bool = False,
    stateless_external: bool = False,
    timeout_seconds: float | None = None,
    idempotency_key: str,
    correlation_id: str,
    context_subject_id: UUID | None = None,
    lifecycle_policy: ConversationPolicy = ConversationPolicy.KEEP,
    bridge_external_effect_binding: BridgeExternalEffectBinding | None = None,
    _editorial_thread_id: UUID | None = None,
    editorial_objective: EditorialDiscussionObjective | None = None,
    editorial_context: EditorialDiscussionContext | None = None,
) -> ModelConversationTurn:
    from cti_app.application.model_conversations import (
        CONVERSATION_PROMPT_ID,
        CONVERSATION_PROMPT_VERSION,
        NO_EVIDENCE_PACK_HASH,
        ConversationBusyError,
        ConversationNotFoundError,
        ConversationPolicyError,
        ConversationTurnFailedError,
        EditorialPurposeReservedError,
        _blob_id,
        _role,
        _routing_hint,
        conversation_close_failure_fields,
    )

    message = message.strip()
    if not message:
        raise ConversationPolicyError("Le message est vide")
    try:
        editorial_turn = turns.prepare(
            message,
            editorial_objective,
            editorial_context,
            external_llm_allowed=external_llm_allowed,
            thread_id=_editorial_thread_id,
        )
    except EditorialTurnContractError:
        raise EditorialPurposeReservedError() from None
    turn_input_text = editorial_turn.input_text if editorial_turn is not None else message
    duplicate_success: ModelConversationTurn | None = None
    duplicate_conversation: ModelConversation | None = None
    retry_existing_turn: ModelConversationTurn | None = None
    resume_existing_turn: ModelConversationTurn | None = None
    durable_editorial_turn: ModelConversationTurn | None = None
    durable_editorial_run: ModelRun | None = None
    async with service._uow_factory() as uow:
        duplicate = await uow.model_conversation_turns.get_by_idempotency_key(idempotency_key)
        if duplicate is not None:
            conversation = await uow.model_conversations.get(duplicate.conversation_id)
            service._ensure_visible(conversation, context_subject_id)
            assert conversation is not None
            await service._require_editorial_thread_path(uow, conversation, _editorial_thread_id)
            service._validate_editorial_objective(
                conversation.purpose, editorial_objective, editorial_context
            )
            if duplicate.conversation_id != conversation_id:
                raise ConversationPolicyError(
                    "La clé d'idempotence appartient à une autre conversation"
                )
            if editorial_turn is not None:
                input_blob_id = _blob_id(duplicate.input_blob_reference)
                input_blob = (
                    await uow.blobs.get(input_blob_id) if input_blob_id is not None else None
                )
                if input_blob_id is None or input_blob is None:
                    raise ConversationPolicyError(
                        "Le contexte du tour éditorial précédent est introuvable"
                    )
                persisted_input = await service._read_reference(
                    duplicate.input_blob_reference, {input_blob_id: input_blob}
                )
                if not turns.matches_persisted_input(
                    persisted_input, message, editorial_turn.context
                ):
                    raise ConversationPolicyError(
                        "La clé d'idempotence appartient à un autre message"
                    )
                # Reuse the nonce and exact prompt bytes persisted before the
                # first gateway call; a replay must retain the same request hash.
                turn_input_text = persisted_input
            if duplicate.input_sha256 != hashlib.sha256(turn_input_text.encode()).hexdigest():
                raise ConversationPolicyError("La clé d'idempotence appartient à un autre message")
            # The caller must be able to tell a completed turn from one that
            # ended badly: returning a failed turn as if it succeeded would
            # have the caller look for an output that will never exist.
            if duplicate.status is ConversationTurnStatus.SUCCEEDED:
                duplicate_success = duplicate
                duplicate_conversation = conversation
            elif duplicate.status is ConversationTurnStatus.RUNNING:
                run = await uow.model_runs.get(duplicate.model_run_id)
                if (
                    editorial_turn is not None
                    and duplicate.objective == "editorial_message"
                    and run is not None
                    and run.status is ModelRunStatus.SUCCEEDED
                ):
                    durable_editorial_turn = duplicate
                    durable_editorial_run = run
                elif (
                    editorial_turn is not None
                    and duplicate.objective == "editorial_message"
                    and run is not None
                    and run.status is ModelRunStatus.RUNNING
                    and run.submission_state is ModelSubmissionState.NOT_SUBMITTED
                ):
                    # The owning turn and ModelRun committed before any
                    # provider handoff; replay can submit this exact run.
                    resume_existing_turn = duplicate
                else:
                    # An uncertain run is never submitted a second time.
                    return duplicate
            else:
                failed_run = (
                    await uow.model_runs.get(duplicate.model_run_id)
                    if duplicate.status is ConversationTurnStatus.FAILED
                    else None
                )
                failed_details = failed_run.error_details if failed_run is not None else None
                completed_response = (
                    isinstance(failed_details, dict)
                    and failed_details.get("completed_response") is True
                )
                raw_phase = (
                    failed_details.get("phase") if isinstance(failed_details, dict) else None
                )
                phase = raw_phase[:64] if isinstance(raw_phase, str) else None
                if completed_response:
                    submission_state = "post_submission"
                elif (
                    failed_run is not None
                    and failed_run.submission_state is ModelSubmissionState.NOT_SUBMITTED
                ):
                    submission_state = "pre_submission"
                else:
                    submission_state = None
                if (
                    editorial_turn is not None
                    and failed_run is not None
                    and failed_run.status is ModelRunStatus.FAILED
                    and failed_run.submission_state is ModelSubmissionState.NOT_SUBMITTED
                    and failed_run.error_code == "bridge_rate_limited"
                ):
                    # A complete Bridge rate-limit response proves the turn
                    # never reached generation. Reuse this exact turn and
                    # ModelRun; uncertain submissions remain sealed.
                    retry_existing_turn = duplicate
                else:
                    raise ConversationTurnFailedError(
                        duplicate.error_message or "Le tour précédent n'a pas abouti",
                        code=duplicate.error_code or "conversation_turn_failed",
                        status=duplicate.status,
                        submission_state=submission_state,
                        phase=phase,
                        completed_response=completed_response,
                        retryable=(
                            isinstance(failed_details, dict)
                            and failed_details.get("retryable") is True
                        ),
                        retry_after_seconds=(
                            float(failed_details["retry_after"])
                            if isinstance(failed_details, dict)
                            and isinstance(failed_details.get("retry_after"), int | float)
                            and not isinstance(failed_details.get("retry_after"), bool)
                            else None
                        ),
                    )
        else:
            conversation = await uow.model_conversations.get(conversation_id)
            service._ensure_visible(conversation, context_subject_id)
            assert conversation is not None
            await service._require_editorial_thread_path(uow, conversation, _editorial_thread_id)
            service._validate_editorial_objective(
                conversation.purpose, editorial_objective, editorial_context
            )

    if durable_editorial_turn is not None and durable_editorial_run is not None:
        execution = await service._gateway.resume(durable_editorial_run.id)
        output_reference = execution.run.raw_output_reference or (
            execution.run.output_references[0] if execution.run.output_references else None
        )
        if (
            execution.run.status is not ModelRunStatus.SUCCEEDED
            or execution.output_text is None
            or output_reference is None
        ):
            return durable_editorial_turn
        output_sha256 = hashlib.sha256(execution.output_text.encode()).hexdigest()
        async with service._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            service._ensure_visible(conversation, context_subject_id)
            if conversation is None:
                raise ConversationNotFoundError("Conversation introuvable")
            await service._require_editorial_thread_path(uow, conversation, _editorial_thread_id)
            turn = await uow.model_conversation_turns.get(durable_editorial_turn.id)
            run = await uow.model_runs.get(durable_editorial_run.id)
            if turn is None or run is None or run.status is not ModelRunStatus.SUCCEEDED:
                return durable_editorial_turn
            if turn.status is ConversationTurnStatus.RUNNING:
                external_turn_id = run.parameters.get(EDITORIAL_BRIDGE_TURN_ID_PARAMETER)
                if not isinstance(external_turn_id, str) or not external_turn_id:
                    external_turn_id = None
                turn.succeed(
                    output_blob_reference=output_reference,
                    output_sha256=output_sha256,
                    external_turn_id=external_turn_id,
                )
                conversation.finish_turn(turn.id, external_locator=conversation.external_locator)
                await uow.model_conversation_turns.save(turn)
                await uow.model_conversations.save(conversation)
                await uow.commit()
            return turn

    if duplicate_success is not None:
        # The durable result may have committed while the process crashed
        # before closing the exact live tab. Replay must never submit again,
        # but it must retry that idempotent external close for archived
        # DELETE_ON_SUCCESS conversations.
        if (
            lifecycle_policy is ConversationPolicy.DELETE_ON_SUCCESS
            and duplicate_conversation is not None
            and duplicate_conversation.status is ConversationStatus.ARCHIVED
            and duplicate_conversation.transport is ConversationTransport.CHATGPT_BRIDGE
            and service._conversation_session_closer is not None
        ):
            try:
                await service._conversation_session_closer.archive_conversation(conversation_id)
            except Exception as exc:
                failure = conversation_close_failure_fields(exc)
                logger.warning(
                    "conversation_session_close_failed conversation_id=%s error_code=%s "
                    "cause_code=%s retryable=%s phase=%s reason=%s details=%s",
                    conversation_id,
                    failure["error_code"],
                    failure["cause_code"],
                    failure.get("retryable"),
                    failure.get("phase"),
                    failure["reason"],
                    failure["details"],
                    exc_info=True,
                )
        return duplicate_success

    input_bytes = turn_input_text.encode()
    input_blob = await service._catalog.ingest(
        BytesIO(input_bytes),
        logical_bucket="model-conversation-inputs",
        mime_type="text/plain; charset=utf-8",
    )
    input_reference = f"blob://{input_blob.id}"
    run_id = (
        retry_existing_turn.model_run_id
        if retry_existing_turn is not None
        else resume_existing_turn.model_run_id
        if resume_existing_turn is not None
        else uuid4()
    )

    async with service._uow_factory() as uow:
        conversation = await uow.model_conversations.get_for_update(conversation_id)
        service._ensure_visible(conversation, context_subject_id)
        assert conversation is not None
        await service._require_editorial_thread_path(uow, conversation, _editorial_thread_id)
        service._validate_editorial_objective(
            conversation.purpose, editorial_objective, editorial_context
        )
        if conversation.status is ConversationStatus.BUSY and resume_existing_turn is None:
            raise ConversationBusyError("Une question est déjà en cours")
        if conversation.transport is ConversationTransport.OPENAI_RESPONSES:
            raise ConversationPolicyError(
                "Le transport Conversation API OpenAI natif n'est pas encore configuré"
            )
        if stateless_external and (
            conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
            or conversation.purpose is not ConversationPurpose.PIVOT_RESEARCH
        ):
            raise ConversationPolicyError(
                "Le transport stateless est réservé aux tours Bridge d'investigation"
            )
        if mode is ConversationMode.CONTINUE and conversation.transport not in {
            ConversationTransport.CHATGPT_BRIDGE,
            ConversationTransport.OPENAI_RESPONSES,
        }:
            raise ConversationPolicyError("Ce transport ne permet pas encore continue")
        if stateless_external:
            previous_turns = await uow.model_conversation_turns.list_for_conversation(
                conversation_id
            )
            parent = max(previous_turns, key=lambda item: item.sequence, default=None)
        else:
            parent = (
                await uow.model_conversation_turns.get(conversation.head_turn_id)
                if conversation.head_turn_id
                else None
            )
        if mode is ConversationMode.CONTINUE and (
            parent is None or (not parent.external_turn_id and not stateless_external)
        ):
            raise ConversationPolicyError(
                "Aucun tour externe vérifié à poursuivre pour cette conversation"
            )
        retry_turn: ModelConversationTurn | None = None
        if retry_existing_turn is not None:
            retry_turn = await uow.model_conversation_turns.get(retry_existing_turn.id)
            retry_run = await uow.model_runs.get_for_update(retry_existing_turn.model_run_id)
            if (
                retry_turn is None
                or retry_run is None
                or retry_turn.status is not ConversationTurnStatus.FAILED
                or retry_run.status is not ModelRunStatus.FAILED
                or retry_run.submission_state is not ModelSubmissionState.NOT_SUBMITTED
                or retry_run.error_code != "bridge_rate_limited"
                or retry_turn.parent_turn_id
                != (conversation.head_turn_id if mode is ConversationMode.CONTINUE else None)
            ):
                raise ConversationPolicyError("Le tour éditorial ne peut pas être repris")
            try:
                conversation.resume_known_pre_submission_retry()
                retry_turn.resume_known_pre_submission_retry()
            except ValueError as exc:
                raise ConversationPolicyError(str(exc)) from None
        elif resume_existing_turn is None:
            try:
                conversation.start_turn(mode=mode)
            except ValueError as exc:
                if str(exc) == "conversation_busy":
                    raise ConversationBusyError("Une question est déjà en cours") from exc
                raise ConversationPolicyError(str(exc)) from exc
        context = (
            None
            if stateless_external
            else ConversationContext(
                mode=mode.value,
                id=conversation.id,
                expected_turn_id=(
                    parent.external_turn_id
                    if mode is ConversationMode.CONTINUE and parent
                    else None
                ),
                parent_turn_id=(
                    conversation.head_turn_id if mode is ConversationMode.CONTINUE else None
                ),
                previous_head_hash=(
                    parent.output_sha256 if mode is ConversationMode.CONTINUE and parent else None
                ),
                expected_profile=conversation.expected_profile,
                ui_model=conversation.requested_model,
                external_id=conversation.external_id,
            )
        )
        request = ModelRequest(
            text=turn_input_text,
            prompt_template_id=(
                editorial_turn.prompt_template_id
                if editorial_turn is not None
                else CONVERSATION_PROMPT_ID
            ),
            prompt_template_version=(
                editorial_turn.prompt_template_version
                if editorial_turn is not None
                else CONVERSATION_PROMPT_VERSION
            ),
            evidence_pack_hash=(
                editorial_turn.evidence_pack_hash
                if editorial_turn is not None
                else NO_EVIDENCE_PACK_HASH
            ),
            external_llm_allowed=external_llm_allowed,
            routing_hint=_routing_hint(conversation.purpose),
            sensitivity=(
                editorial_turn.sensitivity if editorial_turn is not None else "conversation"
            ),
            metadata={
                "conversation_output": True,
                "primary_evidence": False,
                **(editorial_turn.request_metadata if editorial_turn is not None else {}),
            },
            web_search=web_search,
            provider=conversation.provider,
            conversation=context,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
        if retry_turn is not None:
            request = replace(request, allow_failed_resubmit=True)
        run = service._gateway.build_run(request, _role(conversation.purpose))
        turn = (
            retry_turn
            or resume_existing_turn
            or ModelConversationTurn(
                conversation_id=conversation.id,
                sequence=conversation.turn_count,
                parent_turn_id=(
                    parent.id
                    if stateless_external and parent is not None
                    else conversation.head_turn_id
                    if mode is ConversationMode.CONTINUE
                    else None
                ),
                model_run_id=run.id,
                input_blob_reference=input_reference,
                input_sha256=input_blob.descriptor.sha256,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                objective=(editorial_turn.model_objective if editorial_turn is not None else None),
            )
        )
        if bridge_external_effect_binding is not None:
            if (
                not stateless_external
                or conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
            ):
                raise ConversationPolicyError(
                    "External effect bindings are reserved for stateless Bridge turns"
                )
            if bridge_external_effect_binding.authority.bridge_turn_id not in (
                None,
                turn.id,
            ) or bridge_external_effect_binding.authority.model_run_id not in (None, run.id):
                raise ConversationPolicyError(
                    "Bridge external effect binding belongs to another persisted attempt"
                )
            authority = replace(
                bridge_external_effect_binding.authority,
                bridge_turn_id=turn.id,
                model_run_id=run.id,
            )
            bridge_external_effect_binding = replace(
                bridge_external_effect_binding,
                authority=authority,
                bridge_turn_id=turn.id,
                model_run_id=run.id,
                model_input_sha256=turn.input_sha256,
                authorized_input_hash=run.authorized_input_hash,
            )
            run.parameters = {
                **run.parameters,
                BRIDGE_SOURCE_PROVENANCE_KEY: {
                    "family": bridge_external_effect_binding.family,
                    "turn_input_sha256": turn.input_sha256,
                    "authorized_input_hash": run.authorized_input_hash,
                    "evidence_refs": [
                        reference.to_json()
                        for reference in bridge_external_effect_binding.evidence_refs
                    ],
                },
            }
            request = replace(
                request,
                bridge_external_effect_binding=bridge_external_effect_binding,
            )
        if retry_turn is None and resume_existing_turn is None:
            await uow.model_runs.add(run)
            await uow.model_conversation_turns.add(turn)
        elif retry_turn is not None:
            await uow.model_conversation_turns.save(turn)
        if resume_existing_turn is None:
            await uow.model_conversations.save(conversation)
        await uow.commit()

    from cti_app.application.editorial_conversation_turn_execution import (
        execute_editorial_turn,
    )

    return await execute_editorial_turn(
        turns,
        service,
        conversation=conversation,
        turn=turn,
        run=run,
        request=request,
        stateless_external=stateless_external,
        lifecycle_policy=lifecycle_policy,
        editorial_turn=editorial_turn,
    )
