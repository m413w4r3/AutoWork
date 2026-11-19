from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from cti_app.application.editorial_synthesis import candidate as candidate_core
from cti_app.application.editorial_synthesis import targets
from cti_app.application.editorial_synthesis.patch_validation import WirePatch
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_stages import SynthesisService
from cti_app.application.production_synthesis import (
    SynthesisEvidencePackV1,
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import _apply_wire_patch
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    SynthesisMode,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import (
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import (
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication_kind import PublicationKind


@dataclass(frozen=True, slots=True)
class _CandidateFixture:
    run_id: UUID
    snapshot: ProductionInputSnapshot
    extraction: ProductionExtractionV1
    evidence_pack: SynthesisEvidencePackV1
    synthesis: ProductionSynthesisV1
    payload: dict[str, Any]
    target: TargetedEditorialRevisionTarget
    operation: TargetedEditorialRevisionOperation
    patch: WirePatch
    base_sha256: str
    base_artifact: ProductionArtifact


def _fixture() -> _CandidateFixture:
    run_id = uuid4()
    subject_id = uuid4()
    snapshot = ProductionInputSnapshot(
        production_run_id=run_id,
        edition_id=uuid4(),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Synthetic subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Synthetic summary.",
        actor_or_campaign="Synthetic actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        publication_language="fr",
        research_date=date(2026, 2, 1),
    )
    source_document_id = uuid4()
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=source_document_id,
                canonical_url="https://example.test/synthetic-report",
                content_sha256="d" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(
                    ExtractionFactV1(
                        category="other_technical",
                        value="Synthetic finding",
                        attack_id=None,
                        context="Synthetic test context.",
                        evidence_quote="The synthetic report records this finding.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source_document_id,),
                    ),
                ),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="[Synthetic actor] Synthetic report",
        lead=(SynthesisParagraphV1("A synthetic lead claim.", (evidence_ref,)),),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Synthetic observations",
                (SynthesisParagraphV1("A synthetic section claim.", (evidence_ref,)),),
            ),
        ),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    evidence_pack = build_synthesis_evidence_pack(snapshot, extraction)
    payload = production_synthesis_to_json(synthesis)
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.SECTION,
        stable_ids=(targets._section_id(payload["sections"][0]),),
    )
    operation = TargetedEditorialRevisionOperation.RENAME_SECTION
    resolved = targets.resolve_target(payload, target, operation)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    patch = WirePatch(
        target=resolved.path,
        action=operation,
        base_artifact_hash=base_sha256,
        before_hash=resolved.before_hash,
        after="Revised synthetic observations",
        rationale="Synthetic characterization patch.",
    )
    base_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    return _CandidateFixture(
        run_id=run_id,
        snapshot=snapshot,
        extraction=extraction,
        evidence_pack=evidence_pack,
        synthesis=synthesis,
        payload=payload,
        target=target,
        operation=operation,
        patch=patch,
        base_sha256=base_sha256,
        base_artifact=base_artifact,
    )


def _build_candidate(fixture: _CandidateFixture) -> candidate_core.SynthesisCandidate:
    return candidate_core.build_candidate_from_validated_patch(
        base_artifact=fixture.base_artifact,
        base_payload=fixture.payload,
        base_synthesis=fixture.synthesis,
        base_canonical_sha256=fixture.base_sha256,
        patch=fixture.patch,
        target=fixture.target,
        operation=fixture.operation,
        evidence_pack=fixture.evidence_pack,
        extraction=fixture.extraction,
        snapshot=fixture.snapshot,
        apply_patch=_apply_wire_patch,
        publication_kind=PublicationKind.BRIEF,
    )


def test_candidate_core_matches_legacy_revise_candidate_bytes() -> None:
    fixture = _fixture()
    legacy_synthesis, legacy_payload, legacy_before_hash = _apply_wire_patch(
        base_synthesis=fixture.synthesis,
        base_payload=fixture.payload,
        target=fixture.target,
        operation=fixture.operation,
        patch=fixture.patch,
        evidence_pack=fixture.evidence_pack,
        extraction=fixture.extraction,
        snapshot=fixture.snapshot,
        publication_kind=PublicationKind.BRIEF,
    )
    legacy_bytes = ProductionArtifactStore.canonical_json_bytes(legacy_payload)
    legacy_sha256 = hashlib.sha256(legacy_bytes).hexdigest()

    candidate = _build_candidate(fixture)

    assert candidate.synthesis == legacy_synthesis
    assert candidate.payload == legacy_payload
    assert candidate.before_hash == legacy_before_hash
    assert candidate.canonical_bytes == legacy_bytes
    assert candidate.canonical_sha256 == legacy_sha256


def test_candidate_core_makes_no_model_call_and_no_write() -> None:
    fixture = _fixture()
    model_gateway_calls = 0
    persistence_write_calls = 0
    blob_write_calls = 0

    candidate = _build_candidate(fixture)

    assert candidate.canonical_bytes
    assert model_gateway_calls == 0
    assert persistence_write_calls == 0
    assert blob_write_calls == 0
    parameters = inspect.signature(candidate_core.build_candidate_from_validated_patch).parameters
    assert not {"model_gateway", "persistence", "artifact_store", "uow"}.intersection(parameters)


class _StableMemoryBlobStore:
    def __init__(self) -> None:
        self.bytes: dict[UUID, bytes] = {}
        self.payloads: dict[UUID, dict[str, Any]] = {}
        self.read_bytes_calls = 0
        self.write_calls = 0

    def add_json(self, payload: dict[str, Any]) -> UUID:
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        blob_id = uuid5(NAMESPACE_URL, f"candidate-core-test:{digest}")
        self.bytes[blob_id] = encoded
        self.payloads[blob_id] = payload
        return blob_id

    def upload_json_bytes(self, content: bytes) -> UUID:
        digest = hashlib.sha256(content).hexdigest()
        blob_id = uuid5(NAMESPACE_URL, f"candidate-core-test:{digest}")
        self.bytes[blob_id] = content
        self.payloads[blob_id] = json.loads(content)
        self.write_calls += 1
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]

    async def read_bytes(self, blob_id: UUID, **kwargs: object) -> bytes:
        del kwargs
        self.read_bytes_calls += 1
        return self.bytes[blob_id]

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        self.write_calls += 1
        raw_id = None
        if raw is not None:
            raw_bytes = raw.encode("utf-8")
            raw_id = uuid5(
                NAMESPACE_URL,
                f"candidate-core-raw:{hashlib.sha256(raw_bytes).hexdigest()}",
            )
            self.bytes[raw_id] = raw_bytes
        canonical_id = self.add_json(canonical) if canonical is not None else None
        rendered_id = None
        if rendered is not None:
            rendered_bytes = rendered.encode("utf-8")
            rendered_id = uuid5(
                NAMESPACE_URL,
                f"candidate-core-rendered:{hashlib.sha256(rendered_bytes).hexdigest()}",
            )
            self.bytes[rendered_id] = rendered_bytes
        return raw_id, canonical_id, rendered_id


class _MemoryArtifactRepository:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.artifacts = list(artifacts)
        self.events: list[tuple[Any, ...]] = []

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.artifacts if item.production_run_id == run_id]

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        current = [
            item
            for item in self.artifacts
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is ProductionArtifactStatus.VERIFIED
        ]
        return max(current, key=lambda item: item.version, default=None)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.events.append(("append", artifact.id))
        self.artifacts.append(artifact)

    async def mark_stages_stale(self, run_id: UUID, stages: set[str]) -> list[str]:
        self.events.append(("mark_stages_stale", run_id, tuple(sorted(stages))))
        self.artifacts = [
            replace(item, status=ProductionArtifactStatus.STALE)
            if item.production_run_id == run_id
            and item.stage.value in stages
            and item.status is ProductionArtifactStatus.VERIFIED
            else item
            for item in self.artifacts
        ]
        return sorted(stages)


class _MemoryUnitOfWork:
    def __init__(self, artifacts: _MemoryArtifactRepository) -> None:
        self.production_artifacts = artifacts


def _normalized_rows(repository: _MemoryArtifactRepository) -> list[ProductionArtifact]:
    fixed_time = repository.artifacts[0].created_at
    return [replace(artifact, created_at=fixed_time) for artifact in repository.artifacts]


@pytest.mark.asyncio
async def test_persist_candidate_writes_same_rows_as_legacy() -> None:
    fixture = _fixture()
    candidate = _build_candidate(fixture)
    previous_publication = ProductionArtifact(
        production_run_id=fixture.run_id,
        subject_id=fixture.snapshot.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="b" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={"input_artifacts": {"synthesis_artifact_id": str(fixture.base_artifact.id)}},
    )
    legacy_store = _StableMemoryBlobStore()
    candidate_store = _StableMemoryBlobStore()
    base_blob_id = legacy_store.add_json(fixture.payload)
    candidate_base_blob_id = candidate_store.add_json(fixture.payload)
    assert candidate_base_blob_id == base_blob_id
    base_artifact = replace(fixture.base_artifact, canonical_blob_id=base_blob_id)
    initial_rows = (base_artifact, previous_publication)
    legacy_repository = _MemoryArtifactRepository(initial_rows)
    candidate_repository = _MemoryArtifactRepository(initial_rows)
    legacy_uow = _MemoryUnitOfWork(legacy_repository)
    candidate_uow = _MemoryUnitOfWork(candidate_repository)
    legacy_writer = SynthesisService(lambda: legacy_uow, legacy_store)  # type: ignore[arg-type]
    candidate_writer = SynthesisService(lambda: candidate_uow, candidate_store)  # type: ignore[arg-type]
    model_run_id = uuid5(NAMESPACE_URL, "candidate-core-test:model-run")
    result_artifact_id = uuid5(NAMESPACE_URL, "candidate-core-test:result-artifact")
    metadata_extra = {
        "revision": {
            "base_artifact_id": str(base_artifact.id),
            "before_hash": candidate.before_hash,
            "after_hash": candidate.canonical_sha256,
            "rationale": "Synthetic characterization patch.",
        },
        "paragraph_provenance": {"synthetic": "preserved"},
        "revision_contract_version": "candidate-core-test-v1",
    }
    persistence = candidate_core.SynthesisCandidatePersistence(
        run_id=fixture.run_id,
        subject_id=fixture.snapshot.subject_id,
        input_hash=base_artifact.input_hash,
        extraction=fixture.extraction,
        snapshot=fixture.snapshot,
        raw_result="synthetic validated model output",
        model_run_id=model_run_id,
        publication_kind=PublicationKind.BRIEF,
        mode=SynthesisMode.FRESH,
        metadata_extra=metadata_extra,
        artifact_id=result_artifact_id,
        expected_current_artifact_id=base_artifact.id,
        expected_current_input_hash=base_artifact.input_hash,
        expected_current_canonical_sha256=fixture.base_sha256,
        stale_stages=frozenset({ProductionArtifactStage.PUBLICATION.value}),
    )

    legacy_result = await legacy_writer.store_synthesis_result(
        run_id=persistence.run_id,
        subject_id=persistence.subject_id,
        input_hash=persistence.input_hash,
        synthesis=candidate.synthesis,
        extraction=persistence.extraction,
        raw_result=persistence.raw_result,
        model_run_id=persistence.model_run_id,
        snapshot=persistence.snapshot,
        publication_kind=persistence.publication_kind,
        mode=persistence.mode,
        model_policy_version=persistence.model_policy_version,
        routing_policy_version=persistence.routing_policy_version,
        projection_hash=persistence.projection_hash,
        metadata_extra=persistence.metadata_extra,
        artifact_id=persistence.artifact_id,
        expected_current_artifact_id=persistence.expected_current_artifact_id,
        expected_current_input_hash=persistence.expected_current_input_hash,
        expected_current_canonical_sha256=persistence.expected_current_canonical_sha256,
        expected_routing_decision_id=persistence.expected_routing_decision_id,
        expected_routing_decision_hash=persistence.expected_routing_decision_hash,
        stale_stages=set(persistence.stale_stages or ()),
        uow=legacy_uow,
    )
    candidate_result = await candidate_core.persist_candidate(
        candidate_uow,  # type: ignore[arg-type]
        writer=candidate_writer,
        candidate=candidate,
        persistence=persistence,
    )

    assert candidate_result == replace(legacy_result, created_at=candidate_result.created_at)
    assert _normalized_rows(candidate_repository) == _normalized_rows(legacy_repository)
    assert candidate_repository.events == legacy_repository.events
    assert candidate_store.bytes[candidate_result.canonical_blob_id] == candidate.canonical_bytes
    assert legacy_store.bytes[legacy_result.canonical_blob_id] == candidate.canonical_bytes


@pytest.mark.asyncio
async def test_persist_candidate_uses_verified_blobs_without_blob_io_in_uow() -> None:
    fixture = _fixture()
    candidate = _build_candidate(fixture)
    blob_store = _StableMemoryBlobStore()
    base_blob_id = blob_store.add_json(fixture.payload)
    base_bytes = await blob_store.read_bytes(base_blob_id)
    assert hashlib.sha256(base_bytes).hexdigest() == fixture.base_sha256
    candidate_blob_id = blob_store.upload_json_bytes(candidate.canonical_bytes)
    assert hashlib.sha256(await blob_store.read_bytes(candidate_blob_id)).hexdigest() == (
        candidate.canonical_sha256
    )
    verified_read_count = blob_store.read_bytes_calls
    verified_write_count = blob_store.write_calls

    base_artifact = replace(fixture.base_artifact, canonical_blob_id=base_blob_id)
    repository = _MemoryArtifactRepository((base_artifact,))
    uow = _MemoryUnitOfWork(repository)
    writer = SynthesisService(lambda: uow, None)  # type: ignore[arg-type]
    persistence = candidate_core.SynthesisCandidatePersistence(
        run_id=fixture.run_id,
        subject_id=fixture.snapshot.subject_id,
        input_hash=base_artifact.input_hash,
        extraction=fixture.extraction,
        snapshot=fixture.snapshot,
        raw_result=None,
        model_run_id=None,
        publication_kind=PublicationKind.BRIEF,
        artifact_id=uuid5(NAMESPACE_URL, "candidate-core-test:adopted-artifact"),
        expected_current_artifact_id=base_artifact.id,
        expected_current_input_hash=base_artifact.input_hash,
        expected_current_canonical_sha256=fixture.base_sha256,
        stale_stages=frozenset({ProductionArtifactStage.PUBLICATION.value}),
        verified_candidate_blob=candidate_core.VerifiedSynthesisCandidateBlob(
            blob_id=candidate_blob_id,
            canonical_sha256=candidate.canonical_sha256,
        ),
        base_canonical_sha256_verified=True,
    )

    artifact = await candidate_core.persist_candidate(
        uow,  # type: ignore[arg-type]
        writer=writer,
        candidate=candidate,
        persistence=persistence,
    )

    assert artifact.canonical_blob_id == candidate_blob_id
    assert artifact.raw_blob_id is None
    assert artifact.rendered_blob_id is None
    assert blob_store.read_bytes_calls == verified_read_count
    assert blob_store.write_calls == verified_write_count
