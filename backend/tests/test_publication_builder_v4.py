from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from inspect import Parameter, signature
from uuid import UUID

import pytest

import cti_app.application.publication_builder as publication_builder
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    canonical_editorial_enrichment_hash,
    canonical_synthesis_hash,
    compute_editorial_enrichment_invocation_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_synthesis import (
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
    synthesis_invocation_hash,
)
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    _project_publication_iocs,
    _project_synthesis_publication,
    _validate_publication_lineage,
    _validate_synthesis_evidence_refs,
    build_publication_document_v4,
    build_publication_document_v5,
    compute_assembly_input_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.media_assets import media_asset_id
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
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
    source_figure_id,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationAssemblyErrorCode,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
    PublicationDiagramV1,
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import compute_publication_render_input_hash
from cti_app.domain.semantic_annotation import SEMANTIC_ANNOTATION_POLICY_VERSION
from tests.editorial_enrichment_support import build_empty_editorial_enrichment

_EXTRA_SOURCE_ID = UUID(int=20)
_DEFAULT_DIAGRAM_ASSET_ID = UUID(int=30)


def _enrichment_with(
    *,
    extraction,
    synthesis,
    tables=(),
    diagrams=(),
    source_figures=(),
):
    return replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=tables,
        diagrams=diagrams,
        source_figures=source_figures,
    )


def _table(ref: ExtractionEvidenceRefV1, *, key: str = "commands") -> TableSpecV1:
    return TableSpecV1(
        key=key,
        kind=EnrichmentTableKind.COMMANDS,
        title="Observed commands",
        caption="Commands observed in the report",
        columns=(
            TableColumnV1("command", "Command"),
            TableColumnV1("purpose", "Purpose"),
        ),
        rows=(TableRowV1(("-enc", "Execution"), (ref,)),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
    )


def _diagram(
    ref: ExtractionEvidenceRefV1,
    *,
    asset_id: UUID | None = _DEFAULT_DIAGRAM_ASSET_ID,
    key: str = "infection_chain",
) -> DiagramSpecV1:
    return DiagramSpecV1(
        key=key,
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        title="Infection chain",
        caption="Observed execution flow",
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Loader", (ref,)),
            DiagramNodeV1("payload", "Payload", (ref,)),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (ref,)),),
        groups=(DiagramGroupV1("host", "Victim host", ("loader", "payload")),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        compiled_asset_id=asset_id,
    )


def _resolved_figure(source_document_id: UUID, source_url: str) -> ResolvedSourceFigureV1:
    locator = SourceFigureLocatorV1(page=1, figure_label="Network map")
    sha256 = "d" * 64
    return ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source_document_id,
            sha256=sha256,
            source=source_url,
            locator=locator,
        ),
        blob_id=UUID(int=40),
        sha256=sha256,
        mime_type="image/png",
        byte_size=128,
        source_document_id=source_document_id,
        source=source_url,
        provenance="Archived image asset",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="matched_archived_blob",
    )


def _figure(
    source_document_id: UUID,
    source_url: str,
    *,
    key: str = "source_figure_01",
    status: SourceFigureInclusionStatus = SourceFigureInclusionStatus.INCLUDED,
    resolved: ResolvedSourceFigureV1 | None = None,
) -> SourceFigureCandidateV1:
    locator = resolved.locator if resolved is not None else SourceFigureLocatorV1(page=1)
    return SourceFigureCandidateV1(
        key=key,
        source_document_id=source_document_id,
        source_url=source_url,
        caption="Source architecture",
        provenance="Figure 1 from the source publication",
        locator=locator,
        inclusion_status=status,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        resolved_figure=resolved,
    )


def _with_extra_source(snapshot, references, extraction, synthesis):
    source = extraction.sources[0]
    source_url = "https://example.com/extra-report"
    extra_source = replace(
        source,
        source_document_id=_EXTRA_SOURCE_ID,
        canonical_url=source_url,
        content_sha256="e" * 64,
        facts=tuple(
            replace(fact, source_document_ids=(_EXTRA_SOURCE_ID,)) for fact in source.facts
        ),
    )
    extra_reference = replace(
        references.sources[0],
        canonical_url=source_url,
        source_collection_id=UUID(int=21),
        source_document_id=_EXTRA_SOURCE_ID,
        content_sha256="e" * 64,
        role=SourceRole.INDEPENDENT,
    )
    references = replace(references, sources=(references.sources[0], extra_reference))
    extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(references),
        sources=(source, extra_source),
    )
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    return snapshot, references, extraction, synthesis, source_url


def _build_v4(enrichment=None):
    snapshot, references, extraction, synthesis = _canonical_inputs()
    if enrichment is None:
        enrichment = build_empty_editorial_enrichment(
            extraction=extraction,
            synthesis=synthesis,
        )
    return build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )


def test_empty_enrichment_has_no_rich_content() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    v4 = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    assert v4.tables == v4.diagrams == v4.figures == ()


def test_table_is_projected_without_reformulation_and_keeps_evidence() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    table = _table(evidence)
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(table,),
    )

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert document.tables[0].key == table.key
    assert document.tables[0].caption == table.caption
    assert document.tables[0].columns[0].label == table.columns[0].label
    assert document.tables[0].rows[0].cells == table.rows[0].cells
    assert document.tables[0].rows[0].evidence_refs == table.rows[0].evidence_refs
    assert document.tables[0].placement == table.placement


def test_diagram_is_projected_and_compiled_asset_id_is_preserved() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    diagram = _diagram(extraction_evidence_refs_v1(extraction)[0])
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis, diagrams=(diagram,))

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert document.diagrams == (
        PublicationDiagramV1(
            key=diagram.key,
            kind=diagram.kind,
            title=diagram.title,
            caption=diagram.caption,
            direction=diagram.direction,
            nodes=diagram.nodes,
            edges=diagram.edges,
            groups=diagram.groups,
            placement=diagram.placement,
            asset_id=diagram.compiled_asset_id,
        ),
    )


def test_diagram_without_compiled_asset_is_rejected() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        diagrams=(_diagram(extraction_evidence_refs_v1(extraction)[0], asset_id=None),),
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        build_publication_document_v4(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
    assert failure.value.code is PublicationAssemblyErrorCode.DIAGRAM_ASSET_MISSING


@pytest.mark.parametrize(
    "status", (SourceFigureInclusionStatus.PROPOSED, SourceFigureInclusionStatus.EXCLUDED)
)
def test_non_included_figures_are_silently_absent(status: SourceFigureInclusionStatus) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    snapshot, references, extraction, synthesis, source_url = _with_extra_source(
        snapshot, references, extraction, synthesis
    )
    figure = _figure(_EXTRA_SOURCE_ID, source_url, status=status)
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        source_figures=(figure,),
    )

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert document.figures == ()
    assert _EXTRA_SOURCE_ID not in {source.source_document_id for source in document.sources}


def test_included_figure_uses_content_addressed_asset_and_adds_its_source() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    snapshot, references, extraction, synthesis, source_url = _with_extra_source(
        snapshot, references, extraction, synthesis
    )
    resolved = _resolved_figure(_EXTRA_SOURCE_ID, source_url)
    figure = _figure(_EXTRA_SOURCE_ID, source_url, resolved=resolved)
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        source_figures=(figure,),
    )

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert document.figures[0].asset_id == media_asset_id("d" * 64, "image/png")
    assert document.figures[0].source_document_id == _EXTRA_SOURCE_ID
    assert _EXTRA_SOURCE_ID in {source.source_document_id for source in document.sources}


def test_included_figure_without_resolution_is_rejected() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        source_figures=(_figure(source.source_document_id, source.canonical_url, resolved=None),),
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        _build_v4(enrichment)
    assert failure.value.code is PublicationAssemblyErrorCode.SOURCE_FIGURE_UNRESOLVED


@pytest.mark.parametrize(
    ("field", "expected_code"),
    (
        ("decision", PublicationAssemblyErrorCode.SOURCE_FIGURE_INVALID),
        ("sha256", PublicationAssemblyErrorCode.SOURCE_FIGURE_METADATA_MISSING),
        ("mime_type", PublicationAssemblyErrorCode.SOURCE_FIGURE_METADATA_MISSING),
        ("byte_size", PublicationAssemblyErrorCode.SOURCE_FIGURE_METADATA_MISSING),
    ),
)
def test_included_figure_with_invalid_resolution_is_rejected(field, expected_code) -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    resolved = _resolved_figure(source.source_document_id, source.canonical_url)
    update = {field: SourceFigureDecision.PENDING} if field == "decision" else {field: None}
    invalid_resolved = resolved.model_copy(update=update)
    figure = _figure(source.source_document_id, source.canonical_url, resolved=resolved)
    object.__setattr__(figure, "resolved_figure", invalid_resolved)
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        source_figures=(figure,),
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        _build_v4(enrichment)
    assert failure.value.code is expected_code


def test_enrichment_evidence_absent_from_extraction_is_rejected() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    invalid_ref = ExtractionEvidenceRefV1(
        extraction.sources[0].source_document_id, EvidenceKind.FACT, "0" * 64
    )
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(_table(invalid_ref),),
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        _build_v4(enrichment)
    assert failure.value.code is PublicationAssemblyErrorCode.EVIDENCE_MISSING


def test_table_and_diagram_sources_are_added_to_exact_source_union() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    snapshot, references, extraction, synthesis, _source_url = _with_extra_source(
        snapshot, references, extraction, synthesis
    )
    extra_ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction)
        if ref.source_document_id == _EXTRA_SOURCE_ID
    )
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(_table(extra_ref),),
        diagrams=(_diagram(extra_ref, key="extra_flow", asset_id=UUID(int=31)),),
    )

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert {source.source_document_id for source in document.sources} == {
        *(source.source_document_id for source in extraction.sources),
        _EXTRA_SOURCE_ID,
    }


@pytest.mark.parametrize("field", ("extraction_hash", "synthesis_hash"))
def test_enrichment_lineage_mismatch_is_rejected(field: str) -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        **{field: "0" * 64},
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        _build_v4(enrichment)
    assert failure.value.code is PublicationAssemblyErrorCode.INPUTS_MISMATCH


def test_enrichment_subject_mismatch_is_rejected() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        subject_id=UUID(int=99),
    )

    with pytest.raises(PublicationAssemblyValidationError) as failure:
        _build_v4(enrichment)
    assert failure.value.code is PublicationAssemblyErrorCode.INPUTS_MISMATCH


def test_builder_is_deterministic_in_value_and_serialization() -> None:
    first = _build_v4()
    second = _build_v4()

    assert first == second
    assert json.dumps(
        publication_document_v4_to_json(first), sort_keys=True, separators=(",", ":")
    ) == json.dumps(publication_document_v4_to_json(second), sort_keys=True, separators=(",", ":"))


# Canonical builder validation migrated from the former version-specific suite.


def _canonical_inputs() -> tuple[
    ProductionInputSnapshot,
    ProductionReferenceCorpusV1,
    ProductionExtractionV1,
    ProductionSynthesisV1,
]:
    subject_id = UUID(int=1)
    source_document_id = UUID(int=2)
    content_hash = "c" * 64
    snapshot = ProductionInputSnapshot(
        production_run_id=UUID(int=3),
        edition_id=UUID(int=4),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Example subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=UUID(int=5),
        origin_discovery_subject_id=UUID(int=6),
        canonical_discovery_subject_id=UUID(int=7),
        discovery_snapshot_id=UUID(int=8),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Example discovery summary",
        actor_or_campaign="Example actor",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        publication_language="en",
        research_date=date(2025, 2, 1),
    )
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://example.com/report",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Example report",
                publisher="Example publisher",
                published_at=None,
                source_collection_id=UUID(int=9),
                source_document_id=source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=content_hash,
                relevance_reason="Relevant report",
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    fact = ExtractionFactV1(
        category="actors",
        value="Example actor",
        attack_id=None,
        context="",
        evidence_quote="The source identifies the actor.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_document_id,),
    )
    source = ProductionSourceExtractionV1(
        source_document_id=source_document_id,
        canonical_url="https://example.com/report",
        content_sha256=content_hash,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(
            fact,
            replace(
                fact,
                category="infrastructure",
                value="example.net",
                evidence_quote="The source identifies example.net.",
            ),
        ),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_corpus_hash(references),
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )
    evidence = extraction_evidence_refs_v1(extraction)[0]
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="en",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Example synthesis",
        lead=(SynthesisParagraphV1("Example finding.", (evidence,)),),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    return snapshot, references, extraction, synthesis


def test_assembly_input_hash_uses_exact_canonical_functional_payload() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    payload = {
        "snapshot_input_hash": snapshot.input_hash,
        "references_hash": references_corpus_hash(references),
        "extraction_hash": canonical_extraction_hash(extraction),
        "synthesis_hash": hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis))
        ).hexdigest(),
        "editorial_enrichment_hash": canonical_editorial_enrichment_hash(enrichment),
        "relevance_projection_hash": None,
        "publication_document_schema_version": PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
        "assembly_policy_version": publication_builder.ASSEMBLY_POLICY_VERSION,
        "semantic_annotation_policy_version": SEMANTIC_ANNOTATION_POLICY_VERSION,
    }

    expected_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(payload)
    ).hexdigest()
    legacy_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes({**payload, "assembly_policy_version": "1"})
    ).hexdigest()
    assert expected_hash != legacy_hash
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        == expected_hash
    )
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        == expected_hash
    )

    reconstructed_snapshot = replace(snapshot)
    reconstructed_references = replace(references)
    reconstructed_extraction = replace(extraction)
    reconstructed_synthesis = replace(synthesis)
    assert (
        compute_assembly_input_hash(
            snapshot=reconstructed_snapshot,
            references=reconstructed_references,
            extraction=reconstructed_extraction,
            synthesis=reconstructed_synthesis,
            editorial_enrichment=enrichment,
        )
        == expected_hash
    )


def test_assembly_input_hash_changes_with_each_functional_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    original = compute_assembly_input_hash(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    changed_snapshot = replace(
        snapshot,
        subject_title="Changed subject",
        input_hash="",
        reuse_basis_hash="",
    )
    assert changed_snapshot.input_hash != snapshot.input_hash
    assert (
        compute_assembly_input_hash(
            snapshot=changed_snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )

    changed_reference = replace(references.sources[0], title="Changed report")
    changed_references = replace(references, sources=(changed_reference,))
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=changed_references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )

    changed_extraction = replace(extraction, warnings=("Changed extraction",))
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=changed_extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )

    changed_synthesis = replace(synthesis, title="Changed synthesis")
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=changed_synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )

    changed_enrichment = replace(enrichment, warnings=("Editorial note",))
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=changed_enrichment,
        )
        != original
    )

    monkeypatch.setattr(
        publication_builder,
        "PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION",
        "changed-schema",
    )
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )

    monkeypatch.setattr(
        publication_builder,
        "ASSEMBLY_POLICY_VERSION",
        "3-subject-relevance-projection",
    )
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        != original
    )


def test_assembly_input_hash_api_excludes_runtime_and_renderer_inputs() -> None:
    parameters = signature(compute_assembly_input_hash).parameters
    assert tuple(parameters) == (
        "snapshot",
        "references",
        "extraction",
        "synthesis",
        "editorial_enrichment",
        "relevance_projection",
    )
    assert all(parameter.kind is Parameter.KEYWORD_ONLY for parameter in parameters.values())


def test_semantic_policy_invalidates_assembly_but_not_model_call_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    assembly_before = compute_assembly_input_hash(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    synthesis_call_before = synthesis_invocation_hash(
        snapshot,
        extraction,
        build_synthesis_evidence_pack(snapshot, extraction),
        "4" * 64,
    )
    enrichment_pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    enrichment_call_before = compute_editorial_enrichment_invocation_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(enrichment_pack),
        access_policy_hash="5" * 64,
    )

    monkeypatch.setattr(
        publication_builder,
        "SEMANTIC_ANNOTATION_POLICY_VERSION",
        "semantic-annotation-policy-v2",
    )
    assembly_after = compute_assembly_input_hash(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    synthesis_call_after = synthesis_invocation_hash(
        snapshot,
        extraction,
        build_synthesis_evidence_pack(snapshot, extraction),
        "4" * 64,
    )
    enrichment_call_after = compute_editorial_enrichment_invocation_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(enrichment_pack),
        access_policy_hash="5" * 64,
    )

    assert assembly_after != assembly_before
    assert synthesis_call_after == synthesis_call_before
    assert enrichment_call_after == enrichment_call_before

    render_identity = {
        "publication_artifact_id": UUID(int=77),
        "publication_content_sha256": "1" * 64,
        "renderer": "typst",
        "renderer_version": "publication-v5-typst-v1",
        "template_version": "chp-article-v1",
        "template_sha256": "2" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "format": "pdf",
        "font_bundle_version": "chp-fonts-v1",
        "render_policy_version": "typst-publication-v1",
    }
    palette_before = compute_publication_render_input_hash(**render_identity)
    palette_after = compute_publication_render_input_hash(
        **{**render_identity, "template_sha256": "3" * 64}
    )
    assert palette_after != palette_before
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )
        == assembly_after
    )


def test_publication_v4_validators_accept_matching_canonical_inputs() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()

    _validate_publication_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )
    _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)


def test_publication_v5_applies_deterministic_extraction_roles_without_proposals() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    lead = SynthesisParagraphV1(
        "Example actor used example.net.",
        extraction_evidence_refs_v1(extraction),
    )
    synthesis = replace(synthesis, lead=(lead,))
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)

    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    lead_spans = next(
        paragraph.spans
        for paragraph in document.semantic_text.paragraphs
        if paragraph.anchor == "lead:0001"
    )

    assert not enrichment.annotations
    assert "".join(span.text for span in lead_spans) == document.lead[0].text
    assert any(span.text == "Example actor" and span.role.value == "actor" for span in lead_spans)
    assert any(span.text == "example.net" and span.role.value == "technical" for span in lead_spans)


@pytest.mark.parametrize("artifact", ("snapshot", "references", "extraction", "synthesis"))
def test_publication_v4_lineage_rejects_each_subject_mismatch(artifact: str) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    mismatched_subject_id = UUID(int=99)
    if artifact == "snapshot":
        snapshot = replace(
            snapshot,
            subject_id=mismatched_subject_id,
            input_hash="",
            reuse_basis_hash="",
        )
    elif artifact == "references":
        references = replace(references, subject_id=mismatched_subject_id)
    elif artifact == "extraction":
        extraction = replace(extraction, subject_id=mismatched_subject_id)
    else:
        synthesis = replace(synthesis, subject_id=mismatched_subject_id)

    with pytest.raises(ValueError, match="same subject identity"):
        _validate_publication_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v4_lineage_rejects_extraction_snapshot_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    extraction = replace(extraction, production_input_hash="0" * 64)

    with pytest.raises(ValueError, match="production input snapshot"):
        _validate_publication_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v4_lineage_rejects_synthesis_snapshot_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    synthesis = replace(synthesis, production_input_hash="0" * 64)

    with pytest.raises(ValueError, match="production input snapshot"):
        _validate_publication_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v4_lineage_rejects_references_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    extraction = replace(extraction, references_corpus_hash="0" * 64)

    with pytest.raises(ValueError, match="canonical references corpus"):
        _validate_publication_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v4_lineage_rejects_extraction_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    synthesis = replace(synthesis, extraction_hash="0" * 64)

    with pytest.raises(ValueError, match="canonical extraction"):
        _validate_publication_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


@pytest.mark.parametrize("invalid_part", ("source", "key", "kind"))
def test_synthesis_evidence_validation_rejects_noncurrent_identity(
    invalid_part: str,
) -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    current_ref = extraction_evidence_refs_v1(extraction)[0]
    if invalid_part == "source":
        invalid_ref = replace(current_ref, source_document_id=UUID(int=99))
    elif invalid_part == "key":
        invalid_ref = replace(current_ref, evidence_key="0" * 64)
    else:
        invalid_ref = replace(current_ref, kind=EvidenceKind.EVENT)
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("Invalid citation.", (invalid_ref,)),),
    )

    with pytest.raises(ValueError, match="absent from the current extraction"):
        _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)


def test_synthesis_publication_projection_preserves_narrative_and_order() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)
    assert len(evidence) == 2
    source_document_id = evidence[0].source_document_id
    synthesis = replace(
        synthesis,
        lead=(
            SynthesisParagraphV1("Lead first.", (evidence[1],)),
            SynthesisParagraphV1("Lead second.", (evidence[0],)),
        ),
        sections=(
            SynthesisSectionV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="",
                paragraphs=(SynthesisParagraphV1("Overview text.", (evidence[0],)),),
            ),
            SynthesisSectionV1(
                kind=SynthesisSectionKind.TECHNICAL,
                heading="",
                paragraphs=(
                    SynthesisParagraphV1("Technical first.", (evidence[1],)),
                    SynthesisParagraphV1("Technical second.", (evidence[0],)),
                ),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2025, 1, 8),
                date_text="8 January",
                text="Dated event text.",
                evidence_refs=(evidence[1],),
            ),
            SynthesisTimelineEntryV1(
                event_date=None,
                date_text=None,
                text="Undated event text.",
                evidence_refs=(evidence[0],),
            ),
        ),
        uncertainties=(
            SynthesisUncertaintyV1("Uncertainty one.", (source_document_id,)),
            SynthesisUncertaintyV1("Uncertainty two.", (source_document_id,)),
        ),
    )

    projection = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)

    def publication_ref(ref: ExtractionEvidenceRefV1) -> PublicationEvidenceRefV1:
        return PublicationEvidenceRefV1(
            source_document_id=ref.source_document_id,
            kind=PublicationEvidenceKind(ref.kind.value),
            evidence_key=ref.evidence_key,
        )

    assert projection.lead == (
        PublicationParagraphV1("Lead first.", (publication_ref(evidence[1]),)),
        PublicationParagraphV1("Lead second.", (publication_ref(evidence[0]),)),
    )
    assert projection.sections == (
        PublicationSectionV1(
            PublicationSectionKind.OVERVIEW,
            "",
            (PublicationParagraphV1("Overview text.", (publication_ref(evidence[0]),)),),
        ),
        PublicationSectionV1(
            PublicationSectionKind.TECHNICAL,
            "",
            (
                PublicationParagraphV1("Technical first.", (publication_ref(evidence[1]),)),
                PublicationParagraphV1("Technical second.", (publication_ref(evidence[0]),)),
            ),
        ),
    )
    assert projection.timeline == (
        PublicationTimelineEntryV1(
            date(2025, 1, 8),
            "8 January",
            "Dated event text.",
            (publication_ref(evidence[1]),),
        ),
        PublicationTimelineEntryV1(
            None,
            None,
            "Undated event text.",
            (publication_ref(evidence[0]),),
        ),
    )
    assert projection.uncertainties == ()
    assert projection.used_source_document_ids == frozenset({source_document_id})

    reversed_synthesis = replace(
        synthesis,
        lead=tuple(reversed(synthesis.lead)),
        sections=tuple(reversed(synthesis.sections)),
    )
    reversed_projection = _project_synthesis_publication(
        extraction=extraction,
        synthesis=reversed_synthesis,
    )
    assert reversed_projection.lead == tuple(reversed(projection.lead))
    assert reversed_projection.sections == tuple(reversed(projection.sections))


def test_synthesis_publication_projection_validates_evidence_before_conversion() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    current_ref = extraction_evidence_refs_v1(extraction)[0]
    invalid_ref = replace(current_ref, evidence_key="0" * 64)
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("Invalid citation.", (invalid_ref,)),),
    )

    with pytest.raises(ValueError, match="absent from the current extraction"):
        _project_synthesis_publication(extraction=extraction, synthesis=synthesis)


def test_publication_ioc_projection_normalizes_deduplicates_and_merges_provenance() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    source_a = extraction.sources[0]
    source_b_id = UUID(int=5)

    def indicator(
        value: str,
        artifact_type: ArtifactType,
        status: ExtractionIndicatorStatus,
        source_document_id: UUID,
    ) -> ExtractionIndicatorV1:
        return ExtractionIndicatorV1(
            value=value,
            artifact_type=artifact_type,
            indicator_status=status,
            context="",
            evidence_quote=f"The source identifies {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_document_id,),
        )

    source_a = replace(
        source_a,
        indicators=(
            indicator(
                "Example[.]COM.",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "Analyst[at]Example[.]COM",
                ArtifactType.EMAIL,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "A" * 64,
                ArtifactType.HASH,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "2001:DB8::1",
                ArtifactType.IP,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "HXXPS://Example[.]COM/Path",
                ArtifactType.URL,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "contextual.example",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONTEXTUAL,
                source_a.source_document_id,
            ),
            *(
                indicator(
                    "rule artifact",
                    artifact_type,
                    ExtractionIndicatorStatus.CONFIRMED_IOC,
                    source_a.source_document_id,
                )
                for artifact_type in (
                    ArtifactType.YARA_RULE,
                    ArtifactType.SIGMA_RULE,
                    ArtifactType.SURICATA_RULE,
                )
            ),
        ),
    )
    source_b = replace(
        source_a,
        source_document_id=source_b_id,
        canonical_url="https://example.com/second",
        facts=(),
        events=(),
        indicators=(
            indicator(
                "example.com",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_b_id,
            ),
        ),
        uncertainties=(),
    )
    extraction = replace(extraction, sources=(source_a, source_b))
    synthesis = replace(
        synthesis,
        uncertainties=(
            SynthesisUncertaintyV1("Uncertain finding.", (source_a.source_document_id,)),
        ),
    )
    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)

    projection = _project_publication_iocs(extraction=extraction, narrative=narrative)

    assert projection.indicators == (
        PublicationIndicatorGroupV1(
            ArtifactType.DOMAIN,
            (
                PublicationIndicatorV1(
                    "Example[.]COM.",
                    "example.com",
                    ArtifactType.DOMAIN,
                    (source_a.source_document_id, source_b_id),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.EMAIL,
            (
                PublicationIndicatorV1(
                    "Analyst[at]Example[.]COM",
                    "Analyst@example.com",
                    ArtifactType.EMAIL,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.HASH,
            (
                PublicationIndicatorV1(
                    "A" * 64,
                    "a" * 64,
                    ArtifactType.HASH,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.IP,
            (
                PublicationIndicatorV1(
                    "2001:DB8::1",
                    "2001:db8::1",
                    ArtifactType.IP,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.URL,
            (
                PublicationIndicatorV1(
                    "HXXPS://Example[.]COM/Path",
                    "https://example.com/Path",
                    ArtifactType.URL,
                    (source_a.source_document_id,),
                ),
            ),
        ),
    )
    assert projection.used_source_document_ids == frozenset(
        {source_a.source_document_id, source_b_id}
    )

    reordered_extraction = replace(
        extraction,
        sources=(
            replace(source_a, indicators=tuple(reversed(source_a.indicators))),
            replace(source_b, indicators=tuple(reversed(source_b.indicators))),
        ),
    )
    assert (
        _project_publication_iocs(
            extraction=reordered_extraction,
            narrative=narrative,
        )
        == projection
    )


def test_publication_v4_builder_is_exact_deterministic_and_resolves_used_sources() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source_a = extraction.sources[0]
    source_a_id = source_a.source_document_id
    source_b_id = UUID(int=20)
    unused_source_id = UUID(int=30)

    def indicator(source_document_id: UUID) -> ExtractionIndicatorV1:
        return ExtractionIndicatorV1(
            value="shared.example",
            artifact_type=ArtifactType.DOMAIN,
            indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
            context="",
            evidence_quote="The report identifies shared.example.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_document_id,),
        )

    source_a = replace(source_a, indicators=(indicator(source_a_id),))
    source_b = replace(
        source_a,
        source_document_id=source_b_id,
        canonical_url="https://example.com/second-report",
        content_sha256="d" * 64,
        role=SourceRole.INDEPENDENT,
        facts=(
            replace(
                source_a.facts[0],
                value="Another actor",
                source_document_ids=(source_b_id,),
            ),
        ),
        indicators=(indicator(source_b_id),),
    )
    reference_a = references.sources[0]
    reference_b = replace(
        reference_a,
        canonical_url=source_b.canonical_url,
        role=SourceRole.INDEPENDENT,
        title=None,
        publisher=None,
        published_at=date(2025, 1, 12),
        source_collection_id=UUID(int=10),
        source_document_id=source_b_id,
        content_sha256="d" * 64,
    )
    unused_reference = replace(
        reference_a,
        canonical_url="https://example.com/unused-report",
        source_collection_id=UUID(int=11),
        source_document_id=unused_source_id,
        content_sha256="e" * 64,
    )
    references = replace(
        references,
        sources=(reference_a, reference_b, unused_reference),
    )
    extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(references),
        sources=(source_a, source_b),
    )
    evidence = extraction_evidence_refs_v1(extraction)
    fact_a = next(
        ref
        for ref in evidence
        if ref.source_document_id == source_a_id and ref.kind is EvidenceKind.FACT
    )
    fact_b = next(
        ref
        for ref in evidence
        if ref.source_document_id == source_b_id and ref.kind is EvidenceKind.FACT
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        title="Exact canonical title",
        lead=(
            SynthesisParagraphV1("First lead.", (fact_a,)),
            SynthesisParagraphV1("Second lead.", (fact_b,)),
        ),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "",
                (SynthesisParagraphV1("Overview text.", (fact_a, fact_b)),),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "",
                (SynthesisParagraphV1("Technical text.", (fact_b,)),),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(date(2025, 1, 8), "8 January", "Dated event.", (fact_a,)),
            SynthesisTimelineEntryV1(None, None, "Undated event.", (fact_b,)),
        ),
        uncertainties=(
            SynthesisUncertaintyV1("Attribution remains uncertain.", (source_b_id, source_a_id)),
        ),
    )

    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    ref_a = PublicationEvidenceRefV1(
        fact_a.source_document_id,
        PublicationEvidenceKind(fact_a.kind.value),
        fact_a.evidence_key,
    )
    ref_b = PublicationEvidenceRefV1(
        fact_b.source_document_id,
        PublicationEvidenceKind(fact_b.kind.value),
        fact_b.evidence_key,
    )
    expected = PublicationDocumentV4(
        schema_version="4",
        subject_id=snapshot.subject_id,
        publication_language="fr",
        title="Exact canonical title",
        lead=(
            PublicationParagraphV1("First lead.", (ref_a,)),
            PublicationParagraphV1("Second lead.", (ref_b,)),
        ),
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.OVERVIEW,
                "",
                (PublicationParagraphV1("Overview text.", (ref_a, ref_b)),),
            ),
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                "",
                (PublicationParagraphV1("Technical text.", (ref_b,)),),
            ),
        ),
        timeline=(
            PublicationTimelineEntryV1(date(2025, 1, 8), "8 January", "Dated event.", (ref_a,)),
            PublicationTimelineEntryV1(None, None, "Undated event.", (ref_b,)),
        ),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.DOMAIN,
                (
                    PublicationIndicatorV1(
                        "shared.example",
                        "shared.example",
                        ArtifactType.DOMAIN,
                        (source_a_id, source_b_id),
                    ),
                ),
            ),
        ),
        sources=(
            PublicationSourceV1(
                source_document_id=source_a_id,
                canonical_url=reference_a.canonical_url,
                title=reference_a.title,
                publisher=reference_a.publisher,
                published_at=reference_a.published_at,
                tier=reference_a.tier,
                kind=reference_a.kind,
                role=reference_a.role,
            ),
            PublicationSourceV1(
                source_document_id=source_b_id,
                canonical_url=reference_b.canonical_url,
                title=reference_b.title,
                publisher=reference_b.publisher,
                published_at=reference_b.published_at,
                tier=reference_b.tier,
                kind=reference_b.kind,
                role=reference_b.role,
            ),
        ),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )
    assert document == expected

    canonical_json = json.dumps(
        publication_document_v4_to_json(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    repeated = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    assert (
        json.dumps(
            publication_document_v4_to_json(repeated),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        == canonical_json
    )
    missing_references = replace(
        references,
        sources=tuple(
            source for source in references.sources if source.source_document_id != source_b_id
        ),
    )
    missing_extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(missing_references),
    )
    missing_synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(missing_extraction),
    )
    missing_enrichment = replace(
        enrichment,
        extraction_hash=canonical_extraction_hash(missing_extraction),
        synthesis_hash=canonical_synthesis_hash(missing_synthesis),
    )
    with pytest.raises(ValueError, match="absent from the canonical reference corpus"):
        build_publication_document_v4(
            snapshot=snapshot,
            references=missing_references,
            extraction=missing_extraction,
            synthesis=missing_synthesis,
            editorial_enrichment=missing_enrichment,
        )


def test_publication_builder_removes_internal_headings_but_keeps_section_anchors() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("Lead first paragraph.", (evidence,)),),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Internal technical title",
                (SynthesisParagraphV1("Technical continuation.", (evidence,)),),
            ),
        ),
    )
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)

    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert len(document.sections) == 1
    assert document.sections[0].kind is PublicationSectionKind.TECHNICAL
    assert document.sections[0].heading == ""
    assert document.lead[0].text == "Lead first paragraph."
    assert document.sections[0].paragraphs[0].text == "Technical continuation."
