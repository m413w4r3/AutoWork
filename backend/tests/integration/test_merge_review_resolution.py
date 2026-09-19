"""PostgreSQL coverage for UUID-based Fusion review resolution."""

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.errors import (
    DiscoveryMergeNeedsReview,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.fusion import (
    FusionReviewDecision,
    FusionService,
    FusionSnapshotStaleError,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
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
    FusionReviewAction,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.discovery_support import (
    make_discovery_run_for_edition,
    persist_batch_with_candidates,
)

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


async def test_resolving_a_merge_retires_it_and_rejects_a_stale_replay(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition = _edition("Resolve Iran", "RA")
    model_run = _model_run()
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.commit()
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, url="https://vendor.example/one")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second topic",
            url="https://vendor.example/two",
            request_hash="e" * 64,
            local_ref="S2",
        )
        async with uow_factory() as uow:
            await uow.model_runs.add(model_run)
            await persist_batch_with_candidates(uow, first)
            await persist_batch_with_candidates(uow, second)
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

        candidate_id = await _candidate_id_for_batch(uow_factory, second.id)
        fusion = FusionService(uow_factory)
        applied = await fusion.resolve_review(
            edition.id,
            run_id,
            snapshot_version=bootstrap.version,
            decisions=(FusionReviewDecision(FusionReviewAction.ACCEPT, (candidate_id,)),),
            actor_id="analyst",
        )
        assert applied.snapshot_version == bootstrap.version + 1
        assert applied.group_count == 2

        async with uow_factory() as uow:
            settled = await uow.discovery_merge_runs.get(run_id)
            active = await uow.discovery_snapshots.get_active(edition.id)
        assert settled is not None
        # Without this the review panel keeps offering a decision already taken.
        assert settled.validation_status is MergeValidationStatus.RESOLVED
        assert active is not None and active.id == applied.snapshot_id

        # A repeated request was decided against the old board and must not
        # silently create another snapshot.
        with pytest.raises(FusionSnapshotStaleError):
            await fusion.resolve_review(
                edition.id,
                run_id,
                snapshot_version=bootstrap.version,
                decisions=(FusionReviewDecision(FusionReviewAction.ACCEPT, (candidate_id,)),),
                actor_id="analyst",
            )
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
    replanned: list[ReconcileDiscoveryParameters] = []

    async def replan(parameters: ReconcileDiscoveryParameters) -> object:
        replanned.append(parameters)
        return None

    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.commit()
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, url="https://vendor.example/one")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second topic",
            url="https://vendor.example/two",
            request_hash="e" * 64,
            local_ref="S2",
        )
        third = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Third topic",
            url="https://vendor.example/three",
            request_hash="f" * 64,
            local_ref="S3",
        )
        async with uow_factory() as uow:
            await uow.model_runs.add(model_run)
            for batch in (first, second, third):
                await persist_batch_with_candidates(uow, batch)
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
        fusion = FusionService(uow_factory, replan_intake=replan)
        third_candidate_id = await _candidate_id_for_batch(uow_factory, third.id)
        moved_on = await fusion.resolve_review(
            edition.id,
            other.value.run_id,
            snapshot_version=bootstrap.version,
            decisions=(FusionReviewDecision(FusionReviewAction.ACCEPT, (third_candidate_id,)),),
            actor_id="analyst",
        )

        second_candidate_id = await _candidate_id_for_batch(uow_factory, second.id)
        with pytest.raises(FusionSnapshotStaleError):
            await fusion.resolve_review(
                edition.id,
                stale_run_id,
                snapshot_version=moved_on.snapshot_version or 0,
                decisions=(
                    FusionReviewDecision(FusionReviewAction.ACCEPT, (second_candidate_id,)),
                ),
                actor_id="analyst",
            )

        # The contribution is not dropped: it is queued for a fresh plan against
        # the snapshot that won, and the dead run stops blocking the panel.
        assert len(replanned) == 1
        assert replanned[0].intake_id == parked_intake.id
        assert replanned[0].expected_parent_snapshot_id == moved_on.snapshot_id
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
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.commit()
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, url="https://vendor.example/one")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second topic",
            url="https://vendor.example/two",
            request_hash="e" * 64,
            local_ref="S2",
        )
        async with uow_factory() as uow:
            await uow.model_runs.add(model_run)
            await persist_batch_with_candidates(uow, first)
            await persist_batch_with_candidates(uow, second)
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

        # UUID sets must match a pending review group exactly.
        fusion = FusionService(uow_factory)
        with pytest.raises(ValueError, match="match exactly"):
            await fusion.resolve_review(
                edition.id,
                parked.value.run_id,
                snapshot_version=bootstrap.version,
                decisions=(FusionReviewDecision(FusionReviewAction.ACCEPT, (edition.id,)),),
                actor_id="analyst",
            )
    finally:
        await engine.dispose()


async def _candidate_id_for_batch(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    batch_id: UUID,
) -> UUID:
    async with uow_factory() as uow:
        candidates = await uow.discovery_candidates.list_for_batch(batch_id)
    assert len(candidates) == 1
    return candidates[0].id


def _edition(country: str, code: str) -> Edition:
    return Edition(
        country=country,
        country_code=code,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
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
    discovery_run_id: UUID,
    *,
    title: str = "Stable title",
    url: str = "https://vendor.example/report",
    request_hash: str = "c" * 64,
    local_ref: str = "S1",
) -> DiscoveryBatch:
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
        candidates=[candidate],
        discovery_run_id=discovery_run_id,
        discovery_model_run_id=model_run_id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test-parser-v1",
        report_sha256="d" * 64,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
    )
