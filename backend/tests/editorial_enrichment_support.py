"""Test-only Editorial Enrichment builders.

Production never fabricates an empty enrichment: an empty artifact is only a
model decision. Tests that need a valid canonical placeholder build it here.
"""

from __future__ import annotations

from cti_app.application.production_editorial_enrichment import canonical_synthesis_hash
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
    EditorialEnrichmentV1,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1


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
