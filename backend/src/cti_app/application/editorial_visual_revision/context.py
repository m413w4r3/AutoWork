"""Pure inputs and outputs of the shared visual revision planner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentEvidencePackV1,
    EditorialFigureCatalogEntry,
)
from cti_app.domain.production_editorial_enrichment import (
    ChartSpecV1,
    DiagramSpecV1,
    EditorialEnrichmentV1,
    EnrichmentElementRef,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
)


@dataclass(frozen=True, slots=True)
class VisualRevisionContext:
    """Every immutable input the planner may consult, loaded from pinned artifacts."""

    base_enrichment: EditorialEnrichmentV1
    evidence_pack: EditorialEnrichmentEvidencePackV1 | None
    extraction: ProductionExtractionV1
    synthesis: ProductionSynthesisV1
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = ()
    registered_anchor_ids: frozenset[UUID] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_enrichment, EditorialEnrichmentV1):
            raise ValueError("Visual revision base enrichment is invalid")
        if self.evidence_pack is not None and not isinstance(
            self.evidence_pack, EditorialEnrichmentEvidencePackV1
        ):
            raise ValueError("Visual revision evidence pack is invalid")
        if not isinstance(self.extraction, ProductionExtractionV1):
            raise ValueError("Visual revision extraction is invalid")
        if not isinstance(self.synthesis, ProductionSynthesisV1):
            raise ValueError("Visual revision synthesis is invalid")
        if not isinstance(self.figure_catalog, tuple) or any(
            not isinstance(entry, EditorialFigureCatalogEntry) for entry in self.figure_catalog
        ):
            raise ValueError("Visual revision figure catalog is invalid")
        if self.registered_anchor_ids is not None and (
            not isinstance(self.registered_anchor_ids, frozenset)
            or any(not isinstance(anchor_id, UUID) for anchor_id in self.registered_anchor_ids)
        ):
            raise ValueError("Visual revision registered anchors are invalid")


@dataclass(frozen=True, slots=True)
class VisualRevisionPlan:
    """Fully validated candidate enrichment plus its non-canonical preview."""

    revised_enrichment: EditorialEnrichmentV1
    changed_elements: tuple[EnrichmentElementRef, ...]
    preview: dict[str, object]
    compile_chart: ChartSpecV1 | None = None
    materialize_diagram: DiagramSpecV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revised_enrichment, EditorialEnrichmentV1):
            raise ValueError("Visual revision plan enrichment is invalid")
        if not isinstance(self.changed_elements, tuple) or any(
            not isinstance(item, EnrichmentElementRef) for item in self.changed_elements
        ):
            raise ValueError("Visual revision plan changed elements are invalid")
        if not isinstance(self.preview, dict):
            raise ValueError("Visual revision plan preview is invalid")
        if self.compile_chart is not None and not isinstance(self.compile_chart, ChartSpecV1):
            raise ValueError("Visual revision plan chart compilation target is invalid")
        if self.materialize_diagram is not None and not isinstance(
            self.materialize_diagram, DiagramSpecV1
        ):
            raise ValueError("Visual revision plan diagram materialization target is invalid")


class VisualRevisionRejected(ValueError):
    """A closed, typed refusal raised by the planner or its input adapter."""

    def __init__(
        self,
        code: str,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.diagnostics: dict[str, object] = dict(diagnostics or {})
        super().__init__(code)


# Closed error codes for the implemented variants (F2-DESIGN §2.3). The
# unsupported-operation code is the explicit placeholder for F2-03b/F2-03c.
VISUAL_REVISION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "enrichment_revision_patch_invalid",
        "enrichment_element_not_found",
        "enrichment_element_kind_mismatch",
        "enrichment_target_stale",
        "enrichment_placement_changed",
        "enrichment_caption_invalid",
        "enrichment_move_anchor_invalid",
        "enrichment_order_invalid",
        "enrichment_order_group_unsupported",
        "enrichment_revision_no_change",
        "enrichment_revision_unsupported_operation",
        "bar_chart_insufficient_data",
        "bar_chart_value_not_evidenced",
        "editorial_caption_text_violation",
        "editorial_anchor_stale",
        "proposal_base_stale",
        "proposal_base_unavailable",
        "editorial_read_only",
    }
)


def patch_invalid(reason_code: str, **diagnostics: object) -> VisualRevisionRejected:
    """Build the closed-union refusal shared by every element-revision variant."""
    return VisualRevisionRejected(
        "enrichment_revision_patch_invalid",
        {"reason_code": reason_code, **diagnostics},
    )


def admitted_evidence_refs(ctx: VisualRevisionContext) -> frozenset[ExtractionEvidenceRefV1]:
    """Refs currently admitted for this artifact, or a typed refusal.

    A revision may only cite records admitted under the current TLP/revocation
    policy; without the pack the planner cannot make that claim and refuses
    instead of trusting the patch.
    """
    if ctx.evidence_pack is None:
        raise patch_invalid("evidence_pack_unavailable")
    return frozenset(ctx.evidence_pack._handle_to_ref.values())


def visual_preview_payload(
    *,
    operation: str,
    changed_elements: tuple[EnrichmentElementRef, ...],
    schema_version: int,
    details: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Bounded non-canonical projection stored with the proposal diagnostics."""
    payload: dict[str, object] = {
        "operation": operation,
        "changed_elements": [
            {"kind": item.kind.value, "key": item.key} for item in changed_elements
        ],
        "schema_version": schema_version,
    }
    if details is not None:
        payload.update(details)
    return payload
