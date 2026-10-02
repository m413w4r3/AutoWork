"""Local, deterministic discovery merge planners — no external model calls.

`ChatGptMergePlanner` (the non-deterministic, external-model-backed planner)
lives in `chatgpt_planner.py`; it is out of scope for this module.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from uuid import UUID

from cti_app.application.discovery.cumulative.context import _handle_number
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.cumulative.validation import validate_merge_plan
from cti_app.application.discovery_identity import candidates_match_strongly, normalize
from cti_app.domain.discovery import CandidateTopic, canonicalize_http_url
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
BOOTSTRAP_COLLISION_POLICY_VERSION = "bootstrap-collision-v1"

_MECHANISM_STOPWORDS = frozenset(
    {
        "activity",
        "against",
        "attack",
        "campaign",
        "discovered",
        "during",
        "from",
        "group",
        "malware",
        "new",
        "operation",
        "report",
        "same",
        "shows",
        "that",
        "their",
        "this",
        "through",
        "used",
        "using",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class _IntakeCollision:
    confidence: MergeConfidence
    evidence: MergeEvidence
    motive: str


def bootstrap_intake_collision(
    left: CandidateTopic, right: CandidateTopic
) -> _IntakeCollision | None:
    """Find conservative, review-only collisions between incoming candidates.

    A canonical document or similar wording is never enough by itself. A pair
    needs a shared actor or campaign and compatible explicit scopes. A shared
    canonical document plus a matching event date is the high-confidence rule;
    other anchored combinations remain lower-confidence review proposals.
    """
    shared_actors = _shared_values(left.actors, right.actors)
    shared_campaigns = _shared_values(left.campaigns, right.campaigns)
    if not shared_actors and not shared_campaigns:
        return None

    left_actors = {_identity(value) for value in left.actors if _identity(value)}
    right_actors = {_identity(value) for value in right.actors if _identity(value)}
    left_campaigns = {_identity(value) for value in left.campaigns if _identity(value)}
    right_campaigns = {_identity(value) for value in right.campaigns if _identity(value)}
    if left_actors and right_actors and not left_actors & right_actors:
        return None
    if left_campaigns and right_campaigns and not left_campaigns & right_campaigns:
        return None

    shared_documents = _canonical_document_urls(left) & _canonical_document_urls(right)
    shared_malware = _shared_values(left.malware, right.malware)
    shared_cves = _shared_values(left.cves, right.cves)
    shared_mechanisms = _mechanism_phrases(left) & _mechanism_phrases(right)
    overlapping_event_date = left.event_date is not None and left.event_date == right.event_date

    high_confidence = bool(shared_documents and overlapping_event_date)
    if not high_confidence and not (
        shared_documents or overlapping_event_date or shared_malware or shared_mechanisms
    ):
        return None

    semantic_basis = [
        *(f"shared actor: {value}" for value in shared_actors),
        *(f"shared campaign: {value}" for value in shared_campaigns),
        *(f"shared mechanism: {value}" for value in sorted(shared_mechanisms)),
    ]
    if overlapping_event_date:
        assert left.event_date is not None
        semantic_basis.append(f"same event date: {left.event_date.isoformat()}")
    if high_confidence:
        semantic_basis.append("same canonical document, scope anchor, and event date")

    evidence = MergeEvidence(
        shared_publication_urls=sorted(shared_documents),
        shared_campaigns=sorted(shared_campaigns),
        shared_malware=sorted(shared_malware),
        shared_explicit_identifiers=sorted(shared_cves),
        semantic_basis=semantic_basis,
    )
    confidence = MergeConfidence.HIGH if high_confidence else MergeConfidence.MEDIUM
    motive = (
        "the same canonical document, an exact actor/campaign anchor, and the same event date"
        if high_confidence
        else (
            "an exact actor/campaign anchor plus corroborating document, period, malware, "
            "or mechanism evidence"
        )
    )
    return _IntakeCollision(confidence=confidence, evidence=evidence, motive=motive)


def _bootstrap_collision_plan(handles: ResolvedMergeHandles) -> DiscoveryMergePlanV1:
    ordered_handles = sorted(handles.incoming, key=_handle_number)
    collisions = {
        frozenset((left_handle, right_handle)): collision
        for left_handle, right_handle in combinations(ordered_handles, 2)
        if (
            collision := bootstrap_intake_collision(
                handles.incoming[left_handle].candidate,
                handles.incoming[right_handle].candidate,
            )
        )
        is not None
    }

    # Build complete-link groups: every pair in a proposal must have its own
    # collision evidence. This avoids merging a transitive chain of unrelated
    # activities into one all-or-nothing Fusion decision.
    clusters: list[list[str]] = []
    for handle in ordered_handles:
        matching_cluster = next(
            (
                cluster
                for cluster in clusters
                if all(frozenset((handle, member)) in collisions for member in cluster)
            ),
            None,
        )
        if matching_cluster is None:
            clusters.append([handle])
        else:
            matching_cluster.append(handle)

    groups: list[DiscoveryMergeGroup] = []
    for cluster in clusters:
        if len(cluster) == 1:
            groups.append(
                DiscoveryMergeGroup(
                    existing_subject_handles=[],
                    incoming_candidate_handles=cluster,
                    confidence=MergeConfidence.HIGH,
                    disposition=MergeDisposition.APPLY,
                    rationale="No anchored intake collision; create a distinct subject.",
                    evidence=MergeEvidence(semantic_basis=["deterministic intake collision check"]),
                )
            )
            continue

        pair_matches = [
            collisions[frozenset((left, right))] for left, right in combinations(cluster, 2)
        ]
        confidence = (
            MergeConfidence.HIGH
            if all(item.confidence is MergeConfidence.HIGH for item in pair_matches)
            else MergeConfidence.MEDIUM
        )
        evidence = MergeEvidence(
            shared_publication_urls=sorted(
                {url for item in pair_matches for url in item.evidence.shared_publication_urls}
            ),
            shared_campaigns=sorted(
                {value for item in pair_matches for value in item.evidence.shared_campaigns}
            ),
            shared_malware=sorted(
                {value for item in pair_matches for value in item.evidence.shared_malware}
            ),
            shared_explicit_identifiers=sorted(
                {
                    value
                    for item in pair_matches
                    for value in item.evidence.shared_explicit_identifiers
                }
            ),
            semantic_basis=list(
                dict.fromkeys(
                    ["deterministic first-intake collision check"]
                    + [basis for item in pair_matches for basis in item.evidence.semantic_basis]
                )
            ),
        )
        scope_comparison = " || ".join(
            _candidate_scope_comparison(handles.incoming[handle].candidate) for handle in cluster
        )
        motives = "; ".join(dict.fromkeys(item.motive for item in pair_matches))
        groups.append(
            DiscoveryMergeGroup(
                existing_subject_handles=[],
                incoming_candidate_handles=cluster,
                confidence=confidence,
                disposition=MergeDisposition.REVIEW,
                rationale=(
                    f"First-intake collision proposal: {motives}. Side-by-side scope comparison: "
                    f"{scope_comparison}. Analyst decision required."
                ),
                evidence=evidence,
                flags=["intake_collision"],
            )
        )
    return DiscoveryMergePlanV1(groups=groups)


def _identity(value: str) -> str:
    return normalize(value)


def _shared_values(left: Sequence[str], right: Sequence[str]) -> list[str]:
    left_keys = {_identity(value) for value in left if _identity(value)}
    right_keys = {_identity(value) for value in right if _identity(value)}
    shared_keys = left_keys & right_keys
    labels: dict[str, str] = {}
    for value in (*left, *right):
        key = _identity(value)
        if key and key in shared_keys and key not in labels:
            labels[key] = value.strip()
    return [labels[key] for key in sorted(labels)]


def _canonical_document_urls(candidate: CandidateTopic) -> set[str]:
    urls = [source.canonical_url or source.url for source in candidate.sources] + [
        source.raw_url for source in candidate.incomplete_sources if source.raw_url
    ]
    return {canonical for url in urls if (canonical := canonicalize_http_url(url)) is not None}


def _mechanism_phrases(candidate: CandidateTopic) -> set[str]:
    phrases: set[str] = set()
    for text in (candidate.summary, candidate.novelty, candidate.technical_potential_reason):
        words = normalize(text).split()
        for width in (2, 3):
            for start in range(len(words) - width + 1):
                phrase = words[start : start + width]
                if all(len(word) >= 4 and word not in _MECHANISM_STOPWORDS for word in phrase):
                    phrases.add(" ".join(phrase))
    return phrases


def _candidate_scope_comparison(candidate: CandidateTopic) -> str:
    actors = ", ".join(candidate.actors) or "unknown"
    campaigns = ", ".join(candidate.campaigns) or "unknown"
    malware = ", ".join(candidate.malware) or "unknown"
    period = candidate.event_date.isoformat() if candidate.event_date else "unknown"
    mechanisms = ", ".join(sorted(_mechanism_phrases(candidate))[:3]) or "unknown"
    return (
        f"{candidate.title!r} [actor={actors}; campaign={campaigns}; malware={malware}; "
        f"event_date={period}; mechanism={mechanisms}]"
    )


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
            plan, warnings = validate_merge_plan(_bootstrap_collision_plan(handles), handles)
            return PlannedDiscoveryMerge(plan, warnings=warnings)

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


class DeterministicBootstrapPlanner(HeuristicMergePlanner):
    """Review-only collision check used for an edition's first intake."""

    kind = DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP
    policy_version = BOOTSTRAP_COLLISION_POLICY_VERSION

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
        if parent_snapshot is not None:
            raise ValueError("DeterministicBootstrapPlanner requires an empty snapshot")
        plan, warnings = validate_merge_plan(_bootstrap_collision_plan(handles), handles)
        return PlannedDiscoveryMerge(plan, warnings=warnings)


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
    existing subject — no identity-matching, no ambiguity possible. It is
    shared by manual URL attachments and replacements.

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

    def __init__(
        self,
        target_subject_id: UUID,
        incoming_candidate_id: UUID,
        *,
        operation: str = "attach",
    ) -> None:
        if operation not in {"attach", "replace"}:
            raise ValueError("Targeted manual operation must be attach or replace")
        self._target_subject_id = target_subject_id
        self._incoming_candidate_id = incoming_candidate_id
        self._operation = operation
        self.policy_version = f"targeted-{operation}-v1"

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
                if item.candidate_id == self._incoming_candidate_id
            ),
            None,
        )
        if incoming_handle is None:
            raise ValueError(
                f"Incoming candidate {self._incoming_candidate_id} was not found in this delta"
            )
        plan = DiscoveryMergePlanV1(
            groups=[
                DiscoveryMergeGroup(
                    existing_subject_handles=[target_handle],
                    incoming_candidate_handles=[incoming_handle],
                    confidence=MergeConfidence.HIGH,
                    disposition=MergeDisposition.APPLY,
                    rationale=f"manual URL {self._operation}",
                    evidence=MergeEvidence(semantic_basis=["manual edit"]),
                )
            ]
        )
        validated, warnings = validate_merge_plan(plan, handles)
        return PlannedDiscoveryMerge(validated, warnings=warnings)
