"""Explicit, non-resubmitting recovery of an ambiguous production submission."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.application.model_gateway import ModelGateway, ModelGatewayError
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_jobs import (
    PRODUCTION_RECONCILIATION_RESUME_MAX_ATTEMPTS,
    ProductionReconciliationResumeParameters,
    production_reconciliation_resume_idempotency_key,
    production_reconciliation_resume_job_kind,
)
from cti_app.application.production_review_recovery import (
    ACTIVE_SIBLING,
    BATCH_CANCELLED,
    BATCH_MISSING,
    BATCH_SUPERSEDED,
    ReviewRecoveryConflictError,
    prepare_batch_for_recovery,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStatus,
)

MAX_RECOVERY_BYTES = 10_000_000

_RECOVERY_CONFLICTS = {
    BATCH_MISSING: (
        "production_reconciliation_batch_missing",
        "Le lot de production est introuvable.",
    ),
    BATCH_CANCELLED: (
        "production_reconciliation_batch_cancelled",
        "Un lot de production annulé ne peut pas être repris.",
    ),
    BATCH_SUPERSEDED: (
        "production_reconciliation_batch_superseded",
        "Un lot de production plus récent est actif ; cette reprise est obsolète.",
    ),
    ACTIVE_SIBLING: (
        "production_reconciliation_active_sibling",
        "Un autre article du lot est actif ; la reprise est mise en attente.",
    ),
}


class ProductionReconciliationError(ValueError):
    """Safe, typed operator-facing reconciliation error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class VisibleRecoveryTransport(Protocol):
    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]: ...

    async def release_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProductionRecoveryPreview:
    production_run_id: UUID
    model_run_id: UUID
    stage: str
    pipeline_generation: int
    bridge_response_id: str | None
    submission_state: str
    phase: str
    text: str
    sha256: str
    chars: int
    metadata: dict[str, Any]
    visible_available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "production_run_id": str(self.production_run_id),
            "model_run_id": str(self.model_run_id),
            "stage": self.stage,
            "pipeline_generation": self.pipeline_generation,
            "bridge_response_id": self.bridge_response_id,
            "submission_state": self.submission_state,
            "phase": self.phase,
            "text": self.text,
            "sha256": self.sha256,
            "chars": self.chars,
            "metadata": self.metadata,
            "visible_available": self.visible_available,
        }


class ProductionReconciliationService:
    """Application service for preview, adoption, resume and target release."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        model_gateway: ModelGateway,
        jobs: JobService,
        dispatcher: JobDispatcher,
        bridge: VisibleRecoveryTransport | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_gateway = model_gateway
        self._jobs = jobs
        self._dispatcher = dispatcher
        self._bridge = bridge

    async def preview_visible(self, run_id: UUID) -> ProductionRecoveryPreview:
        run, reconciliation, model = await self._load_review(run_id, allow_adopted=False)
        if not reconciliation.bridge_response_id:
            raise ProductionReconciliationError(
                "production_reconciliation_visible_unavailable",
                "La cible ChatGPT exacte n'est plus disponible ; utilisez l'import Markdown.",
            )
        if model.response_id != reconciliation.bridge_response_id:
            raise ProductionReconciliationError(
                "production_reconciliation_identity_mismatch",
                "La réponse du bridge ne correspond pas au ModelRun persistant.",
            )
        if self._bridge is None:
            raise ProductionReconciliationError(
                "production_reconciliation_visible_unavailable",
                "Le bridge ChatGPT n'est pas disponible ; utilisez l'import Markdown.",
            )
        try:
            payload = await self._bridge.preview_visible_recovery(reconciliation.bridge_response_id)
        except Exception as exc:
            raise ProductionReconciliationError(
                "production_reconciliation_visible_unavailable",
                "La cible ChatGPT exacte n'est pas récupérable ; utilisez l'import Markdown.",
            ) from exc
        if payload.get("bridge_run_id") != reconciliation.bridge_response_id:
            raise ProductionReconciliationError(
                "production_reconciliation_identity_mismatch",
                "Le bridge a renvoyé une autre réponse que celle du ModelRun.",
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ProductionReconciliationError(
                "production_reconciliation_empty_response",
                "La réponse ChatGPT récupérée est vide.",
            )
        content = text.encode("utf-8")
        if len(content) > MAX_RECOVERY_BYTES:
            raise ProductionReconciliationError(
                "production_reconciliation_response_too_large",
                "La réponse ChatGPT récupérée dépasse la taille autorisée.",
            )
        return self._preview(
            run,
            reconciliation,
            text,
            visible_available=True,
            metadata=_bounded_metadata(payload.get("metadata")),
        )

    async def preview_manual(self, run_id: UUID, markdown: str) -> ProductionRecoveryPreview:
        run, reconciliation, _ = await self._load_review(run_id, allow_adopted=False)
        self._validate_content(markdown)
        return self._preview(
            run,
            reconciliation,
            markdown,
            visible_available=bool(reconciliation.bridge_response_id),
            metadata={"source": "manual_import"},
        )

    async def adopt_visible(
        self, run_id: UUID, expected_sha256: str, *, actor_id: str
    ) -> dict[str, Any]:
        run, reconciliation, model = await self._load_review(run_id)
        self._validate_expected_hash(expected_sha256)
        self._ensure_resume_safety(run)
        await self._ensure_resume_context(run)
        already_adopted = self._is_adopted(model, expected_sha256, "visible_recovery")
        if already_adopted:
            await self._read_adopted_text(model)
        else:
            preview = await self.preview_visible(run_id)
            self._ensure_hash(preview.sha256, expected_sha256)
            text = preview.text
            try:
                model = await self._model_gateway.adopt_recovery_output(
                    reconciliation.model_run_id,
                    text.encode("utf-8"),
                    provenance="visible_recovery",
                    actor_id=actor_id,
                )
            except ModelGatewayError as exc:
                raise ProductionReconciliationError(
                    "production_reconciliation_adoption_conflict",
                    "Le ModelRun exact ne peut pas être adopté dans cet état.",
                ) from exc
        return await self._adopt_and_resume(
            run_id,
            reconciliation,
            model,
            expected_sha256,
            "visible_recovery",
            actor_id=actor_id,
            release_visible=True,
        )

    async def adopt_manual(
        self,
        run_id: UUID,
        markdown: str,
        expected_sha256: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        run, reconciliation, model = await self._load_review(run_id)
        self._validate_content(markdown)
        self._validate_expected_hash(expected_sha256)
        self._ensure_hash(_sha256(markdown), expected_sha256)
        self._ensure_resume_safety(run)
        await self._ensure_resume_context(run)
        if not self._is_adopted(model, expected_sha256, "manual_import"):
            try:
                model = await self._model_gateway.adopt_recovery_output(
                    reconciliation.model_run_id,
                    markdown.encode("utf-8"),
                    provenance="manual_import",
                    actor_id=actor_id,
                )
            except ModelGatewayError as exc:
                raise ProductionReconciliationError(
                    "production_reconciliation_adoption_conflict",
                    "Le ModelRun exact ne peut pas être adopté dans cet état.",
                ) from exc
        return await self._adopt_and_resume(
            run_id,
            reconciliation,
            model,
            expected_sha256,
            "manual_import",
            actor_id=actor_id,
            release_visible=True,
        )

    async def abandon_visible(self, run_id: UUID) -> dict[str, Any]:
        _, reconciliation, _ = await self._load_review(run_id)
        if not reconciliation.bridge_response_id:
            raise ProductionReconciliationError(
                "production_reconciliation_visible_unavailable",
                "La cible ChatGPT exacte n'est plus disponible.",
            )
        await self._release(reconciliation.bridge_response_id)
        return {"action": "production_reconciliation_abandoned", "run_id": str(run_id)}

    async def _adopt_and_resume(
        self,
        run_id: UUID,
        reconciliation: ProductionSubmissionReconciliation,
        model: Any,
        expected_sha256: str,
        provenance: str,
        *,
        actor_id: str,
        release_visible: bool,
    ) -> dict[str, Any]:
        del actor_id
        if not self._is_adopted(model, expected_sha256, provenance):
            raise ProductionReconciliationError(
                "production_reconciliation_adoption_not_persisted",
                "La récupération du ModelRun n'a pas été confirmée.",
            )
        job_id, already_resumed = await self._resume_and_schedule(
            run_id, reconciliation, expected_sha256, provenance
        )
        released = False
        if release_visible and reconciliation.bridge_response_id:
            released = await self._release(reconciliation.bridge_response_id)
        return {
            "action": "production_reconciliation_adopted",
            "run_id": str(run_id),
            "model_run_id": str(reconciliation.model_run_id),
            "stage": reconciliation.stage.value,
            "pipeline_generation": await self._generation(run_id),
            "job_id": str(job_id) if job_id else None,
            "already_resumed": already_resumed,
            "provenance": provenance,
            "released": released,
            "sha256": expected_sha256,
        }

    async def _resume_and_schedule(
        self,
        run_id: UUID,
        reconciliation: ProductionSubmissionReconciliation,
        expected_sha256: str,
        provenance: str,
    ) -> tuple[UUID | None, bool]:
        async with self._uow_factory() as uow:
            probe = await uow.subject_production_runs.get(run_id)
            if probe is None:
                raise ProductionReconciliationError(
                    "production_run_not_found", "Le run de production est introuvable."
                )
            # Lock in the same order as ordinary production retry/cancellation
            # and as the batch hand-off: edition, then batch, then run, so
            # reconciliation cannot race a publication freeze, a cancellation
            # or a batch transition.
            edition = await uow.editions.get_for_update(probe.edition_id)
            if edition is None or edition.id != probe.edition_id:
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "Le run de production n'est plus associé à la même édition.",
                )
            self._ensure_edition_safety(edition.status)
            await self._ensure_no_publication_freeze(uow, edition.id)
            await self._ensure_batch_safety(uow, probe, reopen=True)
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run is None:
                raise ProductionReconciliationError(
                    "production_run_not_found", "Le run de production est introuvable."
                )
            if edition.id != run.edition_id:
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "Le run de production n'est plus associé à la même édition.",
                )
            current = run.reconciliation
            if current is None or current.model_run_id != reconciliation.model_run_id:
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "L'identité de réconciliation du run a changé.",
                )
            adopted = await uow.model_runs.get(current.model_run_id)
            if (
                adopted is None
                or adopted.status is not ModelRunStatus.SUCCEEDED
                or adopted.raw_output_sha256 != expected_sha256
            ):
                raise ProductionReconciliationError(
                    "production_reconciliation_output_missing",
                    "La sortie du ModelRun exact n'est pas archivée comme réussie.",
                )
            if current.stage is not reconciliation.stage or run.current_stage is not current.stage:
                if run.status is SubjectProductionStatus.RUNNING:
                    return None, True
                raise ProductionReconciliationError(
                    "production_reconciliation_stage_changed",
                    "L'étape à reprendre ne correspond plus au run de production.",
                )
            if current.output_sha256 not in (None, expected_sha256):
                raise ProductionReconciliationError(
                    "production_reconciliation_hash_mismatch",
                    "Le hash de la réponse adoptée ne correspond pas au run.",
                )
            if current.output_sha256 is None:
                run.adopt_reconciliation_output(
                    output_sha256=expected_sha256, provenance=provenance
                )
            elif current.provenance != provenance:
                raise ProductionReconciliationError(
                    "production_reconciliation_provenance_mismatch",
                    "Cette réponse a déjà été adoptée avec une autre provenance.",
                )
            if run.status is SubjectProductionStatus.NEEDS_REVIEW:
                if run.error_code != PRODUCTION_RECONCILIATION_ERROR_CODE:
                    raise ProductionReconciliationError(
                        "production_reconciliation_error_changed",
                        "Le motif de revue du run a changé.",
                    )
                run.resume_reconciled(expected_stage=current.stage)
                await uow.subject_production_runs.save(run)
                await uow.commit()
                generation = run.pipeline_generation
            elif run.status is SubjectProductionStatus.RUNNING and run.error_code is None:
                generation = run.pipeline_generation
                if current.output_sha256 is None:
                    await uow.subject_production_runs.save(run)
                # A repeated adoption may still be the call that reopens the
                # batch, so this path commits even when the run is unchanged.
                await uow.commit()
            else:
                return None, True

        parameters = ProductionReconciliationResumeParameters(
            run_id=run_id,
            expected_stage=reconciliation.stage.value,
            pipeline_generation=generation,
            reconciliation_model_run_id=reconciliation.model_run_id,
            reconciled_output_sha256=expected_sha256,
        )
        key = production_reconciliation_resume_idempotency_key(
            run_id,
            reconciliation.stage,
            generation,
            reconciliation.model_run_id,
            expected_sha256,
        )
        try:
            job = await self._jobs.submit(
                kind=production_reconciliation_resume_job_kind(),
                aggregate_type="subject",
                aggregate_id=run.subject_id,
                idempotency_key=key,
                correlation_id="production-reconciliation",
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=PRODUCTION_RECONCILIATION_RESUME_MAX_ATTEMPTS,
            )
        except DuplicateJobError as exc:
            job = await self._jobs.get(exc.existing_job_id)
        await self._dispatcher.dispatch(job.id)
        return job.id, False

    async def _load_review(
        self, run_id: UUID, *, allow_adopted: bool = True
    ) -> tuple[SubjectProductionRun, ProductionSubmissionReconciliation, Any]:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            if run is None:
                raise ProductionReconciliationError(
                    "production_run_not_found", "Le run de production est introuvable."
                )
            adopted = (
                run.reconciliation is not None
                and run.reconciliation.output_sha256 is not None
                and run.reconciliation.provenance in {"manual_import", "visible_recovery"}
                and run.status in {SubjectProductionStatus.RUNNING, SubjectProductionStatus.READY}
                and run.error_code is None
            )
            reviewable = (
                run.status is SubjectProductionStatus.NEEDS_REVIEW
                and run.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
            )
            if (
                not reviewable and (not allow_adopted or not adopted)
            ) or run.reconciliation is None:
                raise ProductionReconciliationError(
                    "production_reconciliation_not_eligible",
                    "Ce run n'est pas dans l'état de réconciliation attendu.",
                )
            if run.reconciliation.production_run_id != run.id or (
                reviewable and run.reconciliation.stage is not run.current_stage
            ):
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "L'identité persistée ne correspond pas au run ou à l'étape courante.",
                )
            model = await uow.model_runs.get(run.reconciliation.model_run_id)
            if model is None:
                raise ProductionReconciliationError(
                    "production_reconciliation_model_run_missing",
                    "Le ModelRun exact de la réconciliation est introuvable.",
                )
            if reviewable and (
                model.status is not ModelRunStatus.NEEDS_REVIEW
                or model.error_code != PRODUCTION_RECONCILIATION_ERROR_CODE
            ):
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "Le ModelRun persisté n'est plus le ModelRun en échec attendu.",
                )
            return run, run.reconciliation, model

    async def _ensure_batch_safety(
        self, uow: Any, run: SubjectProductionRun, *, reopen: bool = False
    ) -> None:
        """Share the Review recovery rule with the ordinary business retry.

        With ``reopen``, the batch that already finished with issues is put
        back into an explicit review-recovery pass so the chained stages of
        this article can be dispatched again.
        """
        try:
            await prepare_batch_for_recovery(uow, run, reopen=reopen)
        except ReviewRecoveryConflictError as exc:
            code, message = _RECOVERY_CONFLICTS[exc.reason]
            raise ProductionReconciliationError(code, message) from exc

    async def _ensure_resume_context(self, run: SubjectProductionRun) -> None:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(run.edition_id)
            if edition is None:
                raise ProductionReconciliationError(
                    "production_reconciliation_identity_mismatch",
                    "L'édition du run de production est introuvable.",
                )
            self._ensure_edition_safety(edition.status)
            await self._ensure_no_publication_freeze(uow, edition.id)
            await self._ensure_batch_safety(uow, run)

    @staticmethod
    def _ensure_edition_safety(status: EditionStatus) -> None:
        if status is EditionStatus.SELECTION:
            raise ProductionReconciliationError(
                "production_reconciliation_edition_selection",
                "L'édition est revenue à la sélection ; le run ne peut pas être repris.",
            )
        if status not in {EditionStatus.PRODUCTION, EditionStatus.REVIEW}:
            raise ProductionReconciliationError(
                "production_reconciliation_publication_frozen",
                "La publication de l'édition est gelée ; le run ne peut pas être repris.",
            )

    @staticmethod
    async def _ensure_no_publication_freeze(uow: Any, edition_id: UUID) -> None:
        manifests = getattr(uow, "publication_manifests", None)
        if manifests is not None and await manifests.get_latest_for_edition(edition_id) is not None:
            raise ProductionReconciliationError(
                "production_reconciliation_publication_frozen",
                "La publication de l'édition est gelée ; le run ne peut pas être repris.",
            )

    @staticmethod
    def _ensure_resume_safety(run: SubjectProductionRun) -> None:
        already_adopted = (
            run.status in {SubjectProductionStatus.RUNNING, SubjectProductionStatus.READY}
            and run.error_code is None
            and run.reconciliation is not None
            and run.reconciliation.output_sha256 is not None
        )
        if run.status is not SubjectProductionStatus.NEEDS_REVIEW and not already_adopted:
            raise ProductionReconciliationError(
                "production_reconciliation_not_eligible",
                "Ce run n'est plus en attente de réconciliation.",
            )

    @staticmethod
    def _validate_content(text: str) -> None:
        if not isinstance(text, str) or not text:
            raise ProductionReconciliationError(
                "production_reconciliation_empty_response", "La réponse à adopter est vide."
            )
        if len(text.encode("utf-8")) > MAX_RECOVERY_BYTES:
            raise ProductionReconciliationError(
                "production_reconciliation_response_too_large",
                "La réponse à adopter dépasse la taille autorisée.",
            )

    @staticmethod
    def _validate_expected_hash(expected: str) -> None:
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ProductionReconciliationError(
                "production_reconciliation_invalid_hash", "Le hash attendu est invalide."
            )

    @staticmethod
    def _ensure_hash(actual: str, expected: str) -> None:
        if actual != expected:
            raise ProductionReconciliationError(
                "production_reconciliation_hash_mismatch",
                "Le hash de la réponse ne correspond pas à la confirmation opérateur.",
            )

    @staticmethod
    def _is_adopted(model: Any, expected: str, provenance: str) -> bool:
        recovery = (model.error_details or {}).get("recovery")
        return (
            model.status is ModelRunStatus.SUCCEEDED
            and model.raw_output_sha256 == expected
            and isinstance(recovery, dict)
            and recovery.get("provenance") == provenance
        )

    async def _read_adopted_text(self, model: Any) -> str:
        reference = model.raw_output_reference
        if not reference:
            raise ProductionReconciliationError(
                "production_reconciliation_output_missing",
                "La réponse adoptée n'a pas de contenu archivé.",
            )
        try:
            content = await self._model_gateway.read_output(reference, max_bytes=MAX_RECOVERY_BYTES)
        except Exception as exc:
            raise ProductionReconciliationError(
                "production_reconciliation_output_missing",
                "La réponse adoptée n'a pas pu être relue.",
            ) from exc
        text = content.decode("utf-8")
        self._ensure_hash(_sha256(text), model.raw_output_sha256 or "")
        return text

    async def _release(self, bridge_run_id: str) -> bool:
        if self._bridge is None:
            return False
        try:
            await self._bridge.release_visible_recovery(bridge_run_id)
            return True
        except Exception:
            return False

    async def _generation(self, run_id: UUID) -> int:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            return run.pipeline_generation if run else 0

    @staticmethod
    def _preview(
        run: SubjectProductionRun,
        reconciliation: ProductionSubmissionReconciliation,
        text: str,
        *,
        visible_available: bool,
        metadata: dict[str, Any],
    ) -> ProductionRecoveryPreview:
        return ProductionRecoveryPreview(
            production_run_id=run.id,
            model_run_id=reconciliation.model_run_id,
            stage=reconciliation.stage.value,
            pipeline_generation=run.pipeline_generation,
            bridge_response_id=reconciliation.bridge_response_id,
            submission_state=reconciliation.submission_state.value,
            phase=reconciliation.phase,
            text=text,
            sha256=_sha256(text),
            chars=len(text),
            metadata=metadata,
            visible_available=visible_available,
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    def clean(item: object, depth: int = 0) -> object:
        if depth > 2:
            return None
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in list(item.items())[:30]:
                cleaned = clean(child, depth + 1)
                if cleaned is not None:
                    result[str(key)[:64]] = cleaned
            return result
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item[:30]]
        if isinstance(item, (str, int, float, bool)):
            return item[:256] if isinstance(item, str) else item
        return None

    result = clean(value)
    return result if isinstance(result, dict) else {}


__all__ = [
    "MAX_RECOVERY_BYTES",
    "ProductionReconciliationError",
    "ProductionReconciliationService",
    "ProductionRecoveryPreview",
]
