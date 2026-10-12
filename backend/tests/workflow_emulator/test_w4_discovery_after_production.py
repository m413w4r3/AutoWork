"""FAST proof: A is produced through real deterministic jobs and pinned before D2.
The fixture candidate is reconciled by cumulative Fusion, then merged into A's selected
Discovery identity through the real Fusion service. PostgreSQL, Selection, Production,
Fusion, Composition, Review and Acceptance are real; no external service or Typst.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from cti_app.application.discovery.fusion import FusionService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.production import ProductionArtifactStage
from cti_app.infrastructure.database.models.jobs import JobRow
from cti_app.infrastructure.database.models.model_execution import ModelRunRow

from . import invariants
from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


async def _ready_identity(
    uow_factory: UnitOfWorkFactory, run_id: UUID
) -> tuple[UUID, str, int, UUID, int, str, str]:
    async with uow_factory() as uow:
        run = await uow.production_runs.get(run_id)
        artifact = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.PUBLICATION.value
        )
        assert run is not None and artifact is not None and artifact.canonical_blob_id is not None
        blob = await uow.blobs.get(artifact.canonical_blob_id)
    assert blob is not None
    return (
        run.id,
        run.status.value,
        run.pipeline_generation,
        artifact.id,
        artifact.version,
        artifact.input_hash,
        blob.descriptor.sha256,
    )


@pytest.mark.asyncio
async def test_w4_discovery_after_production_keeps_pinned_inputs_publishable(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    driver = workflow_driver
    driver.begin("W4-discovery-after-production")
    async with driver:
        edition_id = await driver.create_edition()
        await driver.run_discovery("D1", ("A",))
        await driver.apply_fusion("D1")
        selected = await driver.select_subjects("D1", ("A",))
        await driver.start_production("A", selected["A"])
        run_id = await driver.advance_jobs_until_terminal("A")
        ready_identity_before = await _ready_identity(uow_factory, run_id)

        await driver.compose((selected["A"],))
        async with uow_factory() as uow:
            pinned_composition = await uow.edition_compositions.get_for_edition(edition_id)
            input_snapshot_before = await uow.production_input_snapshots.get_by_run(run_id)
            active_snapshot_before = await uow.discovery_snapshots.get_active(edition_id)
        assert pinned_composition is not None and input_snapshot_before is not None
        assert active_snapshot_before is not None
        production_before = await invariants.production_state(uow_factory, edition_id)
        jobs_before = await invariants.work_counts(uow_factory)
        async with uow_factory() as uow:
            model_run_ids_before = set(await uow._session.scalars(select(ModelRunRow.id)))

        await driver.run_discovery("D2", ("D2",))
        batch_d2 = driver.discovery_batches["D2"]

        snapshot_after_d2 = await driver.apply_fusion("D2")
        candidate_d1 = driver.discovery_batches["D1"].candidates[0].id
        candidate_d2 = batch_d2.candidates[0].id

        async def link_new_candidate_to_selected_identity() -> dict[str, Any]:
            async with uow_factory() as uow:
                origins = await uow.subject_discovery_origins.list_for_edition(edition_id)
                origin = next(item for item in origins if item.subject_id == selected["A"])
                active_before_merge = await uow.discovery_snapshots.get_active(edition_id)
                assert active_before_merge is not None
                identity_a = await uow.discovery_subject_identities.resolve_canonical_subject(
                    origin.discovery_subject_id
                )
                identity_d2 = next(
                    subject.subject_id
                    for subject in active_before_merge.subjects
                    if any(
                        reference.candidate_id == candidate_d2
                        for reference in subject.member_references
                    )
                )
            invariants.require(
                active_before_merge.id == snapshot_after_d2.id
                and active_before_merge.version == snapshot_after_d2.version,
                driver.scenario,
                "D2.identity_link",
                "D2 cumulative Fusion did not persist its active snapshot",
            )
            invariants.require(
                identity_d2 != identity_a,
                driver.scenario,
                "D2.identity_link",
                "the distinct D2 candidate was already merged before the explicit decision",
            )

            merged = await FusionService(uow_factory).merge(
                edition_id,
                snapshot_version=active_before_merge.version,
                discovery_subject_ids=(identity_a, identity_d2),
                actor_id="workflow-analyst",
            )

            async with uow_factory() as uow:
                active_after_merge = await uow.discovery_snapshots.get_active(edition_id)
                canonical_d2 = await uow.discovery_subject_identities.resolve_canonical_subject(
                    identity_d2
                )
            assert active_after_merge is not None
            current_a = next(
                subject
                for subject in active_after_merge.subjects
                if subject.subject_id == identity_a
            )
            current_members = {reference.candidate_id for reference in current_a.member_references}
            pinned_member_ids = set(input_snapshot_before.member_candidate_ids)
            invariants.require(
                merged.group_count == 1
                and merged.snapshot_version == active_before_merge.version + 1
                and active_after_merge.version == merged.snapshot_version,
                driver.scenario,
                "D2.identity_link",
                "the real Fusion identity merge did not activate the next snapshot version",
            )
            # `_discovery_update_available` compares the pinned Discovery version
            # and member candidate IDs with the active canonical identity.
            invariants.require(
                canonical_d2 == identity_a
                and {candidate_d1, candidate_d2}.issubset(current_members)
                and pinned_member_ids.issubset(current_members),
                driver.scenario,
                "D2.identity_link",
                "D2 candidate was not linked to A's selected canonical Discovery identity",
            )
            invariants.require(
                active_after_merge.version > input_snapshot_before.discovery_snapshot_version
                and current_members != pinned_member_ids,
                driver.scenario,
                "D2.identity_link",
                "the active snapshot must be newer and have different members "
                "from A's pinned inputs",
            )
            return {
                "active_snapshot_id": str(active_after_merge.id),
                "active_snapshot_version": active_after_merge.version,
                "canonical_discovery_subject_id": str(identity_a),
                "d2_discovery_subject_id": str(identity_d2),
                "member_candidate_ids": sorted(str(value) for value in current_members),
            }

        await driver._perform(
            "D2.identity_link",
            link_new_candidate_to_selected_identity,
            via="service",
            action_name="FusionService.merge selected A identity with distinct D2 candidate",
            identities={"edition": edition_id, "subject_a": selected["A"], "batch": batch_d2.id},
            assertions=(
                "d2-started-as-a-distinct-discovery-identity",
                "real-fusion-advanced-active-snapshot",
                "d2-candidate-resolves-to-a-canonical-discovery-identity",
                "a-discovery-membership-changed",
            ),
        )

        async def verify_discovery_did_not_rebuild() -> tuple[str, ...]:
            async with uow_factory() as uow:
                composition_after = await uow.edition_compositions.get_for_edition(edition_id)
                input_snapshot_after = await uow.production_input_snapshots.get_by_run(run_id)
                model_run_ids_after = set(await uow._session.scalars(select(ModelRunRow.id)))
                jobs_after_count = await uow._session.scalar(
                    select(func.count()).select_from(JobRow)
                )
            invariants.require(
                await _ready_identity(uow_factory, run_id) == ready_identity_before,
                driver.scenario,
                "D2.identity_link",
                "A's READY run or PUBLICATION artifact identity changed after Discovery",
            )
            invariants.require(
                input_snapshot_after is not None
                and input_snapshot_after == input_snapshot_before
                and input_snapshot_after.input_hash == input_snapshot_before.input_hash,
                driver.scenario,
                "D2.identity_link",
                "A's historical production input snapshot changed after Discovery",
            )
            invariants.require(
                composition_after == pinned_composition,
                driver.scenario,
                "D2.identity_link",
                "Discovery changed the pinned composition version or entries",
            )
            invariants.require(
                await invariants.production_state(uow_factory, edition_id) == production_before,
                driver.scenario,
                "D2.identity_link",
                "Discovery created or changed a production run or artifact",
            )
            invariants.require(
                model_run_ids_after - model_run_ids_before == {batch_d2.discovery_model_run_id}
                and model_run_ids_before.issubset(model_run_ids_after),
                driver.scenario,
                "D2.identity_link",
                "D2 may add its fixture Discovery ModelRun only; no production ModelRun is allowed",
            )
            invariants.require(
                jobs_after_count == jobs_before[0],
                driver.scenario,
                "D2.identity_link",
                "Discovery created a production job",
            )
            return (
                "ready-run-artifact-version-and-hashes-unchanged",
                "historical-production-input-snapshot-hash-unchanged",
                "pinned-composition-version-and-entries-unchanged",
                "only-fixture-discovery-model-run-added",
                "production-job-count-unchanged",
            )

        await driver._perform(
            "D2.no_rebuild",
            verify_discovery_did_not_rebuild,
            via="service",
            action_name="SQL read-back after D2 cumulative Fusion and identity merge",
            identities={"edition": edition_id, "run": run_id},
            assertions=(
                "ready-run-artifact-version-hashes-unchanged",
                "historical-input-snapshot-hash-unchanged",
                "composition-version-and-entries-unchanged",
                "no-production-run-or-model-run-created",
            ),
        )

        async def read_composition_axes() -> dict[str, Any]:
            response = await driver.client.get(f"/api/editions/{edition_id}/composition")
            invariants.require(
                response.status_code == 200,
                driver.scenario,
                "D2.composition_read",
                f"composition route returned HTTP {response.status_code}",
            )
            return cast(dict[str, Any], response.json())

        async def verify_update_available(body: dict[str, Any]) -> tuple[str, ...]:
            invariants.require(
                body["axes"]["discovery_update_available"] is True,
                driver.scenario,
                "D2.composition_read",
                "the active Discovery snapshot has changed the pinned A candidate membership",
            )
            invariants.require(
                body["composition"]["version"] == pinned_composition.version
                and len(body["composition"]["entries"]) == 1
                and body["composition"]["entries"][0]["subject_id"] == str(selected["A"]),
                driver.scenario,
                "D2.composition_read",
                "composition read changed the pinned A composition",
            )
            return (
                "discovery_update_available=true",
                "d2-member-linked-to-a-selected-canonical-discovery-identity",
                "composition-pin-still-a",
            )

        await driver._perform(
            "D2.composition_read",
            read_composition_axes,
            via="http",
            action_name="GET /composition after D2/F2",
            identities={"edition": edition_id, "subject_a": selected["A"]},
            after=verify_update_available,
        )

        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted = await driver.accept()

        async def verify_still_publishable() -> tuple[str, ...]:
            async with uow_factory() as uow:
                manifest = await uow.publication_manifests.get(accepted.manifest.id)
                entries = await uow.publication_manifest_entries.list_for_manifest(
                    accepted.manifest.id
                )
            identity_after_accept = await _ready_identity(uow_factory, run_id)
            invariants.require(
                manifest is not None and {entry.subject_id for entry in entries} == {selected["A"]},
                driver.scenario,
                "accept_after_d2",
                "A's publication manifest is missing from PostgreSQL",
            )
            invariants.require(
                identity_after_accept == ready_identity_before
                and identity_after_accept[1] == "ready",
                driver.scenario,
                "accept_after_d2",
                "A did not remain READY with the same artifact through acceptance",
            )
            return ("manifest-a-read-from-postgresql", "a-remains-ready-with-same-artifact")

        await driver._perform(
            "accept_after_d2",
            verify_still_publishable,
            via="service",
            action_name="SQL read-back of accepted A and production artifact",
            identities={"edition": edition_id, "run": run_id},
            assertions=(
                "manifest-a-read-from-postgresql",
                "a-remains-ready-with-same-artifact",
            ),
        )
