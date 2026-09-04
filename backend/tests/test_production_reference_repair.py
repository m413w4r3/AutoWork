"""Focused tests for deterministic Q1 reconciliation repairs."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_parsers import (
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_to_json,
)
from cti_app.application.production_repairs import (
    ProductionReferenceRepairService,
    ProductionRepairIssueService,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)

RAW_Q1 = """# REFERENCES
## SOURCE S1
title: First
url: https://one.example/report
publisher: One
role: independent
## SOURCE S2
title: Second
url: https://two.example/report
publisher: Two
role: independent
## EVENT R1
date: 2026-08-02
sources: S1, S2
text: Shared event
## EVENT R2
date: 2026-08-03
sources: S2
text: Second-only event
"""


class _BlobStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, object] = {}

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, object] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        values = (raw, canonical, rendered)
        ids: list[UUID | None] = []
        for value in values:
            blob_id = uuid4() if value is not None else None
            if blob_id is not None:
                self.payloads[blob_id] = value
            ids.append(blob_id)
        return ids[0], ids[1], ids[2]

    async def put_canonical_json(
        self, payload: dict[str, object], *, bucket: str
    ) -> tuple[UUID, str]:
        del bucket
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, "f" * 64

    async def read_text(self, blob_id: UUID) -> str:
        value = self.payloads[blob_id]
        assert isinstance(value, str)
        return value

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        value = self.payloads[blob_id]
        assert isinstance(value, dict)
        return value


class _Artifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []
        self.stale_calls: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale_calls.append((run_id, stage))
        downstream = {
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.PUBLICATION,
        }
        for item in self.items:
            if item.production_run_id == run_id and item.stage in downstream:
                item.status = ProductionArtifactStatus.STALE


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.run if run_id == self.run.id else None

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return replace(self.run) if run_id == self.run.id else None

    async def list_for_edition(self, edition_id: UUID) -> list[SubjectProductionRun]:
        return [self.run] if edition_id == self.run.edition_id else []


class _Uow:
    def __init__(self, run: SubjectProductionRun, collections: list[object]) -> None:
        self.subject_production_runs = _Runs(run)
        self.production_artifacts = _Artifacts()
        self.collections = collections
        self.source_collections = SimpleNamespace(list_for_subject=self._list_collections)
        self.source_documents = SimpleNamespace(list_for_subject=self._list_documents)
        self.editions = SimpleNamespace(get_for_update=lambda _edition_id: self._edition())
        self.committed = False

    async def _edition(self) -> SimpleNamespace:
        return SimpleNamespace(status=EditionStatus.REVIEW)

    async def _list_collections(self, _subject_id: UUID) -> list[object]:
        return self.collections

    async def _list_documents(self, _subject_id: UUID) -> list[object]:
        return [
            item.document
            for item in self.collections
            if getattr(item, "document", None) is not None
        ]

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


def _collection(
    subject_id: UUID,
    url: str,
    *,
    state: CollectionState,
    digest: str | None = None,
) -> SimpleNamespace:
    collection = SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        canonical_url=url,
        state=state,
        error_reason="collection failed" if digest is None else None,
        attempt_count=1,
        source_document_id=None,
        decoded_blob_id=None,
    )
    if digest is not None:
        document = SimpleNamespace(id=uuid4(), decoded_sha256=digest)
        collection.source_document_id = document.id
        collection.document = document
    return collection


@pytest.mark.asyncio
async def test_rebuilds_q1_from_raw_without_model_and_is_idempotent() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = SubjectProductionRun(subject_id=subject_id, edition_id=edition_id)
    run.start_running()
    run.mark_ready()
    first = _collection(
        subject_id,
        "https://one.example/report",
        state=CollectionState.ARCHIVED,
        digest="1" * 64,
    )
    second = _collection(
        subject_id,
        "https://two.example/report",
        state=CollectionState.FAILED_RETRYABLE,
    )
    uow = _Uow(run, [first, second])
    store = _BlobStore()
    parsed = parse_reference_report(RAW_Q1, date(2026, 8, 10))
    assert parsed.value is not None
    initial = reconcile_reference_report_with_archives(parsed.value, {first.canonical_url}).report
    raw_id, canonical_id, _ = await store.store_stage_payloads(
        raw=RAW_Q1, canonical=reference_report_to_json(initial)
    )
    base = ProductionArtifact(
        id=uuid4(),
        production_run_id=run.id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="a" * 64,
        raw_blob_id=raw_id,
        canonical_blob_id=canonical_id,
    )
    uow.production_artifacts.items.extend(
        [
            base,
            ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash="b" * 64,
            ),
            ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=1,
                input_hash="c" * 64,
            ),
        ]
    )

    issues = await ProductionRepairIssueService(
        _Factory(uow), store
    ).list_supplemental_source_issues(edition_id, subject_id)
    assert len(issues) == 1
    assert issues[0].collection_id == second.id

    second.state = CollectionState.ARCHIVED
    second.document = SimpleNamespace(id=uuid4(), decoded_sha256="2" * 64)
    second.source_document_id = second.document.id
    repair = ProductionReferenceRepairService(_Factory(uow), store)
    result = await repair.rebuild_from_archived_q1(run.id, actor_id="analyst")

    assert result.changed is True
    assert result.artifact.id != base.id
    assert result.artifact.raw_blob_id == base.raw_blob_id
    assert result.restored_source_ids == ("S2",)
    assert result.restored_event_ids == ("R2",)
    rebuilt = await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    assert {item["url"] for item in rebuilt["sources"]} == {
        first.canonical_url,
        second.canonical_url,
    }
    assert [item["source_ids"] for item in rebuilt["events"]] == [["S1", "S2"], ["S2"]]
    assert uow.production_artifacts.stale_calls == [(run.id, "references")]
    assert all(
        item.status is ProductionArtifactStatus.STALE
        for item in uow.production_artifacts.items
        if item.stage is ProductionArtifactStage.EXTRACTION
        or item.stage is ProductionArtifactStage.SYNTHESIS
    )

    second_result = await repair.rebuild_from_archived_q1(run.id, actor_id="analyst")
    assert second_result.changed is False
    assert second_result.artifact.id == result.artifact.id
    assert (
        len(
            [
                item
                for item in uow.production_artifacts.items
                if item.stage is ProductionArtifactStage.REFERENCES
            ]
        )
        == 2
    )
