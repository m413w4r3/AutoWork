"""Automatic, non-replaying reconciliation of a production submission."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import ModelGateway
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_review_recovery import prepare_batch_for_recovery
from cti_app.domain.editions import EditionStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.integrations.models import BridgeTransportError


class ReconciliationOutcome(StrEnum):
    RESUMED = "resumed"
    RELEASED = "released"
    UNDECIDED = "undecided"


class ReconciliationTransport(Protocol):
    async def retrieve(self, response_id: str) -> dict[str, Any]: ...


_PENDING_STATUSES = frozenset({"queued", "running", "in_progress", "pending"})
_SUCCESS_STATUSES = frozenset({"completed", "succeeded", "success"})
_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled", "rejected"})
_NOT_FOUND_CODES = frozenset({"bridge_run_not_found", "bridge_not_found", "not_found"})


class ProductionReconciliationResolver:
    """Resolve an exact submitted bridge run without ever posting a prompt."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        transport: ReconciliationTransport | None = None,
        model_gateway: ModelGateway | None = None,
        model_conversation_service: ModelConversationService | None = None,
        diagnostics: DiagnosticsLog | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._transport = transport
        self._model_gateway = model_gateway
        self._model_conversation_service = model_conversation_service
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        self._last_bridge_run_id: str | None = None
        self._last_bridge_status: str | None = None

    async def resolve(self, run_id: UUID) -> ReconciliationOutcome:
        run = await self._get_run(run_id)
        bridge_run_id = _bridge_run_id(run) if run is not None else None
        bridge_status: str | None = None
        outcome = ReconciliationOutcome.UNDECIDED
        self._last_bridge_run_id = bridge_run_id
        self._last_bridge_status = None
        try:
            if run is None or not run.requires_reconciliation:
                return outcome
            if bridge_run_id is None or self._transport is None:
                return outcome

            try:
                payload = await self._transport.retrieve(bridge_run_id)
            except BridgeTransportError as exc:
                bridge_status = exc.bridge_status
                if bridge_status is None and exc.status_code == 404:
                    bridge_status = "not_found"
                if exc.status_code == 404 or exc.code in _NOT_FOUND_CODES:
                    outcome = await self._release(run_id, run)
                    return outcome
                if bridge_status in _FAILED_STATUSES:
                    outcome = await self._release(run_id, run)
                    return outcome
                if exc.retryable:
                    return outcome
                return outcome

            bridge_status = _status(payload)
            self._last_bridge_status = bridge_status
            if bridge_status in _SUCCESS_STATUSES:
                text = _output_text(payload)
                # A successful status without a non-empty answer is still an
                # unresolved provider result. It must not be adopted or replayed.
                if text is None:
                    return outcome
                outcome = await self._adopt_and_resume(run_id, run, payload, text)
                return outcome
            if bridge_status in _PENDING_STATUSES:
                return outcome
            if bridge_status in _FAILED_STATUSES or bridge_status is None:
                outcome = await self._release(run_id, run)
                return outcome
            # Unknown bridge states are treated as a terminal negative result by
            # the bridge contract; no output is adopted on this branch.
            outcome = await self._release(run_id, run)
            return outcome
        finally:
            self._last_bridge_status = bridge_status

    async def _get_run(self, run_id: UUID) -> SubjectProductionRun | None:
        async with self._uow_factory() as uow:
            return await uow.subject_production_runs.get(run_id)

    async def _adopt_and_resume(
        self,
        run_id: UUID,
        original: SubjectProductionRun,
        payload: dict[str, Any],
        text: str,
    ) -> ReconciliationOutcome:
        reconciliation = original.reconciliation
        if reconciliation is None or self._model_gateway is None:
            return ReconciliationOutcome.UNDECIDED
        if reconciliation.production_run_id != original.id:
            return ReconciliationOutcome.UNDECIDED

        model = await self._model_gateway.adopt_recovery_output(
            reconciliation.model_run_id,
            text.encode("utf-8"),
            provenance="automatic_bridge_retrieval",
            actor_id="system:production-reconciliation",
            external_turn_id=_verified_external_turn_id(payload),
        )
        expected_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (
            model.status is not ModelRunStatus.SUCCEEDED
            or model.raw_output_sha256 != expected_sha256
        ):
            return ReconciliationOutcome.UNDECIDED

        # ModelGateway closes a linked turn when it adopts the exact bytes. The
        # conversation service is still the authority that reconciles its
        # availability state, including test/legacy stores without that link.
        await self._reconcile_conversation(original, available=True)

        try:
            async with self._uow_factory() as uow:
                probe = await uow.subject_production_runs.get(run_id)
                if probe is None or not probe.requires_reconciliation:
                    return ReconciliationOutcome.UNDECIDED
                await self._prepare_resume_batch(uow, probe)
                run = await uow.subject_production_runs.get_for_update(run_id)
                if run is None or not run.requires_reconciliation:
                    return ReconciliationOutcome.UNDECIDED
                current = run.reconciliation
                if (
                    current is None
                    or current.model_run_id != reconciliation.model_run_id
                    or current.stage is not run.current_stage
                ):
                    return ReconciliationOutcome.UNDECIDED
                run.adopt_reconciliation_output(
                    output_sha256=expected_sha256,
                    provenance="automatic_bridge_retrieval",
                )
                run.resume_reconciled(expected_stage=current.stage)
                await uow.subject_production_runs.save(run)
                await uow.commit()
        except ValueError:
            # Batch/edition fences are not provider evidence. Keep the run
            # waiting; the next probe can safely retry this decision.
            return ReconciliationOutcome.UNDECIDED
        return ReconciliationOutcome.RESUMED

    async def _release(
        self, run_id: UUID, original: SubjectProductionRun
    ) -> ReconciliationOutcome:
        await self._reconcile_conversation(original, available=False)
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run is None or not run.requires_reconciliation:
                return ReconciliationOutcome.UNDECIDED
            if run.reconciliation is None or run.current_stage is not original.current_stage:
                return ReconciliationOutcome.UNDECIDED
            run.release_reconciliation(expected_stage=run.current_stage)
            await uow.subject_production_runs.save(run)
            await uow.commit()
        return ReconciliationOutcome.RELEASED

    async def _reconcile_conversation(
        self, run: SubjectProductionRun, *, available: bool
    ) -> None:
        if self._model_conversation_service is None:
            return
        conversation_id = _conversation_id(run)
        if conversation_id is None:
            return
        await self._model_conversation_service.reconcile(
            conversation_id,
            available=available,
            context_subject_id=run.subject_id,
        )

    @staticmethod
    async def _prepare_resume_batch(uow: Any, run: SubjectProductionRun) -> None:
        editions = getattr(uow, "editions", None)
        if editions is not None:
            get_for_update = getattr(editions, "get_for_update", None)
            edition = (
                await get_for_update(run.edition_id)
                if callable(get_for_update)
                else await editions.get(run.edition_id)
            )
            if edition is None:
                raise ValueError("edition_not_found")
            if edition.status not in {EditionStatus.PRODUCTION, EditionStatus.REVIEW}:
                raise ValueError("edition_frozen_for_publication")
        await prepare_batch_for_recovery(uow, run, reopen=True)


def _bridge_run_id(run: SubjectProductionRun) -> str | None:
    details = run.error_details if isinstance(run.error_details, dict) else {}
    nested = details.get("details")
    sources = (details, nested) if isinstance(nested, dict) else (details,)

    # A real response id is canonical when the POST response reached us. A
    # timeout may leave only bridge_request_id (`<uuid>:aN`). The bridge route
    # accepts that exact idempotency key as a lookup alias; preserve the suffix
    # instead of guessing a different provider identity.
    for source in sources:
        for key in ("bridge_run_id", "bridge_response_id", "bridge_request_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:255]
    reconciliation = run.reconciliation
    if reconciliation is not None and reconciliation.bridge_response_id:
        return reconciliation.bridge_response_id[:255]
    return None


def _status(payload: dict[str, Any]) -> str | None:
    value = payload.get("status")
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _output_text(payload: dict[str, Any]) -> str | None:
    for key in ("output_text", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _verified_external_turn_id(payload: dict[str, Any]) -> str | None:
    candidates: list[object] = [payload.get("turn_id")]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        candidates.append(metadata.get("turn_id"))
        conversation = metadata.get("conversation")
        if isinstance(conversation, dict):
            candidates.append(conversation.get("turn_id"))
    for candidate in candidates:
        if isinstance(candidate, str):
            value = candidate.strip()
            if value and len(value) <= 512:
                return value
    return None


def _conversation_id(run: SubjectProductionRun) -> UUID | None:
    if run.current_stage is SubjectProductionStage.REFERENCES:
        return run.references_conversation_id
    if run.current_stage is SubjectProductionStage.SYNTHESIS:
        return run.synthesis_conversation_id
    return None


__all__ = ["ProductionReconciliationResolver", "ReconciliationOutcome"]
