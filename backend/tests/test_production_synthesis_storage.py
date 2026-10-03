"""Canonical synthesis storage and exact reuse boundaries."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
from cti_app.application.production_stages import SynthesisService
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
    render_synthesis_markdown,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
)


class MemoryStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}

    async def store_stage_payloads(self, *, raw=None, canonical=None, rendered=None):
        def save(value: object) -> UUID:
            blob_id = uuid4()
            self.blobs[blob_id] = (
                json.dumps(
                    value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
                if isinstance(value, dict)
                else str(value).encode()
            )
            return blob_id

        return (
            save(raw) if raw is not None else None,
            save(canonical) if canonical is not None else None,
            save(rendered) if rendered is not None else None,
        )

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.blobs[blob_id]

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.blobs[blob_id]))


class MemoryArtifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []
        self.staled: list[tuple[UUID, str]] = []

    async def list_for_run(self, run_id):
        return [item for item in self.items if item.production_run_id == run_id]

    async def get_current(self, run_id, stage):
        candidates = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is ProductionArtifactStatus.VERIFIED
        ]
        return max(candidates, key=lambda item: item.version) if candidates else None

    async def append(self, artifact):
        self.items.append(artifact)

    async def mark_downstream_stale(self, run_id, stage):
        self.staled.append((run_id, stage))

    async def find_reusable(self, *, edition_id, subject_id, stage, input_hash, not_before):
        return next(
            (
                item
                for item in self.items
                if item.subject_id == subject_id
                and item.stage.value == stage
                and item.input_hash == input_hash
                and (not_before is None or item.created_at >= not_before)
            ),
            None,
        )


class MemoryUow:
    def __init__(self):
        self.production_artifacts = MemoryArtifacts()
        self.commits = 0

    async def commit(self):
        self.commits += 1


def uow_factory(uow):
    @asynccontextmanager
    async def factory():
        yield uow

    return factory


def canonical_pair(subject_id: UUID):
    document_id = uuid4()
    fact = ExtractionFactV1(
        category="malware",
        value="FooRAT",
        attack_id=None,
        context="",
        evidence_quote="FooRAT observed",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(document_id,),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=document_id,
                canonical_url="https://example.com/report",
                content_sha256="c" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(fact,),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    extraction_hash = canonical_extraction_hash(extraction)
    paragraph = SynthesisParagraphV1(
        text="FooRAT was observed.", evidence_refs=extraction_evidence_refs_v1(extraction)
    )
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=extraction_hash,
        publication_language="en",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="FooRAT report",
        lead=(paragraph,),
        sections=(SynthesisSectionV1(SynthesisSectionKind.TECHNICAL, "Technical", (paragraph,)),),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    return synthesis, extraction


@pytest.mark.asyncio
async def test_canonical_storage_preview_metadata_versions_and_stale():
    uow, store = MemoryUow(), MemoryStore()
    run_id, subject_id, model_run_id = uuid4(), uuid4(), uuid4()
    synthesis, extraction = canonical_pair(subject_id)
    synthesis = replace(synthesis, warnings=("Dropped an unresolved timeline event.",))
    diagnostics = {
        "warnings": ["Dropped an unresolved timeline event."],
        "missing_coverage": ["No execution-chain detail was extracted."],
    }
    service = SynthesisService(uow_factory(uow), store)
    for version in (1, 2):
        artifact = await service.store_synthesis_result(
            run_id=run_id,
            subject_id=subject_id,
            input_hash="d" * 64,
            synthesis=synthesis,
            extraction=extraction,
            raw_result="raw proposal",
            model_run_id=model_run_id,
            mode=SynthesisMode.FRESH,
            model_policy_version="model-v1",
            diagnostics=diagnostics,
        )
        assert artifact.version == version
        assert artifact.canonical_blob_id is not None
        assert artifact.raw_blob_id is not None
        assert artifact.rendered_blob_id is not None
        assert json.loads(store.blobs[artifact.canonical_blob_id])["title"] == synthesis.title
        assert store.blobs[artifact.raw_blob_id] == b"raw proposal"
        assert store.blobs[artifact.rendered_blob_id].decode() == render_synthesis_markdown(
            synthesis, extraction
        )
        rendered = store.blobs[artifact.rendered_blob_id].decode()
        assert "Dropped an unresolved timeline event." not in rendered
        assert "Technical" not in rendered
        assert artifact.metadata["diagnostics"] == diagnostics
        assert artifact.metadata["paragraph_count"] == 2
        assert artifact.metadata["evidence_ref_count"] == 1
        assert artifact.metadata["model_policy_version"] == "model-v1"
        assert "FooRAT" not in str(artifact.metadata)
    assert uow.production_artifacts.staled == [(run_id, "synthesis")] * 2


@pytest.mark.asyncio
async def test_cross_run_reuse_requires_valid_canonical_blob():
    uow, store = MemoryUow(), MemoryStore()
    subject_id, edition_id = uuid4(), uuid4()
    source_run = ProductionRun(subject_id=subject_id, edition_id=edition_id)
    target_run = ProductionRun(subject_id=subject_id, edition_id=edition_id)
    synthesis, extraction = canonical_pair(subject_id)
    original = await SynthesisService(uow_factory(uow), store).store_synthesis_result(
        run_id=source_run.id,
        subject_id=subject_id,
        input_hash="d" * 64,
        synthesis=synthesis,
        extraction=extraction,
        raw_result=None,
        model_run_id=None,
    )
    reuse = ProductionArtifactReuseService(uow_factory(uow), store)
    result = await reuse.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash="d" * 64,
    )
    assert result is not None and result.reused
    assert result.artifact.canonical_blob_id == original.canonical_blob_id
    assert result.artifact.reused_from_artifact_id == original.id
    assert result.artifact.rendered_blob_id == original.rendered_blob_id
    assert result.artifact.metadata["mode"] == SynthesisMode.REUSE_EXACT.value
    assert "FooRAT" not in str(result.artifact.metadata)
    assert len(store.blobs) == 2  # canonical and deterministic preview; no reconstruction


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"bad": "schema"}])
async def test_reuse_refuses_rendered_only_or_malformed_canonical(payload):
    uow, store = MemoryUow(), MemoryStore()
    subject_id, edition_id = uuid4(), uuid4()
    run = ProductionRun(subject_id=subject_id, edition_id=edition_id)
    canonical_id = None
    if payload is not None:
        canonical_id = uuid4()
        store.blobs[canonical_id] = json.dumps(payload).encode()
    legacy = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
        rendered_blob_id=uuid4(),
    )
    uow.production_artifacts.items.append(legacy)
    reuse = ProductionArtifactReuseService(uow_factory(uow), store)
    result = await reuse.find_or_reuse(
        run=run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash="f" * 64,
        allow_cross_run=False,
    )
    assert result is None
    assert uow.production_artifacts.items == [legacy]
