import hashlib
from collections.abc import Callable
from datetime import date
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery.cumulative.errors import DiscoveryMergeNeedsReview
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.fusion import (
    FusionReviewDecision,
    FusionSelectedSubjectConflictError,
    FusionService,
    FusionSnapshotStaleError,
)
from cti_app.application.discovery.manual_source_edits import ManualSourceEditService
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
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.domain.selection import (
    SelectionAction,
    SelectionDecision,
    SubjectDiscoveryOrigin,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.discovery_support import (
    make_discovery_run_for_edition,
    persist_batch_with_candidates,
)

pytestmark = pytest.mark.integration


async def _add_selected_origin(uow_factory, edition, snapshot, discovery_subject_id):
    subject = Subject(
        edition_id=edition.id,
        title="Selected subject",
        slug=f"selected-{uuid4().hex}",
        tlp=edition.tlp,
    )
    decision = SelectionDecision(
        edition_id=edition.id,
        discovery_subject_id=discovery_subject_id,
        snapshot_id=snapshot.id,
        snapshot_version=snapshot.version,
        action=SelectionAction.SELECT,
        subject_id=subject.id,
        actor_id="analyst",
        correlation_id=str(uuid4()),
        idempotency_key=str(uuid4()),
    )
    origin = SubjectDiscoveryOrigin(
        subject_id=subject.id,
        edition_id=edition.id,
        discovery_subject_id=discovery_subject_id,
        selection_decision_id=decision.id,
        selected_snapshot_id=snapshot.id,
        selected_snapshot_version=snapshot.version,
    )
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.selection_decisions.append(decision)
        await uow.subject_discovery_origins.add(origin)
        await uow.commit()
    return subject, origin


class ApplyPlanner:
    kind = DiscoveryPlannerKind.HEURISTIC
    policy_version = "fusion-integration-apply-v1"

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
        return PlannedDiscoveryMerge(
            DiscoveryMergePlanV1(
                groups=[
                    DiscoveryMergeGroup(
                        existing_subject_handles=[],
                        incoming_candidate_handles=[handle],
                        confidence=MergeConfidence.HIGH,
                        disposition=MergeDisposition.APPLY,
                        rationale="integration test",
                    )
                    for handle in sorted(handles.incoming)
                ]
            )
        )


class ReviewPlanner:
    kind = DiscoveryPlannerKind.CHATGPT
    policy_version = "fusion-integration-review-v1"

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
        return PlannedDiscoveryMerge(
            DiscoveryMergePlanV1(
                groups=[
                    DiscoveryMergeGroup(
                        existing_subject_handles=[],
                        incoming_candidate_handles=[handle],
                        confidence=MergeConfidence.MEDIUM,
                        disposition=MergeDisposition.REVIEW,
                        rationale="ambiguous integration fixture",
                    )
                    for handle in sorted(handles.incoming)
                ]
            )
        )


class LocalArchive:
    """Records the manual-import model run the correction batch references."""

    def __init__(self, uow_factory: Callable[[], SqlAlchemyUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def create_manual_research_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        evidence_pack_hash: str,
        actor_id: str,
        operation: str = "manual_import",
    ) -> ModelRun:
        del content, actor_id
        run = ModelRun(
            id=run_id,
            provider=ModelProvider.FAKE,
            model_role=ModelRole.RESEARCH,
            requested_model="manual-import",
            prompt_template_id="manual-import",
            prompt_template_version="1.0",
            authorized_input_hash=hashlib.sha256(b"").hexdigest(),
            evidence_pack_hash=evidence_pack_hash,
            parameters={"operation": operation},
        )
        async with self._uow_factory() as uow:
            await uow.model_runs.add(run)
            await uow.commit()
        return run


async def test_fusion_board_uses_canonical_candidate_ids_and_resolves_review(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Fusion Iran", "FI")
    model_run = _model_run("a")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, local_ref="C1")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Ambiguous topic",
            url="https://vendor.example/ambiguous",
            local_ref="C2",
            request_hash="b" * 64,
        )
        await _persist_batches(uow_factory, first, second)

        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, initial_snapshot = await cumulative.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        async with uow_factory() as uow:
            persisted = await uow.discovery_candidates.list_for_batch(first.id)
        assert len(persisted) == 1

        intake, _ = await cumulative.ingest_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        review_cumulative = CumulativeDiscoveryService(uow_factory, planner=ReviewPlanner())
        with pytest.raises(DiscoveryMergeNeedsReview) as parked:
            await review_cumulative.reconcile_intake(
                intake.id,
                expected_parent_snapshot_id=initial_snapshot.id,
                actor_id="analyst",
            )

        fusion = FusionService(uow_factory)
        board = await fusion.get_board(edition.id)
        assert board.snapshot_version == initial_snapshot.version
        assert board.groups[0].candidate_ids == (persisted[0].id,)
        assert board.pending_reviews[0].candidate_ids == (
            (await _candidate_for_batch(uow_factory, second.id)).id,
        )
        review_candidate_id = board.pending_reviews[0].candidate_ids[0]
        resolved = await fusion.resolve_review(
            edition.id,
            parked.value.run_id,
            snapshot_version=board.snapshot_version or 0,
            decisions=[
                FusionReviewDecision(
                    action=FusionReviewAction.SEPARATE,
                    candidate_ids=(review_candidate_id,),
                )
            ],
            actor_id="analyst",
        )

        assert resolved.snapshot_version == (board.snapshot_version or 0) + 1
        resolved_candidate_ids = {
            candidate_id for group in resolved.groups for candidate_id in group.candidate_ids
        }
        assert resolved_candidate_ids == {
            persisted[0].id,
            review_candidate_id,
        }
        async with uow_factory() as uow:
            run = await uow.discovery_merge_runs.get(parked.value.run_id)
            snapshot = await uow.discovery_snapshots.get_active(edition.id)
        assert run is not None and run.validation_status is MergeValidationStatus.RESOLVED
        assert snapshot is not None and snapshot.id == resolved.snapshot_id
    finally:
        await engine.dispose()


async def test_fusion_stale_version_is_non_mutating(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Stale Fusion Iran", "FS")
    model_run = _model_run("c")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        batch = _batch(edition.id, model_run.id, discovery_run.id)
        await _persist_batches(uow_factory, batch)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, snapshot = await cumulative.reconcile_batch(
            batch, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        fusion = FusionService(uow_factory)
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second subject",
            url="https://vendor.example/second",
            local_ref="C2",
            request_hash="d" * 64,
        )
        await _persist_batches(uow_factory, second)
        _, next_snapshot = await cumulative.reconcile_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        valid_board = await fusion.get_board(edition.id)
        runs_before_stale = await _merge_runs(uow_factory, edition.id)
        with pytest.raises(FusionSnapshotStaleError, match="stale"):
            await fusion.merge(
                edition.id,
                snapshot_version=snapshot.version,
                discovery_subject_ids=tuple(
                    subject.subject_id for subject in next_snapshot.subjects[:2]
                ),
                actor_id="analyst",
            )
        assert await _merge_runs(uow_factory, edition.id) == runs_before_stale
        assert (await fusion.get_board(edition.id)).snapshot_id == valid_board.snapshot_id
    finally:
        await engine.dispose()


async def test_selected_subject_conflict_leaves_active_snapshot_unchanged(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Selected Conflict Fusion Iran", "FK")
    model_run = _model_run("conflict")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, local_ref="C1")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second selected subject",
            url="https://vendor.example/selected-2",
            local_ref="C2",
            request_hash="c" * 64,
        )
        await _persist_batches(uow_factory, first, second)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        await cumulative.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        _, snapshot = await cumulative.reconcile_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        subject_ids = tuple(subject.subject_id for subject in snapshot.subjects)
        await _add_selected_origin(uow_factory, edition, snapshot, subject_ids[0])
        await _add_selected_origin(uow_factory, edition, snapshot, subject_ids[1])
        before = await _active_snapshot(uow_factory, edition.id)
        runs_before = await _merge_runs(uow_factory, edition.id)

        with pytest.raises(FusionSelectedSubjectConflictError):
            await FusionService(uow_factory).merge(
                edition.id,
                snapshot_version=snapshot.version,
                discovery_subject_ids=subject_ids,
                actor_id="analyst",
            )

        after = await _active_snapshot(uow_factory, edition.id)
        assert before is not None and after is not None
        assert (after.id, after.version) == (before.id, before.version)
        assert await _merge_runs(uow_factory, edition.id) == runs_before
    finally:
        await engine.dispose()


async def test_selected_and_undecided_merge_resolves_to_existing_subject(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Selected Existing Fusion Iran", "FE")
    model_run = _model_run("existing")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, local_ref="C1")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Undecided subject",
            url="https://vendor.example/undecided",
            local_ref="C2",
            request_hash="e" * 64,
        )
        await _persist_batches(uow_factory, first, second)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, first_snapshot = await cumulative.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        _, snapshot = await cumulative.reconcile_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        first_identity = first_snapshot.subjects[0].subject_id
        second_identity = next(
            subject.subject_id
            for subject in snapshot.subjects
            if subject.subject_id != first_identity
        )
        selected_subject, origin = await _add_selected_origin(
            uow_factory, edition, snapshot, first_identity
        )

        merged = await FusionService(uow_factory).merge(
            edition.id,
            snapshot_version=snapshot.version,
            discovery_subject_ids=(first_identity, second_identity),
            actor_id="analyst",
        )

        async with uow_factory() as uow:
            assert (
                await uow.discovery_subject_identities.resolve_canonical_subject(second_identity)
            ) == first_identity
            origins = await uow.subject_discovery_origins.list_for_edition(edition.id)
        assert merged.group_count == 1
        assert origins == [origin]
        assert origins[0].subject_id == selected_subject.id
    finally:
        await engine.dispose()


async def test_manual_replacement_keeps_history_and_only_new_candidate_active(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Correction Fusion Iran", "FC")
    model_run = _model_run("e")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        batch = _batch(edition.id, model_run.id, discovery_run.id)
        await _persist_batches(uow_factory, batch)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, snapshot = await cumulative.reconcile_batch(
            batch, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        original = await _candidate_for_batch(uow_factory, batch.id)
        assert snapshot.subjects[0].member_references[0].candidate_id == original.id
        old_url = original.evidence.sources[0].canonical_url
        replacement_url = "https://vendor.example/corrected"
        manual = ManualSourceEditService(uow_factory, LocalArchive(uow_factory), cumulative)
        # La correction s'adresse à l'identité canonique de la candidate, pas au
        # sujet fusionné qui la contient.
        await manual.attach_replacement_source_url(
            edition.id,
            original.id,
            old_url,
            replacement_url,
            actor_id="analyst",
        )

        board = await FusionService(uow_factory).get_board(edition.id)
        active_ids = [
            candidate_id for group in board.groups for candidate_id in group.candidate_ids
        ]
        async with uow_factory() as uow:
            historical = await uow.discovery_candidates.get(original.id)
            all_candidates = await uow.discovery_candidates.list_for_edition(
                edition.id, include_replaced=True
            )
        replacement = next(candidate for candidate in all_candidates if candidate.id != original.id)
        assert historical is not None
        assert replacement.supersedes_candidate_id == original.id
        assert active_ids == [replacement.id]
        assert len(set(active_ids)) == 1
        assert historical.id == original.id
    finally:
        await engine.dispose()


async def test_manual_merge_and_split_are_versioned_and_non_destructive(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Structure Fusion Iran", "FM")
    model_run = _model_run("f")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id, local_ref="C1")
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Second structure",
            url="https://vendor.example/structure-2",
            local_ref="C2",
            request_hash="1" * 64,
        )
        await _persist_batches(uow_factory, first, second)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, first_snapshot = await cumulative.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        _, second_snapshot = await cumulative.reconcile_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        first_candidate = await _candidate_for_batch(uow_factory, first.id)
        second_candidate = await _candidate_for_batch(uow_factory, second.id)
        candidate_ids = [first_candidate.id, second_candidate.id]
        first_identity = first_snapshot.subjects[0].subject_id
        selected_subject, origin = await _add_selected_origin(
            uow_factory, edition, second_snapshot, first_identity
        )
        fusion = FusionService(uow_factory)
        merged = await fusion.merge(
            edition.id,
            snapshot_version=second_snapshot.version,
            discovery_subject_ids=tuple(subject.subject_id for subject in second_snapshot.subjects),
            actor_id="analyst",
        )
        assert merged.snapshot_version == second_snapshot.version + 1
        assert merged.group_count == 1
        # The analyst's decision supersedes the plan that first assembled the
        # group: the board must not keep crediting the planner for it.
        assert merged.groups[0].origin == "human"
        assert merged.groups[0].confidence is None
        assert merged.groups[0].model_suggestion is None
        assert [
            entry.actor_id for entry in merged.groups[0].history if entry.action == "merged"
        ] == ["analyst"]
        merged_snapshot = await _active_snapshot(uow_factory, edition.id)
        assert merged_snapshot is not None

        split = await fusion.split(
            edition.id,
            snapshot_version=merged.snapshot_version or 0,
            discovery_subject_id=merged.groups[0].discovery_subject_id,
            candidate_ids=(candidate_ids[1],),
            actor_id="analyst",
        )
        assert split.snapshot_version == merged.snapshot_version + 1
        assert split.group_count == 2
        assert {
            candidate_id for group in split.groups for candidate_id in group.candidate_ids
        } == set(candidate_ids)
        # A split has no plan and no merge event, so its run payload is the only
        # place the deciding analyst can be recovered from.
        assert {
            entry.actor_id
            for group in split.groups
            for entry in group.history
            if entry.action == "split"
        } == {"analyst"}
        async with uow_factory() as uow:
            historical_merge = await uow.discovery_snapshots.get(merged_snapshot.id)
            runs = await uow.discovery_merge_runs.list_for_edition(edition.id)
            origins = await uow.subject_discovery_origins.list_for_edition(edition.id)
            identities = await uow.discovery_subject_identities.list_for_edition(edition.id)
        assert historical_merge is not None
        assert len(runs) >= 2
        assert first_snapshot.id != split.snapshot_id
        split_identity = next(
            identity for identity in identities if identity.origin_key.startswith("split:")
        )
        assert origins == [origin]
        assert origins[0].subject_id == selected_subject.id
        assert origins[0].discovery_subject_id == first_identity
        assert origins[0].discovery_subject_id != split_identity.id
    finally:
        await engine.dispose()


async def test_archived_fusion_board_is_readable_but_all_mutations_are_rejected(
    migrated_postgres_url: str,
) -> None:
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Archived Fusion Iran", "FA")
    model_run = _model_run("h")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
        first = _batch(edition.id, model_run.id, discovery_run.id)
        second = _batch(
            edition.id,
            model_run.id,
            discovery_run.id,
            title="Archived review",
            url="https://vendor.example/archived-review",
            local_ref="C2",
            request_hash="2" * 64,
        )
        await _persist_batches(uow_factory, first, second)
        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        _, snapshot = await cumulative.reconcile_batch(
            first, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        intake, _ = await cumulative.ingest_batch(
            second, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        with pytest.raises(DiscoveryMergeNeedsReview) as parked:
            await CumulativeDiscoveryService(uow_factory, planner=ReviewPlanner()).reconcile_intake(
                intake.id, expected_parent_snapshot_id=snapshot.id, actor_id="analyst"
            )
        edition.state = EditionStatus.ARCHIVED
        async with uow_factory() as uow:
            assert await uow.editions.update(edition, expected_version=edition.version)
            await uow.commit()

        fusion = FusionService(uow_factory)
        board = await fusion.get_board(edition.id)
        assert board.pending_review_count == 1
        with pytest.raises(ValueError, match="Archived"):
            await fusion.merge(
                edition.id,
                snapshot_version=board.snapshot_version or 0,
                discovery_subject_ids=(snapshot.subjects[0].subject_id,) * 2,
                actor_id="analyst",
            )
        with pytest.raises(ValueError, match="Archived"):
            await fusion.split(
                edition.id,
                snapshot_version=board.snapshot_version or 0,
                discovery_subject_id=snapshot.subjects[0].subject_id,
                candidate_ids=(board.groups[0].candidate_ids[0],),
                actor_id="analyst",
            )
        with pytest.raises(ValueError, match="Archived"):
            await fusion.resolve_review(
                edition.id,
                parked.value.run_id,
                snapshot_version=board.snapshot_version or 0,
                decisions=[
                    FusionReviewDecision(
                        action=FusionReviewAction.SEPARATE,
                        candidate_ids=board.pending_reviews[0].candidate_ids,
                    )
                ],
                actor_id="analyst",
            )
    finally:
        await engine.dispose()


async def test_same_local_ref_in_two_runs_keeps_distinct_canonical_identities(
    migrated_postgres_url: str,
) -> None:
    """AW-007: un local_ref identique dans deux DiscoveryRuns ne confond jamais
    deux candidates. L'identite fonctionnelle reste DiscoveryCandidate.id."""
    engine, uow_factory = _database(migrated_postgres_url)
    edition = _edition("Cross Run Iran", "FX")
    model_run = _model_run("g")
    try:
        await _persist_edition_and_run(uow_factory, edition, model_run)
        run_a = await make_discovery_run_for_edition(uow_factory, edition)
        run_b = await make_discovery_run_for_edition(
            uow_factory,
            edition,
            complementary_axis="second-wave",
        )
        assert run_a.id != run_b.id

        # Meme local_ref "S1" et meme titre, mais deux runs et deux batches distincts.
        batch_a = _batch(
            edition.id,
            model_run.id,
            run_a.id,
            title="Shared wave topic",
            url="https://vendor.example/wave-one",
            local_ref="S1",
            request_hash="1" * 64,
        )
        batch_b = _batch(
            edition.id,
            model_run.id,
            run_b.id,
            title="Shared wave topic",
            url="https://vendor.example/wave-two",
            local_ref="S1",
            request_hash="2" * 64,
        )
        assert batch_a.id != batch_b.id
        await _persist_batches(uow_factory, batch_a, batch_b)

        cumulative = CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner())
        await cumulative.reconcile_batch(
            batch_a, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )
        await cumulative.reconcile_batch(
            batch_b, input_mode=DiscoveryInputMode.BRIDGE_RESEARCH, actor_id="analyst"
        )

        candidate_a = await _candidate_for_batch(uow_factory, batch_a.id)
        candidate_b = await _candidate_for_batch(uow_factory, batch_b.id)

        # Provenance distincte, identite canonique distincte, local_ref partage.
        assert candidate_a.id != candidate_b.id
        assert candidate_a.discovery_run_id == run_a.id
        assert candidate_b.discovery_run_id == run_b.id
        assert candidate_a.discovery_batch_id == batch_a.id
        assert candidate_b.discovery_batch_id == batch_b.id
        assert candidate_a.local_ref == candidate_b.local_ref == "S1"
        assert {c.id for c in await _candidates_for_edition(uow_factory, edition.id)} == {
            candidate_a.id,
            candidate_b.id,
        }

        # Fusion indexe sur les UUID persistants, jamais sur une cle derivee de "S1".
        snapshot = await _active_snapshot(uow_factory, edition.id)
        assert snapshot is not None
        member_ids = [
            reference.candidate_id
            for subject in snapshot.subjects
            for reference in subject.member_references
        ]
        assert sorted(member_ids) == sorted([candidate_a.id, candidate_b.id])
        assert len(member_ids) == len(set(member_ids)) == 2

        board = await FusionService(uow_factory).get_board(edition.id)
        board_ids = [candidate_id for group in board.groups for candidate_id in group.candidate_ids]
        assert sorted(board_ids) == sorted([candidate_a.id, candidate_b.id])
        assert board.candidate_count == 2
        # Chaque membre du board reste rattache a son run d'origine: aucune
        # identite derivee de "S1" ne peut collapser les deux candidates, meme
        # si la Fusion venait a les regrouper sous un meme sujet.
        board_provenance = {
            candidate.id: candidate.discovery_run_id
            for group in board.groups
            for candidate in group.candidates
        }
        assert board_provenance == {candidate_a.id: run_a.id, candidate_b.id: run_b.id}
    finally:
        await engine.dispose()


def _database(migrated_postgres_url: str):
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return engine, uow_factory


async def _persist_edition_and_run(uow_factory, edition: Edition, model_run: ModelRun) -> None:
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.model_runs.add(model_run)
        await uow.commit()


async def _persist_batches(uow_factory, *batches: DiscoveryBatch) -> None:
    async with uow_factory() as uow:
        for batch in batches:
            await persist_batch_with_candidates(uow, batch)
        await uow.commit()


async def _candidate_for_batch(uow_factory, batch_id: UUID):
    async with uow_factory() as uow:
        candidates = await uow.discovery_candidates.list_for_batch(batch_id)
    assert len(candidates) == 1
    return candidates[0]


async def _candidates_for_edition(uow_factory, edition_id: UUID):
    async with uow_factory() as uow:
        return await uow.discovery_candidates.list_for_edition(edition_id, include_replaced=False)


async def _active_snapshot(uow_factory, edition_id: UUID):
    async with uow_factory() as uow:
        return await uow.discovery_snapshots.get_active(edition_id)


async def _merge_runs(uow_factory, edition_id: UUID):
    async with uow_factory() as uow:
        return await uow.discovery_merge_runs.list_for_edition(edition_id)


def _edition(country: str, code: str) -> Edition:
    return Edition(
        country=country,
        country_code=code,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
    )


def _model_run(suffix: str) -> ModelRun:
    return ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="fusion-integration",
        prompt_template_version="1",
        authorized_input_hash=hashlib.sha256(f"fusion-authorized:{suffix}".encode()).hexdigest(),
        evidence_pack_hash=hashlib.sha256(f"fusion-evidence:{suffix}".encode()).hexdigest(),
        parameters={},
    )


def _batch(
    edition_id: UUID,
    model_run_id: UUID,
    discovery_run_id: UUID,
    *,
    title: str = "Stable fusion topic",
    url: str = "https://vendor.example/fusion",
    local_ref: str = "C1",
    request_hash: str = "d" * 64,
) -> DiscoveryBatch:
    candidate = CandidateTopic(
        title=title,
        summary="Integration candidate summary",
        novelty="New evidence",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("Technical source",),
        actors=("Actor",),
        campaigns=("Campaign",),
        malware=("Malware",),
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
        parser_version="fusion-integration-v1",
        report_sha256=(request_hash[0] * 64),
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
    )
