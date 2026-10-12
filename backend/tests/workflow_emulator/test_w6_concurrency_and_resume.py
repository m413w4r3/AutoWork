"""FAST proof of optimistic composition writes, idempotent acceptance/assembly,
post-blob interruption recovery and stale-artifact rejection against PostgreSQL.
Edition, READY runs and VERIFIED artifacts are fixture-inserted; official routes,
Review, Acceptance, Assembly and persistence are real; the blob store is local and
the interruption is injected by the test. No external service or Typst is used.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from cti_app.application.edition_publication import EditionAssemblyService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.edition_composition import EditionCompositionEntry
from cti_app.domain.production import ProductionArtifactStatus
from cti_app.infrastructure.database.models.core import BlobRow
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionEventRow,
    EditionCompositionRow,
)
from cti_app.infrastructure.database.models.edition_publication import (
    EditionReleaseRow,
    PublicationManifestRow,
)
from cti_app.infrastructure.database.models.jobs import JobRow
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.infrastructure.database.models.production import ProductionArtifactRow
from cti_app.infrastructure.database.models.publication_review import PublicationReviewDecisionRow
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs

from . import invariants
from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


async def _state_counts(
    uow_factory: UnitOfWorkFactory, edition_id: UUID
) -> tuple[int, int, int, int, int, int, int, int]:
    async with uow_factory() as uow:
        session = uow._session
        entries = await session.scalar(
            select(func.count())
            .select_from(EditionCompositionEntryRow)
            .join(
                EditionCompositionRow,
                EditionCompositionRow.id == EditionCompositionEntryRow.composition_id,
            )
            .where(EditionCompositionRow.edition_id == edition_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(EditionCompositionEventRow)
            .where(EditionCompositionEventRow.edition_id == edition_id)
        )
        decisions = await session.scalar(
            select(func.count())
            .select_from(PublicationReviewDecisionRow)
            .where(PublicationReviewDecisionRow.edition_id == edition_id)
        )
        manifests = await session.scalar(
            select(func.count())
            .select_from(PublicationManifestRow)
            .where(PublicationManifestRow.edition_id == edition_id)
        )
        releases = await session.scalar(
            select(func.count())
            .select_from(EditionReleaseRow)
            .where(EditionReleaseRow.edition_id == edition_id)
        )
        jobs = await session.scalar(select(func.count()).select_from(JobRow))
        model_runs = await session.scalar(select(func.count()).select_from(ModelRunRow))
        blobs = await session.scalar(select(func.count()).select_from(BlobRow))
    return (
        int(entries or 0),
        int(events or 0),
        int(decisions or 0),
        int(manifests or 0),
        int(releases or 0),
        int(jobs or 0),
        int(model_runs or 0),
        int(blobs or 0),
    )


def _entry_payload(entry: EditionCompositionEntry) -> dict[str, object]:
    return {
        "subject_id": str(entry.subject_id),
        "disposition": entry.disposition.value,
        "position": entry.position,
        "production_run_id": str(entry.production_run_id) if entry.production_run_id else None,
        "pipeline_generation": entry.pipeline_generation,
        "artifact_id": str(entry.artifact_id) if entry.artifact_id else None,
        "artifact_version": entry.artifact_version,
        "artifact_input_hash": entry.artifact_input_hash,
        "provenance_batch_id": (
            str(entry.provenance_batch_id) if entry.provenance_batch_id else None
        ),
        "review_decision_id": str(entry.review_decision_id) if entry.review_decision_id else None,
        "document_sha256": entry.document_sha256,
    }


@pytest.mark.asyncio
async def test_w6_concurrency_and_resume(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = workflow_driver
    driver.begin("W6-concurrency-and-resume")
    async with driver:

        async def seed_ready() -> Any:
            return await _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            )

        edition, artifact_store, subjects, _batches = await driver._perform(
            "seed_ready_runs",
            seed_ready,
            via="fixture",
            action_name="_compose_three_runs with empty initial composition",
            assertions=("fixture-inserts-ready-runs-and-verified-publications",),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)
        await driver.record_seed(
            {label: subjects[label].id for label in ("A", "B", "C")},
            mechanism="PostgreSQL fixture inserts READY runs and VERIFIED PUBLICATION artifacts",
        )

        await driver.compose((subjects["A"].id, subjects["B"].id, subjects["C"].id))
        await driver.review(expect_can_accept=True)
        await driver.validate()
        first_accept = await driver.accept()
        driver.accepted_manifest = first_accept.manifest
        first_release = await driver.assemble()
        assert first_release.manifest_id == first_accept.manifest.id

        historical_hashes = await invariants.manifest_hashes(uow_factory, edition.id)
        invariants.require(
            bool(historical_hashes),
            driver.scenario,
            "seed_historical_release",
            "W6 needs a persisted historical manifest before mutating composition",
        )

        async with uow_factory() as uow:
            composition_before_a = await uow.edition_compositions.get_for_edition(edition.id)
        assert composition_before_a is not None
        version_a = composition_before_a.version
        counts_before_a = await _state_counts(uow_factory, edition.id)

        # First operator advances A/B -> A/C with version v.
        # Reorder all addressed subjects so Review stays complete while the
        # first operator advances the version.
        await driver.compose((subjects["C"].id, subjects["B"].id, subjects["A"].id))

        async def verify_first_operator_write() -> tuple[tuple[int, ...], list[dict[str, object]]]:
            counts = await _state_counts(uow_factory, edition.id)
            invariants.require(
                counts[0] == counts_before_a[0]
                and counts[1] == counts_before_a[1] + 1
                and counts[2] == counts_before_a[2],
                driver.scenario,
                "W6.a.first_operator",
                "the successful composition write did not persist exactly one event "
                "without decisions",
            )
            async with uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition.id)
            assert composition is not None
            payload = [_entry_payload(entry) for entry in composition.entries]
            return counts, payload

        counts_after_first_operator, payload_from_first = await driver._perform(
            "W6.a.verify_first_operator_write",
            verify_first_operator_write,
            via="service",
            action_name="SQL verify first operator composition and event write",
            identities={"edition": edition.id, "expected_version": version_a},
            assertions=("one-composition-event-written", "no-review-decision-written"),
        )

        async def second_operator_stale_write() -> tuple[int, str]:
            response = await driver.client.put(
                f"/api/editions/{edition.id}/composition",
                headers={"Idempotency-Key": f"w6-second-operator-{uuid4()}"},
                json={"expected_version": version_a, "entries": payload_from_first},
            )
            detail = response.json().get("detail", {})
            after = await _state_counts(uow_factory, edition.id)
            async with uow_factory() as uow:
                current = await uow.edition_compositions.get_for_edition(edition.id)
            current_ids = {entry.subject_id for entry in current.entries} if current else set()
            if response.status_code != 409 or detail.get("code") != "composition_version_conflict":
                raise AssertionError(
                    f"expected HTTP 409 composition_version_conflict, got "
                    f"{response.status_code} {detail!r}"
                )
            current_positions = (
                {entry.subject_id: entry.position for entry in current.entries}
                if current is not None
                else {}
            )
            if (
                after != counts_after_first_operator
                or current_ids
                != {
                    subjects["A"].id,
                    subjects["B"].id,
                    subjects["C"].id,
                }
                or current_positions
                != {
                    subjects["C"].id: 1,
                    subjects["B"].id: 2,
                    subjects["A"].id: 3,
                }
            ):
                raise AssertionError("stale operator partially changed composition state")
            return response.status_code, str(detail["code"])

        await driver._perform(
            "W6.a.second_operator_conflict",
            second_operator_stale_write,
            via="http",
            action_name="second PUT /composition with the same stale expected_version",
            identities={"edition": edition.id, "expected_version": version_a},
            assertions=(
                "http-409-composition_version_conflict",
                "entries-events-decisions-unchanged-after-conflict",
                "composition-remains-a-c",
            ),
        )

        async def check_history_after_a() -> tuple[str, ...]:
            invariants.require(bool(historical_hashes), driver.scenario, "W6.a", "empty history")
            return await invariants.assert_manifest_hashes_unchanged(
                uow_factory,
                edition.id,
                historical_hashes,
                scenario=driver.scenario,
                step="W6.a.after_mutation_and_conflict",
            )

        await driver._perform(
            "W6.a.historical_hash",
            check_history_after_a,
            via="service",
            action_name="SQL compare frozen historical manifest content_sha256",
            identities={"edition": edition.id},
            assertions=("historical-manifest-content-sha256-unchanged", "checked=1"),
        )

        await driver.review(expect_can_accept=True)
        await driver.validate()
        async with uow_factory() as uow:
            current_for_b = await uow.edition_compositions.get_for_edition(edition.id)
        assert current_for_b is not None
        jobs_before_b = await _state_counts(uow_factory, edition.id)

        async def accept_once(label: str) -> tuple[str, int]:
            async def action() -> tuple[str, int]:
                response = await driver.client.post(
                    f"/api/editions/{edition.id}/publication/accept",
                    json={"expected_composition_version": current_for_b.version},
                )
                if response.status_code != 202:
                    raise AssertionError(
                        f"accept retry returned HTTP {response.status_code}: {response.text}"
                    )
                return str(response.json()["manifest_id"]), response.status_code

            return await driver._perform(
                f"W6.b.accept_{label}",
                action,
                via="http",
                action_name="POST /publication/accept retry",
                identities={"edition": edition.id, "operator": label},
                assertions=("http-202-accepted",),
            )

        results = await asyncio.gather(accept_once("one"), accept_once("two"))
        invariants.require(
            results[0] == results[1],
            driver.scenario,
            "W6.b.concurrent_accept",
            "same-version concurrent accept retries returned different manifests",
        )
        concurrent_manifest_id = UUID(results[0][0])

        async def verify_single_pending_manifest_and_job() -> tuple[str, ...]:
            invariants.require(
                results[0] == results[1],
                driver.scenario,
                "W6.b.concurrent_accept",
                "same-version concurrent accept retries returned different manifests",
            )
            async with uow_factory() as uow:
                manifest_ids = [
                    row.id
                    for row in await uow._session.scalars(
                        select(PublicationManifestRow).where(
                            PublicationManifestRow.edition_id == edition.id
                        )
                    )
                ]
                releases = await uow._session.scalar(
                    select(func.count())
                    .select_from(EditionReleaseRow)
                    .where(EditionReleaseRow.edition_id == edition.id)
                )
                job_inputs = [
                    dict(job.input_parameters) for job in await uow._session.scalars(select(JobRow))
                ]
                pending = await uow.publication_manifests.get(concurrent_manifest_id)
            manifest_jobs = [
                parameters
                for parameters in job_inputs
                if str(parameters.get("manifest_id")) == str(concurrent_manifest_id)
            ]
            invariants.require(
                len(manifest_ids) == 2
                and manifest_ids.count(concurrent_manifest_id) == 1
                and pending is not None,
                driver.scenario,
                "W6.b.concurrent_accept",
                "concurrent accept retries created more than one pending manifest",
            )
            invariants.require(
                releases == 1 and len(manifest_jobs) <= 1,
                driver.scenario,
                "W6.b.concurrent_accept",
                "concurrent accept created a duplicate release or assembly job",
            )
            invariants.require(
                (await _state_counts(uow_factory, edition.id))[5] <= jobs_before_b[5] + 1,
                driver.scenario,
                "W6.b.concurrent_accept",
                "accept retry created duplicate jobs",
            )
            return (
                "one-pending-manifest-for-two-accept-retries",
                "at-most-one-assembly-job-for-manifest",
                "no-release-created-before-assembly",
            )

        await driver._perform(
            "W6.b.verify_pending_snapshot",
            verify_single_pending_manifest_and_job,
            via="service",
            action_name="SQL verify concurrent acceptance persistence",
            identities={"edition": edition.id, "manifest": concurrent_manifest_id},
            assertions=(
                "two-accept-retries-return-same-manifest",
                "one-pending-manifest-for-two-accept-retries",
                "at-most-one-assembly-job-for-manifest",
                "no-release-created-before-assembly",
            ),
        )
        async with uow_factory() as uow:
            pending_manifest = await uow.publication_manifests.get(concurrent_manifest_id)
        assert pending_manifest is not None
        driver.accepted_manifest = pending_manifest
        release_b = await driver.assemble()
        assert release_b.manifest_id == concurrent_manifest_id
        historical_hashes = await invariants.manifest_hashes(uow_factory, edition.id)
        invariants.require(
            len(historical_hashes) == 2,
            driver.scenario,
            "W6.b.release",
            "expected two frozen manifests after the concurrent accept case",
        )

        # Use a new document order so the interrupted write creates a blob
        # that no earlier release already references.
        await driver.compose((subjects["B"].id, subjects["C"].id, subjects["A"].id))
        await driver.review(expect_can_accept=True)
        await driver.validate()
        third_accept = await driver.accept()
        driver.accepted_manifest = third_accept.manifest
        # Interrupt only after the canonical edition-document blob is durable.
        original_put = driver.artifact_store.put_canonical_json
        orphan_blob_id: UUID | None = None
        interrupted = False

        async def persist_then_interrupt(
            payload: dict[str, object], *, bucket: str
        ) -> tuple[UUID, str]:
            nonlocal orphan_blob_id, interrupted
            result = await original_put(payload, bucket=bucket)
            if bucket == "edition-documents" and not interrupted:
                orphan_blob_id = result[0]
                interrupted = True
                raise RuntimeError("simulated interruption after durable assembly blob")
            return result

        monkeypatch.setattr(driver.artifact_store, "put_canonical_json", persist_then_interrupt)
        blobs_before_interrupt = (await _state_counts(uow_factory, edition.id))[7]
        assembly = EditionAssemblyService(uow_factory, driver.artifact_store)

        async def interrupted_assembly() -> tuple[str, int]:
            with pytest.raises(
                RuntimeError,
                match="simulated interruption after durable assembly blob",
            ):
                await assembly.assemble(third_accept.manifest.id)
            async with uow_factory() as uow:
                release = await uow.edition_releases.get_by_manifest(third_accept.manifest.id)
                blob = await uow.blobs.get(orphan_blob_id) if orphan_blob_id else None
                referenced_release_count = (
                    await uow._session.scalar(
                        select(func.count())
                        .select_from(EditionReleaseRow)
                        .where(EditionReleaseRow.edition_document_blob_id == orphan_blob_id)
                    )
                    if orphan_blob_id is not None
                    else 0
                )
            if (
                release is not None
                or orphan_blob_id is None
                or blob is None
                or referenced_release_count
            ):
                raise AssertionError("interruption did not leave exactly an orphan assembly blob")
            return str(orphan_blob_id), (await _state_counts(uow_factory, edition.id))[7]

        orphan_blob_text, blobs_after_interrupt = await driver._perform(
            "W6.c.blob_write_interrupted",
            interrupted_assembly,
            via="service",
            action_name="EditionAssemblyService.assemble interrupted after put",
            identities={"edition": edition.id, "manifest": third_accept.manifest.id},
            assertions=("orphan-edition-document-blob-durable", "no-release-after-interruption"),
        )
        invariants.require(
            blobs_after_interrupt == blobs_before_interrupt + 1,
            driver.scenario,
            "W6.c.blob_write_interrupted",
            "expected one durable orphan blob after the injected interruption",
        )
        monkeypatch.setattr(driver.artifact_store, "put_canonical_json", original_put)

        release_c = await driver.assemble()
        replayed_release_c = await driver.assemble()

        async def verify_assembly_resume() -> tuple[tuple[int, ...], dict[UUID, str]]:
            invariants.require(
                release_c == replayed_release_c
                and str(release_c.edition_document_blob_id) == orphan_blob_text,
                driver.scenario,
                "W6.c.resume",
                "assembly retry did not reuse the durable blob and one frozen release",
            )
            state = await _state_counts(uow_factory, edition.id)
            invariants.require(
                state[4] == 3 and state[7] == blobs_after_interrupt,
                driver.scenario,
                "W6.c.resume",
                "assembly retry created a duplicate release or rewrote the durable blob",
            )
            frozen_hashes = await invariants.manifest_hashes(uow_factory, edition.id)
            invariants.require(
                len(frozen_hashes) == 3,
                driver.scenario,
                "W6.c.resume",
                "expected three frozen manifests before stale-artifact case",
            )
            return state, frozen_hashes

        _, historical_hashes = await driver._perform(
            "W6.c.verify_assembly_resume",
            verify_assembly_resume,
            via="service",
            action_name="SQL verify interrupted assembly retry and frozen hashes",
            identities={"edition": edition.id, "manifest": third_accept.manifest.id},
            assertions=(
                "retry-reuses-durable-blob",
                "exactly-one-release-for-frozen-manifest",
                "no-second-blob-written",
                "three-manifests-frozen",
            ),
        )

        async def mark_pinned_artifact_stale() -> UUID:
            async with uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition.id)
                assert composition is not None
                pinned_a = next(
                    entry for entry in composition.entries if entry.subject_id == subjects["A"].id
                )
                assert pinned_a.artifact_id is not None
                result = await uow._session.execute(
                    update(ProductionArtifactRow)
                    .where(ProductionArtifactRow.id == pinned_a.artifact_id)
                    .values(status=ProductionArtifactStatus.STALE.value)
                )
                await uow.commit()
                if result.rowcount != 1:
                    raise AssertionError("fixture did not stale exactly the pinned A artifact")
            return pinned_a.artifact_id

        stale_artifact_id = await driver._perform(
            "W6.d.fixture_stale_artifact",
            mark_pinned_artifact_stale,
            via="fixture",
            action_name="SQL fixture update production_artifacts.status=stale",
            identities={"edition": edition.id},
            assertions=("pinned-artifact-staled-by-test-fixture",),
        )
        async with uow_factory() as uow:
            current_composition = await uow.edition_compositions.get_for_edition(edition.id)
        assert current_composition is not None
        counts_before_stale_accept = await _state_counts(uow_factory, edition.id)

        async def reject_stale_artifact() -> tuple[int, str]:
            response = await driver.client.post(
                f"/api/editions/{edition.id}/publication/accept",
                json={"expected_composition_version": current_composition.version},
            )
            detail = response.json().get("detail", {})
            after = await _state_counts(uow_factory, edition.id)
            if (
                response.status_code != 409
                or detail.get("code") != "publication_artifact_changed_during_freeze"
            ):
                raise AssertionError(
                    "expected HTTP 409 publication_artifact_changed_during_freeze, got "
                    f"{response.status_code} {detail!r}"
                )
            if after[:5] != counts_before_stale_accept[:5]:
                raise AssertionError("stale artifact acceptance created a manifest or release")
            async with uow_factory() as uow:
                artifact = await uow.production_artifacts.get(stale_artifact_id)
            if artifact is None or artifact.status is not ProductionArtifactStatus.STALE:
                raise AssertionError("pinned A artifact is not stale in PostgreSQL")
            return response.status_code, str(detail["code"])

        await driver._perform(
            "W6.d.accept_rejected_stale_artifact",
            reject_stale_artifact,
            via="http",
            action_name="POST /publication/accept with stale pinned artifact",
            identities={"edition": edition.id, "artifact": stale_artifact_id},
            assertions=(
                "http-409-publication_artifact_changed_during_freeze",
                "no-new-manifest-or-release",
            ),
        )

        async def verify_all_frozen_hashes() -> tuple[str, ...]:
            invariants.require(
                len(historical_hashes) == 3,
                driver.scenario,
                "W6.d",
                "historical hash snapshot must contain all three frozen manifests",
            )
            return await invariants.assert_manifest_hashes_unchanged(
                uow_factory,
                edition.id,
                historical_hashes,
                scenario=driver.scenario,
                step="W6.d.after_stale_artifact_attempt",
            )

        await driver._perform(
            "W6.d.historical_hashes",
            verify_all_frozen_hashes,
            via="service",
            action_name="SQL compare all frozen historical manifest content_sha256 values",
            identities={"edition": edition.id},
            assertions=("historical-manifest-content-sha256-unchanged", "checked=3"),
        )
