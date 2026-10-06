from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from cti_app.application import publication_qa
from cti_app.application.publication_builder import build_publication_document_v4
from cti_app.application.publication_qa import qa_publication_v4, qa_publication_v5
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication import PublicationSectionKind, PublicationSectionV1
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationUncertaintyV1,
    parse_publication_document,
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
    _with_extra_source,
)


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
                "Internal technical heading",
                (publication.lead[0],),
            ),
        ),
    )
    result = _assert_failed(inputs, headed)
    assert result["checks"]["no_internal_synthesis_headings"] is False

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
        )
        rejected = qa_publication_v5(publication=invalid, **inputs)

        assert rejected["checks"]["title_format"] is False
        assert rejected["passed"] is False
