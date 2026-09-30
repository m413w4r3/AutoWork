from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from cti_app.application.diagram_compilation import CompiledDiagram
from cti_app.application.model_gateway import (
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
    StructuredOutputError,
)
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseResult
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EditorialEnrichmentExecutionStatus,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentProposalV1,
    EditorialEnrichmentStageErrorCode,
    EnrichmentPlacementProposalV1,
    ProductionEditorialEnrichmentExecution,
    ProductionEditorialEnrichmentService,
    TableColumnProposalV1,
    TableProposalV1,
    TableRowProposalV1,
    build_editorial_enrichment_evidence_pack,
    build_editorial_enrichment_model_request,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_model_run_id,
    editorial_enrichment_output_contract_example,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelUsage
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentTableKind,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
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
    production_synthesis_to_json,
)

_SUBJECT_ID = UUID("a0a4f09c-1107-4ae1-8311-bf43fd2a2ce0")
_DOCUMENT_ID = UUID("b8f83b7b-7088-409a-9667-4f93758c18e1")
_INPUT_HASH = "a" * 64


class _MemorySourceDocuments:
    def __init__(self, documents: tuple[SourceDocument, ...]) -> None:
        self.documents = {document.id: document for document in documents}

    async def get(self, document_id: UUID) -> SourceDocument | None:
        return self.documents.get(document_id)

    async def list_for_subject(self, subject_id: UUID) -> tuple[SourceDocument, ...]:
        return tuple(
            document for document in self.documents.values() if document.subject_id == subject_id
        )


def _snapshot(
    *, run_id: UUID | None = None, subject_id: UUID = _SUBJECT_ID
) -> ProductionInputSnapshot:
    ids = [uuid4() for _ in range(7)]
    return ProductionInputSnapshot(
        production_run_id=run_id or ids[0],
        edition_id=ids[1],
        subject_id=subject_id,
        subject_version=1,
        subject_title="Frozen subject title",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=ids[2],
        origin_discovery_subject_id=ids[3],
        canonical_discovery_subject_id=ids[4],
        discovery_snapshot_id=ids[5],
        discovery_snapshot_version=1,
        member_candidate_ids=(ids[6],),
        discovery_summary="A discovery summary.",
        actor_or_campaign="Example actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        publication_language="fr",
        research_date=date(2026, 3, 1),
    )


def _source(
    document_id: UUID = _DOCUMENT_ID,
    *,
    facts: tuple[ExtractionFactV1, ...] | None = None,
) -> ProductionSourceExtractionV1:
    if facts is None:
        facts = (
            ExtractionFactV1(
                category="malware",
                value="ExampleRAT",
                attack_id=None,
                context="",
                evidence_quote="ExampleRAT",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        )
    return ProductionSourceExtractionV1(
        source_document_id=document_id,
        canonical_url="https://example.test/report",
        content_sha256="b" * 64,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=facts,
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )


def _extraction(
    *,
    facts: tuple[ExtractionFactV1, ...] | None = None,
    subject_id: UUID = _SUBJECT_ID,
    input_hash: str = _INPUT_HASH,
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=input_hash,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(_source(facts=facts),),
        omitted_sources=(),
        warnings=(),
    )


def _synthesis(extraction: ProductionExtractionV1) -> ProductionSynthesisV1:
    ref = extraction_evidence_refs_v1(extraction)[0]
    return ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=extraction.subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Frozen subject title",
        lead=(),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Overview",
                (SynthesisParagraphV1("ExampleRAT was observed.", (ref,)),),
            ),
        ),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )


def _proposal(handle: str) -> EditorialEnrichmentProposalV1:
    return EditorialEnrichmentProposalV1(
        tables=(
            TableProposalV1(
                key="tools_table",
                kind=EnrichmentTableKind.TOOLS,
                title="Observed tools",
                columns=(
                    TableColumnProposalV1(key="name", label="Name"),
                    TableColumnProposalV1(key="role", label="Role"),
                ),
                rows=(
                    TableRowProposalV1(cells=("ExampleRAT", "malware"), evidence_handles=(handle,)),
                ),
                placement=EnrichmentPlacementProposalV1(kind=EnrichmentPlacementKind.AFTER_LEAD),
            ),
        ),
        diagrams=(
            {
                "key": "infection_chain",
                "kind": "infection_chain",
                "title": "Observed sequence",
                "direction": "left_to_right",
                "nodes": [
                    {"node_id": "malware", "label": "ExampleRAT", "evidence_handles": [handle]},
                    {"node_id": "execution", "label": "Execution", "evidence_handles": [handle]},
                ],
                "edges": [
                    {
                        "source_node_id": "malware",
                        "target_node_id": "execution",
                        "label": "launches",
                        "evidence_handles": [handle],
                    }
                ],
                "placement": {"kind": "after_section", "section_index": 0},
            },
        ),
    )


def test_evidence_pack_is_stable_private_and_resolves_exact_refs() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)

    first = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    second = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    assert editorial_enrichment_evidence_pack_hash(
        first
    ) == editorial_enrichment_evidence_pack_hash(second)
    assert first.narrative_evidence[0]["handle"] == "E001"
    assert first.resolve_handle("E001") == extraction_evidence_refs_v1(extraction)[0]
    assert "source_document_id" not in json.dumps([dict(item) for item in first.narrative_evidence])
    assert all("body" not in record for record in first.technical_evidence)


def test_proposal_materializes_tables_diagrams_and_empty_decisions() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    enrichment = validate_editorial_enrichment_proposal(
        _proposal("E001"), pack, extraction, synthesis
    )
    empty = validate_editorial_enrichment_proposal(
        {"tables": [], "diagrams": []}, pack, extraction, synthesis
    )

    assert len(enrichment.tables) == len(enrichment.diagrams) == 1
    assert enrichment.source_figures == ()
    assert enrichment.tables[0].rows[0].evidence_refs == (pack.resolve_handle("E001"),)
    assert empty.tables == empty.diagrams == empty.source_figures == ()


def test_strict_proposal_rejects_extra_keys_missing_evidence_and_unknown_handles() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    with pytest.raises(EditorialEnrichmentProposalControlError) as extra:
        validate_editorial_enrichment_proposal(
            {"tables": [], "diagrams": [], "source_figures": []}, pack, extraction, synthesis
        )
    assert extra.value.code is EditorialEnrichmentStageErrorCode.OUTPUT_INVALID

    malformed = _proposal("E001").model_dump(mode="json")
    del malformed["tables"][0]["rows"][0]["evidence_handles"]
    with pytest.raises(EditorialEnrichmentProposalControlError) as missing:
        validate_editorial_enrichment_proposal(malformed, pack, extraction, synthesis)
    assert missing.value.code is EditorialEnrichmentStageErrorCode.OUTPUT_INVALID

    with pytest.raises(EditorialEnrichmentProposalControlError) as unknown:
        validate_editorial_enrichment_proposal(_proposal("E999"), pack, extraction, synthesis)
    assert unknown.value.code is EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE


def _colliding_proposal(collision: str) -> dict[str, object]:
    proposal = _proposal("E001").model_dump(mode="json")
    if collision == "duplicate_table_keys":
        proposal["tables"] = [proposal["tables"][0], dict(proposal["tables"][0])]
    else:
        proposal["diagrams"][0]["key"] = proposal["tables"][0]["key"]
    return proposal


@pytest.mark.parametrize("collision", ["duplicate_table_keys", "table_key_equals_diagram_key"])
def test_global_key_collision_is_output_invalid_not_a_raw_domain_error(collision: str) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    with pytest.raises(EditorialEnrichmentProposalControlError) as caught:
        validate_editorial_enrichment_proposal(
            _colliding_proposal(collision), pack, extraction, synthesis
        )

    assert caught.value.code is EditorialEnrichmentStageErrorCode.OUTPUT_INVALID


@pytest.mark.parametrize(
    "groups",
    (
        [{"group_id": "empty", "label": "Empty", "node_ids": []}],
        [
            {"group_id": "first", "label": "First", "node_ids": ["malware"]},
            {"group_id": "second", "label": "Second", "node_ids": ["malware", "execution"]},
        ],
        [{"group_id": "stage", "label": "Stage", "node_ids": ["malware", "malware"]}],
    ),
)
def test_invalid_diagram_groups_are_output_invalid_not_a_raw_domain_error(
    groups: list[dict[str, object]],
) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    proposal = _proposal("E001").model_dump(mode="json")
    proposal["diagrams"][0]["groups"] = groups

    with pytest.raises(EditorialEnrichmentProposalControlError) as caught:
        validate_editorial_enrichment_proposal(proposal, pack, extraction, synthesis)

    assert caught.value.code is EditorialEnrichmentStageErrorCode.OUTPUT_INVALID


def test_prompt_output_contract_example_satisfies_the_enforced_contract() -> None:
    example = editorial_enrichment_output_contract_example()

    parsed = EditorialEnrichmentProposalV1.model_validate(example)

    for diagram in example["diagrams"]:
        node_ids = {node["node_id"] for node in diagram["nodes"]}
        for edge in diagram["edges"]:
            assert edge["source_node_id"] in node_ids
            assert edge["target_node_id"] in node_ids
    keys = [item.key for item in (*parsed.tables, *parsed.diagrams)]
    assert len(keys) == len(set(keys))


def test_evidence_must_support_technical_literals_on_the_same_element() -> None:
    source_id = _DOCUMENT_ID
    snapshot = _snapshot()
    facts = (
        ExtractionFactV1(
            category="malware",
            value="ExampleRAT",
            attack_id=None,
            context="",
            evidence_quote="ExampleRAT",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
        ),
        ExtractionFactV1(
            category="infrastructure",
            value="203.0.113.9",
            attack_id=None,
            context="C2 address",
            evidence_quote="203.0.113.9",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
        ),
    )
    extraction = _extraction(facts=facts, input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    example_handle = next(
        handle
        for handle, ref in pack._handle_to_ref.items()
        if _all_evidence_payload(extraction, ref)["value"] == "ExampleRAT"
    )
    proposal = {
        "tables": [
            {
                "key": "network_table",
                "kind": "infrastructure",
                "title": "Network infrastructure",
                "columns": [
                    {"key": "indicator", "label": "Indicator"},
                    {"key": "role", "label": "Role"},
                ],
                "rows": [{"cells": ["203.0.113.9", "C2"], "evidence_handles": [example_handle]}],
                "placement": {"kind": "after_lead"},
            }
        ],
        "diagrams": [],
    }

    with pytest.raises(EditorialEnrichmentProposalControlError) as error:
        validate_editorial_enrichment_proposal(proposal, pack, extraction, synthesis)
    assert error.value.code is EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE


def _all_evidence_payload(extraction: ProductionExtractionV1, wanted: object) -> dict[str, object]:
    from cti_app.application.production_synthesis import _all_evidence_entries

    return dict(_all_evidence_entries(extraction)[wanted])


@pytest.mark.parametrize(
    "cell",
    [
        "| name | value |",
        "```mermaid\nflowchart LR\n```",
        "<b>HTML</b>",
        "a -> b",
        "digraph { a -> b }",
        "\\begin{tikzpicture}",
        "#set page(width: 10cm)",
    ],
)
def test_renderer_and_markup_syntax_is_rejected(cell: str) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    proposal = {
        "tables": [
            {
                "key": "bad_table",
                "kind": "custom",
                "title": "Text table",
                "columns": [{"key": "left", "label": "Left"}, {"key": "right", "label": "Right"}],
                "rows": [{"cells": [cell, "plain"], "evidence_handles": ["E001"]}],
                "placement": {"kind": "after_lead"},
            }
        ],
        "diagrams": [],
    }
    with pytest.raises(EditorialEnrichmentProposalControlError):
        validate_editorial_enrichment_proposal(proposal, pack, extraction, synthesis)


def test_model_request_is_stateless_versioned_and_uses_exact_route() -> None:
    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    snapshot = _snapshot(run_id=run.id)
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    source = SourceDocument(
        id=_DOCUMENT_ID,
        subject_id=_SUBJECT_ID,
        blob_id=uuid4(),
        original_name="source.pdf",
        origin="test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
    )

    async def create_policy():
        return await build_synthesis_access_policy(
            snapshot, extraction, _MemorySourceDocuments((source,))
        )

    import asyncio

    access_policy = asyncio.run(create_policy())
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    request = build_editorial_enrichment_model_request(
        run, snapshot, extraction, synthesis, pack, access_policy
    )

    assert request.web_search is False
    assert request.background is False
    assert request.conversation is None
    assert request.routing_hint.value == "editorial_enrichment"
    assert request.prompt_template_version == EDITORIAL_ENRICHMENT_PROMPT_VERSION
    assert request.run_id == editorial_enrichment_model_run_id(
        run, request.metadata["editorial_enrichment_input_hash"]
    )
    assert str(source.id) not in request.text
    assert "blob_id" not in request.text
    assert EDITORIAL_ENRICHMENT_GENERATOR_VERSION == "model-structured-v1"


def test_functional_hash_and_model_run_identity_bind_policy_generation_and_pack() -> None:
    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    left = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash="1" * 64,
        access_policy_hash="2" * 64,
    )
    right = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash="3" * 64,
        access_policy_hash="2" * 64,
    )

    assert left != right
    assert editorial_enrichment_model_run_id(run, left) != editorial_enrichment_model_run_id(
        run, right
    )
    assert editorial_enrichment_model_run_id(run, left) == editorial_enrichment_model_run_id(
        run, left
    )
    assert EditorialEnrichmentExecutionStatus.NEEDS_REVIEW.value == "needs_review"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("<b>C2</b>", EditorialEnrichmentStageErrorCode.OUTPUT_INVALID),
        ("evil.example", EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE),
        ("CVE-2026-1234", EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE),
    ],
)
def test_column_labels_carry_no_evidence_so_they_cannot_state_facts(
    label: str, code: EditorialEnrichmentStageErrorCode
) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    proposal = _proposal("E001").model_dump(mode="json")
    proposal["tables"][0]["columns"][0]["label"] = label

    with pytest.raises(EditorialEnrichmentProposalControlError) as error:
        validate_editorial_enrichment_proposal(proposal, pack, extraction, synthesis)
    assert error.value.code is code


# --- ProductionEditorialEnrichmentService -----------------------------------


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, object]] = {}

    def put(self, payload: dict[str, object]) -> UUID:
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.payloads[blob_id]


class _MemoryMediaAssetStore:
    async def store_diagram(self, compiled: CompiledDiagram, *, source: str) -> SimpleNamespace:
        del source
        return SimpleNamespace(asset_id=uuid4())

    async def put(self, *_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(asset_id=uuid4())


class _MemoryDiagramCompiler:
    async def compile(self, diagram: object) -> CompiledDiagram:
        key = diagram.key  # type: ignore[attr-defined]
        source = f"{key}: source".encode()
        svg = f"<svg>{key}</svg>".encode()
        return CompiledDiagram(
            diagram_key=key,
            source_format="d2",
            source_bytes=source,
            source_sha256=hashlib.sha256(source).hexdigest(),
            media_type="image/svg+xml",
            media_bytes=svg,
            media_sha256=hashlib.sha256(svg).hexdigest(),
            compiler="d2",
            compiler_version="0.9.0",
            compiler_policy_version="diagram-d2-svg-v2",
        )


class _Uow:
    def __init__(self, documents: _MemorySourceDocuments) -> None:
        self.source_documents = documents
        self.blobs = _MemoryBlobs()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _MemoryBlobs:
    async def get(self, blob_id: UUID) -> None:
        del blob_id
        return None


class _RecordingGateway:
    def __init__(self, responder: Callable[[ModelRequest], ModelExecution] | Exception) -> None:
        self._responder = responder
        self.calls: list[tuple[ModelRequest, object]] = []

    async def draft(self, request: ModelRequest, output_schema: object) -> ModelExecution:
        self.calls.append((request, output_schema))
        if isinstance(self._responder, Exception):
            raise self._responder
        return self._responder(request)


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def store_editorial_enrichment_result(self, **kwargs: object) -> ProductionArtifact:
        self.calls.append(kwargs)
        return ProductionArtifact(
            production_run_id=kwargs["run_id"],  # type: ignore[arg-type]
            subject_id=kwargs["subject_id"],  # type: ignore[arg-type]
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            version=len(self.calls),
            input_hash=kwargs["input_hash"],  # type: ignore[arg-type]
            canonical_blob_id=uuid4(),
            model_run_id=kwargs["model_run_id"],  # type: ignore[arg-type]
        )


class _ReuseStub:
    def __init__(self, result: ProductionArtifactReuseResult | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def find_or_reuse(self, **kwargs: object) -> ProductionArtifactReuseResult | None:
        self.calls.append(kwargs)
        return self.result


class _PreSubmissionFailure(ModelGatewayError):
    code = "bridge_unreachable"
    retryable = True


def _model_run(request: ModelRequest) -> ModelRun:
    return ModelRun(
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.DRAFTING,
        requested_model="gpt-5",
        prompt_template_id=request.prompt_template_id,
        prompt_template_version=request.prompt_template_version,
        authorized_input_hash=hashlib.sha256(request.text.encode()).hexdigest(),
        evidence_pack_hash=request.evidence_pack_hash,
        parameters=dict(request.parameters),
        id=request.run_id or uuid4(),
    )


def _succeeded(request: ModelRequest, proposal: BaseModel | None) -> ModelExecution:
    run = _model_run(request)
    run.succeed(
        actual_model_version="gpt-5",
        duration_ms=3,
        usage=ModelUsage(total_tokens=7),
        output_references=("model-output://1",),
        response_id=None,
    )
    return ModelExecution(run=run, output_text="raw answer", structured_output=proposal)


def _document(*, do_not_submit: bool = False) -> SourceDocument:
    return SourceDocument(
        id=_DOCUMENT_ID,
        subject_id=_SUBJECT_ID,
        blob_id=uuid4(),
        original_name="source.pdf",
        origin="test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        external_llm_allowed=not do_not_submit,
        do_not_submit=do_not_submit,
    )


def _world(
    gateway: _RecordingGateway,
    *,
    documents: tuple[SourceDocument, ...] | None = None,
    reuse: _ReuseStub | None = None,
) -> SimpleNamespace:
    snapshot = _snapshot()
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        pipeline_generation=2,
    )
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    store = _MemoryArtifactStore()
    writer = _RecordingWriter()
    source_documents = _MemorySourceDocuments(
        documents if documents is not None else (_document(),)
    )
    service = ProductionEditorialEnrichmentService(
        uow_factory=lambda: _Uow(source_documents),  # type: ignore[arg-type,return-value]
        artifact_store=store,  # type: ignore[arg-type]
        model_gateway=gateway,  # type: ignore[arg-type]
        editorial_enrichment_service=writer,  # type: ignore[arg-type]
        artifact_reuse=reuse,  # type: ignore[arg-type]
        media_asset_store=_MemoryMediaAssetStore(),  # type: ignore[arg-type]
        diagram_compiler=_MemoryDiagramCompiler(),  # type: ignore[arg-type]
    )

    def artifact(stage: ProductionArtifactStage, payload: dict[str, object]) -> ProductionArtifact:
        return ProductionArtifact(
            production_run_id=run.id,
            subject_id=snapshot.subject_id,
            stage=stage,
            version=1,
            input_hash="e" * 64,
            canonical_blob_id=store.put(payload),
        )

    return SimpleNamespace(
        service=service,
        store=store,
        writer=writer,
        gateway=gateway,
        run=run,
        snapshot=snapshot,
        extraction=extraction,
        synthesis=synthesis,
        extraction_artifact=artifact(
            ProductionArtifactStage.EXTRACTION, production_extraction_to_json(extraction)
        ),
        synthesis_artifact=artifact(
            ProductionArtifactStage.SYNTHESIS, production_synthesis_to_json(synthesis)
        ),
    )


async def _execute(world: SimpleNamespace) -> ProductionEditorialEnrichmentExecution:
    service: ProductionEditorialEnrichmentService = world.service
    return await service.execute(
        world.run, world.snapshot, world.extraction_artifact, world.synthesis_artifact
    )


@pytest.mark.asyncio
async def test_service_drafts_once_statelessly_and_stores_model_provenance() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.model_calls, result.table_count, result.diagram_count) == (1, 1, 1)
    assert result.source_figure_count == 0
    request, schema = world.gateway.calls[0]
    assert schema is EditorialEnrichmentProposalV1
    assert request.routing_hint is ModelRoutingHint.EDITORIAL_ENRICHMENT
    assert (
        request.run_id
        == result.model_run_id
        == editorial_enrichment_model_run_id(world.run, result.input_hash)
    )
    stored = world.writer.calls[0]
    assert stored["raw_result"] == "raw answer"
    assert stored["model_run_id"] == result.model_run_id
    assert stored["input_hash"] == result.input_hash
    enrichment = stored["enrichment"]
    assert enrichment.source_figures == ()  # type: ignore[attr-defined]
    assert enrichment.diagrams[0].compiled_asset_id is not None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_empty_model_decision_is_a_valid_stored_enrichment() -> None:
    empty = EditorialEnrichmentProposalV1()
    world = _world(_RecordingGateway(lambda request: _succeeded(request, empty)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.table_count, result.diagram_count, result.model_calls) == (0, 0, 1)
    assert len(world.writer.calls) == 1


@pytest.mark.asyncio
async def test_do_not_submit_needs_review_without_call_or_empty_artifact() -> None:
    world = _world(
        _RecordingGateway(lambda request: _succeeded(request, None)),
        documents=(_document(do_not_submit=True),),
    )

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == EditorialEnrichmentStageErrorCode.POLICY_BLOCKED.value
    assert result.model_calls == 0
    assert world.gateway.calls == [] and world.writer.calls == []


@pytest.mark.asyncio
async def test_missing_exact_source_metadata_blocks_before_drafting() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, None)), documents=())

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.BLOCKED
    assert result.error_code == EditorialEnrichmentStageErrorCode.ACCESS_POLICY_UNAVAILABLE.value
    assert world.gateway.calls == []


@pytest.mark.asyncio
async def test_mismatched_subject_inputs_block_without_model_call() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, None)))
    other = _synthesis(_extraction(subject_id=uuid4(), input_hash=world.snapshot.input_hash))
    world.synthesis_artifact = replace(
        world.synthesis_artifact,
        canonical_blob_id=world.store.put(production_synthesis_to_json(other)),
    )

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.BLOCKED
    assert result.error_code == EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH.value
    assert result.model_calls == 0
    assert world.gateway.calls == []


@pytest.mark.asyncio
async def test_retryable_pre_submission_failure_propagates_without_artifact() -> None:
    world = _world(_RecordingGateway(_PreSubmissionFailure("bridge never reached")))

    with pytest.raises(_PreSubmissionFailure):
        await _execute(world)

    assert len(world.gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["raised", "returned"])
async def test_ambiguous_submission_uses_the_shared_reconciliation_state(outcome: str) -> None:
    reconciliation_run_id = uuid4()
    responder: Callable[[ModelRequest], ModelExecution] | Exception
    if outcome == "raised":
        responder = ModelSubmissionReconciliationRequiredError(
            details={"bridge_reason": "active_signal_stalled"},
            model_run_id=reconciliation_run_id,
        )
    else:

        def responder(request: ModelRequest) -> ModelExecution:
            run = replace(_model_run(request), id=reconciliation_run_id)
            run.require_review(
                PRODUCTION_RECONCILIATION_ERROR_CODE,
                "submission state is unknown",
                details={"bridge_reason": "active_signal_stalled"},
            )
            return ModelExecution(run=run)

    world = _world(_RecordingGateway(responder))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    # The run state machine only records a reconciliation identity for this code.
    assert result.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
    assert result.model_run_id == reconciliation_run_id
    assert result.details is not None
    assert result.details["model_run_id"] == str(reconciliation_run_id)
    assert result.details["bridge_reason"] == "active_signal_stalled"
    assert len(world.gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["absent", "gateway_error", "unknown_handle"])
async def test_invalid_structured_output_needs_review_without_artifact(failure: str) -> None:
    responder: Callable[[ModelRequest], ModelExecution] | Exception
    if failure == "absent":
        responder = lambda request: _succeeded(request, None)  # noqa: E731
    elif failure == "gateway_error":
        responder = StructuredOutputError("schema mismatch")
    else:
        responder = lambda request: _succeeded(request, _proposal("E999"))  # noqa: E731
    world = _world(_RecordingGateway(responder))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code in {
        EditorialEnrichmentStageErrorCode.OUTPUT_INVALID.value,
        EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE.value,
    }
    assert result.model_calls == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["duplicate_table_keys", "table_key_equals_diagram_key"])
async def test_key_collision_in_structured_proposal_needs_review_without_artifact(
    collision: str,
) -> None:
    proposal = EditorialEnrichmentProposalV1.model_validate(_colliding_proposal(collision))
    world = _world(_RecordingGateway(lambda request: _succeeded(request, proposal)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == "editorial_enrichment_output_invalid"
    assert result.model_calls == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_invalid_reuse_candidate_reports_no_model_call() -> None:
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    reuse = _ReuseStub()
    world = _world(gateway, reuse=reuse)
    reuse.result = ProductionArtifactReuseResult(
        ProductionArtifact(
            production_run_id=world.run.id,
            subject_id=world.snapshot.subject_id,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            version=1,
            input_hash="f" * 64,
            canonical_blob_id=uuid4(),
            model_run_id=uuid4(),
        ),
        reused=True,
    )

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == EditorialEnrichmentStageErrorCode.REUSE_INVALID.value
    assert result.model_calls == 0
    assert result.model_run_id is None
    assert gateway.calls == []
