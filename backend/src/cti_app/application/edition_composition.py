"""Commands and read validation for canonical edition compositions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from cti_app.application.production_repairs import repair_issue_blocks_signoff
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    CompositionInvariantError,
    CompositionMutationKind,
    CompositionPreparationStatus,
    EditionComposition,
    EditionCompositionEntry,
    EditionCompositionEvent,
    composition_entry_snapshot,
    composition_from_snapshot,
    composition_request_fingerprint,
    composition_snapshot,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import ProductionArtifactStatus, ProductionRunStatus


class EditionCompositionError(Exception):
    code = "edition_composition_error"
    status_code = 409


class CompositionNotFound(EditionCompositionError):
    code = "edition_not_found"
    status_code = 404


class CompositionVersionConflict(EditionCompositionError):
    """The operator edited a composition version that is no longer current."""

    code = "composition_version_conflict"
    status_code = 409

    def __init__(self, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"expected composition version {expected_version}, current version is {actual_version}"
        )


class CompositionIdempotencyConflict(EditionCompositionError):
    code = "composition_idempotency_conflict"
    status_code = 409


class CompositionEditionArchived(EditionCompositionError):
    code = "composition_edition_archived"
    status_code = 409


class CompositionReferenceError(EditionCompositionError):
    code = "composition_reference_invalid"
    status_code = 409

    def __init__(self, reason: str, subject_id: UUID | None = None) -> None:
        self.reason = reason
        self.subject_id = subject_id
        super().__init__(reason)


class CompositionReorderError(EditionCompositionError):
    code = "composition_reorder_invalid"
    status_code = 422


class CompositionValidationCode(StrEnum):
    SUBJECT_UNADDRESSED = "subject_unaddressed"
    SUBJECT_NOT_IN_EDITION = "subject_not_in_edition"
    RUN_NOT_IN_EDITION = "run_not_in_edition"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_NOT_VERIFIED = "artifact_not_verified"
    ARTIFACT_PIN_MISMATCH = "artifact_pin_mismatch"
    SUPERSEDED_ARTIFACT_AVAILABLE = "superseded_artifact_available"
    PRODUCTION_RUN_NOT_READY = "production_run_not_ready"
    BLOCKING_REPAIR = "blocking_repair"
    DECISION_MISSING = "decision_missing"


@dataclass(frozen=True, slots=True)
class CompositionValidationReason:
    code: CompositionValidationCode
    message: str
    subject_id: UUID | None = None
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CompositionValidationAxes:
    production_validity: bool | None
    discovery_update_available: bool | None
    composition_eligibility: bool


@dataclass(frozen=True, slots=True)
class CompositionRead:
    edition_id: UUID
    composition: EditionComposition | None
    reasons: tuple[CompositionValidationReason, ...]
    axes: CompositionValidationAxes


@dataclass(frozen=True, slots=True)
class CompositionReferenceState:
    subject_exists: bool
    run_exists: bool = False
    run_belongs_to_edition: bool = False
    run_subject_matches: bool = False
    run_pipeline_generation: int | None = None
    run_status: ProductionRunStatus | None = None
    artifact_exists: bool = False
    artifact_run_matches: bool = False
    artifact_subject_matches: bool = False
    artifact_stage: str | None = None
    artifact_status: ProductionArtifactStatus | None = None
    artifact_version: int | None = None
    artifact_input_hash: str | None = None
    latest_artifact_run_id: UUID | None = None
    latest_artifact_version: int | None = None
    linked_batch_id: UUID | None = None
    supplied_batch_exists_in_edition: bool = False
    supplied_batch_is_linked: bool = False
    discovery_update_available: bool | None = None
    decision_matches_identity: bool = False
    decision_is_valid_exclusion: bool = False


@dataclass(frozen=True, slots=True)
class CompositionReferenceSnapshot:
    subject_ids: frozenset[UUID]
    entries: Mapping[UUID, CompositionReferenceState]


class EditionCompositionRepository(Protocol):
    async def get_for_edition(self, edition_id: UUID) -> EditionComposition | None: ...

    async def get_for_edition_for_update(self, edition_id: UUID) -> EditionComposition | None: ...

    async def save(self, composition: EditionComposition) -> None: ...

    async def replace_entries(
        self, composition_id: UUID, entries: Sequence[EditionCompositionEntry]
    ) -> None: ...

    async def list_subject_ids(self, edition_id: UUID) -> Sequence[UUID]: ...

    async def validate_references(
        self,
        edition_id: UUID,
        entries: Sequence[EditionCompositionEntry],
        *,
        lock: bool,
    ) -> CompositionReferenceSnapshot: ...


class EditionCompositionEventRepository(Protocol):
    async def get_by_idempotency_key(
        self, composition_id: UUID, idempotency_key: str
    ) -> EditionCompositionEvent | None: ...

    async def append(self, event: EditionCompositionEvent) -> None: ...


class CompositionUnitOfWork(Protocol):
    editions: Any
    edition_compositions: EditionCompositionRepository
    edition_composition_events: EditionCompositionEventRepository

    async def __aenter__(self) -> CompositionUnitOfWork: ...

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None: ...

    async def commit(self) -> None: ...


class CompositionRepairIssueReader(Protocol):
    async def list_issue_views(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> Sequence[Any]: ...


class EditionCompositionService:
    """Versioned composition commands; this service never starts production work."""

    def __init__(
        self,
        uow_factory: Callable[[], CompositionUnitOfWork],
        repair_issue_reader: CompositionRepairIssueReader | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repair_issue_reader = repair_issue_reader

    async def get(self, edition_id: UUID) -> CompositionRead:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise CompositionNotFound("edition not found")
            composition = await uow.edition_compositions.get_for_edition(edition_id)
            subject_ids = frozenset(await uow.edition_compositions.list_subject_ids(edition_id))
            references = await uow.edition_compositions.validate_references(
                edition_id, composition.entries if composition else (), lock=False
            )

        reasons = _validate_composition(composition, subject_ids, references)
        if composition and self._repair_issue_reader:
            included_ids = {
                entry.subject_id
                for entry in composition.entries
                if entry.disposition is CompositionDisposition.INCLUDED
            }
            if included_ids:
                issues = await self._repair_issue_reader.list_issue_views(edition_id)
                for issue in issues:
                    subject_id = getattr(issue, "subject_id", None)
                    if subject_id in included_ids and repair_issue_blocks_signoff(issue):
                        reasons.append(
                            CompositionValidationReason(
                                code=CompositionValidationCode.BLOCKING_REPAIR,
                                message="included subject has a blocking production repair",
                                subject_id=subject_id,
                                details={
                                    "repair_key": getattr(issue, "repair_key", None),
                                    "kind": str(getattr(issue, "kind", "")),
                                },
                            )
                        )

        included_count = (
            sum(
                entry.disposition is CompositionDisposition.INCLUDED
                for entry in composition.entries
            )
            if composition
            else 0
        )
        production_validity = (
            None
            if included_count == 0
            else not any(
                reason.code
                in {
                    CompositionValidationCode.RUN_NOT_IN_EDITION,
                    CompositionValidationCode.ARTIFACT_NOT_FOUND,
                    CompositionValidationCode.ARTIFACT_NOT_VERIFIED,
                    CompositionValidationCode.ARTIFACT_PIN_MISMATCH,
                    CompositionValidationCode.SUPERSEDED_ARTIFACT_AVAILABLE,
                    CompositionValidationCode.PRODUCTION_RUN_NOT_READY,
                }
                for reason in reasons
            )
        )
        included_update_states = [
            references.entries[entry.subject_id].discovery_update_available
            for entry in (composition.entries if composition else ())
            if entry.disposition is CompositionDisposition.INCLUDED
            and entry.subject_id in references.entries
        ]
        discovery_update_available = (
            None
            if included_count == 0 or not included_update_states
            else True
            if any(value is True for value in included_update_states)
            else False
            if all(value is False for value in included_update_states)
            else None
        )
        read_composition = _with_preparation_status(composition, reasons)
        return CompositionRead(
            edition_id=edition_id,
            composition=read_composition,
            reasons=tuple(reasons),
            axes=CompositionValidationAxes(
                production_validity=production_validity,
                discovery_update_available=discovery_update_available,
                composition_eligibility=composition is not None and not reasons,
            ),
        )

    async def validate(self, edition_id: UUID) -> CompositionRead:
        return await self.get(edition_id)

    async def replace_entries(
        self,
        edition_id: UUID,
        entries: Sequence[EditionCompositionEntry],
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        requested = tuple(entries)
        return await self._mutate(
            edition_id,
            CompositionMutationKind.REPLACE_ENTRIES,
            {"entries": [_entry_request_payload(entry) for entry in requested]},
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
            transform=lambda _current: requested,
        )

    async def set_entries(
        self,
        edition_id: UUID,
        entries: Sequence[EditionCompositionEntry],
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        """Public synonym for replacing the complete desired entry set."""
        return await self.replace_entries(
            edition_id,
            entries,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

    async def include(
        self,
        edition_id: UUID,
        entry: EditionCompositionEntry,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        if entry.disposition is not CompositionDisposition.INCLUDED:
            raise CompositionInvariantError("include requires an included entry")
        return await self._upsert_one(
            edition_id,
            CompositionMutationKind.INCLUDE,
            entry,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

    async def defer(
        self,
        edition_id: UUID,
        subject_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        entry = EditionCompositionEntry(
            subject_id=subject_id, disposition=CompositionDisposition.DEFERRED
        )
        return await self._upsert_one(
            edition_id,
            CompositionMutationKind.DEFER,
            entry,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

    async def exclude(
        self,
        edition_id: UUID,
        subject_id: UUID,
        review_decision_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        entry = EditionCompositionEntry(
            subject_id=subject_id,
            disposition=CompositionDisposition.EXCLUDED,
            review_decision_id=review_decision_id,
        )
        return await self._upsert_one(
            edition_id,
            CompositionMutationKind.EXCLUDE,
            entry,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

    async def reorder(
        self,
        edition_id: UUID,
        ordered_subject_ids: Sequence[UUID],
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        requested = tuple(ordered_subject_ids)

        def transform(
            current: tuple[EditionCompositionEntry, ...],
        ) -> tuple[EditionCompositionEntry, ...]:
            included = {
                entry.subject_id: entry
                for entry in current
                if entry.disposition is CompositionDisposition.INCLUDED
            }
            if len(set(requested)) != len(requested) or set(requested) != set(included):
                raise CompositionReorderError(
                    "reorder must name every included subject exactly once"
                )
            positions = {
                subject_id: position for position, subject_id in enumerate(requested, start=1)
            }
            return tuple(
                replace(entry, position=positions[entry.subject_id])
                if entry.subject_id in positions
                else entry
                for entry in current
            )

        return await self._mutate(
            edition_id,
            CompositionMutationKind.REORDER,
            {"ordered_subject_ids": [str(value) for value in requested]},
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
            transform=transform,
        )

    async def _upsert_one(
        self,
        edition_id: UUID,
        kind: CompositionMutationKind,
        entry: EditionCompositionEntry,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditionComposition:
        payload = {"entry": _entry_request_payload(entry)}

        def transform(
            current: tuple[EditionCompositionEntry, ...],
        ) -> tuple[EditionCompositionEntry, ...]:
            return (
                *(existing for existing in current if existing.subject_id != entry.subject_id),
                entry,
            )

        return await self._mutate(
            edition_id,
            kind,
            payload,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
            transform=transform,
        )

    async def _mutate(
        self,
        edition_id: UUID,
        kind: CompositionMutationKind,
        command_payload: dict[str, Any],
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str,
        transform: Callable[
            [tuple[EditionCompositionEntry, ...]], tuple[EditionCompositionEntry, ...]
        ],
    ) -> EditionComposition:
        _validate_command_identity(idempotency_key, actor_id, correlation_id)
        if expected_version < 0:
            raise ValueError("expected_version must be >= 0")
        payload = {"kind": kind.value, "expected_version": expected_version, **command_payload}
        fingerprint = composition_request_fingerprint(payload)

        async with self._uow_factory() as uow:
            # Edition is always the first lock in the production/composition hierarchy.
            edition: Edition | None = await uow.editions.get_for_update(edition_id)
            if edition is None:
                raise CompositionNotFound("edition not found")
            if edition.state is EditionStatus.ARCHIVED:
                raise CompositionEditionArchived("archived editions cannot be composed")
            repository = uow.edition_compositions
            current = await repository.get_for_edition_for_update(edition_id)
            current_version = current.version if current else 0
            composition_id = current.id if current else uuid4()
            event_repository = uow.edition_composition_events
            prior = await event_repository.get_by_idempotency_key(composition_id, idempotency_key)
            if prior:
                if prior.request_fingerprint != fingerprint:
                    raise CompositionIdempotencyConflict(
                        "the idempotency key was already used with another payload"
                    )
                return composition_from_snapshot(prior.result_snapshot)
            if current_version != expected_version:
                raise CompositionVersionConflict(expected_version, current_version)

            current_entries = current.entries if current else ()
            requested = tuple(transform(current_entries))
            _assert_unique_subjects(requested)
            references = await repository.validate_references(edition_id, requested, lock=True)
            _validate_mutation_references(requested, references)

            now = datetime.now(UTC)
            normalized = tuple(
                replace(
                    entry,
                    id=uuid4() if entry in current_entries else entry.id,
                    provenance_batch_id=_resolved_batch_id(entry, references),
                    preparation_status=CompositionPreparationStatus.UNASSESSED,
                )
                for entry in requested
            )
            composition = EditionComposition(
                id=composition_id,
                edition_id=edition_id,
                version=current_version + 1,
                entries=normalized,
                actor_id=actor_id,
                preparation_status=CompositionPreparationStatus.UNASSESSED,
                created_at=current.created_at if current else now,
                updated_at=now,
            )
            await repository.save(composition)
            await repository.replace_entries(composition.id, composition.entries)
            event = EditionCompositionEvent(
                composition_id=composition.id,
                edition_id=edition_id,
                kind=kind,
                expected_version=expected_version,
                result_version=composition.version,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
                result_snapshot=composition_snapshot(composition),
                occurred_at=now,
            )
            await event_repository.append(event)
            await uow.commit()
            return composition


def _validate_command_identity(idempotency_key: str, actor_id: str, correlation_id: str) -> None:
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("idempotency_key must be non-empty and at most 255 characters")
    if not actor_id.strip() or len(actor_id) > 255:
        raise ValueError("actor_id must be non-empty and at most 255 characters")
    if not correlation_id.strip() or len(correlation_id) > 128:
        raise ValueError("correlation_id must be non-empty and at most 128 characters")


def _entry_request_payload(entry: EditionCompositionEntry) -> dict[str, Any]:
    """Hash only caller-visible fields, excluding generated identity and read status."""
    snapshot = composition_entry_snapshot(entry)
    snapshot.pop("id")
    snapshot.pop("preparation_status")
    return snapshot


def _assert_unique_subjects(entries: Sequence[EditionCompositionEntry]) -> None:
    if len({entry.subject_id for entry in entries}) != len(entries):
        raise CompositionInvariantError("each subject may appear only once")
    included_positions = [
        entry.position for entry in entries if entry.disposition is CompositionDisposition.INCLUDED
    ]
    if len(set(included_positions)) != len(included_positions):
        raise CompositionInvariantError("included entry positions must be unique")


def _resolved_batch_id(
    entry: EditionCompositionEntry, references: CompositionReferenceSnapshot
) -> UUID | None:
    state = references.entries.get(entry.subject_id)
    if state is None:
        return entry.provenance_batch_id
    if entry.provenance_batch_id is not None:
        return entry.provenance_batch_id
    return state.linked_batch_id


def _validate_mutation_references(
    entries: Sequence[EditionCompositionEntry], references: CompositionReferenceSnapshot
) -> None:
    for entry in entries:
        state = references.entries.get(entry.subject_id)
        if state is None or not state.subject_exists:
            raise CompositionReferenceError("subject_not_in_edition", entry.subject_id)
        if entry.production_run_id is not None:
            if (
                not state.run_exists
                or not state.run_belongs_to_edition
                or not state.run_subject_matches
            ):
                raise CompositionReferenceError("production_run_not_in_edition", entry.subject_id)
            if not state.artifact_exists:
                raise CompositionReferenceError("publication_artifact_not_found", entry.subject_id)
            if (
                not state.artifact_run_matches
                or not state.artifact_subject_matches
                or state.artifact_stage != "publication"
                or state.artifact_version != entry.artifact_version
                or state.artifact_input_hash != entry.artifact_input_hash
                or state.run_pipeline_generation != entry.pipeline_generation
            ):
                raise CompositionReferenceError(
                    "publication_artifact_pin_mismatch", entry.subject_id
                )
            if state.artifact_status is not ProductionArtifactStatus.VERIFIED:
                raise CompositionReferenceError(
                    "publication_artifact_not_verified", entry.subject_id
                )
        elif entry.disposition is CompositionDisposition.INCLUDED:
            raise CompositionReferenceError("production_run_not_in_edition", entry.subject_id)
        if entry.provenance_batch_id is not None:
            if not state.supplied_batch_exists_in_edition:
                raise CompositionReferenceError("provenance_batch_not_in_edition", entry.subject_id)
            if entry.production_run_id is not None and not state.supplied_batch_is_linked:
                raise CompositionReferenceError(
                    "provenance_batch_not_linked_to_run", entry.subject_id
                )
        if entry.review_decision_id is not None and not state.decision_matches_identity:
            raise CompositionReferenceError("review_decision_identity_mismatch", entry.subject_id)
        if (
            entry.disposition is CompositionDisposition.EXCLUDED
            and not state.decision_is_valid_exclusion
        ):
            raise CompositionReferenceError("review_exclusion_decision_missing", entry.subject_id)


def _validate_composition(
    composition: EditionComposition | None,
    subject_ids: frozenset[UUID],
    references: CompositionReferenceSnapshot,
) -> list[CompositionValidationReason]:
    reasons: list[CompositionValidationReason] = []
    addressed = {entry.subject_id for entry in composition.entries} if composition else set()
    for subject_id in sorted(subject_ids - addressed, key=str):
        reasons.append(
            CompositionValidationReason(
                code=CompositionValidationCode.SUBJECT_UNADDRESSED,
                message="edition subject has no composition entry",
                subject_id=subject_id,
            )
        )
    if composition is None:
        return reasons

    for entry in composition.entries:
        state = references.entries.get(entry.subject_id)
        if state is None or not state.subject_exists:
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.SUBJECT_NOT_IN_EDITION,
                    message="composition subject does not belong to this edition",
                    subject_id=entry.subject_id,
                )
            )
            continue
        if entry.review_decision_id is not None and not state.decision_matches_identity:
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.DECISION_MISSING,
                    message="review decision does not apply to the pinned artifact identity",
                    subject_id=entry.subject_id,
                )
            )
        elif entry.disposition is CompositionDisposition.EXCLUDED:
            if not state.decision_is_valid_exclusion:
                reasons.append(
                    CompositionValidationReason(
                        code=CompositionValidationCode.DECISION_MISSING,
                        message="excluded subject has no applicable exclusion decision",
                        subject_id=entry.subject_id,
                    )
                )
            continue
        if entry.disposition is not CompositionDisposition.INCLUDED:
            continue
        if (
            not state.run_exists
            or not state.run_belongs_to_edition
            or not state.run_subject_matches
        ):
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.RUN_NOT_IN_EDITION,
                    message="pinned production run does not belong to the edition subject",
                    subject_id=entry.subject_id,
                )
            )
            continue
        if state.run_status is not ProductionRunStatus.READY:
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.PRODUCTION_RUN_NOT_READY,
                    message="pinned production run is not ready",
                    subject_id=entry.subject_id,
                    details={"status": state.run_status.value if state.run_status else None},
                )
            )
        if not state.artifact_exists:
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.ARTIFACT_NOT_FOUND,
                    message="pinned publication artifact no longer exists",
                    subject_id=entry.subject_id,
                )
            )
            continue
        if state.artifact_status is not ProductionArtifactStatus.VERIFIED:
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.ARTIFACT_NOT_VERIFIED,
                    message="pinned publication artifact is stale or not verified",
                    subject_id=entry.subject_id,
                    details={
                        "status": (state.artifact_status.value if state.artifact_status else None)
                    },
                )
            )
        if (
            not state.artifact_run_matches
            or not state.artifact_subject_matches
            or state.artifact_stage != "publication"
            or state.artifact_version != entry.artifact_version
            or state.artifact_input_hash != entry.artifact_input_hash
            or state.run_pipeline_generation != entry.pipeline_generation
        ):
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.ARTIFACT_PIN_MISMATCH,
                    message="pinned publication artifact identity has changed",
                    subject_id=entry.subject_id,
                )
            )
        latest_version = state.latest_artifact_version
        if (
            state.latest_artifact_run_id == entry.production_run_id
            and latest_version is not None
            and entry.artifact_version is not None
            and latest_version > entry.artifact_version
        ):
            reasons.append(
                CompositionValidationReason(
                    code=CompositionValidationCode.SUPERSEDED_ARTIFACT_AVAILABLE,
                    message="a newer verified publication artifact is available",
                    subject_id=entry.subject_id,
                    details={
                        "latest_run_id": str(state.latest_artifact_run_id),
                        "pinned_version": entry.artifact_version,
                        "latest_version": latest_version,
                    },
                )
            )
    return reasons


def _with_preparation_status(
    composition: EditionComposition | None,
    reasons: Sequence[CompositionValidationReason],
) -> EditionComposition | None:
    """Project validation status onto entries without persisting a read result."""
    if composition is None:
        return None
    blocked_subjects = {reason.subject_id for reason in reasons if reason.subject_id is not None}
    entries = tuple(
        replace(
            entry,
            preparation_status=(
                CompositionPreparationStatus.BLOCKED
                if entry.subject_id in blocked_subjects
                else CompositionPreparationStatus.READY
            ),
        )
        if entry.disposition is CompositionDisposition.INCLUDED
        else entry
        for entry in composition.entries
    )
    included = any(entry.disposition is CompositionDisposition.INCLUDED for entry in entries)
    status = (
        CompositionPreparationStatus.UNASSESSED
        if not included and not reasons
        else CompositionPreparationStatus.BLOCKED
        if reasons
        else CompositionPreparationStatus.READY
    )
    return replace(composition, entries=entries, preparation_status=status)
