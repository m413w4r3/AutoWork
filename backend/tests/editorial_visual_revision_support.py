"""Synthetic fixtures for typed chart and diagram visual revisions (F2-03b).

The default editorial fixtures carry no quantitative evidence, so these
builders append the exact FACT records a BAR chart or a diagram revision may
cite. Every reference used by a patch is admitted by the synthetic evidence
pack unless a test deliberately removes it.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from cti_app.application.editorial_visual_revision import VisualRevisionContext
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentEvidencePackV1,
)
from cti_app.application.production_synthesis import _all_evidence_entries
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramNodeV1,
    DiagramProfile,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_extraction import (
    ExtractionFactV1,
    ProductionExtractionV1,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    extraction_evidence_refs_v1,
)
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_production_editorial_enrichment import _extraction, _synthesis

ANCHOR = UUID("11111111-1111-4111-8111-111111111111")
OTHER_ANCHOR = UUID("22222222-2222-4222-8222-222222222222")

ALPHA_VALUE = "Alpha: 12 endpoints"
BETA_VALUE = "Beta: 18 endpoints"


def quantitative_extraction(
    *,
    extra_facts: tuple[tuple[str, str], ...] = (),
) -> tuple[ProductionExtractionV1, dict[str, ExtractionEvidenceRefV1]]:
    """Default synthetic extraction plus the exact numeric FACT records.

    ``extra_facts`` pairs a fact value with its evidence quote, letting a test
    admit a changed number without rewriting the whole fixture.
    """
    base = _extraction()
    source = base.sources[0]
    records = (
        (ALPHA_VALUE, "The synthetic report records Alpha with 12 endpoints."),
        (BETA_VALUE, "The synthetic report records Beta with 18 endpoints."),
        *extra_facts,
    )
    facts = tuple(
        ExtractionFactV1(
            category="other_technical",
            value=value,
            attack_id=None,
            context=f"Synthetic quantitative fixture {value}.",
            evidence_quote=quote,
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        )
        for value, quote in records
    )
    extraction = replace(
        base,
        sources=(replace(source, facts=(*source.facts, *facts)),),
    )
    refs: dict[str, ExtractionEvidenceRefV1] = {
        str(payload.get("value")): ref for ref, payload in _all_evidence_entries(extraction).items()
    }
    return extraction, refs


def quantitative_synthesis(extraction: ProductionExtractionV1) -> ProductionSynthesisV1:
    return _synthesis(extraction)


def evidence_pack(
    refs: tuple[ExtractionEvidenceRefV1, ...],
    *,
    registered_anchors: frozenset[UUID] = frozenset({ANCHOR, OTHER_ANCHOR}),
) -> EditorialEnrichmentEvidencePackV1:
    return EditorialEnrichmentEvidencePackV1(
        publication_language="fr",
        current_synthesis={"lead": [], "sections": []},
        narrative_evidence=(),
        technical_evidence=(),
        reserve_evidence=(),
        source_pair_relations=(),
        registered_anchor_ids=registered_anchors,
        _handle_to_ref={f"E{index:03d}": ref for index, ref in enumerate(refs, start=1)},
    )


def anchored(anchor_id: UUID = ANCHOR) -> EnrichmentPlacementV1:
    return EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor_id)


def purpose(
    question: str,
    refs: tuple[ExtractionEvidenceRefV1, ...],
) -> EditorialAnalyticPurposeV1:
    return EditorialAnalyticPurposeV1(
        question=question,
        available_data="The synthetic extraction records the exact documented counts.",
        comprehension_gain="The comparison exposes the documented difference.",
        scope="Only the synthetic records cited here.",
        evidence_refs=refs,
        knowledge_limits="No unreported value is inferred.",
        placement_reason="Place after the anchor paragraph.",
    )


def bar_chart(
    *,
    key: str = "endpoint_counts",
    refs: dict[str, ExtractionEvidenceRefV1],
    values: tuple[tuple[str, str, ExtractionEvidenceRefV1], ...] | None = None,
    title: str = "Documented endpoint counts",
    caption: str | None = "Synthetic endpoint counts.",
    unit: str = "endpoints",
    placement: EnrichmentPlacementV1 | None = None,
    question: str = "Which endpoint count is documented for each synthetic category?",
    compiled_asset_id: UUID | None = None,
    category_axis_label: str | None = "Source record",
    value_axis_label: str | None = "Documented count",
) -> ChartSpecV1:
    resolved = values or (
        ("Alpha", "12", refs[ALPHA_VALUE]),
        ("Beta", "18", refs[BETA_VALUE]),
    )
    point_refs = tuple(ref for _label, _value, ref in resolved)
    return ChartSpecV1(
        key=key,
        kind=ChartKind.BAR,
        title=title,
        caption=caption,
        placement=placement or anchored(),
        purpose=purpose(question, point_refs),
        points=tuple(
            ChartPointV1(
                label=label,
                date=None,
                series="Endpoints",
                evidence_refs=(ref,),
                value=Decimal(value),
            )
            for label, value, ref in resolved
        ),
        compiled_asset_id=compiled_asset_id,
        category_axis_label=category_axis_label,
        value_axis_label=value_axis_label,
        unit=unit,
    )


def flow_diagram(
    *,
    key: str = "infection_flow",
    refs: dict[str, ExtractionEvidenceRefV1],
    title: str = "Documented infection flow",
    caption: str = "Synthetic flow.",
    placement: EnrichmentPlacementV1 | None = None,
    question: str = "Which documented action links the loader to the payload?",
    compiled_asset_id: UUID | None = None,
) -> DiagramSpecV1:
    alpha_ref = refs[ALPHA_VALUE]
    beta_ref = refs[BETA_VALUE]
    return DiagramSpecV1(
        key=key,
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        profile=DiagramProfile.FLOW,
        title=title,
        caption=caption,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Alpha", (alpha_ref,)),
            DiagramNodeV1("payload", "Beta", (beta_ref,)),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (alpha_ref,)),),
        groups=(),
        placement=placement or EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        compiled_asset_id=compiled_asset_id,
        purpose=purpose(question, (alpha_ref,)),
    )


def enrichment_with(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    charts: tuple[ChartSpecV1, ...] = (),
    diagrams: tuple[DiagramSpecV1, ...] = (),
) -> EditorialEnrichmentV1:
    return replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        charts=charts,
        diagrams=diagrams,
    )


def visual_context(
    *,
    enrichment: EditorialEnrichmentV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    admitted_refs: tuple[ExtractionEvidenceRefV1, ...] | None = None,
    evidence_pack_required: bool = True,
) -> VisualRevisionContext:
    refs = (
        tuple(extraction_evidence_refs_v1(extraction)) if admitted_refs is None else admitted_refs
    )
    return VisualRevisionContext(
        base_enrichment=enrichment,
        evidence_pack=evidence_pack(refs) if evidence_pack_required else None,
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({ANCHOR, OTHER_ANCHOR}),
    )
