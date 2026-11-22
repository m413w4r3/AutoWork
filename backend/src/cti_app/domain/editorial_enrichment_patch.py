"""Closed typed patches for visual revisions of existing enrichment elements.

Every variant names the exact element it changes; the wire form is persisted in
an editorial proposal patch blob and reparsed by adoption before the same pure
planner is applied under the run fence. An unknown wire kind rejects explicitly
instead of being silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cti_app.domain.editorial_workspace import canonical_content_sha256
from cti_app.domain.production_editorial_enrichment import (
    ChartSpecV1,
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentElementRef,
    EnrichmentPlacementV1,
    chart_spec_from_json,
    chart_spec_to_json,
    diagram_spec_from_json,
    diagram_spec_to_json,
    enrichment_element_order,
    enrichment_order_rank,
)
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation

VISUAL_REVISION_PATCH_SCHEMA_VERSION = "editorial-enrichment-visual-patch-v1"
_MAX_ELEMENT_KEY_CHARS = 128


class EditorialEnrichmentPatchError(ValueError):
    """A persisted or supplied visual revision patch violates its closed contract."""

    code = "enrichment_revision_patch_invalid"


def _element_key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ELEMENT_KEY_CHARS:
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _element_kind(value: Any) -> EditorialEnrichmentElementKind:
    if not isinstance(value, EditorialEnrichmentElementKind):
        raise ValueError("Visual revision element kind is invalid")
    return value


def _caption(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Visual revision caption must be text or null")
    return value


@dataclass(frozen=True, slots=True)
class ReviseChartPatch:
    """Replace one existing chart specification in place (slice F2-03b)."""

    element_key: str
    chart: ChartSpecV1

    def __post_init__(self) -> None:
        _element_key(self.element_key, "Chart revision element key")
        if not isinstance(self.chart, ChartSpecV1):
            raise ValueError("Chart revision requires a ChartSpecV1")
        if self.chart.key != self.element_key:
            raise ValueError("Chart revision target key mismatch")

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.REWRITE_CHART


@dataclass(frozen=True, slots=True)
class ReviseDiagramPatch:
    """Replace one existing diagram specification in place (slice F2-03b)."""

    element_key: str
    diagram: DiagramSpecV1

    def __post_init__(self) -> None:
        _element_key(self.element_key, "Diagram revision element key")
        if not isinstance(self.diagram, DiagramSpecV1):
            raise ValueError("Diagram revision requires a DiagramSpecV1")
        if self.diagram.key != self.element_key:
            raise ValueError("Diagram revision target key mismatch")
        if self.diagram.profile is not None and self.diagram.purpose is None:
            # Every canonical profiled diagram also carries its analytic
            # purpose; refusing here keeps the persisted patch codec total.
            raise ValueError("Diagram revision profile requires an analytic purpose")

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.REWRITE_DIAGRAM


@dataclass(frozen=True, slots=True)
class UpdateVisualCaptionPatch:
    """Change only the caption of one existing visual element."""

    element_kind: EditorialEnrichmentElementKind
    element_key: str
    caption: str | None

    def __post_init__(self) -> None:
        _element_kind(self.element_kind)
        _element_key(self.element_key, "Caption revision element key")
        _caption(self.caption)

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION


@dataclass(frozen=True, slots=True)
class MoveEnrichmentElementPatch:
    """Move one existing element after another registered paragraph anchor."""

    element_kind: EditorialEnrichmentElementKind
    element_key: str
    anchor_block_id: UUID

    def __post_init__(self) -> None:
        _element_kind(self.element_kind)
        _element_key(self.element_key, "Move revision element key")
        if not isinstance(self.anchor_block_id, UUID):
            raise ValueError("Move revision anchor must be a UUID")

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.MOVE_ENRICHMENT_ELEMENT


@dataclass(frozen=True, slots=True)
class ReorderEnrichmentElementsPatch:
    """Set the canonical order of every member of one paragraph anchor group."""

    anchor_block_id: UUID
    order: tuple[EnrichmentElementRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_block_id, UUID):
            raise ValueError("Reorder revision anchor must be a UUID")
        if not isinstance(self.order, tuple) or any(
            not isinstance(item, EnrichmentElementRef) for item in self.order
        ):
            raise ValueError("Reorder revision order must be a tuple of element references")

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS


@dataclass(frozen=True, slots=True)
class ReplaceSourceFigurePatch:
    """Replace one existing source figure by a catalog-resolved figure (F2-03c)."""

    element_key: str
    figure_id: UUID
    caption: str | None = None

    def __post_init__(self) -> None:
        _element_key(self.element_key, "Figure replacement element key")
        if not isinstance(self.figure_id, UUID):
            raise ValueError("Figure replacement identity must be a UUID")
        _caption(self.caption)

    @property
    def element_kind(self) -> EditorialEnrichmentElementKind:
        return EditorialEnrichmentElementKind.FIGURE

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        return TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE


@dataclass(frozen=True, slots=True)
class RemoveEnrichmentElementPatch:
    """Remove one existing enrichment element (slice F2-03c)."""

    element_kind: EditorialEnrichmentElementKind
    element_key: str

    def __post_init__(self) -> None:
        _element_kind(self.element_kind)
        _element_key(self.element_key, "Remove revision element key")

    @property
    def operation(self) -> TargetedEditorialRevisionOperation:
        if self.element_kind is EditorialEnrichmentElementKind.TABLE:
            return TargetedEditorialRevisionOperation.DELETE_TABLE
        if self.element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            return TargetedEditorialRevisionOperation.DELETE_DIAGRAM
        if self.element_kind is EditorialEnrichmentElementKind.CHART:
            return TargetedEditorialRevisionOperation.DELETE_CHART
        return TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE


VisualRevisionPatch = (
    ReviseChartPatch
    | ReviseDiagramPatch
    | UpdateVisualCaptionPatch
    | MoveEnrichmentElementPatch
    | ReorderEnrichmentElementsPatch
    | ReplaceSourceFigurePatch
    | RemoveEnrichmentElementPatch
)

# The operations owning a typed visual-revision planner. Adoption uses this set
# to route a proposal to the visual candidate builder instead of the legacy
# element-patch writer; a new variant must be added here with its planner.
IMPLEMENTED_VISUAL_REVISION_OPERATIONS: frozenset[TargetedEditorialRevisionOperation] = frozenset(
    {
        TargetedEditorialRevisionOperation.REWRITE_CHART,
        TargetedEditorialRevisionOperation.REWRITE_DIAGRAM,
        TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION,
        TargetedEditorialRevisionOperation.MOVE_ENRICHMENT_ELEMENT,
        TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS,
        TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE,
        TargetedEditorialRevisionOperation.DELETE_TABLE,
        TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
        TargetedEditorialRevisionOperation.DELETE_CHART,
        TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
    }
)


def visual_patch_element_kind(
    patch: VisualRevisionPatch,
) -> EditorialEnrichmentElementKind:
    """The enrichment family one typed patch targets (chart/diagram or declared)."""
    if isinstance(patch, ReviseChartPatch):
        return EditorialEnrichmentElementKind.CHART
    if isinstance(patch, ReviseDiagramPatch):
        return EditorialEnrichmentElementKind.DIAGRAM
    if isinstance(
        patch,
        (
            UpdateVisualCaptionPatch,
            MoveEnrichmentElementPatch,
            RemoveEnrichmentElementPatch,
        ),
    ):
        return patch.element_kind
    if isinstance(patch, ReplaceSourceFigurePatch):
        return EditorialEnrichmentElementKind.FIGURE
    raise ValueError("Visual revision patch has no element kind")


def enrichment_order_group_hash(
    enrichment: EditorialEnrichmentV1,
    placement: EnrichmentPlacementV1,
) -> str:
    """Stable CAS fence for one placement group in canonical order.

    This is the reorder proposal's ``before_hash``: the artifact SHA already
    fences the whole payload, while this group hash makes the exact ordered
    membership explicit for prepare, adoption and tests.
    """
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    if not isinstance(placement, EnrichmentPlacementV1):
        raise ValueError("Enrichment placement is invalid")
    group = next(
        (
            candidate
            for candidate in enrichment_element_order(enrichment)
            if candidate.placement == placement
        ),
        None,
    )
    ranks = enrichment_order_rank(enrichment)
    members = group.members if group is not None else ()
    return canonical_content_sha256(
        [
            {"kind": member.kind.value, "key": member.key, "order": ranks[member]}
            for member in members
        ]
    )


def visual_patch_to_json(patch: VisualRevisionPatch) -> dict[str, Any]:
    """Return the persisted, non-canonical wire form of one typed patch.

    ``compiled_asset_id`` is deliberately omitted: a revised chart or diagram
    is recompiled at prepare and again at adoption, so a copied asset identity
    can never be mistaken for the revision's output.
    """
    if isinstance(patch, UpdateVisualCaptionPatch):
        return {
            "kind": "update_visual_caption",
            "element_kind": patch.element_kind.value,
            "element_key": patch.element_key,
            "caption": patch.caption,
        }
    if isinstance(patch, MoveEnrichmentElementPatch):
        return {
            "kind": "move_enrichment_element",
            "element_kind": patch.element_kind.value,
            "element_key": patch.element_key,
            "anchor_block_id": str(patch.anchor_block_id),
        }
    if isinstance(patch, ReorderEnrichmentElementsPatch):
        return {
            "kind": "reorder_enrichment_elements",
            "anchor_block_id": str(patch.anchor_block_id),
            "order": [{"kind": item.kind.value, "key": item.key} for item in patch.order],
        }
    if isinstance(patch, ReviseChartPatch):
        chart = chart_spec_to_json(patch.chart)
        chart.pop("compiled_asset_id", None)
        return {"kind": "revise_chart", "element_key": patch.element_key, "chart": chart}
    if isinstance(patch, ReviseDiagramPatch):
        diagram = diagram_spec_to_json(patch.diagram)
        diagram.pop("compiled_asset_id", None)
        return {
            "kind": "revise_diagram",
            "element_key": patch.element_key,
            "diagram": diagram,
        }
    if isinstance(patch, ReplaceSourceFigurePatch):
        return {
            "kind": "replace_source_figure",
            "element_key": patch.element_key,
            "figure_id": str(patch.figure_id),
            "caption": patch.caption,
        }
    if isinstance(patch, RemoveEnrichmentElementPatch):
        return {
            "kind": "remove_enrichment_element",
            "element_kind": patch.element_kind.value,
            "element_key": patch.element_key,
        }
    raise EditorialEnrichmentPatchError(f"Unsupported visual revision patch {type(patch).__name__}")


def visual_patch_from_json(raw: Any) -> VisualRevisionPatch:
    """Reparse one persisted patch blob into the same closed union.

    Every declared variant round-trips; an unknown wire kind rejects
    explicitly instead of being silently ignored.
    """
    if not isinstance(raw, dict):
        raise EditorialEnrichmentPatchError("Visual revision patch must be an object")
    kind = raw.get("kind")
    try:
        if kind == "update_visual_caption":
            return UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind(str(raw["element_kind"])),
                element_key=str(raw["element_key"]),
                caption=(str(raw["caption"]) if raw.get("caption") is not None else None),
            )
        if kind == "move_enrichment_element":
            return MoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind(str(raw["element_kind"])),
                element_key=str(raw["element_key"]),
                anchor_block_id=UUID(str(raw["anchor_block_id"])),
            )
        if kind == "reorder_enrichment_elements":
            raw_order = raw.get("order")
            if not isinstance(raw_order, list):
                raise ValueError("Reorder order must be a list")
            return ReorderEnrichmentElementsPatch(
                anchor_block_id=UUID(str(raw["anchor_block_id"])),
                order=tuple(
                    EnrichmentElementRef(
                        EditorialEnrichmentElementKind(str(item["kind"])),
                        str(item["key"]),
                    )
                    for item in raw_order
                ),
            )
        if kind == "revise_chart":
            return ReviseChartPatch(
                element_key=str(raw["element_key"]),
                chart=chart_spec_from_json(raw["chart"]),
            )
        if kind == "revise_diagram":
            return ReviseDiagramPatch(
                element_key=str(raw["element_key"]),
                diagram=diagram_spec_from_json(raw["diagram"]),
            )
        if kind == "replace_source_figure":
            return ReplaceSourceFigurePatch(
                element_key=str(raw["element_key"]),
                figure_id=UUID(str(raw["figure_id"])),
                caption=(str(raw["caption"]) if raw.get("caption") is not None else None),
            )
        if kind == "remove_enrichment_element":
            return RemoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind(str(raw["element_kind"])),
                element_key=str(raw["element_key"]),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorialEnrichmentPatchError(f"Visual revision patch is invalid: {exc}") from None
    raise EditorialEnrichmentPatchError(f"Visual revision patch kind {kind!r} is unknown")
