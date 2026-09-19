from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    DiscoveryMergePlanV1,
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
    intake: DiscoveryIntake | None,
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
        intake_id=intake.id if intake is not None else None,
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
            **{handle: str(value.candidate_id) for handle, value in handles.incoming.items()},
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


def make_human_merge_run(
    *,
    edition_id: UUID,
    parent_snapshot: DiscoverySnapshot | None,
    intake_id: UUID | None,
    plan: DiscoveryMergePlanV1,
    handle_map: Mapping[str, str],
    included_subject_ids: Sequence[UUID],
    supersedes_merge_run_id: UUID | None = None,
    validation_status: MergeValidationStatus = MergeValidationStatus.VALID,
    review_reasons: Sequence[str] = (),
) -> DiscoveryMergeRun:
    merge_input_hash = canonical_sha256(
        {
            "kind": DiscoveryPlannerKind.HUMAN.value,
            "parent_snapshot_hash": parent_snapshot.snapshot_hash if parent_snapshot else None,
            "intake_id": str(intake_id) if intake_id else None,
            "plan": plan.model_dump(mode="json"),
            "handle_map": dict(sorted(handle_map.items())),
            "supersedes": str(supersedes_merge_run_id) if supersedes_merge_run_id else None,
        }
    )
    return DiscoveryMergeRun(
        id=uuid5(NAMESPACE_URL, f"discovery-merge-run:{merge_input_hash}"),
        edition_id=edition_id,
        parent_snapshot_id=parent_snapshot.id if parent_snapshot else None,
        intake_id=intake_id,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        prompt_version="none",
        policy_version="fusion-human-v1",
        blocking_version="fusion-v1",
        merge_input_hash=merge_input_hash,
        handle_map=dict(handle_map),
        included_subject_ids=tuple(included_subject_ids),
        excluded_subject_count=0,
        validation_status=validation_status,
        review_reasons=tuple(review_reasons),
        plan_payload=plan.model_dump(mode="json"),
        supersedes_merge_run_id=supersedes_merge_run_id,
    )


def make_structural_merge_run(
    *,
    edition_id: UUID,
    parent_snapshot: DiscoverySnapshot,
    operation: str,
    subject_ids: Sequence[UUID],
    actor_id: str,
    candidate_ids: Sequence[UUID] = (),
) -> DiscoveryMergeRun:
    # `DiscoveryMergeRun` carries no actor column; a structural merge/split has
    # no plan either, so its payload is where the deciding analyst is recorded.
    payload: dict[str, object] = {
        "schema_version": "1",
        "operation": operation,
        "actor_id": actor_id,
        "subject_ids": [str(value) for value in subject_ids],
        "candidate_ids": [str(value) for value in sorted(candidate_ids, key=str)],
        "groups": [],
        "warnings": [],
    }
    merge_input_hash = canonical_sha256(
        {
            "kind": operation,
            "actor_id": actor_id,
            "parent_snapshot_id": str(parent_snapshot.id),
            "parent_snapshot_hash": parent_snapshot.snapshot_hash,
            "subject_ids": sorted(str(value) for value in subject_ids),
            "candidate_ids": sorted(str(value) for value in candidate_ids),
        }
    )
    return DiscoveryMergeRun(
        id=uuid5(NAMESPACE_URL, f"discovery-merge-run:{merge_input_hash}"),
        edition_id=edition_id,
        parent_snapshot_id=parent_snapshot.id,
        intake_id=None,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        prompt_version="none",
        policy_version="fusion-structural-v1",
        blocking_version="fusion-v1",
        merge_input_hash=merge_input_hash,
        handle_map={},
        included_subject_ids=tuple(subject_ids),
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.VALID,
        plan_payload=payload,
    )
