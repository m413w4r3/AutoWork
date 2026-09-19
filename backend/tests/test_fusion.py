from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.discovery.cumulative.apply import (
    apply_discovery_merge_plan as apply_plan,
)
from cti_app.application.discovery.cumulative.apply import (
    apply_structural_subject_merge,
    apply_structural_subject_split,
)
from cti_app.application.discovery.cumulative.context import (
    build_discovery_delta,
    build_merge_handles,
)
from cti_app.application.discovery.cumulative.merge_runs import make_merge_run
from cti_app.application.discovery.cumulative.planners import HeuristicMergePlanner
from cti_app.application.discovery.cumulative.validation import validate_candidate_coverage
from cti_app.application.discovery.fusion import (
    FusionReviewAction,
    _coalesce_existing_targets,
    _model_suggestion,
    _signals,
)
from cti_app.domain.discovery import DiscoveryCandidate
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
)
from tests.discovery_support import canonical_candidates_for
from tests.test_discovery_cumulative import _batch, _candidate, _intake


@pytest.mark.asyncio
async def test_structural_merge_and_split_reorganize_memberships_only() -> None:
    edition_id = uuid4()
    batch = _batch(
        edition_id,
        [
            _candidate("First", "https://example.test/one"),
            _candidate("Second", "https://example.test/two"),
        ],
    )
    intake = _intake(batch)
    candidates = tuple(
        DiscoveryCandidate.from_candidate_topic(
            item,
            discovery_run_id=batch.discovery_run_id,
            discovery_batch_id=batch.id,
            position=index,
        )
        for index, item in enumerate(batch.candidates)
    )
    delta = build_discovery_delta(intake, candidates)
    handles = build_merge_handles(None, delta)
    planner = HeuristicMergePlanner()
    outcome = await planner.plan(
        None,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=False,
        sensitivity="internal",
    )
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
    )
    snapshot = apply_plan(
        None,
        delta,
        outcome.plan,
        candidates,
        resolved_handles=handles,
        planner_kind=run.planner_kind,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    ).snapshot
    merged = apply_structural_subject_merge(
        snapshot,
        candidates,
        edition_id=edition_id,
        subject_ids=tuple(subject.subject_id for subject in snapshot.subjects),
        merge_run_id=uuid4(),
        actor_id="analyst",
    )
    assert len(merged.snapshot.subjects) == 1
    assert not merged.contributions
    split = apply_structural_subject_split(
        merged.snapshot,
        candidates,
        edition_id=edition_id,
        subject_id=merged.snapshot.subjects[0].subject_id,
        candidate_ids=(batch.candidates[0].id,),
        merge_run_id=uuid4(),
    )
    assert len(split.snapshot.subjects) == 2
    assert len(split.identities) == 1
    assert not split.contributions


def test_delta_is_built_from_persisted_candidates_only() -> None:
    edition_id = uuid4()
    same_a = _candidate("Same title", "https://example.test/same")
    same_b = _candidate("Same title", "https://example.test/same")
    batch = _batch(edition_id, [same_a, same_b])
    # Identical local_ref inside one batch never merges identities. The cross-run
    # variant lives in tests/integration/test_fusion_workflow.py::
    # test_same_local_ref_in_two_runs_keeps_distinct_canonical_identities.
    same_a.local_ref = same_b.local_ref = "S1"
    intake = _intake(batch)
    persisted = canonical_candidates_for(batch)

    delta = build_discovery_delta(intake, persisted)
    reordered = build_discovery_delta(intake, list(reversed(persisted)))
    batch.candidates[0].title = "Artificial batch-only mutation"
    batch.candidates.pop()
    rebuilt = build_discovery_delta(intake, persisted)

    assert [item.candidate_id for item in delta.candidates] == [same_a.id, same_b.id]
    assert len({item.candidate_id for item in delta.candidates}) == 2
    assert delta.delta_hash == reordered.delta_hash == rebuilt.delta_hash
    assert [item.candidate.title for item in rebuilt.candidates] == ["Same title"] * 2


def test_candidate_coverage_requires_exactly_one_structural_home() -> None:
    edition_id = uuid4()
    batch = _batch(
        edition_id,
        [
            _candidate("First", "https://example.test/one"),
            _candidate("Second", "https://example.test/two"),
        ],
    )
    first, second = canonical_candidates_for(batch)
    subject = DiscoverySubject(
        subject_id=uuid4(),
        candidate=first.to_candidate_topic(),
        member_references=(DiscoveryMemberReference(first.id),),
        created_at=datetime.now(UTC),
    )
    snapshot = DiscoverySnapshot(
        edition_id=edition_id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.HUMAN,
        subjects=(subject,),
        snapshot_hash="a" * 64,
    )

    validate_candidate_coverage([first, second], snapshot, pending_candidate_ids={second.id})
    with pytest.raises(ValueError, match="structural home"):
        validate_candidate_coverage([first, second], snapshot)
    with pytest.raises(ValueError, match="structural home"):
        validate_candidate_coverage(
            [first, second], snapshot, pending_candidate_ids={first.id, second.id}
        )


def test_fusion_signals_are_deterministic_and_separate_differences() -> None:
    edition_id = uuid4()
    first = _candidate("APT-X campaign", "https://example.test/a")
    first.actors = ("APT-X",)
    first.cves = ("CVE-2026-1234",)
    first.malware = ("Foo",)
    second = _candidate("APT-X campaign update", "https://example.test/b")
    second.actors = ("apt-x",)
    second.cves = ("CVE-2026-1234",)
    second.malware = ("Foo", "Bar")
    batch = _batch(edition_id, [first, second])
    candidates = tuple(
        DiscoveryCandidate.from_candidate_topic(
            item,
            discovery_run_id=batch.discovery_run_id,
            discovery_batch_id=batch.id,
            position=index,
        )
        for index, item in enumerate(batch.candidates)
    )

    signals, differences = _signals(candidates)
    reversed_signals, reversed_differences = _signals(tuple(reversed(candidates)))

    both = {first.id, second.id}
    by_kind = {(signal.kind, signal.value): set(signal.candidate_ids) for signal in signals}
    assert by_kind[("shared_actor", "APT-X")] == both
    assert by_kind[("shared_cve", "CVE-2026-1234")] == both
    assert by_kind[("shared_source_domain", "example.test")] == both
    assert ("shared_canonical_url", "https://example.test/a") not in by_kind
    assert any("Bar" in difference for difference in differences)
    assert {(signal.kind, signal.value) for signal in signals} == {
        (signal.kind, signal.value) for signal in reversed_signals
    }
    assert set(differences) == set(reversed_differences)
    assert FusionReviewAction.ATTACH.value == "attach"


def test_human_attachments_to_the_same_subject_are_coalesced() -> None:
    first = DiscoveryMergeGroup(
        existing_subject_handles=["X-target"],
        incoming_candidate_handles=["C1"],
        confidence=MergeConfidence.HIGH,
        disposition=MergeDisposition.APPLY,
        rationale="Attach first",
        evidence=MergeEvidence(shared_campaigns=["Campaign"]),
        flags=["human_decided"],
    )
    second = DiscoveryMergeGroup(
        existing_subject_handles=["X-target"],
        incoming_candidate_handles=["C2"],
        confidence=MergeConfidence.MEDIUM,
        disposition=MergeDisposition.REVIEW,
        rationale="Attach second",
        evidence=MergeEvidence(shared_malware=["Malware"]),
        flags=["human_decided"],
    )

    coalesced = _coalesce_existing_targets(DiscoveryMergePlanV1(groups=[first, second]))

    assert len(coalesced.groups) == 1
    assert coalesced.groups[0].existing_subject_handles == ["X-target"]
    assert coalesced.groups[0].incoming_candidate_handles == ["C1", "C2"]
    assert coalesced.groups[0].evidence.shared_campaigns == ["Campaign"]
    assert coalesced.groups[0].evidence.shared_malware == ["Malware"]


def test_human_successor_keeps_the_persisted_model_suggestion_visible() -> None:
    model_run = DiscoveryMergeRun(
        edition_id=uuid4(),
        parent_snapshot_id=None,
        intake_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.CHATGPT,
        prompt_version="1",
        policy_version="model-v1",
        blocking_version="all-v1",
        merge_input_hash="a" * 64,
        handle_map={"C1": str(uuid4())},
        included_subject_ids=(),
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.NEEDS_REVIEW,
    )
    human_run = DiscoveryMergeRun(
        edition_id=model_run.edition_id,
        parent_snapshot_id=None,
        intake_id=model_run.intake_id,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        prompt_version="none",
        policy_version="fusion-human-v1",
        blocking_version="fusion-v1",
        merge_input_hash="b" * 64,
        handle_map=model_run.handle_map,
        included_subject_ids=(),
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.NEEDS_REVIEW,
        supersedes_merge_run_id=model_run.id,
    )
    group = DiscoveryMergeGroup(
        existing_subject_handles=[],
        incoming_candidate_handles=["C1", "C2"],
        confidence=MergeConfidence.MEDIUM,
        disposition=MergeDisposition.REVIEW,
        rationale="Same campaign with one unresolved date conflict.",
    )

    suggestion = _model_suggestion(
        human_run,
        group,
        {model_run.id: model_run, human_run.id: human_run},
    )

    assert suggestion is not None
    assert suggestion.recommendation == "merge"
    assert suggestion.summary == group.rationale
