"""Local, deterministic discovery merge planners — no external model calls.

`ChatGptMergePlanner` (the non-deterministic, external-model-backed planner)
lives in `chatgpt_planner.py`; it is out of scope for this module.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from cti_app.application.discovery.cumulative.context import _handle_number
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.cumulative.validation import validate_merge_plan
from cti_app.application.discovery_identity import candidates_match_strongly
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
)

HEURISTIC_POLICY_VERSION = "heuristic-v2"


class HeuristicMergePlanner:
    """Deterministic local planner, also available as an explicit operator fallback."""

    kind = DiscoveryPlannerKind.HEURISTIC
    policy_version = HEURISTIC_POLICY_VERSION

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge:
        del delta, edition_id, external_llm_allowed, sensitivity
        if parent_snapshot is None:
            return PlannedDiscoveryMerge(
                DiscoveryMergePlanV1(
                    groups=[
                        DiscoveryMergeGroup(
                            existing_subject_handles=[],
                            incoming_candidate_handles=[handle],
                            confidence=MergeConfidence.HIGH,
                            disposition=MergeDisposition.APPLY,
                            rationale="deterministic bootstrap",
                            evidence=MergeEvidence(semantic_basis=["first intake"]),
                        )
                        for handle in sorted(handles.incoming, key=_handle_number)
                    ]
                )
            )

        subject_handles = {subject_id: handle for handle, subject_id in handles.existing.items()}
        by_target: dict[str, list[str]] = defaultdict(list)
        create_new: list[str] = []
        for incoming_handle in sorted(handles.incoming, key=_handle_number):
            incoming = handles.incoming[incoming_handle]
            matches = [
                subject
                for subject in parent_snapshot.subjects
                if candidates_match_strongly(subject.candidate, incoming.candidate)
            ]
            if len(matches) == 1:
                by_target[subject_handles[matches[0].subject_id]].append(incoming_handle)
            else:
                # Ambiguous candidates stay separate. Increment 1 never auto-merges
                # two durable identities.
                create_new.append(incoming_handle)

        groups = [
            DiscoveryMergeGroup(
                existing_subject_handles=[target],
                incoming_candidate_handles=incoming_handles,
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="deterministic identity match",
                evidence=MergeEvidence(semantic_basis=["local heuristic"]),
            )
            for target, incoming_handles in sorted(by_target.items(), key=lambda item: item[0])
        ]
        groups.extend(
            DiscoveryMergeGroup(
                existing_subject_handles=[],
                incoming_candidate_handles=[handle],
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="no unambiguous deterministic match",
                evidence=MergeEvidence(semantic_basis=["new subject"]),
            )
            for handle in create_new
        )
        return PlannedDiscoveryMerge(DiscoveryMergePlanV1(groups=groups))


@dataclass(frozen=True, slots=True)
class HumanMergeDecision:
    group_index: int
    action: str
    target_subject_handle: str | None = None


class HumanMergePlanner:
    kind = DiscoveryPlannerKind.HUMAN
    policy_version = "human-resolution-v1"

    def __init__(
        self,
        original_plan: DiscoveryMergePlanV1,
        decisions: Sequence[HumanMergeDecision],
    ) -> None:
        self._original_plan = original_plan
        self._decisions = {decision.group_index: decision for decision in decisions}

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge:
        del parent_snapshot, delta, edition_id, external_llm_allowed, sensitivity
        corrected = self._original_plan.model_copy(deep=True)
        for group_index, group in enumerate(corrected.groups):
            decision = self._decisions.get(group_index)
            if decision is None or decision.action == "defer":
                group.disposition = MergeDisposition.REVIEW
                continue
            if decision.action == "create_new":
                group.existing_subject_handles = []
            elif decision.action == "attach_to":
                target = decision.target_subject_handle
                if target not in handles.existing:
                    raise ValueError("attach_to requires a known target_subject_handle")
                group.existing_subject_handles = [target]
            elif decision.action == "merge_existing":
                if len(group.existing_subject_handles) < 2:
                    raise ValueError("merge_existing requires at least two existing subjects")
                target = decision.target_subject_handle
                if target is not None:
                    if target not in group.existing_subject_handles:
                        raise ValueError("Merge target must belong to the reviewed group")
                    group.existing_subject_handles = [
                        target,
                        *(value for value in group.existing_subject_handles if value != target),
                    ]
            elif decision.action != "accept":
                raise ValueError(f"Unknown human merge action {decision.action}")
            group.confidence = MergeConfidence.HIGH
            group.disposition = (
                MergeDisposition.REVIEW
                if len(group.existing_subject_handles) > 1
                else MergeDisposition.APPLY
            )
            group.flags = []
            group.evidence.conflict_signals = []
            group.rationale = f"human resolution: {decision.action}"
        plan, warnings = validate_merge_plan(corrected, handles)
        return PlannedDiscoveryMerge(plan, warnings=warnings)


class TargetedMergePlanner:
    """Deterministically merges one known incoming candidate into one known
    existing subject — no identity-matching, no ambiguity possible.

    Used for edits where the caller already knows exactly which subject an
    incoming candidate belongs to (e.g. attaching a URL to an incomplete
    source) and must not let a planner rediscover it: `HeuristicMergePlanner`
    would refuse to pick a subject if more than one shares its title
    (`candidates_match_strongly`), and `ChatGptMergePlanner` is
    nondeterministic and the wrong tool for a one-field correction. This
    planner never inspects the candidate at all — it trusts the caller.
    """

    kind = DiscoveryPlannerKind.HUMAN
    policy_version = "targeted-attach-v1"

    def __init__(self, target_subject_id: UUID, incoming_candidate_key: UUID) -> None:
        self._target_subject_id = target_subject_id
        self._incoming_candidate_key = incoming_candidate_key

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge:
        del edition_id, external_llm_allowed, sensitivity
        if parent_snapshot is None:
            raise ValueError("TargetedMergePlanner requires an existing parent snapshot")
        target_handle = next(
            (
                handle
                for handle, subject_id in handles.existing.items()
                if subject_id == self._target_subject_id
            ),
            None,
        )
        if target_handle is None:
            raise ValueError(
                f"Target subject {self._target_subject_id} was not included in this merge"
            )
        incoming_handle = next(
            (
                handle
                for handle, item in handles.incoming.items()
                if item.candidate_key == self._incoming_candidate_key
            ),
            None,
        )
        if incoming_handle is None:
            raise ValueError(
                f"Incoming candidate {self._incoming_candidate_key} was not found in this delta"
            )
        plan = DiscoveryMergePlanV1(
            groups=[
                DiscoveryMergeGroup(
                    existing_subject_handles=[target_handle],
                    incoming_candidate_handles=[incoming_handle],
                    confidence=MergeConfidence.HIGH,
                    disposition=MergeDisposition.APPLY,
                    rationale="manual URL attachment",
                    evidence=MergeEvidence(semantic_basis=["manual edit"]),
                )
            ]
        )
        validated, warnings = validate_merge_plan(plan, handles)
        return PlannedDiscoveryMerge(validated, warnings=warnings)
