from __future__ import annotations

from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery.cumulative.chatgpt_planner import (
    DISCOVERY_MERGE_PROMPT_VERSION,
)
from cti_app.application.discovery.cumulative.context import NO_BLOCKING_VERSION
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    DiscoveryMergePlanner,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryIntake,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeValidationStatus,
    canonical_sha256,
)


def make_merge_run(
    *,
    edition_id: UUID,
    parent_snapshot: DiscoverySnapshot | None,
    intake: DiscoveryIntake,
    delta: DiscoveryDelta,
    planner: DiscoveryMergePlanner,
    handles: ResolvedMergeHandles,
    outcome: PlannedDiscoveryMerge | None = None,
    validation_status: MergeValidationStatus | None = None,
    review_reasons: Sequence[str] = (),
    excluded_subject_count: int = 0,
    blocking_version: str = NO_BLOCKING_VERSION,
    supersedes_merge_run_id: UUID | None = None,
    rebase_count: int = 0,
) -> DiscoveryMergeRun:
    merge_input_hash = canonical_sha256(
        {
            "policy_version": planner.policy_version,
            "blocking_version": blocking_version,
            "parent_snapshot_hash": parent_snapshot.snapshot_hash if parent_snapshot else None,
            "delta_hash": delta.delta_hash,
            "human_plan_hash": (
                canonical_sha256(outcome.plan.model_dump(mode="json"))
                if planner.kind is DiscoveryPlannerKind.HUMAN and outcome is not None
                else None
            ),
            "supersedes_merge_run_id": (
                str(supersedes_merge_run_id) if supersedes_merge_run_id else None
            ),
        }
    )
    return DiscoveryMergeRun(
        id=uuid5(NAMESPACE_URL, f"discovery-merge-run:{merge_input_hash}"),
        edition_id=edition_id,
        parent_snapshot_id=parent_snapshot.id if parent_snapshot else None,
        intake_id=intake.id,
        planner_kind=(
            DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP
            if parent_snapshot is None
            else planner.kind
        ),
        prompt_version=(
            DISCOVERY_MERGE_PROMPT_VERSION
            if planner.kind is DiscoveryPlannerKind.CHATGPT
            else "none"
        ),
        policy_version=planner.policy_version,
        blocking_version=blocking_version,
        merge_input_hash=merge_input_hash,
        handle_map={
            **{handle: str(value) for handle, value in handles.existing.items()},
            **{handle: str(value.candidate_key) for handle, value in handles.incoming.items()},
        },
        included_subject_ids=tuple(handles.existing.values()),
        excluded_subject_count=excluded_subject_count,
        validation_status=(
            validation_status
            or (outcome.validation_status if outcome else MergeValidationStatus.VALID)
        ),
        warnings=outcome.warnings if outcome else (),
        review_reasons=tuple(review_reasons),
        plan_payload=(outcome.plan.model_dump(mode="json") if outcome else None),
        merge_model_run_id=outcome.merge_model_run_id if outcome else None,
        raw_output_reference=outcome.raw_output_reference if outcome else None,
        normalized_output_reference=(outcome.normalized_output_reference if outcome else None),
        supersedes_merge_run_id=supersedes_merge_run_id,
        rebase_count=rebase_count,
    )
