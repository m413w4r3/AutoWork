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

import cti_app.application.production_editorial_enrichment as enrichment_module
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
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    AnnotationProposalV1,
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
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
)
from cti_app.application.source_figure_inventory import (
    SourceFigureCatalogMetadata,
    SourceFigureInventoryResult,
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
                context="",
                evidence_quote="ExampleRAT",
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
) -> SourceFigureInventoryResult:
    source = extraction.sources[0]
    locator = SourceFigureLocatorV1(
        page=2,
        section="Network overview",
        figure_label="ExampleRAT execution architecture",
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
                alt_text="ExampleRAT flow",
                caption_text="ExampleRAT execution architecture",
                nearby_heading_text="Network overview",
                anchor="figure:2:network-overview",
                width=640,
                height=400,
            )
        },
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


def test_supporting_full_narrative_evidence_is_available_after_core():
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
        tier=ProductionReferenceTier.SUPPORTING,
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
        url_suffix="supporting",
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
    contract = editorial_enrichment_output_contract_example()

    assert isinstance(contract, str)
    assert "COLUMN C001" in contract and "ROW R001" in contract
    assert "NODE N001" in contract and "RELATION L001" in contract
    assert "GROUP G001" in contract and "NO USEFUL ENRICHMENT" in contract
    assert "FIGURE P001" in contract and "NEEDS N001" in contract
    assert "EVIDENCE: E001" in contract
    assert "D2" in contract


def _figure_wire(
    *,
    handle: str = "F001",
    caption: str = "ExampleRAT execution architecture",
    placement: str | None = "after_section",
    section_index: int = 0,
    extra: str = "",
) -> str:
    lines = [
        "FIGURE P001",
        f"FIGURE_HANDLE: {handle}",
        f"CAPTION: {caption}",
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


def test_figure_proposal_selects_local_asset_and_downgrades_ungrounded_caption() -> None:
    snapshot = _snapshot()
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    inventory = _figure_inventory(extraction)
    catalog = build_editorial_figure_catalog(extraction, inventory)
    parsed = parse_editorial_enrichment_proposal_wire(
        _figure_wire(caption="Brand-new unsupported claim"), pack, figure_catalog=catalog
    )

    assert parsed.proposal is not None
    enrichment = validate_editorial_enrichment_proposal(
        parsed.proposal, pack, extraction, synthesis, figure_catalog=catalog
    )

    selected = enrichment.source_figures[0]
    assert selected.inclusion_status.value == "included"
    assert selected.caption == "ExampleRAT execution architecture"
    assert selected.resolved_figure is not None
    assert selected.resolved_figure.sha256 == "f" * 64
    assert selected.resolved_figure.blob_id == UUID(int=601)
    assert selected.provenance == inventory.figures[0].provenance
    assert "editorial_enrichment_figure_caption_downgraded:F001" in enrichment.warnings
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
    assert enrichment.schema_version == 3
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
    figure_catalog = build_editorial_figure_catalog(extraction, _figure_inventory(extraction))
    request = build_editorial_enrichment_model_request(
        run,
        snapshot,
        extraction,
        synthesis,
        pack,
        access_policy,
        figure_catalog=figure_catalog,
    )

    assert request.web_search is False
    assert request.background is False
    assert request.conversation is None
    assert request.routing_hint.value == "editorial_enrichment"
    assert request.prompt_template_version == EDITORIAL_ENRICHMENT_PROMPT_VERSION
    assert request.run_id == editorial_enrichment_model_run_id(
        run, request.metadata["editorial_enrichment_invocation_hash"]
    )
    assert str(source.id) not in request.text
    assert str(extraction.sources[0].source_document_id) not in request.text
    assert "F001" in request.text
    assert "ExampleRAT execution architecture" in request.text
    assert "640" in request.text and "400" in request.text
    assert "blob_id" not in request.text
    assert EDITORIAL_ENRICHMENT_GENERATOR_VERSION == "model-text-blocks-v3-figures-resource-needs"
    assert EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION == (
        "editorial-enrichment-block-contract-v3-figures-needs"
    )


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
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}
        self.diagnostics: list[dict[str, object]] = []

    async def draft(
        self, request: ModelRequest, output_schema: object | None = None
    ) -> ModelExecution:
        self.calls.append((request, output_schema))
        if isinstance(self._responder, Exception):
            raise self._responder
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
async def test_service_includes_only_the_model_selected_catalog_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded_text(request, _figure_wire())))
    inventory = _figure_inventory(world.extraction)

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
    assert str(world.extraction.sources[0].source_document_id) not in request.text
    assert result.source_figure_count == 1
    enrichment = world.writer.calls[0]["enrichment"]
    assert len(enrichment.source_figures) == 1  # type: ignore[attr-defined]
    assert enrichment.source_figures[0].inclusion_status.value == "included"  # type: ignore[attr-defined]
    assert enrichment.source_figures[0].resolved_figure.sha256 == "f" * 64  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_service_drafts_once_statelessly_and_stores_model_provenance() -> None:
    world = _world(_RecordingGateway(lambda request: _succeeded(request, _proposal("E001"))))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.model_calls, result.table_count, result.diagram_count) == (1, 1, 1)
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
async def test_empty_model_decision_is_a_valid_stored_enrichment() -> None:
    empty = EditorialEnrichmentProposalV1()
    world = _world(_RecordingGateway(lambda request: _succeeded(request, empty)))

    result = await _execute(world)

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert (result.table_count, result.diagram_count, result.model_calls) == (0, 0, 1)
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
    assert len(world.gateway.calls) == 1
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
    assert result.model_calls == 2
    assert [request.web_search for request, _schema in world.gateway.calls] == [False, True]
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
async def test_resource_search_is_blocked_by_external_access_policy_even_when_enabled() -> None:
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

    assert result.status is EditorialEnrichmentExecutionStatus.SUCCEEDED
    assert len(world.gateway.calls) == 1
    assert result.details["resource_proposal_search"]["status"] == (  # type: ignore[index]
        "blocked_by_access_policy"
    )


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
        "END GROUP\nRELATION L_UNKNOWN\nFROM: malware\nTO: missing\nLABEL: leads to\n"
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
    assert len(world.gateway.calls) == 1
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
        ("EDITORIAL_ENRICHMENT_PROMPT_VERSION", "editorial-enrichment-text-blocks-v6"),
        (
            "EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION",
            "editorial-enrichment-block-contract-v3",
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
    assert len(world.gateway.calls) == 2
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
    assert len(world.gateway.calls) == 1
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
    }
    assert result.model_calls == 1
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
    assert result.model_calls == 1
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
