"""Unit coverage for canonical cross-run production artifact reuse."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_extraction, production_workflow
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.model_gateway import ModelExecution, ModelGatewayError, ModelRequest
from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_extraction import extraction_input_hash
from cti_app.application.production_references import (
    production_reference_corpus_from_json,
)
from cti_app.application.production_stages import ReferenceResearchService
from cti_app.application.production_synthesis import (
    ProductionSynthesisExecution,
    SynthesisExecutionStatus,
    build_synthesis_evidence_pack,
    synthesis_input_hash,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _references_input_hash,
    production_references_model_run_id,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelUsage
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionReuseInvalidation,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_to_json
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import production_synthesis_from_json
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_production_synthesis_storage import canonical_pair


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
            and item.canonical_blob_id is not None
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


class _JSONStore(_Store):
    def __init__(self, readable: set[UUID], payload: dict[str, Any]) -> None:
        super().__init__(readable)
        self.payload = payload

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        if blob_id not in self.readable:
            raise FileNotFoundError(blob_id)
        return self.payload


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


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_subject_matches", [True, False])
async def test_editorial_enrichment_reuse_validates_canonical_subject(
    canonical_subject_matches: bool,
) -> None:
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
    source = _artifact(
        source_run,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
    )
    payload_subject = subject_id if canonical_subject_matches else uuid4()
    synthesis, extraction = canonical_pair(payload_subject)
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    store = _JSONStore(
        {cast(UUID, source.canonical_blob_id)},
        editorial_enrichment_to_json(enrichment),
    )
    artifacts = _Artifacts([source])
    uow = _Uow(artifacts, _Invalidations())
    service = ProductionArtifactReuseService(cast(Any, lambda: uow), cast(Any, store))

    result = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        input_hash=source.input_hash,
    )

    assert (result is not None) is canonical_subject_matches
    if result is not None:
        assert result.reused is True
        assert result.artifact.reused_from_artifact_id == source.id
        assert result.artifact.model_run_id == source.model_run_id


def test_same_run_cache_has_priority_and_force_only_disables_cross_run() -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = _run(edition_id=edition_id, subject_id=subject_id, status=ProductionRunStatus.RUNNING)
    run.force_recompute_from_stage = ProductionStage.EXTRACTION

    assert cross_run_reuse_allowed(run, ProductionArtifactStage.REFERENCES)
    assert not cross_run_reuse_allowed(run, ProductionArtifactStage.EXTRACTION)
    assert not cross_run_reuse_allowed(run, ProductionArtifactStage.SYNTHESIS)

    run.force_recompute_from_stage = ProductionStage.EDITORIAL_ENRICHMENT
    assert cross_run_reuse_allowed(run, ProductionArtifactStage.SYNTHESIS)
    assert not cross_run_reuse_allowed(run, ProductionArtifactStage.EDITORIAL_ENRICHMENT)


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
        "publication_language": "fr",
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
    changed_language = ProductionInputSnapshot(
        **{**values, "production_run_id": uuid4(), "publication_language": "en"},
        research_date=date(2026, 8, 30),
    )
    assert first.reuse_basis_hash != changed_language.reuse_basis_hash
    assert first.input_hash != changed_language.input_hash


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
        publication_language="fr",
        research_date=date(2026, 8, 29),
        core_sources=(source,),
        captured_at=datetime.now(UTC),
    )
    base = _references_input_hash(
        snapshot=snapshot,
        research_date=snapshot.research_date,
    )
    assert base == _references_input_hash(
        snapshot=replace(snapshot, production_run_id=uuid4(), input_hash="", reuse_basis_hash=""),
        research_date=snapshot.research_date,
    )
    # The research date is functional: two runs of the same Subject on two
    # different dates are two different corpus computations.
    assert (
        _references_input_hash(
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


def test_extraction_hash_tracks_the_corpus_and_functional_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = extraction_input_hash(references_corpus_hash="b" * 64)
    assert base == extraction_input_hash(references_corpus_hash="b" * 64)
    assert base != extraction_input_hash(references_corpus_hash="c" * 64)
    for version_name in (
        "PRODUCTION_EXTRACTION_SERVICE_VERSION",
        "EXTRACTION_PROFILE_POLICY_VERSION",
        "Q2_EXTRACTION_CONTRACT_VERSION",
        "SOURCE_TEXT_CONTRACT_VERSION",
        "CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION",
        "EXTRACTION_RESPONSE_PARSER_VERSION",
        "ARTIFACT_VERIFIER_VERSION",
        "EXTRACTION_MODEL_POLICY_VERSION",
        "EXTRACTION_ROUTING_POLICY_VERSION",
    ):
        monkeypatch.setattr(production_extraction, version_name, "next")
        assert extraction_input_hash(references_corpus_hash="b" * 64) != base, version_name
        monkeypatch.undo()


def test_synthesis_hash_tracks_extraction_and_every_policy_version() -> None:
    run = _synthesis_run()
    snapshot = _synthesis_snapshot(run)
    extraction = _synthesis_extraction(snapshot, uuid4())
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    base = synthesis_input_hash(snapshot, extraction, pack, "a" * 64)

    assert base == synthesis_input_hash(snapshot, extraction, pack, "a" * 64)
    assert base != synthesis_input_hash(snapshot, extraction, pack, "b" * 64)
    for version in (
        "prompt_version",
        "validator_version",
        "model_policy_version",
        "routing_policy_version",
    ):
        changed = synthesis_input_hash(snapshot, extraction, pack, "a" * 64, **{version: "changed"})
        assert base != changed, version
    changed_extraction = replace(extraction, warnings=("changed",))
    assert base != synthesis_input_hash(snapshot, changed_extraction, pack, "a" * 64)


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
        return cast(dict[str, Any], json.loads((await self.read_bytes(blob_id)).decode("utf-8")))

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
        publication_language="fr",
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
    orchestrator = _corpus_orchestrator(monkeypatch, uow=uow, store=store, gateway=gateway)

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
        _references_input_hash(snapshot=snapshot, research_date=run.research_date),
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
        input_hash=_references_input_hash(snapshot=snapshot, research_date=run.research_date),
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


# --- canonical Synthesis orchestration (AW-012 cutover) ----------------------


class _SynthesisArtifacts:
    """Artifact repository double: the REFERENCES stage is never readable."""

    _READABLE_STAGES: ClassVar[frozenset[str]] = frozenset({"extraction", "synthesis"})

    def __init__(self, items: list[ProductionArtifact] | None = None) -> None:
        self.items = list(items or [])
        self.requested: list[tuple[UUID, str]] = []
        self.appended: list[ProductionArtifact] = []
        self.stale: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        self.requested.append((run_id, stage))
        if stage not in self._READABLE_STAGES:
            raise AssertionError(f"canonical synthesis must not read the {stage} artifact")
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
        del edition_id
        matches = [
            item
            for item in self.items
            if item.subject_id == subject_id
            and item.stage.value == stage
            and item.input_hash == input_hash
            and item.status is ProductionArtifactStatus.VERIFIED
            and item.canonical_blob_id is not None
            and (not_before is None or item.created_at > not_before)
        ]
        return max(matches, key=lambda item: item.created_at, default=None)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, item: ProductionArtifact) -> None:
        self.appended.append(item)
        self.items.append(item)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale.append((run_id, stage))


class _SynthesisRuns:
    def __init__(self, runs: dict[UUID, ProductionRun]) -> None:
        self.runs = runs

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.runs.get(run_id)

    async def get_for_update(self, run_id: UUID) -> ProductionRun | None:
        return self.runs.get(run_id)


class _SynthesisSnapshots:
    def __init__(self, snapshots: dict[UUID, ProductionInputSnapshot]) -> None:
        self.snapshots = snapshots

    async def get_by_run(self, run_id: UUID) -> ProductionInputSnapshot | None:
        return self.snapshots.get(run_id)


class _SynthesisDocuments:
    def __init__(self, documents: list[SourceDocument]) -> None:
        self.documents = {document.id: document for document in documents}

    async def get(self, document_id: UUID) -> SourceDocument | None:
        return self.documents.get(document_id)


class _SynthesisUow:
    """Only the frozen snapshot, the extraction and its documents are reachable."""

    def __init__(
        self,
        *,
        runs: dict[UUID, ProductionRun],
        snapshots: dict[UUID, ProductionInputSnapshot],
        artifacts: _SynthesisArtifacts,
        documents: list[SourceDocument] | None = None,
    ) -> None:
        self.production_runs = _SynthesisRuns(runs)
        self.production_input_snapshots = _SynthesisSnapshots(snapshots)
        self.production_artifacts = artifacts
        self.production_reuse_invalidations = _Invalidations()
        self.source_documents = _SynthesisDocuments(list(documents or []))
        self.commits = 0

    async def __aenter__(self) -> _SynthesisUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _SynthesisGateway:
    """A drafting gateway double returning text blocks; every request is recorded."""

    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal
        self.requests: list[ModelRequest] = []
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}

    def _wire_text(self) -> str:
        lines = ["@@LEAD@@"]
        lead = cast(list[dict[str, Any]], self.proposal["lead"])
        for index, claim in enumerate(lead, start=1):
            lines.extend(
                (
                    f"@@CLAIM L{index:03d}@@",
                    f"EVIDENCE: {', '.join(claim['evidence_handles'])}",
                    f"TEXT: {claim['text']}",
                )
            )
        return "\n".join(lines)

    async def draft(
        self, request: ModelRequest, output_schema: object | None = None
    ) -> ModelExecution:
        self.requests.append(request)
        text = self._wire_text()
        raw_bytes = text.encode("utf-8")
        run = ModelRun(
            provider=ModelProvider.OPENAI,
            model_role=ModelRole.DRAFTING,
            requested_model="gpt-5",
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash=hashlib.sha256(request.text.encode("utf-8")).hexdigest(),
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=dict(request.parameters),
            id=request.run_id or uuid4(),
        )
        reference = f"model-output://{run.id}"
        run.raw_output_reference = reference
        run.raw_output_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        run.raw_output_chars = len(text)
        run.succeed(
            actual_model_version="gpt-5",
            duration_ms=3,
            usage=ModelUsage(total_tokens=7),
            output_references=(reference,),
            response_id=None,
        )
        self.outputs[reference] = raw_bytes
        self.runs[run.id] = run
        return ModelExecution(run=run, output_text=text)

    async def get_run(self, run_id: UUID) -> ModelRun | None:
        return self.runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        return self.outputs[reference]

    async def archive_output(self, content: bytes, *, mime_type: str) -> str:
        del mime_type
        reference = f"model-normalized://{len(self.outputs)}"
        self.outputs[reference] = content
        return reference

    async def record_output_diagnostics(self, run_id: UUID, **values: object) -> None:
        run = self.runs[run_id]
        run.normalized_output_reference = values["normalized_reference"]  # type: ignore[assignment]
        run.normalized_output_sha256 = values["normalized_sha256"]  # type: ignore[assignment]
        run.parser_stage = values["parser_stage"]  # type: ignore[assignment]
        run.normalization_version = values["normalization_version"]  # type: ignore[assignment]
        run.transformations = values["transformations"]  # type: ignore[assignment]
        run.validation_errors = values["validation_errors"]  # type: ignore[assignment]


class _RefusingDraftGateway:
    """Exact reuse must never reach the model, so any draft call fails the test."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def draft(
        self, request: ModelRequest, output_schema: object | None = None
    ) -> ModelExecution:
        self.requests.append(request)
        raise AssertionError("exact synthesis reuse must not submit a drafting request")


def _synthesis_run(
    *, subject_id: UUID | None = None, edition_id: UUID | None = None
) -> ProductionRun:
    run = ProductionRun(
        subject_id=subject_id or uuid4(),
        edition_id=edition_id or uuid4(),
        status=ProductionRunStatus.RUNNING,
    )
    run.current_stage = ProductionStage.SYNTHESIS
    return run


def _synthesis_snapshot(run: ProductionRun) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=run.id,
        edition_id=run.edition_id,
        subject_id=run.subject_id,
        subject_version=1,
        subject_title="Frozen subject title",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=run.subject_id,
        canonical_discovery_subject_id=run.subject_id,
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(uuid4(),),
        discovery_summary="A discovery summary.",
        actor_or_campaign="Example actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        publication_language="fr",
        research_date=date(2026, 3, 1),
    )


def _synthesis_source_document(subject_id: UUID, source_id: UUID) -> SourceDocument:
    return SourceDocument(
        id=source_id,
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="vendor-report.pdf",
        origin="test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
    )


def _synthesis_extraction(
    snapshot: ProductionInputSnapshot, source_id: UUID
) -> ProductionExtractionV1:
    source = ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url="https://vendor.example/report",
        content_sha256=hashlib.sha256(b"vendor report").hexdigest(),
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(
            ExtractionFactV1(
                category="malware",
                value="FooRAT",
                attack_id=None,
                context="Initial access",
                evidence_quote="The report identifies FooRAT.",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(source_id,),
            ),
        ),
        events=(
            ExtractionEventV1(
                event_date=date(2026, 7, 2),
                date_text=None,
                text="The campaign began.",
                context="Campaign chronology.",
                evidence_quote="On 2026-07-02 the campaign began.",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(source_id,),
            ),
        ),
        indicators=(),
        rules=(),
        uncertainties=("Attribution remains uncertain.",),
    )
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )


def _synthesis_proposal(handles: dict[str, str]) -> dict[str, object]:
    return {
        "lead": [
            {
                "text": "FooRAT was identified.",
                "evidence_handles": [handles["fact"], handles["event"]],
            }
        ],
        "sections": [],
    }


def _synthesis_world() -> SimpleNamespace:
    """One canonical extraction, ready for the SYNTHESIS stage."""
    run = _synthesis_run()
    snapshot = _synthesis_snapshot(run)
    source_id = uuid4()
    extraction = _synthesis_extraction(snapshot, source_id)
    document = _synthesis_source_document(run.subject_id, source_id)
    store = _BlobStore()
    extraction_blob_id = store.put(
        ProductionArtifactStore.canonical_json_bytes(production_extraction_to_json(extraction))
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="c" * 64,
        canonical_blob_id=extraction_blob_id,
    )
    artifacts = _SynthesisArtifacts([extraction_artifact])
    uow = _SynthesisUow(
        runs={run.id: run},
        snapshots={run.id: snapshot},
        artifacts=artifacts,
        documents=[document],
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    handles = {str(entry["kind"]): str(entry["handle"]) for entry in pack.narrative_evidence}
    gateway = _SynthesisGateway(_synthesis_proposal(handles))
    orchestrator = ProductionWorkflowOrchestrator(
        cast(Any, lambda: uow),
        model_gateway=cast(Any, gateway),
        artifact_store=cast(Any, store),
    )
    return SimpleNamespace(
        run=run,
        snapshot=snapshot,
        extraction=extraction,
        source_id=source_id,
        document=document,
        extraction_blob_id=extraction_blob_id,
        extraction_artifact=extraction_artifact,
        artifacts=artifacts,
        uow=uow,
        store=store,
        pack=pack,
        gateway=gateway,
        orchestrator=orchestrator,
    )


@pytest.mark.asyncio
async def test_synthesis_stage_uses_only_the_canonical_extraction_artifact() -> None:
    world = _synthesis_world()
    for legacy_input in (
        "load_reference_projection",
        "legacy_technical_extraction_from_payload",
        "reference_report_to_json",
    ):
        assert not hasattr(production_workflow, legacy_input)

    result = await world.orchestrator.execute_stage(world.run.id, ProductionStage.SYNTHESIS)

    assert result["status"] == "success"
    assert result["mode"] == "fresh"
    assert {stage for _, stage in world.artifacts.requested} == {"extraction", "synthesis"}
    assert not hasattr(world.run, "synthesis_conversation_id")
    assert len(world.gateway.requests) == 1
    request = world.gateway.requests[0]
    assert request.web_search is False
    assert request.conversation is None
    assert request.run_id is not None
    stored = world.artifacts.appended[-1]
    assert stored.stage is ProductionArtifactStage.SYNTHESIS
    assert stored.model_run_id is not None
    assert stored.canonical_blob_id is not None
    canonical = production_synthesis_from_json(
        await world.store.read_json(stored.canonical_blob_id)
    )
    assert canonical.title == world.snapshot.subject_title
    assert canonical.publication_language == "fr"
    assert canonical.lead[0].text == "FooRAT was identified."
    assert canonical.timeline[0].event_date == date(2026, 7, 2)


@pytest.mark.asyncio
async def test_synthesis_stage_exact_reuse_returns_zero_drafting_calls() -> None:
    first = _synthesis_world()
    result = await first.orchestrator.execute_stage(first.run.id, ProductionStage.SYNTHESIS)
    assert result["status"] == "success"
    source_artifact = first.artifacts.appended[-1]

    run_b = _synthesis_run(subject_id=first.run.subject_id, edition_id=first.run.edition_id)
    snapshot_b = replace(first.snapshot, production_run_id=run_b.id)
    extraction_artifact_b = ProductionArtifact(
        production_run_id=run_b.id,
        subject_id=run_b.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="d" * 64,
        canonical_blob_id=first.extraction_blob_id,
    )
    artifacts_b = _SynthesisArtifacts([extraction_artifact_b, source_artifact])
    uow_b = _SynthesisUow(
        runs={run_b.id: run_b},
        snapshots={run_b.id: snapshot_b},
        artifacts=artifacts_b,
        documents=[first.document],
    )
    gateway_b = _RefusingDraftGateway()
    orchestrator_b = ProductionWorkflowOrchestrator(
        cast(Any, lambda: uow_b),
        model_gateway=cast(Any, gateway_b),
        artifact_store=cast(Any, first.store),
    )

    reused = await orchestrator_b.execute_stage(run_b.id, ProductionStage.SYNTHESIS)

    assert reused["status"] == "reused"
    assert reused["reused"] is True
    assert reused["reused_from_artifact_id"] == str(source_artifact.id)
    assert gateway_b.requests == []
    cloned = artifacts_b.appended[-1]
    assert cloned.reused_from_artifact_id == source_artifact.id
    assert cloned.canonical_blob_id == source_artifact.canonical_blob_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_payload", "not_verified"])
async def test_synthesis_stage_rejects_an_invalid_canonical_extraction(failure: str) -> None:
    world = _synthesis_world()
    if failure == "invalid_payload":
        world.store.blobs[world.extraction_blob_id] = b'{"schema_version": 1}'
    else:
        world.artifacts.items = [
            replace(world.extraction_artifact, status=ProductionArtifactStatus.NEEDS_REVIEW)
        ]

    result = await world.orchestrator.execute_stage(world.run.id, ProductionStage.SYNTHESIS)

    assert result["status"] == "terminal_error"
    assert result["error_code"] == "synthesis_inputs_missing"
    assert world.gateway.requests == []
    assert world.artifacts.appended == []


@pytest.mark.asyncio
async def test_synthesis_stage_returns_needs_review_without_format_repair() -> None:
    world = _synthesis_world()
    world.gateway.proposal = {
        "lead": [{"text": "FooRAT was identified.", "evidence_handles": ["E999"]}],
        "sections": [],
    }

    result = await world.orchestrator.execute_stage(world.run.id, ProductionStage.SYNTHESIS)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "synthesis_unknown_evidence"
    # Exactly one submission: an invalid answer is never re-asked.
    assert len(world.gateway.requests) == 1
    assert world.artifacts.appended == []


def test_synthesis_stage_result_preserves_submission_reconciliation() -> None:
    model_run_id = uuid4()
    execution = ProductionSynthesisExecution(
        status=SynthesisExecutionStatus.NEEDS_REVIEW,
        mode=SynthesisMode.FRESH,
        model_run_id=model_run_id,
        input_hash="a" * 64,
        extraction_hash="b" * 64,
        model_calls=1,
        error_code=PRODUCTION_RECONCILIATION_ERROR_CODE,
        error="A provider submission may have been accepted and must be reconciled",
        details={
            "error_code": PRODUCTION_RECONCILIATION_ERROR_CODE,
            "model_run_id": str(model_run_id),
        },
    )

    result = ProductionWorkflowOrchestrator._synthesis_execution_result(execution)

    assert result["status"] == "needs_review"
    assert result["error_code"] == PRODUCTION_RECONCILIATION_ERROR_CODE
    assert result["details"]["model_run_id"] == str(model_run_id)
    assert result["model_calls"] == 1
