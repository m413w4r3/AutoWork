from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_gateway import (
    ConversationContext,
    ModelExecution,
    ModelGateway,
    ModelRequest,
    ModelRoutingHint,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobRecord
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
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
)

logger = logging.getLogger("cti_app.model_conversations")

NO_EVIDENCE_PACK_HASH = hashlib.sha256(b"model-conversation:no-evidence-pack").hexdigest()
CONVERSATION_PROMPT_ID = "analyst-conversation"
CONVERSATION_PROMPT_VERSION = "1"


class ConversationSessionCloser(Protocol):
    """Closes the exact live browser Temporary Chat session for a conversation.

    Implemented by the bridge capabilities provider. Only CHATGPT_BRIDGE
    transport conversations ever call this — other transports have no
    browser session to close.
    """

    async def archive_conversation(self, conversation_id: UUID) -> None: ...


class ModelConversationError(RuntimeError):
    code = "model_conversation_error"
    status_code = 400


class ConversationNotFoundError(ModelConversationError):
    code = "conversation_not_found"
    status_code = 404


class ConversationBusyError(ModelConversationError):
    code = "conversation_busy"
    status_code = 409


class ConversationTurnFailedError(ModelConversationError):
    """A turn replayed by idempotency key had already ended badly."""

    def __init__(self, message: str, *, code: str, status: ConversationTurnStatus) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ConversationPolicyError(ModelConversationError):
    code = "conversation_policy_blocked"
    status_code = 422


@dataclass(frozen=True, slots=True)
class ConversationTurnContent:
    turn: ModelConversationTurn
    input_text: str
    output_text: str | None


class ModelConversationService:
    """Persistent, transport-neutral conversation aggregate.

    Conversation outputs are deliberately isolated from claims, indicators and
    evidence packs. This service can only create ModelRuns and immutable blobs.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        gateway: ModelGateway,
        blob_store: BlobStore,
        *,
        retention_days: int = 90,
        conversation_session_closer: ConversationSessionCloser | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self.retention_days = retention_days
        self._conversation_session_closer = conversation_session_closer

    async def create(
        self,
        *,
        provider: ModelProvider,
        transport: ConversationTransport,
        purpose: ConversationPurpose,
        title: str,
        edition_id: UUID | None,
        subject_id: UUID | None,
        expected_profile: str | None,
        requested_model: str | None,
    ) -> ModelConversation:
        if (
            provider is not ModelProvider.OPENAI
            and transport is not ConversationTransport.APPLICATION_MANAGED
        ):
            raise ConversationPolicyError("Ce provider exige le transport application_managed")
        if (
            provider is ModelProvider.OPENAI
            and transport is ConversationTransport.APPLICATION_MANAGED
        ):
            raise ConversationPolicyError(
                "OpenAI doit utiliser un transport conversationnel explicite"
            )
        conversation = ModelConversation(
            provider=provider,
            transport=transport,
            purpose=purpose,
            title=title,
            edition_id=edition_id,
            subject_id=subject_id,
            expected_profile=expected_profile,
            requested_model=requested_model,
        )
        async with self._uow_factory() as uow:
            if subject_id is not None and await uow.subjects.get(subject_id) is None:
                raise ConversationNotFoundError("Sujet introuvable")
            if edition_id is not None and await uow.editions.get(edition_id) is None:
                raise ConversationNotFoundError("Édition introuvable")
            await uow.model_conversations.add(conversation)
            await uow.commit()
        return conversation

    async def get_or_create(
        self,
        conversation_id: UUID,
        *,
        provider: ModelProvider,
        transport: ConversationTransport,
        purpose: ConversationPurpose,
        title: str,
        edition_id: UUID | None,
        subject_id: UUID | None,
        expected_profile: str | None,
        requested_model: str | None,
    ) -> ModelConversation:
        """Reuse a conversation the caller can rederive a stable id for.

        Used for Q2's per-chunk, bounded conversations: `conversation_id` is
        deterministic from the chunk's checkpoint identity, so a retry lands
        on the same conversation instead of orphaning a new one — the turn
        idempotency check in `add_turn` requires exactly that.
        """
        async with self._uow_factory() as uow:
            existing = await uow.model_conversations.get(conversation_id)
            if existing is not None:
                self._ensure_visible(existing, subject_id)
                return existing
            conversation = ModelConversation(
                id=conversation_id,
                provider=provider,
                transport=transport,
                purpose=purpose,
                title=title,
                edition_id=edition_id,
                subject_id=subject_id,
                expected_profile=expected_profile,
                requested_model=requested_model,
            )
            if subject_id is not None and await uow.subjects.get(subject_id) is None:
                raise ConversationNotFoundError("Sujet introuvable")
            if edition_id is not None and await uow.editions.get(edition_id) is None:
                raise ConversationNotFoundError("Édition introuvable")
            await uow.model_conversations.add(conversation)
            await uow.commit()
        return conversation

    async def list(
        self,
        *,
        edition_id: UUID | None = None,
        subject_id: UUID | None = None,
        purpose: ConversationPurpose | None = None,
        status: ConversationStatus | None = None,
        provider: ModelProvider | None = None,
    ) -> Sequence[ModelConversation]:
        async with self._uow_factory() as uow:
            return await uow.model_conversations.list(
                edition_id=edition_id,
                subject_id=subject_id,
                purpose=purpose,
                status=status,
                provider=provider,
            )

    async def get(
        self, conversation_id: UUID, *, context_subject_id: UUID | None = None
    ) -> ModelConversation:
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get(conversation_id)
        self._ensure_visible(conversation, context_subject_id)
        assert conversation is not None
        return conversation

    async def turns(
        self, conversation_id: UUID, *, context_subject_id: UUID | None = None
    ) -> Sequence[ConversationTurnContent]:
        await self.get(conversation_id, context_subject_id=context_subject_id)
        async with self._uow_factory() as uow:
            turns = await uow.model_conversation_turns.list_for_conversation(conversation_id)
            blobs = {}
            for turn in turns:
                for reference in (turn.input_blob_reference, turn.output_blob_reference):
                    blob_id = _blob_id(reference)
                    if blob_id is not None and blob_id not in blobs:
                        blobs[blob_id] = await uow.blobs.get(blob_id)
        result = []
        for turn in turns:
            input_text = await self._read_reference(turn.input_blob_reference, blobs)
            output_text = (
                await self._read_reference(turn.output_blob_reference, blobs)
                if turn.output_blob_reference
                else None
            )
            result.append(ConversationTurnContent(turn, input_text, output_text))
        return result

    async def archive_normalized_output(
        self,
        turn: ModelConversationTurn,
        content: str,
        *,
        parser_stage: str,
        normalization_version: str,
        transformations: tuple[str, ...] = (),
    ) -> None:
        """Archive canonical output and attach its diagnostics to the turn run."""
        if turn.status is not ConversationTurnStatus.SUCCEEDED:
            raise ModelConversationError("Only a successful turn can archive normalized output")
        normalized = content.encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()
        run = await self._gateway.get_run(turn.model_run_id)
        if run is None or run.status is not ModelRunStatus.SUCCEEDED:
            raise ModelConversationError("The turn has no successful ModelRun")
        reference = run.normalized_output_reference
        if run.normalized_output_sha256 != digest or not reference:
            reference = await self._gateway.archive_output(
                normalized, mime_type="application/json; charset=utf-8"
            )
        await self._gateway.record_output_diagnostics(
            turn.model_run_id,
            normalized_reference=reference,
            normalized_sha256=digest,
            parser_stage=parser_stage,
            normalization_version=normalization_version,
            transformations=transformations,
            validation_errors=(),
        )

    async def add_turn(
        self,
        conversation_id: UUID,
        *,
        message: str,
        mode: ConversationMode,
        external_llm_allowed: bool,
        web_search: bool = False,
        idempotency_key: str,
        correlation_id: str,
        context_subject_id: UUID | None = None,
        lifecycle_policy: ConversationPolicy = ConversationPolicy.KEEP,
    ) -> ModelConversationTurn:
        message = message.strip()
        if not message:
            raise ConversationPolicyError("Le message est vide")
        duplicate_success: ModelConversationTurn | None = None
        duplicate_conversation: ModelConversation | None = None
        async with self._uow_factory() as uow:
            duplicate = await uow.model_conversation_turns.get_by_idempotency_key(idempotency_key)
            if duplicate is not None:
                conversation = await uow.model_conversations.get(duplicate.conversation_id)
                self._ensure_visible(conversation, context_subject_id)
                if duplicate.conversation_id != conversation_id:
                    raise ConversationPolicyError(
                        "La clé d'idempotence appartient à une autre conversation"
                    )
                if duplicate.input_sha256 != hashlib.sha256(message.encode()).hexdigest():
                    raise ConversationPolicyError(
                        "La clé d'idempotence appartient à un autre message"
                    )
                # The caller must be able to tell a completed turn from one that
                # ended badly: returning a failed turn as if it succeeded would
                # have the caller look for an output that will never exist.
                if duplicate.status is ConversationTurnStatus.SUCCEEDED:
                    duplicate_success = duplicate
                    duplicate_conversation = conversation
                elif duplicate.status is ConversationTurnStatus.RUNNING:
                    # Resume the existing turn rather than re-sending the prompt.
                    return duplicate
                else:
                    raise ConversationTurnFailedError(
                        duplicate.error_message or "Le tour précédent n'a pas abouti",
                        code=duplicate.error_code or "conversation_turn_failed",
                        status=duplicate.status,
                    )

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
                and self._conversation_session_closer is not None
            ):
                try:
                    await self._conversation_session_closer.archive_conversation(conversation_id)
                except Exception:
                    logger.warning(
                        "conversation_session_close_failed conversation_id=%s",
                        conversation_id,
                        exc_info=True,
                    )
            return duplicate_success

        input_bytes = message.encode()
        input_blob = await self._catalog.ingest(
            BytesIO(input_bytes),
            logical_bucket="model-conversation-inputs",
            mime_type="text/plain; charset=utf-8",
        )
        input_reference = f"blob://{input_blob.id}"
        run_id = uuid4()

        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            assert conversation is not None
            if conversation.status is ConversationStatus.BUSY:
                raise ConversationBusyError("Une question est déjà en cours")
            if conversation.transport is ConversationTransport.OPENAI_RESPONSES:
                raise ConversationPolicyError(
                    "Le transport Conversation API OpenAI natif n'est pas encore configuré"
                )
            if mode is ConversationMode.CONTINUE and conversation.transport not in {
                ConversationTransport.CHATGPT_BRIDGE,
                ConversationTransport.OPENAI_RESPONSES,
            }:
                raise ConversationPolicyError("Ce transport ne permet pas encore continue")
            parent = (
                await uow.model_conversation_turns.get(conversation.head_turn_id)
                if conversation.head_turn_id
                else None
            )
            if mode is ConversationMode.CONTINUE and (
                parent is None or not parent.external_turn_id
            ):
                raise ConversationPolicyError(
                    "Aucun tour externe vérifié à poursuivre pour cette conversation"
                )
            try:
                conversation.start_turn(mode=mode)
            except ValueError as exc:
                if str(exc) == "conversation_busy":
                    raise ConversationBusyError("Une question est déjà en cours") from exc
                raise ConversationPolicyError(str(exc)) from exc
            context = ConversationContext(
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
                requested_model=conversation.requested_model,
                external_id=conversation.external_id,
            )
            request = ModelRequest(
                text=message,
                prompt_template_id=CONVERSATION_PROMPT_ID,
                prompt_template_version=CONVERSATION_PROMPT_VERSION,
                evidence_pack_hash=NO_EVIDENCE_PACK_HASH,
                external_llm_allowed=external_llm_allowed,
                routing_hint=_routing_hint(conversation.purpose),
                sensitivity="conversation",
                metadata={"conversation_output": True, "primary_evidence": False},
                web_search=web_search,
                provider=conversation.provider,
                conversation=context,
                run_id=run_id,
            )
            run = self._gateway.build_run(request, _role(conversation.purpose))
            turn = ModelConversationTurn(
                conversation_id=conversation.id,
                sequence=conversation.turn_count,
                parent_turn_id=(
                    conversation.head_turn_id if mode is ConversationMode.CONTINUE else None
                ),
                model_run_id=run.id,
                input_blob_reference=input_reference,
                input_sha256=input_blob.descriptor.sha256,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            await uow.model_runs.add(run)
            await uow.model_conversation_turns.add(turn)
            await uow.model_conversations.save(conversation)
            await uow.commit()

        try:
            execution = await self._gateway.execute(request, _role(conversation.purpose))
            if execution.run.status is not ModelRunStatus.SUCCEEDED or not execution.output_text:
                raise ModelConversationError("Le modèle n'a pas produit de réponse finale")
            metadata = execution.conversation
            if conversation.transport is ConversationTransport.CHATGPT_BRIDGE and (
                metadata is None
                or metadata.id != str(conversation.id)
                or not metadata.verified
                or not metadata.turn_id
            ):
                raise ModelConversationError("Le bridge n'a pas vérifié la conversation cible")
            output_reference = execution.run.output_references[0]
            output_sha256 = hashlib.sha256(execution.output_text.encode()).hexdigest()
            should_close_session = (
                lifecycle_policy is ConversationPolicy.DELETE_ON_SUCCESS
                and conversation.transport is ConversationTransport.CHATGPT_BRIDGE
            )
            async with self._uow_factory() as uow:
                persisted_conversation = await uow.model_conversations.get_for_update(
                    conversation.id
                )
                persisted_turn = await uow.model_conversation_turns.get(turn.id)
                if persisted_conversation is None or persisted_turn is None:
                    raise ModelConversationError("Le tour conversationnel a disparu")
                persisted_turn.succeed(
                    output_blob_reference=output_reference,
                    output_sha256=output_sha256,
                    # No verified bridge metadata means no verified external
                    # identity: leave it unset rather than passing off the
                    # ModelRun response id as a continuation handle.
                    external_turn_id=metadata.turn_id if metadata else None,
                )
                persisted_conversation.finish_turn(
                    persisted_turn.id,
                    external_locator=metadata.external_locator if metadata else None,
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

            if should_close_session and self._conversation_session_closer is not None:
                try:
                    await self._conversation_session_closer.archive_conversation(conversation.id)
                except Exception:
                    # A close failure never rewrites the already-successful model
                    # output as failed — it is only logged.
                    logger.warning(
                        "conversation_session_close_failed conversation_id=%s",
                        conversation.id,
                        exc_info=True,
                    )
            return persisted_turn
        except Exception as exc:
            pre_submission = getattr(exc, "submission_state", None) == "pre_submission"
            uncertain = not pre_submission and (
                bool(getattr(exc, "retryable", False))
                or getattr(exc, "code", "")
                in {
                    "bridge_server_error",
                    "bridge_timeout",
                    "bridge_ui_timeout",
                    "model_submission_reconciliation_required",
                }
            )
            blocked = getattr(exc, "code", "") == "external_llm_blocked"
            async with self._uow_factory() as uow:
                persisted_conversation = await uow.model_conversations.get_for_update(
                    conversation.id
                )
                persisted_turn = await uow.model_conversation_turns.get(turn.id)
                if persisted_conversation and persisted_turn:
                    persisted_turn.fail(
                        code=str(getattr(exc, "code", "conversation_turn_failed")),
                        message=str(exc),
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
                # FAILED, or SUBMITTED_OR_UNKNOWN) is left untouched.
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
                    raise exc
                raise ConversationTurnFailedError(
                    str(exc),
                    code=str(getattr(exc, "code", "conversation_turn_failed")),
                    status=ConversationTurnStatus.NEEDS_REVIEW,
                ) from exc
            raise

    async def extract_structured(
        self,
        *,
        message: str,
        evidence_pack_hash: str,
        output_schema: type[BaseModel],
        external_llm_allowed: bool,
        prompt_template_id: str,
        prompt_template_version: str,
        correlation_id: str,
        web_search: bool = False,
        run_id: UUID | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ModelExecution:
        """Run a stateless, schema-constrained extraction model request."""
        request = ModelRequest(
            text=message,
            prompt_template_id=prompt_template_id,
            prompt_template_version=prompt_template_version,
            evidence_pack_hash=evidence_pack_hash,
            external_llm_allowed=external_llm_allowed,
            routing_hint=ModelRoutingHint.BULK_EXTRACTION,
            sensitivity="archived_evidence",
            metadata={
                "conversation_output": False,
                "primary_evidence": True,
                "correlation_id": correlation_id,
            },
            parameters=parameters or {},
            web_search=web_search,
            run_id=run_id or uuid4(),
        )
        execution = await self._gateway.extract(request, output_schema)
        if execution.run.status is not ModelRunStatus.SUCCEEDED:
            raise ModelConversationError("Le modèle n'a pas produit d'extraction structurée")
        if not isinstance(execution.structured_output, output_schema):
            raise ModelConversationError("Le modèle n'a pas produit le schéma d'extraction attendu")
        return execution

    async def archive(
        self, conversation_id: UUID, *, context_subject_id: UUID | None = None
    ) -> ModelConversation:
        """Archive the application conversation, then close its browser session.

        Domain state is made durable first so new turns are blocked
        immediately. A repeated call is safe: it does not re-archive an
        already-archived conversation, but it does retry closing the exact
        external tab. A close failure surfaces as an honest error without
        reactivating the (already durable) archived conversation.
        """
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            assert conversation is not None
            if conversation.status is not ConversationStatus.ARCHIVED:
                try:
                    conversation.archive()
                except ValueError as exc:
                    raise ConversationBusyError(str(exc)) from exc
                await uow.model_conversations.save(conversation)
                await uow.commit()
            transport = conversation.transport

        # No row lock is held here: the closer performs external HTTP/browser I/O.
        if transport is ConversationTransport.CHATGPT_BRIDGE:
            if self._conversation_session_closer is None:
                raise ModelConversationError(
                    "Aucun mécanisme de fermeture de session bridge n'est configuré"
                )
            try:
                await self._conversation_session_closer.archive_conversation(conversation_id)
            except Exception as exc:
                logger.warning(
                    "conversation_session_close_failed conversation_id=%s",
                    conversation_id,
                    exc_info=True,
                )
                raise ModelConversationError(
                    "La conversation est archivée mais sa session navigateur n'a pas pu être fermée"
                ) from exc
        return conversation

    async def reconcile(
        self, conversation_id: UUID, *, available: bool, context_subject_id: UUID | None = None
    ) -> ModelConversation:
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            assert conversation is not None
            if conversation.status is ConversationStatus.ARCHIVED:
                raise ConversationPolicyError(
                    "Une conversation archivée ne peut pas être réconciliée"
                )
            running_turns = [
                turn
                for turn in await uow.model_conversation_turns.list_for_conversation(
                    conversation_id
                )
                if turn.status is ConversationTurnStatus.RUNNING
            ]
            for turn in running_turns:
                turn.fail(
                    code="conversation_reconciled_uncertain",
                    message="Le résultat du clic précédent reste incertain ; aucune resoumission.",
                    uncertain=True,
                )
                await uow.model_conversation_turns.save(turn)
            conversation.status = (
                ConversationStatus.READY if available else ConversationStatus.UNAVAILABLE
            )
            conversation.version += 1
            await uow.model_conversations.save(conversation)
            await uow.commit()
            return conversation

    @staticmethod
    def _ensure_visible(
        conversation: ModelConversation | None, context_subject_id: UUID | None
    ) -> None:
        if conversation is None or (
            context_subject_id is not None
            and conversation.subject_id is not None
            and conversation.subject_id != context_subject_id
        ):
            raise ConversationNotFoundError("Conversation introuvable dans ce sujet")

    async def _read_reference(self, reference: str, blobs: dict[UUID, BlobRecord | None]) -> str:
        blob_id = _blob_id(reference)
        if blob_id is None:
            raise ModelConversationError("Référence de blob conversationnel invalide")
        blob = blobs.get(blob_id)
        if blob is None:
            raise ModelConversationError("Blob conversationnel introuvable")
        return (await self._blob_store.read(blob.descriptor, max_bytes=2_000_000)).decode()


def _blob_id(reference: str | None) -> UUID | None:
    if not reference or not reference.startswith("blob://"):
        return None
    try:
        return UUID(reference.removeprefix("blob://"))
    except ValueError:
        return None


def _role(purpose: ConversationPurpose) -> ModelRole:
    if purpose is ConversationPurpose.CRITIC:
        return ModelRole.CRITIC
    if purpose is ConversationPurpose.DRAFTING:
        return ModelRole.DRAFTING
    return ModelRole.RESEARCH


def _routing_hint(purpose: ConversationPurpose) -> ModelRoutingHint:
    if purpose is ConversationPurpose.CRITIC:
        return ModelRoutingHint.CRITIQUE
    if purpose is ConversationPurpose.DRAFTING:
        return ModelRoutingHint.STANDARD_DRAFT
    return ModelRoutingHint.WEB_RESEARCH
