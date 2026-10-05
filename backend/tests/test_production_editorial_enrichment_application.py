from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

import cti_app.api.production as production_api
import cti_app.application.production_editorial_enrichment as enrichment_module
import cti_app.application.production_enrichment_revision as revision_module
from cti_app.application.diagram_compilation import (
    CompiledDiagram,
    DiagramCompilerProcessError,
)
from cti_app.application.model_gateway import (
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
    StructuredOutputError,
)
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseResult
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    AnalyticPurposeProposalV1,
    AnnotationProposalV1,
    EditorialEnrichmentEvidencePackV1,
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
    build_editorial_figure_catalog,
    compute_editorial_enrichment_input_hash,
    compute_editorial_enrichment_invocation_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_model_run_id,
    editorial_enrichment_output_contract_example,
    parse_editorial_enrichment_proposal_wire,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_enrichment_revision import (
    EditorialEnrichmentRevisionValidationError,
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_prompts import EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.source_figure_inventory import (
    ArchivedFigureAsset,
    ArchivedFigureSource,
    SourceFigureCatalogMetadata,
    SourceFigureInventory,
    SourceFigureInventoryResult,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import SourceCollection
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
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramNodeRole,
    DiagramRelationType,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentRevisionOutcome,
    EditorialMediaType,
    EnrichmentDiagramDirection,
    EnrichmentPlacementKind,
    EnrichmentTableKind,
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
    source_figure_id,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
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
from cti_app.domain.publication import ArtifactType
from cti_app.domain.semantic_annotation import SemanticRole
from cti_app.domain.source_media import (
    SourceFigureProvenanceDiagnostic,
    SourceFigureProvenanceStage,
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


class _MemorySourceCollections:
    def __init__(self, collections: tuple[SourceCollection, ...] = ()) -> None:
        self.collections = {collection.id: collection for collection in collections}

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        return self.collections.get(collection_id)


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
    events: tuple[ExtractionEventV1, ...] = (),
    indicators: tuple[ExtractionIndicatorV1, ...] = (),
    tier: ProductionReferenceTier = ProductionReferenceTier.CORE,
    profile: ExtractionProfile = ExtractionProfile.FULL,
    role: SourceRole = SourceRole.PRIMARY,
    editorial_role: ProductionEditorialRole | None = None,
    kind: ProductionReferenceKind = ProductionReferenceKind.PUBLICATION,
    url_suffix: str = "report",
) -> ProductionSourceExtractionV1:
    if facts is None:
        facts = (
            ExtractionFactV1(
                category="malware",
                value="ExampleRAT",
                attack_id=None,
                context="ExampleRAT launches execution.",
                evidence_quote="ExampleRAT launches execution.",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        )
    return ProductionSourceExtractionV1(
        source_document_id=document_id,
        canonical_url=f"https://example.test/{url_suffix}",
        content_sha256="b" * 64,
        tier=tier,
        kind=kind,
        role=role,
        editorial_role=editorial_role,
        profile=profile,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=facts,
        events=events,
        indicators=indicators,
        rules=(),
        uncertainties=(),
    )


def _extraction(
    *,
    facts: tuple[ExtractionFactV1, ...] | None = None,
    subject_id: UUID = _SUBJECT_ID,
    input_hash: str = _INPUT_HASH,
    sources: tuple[ProductionSourceExtractionV1, ...] | None = None,
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=input_hash,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=sources or (_source(facts=facts),),
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


def _figure_inventory(
    extraction: ProductionExtractionV1,
    *,
    decision: SourceFigureDecision = SourceFigureDecision.ACCEPTED,
    figure_label: str = "ExampleRAT execution architecture",
    source_caption: str | None = "ExampleRAT execution architecture",
    alt_text: str | None = "ExampleRAT flow",
) -> SourceFigureInventoryResult:
    source = extraction.sources[0]
    locator = SourceFigureLocatorV1(
        page=2,
        section="Network overview",
        figure_label=figure_label,
        original_asset_url="https://cdn.example.test/example-rat.png",
    )
    digest = "f" * 64
    figure = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source.source_document_id,
            sha256=digest,
            source=source.canonical_url,
            locator=locator,
        ),
        blob_id=UUID(int=601) if decision is SourceFigureDecision.ACCEPTED else None,
        sha256=digest,
        mime_type="image/png",
        byte_size=4096,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance=f"archived source document {source.source_document_id}; figure context",
        locator=locator,
        decision=decision,
        decision_reason=(
            "accepted_for_review"
            if decision is SourceFigureDecision.ACCEPTED
            else "boilerplate_pattern"
        ),
    )
    return SourceFigureInventoryResult(
        figures=(figure,),
        policy_sha256="d" * 64,
        catalog_metadata={
            figure.figure_id: SourceFigureCatalogMetadata(
                alt_text=alt_text,
                caption_text=source_caption,
                nearby_heading_text="Network overview",
                anchor="figure:2:network-overview",
                width=640,
                height=400,
            )
        },
        provenance_diagnostics=(
            SourceFigureProvenanceDiagnostic(
                stage=SourceFigureProvenanceStage.DISCOVERED,
                source_document_id=source.source_document_id,
                figure_id=figure.figure_id,
            ),
            *(
                (
                    SourceFigureProvenanceDiagnostic(
                        stage=SourceFigureProvenanceStage.ARCHIVED,
                        source_document_id=source.source_document_id,
                        figure_id=figure.figure_id,
                        blob_id=figure.blob_id,
                        sha256=figure.sha256,
                    ),
                )
                if decision is SourceFigureDecision.ACCEPTED and figure.blob_id is not None
                else ()
            ),
        ),
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
                purpose=AnalyticPurposeProposalV1(
                    question="Which named tool is documented?",
                    available_data="The report names ExampleRAT.",
                    comprehension_gain="A compact row pairs the tool with its role.",
                    scope="The single named tool in this evidence item.",
                    evidence_handles=(handle,),
                    knowledge_limits="No additional tool behavior is documented.",
                    placement_reason="Place after the opening paragraph introducing the tool.",
                ),
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
                        "relation_type": "factual",
                        "evidence_handles": [handle],
                    }
                ],
                "placement": {"kind": "after_section", "section_index": 0},
                "purpose": {
                    "question": "Does ExampleRAT launch an execution step?",
                    "available_data": "The source states that ExampleRAT launches execution.",
                    "comprehension_gain": "A short sequence shows the documented action.",
                    "scope": "Only the two endpoints in this evidence item.",
                    "evidence_handles": [handle],
                    "knowledge_limits": "No later infection stages are documented.",
                    "placement_reason": "Place beside the paragraph describing execution.",
                },
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


def test_core_full_narrative_evidence_is_available_after_primary_core():
    snapshot = _snapshot()
    core_id, supporting_id, annex_id = uuid4(), uuid4(), uuid4()
    core = _source(
        core_id,
        facts=(
            ExtractionFactV1(
                category="actors",
                value="Core actor",
                attack_id=None,
                context="",
                evidence_quote="Core actor",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(core_id,),
            ),
        ),
        events=(
            ExtractionEventV1(
                event_date=date(2026, 1, 2),
                date_text=None,
                text="The core operation began.",
                context="",
                evidence_quote="The core operation began.",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(core_id,),
            ),
        ),
        url_suffix="core",
    )
    supporting = _source(
        supporting_id,
        tier=ProductionReferenceTier.CORE,
        profile=ExtractionProfile.FULL,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        facts=(
            ExtractionFactV1(
                category="malware",
                value="Independent tool",
                attack_id=None,
                context="",
                evidence_quote="Independent tool",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(supporting_id,),
            ),
        ),
        events=(
            ExtractionEventV1(
                event_date=date(2026, 1, 3),
                date_text=None,
                text="Independent analysis confirmed the activity.",
                context="",
                evidence_quote="Independent analysis confirmed the activity.",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(supporting_id,),
            ),
        ),
        url_suffix="corroborating-core",
    )
    annex = _source(
        annex_id,
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        profile=ExtractionProfile.IOC_RULES,
        editorial_role=ProductionEditorialRole.CONTEXT,
        facts=(
            ExtractionFactV1(
                category="files",
                value="Annex-only narrative must stay technical",
                attack_id=None,
                context="",
                evidence_quote="Annex-only narrative must stay technical",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(annex_id,),
            ),
        ),
        indicators=(
            ExtractionIndicatorV1(
                value="annex.example",
                artifact_type=ArtifactType.DOMAIN,
                indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
                context="Published in an IOC annex.",
                evidence_quote="annex.example",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(annex_id,),
            ),
        ),
        url_suffix="technical-annex",
    )
    extraction = _extraction(
        subject_id=snapshot.subject_id,
        input_hash=snapshot.input_hash,
        sources=(supporting, annex, core),
    )
    synthesis = _synthesis(extraction)

    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    core_positions = [
        index
        for index, record in enumerate(pack.narrative_evidence)
        if record["source_role"] == "primary"
    ]
    supporting_positions = [
        index
        for index, record in enumerate(pack.narrative_evidence)
        if record["source_role"] == "independent"
    ]

    independent_kinds = {
        record["kind"]
        for record in pack.narrative_evidence
        if record["source_role"] == "independent"
    }
    assert independent_kinds == {
        "fact",
        "event",
    }
    assert max(core_positions) < min(supporting_positions)
    assert all(
        record["editorial_role"] == ProductionEditorialRole.CORROBORATION.value
        for record in pack.narrative_evidence
        if record["source_role"] == "independent"
    )
    assert "Annex-only narrative must stay technical" not in str(pack.narrative_evidence)
    assert [record["value"] for record in pack.technical_evidence] == ["annex.example"]
    assert str(supporting_id) not in json.dumps([dict(item) for item in pack.narrative_evidence])
    assert str(annex_id) not in json.dumps([dict(item) for item in pack.technical_evidence])


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
        duplicate = dict(proposal["tables"][0])
        duplicate["purpose"] = {
            **duplicate["purpose"],
            "question": "Which different named tool is documented?",
        }
        proposal["tables"] = [proposal["tables"][0], duplicate]
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
    contract = editorial_enrichment_output_contract_example()

    assert isinstance(contract, str)
    assert "COLUMN C001" in contract and "ROW R001" in contract
    assert "NODE N001" in contract and "RELATION L001" in contract
    assert "ROLE: infrastructure" in contract
    assert "TITLE: Résolution via une adresse Bitcoin" in contract
    assert "RELATION_TYPE is required" in contract
    assert "factual | inference | comparison" in contract
    assert "NO USEFUL ENRICHMENT" in contract
    assert "FIGURE P001" in contract and "NEEDS N001" in contract
    assert "EVIDENCE: E001" in contract
    assert "D2" in contract
    assert "CHART" in contract
    assert "CHART est une branche conceptuelle" in contract
    assert (
        "Une représentation existante de la source est prioritaire sur une représentation "
        "reconstruite, lorsque les deux répondent à la même question analytique."
    ) in contract
    assert "Deux enrichissements ne doivent pas répondre à la même question analytique." in contract


def test_prompt_uses_analytic_intent_without_row_or_node_quotas() -> None:
    snapshot = _snapshot()
    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    snapshot = replace(snapshot, production_run_id=run.id)
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    document = _document()
    import asyncio

    access_policy = asyncio.run(
        build_synthesis_access_policy(snapshot, extraction, _MemorySourceDocuments((document,)))
    )
    request = build_editorial_enrichment_model_request(
        run, snapshot, extraction, synthesis, pack, access_policy
    )
    prompt_payload = json.loads(request.text)

    for field in (
        "PURPOSE",
        "DATA",
        "GAIN",
        "SCOPE",
        "PURPOSE_EVIDENCE",
        "LIMITS",
        "PLACEMENT",
        "PLACEMENT_REASON",
    ):
        assert field in request.text
    assert "comprehension gain" in request.text.lower() or "avantage sur le paragraphe" in (
        request.text.lower()
    )
    assert "table_rows" not in request.text
    assert "diagram_nodes" not in request.text
    assert "row or node target exists" in request.text
    assert "control-plane and data-plane" in request.text
    assert "diagram_node_roles" in prompt_payload["editorial_guidance"]
    assert prompt_payload["editorial_guidance"]["diagram_layout_budgets"]["maximum_nodes"] == 8
    layout_budgets = prompt_payload["editorial_guidance"]["diagram_layout_budgets"]
    assert layout_budgets["vertical_when_nodes_over"] == 3
    assert layout_budgets["vertical_when_label_characters_over"] == 30
    assert "plus de 4 nœuds" not in request.text
    assert "infection_chain aux étapes ordonnées d'une intrusion" in request.text
    assert "Commence par identifier une question analytique" in request.text
    assert "CHART est une branche conceptuelle" in request.text
    assert "Aucun quota minimal de médias ne s'applique" in request.text
    assert prompt_payload["editorial_guidance"]["analytic_validation_policy_version"] == (
        enrichment_module.EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION
    )


def test_parser_keeps_table_rows_unbounded_and_accepts_eight_diagram_nodes() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    proposal = _proposal("E001")
    table = proposal.tables[0].model_copy(
        update={
            "rows": tuple(
                TableRowProposalV1(
                    cells=(f"Item {index:03d}", f"Detail {index:03d}"),
                    evidence_handles=("E001",),
                )
                for index in range(1, 52)
            )
        }
    )
    diagram = proposal.diagrams[0]
    nodes = (
        *diagram.nodes,
        *(
            diagram.nodes[0].model_copy(
                update={"node_id": f"component_{index}", "label": f"Component {index}"}
            )
            for index in range(3, 9)
        ),
    )
    diagram = diagram.model_copy(
        update={"nodes": nodes, "direction": EnrichmentDiagramDirection.TOP_TO_BOTTOM}
    )
    proposal = proposal.model_copy(update={"tables": (table,), "diagrams": (diagram,)})

    result = parse_editorial_enrichment_proposal_wire(_proposal_to_wire(proposal), pack)

    assert result.proposal is not None
    assert len(result.proposal.tables[0].rows) == 51
    assert len(result.proposal.diagrams[0].nodes) == 8


def test_diagram_with_long_labels_requires_top_to_bottom_direction() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001")).replace(
        "LABEL: ExampleRAT", "LABEL: Résolution Bitcoin serveur C2 iranien", 1
    )

    result = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert result.proposal is not None
    assert result.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_direction_requires_top_to_bottom" in {
        item.reason_code for item in result.rejections
    }


@pytest.mark.parametrize("label_kind", ("relation", "group"))
def test_diagram_relation_and_group_labels_require_top_to_bottom_direction(
    label_kind: str,
) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    proposal_payload = _proposal("E001").model_dump(mode="json")
    diagram_payload = proposal_payload["diagrams"][0]
    if label_kind == "relation":
        diagram_payload["edges"][0]["label"] = "x" * 31
    else:
        diagram_payload["groups"] = [
            {
                "group_id": "source-group",
                "label": "x" * 31,
                "node_ids": ["malware"],
            }
        ]
    proposal = EditorialEnrichmentProposalV1.model_validate(proposal_payload)

    result = parse_editorial_enrichment_proposal_wire(_proposal_to_wire(proposal), pack)

    assert result.proposal is not None
    assert result.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_direction_requires_top_to_bottom" in {
        item.reason_code for item in result.rejections
    }


def test_purpose_is_required_and_duplicate_questions_reject_only_one_sibling() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001"))

    missing_purpose = wire.replace("PURPOSE: Which named tool is documented?", "PURPOSE:", 1)
    missing_result = parse_editorial_enrichment_proposal_wire(missing_purpose, pack)
    assert missing_result.proposal is not None
    assert missing_result.proposal.tables == ()
    assert len(missing_result.proposal.diagrams) == 1
    assert "editorial_enrichment_analytic_purpose_missing_or_invalid" in {
        item.reason_code for item in missing_result.rejections
    }

    duplicate_purpose = wire.replace(
        "PURPOSE: Does ExampleRAT launch an execution step?",
        "PURPOSE: Which named tool is documented?!",
        1,
    )
    duplicate_result = parse_editorial_enrichment_proposal_wire(duplicate_purpose, pack)
    assert duplicate_result.proposal is not None
    assert len(duplicate_result.proposal.tables) == 1
    assert duplicate_result.proposal.diagrams == ()
    assert "editorial_enrichment_duplicate_analytic_purpose" in {
        item.reason_code for item in duplicate_result.rejections
    }


def test_fixture_a_source_figure_wins_over_duplicate_diagram_and_table() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    catalog = build_editorial_figure_catalog(extraction, _figure_inventory(extraction))
    question = "What execution action does ExampleRAT perform?"
    proposal_wire = _proposal_to_wire(_proposal("E001"))
    table_wire = _top_wire_block(proposal_wire, "TABLE T001").replace(
        "PURPOSE: Which named tool is documented?", f"PURPOSE: {question}", 1
    )
    diagram_wire = _top_wire_block(proposal_wire, "DIAGRAM D001").replace(
        "PURPOSE: Does ExampleRAT launch an execution step?", f"PURPOSE: {question}", 1
    )
    wire = "\n\n".join((_figure_wire(purpose_question=question), table_wire, diagram_wire))

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack, figure_catalog=catalog)

    assert parsed.proposal is not None
    assert len(parsed.proposal.figures) == 1
    assert parsed.proposal.tables == ()
    assert parsed.proposal.diagrams == ()
    assert {item.reason_code for item in parsed.rejections} == {
        "editorial_enrichment_source_figure_preferred_over_reconstruction"
    }
    assert any(
        warning.startswith("editorial_enrichment_source_figure_preferred_over_reconstruction:F001:")
        for warning in parsed.warnings
    )

    enrichment = validate_editorial_enrichment_proposal(
        parsed.proposal,
        pack,
        extraction,
        synthesis,
        figure_catalog=catalog,
        warnings=parsed.warnings,
    )
    figure = next(item for item in enrichment.source_figures if item.purpose is not None)
    payload = editorial_enrichment_to_json(enrichment)
    restored = editorial_enrichment_from_json(payload)
    assert figure.purpose is not None
    assert restored.source_figures[-1].purpose == figure.purpose


def test_fixture_b_c2_without_source_figure_keeps_the_diagram() -> None:
    text = "ExampleRAT sends commands to c2.example.test through DNS."
    fact = ExtractionFactV1(
        category="infrastructure",
        value=text,
        attack_id=None,
        context=text,
        evidence_quote=text,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(_DOCUMENT_ID,),
    )
    snapshot = _snapshot()
    extraction = _extraction(facts=(fact,), input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = """DIAGRAM D001
KEY: c2_flow
KIND: network_flow
TITLE: Communication avec le serveur C2
PURPOSE: How does ExampleRAT contact its C2 server?
DATA: ExampleRAT sends commands to c2.example.test through DNS.
GAIN: Two nodes make the documented communication path visible.
SCOPE: The single communication path stated in E001.
PURPOSE_EVIDENCE: E001
LIMITS: No other C2 endpoint is documented.
DIRECTION: left_to_right
PLACEMENT: after_lead
PLACEMENT_REASON: Place beside the paragraph that describes the C2 communication.
NODE N001
ID: implant
ROLE: malware_tool
LABEL: ExampleRAT
EVIDENCE: E001
END NODE
NODE N002
ID: c2
ROLE: infrastructure
LABEL: c2.example.test
EVIDENCE: E001
END NODE
RELATION L001
FROM: implant
TO: c2
RELATION_TYPE: factual
LABEL: sends commands to
EVIDENCE: E001
END RELATION
END DIAGRAM"""

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is not None
    assert parsed.proposal.tables == ()
    assert len(parsed.proposal.diagrams) == 1
    assert parsed.proposal.figures == ()


def test_fixture_c_chart_branch_is_prompted_without_a_chart_wire_contract() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    contract = editorial_enrichment_output_contract_example()

    assert EditorialMediaType.CHART.value == "chart"
    assert "les informations sont-elles principalement temporelles" in contract
    assert "OUI → CHART" in contract
    assert "ce contrat n'accepte pas encore de bloc\nCHART" in contract
    assert "CHART C001" not in contract
    unsupported = parse_editorial_enrichment_proposal_wire(
        "CHART C001\nPURPOSE: When were these domains created?\nEND CHART", pack
    )
    assert unsupported.proposal is None
    assert unsupported.error_code == "editorial_enrichment_unintelligible_response"


def test_fixture_d_structured_exfiltration_mapping_keeps_one_table() -> None:
    text = (
        "Credentials are collected during exfiltration at the collection step under "
        "identifier account_id. Passwords are exfiltrated during credential theft under "
        "identifier password_list."
    )
    fact = ExtractionFactV1(
        category="other_technical",
        value=text,
        attack_id=None,
        context=text,
        evidence_quote=text,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(_DOCUMENT_ID,),
    )
    snapshot = _snapshot()
    extraction = _extraction(facts=(fact,), input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = """TABLE T001
KEY: exfiltration_mapping
KIND: custom
TITLE: Correspondances d'exfiltration documentées
PURPOSE: Which information is exfiltrated at each step and under which identifier?
DATA: E001 pairs each information type with its step and identifier.
GAIN: The three fields make each documented mapping easy to consult.
SCOPE: The two information mappings in E001.
PURPOSE_EVIDENCE: E001
LIMITS: Only the two mappings stated in E001 are included.
PLACEMENT: after_lead
PLACEMENT_REASON: Place beside the paragraph describing the exfiltration mappings.
COLUMN C001
KEY: information
LABEL: Information
END COLUMN
COLUMN C002
KEY: step
LABEL: Step
END COLUMN
COLUMN C003
KEY: identifier
LABEL: Identifier
END COLUMN
ROW R001
CELL: Credentials
CELL: collection
CELL: account_id
EVIDENCE: E001
END ROW
ROW R002
CELL: Passwords
CELL: credential theft
CELL: password_list
EVIDENCE: E001
END ROW
END TABLE"""

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is not None
    assert len(parsed.proposal.tables) == 1
    assert parsed.proposal.diagrams == ()
    assert parsed.proposal.figures == ()


def test_fixture_e_trivial_prose_accepts_nothing() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    parsed = parse_editorial_enrichment_proposal_wire("NO USEFUL ENRICHMENT", pack)

    assert parsed.proposal is not None
    assert parsed.proposal.figures == ()
    assert parsed.proposal.tables == ()
    assert parsed.proposal.diagrams == ()
    assert parsed.proposal.annotations == ()
    assert parsed.proposal.resource_needs == ()


def test_paraphrase_only_table_is_rejected_while_a_valid_table_sibling_survives() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace(
        "CELL: ExampleRAT\nCELL: malware",
        "CELL: ExampleRAT was observed.\nCELL: ExampleRAT was observed.",
        1,
    )
    wire += """
TABLE T002
KEY: documented_action
KIND: custom
TITLE: Documented action
PURPOSE: Which action is stated in the evidence?
DATA: ExampleRAT launches execution.
GAIN: The pair distinguishes the named actor from the action.
SCOPE: One action from E001.
PURPOSE_EVIDENCE: E001
LIMITS: No later stage is described.
PLACEMENT: after_lead
PLACEMENT_REASON: Place after the paragraph introducing the action.
COLUMN C003
KEY: actor
LABEL: Actor
END COLUMN
COLUMN C004
KEY: action
LABEL: Action
END COLUMN
ROW R002
CELL: ExampleRAT
CELL: launches execution
EVIDENCE: E001
END ROW
END TABLE
"""

    result = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert result.proposal is not None
    assert [item.key for item in result.proposal.tables] == ["documented_action"]
    assert {item.reason_code for item in result.rejections} >= {
        "editorial_enrichment_table_paraphrase_only"
    }


def test_relation_without_endpoint_support_is_rejected_without_dropping_table() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace("LABEL: ExampleRAT", "LABEL: OP_RETURN", 1)
    wire = wire.replace("LABEL: Execution", "LABEL: JSON-RPC", 1)

    result = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert result.proposal is not None
    assert len(result.proposal.tables) == 1
    assert result.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_relation_endpoint_unsupported" in {
        item.reason_code for item in result.rejections
    }


def test_factual_relation_needs_both_endpoints_in_the_same_cited_evidence_item() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    second_ref = replace(pack.resolve_handle("E001"), evidence_key="e" * 64)
    separated_pack = replace(
        pack,
        narrative_evidence=(
            *pack.narrative_evidence,
            {"handle": "E002", "context": "OP_RETURN appears in a separate observation."},
        ),
        _handle_to_ref={**pack._handle_to_ref, "E002": second_ref},
    )
    wire = _proposal_to_wire(_proposal("E001")).replace(
        "KIND: infection_chain", "KIND: component_relationship", 1
    )
    wire = wire.replace(
        "ID: execution\nLABEL: Execution\nROLE: unknown\nEVIDENCE: E001",
        "ID: execution\nLABEL: OP_RETURN\nROLE: unknown\nEVIDENCE: E002",
        1,
    )
    wire = wire.replace(
        "LABEL: launches\nEVIDENCE: E001\nEND RELATION",
        "LABEL: launches\nEVIDENCE: E001, E002\nEND RELATION",
        1,
    )

    result = parse_editorial_enrichment_proposal_wire(wire, separated_pack)

    assert result.proposal is not None
    assert len(result.proposal.tables) == 1
    assert result.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_relation_without_endpoint_support" in {
        item.reason_code for item in result.rejections
    }


def test_inference_relation_is_explicit_and_kept_with_supporting_handles() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace("KIND: infection_chain", "KIND: component_relationship", 1)
    wire = wire.replace("RELATION_TYPE: factual", "RELATION_TYPE: inference", 1)

    result = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert result.proposal is not None
    assert result.proposal.diagrams[0].edges[0].relation_type.value == "inference"
    assert result.proposal.diagrams[0].edges[0].evidence_handles == ("E001",)


def test_link_not_demonstrated_projection_rejects_a_factual_cross_source_edge() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    first_ref = pack.resolve_handle("E001")
    second_ref = replace(first_ref, source_document_id=uuid4(), evidence_key="e" * 64)
    contradictory_pack = replace(
        pack,
        narrative_evidence=(
            *pack.narrative_evidence,
            {"handle": "E002", "context": "Execution is mentioned by source B."},
        ),
        reserve_evidence=(
            {"handle": "R001", "context": "ExampleRAT is mentioned."},
            {"handle": "R002", "context": "Execution is mentioned."},
        ),
        source_pair_relations=(
            {
                "relation": "link_not_demonstrated",
                "reason": "The two source observations are not linked.",
                "supporting_handles": ("R001", "R002"),
            },
        ),
        _handle_to_ref={**pack._handle_to_ref, "E002": second_ref},
        _reserve_handle_to_ref={"R001": first_ref, "R002": second_ref},
    )
    wire = _proposal_to_wire(_proposal("E001")).replace(
        "ID: execution\nLABEL: Execution\nROLE: unknown\nEVIDENCE: E001",
        "ID: execution\nLABEL: Execution\nROLE: unknown\nEVIDENCE: E002",
        1,
    )

    result = parse_editorial_enrichment_proposal_wire(wire, contradictory_pack)

    assert result.proposal is not None
    assert result.proposal.diagrams == ()
    assert "editorial_enrichment_relation_contradicts_projection" in {
        item.reason_code for item in result.rejections
    }


def test_comparison_is_not_an_infection_sequence_and_sequences_need_relation_evidence() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = _proposal_to_wire(_proposal("E001"))
    comparison = wire.replace("KIND: infection_chain", "KIND: custom", 1)
    comparison = comparison.replace("RELATION_TYPE: factual", "RELATION_TYPE: comparison", 1)
    comparison = comparison.replace("LABEL: launches", "LABEL: comparison of observations", 1)
    comparison_result = parse_editorial_enrichment_proposal_wire(comparison, pack)
    assert comparison_result.proposal is not None
    assert comparison_result.proposal.diagrams[0].edges[0].relation_type.value == "comparison"

    false_sequence = wire.replace("LABEL: launches", "LABEL: triggers a later stage", 1)
    sequence_result = parse_editorial_enrichment_proposal_wire(false_sequence, pack)
    assert sequence_result.proposal is not None
    assert sequence_result.proposal.diagrams == ()
    assert "editorial_enrichment_infection_chain_sequence_not_documented" in {
        item.reason_code for item in sequence_result.rejections
    }

    comparison_sequence = comparison.replace("KIND: custom", "KIND: infection_chain", 1)
    invalid_result = parse_editorial_enrichment_proposal_wire(comparison_sequence, pack)
    assert invalid_result.proposal is not None
    assert invalid_result.proposal.diagrams == ()
    assert "editorial_enrichment_comparison_cannot_be_infection_chain" in {
        item.reason_code for item in invalid_result.rejections
    }


def test_zero_tables_and_diagrams_are_a_valid_explicit_empty_decision() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)

    result = parse_editorial_enrichment_proposal_wire("NO USEFUL ENRICHMENT", pack)

    assert result.explicit_empty is True
    assert result.proposal is not None
    assert result.proposal.tables == result.proposal.diagrams == ()


def _figure_wire(
    *,
    handle: str = "F001",
    caption: str = "ExampleRAT execution architecture",
    purpose_question: str = "What execution action does ExampleRAT perform?",
    placement: str | None = "after_section",
    section_index: int = 0,
    extra: str = "",
) -> str:
    lines = [
        "FIGURE P001",
        f"FIGURE_HANDLE: {handle}",
        f"CAPTION: {caption}",
        f"PURPOSE: {purpose_question}",
        "DATA: ExampleRAT launches execution.",
        "GAIN: The figure clarifies the execution action.",
        "SCOPE: The launch of execution by ExampleRAT.",
        "PURPOSE_EVIDENCE: E001",
        "LIMITS: The evidence states only that ExampleRAT launches execution.",
        "PLACEMENT_REASON: Place beside the statement that ExampleRAT launches execution.",
        "EVIDENCE: E001",
    ]
    if placement is not None:
        lines.append(f"PLACEMENT: {placement}")
    if placement == "after_section":
        lines.append(f"SECTION_INDEX: {section_index}")
    lines.extend(
        ["REASON: The source figure clarifies the execution context.", extra, "END FIGURE"]
    )
    return "\n".join(line for line in lines if line)


def test_figure_proposal_selects_local_asset_with_the_model_written_caption() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    inventory = _figure_inventory(extraction)
    catalog = build_editorial_figure_catalog(extraction, inventory)
    model_caption = "Architecture diagram for ExampleRAT execution"
    parsed = parse_editorial_enrichment_proposal_wire(
        _figure_wire(caption=model_caption), pack, figure_catalog=catalog
    )

    assert parsed.proposal is not None
    enrichment = validate_editorial_enrichment_proposal(
        parsed.proposal, pack, extraction, synthesis, figure_catalog=catalog
    )

    selected = enrichment.source_figures[0]
    assert selected.inclusion_status.value == "included"
    assert selected.caption == model_caption
    assert selected.resolved_figure is not None
    assert selected.resolved_figure.sha256 == "f" * 64
    assert selected.resolved_figure.blob_id == UUID(int=601)
    assert selected.provenance == inventory.figures[0].provenance
    assert "editorial_enrichment_figure_caption_downgraded:F001" not in enrichment.warnings
    trace = enrichment.figure_decisions[0]
    assert trace.decision.value == "included_by_model"
    assert trace.actor.value == "model_proposal"
    assert trace.reason == "The source figure clarifies the execution context."
    assert trace.evidence_refs == (pack.resolve_handle("E001"),)


@pytest.mark.parametrize(
    ("wire", "decision", "reason"),
    [
        (
            _figure_wire(handle="F999"),
            SourceFigureDecision.ACCEPTED,
            "editorial_enrichment_unknown_figure_handle",
        ),
        (
            _figure_wire(),
            SourceFigureDecision.REJECTED,
            "editorial_enrichment_figure_excluded_by_rule",
        ),
        (
            _figure_wire(placement=None),
            SourceFigureDecision.ACCEPTED,
            "editorial_enrichment_figure_placement_missing",
        ),
        (
            _figure_wire(section_index=9),
            SourceFigureDecision.ACCEPTED,
            "editorial_enrichment_figure_placement_anchor_unknown",
        ),
    ],
)
def test_figure_wire_rejects_unknown_excluded_or_unplaced_candidates(
    wire: str, decision: SourceFigureDecision, reason: str
) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    catalog = build_editorial_figure_catalog(
        extraction, _figure_inventory(extraction, decision=decision)
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack, figure_catalog=catalog)

    assert parsed.proposal is None
    assert reason in {item.reason_code for item in parsed.rejections}


def test_figure_evidence_from_another_source_is_rejected() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    original_ref = pack.resolve_handle("E001")
    wrong_source_ref = replace(
        original_ref,
        source_document_id=UUID("90000000-0000-0000-0000-000000000009"),
    )
    wrong_source_pack = replace(
        pack,
        _handle_to_ref={**pack._handle_to_ref, "E002": wrong_source_ref},
    )
    catalog = build_editorial_figure_catalog(extraction, _figure_inventory(extraction))

    parsed = parse_editorial_enrichment_proposal_wire(
        _figure_wire().replace("EVIDENCE: E001", "EVIDENCE: E002"),
        wrong_source_pack,
        figure_catalog=catalog,
    )

    assert parsed.proposal is None
    assert "editorial_enrichment_figure_evidence_source_mismatch" in {
        item.reason_code for item in parsed.rejections
    }


def test_figure_caption_falls_back_to_source_caption_then_alt_text() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    source_caption_inventory = _figure_inventory(
        extraction,
        source_caption="Original caption from the article",
        alt_text="Useful alt description",
    )
    source_catalog = build_editorial_figure_catalog(extraction, source_caption_inventory)
    no_model_caption = _figure_wire().replace("CAPTION: ExampleRAT execution architecture\n", "")

    source_fallback = parse_editorial_enrichment_proposal_wire(
        no_model_caption, pack, figure_catalog=source_catalog
    )
    assert source_fallback.proposal is not None
    source_enrichment = validate_editorial_enrichment_proposal(
        source_fallback.proposal,
        pack,
        extraction,
        synthesis,
        figure_catalog=source_catalog,
    )
    assert source_enrichment.source_figures[0].caption == "Original caption from the article"

    alt_inventory = _figure_inventory(
        extraction, source_caption=None, alt_text="Useful alt description"
    )
    alt_catalog = build_editorial_figure_catalog(extraction, alt_inventory)
    alt_fallback = parse_editorial_enrichment_proposal_wire(
        no_model_caption, pack, figure_catalog=alt_catalog
    )
    assert alt_fallback.proposal is not None
    alt_enrichment = validate_editorial_enrichment_proposal(
        alt_fallback.proposal, pack, extraction, synthesis, figure_catalog=alt_catalog
    )
    assert alt_enrichment.source_figures[0].caption == "Useful alt description"


def test_figure_without_any_caption_fallback_is_not_published() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    catalog = build_editorial_figure_catalog(
        extraction,
        _figure_inventory(extraction, source_caption=None, alt_text=None),
    )

    parsed = parse_editorial_enrichment_proposal_wire(
        _figure_wire().replace("CAPTION: ExampleRAT execution architecture\n", ""),
        pack,
        figure_catalog=catalog,
    )

    assert parsed.proposal is None
    assert "editorial_enrichment_figure_caption_missing" in {
        item.reason_code for item in parsed.rejections
    }


def test_model_cannot_replace_figure_handle_with_an_image_url() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    catalog = build_editorial_figure_catalog(extraction, _figure_inventory(extraction))

    parsed = parse_editorial_enrichment_proposal_wire(
        _figure_wire(extra="IMAGE_URL: https://invented.example/image.png"),
        pack,
        figure_catalog=catalog,
    )

    assert parsed.proposal is None
    assert "editorial_enrichment_unknown_field" in {item.reason_code for item in parsed.rejections}


def test_two_catalog_figures_allow_only_the_relevant_one_to_be_selected() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    inventory = _figure_inventory(extraction, figure_label="BlueMoon exploitation chain")
    first = inventory.accepted[0]
    second = first.model_copy(
        update={
            "figure_id": UUID(int=702),
            "locator": replace(first.locator, figure_label="Unrelated marketing banner"),
        }
    )
    metadata = inventory.catalog_metadata[first.figure_id]
    inventory = replace(
        inventory,
        figures=(first, second),
        catalog_metadata={first.figure_id: metadata, second.figure_id: metadata},
    )
    catalog = build_editorial_figure_catalog(extraction, inventory)
    wire = _figure_wire(
        caption="Chaîne d'exploitation BlueMoon, du leurre aux charges post-exploitation."
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack, figure_catalog=catalog)

    assert parsed.proposal is not None
    enrichment = validate_editorial_enrichment_proposal(
        parsed.proposal, pack, extraction, synthesis, figure_catalog=catalog
    )
    assert len(enrichment.source_figures) == 1
    assert enrichment.source_figures[0].resolved_figure == first
    assert enrichment.figure_decisions[1].decision.value == "not_selected_by_model"


def test_empty_figure_response_is_valid_and_model_cannot_supply_hash_or_image() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    catalog = build_editorial_figure_catalog(extraction, _figure_inventory(extraction))

    empty = parse_editorial_enrichment_proposal_wire(
        "NO USEFUL ENRICHMENT", pack, figure_catalog=catalog
    )
    altered = parse_editorial_enrichment_proposal_wire(
        _figure_wire(extra="SHA256: " + "0" * 64), pack, figure_catalog=catalog
    )

    assert empty.proposal is not None and empty.proposal.figures == ()
    assert altered.proposal is None
    assert "editorial_enrichment_unknown_field" in {item.reason_code for item in altered.rejections}
    enrichment = validate_editorial_enrichment_proposal(
        empty.proposal, pack, extraction, synthesis, figure_catalog=catalog
    )
    assert "editorial_enrichment_no_figure_selected" in enrichment.warnings


def test_figure_selection_accepts_at_most_three_figures() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    base_inventory = _figure_inventory(extraction)
    template = base_inventory.accepted[0]
    figures = tuple(
        template.model_copy(
            update={
                "figure_id": UUID(int=700 + index),
                "locator": replace(template.locator, figure_label=f"Candidate {index}"),
            }
        )
        for index in range(9)
    )
    inventory = replace(
        base_inventory,
        figures=figures,
        catalog_metadata={
            figure.figure_id: base_inventory.catalog_metadata[template.figure_id]
            for figure in figures
        },
    )
    catalog = build_editorial_figure_catalog(extraction, inventory)

    over_limit = "\n\n".join(
        _figure_wire(
            handle=f"F{index:03d}",
            purpose_question=f"Which documented execution detail does figure {index} show?",
        ).replace("FIGURE P001", f"FIGURE P{index:03d}")
        for index in range(1, 5)
    )
    parsed = parse_editorial_enrichment_proposal_wire(over_limit, pack, figure_catalog=catalog)
    assert parsed.proposal is not None
    assert len(parsed.proposal.figures) == 3
    assert "editorial_enrichment_figure_limit_exceeded" in {
        item.reason_code for item in parsed.rejections
    }


def test_annotation_wire_blocks_validate_anchor_and_segment_then_persist() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = (
        "ANNOTATION A001\n"
        "CATEGORY: malware\n"
        "PARAGRAPH_ANCHOR: section:0:paragraph:0001\n"
        "EXACT_TEXT: ExampleRAT\n"
        "END ANNOTATION"
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)
    assert parsed.proposal is not None
    assert parsed.proposal.annotations == (
        AnnotationProposalV1(
            role=SemanticRole.MALWARE,
            paragraph_anchor="section:0:paragraph:0001",
            text="ExampleRAT",
        ),
    )
    enrichment = validate_editorial_enrichment_proposal(
        parsed.proposal, pack, extraction, synthesis
    )
    assert enrichment.annotations[0].text == "ExampleRAT"
    assert enrichment.schema_version == 6
    assert editorial_enrichment_from_json(editorial_enrichment_to_json(enrichment)) == enrichment


@pytest.mark.parametrize(
    ("anchor", "segment", "reason"),
    [
        (
            "lead:0001",
            "ExampleRAT",
            "editorial_enrichment_annotation_anchor_unknown",
        ),
        (
            "section:0:paragraph:0001",
            "MissingFamily",
            "editorial_enrichment_annotation_text_not_found",
        ),
    ],
)
def test_annotation_wire_rejects_wrong_anchor_or_missing_exact_segment(
    anchor: str, segment: str, reason: str
) -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = (
        "ANNOTATION A001\nCATEGORY: malware\n"
        f"PARAGRAPH_ANCHOR: {anchor}\nEXACT_TEXT: {segment}\nEND ANNOTATION"
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is None
    assert [item.reason_code for item in parsed.rejections] == [reason]


def test_annotation_wire_rejects_unknown_category_with_reason_code() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    wire = (
        "ANNOTATION A001\nCATEGORY: typst\n"
        "PARAGRAPH_ANCHOR: section:0:paragraph:0001\n"
        "EXACT_TEXT: ExampleRAT\nEND ANNOTATION"
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is None
    assert [item.reason_code for item in parsed.rejections] == [
        "editorial_enrichment_annotation_category_unknown"
    ]


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
                "purpose": {
                    "question": "Which infrastructure indicator is recorded?",
                    "available_data": "One infrastructure observation is present.",
                    "comprehension_gain": "The row pairs the literal and its role.",
                    "scope": "Only the indicator cited by this row.",
                    "evidence_handles": [example_handle],
                    "knowledge_limits": "No additional indicator details are stated.",
                    "placement_reason": "Place with the paragraph introducing infrastructure.",
                },
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
    inventory = _figure_inventory(extraction)
    figure_catalog = build_editorial_figure_catalog(extraction, inventory)
    request = build_editorial_enrichment_model_request(
        run,
        snapshot,
        extraction,
        synthesis,
        pack,
        access_policy,
        figure_catalog=figure_catalog,
    )

    # An accepted catalog figure makes the model look at the live article.
    assert request.web_search is any(
        entry.figure.decision.value == "accepted" for entry in figure_catalog
    )
    assert request.background is False
    assert request.conversation is None
    assert request.routing_hint.value == "editorial_enrichment"
    assert request.prompt_template_version == EDITORIAL_ENRICHMENT_PROMPT_VERSION
    assert request.run_id == editorial_enrichment_model_run_id(
        run, request.metadata["editorial_enrichment_invocation_hash"]
    )
    assert str(source.id) not in request.text
    assert str(extraction.sources[0].source_document_id) not in request.text
    # Each prompt entry carries the page, safe asset path, context, dimensions,
    # and only evidence handles belonging to that source.
    assert request.text.count(extraction.sources[0].canonical_url) == len(figure_catalog)
    assert "F001" in request.text
    assert '"asset_url":"https://cdn.example.test/example-rat.png"' in request.text
    assert '"source_caption":"ExampleRAT execution architecture"' in request.text
    assert '"evidence":["E001"]' in request.text
    assert '"width":640' in request.text and '"height":400' in request.text
    assert "ExampleRAT execution architecture" in request.text
    assert "blob_id" not in request.text
    assert "RELATION_TYPE: factual | inference | comparison" in request.text
    assert EDITORIAL_ENRICHMENT_GENERATOR_VERSION == "model-text-blocks-v6-dedicated-annotations"
    assert EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION == (
        "editorial-enrichment-block-contract-v9-analytic-media-arbitration"
    )

    assert "sélectionne zéro ou une image" in request.text
    assert "capture de code ou artefact directement informative" in request.text
    assert "image d'un article adjacent" in request.text
    assert "SOURCE_PAGE_URL" in request.text and "ASSET_URL" in request.text

    revision_request = ProductionEditorialEnrichmentRevisionService._model_request(
        request_identity="c" * 64,
        request_identity_payload={"base_input_hash": "d" * 64},
        action=EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE,
        instruction="Choose a clearer figure.",
        element_kind=EditorialEnrichmentElementKind.FIGURE,
        element_key="F001",
        base_element={
            "key": "F001",
            "source_document_id": str(source.id),
            "source_url": extraction.sources[0].canonical_url,
            "caption": "Evidence from https://cdn.example.test/example-rat.png",
            "locator": {"original_asset_url": "https://cdn.example.test/example-rat.png"},
            "resolved_figure": {"figure_id": str(figure_catalog[0].figure.figure_id)},
        },
        element_evidence_handles=[],
        evidence_pack=pack,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(pack),
        figure_catalog=figure_catalog,
        access_policy=access_policy,
    )
    assert revision_request.prompt_template_version == EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
    assert str(source.id) not in revision_request.text
    assert extraction.sources[0].canonical_url not in revision_request.text
    assert "https://cdn.example.test/example-rat.png" not in revision_request.text


def test_functional_hash_and_model_run_identity_bind_policy_generation_and_pack() -> None:
    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    invocation_left = compute_editorial_enrichment_invocation_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash="1" * 64,
        access_policy_hash="2" * 64,
    )
    invocation_right = compute_editorial_enrichment_invocation_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash="3" * 64,
        access_policy_hash="2" * 64,
    )

    assert invocation_left != invocation_right
    assert editorial_enrichment_model_run_id(
        run, invocation_left
    ) != editorial_enrichment_model_run_id(run, invocation_right)
    assert editorial_enrichment_model_run_id(
        run, invocation_left
    ) == editorial_enrichment_model_run_id(run, invocation_left)
    assert EditorialEnrichmentExecutionStatus.NEEDS_REVIEW.value == "needs_review"


def test_parser_version_changes_artifact_identity_but_not_invocation_identity() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    common = {
        "extraction": extraction,
        "synthesis": synthesis,
        "evidence_pack_hash": "1" * 64,
        "access_policy_hash": "2" * 64,
    }

    invocation_v1 = compute_editorial_enrichment_invocation_hash(**common)
    invocation_v2 = compute_editorial_enrichment_invocation_hash(
        **common, prompt_version=EDITORIAL_ENRICHMENT_PROMPT_VERSION
    )
    artifact_v1 = compute_editorial_enrichment_input_hash(**common, parser_version="wire-v1")
    artifact_v2 = compute_editorial_enrichment_input_hash(**common, parser_version="wire-v2")

    assert invocation_v1 == invocation_v2
    assert artifact_v1 != artifact_v2


def test_resource_search_setting_changes_artifact_identity_without_changing_primary_call() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    common = {
        "extraction": extraction,
        "synthesis": synthesis,
        "evidence_pack_hash": "1" * 64,
        "access_policy_hash": "2" * 64,
    }

    primary_call = compute_editorial_enrichment_invocation_hash(**common)
    off = compute_editorial_enrichment_input_hash(**common, resource_search_enabled=False)
    on = compute_editorial_enrichment_input_hash(**common, resource_search_enabled=True)

    assert off != on
    assert primary_call == compute_editorial_enrichment_invocation_hash(**common)


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
        self.payloads: dict[UUID, dict[str, object] | bytes] = {}

    def put(self, payload: dict[str, object]) -> UUID:
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        payload = self.payloads[blob_id]
        if isinstance(payload, bytes):
            value = json.loads(payload)
            assert isinstance(value, dict)
            return value
        return payload

    async def read_bytes(self, blob_id: UUID) -> bytes:
        payload = self.payloads[blob_id]
        if isinstance(payload, bytes):
            return payload
        return ProductionArtifactStore.canonical_json_bytes(payload)

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, object] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        raw_id = None
        canonical_id = None
        rendered_id = None
        if raw is not None:
            raw_id = uuid4()
            self.payloads[raw_id] = raw.encode("utf-8")
        if canonical is not None:
            canonical_id = self.put(canonical)
        if rendered is not None:
            rendered_id = uuid4()
            self.payloads[rendered_id] = rendered.encode("utf-8")
        return raw_id, canonical_id, rendered_id


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
            compiler_policy_version="diagram-d2-svg-v3-relation-semantics",
        )


class _Uow:
    def __init__(
        self,
        documents: _MemorySourceDocuments,
        collections: tuple[SourceCollection, ...] = (),
    ) -> None:
        self.source_documents = documents
        self.source_collections = _MemorySourceCollections(collections)
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
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}
        self.diagnostics: list[dict[str, object]] = []

    async def draft(
        self, request: ModelRequest, output_schema: object | None = None
    ) -> ModelExecution:
        self.calls.append((request, output_schema))
        if isinstance(self._responder, Exception):
            raise self._responder
        if request.prompt_template_version == enrichment_module.SEMANTIC_ANNOTATION_PROMPT_VERSION:
            execution = _succeeded_text(request, "NO ANNOTATIONS")
        else:
            execution = self._responder(request)
        if execution.output_text is None:
            return execution
        raw = execution.output_text.encode("utf-8")
        reference = f"model-output://{execution.run.id}"
        execution.run.raw_output_reference = reference
        execution.run.raw_output_sha256 = hashlib.sha256(raw).hexdigest()
        execution.run.raw_output_chars = len(execution.output_text)
        execution.run.output_references = (reference,)
        self.outputs[reference] = raw
        self.runs[execution.run.id] = execution.run
        return execution

    async def get_run(self, run_id: UUID) -> ModelRun | None:
        return self.runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        del max_bytes
        return self.outputs[reference]

    async def archive_output(self, content: bytes, *, mime_type: str) -> str:
        del mime_type
        reference = f"model-normalized://{uuid4()}"
        self.outputs[reference] = content
        return reference

    async def record_output_diagnostics(
        self,
        run_id: UUID,
        *,
        normalized_reference: str | None,
        normalized_sha256: str | None,
        parser_stage: str | None,
        normalization_version: str | None,
        transformations: tuple[str, ...],
        validation_errors: tuple[dict[str, object], ...],
    ) -> None:
        run = self.runs[run_id]
        run.normalized_output_reference = normalized_reference
        run.normalized_output_sha256 = normalized_sha256
        run.parser_stage = parser_stage
        run.normalization_version = normalization_version
        run.transformations = transformations
        run.validation_errors = validation_errors  # type: ignore[assignment]
        self.diagnostics.append(
            {
                "run_id": run_id,
                "parser_stage": parser_stage,
                "normalization_version": normalization_version,
                "transformations": transformations,
                "validation_errors": validation_errors,
            }
        )


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
    output_text = (
        _proposal_to_wire(proposal)
        if isinstance(proposal, EditorialEnrichmentProposalV1)
        else "raw answer"
    )
    return ModelExecution(run=run, output_text=output_text, structured_output=None)


def _succeeded_text(request: ModelRequest, output_text: str) -> ModelExecution:
    run = _model_run(request)
    run.succeed(
        actual_model_version="gpt-5",
        duration_ms=3,
        usage=ModelUsage(total_tokens=7),
        output_references=("model-output://1",),
        response_id=None,
    )
    return ModelExecution(run=run, output_text=output_text, structured_output=None)


def _proposal_to_wire(proposal: EditorialEnrichmentProposalV1) -> str:
    payload = proposal.model_dump(mode="json")
    lines: list[str] = []
    for index, table in enumerate(payload["tables"], start=1):
        lines.extend(
            [
                f"TABLE T{index:03d}",
                f"KEY: {table['key']}",
                f"KIND: {table['kind']}",
                f"TITLE: {table['title']}",
            ]
        )
        purpose = table["purpose"]
        lines.extend(
            [
                f"PURPOSE: {purpose['question']}",
                f"DATA: {purpose['available_data']}",
                f"GAIN: {purpose['comprehension_gain']}",
                f"SCOPE: {purpose['scope']}",
                f"PURPOSE_EVIDENCE: {', '.join(purpose['evidence_handles'])}",
                f"LIMITS: {purpose['knowledge_limits']}",
                f"PLACEMENT_REASON: {purpose['placement_reason']}",
            ]
        )
        if table.get("caption") is not None:
            lines.append(f"CAPTION: {table['caption']}")
        placement = table["placement"]
        lines.append(f"PLACEMENT: {placement['kind']}")
        if placement.get("section_index") is not None:
            lines.append(f"SECTION_INDEX: {placement['section_index']}")
        for column_index, column in enumerate(table["columns"], start=1):
            lines.extend(
                [
                    f"COLUMN C{index:03d}_{column_index:03d}",
                    f"KEY: {column['key']}",
                    f"LABEL: {column['label']}",
                    "END COLUMN",
                ]
            )
        for row_index, row in enumerate(table["rows"], start=1):
            lines.append(f"ROW R{index:03d}_{row_index:03d}")
            lines.extend(f"CELL: {cell}" for cell in row["cells"])
            lines.append(f"EVIDENCE: {', '.join(row['evidence_handles'])}")
            lines.append("END ROW")
        lines.append("END TABLE")
    for index, diagram in enumerate(payload["diagrams"], start=1):
        lines.extend(
            [
                f"DIAGRAM D{index:03d}",
                f"KEY: {diagram['key']}",
                f"KIND: {diagram['kind']}",
                f"TITLE: {diagram['title']}",
            ]
        )
        purpose = diagram["purpose"]
        lines.extend(
            [
                f"PURPOSE: {purpose['question']}",
                f"DATA: {purpose['available_data']}",
                f"GAIN: {purpose['comprehension_gain']}",
                f"SCOPE: {purpose['scope']}",
                f"PURPOSE_EVIDENCE: {', '.join(purpose['evidence_handles'])}",
                f"LIMITS: {purpose['knowledge_limits']}",
                f"PLACEMENT_REASON: {purpose['placement_reason']}",
            ]
        )
        if diagram.get("caption") is not None:
            lines.append(f"CAPTION: {diagram['caption']}")
        lines.append(f"DIRECTION: {diagram['direction']}")
        placement = diagram["placement"]
        lines.append(f"PLACEMENT: {placement['kind']}")
        if placement.get("section_index") is not None:
            lines.append(f"SECTION_INDEX: {placement['section_index']}")
        for node_index, node in enumerate(diagram["nodes"], start=1):
            lines.extend(
                [
                    f"NODE N{index:03d}_{node_index:03d}",
                    f"ID: {node['node_id']}",
                    f"LABEL: {node['label']}",
                    f"ROLE: {node['role']}",
                    f"EVIDENCE: {', '.join(node['evidence_handles'])}",
                    "END NODE",
                ]
            )
        for edge_index, edge in enumerate(diagram["edges"], start=1):
            lines.extend(
                [
                    f"RELATION L{index:03d}_{edge_index:03d}",
                    f"FROM: {edge['source_node_id']}",
                    f"TO: {edge['target_node_id']}",
                    f"RELATION_TYPE: {edge['relation_type']}",
                ]
            )
            if edge.get("label") is not None:
                lines.append(f"LABEL: {edge['label']}")
            lines.extend([f"EVIDENCE: {', '.join(edge['evidence_handles'])}", "END RELATION"])
        for group_index, group in enumerate(diagram["groups"], start=1):
            lines.extend(
                [
                    f"GROUP G{index:03d}_{group_index:03d}",
                    f"ID: {group['group_id']}",
                    f"LABEL: {group['label']}",
                    f"NODES: {', '.join(group['node_ids'])}",
                    "END GROUP",
                ]
            )
        lines.append("END DIAGRAM")
    return "\n".join(lines) if lines else "NO USEFUL ENRICHMENT"


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
    collections: tuple[SourceCollection, ...] = (),
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
        uow_factory=lambda: _Uow(source_documents, collections),  # type: ignore[arg-type,return-value]
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
async def test_bluemoon_archived_chain_is_catalogued_selected_and_published_from_same_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain_caption = "Chaîne d'exploitation BlueMoon, du leurre aux charges post-exploitation."
    world = _world(
        _RecordingGateway(
            lambda request: _succeeded_text(request, _figure_wire(caption=chain_caption))
        )
    )
    article_source = world.extraction.sources[0]
    image_url = "https://cdn.example.test/bluemoon-exploitation-chain.png"
    article_bytes = (
        "<article><h2>BlueMoon exploitation chain</h2>"
        "<p>The lure launches the loader.</p><figure>"
        f'<img src="{image_url}" alt="BlueMoon exploitation chain">'
        "<figcaption>From lure to post-exploitation payloads.</figcaption>"
        "</figure><p>The loader deploys the final payload.</p></article>"
    ).encode()
    archived_source = ArchivedFigureSource(
        source_document_id=article_source.source_document_id,
        source_url=article_source.canonical_url,
        mime_type="text/html",
        blob_id=UUID(int=602),
        sha256=hashlib.sha256(article_bytes).hexdigest(),
        byte_size=len(article_bytes),
        content=article_bytes,
    )
    archived_asset = ArchivedFigureAsset(
        source_document_id=article_source.source_document_id,
        source_url=image_url,
        blob_id=UUID(int=601),
        sha256="f" * 64,
        mime_type="image/png",
        byte_size=4096,
        width=640,
        height=400,
    )
    inventory = SourceFigureInventory().inventory((archived_source,), (archived_asset,))

    async def load_inventory(**_kwargs: object) -> SourceFigureInventoryResult:
        return inventory

    monkeypatch.setattr(
        enrichment_module,
        "load_archived_source_figure_inventory",
        load_inventory,
    )

    async def no_ingest(_inventory: SourceFigureInventoryResult) -> None:
        return None

    monkeypatch.setattr(world.service, "_ingest_source_figures", no_ingest)

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    request = world.gateway.calls[0][0]
    assert "F001" in request.text
    assert request.web_search is True
    assert "BlueMoon exploitation chain" in request.text
    assert image_url in request.text
    assert str(world.extraction.sources[0].source_document_id) not in request.text
    assert result.source_figure_count == 1
    enrichment = world.writer.calls[0]["enrichment"]
    assert len(enrichment.source_figures) == 1  # type: ignore[attr-defined]
    assert enrichment.source_figures[0].inclusion_status.value == "included"  # type: ignore[attr-defined]
    assert enrichment.source_figures[0].resolved_figure.sha256 == "f" * 64  # type: ignore[attr-defined]
    assert enrichment.source_figures[0].resolved_figure.blob_id == UUID(int=601)  # type: ignore[attr-defined]
    metadata = world.writer.calls[0]["metadata_extra"]
    assert metadata["source_figure_provenance_version"] == "source-figure-provenance-v1"  # type: ignore[index]
    diagnostics = metadata["source_figure_provenance_diagnostics"]  # type: ignore[index]
    stages = [item["stage"] for item in diagnostics]  # type: ignore[union-attr]
    assert stages == ["discovered", "archived", "catalogued", "selected"]
    selected = diagnostics[-1]  # type: ignore[index]
    assert selected["blob_id"] == str(UUID(int=601))
    assert selected["sha256"] == "f" * 64
    assert "image_bytes" not in json.dumps(diagnostics)


@pytest.mark.asyncio
async def test_service_drafts_once_statelessly_and_stores_model_provenance() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.model_calls, result.table_count, result.diagram_count) == (2, 1, 1)
    assert result.source_figure_count == 0
    request, schema = world.gateway.calls[0]
    assert schema is None
    assert request.routing_hint is ModelRoutingHint.EDITORIAL_ENRICHMENT
    assert (
        request.run_id
        == result.model_run_id
        == editorial_enrichment_model_run_id(
            world.run, request.metadata["editorial_enrichment_invocation_hash"]
        )
    )
    stored = world.writer.calls[0]
    assert stored["raw_result"] == _proposal_to_wire(_proposal("E001"))
    assert stored["model_run_id"] == result.model_run_id
    assert stored["input_hash"] == result.input_hash
    enrichment = stored["enrichment"]
    assert enrichment.source_figures == ()  # type: ignore[attr-defined]
    assert enrichment.diagrams[0].compiled_asset_id is not None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_failed_diagram_is_reviewed_while_valid_sibling_and_artifact_are_stored() -> None:
    proposal = _proposal("E001")
    sibling_purpose = proposal.diagrams[0].purpose.model_copy(
        update={"question": "Which second documented action follows the initial step?"}
    )
    sibling = proposal.diagrams[0].model_copy(
        update={"key": "sibling_chain", "purpose": sibling_purpose}
    )
    proposal = proposal.model_copy(update={"diagrams": (proposal.diagrams[0], sibling)})
    world = _world(_RecordingGateway(lambda request: _succeeded(request, proposal)))
    successful_compiler = _MemoryDiagramCompiler()

    class Compiler:
        async def compile(self, diagram: object) -> CompiledDiagram:
            if diagram.key == "infection_chain":  # type: ignore[attr-defined]
                raise DiagramCompilerProcessError("D2 rejected this diagram")
            return await successful_compiler.compile(diagram)

    world.service._diagram_compiler = Compiler()

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert result.diagram_count == 1
    assert len(world.writer.calls) == 1
    stored = world.writer.calls[0]["enrichment"]
    assert [item.key for item in stored.diagrams] == ["sibling_chain"]  # type: ignore[attr-defined]
    assert len(stored.tables) == 1  # type: ignore[attr-defined]
    assert (
        "editorial_enrichment_diagram_render_failed:infection_chain:"
        "diagram_compiler_process_failure"
    ) in stored.warnings  # type: ignore[attr-defined]
    assert result.details is not None
    assert result.details["diagram_rejections"] == [
        {
            "diagram_key": "infection_chain",
            "reason_code": "editorial_enrichment_diagram_render_failed",
            "compiler_error_code": "diagram_compiler_process_failure",
        }
    ]


@pytest.mark.asyncio
async def test_all_failed_diagrams_degrade_to_stored_tables_with_review_rejections() -> None:
    proposal = _proposal("E001")
    sibling_purpose = proposal.diagrams[0].purpose.model_copy(
        update={"question": "Which second documented action follows the initial step?"}
    )
    sibling = proposal.diagrams[0].model_copy(
        update={"key": "sibling_chain", "purpose": sibling_purpose}
    )
    proposal = proposal.model_copy(update={"diagrams": (proposal.diagrams[0], sibling)})
    world = _world(_RecordingGateway(lambda request: _succeeded(request, proposal)))

    class Compiler:
        async def compile(self, diagram: object) -> CompiledDiagram:
            raise DiagramCompilerProcessError(
                f"D2 rejected {diagram.key}"  # type: ignore[attr-defined]
            )

    world.service._diagram_compiler = Compiler()

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert result.diagram_count == 0
    assert len(world.writer.calls) == 1
    stored = world.writer.calls[0]["enrichment"]
    assert stored.diagrams == ()  # type: ignore[attr-defined]
    assert len(stored.tables) == 1  # type: ignore[attr-defined]
    assert len(result.details["diagram_rejections"]) == 2  # type: ignore[index]


@pytest.mark.asyncio
async def test_empty_model_decision_is_a_valid_stored_enrichment() -> None:
    empty = EditorialEnrichmentProposalV1()
    world = _world(_RecordingGateway(lambda request: _succeeded(request, empty)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.table_count, result.diagram_count, result.model_calls) == (0, 0, 2)
    assert len(world.writer.calls) == 1


@pytest.mark.asyncio
async def test_resource_need_is_persisted_and_search_stays_off_by_default() -> None:
    needs_wire = (
        "NEEDS N001\nKIND: MEDIA\n"
        "REASON: No relevant execution figure is archived.\n"
        "QUERY_HINT: Example incident ExampleRAT execution diagram\nEND NEEDS"
    )
    world = _world(_RecordingGateway(lambda request: _succeeded_text(request, needs_wire)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert world.service._resource_search_enabled is False
    assert len(world.gateway.calls) == 2
    enrichment = world.writer.calls[0]["enrichment"]
    assert len(enrichment.resource_needs) == 1  # type: ignore[attr-defined]
    assert enrichment.resource_needs[0].kind.value == "MEDIA"  # type: ignore[attr-defined]
    assert enrichment.resource_proposals == ()  # type: ignore[attr-defined]
    assert result.details["resource_proposal_search"]["status"] == (  # type: ignore[index]
        "disabled_by_configuration"
    )


@pytest.mark.asyncio
async def test_resource_search_opt_in_uses_separate_call_and_persists_proposals_only() -> None:
    needs_wire = (
        "NEEDS N001\nKIND: TECHNICAL_ANALYSIS\n"
        "REASON: A protocol field explanation is missing.\n"
        "QUERY_HINT: Example incident protocol field analysis\nEND NEEDS"
    )
    resource_wire = (
        "RESOURCE R001\nNEED: N001\nURL: https://vendor.example/report\n"
        "JUSTIFICATION: The page may explain the named protocol field.\nEND RESOURCE"
    )

    def respond(request: ModelRequest) -> ModelExecution:
        return _succeeded_text(request, resource_wire if request.web_search else needs_wire)

    world = _world(_RecordingGateway(respond))
    world.service._resource_search_enabled = True

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert result.model_calls == 3
    assert [request.web_search for request, _schema in world.gateway.calls] == [False, True, False]
    assert all(request.conversation is None for request, _schema in world.gateway.calls)
    enrichment = world.writer.calls[0]["enrichment"]
    assert len(enrichment.resource_needs) == 1  # type: ignore[attr-defined]
    assert len(enrichment.resource_proposals) == 1  # type: ignore[attr-defined]
    candidate = enrichment.resource_proposals[0]  # type: ignore[attr-defined]
    assert candidate.url == "https://vendor.example/report"
    assert candidate.source_model_run_id == world.gateway.calls[1][0].run_id
    assert enrichment.source_figures == ()  # type: ignore[attr-defined]
    assert result.details["resource_proposals"][0]["url"] == candidate.url  # type: ignore[index]
    assert world.gateway.diagnostics[-1]["parser_stage"] == "editorial_resource_proposals"


@pytest.mark.asyncio
async def test_document_access_policy_blocks_enrichment_before_gateway() -> None:
    needs_wire = (
        "NEEDS N001\nKIND: MEDIA\nREASON: Missing figure.\n"
        "QUERY_HINT: Example incident source media\nEND NEEDS"
    )
    world = _world(
        _RecordingGateway(lambda request: _succeeded_text(request, needs_wire)),
        documents=(replace(_document(), external_llm_allowed=False),),
    )
    world.service._resource_search_enabled = True

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == EditorialEnrichmentStageErrorCode.POLICY_BLOCKED
    assert result.model_calls == 0
    assert world.gateway.calls == []


@pytest.mark.asyncio
async def test_collection_access_policy_blocks_enrichment_before_gateway() -> None:
    collection = SourceCollection(
        subject_id=_SUBJECT_ID,
        edition_id=uuid4(),
        requested_url="https://restricted.example/report",
        proposed_role=SourceRole.PRIMARY,
        external_llm_allowed=False,
    )
    document = replace(_document(), source_collection_id=collection.id)
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _world(gateway, documents=(document,), collections=(collection,))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == EditorialEnrichmentStageErrorCode.POLICY_BLOCKED
    assert result.model_calls == 0
    assert world.gateway.calls == []


@pytest.mark.asyncio
async def test_dirty_text_blocks_keep_valid_items_and_report_local_rejections() -> None:
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace(
        "END TABLE",
        "ROW R_BAD\nCELL: malformed row\nEVIDENCE: E001\nEND ROW\nEND TABLE",
        1,
    )
    wire = wire.replace(
        "END DIAGRAM",
        "GROUP G_VALID\nID: stage\nLABEL: Execution stage\nNODES: malware, execution\n"
        "END GROUP\nRELATION L_UNKNOWN\nFROM: malware\nTO: missing\n"
        "RELATION_TYPE: factual\nLABEL: leads to\n"
        "EVIDENCE: E001\nEND RELATION\nEND DIAGRAM",
        1,
    )
    wire = wire.replace("CELL: ExampleRAT", 'CELL: "ExampleRAT"', 1)
    wire = wire.replace("EVIDENCE: E001", "EVIDENCE: E001【cite:turn2】", 1)
    polluted = f"```text\n{wire}\n```"
    world = _world(
        _RecordingGateway(
            lambda request: replace(_succeeded(request, _proposal("E001")), output_text=polluted)
        )
    )

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert result.table_count == result.diagram_count == 1
    assert result.details is not None
    rejections = result.details["rejections"]
    assert {item["reason_code"] for item in rejections} == {
        "editorial_enrichment_table_row_column_count_mismatch",
        "editorial_enrichment_diagram_relation_unknown_node",
    }
    enrichment = world.writer.calls[0]["enrichment"]
    assert len(enrichment.tables[0].rows) == 1  # type: ignore[attr-defined]
    assert enrichment.tables[0].rows[0].cells[0] == '"ExampleRAT"'  # type: ignore[attr-defined]
    assert len(enrichment.diagrams[0].edges) == 1  # type: ignore[attr-defined]
    assert len(enrichment.diagrams[0].groups) == 1  # type: ignore[attr-defined]
    assert enrichment.tables[0].placement.kind is EnrichmentPlacementKind.AFTER_LEAD  # type: ignore[attr-defined]
    assert enrichment.diagrams[0].placement.section_index == 0  # type: ignore[attr-defined]
    assert world.writer.calls[0]["raw_result"] == polluted
    run = world.gateway.runs[result.model_run_id]
    assert "bridge_ui_markers_removed" in run.transformations
    assert "markdown_fences_removed" in run.transformations
    assert {item["code"] for item in run.validation_errors} == {
        "editorial_enrichment_table_row_column_count_mismatch",
        "editorial_enrichment_diagram_relation_unknown_node",
    }


@pytest.mark.asyncio
async def test_unintelligible_nonempty_response_needs_review() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, None)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert result.error_code == EditorialEnrichmentStageErrorCode.OUTPUT_INVALID.value
    assert result.details is not None
    assert result.details["wire_error_code"] == "editorial_enrichment_unintelligible_response"
    assert result.model_calls == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_parser_version_bump_reparses_archived_output_without_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))
    first = await _execute(world)
    first_enrichment = world.writer.calls[0]["enrichment"]
    original_identity = world.gateway.runs[first.model_run_id].normalization_version
    original_normalized_sha256 = world.gateway.runs[first.model_run_id].normalized_output_sha256

    monkeypatch.setattr(
        enrichment_module,
        "EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION",
        "editorial-enrichment-wire-v2",
    )
    second = await _execute(world)

    assert first.status is second.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert second.model_calls == 0
    assert len(world.gateway.calls) == 2
    assert second.model_run_id == first.model_run_id
    assert second.input_hash != first.input_hash
    reparsed = world.writer.calls[1]["enrichment"]
    assert reparsed.tables == first_enrichment.tables  # type: ignore[attr-defined]
    assert tuple(
        replace(item, compiled_asset_id=None)
        for item in reparsed.diagrams  # type: ignore[attr-defined]
    ) == tuple(
        replace(item, compiled_asset_id=None)
        for item in first_enrichment.diagrams  # type: ignore[attr-defined]
    )
    assert world.gateway.runs[second.model_run_id].normalization_version != original_identity
    assert (
        world.gateway.runs[second.model_run_id].normalized_output_sha256
        == original_normalized_sha256
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_name", "version_value"),
    (
        ("EDITORIAL_ENRICHMENT_PROMPT_VERSION", "editorial-enrichment-text-blocks-v9-test"),
        (
            "EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION",
            "editorial-enrichment-block-contract-v6-test",
        ),
    ),
)
async def test_prompt_or_contract_version_bump_uses_a_new_model_invocation(
    monkeypatch: pytest.MonkeyPatch, version_name: str, version_value: str
) -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))
    first = await _execute(world)
    monkeypatch.setattr(enrichment_module, version_name, version_value)

    second = await _execute(world)

    assert second.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert len(world.gateway.calls) == 3
    assert second.model_calls == 1
    assert second.input_hash != first.input_hash
    assert second.model_run_id != first.model_run_id


@pytest.mark.asyncio
async def test_restart_resumes_from_verified_raw_output_with_identical_result() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))
    first = await _execute(world)
    first_enrichment = world.writer.calls[0]["enrichment"]
    world.service = ProductionEditorialEnrichmentService(
        uow_factory=world.service._uow_factory,  # type: ignore[attr-defined,arg-type]
        artifact_store=world.store,  # type: ignore[arg-type]
        model_gateway=world.gateway,  # type: ignore[arg-type]
        editorial_enrichment_service=world.writer,  # type: ignore[arg-type]
        artifact_reuse=world.service._artifact_reuse,  # type: ignore[attr-defined,arg-type]
        media_asset_store=world.service._media_asset_store,  # type: ignore[attr-defined,arg-type]
        diagram_compiler=world.service._diagram_compiler,  # type: ignore[attr-defined,arg-type]
    )

    resumed = await _execute(world)

    assert resumed.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert resumed.model_calls == 0
    assert len(world.gateway.calls) == 2
    assert resumed.model_run_id == first.model_run_id
    resumed_enrichment = world.writer.calls[1]["enrichment"]
    assert resumed_enrichment.tables == first_enrichment.tables  # type: ignore[attr-defined]
    assert tuple(
        replace(item, compiled_asset_id=None)
        for item in resumed_enrichment.diagrams  # type: ignore[attr-defined]
    ) == tuple(
        replace(item, compiled_asset_id=None)
        for item in first_enrichment.diagrams  # type: ignore[attr-defined]
    )
    assert world.writer.calls[1]["raw_result"] == world.writer.calls[0]["raw_result"]


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
        EditorialEnrichmentStageErrorCode.EMPTY_AFTER_REJECTIONS.value,
    }
    assert result.model_calls == (2 if failure == "unknown_handle" else 1)
    assert world.writer.calls == []
    if failure == "unknown_handle":
        assert result.details is not None
        assert any(
            item["reason_code"] == "editorial_enrichment_unknown_evidence_handle"
            for item in result.details["rejections"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["duplicate_table_keys", "table_key_equals_diagram_key"])
async def test_key_collision_rejects_only_the_conflicting_wire_block(
    collision: str,
) -> None:
    proposal = EditorialEnrichmentProposalV1.model_validate(_colliding_proposal(collision))
    world = _world(_RecordingGateway(lambda request: _succeeded(request, proposal)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert result.details is not None
    assert any(
        item["reason_code"] == "editorial_enrichment_duplicate_key"
        for item in result.details["rejections"]
    )
    # One conflicting table is rejected and receives the bounded repair attempt.
    assert result.model_calls == 3
    assert len(world.writer.calls) == 1


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


class _RevisionArtifactRepository:
    def __init__(self, artifacts: list[ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.artifacts if item.id == artifact_id), None)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.artifacts
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version, default=None)

    async def get_current_for_revision(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        return await self.get_current(run_id, stage)

    async def list_for_run(self, run_id: UUID) -> tuple[ProductionArtifact, ...]:
        return tuple(item for item in self.artifacts if item.production_run_id == run_id)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.artifacts.append(artifact)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        if stage != ProductionArtifactStage.EDITORIAL_ENRICHMENT.value:
            return
        for item in self.artifacts:
            if (
                item.production_run_id == run_id
                and item.stage is ProductionArtifactStage.PUBLICATION
            ):
                item.status = ProductionArtifactStatus.STALE

    async def find_reusable(self, **_kwargs: object) -> None:
        return None


class _RevisionUow:
    def __init__(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        source_documents: _MemorySourceDocuments,
        artifacts: _RevisionArtifactRepository,
        source_collections: _MemorySourceCollections,
    ) -> None:
        self.production_runs = SimpleNamespace(get_current_for_subject=self._current_run)
        self.production_input_snapshots = SimpleNamespace(get_by_run=self._snapshot_for_run)
        self.source_documents = source_documents
        self.source_collections = source_collections
        self.blobs = _MemoryBlobs()
        self.production_artifacts = artifacts
        self._run = run
        self._snapshot = snapshot

    async def _current_run(self, subject_id: UUID) -> ProductionRun | None:
        return self._run if self._run.subject_id == subject_id else None

    async def _snapshot_for_run(self, run_id: UUID) -> ProductionInputSnapshot | None:
        return self._snapshot if self._snapshot.production_run_id == run_id else None

    async def __aenter__(self) -> _RevisionUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def _revision_harness(
    monkeypatch: pytest.MonkeyPatch,
    gateway: _RecordingGateway,
    *,
    collection: SourceCollection | None = None,
) -> SimpleNamespace:
    from cti_app.application.publication_assembly import PublicationAssemblyService

    world = _world(gateway)
    inventory = _figure_inventory(world.extraction)

    async def load_inventory(**_kwargs: object) -> SourceFigureInventoryResult:
        return inventory

    monkeypatch.setattr(revision_module, "load_archived_source_figure_inventory", load_inventory)
    monkeypatch.setattr(
        revision_module,
        "production_reference_corpus_from_json",
        lambda _payload: SimpleNamespace(),
    )

    document = _document()
    if collection is not None:
        document = replace(document, source_collection_id=collection.id)
    source_documents = _MemorySourceDocuments((document,))
    source_collections = _MemorySourceCollections((collection,) if collection is not None else ())
    policy = await build_synthesis_access_policy(
        world.snapshot, world.extraction, source_documents, source_collections
    )
    evidence_pack = build_editorial_enrichment_evidence_pack(
        world.snapshot, world.extraction, world.synthesis
    )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    access_hash = synthesis_access_policy_hash(policy)
    inventory_hash = inventory.functional_hash()
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=world.extraction,
        synthesis=world.synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_hash,
        source_figure_inventory_hash=inventory_hash,
    )
    enrichment = validate_editorial_enrichment_proposal(
        _proposal("E001"), evidence_pack, world.extraction, world.synthesis
    )
    base_payload = editorial_enrichment_to_json(enrichment)
    base = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash=input_hash,
        canonical_blob_id=world.store.put(base_payload),
        metadata={
            "evidence_pack_hash": evidence_pack_hash,
            "access_policy_hash": access_hash,
            "source_figure_inventory_hash": inventory_hash,
            "routing_policy_version": "routing-v1",
        },
    )
    reference_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="a" * 64,
        canonical_blob_id=world.store.put({"schema_version": 1}),
    )
    publication_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="b" * 64,
        canonical_blob_id=world.store.put({"schema_version": 5}),
        metadata={
            "input_artifacts": {
                "editorial_enrichment_artifact_id": str(base.id),
            }
        },
    )
    artifacts = _RevisionArtifactRepository(
        [
            reference_artifact,
            world.extraction_artifact,
            world.synthesis_artifact,
            base,
            publication_artifact,
        ]
    )

    def uow_factory() -> _RevisionUow:
        return _RevisionUow(
            run=world.run,
            snapshot=world.snapshot,
            source_documents=source_documents,
            artifacts=artifacts,
            source_collections=source_collections,
        )

    world.service._uow_factory = uow_factory  # type: ignore[assignment]
    assembled: list[dict[str, Any]] = []
    rendered: list[UUID] = []

    async def fake_assemble(
        assembly: Any,
        *,
        run: ProductionRun,
        metadata_extra: dict[str, Any],
        **_kwargs: Any,
    ) -> ProductionArtifact:
        del assembly
        assembled.append(metadata_extra)
        existing = await artifacts.list_for_run(run.id)
        publication = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=max(
                (
                    item.version
                    for item in existing
                    if item.stage is ProductionArtifactStage.PUBLICATION
                ),
                default=0,
            )
            + 1,
            input_hash="c" * 64,
            canonical_blob_id=world.store.put({"schema_version": 5}),
            metadata=metadata_extra,
        )
        await artifacts.append(publication)
        return publication

    async def render_preview(artifact_id: UUID) -> None:
        rendered.append(artifact_id)

    monkeypatch.setattr(PublicationAssemblyService, "assemble_publication", fake_assemble)
    service = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=world.service,
        persistence_service=EditorialEnrichmentService(uow_factory, world.store),
        publication_render_service=SimpleNamespace(render_preview=render_preview),  # type: ignore[arg-type]
    )
    return SimpleNamespace(
        world=world,
        service=service,
        artifacts=artifacts,
        base=base,
        base_payload=base_payload,
        base_sha256=hashlib.sha256(
            await world.store.read_bytes(base.canonical_blob_id)
        ).hexdigest(),
        input_hash=input_hash,
        assembled=assembled,
        rendered=rendered,
    )


def _revision_args(harness: SimpleNamespace) -> dict[str, Any]:
    return {
        "subject_id": harness.world.snapshot.subject_id,
        "base_artifact_id": harness.base.id,
        "base_version": harness.base.version,
        "base_input_hash": harness.input_hash,
        "base_canonical_sha256": harness.base_sha256,
        "element_kind": EditorialEnrichmentElementKind.TABLE,
        "element_key": "tools_table",
        "action": EditorialEnrichmentRevisionAction.IMPROVE_TABLE,
        "instruction": "Improve readability without adding facts.",
    }


def _table_revision_wire() -> str:
    proposal = _proposal("E001")
    table = proposal.tables[0].model_copy(update={"title": "Documented tools"})
    targeted = proposal.model_copy(update={"tables": (table,), "diagrams": ()})
    return _proposal_to_wire(targeted)


@pytest.mark.asyncio
async def test_element_revision_appends_one_version_carries_other_elements_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )

    result = await world.service.revise(**_revision_args(world))
    repeated = await world.service.revise(**_revision_args(world))

    assert result.outcome is EditorialEnrichmentRevisionOutcome.REVISED
    assert result.artifact.version == 2
    assert result.artifact.id == repeated.artifact.id
    assert len(world.world.gateway.calls) == 1
    request = world.world.gateway.calls[0][0]
    assert request.prompt_template_id == "production-editorial-enrichment"
    assert request.prompt_template_version == EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
    assert request.parameters["contract_version"].startswith(
        "editorial-enrichment-revision-contract-"
    )
    revision_identity_payload = request.metadata["revision_request"]
    assert revision_identity_payload["revision_prompt_version"] == (
        EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
    )
    assert (
        revision_identity_payload["revision_contract_version"]
        == (request.parameters["contract_version"])
    )
    assert (
        request.metadata["editorial_revision_request_identity"]
        == hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(revision_identity_payload)
        ).hexdigest()
    )
    assert request.web_search is False
    assert len(world.assembled) == len(world.rendered) == 1
    assert result.revision["element_before"] != result.revision["element_after"]
    old_content = await world.world.store.read_bytes(world.base.canonical_blob_id)
    assert hashlib.sha256(old_content).hexdigest() == world.base_sha256
    new_content = await world.world.store.read_json(result.artifact.canonical_blob_id)
    assert new_content["diagrams"] == world.base_payload["diagrams"]  # type: ignore[index]
    assert (
        result.artifact.metadata["evidence_pack_hash"] == world.base.metadata["evidence_pack_hash"]
    )
    assert result.previous_publication_artifact_id is not None


@pytest.mark.asyncio
async def test_collection_access_policy_blocks_revision_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = SourceCollection(
        subject_id=_SUBJECT_ID,
        edition_id=uuid4(),
        requested_url="https://restricted.example/report",
        proposed_role=SourceRole.PRIMARY,
        external_llm_allowed=False,
    )
    world = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
        collection=collection,
    )

    with pytest.raises(ValueError, match="editorial_enrichment_revision_policy_blocked"):
        await world.service.revise(**_revision_args(world))

    assert world.world.gateway.calls == []


@pytest.mark.asyncio
async def test_stale_base_and_changed_hash_are_typed_conflicts_without_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    newer = ProductionArtifact(
        production_run_id=stale.world.run.id,
        subject_id=stale.world.snapshot.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash=stale.input_hash,
        canonical_blob_id=stale.base.canonical_blob_id,
    )
    stale.artifacts.artifacts.append(newer)
    with pytest.raises(revision_module.EditorialEnrichmentRevisionConflictError) as stale_error:
        await stale.service.revise(**_revision_args(stale))
    assert stale_error.value.code == "editorial_enrichment_stale_base"
    assert stale.world.gateway.calls == []

    changed_hash = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    changed_args = _revision_args(changed_hash)
    changed_args["base_canonical_sha256"] = "0" * 64
    with pytest.raises(revision_module.EditorialEnrichmentRevisionConflictError):
        await changed_hash.service.revise(**changed_args)
    assert changed_hash.world.gateway.calls == []


@pytest.mark.asyncio
async def test_revision_api_maps_stale_base_to_http_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConflictingRevisionService:
        async def revise(self, **_kwargs: object) -> None:
            from cti_app.application.production_editorial_enrichment import (
                EditorialEnrichmentRevisionConflictError,
            )

            raise EditorialEnrichmentRevisionConflictError("base changed")

    monkeypatch.setattr(
        production_api,
        "_production_enrichment_revision_service",
        lambda _request: _ConflictingRevisionService(),
    )
    request = production_api.EditorialEnrichmentRevisionRequest(
        base_artifact_id=uuid4(),
        base_version=1,
        base_input_hash="a" * 64,
        base_canonical_sha256="b" * 64,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="tools_table",
        action=EditorialEnrichmentRevisionAction.IMPROVE_TABLE,
        instruction="Improve readability.",
    )

    with pytest.raises(HTTPException) as conflict:
        await production_api.revise_editorial_enrichment_artifact(
            _SUBJECT_ID, request, SimpleNamespace()
        )

    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "editorial_enrichment_stale_base"  # type: ignore[index]


@pytest.mark.asyncio
async def test_revision_needs_new_evidence_persists_l7b_resource_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    needs_wire = (
        "NEEDS N009\nKIND: TECHNICAL_ANALYSIS\n"
        "REASON: A protocol detail is absent from the admitted evidence.\n"
        "QUERY_HINT: ExampleRAT protocol detail\nEND NEEDS"
    )
    world = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, needs_wire)),
    )

    result = await world.service.revise(**_revision_args(world))

    assert result.outcome is EditorialEnrichmentRevisionOutcome.NEEDS_NEW_EVIDENCE
    assert result.revision["resource_need"]["kind"] == "TECHNICAL_ANALYSIS"  # type: ignore[index]
    stored = await world.world.store.read_json(result.artifact.canonical_blob_id)
    assert len(stored["resource_needs"]) == 1  # type: ignore[arg-type]
    assert stored["diagrams"] == world.base_payload["diagrams"]  # type: ignore[index]
    assert len(world.world.gateway.calls) == 1
    assert world.world.gateway.calls[0][0].web_search is False


@pytest.mark.asyncio
async def test_revision_surfaces_validator_rejections_and_rejects_oversized_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = _proposal("E999").model_copy(update={"diagrams": ()})
    world = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _proposal_to_wire(invalid))),
    )
    with pytest.raises(EditorialEnrichmentRevisionValidationError) as rejected:
        await world.service.revise(**_revision_args(world))
    assert rejected.value.rejections
    assert any("evidence" in item["reason_code"] for item in rejected.value.rejections)

    oversized = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    args = _revision_args(oversized)
    args["instruction"] = "x" * 2001
    with pytest.raises(ValueError, match="editorial_enrichment_revision_instruction_invalid"):
        await oversized.service.revise(**args)
    assert oversized.world.gateway.calls == []


def _real_run_evidence_pack() -> EditorialEnrichmentEvidencePackV1:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    base = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    source_ref = base.resolve_handle("E001")
    # These handle texts are synthetic; the fixture's real handle IDs are E011, E013, E014,
    # E018, E023, E026, and E029.
    labels = {
        "E029": (
            "Appareil compromis interrogeant l'emplacement blockchain récupère les données "
            "de routage C2 Transaction Bitcoin contenant des données de routage C2 encodées"
        ),
        "E011": (
            "Transaction Bitcoin contenant des données de routage C2 encodées données "
            "récupérées puis décodées Données C2 décodées permet la connexion à "
            "l'infrastructure récupérée Infrastructure hors chaîne"
        ),
        "E023": "La documentation décrit le fonctionnement général des BDD.",
        "E013": "Les wallets opérateurs et les resolver contracts sont suivis on-chain.",
        "E014": "Des appels JSON-RPC sortants sont observables côté endpoint.",
        "E018": "Une limite analytique qui ne figure dans aucune ligne du tableau.",
        "E026": "Les chaînes de financement et les historiques de mise à jour sont rapprochés.",
    }
    annotation_sections = (
        {"section_index": 0, "paragraphs": []},
        {"section_index": 1, "paragraphs": []},
        {
            "section_index": 2,
            "paragraphs": [
                {
                    "anchor": "section:2:paragraph:0001",
                    "text": (
                        "Le blockchain dead drop s'appuie sur MITRE ATT&CK T1102.002 et "
                        "Web Service: Dead Drop Resolver."
                    ),
                }
            ],
        },
        {"section_index": 3, "paragraphs": []},
        {
            "section_index": 4,
            "paragraphs": [
                {
                    "anchor": "section:4:paragraph:0001",
                    "text": "Les appels JSON-RPC sortants sont observables côté endpoint.",
                }
            ],
        },
    )
    handles = tuple(labels)
    assert set(handles) == {"E011", "E013", "E014", "E018", "E023", "E026", "E029"}
    return EditorialEnrichmentEvidencePackV1(
        publication_language="fr",
        # Synthetic paragraph text satisfies the fixture's exact anchor and text checks.
        current_synthesis={
            "lead": [
                {
                    "anchor": "lead:0001",
                    "text": (
                        "Le Ministry of Intelligence iranien utilise une couche de résolution "
                        "C2 via Bitcoin OP_RETURN."
                    ),
                }
            ],
            "sections": annotation_sections,
        },
        narrative_evidence=tuple(
            {"handle": handle, "context": labels[handle]} for handle in handles
        ),
        technical_evidence=(),
        reserve_evidence=(),
        source_pair_relations=(),
        _handle_to_ref={handle: source_ref for handle in handles},
    )


def _top_wire_block(wire: str, header: str) -> str:
    kind = header.split()[0]
    lines = wire.splitlines()
    start = lines.index(header)
    end = next(index for index in range(start, len(lines)) if lines[index] == f"END {kind}")
    return "\n".join(lines[start : end + 1])


def test_real_run_wire_fixture_keeps_evidence_backed_enrichments_and_needs() -> None:
    wire = (Path(__file__).parent / "fixtures" / "real_run_enrichment_2026-10-03.txt").read_text(
        encoding="utf-8"
    )

    parsed = parse_editorial_enrichment_proposal_wire(wire, _real_run_evidence_pack())

    assert parsed.proposal is not None
    assert [item.key for item in parsed.proposal.tables] == ["detection_pivots"]
    table = parsed.proposal.tables[0]
    assert table.purpose.evidence_handles == ("E013", "E014", "E026")
    assert any(
        warning.startswith("editorial_enrichment_purpose_evidence_trimmed:TABLE:T001:E018")
        for warning in parsed.warnings
    )
    assert [item.key for item in parsed.proposal.diagrams] == ["iran_bitcoin_bdd_flow"]
    diagram = parsed.proposal.diagrams[0]
    assert diagram.kind.value == "network_flow"
    assert [node.role.value for node in diagram.nodes] == [
        "data_artifact",
        "victim",
        "data_artifact",
        "infrastructure",
    ]
    assert [edge.relation_type for edge in diagram.edges] == [
        DiagramRelationType.FACTUAL,
        DiagramRelationType.FACTUAL,
        DiagramRelationType.FACTUAL,
    ]
    assert not any(
        item.reason_code.startswith("editorial_enrichment_diagram_relation_")
        for item in parsed.rejections
    )
    assert not any(
        "editorial_enrichment_relation_type_normalized:" in item for item in parsed.warnings
    )
    assert [item.key for item in parsed.proposal.resource_needs] == ["N001"]
    assert len(parsed.proposal.annotations) == 7
    assert {item.paragraph_anchor for item in parsed.proposal.annotations} == {
        "lead:0001",
        "section:2:paragraph:0001",
        "section:4:paragraph:0001",
    }
    assert "editorial_enrichment_duplicate_local_block_id" not in {
        item.reason_code for item in parsed.rejections
    }


def test_relation_type_synonyms_normalize_only_for_infection_chain() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    pack = build_editorial_enrichment_evidence_pack(
        snapshot,
        extraction,
        _synthesis(extraction),
    )
    wire = _proposal_to_wire(_proposal("E001"))
    infection_chain = wire.replace("RELATION_TYPE: factual", "RELATION_TYPE: sequence", 1)
    normalized = parse_editorial_enrichment_proposal_wire(infection_chain, pack)
    assert normalized.proposal is not None
    assert normalized.proposal.diagrams[0].edges[0].relation_type is DiagramRelationType.FACTUAL
    assert any("sequence->factual" in warning for warning in normalized.warnings)

    unknown = wire.replace("RELATION_TYPE: factual", "RELATION_TYPE: causal", 1)
    rejected = parse_editorial_enrichment_proposal_wire(unknown, pack)
    assert rejected.proposal is not None
    assert rejected.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_relation_type_invalid" in {
        item.reason_code for item in rejected.rejections
    }


def test_diagram_node_role_defaults_with_warning_for_legacy_wire() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, _synthesis(extraction))
    wire = _proposal_to_wire(_proposal("E001")).replace("ROLE: unknown\n", "", 1)

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is not None
    assert parsed.proposal.diagrams[0].nodes[0].role is DiagramNodeRole.UNKNOWN
    assert any(
        warning.startswith("editorial_enrichment_diagram_node_role_missing:")
        for warning in parsed.warnings
    )


def test_diagram_node_rejects_role_outside_closed_vocabulary() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, _synthesis(extraction))
    wire = _proposal_to_wire(_proposal("E001")).replace("ROLE: unknown", "ROLE: analyst", 1)

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is not None
    assert parsed.proposal.diagrams == ()
    assert "editorial_enrichment_diagram_node_role_invalid" in {
        item.reason_code for item in parsed.rejections
    }


def test_purpose_evidence_trimming_preserves_unknown_handle_rejection() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    base = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    pack = replace(
        base,
        _handle_to_ref={**base._handle_to_ref, "E002": base.resolve_handle("E001")},
    )
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace("PURPOSE_EVIDENCE: E001", "PURPOSE_EVIDENCE: E001, E002")

    parsed = parse_editorial_enrichment_proposal_wire(wire, pack)

    assert parsed.proposal is not None
    assert parsed.proposal.tables[0].purpose.evidence_handles == ("E001",)
    assert parsed.proposal.diagrams[0].purpose.evidence_handles == ("E001",)
    assert any("TABLE:T001:E002" in warning for warning in parsed.warnings)
    assert any("DIAGRAM:D001:E002" in warning for warning in parsed.warnings)
    unknown = parse_editorial_enrichment_proposal_wire(
        wire.replace("PURPOSE_EVIDENCE: E001, E002", "PURPOSE_EVIDENCE: E999", 1), pack
    )
    assert "editorial_enrichment_unknown_evidence_handle" in {
        item.reason_code for item in unknown.rejections
    }


def test_child_ids_are_parent_scoped_and_same_parent_duplicates_still_reject() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, _synthesis(extraction))
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace("COLUMN C001_001", "COLUMN C001", 1)
    wire = wire.replace("COLUMN C001_002", "COLUMN", 1)
    generated = parse_editorial_enrichment_proposal_wire(wire, pack)
    assert generated.proposal is not None
    assert len(generated.proposal.tables[0].columns) == 2
    assert "editorial_enrichment_duplicate_local_block_id" not in {
        item.reason_code for item in generated.rejections
    }

    duplicated = (
        _proposal_to_wire(_proposal("E001"))
        .replace("COLUMN C001_001", "COLUMN C001", 1)
        .replace("COLUMN C001_002", "COLUMN C001", 1)
    )
    duplicate_result = parse_editorial_enrichment_proposal_wire(duplicated, pack)
    assert "editorial_enrichment_duplicate_local_block_id" in {
        item.reason_code for item in duplicate_result.rejections
    }


def test_repair_merge_cannot_reuse_an_accepted_top_level_id() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, _synthesis(extraction))
    original_wire = _proposal_to_wire(_proposal("E001"))
    original_table = _top_wire_block(original_wire, "TABLE T001")
    duplicate_table = original_table.replace("KEY: tools_table", "KEY: repaired_tools_table", 1)
    first_pass = parse_editorial_enrichment_proposal_wire(
        f"{original_table}\n\n{duplicate_table}", pack
    )
    repair_pass = parse_editorial_enrichment_proposal_wire(duplicate_table, pack)

    assert first_pass.proposal is not None
    assert len(first_pass.rejected_blocks) == 1
    merged, repaired, _unexpected = enrichment_module._merge_repaired_editorial_enrichment_proposal(
        first_pass, repair_pass, first_pass.rejected_blocks
    )

    assert merged.proposal is not None
    assert [item.key for item in merged.proposal.tables] == ["tools_table"]
    assert repaired == ()
    assert "editorial_enrichment_duplicate_local_block_id" in {
        item.reason_code for item in merged.rejections
    }


@pytest.mark.asyncio
async def test_targeted_repair_is_single_call_versioned_and_reused_on_replay() -> None:
    valid_wire = _proposal_to_wire(_proposal("E001"))
    table = _top_wire_block(valid_wire, "TABLE T001")
    invalid_table = table.replace("PURPOSE_EVIDENCE: E001", "PURPOSE_EVIDENCE: E999", 1)
    first_pass = "\n\n".join((invalid_table, _top_wire_block(valid_wire, "DIAGRAM D001")))

    def respond(request: ModelRequest) -> ModelExecution:
        if request.prompt_template_id == "production-editorial-enrichment-repair":
            repair_payload = json.loads(request.text)
            assert [item["kind"] for item in repair_payload["rejected_blocks"]] == ["TABLE"]
            assert repair_payload["rejected_blocks"][0]["reason_codes"] == [
                "editorial_enrichment_unknown_evidence_handle"
            ]
            return _succeeded_text(request, table)
        return _succeeded_text(request, first_pass)

    world = _world(_RecordingGateway(respond))

    first = await _execute(world)
    second = await _execute(world)

    assert first.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert first.table_count == first.diagram_count == 1
    assert first.model_calls == 3
    assert second.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert second.model_calls == 0
    assert len(world.gateway.calls) == 3
    stored_call = world.writer.calls[0]
    assert stored_call["metadata_extra"]["repaired_block_count"] == 1  # type: ignore[index]
    assert stored_call["metadata_extra"]["editorial_enrichment_wire_details"]["rejections"]  # type: ignore[index]
    enrichment = stored_call["enrichment"]
    assert "editorial_enrichment_repair_blocks_repaired:1" in enrichment.warnings  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_repair_failure_keeps_first_pass_and_all_rejected_proposals_need_review() -> None:
    valid_wire = _proposal_to_wire(_proposal("E001"))
    table = _top_wire_block(valid_wire, "TABLE T001")
    invalid_sibling = table.replace("TABLE T001", "TABLE T002", 1).replace(
        "PURPOSE_EVIDENCE: E001", "PURPOSE_EVIDENCE: E999", 1
    )
    first_pass = "\n\n".join((valid_wire, invalid_sibling))

    def fail_repair(request: ModelRequest) -> ModelExecution:
        if request.prompt_template_id == "production-editorial-enrichment-repair":
            raise ModelGatewayError("repair unavailable")
        return _succeeded_text(request, first_pass)

    world = _world(_RecordingGateway(fail_repair))
    fallback = await _execute(world)
    assert fallback.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert fallback.table_count == fallback.diagram_count == 1
    assert fallback.model_calls == 3
    assert fallback.details is not None
    assert fallback.details["repair"]["status"] == "failed"  # type: ignore[index]

    invalid_table = table.replace("PURPOSE_EVIDENCE: E001", "PURPOSE_EVIDENCE: E999", 1)
    invalid_diagram = _top_wire_block(valid_wire, "DIAGRAM D001").replace(
        "RELATION_TYPE: factual", "RELATION_TYPE: causal", 1
    )

    def unrepaired(request: ModelRequest) -> ModelExecution:
        if request.prompt_template_id == "production-editorial-enrichment-repair":
            return _succeeded_text(request, "NO USEFUL ENRICHMENT")
        return _succeeded_text(request, "\n\n".join((invalid_table, invalid_diagram)))

    rejected_world = _world(_RecordingGateway(unrepaired))
    rejected = await _execute(rejected_world)
    assert rejected.status is EditorialEnrichmentExecutionStatus.NEEDS_REVIEW
    assert rejected.error_code == "editorial_enrichment_empty_after_rejections"
    assert rejected.model_calls == 2


def test_source_figure_inventory_warning_only_reports_pending_items() -> None:
    locator = SourceFigureLocatorV1(page=1)
    source = "https://example.test/figure.png"

    def figure(decision: SourceFigureDecision) -> ResolvedSourceFigureV1:
        digest = "a" * 64 if decision is SourceFigureDecision.ACCEPTED else None
        return ResolvedSourceFigureV1(
            figure_id=source_figure_id(
                source_document_id=_DOCUMENT_ID,
                sha256=digest,
                source=source,
                locator=locator,
            ),
            blob_id=uuid4() if digest is not None else None,
            sha256=digest,
            mime_type="image/png" if digest is not None else None,
            byte_size=128 if digest is not None else None,
            source_document_id=_DOCUMENT_ID,
            source=source,
            provenance="fixture",
            locator=locator,
            decision=decision,
            decision_reason="fixture decision",
        )

    excluded = SourceFigureInventoryResult(figures=(figure(SourceFigureDecision.REJECTED),))
    accepted = SourceFigureInventoryResult(figures=(figure(SourceFigureDecision.ACCEPTED),))
    pending = SourceFigureInventoryResult(figures=(figure(SourceFigureDecision.PENDING),))
    page_excerpt = SourceFigureInventoryResult(
        figures=(), warnings=("source_figure_pdf_page_excerpt_needed",)
    )
    oversized_source = SourceFigureInventoryResult(
        figures=(), warnings=("source_figure_source_exceeds_byte_limit",)
    )

    warning = "source_figure_inventory_contains_unresolved_items"
    assert warning not in enrichment_module._source_figure_inventory_warnings(excluded)
    assert warning not in enrichment_module._source_figure_inventory_warnings(accepted)
    assert warning in enrichment_module._source_figure_inventory_warnings(pending)
    assert warning in enrichment_module._source_figure_inventory_warnings(page_excerpt)
    assert warning in enrichment_module._source_figure_inventory_warnings(oversized_source)


def test_translated_diagram_labels_are_grounded_by_their_cited_english_evidence() -> None:
    # Real 2026-10-05 output: French labels over English evidence were all rejected
    # as endpoint_unsupported because no label token appeared in the record.
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    english = {
        "handle": "E001",
        "context": "The device sends an RPC request to the contract and then it is from "
        "the contract that the malware reads the address of the C2 server.",
    }
    translated_pack = replace(pack, narrative_evidence=(english,), technical_evidence=())
    wire = _proposal_to_wire(_proposal("E001"))
    wire = wire.replace("LABEL: ExampleRAT", "LABEL: Machine compromise", 1)
    wire = wire.replace("LABEL: Execution", "LABEL: Serveur hors chaîne", 1)
    wire = wire.replace("LABEL: launches", "LABEL: interroge puis contacte", 1)

    result = parse_editorial_enrichment_proposal_wire(wire, translated_pack)

    assert result.proposal is not None
    assert len(result.proposal.diagrams) == 1
