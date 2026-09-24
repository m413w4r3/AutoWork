"""Unit coverage for canonical cross-run production artifact reuse."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_workflow
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.model_gateway import ModelGatewayError
from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_references import (
    production_reference_corpus_from_json,
)
from cti_app.application.production_stages import ReferenceResearchService
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _extraction_input_hash,
    _references_input_hash,
    _synthesis_input_hash,
    production_references_model_run_id,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionReuseInvalidation,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_references import (
    ProductionReferenceTier,
)


class _Artifacts:
    def __init__(self, items: list[ProductionArtifact]) -> None:
        self.items = items
        self.not_before: datetime | None = None
        self.stale: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version, default=None)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale.append((run_id, stage))

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
    def __init__(self, run: ProductionRun) -> None:
        self.production_runs = self
        self.run = run
        self.production_input_snapshots = self

    async def __aenter__(self) -> _WorkflowUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.run if run_id == self.run.id else None

    async def get_by_run(self, run_id: UUID) -> object | None:
        return object() if run_id == self.run.id else None


def _run(*, edition_id: UUID, subject_id: UUID, status: ProductionRunStatus) -> ProductionRun:
    return ProductionRun(
        id=uuid4(),
        edition_id=edition_id,
        subject_id=subject_id,
        status=status,
    )


def _artifact(
    run: ProductionRun,
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
        status=ProductionRunStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=ProductionRunStatus.RUNNING,
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
        status=ProductionRunStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=ProductionRunStatus.RUNNING,
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
    run = _run(edition_id=edition_id, subject_id=subject_id, status=ProductionRunStatus.RUNNING)
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
    run = _run(edition_id=edition_id, subject_id=subject_id, status=ProductionRunStatus.RUNNING)
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
        status=ProductionRunStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=ProductionRunStatus.RUNNING,
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
    run = _run(edition_id=uuid4(), subject_id=uuid4(), status=ProductionRunStatus.RUNNING)
    run.current_stage = ProductionStage.REFERENCES
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
    result = await orchestrator.execute_stage(run.id, ProductionStage.REFERENCES)

    assert result["status"] == "transient_error"
    assert result["error_code"] == "production_reuse_storage_unavailable"
    assert sentinel.called is False


@pytest.mark.asyncio
async def test_invalidation_cutoff_excludes_old_candidate() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    source_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=ProductionRunStatus.READY,
    )
    target_run = _run(
        edition_id=edition_id,
        subject_id=subject_id,
        status=ProductionRunStatus.RUNNING,
    )
    source = _artifact(source_run, created_at=datetime.now(UTC) - timedelta(minutes=2))
    invalidation = ProductionReuseInvalidation(
        edition_id=edition_id,
        subject_id=subject_id,
        from_stage=ProductionStage.EXTRACTION,
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
    run = _run(edition_id=edition_id, subject_id=subject_id, status=ProductionRunStatus.RUNNING)
    run.force_recompute_from_stage = ProductionStage.EXTRACTION

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
        "subject_version": 1,
        "subject_title": "Subject",
        "subject_tlp": TLP.CLEAR,
        "selection_decision_id": uuid4(),
        "origin_discovery_subject_id": uuid4(),
        "canonical_discovery_subject_id": uuid4(),
        "discovery_snapshot_id": uuid4(),
        "discovery_snapshot_version": 1,
        "member_candidate_ids": (),
        "discovery_summary": "Description",
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


def test_references_hash_tracks_functional_snapshot_and_ignores_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id = uuid4()
    source = ProductionInputSource(
        discovery_candidate_id=candidate_id,
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
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(candidate_id,),
        discovery_summary="Description",
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
        research_date=snapshot.research_date,
    )
    assert base == _references_input_hash(
        subject_id=snapshot.subject_id,
        snapshot=replace(snapshot, production_run_id=uuid4(), input_hash="", reuse_basis_hash=""),
        research_date=snapshot.research_date,
    )
    # The research date is functional: two runs of the same Subject on two
    # different dates are two different corpus computations.
    assert (
        _references_input_hash(
            subject_id=snapshot.subject_id,
            snapshot=snapshot,
            research_date=date(2026, 8, 30),
        )
        != base
    )
    for changed in (
        replace(snapshot, discovery_snapshot_version=2, input_hash="", reuse_basis_hash=""),
        replace(snapshot, discovery_summary="Changed", input_hash="", reuse_basis_hash=""),
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
                research_date=changed.research_date,
            )
            != base
        )
    for version_name in (
        "REFERENCES_PROMPT_VERSION",
        "PRODUCTION_REFERENCE_PARSER_VERSION",
        "PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION",
        "REFERENCES_ROUTING_POLICY_VERSION",
    ):
        monkeypatch.setattr(production_workflow, version_name, "next")
        assert (
            _references_input_hash(
                subject_id=snapshot.subject_id,
                snapshot=snapshot,
                research_date=snapshot.research_date,
            )
            != base
        ), version_name
        monkeypatch.undo()


def test_production_references_model_run_id_is_stable_per_run_and_input() -> None:
    run_id = uuid4()
    other_run_id = uuid4()
    references_input_hash = "a" * 64

    identity = production_references_model_run_id(run_id, references_input_hash)

    assert identity == production_references_model_run_id(run_id, references_input_hash)
    assert identity != production_references_model_run_id(other_run_id, references_input_hash)
    assert identity != production_references_model_run_id(run_id, "b" * 64)


def test_extraction_hash_tracks_payload_urls_and_functional_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    for version_name in (
        "EXTRACTION_PROMPT_VERSION",
        "IOC_RULES_PROMPT_VERSION",
        "IOC_RULES_BATCH_PROMPT_VERSION",
        "Q2_MARKDOWN_PARSER_VERSION",
        "Q2_BATCH_PARSER_VERSION",
        "ARTIFACT_VERIFIER_VERSION",
        "IANA_TLD_SNAPSHOT_VERSION",
        "Q2_ROUTING_POLICY_VERSION",
    ):
        monkeypatch.setattr(production_workflow, version_name, "next")
        assert _extraction_input_hash(**kwargs, pipeline_generation=0) != base, version_name
        monkeypatch.undo()


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


# --- AW-010 REFERENCES corpus production, persistence and rebuild -----------


_RAW_REFERENCES = """# REFERENCES
editorial-title: Legacy title kept for the temporary projection

## SOURCE S1

title: Core publication
url: https://core.example/report
publisher: Core Publisher
published-at: 2026-08-01
role: relay
kind: publication
reason: Duplicates a snapshot core URL

## SOURCE S2

title: Technical annex
url: https://annex.example/iocs
publisher: Annex publisher
published-at: 2026-08-02
role: independent
kind: technical_resource
reason: IOC annex for the same incident

## EVENT R1

date: 2026-08-02
sources: S1, S2
text: Legacy event kept for the temporary projection

# UNCERTAINTIES
- kept for the temporary projection
"""


class _BlobStore:
    """In-memory artifact store with real canonical bytes and JSON reads."""

    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}
        self.reads: list[UUID] = []

    def put(self, content: bytes) -> UUID:
        blob_id = uuid4()
        self.blobs[blob_id] = content
        return blob_id

    async def read_bytes(self, blob_id: UUID) -> bytes:
        self.reads.append(blob_id)
        return self.blobs[blob_id]

    async def read_text(self, blob_id: UUID) -> str:
        return (await self.read_bytes(blob_id)).decode("utf-8")

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return json.loads((await self.read_bytes(blob_id)).decode("utf-8"))

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        raw_id = self.put(raw.encode("utf-8")) if raw else None
        canonical_id = (
            self.put(ProductionArtifactStore.canonical_json_bytes(canonical))
            if canonical is not None
            else None
        )
        rendered_id = self.put(rendered.encode("utf-8")) if rendered else None
        return raw_id, canonical_id, rendered_id


class _CollectionsRepository:
    def __init__(self, items: list[SourceCollection] | None = None) -> None:
        self.items = list(items or [])

    async def list_for_subject(self, subject_id: UUID) -> list[SourceCollection]:
        return [item for item in self.items if item.subject_id == subject_id]


class _DocumentsRepository:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items = list(items or [])

    async def list_for_subject(self, subject_id: UUID) -> list[Any]:
        del subject_id
        return list(self.items)


class _CorpusUow:
    def __init__(
        self,
        artifacts: _Artifacts,
        *,
        collections: list[SourceCollection] | None = None,
        documents: list[Any] | None = None,
    ) -> None:
        self.production_artifacts = artifacts
        self.production_reuse_invalidations = _Invalidations()
        self.source_collections = _CollectionsRepository(collections)
        self.source_documents = _DocumentsRepository(documents)
        self.commits = 0

    async def __aenter__(self) -> _CorpusUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _ResearchGateway:
    """A gateway double that counts research submissions."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[Any] = []

    async def research(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            output_text=self.response,
            run=SimpleNamespace(id=request.run_id),
        )


def _core_snapshot(subject_id: UUID, *urls: str) -> ProductionInputSnapshot:
    core_sources = tuple(
        ProductionInputSource(
            discovery_candidate_id=uuid4(),
            source_candidate_id=uuid4(),
            canonical_url=url,
            role=SourceRole.PRIMARY,
            title=f"Core {index}",
            publisher="Core publisher",
            published_at=date(2026, 8, 1),
            tlp=TLP.AMBER,
            sensitivity="public",
            external_llm_allowed=True,
        )
        for index, url in enumerate(urls, start=1)
    )
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=subject_id,
        edition_id=uuid4(),
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=subject_id,
        canonical_discovery_subject_id=subject_id,
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=tuple(source.discovery_candidate_id for source in core_sources),
        discovery_summary="Description",
        actor_or_campaign="Actor",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        research_date=date(2026, 8, 29),
        core_sources=core_sources,
        captured_at=datetime.now(UTC),
    )


def _archived_collection(
    subject_id: UUID, url: str, *, digest: str = "a" * 64
) -> tuple[SourceCollection, Any]:
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=uuid4(),
        requested_url=url,
        canonical_url=url,
        origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
        proposed_role=SourceRole.PRIMARY,
        state=CollectionState.ARCHIVED,
    )
    document = SimpleNamespace(id=uuid4(), decoded_sha256=digest)
    collection.source_document_id = document.id
    return collection, document


def _corpus_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    uow: _CorpusUow,
    store: _BlobStore,
    gateway: Any | None = None,
    collection_service: Any | None = None,
    external_llm_allowed: bool = True,
) -> ProductionWorkflowOrchestrator:
    async def production_context(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return SimpleNamespace(
            subject_title="Subject",
            subject_description="Description",
            actor_info="Actor",
            technical_summary="Summary",
            period_start="2026-08-01",
            period_end="2026-08-31",
            core_sources_text="",
            supporting_sources_text="",
            external_llm_allowed=external_llm_allowed,
        )

    monkeypatch.setattr(production_workflow, "build_subject_production_context", production_context)
    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    factory = cast(Any, lambda: uow)
    orchestrator._uow_factory = factory
    orchestrator._artifact_store = cast(Any, store)
    orchestrator._model_gateway = gateway
    orchestrator._model_service = None
    orchestrator._collection_service = collection_service
    orchestrator._diagnostics = DiagnosticsLog(None)
    orchestrator._correlation_id = "-"
    orchestrator._references = ReferenceResearchService(factory, cast(Any, store))
    orchestrator._artifact_reuse = ProductionArtifactReuseService(factory, cast(Any, store))
    return orchestrator


def _run_for(snapshot: ProductionInputSnapshot, *, status: ProductionRunStatus) -> ProductionRun:
    run = ProductionRun(
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        status=status,
    )
    run.research_date = snapshot.research_date
    return run


@pytest.mark.asyncio
async def test_references_stage_is_stateless_and_persists_only_the_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_url = "https://core.example/report"
    snapshot = _core_snapshot(uuid4(), core_url)
    core_collection, core_document = _archived_collection(
        snapshot.subject_id, core_url, digest="c" * 64
    )
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts, collections=[core_collection], documents=[core_document])
    store = _BlobStore()
    gateway = _ResearchGateway(_RAW_REFERENCES)
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    orchestrator = _corpus_orchestrator(
        monkeypatch, uow=uow, store=store, gateway=gateway
    )

    result = await orchestrator._execute_references_stage(run, None, snapshot)

    assert result["status"] == "success", result
    assert result["core_source_count"] == 1
    assert result["technical_source_count"] == 1
    assert result["eligible_source_count"] == 1
    assert result["unavailable_source_count"] == 1
    assert result["warnings"] == ["supporting_source_unavailable:https://annex.example/iocs"]
    # One stateless research request: no conversation, exact retry identity.
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.conversation is None
    assert request.web_search is True
    assert request.routing_hint is production_workflow.ModelRoutingHint.WEB_RESEARCH
    assert request.run_id == production_references_model_run_id(
        run.id,
        _references_input_hash(
            subject_id=run.subject_id, snapshot=snapshot, research_date=run.research_date
        ),
    )

    artifact = max(artifacts.items, key=lambda item: item.version)
    assert artifact.stage is ProductionArtifactStage.REFERENCES
    assert artifact.version == 1
    assert artifact.model_run_id == request.run_id
    assert artifact.metadata["schema_version"] == 1
    assert artifact.metadata["core_source_count"] == 1
    assert artifact.metadata["supporting_source_count"] == 0
    assert artifact.metadata["technical_source_count"] == 1
    assert artifact.metadata["eligible_source_count"] == 1
    assert artifact.metadata["unavailable_source_count"] == 1
    assert artifact.metadata["research_model_run_id"] == str(request.run_id)
    assert (
        artifact.metadata["parser_version"]
        == production_workflow.PRODUCTION_REFERENCE_PARSER_VERSION
    )
    assert "repair_source_index" not in artifact.metadata
    assert "event_count" not in artifact.metadata
    assert "source_count" not in artifact.metadata
    assert artifact.conversation_turn_id is None

    payload = await store.read_json(cast(UUID, artifact.canonical_blob_id))
    assert set(payload) == {
        "schema_version",
        "subject_id",
        "research_date",
        "production_input_hash",
        "research_status",
        "sources",
        "warnings",
    }
    assert payload["production_input_hash"] == snapshot.input_hash
    assert "production_run_id" not in json.dumps(payload)
    corpus = production_reference_corpus_from_json(payload)
    assert [source.tier for source in corpus.sources] == [
        ProductionReferenceTier.CORE,
        ProductionReferenceTier.TECHNICAL,
    ]
    # The core always wins a duplicated URL, including its snapshot role.
    assert corpus.sources[0].role is SourceRole.PRIMARY
    assert corpus.sources[0].proposed_by_model is False
    assert corpus.sources[0].source_document_id == core_document.id
    assert corpus.sources[0].content_sha256 == "c" * 64
    assert corpus.sources[0].eligible_for_extraction is True
    assert corpus.sources[1].proposed_by_model is True
    assert corpus.sources[1].relevance_reason == "IOC annex for the same incident"
    assert corpus.sources[1].eligible_for_extraction is False
    assert await store.read_text(cast(UUID, artifact.raw_blob_id)) == _RAW_REFERENCES
    assert artifacts.stale == [(run.id, "references")]


@pytest.mark.asyncio
async def test_manual_archive_rebuilds_the_corpus_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_url = "https://core.example/report"
    annex_url = "https://annex.example/iocs"
    snapshot = _core_snapshot(uuid4(), core_url)
    core_collection, core_document = _archived_collection(
        snapshot.subject_id, core_url, digest="c" * 64
    )
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts, collections=[core_collection], documents=[core_document])
    store = _BlobStore()
    gateway = _ResearchGateway(_RAW_REFERENCES)
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)

    first = await orchestrator._execute_references_stage(run, None, snapshot)
    assert first["status"] == "success"
    assert first["eligible_source_count"] == 1
    assert len(gateway.requests) == 1

    noop = await orchestrator._execute_references_stage(run, None, snapshot)
    assert noop["status"] == "cached"
    assert len(artifacts.items) == 1
    assert len(gateway.requests) == 1

    annex_collection, annex_document = _archived_collection(
        snapshot.subject_id, annex_url, digest="e" * 64
    )
    uow.source_collections.items.append(annex_collection)
    uow.source_documents.items.append(annex_document)

    rebuilt = await orchestrator._execute_references_stage(run, None, snapshot)

    assert rebuilt["status"] == "success"
    assert rebuilt["rebuilt"] is True
    assert rebuilt["eligible_source_count"] == 2
    assert rebuilt["unavailable_source_count"] == 0
    # The rebuild replays the RAW, never the model.
    assert len(gateway.requests) == 1
    assert sorted(item.version for item in artifacts.items) == [1, 2]
    current = max(artifacts.items, key=lambda item: item.version)
    corpus = production_reference_corpus_from_json(
        await store.read_json(cast(UUID, current.canonical_blob_id))
    )
    annex = next(source for source in corpus.sources if source.canonical_url == annex_url)
    assert annex.eligible_for_extraction is True
    assert annex.content_sha256 == "e" * 64
    assert annex.source_document_id == annex_document.id
    assert corpus.warnings == ()

    # And a second rebuild of the very same state is a no-op again.
    again = await orchestrator._execute_references_stage(run, None, snapshot)
    assert again["status"] == "cached"
    assert sorted(item.version for item in artifacts.items) == [1, 2]
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_references_cross_run_reuse_skips_research_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_url = "https://core.example/report"
    snapshot = _core_snapshot(uuid4(), core_url)
    core_collection, core_document = _archived_collection(
        snapshot.subject_id, core_url, digest="c" * 64
    )
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts, collections=[core_collection], documents=[core_document])
    store = _BlobStore()
    gateway = _ResearchGateway(_RAW_REFERENCES)
    first_run = _run_for(snapshot, status=ProductionRunStatus.READY)
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)
    await orchestrator._execute_references_stage(first_run, None, snapshot)
    assert len(gateway.requests) == 1
    source_artifact = artifacts.items[0]

    second_run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    assert second_run.id != first_run.id

    result = await orchestrator._execute_references_stage(second_run, None, snapshot)

    assert result["status"] == "reused", result
    assert len(gateway.requests) == 1
    cloned = [item for item in artifacts.items if item.production_run_id == second_run.id]
    assert len(cloned) == 1
    assert cloned[0].canonical_blob_id == source_artifact.canonical_blob_id
    payload = await store.read_json(cast(UUID, cloned[0].canonical_blob_id))
    serialized = json.dumps(payload)
    assert str(first_run.id) not in serialized
    assert str(second_run.id) not in serialized


@pytest.mark.asyncio
async def test_no_usable_core_source_is_needs_review_with_a_persisted_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_url = "https://core.example/report"
    snapshot = _core_snapshot(uuid4(), core_url)
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts)
    store = _BlobStore()
    gateway = _ResearchGateway(_RAW_REFERENCES)
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)

    result = await orchestrator._execute_references_stage(run, None, snapshot)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "references_no_usable_core_source"
    assert result["eligible_source_count"] == 0
    assert len(artifacts.items) == 1
    corpus = production_reference_corpus_from_json(
        await store.read_json(cast(UUID, artifacts.items[0].canonical_blob_id))
    )
    # The corpus is persisted, and both inaccessible sources stay in it.
    assert [source.canonical_url for source in corpus.sources] == [
        core_url,
        "https://annex.example/iocs",
    ]
    assert corpus.warnings == (
        f"core_source_unavailable:{core_url}",
        "supporting_source_unavailable:https://annex.example/iocs",
    )
    assert artifacts.stale == [(run.id, "references")]


@pytest.mark.asyncio
async def test_external_policy_blocks_references_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _core_snapshot(uuid4(), "https://core.example/report")
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts)
    store = _BlobStore()
    gateway = _ResearchGateway(_RAW_REFERENCES)
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    orchestrator = _corpus_orchestrator(
        monkeypatch,
        uow=uow,
        store=store,
        gateway=gateway,
        external_llm_allowed=False,
    )

    result = await orchestrator._execute_references_stage(run, None, snapshot)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "external_llm_blocked"
    assert gateway.requests == []
    assert artifacts.items == []


@pytest.mark.asyncio
async def test_references_rebuild_never_upgrades_a_legacy_imported_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _core_snapshot(uuid4(), "https://core.example/report")
    store = _BlobStore()
    raw_id, canonical_id, _ = await store.store_stage_payloads(
        raw="# REFERENCES\n",
        canonical={
            "parser_version": "production-markdown-v4",
            "schema_version": "2",
            "editorial_title": None,
            "sources": [],
            "events": [],
            "uncertainties": [],
        },
    )
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    imported = ProductionArtifact(
        production_run_id=run.id,
        subject_id=snapshot.subject_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash=_references_input_hash(
            subject_id=run.subject_id, snapshot=snapshot, research_date=run.research_date
        ),
        status=ProductionArtifactStatus.VERIFIED,
        raw_blob_id=raw_id,
        canonical_blob_id=canonical_id,
    )
    artifacts = _Artifacts([imported])
    uow = _CorpusUow(artifacts)
    gateway = _ResearchGateway(_RAW_REFERENCES)
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)

    result = await orchestrator._execute_references_stage(run, None, snapshot)

    assert result["status"] == "cached"
    assert result["artifact_id"] == str(imported.id)
    assert artifacts.items == [imported]
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_references_retry_reuses_the_same_model_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a lost response must not become a second submission."""
    snapshot = _core_snapshot(uuid4(), "https://core.example/report")
    core_collection, core_document = _archived_collection(
        snapshot.subject_id, "https://core.example/report", digest="c" * 64
    )
    artifacts = _Artifacts([])
    uow = _CorpusUow(artifacts, collections=[core_collection], documents=[core_document])
    store = _BlobStore()

    class FlakyGateway(_ResearchGateway):
        def __init__(self) -> None:
            super().__init__(_RAW_REFERENCES)
            self.failures = 1

        async def research(self, request: Any) -> Any:
            self.requests.append(request)
            if self.failures:
                self.failures -= 1
                raise ModelGatewayError("provider response lost after submission")
            return SimpleNamespace(
                output_text=self.response,
                run=SimpleNamespace(id=request.run_id),
            )

    gateway = FlakyGateway()
    run = _run_for(snapshot, status=ProductionRunStatus.RUNNING)
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)

    first = await orchestrator._execute_references_stage(run, None, snapshot)
    assert first["status"] in {"transient_error", "terminal_error"}
    assert artifacts.items == []

    second = await orchestrator._execute_references_stage(run, None, snapshot)

    assert second["status"] == "success"
    assert len(gateway.requests) == 2
    # The same functional run identity, so the gateway can re-read the
    # persisted ModelRun instead of posting the prompt twice.
    assert gateway.requests[0].run_id == gateway.requests[1].run_id
    assert len(artifacts.items) == 1
