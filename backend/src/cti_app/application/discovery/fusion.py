"""Fusion: the only human authority over the structure of discovered subjects.

The board is a read model rebuilt from canonical state on every read
(`DiscoveryCandidate`, the active `DiscoverySnapshot`, `DiscoveryMergeRun`,
contributions, identities and merge events). Nothing here is persisted except
through the existing cumulative records: every structural mutation produces an
auditable HUMAN merge run and a new snapshot version.

Planner handles (`C1`, `X1`) stay inside this module and the cumulative layer;
the board and the mutations speak exclusively in business UUIDs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from itertools import combinations
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.discovery.cumulative.apply import (
    apply_discovery_merge_plan,
    apply_structural_subject_merge,
    apply_structural_subject_split,
)
from cti_app.application.discovery.cumulative.context import build_discovery_delta
from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.merge_runs import (
    make_human_merge_run,
    make_structural_merge_run,
)
from cti_app.application.discovery.cumulative.types import ResolvedMergeHandles
from cti_app.application.discovery.cumulative.validation import (
    requires_review,
    validate_candidate_coverage,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.discovery import CandidateTopic, DiscoveryCandidate, same_publication
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    FusionReviewAction,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
    SubjectContribution,
    SubjectMergeEvent,
)
from cti_app.domain.editions import Edition, EditionStatus

HUMAN_DECIDED_FLAG = "human_decided"
_STRUCTURAL_ACTIONS = frozenset({"merged", "split", "split_created"})
_MODEL_SUMMARY_LIMIT = 280
_TITLE_SIMILARITY_THRESHOLD = 0.8
_DATE_PROXIMITY_DAYS = 7


class FusionSnapshotStaleError(RuntimeError):
    code = "fusion_snapshot_stale"


class FusionEditionArchivedError(ValueError):
    code = "fusion_edition_archived"


@dataclass(frozen=True, slots=True)
class FusionPublication:
    id: UUID
    url: str
    canonical_url: str
    title: str
    publisher: str
    published_at: date | None


@dataclass(frozen=True, slots=True)
class FusionCandidate:
    id: UUID
    discovery_run_id: UUID
    discovery_batch_id: UUID
    supersedes_candidate_id: UUID | None
    title: str
    summary: str
    event_date: date | None
    actors: tuple[str, ...]
    campaigns: tuple[str, ...]
    malware: tuple[str, ...]
    cves: tuple[str, ...]
    iocs: tuple[str, ...]
    countries: tuple[str, ...]
    sectors: tuple[str, ...]
    likely_artifacts: tuple[str, ...]
    publications: tuple[FusionPublication, ...]


@dataclass(frozen=True, slots=True)
class FusionDeterministicSignal:
    kind: str
    value: str
    candidate_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FusionModelSuggestion:
    recommendation: str
    summary: str


@dataclass(frozen=True, slots=True)
class FusionHistoryEntry:
    action: str
    merge_run_id: UUID | None
    planner_kind: str | None
    actor_id: str | None
    candidate_ids: tuple[UUID, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FusionGroup:
    discovery_subject_id: UUID
    title: str
    summary: str
    candidate_ids: tuple[UUID, ...]
    candidates: tuple[FusionCandidate, ...]
    confidence: str | None
    origin: str | None
    resolution_state: str
    deterministic_signals: tuple[FusionDeterministicSignal, ...]
    model_suggestion: FusionModelSuggestion | None
    differences: tuple[str, ...]
    history: tuple[FusionHistoryEntry, ...]


@dataclass(frozen=True, slots=True)
class FusionReviewGroup:
    candidate_ids: tuple[UUID, ...]
    candidates: tuple[FusionCandidate, ...]
    proposed_discovery_subject_ids: tuple[UUID, ...]
    confidence: str
    requires_decision: bool
    deterministic_signals: tuple[FusionDeterministicSignal, ...]
    model_suggestion: FusionModelSuggestion | None
    differences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FusionPendingReview:
    merge_run_id: UUID
    candidate_ids: tuple[UUID, ...]
    discovery_subject_ids: tuple[UUID, ...]
    review_reasons: tuple[str, ...]
    stale: bool
    groups: tuple[FusionReviewGroup, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FusionReviewDecision:
    action: FusionReviewAction
    candidate_ids: tuple[UUID, ...]
    target_discovery_subject_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FusionBoard:
    edition_id: UUID
    snapshot_id: UUID | None
    snapshot_version: int | None
    candidate_count: int
    group_count: int
    pending_review_count: int
    groups: tuple[FusionGroup, ...]
    pending_reviews: tuple[FusionPendingReview, ...]
    unstabilized_candidates: tuple[FusionCandidate, ...]
    read_only: bool = False


class FusionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        after_activation: Callable[[UUID], Awaitable[object]] | None = None,
        replan_intake: Callable[[ReconcileDiscoveryParameters], Awaitable[object]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._after_activation = after_activation
        self._replan_intake = replan_intake

    async def get_board(self, edition_id: UUID) -> FusionBoard:
        async with self._uow_factory() as uow:
            return await self._board(uow, edition_id)

    async def resolve_review(
        self,
        edition_id: UUID,
        merge_run_id: UUID,
        *,
        snapshot_version: int,
        decisions: Sequence[FusionReviewDecision],
        actor_id: str,
    ) -> FusionBoard:
        activated = False
        stale_parent = False
        replan: ReconcileDiscoveryParameters | None = None
        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(edition_id)
            self._ensure_writable(edition)
            snapshot = await uow.discovery_snapshots.get_active_for_update(edition_id)
            self._check_version(snapshot, snapshot_version)
            run = await uow.discovery_merge_runs.get(merge_run_id)
            if run is None or run.edition_id != edition_id:
                raise LookupError(f"Unknown fusion merge run {merge_run_id}")
            if run.plan_payload is None or run.intake_id is None:
                raise ValueError("The fusion review has no intake-backed plan")
            if run.validation_status is not MergeValidationStatus.NEEDS_REVIEW:
                raise ValueError("The fusion review is no longer actionable")
            snapshot_id = snapshot.id if snapshot else None
            if run.parent_snapshot_id != snapshot_id:
                # The proposal was planned against another snapshot: its handles
                # would silently rewrite a different state. Retire it and replan
                # the intake against the current snapshot when possible.
                stale_parent = True
                if self._replan_intake is not None:
                    await uow.discovery_merge_runs.mark_resolved(run.id)
                    await uow.commit()
                    replan = ReconcileDiscoveryParameters(
                        intake_id=run.intake_id,
                        edition_id=edition_id,
                        expected_parent_snapshot_id=snapshot_id,
                        actor_id=actor_id,
                    )
            else:
                plan = DiscoveryMergePlanV1.model_validate(run.plan_payload)
                decisions_by_group = self._validate_decisions(plan, run, decisions)
                intake = await uow.discovery_intakes.get(run.intake_id)
                if intake is None:
                    raise RuntimeError("Fusion review references a missing intake")
                candidates = await uow.discovery_candidates.list_for_batch(intake.batch_id)
                active_candidates = await uow.discovery_candidates.list_for_edition(
                    edition_id, include_replaced=False
                )
                delta = build_discovery_delta(intake, candidates)
                existing = {
                    handle: UUID(value)
                    for handle, value in run.handle_map.items()
                    if handle.startswith("X")
                }
                handles = ResolvedMergeHandles(
                    existing=existing,
                    incoming={item.handle: item for item in delta.candidates},
                )
                transformed, resolved_handle_map = self._human_plan(
                    plan, run, decisions_by_group, handles, snapshot
                )
                deferred = any(
                    decision.action is FusionReviewAction.DEFER
                    for decision in decisions_by_group.values()
                )
                if deferred:
                    successor = make_human_merge_run(
                        edition_id=edition_id,
                        parent_snapshot=snapshot,
                        intake_id=run.intake_id,
                        plan=transformed,
                        handle_map=resolved_handle_map,
                        included_subject_ids=tuple(handles.existing.values()),
                        supersedes_merge_run_id=run.id,
                        validation_status=MergeValidationStatus.NEEDS_REVIEW,
                        review_reasons=("human_decision_deferred",),
                    )
                    await uow.discovery_merge_runs.add_if_absent(successor)
                    await uow.discovery_merge_runs.mark_resolved(run.id)
                    await uow.commit()
                else:
                    transformed = _coalesce_existing_targets(transformed)
                    human_run = make_human_merge_run(
                        edition_id=edition_id,
                        parent_snapshot=snapshot,
                        intake_id=run.intake_id,
                        plan=transformed,
                        handle_map=resolved_handle_map,
                        included_subject_ids=tuple(handles.existing.values()),
                        supersedes_merge_run_id=run.id,
                    )
                    applied = apply_discovery_merge_plan(
                        snapshot,
                        delta,
                        transformed,
                        active_candidates,
                        resolved_handles=handles,
                        planner_kind=DiscoveryPlannerKind.HUMAN,
                        edition_id=edition_id,
                        intake_id=run.intake_id,
                        merge_run_id=human_run.id,
                        actor_id=actor_id,
                    )
                    await self._validate_activation(
                        uow, edition_id, run.intake_id, active_candidates, applied.snapshot
                    )
                    await uow.discovery_merge_runs.add_if_absent(human_run)
                    await uow.discovery_subject_identities.add_many_if_absent(applied.identities)
                    await uow.subject_merge_events.append_many(applied.merge_events)
                    if snapshot is not None:
                        await uow.discovery_snapshots.deactivate(snapshot.id)
                    await uow.discovery_snapshots.append(applied.snapshot)
                    await uow.subject_contributions.append_many(applied.contributions)
                    await uow.discovery_merge_runs.mark_resolved(run.id)
                    await uow.commit()
                    activated = True
        if stale_parent:
            if replan is not None and self._replan_intake is not None:
                await self._replan_intake(replan)
            raise FusionSnapshotStaleError(
                "The fusion proposal was planned against an older snapshot"
            )
        if activated:
            await self._after_activation_call(edition_id)
        return await self.get_board(edition_id)

    async def merge(
        self,
        edition_id: UUID,
        *,
        snapshot_version: int,
        discovery_subject_ids: Sequence[UUID],
        actor_id: str,
    ) -> FusionBoard:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(edition_id)
            self._ensure_writable(edition)
            snapshot = await uow.discovery_snapshots.get_active_for_update(edition_id)
            self._check_version(snapshot, snapshot_version)
            if snapshot is None or len(discovery_subject_ids) < 2:
                raise ValueError("Manual merge requires at least two discovery subjects")
            if len(set(discovery_subject_ids)) != len(discovery_subject_ids):
                raise ValueError("Manual merge discovery subjects must be distinct")
            if not set(discovery_subject_ids) <= {item.subject_id for item in snapshot.subjects}:
                raise ValueError("Manual merge discovery subjects must belong to the snapshot")
            candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            run = make_structural_merge_run(
                edition_id=edition_id,
                parent_snapshot=snapshot,
                operation="merge",
                subject_ids=discovery_subject_ids,
                actor_id=actor_id,
            )
            applied = apply_structural_subject_merge(
                snapshot,
                candidates,
                edition_id=edition_id,
                subject_ids=discovery_subject_ids,
                merge_run_id=run.id,
                actor_id=actor_id,
            )
            await self._validate_activation(uow, edition_id, None, candidates, applied.snapshot)
            await uow.discovery_merge_runs.add_if_absent(run)
            await uow.discovery_subject_identities.add_many_if_absent(applied.identities)
            await uow.subject_merge_events.append_many(applied.merge_events)
            await uow.discovery_snapshots.deactivate(snapshot.id)
            await uow.discovery_snapshots.append(applied.snapshot)
            await uow.commit()
        await self._after_activation_call(edition_id)
        return await self.get_board(edition_id)

    async def split(
        self,
        edition_id: UUID,
        *,
        snapshot_version: int,
        discovery_subject_id: UUID,
        candidate_ids: Sequence[UUID],
        actor_id: str,
    ) -> FusionBoard:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(edition_id)
            self._ensure_writable(edition)
            snapshot = await uow.discovery_snapshots.get_active_for_update(edition_id)
            self._check_version(snapshot, snapshot_version)
            if snapshot is None:
                raise ValueError("Manual split requires an active snapshot")
            run = make_structural_merge_run(
                edition_id=edition_id,
                parent_snapshot=snapshot,
                operation="split",
                subject_ids=(discovery_subject_id,),
                actor_id=actor_id,
                candidate_ids=candidate_ids,
            )
            candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            applied = apply_structural_subject_split(
                snapshot,
                candidates,
                edition_id=edition_id,
                subject_id=discovery_subject_id,
                candidate_ids=candidate_ids,
                merge_run_id=run.id,
            )
            await self._validate_activation(uow, edition_id, None, candidates, applied.snapshot)
            await uow.discovery_merge_runs.add_if_absent(run)
            await uow.discovery_subject_identities.add_many_if_absent(applied.identities)
            await uow.discovery_snapshots.deactivate(snapshot.id)
            await uow.discovery_snapshots.append(applied.snapshot)
            await uow.commit()
        await self._after_activation_call(edition_id)
        return await self.get_board(edition_id)

    async def _board(self, uow: UnitOfWork, edition_id: UUID) -> FusionBoard:
        edition = await uow.editions.get(edition_id)
        if edition is None:
            raise LookupError(f"Unknown edition {edition_id}")
        snapshot = await uow.discovery_snapshots.get_active(edition_id)
        active = await uow.discovery_candidates.list_for_edition(
            edition_id, include_replaced=False
        )
        historical = await uow.discovery_candidates.list_for_edition(
            edition_id, include_replaced=True
        )
        active_map = {candidate.id: candidate for candidate in active}
        # Pending reviews may name a candidate replaced since; keep it readable.
        any_map = {candidate.id: candidate for candidate in historical}
        runs = list(await uow.discovery_merge_runs.list_for_edition(edition_id))
        runs_by_id = {run.id: run for run in runs}
        identities = {
            identity.id: identity
            for identity in await uow.discovery_subject_identities.list_for_edition(edition_id)
        }
        events = list(await uow.subject_merge_events.list_for_edition(edition_id))

        groups: list[FusionGroup] = []
        subjects = snapshot.subjects if snapshot else ()
        for subject in subjects:
            contributions = await uow.discovery_subject_identities.contribution_closure(
                subject.subject_id
            )
            groups.append(
                _group_view(
                    subject,
                    active_map,
                    contributions=contributions,
                    runs_by_id=runs_by_id,
                    identity=identities.get(subject.subject_id),
                    events=events,
                )
            )

        subjects_by_id = {subject.subject_id: subject for subject in subjects}
        reviews: list[FusionPendingReview] = []
        for run in runs:
            if (
                run.validation_status is not MergeValidationStatus.NEEDS_REVIEW
                or run.plan_payload is None
                or run.intake_id is None
            ):
                continue
            if await uow.discovery_snapshots.get_for_intake(run.intake_id) is not None:
                continue
            reviews.append(
                _pending_review(
                    run,
                    any_map,
                    active_map,
                    subjects_by_id,
                    runs_by_id,
                    stale=run.parent_snapshot_id != (snapshot.id if snapshot else None),
                )
            )

        placed = {
            reference.candidate_id
            for subject in subjects
            for reference in subject.member_references
        } | {candidate_id for review in reviews for candidate_id in review.candidate_ids}
        unstabilized = tuple(
            _candidate_view(candidate)
            for candidate in sorted(active, key=lambda item: str(item.id))
            if candidate.id not in placed
        )
        return FusionBoard(
            edition_id=edition_id,
            snapshot_id=snapshot.id if snapshot else None,
            snapshot_version=snapshot.version if snapshot else None,
            candidate_count=len(active),
            group_count=len(groups),
            pending_review_count=len(reviews),
            groups=tuple(groups),
            pending_reviews=tuple(reviews),
            unstabilized_candidates=unstabilized,
            read_only=edition.state is EditionStatus.ARCHIVED,
        )

    @staticmethod
    def _ensure_writable(edition: Edition | None) -> None:
        if edition is None:
            raise LookupError("Unknown edition")
        if edition.state is EditionStatus.ARCHIVED:
            raise FusionEditionArchivedError("Archived editions cannot be modified")

    @staticmethod
    def _check_version(snapshot: DiscoverySnapshot | None, version: int) -> None:
        # Version 0 designates "no snapshot yet" (a review parked on bootstrap).
        current = snapshot.version if snapshot is not None else 0
        if current != version:
            raise FusionSnapshotStaleError("The active fusion snapshot is stale")

    @staticmethod
    def _validate_decisions(
        plan: DiscoveryMergePlanV1,
        run: DiscoveryMergeRun,
        decisions: Sequence[FusionReviewDecision],
    ) -> dict[int, FusionReviewDecision]:
        expected: dict[frozenset[UUID], int] = {}
        for index, group in enumerate(plan.groups):
            if not _needs_decision(group):
                continue
            ids = frozenset(
                UUID(run.handle_map[handle])
                for handle in group.incoming_candidate_handles
                if handle in run.handle_map
            )
            expected[ids] = index
        result: dict[int, FusionReviewDecision] = {}
        for decision in decisions:
            normalized = FusionReviewDecision(
                FusionReviewAction(decision.action),
                tuple(sorted(set(decision.candidate_ids), key=str)),
                decision.target_discovery_subject_id,
            )
            key = frozenset(normalized.candidate_ids)
            if key not in expected:
                raise ValueError("Each decision must match exactly one review group")
            index = expected[key]
            if index in result:
                raise ValueError("A fusion review group may be decided only once")
            if (
                normalized.action is FusionReviewAction.ATTACH
                and normalized.target_discovery_subject_id is None
            ):
                raise ValueError("attach requires target_discovery_subject_id")
            result[index] = normalized
        if set(result) != set(expected.values()):
            raise ValueError("Every review group requires exactly one decision")
        return result

    @staticmethod
    def _human_plan(
        plan: DiscoveryMergePlanV1,
        run: DiscoveryMergeRun,
        decisions: dict[int, FusionReviewDecision],
        handles: ResolvedMergeHandles,
        snapshot: DiscoverySnapshot | None,
    ) -> tuple[DiscoveryMergePlanV1, dict[str, str]]:
        transformed = plan.model_copy(deep=True)
        handle_map = dict(run.handle_map)
        target_handles = {value: key for key, value in handle_map.items() if key.startswith("X")}
        snapshot_subject_ids = {
            subject.subject_id for subject in (snapshot.subjects if snapshot else ())
        }
        # Reverse order keeps earlier indexes valid while `separate` expands a group.
        for index in sorted(decisions, reverse=True):
            decision = decisions[index]
            group = transformed.groups[index]
            flags = [*group.flags, HUMAN_DECIDED_FLAG]
            if decision.action is FusionReviewAction.ACCEPT:
                transformed.groups[index] = group.model_copy(update={"flags": flags})
            elif decision.action is FusionReviewAction.SEPARATE:
                transformed.groups[index : index + 1] = [
                    group.model_copy(
                        update={
                            "existing_subject_handles": [],
                            "incoming_candidate_handles": [candidate_handle],
                            "disposition": MergeDisposition.APPLY,
                            "confidence": MergeConfidence.HIGH,
                            "flags": flags,
                        }
                    )
                    for candidate_handle in group.incoming_candidate_handles
                ]
            elif decision.action is FusionReviewAction.ATTACH:
                target = decision.target_discovery_subject_id
                if target is None or target not in snapshot_subject_ids:
                    raise ValueError(
                        "attach requires a target_discovery_subject_id of the active snapshot"
                    )
                target_handle = target_handles.get(str(target))
                if target_handle is None:
                    # Internal-only handle for a subject outside the blocking set;
                    # persisted in the HUMAN run, never exposed by the board.
                    target_handle = f"X-fusion-{target}"
                    handle_map[target_handle] = str(target)
                    handles.existing[target_handle] = target
                transformed.groups[index] = group.model_copy(
                    update={
                        "existing_subject_handles": [target_handle],
                        "disposition": MergeDisposition.APPLY,
                        "confidence": MergeConfidence.HIGH,
                        "flags": flags,
                    }
                )
            # DEFER keeps the group untouched: it stays in review.
        return transformed, handle_map

    async def _after_activation_call(self, edition_id: UUID) -> None:
        if self._after_activation is not None:
            await self._after_activation(edition_id)

    @staticmethod
    async def _validate_activation(
        uow: UnitOfWork,
        edition_id: UUID,
        intake_id: UUID | None,
        candidates: Sequence[DiscoveryCandidate],
        snapshot: DiscoverySnapshot,
    ) -> None:
        intakes = await uow.discovery_intakes.list_for_edition(edition_id)
        intaked_batches = {item.batch_id for item in intakes}
        pending: set[UUID] = set()
        for intake in intakes:
            if intake.id == intake_id:
                continue
            if await uow.discovery_snapshots.get_for_intake(intake.id) is None:
                pending.update(
                    candidate.id
                    for candidate in candidates
                    if candidate.discovery_batch_id == intake.batch_id
                )
        validate_candidate_coverage(
            [
                candidate
                for candidate in candidates
                if candidate.discovery_batch_id in intaked_batches
            ],
            snapshot,
            pending_candidate_ids=pending,
        )


def _needs_decision(group: DiscoveryMergeGroup) -> bool:
    return requires_review(group) and HUMAN_DECIDED_FLAG not in group.flags


def _coalesce_existing_targets(plan: DiscoveryMergePlanV1) -> DiscoveryMergePlanV1:
    """Collapse human-decided groups that target the same current subject.

    The product contract permits attaching any review group to any active
    discovery subject. Several decisions may therefore choose the same target,
    while the cumulative plan invariant allows an existing subject in only one
    group. Coalescing expresses those decisions as one auditable group without
    weakening that invariant.
    """
    grouped: list[list[DiscoveryMergeGroup]] = []
    for group in plan.groups:
        handles = set(group.existing_subject_handles)
        if not handles:
            grouped.append([group])
            continue
        matching = [
            index
            for index, component in enumerate(grouped)
            if handles
            & {
                handle
                for item in component
                for handle in item.existing_subject_handles
            }
        ]
        if not matching:
            grouped.append([group])
            continue
        target = matching[0]
        grouped[target].append(group)
        for index in reversed(matching[1:]):
            grouped[target].extend(grouped.pop(index))

    return plan.model_copy(
        update={
            "groups": [
                component[0] if len(component) == 1 else _combine_groups(component)
                for component in grouped
            ]
        }
    )


def _combine_groups(groups: Sequence[DiscoveryMergeGroup]) -> DiscoveryMergeGroup:
    confidence_rank = {
        MergeConfidence.HIGH: 0,
        MergeConfidence.MEDIUM: 1,
        MergeConfidence.LOW: 2,
    }
    evidence_fields = tuple(MergeEvidence.model_fields)
    evidence = MergeEvidence.model_validate(
        {
            field_name: list(
                dict.fromkeys(
                    value
                    for group in groups
                    for value in getattr(group.evidence, field_name)
                )
            )
            for field_name in evidence_fields
        }
    )
    return groups[0].model_copy(
        update={
            "existing_subject_handles": list(
                dict.fromkeys(
                    handle for group in groups for handle in group.existing_subject_handles
                )
            ),
            "incoming_candidate_handles": list(
                dict.fromkeys(
                    handle for group in groups for handle in group.incoming_candidate_handles
                )
            ),
            "confidence": max(
                (group.confidence for group in groups), key=confidence_rank.__getitem__
            ),
            "disposition": (
                MergeDisposition.REVIEW
                if any(group.disposition is MergeDisposition.REVIEW for group in groups)
                else MergeDisposition.APPLY
            ),
            "rationale": " | ".join(
                dict.fromkeys(group.rationale for group in groups if group.rationale)
            ),
            "evidence": evidence,
            "flags": list(
                dict.fromkeys(flag for group in groups for flag in group.flags)
            ),
        }
    )


def _candidate_view(candidate: DiscoveryCandidate) -> FusionCandidate:
    topic = candidate.candidate
    return FusionCandidate(
        id=candidate.id,
        discovery_run_id=candidate.discovery_run_id,
        discovery_batch_id=candidate.discovery_batch_id,
        supersedes_candidate_id=candidate.supersedes_candidate_id,
        title=topic.title,
        summary=topic.summary,
        event_date=_candidate_date(topic),
        actors=topic.actors,
        campaigns=topic.campaigns,
        malware=topic.malware,
        cves=topic.cves,
        iocs=_ioc_values(topic),
        countries=topic.countries,
        sectors=topic.sectors,
        likely_artifacts=topic.likely_artifacts,
        publications=tuple(
            FusionPublication(
                id=source.id,
                url=source.url,
                canonical_url=source.canonical_url,
                title=source.title,
                publisher=source.publisher,
                published_at=source.published_at,
            )
            for source in topic.sources
        ),
    )


def _group_view(
    subject: DiscoverySubject,
    active_map: Mapping[UUID, DiscoveryCandidate],
    *,
    contributions: Sequence[SubjectContribution],
    runs_by_id: Mapping[UUID, DiscoveryMergeRun],
    identity: DiscoverySubjectIdentity | None,
    events: Sequence[SubjectMergeEvent],
) -> FusionGroup:
    members = tuple(
        active_map[reference.candidate_id]
        for reference in subject.member_references
        if reference.candidate_id in active_map
    )
    signals, differences = _signals(members)
    history = _history(subject.subject_id, contributions, runs_by_id, identity, events)
    latest = max(
        (item for item in contributions if item.subject_id == subject.subject_id),
        key=lambda item: (item.first_seen_version, item.created_at),
        default=None,
    )
    latest_run = runs_by_id.get(latest.merge_run_id) if latest is not None else None
    latest_group = (
        _plan_group(latest_run, latest.merge_group_index)
        if latest is not None and latest_run is not None
        else None
    )
    # A human restructuring supersedes the plan that first assembled the group:
    # keeping the planner's confidence and suggestion would credit the model for
    # a decision the analyst has since overruled.
    restructured = max(
        (entry for entry in history if entry.action in _STRUCTURAL_ACTIONS),
        key=lambda item: item.created_at,
        default=None,
    )
    if restructured is not None and (
        latest is None or restructured.created_at >= latest.created_at
    ):
        latest_run = (
            runs_by_id.get(restructured.merge_run_id)
            if restructured.merge_run_id is not None
            else None
        )
        latest_group = None
    return FusionGroup(
        discovery_subject_id=subject.subject_id,
        title=subject.canonical_title,
        summary=subject.canonical_summary,
        candidate_ids=tuple(item.id for item in members),
        candidates=tuple(_candidate_view(item) for item in members),
        confidence=latest_group.confidence.value if latest_group is not None else None,
        origin=latest_run.planner_kind.value if latest_run is not None else None,
        resolution_state="established",
        deterministic_signals=signals,
        model_suggestion=(
            _model_suggestion(latest_run, latest_group, runs_by_id)
            if latest_run is not None and latest_group is not None
            else None
        ),
        differences=differences,
        history=history,
    )


def _history(
    subject_id: UUID,
    contributions: Sequence[SubjectContribution],
    runs_by_id: Mapping[UUID, DiscoveryMergeRun],
    identity: DiscoverySubjectIdentity | None,
    events: Sequence[SubjectMergeEvent],
) -> tuple[FusionHistoryEntry, ...]:
    def kind(run_id: UUID | None) -> str | None:
        run = runs_by_id.get(run_id) if run_id is not None else None
        return run.planner_kind.value if run is not None else None

    entries: list[FusionHistoryEntry] = []
    if identity is not None:
        entries.append(
            FusionHistoryEntry(
                action="split_created"
                if identity.origin_key.startswith("split:")
                else "created",
                merge_run_id=identity.created_by_merge_run_id,
                planner_kind=kind(identity.created_by_merge_run_id),
                actor_id=None,
                candidate_ids=(),
                created_at=identity.created_at,
            )
        )
    for contribution in contributions:
        entries.append(
            FusionHistoryEntry(
                action="candidate_added",
                merge_run_id=contribution.merge_run_id,
                planner_kind=kind(contribution.merge_run_id),
                actor_id=None,
                candidate_ids=(contribution.candidate_id,),
                created_at=contribution.created_at,
            )
        )
    for event in events:
        if event.into_subject_id != subject_id:
            continue
        entries.append(
            FusionHistoryEntry(
                action="merged",
                merge_run_id=event.merge_run_id,
                planner_kind=kind(event.merge_run_id),
                actor_id=event.actor_id,
                candidate_ids=(),
                created_at=event.created_at,
            )
        )
    for run in runs_by_id.values():
        payload = run.plan_payload or {}
        subject_ids = payload.get("subject_ids")
        if (
            payload.get("operation") == "split"
            and isinstance(subject_ids, list)
            and str(subject_id) in subject_ids
        ):
            raw_ids = payload.get("candidate_ids")
            actor = payload.get("actor_id")
            entries.append(
                FusionHistoryEntry(
                    action="split",
                    merge_run_id=run.id,
                    planner_kind=run.planner_kind.value,
                    actor_id=actor if isinstance(actor, str) else None,
                    candidate_ids=tuple(
                        UUID(value) for value in raw_ids if isinstance(value, str)
                    )
                    if isinstance(raw_ids, list)
                    else (),
                    created_at=run.created_at,
                )
            )
    return tuple(sorted(entries, key=lambda item: item.created_at))


def _pending_review(
    run: DiscoveryMergeRun,
    any_map: Mapping[UUID, DiscoveryCandidate],
    active_map: Mapping[UUID, DiscoveryCandidate],
    subjects_by_id: Mapping[UUID, DiscoverySubject],
    runs_by_id: Mapping[UUID, DiscoveryMergeRun],
    *,
    stale: bool,
) -> FusionPendingReview:
    plan = DiscoveryMergePlanV1.model_validate(run.plan_payload)
    groups: list[FusionReviewGroup] = []
    for group in plan.groups:
        candidate_ids = tuple(
            UUID(run.handle_map[handle])
            for handle in group.incoming_candidate_handles
            if handle in run.handle_map
        )
        subject_ids = tuple(
            UUID(run.handle_map[handle])
            for handle in group.existing_subject_handles
            if handle in run.handle_map
        )
        incoming = [any_map[value] for value in candidate_ids if value in any_map]
        target_members = [
            active_map[reference.candidate_id]
            for value in subject_ids
            if value in subjects_by_id
            for reference in subjects_by_id[value].member_references
            if reference.candidate_id in active_map
        ]
        signals, differences = _signals([*incoming, *target_members])
        groups.append(
            FusionReviewGroup(
                candidate_ids=candidate_ids,
                candidates=tuple(_candidate_view(item) for item in incoming),
                proposed_discovery_subject_ids=subject_ids,
                confidence=group.confidence.value,
                requires_decision=_needs_decision(group),
                deterministic_signals=signals,
                model_suggestion=_model_suggestion(run, group, runs_by_id),
                differences=differences,
            )
        )
    return FusionPendingReview(
        merge_run_id=run.id,
        candidate_ids=tuple(
            candidate_id for group in groups for candidate_id in group.candidate_ids
        ),
        discovery_subject_ids=tuple(
            dict.fromkeys(
                subject_id
                for group in groups
                for subject_id in group.proposed_discovery_subject_ids
            )
        ),
        review_reasons=run.review_reasons,
        stale=stale,
        groups=tuple(groups),
        created_at=run.created_at,
    )


def _plan_group(run: DiscoveryMergeRun, index: int) -> DiscoveryMergeGroup | None:
    if run.plan_payload is None or "operation" in run.plan_payload:
        return None
    try:
        plan = DiscoveryMergePlanV1.model_validate(run.plan_payload)
    except ValueError:
        return None
    return plan.groups[index] if 0 <= index < len(plan.groups) else None


def _model_suggestion(
    run: DiscoveryMergeRun,
    group: DiscoveryMergeGroup,
    runs_by_id: Mapping[UUID, DiscoveryMergeRun] | None = None,
) -> FusionModelSuggestion | None:
    """Product-facing synthesis of a persisted model plan; never a transcript."""
    source = run
    seen: set[UUID] = set()
    while (
        source.planner_kind is not DiscoveryPlannerKind.CHATGPT
        and source.merge_model_run_id is None
        and source.supersedes_merge_run_id is not None
        and runs_by_id is not None
        and source.id not in seen
    ):
        seen.add(source.id)
        previous = runs_by_id.get(source.supersedes_merge_run_id)
        if previous is None:
            break
        source = previous
    if (
        source.planner_kind is not DiscoveryPlannerKind.CHATGPT
        and source.merge_model_run_id is None
    ):
        return None
    if group.existing_subject_handles:
        recommendation = "attach"
    elif len(group.incoming_candidate_handles) > 1:
        recommendation = "merge"
    else:
        recommendation = "separate"
    summary = " ".join(group.rationale.split())
    if len(summary) > _MODEL_SUMMARY_LIMIT:
        summary = summary[: _MODEL_SUMMARY_LIMIT - 1].rstrip() + "…"
    return FusionModelSuggestion(recommendation, summary)


_SHARED_FIELDS: tuple[tuple[str, str], ...] = (
    ("shared_actor", "actors"),
    ("shared_campaign", "campaigns"),
    ("shared_malware", "malware"),
    ("shared_cve", "cves"),
    ("shared_country", "countries"),
    ("shared_sector", "sectors"),
    ("shared_artifact", "likely_artifacts"),
)
_DIFFERENCE_LABELS = {
    "actors": "Acteur",
    "campaigns": "Campagne",
    "malware": "Malware",
    "cves": "CVE",
}


def _signals(
    candidates: Sequence[DiscoveryCandidate],
) -> tuple[tuple[FusionDeterministicSignal, ...], tuple[str, ...]]:
    """Deterministic, locally computed reasons for and against a grouping."""
    candidates = sorted(
        {candidate.id: candidate for candidate in candidates}.values(),
        key=lambda candidate: str(candidate.id),
    )
    if len(candidates) < 2:
        return (), ()
    signals: list[FusionDeterministicSignal] = []
    differences: list[str] = []

    def shared(kind: str, values_by_candidate: Mapping[UUID, Mapping[str, str]]) -> None:
        owners: dict[str, list[UUID]] = {}
        labels: dict[str, set[str]] = {}
        for candidate_id, values in values_by_candidate.items():
            for key, label in values.items():
                owners.setdefault(key, []).append(candidate_id)
                labels.setdefault(key, set()).add(label)
        for key in sorted(owners):
            if len(owners[key]) >= 2:
                display = min(labels[key], key=lambda value: (value.casefold(), value))
                signals.append(
                    FusionDeterministicSignal(
                        kind,
                        display,
                        tuple(sorted(owners[key], key=str)),
                    )
                )

    shared("shared_canonical_url", {item.id: _urls(item.candidate) for item in candidates})
    shared("shared_source_domain", {item.id: _domains(item.candidate) for item in candidates})
    for left, right in combinations(candidates, 2):
        for left_source in left.candidate.sources:
            match = next(
                (
                    right_source
                    for right_source in right.candidate.sources
                    if right_source.canonical_url != left_source.canonical_url
                    and same_publication(left_source, right_source)
                ),
                None,
            )
            if match is not None:
                signals.append(
                    FusionDeterministicSignal(
                        "same_publication", left_source.title, (left.id, right.id)
                    )
                )
                break
    for kind, field_name in _SHARED_FIELDS:
        shared(
            kind,
            {item.id: _normalized(getattr(item.candidate, field_name)) for item in candidates},
        )
    shared("shared_ioc", {item.id: _normalized(_ioc_values(item.candidate)) for item in candidates})

    best_ratio, best_pair = 0.0, (candidates[0].id, candidates[1].id)
    for left, right in combinations(candidates, 2):
        ratio = SequenceMatcher(
            None, left.candidate.title.casefold(), right.candidate.title.casefold()
        ).ratio()
        if ratio > best_ratio:
            best_ratio, best_pair = ratio, (left.id, right.id)
    if best_ratio >= _TITLE_SIMILARITY_THRESHOLD:
        signals.append(
            FusionDeterministicSignal("title_similarity", f"{best_ratio:.2f}", best_pair)
        )

    dated = [(item, _candidate_date(item.candidate)) for item in candidates]
    known = [(item, value) for item, value in dated if value is not None]
    if len(known) >= 2:
        earliest = min(value for _, value in known)
        latest = max(value for _, value in known)
        gap = (latest - earliest).days
        if gap <= _DATE_PROXIMITY_DAYS:
            signals.append(
                FusionDeterministicSignal(
                    "date_proximity", f"{gap} jour(s)", tuple(item.id for item, _ in known)
                )
            )
        if gap:
            differences.append(f"Les dates diffèrent de {gap} jour(s)")

    for field_name, label in _DIFFERENCE_LABELS.items():
        values = {item.id: _normalized(getattr(item.candidate, field_name)) for item in candidates}
        for item in candidates:
            for key, display in values[item.id].items():
                holders = [other for other in candidates if key in values[other.id]]
                if len(holders) < len(candidates):
                    differences.append(
                        f"{label} « {display} » absent de "
                        + ", ".join(
                            f"« {other.candidate.title} »"
                            for other in candidates
                            if key not in values[other.id]
                        )
                    )
    ioc_counts = {item.id: len(_ioc_values(item.candidate)) for item in candidates}
    if len(set(ioc_counts.values())) > 1:
        differences.append(
            "IOC visibles : "
            + ", ".join(
                f"« {item.candidate.title} » {ioc_counts[item.id]}" for item in candidates
            )
        )
    return tuple(signals), tuple(dict.fromkeys(differences))


def _normalized(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key = " ".join(value.casefold().split())
        if key:
            result.setdefault(key, value.strip())
    return result


def _ioc_values(candidate: CandidateTopic) -> tuple[str, ...]:
    values = [*candidate.iocs]
    values.extend(ioc.normalized_value or ioc.raw_value for ioc in candidate.provisional_iocs)
    return tuple(dict.fromkeys(value for value in values if value))


def _urls(candidate: CandidateTopic) -> dict[str, str]:
    return {source.canonical_url: source.canonical_url for source in candidate.sources}


def _domains(candidate: CandidateTopic) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in candidate.sources:
        host = (urlsplit(source.canonical_url).hostname or "").casefold()
        domain = host.removeprefix("www.")
        if domain:
            result[domain] = domain
    return result


def _candidate_date(candidate: CandidateTopic) -> date | None:
    if candidate.event_date is not None:
        return candidate.event_date
    dates = [
        value
        for source in candidate.sources
        for value in (source.event_date, source.published_at)
        if value is not None
    ]
    return min(dates) if dates else None
