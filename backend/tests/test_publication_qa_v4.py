from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.publication_builder import build_publication_document_v4
from cti_app.application.publication_qa import qa_publication_v4
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    parse_publication_document,
    serialize_publication_document,
)
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _diagram,
    _enrichment_with,
    _figure,
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
