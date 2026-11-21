from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentValidationError,
    _record_covers,
    _record_is_foreign_to,
    _validate_plain_editorial_text,
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
    EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramProfile,
    DiagramRelationDirection,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentElementRef,
    EnrichmentOrderEntry,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    clear_group_order,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
    enrichment_element_order,
    enrichment_order_rank,
    enrichment_order_rank_from_entries,
    next_group_order,
    remove_enrichment_element,
    source_figure_id,
    with_element_group_order,
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


def test_plain_editorial_validator_accepts_technical_placeholders_and_wildcard_domains() -> None:
    _validate_plain_editorial_text(
        r"Une tâche NetSync_<username> utilise %APPDATA%\Microsoft\Network; "
        "# shepherd-persist; ~/.node_packages; "
        "conhost.exe --headless <deno_path> --allow-run <workdir> app.js; "
        "WebSocket vers *.cloudfront.net et fichier *.exe."
    )


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
                purpose=EditorialAnalyticPurposeV1(
                    question="Which command is documented?",
                    available_data="The report records -enc.",
                    comprehension_gain="The adjacent cells pair the literal and role.",
                    scope="The single command listed in the evidence.",
                    evidence_refs=(ref,),
                    knowledge_limits="No other command effects are stated.",
                    placement_reason="Place after the section that introduces the command.",
                ),
            ),
        ),
        diagrams=(
            DiagramSpecV1(
                key="infection_chain",
                kind=EnrichmentDiagramKind.INFECTION_CHAIN,
                profile=DiagramProfile.FLOW,
                title="Infection chain",
                caption="Documented path from loader to payload.",
                direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
                nodes=(
                    DiagramNodeV1("loader", "Loader", (ref,)),
                    DiagramNodeV1("payload", "Payload", (ref,)),
                ),
                edges=(DiagramEdgeV1("loader", "payload", "loads", (ref,)),),
                groups=(DiagramGroupV1("host", "Victim host", ("loader", "payload")),),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
                purpose=EditorialAnalyticPurposeV1(
                    question="What documented action connects the loader and payload?",
                    available_data="The source describes a loader action.",
                    comprehension_gain="A compact edge exposes the documented action.",
                    scope="Only the two named components.",
                    evidence_refs=(ref,),
                    knowledge_limits="No other stages are documented.",
                    placement_reason="Place after the technical explanation.",
                ),
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
    assert payload["tables"][0]["purpose"]["question"] == "Which command is documented?"
    assert payload["diagrams"][0]["profile"] == "flow"
    assert payload["diagrams"][0]["edges"][0]["relation_type"] == "factual"
    assert payload["diagrams"][0]["edges"][0]["direction"] == "directed"
    assert editorial_enrichment_from_json(payload) == enrichment
    assert editorial_enrichment_to_json(editorial_enrichment_from_json(payload)) == payload


def test_v3_enrichment_payload_remains_readable_without_v4_analytic_fields() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = _populated_enrichment(extraction, synthesis)
    payload = editorial_enrichment_to_json(enrichment)
    payload["schema_version"] = 3
    payload["enrichment_policy_version"] = "editorial-enrichment-v3-figures-resource-proposals"
    payload.pop("charts", None)
    for table in payload["tables"]:
        table.pop("purpose")
    for diagram in payload["diagrams"]:
        diagram.pop("purpose")
        diagram.pop("profile")
        for edge in diagram["edges"]:
            edge.pop("relation_type")
            edge.pop("direction")
        for node in diagram["nodes"]:
            node.pop("role")

    decoded = editorial_enrichment_from_json(payload)

    assert decoded.schema_version == 3
    assert decoded.tables[0].purpose is None
    assert decoded.diagrams[0].purpose is None
    assert decoded.diagrams[0].edges[0].relation_type.value == "factual"
    assert editorial_enrichment_to_json(decoded) == payload


def test_v7_diagram_artifact_remains_readable_without_profile_or_edge_direction() -> None:
    extraction = _extraction()
    enrichment = _populated_enrichment(extraction, _synthesis(extraction))
    payload = editorial_enrichment_to_json(enrichment)
    payload["schema_version"] = 7
    payload["enrichment_policy_version"] = "editorial-enrichment-v8-timeline-charts"
    for diagram in payload["diagrams"]:
        diagram.pop("profile")
        for edge in diagram["edges"]:
            edge.pop("direction")

    decoded = editorial_enrichment_from_json(payload)

    assert decoded.schema_version == 7
    assert decoded.diagrams[0].profile is None
    assert decoded.diagrams[0].edges[0].direction is DiagramRelationDirection.DIRECTED
    assert editorial_enrichment_to_json(decoded) == payload


def test_v4_analytic_questions_must_normalize_to_distinct_nonempty_keys() -> None:
    extraction = _extraction()
    enrichment = _populated_enrichment(extraction, _synthesis(extraction))
    table = enrichment.tables[0]
    diagram = enrichment.diagrams[0]
    assert table.purpose is not None and diagram.purpose is not None

    with pytest.raises(ValueError, match="analytic questions must be distinct"):
        replace(
            enrichment,
            tables=(
                replace(
                    table,
                    purpose=replace(table.purpose, question="!!!"),
                ),
            ),
        )

    with pytest.raises(ValueError, match="analytic questions must be distinct"):
        replace(
            enrichment,
            diagrams=(
                replace(
                    diagram,
                    purpose=replace(
                        diagram.purpose,
                        question="Which command is documented?",
                    ),
                ),
            ),
        )


def test_compiled_diagram_asset_identity_round_trips_in_canonical_enrichment() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    enrichment = _populated_enrichment(extraction, synthesis)
    asset_id = uuid4()
    compiled_diagram = replace(enrichment.diagrams[0], compiled_asset_id=asset_id)
    compiled = replace(enrichment, diagrams=(compiled_diagram,))

    payload = editorial_enrichment_to_json(compiled)

    assert payload["diagrams"][0]["compiled_asset_id"] == str(asset_id)
    assert editorial_enrichment_from_json(payload) == compiled


def test_resolved_source_figure_round_trips_without_changing_legacy_candidate_usage() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    source = extraction.sources[0]
    locator = SourceFigureLocatorV1(
        page=1,
        figure_label="Network diagram",
        original_asset_url="https://example.test/images/network.png",
    )
    figure_hash = "d" * 64
    resolved = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source.source_document_id,
            sha256=figure_hash,
            source=source.canonical_url,
            locator=locator,
        ),
        blob_id=UUID("30000000-0000-0000-0000-000000000003"),
        sha256=figure_hash,
        mime_type="image/png",
        byte_size=128,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance="Matched to an image blob already in the archive",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="matched_archived_blob",
    )
    candidate = SourceFigureCandidateV1(
        key=f"source_figure_{resolved.figure_id.hex}",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption="Network diagram",
        provenance=resolved.provenance,
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.PROPOSED,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        resolved_figure=resolved,
    )
    enrichment = replace(
        _populated_enrichment(extraction, synthesis),
        source_figures=(candidate,),
    )

    payload = editorial_enrichment_to_json(enrichment)

    assert editorial_enrichment_from_json(payload) == enrichment
    assert editorial_enrichment_to_json(editorial_enrichment_from_json(payload)) == payload
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
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
    with pytest.raises(ValueError, match="registered anchor_block_id"):
        EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_PARAGRAPH)
    anchored = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=uuid4(),
    )
    assert anchored.anchor_block_id is not None

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


def test_v8_legacy_placement_still_reads() -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    current = _populated_enrichment(extraction, synthesis)
    legacy = replace(
        current,
        schema_version=8,
        enrichment_policy_version="editorial-enrichment-v12-article-density-diagnostics",
    )

    payload = editorial_enrichment_to_json(legacy)
    restored = editorial_enrichment_from_json(payload)

    assert payload["schema_version"] == 8
    assert payload["tables"][0]["placement"] == {
        "kind": "after_section",
        "section_index": 0,
    }
    assert restored == legacy
    assert editorial_enrichment_to_json(restored) == payload


def test_diagram_node_edge_and_group_invariants_are_enforced() -> None:
    ref = ExtractionEvidenceRefV1(_DOCUMENT_ID, EvidenceKind.FACT, "d" * 64)
    placement = EnrichmentPlacementV1(EnrichmentPlacementKind.END)
    one_node = DiagramNodeV1("a", "Node A", (ref,))
    another_node = DiagramNodeV1("b", "Node B", (ref,))

    with pytest.raises(ValueError, match="at least one evidence"):
        DiagramNodeV1("a", "Node A", ())
    with pytest.raises(ValueError, match="at least one evidence"):
        DiagramEdgeV1("a", "b", None, ())
    with pytest.raises(ValueError, match="at least one node"):
        DiagramGroupV1("empty", "Empty", ())
    for control in ("\x00", "\x1b", "\x7f", "\x85", "\ud800"):
        with pytest.raises(ValueError, match="control characters"):
            DiagramNodeV1("a", f"Node{control}A", (ref,))
        with pytest.raises(ValueError, match="control characters"):
            DiagramEdgeV1("a", "b", f"edge{control}", (ref,))
        with pytest.raises(ValueError, match="control characters"):
            DiagramGroupV1("group", f"Group{control}", ("a",))
    assert DiagramNodeV1("a", "line\nbreak\tand\r\u2028", (ref,)).label == (
        "line\nbreak\tand\r\u2028"
    )

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
    with pytest.raises(ValueError, match="must not share nodes"):
        DiagramSpecV1(
            **{
                **base,
                "groups": (
                    DiagramGroupV1("first", "First", ("a",)),
                    DiagramGroupV1("second", "Second", ("a", "b")),
                ),
            }
        )


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


def test_foreign_quote_is_deduced_from_the_record_not_from_an_english_word_list():
    german = {
        "kind": "fact",
        "value": "Le malware récupère les données de routage puis poursuit hors chaîne.",
        "context": "",
        "evidence": "Die Schadsoftware ruft die Routingdaten ab und setzt den Angriff fort.",
    }
    same_language = {
        "kind": "fact",
        "value": "Le malware récupère les données de routage encodées.",
        "context": "",
        "evidence": "Le malware récupère les données de routage encodées dans la transaction.",
    }
    short_english = {
        "kind": "fact",
        "value": "Le malware poursuit le cycle hors chaîne.",
        "context": "",
        "evidence": "Once the malware retrieves what it needs, the operation moves off-chain.",
    }

    assert _record_is_foreign_to(german, "fr")
    assert _record_is_foreign_to(short_english, "fr")
    assert not _record_is_foreign_to(same_language, "fr")
    assert not _record_is_foreign_to(german, "en")


def test_label_coverage_tolerates_accents_inflection_and_partial_wording():
    record = {
        "value": "Le malware récupère les données puis poursuit hors chaîne.",
        "evidence": "The malware retrieves the data; the operation continues off-chain.",
    }

    assert _record_covers(record, "Après récupération, le malware poursuit hors chaîne")
    assert _record_covers(record, "Malware qui récupérait des donnees")
    assert not _record_covers(record, "Compromission du portefeuille crypto")
    assert not _record_covers(record, "")


def _ordering_enrichment(
    *,
    schema_version: int = 11,
    tables: tuple[TableSpecV1, ...] = (),
    diagrams: tuple[DiagramSpecV1, ...] = (),
    figures: tuple[SourceFigureCandidateV1, ...] = (),
) -> EditorialEnrichmentV1:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    base = _populated_enrichment(extraction, synthesis)
    return replace(
        base,
        schema_version=schema_version,
        enrichment_policy_version=(
            EDITORIAL_ENRICHMENT_V11_POLICY_VERSION
            if schema_version == 11
            else EDITORIAL_ENRICHMENT_POLICY_VERSION
        ),
        tables=tables,
        diagrams=diagrams,
        source_figures=figures,
    )


def _ordering_table(
    template: TableSpecV1,
    *,
    key: str,
    placement: EnrichmentPlacementV1,
    group_order: int | None = None,
    question: str | None = None,
) -> TableSpecV1:
    assert template.purpose is not None
    return replace(
        template,
        key=key,
        placement=placement,
        group_order=group_order,
        purpose=replace(
            template.purpose,
            question=question or f"Which documented value does {key} expose?",
        ),
    )


def _ordering_figure(
    template: SourceFigureCandidateV1,
    *,
    key: str,
    placement: EnrichmentPlacementV1,
    group_order: int | None = None,
    order: int | None = None,
) -> SourceFigureCandidateV1:
    return replace(
        template,
        key=key,
        placement=placement,
        group_order=group_order,
        order=order,
    )


def test_v11_round_trips_group_order_for_every_family() -> None:
    anchor = uuid4()
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor
    )
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    assert base.diagrams[0].purpose is not None
    table = _ordering_table(
        _populated_enrichment(_extraction(), _synthesis(_extraction())).tables[0],
        key="ordered_table",
        placement=placement,
        group_order=1,
    )
    diagram = replace(
        base.diagrams[0],
        key="ordered_diagram",
        placement=placement,
        group_order=2,
    )
    figure = _ordering_figure(
        base.source_figures[0], key="ordered_figure", placement=placement, group_order=0
    )
    enrichment = _ordering_enrichment(tables=(table,), diagrams=(diagram,), figures=(figure,))

    payload = editorial_enrichment_to_json(enrichment)

    assert payload["schema_version"] == 11
    assert payload["enrichment_policy_version"] == EDITORIAL_ENRICHMENT_V11_POLICY_VERSION
    assert payload["tables"][0]["group_order"] == 1
    assert payload["diagrams"][0]["group_order"] == 2
    assert payload["source_figures"][0]["group_order"] == 0
    assert editorial_enrichment_from_json(payload) == enrichment
    assert editorial_enrichment_to_json(editorial_enrichment_from_json(payload)) == payload

    unordered = _ordering_enrichment(
        tables=(_ordering_table(table, key="plain_table", placement=placement),)
    )
    unordered_payload = editorial_enrichment_to_json(unordered)
    assert "group_order" not in unordered_payload["tables"][0]
    assert editorial_enrichment_from_json(unordered_payload) == unordered


def test_v10_reader_rejects_group_order_without_rewriting_legacy_payloads() -> None:
    placement = EnrichmentPlacementV1(EnrichmentPlacementKind.END)
    table = _ordering_table(
        _populated_enrichment(_extraction(), _synthesis(_extraction())).tables[0],
        key="legacy_table",
        placement=placement,
    )
    legacy = _ordering_enrichment(schema_version=10, tables=(table,))
    payload = editorial_enrichment_to_json(legacy)
    assert "group_order" not in payload["tables"][0]
    assert editorial_enrichment_from_json(payload) == legacy

    payload["tables"][0]["group_order"] = 0
    with pytest.raises(ValueError, match="missing or extra fields"):
        editorial_enrichment_from_json(payload)


def test_v11_requires_its_policy_and_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="policy version is unsupported"):
        replace(
            _ordering_enrichment(schema_version=11),
            enrichment_policy_version=EDITORIAL_ENRICHMENT_POLICY_VERSION,
        )
    with pytest.raises(ValueError, match="policy version is unsupported"):
        replace(
            _ordering_enrichment(schema_version=10),
            enrichment_policy_version=EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
        )

    placement = EnrichmentPlacementV1(EnrichmentPlacementKind.END)
    table = _ordering_table(
        _populated_enrichment(_extraction(), _synthesis(_extraction())).tables[0],
        key="unknown_key_table",
        placement=placement,
    )
    payload = editorial_enrichment_to_json(_ordering_enrichment(tables=(table,)))
    payload["tables"][0]["unexpected_field"] = "x"
    with pytest.raises(ValueError, match="missing or extra fields"):
        editorial_enrichment_from_json(payload)

    payload = editorial_enrichment_to_json(_ordering_enrichment(tables=(table,)))
    payload["tables"][0]["group_order"] = -1
    with pytest.raises(ValueError, match="non-negative integer"):
        editorial_enrichment_from_json(payload)


def test_group_order_must_be_unique_within_a_placement_group() -> None:
    anchor = uuid4()
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor
    )
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    first = _ordering_table(base.tables[0], key="first_table", placement=placement, group_order=0)
    second = _ordering_table(base.tables[0], key="second_table", placement=placement, group_order=0)
    with pytest.raises(ValueError, match="unique within a placement group"):
        _ordering_enrichment(tables=(first, second))

    other_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=uuid4()
    )
    third = _ordering_table(
        base.tables[0], key="third_table", placement=other_placement, group_order=0
    )
    assert len(_ordering_enrichment(tables=(first, third)).tables) == 2


def test_legacy_rank_reproduces_todays_pdf_order() -> None:
    anchor = uuid4()
    paragraph_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor
    )
    lead_placement = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD)
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    assert base.diagrams[0].purpose is not None
    enrichment = _ordering_enrichment(
        tables=(
            _ordering_table(base.tables[0], key="t_a", placement=paragraph_placement),
            _ordering_table(base.tables[0], key="t_z", placement=lead_placement),
        ),
        diagrams=(
            replace(
                base.diagrams[0],
                key="d_a",
                placement=lead_placement,
                purpose=replace(base.diagrams[0].purpose, question="Which diagram is documented?"),
            ),
        ),
        figures=(
            _ordering_figure(
                base.source_figures[0], key="f_z", placement=paragraph_placement, order=1
            ),
            _ordering_figure(
                base.source_figures[0], key="f_a", placement=paragraph_placement, order=0
            ),
            _ordering_figure(base.source_figures[0], key="f_lead", placement=lead_placement),
        ),
    )

    assert len(enrichment_element_order(enrichment)) == 2
    ranks = enrichment_order_rank(enrichment)

    # after_paragraph: (family-local index, key), exactly the historical sort.
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.FIGURE, "f_z")] == 0
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "t_a")] == 1
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.FIGURE, "f_a")] == 2
    # Figure.order is ignored: f_a declares order 0 but sorts after f_z (order 1).
    # Other placements: (family rank, family-local index, key) = Typst family loops.
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "t_z")] == 0
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.DIAGRAM, "d_a")] == 1
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.FIGURE, "f_lead")] == 2


def test_explicit_group_order_ranks_mixed_members_and_implicit_members_last() -> None:
    anchor = uuid4()
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor
    )
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    enrichment = _ordering_enrichment(
        tables=(
            _ordering_table(
                base.tables[0], key="table_ordered", placement=placement, group_order=2
            ),
            _ordering_table(base.tables[0], key="table_implicit", placement=placement),
        ),
        figures=(
            _ordering_figure(
                base.source_figures[0], key="figure_ordered", placement=placement, group_order=0
            ),
        ),
    )

    groups = enrichment_element_order(enrichment)
    assert len(groups) == 1
    assert groups[0].placement == placement
    assert groups[0].members == (
        EnrichmentElementRef(EditorialEnrichmentElementKind.FIGURE, "figure_ordered"),
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_ordered"),
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_implicit"),
    )
    # Raw ordering entries follow the same single implementation.
    assert enrichment_order_rank_from_entries(
        (
            EnrichmentOrderEntry(
                EditorialEnrichmentElementKind.TABLE,
                "table_ordered",
                placement,
                0,
                2,
            ),
            EnrichmentOrderEntry(
                EditorialEnrichmentElementKind.TABLE,
                "table_implicit",
                placement,
                1,
            ),
            EnrichmentOrderEntry(
                EditorialEnrichmentElementKind.FIGURE,
                "figure_ordered",
                placement,
                0,
                0,
            ),
        )
    ) == enrichment_order_rank(enrichment)


def test_create_move_and_delete_helpers_apply_the_deterministic_rules() -> None:
    anchor = uuid4()
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor
    )
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    enrichment = _ordering_enrichment(
        tables=(
            _ordering_table(base.tables[0], key="alpha", placement=placement, group_order=1),
            _ordering_table(base.tables[0], key="beta", placement=placement, group_order=4),
            _ordering_table(base.tables[0], key="gamma", placement=placement),
        )
    )

    assert next_group_order(enrichment, placement) == 5
    assert next_group_order(enrichment, EnrichmentPlacementV1(EnrichmentPlacementKind.END)) is None

    reordered = with_element_group_order(
        enrichment,
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "gamma"),
        7,
    )
    assert next_group_order(reordered, placement) == 8
    assert (
        enrichment_order_rank(reordered)[
            EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "gamma")
        ]
        == 2
    )

    cleared = clear_group_order(
        enrichment, EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "alpha")
    )
    ranks = enrichment_order_rank(cleared)
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "beta")] == 0
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "alpha")] == 1
    assert ranks[EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "gamma")] == 2

    removed = remove_enrichment_element(
        enrichment, EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "beta")
    )
    assert [item.key for item in removed.tables] == ["alpha", "gamma"]
    assert [item.group_order for item in removed.tables] == [1, None]
    assert (
        enrichment_order_rank(removed)[
            EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "alpha")
        ]
        == 0
    )

    with pytest.raises(ValueError, match="element not found"):
        clear_group_order(
            enrichment, EnrichmentElementRef(EditorialEnrichmentElementKind.CHART, "missing")
        )


def test_explicit_order_requires_a_paragraph_anchor() -> None:
    base = _populated_enrichment(_extraction(), _synthesis(_extraction()))
    legacy_placement = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD)
    table = _ordering_table(
        base.tables[0], key="legacy_ordered_table", placement=legacy_placement, group_order=0
    )
    with pytest.raises(ValueError, match="requires a paragraph anchor"):
        _ordering_enrichment(tables=(table,))
