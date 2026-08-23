from __future__ import annotations

from uuid import UUID

from cti_app.application.discovery.cumulative.types import ResolvedMergeHandles
from cti_app.application.discovery_identity import normalize
from cti_app.domain.discovery import CandidateTopic
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
)


def validate_merge_plan(
    plan: DiscoveryMergePlanV1,
    handles: ResolvedMergeHandles,
    *,
    known_evidence_urls: set[str] | None = None,
) -> tuple[DiscoveryMergePlanV1, tuple[str, ...]]:
    plan = plan.model_copy(deep=True)
    warnings: list[str] = list(plan.warnings)
    expected_incoming = set(handles.incoming)
    seen_incoming: list[str] = []
    seen_existing: list[str] = []
    for group_index, group in enumerate(plan.groups):
        if not group.incoming_candidate_handles:
            raise ValueError("Every merge group must contain an incoming candidate")
        unknown_incoming = set(group.incoming_candidate_handles) - expected_incoming
        if unknown_incoming:
            raise ValueError(f"Unknown incoming handles: {sorted(unknown_incoming)}")
        unknown_existing = set(group.existing_subject_handles) - set(handles.existing)
        if unknown_existing:
            raise ValueError(f"Unknown existing handles: {sorted(unknown_existing)}")
        if (
            len(group.existing_subject_handles) > 1
            and group.disposition is not MergeDisposition.REVIEW
        ):
            raise ValueError("Merging existing subjects requires review")
        seen_incoming.extend(group.incoming_candidate_handles)
        seen_existing.extend(group.existing_subject_handles)
        if not group.rationale.strip():
            warnings.append(f"group {group_index}: empty rationale")
        if known_evidence_urls is not None:
            unknown_urls = [
                url
                for url in group.evidence.shared_publication_urls
                if url not in known_evidence_urls
            ]
            if unknown_urls:
                group.evidence.shared_publication_urls = [
                    url
                    for url in group.evidence.shared_publication_urls
                    if url in known_evidence_urls
                ]
                warnings.append(
                    f"group {group_index}: removed unknown evidence URLs: "
                    + ", ".join(sorted(unknown_urls))
                )
        if group.evidence.conflict_signals and group.confidence is MergeConfidence.HIGH:
            group.disposition = MergeDisposition.REVIEW
            warnings.append(f"group {group_index}: conflicts force human review")
    if set(seen_incoming) != expected_incoming or len(seen_incoming) != len(set(seen_incoming)):
        raise ValueError("Merge plan must cover every incoming handle exactly once")
    if len(seen_existing) != len(set(seen_existing)):
        raise ValueError("An existing subject may appear in at most one merge group")
    plan.warnings = list(dict.fromkeys(warnings))
    return plan, tuple(plan.warnings)


def apply_editorial_duplicate_guard(
    plan: DiscoveryMergePlanV1,
    handles: ResolvedMergeHandles,
    parent_snapshot: DiscoverySnapshot | None,
    *,
    editorial_subject_ids: set[UUID],
) -> tuple[DiscoveryMergePlanV1, tuple[str, ...]]:
    guarded = plan.model_copy(deep=True)
    if parent_snapshot is None or not editorial_subject_ids:
        return guarded, ()
    editorial_subjects = [
        subject
        for subject in parent_snapshot.subjects
        if subject.subject_id in editorial_subject_ids
    ]
    warnings: list[str] = []
    for group_index, group in enumerate(guarded.groups):
        if group.existing_subject_handles:
            continue
        incoming = [
            handles.incoming[handle].candidate for handle in group.incoming_candidate_handles
        ]
        duplicates = {
            subject.subject_id
            for subject in editorial_subjects
            if any(
                _shares_strict_identity_key(subject.candidate, candidate) for candidate in incoming
            )
        }
        if not duplicates:
            continue
        if "possible_duplicate_of_editorial_subject" not in group.flags:
            group.flags.append("possible_duplicate_of_editorial_subject")
        group.disposition = MergeDisposition.REVIEW
        warnings.append(
            f"group {group_index}: possible duplicate of editorial subject(s) "
            + ", ".join(sorted(str(value) for value in duplicates))
        )
    return guarded, tuple(warnings)


def merge_plan_review_reasons(plan: DiscoveryMergePlanV1) -> tuple[str, ...]:
    reasons: list[str] = []
    for group in plan.groups:
        if group.confidence is not MergeConfidence.HIGH:
            reasons.append(f"confidence_{group.confidence.value}")
        if len(group.existing_subject_handles) > 1:
            reasons.append("multiple_existing_subjects")
        if group.evidence.conflict_signals:
            reasons.append("conflict_signals")
        if "possible_duplicate_of_editorial_subject" in group.flags:
            reasons.append("possible_duplicate_of_editorial_subject")
        if "incoming_subject_may_require_split" in group.flags:
            reasons.append("incoming_subject_may_require_split")
        if group.disposition is MergeDisposition.REVIEW and not reasons:
            reasons.append("planner_requested_review")
    return tuple(dict.fromkeys(reasons))


def _requires_review(group: DiscoveryMergeGroup) -> bool:
    return (
        group.disposition is MergeDisposition.REVIEW
        or group.confidence is not MergeConfidence.HIGH
        or len(group.existing_subject_handles) > 1
        or bool(group.evidence.conflict_signals)
        or "incoming_subject_may_require_split" in group.flags
    )


def _shares_strict_identity_key(left: CandidateTopic, right: CandidateTopic) -> bool:
    for field_name in ("campaigns", "malware", "cves"):
        left_values = {normalize(value) for value in getattr(left, field_name) if normalize(value)}
        right_values = {
            normalize(value) for value in getattr(right, field_name) if normalize(value)
        }
        if left_values & right_values:
            return True
    return bool(
        {source.canonical_url for source in left.sources}
        & {source.canonical_url for source in right.sources}
    )
