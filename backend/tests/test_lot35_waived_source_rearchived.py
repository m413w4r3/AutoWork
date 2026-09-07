"""LOT 35 — waive, then archive: the corpus fact wins over the older decision.

The analyst first decided to publish without a supplemental source, then
finally obtained the publication and archived it.  From that moment the
edition owes a REFERENCES reconciliation: sign-off must be refused, the desk
must offer the source reintegration, and the waiver must stay untouched in the
append-only audit.

Everything below is read from a brand-new client and a brand-new set of
services, so no in-memory frontend state can be credited for the answer.  The
payload the desk really receives is also frozen as the fixture the React tests
render, so the two sides can never drift.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.collection import SubjectCollectionService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_to_json,
)
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRepairImpactKind,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    SupplementalSourceRepairState,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_repair_desk_source_durability import (
    RAW_Q1,
    S2_HTML,
    SOURCE_ONE,
    SOURCE_TWO,
    _application,
    _BlobCatalog,
    _client,
    _collector,
    _Dispatcher,
    _ExplodingModelGateway,
    _Jobs,
    _selected_subject,
    _stage_artifact,
    _World,
)

#: Golden payload shared with ``RepairDeskWaivedArchivedSource.test.tsx``.
#: Regenerate with ``LOT35_UPDATE_FIXTURE=1`` after a deliberate API change.
FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "test-utils"
    / "fixtures"
    / "lot35WaivedArchivedSource.json"
)


class _AppendOnlyDecisions:
    """The real append-only semantics: a revision never rewrites its past."""

    def __init__(self) -> None:
        self.history: list[Any] = []

    async def append(self, decision: Any) -> None:
        self.history.append(decision)

    async def list_for_edition(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[Any, ...]:
        return tuple(
            decision
            for decision in self.history
            if decision.edition_id == edition_id
            and (subject_id is None or decision.subject_id == subject_id)
        )

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[Any, ...]:
        latest: dict[tuple[UUID, str], Any] = {}
        for decision in sorted(
            await self.list_for_edition(edition_id, subject_id),
            key=lambda item: (item.created_at, item.id),
        ):
            latest[(decision.subject_id, decision.repair_key)] = decision
        return tuple(latest.values())

    async def list_for_key(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> tuple[Any, ...]:
        return tuple(
            decision
            for decision in await self.list_for_edition(edition_id, subject_id)
            if decision.repair_key == repair_key
        )


def _normalized(payload: Any, replacements: dict[str, str]) -> Any:
    """Replace this run's identities with the stable names the fixture uses."""
    if isinstance(payload, dict):
        return {
            key: (
                "2026-09-04T10:00:00+00:00"
                if key == "created_at"
                else _normalized(value, replacements)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_normalized(value, replacements) for value in payload]
    if isinstance(payload, str):
        return replacements.get(payload, payload)
    return payload


def _assert_fixture(name: str, payload: Any) -> None:
    document = json.loads(FIXTURE_PATH.read_text()) if FIXTURE_PATH.exists() else {}
    if os.environ.get("LOT35_UPDATE_FIXTURE"):
        document[name] = payload
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        return
    assert document.get(name) == payload, (
        f"{FIXTURE_PATH} is out of date for '{name}'. "
        "Rerun with LOT35_UPDATE_FIXTURE=1 if the API change is deliberate."
    )


@pytest.mark.asyncio
async def test_lot35_waived_source_archived_later_blocks_signoff_until_rebuild(
    tmp_path: Path,
) -> None:
    collection_factory = InMemoryCollectionUnitOfWorkFactory()
    subject, edition = _selected_subject(collection_factory, (SOURCE_ONE, SOURCE_TWO))
    for step in (
        EditionStatus.DISCOVERY,
        EditionStatus.SELECTION,
        EditionStatus.PRODUCTION,
        EditionStatus.REVIEW,
    ):
        edition.transition(step)
    collection_service = SubjectCollectionService(
        collection_factory,
        _collector(),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    sources = await collection_service.initialize(subject.id)
    first = next(item for item in sources if item.canonical_url == SOURCE_ONE)
    second = next(item for item in sources if item.canonical_url == SOURCE_TWO)
    collection_factory.collections[first.id].state = CollectionState.ARCHIVED
    collection_factory.collections[first.id].origin_kind = SourceOriginKind.DISCOVERY
    collection_factory.collections[second.id].state = CollectionState.FAILED_TERMINAL

    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 15),
    )
    world = _World(
        edition=edition,
        subject_id=subject.id,
        run=run,
        collections=collection_factory.collections,
        documents=collection_factory.documents,
    )
    world.decisions = _AppendOnlyDecisions()  # type: ignore[assignment]
    world.store_catalog = _BlobCatalog()  # type: ignore[attr-defined]
    world.jobs = _Jobs()  # type: ignore[attr-defined]
    world.dispatcher = _Dispatcher()  # type: ignore[attr-defined]
    world.gateway = _ExplodingModelGateway()  # type: ignore[attr-defined]
    store = ProductionArtifactStore(world.store_catalog)  # type: ignore[arg-type]

    parsed = parse_reference_report(RAW_Q1, date(2026, 8, 15))
    assert parsed.value is not None
    canonical_v1 = reconcile_reference_report_with_archives(parsed.value, {SOURCE_ONE}).report
    raw_id, canonical_id, _ = await store.store_stage_payloads(
        raw=RAW_Q1, canonical=reference_report_to_json(canonical_v1)
    )
    references_v1 = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        raw_blob_id=raw_id,
        canonical_blob_id=canonical_id,
        metadata={
            "repair_source_index": {
                "proposed": [
                    {"source_id": "S1", "source_url": SOURCE_ONE, "source_title": "First"},
                    {"source_id": "S2", "source_url": SOURCE_TWO, "source_title": "Second"},
                ],
                "canonical": [{"source_id": "S1", "source_url": SOURCE_ONE}],
            }
        },
    )
    await world.artifacts.append(references_v1)
    for stage, digest in (
        (ProductionArtifactStage.EXTRACTION, "b"),
        (ProductionArtifactStage.SYNTHESIS, "c"),
        (ProductionArtifactStage.PUBLICATION, "d"),
    ):
        await world.artifacts.append(_stage_artifact(run, stage, 1, input_hash=digest * 64))

    # --- 1. The unarchived source is an open arbitration. -------------------
    async with _client(_application(world, collection_service)) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs")
    item = listed.json()["items"][0]
    repair_key = item["repair_key"]
    assert item["repair_state"] == SupplementalSourceRepairState.UNARCHIVED
    assert item["resolved"] is False

    # --- 2. D1: the analyst publishes without the source. -------------------
    async with _client(_application(world, collection_service)) as client:
        decided = await client.post(
            f"/api/editions/{edition.id}/review/repairs/{repair_key}/decision",
            json={
                "action": "continue_without_source",
                "observed_subject_id": str(subject.id),
                "observed_run_id": str(run.id),
                "observed_artifact_id": str(references_v1.id),
                "observed_pipeline_generation": run.pipeline_generation,
                "reason": "source définitivement indisponible",
            },
        )
    assert decided.status_code == 200, decided.text
    decision_id = decided.json()["decision_id"]

    async with _client(_application(world, collection_service)) as client:
        waived = await client.get(f"/api/editions/{edition.id}/review/repairs?status=all")
        review = await client.get(f"/api/editions/{edition.id}/review")
    waived_item = waived.json()["items"][0]
    assert waived_item["repair_state"] == SupplementalSourceRepairState.UNARCHIVED
    assert waived_item["effective_action"] == "continue_without_source"
    assert waived_item["resolved"] is True
    assert waived_item["execution_plan"]["impact_kind"] == (
        ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    )
    assert waived_item["rebuild_required"] is False
    # Sign-off is not blocked by a waived source.
    assert review.json()["can_accept"] is True
    assert review.json()["pending_rebuild_count"] == 0

    # --- 3. The source finally exists. --------------------------------------
    async with _client(_application(world, collection_service)) as client:
        archived = await client.post(
            f"/api/subjects/{subject.id}/sources/{second.id}/content",
            json={
                "content": S2_HTML.decode("utf-8"),
                "declared_mime_type": "text/html",
                "final_url": SOURCE_TWO,
            },
        )
    assert archived.status_code == 200, archived.text
    assert collection_factory.collections[second.id].state is CollectionState.ARCHIVED

    # --- 4. A brand-new fetch: the corpus fact now dominates D1. ------------
    fresh = _application(world, collection_service)
    async with _client(fresh) as client:
        after = await client.get(f"/api/editions/{edition.id}/review/repairs?status=all")
        detail = await client.get(f"/api/editions/{edition.id}/review/repairs/{repair_key}")
        review = await client.get(f"/api/editions/{edition.id}/review")
    page = after.json()
    archived_item = page["items"][0]

    # The waiver was never rewritten: it is still the effective decision.
    assert archived_item["effective_action"] == "continue_without_source"
    assert archived_item["effective_decision_id"] == decision_id
    assert [entry["action"] for entry in detail.json()["decision_history"]] == [
        "continue_without_source"
    ]
    assert detail.json()["effective_decision"]["id"] == decision_id

    assert archived_item["repair_state"] == (
        SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES
    )
    assert archived_item["execution_plan"]["impact_kind"] == (
        ProductionRepairImpactKind.SOURCE_CORPUS
    )
    assert archived_item["execution_plan"]["ready_to_apply"] is True
    assert archived_item["rebuild_required"] is True
    assert archived_item["recommended_stage"] == "rebuild_references"
    # The detail endpoint answers exactly the same plan as the list.
    assert detail.json()["execution_plan"] == archived_item["execution_plan"]
    assert detail.json()["rebuild_required"] is True
    # ... and the article the Repair Desk renders offers the reintegration.
    assert page["articles"][0]["execution_plan"]["impact_kind"] == (
        ProductionRepairImpactKind.SOURCE_CORPUS
    )
    assert page["articles"][0]["execution_plan"]["ready_to_apply"] is True
    assert page["articles"][0]["resolved_since_last_build_count"] == 1
    assert page["summary"]["articles_needing_rebuild"] == 1

    # --- 5. Sign-off is refused while the debt stands. ----------------------
    assert review.json()["can_accept"] is False
    assert review.json()["pending_rebuild_count"] == 1
    async with _client(fresh) as client:
        refused = await client.post(f"/api/editions/{edition.id}/publication/accept")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "review_cannot_be_accepted"
    assert world.manifests.items == []

    # The exact payload the React Repair Desk renders in its own test.
    document = await world.artifacts.get_current(run.id, ProductionArtifactStage.PUBLICATION.value)
    assert document is not None
    replacements = {
        str(document.id): "document-1",
        str(edition.id): "edition-lot35",
        str(subject.id): "subject-1",
        str(run.id): "run-1",
        str(references_v1.id): "artifact-references-1",
        str(second.id): "collection-s2",
        repair_key: "source-s2",
        decision_id: "decision-waive",
    }
    _assert_fixture("repair_page", _normalized(page, replacements))
    _assert_fixture("review", _normalized(review.json(), replacements))

    # --- 6. The deterministic REFERENCES rebuild clears the debt. -----------
    async with _client(fresh) as client:
        rebuilt = await client.post(f"/api/editions/{edition.id}/review/items/{subject.id}/rebuild")
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["action"] == "rebuild_references_and_retry"

    references_v2 = await world.artifacts.get_current(
        run.id, ProductionArtifactStage.REFERENCES.value
    )
    assert references_v2 is not None and references_v2.version == 2
    rebuilt_report = await store.read_json(references_v2.canonical_blob_id)  # type: ignore[arg-type]
    assert {source["url"] for source in rebuilt_report["sources"]} == {SOURCE_ONE, SOURCE_TWO}

    replayed = await world.runs.get(run.id)
    assert replayed is not None
    for stage, digest in (
        (ProductionArtifactStage.EXTRACTION, "e"),
        (ProductionArtifactStage.SYNTHESIS, "f"),
        (ProductionArtifactStage.PUBLICATION, "0"),
    ):
        await world.artifacts.append(_stage_artifact(replayed, stage, 2, input_hash=digest * 64))
    replayed.mark_ready()

    # --- 7. The issue and the sign-off debt are gone. -----------------------
    final = _application(world, collection_service)
    async with _client(final) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs?status=all")
        review = await client.get(f"/api/editions/{edition.id}/review")
    assert listed.json()["items"] == []
    assert listed.json()["summary"]["articles_needing_rebuild"] == 0
    assert review.json()["pending_rebuild_count"] == 0
    assert review.json()["can_accept"] is True

    async with _client(final) as client:
        accepted = await client.post(f"/api/editions/{edition.id}/publication/accept")
    assert accepted.status_code == 202, accepted.text
    assert len(world.manifests.items) == 1
    # The whole waive/archive/rebuild cycle ran without a single model call.
    assert world.gateway.calls == 0
