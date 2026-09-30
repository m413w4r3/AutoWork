"""Application helpers for Editorial Enrichment lineage and hashing."""

from __future__ import annotations

import hashlib

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
    EditorialEnrichmentV1,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)

EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "bootstrap-empty-v1"


class EditorialEnrichmentValidationError(ValueError):
    """A cross-artifact Editorial Enrichment invariant failed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def canonical_synthesis_hash(synthesis: ProductionSynthesisV1) -> str:
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis))
    ).hexdigest()


def canonical_editorial_enrichment_hash(enrichment: EditorialEnrichmentV1) -> str:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(editorial_enrichment_to_json(enrichment))
    ).hexdigest()


def compute_editorial_enrichment_input_hash(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> str:
    # Import lazily because production_stages may call this module while wiring
    # the newly added stage into the pipeline.
    from cti_app.application.production_stages import compute_input_hash

    return compute_input_hash(
        {
            "stage": "editorial_enrichment",
            "production_input_hash": synthesis.production_input_hash,
            "extraction_hash": canonical_extraction_hash(extraction),
            "synthesis_hash": canonical_synthesis_hash(synthesis),
            "schema_version": EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
            "policy_version": EDITORIAL_ENRICHMENT_POLICY_VERSION,
            "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
        }
    )


def validate_editorial_enrichment(
    enrichment: EditorialEnrichmentV1,
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> None:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")

    if (
        enrichment.subject_id != extraction.subject_id
        or enrichment.subject_id != synthesis.subject_id
    ):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment subject does not match its extraction and synthesis",
        )
    if not (
        enrichment.production_input_hash
        == extraction.production_input_hash
        == synthesis.production_input_hash
    ):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment production input does not match its extraction and synthesis",
        )
    if enrichment.extraction_hash != canonical_extraction_hash(extraction):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment extraction hash does not match the canonical extraction",
        )
    if enrichment.synthesis_hash != canonical_synthesis_hash(synthesis):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment synthesis hash does not match the canonical synthesis",
        )
    if enrichment.publication_language != synthesis.publication_language:
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment publication language does not match the canonical synthesis",
        )

    unknown_refs = editorial_enrichment_evidence_refs(enrichment) - set(
        extraction_evidence_refs_v1(extraction)
    )
    if unknown_refs:
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_evidence_missing",
            "Editorial enrichment references evidence absent from the extraction",
        )

    placements = (
        *(table.placement for table in enrichment.tables),
        *(diagram.placement for diagram in enrichment.diagrams),
        *(figure.placement for figure in enrichment.source_figures),
    )
    for placement in placements:
        if (
            placement.kind.value == "after_section"
            and placement.section_index is not None
            and placement.section_index >= len(synthesis.sections)
        ):
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_placement_invalid",
                "Editorial enrichment placement references a missing synthesis section",
            )

    sources_by_id = {source.source_document_id: source for source in extraction.sources}
    for figure in enrichment.source_figures:
        source = sources_by_id.get(figure.source_document_id)
        if source is None:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "Source figure references a document absent from the extraction",
            )
        if figure.source_url != source.canonical_url:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "Source figure URL differs from the canonical extraction source URL",
            )


def build_empty_editorial_enrichment(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> EditorialEnrichmentV1:
    return EditorialEnrichmentV1(
        schema_version=EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
        subject_id=synthesis.subject_id,
        production_input_hash=synthesis.production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        synthesis_hash=canonical_synthesis_hash(synthesis),
        publication_language=synthesis.publication_language,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_POLICY_VERSION,
        tables=(),
        diagrams=(),
        source_figures=(),
        warnings=(),
    )
