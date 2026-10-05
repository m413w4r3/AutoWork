from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.model_gateway import (
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    build_editorial_enrichment_model_request,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_prompts import (
    RELEVANCE_CLASSIFIER_CONTRACT_VERSION,
    RELEVANCE_CLASSIFIER_PROMPT_VERSION,
    RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION,
)
from cti_app.application.production_relevance import (
    ProductionRelevanceProjectionService,
    RelevanceProjectionExecutionStatus,
    build_relevance_projection,
    production_evidence_gate_details,
    relevance_projection_input_hash,
    subject_relevance_evidence_counts,
)
from cti_app.application.production_relevance_model import (
    ModelRelevanceClassifier,
    RelevanceProposalStatus,
    build_relevance_classifier_model_request,
    build_relevance_model_evidence_pack,
    parse_relevance_classifier_wire,
)
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    SynthesisAccessSourceV1,
    build_synthesis_evidence_pack,
    build_synthesis_model_request,
    canonical_extraction_hash,
    extraction_evidence_elements,
    synthesis_evidence_pack_hash,
)
from cti_app.application.publication_builder import (
    _project_publication_sources,
    _project_synthesis_publication,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelUsage
from cti_app.domain.production import (
    PRODUCTION_INSUFFICIENT_SUBJECT_EVIDENCE_CODE,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import (
    DEFAULT_RELEVANCE_CLASSIFIER_VERSION,
    RELEVANCE_PROJECTION_POLICY_VERSION,
    RelevanceClassification,
    RelevanceDecisionProvenance,
    RelevanceProposalRejectionReason,
    RelevanceSourcePairRelation,
    relevance_projection_from_json,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import ArtifactType
from tests.test_production_relevance_domain import (
    _event,
    _extraction,
    _fact,
    _indicator,
    _snapshot,
    _source,
)


def _world() -> tuple[ProductionInputSnapshot, ProductionExtractionV1, tuple[UUID, UUID]]:
    snapshot = _snapshot()
    primary_id, counter_id = uuid4(), uuid4()
    primary = _source(
        primary_id,
        facts=(
            _fact(primary_id, "MOIS Bitcoin operation", context="A MOIS-linked Bitcoin operation."),
            _fact(primary_id, "Campaign period", context="The operation ran in 2025."),
        ),
        indicators=(
            _indicator(
                primary_id,
                "198.51.100.27",
                ArtifactType.IP,
                context="MOIS malware command-and-control server.",
            ),
            _indicator(
                primary_id,
                "config.json",
                ArtifactType.FILENAME,
                context="A MOIS malware report mentions config.json.",
            ),
        ),
    )
    counter = _source(
        counter_id,
        editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
        facts=(
            _fact(
                counter_id,
                "Bitcoin observations are unattributed",
                context="The vendor says it cannot attribute this activity to an actor.",
            ),
        ),
    )
    return snapshot, _extraction(snapshot, (primary, counter)), (primary_id, counter_id)


def _access_policy(snapshot: ProductionInputSnapshot, source_ids: tuple[UUID, ...]):
    sources = tuple(
        SynthesisAccessSourceV1(source_id, TLP.CLEAR, True, False) for source_id in source_ids
    )
    return SynthesisAccessPolicyV1(
        subject_tlp=snapshot.subject_tlp,
        effective_tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
        sources=sources,
    )


def _wire_classification(
    handle: str,
    classification: str,
    reason: str,
    *,
    supporting: str = "",
    block_id: str = "C001",
) -> str:
    rows = [
        f"@@ CLASSIFICATION {block_id} @@",
        f"handle: {handle}",
        f"classification: {classification}",
        f"reason_code: {reason}",
    ]
    if supporting:
        rows.append(f"supporting_handles: {supporting}")
    rows.append("END CLASSIFICATION")
    return "\n".join(rows)


def _wire_relation(handles: str, *, relation: str = "CONTRADICTION") -> str:
    return "\n".join(
        (
            "@@ RELATION R001 @@",
            f"relation: {relation}",
            "reason: One report attributes the activity; the other states it is unattributed.",
            f"supporting_handles: {handles}",
            "END RELATION",
        )
    )


def _ref_for(extraction, kind: EvidenceKind, value: str):
    return next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is kind and value in {str(payload.get("value")), str(payload.get("text"))}
    )


class _FakeGateway:
    def __init__(self, responses: list[str] | Exception) -> None:
        self.responses = responses if isinstance(responses, Exception) else list(responses)
        self.calls: list[ModelRequest] = []
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}
        self.diagnostics: list[dict[str, object]] = []
        self._normalized_counter = 0

    async def draft(self, request: ModelRequest, output_schema: object | None = None):
        del output_schema
        self.calls.append(request)
        if isinstance(self.responses, Exception):
            raise self.responses
        text = self.responses.pop(0)
        run = ModelRun(
            provider=ModelProvider.FAKE,
            model_role=ModelRole.DRAFTING,
            requested_model="fake-relevance-model",
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash=hashlib.sha256(request.text.encode()).hexdigest(),
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=dict(request.parameters),
            id=request.run_id or uuid4(),
        )
        raw = text.encode()
        reference = f"model-output://{run.id}"
        run.raw_output_reference = reference
        run.raw_output_sha256 = hashlib.sha256(raw).hexdigest()
        run.raw_output_chars = len(text)
        run.succeed(
            actual_model_version="fake-v1",
            duration_ms=1,
            usage=ModelUsage(total_tokens=1),
            output_references=(reference,),
            response_id=None,
        )
        self.outputs[reference] = raw
        self.runs[run.id] = run
        return ModelExecution(run=run, output_text=text)

    async def get_run(self, run_id: UUID):
        return self.runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000):
        raw = self.outputs[reference]
        if len(raw) > max_bytes:
            raise ValueError("too large")
        return raw

    async def archive_output(self, content: bytes, *, mime_type: str):
        del mime_type
        self._normalized_counter += 1
        reference = f"model-normalized://{self._normalized_counter}"
        self.outputs[reference] = content
        return reference

    async def record_output_diagnostics(self, run_id: UUID, **values: object) -> None:
        self.diagnostics.append({"run_id": run_id, **values})
        run = self.runs[run_id]
        run.normalized_output_reference = values["normalized_reference"]  # type: ignore[assignment]
        run.normalized_output_sha256 = values["normalized_sha256"]  # type: ignore[assignment]
        run.parser_stage = values["parser_stage"]  # type: ignore[assignment]
        run.normalization_version = values["normalization_version"]  # type: ignore[assignment]
        run.transformations = values["transformations"]  # type: ignore[assignment]
        run.validation_errors = values["validation_errors"]  # type: ignore[assignment]


class _MemoryArtifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []
        self.stale: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str):
        matches = [
            artifact
            for artifact in self.items
            if artifact.production_run_id == run_id
            and artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda artifact: artifact.version, default=None)

    async def list_for_run(self, run_id: UUID):
        return [artifact for artifact in self.items if artifact.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale.append((run_id, stage))


class _MemoryStore:
    def __init__(self, payloads: dict[UUID, bytes] | None = None) -> None:
        self.payloads = payloads or {}

    async def read_json(self, blob_id: UUID):
        return json.loads(self.payloads[blob_id])

    async def read_bytes(self, blob_id: UUID):
        return self.payloads[blob_id]

    async def store_stage_payloads(self, *, canonical: dict[str, object]):
        blob_id = uuid4()
        self.payloads[blob_id] = ProductionArtifactStore.canonical_json_bytes(canonical)
        return None, blob_id, None


class _MemoryUow:
    def __init__(
        self,
        documents: dict[UUID, object],
        artifacts: _MemoryArtifacts,
        collections: tuple[SourceCollection, ...] = (),
    ) -> None:
        self.source_documents = _MemorySourceDocuments(documents)
        self.source_collections = _MemorySourceCollections(collections)
        self.production_artifacts = artifacts
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        self.commits += 1


class _MemorySourceDocuments:
    def __init__(self, documents: dict[UUID, object]) -> None:
        self.documents = documents

    async def get(self, source_id: UUID):
        return self.documents.get(source_id)


class _MemorySourceCollections:
    def __init__(self, collections: tuple[SourceCollection, ...]) -> None:
        self.collections = {item.id: item for item in collections}

    async def get(self, collection_id: UUID):
        return self.collections.get(collection_id)


class _MemoryUowFactory:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    def __call__(self):
        return self.uow


def _service_world(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    source_ids: tuple[UUID, ...],
    gateway: _FakeGateway,
    *,
    collections: tuple[SourceCollection, ...] = (),
) -> Any:
    documents = {
        source_id: SimpleNamespace(
            id=source_id,
            subject_id=snapshot.subject_id,
            tlp=TLP.CLEAR,
            external_llm_allowed=True,
            do_not_submit=False,
            source_collection_id=None,
        )
        for source_id in source_ids
    }
    artifacts = _MemoryArtifacts()
    uow = _MemoryUow(documents, artifacts, collections)
    extraction_blob_id = uuid4()
    store = _MemoryStore(
        {
            extraction_blob_id: ProductionArtifactStore.canonical_json_bytes(
                production_extraction_to_json(extraction)
            )
        }
    )
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="a" * 64,
        canonical_blob_id=extraction_blob_id,
    )
    service = ProductionRelevanceProjectionService(
        _MemoryUowFactory(uow),
        store,  # type: ignore[arg-type]
        model_gateway=gateway,
    )
    return SimpleNamespace(
        service=service,
        run=run,
        extraction_artifact=extraction_artifact,
        store=store,
        artifacts=artifacts,
        uow=uow,
    )


@pytest.mark.asyncio
async def test_model_proposals_merge_with_deterministic_fallback_and_guards() -> None:
    snapshot, extraction, source_ids = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    by_value = {
        payload.get("value"): ref for ref, payload in extraction_evidence_elements(extraction)
    }
    handles = {ref: handle for handle, ref in pack._handle_to_ref.items()}
    primary = by_value["MOIS Bitcoin operation"]
    omitted = by_value["Campaign period"]
    ioc = by_value["198.51.100.27"]
    generic = by_value["config.json"]
    counter = next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if payload.get("value") == "Bitcoin observations are unattributed"
    )
    raw = "\n".join(
        (
            _wire_classification(
                handles[primary], "CONTEXT", "context_source_without_relation", block_id="C001"
            ),
            _wire_classification(
                handles[ioc],
                "DIRECT",
                "malicious_subject_relation",
                supporting=handles[primary],
                block_id="C002",
            ),
            _wire_classification(
                handles[generic],
                "DIRECT",
                "malicious_subject_relation",
                supporting=handles[primary],
                block_id="C003",
            ),
            _wire_classification(
                handles[counter],
                "COUNTER_INDICATION",
                "explicit_counter_analysis",
                block_id="C004",
            ),
            _wire_relation(f"{handles[primary]}, {handles[counter]}"),
            _wire_classification("E999", "DIRECT", "malicious_subject_relation", block_id="C006"),
        )
    )
    gateway = _FakeGateway([raw])
    world = _service_world(snapshot, extraction, source_ids, gateway)

    result = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert result.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert result.model_calls == 1
    decisions = {item.evidence_ref: item for item in result.projection.classifications}
    assert decisions[primary].classification is RelevanceClassification.CONTEXT
    assert decisions[primary].provenance is RelevanceDecisionProvenance.MODEL_PROPOSAL
    assert decisions[omitted].classification is RelevanceClassification.DIRECT
    assert decisions[omitted].provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
    assert decisions[ioc].classification is RelevanceClassification.DIRECT
    assert decisions[ioc].provenance is RelevanceDecisionProvenance.MODEL_PROPOSAL
    assert decisions[generic].classification is RelevanceClassification.CONTEXT
    assert decisions[generic].reason_code.value == "generic_filename"
    assert len(result.projection.source_pair_relations) == 1
    assert (
        result.projection.source_pair_relations[0].relation
        is RelevanceSourcePairRelation.CONTRADICTION
    )
    rejection_codes = {item.reason_code for item in result.projection.model_proposal_rejections}
    assert RelevanceProposalRejectionReason.GENERIC_FILENAME_GUARD in rejection_codes
    assert RelevanceProposalRejectionReason.UNKNOWN_HANDLE in rejection_codes
    assert gateway.calls[0].web_search is False
    assert all(str(source_id) not in gateway.calls[0].text for source_id in source_ids)
    assert result.artifact is not None
    stored = await world.store.read_json(result.artifact.canonical_blob_id)
    restored = relevance_projection_from_json(stored)
    assert restored.source_pair_relations == result.projection.source_pair_relations
    assert world.artifacts.items[0].metadata["model_calls"] == 1


def test_explicit_none_marker_is_a_valid_empty_proposal() -> None:
    snapshot, extraction, _ = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)

    parsed = parse_relevance_classifier_wire("```\n@@NONE@@\n```", pack, extraction)

    assert parsed.error_code is None
    assert parsed.explicit_none is True
    assert parsed.classifications == ()
    assert parsed.source_pair_relations == ()
    assert parsed.rejections == ()
    unintelligible = parse_relevance_classifier_wire("I cannot help with that.", pack, extraction)
    assert unintelligible.error_code == "relevance_classifier_unintelligible_response"


def test_model_direct_core_ioc_without_documented_relation_keeps_baseline() -> None:
    snapshot, extraction, _ = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    ioc_ref = _ref_for(extraction, EvidenceKind.INDICATOR, "198.51.100.27")
    handle = pack._handle_for_ref[ioc_ref]
    parsed = parse_relevance_classifier_wire(
        _wire_classification(handle, "DIRECT", "malicious_subject_relation"),
        pack,
        extraction,
    )
    baseline = build_relevance_projection(snapshot, extraction)
    from cti_app.application.production_relevance_model import ModelRelevanceProposalExecution

    merged = ProductionRelevanceProjectionService._merge_model_proposals(
        baseline,
        extraction,
        ModelRelevanceProposalExecution(
            status=RelevanceProposalStatus.SUCCEEDED,
            classifications=parsed.classifications,
            rejections=tuple(item.as_domain_rejection() for item in parsed.rejections),
        ),
    )

    decision = merged.classification_for(ioc_ref)
    assert decision.classification is RelevanceClassification.DIRECT
    assert decision.provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
    assert any(
        item.reason_code is RelevanceProposalRejectionReason.RELATION_NOT_DOCUMENTED
        for item in merged.model_proposal_rejections
    )


def test_model_cannot_promote_supporting_ioc_from_unrelated_core_fact() -> None:
    snapshot, extraction, _ = _world()
    support_id = uuid4()
    supporting = _source(
        support_id,
        tier=ProductionReferenceTier.SUPPORTING,
        editorial_role=ProductionEditorialRole.CONTEXT,
        indicators=(
            _indicator(
                support_id,
                "203.0.113.90",
                ArtifactType.IP,
                context="A supporting IOC with no relation to the primary report.",
            ),
        ),
    )
    extraction = _extraction(snapshot, (*extraction.sources, supporting))
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    ioc_ref = _ref_for(extraction, EvidenceKind.INDICATOR, "203.0.113.90")
    unrelated_core_fact = _ref_for(extraction, EvidenceKind.FACT, "MOIS Bitcoin operation")
    parsed = parse_relevance_classifier_wire(
        _wire_classification(
            pack._handle_for_ref[ioc_ref],
            "DIRECT",
            "malicious_subject_relation",
            supporting=pack._handle_for_ref[unrelated_core_fact],
        ),
        pack,
        extraction,
    )
    baseline = build_relevance_projection(snapshot, extraction)
    from cti_app.application.production_relevance_model import ModelRelevanceProposalExecution

    merged = ProductionRelevanceProjectionService._merge_model_proposals(
        baseline,
        extraction,
        ModelRelevanceProposalExecution(
            status=RelevanceProposalStatus.SUCCEEDED,
            classifications=parsed.classifications,
            rejections=tuple(item.as_domain_rejection() for item in parsed.rejections),
        ),
    )

    assert (
        merged.classification_for(ioc_ref).classification is RelevanceClassification.INDETERMINATE
    )
    assert any(
        item.reason_code is RelevanceProposalRejectionReason.RELATION_NOT_DOCUMENTED
        for item in merged.model_proposal_rejections
    )


def test_wire_parser_keeps_valid_blocks_around_citations_fences_and_malformed_block() -> None:
    snapshot, extraction, _ = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    ref = _ref_for(extraction, EvidenceKind.FACT, "MOIS Bitcoin operation")
    handle = pack._handle_for_ref[ref]
    raw = "\n".join(
        (
            "```text",
            "citeturn0search0",
            "@@ CLASSIFICATION BAD @@",
            f"handle: {handle}",
            "classification: CONTEXT",
            "END CLASSIFICATION",
            _wire_classification(
                handle,
                "CONTEXT",
                "context_source_without_relation",
                block_id="GOOD",
            ),
            "```",
        )
    )

    parsed = parse_relevance_classifier_wire(raw, pack, extraction)

    assert len(parsed.classifications) == 1
    assert parsed.classifications[0].evidence_ref == ref
    assert parsed.rejections[0].reason_code is RelevanceProposalRejectionReason.MALFORMED_BLOCK
    assert parsed.transformations


@pytest.mark.asyncio
async def test_model_classifier_replays_parser_changes_and_invokes_on_prompt_changes() -> None:
    snapshot, extraction, source_ids = _world()
    access = _access_policy(snapshot, source_ids)
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    target = _ref_for(extraction, EvidenceKind.FACT, "MOIS Bitcoin operation")
    text = _wire_classification(
        pack._handle_for_ref[target], "CONTEXT", "context_source_without_relation"
    )
    gateway = _FakeGateway([text, text])
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )

    first = await ModelRelevanceClassifier(gateway).propose(run, snapshot, extraction, access)
    parser_bump = await ModelRelevanceClassifier(
        gateway, parser_version="subject-relevance-wire-v4-test"
    ).propose(run, snapshot, extraction, access)
    prompt_bump = await ModelRelevanceClassifier(
        gateway, prompt_version="subject-relevance-classifier-v4-test"
    ).propose(run, snapshot, extraction, access)

    assert first.status is RelevanceProposalStatus.SUCCEEDED
    assert first.model_calls == 1
    assert parser_bump.status is RelevanceProposalStatus.SUCCEEDED
    assert parser_bump.model_calls == 0
    assert parser_bump.invocation_hash == first.invocation_hash
    assert parser_bump.parse_identity != first.parse_identity
    assert prompt_bump.model_calls == 1
    assert prompt_bump.invocation_hash != first.invocation_hash
    assert len(gateway.calls) == 2
    assert gateway.diagnostics[0]["parser_stage"] == "relevance_classifier"


@pytest.mark.asyncio
async def test_unintelligible_and_ambiguous_model_results_require_review_without_fallback() -> None:
    snapshot, extraction, source_ids = _world()
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    access = _access_policy(snapshot, source_ids)
    unintelligible_gateway = _FakeGateway(["I do not understand the evidence."])
    unintelligible = await ModelRelevanceClassifier(unintelligible_gateway).propose(
        run, snapshot, extraction, access
    )
    assert unintelligible.status is RelevanceProposalStatus.NEEDS_REVIEW
    assert unintelligible.model_calls == 1
    assert not unintelligible.classifications

    ambiguous_gateway = _FakeGateway(
        ModelSubmissionReconciliationRequiredError(details={"phase": "submit"})
    )
    world = _service_world(snapshot, extraction, source_ids, ambiguous_gateway)
    result = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert result.status is RelevanceProjectionExecutionStatus.NEEDS_REVIEW
    assert result.model_calls == 1
    assert result.error_code == "model_submission_reconciliation_required"
    assert result.artifact is None
    assert world.artifacts.items == []
    assert len(ambiguous_gateway.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_calls"),
    [
        ("unintelligible", "relevance_classifier_unintelligible_response", 1),
        ("no_valid_blocks", "relevance_classifier_no_valid_blocks", 1),
        ("policy", "relevance_classifier_policy_blocked", 0),
        ("do_not_submit", "relevance_classifier_policy_blocked", 0),
        ("auth", "relevance_classifier_model_call_failed", 1),
        ("access_policy", "relevance_classifier_access_policy_unavailable", 0),
    ],
)
async def test_nonambiguous_classifier_failures_persist_deterministic_fallback(
    failure: str, expected_code: str, expected_calls: int
) -> None:
    snapshot, extraction, source_ids = _world()
    responses: list[str] | Exception
    if failure == "unintelligible":
        responses = ["I cannot classify this evidence."]
    elif failure == "no_valid_blocks":
        responses = ["@@ CLASSIFICATION BAD @@\nclassification: MYSTERY\nEND CLASSIFICATION"]
    elif failure == "auth":
        responses = ModelGatewayError("provider authentication failed")
    else:
        responses = ["unused"]
    gateway = _FakeGateway(responses)
    world = _service_world(snapshot, extraction, source_ids, gateway)
    if failure == "policy":
        world.uow.source_documents.documents[source_ids[0]].external_llm_allowed = False
    elif failure == "do_not_submit":
        world.uow.source_documents.documents[source_ids[0]].do_not_submit = True
    elif failure == "access_policy":
        world.uow.source_documents.documents.pop(source_ids[0])

    result = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert result.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert result.projection.classifier_version == DEFAULT_RELEVANCE_CLASSIFIER_VERSION
    assert result.model_calls == expected_calls
    assert len(gateway.calls) == expected_calls
    assert result.details is not None
    assert result.details["model_classifier_fallback"]["error_code"] == expected_code
    assert world.artifacts.items[0].metadata["model_classifier_fallback"]["error_code"] == (
        expected_code
    )


@pytest.mark.asyncio
async def test_collection_policy_blocks_classifier_before_gateway_call() -> None:
    snapshot, extraction, source_ids = _world()
    collection = SourceCollection(
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        requested_url="https://core.example/report",
        proposed_role=SourceRole.PRIMARY,
        state=CollectionState.ARCHIVED,
        source_tlp=TLP.AMBER_STRICT,
        external_llm_allowed=False,
    )
    gateway = _FakeGateway(["unused"])
    world = _service_world(snapshot, extraction, source_ids, gateway, collections=(collection,))
    world.uow.source_documents.documents[source_ids[0]].source_collection_id = collection.id

    result = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert result.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert result.details is not None
    assert result.details["model_classifier_fallback"]["error_code"] == (
        "relevance_classifier_policy_blocked"
    )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_fallback_on_existing_baseline_still_succeeds_and_records_metadata() -> None:
    snapshot, extraction, source_ids = _world()
    gateway = _FakeGateway(["unused"])
    world = _service_world(snapshot, extraction, source_ids, gateway)
    world.service = ProductionRelevanceProjectionService(
        _MemoryUowFactory(world.uow),
        world.store,  # type: ignore[arg-type]
        model_gateway=gateway,
        model_enabled=False,
    )
    baseline = await world.service.execute(world.run, snapshot, world.extraction_artifact)
    world.uow.source_documents.documents[source_ids[0]].external_llm_allowed = False
    world.service = ProductionRelevanceProjectionService(
        _MemoryUowFactory(world.uow),
        world.store,  # type: ignore[arg-type]
        model_gateway=gateway,
    )

    fallback = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert baseline.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert fallback.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert fallback.artifact is not None
    assert fallback.artifact.metadata["model_classifier_fallback"]["error_code"] == (
        "relevance_classifier_policy_blocked"
    )
    assert fallback.details["model_classifier_fallback"]["error_code"] == (
        "relevance_classifier_policy_blocked"
    )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_classifier_retries_after_fallback_with_model_classifier_identity() -> None:
    snapshot, extraction, source_ids = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    target = _ref_for(extraction, EvidenceKind.FACT, "MOIS Bitcoin operation")
    gateway = _FakeGateway(
        [
            "I cannot classify this evidence.",
            _wire_classification(
                pack._handle_for_ref[target], "CONTEXT", "context_source_without_relation"
            ),
        ]
    )
    world = _service_world(snapshot, extraction, source_ids, gateway)

    first = await world.service.execute(world.run, snapshot, world.extraction_artifact)
    next_run = replace(world.run, id=uuid4())
    next_extraction_artifact = replace(world.extraction_artifact, production_run_id=next_run.id)
    second = await world.service.execute(next_run, snapshot, next_extraction_artifact)

    assert first.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert second.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert first.projection.classifier_version == DEFAULT_RELEVANCE_CLASSIFIER_VERSION
    assert second.projection.classifier_version.startswith(
        "model-subject-scope-v3-counter-analysis:"
    )
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_auth_failure_requires_review_and_disabled_policy_uses_deterministic_default() -> (
    None
):
    snapshot, extraction, source_ids = _world()
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    access = _access_policy(snapshot, source_ids)
    failed = await ModelRelevanceClassifier(
        _FakeGateway(ModelGatewayError("provider authentication failed"))
    ).propose(run, snapshot, extraction, access)
    assert failed.status is RelevanceProposalStatus.NEEDS_REVIEW
    assert failed.error_code == "relevance_classifier_model_call_failed"
    assert failed.model_calls == 1

    gateway = _FakeGateway(["unused"])
    disabled = _service_world(snapshot, extraction, source_ids, gateway)
    disabled.service = ProductionRelevanceProjectionService(
        _MemoryUowFactory(disabled.uow),
        disabled.store,  # type: ignore[arg-type]
        model_gateway=gateway,
        model_enabled=False,
    )
    result = await disabled.service.execute(disabled.run, snapshot, disabled.extraction_artifact)
    assert result.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert result.model_calls == 0
    assert gateway.calls == []
    assert all(
        item.provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
        for item in result.projection.classifications
    )


def test_reserve_and_contradiction_context_is_handle_addressed_and_leak_guard_clean() -> None:
    snapshot, extraction, source_ids = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    by_ref = {ref: handle for handle, ref in pack._handle_to_ref.items()}
    first = _ref_for(extraction, EvidenceKind.FACT, "MOIS Bitcoin operation")
    second = _ref_for(extraction, EvidenceKind.FACT, "Bitcoin observations are unattributed")
    output = "\n".join(
        (
            _wire_classification(
                by_ref[second],
                "COUNTER_INDICATION",
                "explicit_counter_analysis",
                block_id="C002",
            ),
            _wire_relation(f"{by_ref[first]}, {by_ref[second]}"),
        )
    )
    parsed = parse_relevance_classifier_wire(output, pack, extraction)
    assert parsed.source_pair_relations, parsed.rejections
    baseline = build_relevance_projection(snapshot, extraction)
    from cti_app.application.production_relevance_model import ModelRelevanceProposalExecution

    projection = ProductionRelevanceProjectionService._merge_model_proposals(
        baseline,
        extraction,
        ModelRelevanceProposalExecution(
            status=RelevanceProposalStatus.SUCCEEDED,
            classifications=parsed.classifications,
            source_pair_relations=parsed.source_pair_relations,
        ),
    )
    synthesis_pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
    policy = _access_policy(snapshot, source_ids)
    synthesis_request = build_synthesis_model_request(
        ProductionRun(
            id=snapshot.production_run_id,
            subject_id=snapshot.subject_id,
            edition_id=snapshot.edition_id,
        ),
        snapshot,
        extraction,
        synthesis_pack,
        policy,
        SynthesisMode.FRESH,
    )
    synthesis = ProductionSynthesisV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    enrichment_pack = build_editorial_enrichment_evidence_pack(
        snapshot, extraction, synthesis, projection
    )
    enrichment_request = build_editorial_enrichment_model_request(
        ProductionRun(
            id=snapshot.production_run_id,
            subject_id=snapshot.subject_id,
            edition_id=snapshot.edition_id,
        ),
        snapshot,
        extraction,
        synthesis,
        enrichment_pack,
        policy,
    )

    assert synthesis_pack.reserve_evidence
    assert synthesis_pack.source_pair_relations[0]["relation"] == "contradiction"
    assert "NON-AUTHORITATIVE CONTEXT" in synthesis_request.text
    reserve_handles = tuple(str(item["handle"]) for item in synthesis_pack.reserve_evidence)
    assert reserve_handles
    assert all(f"@@EVIDENCE {handle}@@" in synthesis_request.text for handle in reserve_handles)
    assert "citeable only for" in synthesis_request.text
    assert "Every source cited through an R handle must appear in the publication sources." in (
        synthesis_request.text
    )
    assert all(str(source_id) not in synthesis_request.text for source_id in source_ids)
    assert "reserves_and_contradictions_non_authoritative" in enrichment_request.text
    assert all(str(source_id) not in enrichment_request.text for source_id in source_ids)
    assert enrichment_pack.reserve_evidence
    assert synthesis_evidence_pack_hash(synthesis_pack) != ""
    assert editorial_enrichment_evidence_pack_hash(enrichment_pack) != ""
    assert all(
        synthesis_pack.resolve_handle(handle) in set(extraction_evidence_refs_v1(extraction))
        for handle in reserve_handles
    )


def test_model_relevance_context_and_projection_are_subject_specific_for_shared_capture() -> None:
    first_snapshot, first_extraction, _ = _world()
    second_snapshot = replace(
        first_snapshot,
        subject_id=uuid4(),
        production_run_id=uuid4(),
        subject_title="Different malware campaign",
        actor_or_campaign="Different actor",
        reuse_basis_hash="",
        input_hash="",
    )
    second_extraction = _extraction(second_snapshot, first_extraction.sources)

    first_pack = build_relevance_model_evidence_pack(first_snapshot, first_extraction)
    second_pack = build_relevance_model_evidence_pack(second_snapshot, second_extraction)
    first_projection = build_relevance_projection(first_snapshot, first_extraction)
    second_projection = build_relevance_projection(second_snapshot, second_extraction)

    assert first_pack.evidence_items == second_pack.evidence_items
    assert first_pack.evidence_pack_hash != second_pack.evidence_pack_hash
    assert first_projection.input_hash != second_projection.input_hash
    assert first_projection.projection_hash != second_projection.projection_hash


def _real_run_fixture_extraction(snapshot: ProductionInputSnapshot):
    """Synthetic evidence layout for replaying the saved real-run wire blocks.

    The fixture retains its actual C001-C068 text, but the corresponding E001-E068
    extraction items are synthetic: 3 primary events, one OP_RETURN claim fact,
    54 primary indicators, and 10 Bitquery-style reserve uncertainties. This
    supplies a deterministic handle map without implying the original extraction
    was saved in the repository. Two extra non-primary context facts occupy
    E069-E070 solely to exercise non-upgrading reason repairs.
    """
    primary_id, counter_id = UUID(int=1), UUID(int=2)
    claim = _fact(
        primary_id,
        "Iranian MOIS Bitcoin OP_RETURN attribution",
        context="Chainalysis links this OP_RETURN activity to the Iranian operator.",
    )
    primary = replace(
        _source(
            primary_id,
            facts=(claim,),
            events=tuple(
                _event(primary_id, f"Synthetic primary chronology item {index}")
                for index in range(3)
            ),
            indicators=tuple(
                _indicator(
                    primary_id,
                    f"synthetic-primary-indicator-{index:03d}",
                    ArtifactType.FILENAME,
                    context="Synthetic primary item for fixture handle mapping.",
                )
                for index in range(54)
            ),
        ),
        canonical_url="https://chainalysis.com/report",
    )
    counter = replace(
        _source(
            counter_id,
            tier=ProductionReferenceTier.SUPPORTING,
            editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
            role=SourceRole.INDEPENDENT,
            uncertainties=(
                "Le motif Bitcoin OP_RETURN observé depuis 2020 n'est attribué à aucun acteur.",
                *(f"Synthetic Bitquery reserve limitation {index}." for index in range(9)),
            ),
        ),
        canonical_url="https://bitquery.io/report",
    )
    other_id = UUID(int=3)
    other = _source(
        other_id,
        tier=ProductionReferenceTier.SUPPORTING,
        editorial_role=ProductionEditorialRole.CONTEXT,
        role=SourceRole.RELAY,
        facts=(
            _fact(other_id, "Synthetic independent context item one"),
            _fact(other_id, "Synthetic independent context item two"),
        ),
    )
    return (
        _extraction(snapshot, (primary, counter, other)),
        (primary_id, counter_id, other_id),
        claim,
    )


@pytest.mark.asyncio
async def test_saved_fixture_repairs_reasons_and_publishes_counter_reserve() -> None:
    snapshot = _snapshot(title="Iranian operator Bitcoin OP_RETURN dead drop")
    extraction, source_ids, primary_claim = _real_run_fixture_extraction(snapshot)
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    claim_ref = _ref_for(extraction, EvidenceKind.FACT, primary_claim.value)
    counter_ref = next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.UNCERTAINTY and "OP_RETURN" in str(payload.get("text"))
    )

    fixture_path = Path(__file__).parent / "fixtures/real_run_relevance_classifier_2026-10-03.txt"
    fixture = fixture_path.read_text(encoding="utf-8")
    assert fixture.count("@@ CLASSIFICATION ") == 68
    assert "@@ RELATION " not in fixture
    wire = "\n".join(
        (
            fixture,
            _wire_classification("E999", "DIRECT", "malicious_subject_relation", block_id="C069"),
            _wire_classification("E003", "DIRECT", "generic_filename", block_id="C070"),
            _wire_classification(
                "E069", "INDETERMINATE", "malicious_subject_relation", block_id="C071"
            ),
            _wire_classification(
                "E070", "OUT_OF_SCOPE", "explicit_counter_analysis", block_id="C072"
            ),
        )
    )
    parsed = parse_relevance_classifier_wire(wire, pack, extraction)

    assert parsed.error_code is None
    assert len(parsed.classifications) == 70
    assert len(parsed.warnings) == 10
    assert (
        "relevance_reason_repaired:C015:subject_link_not_demonstrated->explicit_other_actor"
        in parsed.warnings
    )
    parsed_by_block = {item.block_id: item for item in parsed.classifications}
    assert parsed_by_block["C015"].classification is RelevanceClassification.OUT_OF_SCOPE
    assert parsed_by_block["C071"].classification is RelevanceClassification.INDETERMINATE
    assert parsed_by_block["C072"].classification is RelevanceClassification.OUT_OF_SCOPE
    assert (
        sum(
            item.reason_code is RelevanceProposalRejectionReason.UNKNOWN_HANDLE
            for item in parsed.rejections
        )
        == 1
    )
    assert (
        sum(
            item.reason_code is RelevanceProposalRejectionReason.INVALID_REASON_FOR_CLASSIFICATION
            for item in parsed.rejections
        )
        == 1
    )

    relation_wire = _wire_relation(
        f"{pack._handle_for_ref[claim_ref]}, {pack._handle_for_ref[counter_ref]}",
        relation="COUNTER_INDICATION",
    )
    mixed_profile_relation = parse_relevance_classifier_wire(relation_wire, pack, extraction)
    assert len(mixed_profile_relation.source_pair_relations) == 1
    assert (
        mixed_profile_relation.source_pair_relations[0].relation
        is RelevanceSourcePairRelation.COUNTER_INDICATION
    )

    gateway = _FakeGateway([wire])
    world = _service_world(snapshot, extraction, source_ids, gateway)
    result = await world.service.execute(world.run, snapshot, world.extraction_artifact)

    assert result.status is RelevanceProjectionExecutionStatus.SUCCEEDED
    assert result.artifact is not None
    decisions = {item.evidence_ref: item for item in result.projection.classifications}
    assert decisions[claim_ref].classification is RelevanceClassification.DIRECT
    assert decisions[counter_ref].classification is RelevanceClassification.COUNTER_INDICATION
    assert decisions[counter_ref].classification is not RelevanceClassification.INDETERMINATE
    assert set(decisions[counter_ref].supporting_evidence_refs) == {claim_ref, counter_ref}
    assert subject_relevance_evidence_counts(result.projection) == {
        "direct_count": 1,
        "context_count": 3,
        "out_of_scope_count": 1,
    }
    assert len(result.projection.source_pair_relations) == 1
    relation = result.projection.source_pair_relations[0]
    assert relation.relation is RelevanceSourcePairRelation.COUNTER_INDICATION
    assert set(relation.supporting_evidence_refs) == {claim_ref, counter_ref}

    metadata = result.artifact.metadata
    assert metadata["model_proposal_rejection_count"] >= 2
    assert "unknown_handle" in metadata["model_proposal_rejection_codes"]
    assert "invalid_reason_for_classification" in metadata["model_proposal_rejection_codes"]
    assert len(metadata["model_proposal_warnings"]) == 10
    assert metadata["source_pair_relation_count"] == 1

    synthesis_pack = build_synthesis_evidence_pack(snapshot, extraction, result.projection)
    reserve = next(
        item
        for item in synthesis_pack.reserve_evidence
        if item["source_label"] == "bitquery.io"
        and item["text"].startswith("Le motif Bitcoin OP_RETURN")
    )
    reserve_handle = str(reserve["handle"])
    assert reserve["source_label"] == "bitquery.io"
    assert reserve["text"].startswith("Le motif Bitcoin OP_RETURN")
    synthesis_request = build_synthesis_model_request(
        world.run,
        snapshot,
        extraction,
        synthesis_pack,
        _access_policy(snapshot, source_ids),
        SynthesisMode.FRESH,
    )
    assert "bitquery.io" in synthesis_request.text
    assert f"@@EVIDENCE {reserve_handle}@@" in synthesis_request.text
    assert "analytic limit" in synthesis_request.text.casefold()
    assert "publication sources" in synthesis_request.text.casefold()

    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(
            SynthesisParagraphV1(
                text=(
                    "Bitquery rappelle que ce motif OP_RETURN reste sans attribution indépendante."
                ),
                evidence_refs=(counter_ref, claim_ref),
            ),
        ),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://chainalysis.com/report",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Chainalysis report",
                publisher="Chainalysis",
                published_at=None,
                source_collection_id=None,
                source_document_id=source_ids[0],
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=(str(source_ids[0].int).zfill(64))[-64:],
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=ProductionEditorialRole.PRIMARY,
            ),
            ProductionReferenceSourceV1(
                canonical_url="https://bitquery.io/report",
                tier=ProductionReferenceTier.SUPPORTING,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.INDEPENDENT,
                title="Bitquery report",
                publisher="Bitquery",
                published_at=None,
                source_collection_id=None,
                source_document_id=source_ids[1],
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=(str(source_ids[1].int).zfill(64))[-64:],
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
            ),
        ),
        warnings=(),
    )
    publication_sources = _project_publication_sources(
        references=references,
        used_source_document_ids=narrative.used_source_document_ids,
    )
    assert {item.source_document_id for item in publication_sources} == set(source_ids[:2])
    assert any(item.publisher == "Bitquery" for item in publication_sources)


@pytest.mark.parametrize(
    ("direct_count", "minimum", "blocked"),
    ((3, 4, True), (4, 4, False), (5, 4, False), (0, 0, False)),
)
def test_insufficient_subject_evidence_gate_thresholds(
    direct_count: int, minimum: int, blocked: bool
) -> None:
    counts = {"direct_count": direct_count, "context_count": 6, "out_of_scope_count": 7}
    details = production_evidence_gate_details(
        counts,
        minimum=minimum,
        projection_artifact_id=UUID(int=42),
        override_active=False,
    )
    assert (details is not None) is blocked
    if details is not None:
        assert details == {
            "direct_count": direct_count,
            "minimum": minimum,
            "context_count": 6,
            "out_of_scope_count": 7,
            "projection_artifact_id": str(UUID(int=42)),
        }


def test_insufficient_subject_evidence_override_is_audited_idempotent_and_bypasses_gate() -> None:
    snapshot = _snapshot()
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    projection_artifact_id = UUID(int=42)
    decision = run.record_insufficient_subject_evidence_override(
        projection_artifact_id=projection_artifact_id,
        minimum=4,
        direct_count=2,
        context_count=8,
        out_of_scope_count=3,
        actor_id="analyst-7",
        reason="Review confirmed sufficient qualitative context.",
    )
    repeated = run.record_insufficient_subject_evidence_override(
        projection_artifact_id=projection_artifact_id,
        minimum=4,
        direct_count=2,
        context_count=8,
        out_of_scope_count=3,
        actor_id="analyst-7",
        reason="Review confirmed sufficient qualitative context.",
    )

    assert repeated == decision
    assert len(run.review_overrides) == 1
    assert decision["actor_id"] == "analyst-7"
    assert decision["code"] == PRODUCTION_INSUFFICIENT_SUBJECT_EVIDENCE_CODE
    assert run.has_insufficient_subject_evidence_override(
        projection_artifact_id=projection_artifact_id, minimum=4
    )
    assert (
        production_evidence_gate_details(
            {"direct_count": 2, "context_count": 8, "out_of_scope_count": 3},
            minimum=4,
            projection_artifact_id=projection_artifact_id,
            override_active=run.has_insufficient_subject_evidence_override(
                projection_artifact_id=projection_artifact_id, minimum=4
            ),
        )
        is None
    )


def test_relevance_contract_versions_invalidate_old_classifier_reuse() -> None:
    assert RELEVANCE_CLASSIFIER_PROMPT_VERSION == "subject-relevance-classifier-v3-counter-analysis"
    assert RELEVANCE_CLASSIFIER_CONTRACT_VERSION == "subject-relevance-text-blocks-v2-reason-pairs"
    assert RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION == "subject-relevance-wire-v3-repair-reserves"
    assert RELEVANCE_PROJECTION_POLICY_VERSION == "subject-relevance-counter-analysis-v4"
    assert DEFAULT_RELEVANCE_CLASSIFIER_VERSION == "deterministic-subject-scope-v4-counter-reserve"

    snapshot, extraction, source_ids = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    access = _access_policy(snapshot, source_ids)
    current_request = build_relevance_classifier_model_request(
        run,
        snapshot,
        pack,
        access,
        extraction_hash=canonical_extraction_hash(extraction),
    )
    old_request = build_relevance_classifier_model_request(
        run,
        snapshot,
        pack,
        access,
        extraction_hash=canonical_extraction_hash(extraction),
        prompt_version="subject-relevance-classifier-v2",
        contract_version="subject-relevance-text-blocks-v1",
    )
    assert (
        current_request.metadata["relevance_classifier_invocation_hash"]
        != (old_request.metadata["relevance_classifier_invocation_hash"])
    )

    projection = build_relevance_projection(snapshot, extraction)
    assert projection.input_hash != relevance_projection_input_hash(
        snapshot,
        extraction,
        classifier_version="deterministic-subject-scope-v3",
    )


def test_relevance_classifier_prompt_lists_only_valid_reason_pairs_and_counter_examples() -> None:
    snapshot, extraction, source_ids = _world()
    pack = build_relevance_model_evidence_pack(snapshot, extraction)
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
    )
    request = build_relevance_classifier_model_request(
        run,
        snapshot,
        pack,
        _access_policy(snapshot, source_ids),
        extraction_hash=canonical_extraction_hash(extraction),
    )
    for pair in (
        "DIRECT: primary_core_default, subject_matched_primary, malicious_subject_relation",
        "CORROBORATION: subject_matched_corroboration, malicious_subject_corroboration",
        "CONTEXT: context_source_without_relation, malicious_role_not_demonstrated",
        "COUNTER_INDICATION: explicit_counter_analysis, explicit_subject_denial",
        "OUT_OF_SCOPE: explicit_other_actor",
        "INDETERMINATE: relation_not_established, subject_link_not_demonstrated",
    ):
        assert pair in request.text
    assert "Chainalysis" in request.text
    assert "Bitquery" in request.text
    assert "OP_RETURN" in request.text
    assert "Record a COUNTER_INDICATION relation" in request.text
