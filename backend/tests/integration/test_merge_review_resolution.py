"""What happens to a parked merge once a human decides on it.

The failure these cover is not a wrong merge: it is a merge that applies
correctly and then leaves the review panel showing the same decision forever,
because nothing retires the run that was just settled. Clicking again then
replays a snapshot id derived from (parent, intake, run) and dies on the
primary key, which reaches the browser as an unexplained server error.
"""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from cti_app.application.discovery_cumulative import (
    CumulativeDiscoveryService,
    DiscoveryDelta,
    DiscoveryMergeNeedsReview,
    DiscoverySnapshotStaleError,
    HumanMergeDecision,
    PlannedDiscoveryMerge,
    ReconcileDiscoveryParameters,
    ResolvedMergeHandles,
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
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


class ParkingPlanner:
    """Puts every incoming candidate in its own group, always needing review."""

    kind = DiscoveryPlannerKind.CHATGPT
    policy_version = "parking-test-v1"

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
        return PlannedDiscoveryMerge(
            DiscoveryMergePlanV1(
                groups=[
                    DiscoveryMergeGroup(
                        existing_subject_handles=[],
                        incoming_candidate_handles=[handle],
                        # Medium confidence is what forces the human stop.
                        confidence=MergeConfidence.MEDIUM,
                        disposition=MergeDisposition.REVIEW,
                        rationale="test parking",
                        evidence=MergeEvidence(),
                    )
                    for handle in sorted(handles.incoming)
                ]
            )
        )


async def test_resolving_a_merge_retires_it_and_stays_idempotent(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition = _edition("Resolve Iran", "RA")
    model_run = _model_run()
    first = _batch(edition.id, model_run.id, url="https://vendor.example/one")
    second = _batch(
        edition.id,
        model_run.id,
        title="Second topic",
        url="https://vendor.example/two",
        request_hash="e" * 64,
        local_ref="S2",
    )
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(model_run)
            assert await uow.discovery_batches.add_if_absent(first)
            assert await uow.discovery_batches.add_if_absent(second)
            await uow.commit()

        service = CumulativeDiscoveryService(uow_factory, planner=ParkingPlanner())
        _, bootstrap = await service.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        intake, _ = await service.ingest_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        with pytest.raises(DiscoveryMergeNeedsReview) as parked:
            await service.reconcile_intake(
                intake.id, expected_parent_snapshot_id=bootstrap.id, actor_id="test"
            )
        run_id = parked.value.run_id

        applied = await service.resolve_merge_run(
            edition.id, run_id, [HumanMergeDecision(0, "accept")], actor_id="analyst"
        )
        assert applied.version == bootstrap.version + 1
        assert len(applied.subjects) == 2

        async with uow_factory() as uow:
            settled = await uow.discovery_merge_runs.get(run_id)
            active = await uow.discovery_snapshots.get_active(edition.id)
        assert settled is not None
        # Without this the review panel keeps offering a decision already taken.
        assert settled.validation_status is MergeValidationStatus.RESOLVED
        assert active is not None and active.id == applied.id

        # The second click of an impatient reviewer: same snapshot, no crash.
        replayed = await service.resolve_merge_run(
            edition.id, run_id, [HumanMergeDecision(0, "accept")], actor_id="analyst"
        )
        assert replayed.id == applied.id
        assert replayed.version == applied.version
    finally:
        await engine.dispose()


async def test_a_merge_planned_against_a_superseded_snapshot_is_replanned(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition = _edition("Stale Iran", "RB")
    model_run = _model_run()
    first = _batch(edition.id, model_run.id, url="https://vendor.example/one")
    second = _batch(
        edition.id,
        model_run.id,
        title="Second topic",
        url="https://vendor.example/two",
        request_hash="e" * 64,
        local_ref="S2",
    )
    third = _batch(
        edition.id,
        model_run.id,
        title="Third topic",
        url="https://vendor.example/three",
        request_hash="f" * 64,
        local_ref="S3",
    )
    replanned: list[ReconcileDiscoveryParameters] = []

    async def replan(parameters: ReconcileDiscoveryParameters) -> object:
        replanned.append(parameters)
        return None

    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(model_run)
            for batch in (first, second, third):
                assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        service = CumulativeDiscoveryService(
            uow_factory, planner=ParkingPlanner(), replan_intake=replan
        )
        _, bootstrap = await service.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        parked_intake, _ = await service.ingest_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        with pytest.raises(DiscoveryMergeNeedsReview) as parked:
            await service.reconcile_intake(
                parked_intake.id, expected_parent_snapshot_id=bootstrap.id, actor_id="test"
            )
        stale_run_id = parked.value.run_id

        # A third contribution is settled first, so the parked plan now names
        # handles resolved against a snapshot that is no longer the edition.
        other_intake, _ = await service.ingest_batch(
            third, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        with pytest.raises(DiscoveryMergeNeedsReview) as other:
            await service.reconcile_intake(
                other_intake.id, expected_parent_snapshot_id=bootstrap.id, actor_id="test"
            )
        moved_on = await service.resolve_merge_run(
            edition.id, other.value.run_id, [HumanMergeDecision(0, "accept")], actor_id="analyst"
        )

        with pytest.raises(DiscoverySnapshotStaleError):
            await service.resolve_merge_run(
                edition.id, stale_run_id, [HumanMergeDecision(0, "accept")], actor_id="analyst"
            )

        # The contribution is not dropped: it is queued for a fresh plan against
        # the snapshot that won, and the dead run stops blocking the panel.
        assert len(replanned) == 1
        assert replanned[0].intake_id == parked_intake.id
        assert replanned[0].expected_parent_snapshot_id == moved_on.id
        async with uow_factory() as uow:
            retired = await uow.discovery_merge_runs.get(stale_run_id)
        assert retired is not None
        assert retired.validation_status is MergeValidationStatus.RESOLVED
    finally:
        await engine.dispose()


async def test_a_decision_naming_an_unknown_group_is_refused(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition = _edition("Bounds Iran", "RC")
    model_run = _model_run()
    first = _batch(edition.id, model_run.id, url="https://vendor.example/one")
    second = _batch(
        edition.id,
        model_run.id,
        title="Second topic",
        url="https://vendor.example/two",
        request_hash="e" * 64,
        local_ref="S2",
    )
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(model_run)
            assert await uow.discovery_batches.add_if_absent(first)
            assert await uow.discovery_batches.add_if_absent(second)
            await uow.commit()

        service = CumulativeDiscoveryService(uow_factory, planner=ParkingPlanner())
        _, bootstrap = await service.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        intake, _ = await service.ingest_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="test"
        )
        with pytest.raises(DiscoveryMergeNeedsReview) as parked:
            await service.reconcile_intake(
                intake.id, expected_parent_snapshot_id=bootstrap.id, actor_id="test"
            )

        # Silently ignoring it would report "applied" for a group nobody decided.
        with pytest.raises(ValueError, match="n'existe pas"):
            await service.resolve_merge_run(
                edition.id,
                parked.value.run_id,
                [HumanMergeDecision(7, "accept")],
                actor_id="analyst",
            )
    finally:
        await engine.dispose()


def _edition(country: str, code: str) -> Edition:
    return Edition(
        country=country,
        country_code=code,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_major_articles=2,
        target_briefs=6,
        source_profile="iran-default",
    )


def _model_run() -> ModelRun:
    return ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="discovery",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )


def _batch(
    edition_id: UUID,
    model_run_id: UUID,
    *,
    title: str = "Stable title",
    url: str = "https://vendor.example/report",
    request_hash: str = "c" * 64,
    local_ref: str = "S1",
) -> DiscoveryBatch:
    now = datetime.now(UTC)
    candidate = CandidateTopic(
        title=title,
        summary=f"{title} summary",
        novelty="New evidence",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("Technical source",),
        actors=(f"{title} actor",),
        campaigns=(f"{title} campaign",),
        malware=(f"{title} malware",),
        cves=(),
        victims=(),
        sectors=("government",),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=[
            SourceCandidate(
                url=url,
                title="Report",
                publisher="Vendor",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        local_ref=local_ref,
    )
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=request_hash,
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
        ],
        discovery_model_run_id=model_run_id,
        structuring_model_run_id=model_run_id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        report_sha256="d" * 64,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
    )
