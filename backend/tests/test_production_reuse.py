"""Unit coverage for canonical cross-run production artifact reuse."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import ProductionReuseStorageUnavailableError
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _extraction_input_hash,
    _references_input_hash,
    _synthesis_input_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
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
            and (
                item.canonical_blob_id is not None
                if stage
                in {
                    ProductionArtifactStage.REFERENCES.value,
                    ProductionArtifactStage.EXTRACTION.value,
                }
                else item.rendered_blob_id is not None
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


class _UnavailableStore:
    async def read_bytes(self, blob_id: UUID) -> bytes:
        raise ProductionReuseStorageUnavailableError(f"storage unavailable for {blob_id}")


class _WorkflowUow:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.subject_production_runs = self
        self.run = run

    async def __aenter__(self) -> _WorkflowUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.run if run_id == self.run.id else None


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
async def test_same_run_needs_review_is_not_a_cache_hit() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = _run(edition_id=edition_id, subject_id=subject_id, status=SubjectProductionStatus.RUNNING)
    current = _artifact(run, status=ProductionArtifactStatus.NEEDS_REVIEW)
    artifacts = _Artifacts([current])
    uow = _Uow(artifacts, _Invalidations())
    service = ProductionArtifactReuseService(
        cast(Any, lambda: uow), cast(Any, _Store({cast(UUID, current.canonical_blob_id)}))
    )

    result = await service.find_or_reuse(
        run=run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=current.input_hash,
        allow_cross_run=False,
    )

    assert result is None
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_same_run_verified_artifact_without_required_blob_is_a_miss() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = _run(edition_id=edition_id, subject_id=subject_id, status=SubjectProductionStatus.RUNNING)
    current = _artifact(run)
    current.canonical_blob_id = None
    artifacts = _Artifacts([current])
    uow = _Uow(artifacts, _Invalidations())
    service = ProductionArtifactReuseService(cast(Any, lambda: uow), cast(Any, _Store(set())))

    result = await service.find_or_reuse(
        run=run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=current.input_hash,
        allow_cross_run=False,
    )

    assert result is None
    assert artifacts.items == [current]
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_storage_outage_is_retryable_and_not_a_cache_miss() -> None:
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
    service = ProductionArtifactReuseService(cast(Any, lambda: uow), cast(Any, _UnavailableStore()))

    with pytest.raises(ProductionReuseStorageUnavailableError) as error:
        await service.find_or_reuse(
            run=target_run,
            stage=ProductionArtifactStage.REFERENCES,
            input_hash=source.input_hash,
        )

    assert error.value.code == "production_reuse_storage_unavailable"
    assert error.value.retryable is True
    assert artifacts.items == [source]
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_storage_outage_stage_is_transient_without_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(edition_id=uuid4(), subject_id=uuid4(), status=SubjectProductionStatus.RUNNING)
    run.current_stage = SubjectProductionStage.REFERENCES
    uow = _WorkflowUow(run)

    class SentinelModel:
        called = False

        async def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.called = True
            raise AssertionError("model must not be called while storage is unavailable")

    sentinel = SentinelModel()
    orchestrator = ProductionWorkflowOrchestrator(
        cast(Any, lambda: uow), model_gateway=cast(Any, sentinel)
    )

    async def unavailable(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        raise ProductionReuseStorageUnavailableError("temporary storage outage")

    monkeypatch.setattr(orchestrator, "_execute_references_stage", unavailable)
    result = await orchestrator.execute_stage(run.id, SubjectProductionStage.REFERENCES)

    assert result["status"] == "transient_error"
    assert result["error_code"] == "production_reuse_storage_unavailable"
    assert sentinel.called is False


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


def test_references_hash_tracks_functional_snapshot_and_ignores_run_identity() -> None:
    source = ProductionInputSource(
        batch_id=uuid4(),
        candidate_id=uuid4(),
        source_candidate_id=uuid4(),
        canonical_url="https://example.test/article",
        role=SourceRole.PRIMARY,
        title="Article",
        publisher="Publisher",
        published_at=date(2026, 8, 1),
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
    )
    snapshot = ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        edition_id=uuid4(),
        editorial_group_id=uuid4(),
        editorial_group_version=1,
        subject_title="Subject",
        subject_description="Description",
        actor_or_campaign="Actor",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        research_date=date(2026, 8, 29),
        core_sources=(source,),
        captured_at=datetime.now(UTC),
    )
    base = _references_input_hash(
        subject_id=snapshot.subject_id,
        snapshot=snapshot,
        subject_title=snapshot.subject_title,
        subject_description=snapshot.subject_description,
        research_date=snapshot.research_date,
    )
    assert base == _references_input_hash(
        subject_id=snapshot.subject_id,
        snapshot=replace(snapshot, production_run_id=uuid4(), input_hash="", reuse_basis_hash=""),
        subject_title=snapshot.subject_title,
        subject_description=snapshot.subject_description,
        research_date=snapshot.research_date,
    )
    for changed in (
        replace(snapshot, editorial_group_version=2, input_hash="", reuse_basis_hash=""),
        replace(snapshot, subject_description="Changed", input_hash="", reuse_basis_hash=""),
        replace(
            snapshot,
            core_sources=(replace(source, canonical_url="https://other.test"),),
            input_hash="",
            reuse_basis_hash="",
        ),
        replace(
            snapshot,
            core_sources=(replace(source, publisher="Other publisher"),),
            input_hash="",
            reuse_basis_hash="",
        ),
    ):
        assert (
            _references_input_hash(
                subject_id=changed.subject_id,
                snapshot=changed,
                subject_title=changed.subject_title,
                subject_description=changed.subject_description,
                research_date=changed.research_date,
            )
            != base
        )


def test_extraction_hash_tracks_payload_urls_and_functional_versions() -> None:
    kwargs = {
        "subject_id": uuid4(),
        "references_hash": "b" * 64,
        "references_payload_hash": "c" * 64,
        "source_urls": ["https://example.test/source"],
    }
    base = _extraction_input_hash(**kwargs, pipeline_generation=0)
    assert base == _extraction_input_hash(**kwargs, pipeline_generation=9)
    assert base != _extraction_input_hash(
        **{**kwargs, "references_payload_hash": "d" * 64}, pipeline_generation=0
    )
    assert base != _extraction_input_hash(
        **{**kwargs, "source_urls": ["https://example.test/other"]}, pipeline_generation=0
    )


def test_synthesis_hash_tracks_content_evidence_and_routing_identity() -> None:
    kwargs = {
        "subject_id": uuid4(),
        "references_hash": "a" * 64,
        "reference_report_hash": "b" * 64,
        "extraction_hash": "c" * 64,
        "technical_extraction_hash": "d" * 64,
        "synthesis_evidence_pack_hash": "e" * 64,
    }
    base = _synthesis_input_hash(**kwargs)
    assert base == _synthesis_input_hash(**kwargs)
    for field in (
        "technical_extraction_hash",
        "synthesis_evidence_pack_hash",
        "prompt_version",
        "routing_policy_version",
    ):
        changed = dict(kwargs)
        if field == "prompt_version":
            assert base != _synthesis_input_hash(**changed, prompt_version="changed")
        elif field == "routing_policy_version":
            assert base != _synthesis_input_hash(**changed, routing_policy_version="changed")
        else:
            changed[field] = "f" * 64
            assert base != _synthesis_input_hash(**changed)
