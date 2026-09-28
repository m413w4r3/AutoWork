"""Deterministic preview of a canonical production synthesis."""

from __future__ import annotations

from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
)

SYNTHESIS_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "language",
        "mode",
        "section_count",
        "paragraph_count",
        "timeline_entry_count",
        "evidence_ref_count",
        "uncertainty_count",
        "warnings_count",
        "word_count",
        "model_policy_version",
        "routing_policy_version",
        "synthesis_policy_version",
    }
)


def render_synthesis_markdown(
    synthesis: ProductionSynthesisV1, extraction: ProductionExtractionV1
) -> str:
    """Render the V1 contract for reading; the canonical blob remains authoritative."""
    if synthesis.subject_id != extraction.subject_id:
        raise ValueError("Synthesis and extraction subjects differ")
    source_urls = {source.source_document_id: source.canonical_url for source in extraction.sources}

    def sources(refs: tuple[ExtractionEvidenceRefV1, ...]) -> str:
        urls = sorted({source_urls[ref.source_document_id] for ref in refs})
        return "Sources: " + ", ".join(urls)

    lines = [f"# {synthesis.title}", ""]
    for paragraph in synthesis.lead:
        lines.extend((paragraph.text, sources(paragraph.evidence_refs), ""))
    for section in synthesis.sections:
        lines.extend((f"## {section.heading}", ""))
        for paragraph in section.paragraphs:
            lines.extend((paragraph.text, sources(paragraph.evidence_refs), ""))
    if synthesis.timeline:
        lines.extend(("## Timeline", ""))
        for entry in synthesis.timeline:
            label = entry.date_text or (
                entry.event_date.isoformat() if entry.event_date else "Undated"
            )
            lines.extend((f"- {label}: {entry.text}", f"  {sources(entry.evidence_refs)}"))
        lines.append("")
    if synthesis.uncertainties:
        lines.extend(("## Uncertainties", ""))
        for item in synthesis.uncertainties:
            urls = sorted(source_urls[document_id] for document_id in item.source_document_ids)
            lines.extend((f"- {item.text}", f"  Sources: {', '.join(urls)}"))
        lines.append("")
    if synthesis.warnings:
        lines.extend(("## Warnings", ""))
        lines.extend(f"- {warning}" for warning in synthesis.warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
