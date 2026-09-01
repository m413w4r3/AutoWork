"""Application service and read model for edition publication review."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifactStatus,
    ProductionSubmissionReconciliation,
    SubjectProductionStage,
    SubjectProductionStatus,
    requires_submission_reconciliation,
)
from cti_app.domain.publication_review import (
    PublicationDecision,
    PublicationReviewDecision,
)


@dataclass(frozen=True, slots=True)
class EditionReviewReadItem:
    """Denormalized row returned by the set-based database read model."""

    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    pipeline_generation: int
    run_status: SubjectProductionStatus
    document_artifact_id: UUID | None
    document_artifact_version: int | None
    document_input_hash: str | None
    document_artifact_status: ProductionArtifactStatus | None
    error_code: str | None
    error_message: str | None
    effective_decision: PublicationDecision | None
    effective_decision_id: UUID | None = None
    retry_stage: SubjectProductionStage | None = None
    reconciliation: ProductionSubmissionReconciliation | None = None


class EditionReviewReadRepository(Protocol):
    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionReviewReadItem]: ...


def requires_reconciliation(
    run_status: SubjectProductionStatus,
    error_code: str | None,
    reconciliation: ProductionSubmissionReconciliation | None,
) -> bool:
    """An ambiguous provider submission owns its own recovery use case.

    The exact ChatGPT answer may already exist on the provider side.  Replaying
    the stage would either duplicate that work or silently drop it, so the run
    is not retryable until the operator adopts or abandons the exact answer.

    The rule itself lives in the domain, next to the fence that refuses
    ``SubjectProductionRun.retry_from_stage``: the read model and the write
    barrier must never diverge.
    """
    return requires_submission_reconciliation(run_status, error_code, reconciliation)


def review_item_can_retry(
    run_status: SubjectProductionStatus,
    *,
    artifact_verified: bool,
    reconciliation_required: bool,
) -> bool:
    """The single Review retry policy, shared by the read model and the API.

    ``CANCELLED`` is deliberately absent: the domain refuses
    ``SubjectProductionRun.retry_from_stage`` on a cancelled run, so offering a
    retry would only produce a conflict.  A cancelled article is resolved by
    excluding it from the edition.
    """
    if reconciliation_required:
        return False
    return run_status in {
        SubjectProductionStatus.FAILED,
        SubjectProductionStatus.NEEDS_REVIEW,
    } or (run_status is SubjectProductionStatus.READY and not artifact_verified)


@dataclass(frozen=True, slots=True)
class EditionReviewItem:
    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    pipeline_generation: int
    run_status: SubjectProductionStatus
    document_artifact_id: UUID | None
    document_artifact_version: int | None
    document_input_hash: str | None
    error_code: str | None
    error_message: str | None
    effective_decision: PublicationDecision | None
    included: bool
    blocking: bool
    can_retry: bool
    effective_decision_id: UUID | None = None
    retry_stage: SubjectProductionStage | None = None
    requires_reconciliation: bool = False
    reconciliation: ProductionSubmissionReconciliation | None = None


@dataclass(frozen=True, slots=True)
class EditionReview:
    edition_id: UUID
    items: tuple[EditionReviewItem, ...]
    can_accept: bool


class EditionReviewNotFoundError(ValueError):
    pass


class EditionReviewStatusError(ValueError):
    pass


class EditionReviewItemNotFoundError(ValueError):
    pass


class ReviewItemStaleError(ValueError):
    pass


class InvalidReviewReasonError(ValueError):
    pass


class InvalidReviewDocumentError(ValueError):
    pass


class EditionReviewService:
    """Read and append review decisions without mutating production state."""

    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def get(self, edition_id: UUID) -> EditionReview:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            self._require_review(edition, edition_id)
            rows = await uow.edition_review_read_model.list_for_edition(edition_id)
        return self.from_rows(edition_id, rows)

    @staticmethod
    def from_rows(edition_id: UUID, rows: Sequence[EditionReviewReadItem]) -> EditionReview:
        """Evaluate the same review rules inside an already open transaction."""
        items = tuple(_build_item(row) for row in rows)
        return EditionReview(
            edition_id=edition_id,
            items=items,
            can_accept=bool(items)
            and any(item.included for item in items)
            and all(not item.blocking for item in items),
        )

    async def decide(
        self,
        edition_id: UUID,
        subject_id: UUID,
        *,
        decision: PublicationDecision,
        production_run_id: UUID,
        pipeline_generation: int,
        document_artifact_id: UUID | None,
        document_artifact_version: int | None,
        document_input_hash: str | None,
        actor_id: str,
        reason: str | None = None,
    ) -> PublicationReviewDecision:
        normalized_reason = reason.strip() if reason is not None else None
        if decision is PublicationDecision.EXCLUDE and not normalized_reason:
            raise InvalidReviewReasonError("exclude decisions require a non-empty reason")
        _validate_document_identity(
            document_artifact_id, document_artifact_version, document_input_hash
        )

        async with self._uow_factory() as uow:
            edition = await _get_edition_for_update(uow, edition_id)
            self._require_review(edition, edition_id)
            rows = await uow.edition_review_read_model.list_for_edition(edition_id)
            row = next(
                (candidate for candidate in rows if candidate.subject_id == subject_id),
                None,
            )
            if row is None:
                raise EditionReviewItemNotFoundError(str(subject_id))

            # The run lock serializes this comparison with the existing retry
            # use case, which changes generation and stales its artifacts.
            runs_repository = uow.subject_production_runs
            get_run_for_update = getattr(runs_repository, "get_for_update", None)
            run = (
                await get_run_for_update(row.run_id)
                if get_run_for_update is not None
                else await runs_repository.get(row.run_id)
            )
            artifact = await current_publication_artifact(uow.production_artifacts, row.run_id)
            if (
                run is None
                or row.run_id != production_run_id
                or run.edition_id != edition_id
                or run.subject_id != subject_id
                or run.pipeline_generation != pipeline_generation
            ):
                raise ReviewItemStaleError("review item does not refer to the current document")
            if artifact is None:
                if (
                    decision is not PublicationDecision.EXCLUDE
                    or run.status
                    not in {
                        SubjectProductionStatus.FAILED,
                        SubjectProductionStatus.NEEDS_REVIEW,
                        SubjectProductionStatus.CANCELLED,
                    }
                    or any(
                        value is not None
                        for value in (
                            document_artifact_id,
                            document_artifact_version,
                            document_input_hash,
                        )
                    )
                ):
                    raise ReviewItemStaleError("review item does not refer to the current document")
            elif (
                artifact.id != document_artifact_id
                or artifact.version != document_artifact_version
                or artifact.input_hash != document_input_hash
            ):
                raise ReviewItemStaleError("review item does not refer to the current document")

            event = PublicationReviewDecision(
                edition_id=edition_id,
                subject_id=subject_id,
                production_run_id=production_run_id,
                pipeline_generation=pipeline_generation,
                document_artifact_id=document_artifact_id,
                document_artifact_version=document_artifact_version,
                document_input_hash=document_input_hash,
                decision=decision,
                actor_id=actor_id,
                reason=normalized_reason,
            )
            await uow.publication_review_decisions.append(event)
            await uow.commit()
            return event

    @staticmethod
    def _require_review(edition: object, edition_id: UUID) -> None:
        if edition is None:
            raise EditionReviewNotFoundError(str(edition_id))
        if getattr(edition, "status", None) is not EditionStatus.REVIEW:
            raise EditionReviewStatusError("edition_must_be_in_review")


async def _get_edition_for_update(uow: object, edition_id: UUID) -> object:
    repository = uow.editions  # type: ignore[attr-defined]
    get_for_update = getattr(repository, "get_for_update", None)
    if get_for_update is not None:
        return await get_for_update(edition_id)
    return await repository.get(edition_id)


def _build_item(row: EditionReviewReadItem) -> EditionReviewItem:
    artifact_verified = (
        row.document_artifact_id is not None
        and row.document_artifact_status is ProductionArtifactStatus.VERIFIED
    )
    decision = row.effective_decision
    if decision is None and row.run_status is SubjectProductionStatus.READY:
        decision = PublicationDecision.INCLUDE

    if row.run_status is SubjectProductionStatus.READY:
        blocking = decision is not PublicationDecision.EXCLUDE and not artifact_verified
    elif row.run_status in {
        SubjectProductionStatus.FAILED,
        SubjectProductionStatus.NEEDS_REVIEW,
        SubjectProductionStatus.CANCELLED,
    }:
        blocking = decision is not PublicationDecision.EXCLUDE
    else:
        # QUEUED/RUNNING are never made publishable by a review command.
        blocking = True

    included = decision is PublicationDecision.INCLUDE and artifact_verified
    reconciliation_required = requires_reconciliation(
        row.run_status, row.error_code, row.reconciliation
    )
    can_retry = review_item_can_retry(
        row.run_status,
        artifact_verified=artifact_verified,
        reconciliation_required=reconciliation_required,
    )
    retry_stage = row.retry_stage if can_retry else None
    return EditionReviewItem(
        position=row.position,
        subject_id=row.subject_id,
        title=row.title,
        run_id=row.run_id,
        pipeline_generation=row.pipeline_generation,
        run_status=row.run_status,
        document_artifact_id=row.document_artifact_id,
        document_artifact_version=row.document_artifact_version,
        document_input_hash=row.document_input_hash,
        error_code=row.error_code,
        error_message=row.error_message,
        effective_decision=decision,
        included=included,
        blocking=blocking,
        can_retry=can_retry,
        effective_decision_id=row.effective_decision_id,
        retry_stage=retry_stage,
        requires_reconciliation=reconciliation_required,
        reconciliation=row.reconciliation if reconciliation_required else None,
    )


def _validate_document_identity(
    artifact_id: UUID | None,
    artifact_version: int | None,
    input_hash: str | None,
) -> None:
    values = (artifact_id, artifact_version, input_hash)
    if any(value is None for value in values) and any(value is not None for value in values):
        raise InvalidReviewDocumentError("document identity must be complete or empty")


__all__ = [
    "EditionReview",
    "EditionReviewItem",
    "EditionReviewItemNotFoundError",
    "EditionReviewNotFoundError",
    "EditionReviewReadItem",
    "EditionReviewReadRepository",
    "EditionReviewService",
    "EditionReviewStatusError",
    "InvalidReviewDocumentError",
    "InvalidReviewReasonError",
    "ReviewItemStaleError",
    "requires_reconciliation",
    "review_item_can_retry",
]
