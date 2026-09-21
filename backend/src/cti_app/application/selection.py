from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.subjects import SubjectService
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import CandidateTopic, DiscoveryCandidate
from cti_app.domain.discovery_cumulative import (
    DiscoveryIdentityStatus,
    DiscoveryMergeRun,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import ProvenanceEvent, Subject
from cti_app.domain.selection import (
    SelectionAction,
    SelectionDecision,
    SelectionIdempotencyRecord,
    SubjectDiscoveryOrigin,
    selection_request_fingerprint,
)

logger = logging.getLogger(__name__)


class SelectionError(ValueError):
    code = "selection_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class SelectionSnapshotStaleError(SelectionError):
    code = "selection_snapshot_stale"


class SelectionDecisionStaleError(SelectionError):
    code = "selection_decision_stale"


class SelectionSubjectAlreadyMaterializedError(SelectionError):
    code = "selection_subject_already_materialized"


class SelectionEditionArchivedError(SelectionError):
    code = "selection_edition_archived"


class SelectionInvalidCommandError(SelectionError):
    code = "selection_invalid_command"


class SelectionIdempotencyConflictError(SelectionError):
    code = "selection_idempotency_conflict"


class SelectionNotFoundError(SelectionError):
    code = "selection_not_found"


@dataclass(frozen=True, slots=True)
class SelectionRecommendation:
    recommended: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SelectionLastDecision:
    id: UUID
    action: SelectionAction
    snapshot_id: UUID
    snapshot_version: int
    subject_id: UUID | None
    actor_id: str
    occurred_at: datetime

    @property
    def decision_id(self) -> UUID:
        return self.id


SelectionDecisionProjection = SelectionLastDecision


@dataclass(frozen=True, slots=True)
class SelectionItem:
    discovery_subject_id: UUID
    canonical_discovery_subject_id: UUID
    title: str
    summary: str
    actor_or_campaign: str
    technical_potential: int
    technical_potential_reason: str
    artifacts: tuple[str, ...]
    publications: tuple[object, ...]
    provisional_iocs: tuple[object, ...]
    uncertainties: tuple[str, ...]
    selectable: bool
    blocking_reason: str | None
    effective_state: str
    subject_id: UUID | None
    recommendation: SelectionRecommendation
    last_decision: SelectionLastDecision | None
    updated_since_decision: bool
    member_candidate_ids: tuple[UUID, ...]

    @property
    def selection_status(self) -> str:
        return self.effective_state


@dataclass(frozen=True, slots=True)
class SelectionBoard:
    edition_id: UUID
    snapshot_id: UUID | None
    snapshot_version: int | None
    items: tuple[SelectionItem, ...]
    fusion_review_count: int

    @property
    def selected(self) -> int:
        return sum(item.effective_state == "selected" for item in self.items)

    @property
    def ignored(self) -> int:
        return sum(item.effective_state == "ignored" for item in self.items)

    @property
    def undecided(self) -> int:
        return sum(item.effective_state == "undecided" for item in self.items)

    @property
    def selection_items(self) -> tuple[SelectionItem, ...]:
        return self.items


@dataclass(frozen=True, slots=True)
class SelectionDecisionCommand:
    discovery_subject_id: UUID
    action: SelectionAction
    expected_decision_id: UUID | None
    actor_id: str
    correlation_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SelectionRecommendationPolicyV1:
    """Advisory-only recommendation policy for the selection board."""

    version: int = 1

    def recommend(self, candidate: CandidateTopic) -> SelectionRecommendation:
        if _candidate_has_ioc_signal(candidate):
            return SelectionRecommendation(recommended=True, reason="ioc_signal")
        return SelectionRecommendation(recommended=False, reason=None)


class SelectionWorkspaceMaterializer(Protocol):
    async def materialize(
        self,
        subject: Subject,
        source_documents: Sequence[object],
        samples: Sequence[object],
        blobs: Mapping[UUID, object],
        workspace_root: Path,
    ) -> object: ...


class SelectionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        materializer: SelectionWorkspaceMaterializer | SubjectWorkspaceMaterializer | None = None,
        workspace_root: Path = Path("work/subjects"),
        recommendation_policy: SelectionRecommendationPolicyV1 | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._subjects = SubjectService(uow_factory)
        self._materializer = materializer
        self._workspace_root = workspace_root
        self._recommendation_policy = recommendation_policy or SelectionRecommendationPolicyV1()

    async def board(self, edition_id: UUID) -> SelectionBoard:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise SelectionNotFoundError(str(edition_id))
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
            if snapshot is None:
                return SelectionBoard(edition_id, None, None, (), 0)
            identities = list(await uow.discovery_subject_identities.list_for_edition(edition_id))
            candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            decisions = list(await uow.selection_decisions.list_for_edition(edition_id))
            origins = list(await uow.subject_discovery_origins.list_for_edition(edition_id))
            subjects = list(await uow.subjects.list_for_edition(edition_id))
            merge_runs = list(await uow.discovery_merge_runs.list_for_edition(edition_id))
            snapshots: dict[UUID, DiscoverySnapshot] = {snapshot.id: snapshot}
            board = await self._build_board(
                edition,
                snapshot,
                identities,
                candidates,
                decisions,
                origins,
                subjects,
                merge_runs,
                snapshots,
                uow,
            )
            return board

    async def decide_many(
        self,
        edition_id: UUID,
        commands: Sequence[SelectionDecisionCommand],
        *,
        snapshot_version: int | None = None,
        expected_snapshot_version: int | None = None,
    ) -> SelectionBoard:
        if not commands:
            raise SelectionInvalidCommandError("At least one selection decision is required")
        ids = [command.discovery_subject_id for command in commands]
        if len(ids) != len(set(ids)):
            raise SelectionInvalidCommandError("A discovery subject can only be decided once")
        keys = {command.idempotency_key for command in commands}
        if len(keys) != 1:
            raise SelectionInvalidCommandError("A batch carries exactly one idempotency key")
        idempotency_key = keys.pop()
        if snapshot_version is not None and expected_snapshot_version is not None:
            if snapshot_version != expected_snapshot_version:
                raise SelectionSnapshotStaleError()
        expected_version = (
            snapshot_version if snapshot_version is not None else expected_snapshot_version
        )

        created: list[Subject] = []
        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(edition_id)
            if edition is None:
                raise SelectionNotFoundError(str(edition_id))
            if edition.state is EditionStatus.ARCHIVED:
                raise SelectionEditionArchivedError()
            snapshot = await uow.discovery_snapshots.get_active_for_update(edition_id)
            if snapshot is None:
                raise SelectionNotFoundError("No active discovery snapshot")
            if expected_version is not None and expected_version != snapshot.version:
                raise SelectionSnapshotStaleError()

            fingerprint = selection_request_fingerprint(
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                decisions=[(command.discovery_subject_id, command.action) for command in commands],
            )
            recorded = await uow.selection_idempotency.get_for_update(edition_id, idempotency_key)
            if recorded is not None and recorded.request_fingerprint != fingerprint:
                # Same key, other snapshot or other set of decisions: the key
                # is already spent, so nothing may be applied under it.
                raise SelectionIdempotencyConflictError()
            replay = recorded is not None
            if replay:
                # Exact replay of an already applied batch: release the locks
                # and answer with the canonical board, without appending.
                await uow.rollback()
            else:
                await uow.selection_idempotency.add(
                    SelectionIdempotencyRecord(
                        edition_id=edition_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        actor_id=commands[0].actor_id,
                        correlation_id=commands[0].correlation_id,
                    )
                )
                created = await self._apply_decisions(uow, edition, snapshot, commands)
                await uow.commit()

        for subject in created:
            await self._materialize_after_commit(subject, edition_id)
        return await self.board(edition_id)

    async def _apply_decisions(
        self,
        uow: UnitOfWork,
        edition: Edition,
        snapshot: DiscoverySnapshot,
        commands: Sequence[SelectionDecisionCommand],
    ) -> list[Subject]:
        """Validate the whole batch, then append it inside the caller's transaction."""

        edition_id = edition.id
        created: list[Subject] = []
        identities = list(await uow.discovery_subject_identities.list_for_edition(edition_id))
        identity_map = {identity.id: identity for identity in identities}
        decisions = list(await uow.selection_decisions.list_for_edition(edition_id))
        origins = list(await uow.subject_discovery_origins.list_for_edition(edition_id))
        merge_runs = list(await uow.discovery_merge_runs.list_for_edition(edition_id))
        active_by_id = {item.subject_id: item for item in snapshot.subjects}
        canonical = self._canonical_resolver(identity_map)
        blocked = self._blocked_ids(snapshot, merge_runs)

        locked: list[tuple[SelectionDecisionCommand, DiscoverySubject, UUID, tuple[UUID, ...]]] = []
        for command in commands:
            active_subject = active_by_id.get(command.discovery_subject_id)
            if active_subject is None or command.discovery_subject_id not in identity_map:
                raise SelectionInvalidCommandError("Target is not an active snapshot identity")
            if command.discovery_subject_id in blocked:
                raise SelectionInvalidCommandError("Target is awaiting fusion review")
            closure = tuple(
                identity.id
                for identity in identities
                if canonical(identity.id) == canonical(command.discovery_subject_id)
            )
            # Locking all target identities is done before any append. The
            # list was loaded once, so canonical resolution remains in-memory.
            for identity_id in sorted(closure, key=lambda value: value.hex):
                if await uow.discovery_subject_identities.get_for_update(identity_id) is None:
                    raise SelectionInvalidCommandError("Target identity disappeared")
            locked.append(
                (command, active_subject, canonical(command.discovery_subject_id), closure)
            )

        candidate_rows = await uow.discovery_candidates.list_for_edition(
            edition_id, include_replaced=False
        )
        candidates_by_id = {candidate.id: candidate for candidate in candidate_rows}
        validated: list[
            tuple[
                SelectionDecisionCommand,
                DiscoverySubject,
                tuple[UUID, ...],
                SubjectDiscoveryOrigin | None,
            ]
        ] = []
        for command, active_subject, _, closure in locked:
            origin = self._origin_for_closure(origins, closure)
            if command.action is SelectionAction.IGNORE and origin is not None:
                raise SelectionSubjectAlreadyMaterializedError()
            newest = self._newest_decision(decisions, closure)
            newest_id = newest.id if newest is not None else None
            # An absent expectation is an expectation: the operator read an
            # undecided subject, so a decision appended in the meantime must
            # make the command stale instead of silently applying.
            if newest_id != command.expected_decision_id:
                raise SelectionDecisionStaleError()
            validated.append((command, active_subject, closure, origin))

        for command, active_subject, _closure, origin in validated:
            if command.action is SelectionAction.SELECT and origin is not None:
                continue

            subject: Subject | None = None
            if command.action is SelectionAction.SELECT:
                members = [
                    candidates_by_id[reference.candidate_id]
                    for reference in active_subject.member_references
                    if reference.candidate_id in candidates_by_id
                ]
                subject_tlp = self._most_restrictive_tlp(edition, members)
                subject = await self._subjects.materialize_in_uow(
                    uow,
                    edition_id=edition_id,
                    title=active_subject.canonical_title,
                    initial_tlp=subject_tlp,
                )
            decision = SelectionDecision(
                edition_id=edition_id,
                discovery_subject_id=command.discovery_subject_id,
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                action=command.action,
                subject_id=subject.id if subject else None,
                actor_id=command.actor_id,
                correlation_id=command.correlation_id,
                idempotency_key=command.idempotency_key,
            )
            await uow.selection_decisions.append(decision)
            decisions.append(decision)
            if subject is not None:
                origin_record = SubjectDiscoveryOrigin(
                    subject_id=subject.id,
                    edition_id=edition_id,
                    discovery_subject_id=command.discovery_subject_id,
                    selection_decision_id=decision.id,
                    selected_snapshot_id=snapshot.id,
                    selected_snapshot_version=snapshot.version,
                )
                await uow.subject_discovery_origins.add(origin_record)
                origins.append(origin_record)
                await uow.provenance.append(
                    ProvenanceEvent(
                        subject_id=subject.id,
                        aggregate_type="subject",
                        aggregate_id=subject.id,
                        event_type="subject.created_from_selection",
                        payload={
                            "discovery_subject_id": str(command.discovery_subject_id),
                            "selection_decision_id": str(decision.id),
                            "snapshot_id": str(snapshot.id),
                            "snapshot_version": snapshot.version,
                            "candidate_ids": [
                                str(reference.candidate_id)
                                for reference in active_subject.member_references
                            ],
                        },
                        tlp=subject.tlp,
                        actor_id=command.actor_id,
                    )
                )
                created.append(subject)
        return created

    async def _materialize_after_commit(self, subject: Subject, edition_id: UUID) -> None:
        if self._materializer is None:
            return
        try:
            await self._materializer.materialize(subject, (), (), {}, self._workspace_root)
        except Exception:
            logger.exception(
                "subject_workspace_materialize_failed",
                extra={"edition_id": str(edition_id), "subject_id": str(subject.id)},
            )

    async def _build_board(
        self,
        edition: Edition,
        snapshot: DiscoverySnapshot,
        identities: Sequence[DiscoverySubjectIdentity],
        candidates: Sequence[DiscoveryCandidate],
        decisions: Sequence[SelectionDecision],
        origins: Sequence[SubjectDiscoveryOrigin],
        subjects: Sequence[Subject],
        merge_runs: Sequence[DiscoveryMergeRun],
        snapshots: dict[UUID, DiscoverySnapshot],
        uow: UnitOfWork,
    ) -> SelectionBoard:
        identity_map = {identity.id: identity for identity in identities}
        canonical = self._canonical_resolver(identity_map)
        closures = self._identity_closures(identities, canonical)
        subjects_by_id = {subject.id: subject for subject in subjects}
        decisions_by_canonical: dict[UUID, list[SelectionDecision]] = {}
        for decision in decisions:
            decisions_by_canonical.setdefault(canonical(decision.discovery_subject_id), []).append(
                decision
            )
        origins_by_canonical: dict[UUID, SubjectDiscoveryOrigin] = {}
        for known_origin in origins:
            origins_by_canonical.setdefault(
                canonical(known_origin.discovery_subject_id), known_origin
            )
        blocked = self._blocked_ids(snapshot, merge_runs)
        historical_subject_maps: dict[UUID, dict[UUID, DiscoverySubject]] = {}
        items: list[SelectionItem] = []
        for discovery_subject in snapshot.subjects:
            identity_id = discovery_subject.subject_id
            canonical_id = canonical(identity_id)
            closure = closures.get(canonical_id, (identity_id,))
            origin = origins_by_canonical.get(canonical_id)
            last_decision = self._newest_decision(
                decisions_by_canonical.get(canonical_id, ()), closure
            )
            selected_subject = subjects_by_id.get(origin.subject_id) if origin else None
            topic = discovery_subject.candidate
            historical_snapshot_id = (
                origin.selected_snapshot_id
                if origin
                else last_decision.snapshot_id
                if last_decision
                else None
            )
            updated = False
            if historical_snapshot_id is not None:
                historical = await self._snapshot_cached(historical_snapshot_id, snapshots, uow)
                if historical is not None and historical.id not in historical_subject_maps:
                    historical_subject_maps[historical.id] = {
                        canonical(subject.subject_id): subject for subject in historical.subjects
                    }
                historical_subject = (
                    historical_subject_maps.get(historical.id, {}).get(canonical_id)
                    if historical
                    else None
                )
                if historical_subject is not None:
                    current_ids = {
                        reference.candidate_id for reference in discovery_subject.member_references
                    }
                    old_ids = {
                        reference.candidate_id for reference in historical_subject.member_references
                    }
                    updated = current_ids != old_ids
            state = (
                "selected"
                if origin
                else (
                    "ignored"
                    if last_decision and last_decision.action is SelectionAction.IGNORE
                    else "undecided"
                )
            )
            item_decision = (
                SelectionLastDecision(
                    id=last_decision.id,
                    action=last_decision.action,
                    snapshot_id=last_decision.snapshot_id,
                    snapshot_version=last_decision.snapshot_version,
                    subject_id=last_decision.subject_id,
                    actor_id=last_decision.actor_id,
                    occurred_at=last_decision.occurred_at,
                )
                if last_decision
                else None
            )
            item = SelectionItem(
                discovery_subject_id=identity_id,
                canonical_discovery_subject_id=canonical(identity_id),
                title=topic.title,
                summary=topic.summary,
                actor_or_campaign=topic.actor_or_campaign,
                technical_potential=topic.technical_potential,
                technical_potential_reason=topic.technical_potential_reason,
                artifacts=tuple(topic.likely_artifacts),
                publications=tuple(topic.sources),
                provisional_iocs=tuple(topic.provisional_iocs),
                uncertainties=tuple(topic.uncertainties),
                selectable=identity_id not in blocked,
                blocking_reason="fusion_review_pending" if identity_id in blocked else None,
                effective_state=state,
                subject_id=selected_subject.id
                if selected_subject
                else (last_decision.subject_id if last_decision else None),
                recommendation=self._recommendation_policy.recommend(topic),
                last_decision=item_decision,
                updated_since_decision=updated,
                member_candidate_ids=tuple(
                    reference.candidate_id for reference in discovery_subject.member_references
                ),
            )
            items.append(item)
        superseded_runs = {
            run.supersedes_merge_run_id
            for run in merge_runs
            if run.supersedes_merge_run_id is not None
        }
        review_count = sum(
            run.validation_status is MergeValidationStatus.NEEDS_REVIEW
            and run.parent_snapshot_id == snapshot.id
            and run.id not in superseded_runs
            for run in merge_runs
        )
        return SelectionBoard(
            edition_id=edition.id,
            snapshot_id=snapshot.id,
            snapshot_version=snapshot.version,
            items=tuple(items),
            fusion_review_count=review_count,
        )

    async def _snapshot_cached(
        self,
        snapshot_id: UUID,
        cache: dict[UUID, DiscoverySnapshot],
        uow: UnitOfWork,
    ) -> DiscoverySnapshot | None:
        if snapshot_id not in cache:
            cache[snapshot_id] = await uow.discovery_snapshots.get(snapshot_id)  # type: ignore[assignment]
        return cache.get(snapshot_id)

    @staticmethod
    def _identity_closures(
        identities: Sequence[DiscoverySubjectIdentity],
        canonical: Callable[[UUID], UUID],
    ) -> dict[UUID, tuple[UUID, ...]]:
        grouped: dict[UUID, list[UUID]] = {}
        for identity in identities:
            grouped.setdefault(canonical(identity.id), []).append(identity.id)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _canonical_resolver(
        identities: Mapping[UUID, DiscoverySubjectIdentity],
    ) -> Callable[[UUID], UUID]:
        cache: dict[UUID, UUID] = {}

        def resolve(subject_id: UUID) -> UUID:
            if subject_id in cache:
                return cache[subject_id]
            current = subject_id
            seen: set[UUID] = set()
            while current in identities:
                if current in seen:
                    break
                seen.add(current)
                identity = identities[current]
                if (
                    identity.status is not DiscoveryIdentityStatus.MERGED
                    or identity.merged_into_id is None
                ):
                    break
                current = identity.merged_into_id
            for visited in seen:
                cache[visited] = current
            cache[subject_id] = current
            return current

        return resolve

    @staticmethod
    def _blocked_ids(
        snapshot: DiscoverySnapshot, merge_runs: Sequence[DiscoveryMergeRun]
    ) -> set[UUID]:
        blocked: set[UUID] = set()
        superseded = {
            run.supersedes_merge_run_id
            for run in merge_runs
            if run.supersedes_merge_run_id is not None
        }
        for run in merge_runs:
            if (
                run.validation_status is MergeValidationStatus.NEEDS_REVIEW
                and run.parent_snapshot_id == snapshot.id
                and run.id not in superseded
            ):
                blocked.update(run.included_subject_ids)
        return blocked

    @staticmethod
    def _newest_decision(
        decisions: Sequence[SelectionDecision], closure: Sequence[UUID]
    ) -> SelectionDecision | None:
        candidates = [
            decision for decision in decisions if decision.discovery_subject_id in closure
        ]
        return max(
            candidates,
            key=lambda decision: (decision.occurred_at, decision.id.hex),
            default=None,
        )

    @staticmethod
    def _origin_for_closure(
        origins: Sequence[SubjectDiscoveryOrigin], closure: Sequence[UUID]
    ) -> SubjectDiscoveryOrigin | None:
        return next((origin for origin in origins if origin.discovery_subject_id in closure), None)

    @staticmethod
    def _most_restrictive_tlp(edition: Edition, candidates: Sequence[DiscoveryCandidate]) -> TLP:
        order = {TLP.CLEAR: 0, TLP.GREEN: 1, TLP.AMBER: 2, TLP.AMBER_STRICT: 3, TLP.RED: 4}
        return max(
            (edition.tlp, *(candidate.tlp for candidate in candidates)),
            key=order.__getitem__,
        )


def _candidate_has_ioc_signal(candidate: CandidateTopic) -> bool:
    if candidate.iocs or candidate.provisional_iocs:
        return True
    return any(
        getattr(source.ioc_presence, "value", source.ioc_presence)
        in {"declared", "visible", "present"}
        or (source.ioc_declared_count is not None and source.ioc_declared_count > 0)
        or (source.ioc_visible_count is not None and source.ioc_visible_count > 0)
        for source in candidate.sources
    )
