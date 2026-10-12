"""FULL proof: a cumulative Fusion proposal touching a selected identity is parked
for human review; a direct merge of two selected identities is refused. Explicit
Fusion review resolution keeps selected subjects, production history and Composition
unchanged. PostgreSQL and the deterministic production runner are real; no network or
Typst is used. Fusion review currently has a service surface, not an HTTP route.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery.cumulative.errors import DiscoveryMergeNeedsReview
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.fusion import (
    FusionReviewDecision,
    FusionSelectedSubjectConflictError,
    FusionService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    FusionReviewAction,
    MergeValidationStatus,
)
from cti_app.domain.production import ProductionArtifactStage

from . import invariants
from .driver import WorkflowDriver
from .providers import SelectedIdentityReviewPlanner

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


@pytest.mark.asyncio
async def test_w7_selected_discovery_fusion_requires_human_resolution(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Exercise real Fusion persistence and explicit human review at service level."""
    driver = workflow_driver
    driver.begin("W7-discovery-fusion-conflicts")
    async with driver:
        edition_id = await driver.create_edition()
        await driver.run_discovery("D1", ("A", "B"))
        await driver.apply_fusion("D1")
        selected = await driver.select_subjects("D1", ("A", "B"))
        await driver.start_production("A", selected["A"])
        run_a = await driver.advance_jobs_until_terminal("A")

        async with uow_factory() as uow:
            run = await uow.production_runs.get(run_a)
            artifact = await uow.production_artifacts.get_current(
                run_a, ProductionArtifactStage.PUBLICATION.value
            )
            origins = await uow.subject_discovery_origins.list_for_edition(edition_id)
            subjects_before = await uow.subjects.list_for_edition(edition_id)
            snapshot_before = await uow.discovery_snapshots.get_active(edition_id)
        invariants.require(
            run is not None
            and artifact is not None
            and artifact.canonical_blob_id is not None
            and snapshot_before is not None,
            driver.scenario,
            "seed_selected_subjects",
            "A's READY publication or the active Discovery snapshot is missing",
        )
        assert run is not None and artifact is not None and snapshot_before is not None
        identity_by_subject = {origin.subject_id: origin.discovery_subject_id for origin in origins}
        identity_a = identity_by_subject[selected["A"]]
        identity_b = identity_by_subject[selected["B"]]

        payload = {
            "expected_version": 0,
            "entries": [
                {
                    "subject_id": str(selected["A"]),
                    "disposition": "included",
                    "position": 1,
                    "production_run_id": str(run.id),
                    "pipeline_generation": run.pipeline_generation,
                    "artifact_id": str(artifact.id),
                    "artifact_version": artifact.version,
                    "artifact_input_hash": artifact.input_hash,
                    "provenance_batch_id": str(driver.production_batch_ids["A"]),
                },
                {"subject_id": str(selected["B"]), "disposition": "deferred"},
            ],
        }

        async def compose_selected_subjects() -> Any:
            response = await driver.client.put(
                f"/api/editions/{edition_id}/composition",
                headers={"Idempotency-Key": f"w7-composition-{uuid4()}"},
                json=payload,
            )
            invariants.require(
                response.status_code == 200,
                driver.scenario,
                "composition_before_fusion",
                f"official Composition route returned HTTP {response.status_code}: {response.text}",
            )
            return response.json()

        async def verify_composition(body: Any) -> tuple[str, ...]:
            async with uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            invariants.require(
                composition is not None
                and body["version"] == composition.version
                and len(composition.entries) == 2,
                driver.scenario,
                "composition_before_fusion",
                "A-included/B-deferred Composition was not persisted at its returned version",
            )
            assert composition is not None
            return ("composition-read-from-postgresql", "a-included-b-deferred")

        composition_body = await driver._perform(
            "composition_before_fusion",
            compose_selected_subjects,
            via="http",
            action_name="PUT /composition with selected A included and B deferred",
            identities={
                "edition": edition_id,
                "subject_a": selected["A"],
                "subject_b": selected["B"],
            },
            assertions=("composition-versioned-before-fusion",),
            after=verify_composition,
        )
        async with uow_factory() as uow:
            composition_before = await uow.edition_compositions.get_for_edition(edition_id)
        invariants.require(
            composition_before is not None
            and composition_before.version == composition_body["version"],
            driver.scenario,
            "composition_before_fusion",
            "canonical Composition version differs from the route response",
        )
        assert composition_before is not None

        await driver.run_discovery("D2", ("C",))
        batch_d2 = driver.discovery_batches["D2"]
        production_before_link = await invariants.production_state(uow_factory, edition_id)
        work_before_link = await invariants.work_counts(uow_factory)
        composition_before_link = composition_before

        async def park_cumulative_merge() -> UUID:
            service = CumulativeDiscoveryService(
                uow_factory,
                planner=SelectedIdentityReviewPlanner(identity_a),
            )
            try:
                await service.reconcile_batch(
                    batch_d2,
                    input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
                    actor_id="workflow-analyst",
                )
            except DiscoveryMergeNeedsReview as exc:
                return exc.run_id
            raise AssertionError(
                "W7 / fusion_pending: CumulativeDiscoveryService silently applied a proposal "
                "that linked incoming C to selected A"
            )

        async def verify_pending_merge(run_id: UUID) -> tuple[str, ...]:
            board = await FusionService(uow_factory).get_board(edition_id)
            async with uow_factory() as uow:
                merge_run = await uow.discovery_merge_runs.get(run_id)
                snapshot = await uow.discovery_snapshots.get_active(edition_id)
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            invariants.require(
                merge_run is not None
                and merge_run.validation_status is MergeValidationStatus.NEEDS_REVIEW
                and len(board.pending_reviews) == 1
                and board.pending_reviews[0].merge_run_id == run_id,
                driver.scenario,
                "fusion_pending",
                "Fusion must persist one actionable human-review proposal",
            )
            invariants.require(
                snapshot == snapshot_before
                and composition == composition_before_link
                and await invariants.production_state(uow_factory, edition_id)
                == production_before_link
                and await invariants.work_counts(uow_factory) == work_before_link,
                driver.scenario,
                "fusion_pending",
                "pending Fusion changed active identities, Composition, production, "
                "jobs, or models",
            )
            return (
                "merge-run-needs-review-read-from-postgresql",
                "active-snapshot-unchanged",
                "composition-production-and-work-counts-unchanged",
            )

        merge_run_id = await driver._perform(
            "fusion_pending",
            park_cumulative_merge,
            via="service",
            action_name="CumulativeDiscoveryService.reconcile_batch parked for human review",
            identities={"edition": edition_id, "batch": batch_d2.id, "selected_a": identity_a},
            assertions=("selected-identity-link-not-applied-silently", "human-decision-required"),
            after=verify_pending_merge,
        )

        async def refuse_selected_identity_merge() -> tuple[str, ...]:
            try:
                await FusionService(uow_factory).merge(
                    edition_id,
                    snapshot_version=snapshot_before.version,
                    discovery_subject_ids=(identity_a, identity_b),
                    actor_id="workflow-analyst",
                )
            except FusionSelectedSubjectConflictError as exc:
                invariants.require(
                    set(exc.subject_ids) == {selected["A"], selected["B"]},
                    driver.scenario,
                    "manual_merge_refused",
                    "Fusion refused a different selected-subject set than A and B",
                )
            else:
                raise AssertionError(
                    "W7 / manual_merge_refused: Fusion merged two already selected subjects"
                )
            async with uow_factory() as uow:
                snapshot = await uow.discovery_snapshots.get_active(edition_id)
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            invariants.require(
                snapshot == snapshot_before
                and composition == composition_before_link
                and await invariants.production_state(uow_factory, edition_id)
                == production_before_link,
                driver.scenario,
                "manual_merge_refused",
                "refused manual Fusion changed the snapshot, Composition, or production history",
            )
            return ("selected-subject-conflict-raised", "no-silent-document-or-subject-merge")

        await driver._perform(
            "manual_merge_refused",
            refuse_selected_identity_merge,
            via="service",
            action_name="FusionService.merge refused for selected A and B",
            identities={"edition": edition_id, "identity_a": identity_a, "identity_b": identity_b},
            assertions=("fusion-selected-subject-conflict", "state-unchanged"),
        )

        async with uow_factory() as uow:
            candidates = await uow.discovery_candidates.list_for_batch(batch_d2.id)
        invariants.require(
            len(candidates) == 1,
            driver.scenario,
            "fusion_review_resolution",
            f"expected one persisted incoming C candidate, found {len(candidates)}",
        )
        candidate_id = candidates[0].id

        async def resolve_as_separate() -> Any:
            return await FusionService(uow_factory).resolve_review(
                edition_id,
                merge_run_id,
                snapshot_version=snapshot_before.version,
                decisions=(
                    FusionReviewDecision(
                        FusionReviewAction.SEPARATE,
                        (candidate_id,),
                    ),
                ),
                actor_id="workflow-analyst",
            )

        async def verify_resolution(board: Any) -> tuple[str, ...]:
            async with uow_factory() as uow:
                settled = await uow.discovery_merge_runs.get(merge_run_id)
                snapshot = await uow.discovery_snapshots.get_active(edition_id)
                origins_after = await uow.subject_discovery_origins.list_for_edition(edition_id)
                subjects_after = await uow.subjects.list_for_edition(edition_id)
                composition = await uow.edition_compositions.get_for_edition(edition_id)
                canonical_a = await uow.discovery_subject_identities.resolve_canonical_subject(
                    identity_a
                )
                canonical_b = await uow.discovery_subject_identities.resolve_canonical_subject(
                    identity_b
                )
            invariants.require(
                settled is not None
                and settled.validation_status is MergeValidationStatus.RESOLVED
                and snapshot is not None
                and board.snapshot_id == snapshot.id
                and snapshot.version == snapshot_before.version + 1,
                driver.scenario,
                "fusion_review_resolution",
                "the explicit SEPARATE decision did not settle the merge into a new snapshot",
            )
            invariants.require(
                canonical_a == identity_a
                and canonical_b == identity_b
                and len(snapshot.subjects) == 3
                and len(subjects_after) == len(subjects_before)
                and {item.id for item in subjects_after} == {item.id for item in subjects_before}
                and {(item.subject_id, item.discovery_subject_id) for item in origins_after}
                == {(item.subject_id, item.discovery_subject_id) for item in origins},
                driver.scenario,
                "fusion_review_resolution",
                "selected subjects were merged, created, removed, or relinked by Fusion resolution",
            )
            invariants.require(
                composition == composition_before_link
                and await invariants.production_state(uow_factory, edition_id)
                == production_before_link
                and await invariants.work_counts(uow_factory) == work_before_link,
                driver.scenario,
                "fusion_review_resolution",
                "human Fusion resolution changed Composition, production artifacts, "
                "jobs, or models",
            )
            separate_subject = next(
                (
                    item
                    for item in snapshot.subjects
                    if any(ref.candidate_id == candidate_id for ref in item.member_references)
                ),
                None,
            )
            invariants.require(
                separate_subject is not None
                and separate_subject.subject_id not in {identity_a, identity_b}
                and all(
                    origin.discovery_subject_id != separate_subject.subject_id
                    for origin in origins_after
                ),
                driver.scenario,
                "fusion_review_resolution",
                "human SEPARATE resolution did not leave incoming C as an unselected identity",
            )
            return (
                "merge-review-resolved-in-postgresql",
                "selected-identities-and-origins-unchanged",
                "incoming-c-remains-separate-and-unselected",
                "production-composition-and-work-counts-unchanged",
            )

        await driver._perform(
            "fusion_review_resolution",
            resolve_as_separate,
            via="service",
            action_name="FusionService.resolve_review with explicit SEPARATE decision",
            identities={
                "edition": edition_id,
                "merge_run": merge_run_id,
                "candidate": candidate_id,
            },
            assertions=("human-separate-decision-recorded", "selected-subjects-preserved"),
            after=verify_resolution,
        )
