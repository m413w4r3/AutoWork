from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery.cumulative.apply import (
    apply_discovery_merge_plan as _apply_discovery_merge_plan,
)
from cti_app.application.discovery.cumulative.apply import apply_structural_subject_merge
from cti_app.application.discovery.cumulative.context import (
    build_discovery_delta as _build_discovery_delta,
)
from cti_app.application.discovery.cumulative.context import build_merge_handles
from cti_app.application.discovery.cumulative.merge_runs import make_merge_run
from cti_app.application.discovery.cumulative.planners import (
    DeterministicBootstrapPlanner,
    HeuristicMergePlanner,
    bootstrap_intake_collision,
)
from cti_app.application.discovery.cumulative.service import _applied_diagnostic_event
from cti_app.application.discovery.cumulative.types import AppliedDiscoveryMerge, DiscoveryDelta
from cti_app.application.discovery.cumulative.validation import (
    merge_plan_review_reasons,
    validate_merge_plan,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
)


def _canonical_candidates(batch: DiscoveryBatch) -> tuple[DiscoveryCandidate, ...]:
    return tuple(
        DiscoveryCandidate.from_candidate_topic(
            candidate,
            discovery_run_id=batch.discovery_run_id,
            discovery_batch_id=batch.id,
            position=index,
        )
        for index, candidate in enumerate(batch.candidates)
    )


def build_discovery_delta(intake: DiscoveryIntake, batch: DiscoveryBatch) -> DiscoveryDelta:
    return _build_discovery_delta(intake, _canonical_candidates(batch))


def apply_discovery_merge_plan(
    parent: DiscoverySnapshot | None,
    delta: DiscoveryDelta,
    plan: DiscoveryMergePlanV1,
    **kwargs: Any,
) -> AppliedDiscoveryMerge:
    """Test adapter: materialize the canonical candidates the service would load."""
    candidates: list[DiscoveryCandidate] = []
    for item in delta.candidates:
        topic = deepcopy(item.candidate)
        topic.id = item.candidate_id
        candidates.append(
            DiscoveryCandidate.from_candidate_topic(
                topic,
                discovery_run_id=uuid4(),
                discovery_batch_id=uuid4(),
                position=len(candidates),
            )
        )
    if parent is not None:
        known = {candidate.id for candidate in candidates}
        for subject in parent.subjects:
            for reference in subject.member_references:
                if reference.candidate_id in known:
                    continue
                topic = deepcopy(subject.candidate)
                topic.id = reference.candidate_id
                candidates.append(
                    DiscoveryCandidate.from_candidate_topic(
                        topic,
                        discovery_run_id=uuid4(),
                        discovery_batch_id=uuid4(),
                        position=len(candidates),
                    )
                )
    return _apply_discovery_merge_plan(parent, delta, plan, candidates, **kwargs)


@pytest.mark.asyncio
async def test_bootstrap_uses_same_applier_and_local_stable_ids() -> None:
    edition_id = uuid4()
    batch = _batch(edition_id, [_candidate("A", "https://example.test/a")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    planner = HeuristicMergePlanner()
    plan = (
        await planner.plan(
            None,
            delta,
            handles,
            edition_id=edition_id,
            external_llm_allowed=True,
            sensitivity="internal",
        )
    ).plan
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
    )

    applied = apply_discovery_merge_plan(
        None,
        delta,
        plan,
        resolved_handles=handles,
        planner_kind=run.planner_kind,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    )

    assert applied.snapshot.version == 1
    assert applied.snapshot.subjects[0].subject_id == applied.identities[0].id
    assert applied.contributions[0].subject_id == applied.identities[0].id
    assert applied.contributions[0].candidate_id == batch.candidates[0].id


@pytest.mark.asyncio
async def test_bootstrap_proposes_coherent_research_parts_for_fusion_review() -> None:
    edition_id = uuid4()
    candidates = [
        _candidate(
            f"Operation Kite research part {index}",
            "https://example.test/research?utm_source=monthly-report",
            summary=f"Part {index} describes credential theft through a fake browser update.",
            actors=("Example APT",),
            campaigns=("Operation Kite",),
            malware=("KiteLoader",),
            event_date=date(2026, 7, 10),
            novelty="credential theft via fake browser updates",
        )
        for index in range(1, 4)
    ]
    batch = _batch(edition_id, candidates)
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    planner = DeterministicBootstrapPlanner()

    outcome = await planner.plan(
        None,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=False,
        sensitivity="internal",
    )

    assert len(outcome.plan.groups) == 1
    proposal = outcome.plan.groups[0]
    assert proposal.incoming_candidate_handles == ["C1", "C2", "C3"]
    assert proposal.disposition is MergeDisposition.REVIEW
    assert proposal.confidence is MergeConfidence.HIGH
    assert proposal.flags == ["intake_collision"]
    assert proposal.evidence.shared_campaigns == ["Operation Kite"]
    assert proposal.evidence.shared_malware == ["KiteLoader"]
    assert proposal.evidence.shared_publication_urls
    assert all(candidate.title in proposal.rationale for candidate in candidates)
    assert "actor=Example APT" in proposal.rationale
    assert "campaign=Operation Kite" in proposal.rationale
    assert "event_date=2026-07-10" in proposal.rationale
    assert "mechanism=" in proposal.rationale
    assert "Analyst decision required" in proposal.rationale
    assert merge_plan_review_reasons(outcome.plan)


@pytest.mark.asyncio
async def test_monthly_report_keeps_independent_campaigns_as_three_subjects() -> None:
    edition_id = uuid4()
    candidates = [
        _candidate(
            f"Campaign {index}",
            "https://example.test/monthly-report",
            actors=("Example APT",),
            campaigns=(f"Independent campaign {index}",),
            malware=(f"Family {index}",),
            event_date=date(2026, 7, 10),
        )
        for index in range(1, 4)
    ]
    batch = _batch(edition_id, candidates)
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    planner = DeterministicBootstrapPlanner()
    outcome = await planner.plan(
        None,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
        outcome=outcome,
    )

    applied = apply_discovery_merge_plan(
        None,
        delta,
        outcome.plan,
        resolved_handles=handles,
        planner_kind=run.planner_kind,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    )

    assert len(applied.snapshot.subjects) == 3
    assert all(len(group.incoming_candidate_handles) == 1 for group in outcome.plan.groups)
    assert all(group.disposition is MergeDisposition.APPLY for group in outcome.plan.groups)
    assert not applied.merge_events


def test_bootstrap_collision_rule_requires_anchors_and_uses_period_and_mechanism() -> None:
    left = _candidate(
        "Research section A",
        "https://example.test/report",
        summary="Credential theft via fake browser updates",
        actors=("Example APT",),
        campaigns=("Operation Kite",),
        event_date=date(2026, 7, 10),
    )
    same_scope = _candidate(
        "Research section B",
        "https://example.test/report?utm_medium=share",
        summary="Credential theft via fake browser updates",
        actors=("Example APT",),
        campaigns=("Operation Kite",),
        event_date=date(2026, 7, 10),
    )

    strong = bootstrap_intake_collision(left, same_scope)
    assert strong is not None
    assert strong.confidence is MergeConfidence.HIGH

    no_period = _candidate(
        "Research section B",
        "https://example.test/report",
        actors=("Example APT",),
        campaigns=("Operation Kite",),
    )
    ambiguous_document = bootstrap_intake_collision(left, no_period)
    assert ambiguous_document is not None
    assert ambiguous_document.confidence is MergeConfidence.MEDIUM

    url_only = _candidate("Another subject", "https://example.test/report")
    assert bootstrap_intake_collision(left, url_only) is None

    title_only = _candidate("Research section A update", "https://example.test/other")
    assert bootstrap_intake_collision(left, title_only) is None

    different_campaign = _candidate(
        "Another activity",
        "https://example.test/report",
        actors=("Example APT",),
        campaigns=("Independent Operation",),
        event_date=date(2026, 7, 10),
    )
    assert bootstrap_intake_collision(left, different_campaign) is None

    mechanism_support = _candidate(
        "A separate publication",
        "https://different.example.test/article",
        summary="Credential theft via fake browser updates",
        actors=("Example APT",),
        campaigns=("Operation Kite",),
    )
    ambiguous_mechanism = bootstrap_intake_collision(left, mechanism_support)
    assert ambiguous_mechanism is not None
    assert ambiguous_mechanism.confidence is MergeConfidence.MEDIUM


def test_bootstrap_audit_uses_a_non_merge_event_when_no_merge_event_exists() -> None:
    assert (
        _applied_diagnostic_event(
            DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            merge_event_count=0,
        )
        == "discovery.bootstrap_applied"
    )
    assert (
        _applied_diagnostic_event(DiscoveryPlannerKind.HEURISTIC, merge_event_count=0)
        == "merge.applied"
    )
    assert (
        _applied_diagnostic_event(
            DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            merge_event_count=1,
        )
        == "merge.applied"
    )


@pytest.mark.asyncio
async def test_rediscovery_after_manual_merge_keeps_absorbed_identity_and_history() -> None:
    edition_id = uuid4()
    candidates = [
        _candidate(
            "Campaign A",
            "https://example.test/a",
            actors=("Example APT",),
            campaigns=("Operation A",),
            malware=("Family A",),
        ),
        _candidate(
            "Campaign B",
            "https://example.test/b",
            actors=("Example APT",),
            campaigns=("Operation B",),
            malware=("Family B",),
        ),
    ]
    first_batch = _batch(edition_id, candidates)
    first_intake = _intake(first_batch)
    first_delta = build_discovery_delta(first_intake, first_batch)
    first_handles = build_merge_handles(None, first_delta)
    bootstrap = DeterministicBootstrapPlanner()
    first_outcome = await bootstrap.plan(
        None,
        first_delta,
        first_handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )
    first_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=first_intake,
        delta=first_delta,
        planner=bootstrap,
        handles=first_handles,
        outcome=first_outcome,
    )
    first_applied = apply_discovery_merge_plan(
        None,
        first_delta,
        first_outcome.plan,
        resolved_handles=first_handles,
        planner_kind=first_run.planner_kind,
        edition_id=edition_id,
        intake_id=first_intake.id,
        merge_run_id=first_run.id,
    )
    original_subject_ids = {subject.subject_id for subject in first_applied.snapshot.subjects}
    original_contributions = tuple(first_applied.contributions)
    canonical_candidates = _canonical_candidates(first_batch)

    manual_merge = apply_structural_subject_merge(
        first_applied.snapshot,
        canonical_candidates,
        edition_id=edition_id,
        subject_ids=tuple(original_subject_ids),
        merge_run_id=uuid4(),
        actor_id="analyst",
    )
    survivor_id = manual_merge.snapshot.subjects[0].subject_id
    absorbed_ids = original_subject_ids - {survivor_id}
    assert len(absorbed_ids) == 1
    assert manual_merge.merge_events[0].from_subject_id in absorbed_ids
    assert manual_merge.merge_events[0].into_subject_id == survivor_id
    assert not manual_merge.contributions

    rediscovery_candidates = [
        _candidate(
            candidate.title,
            candidate.sources[0].url,
            actors=candidate.actors,
            campaigns=candidate.campaigns,
            malware=candidate.malware,
        )
        for candidate in candidates
    ]
    second_batch = _batch(edition_id, rediscovery_candidates)
    second_intake = _intake(second_batch)
    second_delta = build_discovery_delta(second_intake, second_batch)
    second_handles = build_merge_handles(manual_merge.snapshot, second_delta)
    heuristic = HeuristicMergePlanner()
    second_outcome = await heuristic.plan(
        manual_merge.snapshot,
        second_delta,
        second_handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )
    second_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=manual_merge.snapshot,
        intake=second_intake,
        delta=second_delta,
        planner=heuristic,
        handles=second_handles,
        outcome=second_outcome,
    )
    rediscovered = apply_discovery_merge_plan(
        manual_merge.snapshot,
        second_delta,
        second_outcome.plan,
        resolved_handles=second_handles,
        planner_kind=second_run.planner_kind,
        edition_id=edition_id,
        intake_id=second_intake.id,
        merge_run_id=second_run.id,
    )

    assert {subject.subject_id for subject in rediscovered.snapshot.subjects} == {survivor_id}
    assert not (absorbed_ids & {subject.subject_id for subject in rediscovered.snapshot.subjects})
    assert not rediscovered.identities
    assert not rediscovered.merge_events
    assert all(item.subject_id == survivor_id for item in rediscovered.contributions)
    assert tuple(first_applied.contributions) == original_contributions
    assert {item.subject_id for item in original_contributions} == original_subject_ids


@pytest.mark.asyncio
async def test_enrichment_keeps_identity_title_and_all_sources() -> None:
    edition_id = uuid4()
    planner = HeuristicMergePlanner()
    first_batch = _batch(
        edition_id,
        [_candidate("Canonical title", "https://example.test/a", summary="Original summary")],
    )
    first_intake = _intake(first_batch)
    first_delta = build_discovery_delta(first_intake, first_batch)
    first_handles = build_merge_handles(None, first_delta)
    first_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=first_intake,
        delta=first_delta,
        planner=planner,
        handles=first_handles,
    )
    first = apply_discovery_merge_plan(
        None,
        first_delta,
        (
            await planner.plan(
                None,
                first_delta,
                first_handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan,
        resolved_handles=first_handles,
        planner_kind=first_run.planner_kind,
        edition_id=edition_id,
        intake_id=first_intake.id,
        merge_run_id=first_run.id,
    ).snapshot

    second_batch = _batch(
        edition_id,
        [_candidate("Canonical title", "https://example.test/b", summary="Rewritten summary")],
    )
    second_intake = _intake(second_batch)
    second_delta = build_discovery_delta(second_intake, second_batch)
    second_handles = build_merge_handles(first, second_delta)
    second_plan = (
        await planner.plan(
            first,
            second_delta,
            second_handles,
            edition_id=edition_id,
            external_llm_allowed=True,
            sensitivity="internal",
        )
    ).plan
    second_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=first,
        intake=second_intake,
        delta=second_delta,
        planner=planner,
        handles=second_handles,
    )
    result = apply_discovery_merge_plan(
        first,
        second_delta,
        second_plan,
        resolved_handles=second_handles,
        planner_kind=second_run.planner_kind,
        edition_id=edition_id,
        intake_id=second_intake.id,
        merge_run_id=second_run.id,
    ).snapshot

    assert len(result.subjects) == 1
    assert result.subjects[0].subject_id == first.subjects[0].subject_id
    assert result.subjects[0].canonical_title == "Canonical title"
    assert result.subjects[0].canonical_summary == "Original summary"
    assert {source.canonical_url for source in result.subjects[0].candidate.sources} == {
        "https://example.test/a",
        "https://example.test/b",
    }


@pytest.mark.asyncio
async def test_cross_batch_near_duplicate_urls_collapse_into_one_source() -> None:
    """Regression test for the "same article proposed 10+ times" bug.

    Independent report runs often cite the same article via slightly
    different URL shapes; each contribution here uses a distinct mirror URL
    for the same title+publisher, and the merged subject must end up with
    exactly one source, not one per contribution.
    """
    edition_id = uuid4()
    planner = HeuristicMergePlanner()
    subject_title = "Iran central bank says no new disruption after banking cyberattack"
    snapshot: DiscoverySnapshot | None = None
    for index, url in enumerate(
        [
            "https://mirror-one.example/story?ref=123",
            "https://mirror-two.example/story?session=abc",
            "https://mirror-three.example/story?tracking=xyz",
        ]
    ):
        batch = _batch(
            edition_id, [_candidate(subject_title, url, summary=f"Contribution {index}")]
        )
        intake = _intake(batch)
        delta = build_discovery_delta(intake, batch)
        handles = build_merge_handles(snapshot, delta)
        plan = (
            await planner.plan(
                snapshot,
                delta,
                handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan
        run = make_merge_run(
            edition_id=edition_id,
            parent_snapshot=snapshot,
            intake=intake,
            delta=delta,
            planner=planner,
            handles=handles,
        )
        snapshot = apply_discovery_merge_plan(
            snapshot,
            delta,
            plan,
            resolved_handles=handles,
            planner_kind=run.planner_kind,
            edition_id=edition_id,
            intake_id=intake.id,
            merge_run_id=run.id,
        ).snapshot

    assert snapshot is not None
    assert len(snapshot.subjects) == 1
    assert len(snapshot.subjects[0].candidate.sources) == 1


@pytest.mark.asyncio
async def test_cross_batch_repeated_incomplete_citation_does_not_balloon() -> None:
    """The incomplete-source counterpart of the near-duplicate-URL test above."""
    edition_id = uuid4()
    planner = HeuristicMergePlanner()
    snapshot: DiscoverySnapshot | None = None
    for index in range(3):
        batch = _batch(
            edition_id,
            [_candidate_with_incomplete("Canonical title", f"https://example.test/anchor-{index}")],
        )
        intake = _intake(batch)
        delta = build_discovery_delta(intake, batch)
        handles = build_merge_handles(snapshot, delta)
        plan = (
            await planner.plan(
                snapshot,
                delta,
                handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan
        run = make_merge_run(
            edition_id=edition_id,
            parent_snapshot=snapshot,
            intake=intake,
            delta=delta,
            planner=planner,
            handles=handles,
        )
        snapshot = apply_discovery_merge_plan(
            snapshot,
            delta,
            plan,
            resolved_handles=handles,
            planner_kind=run.planner_kind,
            edition_id=edition_id,
            intake_id=intake.id,
            merge_run_id=run.id,
        ).snapshot

    assert snapshot is not None
    assert len(snapshot.subjects) == 1
    assert len(snapshot.subjects[0].candidate.incomplete_sources) == 1


@pytest.mark.asyncio
async def test_cross_batch_incomplete_source_recovers_url_from_sibling_contribution() -> None:
    """One contribution cites the article without a URL, another (to the same
    subject) cites it with one — the merged subject should end up with the
    URL attached and no lingering incomplete entry, not both side by side."""
    edition_id = uuid4()
    planner = HeuristicMergePlanner()
    subject_title = "Iran central bank says no new disruption after banking cyberattack"

    first_batch = _batch(edition_id, [_candidate(subject_title, "https://example.test/anchor")])
    first_intake = _intake(first_batch)
    first_delta = build_discovery_delta(first_intake, first_batch)
    first_handles = build_merge_handles(None, first_delta)
    first_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=first_intake,
        delta=first_delta,
        planner=planner,
        handles=first_handles,
    )
    snapshot = apply_discovery_merge_plan(
        None,
        first_delta,
        (
            await planner.plan(
                None,
                first_delta,
                first_handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan,
        resolved_handles=first_handles,
        planner_kind=first_run.planner_kind,
        edition_id=edition_id,
        intake_id=first_intake.id,
        merge_run_id=first_run.id,
    ).snapshot

    # The second contribution's own anchor source must have a distinct title
    # from the incomplete entry below — otherwise recovery would already fire
    # intra-candidate (during this CandidateTopic's own construction) and the
    # test would not exercise the cross-batch path at all. The incomplete
    # entry instead matches the *first* contribution's anchor article.
    second_candidate = _candidate(
        subject_title, "https://example.test/anchor-2", summary="Second contribution"
    )
    second_candidate.sources = [
        SourceCandidate(
            url="https://example.test/anchor-2",
            title="An unrelated second-contribution article headline",
            publisher="Publisher",
            role=SourceRole.PRIMARY,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )
    ]
    second_candidate.incomplete_sources = [
        IncompleteSourceCandidate(
            title="Iran central bank says no new disruption after banking cyberattack",
            publisher="Publisher",
        )
    ]
    second_batch = _batch(edition_id, [second_candidate])
    second_intake = _intake(second_batch)
    second_delta = build_discovery_delta(second_intake, second_batch)
    second_handles = build_merge_handles(snapshot, second_delta)
    second_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=snapshot,
        intake=second_intake,
        delta=second_delta,
        planner=planner,
        handles=second_handles,
    )
    result = apply_discovery_merge_plan(
        snapshot,
        second_delta,
        (
            await planner.plan(
                snapshot,
                second_delta,
                second_handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan,
        resolved_handles=second_handles,
        planner_kind=second_run.planner_kind,
        edition_id=edition_id,
        intake_id=second_intake.id,
        merge_run_id=second_run.id,
    ).snapshot

    assert len(result.subjects) == 1
    assert result.subjects[0].candidate.incomplete_sources == []


@pytest.mark.asyncio
async def test_unmentioned_parent_subject_is_carried_forward_unchanged() -> None:
    edition_id = uuid4()
    planner = HeuristicMergePlanner()
    batch = _batch(
        edition_id,
        [
            _candidate("Subject A", "https://example.test/a"),
            _candidate("Subject B", "https://example.test/b"),
        ],
    )
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
    )
    parent = apply_discovery_merge_plan(
        None,
        delta,
        (
            await planner.plan(
                None,
                delta,
                handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan,
        resolved_handles=handles,
        planner_kind=run.planner_kind,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    ).snapshot
    untouched = next(
        subject for subject in parent.subjects if subject.canonical_title == "Subject B"
    )

    update_batch = _batch(edition_id, [_candidate("Subject A", "https://example.test/c")])
    update_intake = _intake(update_batch)
    update_delta = build_discovery_delta(update_intake, update_batch)
    update_handles = build_merge_handles(parent, update_delta)
    update_run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=parent,
        intake=update_intake,
        delta=update_delta,
        planner=planner,
        handles=update_handles,
    )
    result = apply_discovery_merge_plan(
        parent,
        update_delta,
        (
            await planner.plan(
                parent,
                update_delta,
                update_handles,
                edition_id=edition_id,
                external_llm_allowed=True,
                sensitivity="internal",
            )
        ).plan,
        resolved_handles=update_handles,
        planner_kind=update_run.planner_kind,
        edition_id=edition_id,
        intake_id=update_intake.id,
        merge_run_id=update_run.id,
    ).snapshot

    assert (
        next(subject for subject in result.subjects if subject.subject_id == untouched.subject_id)
        == untouched
    )


def test_plan_validation_rejects_missing_duplicate_and_unknown_handles() -> None:
    edition_id = uuid4()
    batch = _batch(
        edition_id,
        [_candidate("A", "https://example.test/a"), _candidate("B", "https://example.test/b")],
    )
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)

    for incoming in (["C1"], ["C1", "C1"], ["C1", "C2", "C404"]):
        plan = DiscoveryMergePlanV1(
            groups=[
                DiscoveryMergeGroup(
                    existing_subject_handles=[],
                    incoming_candidate_handles=incoming,
                    confidence=MergeConfidence.HIGH,
                    disposition=MergeDisposition.APPLY,
                    rationale="test",
                    evidence=MergeEvidence(),
                )
            ]
        )
        with pytest.raises(ValueError):
            validate_merge_plan(plan, handles)


def _candidate(
    title: str,
    url: str,
    *,
    summary: str = "Summary",
    novelty: str = "Novelty",
    actors: tuple[str, ...] = (),
    campaigns: tuple[str, ...] = (),
    malware: tuple[str, ...] = (),
    event_date: date | None = None,
) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary=summary,
        novelty=novelty,
        technical_potential=3,
        uncertainties=(),
        relevance_reasons=("Relevant",),
        actors=actors,
        campaigns=campaigns,
        malware=malware,
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[
            SourceCandidate(
                url=url,
                title=title,
                publisher="Publisher",
                role=SourceRole.PRIMARY,
                published_at=date(2026, 7, 1),
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        event_date=event_date,
        local_ref="S1",
    )


def _candidate_with_incomplete(title: str, anchor_url: str) -> CandidateTopic:
    """A candidate whose only non-anchor publication has no URL.

    `anchor_url` gives the subject a stable real source so cross-batch
    identity matching (which anchors on shared entities/URLs) groups these
    contributions into the same subject; the incomplete_sources entry is the
    one under test.
    """
    candidate = _candidate(title, anchor_url)
    candidate.incomplete_sources = [
        IncompleteSourceCandidate(
            title="Iran central bank says no new disruption after banking cyberattack",
            publisher="bne IntelliNews",
        )
    ]
    return candidate


def _batch(edition_id: UUID, candidates: list[CandidateTopic]) -> DiscoveryBatch:
    for index, candidate in enumerate(candidates, 1):
        candidate.local_ref = f"S{index}"
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=uuid4().hex * 2,
        complementary_axis="initial",
        queries=(),
        citations=(),
        candidates=candidates,
        discovery_run_id=uuid4(),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test-parser-v1",
        report_sha256=uuid4().hex * 2,
        source_mode=DiscoverySourceMode.VISIBLE_CITATIONS_ONLY,
    )


def _intake(batch: DiscoveryBatch) -> DiscoveryIntake:
    return DiscoveryIntake(
        edition_id=batch.edition_id,
        sequence=1,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        raw_report_hash=batch.report_sha256 or batch.request_hash,
        parsed_report_hash="a" * 64,
        intake_hash="b" * 64,
        research_model_run_id=batch.discovery_model_run_id,
        source_mode=batch.source_mode,
        complementary_axis=batch.complementary_axis,
        batch_id=batch.id,
        created_by="test",
    )
