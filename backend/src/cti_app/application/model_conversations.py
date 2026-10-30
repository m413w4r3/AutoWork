from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, Protocol
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_gateway import (
    BRIDGE_SOURCE_PROVENANCE_KEY,
    BridgeExternalEffectBinding,
    ConversationContext,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.investigation_state import (
    StopReason,
    investigation_abandoned_turn_id,
    investigation_bridge_attempt,
    investigation_orphaned_turn_id,
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
    ModelSubmissionState,
)

logger = logging.getLogger("cti_app.model_conversations")

MODEL_SUBMISSION_RECONCILIATION_CODE = ModelSubmissionReconciliationRequiredError.code

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


class VisibleRecoveryTransport(Protocol):
    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]: ...


class ModelConversationError(RuntimeError):
    code = "model_conversation_error"
    status_code = 400


class ModelConversationReconciliationError(ModelConversationError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class ConversationSessionCloseError(ModelConversationError):
    """The durable conversation archive succeeded but browser cleanup failed."""

    code = "conversation_session_close_failed"
    status_code = 502

    def __init__(
        self,
        conversation_id: UUID,
        *,
        cause_code: str | None,
        retryable: bool,
        details: dict[str, Any] | None = None,
        phase: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(
            "La conversation est archivée mais sa session navigateur n'a pas pu être fermée"
        )
        self.conversation_id = conversation_id
        self.cause_code = cause_code
        self.retryable = retryable
        self.details = _bounded_close_details(details or {})
        self.phase = phase
        self.reason = _bounded_close_text(reason) if reason else None


def _bounded_close_text(value: object) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:512]


def _bounded_close_details(value: object, depth: int = 0) -> dict[str, Any]:
    """Keep bridge cleanup diagnostics small and free of prompt-like fields."""
    if not isinstance(value, dict) or depth > 2:
        return {}
    result: dict[str, Any] = {}
    for key, child in list(value.items())[:32]:
        name = str(key)
        if any(
            marker in name.casefold()
            for marker in ("prompt", "composer", "input_text", "output_text")
        ):
            continue
        if isinstance(child, dict):
            cleaned = _bounded_close_details(child, depth + 1)
            if cleaned:
                result[name[:64]] = cleaned
        elif isinstance(child, list):
            result[name[:64]] = [
                _bounded_close_text(item)
                for item in child[:16]
                if not isinstance(item, (dict, list))
            ]
        elif isinstance(child, (str, int, float, bool)) or child is None:
            result[name[:64]] = _bounded_close_text(child) if isinstance(child, str) else child
    return result


def conversation_close_failure_fields(exc: BaseException) -> dict[str, Any]:
    """Extract a bounded, structured view of a browser cleanup failure."""
    chain: list[BaseException] = [exc]
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        chain.append(cause)

    error_code = getattr(exc, "code", None)
    error_code = error_code if isinstance(error_code, str) else None
    cause_code = getattr(exc, "cause_code", None)
    cause_code = cause_code if isinstance(cause_code, str) else None
    if cause_code is None:
        for candidate in chain[1:]:
            candidate_code = getattr(candidate, "code", None)
            if isinstance(candidate_code, str):
                cause_code = candidate_code
                break
    if cause_code is None and error_code not in {None, ConversationSessionCloseError.code}:
        cause_code = error_code

    retryable: bool | None = None
    phase: str | None = None
    details: dict[str, Any] = {}
    for candidate in chain:
        candidate_retryable = getattr(candidate, "retryable", None)
        if retryable is None and isinstance(candidate_retryable, bool):
            retryable = candidate_retryable
        candidate_phase = getattr(candidate, "phase", None)
        if phase is None and isinstance(candidate_phase, str):
            phase = candidate_phase[:64]
        candidate_details = getattr(candidate, "details", None)
        if not isinstance(candidate_details, dict):
            candidate_details = getattr(candidate, "diagnostics", None)
        if not details and isinstance(candidate_details, dict):
            details = _bounded_close_details(candidate_details)

    reason = getattr(exc, "reason", None)
    if not isinstance(reason, str):
        reason = _bounded_close_text(chain[1] if len(chain) > 1 else exc)
    result: dict[str, Any] = {
        "error_code": error_code or type(exc).__name__,
        "cause_code": cause_code,
        "reason": _bounded_close_text(reason),
        "details": details,
    }
    if retryable is not None:
        result["retryable"] = retryable
    if phase is not None:
        result["phase"] = phase
    return result


class ConversationNotFoundError(ModelConversationError):
    code = "conversation_not_found"
    status_code = 404


class ConversationBusyError(ModelConversationError):
    code = "conversation_busy"
    status_code = 409


class ConversationTurnFailedError(ModelConversationError):
    """A turn replayed by idempotency key had already ended badly."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: ConversationTurnStatus,
        submission_state: str | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        completed_response: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.submission_state = submission_state
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.completed_response = completed_response


class ConversationPolicyError(ModelConversationError):
    code = "conversation_policy_blocked"
    status_code = 422


class ConversationNeedsReviewError(ModelConversationError):
    """The provider answered, but not with a final answer this turn can close.

    The bridge already sealed its own run as `needs_review` after the prompt was
    submitted. Collapsing that into a generic "no final answer" error lost the
    reason, the exact ModelRun and the fact that a visible candidate answer may
    still be recoverable -- and made Production record a terminal failure for a
    subject whose answer was on screen. This error carries that identity so the
    run lands on the explicit reconciliation path instead.
    """

    code = MODEL_SUBMISSION_RECONCILIATION_CODE
    status_code = 409
    retryable = False
    submission_state = "post_submission"

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        model_run_id: UUID,
        bridge_response_id: str | None,
        details: dict[str, Any],
        recovery_available: bool,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.model_run_id = model_run_id
        self.bridge_response_id = bridge_response_id
        self.details = details
        self.recovery_available = recovery_available


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
        visible_recovery_transport: VisibleRecoveryTransport | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self.retention_days = retention_days
        self._conversation_session_closer = conversation_session_closer
        self._visible_recovery_transport = visible_recovery_transport

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

    async def turn_content_by_idempotency_key(
        self,
        conversation_id: UUID,
        idempotency_key: str,
        *,
        context_subject_id: UUID | None = None,
    ) -> ConversationTurnContent | None:
        """Load one persisted turn without replaying a conversation's full history."""
        await self.get(conversation_id, context_subject_id=context_subject_id)
        async with self._uow_factory() as uow:
            turn = await uow.model_conversation_turns.get_by_idempotency_key(idempotency_key)
            if turn is None:
                return None
            if turn.conversation_id != conversation_id:
                raise ConversationPolicyError(
                    "La clé d'idempotence appartient à une autre conversation"
                )
            blobs = {}
            blob_id = _blob_id(turn.output_blob_reference)
            if blob_id is not None:
                blobs[blob_id] = await uow.blobs.get(blob_id)
        output_text = (
            await self._read_reference(turn.output_blob_reference, blobs)
            if turn.output_blob_reference
            else None
        )
        return ConversationTurnContent(turn, "", output_text)

    async def _recovery_target(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        context_subject_id: UUID | None,
    ) -> tuple[ModelConversation, ModelConversationTurn, Any]:
        conversation = await self.get(conversation_id, context_subject_id=context_subject_id)
        async with self._uow_factory() as uow:
            turn = await uow.model_conversation_turns.get(turn_id)
            run = (
                await uow.model_runs.get(turn.model_run_id)
                if turn is not None and turn.conversation_id == conversation_id
                else None
            )
        if turn is None or turn.conversation_id != conversation_id:
            raise ConversationNotFoundError("Tour introuvable dans cette conversation")
        if run is None:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_run_missing",
                "Le ModelRun du tour est introuvable.",
            )
        return conversation, turn, run

    @staticmethod
    def _visible_recovery_identity(conversation: ModelConversation, run: Any) -> str:
        if (
            conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
            or run.backend is not ModelBackend.CHATGPT_BRIDGE
        ):
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_backend_unsupported",
                "La récupération visible est réservée aux tours ChatGPT Bridge.",
                status_code=422,
            )
        legacy_abandoned_unknown = (
            run.status is ModelRunStatus.FAILED
            and run.error_code == "operator_abandoned"
            and isinstance(run.error_details, dict)
            and isinstance(run.error_details.get("operator_abandonment"), dict)
            and run.error_details["operator_abandonment"].get("external_outcome")
            in {
                ModelSubmissionState.EXTERNAL_STATE_UNKNOWN.value,
                ModelSubmissionState.SUBMISSION_IN_PROGRESS.value,
            }
            and run.submission_state.value
            in {
                ModelSubmissionState.EXTERNAL_STATE_UNKNOWN.value,
                ModelSubmissionState.SUBMISSION_IN_PROGRESS.value,
            }
        )
        if run.status is not ModelRunStatus.NEEDS_REVIEW and not legacy_abandoned_unknown:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_not_required",
                "Le ModelRun ne nécessite plus de réconciliation.",
            )
        preview_unavailable = (run.error_details or {}).get("recovery_preview_available") is False
        response_id = run.response_id
        if not isinstance(response_id, str) or not response_id or preview_unavailable:
            raise ModelConversationService._visible_unavailable()
        return response_id

    @staticmethod
    def _visible_unavailable() -> ModelConversationReconciliationError:
        return ModelConversationReconciliationError(
            "conversation_reconciliation_visible_unavailable",
            "L'aperçu Bridge n'est pas disponible pour ce tour ; aucune requête n'a été resoumise.",
            status_code=409,
        )

    async def preview_visible_recovery(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        context_subject_id: UUID | None = None,
    ) -> dict[str, Any]:
        conversation, turn, run = await self._recovery_target(
            conversation_id, turn_id, context_subject_id=context_subject_id
        )
        legacy_abandoned_unknown = (
            turn.status is ConversationTurnStatus.FAILED and turn.error_code == "operator_abandoned"
        )
        if turn.status is not ConversationTurnStatus.NEEDS_REVIEW and not legacy_abandoned_unknown:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_not_required",
                "Ce tour ne nécessite plus de réconciliation.",
            )
        bridge_run_id = self._visible_recovery_identity(conversation, run)
        if self._visible_recovery_transport is None:
            raise self._visible_unavailable()
        try:
            payload = await self._visible_recovery_transport.preview_visible_recovery(bridge_run_id)
        except Exception as exc:
            raise self._visible_unavailable() from exc
        if payload.get("bridge_run_id") != bridge_run_id:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_identity_mismatch",
                "Le Bridge a renvoyé un résultat différent du ModelRun ciblé.",
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_empty_response",
                "Aucune réponse finale visible n'est disponible pour ce tour.",
            )
        content = text.encode("utf-8")
        return {
            "turn_id": turn.id,
            "sha256": hashlib.sha256(content).hexdigest(),
            "characters": len(text),
            "text": text,
        }

    async def adopt_visible_recovery(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        expected_sha256: str,
        actor_id: str,
        context_subject_id: UUID | None = None,
    ) -> ModelConversationTurn:
        if not _is_sha256(expected_sha256):
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_invalid_hash",
                "expected_sha256 doit contenir 64 caractères hexadécimaux minuscules.",
                status_code=422,
            )
        _, turn, run = await self._recovery_target(
            conversation_id, turn_id, context_subject_id=context_subject_id
        )
        if run.status is ModelRunStatus.SUCCEEDED:
            recovery = (run.error_details or {}).get("recovery")
            if (
                run.raw_output_sha256 == expected_sha256
                and isinstance(recovery, dict)
                and recovery.get("provenance") == "visible_recovery"
            ):
                return turn
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_adoption_conflict",
                "Le tour a déjà été réconcilié avec un autre résultat.",
            )
        legacy_abandoned_unknown = (
            turn.status is ConversationTurnStatus.FAILED and turn.error_code == "operator_abandoned"
        )
        if turn.status is not ConversationTurnStatus.NEEDS_REVIEW and not legacy_abandoned_unknown:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_not_required",
                "Ce tour ne peut pas être adopté dans son état actuel.",
            )
        preview = await self.preview_visible_recovery(
            conversation_id, turn_id, context_subject_id=context_subject_id
        )
        if preview["sha256"] != expected_sha256:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_hash_mismatch",
                "Le SHA-256 visible ne correspond pas à expected_sha256.",
            )
        try:
            await self._gateway.adopt_recovery_output(
                run.id,
                preview["text"].encode("utf-8"),
                provenance="visible_recovery",
                actor_id=actor_id,
                recovery_reason="operator_confirmed_visible_sha256",
                external_turn_id=None,
                bridge_response_id=run.response_id,
            )
        except (ModelGatewayError, ValueError) as exc:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_adoption_conflict",
                "Le ModelRun exact ne peut pas adopter ce résultat dans son état actuel.",
            ) from exc
        async with self._uow_factory() as uow:
            adopted = await uow.model_conversation_turns.get(turn_id)
        if adopted is None or adopted.status is not ConversationTurnStatus.SUCCEEDED:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_adoption_incomplete",
                "Le ModelRun a été adopté mais le tour local n'a pas été clôturé.",
                status_code=500,
            )
        return adopted

    async def seal_verified_pre_submission(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        claim_id: UUID,
        actor_id: str,
        context_subject_id: UUID | None = None,
    ) -> ModelConversationTurn:
        """Consume a server-verified no-handoff claim for this exact Bridge turn."""
        if not actor_id.strip():
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_actor_required",
                "Une identité opérateur est requise.",
                status_code=403,
            )
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            turn = await uow.model_conversation_turns.get(turn_id)
            if turn is None or turn.conversation_id != conversation_id:
                raise ConversationNotFoundError("Tour introuvable dans cette conversation")
            run = await uow.model_runs.get_for_update(turn.model_run_id)
            if (
                conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
                or conversation.purpose is not ConversationPurpose.PIVOT_RESEARCH
                or run is None
                or run.backend is not ModelBackend.CHATGPT_BRIDGE
            ):
                raise ModelConversationReconciliationError(
                    "conversation_reconciliation_no_handoff_conflict",
                    "La preuve ne correspond pas à un tour Bridge d'investigation.",
                )
            proof = (run.error_details or {}).get("pre_submission_proof") if run is not None else None
            if (
                run is not None
                and turn.status is ConversationTurnStatus.FAILED
                and turn.error_code == "bridge_pre_submission_failure"
                and run.status is ModelRunStatus.FAILED
                and run.error_code == "bridge_pre_submission_failure"
                and isinstance(proof, dict)
                and proof.get("source") == "external_effect_claim"
                and proof.get("claim_id") == str(claim_id)
                and turn.output_blob_reference is None
                and run.response_id is None
                and not run.output_references
                and run.raw_output_reference is None
                and run.raw_output_sha256 is None
                and run.raw_output_chars in (None, 0)
                and turn.error_details == {
                    "pre_submission_proof": {
                        "source": "external_effect_claim",
                        "claim_id": str(claim_id),
                        "model_run_id": str(run.id),
                    }
                }
            ):
                return turn
            if (
                run is None
                or turn.status is not ConversationTurnStatus.NEEDS_REVIEW
                or turn.output_blob_reference is not None
                or run.response_id is not None
                or run.output_references
                or run.raw_output_reference is not None
                or run.raw_output_sha256 is not None
                or run.raw_output_chars not in (None, 0)
            ):
                raise ModelConversationReconciliationError(
                    "conversation_reconciliation_no_handoff_conflict",
                    "Le tour contient une réponse ou ne correspond pas à une preuve de non-handoff.",
                )
            try:
                run.seal_verified_no_handoff(claim_id=claim_id)
                turn.seal_verified_no_handoff(model_run_id=run.id, claim_id=claim_id)
            except (TypeError, ValueError) as exc:
                raise ModelConversationReconciliationError(
                    "conversation_reconciliation_no_handoff_conflict",
                    "Le ModelRun ne peut pas être scellé comme non soumis.",
                ) from exc
            conversation.mark_problem(uncertain=False)
            await uow.model_conversation_turns.save(turn)
            await uow.model_runs.save(run)
            await uow.model_conversations.save(conversation)
            await uow.commit()
            return turn

    async def abandon_investigation_turn(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        reason: str,
        actor_id: str,
        context_subject_id: UUID | None = None,
    ) -> ModelConversationTurn:
        reason = " ".join(reason.replace("\x00", " ").split())
        if not reason or len(reason) > 1000:
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_reason_required",
                "Une raison de 1 à 1000 caractères est requise.",
                status_code=422,
            )
        if not actor_id.strip():
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_actor_required",
                "Une identité opérateur est requise.",
                status_code=403,
            )
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            turn = await uow.model_conversation_turns.get(turn_id)
            if turn is None or turn.conversation_id != conversation_id:
                raise ConversationNotFoundError("Tour introuvable dans cette conversation")
            raise ModelConversationReconciliationError(
                "conversation_reconciliation_requires_provider_resolution",
                "Une raison libre ne prouve pas la non-exécution. Récupérez et adoptez la réponse "
                "Bridge exacte, ou attendez une preuve serveur de non-handoff ; aucun nouvel "
                "envoi n'est autorisé par cette action.",
            )

    async def reconcile_orphaned_investigation_turn(
        self,
        *,
        investigation_id: UUID,
        turn_index: int,
        context_sha256: str,
        attempt: int,
        owner_job_terminal: bool,
    ) -> str:
        """Seal an ownerless, output-free turn and reserve a distinct retry key.

        The caller establishes that the prior investigation job is terminal
        before enabling recovery. A captured local result is restored/replayed
        under its original key; it is never submitted to Bridge again.
        """
        if not owner_job_terminal:
            return "active_job"
        from cti_app.application.bridge_investigator import (
            make_investigation_conversation_id,
            make_investigation_turn_idempotency_key,
        )

        expected_key = make_investigation_turn_idempotency_key(
            investigation_id=investigation_id,
            turn_index=turn_index,
            context_sha256=context_sha256,
            attempt=attempt,
        )
        conversation_id = make_investigation_conversation_id(investigation_id)
        async with self._uow_factory() as uow:
            state = await uow.investigation_states.get(investigation_id)
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            turn = await uow.model_conversation_turns.get_by_idempotency_key(expected_key)
            if (
                state is None
                or state.turn_index != turn_index
                or state.pending_context_sha256 != context_sha256
                or conversation is None
                or conversation.purpose is not ConversationPurpose.PIVOT_RESEARCH
                or turn is None
                or turn.conversation_id != conversation_id
            ):
                return "none"
            run = await uow.model_runs.get_for_update(turn.model_run_id)
            if run is None:
                return "needs_review"

            output_reference = turn.output_blob_reference or (
                run.output_references[0] if run.output_references else None
            )
            if turn.status is ConversationTurnStatus.SUCCEEDED:
                return "captured_output"
            if run.status is ModelRunStatus.SUCCEEDED and output_reference is not None:
                content = await self._gateway.read_output_unbounded(output_reference)
                turn.adopt_recovery_output(
                    output_blob_reference=output_reference,
                    output_sha256=hashlib.sha256(content).hexdigest(),
                    external_turn_id=None,
                    recovery_audit={"provenance": "persisted_model_run_output"},
                )
                conversation.finish_turn(turn.id, external_locator=None)
                await uow.model_conversation_turns.save(turn)
                await uow.model_conversations.save(conversation)
                await uow.commit()
                return "captured_output"
            if output_reference is not None or run.status is ModelRunStatus.SUCCEEDED:
                # A durable output reference exists but cannot be read/adopted
                # safely. Keep this checkpoint for explicit reconciliation.
                return "needs_review"
            if turn.status is ConversationTurnStatus.NEEDS_REVIEW and turn.error_code not in {
                "operator_interrupted",
                "orphaned_running_turn",
            }:
                return "needs_review"
            if run.response_id is not None:
                # A provider response identity is evidence that an answer may
                # exist remotely even when no local output was captured.
                return "needs_review"
            if turn.status not in {
                ConversationTurnStatus.RUNNING,
                ConversationTurnStatus.NEEDS_REVIEW,
            }:
                return "none"

            if run.status not in {
                ModelRunStatus.RUNNING,
                ModelRunStatus.WAITING_BACKGROUND,
                ModelRunStatus.NEEDS_REVIEW,
                ModelRunStatus.FAILED,
            }:
                return "needs_review"
            if run.submission_state is ModelSubmissionState.NOT_SUBMITTED:
                turn.mark_pre_submission_retryable(
                    model_run_id=run.id,
                    submission_attempt=run.submission_attempt,
                )
                run.mark_pre_submission_retryable()
                conversation.mark_problem(uncertain=False)
                updated_state = replace(
                    state,
                    version=state.version + 1,
                    recent_actions=(
                        *state.recent_actions,
                        "bridge_turn_pre_submission_retry:"
                        f"{turn_index}:next_attempt={attempt + 1}:turn_id={turn.id}",
                    )[-32:],
                )
                await uow.investigation_states.save_if_version(
                    updated_state, expected_version=state.version
                )
                await uow.model_conversation_turns.save(turn)
                await uow.model_runs.save(run)
                await uow.model_conversations.save(conversation)
                await uow.commit()
                return "orphaned_running_turn"
            turn.mark_orphaned()
            run.mark_orphaned_running_turn()
            conversation.mark_problem(uncertain=True)
            await uow.model_conversation_turns.save(turn)
            await uow.model_runs.save(run)
            await uow.model_conversations.save(conversation)
            await uow.commit()
            return "needs_review"

    async def seal_orphaned_investigation_turn(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        context_subject_id: UUID | None = None,
    ) -> ModelConversationTurn:
        """Seal a duplicate RUNNING Bridge turn without resubmitting it.

        This path is used when the durable investigation job was recovered
        after a stale heartbeat and re-enters the same idempotency key. The
        current worker does not infer that the prior provider result is absent;
        it moves the old attempt to the existing reconciliation path.
        """
        async with self._uow_factory() as uow:
            turn = await uow.model_conversation_turns.get(turn_id)
            if turn is None or turn.conversation_id != conversation_id:
                raise ConversationNotFoundError("Tour introuvable dans cette conversation")
            run = await uow.model_runs.get_for_update(turn.model_run_id)
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            self._ensure_visible(conversation, context_subject_id)
            locked_turn = await uow.model_conversation_turns.get(turn_id)
            if run is None or conversation is None or locked_turn is None:
                raise ModelConversationReconciliationError(
                    "conversation_reconciliation_run_not_recoverable",
                    "Le tour Bridge ne peut pas être réconcilié.",
                )
            if locked_turn.status is not ConversationTurnStatus.RUNNING:
                return locked_turn
            output_reference = locked_turn.output_blob_reference or (
                run.output_references[0] if run.output_references else None
            )
            if run.status is ModelRunStatus.SUCCEEDED and output_reference is not None:
                content = await self._gateway.read_output_unbounded(output_reference)
                locked_turn.adopt_recovery_output(
                    output_blob_reference=output_reference,
                    output_sha256=hashlib.sha256(content).hexdigest(),
                    external_turn_id=None,
                    recovery_audit={"provenance": "persisted_model_run_output"},
                )
                conversation.finish_turn(locked_turn.id, external_locator=None)
            else:
                if run.status not in {
                    ModelRunStatus.RUNNING,
                    ModelRunStatus.WAITING_BACKGROUND,
                    ModelRunStatus.NEEDS_REVIEW,
                    ModelRunStatus.FAILED,
                }:
                    raise ModelConversationReconciliationError(
                        "conversation_reconciliation_run_not_recoverable",
                        "Le tour Bridge doit être réconcilié avant toute nouvelle tentative.",
                    )
                locked_turn.mark_orphaned()
                run.mark_orphaned_running_turn()
                conversation.mark_problem(uncertain=True)
            await uow.model_conversation_turns.save(locked_turn)
            await uow.model_runs.save(run)
            await uow.model_conversations.save(conversation)
            await uow.commit()
            return locked_turn

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
        stateless_external: bool = False,
        timeout_seconds: float | None = None,
        idempotency_key: str,
        correlation_id: str,
        context_subject_id: UUID | None = None,
        lifecycle_policy: ConversationPolicy = ConversationPolicy.KEEP,
        bridge_external_effect_binding: BridgeExternalEffectBinding | None = None,
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
                    if completed_response:
                        submission_state = "post_submission"
                    elif (
                        failed_run is not None
                        and failed_run.submission_state is ModelSubmissionState.NOT_SUBMITTED
                    ):
                        submission_state = "pre_submission"
                    else:
                        submission_state = None
                    raise ConversationTurnFailedError(
                        duplicate.error_message or "Le tour précédent n'a pas abouti",
                        code=duplicate.error_code or "conversation_turn_failed",
                        status=duplicate.status,
                        submission_state=submission_state,
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
                        parent.output_sha256
                        if mode is ConversationMode.CONTINUE and parent
                        else None
                    ),
                    expected_profile=conversation.expected_profile,
                    ui_model=conversation.requested_model,
                    external_id=conversation.external_id,
                )
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
                timeout_seconds=timeout_seconds,
            )
            run = self._gateway.build_run(request, _role(conversation.purpose))
            turn = ModelConversationTurn(
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
            await uow.model_runs.add(run)
            await uow.model_conversation_turns.add(turn)
            await uow.model_conversations.save(conversation)
            await uow.commit()

        try:
            execution = await self._gateway.execute(request, _role(conversation.purpose))
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
                    external_turn_id=(
                        metadata.turn_id
                        if metadata is not None and not stateless_external
                        else None
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

            if should_close_session and self._conversation_session_closer is not None:
                try:
                    await self._conversation_session_closer.archive_conversation(conversation.id)
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
                    self._seal_interrupted_turn(
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
                    raise exc
                raise ConversationTurnFailedError(
                    str(exc),
                    code=str(getattr(exc, "code", "conversation_turn_failed")),
                    status=ConversationTurnStatus.NEEDS_REVIEW,
                ) from exc
            raise

    async def _seal_interrupted_turn(
        self, *, conversation_id: UUID, turn_id: UUID, run_id: UUID
    ) -> None:
        """Persist cancellation as terminal or explicitly reviewable state."""
        async with self._uow_factory() as uow:
            conversation = await uow.model_conversations.get_for_update(conversation_id)
            turn = await uow.model_conversation_turns.get(turn_id)
            run = await uow.model_runs.get_for_update(run_id)
            if conversation is None or turn is None or run is None:
                return
            if turn.status is not ConversationTurnStatus.RUNNING:
                return
            uncertain = run.submission_state is not ModelSubmissionState.NOT_SUBMITTED
            turn.fail(
                code="operator_interrupted",
                message="Le job propriétaire a été interrompu pendant l'appel Bridge.",
                uncertain=True,
                details={"submission_state": run.submission_state.value},
            )
            if run.status in {ModelRunStatus.RUNNING, ModelRunStatus.WAITING_BACKGROUND}:
                if uncertain:
                    run.mark_external_state_unknown()
                    run.require_review(
                        "operator_interrupted",
                        "L'appel Bridge a été interrompu ; son résultat reste incertain.",
                        details={"submission_state": run.submission_state.value},
                    )
                else:
                    run.fail(
                        "operator_interrupted",
                        "Le job a été interrompu avant la soumission au fournisseur.",
                    )
            conversation.mark_problem(uncertain=True)
            await uow.model_conversation_turns.save(turn)
            await uow.model_runs.save(run)
            await uow.model_conversations.save(conversation)
            await uow.commit()

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
                cause = ModelConversationError(
                    "Aucun mécanisme de fermeture de session bridge n'est configuré"
                )
                raise ConversationSessionCloseError(
                    conversation_id,
                    cause_code="conversation_session_closer_unconfigured",
                    retryable=False,
                    phase="conversation_archive",
                    reason=str(cause),
                ) from cause
            try:
                await self._conversation_session_closer.archive_conversation(conversation_id)
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
                raise ConversationSessionCloseError(
                    conversation_id,
                    cause_code=failure.get("cause_code"),
                    retryable=bool(failure.get("retryable", False)),
                    details=failure.get("details"),
                    phase=failure.get("phase"),
                    reason=failure.get("reason"),
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
        return (await self._blob_store.read(blob.descriptor, max_bytes=blob.descriptor.size)).decode()


def _needs_review_error(execution: ModelExecution) -> ConversationNeedsReviewError:
    """Turn a native provider needs_review into a typed reconciliation outcome."""
    run = execution.run
    details = {**(run.error_details or {}), **execution.metadata}
    reason = str(details.get("reason") or run.error_code or "no_final_answer")[:64]
    output_chars = details.get("output_chars")
    # `recovery_preview_available` is the bridge's own statement about durable
    # persistence: it is True only once the preview really reached the run
    # registry. An explicit False therefore means "the text was visible but is
    # NOT durably recoverable" and must never be upgraded here just because
    # output_chars > 0 — that would send a human to adopt a preview that does
    # not exist. Only legacy runs recorded before the flag existed fall back on
    # the character count.
    explicit = details.get("recovery_preview_available")
    recovery_available = (
        explicit
        if isinstance(explicit, bool)
        else isinstance(output_chars, int) and output_chars > 0
    )
    message = (
        "Réponse finale visible mais identité de tour externe non vérifiable ; "
        "réconciliation explicite requise."
        if reason == "external_turn_identity_unavailable"
        else (
            f"{run.error_message or 'Le modèle n a pas conclu ce tour'} "
            f"(motif bridge : {reason}) ; réconciliation explicite requise."
        )
    )
    return ConversationNeedsReviewError(
        message,
        reason=reason,
        model_run_id=run.id,
        bridge_response_id=run.response_id,
        details={
            **details,
            "reason": reason,
            "model_run_id": str(run.id),
            "bridge_response_id": run.response_id,
            "submission_state": run.submission_state.value,
            "recovery_available": recovery_available,
        },
        recovery_available=recovery_available,
    )


def _blob_id(reference: str | None) -> UUID | None:
    if not reference or not reference.startswith("blob://"):
        return None
    try:
        return UUID(reference.removeprefix("blob://"))
    except ValueError:
        return None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


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
