"""FAST proof: real Selection and A production; Review diagnoses deferred B and
the publication freeze rejects it until its explicit unproduced exclusion is recorded.
PostgreSQL, workflow routes and the filesystem-backed blob catalog are real; Discovery
outputs and production provider responses are deterministic fixtures; no Typst or network.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.production import ProductionArtifactStage
from cti_app.infrastructure.database.models.edition_publication import (
    EditionReleaseRow,
    PublicationManifestRow,
)
from cti_app.infrastructure.database.models.jobs import JobRow
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.infrastructure.database.models.production import ProductionRunRow

from . import invariants
from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]
EXCLUSION_REASON = "Le sujet n'a pas de production prête pour cette édition."


async def _work_counts(
    uow_factory: UnitOfWorkFactory, edition_id: UUID, subject_b_id: UUID
) -> tuple[int, int, int, int]:
    async with uow_factory() as uow:
        session = uow._session
        runs = await session.scalar(
            select(func.count())
            .select_from(ProductionRunRow)
            .where(ProductionRunRow.edition_id == edition_id)
        )
        jobs = await session.scalar(select(func.count()).select_from(JobRow))
        model_runs = await session.scalar(select(func.count()).select_from(ModelRunRow))
        runs_for_b = await session.scalar(
            select(func.count())
            .select_from(ProductionRunRow)
            .where(ProductionRunRow.subject_id == subject_b_id)
        )
    return int(runs or 0), int(jobs or 0), int(model_runs or 0), int(runs_for_b or 0)


async def _prepare_deferred_composition(
    driver: WorkflowDriver, uow_factory: UnitOfWorkFactory
) -> tuple[UUID, dict[str, UUID], tuple[int, int, int, int], int]:
    edition_id = await driver.create_edition()
    await driver.run_discovery("D1", ("A", "B"))
    await driver.apply_fusion("D1")
    selected = await driver.select_subjects("D1", ("A", "B"))

    await driver.start_production("A", selected["A"])
    await driver.advance_jobs_until_terminal("A")

    async def inspect_before_composition() -> tuple[int, int, int, int]:
        counts = await _work_counts(uow_factory, edition_id, selected["B"])
        invariants.require(
            counts[3] == 0,
            driver.scenario,
            "seed_unproduced_b",
            "subject B unexpectedly has a production run",
        )
        return counts

    counts_before = await driver._perform(
        "seed_unproduced_b",
        inspect_before_composition,
        via="service",
        action_name="SQL production/job/model_run counts before composition",
        identities={"edition": edition_id, "subject_b": selected["B"]},
        assertions=("subject-b-has-no-production-run",),
    )

    async with uow_factory() as uow:
        run = next(
            item
            for item in await uow.production_runs.list_for_subject(selected["A"])
            if item.id == driver.production_run_ids["A"]
        )
        artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
        assert artifact is not None and artifact.canonical_blob_id is not None
        blob = await uow.blobs.get(artifact.canonical_blob_id)
        assert blob is not None

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
                "document_sha256": blob.descriptor.sha256,
            },
            {"subject_id": str(selected["B"]), "disposition": "deferred"},
        ],
    }

    async def compose_deferred() -> dict[str, Any]:
        response = await driver.client.put(
            f"/api/editions/{edition_id}/composition",
            headers={"Idempotency-Key": f"w3-defer-{uuid4()}"},
            json=payload,
        )
        invariants.require(
            response.status_code == 200,
            driver.scenario,
            "compose_deferred",
            f"composition route returned HTTP {response.status_code}: {response.text}",
        )
        return cast(dict[str, Any], response.json())

    async def verify_deferred(body: dict[str, Any]) -> tuple[str, ...]:
        async with uow_factory() as uow:
            composition = await uow.edition_compositions.get_for_edition(edition_id)
        invariants.require(
            composition is not None and body["version"] == composition.version,
            driver.scenario,
            "compose_deferred",
            "deferred composition was not persisted at the returned version",
        )
        assert composition is not None
        dispositions = {entry.subject_id: entry.disposition.value for entry in composition.entries}
        invariants.require(
            dispositions == {selected["A"]: "included", selected["B"]: "deferred"},
            driver.scenario,
            "compose_deferred",
            "PostgreSQL composition must pin A and keep B deferred",
        )
        deferred = await invariants.assert_deferred_subjects_not_excluded(
            uow_factory,
            edition_id,
            scenario=driver.scenario,
            step="compose_deferred",
        )
        return ("composition-read-from-postgresql", *deferred)

    body = await driver._perform(
        "compose_deferred",
        compose_deferred,
        via="http",
        action_name="PUT /composition with A included and B deferred",
        identities={
            "edition": edition_id,
            "subject_a": selected["A"],
            "subject_b": selected["B"],
        },
        after=verify_deferred,
    )
    return edition_id, selected, counts_before, int(body["version"])


@pytest.mark.asyncio
async def test_w3_unproduced_subject_is_explicitly_excluded(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    driver = workflow_driver
    driver.begin("W3-unproduced-subject-excluded")
    async with driver:
        edition_id = await driver.create_edition()
        await driver.run_discovery("D1", ("A", "B"))
        await driver.apply_fusion("D1")
        selected = await driver.select_subjects("D1", ("A", "B"))

        await driver.start_production("A", selected["A"])
        await driver.advance_jobs_until_terminal("A")

        async def inspect_before_composition() -> tuple[int, int, int, int]:
            counts = await _work_counts(uow_factory, edition_id, selected["B"])
            invariants.require(
                counts[3] == 0,
                driver.scenario,
                "seed_unproduced_b",
                "subject B unexpectedly has a production run",
            )
            return counts

        counts_before = await driver._perform(
            "seed_unproduced_b",
            inspect_before_composition,
            via="service",
            action_name="SQL production/job/model_run counts before composition",
            identities={"edition": edition_id, "subject_b": selected["B"]},
            assertions=("subject-b-has-no-production-run",),
        )

        async with uow_factory() as uow:
            run = next(
                item
                for item in await uow.production_runs.list_for_subject(selected["A"])
                if item.id == driver.production_run_ids["A"]
            )
            artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
            assert artifact is not None and artifact.canonical_blob_id is not None
            blob = await uow.blobs.get(artifact.canonical_blob_id)
            assert blob is not None

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
                    "document_sha256": blob.descriptor.sha256,
                },
                {"subject_id": str(selected["B"]), "disposition": "deferred"},
            ],
        }

        async def compose_deferred() -> dict[str, Any]:
            response = await driver.client.put(
                f"/api/editions/{edition_id}/composition",
                headers={"Idempotency-Key": f"w3-defer-{uuid4()}"},
                json=payload,
            )
            invariants.require(
                response.status_code == 200,
                driver.scenario,
                "compose_deferred",
                f"composition route returned HTTP {response.status_code}: {response.text}",
            )
            return cast(dict[str, Any], response.json())

        async def verify_deferred(body: dict[str, Any]) -> tuple[str, ...]:
            async with uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            invariants.require(
                composition is not None and body["version"] == composition.version,
                driver.scenario,
                "compose_deferred",
                "deferred composition was not persisted at the returned version",
            )
            assert composition is not None
            dispositions = {
                entry.subject_id: entry.disposition.value for entry in composition.entries
            }
            invariants.require(
                dispositions == {selected["A"]: "included", selected["B"]: "deferred"},
                driver.scenario,
                "compose_deferred",
                "PostgreSQL composition must pin A and keep B deferred",
            )
            deferred = await invariants.assert_deferred_subjects_not_excluded(
                uow_factory,
                edition_id,
                scenario=driver.scenario,
                step="compose_deferred",
            )
            return ("composition-read-from-postgresql", *deferred)

        body = await driver._perform(
            "compose_deferred",
            compose_deferred,
            via="http",
            action_name="PUT /composition with A included and B deferred",
            identities={
                "edition": edition_id,
                "subject_a": selected["A"],
                "subject_b": selected["B"],
            },
            after=verify_deferred,
        )
        composition_version = int(body["version"])

        await driver.review(expected_deferred_subject_id=selected["B"])

        async def reject_incomplete_accept() -> tuple[int, str, tuple[int, int, int, int]]:
            before = await _work_counts(uow_factory, edition_id, selected["B"])
            response = await driver.client.post(
                f"/api/editions/{edition_id}/publication/accept",
                json={"expected_composition_version": composition_version},
            )
            after = await _work_counts(uow_factory, edition_id, selected["B"])
            async with uow_factory() as uow:
                manifests = await uow._session.scalar(
                    select(func.count())
                    .select_from(PublicationManifestRow)
                    .where(PublicationManifestRow.edition_id == edition_id)
                )
                releases = await uow._session.scalar(
                    select(func.count())
                    .select_from(EditionReleaseRow)
                    .where(EditionReleaseRow.edition_id == edition_id)
                )
            detail = response.json().get("detail", {})
            invariants.require(
                response.status_code == 409 and detail.get("code") == "composition_incomplete",
                driver.scenario,
                "accept_deferred",
                f"expected HTTP 409 composition_incomplete, got {response.status_code} {detail!r}",
            )
            invariants.require(
                before == after == counts_before and not manifests and not releases,
                driver.scenario,
                "accept_deferred",
                "blocked acceptance changed work counts or created a manifest/release",
            )
            return response.status_code, str(detail["code"]), after

        await driver._perform(
            "accept_deferred",
            reject_incomplete_accept,
            via="http",
            action_name="POST /publication/accept rejected as incomplete",
            identities={"edition": edition_id, "subject_b": selected["B"]},
            assertions=(
                "http-409-composition_incomplete",
                "no-manifest-or-release",
                "work-counts-unchanged",
            ),
        )

        decision_id = await driver.exclude_subject(selected["B"], reason=EXCLUSION_REASON)
        await driver.review(
            required_exclusions={
                selected["B"]: ("workflow-analyst", EXCLUSION_REASON),
            },
            expect_can_accept=True,
        )
        await driver.validate()
        accepted = await driver.accept()

        async def verify_unproduced_exclusion() -> tuple[str, ...]:
            manifest = accepted.manifest
            async with uow_factory() as uow:
                persisted = await uow.publication_manifests.get(manifest.id)
                entries = await uow.publication_manifest_entries.list_for_manifest(manifest.id)
                exclusions = await uow.publication_manifest_exclusions.list_for_manifest(
                    manifest.id
                )
                decisions = await uow.publication_review_decisions.list_for_edition(edition_id)
            decision = next((item for item in decisions if item.id == decision_id), None)
            invariants.require(
                persisted is not None and persisted.schema_version == "2",
                driver.scenario,
                "accept_excluded_b",
                "version-2 manifest is absent from PostgreSQL",
            )
            invariants.require(
                {item.subject_id for item in entries} == {selected["A"]}
                and [(item.subject_id, item.review_decision_id) for item in exclusions]
                == [(selected["B"], decision_id)],
                driver.scenario,
                "accept_excluded_b",
                "manifest must contain A and the explicit exclusion of B only",
            )
            invariants.require(
                decision is not None
                and decision.production_run_id is None
                and decision.document_artifact_id is None
                and decision.actor_id == "workflow-analyst"
                and decision.reason == EXCLUSION_REASON,
                driver.scenario,
                "accept_excluded_b",
                "B exclusion must be an audited decision with no production identity",
            )
            return (
                "manifest-v2-read-from-postgresql",
                "only-a-is-published",
                "b-exclusion-has-null-production-run-id",
            )

        await driver._perform(
            "verify_unproduced_exclusion",
            verify_unproduced_exclusion,
            via="service",
            action_name="SQL read-back of manifest and B exclusion decision",
            identities={"edition": edition_id, "subject_b": selected["B"]},
            assertions=(
                "manifest-v2-contains-only-a",
                "b-exclusion-has-null-production-run-id",
                "exclusion-reason-and-actor-read-from-postgresql",
            ),
        )

        release = await driver.assemble()

        async def inspect_assembled_document() -> str:
            document = await driver.artifact_store.read_json(release.edition_document_blob_id)
            serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
            invariants.require(
                str(selected["B"]) not in serialized,
                driver.scenario,
                "assembled_document",
                "the assembled edition document contains excluded subject B",
            )
            return "excluded-b-absent-from-assembled-document-blob"

        await driver._perform(
            "assembled_document",
            inspect_assembled_document,
            via="service",
            action_name="read edition-document blob and inspect subject identities",
            identities={"edition": edition_id, "subject_b": selected["B"]},
            assertions=("excluded-b-absent-from-assembled-document-blob",),
        )

        async def verify_counts_unchanged() -> tuple[int, int, int, int]:
            final_counts = await _work_counts(uow_factory, edition_id, selected["B"])
            invariants.require(
                final_counts == counts_before and final_counts[3] == 0,
                driver.scenario,
                "after_release",
                "exclusion, acceptance, or assembly created work for unproduced B",
            )
            return final_counts

        await driver._perform(
            "after_release",
            verify_counts_unchanged,
            via="service",
            action_name="SQL production/job/model_run counts after release",
            identities={"edition": edition_id, "subject_b": selected["B"]},
            assertions=("production-runs-jobs-model-runs-unchanged", "b-still-has-no-run"),
        )


@pytest.mark.asyncio
async def test_w3_deferred_subject_review_does_not_offer_acceptance(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    """FAST contract check: Review must keep acceptance disabled for a deferred subject.

    Discovery output is fixture-persisted, Fusion and Selection are real, and A runs
    through real deterministic production jobs while B has no run. Review and its
    PostgreSQL projection are real; no external service or Typst is used.
    """
    driver = workflow_driver
    driver.begin("W3-deferred-subject-review-does-not-offer-acceptance")
    async with driver:
        (
            edition_id,
            selected,
            _counts_before,
            _composition_version,
        ) = await _prepare_deferred_composition(driver, uow_factory)
        review = await driver.review(expected_deferred_subject_id=selected["B"])
        observed_can_accept = cast(bool, review["can_accept"])

        async def assert_deferred_review_is_blocking() -> bool:
            invariants.require(
                observed_can_accept is False,
                driver.scenario,
                "review_can_accept_deferred",
                "Review must not offer acceptance while B is deferred; "
                f"observed can_accept={observed_can_accept!r}",
            )
            return observed_can_accept

        await driver._perform(
            "review_can_accept_deferred",
            assert_deferred_review_is_blocking,
            via="http",
            action_name="assert GET /review can_accept while B remains deferred",
            identities={
                "edition": edition_id,
                "subject_b": selected["B"],
                "observed_can_accept": str(observed_can_accept),
            },
            assertions=("review-can-accept-false-while-b-deferred",),
        )
