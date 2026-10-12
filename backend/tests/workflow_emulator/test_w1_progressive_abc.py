"""Émulation workflow :

Discovery fixturée (sorties persistées via les repositories, ni route, ni parseur, ni service
Discovery) ; Fusion cumulative via le vrai CumulativeDiscoveryService.reconcile_batch avec planner
déterministe ; Selection via route officielle ; Production via vrais jobs ; Composition, Review,
Acceptation et Assembly réels. PostgreSQL réel ; compilateur PDF absent.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_review import PublicationDecision

from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


async def _assert_ready_identity(
    driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    label: str,
    subject_id: UUID,
    step: str,
) -> tuple[UUID, UUID, int, str, str]:
    run_id = driver.production_run_ids[label]
    async with uow_factory() as uow:
        run = await uow.production_runs.get(run_id)
        artifact = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.PUBLICATION.value
        )
        assert artifact is not None, f"W1 / {step}: subject {label}'s PUBLICATION artifact is gone"
        blob = await uow.blobs.get(artifact.canonical_blob_id)
    assert run is not None
    assert run.subject_id == subject_id
    assert run.edition_id == driver.edition_id
    assert run.status.value == "ready"
    assert artifact.status is ProductionArtifactStatus.VERIFIED
    assert artifact.production_run_id == run.id
    assert artifact.subject_id == subject_id
    assert artifact.version >= 1
    assert len(artifact.input_hash) == 64
    assert blob is not None
    assert len(blob.descriptor.sha256) == 64
    return run.id, artifact.id, artifact.version, artifact.input_hash, blob.descriptor.sha256


@pytest.mark.asyncio
async def test_w1_progressive_abc_uses_real_selection_and_production_transitions(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Discovery input is fixtured; Fusion, Selection, production and release are real."""
    driver = workflow_driver
    driver.begin("W1-progressive-ABC")
    async with driver:
        edition_id = await driver.create_edition()

        await driver.run_discovery("D1", ("A", "B"))
        snapshot_d1 = await driver.apply_fusion("D1")
        assert len(snapshot_d1.subjects) == 2
        selected_d1 = await driver.select_subjects("D1", ("A", "B"))
        await driver.start_production("A", selected_d1["A"])
        run_a = await driver.advance_jobs_until_terminal("A")
        identity_a = await _assert_ready_identity(
            driver, uow_factory, "A", selected_d1["A"], "after production of A"
        )
        assert run_a == identity_a[0]

        await driver.run_discovery("D2", ("C",))
        snapshot_d2 = await driver.apply_fusion("D2")
        assert len(snapshot_d2.subjects) == 3
        selected_c = await driver.select_subjects("D2", ("C",))
        await driver.start_production("C", selected_c["C"])
        run_c = await driver.advance_jobs_until_terminal("C")
        identity_c = await _assert_ready_identity(
            driver, uow_factory, "C", selected_c["C"], "after production of C"
        )
        assert run_c == identity_c[0]
        assert identity_a == await _assert_ready_identity(
            driver, uow_factory, "A", selected_d1["A"], "after production of C"
        )

        await driver.run_discovery("D3", ("D",))
        snapshot_d3 = await driver.apply_fusion("D3")
        assert len(snapshot_d3.subjects) == 4
        await driver.start_production("B", selected_d1["B"])
        run_b = await driver.advance_jobs_until_terminal("B")
        identity_b = await _assert_ready_identity(
            driver, uow_factory, "B", selected_d1["B"], "after production of B"
        )
        assert run_b == identity_b[0]
        assert identity_a == await _assert_ready_identity(
            driver, uow_factory, "A", selected_d1["A"], "after production of B"
        )
        assert identity_c == await _assert_ready_identity(
            driver, uow_factory, "C", selected_c["C"], "after production of B"
        )

        await driver.compose((selected_d1["A"], selected_d1["B"], selected_c["C"]))
        review = await driver.review()
        assert review["can_accept"] is True
        await driver.validate()
        accepted = await driver.accept()
        manifest = accepted.manifest
        expected = {
            selected_d1["A"]: identity_a,
            selected_d1["B"]: identity_b,
            selected_c["C"]: identity_c,
        }
        assert manifest.schema_version == "2"
        assert len(manifest.entries) == 3
        assert not manifest.exclusions
        for entry in manifest.entries:
            run_id, artifact_id, version, input_hash, document_hash = expected[entry.subject_id]
            assert entry.production_run_id == run_id
            assert entry.document_artifact_id == artifact_id
            assert entry.document_artifact_version == version
            assert entry.document_input_hash == input_hash
            assert entry.document_sha256 == document_hash
        decisions = await _decisions(uow_factory, edition_id)
        expected_decision_count = 0
        assert len(decisions) == expected_decision_count, (
            "W1 / accept: expected exactly 0 review decisions for the inclusion-only manifest"
        )
        assert all(decision.decision is not PublicationDecision.EXCLUDE for decision in decisions)

        release = await driver.assemble()
        assert release.edition_id == edition_id
        assert release.manifest_id == manifest.id


async def _decisions(uow_factory: UnitOfWorkFactory, edition_id: UUID):
    async with uow_factory() as uow:
        return await uow.publication_review_decisions.list_for_edition(edition_id)
