from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.discovery.cumulative.apply import apply_discovery_merge_plan
from cti_app.application.discovery.cumulative.context import (
    DiscoveryBlockingStrategy,
    _candidate_content,
    build_discovery_delta,
    build_merge_handles,
)
from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.errors import (
    DiscoveryMergeNeedsReview,
    DiscoverySnapshotStaleError,
    MergePlanInvalidError,
)
from cti_app.application.discovery.cumulative.merge_runs import make_merge_run
from cti_app.application.discovery.cumulative.planners import HeuristicMergePlanner
from cti_app.application.discovery.cumulative.types import (
    DiscoveryMergePlanner,
    MergeHandleLabel,
    PlannedDiscoveryMerge,
)
from cti_app.application.discovery.cumulative.validation import (
    apply_editorial_duplicate_guard,
    merge_plan_review_reasons,
    validate_candidate_coverage,
)
from cti_app.application.model_gateway import ExternalModelBlockedError
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.discovery import CandidateTopic, DiscoveryBatch
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMergePlanV1,
    DiscoveryMergeRun,
    DiscoverySnapshot,
    MergeValidationStatus,
    canonical_sha256,
)
from cti_app.domain.editorial import CandidateReference, EditorialGroupStatus
from cti_app.logging import get_correlation_id

logger = logging.getLogger(__name__)


def _handle_label(handle: str, candidate: CandidateTopic) -> MergeHandleLabel:
    return MergeHandleLabel(
        handle=handle,
        title=candidate.title,
        summary=candidate.summary,
        source_urls=tuple(
            source.canonical_url
            for source in sorted(candidate.sources, key=lambda item: item.canonical_url)
        ),
    )


class CumulativeDiscoveryService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        planner: DiscoveryMergePlanner | None = None,
        blocking_strategy: DiscoveryBlockingStrategy | None = None,
        after_activation: Callable[[UUID], Awaitable[object]] | None = None,
        diagnostics: DiagnosticsLog | None = None,
        replan_intake: Callable[[ReconcileDiscoveryParameters], Awaitable[object]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._planner = planner or HeuristicMergePlanner()
        self._blocking = blocking_strategy or DiscoveryBlockingStrategy()
        self._after_activation = after_activation
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        # Replanning calls the merge model, so it cannot run inside the request
        # that discovered the staleness; the host hands over a way to queue it.
        self._replan_intake = replan_intake

    def set_replan_intake(
        self, replan_intake: Callable[[ReconcileDiscoveryParameters], Awaitable[object]]
    ) -> None:
        """Wire replanning after construction: it needs the job service, which
        is itself built from this service's job registrations."""
        self._replan_intake = replan_intake

    async def ingest_batch(
        self,
        batch: DiscoveryBatch,
        *,
        input_mode: DiscoveryInputMode,
        actor_id: str,
    ) -> tuple[DiscoveryIntake, bool]:
        raw_hash = batch.report_sha256 or batch.request_hash
        async with self._uow_factory() as uow:
            candidates = await uow.discovery_candidates.list_for_batch(batch.id)
            parsed_hash = canonical_sha256(
                [
                    _candidate_content(candidate.candidate, candidate_id=candidate.id)
                    for candidate in sorted(
                        candidates, key=lambda item: (item.position, str(item.id))
                    )
                ]
            )
            intake_hash = canonical_sha256(
                {
                    "raw_report_hash": raw_hash,
                    "parsed_report_hash": parsed_hash,
                    "edition_id": str(batch.edition_id),
                    "input_mode": input_mode.value,
                    "source_mode": batch.source_mode.value,
                    "complementary_axis": batch.complementary_axis,
                }
            )
            existing = await uow.discovery_intakes.get_by_batch(batch.id)
            if existing is not None:
                return existing, True
            sequence = await uow.discovery_intakes.next_sequence(batch.edition_id)
            intake = DiscoveryIntake(
                id=uuid5(NAMESPACE_URL, f"discovery-intake:{batch.edition_id}:{intake_hash}"),
                edition_id=batch.edition_id,
                sequence=sequence,
                input_mode=input_mode,
                raw_report_hash=raw_hash,
                parsed_report_hash=parsed_hash,
                intake_hash=intake_hash,
                research_model_run_id=batch.discovery_model_run_id,
                source_mode=batch.source_mode,
                complementary_axis=batch.complementary_axis,
                batch_id=batch.id,
                created_by=actor_id,
            )
            inserted = await uow.discovery_intakes.add_if_absent(intake)
            if not inserted:
                canonical = await uow.discovery_intakes.get_by_batch(batch.id)
                if canonical is None:
                    raise RuntimeError("Discovery intake conflict without canonical row")
                return canonical, True
            await uow.commit()
            return intake, False

    async def reconcile_intake(
        self,
        intake_id: UUID,
        *,
        expected_parent_snapshot_id: UUID | None,
        actor_id: str,
        rebase_count: int = 0,
        planner_override: DiscoveryMergePlanner | None = None,
    ) -> DiscoverySnapshot:
        async with self._uow_factory() as uow:
            already_applied = await uow.discovery_snapshots.get_for_intake(intake_id)
            if already_applied is not None:
                return already_applied
            intake = await uow.discovery_intakes.get(intake_id)
            if intake is None:
                raise LookupError(f"Unknown discovery intake {intake_id}")
            batch = await uow.discovery_batches.get(intake.batch_id)
            if batch is None:
                raise RuntimeError("Discovery intake references a missing audit batch")
            candidates = await uow.discovery_candidates.list_for_batch(intake.batch_id)
            active_candidates = await uow.discovery_candidates.list_for_edition(
                intake.edition_id, include_replaced=False
            )
            parent = await uow.discovery_snapshots.get_active_for_update(intake.edition_id)
            already_applied = await uow.discovery_snapshots.get_for_intake(intake_id)
            if already_applied is not None:
                return already_applied
            current_parent_id = parent.id if parent else None
            if expected_parent_snapshot_id != current_parent_id:
                if rebase_count >= 2:
                    delta = build_discovery_delta(intake, candidates)
                    handles = build_merge_handles(parent, delta)
                    run = make_merge_run(
                        edition_id=intake.edition_id,
                        parent_snapshot=parent,
                        intake=intake,
                        delta=delta,
                        planner=self._planner,
                        handles=handles,
                        validation_status=MergeValidationStatus.NEEDS_REVIEW,
                        review_reasons=("merge_rebase_limit_reached",),
                        rebase_count=2,
                    )
                    await uow.discovery_merge_runs.add_if_absent(run)
                    await uow.commit()
                    raise DiscoverySnapshotStaleError("merge_rebase_limit_reached")
                rebase_count += 1

            delta = build_discovery_delta(intake, candidates)
            groups = await uow.editorial_groups.list_for_edition(intake.edition_id)
            editorial_subject_ids = {
                group.discovery_subject_id
                for group in groups
                if group.discovery_subject_id is not None
            }
            recent_subject_ids = set(
                await uow.subject_contributions.list_recent_subject_ids(
                    intake.edition_id,
                    minimum_snapshot_version=max(1, (parent.version if parent else 1) - 2),
                )
            )
            included = self._blocking.select(
                parent,
                delta,
                editorial_subject_ids=editorial_subject_ids,
                recent_subject_ids=recent_subject_ids,
            )
            handles = build_merge_handles(parent, delta, included_subjects=included)
            planner: DiscoveryMergePlanner = (
                HeuristicMergePlanner() if parent is None else (planner_override or self._planner)
            )
            excluded_subject_count = len(parent.subjects) - len(handles.existing) if parent else 0
            cache_key_run = make_merge_run(
                edition_id=intake.edition_id,
                parent_snapshot=parent,
                intake=intake,
                delta=delta,
                planner=planner,
                handles=handles,
                excluded_subject_count=excluded_subject_count,
                blocking_version=self._blocking.version,
                rebase_count=rebase_count,
            )
            cached = await uow.discovery_merge_runs.get_by_input_hash(
                cache_key_run.merge_input_hash
            )
            if cached is not None and cached.plan_payload is not None:
                if cached.validation_status is MergeValidationStatus.NEEDS_REVIEW:
                    self._diagnostics.record(
                        event="merge.needs_review",
                        run_id=cached.id,
                        stage="discovery_merge",
                        correlation_id=get_correlation_id(),
                        edition_id=str(intake.edition_id),
                        intake_id=str(intake.id),
                        cached=True,
                        review_reasons=list(cached.review_reasons),
                    )
                    raise DiscoveryMergeNeedsReview(cached.id, cached.review_reasons)
                outcome = PlannedDiscoveryMerge(
                    DiscoveryMergePlanV1.model_validate(cached.plan_payload),
                    merge_model_run_id=cached.merge_model_run_id,
                    raw_output_reference=cached.raw_output_reference,
                    normalized_output_reference=cached.normalized_output_reference,
                    validation_status=cached.validation_status,
                    warnings=cached.warnings,
                )
            else:
                try:
                    outcome = await planner.plan(
                        parent,
                        delta,
                        handles,
                        edition_id=intake.edition_id,
                        external_llm_allowed=batch.external_llm_allowed,
                        sensitivity=batch.sensitivity,
                    )
                except ExternalModelBlockedError:
                    run = make_merge_run(
                        edition_id=intake.edition_id,
                        parent_snapshot=parent,
                        intake=intake,
                        delta=delta,
                        planner=planner,
                        handles=handles,
                        validation_status=MergeValidationStatus.NEEDS_REVIEW,
                        review_reasons=("external_merge_not_allowed",),
                        excluded_subject_count=excluded_subject_count,
                        blocking_version=self._blocking.version,
                        rebase_count=rebase_count,
                    )
                    await uow.discovery_merge_runs.add_if_absent(run)
                    await uow.commit()
                    raise DiscoveryMergeNeedsReview(run.id, run.review_reasons) from None
                except MergePlanInvalidError as exc:
                    outcome = PlannedDiscoveryMerge(
                        DiscoveryMergePlanV1(groups=[]),
                        merge_model_run_id=exc.merge_model_run_id,
                        raw_output_reference=exc.raw_output_reference,
                        normalized_output_reference=exc.normalized_output_reference,
                        validation_status=MergeValidationStatus.NEEDS_REVIEW,
                    )
                    run = make_merge_run(
                        edition_id=intake.edition_id,
                        parent_snapshot=parent,
                        intake=intake,
                        delta=delta,
                        planner=planner,
                        handles=handles,
                        outcome=outcome,
                        review_reasons=("plan_invalid_after_repair",),
                        excluded_subject_count=excluded_subject_count,
                        blocking_version=self._blocking.version,
                        rebase_count=rebase_count,
                    )
                    await uow.discovery_merge_runs.add_if_absent(run)
                    await uow.commit()
                    self._diagnostics.record_failure(
                        event="merge.plan_invalid",
                        run_id=run.id,
                        stage="discovery_merge",
                        correlation_id=get_correlation_id(),
                        error=exc,
                        error_code="plan_invalid_after_repair",
                        edition_id=str(intake.edition_id),
                        intake_id=str(intake.id),
                        merge_model_run_id=(
                            str(exc.merge_model_run_id) if exc.merge_model_run_id else None
                        ),
                        raw_output_reference=exc.raw_output_reference,
                    )
                    raise DiscoveryMergeNeedsReview(run.id, run.review_reasons) from exc

            # The guard is deliberately re-run for cached plans: editorial state may
            # have gained a protected artifact since the model output was archived.
            plan, guard_warnings = apply_editorial_duplicate_guard(
                outcome.plan,
                handles,
                parent,
                editorial_subject_ids=editorial_subject_ids,
            )
            outcome = replace(
                outcome,
                plan=plan,
                warnings=tuple(dict.fromkeys((*outcome.warnings, *guard_warnings))),
            )
            review_reasons = merge_plan_review_reasons(plan)
            run = make_merge_run(
                edition_id=intake.edition_id,
                parent_snapshot=parent,
                intake=intake,
                delta=delta,
                planner=planner,
                handles=handles,
                outcome=outcome,
                review_reasons=review_reasons,
                excluded_subject_count=excluded_subject_count,
                blocking_version=self._blocking.version,
                rebase_count=rebase_count,
            )
            if review_reasons:
                run = replace(run, validation_status=MergeValidationStatus.NEEDS_REVIEW)
                await uow.discovery_merge_runs.add_if_absent(run)
                await uow.commit()
                self._diagnostics.record(
                    event="merge.needs_review",
                    run_id=run.id,
                    stage="discovery_merge",
                    correlation_id=get_correlation_id(),
                    edition_id=str(intake.edition_id),
                    intake_id=str(intake.id),
                    planner_kind=run.planner_kind.value,
                    review_reasons=list(review_reasons),
                    group_count=len(plan.groups),
                    warnings=list(outcome.warnings),
                )
                raise DiscoveryMergeNeedsReview(run.id, review_reasons)
            applied = apply_discovery_merge_plan(
                parent,
                delta,
                plan,
                active_candidates,
                resolved_handles=handles,
                planner_kind=run.planner_kind,
                edition_id=intake.edition_id,
                intake_id=intake.id,
                merge_run_id=run.id,
            )
            # This is deliberately checked after planning and while the active
            # snapshot/advisory lock is still held, but before deactivation.
            # Candidates whose batch has no intake are historical unstabilized
            # rows and are excluded from the blocking invariant.
            intakes = await uow.discovery_intakes.list_for_edition(intake.edition_id)
            intakes_by_batch = {item.batch_id: item for item in intakes}
            eligible_candidates = [
                candidate
                for candidate in active_candidates
                if candidate.discovery_batch_id in intakes_by_batch
            ]
            pending_candidate_ids: set[UUID] = set()
            for other_intake in intakes:
                if other_intake.id == intake.id:
                    continue
                if await uow.discovery_snapshots.get_for_intake(other_intake.id) is not None:
                    continue
                pending_candidate_ids.update(
                    candidate.id
                    for candidate in active_candidates
                    if candidate.discovery_batch_id == other_intake.batch_id
                )
            validate_candidate_coverage(
                eligible_candidates,
                applied.snapshot,
                pending_candidate_ids=pending_candidate_ids,
            )
            existing_snapshot = await uow.discovery_snapshots.get(applied.snapshot.id)
            if existing_snapshot is not None:
                return existing_snapshot
            await uow.discovery_merge_runs.add_if_absent(
                replace(run, warnings=tuple(dict.fromkeys((*run.warnings, *applied.warnings))))
            )
            await uow.discovery_subject_identities.add_many_if_absent(applied.identities)
            await uow.subject_merge_events.append_many(applied.merge_events)
            if parent is not None:
                # Guard checked while holding the active row lock. The unique partial
                # index is the final database-level safety net.
                await uow.discovery_snapshots.deactivate(parent.id)
            await uow.discovery_snapshots.append(applied.snapshot)
            await uow.subject_contributions.append_many(applied.contributions)
            await self._link_editorial_groups(uow, applied.snapshot)
            await uow.commit()
            self._diagnostics.record(
                event="merge.applied",
                run_id=run.id,
                stage="discovery_merge",
                correlation_id=get_correlation_id(),
                edition_id=str(intake.edition_id),
                intake_id=str(intake.id),
                planner_kind=run.planner_kind.value,
                snapshot_id=str(applied.snapshot.id),
                snapshot_version=applied.snapshot.version,
                group_count=len(plan.groups),
                subject_count=len(applied.snapshot.subjects),
                merge_event_count=len(applied.merge_events),
                warnings=list(applied.warnings),
            )
            await self._after_snapshot_activation(applied.snapshot)
            return applied.snapshot

    async def reconcile_batch(
        self,
        batch: DiscoveryBatch,
        *,
        input_mode: DiscoveryInputMode,
        actor_id: str,
    ) -> tuple[DiscoveryIntake, DiscoverySnapshot]:
        intake, _ = await self.ingest_batch(batch, input_mode=input_mode, actor_id=actor_id)
        async with self._uow_factory() as uow:
            parent = await uow.discovery_snapshots.get_active(batch.edition_id)
        snapshot = await self.reconcile_intake(
            intake.id,
            expected_parent_snapshot_id=parent.id if parent else None,
            actor_id=actor_id,
        )
        return intake, snapshot

    async def active_snapshot(self, edition_id: UUID) -> DiscoverySnapshot | None:
        async with self._uow_factory() as uow:
            return await uow.discovery_snapshots.get_active(edition_id)

    async def list_merge_runs(self, edition_id: UUID) -> Sequence[DiscoveryMergeRun]:
        async with self._uow_factory() as uow:
            return await uow.discovery_merge_runs.list_for_edition(edition_id)

    async def get_merge_run(self, edition_id: UUID, run_id: UUID) -> DiscoveryMergeRun:
        async with self._uow_factory() as uow:
            run = await uow.discovery_merge_runs.get(run_id)
        if run is None or run.edition_id != edition_id:
            raise LookupError(f"Unknown discovery merge run {run_id}")
        return run

    async def describe_merge_handles(
        self, edition_id: UUID, run_id: UUID
    ) -> dict[str, MergeHandleLabel]:
        """Resolve X1/C2 back to the titles a reviewer can actually judge.

        The plan speaks in handles because the model must not invent identifiers.
        A human deciding whether two subjects are the same needs the titles and
        the sources behind those handles.
        """
        async with self._uow_factory() as uow:
            run = await uow.discovery_merge_runs.get(run_id)
            if run is None or run.edition_id != edition_id:
                raise LookupError(f"Unknown discovery merge run {run_id}")
            labels: dict[str, MergeHandleLabel] = {}

            if run.parent_snapshot_id is not None:
                parent = await uow.discovery_snapshots.get(run.parent_snapshot_id)
                if parent is not None:
                    by_id = {subject.subject_id: subject for subject in parent.subjects}
                    for handle, raw_id in run.handle_map.items():
                        if not handle.startswith("X"):
                            continue
                        subject = by_id.get(UUID(raw_id))
                        if subject is not None:
                            labels[handle] = _handle_label(handle, subject.candidate)

            intake = (
                await uow.discovery_intakes.get(run.intake_id)
                if run.intake_id is not None
                else None
            )
            candidates = (
                await uow.discovery_candidates.list_for_batch(intake.batch_id)
                if intake is not None
                else ()
            )
            if intake is not None:
                for item in build_discovery_delta(intake, candidates).candidates:
                    labels[item.handle] = _handle_label(item.handle, item.candidate)
            return labels

    async def _after_snapshot_activation(self, snapshot: DiscoverySnapshot) -> None:
        if self._after_activation is None:
            return
        await self._after_activation(snapshot.edition_id)
        # The synchronizer may have created new groups, so bind them after it
        # completes as well as inside the activation transaction.
        async with self._uow_factory() as uow:
            await self._link_editorial_groups(uow, snapshot)
            await uow.commit()

    @staticmethod
    async def _link_editorial_groups(uow: object, snapshot: DiscoverySnapshot) -> None:
        groups = await uow.editorial_groups.list_for_edition(snapshot.edition_id)  # type: ignore[attr-defined]
        candidates = await uow.discovery_candidates.list_for_edition(  # type: ignore[attr-defined]
            snapshot.edition_id, include_replaced=False
        )
        candidates_by_id = {candidate.id: candidate for candidate in candidates}
        active_subject_ids = {subject.subject_id for subject in snapshot.subjects}
        for group in groups:
            if (
                group.status is EditorialGroupStatus.PROPOSED
                and group.discovery_subject_id is not None
                and group.discovery_subject_id not in active_subject_ids
            ):
                group.supersede()
                await uow.editorial_groups.save(group)  # type: ignore[attr-defined]
                continue
            if group.discovery_subject_id is None:
                continue
            subject = next(
                (
                    item
                    for item in snapshot.subjects
                    if item.subject_id == group.discovery_subject_id
                ),
                None,
            )
            if subject is None or group.status not in {
                EditorialGroupStatus.PROPOSED,
                EditorialGroupStatus.SELECTED,
            }:
                continue
            references = tuple(
                CandidateReference(
                    candidates_by_id[reference.candidate_id].discovery_batch_id,
                    reference.candidate_id,
                )
                for reference in subject.member_references
                if reference.candidate_id in candidates_by_id
            )
            if not references:
                if group.status is EditorialGroupStatus.PROPOSED:
                    group.supersede()
                    await uow.editorial_groups.save(group)  # type: ignore[attr-defined]
                continue
            if references != group.candidate_references:
                group.synchronize_candidate_references(references)
                group.needs_source_expansion = True
                group.needs_source_verification = True
                await uow.editorial_groups.save(group)  # type: ignore[attr-defined]
