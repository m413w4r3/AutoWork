"""Unit coverage for canonical cross-run production artifact reuse."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_workflow import _extraction_input_hash
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionReuseInvalidation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class _Artifacts:
    def __init__(self, items: list[ProductionArtifact]) -> None:
        self.items = items
        self.not_before: datetime | None = None

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version, default=None)

    async def find_reusable(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        stage: str,
        input_hash: str,
        not_before: datetime | None = None,
    ) -> ProductionArtifact | None:
        self.not_before = not_before
        matches = [
            item
            for item in self.items
            if item.subject_id == subject_id
            and item.stage.value == stage
            and item.input_hash == input_hash
            and item.status is ProductionArtifactStatus.VERIFIED
            and item.canonical_blob_id is not None
            and (
                stage != ProductionArtifactStage.SYNTHESIS.value
                or item.rendered_blob_id is not None
            )
            and (not_before is None or item.created_at > not_before)
        ]
        return max(matches, key=lambda item: item.created_at, default=None)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, item: ProductionArtifact) -> None:
        self.items.append(item)


class _Invalidations:
    def __init__(self, items: list[ProductionReuseInvalidation] | None = None) -> None:
        self.items = items or []

    async def list_for_subject(
        self, edition_id: UUID, subject_id: UUID
    ) -> list[ProductionReuseInvalidation]:
        return [
            item
            for item in self.items
            if item.edition_id == edition_id and item.subject_id == subject_id
        ]


class _Uow:
    def __init__(self, artifacts: _Artifacts, invalidations: _Invalidations) -> None:
        self.production_artifacts = artifacts
        self.production_reuse_invalidations = invalidations
        self.commits = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Store:
    def __init__(self, readable: set[UUID]) -> None:
        self.readable = readable
        self.reads: list[UUID] = []

    async def read_bytes(self, blob_id: UUID) -> bytes:
        self.reads.append(blob_id)
        if blob_id not in self.readable:
            raise FileNotFoundError(blob_id)
        return b"payload"


def _run(
    *, edition_id: UUID, subject_id: UUID, status: SubjectProductionStatus
) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=uuid4(),
        edition_id=edition_id,
        subject_id=subject_id,
        status=status,
    )


def _artifact(
    run: SubjectProductionRun,
    *,
    stage: ProductionArtifactStage = ProductionArtifactStage.REFERENCES,
    version: int = 1,
    created_at: datetime | None = None,
    status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
    canonical_blob_id: UUID | None = None,
    rendered_blob_id: UUID | None = None,
) -> ProductionArtifact:
    return ProductionArtifact(
        id=uuid4(),
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=stage,
        version=version,
        input_hash="a" * 64,
        status=status,
        raw_blob_id=uuid4(),
        canonical_blob_id=canonical_blob_id or uuid4(),
        rendered_blob_id=rendered_blob_id,
        model_run_id=uuid4(),
        conversation_turn_id=uuid4(),
        created_at=created_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_cross_run_hit_clones_identity_and_reuses_every_blob() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    source_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.RUNNING,
    )
    source = _artifact(source_run)
    artifacts = _Artifacts([source])
    uow = _Uow(artifacts, _Invalidations())
    store = _Store({cast(UUID, source.canonical_blob_id)})
    service = ProductionArtifactReuseService(cast(Any, lambda: uow), cast(Any, store))

    result = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=source.input_hash,
    )

    assert result is not None and result.reused
    assert result.artifact.id != source.id
    assert result.artifact.production_run_id == target_run.id
    assert result.artifact.reused_from_artifact_id == source.id
    assert result.artifact.canonical_blob_id == source.canonical_blob_id
    assert result.artifact.raw_blob_id == source.raw_blob_id
    assert result.artifact.model_run_id == source.model_run_id
    assert source.reused_from_artifact_id is None
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_missing_required_blob_is_a_miss_and_does_not_append() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    source_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.RUNNING,
    )
    source = _artifact(source_run)
    artifacts = _Artifacts([source])
    uow = _Uow(artifacts, _Invalidations())
    service = ProductionArtifactReuseService(cast(Any, lambda: uow), cast(Any, _Store(set())))

    result = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=source.input_hash,
    )

    assert result is None
    assert artifacts.items == [source]
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_invalidation_cutoff_excludes_old_candidate() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    source_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=SubjectProductionStatus.RUNNING,
    )
    source = _artifact(source_run, created_at=datetime.now(UTC) - timedelta(minutes=2))
    invalidation = ProductionReuseInvalidation(
        edition_id=edition_id,
        subject_id=subject_id,
        from_stage=SubjectProductionStage.EXTRACTION,
        actor_id="operator",
        correlation_id="corr",
        occurred_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    artifacts = _Artifacts([source])
    uow = _Uow(artifacts, _Invalidations([invalidation]))
    service = ProductionArtifactReuseService(
        cast(Any, lambda: uow), cast(Any, _Store({cast(UUID, source.canonical_blob_id)}))
    )

    result = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=source.input_hash,
    )

    assert result is None
    assert artifacts.not_before == invalidation.occurred_at


def test_same_run_cache_has_priority_and_force_only_disables_cross_run() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = _run(edition_id=edition_id, subject_id=subject_id, status=SubjectProductionStatus.RUNNING)
    run.force_recompute_from_stage = SubjectProductionStage.EXTRACTION

    assert cross_run_reuse_allowed(run, ProductionArtifactStage.REFERENCES)
    assert not cross_run_reuse_allowed(run, ProductionArtifactStage.EXTRACTION)
    assert not cross_run_reuse_allowed(run, ProductionArtifactStage.SYNTHESIS)


def test_functional_extraction_hash_ignores_pipeline_generation() -> None:
    kwargs = {
        "subject_id": uuid4(),
        "references_hash": "b" * 64,
        "references_payload_hash": "c" * 64,
        "source_urls": ["https://example.test/source"],
    }
    assert _extraction_input_hash(**kwargs, pipeline_generation=0) == _extraction_input_hash(
        **kwargs, pipeline_generation=9
    )


def test_snapshot_reuse_basis_excludes_research_date() -> None:
    values: dict[str, object] = {
        "production_run_id": uuid4(),
        "subject_id": uuid4(),
        "edition_id": uuid4(),
        "editorial_group_id": uuid4(),
        "editorial_group_version": 1,
        "subject_title": "Subject",
        "subject_description": "Description",
        "actor_or_campaign": "Actor",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "core_sources": (),
        "captured_at": datetime.now(UTC),
    }
    first = ProductionInputSnapshot(**values, research_date=date(2026, 8, 29))
    second_values = dict(values)
    second_values["production_run_id"] = uuid4()
    second = ProductionInputSnapshot(
        **second_values,
        research_date=date(2026, 8, 30),
    )
    assert first.reuse_basis_hash == second.reuse_basis_hash
    assert first.input_hash != second.input_hash
