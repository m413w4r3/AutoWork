"""Application service and read model for edition publication review."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.application.production_repairs import (
    repair_issue_application_state,
    repair_issue_blocks_signoff,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifactStatus,
    ProductionRepairIssueKind,
    ProductionSubmissionReconciliation,
    RepairDecisionApplicationState,
    SubjectProductionStage,
    SubjectProductionStatus,
    SupplementalSourceRepairState,
    requires_submission_reconciliation,
)
from cti_app.domain.publication_review import (
    PublicationDecision,
    PublicationReviewDecision,
)

#: Statuses whose review and Repair Desk stay readable. Reading is separate
#: from writing: an edition frozen for publication keeps its audit trail
#: visible while every mutation policy keeps refusing it.
READABLE_REVIEW_STATUSES = frozenset(
    {
        EditionStatus.PRODUCTION.value,
        EditionStatus.REVIEW.value,
        EditionStatus.ASSEMBLING.value,
        EditionStatus.PUBLISHED.value,
        EditionStatus.ARCHIVED.value,
    }
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
    rejected_indicator_count: int = 0
    rejected_ioc_count: int = 0
    rejected_other_artifact_count: int = 0
    rejected_rule_count: int = 0
    published_rule_count: int = 0


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
    rejected_indicator_count: int = 0
    rejected_ioc_count: int = 0
    rejected_other_artifact_count: int = 0
    rejected_rule_count: int = 0
    published_rule_count: int = 0
    active_repair_count: int = 0
    unresolved_repair_count: int = 0
    pending_rebuild_count: int = 0


@dataclass(frozen=True, slots=True)
class EditionReview:
    edition_id: UUID
    items: tuple[EditionReviewItem, ...]
    can_accept: bool
    unresolved_repair_count: int = 0
    repair_review_complete: bool = True
    # Repairs whose content exists but whose article was not rebuilt yet.
    # Sign-off is refused while this is non-zero, so no manifest can be frozen
    # between a manual archive and the REFERENCES version that reintegrates it.
    pending_rebuild_count: int = 0


@dataclass(frozen=True, slots=True)
class EditionRepairItem:
    """Cross-subject, bounded Repair Desk representation."""

    repair_key: str
    kind: str
    position: int
    subject_id: UUID
    article_title: str
    run_id: UUID
    pipeline_generation: int
    artifact_id: UUID | None
    artifact_version: int | None
    source_id: str | None
    source_title: str | None
    source_url: str | None
    collection_id: UUID | None
    collection_state: str | None
    artifact_type: str | None
    preview: str
    reason_code: str
    value_sha256: str
    payload_available: bool
    effective_action: str | None
    effective_decision_id: UUID | None
    resolved: bool
    resolution_reason: str | None
    rebuild_required: bool
    recommended_stage: str | None
    # Only carried by supplemental-source issues; ``None`` for Q2 rejections.
    repair_state: str | None = None
    is_publication_ioc: bool = False
    # False when the analyst excluded this article from the deliverable: the
    # issue stays visible and arbitrable, but it is not a loss for the
    # publication scope and must not gate the edition.
    in_publication_scope: bool = True
    # What the current projection really materializes, so an audit never reads
    # "the analyst decided INCLUDE" as "the deliverable contains the value".
    application_state: str = RepairDecisionApplicationState.UNRESOLVED.value


@dataclass(frozen=True, slots=True)
class EditionRepairSummary:
    unresolved_total: int
    sources_to_supply: int
    rejected_iocs_to_review: int
    rejected_rules_to_review: int
    rejected_other_artifacts: int
    articles_with_repairs: int
    articles_needing_rebuild: int


@dataclass(frozen=True, slots=True)
class EditionRepairArticle:
    subject_id: UUID
    has_pending_projection: bool
    recommended_stage: str
    active_repair_count: int
    resolved_since_last_build_count: int


@dataclass(frozen=True, slots=True)
class EditionRepairPage:
    summary: EditionRepairSummary
    items: tuple[EditionRepairItem, ...]
    articles: tuple[EditionRepairArticle, ...]
    next_cursor: str | None


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


class ProductionRepairIssueReader(Protocol):
    async def list_issue_views(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> Sequence[Any]: ...

    async def list_supplemental_source_issues(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> Sequence[Any]: ...


class EditionRepairReadService:
    """Aggregate current ProductionRepairService issues in edition order."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        issue_reader: ProductionRepairIssueReader,
    ) -> None:
        self._uow_factory = uow_factory
        self._issue_reader = issue_reader

    async def list(
        self,
        edition_id: UUID,
        *,
        status: str = "open",
        kind: ProductionRepairIssueKind | None = None,
        subject_id: UUID | None = None,
        artifact_type: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> EditionRepairPage:
        if status not in {"open", "resolved", "all"}:
            raise ValueError("invalid_repair_status")
        if limit < 1:
            raise ValueError("invalid_repair_limit")

        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            EditionReviewService._require_readable(edition, edition_id)
            rows = await uow.edition_review_read_model.list_for_edition(edition_id)

        rows_by_subject = {row.subject_id: row for row in rows}
        rows_by_run = {row.run_id: row for row in rows}
        issue_views = await self._issue_views(edition_id, subject_id)
        records: list[EditionRepairItem] = []
        for issue in issue_views:
            issue_subject = _issue_subject(issue)
            row = rows_by_subject.get(issue_subject) if issue_subject is not None else None
            if row is None:
                issue_run_id = getattr(issue, "production_run_id", None)
                row = rows_by_run.get(issue_run_id) if isinstance(issue_run_id, UUID) else None
            item = self._item_from_issue(issue, row)
            if item is not None:
                records.append(item)
        records.sort(key=lambda item: (item.position, item.repair_key))

        scoped = [
            item
            for item in records
            if (kind is None or item.kind == kind.value)
            and (subject_id is None or item.subject_id == subject_id)
            and (artifact_type is None or item.artifact_type == artifact_type)
        ]
        page_records = [
            item
            for item in scoped
            if status == "all"
            or (status == "open" and not item.resolved)
            or (status == "resolved" and item.resolved)
        ]
        start = _repair_cursor_position(cursor, page_records)
        page = tuple(page_records[start : start + limit])
        next_cursor = (
            _repair_cursor_encode(page[-1].position, page[-1].repair_key)
            if start + len(page) < len(page_records) and page
            else None
        )
        return EditionRepairPage(
            summary=_repair_summary(scoped),
            items=page,
            articles=_repair_articles(scoped),
            next_cursor=next_cursor,
        )

    async def _issue_views(self, edition_id: UUID, subject_id: UUID | None) -> tuple[Any, ...]:
        getter = getattr(self._issue_reader, "list_issue_views", None)
        if callable(getter):
            extraction = await getter(edition_id, subject_id)
        else:
            extraction = await self._issue_reader.list_issues(edition_id, subject_id)  # type: ignore[attr-defined]
        supplemental_getter = getattr(self._issue_reader, "list_supplemental_source_issues", None)
        supplemental = (
            await supplemental_getter(edition_id, subject_id)
            if callable(supplemental_getter)
            else ()
        )
        return tuple([*extraction, *supplemental])

    @staticmethod
    def _item_from_issue(issue: Any, row: EditionReviewReadItem | None) -> EditionRepairItem | None:
        if row is None:
            return None
        kind = _issue_kind(issue)
        decision = getattr(issue, "effective_decision", None)
        is_source = kind == ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value
        pending_references = _issue_pending_references(issue)
        # An archived source no longer needs an arbitration: what it owes the
        # edition is a REFERENCES reconciliation, tracked as a rebuild debt.
        resolved = decision is not None or pending_references
        is_ioc = bool(getattr(issue, "is_publication_ioc", False))
        repair_key = str(issue.repair_key)
        source_id = getattr(issue, "source_id", None)
        source_title = getattr(issue, "source_title", None)
        source_url = getattr(issue, "source_url", None)
        collection_id = getattr(issue, "collection_id", None)
        collection_state = getattr(issue, "collection_state", None)
        artifact_type = getattr(issue, "artifact_type", None)
        artifact_id = getattr(issue, "observed_artifact_id", None)
        artifact_version = getattr(issue, "observed_artifact_version", None)
        state = issue_application_state(issue, decision)
        if is_source and pending_references:
            # The content exists; only the deterministic REFERENCES rebuild is
            # missing. This debt is served by the backend, so a page reload
            # cannot lose it.
            rebuild_required = repair_issue_blocks_signoff(issue)
            recommended_stage = "rebuild_references"
        elif is_source:
            rebuild_required = False
            # The source still needs an explicit archive/waive decision before
            # the deterministic REFERENCES reconciliation can run.
            recommended_stage = "none" if resolved else "rebuild_references"
        elif state is RepairDecisionApplicationState.UNRESOLVED:
            rebuild_required = False
            recommended_stage = None
        elif state is RepairDecisionApplicationState.UNBUILDABLE:
            # The decision is recorded but nothing materialized it. It stays a
            # blocking debt the analyst clears by revising it to EXCLUDE.
            rebuild_required = repair_issue_blocks_signoff(issue)
            recommended_stage = "revise_decision"
        elif state is RepairDecisionApplicationState.PROJECTION_REQUIRED:
            # Covers the first INCLUDE as well as every revision that makes the
            # applied projection disagree with the effective decision.
            rebuild_required = repair_issue_blocks_signoff(issue)
            recommended_stage = "apply_projection"
        else:
            rebuild_required = row.document_artifact_id is None
            recommended_stage = "synthesis" if rebuild_required else "none"

        return EditionRepairItem(
            repair_key=repair_key,
            kind=kind,
            position=row.position,
            subject_id=row.subject_id,
            article_title=row.title,
            run_id=row.run_id,
            pipeline_generation=row.pipeline_generation,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            source_id=(str(source_id) if source_id else None),
            source_title=(str(source_title) if source_title else None),
            source_url=(str(source_url) if source_url else None),
            collection_id=collection_id,
            collection_state=(str(collection_state) if collection_state is not None else None),
            artifact_type=(str(artifact_type) if artifact_type is not None else None),
            preview=str(getattr(issue, "preview", "")),
            reason_code=str(
                getattr(issue, "reason_code", None)
                or getattr(issue, "error_reason", None)
                or "supplemental_source_unarchived"
            ),
            value_sha256=str(getattr(issue, "value_sha256", "")),
            payload_available=bool(getattr(issue, "payload_available", False)),
            effective_action=(
                getattr(getattr(decision, "action", None), "value", None)
                if decision is not None
                else None
            ),
            effective_decision_id=getattr(decision, "id", None),
            repair_state=_issue_repair_state(issue),
            resolved=resolved,
            resolution_reason=(
                getattr(decision, "reason", None)
                if decision is not None
                else "source_archived_pending_references"
                if pending_references
                else None
            ),
            rebuild_required=rebuild_required,
            recommended_stage=recommended_stage,
            is_publication_ioc=is_ioc,
            in_publication_scope=_row_in_publication_scope(row),
            application_state=state.value,
        )


def _issue_kind(issue: Any) -> str:
    value = getattr(issue, "kind", "")
    return str(getattr(value, "value", value))


def issue_application_state(issue: Any, decision: Any) -> RepairDecisionApplicationState:
    """Read the state the issue reader computed, or derive it for plain DTOs."""
    return repair_issue_application_state(issue, decision)


def _issue_repair_state(issue: Any) -> str | None:
    value = getattr(issue, "repair_state", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _issue_pending_references(issue: Any) -> bool:
    """True when the source is archived but REFERENCES has not caught up.

    This is the single backend definition of the rebuild debt: the Repair Desk
    read model, the review sign-off rule and the publication freeze all use it,
    so no client-side state can make the debt disappear.
    """
    return (
        _issue_repair_state(issue)
        == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES.value
    )


def _issue_subject(issue: Any) -> UUID | None:
    value = getattr(issue, "subject_id", None)
    if isinstance(value, UUID):
        return value
    # Repair issue DTOs carry the run but not the subject for historical
    # compatibility. The caller can only safely use the explicit identity.
    return None


def _row_in_publication_scope(row: EditionReviewReadItem) -> bool:
    """Mirror ``_build_item``: READY without a decision is implicitly included."""
    decision = row.effective_decision
    if decision is None and row.run_status is SubjectProductionStatus.READY:
        decision = PublicationDecision.INCLUDE
    return decision is not PublicationDecision.EXCLUDE


def _repair_summary(items: Sequence[EditionRepairItem]) -> EditionRepairSummary:
    # Counters that gate sign-off only describe the publication scope; an
    # excluded article keeps its issues listed but adds no edition-level debt.
    in_scope = [item for item in items if item.in_publication_scope]
    open_items = [item for item in in_scope if not item.resolved]
    return EditionRepairSummary(
        unresolved_total=len(open_items),
        sources_to_supply=sum(
            item.kind == ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value
            for item in open_items
        ),
        rejected_iocs_to_review=sum(
            item.kind == ProductionRepairIssueKind.REJECTED_INDICATOR.value
            and item.is_publication_ioc
            for item in open_items
        ),
        rejected_rules_to_review=sum(
            item.kind == ProductionRepairIssueKind.REJECTED_RULE.value for item in open_items
        ),
        rejected_other_artifacts=sum(
            item.kind == ProductionRepairIssueKind.REJECTED_INDICATOR.value
            and not item.is_publication_ioc
            for item in open_items
        ),
        articles_with_repairs=len({item.subject_id for item in items}),
        articles_needing_rebuild=len(
            {item.subject_id for item in in_scope if item.rebuild_required}
        ),
    )


def _repair_articles(items: Sequence[EditionRepairItem]) -> tuple[EditionRepairArticle, ...]:
    by_subject: dict[UUID, list[EditionRepairItem]] = {}
    for item in items:
        by_subject.setdefault(item.subject_id, []).append(item)
    priority = {
        "rebuild_references": 0,
        "references": 1,
        "extraction": 2,
        # A decision nothing could materialize must be revised before any
        # rebuild stage can make the article true again.
        "revise_decision": 3,
        "apply_projection": 4,
        "synthesis": 5,
        "none": 6,
    }
    articles: list[tuple[int, EditionRepairArticle]] = []
    for subject_id, subject_items in by_subject.items():
        recommended = min(
            (item.recommended_stage or "none" for item in subject_items),
            key=lambda value: priority.get(value, 99),
        )
        articles.append(
            (
                min(item.position for item in subject_items),
                EditionRepairArticle(
                    subject_id=subject_id,
                    has_pending_projection=any(
                        item.recommended_stage == "apply_projection" for item in subject_items
                    ),
                    recommended_stage=recommended,
                    active_repair_count=sum(not item.resolved for item in subject_items),
                    resolved_since_last_build_count=sum(
                        item.resolved and item.rebuild_required for item in subject_items
                    ),
                ),
            )
        )
    return tuple(item for _position, item in sorted(articles, key=lambda pair: pair[0]))


def _repair_cursor_encode(position: int, repair_key: str) -> str:
    import base64
    import json

    value = json.dumps(
        {"position": position, "repair_key": repair_key},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _repair_cursor_position(cursor: str | None, items: Sequence[EditionRepairItem]) -> int:
    if not cursor:
        return 0
    import base64
    import binascii
    import json

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        position = int(value["position"])
        repair_key = str(value["repair_key"])
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("invalid_repair_cursor") from exc
    for index, item in enumerate(items):
        if (item.position, item.repair_key) > (position, repair_key):
            return index
    return len(items)


class EditionReviewService:
    """Read and append review decisions without mutating production state."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        repair_issue_reader: ProductionRepairIssueReader | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repair_issue_reader = repair_issue_reader

    async def get(self, edition_id: UUID) -> EditionReview:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            self._require_readable(edition, edition_id)
            rows = await uow.edition_review_read_model.list_for_edition(edition_id)
        repairs = await self._repair_issues(edition_id) if self._repair_issue_reader else ()
        return self.from_rows(edition_id, rows, repair_issues=repairs)

    @staticmethod
    def from_rows(
        edition_id: UUID,
        rows: Sequence[EditionReviewReadItem],
        *,
        repair_issues: Sequence[Any] = (),
    ) -> EditionReview:
        """Evaluate the same review rules inside an already open transaction."""
        rows_by_run = {row.run_id: row for row in rows}
        repair_by_subject: dict[UUID, list[Any]] = {}
        for issue in repair_issues:
            subject_id = _issue_subject(issue)
            if subject_id is None:
                run_id = getattr(issue, "production_run_id", None)
                row = rows_by_run.get(run_id) if isinstance(run_id, UUID) else None
                subject_id = row.subject_id if row is not None else None
            if subject_id is not None:
                repair_by_subject.setdefault(subject_id, []).append(issue)
        items = tuple(_build_item(row, repair_by_subject.get(row.subject_id, ())) for row in rows)
        # LOT 21 business rule: the publication scope is what will be delivered.
        # An article the analyst deliberately excluded carries no loss for that
        # scope, so its open repairs must not hold the whole edition hostage.
        # The per-item count stays truthful for the Repair Desk.
        unresolved_repair_count = sum(
            item.unresolved_repair_count
            for item in items
            if item.effective_decision is not PublicationDecision.EXCLUDE
        )
        pending_rebuild_count = sum(
            item.pending_rebuild_count
            for item in items
            if item.effective_decision is not PublicationDecision.EXCLUDE
        )
        return EditionReview(
            edition_id=edition_id,
            items=items,
            can_accept=bool(items)
            and any(item.included for item in items)
            and all(not item.blocking for item in items)
            and unresolved_repair_count == 0
            and pending_rebuild_count == 0,
            unresolved_repair_count=unresolved_repair_count,
            repair_review_complete=unresolved_repair_count == 0,
            pending_rebuild_count=pending_rebuild_count,
        )

    async def _repair_issues(self, edition_id: UUID) -> tuple[Any, ...]:
        assert self._repair_issue_reader is not None
        getter = getattr(self._repair_issue_reader, "list_issue_views", None)
        extraction = (
            await getter(edition_id)
            if callable(getter)
            else await self._repair_issue_reader.list_issues(edition_id)  # type: ignore[attr-defined]
        )
        supplemental_getter = getattr(
            self._repair_issue_reader, "list_supplemental_source_issues", None
        )
        supplemental = (
            await supplemental_getter(edition_id) if callable(supplemental_getter) else ()
        )
        return tuple([*extraction, *supplemental])

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

    @staticmethod
    def _require_readable(edition: object, edition_id: UUID) -> None:
        """Read policy: the audit trail survives the freeze.

        Reading a historical review is not editing it. Every state from
        PRODUCTION onwards -- the freeze and the publication included -- keeps
        its Repair Desk legible; the write policies above stay unchanged, so a
        frozen edition still refuses every mutation.
        """
        if edition is None:
            raise EditionReviewNotFoundError(str(edition_id))
        edition_status = getattr(edition, "status", None)
        status_value = getattr(edition_status, "value", edition_status)
        if status_value not in READABLE_REVIEW_STATUSES:
            raise EditionReviewStatusError("edition_has_no_review")


async def _get_edition_for_update(uow: object, edition_id: UUID) -> object:
    repository = uow.editions  # type: ignore[attr-defined]
    get_for_update = getattr(repository, "get_for_update", None)
    if get_for_update is not None:
        return await get_for_update(edition_id)
    return await repository.get(edition_id)


def _build_item(row: EditionReviewReadItem, repair_issues: Sequence[Any] = ()) -> EditionReviewItem:
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
    active_repair_count = len(repair_issues)
    unresolved_repair_count = sum(
        _repair_issue_is_actionable(issue) and not _repair_issue_resolved(issue)
        for issue in repair_issues
    )
    pending_rebuild_count = sum(repair_issue_blocks_signoff(issue) for issue in repair_issues)
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
        # Une perte d'indicateurs ou de règles n'est jamais bloquante : c'est un
        # signal éditorial. Bloquer automatiquement empêcherait de publier un
        # article dont la source ne fournit tout simplement pas de règle.
        rejected_indicator_count=row.rejected_indicator_count,
        rejected_ioc_count=row.rejected_ioc_count,
        rejected_other_artifact_count=row.rejected_other_artifact_count,
        rejected_rule_count=row.rejected_rule_count,
        published_rule_count=row.published_rule_count,
        active_repair_count=active_repair_count,
        unresolved_repair_count=unresolved_repair_count,
        pending_rebuild_count=pending_rebuild_count,
    )


def _repair_issue_is_actionable(issue: Any) -> bool:
    kind = _issue_kind(issue)
    return kind in {
        ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value,
        ProductionRepairIssueKind.REJECTED_RULE.value,
    } or (
        kind == ProductionRepairIssueKind.REJECTED_INDICATOR.value
        and bool(getattr(issue, "is_publication_ioc", False))
    )


def _repair_issue_resolved(issue: Any) -> bool:
    # An archived source has nothing left to arbitrate; its remaining debt is
    # counted by ``pending_rebuild_count`` instead.
    return getattr(issue, "effective_decision", None) is not None or _issue_pending_references(
        issue
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
    "EditionRepairArticle",
    "EditionRepairItem",
    "EditionRepairPage",
    "EditionRepairReadService",
    "EditionRepairSummary",
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
    "ProductionRepairIssueReader",
    "ReviewItemStaleError",
    "requires_reconciliation",
    "review_item_can_retry",
]
