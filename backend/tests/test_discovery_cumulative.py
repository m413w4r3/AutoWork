from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery_cumulative import (
    HeuristicMergePlanner,
    apply_discovery_merge_plan,
    build_discovery_delta,
    build_merge_handles,
    make_merge_run,
    validate_merge_plan,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    discovery_candidate_key,
)


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
    assert applied.contributions[0].candidate_key == discovery_candidate_key(intake.id, "S1")


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


def _candidate(title: str, url: str, *, summary: str = "Summary") -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary=summary,
        novelty="Novelty",
        technical_potential=3,
        uncertainties=(),
        relevance_reasons=("Relevant",),
        actors=(),
        campaigns=(),
        malware=(),
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
        local_ref="S1",
    )


def _batch(edition_id: UUID, candidates: list[CandidateTopic]) -> DiscoveryBatch:
    now = datetime.now(UTC)
    for index, candidate in enumerate(candidates, 1):
        candidate.local_ref = f"S{index}"
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=uuid4().hex * 2,
        complementary_axis="initial",
        queries=(),
        citations=(),
        contributions=[
            DiscoveryContribution(
                candidate=candidate,
                status=ContributionStatus.ACCEPTED,
                created_at=now,
                accepted_at=now,
            )
            for candidate in candidates
        ],
        discovery_model_run_id=uuid4(),
        structuring_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
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
