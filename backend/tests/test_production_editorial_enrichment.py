from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentValidationError,
    canonical_editorial_enrichment_hash,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    validate_editorial_enrichment,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import ExtractionProfile, ProductionEvidenceBasis
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EditorialEnrichmentV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureCandidateV1,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
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
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    evidence_ref_sort_key,
    extraction_evidence_refs_v1,
)
from tests.editorial_enrichment_support import build_empty_editorial_enrichment

_SUBJECT_ID = UUID("a0a4f09c-1107-4ae1-8311-bf43fd2a2ce0")
_DOCUMENT_ID = UUID("b8f83b7b-7088-409a-9667-4f93758c18e1")
_INPUT_HASH = "a" * 64


def _extraction(
    *,
    subject_id: UUID = _SUBJECT_ID,
    document_id: UUID = _DOCUMENT_ID,
    url: str = "https://example.test/report",
) -> ProductionExtractionV1:
    source = ProductionSourceExtractionV1(
        source_document_id=document_id,
        canonical_url=url,
        content_sha256="b" * 64,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(
            ExtractionFactV1(
                category="malware",
                value="ExampleRAT",
                attack_id=None,
                context="",
                evidence_quote="ExampleRAT",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        ),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=_INPUT_HASH,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )


def _synthesis(
    extraction: ProductionExtractionV1,
    *,
    subject_id: UUID | None = None,
    sections: tuple[SynthesisSectionV1, ...] | None = None,
) -> ProductionSynthesisV1:
    ref = extraction_evidence_refs_v1(extraction)[0]
    default_sections = (
        SynthesisSectionV1(
            SynthesisSectionKind.OVERVIEW,
            "Overview",
            (SynthesisParagraphV1("ExampleRAT was observed.", (ref,)),),
        ),
    )
    return ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id or extraction.subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Example report",
        lead=(),
        sections=default_sections if sections is None else sections,
        timeline=(),
        uncertainties=(),
        warnings=(),
    )


def _populated_enrichment(
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> EditorialEnrichmentV1:
    ref = extraction_evidence_refs_v1(extraction)[0]
    return replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(
            TableSpecV1(
                key="commands",
                kind=EnrichmentTableKind.COMMANDS,
                title="Observed command",
                caption="",
                columns=(TableColumnV1("command", "Command"), TableColumnV1("purpose", "Purpose")),
                rows=(TableRowV1(("-enc", "Execution"), (ref,)),),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_SECTION, 0),
            ),
        ),
        diagrams=(
            DiagramSpecV1(
                key="infection_chain",
                kind=EnrichmentDiagramKind.INFECTION_CHAIN,
                title="Infection chain",
                caption=None,
                direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
                nodes=(
                    DiagramNodeV1("loader", "Loader", (ref,)),
                    DiagramNodeV1("payload", "Payload", (ref,)),
                ),
                edges=(DiagramEdgeV1("loader", "payload", "loads", (ref,)),),
                groups=(DiagramGroupV1("host", "Victim host", ("loader", "payload")),),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
            ),
        ),
        source_figures=(
            SourceFigureCandidateV1(
                key="source_figure_01",
                source_document_id=extraction.sources[0].source_document_id,
                source_url=extraction.sources[0].canonical_url,
                caption="Source architecture figure",
                provenance="Figure 1 from the source publication",
                locator=SourceFigureLocatorV1(page=1),
                inclusion_status=SourceFigureInclusionStatus.PROPOSED,
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
            ),
        ),
    )


def test_empty_contract_round_trips_and_is_valid() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)

    assert enrichment.schema_version == EDITORIAL_ENRICHMENT_SCHEMA_VERSION
    assert enrichment.enrichment_policy_version == EDITORIAL_ENRICHMENT_POLICY_VERSION
    assert enrichment.tables == enrichment.diagrams == enrichment.source_figures == ()
    assert editorial_enrichment_from_json(editorial_enrichment_to_json(enrichment)) == enrichment
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)


def test_populated_contract_round_trips_canonically() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = _populated_enrichment(extraction, synthesis)

    payload = editorial_enrichment_to_json(enrichment)
    assert editorial_enrichment_from_json(payload) == enrichment
    assert editorial_enrichment_to_json(editorial_enrichment_from_json(payload)) == payload
    assert enrichment.tables[0].caption is None
    assert canonical_editorial_enrichment_hash(enrichment) == canonical_editorial_enrichment_hash(
        editorial_enrichment_from_json(payload)
    )
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)


def test_unknown_json_keys_and_invalid_hashes_are_rejected() -> None:
    enrichment = build_empty_editorial_enrichment(
        extraction=_extraction(), synthesis=_synthesis(_extraction())
    )
    payload = editorial_enrichment_to_json(enrichment)
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="missing or extra fields"):
        editorial_enrichment_from_json(payload)

    payload.pop("unexpected")
    payload["extraction_hash"] = "A" * 64
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        editorial_enrichment_from_json(payload)


def test_policy_version_and_global_key_collisions_are_rejected() -> None:
    enrichment = build_empty_editorial_enrichment(
        extraction=_extraction(), synthesis=_synthesis(_extraction())
    )
    with pytest.raises(ValueError, match="policy version"):
        replace(enrichment, enrichment_policy_version="future-policy")

    extraction = _extraction()
    synthesis = _synthesis(extraction)
    populated = _populated_enrichment(extraction, synthesis)
    duplicate = replace(populated.diagrams[0], key="commands")
    with pytest.raises(ValueError, match="globally unique"):
        replace(populated, diagrams=(duplicate,))


def test_placement_and_table_invariants_are_enforced() -> None:
    with pytest.raises(ValueError, match="non-negative section_index"):
        EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_SECTION)
    with pytest.raises(ValueError, match="non-negative section_index"):
        EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_SECTION, -1)
    with pytest.raises(ValueError, match="non-negative section_index"):
        EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_SECTION, True)
    with pytest.raises(ValueError, match="only valid"):
        EnrichmentPlacementV1(EnrichmentPlacementKind.END, 0)

    ref = ExtractionEvidenceRefV1(_DOCUMENT_ID, EvidenceKind.FACT, "d" * 64)
    placement = EnrichmentPlacementV1(EnrichmentPlacementKind.END)
    columns = (TableColumnV1("left", "Left"), TableColumnV1("right", "Right"))
    with pytest.raises(ValueError, match="width"):
        TableSpecV1(
            "table",
            EnrichmentTableKind.CUSTOM,
            "Table",
            None,
            columns,
            (TableRowV1(("one",), (ref,)),),
            placement,
        )
    with pytest.raises(ValueError, match="at least one evidence"):
        TableRowV1(("one", "two"), ())


def test_diagram_node_edge_and_group_invariants_are_enforced() -> None:
    ref = ExtractionEvidenceRefV1(_DOCUMENT_ID, EvidenceKind.FACT, "d" * 64)
    placement = EnrichmentPlacementV1(EnrichmentPlacementKind.END)
    one_node = DiagramNodeV1("a", "Node A", (ref,))
    another_node = DiagramNodeV1("b", "Node B", (ref,))

    with pytest.raises(ValueError, match="at least one evidence"):
        DiagramNodeV1("a", "Node A", ())
    with pytest.raises(ValueError, match="at least one evidence"):
        DiagramEdgeV1("a", "b", None, ())

    base = dict(
        key="diagram",
        kind=EnrichmentDiagramKind.CUSTOM,
        title="Diagram",
        caption=None,
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        nodes=(one_node, another_node),
        edges=(DiagramEdgeV1("a", "b", None, (ref,)),),
        groups=(),
        placement=placement,
    )
    with pytest.raises(ValueError, match="node IDs must be unique"):
        DiagramSpecV1(**{**base, "nodes": (one_node, one_node)})
    with pytest.raises(ValueError, match="endpoints"):
        DiagramSpecV1(**{**base, "edges": (DiagramEdgeV1("a", "missing", None, (ref,)),)})
    with pytest.raises(ValueError, match="groups must reference"):
        DiagramSpecV1(**{**base, "groups": (DiagramGroupV1("group", "Group", ("missing",)),)})


def test_source_figure_requires_locator_and_positive_page() -> None:
    with pytest.raises(ValueError, match="requires at least one location"):
        SourceFigureLocatorV1()
    with pytest.raises(ValueError, match="positive integer"):
        SourceFigureLocatorV1(page=0)
    with pytest.raises(ValueError, match="positive integer"):
        SourceFigureLocatorV1(page=True)


def test_evidence_refs_are_sorted_and_aggregated_from_editorial_objects() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = _populated_enrichment(extraction, synthesis)

    refs = editorial_enrichment_evidence_refs(enrichment)
    assert refs == frozenset(extraction_evidence_refs_v1(extraction))
    assert enrichment.tables[0].rows[0].evidence_refs == tuple(
        sorted(enrichment.tables[0].rows[0].evidence_refs, key=evidence_ref_sort_key)
    )


def test_inter_artifact_lineage_evidence_placement_and_source_figure_validation() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = _populated_enrichment(extraction, synthesis)
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)

    unknown = ExtractionEvidenceRefV1(uuid4(), EvidenceKind.FACT, "e" * 64)
    bad_table = replace(enrichment.tables[0], rows=(TableRowV1(("-enc", "Execution"), (unknown,)),))
    with pytest.raises(EditorialEnrichmentValidationError) as evidence_error:
        validate_editorial_enrichment(
            replace(enrichment, tables=(bad_table,)), extraction=extraction, synthesis=synthesis
        )
    assert evidence_error.value.code == "editorial_enrichment_evidence_missing"

    with pytest.raises(EditorialEnrichmentValidationError, match="subject"):
        validate_editorial_enrichment(
            replace(enrichment, subject_id=uuid4()), extraction=extraction, synthesis=synthesis
        )
    with pytest.raises(EditorialEnrichmentValidationError, match="extraction hash"):
        validate_editorial_enrichment(
            replace(enrichment, extraction_hash="f" * 64),
            extraction=extraction,
            synthesis=synthesis,
        )
    with pytest.raises(EditorialEnrichmentValidationError, match="synthesis hash"):
        validate_editorial_enrichment(
            replace(enrichment, synthesis_hash="f" * 64), extraction=extraction, synthesis=synthesis
        )
    with pytest.raises(EditorialEnrichmentValidationError, match="language"):
        validate_editorial_enrichment(
            replace(enrichment, publication_language="de"),
            extraction=extraction,
            synthesis=synthesis,
        )

    out_of_range = replace(
        enrichment.tables[0],
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_SECTION, 1),
    )
    with pytest.raises(EditorialEnrichmentValidationError, match="section"):
        validate_editorial_enrichment(
            replace(enrichment, tables=(out_of_range,)), extraction=extraction, synthesis=synthesis
        )

    unknown_figure = replace(enrichment.source_figures[0], source_document_id=uuid4())
    with pytest.raises(EditorialEnrichmentValidationError, match="absent"):
        validate_editorial_enrichment(
            replace(enrichment, source_figures=(unknown_figure,)),
            extraction=extraction,
            synthesis=synthesis,
        )
    wrong_url = replace(enrichment.source_figures[0], source_url="https://other.test/report")
    with pytest.raises(EditorialEnrichmentValidationError, match="URL differs"):
        validate_editorial_enrichment(
            replace(enrichment, source_figures=(wrong_url,)),
            extraction=extraction,
            synthesis=synthesis,
        )


def test_builder_and_stage_hash_are_deterministic_and_bind_both_inputs() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    built = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)

    assert built.extraction_hash == canonical_extraction_hash(extraction)
    assert built.synthesis_hash == canonical_synthesis_hash(synthesis)
    hashes = {"evidence_pack_hash": "1" * 64, "access_policy_hash": "2" * 64}
    first = compute_editorial_enrichment_input_hash(
        extraction=extraction, synthesis=synthesis, **hashes
    )
    assert first == compute_editorial_enrichment_input_hash(
        extraction=extraction, synthesis=synthesis, **hashes
    )

    changed_synthesis = replace(synthesis, title="Changed title")
    assert first != compute_editorial_enrichment_input_hash(
        extraction=extraction, synthesis=changed_synthesis, **hashes
    )
