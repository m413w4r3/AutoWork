"""Pure discovery-merge application engine.

Turns (parent snapshot, delta, validated plan) into an `AppliedDiscoveryMerge`.
No I/O, no unit of work, no diagnostics, no external planner — those stay in
`service.py`, which orchestrates this engine against persistence and the
planner protocol.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery.cumulative.context import _candidate_content
from cti_app.application.discovery.cumulative.types import (
    AppliedDiscoveryMerge,
    DiscoveryDelta,
    IncomingDiscoveryCandidate,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.cumulative.validation import (
    requires_review,
    validate_merge_plan,
)
from cti_app.application.discovery_identity import normalize
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryCandidate,
    IncompleteSourceCandidate,
    ProvisionalDiscoveryIoc,
    SourceCandidate,
    SourceRole,
    recover_incomplete_source_urls,
    remap_ioc_publication_ids,
    same_publication,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    SubjectContribution,
    SubjectMergeEvent,
    canonical_sha256,
    discovery_origin_key,
    discovery_subject_id,
)


def apply_discovery_merge_plan(
    parent_snapshot: DiscoverySnapshot | None,
    delta: DiscoveryDelta,
    plan: DiscoveryMergePlanV1,
    canonical_candidates: Sequence[DiscoveryCandidate],
    *,
    resolved_handles: ResolvedMergeHandles,
    planner_kind: DiscoveryPlannerKind,
    edition_id: UUID,
    intake_id: UUID | None,
    merge_run_id: UUID,
    actor_id: str = "system",
) -> AppliedDiscoveryMerge:
    plan, validation_warnings = validate_merge_plan(plan, resolved_handles)
    review_groups = [
        index
        for index, group in enumerate(plan.groups)
        if requires_review(group) and planner_kind is not DiscoveryPlannerKind.HUMAN
    ]
    if review_groups:
        raise ValueError(f"Merge plan requires human review for groups {review_groups}")

    candidates_by_id = {candidate.id: candidate for candidate in canonical_candidates}
    parent_subjects = {
        subject.subject_id: deepcopy(subject)
        for subject in (parent_snapshot.subjects if parent_snapshot else ())
    }
    # A historical snapshot may still contain members that were superseded by
    # a later canonical candidate. Keep the historical subject available for a
    # targeted replacement, but only carry subjects with active members into
    # the new snapshot.
    final_subjects: dict[UUID, DiscoverySubject] = {}
    for subject in parent_subjects.values():
        reconstructed = _reconstruct_subject(subject, candidates_by_id)
        if reconstructed is not None:
            final_subjects[subject.subject_id] = reconstructed
    identities: list[DiscoverySubjectIdentity] = []
    contributions: list[SubjectContribution] = []
    merge_events: list[SubjectMergeEvent] = []
    warnings = list(validation_warnings)
    next_version = 1 if parent_snapshot is None else parent_snapshot.version + 1
    parent_key = str(parent_snapshot.id) if parent_snapshot else "root"
    snapshot_id = uuid5(
        NAMESPACE_URL,
        f"discovery-snapshot:{edition_id}:{parent_key}:{intake_id}:{merge_run_id}",
    )

    for group_index, group in enumerate(plan.groups):
        incoming = [
            resolved_handles.incoming[handle] for handle in group.incoming_candidate_handles
        ]
        existing_ids = [
            resolved_handles.existing[handle] for handle in group.existing_subject_handles
        ]
        if existing_ids:
            subject_id = existing_ids[0]
            base = parent_subjects[subject_id]
            absorbed = [parent_subjects[value] for value in existing_ids[1:]]
            member_ids = _active_member_ids(
                [
                    *(reference.candidate_id for reference in base.member_references),
                    *(
                        reference.candidate_id
                        for subject in absorbed
                        for reference in subject.member_references
                    ),
                    *(item.candidate_id for item in incoming),
                ],
                candidates_by_id,
            )
            if not member_ids:
                final_subjects.pop(subject_id, None)
                continue
            merged_candidate, merge_warnings = _subject_topic(
                member_ids, candidates_by_id, preferred_id=base.candidate.id
            )
            warnings.extend(merge_warnings)
            references = _unique_member_references(
                DiscoveryMemberReference(candidate_id) for candidate_id in member_ids
            )
            final_subjects[subject_id] = DiscoverySubject(
                subject_id=subject_id,
                candidate=merged_candidate,
                member_references=references,
                created_at=base.created_at,
            )
            for absorbed_subject in absorbed:
                final_subjects.pop(absorbed_subject.subject_id, None)
                merge_events.append(
                    SubjectMergeEvent(
                        edition_id=edition_id,
                        from_subject_id=absorbed_subject.subject_id,
                        into_subject_id=subject_id,
                        merge_run_id=merge_run_id,
                        actor_id=actor_id,
                        reason=group.rationale or "human subject merge",
                        id=uuid5(
                            NAMESPACE_URL,
                            "discovery-subject-merge:"
                            f"{merge_run_id}:{absorbed_subject.subject_id}:{subject_id}",
                        ),
                    )
                )
        else:
            active_incoming = [
                item for item in incoming if item.candidate_id in candidates_by_id
            ]
            member_ids = tuple(
                sorted({item.candidate_id for item in active_incoming}, key=str)
            )
            if not member_ids:
                continue
            origin_key = discovery_origin_key(member_ids)
            subject_id = discovery_subject_id(edition_id, origin_key)
            canonical_incoming = [
                IncomingDiscoveryCandidate(
                    handle=item.handle,
                    candidate_id=item.candidate_id,
                    candidate=candidates_by_id[item.candidate_id].to_candidate_topic(),
                )
                for item in active_incoming
            ]
            representative = _pick_new_subject_representative(canonical_incoming)
            merged_candidate, merge_warnings = _merge_candidates(
                representative.candidate,
                [item.candidate for item in canonical_incoming if item is not representative],
            )
            warnings.extend(merge_warnings)
            created_at = datetime.now(UTC)
            final_subjects[subject_id] = DiscoverySubject(
                subject_id=subject_id,
                candidate=merged_candidate,
                member_references=_unique_member_references(
                    DiscoveryMemberReference(candidate_id) for candidate_id in member_ids
                ),
                created_at=created_at,
            )
            identities.append(
                DiscoverySubjectIdentity(
                    id=subject_id,
                    edition_id=edition_id,
                    origin_key=origin_key,
                    created_by_merge_run_id=merge_run_id,
                    created_at=created_at,
                )
            )

        for item in incoming:
            if intake_id is None:
                continue
            if item.candidate_id not in candidates_by_id:
                continue
            candidate = candidates_by_id[item.candidate_id].to_candidate_topic()
            contributions.append(
                SubjectContribution(
                    subject_id=subject_id,
                    intake_id=intake_id,
                    candidate_id=item.candidate_id,
                    first_seen_snapshot_id=snapshot_id,
                    first_seen_version=next_version,
                    contributed_title=candidate.title,
                    contributed_summary=candidate.summary,
                    contributed_source_ids=tuple(source.id for source in candidate.sources),
                    contributed_provisional_ioc_ids=tuple(
                        ioc.id for ioc in candidate.provisional_iocs
                    ),
                    merge_run_id=merge_run_id,
                    merge_group_index=group_index,
                    id=uuid5(
                        NAMESPACE_URL,
                        f"discovery-contribution:{intake_id}:{item.candidate_id}:{subject_id}",
                    ),
                )
            )

    ordered_subjects = tuple(sorted(final_subjects.values(), key=lambda item: str(item.subject_id)))
    snapshot_hash = _snapshot_hash(ordered_subjects)
    snapshot = DiscoverySnapshot(
        id=snapshot_id,
        edition_id=edition_id,
        version=next_version,
        parent_snapshot_id=parent_snapshot.id if parent_snapshot else None,
        intake_id=intake_id,
        merge_run_id=merge_run_id,
        planner_kind=planner_kind,
        subjects=ordered_subjects,
        snapshot_hash=snapshot_hash,
        is_active=True,
    )
    _assert_non_loss(parent_snapshot, delta, snapshot, candidates_by_id)
    return AppliedDiscoveryMerge(
        snapshot=snapshot,
        identities=tuple(identities),
        contributions=tuple(contributions),
        merge_events=tuple(merge_events),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def apply_structural_subject_merge(
    parent_snapshot: DiscoverySnapshot,
    canonical_candidates: Sequence[DiscoveryCandidate],
    *,
    edition_id: UUID,
    subject_ids: Sequence[UUID],
    merge_run_id: UUID,
    actor_id: str,
) -> AppliedDiscoveryMerge:
    """Reorganize existing memberships without inventing contributions."""
    selected = [
        subject for subject in parent_snapshot.subjects if subject.subject_id in subject_ids
    ]
    if (
        len(set(subject_ids)) != len(subject_ids)
        or len(selected) != len(set(subject_ids))
        or len(selected) < 2
    ):
        raise ValueError("Manual merge subjects must all belong to the active snapshot")
    survivor = min(selected, key=lambda item: (item.created_at, str(item.subject_id)))
    candidates_by_id = {candidate.id: candidate for candidate in canonical_candidates}
    member_ids = _active_member_ids(
        (
            reference.candidate_id
            for subject in selected
            for reference in subject.member_references
        ),
        candidates_by_id,
    )
    if not member_ids:
        raise ValueError("Manual merge subjects have no active canonical candidate")
    merged_subject = _subject_from_member_ids(
        survivor.subject_id,
        survivor.created_at,
        member_ids,
        candidates_by_id,
        preferred_id=survivor.candidate.id,
    )
    subjects = tuple(
        sorted(
            [
                merged_subject,
                *_reconstructed_others(
                    parent_snapshot, candidates_by_id, excluded=set(subject_ids)
                ),
            ],
            key=lambda item: str(item.subject_id),
        )
    )
    events = tuple(
        SubjectMergeEvent(
            edition_id=edition_id,
            from_subject_id=subject.subject_id,
            into_subject_id=survivor.subject_id,
            merge_run_id=merge_run_id,
            actor_id=actor_id,
            reason="human fusion merge",
            id=uuid5(
                NAMESPACE_URL,
                f"discovery-subject-merge:{merge_run_id}:{subject.subject_id}:{survivor.subject_id}",
            ),
        )
        for subject in selected
        if subject.subject_id != survivor.subject_id
    )
    return _structural_result(
        parent_snapshot,
        subjects,
        edition_id=edition_id,
        merge_run_id=merge_run_id,
        merge_events=events,
    )


def apply_structural_subject_split(
    parent_snapshot: DiscoverySnapshot,
    canonical_candidates: Sequence[DiscoveryCandidate],
    *,
    edition_id: UUID,
    subject_id: UUID,
    candidate_ids: Sequence[UUID],
    merge_run_id: UUID,
) -> AppliedDiscoveryMerge:
    subjects_by_id = {subject.subject_id: subject for subject in parent_snapshot.subjects}
    original = subjects_by_id.get(subject_id)
    if original is None:
        raise ValueError("The subject does not belong to the active snapshot")
    candidates_by_id = {candidate.id: candidate for candidate in canonical_candidates}
    active_ids = set(
        _active_member_ids(
            (reference.candidate_id for reference in original.member_references),
            candidates_by_id,
        )
    )
    split_ids = set(candidate_ids)
    if len(split_ids) != len(candidate_ids) or not split_ids or not split_ids < active_ids:
        raise ValueError(
            "Split candidate_ids must be a non-empty strict subset of the active members"
        )
    remaining_ids = tuple(sorted(active_ids - split_ids, key=str))
    separated_ids = tuple(sorted(split_ids, key=str))
    origin_key = f"split:{merge_run_id}:{':'.join(str(value) for value in separated_ids)}"
    separated_subject_id = discovery_subject_id(edition_id, origin_key)
    remaining = _subject_from_member_ids(
        subject_id,
        original.created_at,
        remaining_ids,
        candidates_by_id,
        preferred_id=original.candidate.id,
    )
    separated = _subject_from_member_ids(
        separated_subject_id,
        datetime.now(UTC),
        separated_ids,
        candidates_by_id,
    )
    subjects = tuple(
        sorted(
            [
                separated,
                *_reconstructed_others(parent_snapshot, candidates_by_id, excluded={subject_id}),
                remaining,
            ],
            key=lambda item: str(item.subject_id),
        )
    )
    identity = DiscoverySubjectIdentity(
        edition_id=edition_id,
        origin_key=origin_key,
        created_by_merge_run_id=merge_run_id,
        id=separated_subject_id,
    )
    return _structural_result(
        parent_snapshot,
        subjects,
        edition_id=edition_id,
        merge_run_id=merge_run_id,
        identities=(identity,),
    )


def _subject_from_member_ids(
    subject_id: UUID,
    created_at: datetime,
    member_ids: Sequence[UUID],
    candidates_by_id: dict[UUID, DiscoveryCandidate],
    *,
    preferred_id: UUID | None = None,
) -> DiscoverySubject:
    candidate, _ = _subject_topic(member_ids, candidates_by_id, preferred_id=preferred_id)
    return DiscoverySubject(
        subject_id=subject_id,
        candidate=candidate,
        member_references=tuple(DiscoveryMemberReference(value) for value in member_ids),
        created_at=created_at,
    )


def _reconstructed_others(
    parent_snapshot: DiscoverySnapshot,
    candidates_by_id: dict[UUID, DiscoveryCandidate],
    *,
    excluded: set[UUID],
) -> list[DiscoverySubject]:
    others: list[DiscoverySubject] = []
    for subject in parent_snapshot.subjects:
        if subject.subject_id in excluded:
            continue
        reconstructed = _reconstruct_subject(subject, candidates_by_id)
        if reconstructed is not None:
            others.append(reconstructed)
    return others


def _structural_result(
    parent_snapshot: DiscoverySnapshot,
    subjects: tuple[DiscoverySubject, ...],
    *,
    edition_id: UUID,
    merge_run_id: UUID,
    identities: Sequence[DiscoverySubjectIdentity] = (),
    merge_events: Sequence[SubjectMergeEvent] = (),
) -> AppliedDiscoveryMerge:
    snapshot = DiscoverySnapshot(
        id=uuid5(
            NAMESPACE_URL,
            f"discovery-snapshot:{edition_id}:{parent_snapshot.id}:structural:{merge_run_id}",
        ),
        edition_id=edition_id,
        version=parent_snapshot.version + 1,
        parent_snapshot_id=parent_snapshot.id,
        intake_id=None,
        merge_run_id=merge_run_id,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        subjects=subjects,
        snapshot_hash=_snapshot_hash(subjects),
        is_active=True,
    )
    return AppliedDiscoveryMerge(
        snapshot=snapshot,
        identities=tuple(identities),
        contributions=(),
        merge_events=tuple(merge_events),
        warnings=(),
    )


def _pick_new_subject_representative(
    incoming: Sequence[IncomingDiscoveryCandidate],
) -> IncomingDiscoveryCandidate:
    role_priority = {SourceRole.PRIMARY: 2, SourceRole.INDEPENDENT: 1}

    def rank(item: IncomingDiscoveryCandidate) -> tuple[int, int, int, str]:
        roles = [role_priority.get(source.role, 0) for source in item.candidate.sources]
        return (
            max(roles, default=0),
            item.candidate.technical_potential,
            len(item.candidate.sources),
            # min() is used below; reverse the stable UUID preference separately.
            str(item.candidate_id),
        )

    best_rank = max(rank(item)[:3] for item in incoming)
    return min(
        (item for item in incoming if rank(item)[:3] == best_rank),
        key=lambda item: str(item.candidate_id),
    )


def _active_member_ids(
    candidate_ids: Iterable[UUID], candidates_by_id: dict[UUID, DiscoveryCandidate]
) -> tuple[UUID, ...]:
    return tuple(
        sorted(
            {candidate_id for candidate_id in candidate_ids if candidate_id in candidates_by_id},
            key=str,
        )
    )


def _subject_topic(
    member_ids: Sequence[UUID],
    candidates_by_id: dict[UUID, DiscoveryCandidate],
    *,
    preferred_id: UUID | None = None,
) -> tuple[CandidateTopic, list[str]]:
    """Rebuild a subject aggregate from its active canonical members.

    D10: an existing subject keeps the prose of its current representative as
    long as that member is still active; otherwise the deterministic
    new-subject representative is chosen among the remaining members.
    """
    incoming = tuple(
        IncomingDiscoveryCandidate(
            handle=f"C{index}",
            candidate_id=candidate_id,
            candidate=candidates_by_id[candidate_id].to_candidate_topic(),
        )
        for index, candidate_id in enumerate(member_ids, 1)
    )
    representative = next(
        (item for item in incoming if item.candidate_id == preferred_id), None
    ) or _pick_new_subject_representative(incoming)
    return _merge_candidates(
        representative.candidate,
        [item.candidate for item in incoming if item is not representative],
    )


def _reconstruct_subject(
    subject: DiscoverySubject, candidates_by_id: dict[UUID, DiscoveryCandidate]
) -> DiscoverySubject | None:
    member_ids = _active_member_ids(
        (reference.candidate_id for reference in subject.member_references), candidates_by_id
    )
    if not member_ids:
        return None
    return _subject_from_member_ids(
        subject.subject_id,
        subject.created_at,
        member_ids,
        candidates_by_id,
        preferred_id=subject.candidate.id,
    )


def _merge_candidates(
    base: CandidateTopic, incoming: Sequence[CandidateTopic]
) -> tuple[CandidateTopic, list[str]]:
    result = deepcopy(base)
    warnings: list[str] = []
    all_candidates = [base, *incoming]
    for field_name in (
        "uncertainties",
        "relevance_reasons",
        "actors",
        "campaigns",
        "malware",
        "cves",
        "victims",
        "sectors",
        "countries",
        "likely_artifacts",
        "iocs",
    ):
        values: dict[str, str] = {}
        for candidate in all_candidates:
            for value in getattr(candidate, field_name):
                values.setdefault(normalize(value), value)
        setattr(result, field_name, tuple(values[key] for key in sorted(values)))
    result.technical_potential = max(item.technical_potential for item in all_candidates)
    result.sources, source_id_remap = _merge_sources(all_candidates, warnings)
    result.provisional_iocs = _merge_iocs(all_candidates)
    if source_id_remap:
        result.provisional_iocs = remap_ioc_publication_ids(
            result.provisional_iocs, source_id_remap
        )
    # Same-subject only: an incomplete source here might now match a full
    # source that arrived from a *different* contribution to this same
    # subject, so recover it against the just-merged `result.sources` rather
    # than only what its own contribution originally had.
    result.incomplete_sources = recover_incomplete_source_urls(
        result.sources, _merge_incomplete_sources(all_candidates)
    )
    # D10: title, summary and creation-facing prose always come from the existing
    # subject (or the deterministic representative for a new subject).
    return result, warnings


def _merge_sources(
    candidates: Sequence[CandidateTopic], warnings: list[str]
) -> tuple[list[SourceCandidate], dict[UUID, UUID]]:
    """Fold every contribution's sources into one list, one row per real article.

    Sources are the same publication per `same_publication` (exact
    canonical_url, or a corroborated title-fingerprint match) — not raw
    canonical_url equality alone, since the same article is often cited via
    slightly different URL shapes across independent report runs. Returns
    the merged list plus a map from each folded-away source's id to the
    surviving source's id, so callers can keep IOC publication references
    pointing at a source that still exists.
    """
    role_priority = {
        SourceRole.PRIMARY: 5,
        SourceRole.INDEPENDENT: 4,
        SourceRole.RELAY: 3,
        SourceRole.AGGREGATOR: 2,
        SourceRole.SOCIAL: 2,
        SourceRole.UNKNOWN: 1,
    }
    merged: list[SourceCandidate] = []
    remap: dict[UUID, UUID] = {}
    for candidate in candidates:
        for incoming in candidate.sources:
            existing = next((item for item in merged if same_publication(item, incoming)), None)
            if existing is None:
                merged.append(deepcopy(incoming))
                continue
            if "url_replaced_manually" in incoming.parsing_warnings:
                # A manual replacement is an explicit correction of the
                # publication identity. Keep the incoming URL even though
                # same_publication deliberately recognizes the old metadata
                # as the same article, and remap IOC relations to its id.
                merged[merged.index(existing)] = deepcopy(incoming)
                remap[existing.id] = incoming.id
                continue
            remap[incoming.id] = existing.id
            if existing.publisher.casefold() in {"", "unknown"}:
                existing.publisher = incoming.publisher
            elif (
                incoming.publisher.casefold() not in {"", "unknown"}
                and existing.publisher != incoming.publisher
            ):
                chosen = min(
                    existing.publisher,
                    incoming.publisher,
                    key=lambda value: (value.casefold(), value),
                )
                warnings.append(f"publisher conflict for {existing.canonical_url}: kept {chosen}")
                existing.publisher = chosen
            for field_name in ("published_at", "event_date"):
                current = getattr(existing, field_name)
                value = getattr(incoming, field_name)
                if current is None or (value is not None and value < current):
                    setattr(existing, field_name, value)
                elif value is not None and current != value:
                    warnings.append(f"{field_name} conflict for {existing.canonical_url}")
            if role_priority[incoming.role] > role_priority[existing.role]:
                existing.role = incoming.role
            if existing.canonical_url != incoming.canonical_url:
                warnings.append(
                    f"folded near-duplicate publication {incoming.canonical_url} "
                    f"into {existing.canonical_url}"
                )
            existing.parsing_warnings = tuple(
                dict.fromkeys((*existing.parsing_warnings, *incoming.parsing_warnings))
            )
    return sorted(merged, key=lambda item: item.canonical_url), remap


def _merge_incomplete_sources(
    candidates: Sequence[CandidateTopic],
) -> list[IncompleteSourceCandidate]:
    """Fold incomplete (no-URL) publications the same way `_merge_sources` does.

    The previous key (`local_ref:raw_url:title`) included `local_ref`, which
    is batch-local and not stable across independent LLM report runs, so a
    repeatedly-recited no-URL article never collapsed across contributions.
    """
    merged: list[IncompleteSourceCandidate] = []
    for candidate in candidates:
        for incoming in candidate.incomplete_sources:
            existing = next((item for item in merged if same_publication(item, incoming)), None)
            if existing is None:
                merged.append(deepcopy(incoming))
                continue
            if existing.publisher.casefold() in {"", "unknown"}:
                existing.publisher = incoming.publisher
            if existing.raw_url is None:
                existing.raw_url = incoming.raw_url
            if existing.published_at is None:
                existing.published_at = incoming.published_at
            existing.parsing_warnings = tuple(
                dict.fromkeys((*existing.parsing_warnings, *incoming.parsing_warnings))
            )
    return sorted(merged, key=lambda item: (item.local_ref or "", item.title))


def _merge_iocs(candidates: Sequence[CandidateTopic]) -> list[ProvisionalDiscoveryIoc]:
    values: dict[tuple[str, str], ProvisionalDiscoveryIoc] = {}
    for candidate in candidates:
        for ioc in candidate.provisional_iocs:
            key = (ioc.proposed_type.value, (ioc.normalized_value or ioc.raw_value).casefold())
            values.setdefault(key, deepcopy(ioc))
    return [values[key] for key in sorted(values)]


def _unique_member_references(
    references: Iterable[DiscoveryMemberReference],
) -> tuple[DiscoveryMemberReference, ...]:
    materialized = list(references)
    unique = {item.candidate_id: item for item in materialized}
    return tuple(
        unique[key] for key in sorted(unique, key=str)
    )


def _snapshot_hash(subjects: Sequence[DiscoverySubject]) -> str:
    return canonical_sha256(
        [
            {
                "subject_id": str(subject.subject_id),
                "candidate": _candidate_content(subject.candidate),
                "member_references": sorted(
                    str(ref.candidate_id) for ref in subject.member_references
                ),
            }
            for subject in sorted(subjects, key=lambda item: str(item.subject_id))
        ]
    )


def _assert_non_loss(
    parent: DiscoverySnapshot | None,
    delta: DiscoveryDelta,
    result: DiscoverySnapshot,
    candidates_by_id: dict[UUID, DiscoveryCandidate],
) -> None:
    final_sources = [source for subject in result.subjects for source in subject.candidate.sources]
    expected_sources = [
        source
        for candidate in delta.candidates
        if candidate.candidate_id in candidates_by_id
        for source in candidates_by_id[candidate.candidate_id].candidate.sources
    ]
    if parent is not None:
        expected_sources.extend(
            source
            for subject in parent.subjects
            for reference in subject.member_references
            if reference.candidate_id in candidates_by_id
            for source in candidates_by_id[reference.candidate_id].candidate.sources
        )
        parent_refs = {
            ref.candidate_id
            for subject in parent.subjects
            for ref in subject.member_references
            if ref.candidate_id in candidates_by_id
        }
        final_refs = {
            ref.candidate_id
            for subject in result.subjects
            for ref in subject.member_references
        }
        if not parent_refs <= final_refs:
            raise RuntimeError("Discovery merge lost member references")
    # A source counts as preserved if it's still present directly, or if it
    # was intentionally folded into a surviving near-duplicate publication
    # (see `_merge_sources` / `same_publication`) — not just exact
    # canonical_url equality, since that folding is the whole point of the fix.
    lost = [
        source
        for source in expected_sources
        if not any(same_publication(source, final) for final in final_sources)
    ]
    if lost:
        raise RuntimeError("Discovery merge lost sources")
