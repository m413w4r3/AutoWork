"""Émulation workflow — production fixturée (runs READY insérés), services/API réels,
PostgreSQL réel, compilateur PDF absent."""

from __future__ import annotations

from pathlib import Path

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.publication_review import PublicationDecision
from tests.integration.test_edition_multibatch_publication_postgres import (
    _compose_three_runs,
)

from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]
EXCLUSION_REASON = "Hors périmètre du bulletin de la période."


@pytest.mark.asyncio
async def test_w2_inclusion_exclusion_is_persisted_through_release(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    """W2 proves the real review and publication exclusion audit on PostgreSQL."""
    driver = workflow_driver
    driver.begin("W2-inclusion-exclusion")
    async with driver:
        edition, artifact_store, subjects, batches = await _compose_three_runs(
            uow_factory,
            tmp_path,
            production_order=("A", "B", "C"),
            composition_order=(),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)
        await driver.record_seed(
            {label: subjects[label].id for label in ("A", "B", "C")},
            mechanism="inserted READY runs + VERIFIED artifacts",
        )

        initial = await driver.compose(
            (subjects["A"].id, subjects["B"].id, subjects["C"].id),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        assert initial["version"] == 2
        async with uow_factory() as uow:
            current = await uow.edition_compositions.get_for_edition(edition.id)
        assert current is not None
        assert {entry.subject_id: entry.disposition for entry in current.entries} == {
            subjects["A"].id: CompositionDisposition.INCLUDED,
            subjects["B"].id: CompositionDisposition.INCLUDED,
            subjects["C"].id: CompositionDisposition.INCLUDED,
        }

        decision_id = await driver.exclude_subject(subjects["B"].id, reason=EXCLUSION_REASON)
        await driver.review(
            required_exclusions={subjects["B"].id: ("workflow-analyst", EXCLUSION_REASON)}
        )
        validation = await driver.validate()
        assert validation.reasons == ()

        accepted = await driver.accept()
        manifest = accepted.manifest

        async with uow_factory() as uow:
            persisted_manifest = await uow.publication_manifests.get(manifest.id)
            persisted_entries = await uow.publication_manifest_entries.list_for_manifest(
                manifest.id
            )
            persisted_exclusions = await uow.publication_manifest_exclusions.list_for_manifest(
                manifest.id
            )
            decisions = await uow.publication_review_decisions.list_for_edition(edition.id)
        assert persisted_manifest is not None, "W2 / accept: manifest row is absent from PostgreSQL"
        assert persisted_manifest.schema_version == "2"
        assert {entry.subject_id for entry in persisted_entries} == {
            subjects["A"].id,
            subjects["C"].id,
        }, "W2 / accept: PostgreSQL manifest entries must contain A and C"
        assert [(item.subject_id, item.review_decision_id) for item in persisted_exclusions] == [
            (subjects["B"].id, decision_id)
        ], "W2 / accept: PostgreSQL manifest exclusion row for B is missing"
        assert persisted_manifest.content_sha256 == manifest.content_sha256
        decision = next(item for item in decisions if item.id == decision_id)
        assert decision.decision is PublicationDecision.EXCLUDE
        assert decision.actor_id == "workflow-analyst"
        assert decision.reason == EXCLUSION_REASON

        assert manifest.schema_version == "2"
        assert {entry.subject_id for entry in manifest.entries} == {
            subjects["A"].id,
            subjects["C"].id,
        }
        assert len(manifest.exclusions) == 1
        exclusion = manifest.exclusions[0]
        assert exclusion.subject_id == subjects["B"].id
        assert exclusion.review_decision_id == decision_id

        release = await driver.assemble()
        assert release.edition_id == edition.id
        assert release.manifest_id == manifest.id
