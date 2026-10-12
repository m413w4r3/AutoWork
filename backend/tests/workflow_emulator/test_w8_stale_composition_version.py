"""FAST proof: real Composition, Review, Accept and Assembly routes/services reject
an old composition version without writing a manifest, then freeze the current A/C set.
Edition, READY runs and VERIFIED artifacts are inserted by the PostgreSQL fixture helper;
all later state is canonical PostgreSQL; no production jobs, external services or Typst.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.edition_composition import CompositionDisposition, EditionCompositionEntry
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionEventRow,
    EditionCompositionRow,
)
from cti_app.infrastructure.database.models.edition_publication import (
    EditionReleaseRow,
    PublicationManifestRow,
)
from cti_app.infrastructure.database.models.publication_review import PublicationReviewDecisionRow
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs

from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


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


async def _edition_state_counts(
    uow_factory: UnitOfWorkFactory, edition_id: UUID
) -> tuple[int, int, int, int, int]:
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
    return (
        int(entries or 0),
        int(events or 0),
        int(decisions or 0),
        int(manifests or 0),
        int(releases or 0),
    )


@pytest.mark.asyncio
async def test_w8_accept_rejects_stale_composition_version(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W8-stale-composition-version")
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
        async with uow_factory() as uow:
            first_composition = await uow.edition_compositions.get_for_edition(edition.id)
        assert first_composition is not None
        stale_version = first_composition.version

        async def exclude_b_for_next_composition() -> UUID:
            review_response = await driver.client.get(f"/api/editions/{edition.id}/review")
            if review_response.status_code != 200:
                raise AssertionError(f"review route returned HTTP {review_response.status_code}")
            review_item = next(
                item
                for item in review_response.json()["items"]
                if item["subject_id"] == str(subjects["B"].id)
            )
            response = await driver.client.post(
                f"/api/editions/{edition.id}/review/items/{subjects['B'].id}/exclude",
                json={
                    "production_run_id": review_item["run_id"],
                    "pipeline_generation": review_item["pipeline_generation"],
                    "document_artifact_id": review_item["document_artifact_id"],
                    "document_artifact_version": review_item["document_artifact_version"],
                    "document_input_hash": review_item["document_input_hash"],
                    "reason": "B is explicitly excluded from the current composition.",
                },
            )
            if response.status_code != 200:
                raise AssertionError(
                    f"review exclusion route returned HTTP {response.status_code}: {response.text}"
                )
            return UUID(response.json()["id"])

        b_decision_id = await driver._perform(
            "review_exclude_b",
            exclude_b_for_next_composition,
            via="http",
            action_name="POST /review/items/B/exclude before one composition replacement",
            identities={"edition": edition.id, "subject_b": subjects["B"].id},
            assertions=("b-exclusion-decision-persisted-through-official-review-route",),
        )

        async with uow_factory() as uow:
            composition_before_replacement = await uow.edition_compositions.get_for_edition(
                edition.id
            )
        assert composition_before_replacement is not None
        changed_entries = tuple(
            replace(
                entry,
                disposition=CompositionDisposition.EXCLUDED,
                position=None,
                review_decision_id=b_decision_id,
            )
            if entry.subject_id == subjects["B"].id
            else replace(
                entry,
                disposition=CompositionDisposition.INCLUDED,
                position=1 if entry.subject_id == subjects["A"].id else 2,
                review_decision_id=None,
            )
            for entry in composition_before_replacement.entries
        )

        async def replace_with_a_c() -> dict[str, Any]:
            response = await driver.client.put(
                f"/api/editions/{edition.id}/composition",
                headers={"Idempotency-Key": f"w8-replace-{edition.id}-{stale_version}"},
                json={
                    "expected_version": stale_version,
                    "entries": [_entry_payload(entry) for entry in changed_entries],
                },
            )
            if response.status_code != 200:
                raise AssertionError(
                    f"composition route returned HTTP {response.status_code}: {response.text}"
                )
            return cast(dict[str, Any], response.json())

        async def verify_a_c_version(body: dict[str, Any]) -> tuple[str, ...]:
            async with uow_factory() as uow:
                persisted = await uow.edition_compositions.get_for_edition(edition.id)
            included = {
                entry.subject_id
                for entry in (persisted.entries if persisted is not None else ())
                if entry.disposition is CompositionDisposition.INCLUDED
            }
            excluded = {
                entry.subject_id
                for entry in (persisted.entries if persisted is not None else ())
                if entry.disposition is CompositionDisposition.EXCLUDED
            }
            if (
                persisted is None
                or body["version"] != stale_version + 1
                or included != {subjects["A"].id, subjects["C"].id}
                or excluded != {subjects["B"].id}
            ):
                raise AssertionError(
                    "v+1 composition is not A/C included with B explicitly excluded"
                )
            return ("composition-v-plus-one-read-from-postgresql", "included-set-is-a-c")

        await driver._perform(
            "composition_a_c_v_plus_one",
            replace_with_a_c,
            via="http",
            action_name="PUT /composition changing included set from A/B/C to A/C",
            identities={"edition": edition.id},
            after=verify_a_c_version,
        )
        async with uow_factory() as uow:
            current_composition = await uow.edition_compositions.get_for_edition(edition.id)
        assert current_composition is not None
        assert current_composition.version == stale_version + 1
        counts_before_rejection = await _edition_state_counts(uow_factory, edition.id)

        async def reject_old_version() -> tuple[int, str]:
            response = await driver.client.post(
                f"/api/editions/{edition.id}/publication/accept",
                json={"expected_composition_version": stale_version},
            )
            detail = response.json().get("detail", {})
            after = await _edition_state_counts(uow_factory, edition.id)
            async with uow_factory() as uow:
                persisted = await uow.edition_compositions.get_for_edition(edition.id)
            current_ids = (
                {entry.subject_id for entry in persisted.entries}
                if persisted is not None
                else set()
            )
            expected_ids = {subjects["A"].id, subjects["B"].id, subjects["C"].id}
            if response.status_code != 409 or detail.get("code") != "composition_version_conflict":
                raise AssertionError(
                    f"expected HTTP 409 composition_version_conflict, got "
                    f"{response.status_code} {detail!r}"
                )
            if after != counts_before_rejection or current_ids != expected_ids:
                raise AssertionError(
                    "stale accept changed manifest/release state or mixed the current "
                    "A/C composition"
                )
            return response.status_code, str(detail["code"])

        await driver._perform(
            "accept_stale_version",
            reject_old_version,
            via="http",
            action_name="POST /publication/accept with stale expected_composition_version",
            identities={"edition": edition.id, "expected_version": stale_version},
            assertions=(
                "http-409-composition_version_conflict",
                "no-manifest-or-release-created",
                "composition-remains-exactly-a-c",
            ),
        )

        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted = await driver.accept()

        async def verify_current_manifest() -> tuple[str, ...]:
            async with uow_factory() as uow:
                persisted = await uow.publication_manifests.get(accepted.manifest.id)
                entries = await uow.publication_manifest_entries.list_for_manifest(
                    accepted.manifest.id
                )
                exclusions = await uow.publication_manifest_exclusions.list_for_manifest(
                    accepted.manifest.id
                )
            if persisted is None or persisted.composition_version != current_composition.version:
                raise AssertionError("manifest does not identify the current composition version")
            if {entry.subject_id for entry in entries} != {
                subjects["A"].id,
                subjects["C"].id,
            } or [(item.subject_id, item.review_decision_id) for item in exclusions] != [
                (subjects["B"].id, b_decision_id)
            ]:
                raise AssertionError(
                    "manifest entries must contain A/C and its exclusion table must contain B"
                )
            return (
                "manifest-v2-pins-current-composition-version",
                "manifest-entry-set-contains-only-a-and-c",
                "b-is-recorded-as-an-explicit-exclusion",
            )

        await driver._perform(
            "verify_manifest_a_c",
            verify_current_manifest,
            via="service",
            action_name="SQL read-back of the accepted A/C manifest",
            identities={"edition": edition.id, "manifest": accepted.manifest.id},
            assertions=(
                "manifest-pins-current-composition-version",
                "manifest-entry-set-is-a-c",
                "b-has-explicit-exclusion",
            ),
        )
        release = await driver.assemble()
        assert release.manifest_id == accepted.manifest.id

        # The FAST fixture helper creates READY identities directly; it provides
        # no production retry runner or version-changing route in this ASGI app.
        # The stale exclusion-decision generation case is therefore left to the
        # full production workflow where a real retry/recovery action is reachable.
