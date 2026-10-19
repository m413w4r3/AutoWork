from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any
from uuid import UUID

import pytest

from cti_app.application import publication_qa
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.publication_builder import (
    build_publication_document_v4,
    build_publication_document_v5,
)
from cti_app.application.publication_qa import (
    qa_publication_v4,
    qa_publication_v5,
    qa_publication_v6,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import ExtractionProfile, ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionEventV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    encode_indicator_section_paths,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import RelevanceClassification, RelevanceReasonCode
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    PublicationSourceV1,
    PublicationUncertaintyV1,
    parse_publication_document,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import SemanticParagraphV1, SemanticRole, SemanticTextSpanV1
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _diagram,
    _enrichment_with,
    _figure,
    _frontmatter_v6_case,
    _resolved_figure,
    _table,
    _technical_evidence_publication_case,
    _with_extra_source,
)
from tests.test_publication_document_v6 import _approved_opinion


def _rich_inputs() -> tuple[dict[str, Any], PublicationDocumentV4]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(_table(evidence),),
        diagrams=(_diagram(evidence),),
    )
    publication = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    return {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }, publication


def _timeline_publication_case() -> tuple[dict[str, Any], PublicationDocumentV5]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    event = ExtractionEventV1(
        event_date=date(2025, 1, 15),
        date_text="15 January 2025",
        text="The actor deployed a loader.",
        context="",
        evidence_quote="The actor deployed a loader.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    extraction = replace(extraction, sources=(replace(source, events=(event,)),))
    event_ref = next(
        ref for ref in extraction_evidence_refs_v1(extraction) if ref.kind is EvidenceKind.EVENT
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=event.event_date,
                date_text=event.date_text,
                text=event.text,
                evidence_refs=(event_ref,),
            ),
        ),
    )
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    return {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }, publication


def test_publication_section_order_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    inputs = {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }
    v4 = build_publication_document_v4(**inputs)
    v5_without_ioc_details = build_publication_document_v5(**inputs)
    v5_with_ioc_details, v5_detail_inputs, *_ = _frontmatter_v6_case()
    timeline_inputs, v5_with_chronology = _timeline_publication_case()

    opinion, citation_context = _approved_opinion(v5_without_ioc_details)
    v6_with_opinion = augment_publication_with_opinion(
        v5_without_ioc_details, opinion, citation_context
    )
    v6_without_opinion = PublicationDocumentV6(
        base_publication=v5_without_ioc_details,
        provenance=PublicationDocumentV6ProvenanceV1(
            base_publication_artifact_id=UUID(int=900),
            base_publication_sha256=publication_document_v5_sha256(v5_without_ioc_details),
        ),
    )

    cases = (
        (v4, inputs, qa_publication_v4, ("references", "synthesis")),
        (v5_without_ioc_details, inputs, qa_publication_v5, ("references", "synthesis")),
        (
            v5_with_ioc_details,
            v5_detail_inputs,
            qa_publication_v5,
            # C8 adds body IOC lists before the exhaustive technical annex.
            ("references", "synthesis", "ioc", "technical_annex"),
        ),
        (
            v5_with_chronology,
            timeline_inputs,
            qa_publication_v5,
            # C8 presents the sole sourced chronology before synthesis.
            ("references", "chronology", "synthesis"),
        ),
        (
            v6_without_opinion,
            inputs,
            qa_publication_v6,
            ("references", "synthesis", "analyst_opinion"),
        ),
        (
            v6_with_opinion,
            inputs,
            qa_publication_v6,
            ("references", "synthesis", "analyst_opinion"),
        ),
    )
    for publication, case_inputs, qa, expected_types in cases:
        model = project_publication_to_typst_model(publication)
        assert tuple(section["type"] for section in model.content_sections) == expected_types
        if publication is v6_without_opinion:
            opinion_section = next(
                section
                for section in model.content_sections
                if section["type"] == "analyst_opinion"
            )
            assert opinion_section["empty_message"] == "Aucun avis publié."
        assert qa(publication=publication, **case_inputs)["passed"] is True

    timeline_model = project_publication_to_typst_model(v5_with_chronology)
    monkeypatch.setattr(
        publication_qa,
        "project_publication_to_typst_model",
        lambda _publication: replace(
            timeline_model,
            content_sections=(
                timeline_model.content_sections[1],
                *timeline_model.content_sections[:1],
                *timeline_model.content_sections[2:],
            ),
        ),
    )
    rejected = qa_publication_v5(publication=v5_with_chronology, **timeline_inputs)
    assert rejected["checks"]["references_then_synthesis"] is False
    assert "Publication sections are not ordered" in rejected["errors"][-1]


def _assert_failed(inputs: dict[str, Any], publication: PublicationDocumentV4) -> dict[str, Any]:
    result = qa_publication_v4(publication=publication, **inputs)
    assert result["passed"] is False
    return result


def _with_synthesis_title(
    inputs: dict[str, Any], publication: PublicationDocumentV4, title: str
) -> tuple[dict[str, Any], PublicationDocumentV4]:
    synthesis = replace(inputs["synthesis"], title=title)
    extraction = inputs["extraction"]
    evidence = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(_table(evidence),),
        diagrams=(_diagram(evidence),),
    )
    updated_inputs = {
        **inputs,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }
    return updated_inputs, replace(publication, title=title)


def test_correct_v4_passes_and_title_tampering_fails() -> None:
    inputs, publication = _rich_inputs()
    assert qa_publication_v4(publication=publication, **inputs)["passed"] is True

    tampered = replace(publication, title="Manually changed title")
    result = _assert_failed(inputs, tampered)
    assert result["checks"]["exact_projection"] is False  # type: ignore[index]


def test_qa_accepts_technical_evidence_in_infection_chain_section() -> None:
    inputs, publication = _technical_evidence_publication_case()

    result = qa_publication_v4(publication=publication, **inputs)

    assert result["passed"] is True
    assert result["checks"]["valid_synthesis_section_headings"] is True


def test_v5_qa_excludes_other_case_iocs_from_publication() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    core = extraction.sources[0]
    indicator = ExtractionIndicatorV1(
        value="other-case.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context=encode_indicator_section_paths(
            (((2, "GTG-84006: MEK-aligned activity"), (4, "Indicators of compromise")),)
        ),
        evidence_quote="other-case.example",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(core.source_document_id,),
    )
    extraction = replace(extraction, sources=(replace(core, indicators=(indicator,)),))
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    relevance_projection = build_relevance_projection(snapshot, extraction)
    editorial_enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance_projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
    )
    result = qa_publication_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance_projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
        publication=publication,
    )

    assert result["passed"] is True
    assert result["checks"]["ioc_groups_separated"] is True
    assert result["checks"]["ioc_details_scope"] is True
    assert publication.indicators == ()
    assert publication.original_indicators == ()
    assert all(item.value != "other-case.example" for item in publication.ioc_details)

    out_of_scope_detail = PublicationIOCDetailV1(
        artifact_type=ArtifactType.DOMAIN,
        value="other-case.example",
        normalized_value="other-case.example",
        source_role=core.role,
        context=indicator.context,
        evidence_quote=indicator.evidence_quote,
        evidence_basis=indicator.evidence_basis,
        source_document_ids=(core.source_document_id,),
        disposition=PublicationIOCDisposition.EXCLUDED,
        disposition_reason=RelevanceReasonCode.INDICATOR_SECTION_OTHER_CASE.value,
        relevance_classification=RelevanceClassification.OUT_OF_SCOPE,
        relevance_reason_code=RelevanceReasonCode.INDICATOR_SECTION_OTHER_CASE,
    )
    leaked = replace(publication, ioc_details=(out_of_scope_detail,))
    leaked_result = qa_publication_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance_projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
        publication=leaked,
    )
    assert leaked_result["checks"]["ioc_details_scope"] is False
    assert leaked_result["passed"] is False


def test_context_source_round_trip_qa_and_typst_projection() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    core_source = extraction.sources[0]
    context_id = UUID(int=23)
    content_sha256 = "d" * 64
    indicator = ExtractionIndicatorV1(
        value="NanoDump",
        artifact_type=ArtifactType.OTHER,
        indicator_status=ExtractionIndicatorStatus.CONTEXTUAL,
        context="NanoDump is a memory dumper.",
        evidence_quote="NanoDump is a memory dumper.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(context_id,),
    )
    context_source = replace(
        core_source,
        source_document_id=context_id,
        canonical_url="https://technical.example/nanodump",
        content_sha256=content_sha256,
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        profile=ExtractionProfile.IOC_RULES,
        editorial_role=ProductionEditorialRole.CONTEXT,
        profile_reason_code=None,
        facts=(),
        events=(),
        indicators=(indicator,),
        rules=(),
        uncertainties=(),
    )
    context_reference = replace(
        references.sources[0],
        canonical_url=context_source.canonical_url,
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        role=SourceRole.PRIMARY,
        title="NanoDump reference",
        publisher="Technical Feed",
        source_collection_id=UUID(int=123),
        source_document_id=context_id,
        collection_state=CollectionState.ARCHIVED,
        content_sha256=content_sha256,
        relevance_reason="Operator-selected additive source",
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    references = replace(references, sources=(*references.sources, context_reference))
    extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(references),
        sources=(*extraction.sources, context_source),
    )
    context_ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction)
        if ref.source_document_id == context_id and ref.kind is EvidenceKind.INDICATOR
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        sections=(
            SynthesisSectionV1(
                kind=SynthesisSectionKind.TECHNICAL,
                heading="NanoDump",
                paragraphs=(SynthesisParagraphV1("NanoDump is a memory dumper.", (context_ref,)),),
            ),
        ),
    )
    projection = build_relevance_projection(snapshot, extraction)
    editorial_enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
    )

    source = next(item for item in publication.sources if item.source_document_id == context_id)
    assert isinstance(source, PublicationSourceV1)
    assert source.editorial_role is ProductionEditorialRole.CONTEXT
    assert source.source_collection_id == UUID(int=123)
    assert source.content_sha256 == content_sha256
    assert source.source_criticality is not None
    assert source.criticality_reason is not None
    reference = next(
        item for item in publication.references if item.source_document_id == context_id
    )
    assert reference.text == (
        "Technical Feed : NanoDump reference — ressource technique de contexte."
    )
    qa = qa_publication_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
        publication=publication,
    )
    assert qa["passed"] is True
    assert qa["checks"]["source_provenance"] is True

    restored = parse_publication_document(serialize_publication_document(publication))
    restored_source = next(
        item for item in restored.sources if item.source_document_id == context_id
    )
    assert restored_source == source
    render_model = project_publication_to_typst_model(publication)
    source_row = next(
        item
        for item in render_model.content_sections[0]["sources"]
        if item["url"] == context_source.canonical_url
    )
    assert source_row["publisher"] == "Contexte · Technical Feed"
    reference_row = next(
        item
        for item in render_model.content_sections[0]["timeline"]
        if item["source_urls"] == [context_source.canonical_url]
    )
    assert reference_row["role_label"] == "Contexte technique"
    assert reference_row["display_date"] == "Date de publication non précisée"


def test_table_cell_and_diagram_asset_tampering_fail() -> None:
    inputs, publication = _rich_inputs()
    table = publication.tables[0]
    row = replace(table.rows[0], cells=("tampered", *table.rows[0].cells[1:]))
    table_tampered = replace(publication, tables=(replace(table, rows=(row,)),))
    _assert_failed(inputs, table_tampered)

    diagram_tampered = replace(
        publication,
        diagrams=(replace(publication.diagrams[0], asset_id=UUID(int=999)),),
    )
    _assert_failed(inputs, diagram_tampered)


def test_figure_provenance_and_manual_figure_addition_fail() -> None:
    snapshot, references, extraction, synthesis, _source_url = _with_extra_source(
        *_canonical_inputs()
    )
    source = extraction.sources[-1]
    resolved = _resolved_figure(source.source_document_id, source.canonical_url)
    figure = _figure(source.source_document_id, source.canonical_url, resolved=resolved)
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        source_figures=(figure,),
    )
    publication = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    inputs = {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }

    provenance_tampered = replace(
        publication,
        figures=(replace(publication.figures[0], provenance="Manual provenance"),),
    )
    _assert_failed(inputs, provenance_tampered)

    added = replace(publication.figures[0], key="source_figure_02")
    _assert_failed(inputs, replace(publication, figures=(*publication.figures, added)))


def test_publication_format_checks_reject_headings_uncertainties_language_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, publication = _rich_inputs()

    headed = replace(
        publication,
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                "Evidence pack output",
                (publication.lead[0],),
            ),
        ),
    )
    result = _assert_failed(inputs, headed)
    assert result["checks"]["valid_synthesis_section_headings"] is False

    uncertain = replace(
        publication,
        uncertainties=(
            PublicationUncertaintyV1(
                "Unresolved attribution",
                (publication.sources[0].source_document_id,),
            ),
        ),
    )
    result = _assert_failed(inputs, uncertain)
    assert result["checks"]["no_uncertainty_list"] is False

    wrong_language = replace(publication, publication_language="fr")
    result = _assert_failed(inputs, wrong_language)
    assert result["checks"]["publication_language"] is False

    leaked = replace(publication, title="synthesis_output_invalid")
    result = _assert_failed(inputs, leaked)
    assert result["checks"]["no_diagnostic_code_leaks"] is False

    original_projector = project_publication_to_typst_model
    monkeypatch.setattr(
        publication_qa,
        "project_publication_to_typst_model",
        lambda document: replace(
            original_projector(document),
            content_sections=list(reversed(original_projector(document).content_sections)),
        ),
    )
    result = qa_publication_v4(publication=publication, **inputs)
    assert result["checks"]["references_then_synthesis"] is False
    assert result["passed"] is False


@pytest.mark.parametrize(
    "sentence",
    (
        "Sur la seule base du pack fourni, aucune victime n'est établie.",
        "les éléments fournis ne comportent pas d'information sur la chaîne d'infection.",
        "La base du pack ne décrit pas cette valeur.",
        "Le pack de preuves rassemble ces éléments.",
        "Les evidence packs sont conservés en interne.",
        "Les éléments fournis par le pack ne suffisent pas.",
        "The evidence handles identify internal records.",
        "Le code interne E011 apparaît dans le rapport.",
    ),
)
def test_pipeline_vocabulary_leaks_fail_publication_qa(sentence: str) -> None:
    inputs, publication = _rich_inputs()
    inputs, publication = _with_synthesis_title(inputs, publication, sentence)

    result = _assert_failed(inputs, publication)

    assert result["checks"]["exact_projection"] is True
    assert result["checks"]["no_pipeline_vocabulary_leaks"] is False
    assert any("no_pipeline_vocabulary_leaks" in error for error in result["errors"])


@pytest.mark.parametrize(
    "sentence",
    (
        "Le modèle BDD décrit par la source explique le comportement observé.",
        "Le handle Windows reste ouvert après l'exécution.",
        "L'extraction de données intervient après la connexion.",
        "Le pipeline CI/CD publie les mises à jour signées.",
        "L'exploit pack cible les systèmes anciens.",
        "Le hash SHA-256 a1e011fab reste associé à l'événement.",
    ),
)
def test_cti_vocabulary_and_embedded_hash_ids_pass_publication_qa(sentence: str) -> None:
    inputs, publication = _rich_inputs()
    inputs, publication = _with_synthesis_title(inputs, publication, sentence)

    result = qa_publication_v4(publication=publication, **inputs)

    assert result["passed"] is True
    assert result["checks"]["no_pipeline_vocabulary_leaks"] is True


def test_source_removal_is_rejected_by_the_v4_document_boundary() -> None:
    _inputs, publication = _rich_inputs()
    payload = serialize_publication_document(publication)
    payload["sources"] = payload["sources"][1:]
    with pytest.raises(ValueError):
        parse_publication_document(payload)


def test_injected_evidence_ref_fails_exact_projection() -> None:
    inputs, publication = _rich_inputs()
    payload = serialize_publication_document(publication)
    existing = dict(payload["lead"][0]["evidence_refs"][0])
    existing["evidence_key"] = "0" * 64
    payload["lead"][0]["evidence_refs"].append(existing)
    tampered = parse_publication_document(payload)
    _assert_failed(inputs, tampered)


def test_v6_qa_requires_editorial_title_format() -> None:
    publication, inputs, *_ids = _frontmatter_v6_case()

    valid = qa_publication_v5(publication=publication, **inputs)

    assert valid["checks"]["title_format"] is True
    for title in (
        "Acteur : APT31 | Outil : BlueMoon",
        "**[APT31]** Plusieurs acteurs adoptent rapidement BlueMoon",
        "[APT31] Titre.",
    ):
        base = replace(publication.document, title=title)
        semantic_text = replace(
            publication.semantic_text,
            paragraphs=tuple(
                SemanticParagraphV1(
                    paragraph.anchor,
                    (SemanticTextSpanV1(SemanticRole.TEXT, base.title),),
                )
                if paragraph.anchor == "title"
                else paragraph
                for paragraph in publication.semantic_text.paragraphs
            ),
        )
        invalid = PublicationDocumentV5(
            document=base,
            semantic_text=semantic_text,
            references=publication.references,
            original_indicators=publication.original_indicators,
            ioc_details=publication.ioc_details,
        )
        rejected = qa_publication_v5(publication=invalid, **inputs)

        assert rejected["checks"]["title_format"] is False
        assert rejected["passed"] is False
