"""Canonical PublicationDocumentV4 model, validation and strict JSON codec."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.investigation import DetectionCandidateStatus
from cti_app.domain.investigation_graph import InvestigationGraph
from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureLocatorV1,
    diagram_edge_from_json,
    diagram_edge_to_json,
    diagram_group_from_json,
    diagram_group_to_json,
    diagram_node_from_json,
    diagram_node_to_json,
    evidence_ref_to_json,
    evidence_refs_from_json,
    json_array,
    json_enum,
    json_int,
    json_sha256,
    json_text,
    json_uuid,
    normalize_evidence_refs,
    placement_from_json,
    placement_to_json,
    source_figure_locator_from_json,
    source_figure_locator_to_json,
    validate_editorial_key,
    validate_sha256,
    validate_text,
)
from cti_app.domain.production_editorial_enrichment import publication_json_object as json_object
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
)
from cti_app.domain.production_relevance import RelevanceClassification, RelevanceReasonCode
from cti_app.domain.production_synthesis import ExtractionEvidenceRefV1
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationReferenceEntryV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
    indicator_source_ids,
)
from cti_app.domain.rule_workbench import RuleValidationStatus
from cti_app.domain.semantic_annotation import (
    SemanticTextV1,
    semantic_text_from_json,
    semantic_text_to_json,
)

PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION = "4"
PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION = "5"
PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION = "6"
_PUBLICATION_SOURCE_REQUIRED_FIELDS = frozenset(
    {
        "source_document_id",
        "canonical_url",
        "title",
        "publisher",
        "published_at",
        "tier",
        "kind",
        "role",
    }
)
_PUBLICATION_SOURCE_PROVENANCE_FIELDS = frozenset(
    {
        "editorial_role",
        "source_criticality",
        "criticality_reason",
        "source_collection_id",
        "content_sha256",
    }
)


def _publication_source_object(raw: Any) -> Mapping[str, Any]:
    """Read legacy source rows and rows with optional provenance fields."""
    if isinstance(raw, Mapping):
        keys = set(raw)
        if _PUBLICATION_SOURCE_REQUIRED_FIELDS <= keys and keys <= (
            _PUBLICATION_SOURCE_REQUIRED_FIELDS | _PUBLICATION_SOURCE_PROVENANCE_FIELDS
        ):
            return raw
    return json_object(raw, _PUBLICATION_SOURCE_REQUIRED_FIELDS, "Publication source")


@dataclass(frozen=True, slots=True)
class PublicationTableColumnV1:
    key: str
    label: str

    def __post_init__(self) -> None:
        validate_text(self.key, "Publication table column key", semantic=True)
        validate_text(self.label, "Publication table column label", semantic=True)


@dataclass(frozen=True, slots=True)
class PublicationTableRowV1:
    cells: tuple[str, ...]
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cells, tuple) or any(
            not isinstance(cell, str) for cell in self.cells
        ):
            raise ValueError("Publication table row cells must be a tuple of text")
        object.__setattr__(
            self,
            "evidence_refs",
            normalize_evidence_refs(
                self.evidence_refs, "Publication table row evidence references"
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicationTableV1:
    key: str
    kind: EnrichmentTableKind
    title: str
    caption: str | None
    columns: tuple[PublicationTableColumnV1, ...]
    rows: tuple[PublicationTableRowV1, ...]
    placement: EnrichmentPlacementV1

    def __post_init__(self) -> None:
        validate_editorial_key(self.key, "Publication table key")
        if not isinstance(self.kind, EnrichmentTableKind):
            raise ValueError("Publication table kind is invalid")
        validate_text(self.title, "Publication table title", semantic=True)
        if self.caption is not None:
            validate_text(self.caption, "Publication table caption")
            if not self.caption.strip():
                object.__setattr__(self, "caption", None)
        if not isinstance(self.columns, tuple) or any(
            not isinstance(column, PublicationTableColumnV1) for column in self.columns
        ):
            raise ValueError("Publication table columns must be a tuple of columns")
        if len(self.columns) < 2:
            raise ValueError("Publication tables require at least two columns")
        column_keys = [column.key for column in self.columns]
        if len(column_keys) != len(set(column_keys)):
            raise ValueError("Publication table column keys must be unique")
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, PublicationTableRowV1) for row in self.rows
        ):
            raise ValueError("Publication table rows must be a tuple of rows")
        if not self.rows:
            raise ValueError("Publication tables require at least one row")
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError("Publication table row width must match the number of columns")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Publication table placement is invalid")


@dataclass(frozen=True, slots=True)
class PublicationDiagramV1:
    key: str
    kind: EnrichmentDiagramKind
    title: str
    caption: str | None
    direction: EnrichmentDiagramDirection
    nodes: tuple[DiagramNodeV1, ...]
    edges: tuple[DiagramEdgeV1, ...]
    groups: tuple[DiagramGroupV1, ...]
    placement: EnrichmentPlacementV1
    asset_id: UUID

    def __post_init__(self) -> None:
        validate_editorial_key(self.key, "Publication diagram key")
        if not isinstance(self.kind, EnrichmentDiagramKind):
            raise ValueError("Publication diagram kind is invalid")
        validate_text(self.title, "Publication diagram title", semantic=True)
        if self.caption is not None:
            validate_text(self.caption, "Publication diagram caption")
            if not self.caption.strip():
                object.__setattr__(self, "caption", None)
        if not isinstance(self.direction, EnrichmentDiagramDirection):
            raise ValueError("Publication diagram direction is invalid")
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(node, DiagramNodeV1) for node in self.nodes
        ):
            raise ValueError("Publication diagram nodes must be a tuple of DiagramNodeV1")
        if len(self.nodes) < 2:
            raise ValueError("Publication diagrams require at least two nodes")
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Publication diagram node IDs must be unique")
        if not isinstance(self.edges, tuple) or any(
            not isinstance(edge, DiagramEdgeV1) for edge in self.edges
        ):
            raise ValueError("Publication diagram edges must be a tuple of DiagramEdgeV1")
        if not self.edges:
            raise ValueError("Publication diagrams require at least one edge")
        node_id_set = set(node_ids)
        if any(
            edge.source_node_id not in node_id_set or edge.target_node_id not in node_id_set
            for edge in self.edges
        ):
            raise ValueError("Publication diagram edges must reference existing nodes")
        if not isinstance(self.groups, tuple) or any(
            not isinstance(group, DiagramGroupV1) for group in self.groups
        ):
            raise ValueError("Publication diagram groups must be a tuple of DiagramGroupV1")
        group_ids = tuple(group.group_id for group in self.groups)
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("Publication diagram group IDs must be unique")
        if any(not set(group.node_ids) <= node_id_set for group in self.groups):
            raise ValueError("Publication diagram groups must reference existing nodes")
        grouped_node_ids = [node_id for group in self.groups for node_id in group.node_ids]
        if len(grouped_node_ids) != len(set(grouped_node_ids)):
            raise ValueError("Publication diagram groups must not share nodes")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Publication diagram placement is invalid")
        if not isinstance(self.asset_id, UUID):
            raise ValueError("Publication diagram asset identity must be a UUID")


@dataclass(frozen=True, slots=True)
class PublicationChartV1:
    key: str
    kind: ChartKind
    title: str
    caption: str | None
    placement: EnrichmentPlacementV1
    asset_id: UUID
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        validate_editorial_key(self.key, "Publication chart key")
        if self.kind is not ChartKind.TIMELINE:
            raise ValueError("Publication chart kind is invalid")
        validate_text(self.title, "Publication chart title", semantic=True)
        if self.caption is not None:
            validate_text(self.caption, "Publication chart caption")
            if not self.caption.strip():
                object.__setattr__(self, "caption", None)
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Publication chart placement is invalid")
        if not isinstance(self.asset_id, UUID):
            raise ValueError("Publication chart asset identity must be a UUID")
        object.__setattr__(
            self,
            "evidence_refs",
            normalize_evidence_refs(self.evidence_refs, "Publication chart evidence references"),
        )


@dataclass(frozen=True, slots=True)
class PublicationSourceFigureV1:
    key: str
    asset_id: UUID
    sha256: str
    mime_type: str
    byte_size: int
    source_document_id: UUID
    source_url: str
    caption: str
    provenance: str
    locator: SourceFigureLocatorV1
    placement: EnrichmentPlacementV1

    def __post_init__(self) -> None:
        validate_editorial_key(self.key, "Publication source figure key")
        if not isinstance(self.asset_id, UUID):
            raise ValueError("Publication source figure asset identity must be a UUID")
        validate_sha256(self.sha256, "Publication source figure SHA-256")
        if not isinstance(self.mime_type, str) or self.mime_type not in SUPPORTED_MEDIA_MIME_TYPES:
            raise ValueError("Publication source figure MIME type is not supported")
        if type(self.byte_size) is not int or self.byte_size <= 0:
            raise ValueError("Publication source figure byte size must be positive")
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Publication source figure document identity must be a UUID")
        validate_text(self.source_url, "Publication source figure URL", semantic=True)
        validate_text(self.caption, "Publication source figure caption", semantic=True)
        validate_text(self.provenance, "Publication source figure provenance", semantic=True)
        if not isinstance(self.locator, SourceFigureLocatorV1):
            raise ValueError("Publication source figure locator is invalid")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Publication source figure placement is invalid")


def _date_from_json(value: Any, label: str) -> date | None:
    if value is None:
        return None
    text = json_text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date string or null") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use canonical ISO date form")
    return parsed


def _publication_evidence_refs_from_json(value: Any) -> tuple[PublicationEvidenceRefV1, ...]:
    fields = frozenset({"source_document_id", "kind", "evidence_key"})
    return tuple(
        PublicationEvidenceRefV1(
            source_document_id=json_uuid(item["source_document_id"], "Evidence source_document_id"),
            kind=json_enum(PublicationEvidenceKind, item["kind"], "Evidence kind"),
            evidence_key=json_text(item["evidence_key"], "Evidence key"),
        )
        for item in (
            json_object(raw, fields, "Evidence reference")
            for raw in json_array(value, "Evidence references")
        )
    )


def _source_ids_from_json(value: Any, label: str) -> tuple[UUID, ...]:
    return tuple(json_uuid(item, label) for item in json_array(value, label))


def _table_to_json(table: PublicationTableV1) -> dict[str, Any]:
    return {
        "key": table.key,
        "kind": table.kind.value,
        "title": table.title,
        "caption": table.caption,
        "columns": [{"key": column.key, "label": column.label} for column in table.columns],
        "rows": [
            {
                "cells": list(row.cells),
                "evidence_refs": [evidence_ref_to_json(ref) for ref in row.evidence_refs],
            }
            for row in table.rows
        ],
        "placement": placement_to_json(table.placement),
    }


def _table_from_json(raw: Any) -> PublicationTableV1:
    item = json_object(
        raw,
        frozenset({"key", "kind", "title", "caption", "columns", "rows", "placement"}),
        "Publication table",
    )
    columns = tuple(
        PublicationTableColumnV1(
            key=json_text(column["key"], "Table column key"),
            label=json_text(column["label"], "Table column label"),
        )
        for column in (
            json_object(value, frozenset({"key", "label"}), "Publication table column")
            for value in json_array(item["columns"], "Table columns")
        )
    )
    rows = tuple(
        PublicationTableRowV1(
            cells=tuple(
                json_text(cell, "Table cell")
                for cell in json_array(row["cells"], "Table row cells")
            ),
            evidence_refs=evidence_refs_from_json(
                row["evidence_refs"], "Table row evidence references"
            ),
        )
        for row in (
            json_object(value, frozenset({"cells", "evidence_refs"}), "Publication table row")
            for value in json_array(item["rows"], "Table rows")
        )
    )
    caption = item["caption"]
    if caption is not None:
        caption = json_text(caption, "Table caption")
    return PublicationTableV1(
        key=json_text(item["key"], "Table key"),
        kind=json_enum(EnrichmentTableKind, item["kind"], "Table kind"),
        title=json_text(item["title"], "Table title"),
        caption=caption,
        columns=columns,
        rows=rows,
        placement=placement_from_json(item["placement"]),
    )


def _diagram_to_json(
    diagram: PublicationDiagramV1, *, include_node_roles: bool = False
) -> dict[str, Any]:
    return {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [
            diagram_node_to_json(node, include_role=include_node_roles) for node in diagram.nodes
        ],
        "edges": [diagram_edge_to_json(edge) for edge in diagram.edges],
        "groups": [diagram_group_to_json(group) for group in diagram.groups],
        "placement": placement_to_json(diagram.placement),
        "asset_id": str(diagram.asset_id),
    }


def _diagram_from_json(raw: Any, *, require_node_roles: bool = False) -> PublicationDiagramV1:
    item = json_object(
        raw,
        frozenset(
            {
                "key",
                "kind",
                "title",
                "caption",
                "direction",
                "nodes",
                "edges",
                "groups",
                "placement",
                "asset_id",
            }
        ),
        "Publication diagram",
    )
    caption = item["caption"]
    if caption is not None:
        caption = json_text(caption, "Diagram caption")
    return PublicationDiagramV1(
        key=json_text(item["key"], "Diagram key"),
        kind=json_enum(EnrichmentDiagramKind, item["kind"], "Diagram kind"),
        title=json_text(item["title"], "Diagram title"),
        caption=caption,
        direction=json_enum(EnrichmentDiagramDirection, item["direction"], "Diagram direction"),
        nodes=tuple(
            diagram_node_from_json(value, require_role=require_node_roles)
            for value in json_array(item["nodes"], "Diagram nodes")
        ),
        edges=tuple(
            diagram_edge_from_json(value) for value in json_array(item["edges"], "Diagram edges")
        ),
        groups=tuple(
            diagram_group_from_json(value) for value in json_array(item["groups"], "Diagram groups")
        ),
        placement=placement_from_json(item["placement"]),
        asset_id=json_uuid(item["asset_id"], "Diagram asset ID"),
    )


def _chart_to_json(chart: PublicationChartV1) -> dict[str, Any]:
    return {
        "key": chart.key,
        "kind": chart.kind.value,
        "title": chart.title,
        "caption": chart.caption,
        "placement": placement_to_json(chart.placement),
        "asset_id": str(chart.asset_id),
        "evidence_refs": [evidence_ref_to_json(ref) for ref in chart.evidence_refs],
    }


def _chart_from_json(raw: Any) -> PublicationChartV1:
    item = json_object(
        raw,
        frozenset({"key", "kind", "title", "caption", "placement", "asset_id", "evidence_refs"}),
        "Publication chart",
    )
    caption = item["caption"]
    if caption is not None:
        caption = json_text(caption, "Chart caption")
    return PublicationChartV1(
        key=json_text(item["key"], "Chart key"),
        kind=json_enum(ChartKind, item["kind"], "Chart kind"),
        title=json_text(item["title"], "Chart title"),
        caption=caption,
        placement=placement_from_json(item["placement"]),
        asset_id=json_uuid(item["asset_id"], "Chart asset ID"),
        evidence_refs=evidence_refs_from_json(
            item["evidence_refs"], "Publication chart evidence references"
        ),
    )


def _figure_to_json(figure: PublicationSourceFigureV1) -> dict[str, Any]:
    return {
        "key": figure.key,
        "asset_id": str(figure.asset_id),
        "sha256": figure.sha256,
        "mime_type": figure.mime_type,
        "byte_size": figure.byte_size,
        "source_document_id": str(figure.source_document_id),
        "source_url": figure.source_url,
        "caption": figure.caption,
        "provenance": figure.provenance,
        "locator": source_figure_locator_to_json(figure.locator),
        "placement": placement_to_json(figure.placement),
    }


def _figure_from_json(raw: Any) -> PublicationSourceFigureV1:
    item = json_object(
        raw,
        frozenset(
            {
                "key",
                "asset_id",
                "sha256",
                "mime_type",
                "byte_size",
                "source_document_id",
                "source_url",
                "caption",
                "provenance",
                "locator",
                "placement",
            }
        ),
        "Publication source figure",
    )
    return PublicationSourceFigureV1(
        key=json_text(item["key"], "Source figure key"),
        asset_id=json_uuid(item["asset_id"], "Source figure asset ID"),
        sha256=json_sha256(item["sha256"], "Source figure SHA-256"),
        mime_type=json_text(item["mime_type"], "Source figure MIME type"),
        byte_size=json_int(item["byte_size"], "Source figure byte size"),
        source_document_id=json_uuid(
            item["source_document_id"], "Source figure source_document_id"
        ),
        source_url=json_text(item["source_url"], "Source figure URL"),
        caption=json_text(item["caption"], "Source figure caption"),
        provenance=json_text(item["provenance"], "Source figure provenance"),
        locator=source_figure_locator_from_json(item["locator"]),
        placement=placement_from_json(item["placement"]),
    )


def _validate_publication_core(
    *,
    subject_id: UUID,
    publication_language: str,
    title: str,
    lead: tuple[PublicationParagraphV1, ...],
    sections: tuple[PublicationSectionV1, ...],
    timeline: tuple[PublicationTimelineEntryV1, ...],
    indicators: tuple[PublicationIndicatorGroupV1, ...],
    sources: tuple[PublicationSourceV1, ...],
    uncertainties: tuple[PublicationUncertaintyV1, ...],
    additional_source_ids: set[UUID] | frozenset[UUID] = frozenset(),
) -> None:
    if not isinstance(subject_id, UUID):
        raise ValueError("Publication subject identity must be a UUID")
    if not isinstance(publication_language, str) or not publication_language.strip():
        raise ValueError("Publication language must be non-empty text")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Publication title must be non-empty text")
    tuple_fields: tuple[tuple[str, Any, type[Any]], ...] = (
        ("lead", lead, PublicationParagraphV1),
        ("sections", sections, PublicationSectionV1),
        ("timeline", timeline, PublicationTimelineEntryV1),
        ("indicators", indicators, PublicationIndicatorGroupV1),
        ("sources", sources, PublicationSourceV1),
        ("uncertainties", uncertainties, PublicationUncertaintyV1),
    )
    for label, items, item_type in tuple_fields:
        if not isinstance(items, tuple):
            raise ValueError(f"Publication {label} must be a tuple")
        if any(not isinstance(item, item_type) for item in items):
            raise ValueError(f"Publication {label} contains an invalid value type")
    indicator_types = tuple(group.artifact_type for group in indicators)
    if len(set(indicator_types)) != len(indicator_types):
        raise ValueError("Publication indicator groups must not repeat artifact types")
    source_ids = tuple(source.source_document_id for source in sources)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("Publication sources must not repeat source_document_id values")
    if len(set(uncertainties)) != len(uncertainties):
        raise ValueError("Publication uncertainties must not repeat")
    used_source_ids = {
        ref.source_document_id for paragraph in lead for ref in paragraph.evidence_refs
    }
    used_source_ids.update(
        ref.source_document_id
        for section in sections
        for paragraph in section.paragraphs
        for ref in paragraph.evidence_refs
    )
    used_source_ids.update(
        ref.source_document_id for entry in timeline for ref in entry.evidence_refs
    )
    used_source_ids.update(
        source_id
        for group in indicators
        for indicator in group.indicators
        for source_id in indicator.source_document_ids
    )
    used_source_ids.update(
        source_id for uncertainty in uncertainties for source_id in uncertainty.source_document_ids
    )
    used_source_ids.update(additional_source_ids)
    if used_source_ids != set(source_ids):
        unknown = sorted(map(str, used_source_ids - set(source_ids)))
        unused = sorted(map(str, set(source_ids) - used_source_ids))
        raise ValueError(
            "Publication sources must exactly match the source identities used by the "
            f"document (unknown={unknown}, unused={unused})"
        )


def _indicator_group_to_json(group: PublicationIndicatorGroupV1) -> dict[str, Any]:
    return {
        "artifact_type": group.artifact_type.value,
        "indicators": [
            {
                "value": indicator.value,
                "normalized_value": indicator.normalized_value,
                "artifact_type": indicator.artifact_type.value,
                "source_document_ids": [
                    str(source_id) for source_id in indicator.source_document_ids
                ],
            }
            for indicator in group.indicators
        ],
    }


def _indicator_group_from_json(raw: Any, label: str) -> PublicationIndicatorGroupV1:
    item = json_object(raw, frozenset({"artifact_type", "indicators"}), label)
    return PublicationIndicatorGroupV1(
        artifact_type=json_enum(ArtifactType, item["artifact_type"], "Indicator group type"),
        indicators=tuple(
            PublicationIndicatorV1(
                value=json_text(nested["value"], "Indicator value"),
                normalized_value=json_text(
                    nested["normalized_value"], "Indicator normalized_value"
                ),
                artifact_type=json_enum(
                    ArtifactType, nested["artifact_type"], "Indicator artifact_type"
                ),
                source_document_ids=_source_ids_from_json(
                    nested["source_document_ids"], "Indicator source_document_ids"
                ),
            )
            for nested in (
                json_object(
                    raw_indicator,
                    frozenset(
                        {
                            "value",
                            "normalized_value",
                            "artifact_type",
                            "source_document_ids",
                        }
                    ),
                    "Publication indicator",
                )
                for raw_indicator in json_array(item["indicators"], "Indicators in group")
            )
        ),
    )


def _publication_reference_to_json(item: PublicationReferenceEntryV1) -> dict[str, Any]:
    return {
        "source_document_id": str(item.source_document_id),
        "text": item.text,
        "evidence_refs": [
            {
                "source_document_id": str(ref.source_document_id),
                "kind": ref.kind.value,
                "evidence_key": ref.evidence_key,
            }
            for ref in item.evidence_refs
        ],
    }


def _publication_reference_from_json(raw: Any) -> PublicationReferenceEntryV1:
    item = json_object(
        raw,
        frozenset({"source_document_id", "text", "evidence_refs"}),
        "Publication reference entry",
    )
    refs_raw = json_array(item["evidence_refs"], "Publication reference evidence refs")
    refs = tuple(
        PublicationEvidenceRefV1(
            source_document_id=json_uuid(ref["source_document_id"], "Reference evidence source ID"),
            kind=json_enum(PublicationEvidenceKind, ref["kind"], "Reference evidence kind"),
            evidence_key=json_sha256(ref["evidence_key"], "Reference evidence key"),
        )
        for ref in (
            json_object(
                value,
                frozenset({"source_document_id", "kind", "evidence_key"}),
                "Reference evidence ref",
            )
            for value in refs_raw
        )
    )
    return PublicationReferenceEntryV1(
        source_document_id=json_uuid(item["source_document_id"], "Reference source ID"),
        text=json_text(item["text"], "Reference text"),
        evidence_refs=refs,
    )


@dataclass(frozen=True, slots=True)
class PublicationDocumentV4:
    schema_version: str
    subject_id: UUID
    publication_language: str
    title: str
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    indicators: tuple[PublicationIndicatorGroupV1, ...]
    sources: tuple[PublicationSourceV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]
    tables: tuple[PublicationTableV1, ...]
    diagrams: tuple[PublicationDiagramV1, ...]
    figures: tuple[PublicationSourceFigureV1, ...]
    additional_source_ids: frozenset[UUID] = frozenset()
    charts: tuple[PublicationChartV1, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
            raise ValueError(
                "PublicationDocumentV4 requires "
                f"schema_version={PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION!r}"
            )
        tuple_fields: tuple[tuple[str, Any, type[Any]], ...] = (
            ("lead", self.lead, PublicationParagraphV1),
            ("sections", self.sections, PublicationSectionV1),
            ("timeline", self.timeline, PublicationTimelineEntryV1),
            ("indicators", self.indicators, PublicationIndicatorGroupV1),
            ("sources", self.sources, PublicationSourceV1),
            ("uncertainties", self.uncertainties, PublicationUncertaintyV1),
            ("tables", self.tables, PublicationTableV1),
            ("diagrams", self.diagrams, PublicationDiagramV1),
            ("figures", self.figures, PublicationSourceFigureV1),
            ("charts", self.charts, PublicationChartV1),
        )
        for label, items, item_type in tuple_fields:
            if not isinstance(items, tuple):
                raise ValueError(f"Publication {label} must be a tuple")
            if any(not isinstance(item, item_type) for item in items):
                raise ValueError(f"Publication {label} contains an invalid value type")
        rich_keys = (
            *(table.key for table in self.tables),
            *(diagram.key for diagram in self.diagrams),
            *(figure.key for figure in self.figures),
            *(chart.key for chart in self.charts),
        )
        if len(rich_keys) != len(set(rich_keys)):
            raise ValueError("Publication enriched keys must be globally unique")
        enrichment_source_ids = {
            ref.source_document_id
            for table in self.tables
            for row in table.rows
            for ref in row.evidence_refs
        }
        enrichment_source_ids.update(
            ref.source_document_id
            for diagram in self.diagrams
            for node in diagram.nodes
            for ref in node.evidence_refs
        )
        enrichment_source_ids.update(
            ref.source_document_id
            for diagram in self.diagrams
            for edge in diagram.edges
            for ref in edge.evidence_refs
        )
        enrichment_source_ids.update(figure.source_document_id for figure in self.figures)
        enrichment_source_ids.update(
            ref.source_document_id for chart in self.charts for ref in chart.evidence_refs
        )
        if not isinstance(self.additional_source_ids, frozenset) or any(
            not isinstance(source_id, UUID) for source_id in self.additional_source_ids
        ):
            raise ValueError("Publication additional source identities are invalid")
        enrichment_source_ids.update(self.additional_source_ids)
        _validate_publication_core(
            subject_id=self.subject_id,
            publication_language=self.publication_language,
            title=self.title,
            lead=self.lead,
            sections=self.sections,
            timeline=self.timeline,
            indicators=self.indicators,
            sources=self.sources,
            uncertainties=self.uncertainties,
            additional_source_ids=enrichment_source_ids,
        )
        object.__setattr__(
            self,
            "indicators",
            tuple(sorted(self.indicators, key=lambda group: group.artifact_type.value)),
        )
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(self.sources, key=lambda source: str(source.source_document_id))),
        )
        object.__setattr__(
            self,
            "uncertainties",
            tuple(
                sorted(
                    self.uncertainties,
                    key=lambda item: (item.text, tuple(map(str, item.source_document_ids))),
                )
            ),
        )

    def _to_json(self) -> dict[str, Any]:
        def paragraph_to_json(paragraph: PublicationParagraphV1) -> dict[str, Any]:
            return {
                "text": paragraph.text,
                "evidence_refs": [
                    {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    }
                    for ref in paragraph.evidence_refs
                ],
            }

        return {
            "schema_version": self.schema_version,
            "subject_id": str(self.subject_id),
            "publication_language": self.publication_language,
            "title": self.title,
            "lead": [paragraph_to_json(paragraph) for paragraph in self.lead],
            "sections": [
                {
                    "kind": section.kind.value,
                    "heading": section.heading,
                    "paragraphs": [
                        paragraph_to_json(paragraph) for paragraph in section.paragraphs
                    ],
                }
                for section in self.sections
            ],
            "timeline": [
                {
                    "event_date": entry.event_date.isoformat()
                    if entry.event_date is not None
                    else None,
                    "date_text": entry.date_text,
                    "text": entry.text,
                    "evidence_refs": [
                        {
                            "source_document_id": str(ref.source_document_id),
                            "kind": ref.kind.value,
                            "evidence_key": ref.evidence_key,
                        }
                        for ref in entry.evidence_refs
                    ],
                }
                for entry in self.timeline
            ],
            "indicators": [
                {
                    "artifact_type": group.artifact_type.value,
                    "indicators": [
                        {
                            "value": indicator.value,
                            "normalized_value": indicator.normalized_value,
                            "artifact_type": indicator.artifact_type.value,
                            "source_document_ids": [
                                str(source_id) for source_id in indicator.source_document_ids
                            ],
                        }
                        for indicator in group.indicators
                    ],
                }
                for group in self.indicators
            ],
            "sources": [
                {
                    "source_document_id": str(source.source_document_id),
                    "canonical_url": source.canonical_url,
                    "title": source.title,
                    "publisher": source.publisher,
                    "published_at": source.published_at.isoformat()
                    if source.published_at is not None
                    else None,
                    "tier": source.tier.value,
                    "kind": source.kind.value,
                    "role": source.role.value,
                    **(
                        {"editorial_role": source.editorial_role.value}
                        if source.editorial_role is not None
                        else {}
                    ),
                    **(
                        {"source_criticality": source.source_criticality.value}
                        if source.source_criticality is not None
                        else {}
                    ),
                    **(
                        {"criticality_reason": source.criticality_reason.value}
                        if source.criticality_reason is not None
                        else {}
                    ),
                    **(
                        {"source_collection_id": str(source.source_collection_id)}
                        if source.source_collection_id is not None
                        else {}
                    ),
                    **(
                        {"content_sha256": source.content_sha256}
                        if source.content_sha256 is not None
                        else {}
                    ),
                }
                for source in self.sources
            ],
            "uncertainties": [
                {
                    "text": uncertainty.text,
                    "source_document_ids": [
                        str(source_id) for source_id in uncertainty.source_document_ids
                    ],
                }
                for uncertainty in self.uncertainties
            ],
            "tables": [_table_to_json(table) for table in self.tables],
            "diagrams": [_diagram_to_json(diagram) for diagram in self.diagrams],
            "figures": [_figure_to_json(figure) for figure in self.figures],
            **({"charts": [_chart_to_json(chart) for chart in self.charts]} if self.charts else {}),
        }

    @classmethod
    def _from_json(
        cls,
        payload: Mapping[str, Any],
        *,
        additional_source_ids: frozenset[UUID] = frozenset(),
        require_node_roles: bool = False,
    ) -> PublicationDocumentV4:
        root_keys = frozenset(
            {
                "schema_version",
                "subject_id",
                "publication_language",
                "title",
                "lead",
                "sections",
                "timeline",
                "indicators",
                "sources",
                "uncertainties",
                "tables",
                "diagrams",
                "figures",
            }
        )
        if isinstance(payload, Mapping) and "charts" in payload:
            root_keys |= {"charts"}
        document = json_object(
            payload,
            root_keys,
            "PublicationDocumentV4",
        )

        def paragraph(raw: Any) -> PublicationParagraphV1:
            item = json_object(raw, frozenset({"text", "evidence_refs"}), "Publication paragraph")
            return PublicationParagraphV1(
                text=json_text(item["text"], "Paragraph text"),
                evidence_refs=_publication_evidence_refs_from_json(item["evidence_refs"]),
            )

        sections = tuple(
            PublicationSectionV1(
                kind=json_enum(PublicationSectionKind, item["kind"], "Section kind"),
                heading=json_text(item["heading"], "Section heading"),
                paragraphs=tuple(
                    paragraph(nested)
                    for nested in json_array(item["paragraphs"], "Section paragraphs")
                ),
            )
            for item in (
                json_object(
                    raw, frozenset({"kind", "heading", "paragraphs"}), "Publication section"
                )
                for raw in json_array(document["sections"], "Sections")
            )
        )
        timeline = tuple(
            PublicationTimelineEntryV1(
                event_date=_date_from_json(item["event_date"], "Timeline event_date"),
                date_text=None
                if item["date_text"] is None
                else json_text(item["date_text"], "Timeline date_text"),
                text=json_text(item["text"], "Timeline text"),
                evidence_refs=_publication_evidence_refs_from_json(item["evidence_refs"]),
            )
            for item in (
                json_object(
                    raw,
                    frozenset({"event_date", "date_text", "text", "evidence_refs"}),
                    "Publication timeline entry",
                )
                for raw in json_array(document["timeline"], "Timeline")
            )
        )
        indicators = tuple(
            PublicationIndicatorGroupV1(
                artifact_type=json_enum(
                    ArtifactType, item["artifact_type"], "Indicator group type"
                ),
                indicators=tuple(
                    PublicationIndicatorV1(
                        value=json_text(nested["value"], "Indicator value"),
                        normalized_value=json_text(
                            nested["normalized_value"], "Indicator normalized_value"
                        ),
                        artifact_type=json_enum(
                            ArtifactType, nested["artifact_type"], "Indicator artifact_type"
                        ),
                        source_document_ids=_source_ids_from_json(
                            nested["source_document_ids"], "Indicator source_document_ids"
                        ),
                    )
                    for nested in (
                        json_object(
                            raw_indicator,
                            frozenset(
                                {
                                    "value",
                                    "normalized_value",
                                    "artifact_type",
                                    "source_document_ids",
                                }
                            ),
                            "Publication indicator",
                        )
                        for raw_indicator in json_array(item["indicators"], "Indicators in group")
                    )
                ),
            )
            for item in (
                json_object(
                    raw, frozenset({"artifact_type", "indicators"}), "Publication indicator group"
                )
                for raw in json_array(document["indicators"], "Indicator groups")
            )
        )
        sources = tuple(
            PublicationSourceV1(
                source_document_id=json_uuid(
                    item["source_document_id"], "Source source_document_id"
                ),
                canonical_url=json_text(item["canonical_url"], "Source canonical_url"),
                title=None if item["title"] is None else json_text(item["title"], "Source title"),
                publisher=None
                if item["publisher"] is None
                else json_text(item["publisher"], "Source publisher"),
                published_at=_date_from_json(item["published_at"], "Source published_at"),
                tier=json_enum(ProductionReferenceTier, item["tier"], "Source tier"),
                kind=json_enum(ProductionReferenceKind, item["kind"], "Source kind"),
                role=json_enum(SourceRole, item["role"], "Source role"),
                editorial_role=(
                    json_enum(
                        ProductionEditorialRole,
                        item["editorial_role"],
                        "Source editorial role",
                    )
                    if item.get("editorial_role") is not None
                    else None
                ),
                source_criticality=(
                    json_enum(
                        ProductionSourceCriticality,
                        item["source_criticality"],
                        "Source criticality",
                    )
                    if item.get("source_criticality") is not None
                    else None
                ),
                criticality_reason=(
                    json_enum(
                        ProductionSourceCriticalityReason,
                        item["criticality_reason"],
                        "Source criticality reason",
                    )
                    if item.get("criticality_reason") is not None
                    else None
                ),
                source_collection_id=(
                    json_uuid(item["source_collection_id"], "Source collection ID")
                    if item.get("source_collection_id") is not None
                    else None
                ),
                content_sha256=(
                    json_sha256(item["content_sha256"], "Source archive hash")
                    if item.get("content_sha256") is not None
                    else None
                ),
            )
            for item in (
                _publication_source_object(raw)
                for raw in json_array(document["sources"], "Sources")
            )
        )
        uncertainties = tuple(
            PublicationUncertaintyV1(
                text=json_text(item["text"], "Uncertainty text"),
                source_document_ids=_source_ids_from_json(
                    item["source_document_ids"], "Uncertainty source_document_ids"
                ),
            )
            for item in (
                json_object(
                    raw, frozenset({"text", "source_document_ids"}), "Publication uncertainty"
                )
                for raw in json_array(document["uncertainties"], "Uncertainties")
            )
        )
        return cls(
            schema_version=json_text(document["schema_version"], "Publication schema_version"),
            subject_id=json_uuid(document["subject_id"], "Publication subject_id"),
            publication_language=json_text(
                document["publication_language"], "Publication language"
            ),
            title=json_text(document["title"], "Publication title"),
            lead=tuple(paragraph(raw) for raw in json_array(document["lead"], "Lead")),
            sections=sections,
            timeline=timeline,
            indicators=indicators,
            sources=sources,
            uncertainties=uncertainties,
            tables=tuple(_table_from_json(raw) for raw in json_array(document["tables"], "Tables")),
            diagrams=tuple(
                _diagram_from_json(raw, require_node_roles=require_node_roles)
                for raw in json_array(document["diagrams"], "Diagrams")
            ),
            figures=tuple(
                _figure_from_json(raw) for raw in json_array(document["figures"], "Figures")
            ),
            additional_source_ids=additional_source_ids,
            charts=(
                tuple(_chart_from_json(raw) for raw in json_array(document["charts"], "Charts"))
                if "charts" in document
                else ()
            ),
        )


def publication_document_text_anchors(
    document: PublicationDocumentV4,
    *,
    references: tuple[PublicationReferenceEntryV1, ...] = (),
) -> dict[str, str]:
    """Return every text-bearing article field rendered with semantic spans."""
    result: dict[str, str] = {"title": document.title}
    result.update(
        {f"lead:{index:04d}": item.text for index, item in enumerate(document.lead, start=1)}
    )
    for section_index, section in enumerate(document.sections):
        for paragraph_index, paragraph in enumerate(section.paragraphs, start=1):
            result[f"section:{section_index}:paragraph:{paragraph_index:04d}"] = paragraph.text
    for index, item in enumerate(document.timeline, start=1):
        result[f"timeline:{index:04d}"] = item.text
    for index, reference in enumerate(references, start=1):
        result[f"reference:{index:04d}"] = reference.text
    for table in document.tables:
        result[f"table:{table.key}:title"] = table.title
        if table.caption is not None:
            result[f"table:{table.key}:caption"] = table.caption
        for column_index, column in enumerate(table.columns, start=1):
            result[f"table:{table.key}:column:{column_index:04d}"] = column.label
        for row_index, row in enumerate(table.rows, start=1):
            for cell_index, cell in enumerate(row.cells, start=1):
                result[f"table:{table.key}:row:{row_index:04d}:cell:{cell_index:04d}"] = cell
    for diagram in document.diagrams:
        result[f"diagram:{diagram.key}:title"] = diagram.title
        if diagram.caption is not None:
            result[f"diagram:{diagram.key}:caption"] = diagram.caption
    for chart in document.charts:
        result[f"chart:{chart.key}:title"] = chart.title
        if chart.caption is not None:
            result[f"chart:{chart.key}:caption"] = chart.caption
    for figure in document.figures:
        result[f"figure:{figure.key}:caption"] = figure.caption
        result[f"figure:{figure.key}:provenance"] = figure.provenance
    return result


def order_publication_references(
    references: tuple[PublicationReferenceEntryV1, ...],
    sources: tuple[PublicationSourceV1, ...],
) -> tuple[PublicationReferenceEntryV1, ...]:
    """Order references by article release date; undated ones last, core after supporting."""
    sources_by_id = {source.source_document_id: source for source in sources}

    def order(item: PublicationReferenceEntryV1) -> tuple[Any, ...]:
        source = sources_by_id[item.source_document_id]
        return (
            source.published_at is None,
            source.published_at or date.max,
            source.tier is ProductionReferenceTier.CORE,
            source.canonical_url,
        )

    return tuple(sorted(references, key=order))


class PublicationIOCDisposition(StrEnum):
    RETAINED = "retained"
    UNCERTAIN = "uncertain"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class PublicationIOCDetailV1:
    """One source-verified indicator with an explicit publication decision."""

    artifact_type: ArtifactType
    value: str
    normalized_value: str
    source_role: SourceRole
    context: str
    evidence_quote: str
    evidence_basis: ProductionEvidenceBasis
    source_document_ids: tuple[UUID, ...]
    disposition: PublicationIOCDisposition
    disposition_reason: str
    relevance_classification: RelevanceClassification | None = None
    relevance_reason_code: RelevanceReasonCode | None = None
    first_observed: date | None = None
    last_observed: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_type, ArtifactType):
            raise ValueError("Publication IOC artifact type is invalid")
        if not isinstance(self.source_role, SourceRole):
            raise ValueError("Publication IOC source role is invalid")
        if not isinstance(self.evidence_basis, ProductionEvidenceBasis):
            raise ValueError("Publication IOC evidence basis is invalid")
        if not isinstance(self.disposition, PublicationIOCDisposition):
            raise ValueError("Publication IOC disposition is invalid")
        for field_name in ("value", "normalized_value", "evidence_quote", "disposition_reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Publication IOC {field_name} must be non-empty text")
        if not isinstance(self.context, str):
            raise ValueError("Publication IOC context must be text")
        if not isinstance(self.source_document_ids, tuple) or not self.source_document_ids:
            raise ValueError("Publication IOC source identities must be a non-empty tuple")
        if any(not isinstance(item, UUID) for item in self.source_document_ids):
            raise ValueError("Publication IOC source identities must be UUIDs")
        source_ids = tuple(sorted(set(self.source_document_ids), key=str))
        if len(source_ids) != len(self.source_document_ids):
            raise ValueError("Publication IOC source identities must be unique")
        object.__setattr__(self, "source_document_ids", source_ids)
        if (self.relevance_classification is None) != (self.relevance_reason_code is None):
            raise ValueError("Publication IOC relevance classification and reason must be paired")
        if self.relevance_classification is not None and not isinstance(
            self.relevance_classification, RelevanceClassification
        ):
            raise ValueError("Publication IOC relevance classification is invalid")
        if self.relevance_reason_code is not None and not isinstance(
            self.relevance_reason_code, RelevanceReasonCode
        ):
            raise ValueError("Publication IOC relevance reason is invalid")
        for field_name in ("first_observed", "last_observed"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, date) or hasattr(value, "hour")):
                raise ValueError(f"Publication IOC {field_name} must be a date or None")
        if (
            self.first_observed is not None
            and self.last_observed is not None
            and self.first_observed > self.last_observed
        ):
            raise ValueError("Publication IOC observation dates are reversed")


def _publication_ioc_detail_sort_key(
    item: PublicationIOCDetailV1,
) -> tuple[str, str, str, tuple[str, ...], str, str]:
    return (
        item.artifact_type.value,
        item.normalized_value.casefold(),
        item.value,
        tuple(map(str, item.source_document_ids)),
        item.context.casefold(),
        item.evidence_quote,
    )


def _publication_ioc_detail_to_json(item: PublicationIOCDetailV1) -> dict[str, Any]:
    return {
        "artifact_type": item.artifact_type.value,
        "value": item.value,
        "normalized_value": item.normalized_value,
        "source_role": item.source_role.value,
        "context": item.context,
        "evidence_quote": item.evidence_quote,
        "evidence_basis": item.evidence_basis.value,
        "source_document_ids": [str(source_id) for source_id in item.source_document_ids],
        "disposition": item.disposition.value,
        "disposition_reason": item.disposition_reason,
        "relevance_classification": (
            item.relevance_classification.value
            if item.relevance_classification is not None
            else None
        ),
        "relevance_reason_code": (
            item.relevance_reason_code.value if item.relevance_reason_code is not None else None
        ),
        "first_observed": item.first_observed.isoformat() if item.first_observed else None,
        "last_observed": item.last_observed.isoformat() if item.last_observed else None,
    }


def _publication_ioc_detail_from_json(raw: Any) -> PublicationIOCDetailV1:
    fields = frozenset(
        {
            "artifact_type",
            "value",
            "normalized_value",
            "source_role",
            "context",
            "evidence_quote",
            "evidence_basis",
            "source_document_ids",
            "disposition",
            "disposition_reason",
            "relevance_classification",
            "relevance_reason_code",
            "first_observed",
            "last_observed",
        }
    )
    item = json_object(raw, fields, "Publication IOC detail")
    classification = item["relevance_classification"]
    reason_code = item["relevance_reason_code"]
    return PublicationIOCDetailV1(
        artifact_type=json_enum(ArtifactType, item["artifact_type"], "Publication IOC type"),
        value=json_text(item["value"], "Publication IOC value"),
        normalized_value=json_text(item["normalized_value"], "Publication IOC normalized value"),
        source_role=json_enum(SourceRole, item["source_role"], "Publication IOC source role"),
        context=json_text(item["context"], "Publication IOC context"),
        evidence_quote=json_text(item["evidence_quote"], "Publication IOC evidence quote"),
        evidence_basis=json_enum(
            ProductionEvidenceBasis, item["evidence_basis"], "Publication IOC evidence basis"
        ),
        source_document_ids=tuple(
            json_uuid(value, "Publication IOC source identity")
            for value in json_array(
                item["source_document_ids"], "Publication IOC source identities"
            )
        ),
        disposition=json_enum(
            PublicationIOCDisposition, item["disposition"], "Publication IOC disposition"
        ),
        disposition_reason=json_text(
            item["disposition_reason"], "Publication IOC disposition reason"
        ),
        relevance_classification=(
            json_enum(RelevanceClassification, classification, "Publication IOC relevance")
            if classification is not None
            else None
        ),
        relevance_reason_code=(
            json_enum(RelevanceReasonCode, reason_code, "Publication IOC relevance reason")
            if reason_code is not None
            else None
        ),
        first_observed=_date_from_json(item["first_observed"], "Publication IOC first observation"),
        last_observed=_date_from_json(item["last_observed"], "Publication IOC last observation"),
    )


@dataclass(frozen=True, slots=True)
class PublicationDocumentV5:
    """V4 content plus semantic text, dated source notes and original IOC groups."""

    document: PublicationDocumentV4
    semantic_text: SemanticTextV1
    references: tuple[PublicationReferenceEntryV1, ...] = ()
    original_indicators: tuple[PublicationIndicatorGroupV1, ...] = ()
    ioc_details: tuple[PublicationIOCDetailV1, ...] = ()

    @property
    def schema_version(self) -> str:
        return PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION

    def __getattr__(self, name: str) -> Any:
        return getattr(self.document, name)

    def __post_init__(self) -> None:
        if not isinstance(self.document, PublicationDocumentV4):
            raise ValueError("PublicationDocumentV5 requires a validated V4 base document")
        if not isinstance(self.semantic_text, SemanticTextV1):
            raise ValueError("PublicationDocumentV5 requires versioned semantic text")
        if not isinstance(self.references, tuple) or any(
            not isinstance(item, PublicationReferenceEntryV1) for item in self.references
        ):
            raise ValueError("Publication source references have an invalid type")
        if not isinstance(self.original_indicators, tuple) or any(
            not isinstance(item, PublicationIndicatorGroupV1) for item in self.original_indicators
        ):
            raise ValueError("Original IOC groups have an invalid type")
        if not isinstance(self.ioc_details, tuple) or any(
            not isinstance(item, PublicationIOCDetailV1) for item in self.ioc_details
        ):
            raise ValueError("Publication IOC details have an invalid type")
        sources_by_id = {source.source_document_id: source for source in self.document.sources}
        if len(sources_by_id) != len(self.document.sources):
            raise ValueError("Publication sources must not repeat source identities")
        reference_source_ids = {item.source_document_id for item in self.references}
        if len(reference_source_ids) != len(self.references):
            raise ValueError("Publication source references must not repeat sources")
        reference_evidence_source_ids = {
            ref.source_document_id for item in self.references for ref in item.evidence_refs
        }
        ioc_source_ids = {
            source_id for item in self.ioc_details for source_id in item.source_document_ids
        }
        if not ioc_source_ids.issubset(sources_by_id):
            raise ValueError("Publication IOC detail identifies an unknown source")
        if not reference_evidence_source_ids.issubset(sources_by_id):
            raise ValueError("Publication reference evidence identifies an unknown source")
        for source_id in reference_source_ids:
            source = sources_by_id.get(source_id)
            is_reader_facing_publication = source is not None and (
                source.kind is ProductionReferenceKind.PUBLICATION
                and source.tier
                in {ProductionReferenceTier.CORE, ProductionReferenceTier.SUPPORTING}
            )
            is_cited_technical_context = source is not None and (
                source.kind is ProductionReferenceKind.TECHNICAL_RESOURCE
                and source.tier is ProductionReferenceTier.TECHNICAL
                and source.editorial_role is ProductionEditorialRole.CONTEXT
            )
            if not (is_reader_facing_publication or is_cited_technical_context):
                raise ValueError(
                    "Publication reference must identify a core/supporting publication "
                    "or technical context source"
                )
        original_types = [group.artifact_type for group in self.original_indicators]
        if len(original_types) != len(set(original_types)):
            raise ValueError("Original IOC groups must not repeat artifact types")
        main_values = {
            indicator.normalized_value
            for group in self.document.indicators
            for indicator in group.indicators
        }
        original_values = [
            indicator.normalized_value
            for group in self.original_indicators
            for indicator in group.indicators
        ]
        if len(original_values) != len(set(original_values)) or main_values.intersection(
            original_values
        ):
            raise ValueError("Publication IOCs must be deduplicated across both groups")
        if self.document.additional_source_ids != (
            reference_source_ids
            | reference_evidence_source_ids
            | indicator_source_ids(self.original_indicators)
            | ioc_source_ids
        ):
            raise ValueError(
                "Publication source identities do not match references, their evidence, "
                "and original IOCs"
            )

        references = order_publication_references(self.references, self.document.sources)
        object.__setattr__(self, "references", references)
        object.__setattr__(
            self,
            "original_indicators",
            tuple(sorted(self.original_indicators, key=lambda group: group.artifact_type.value)),
        )
        object.__setattr__(
            self,
            "ioc_details",
            tuple(sorted(self.ioc_details, key=_publication_ioc_detail_sort_key)),
        )
        expected = publication_document_text_anchors(self.document, references=references)
        actual = {item.anchor: item.text for item in self.semantic_text.paragraphs}
        if actual != expected:
            raise ValueError("PublicationDocumentV5 semantic text must preserve every text field")

    def _to_json(self) -> dict[str, Any]:
        payload = self.document._to_json()
        payload["schema_version"] = self.schema_version
        payload["diagrams"] = [
            _diagram_to_json(diagram, include_node_roles=True) for diagram in self.document.diagrams
        ]
        payload["references"] = [_publication_reference_to_json(item) for item in self.references]
        payload["original_indicators"] = [
            _indicator_group_to_json(group) for group in self.original_indicators
        ]
        if self.ioc_details:
            payload["ioc_details"] = [
                _publication_ioc_detail_to_json(item) for item in self.ioc_details
            ]
        payload["rich_text"] = semantic_text_to_json(self.semantic_text)
        return payload

    @classmethod
    def _from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV5:
        root_keys = frozenset(
            {
                "schema_version",
                "subject_id",
                "publication_language",
                "title",
                "lead",
                "sections",
                "timeline",
                "indicators",
                "sources",
                "uncertainties",
                "tables",
                "diagrams",
                "figures",
                "references",
                "original_indicators",
                "rich_text",
            }
        )
        if isinstance(payload, Mapping) and "ioc_details" in payload:
            root_keys |= {"ioc_details"}
        if isinstance(payload, Mapping) and "charts" in payload:
            root_keys |= {"charts"}
        document = json_object(
            payload,
            root_keys,
            "PublicationDocumentV5",
        )
        if document["schema_version"] != PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION:
            raise ValueError("PublicationDocumentV5 schema version is unsupported")
        references = tuple(
            _publication_reference_from_json(raw)
            for raw in json_array(document["references"], "Publication references")
        )
        original_indicators = tuple(
            _indicator_group_from_json(raw, "Original IOC group")
            for raw in json_array(document["original_indicators"], "Original IOC groups")
        )
        ioc_details = tuple(
            _publication_ioc_detail_from_json(raw)
            for raw in json_array(document.get("ioc_details", []), "Publication IOC details")
        )
        additional_source_ids = {item.source_document_id for item in references}
        additional_source_ids.update(
            ref.source_document_id for item in references for ref in item.evidence_refs
        )
        additional_source_ids |= indicator_source_ids(original_indicators)
        additional_source_ids.update(
            source_id for item in ioc_details for source_id in item.source_document_ids
        )
        base_payload = dict(document)
        for key in ("references", "original_indicators", "rich_text", "ioc_details"):
            base_payload.pop(key, None)
        base_payload["schema_version"] = PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION
        base = PublicationDocumentV4._from_json(
            base_payload,
            additional_source_ids=frozenset(additional_source_ids),
            require_node_roles=True,
        )
        return cls(
            document=base,
            semantic_text=semantic_text_from_json(document["rich_text"]),
            references=references,
            original_indicators=original_indicators,
            ioc_details=ioc_details,
        )


class PublicationDocumentV6ErrorCode(StrEnum):
    BASE_IDENTITY_MISMATCH = "publication_v6_base_identity_mismatch"
    OPINION_INVALID = "publication_v6_opinion_invalid"
    OPINION_NOT_APPROVED = "publication_v6_opinion_not_approved"
    OPINION_EMPTY = "publication_v6_opinion_empty"
    DETECTION_INVALID = "publication_v6_detection_invalid"


class PublicationDocumentV6ValidationError(ValueError):
    """A typed invariant failure in a V6 wrapper."""

    def __init__(self, code: PublicationDocumentV6ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class PublicationDocumentV6BaseMismatchError(PublicationDocumentV6ValidationError):
    """The wrapper provenance does not identify its embedded V5 document."""

    def __init__(self) -> None:
        super().__init__(PublicationDocumentV6ErrorCode.BASE_IDENTITY_MISMATCH)


@dataclass(frozen=True, slots=True)
class PublicationDocumentV6ProvenanceV1:
    base_publication_artifact_id: UUID
    base_publication_sha256: str
    investigation_id: UUID | None = None
    opinion_id: UUID | None = None
    opinion_version: int | None = None
    opinion_content_sha256: str | None = None
    reviewer_id: str | None = None
    approver_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_publication_artifact_id, UUID):
            raise PublicationDocumentV6BaseMismatchError()
        validate_sha256(self.base_publication_sha256, "V6 base publication SHA-256")
        values = (
            self.investigation_id,
            self.opinion_id,
            self.opinion_version,
            self.opinion_content_sha256,
            self.reviewer_id,
            self.approver_id,
        )
        if all(value is None for value in values):
            return
        if any(value is None for value in values):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        assert self.opinion_content_sha256 is not None
        validate_sha256(self.opinion_content_sha256, "V6 opinion content SHA-256")
        if (
            isinstance(self.opinion_version, bool)
            or not isinstance(self.opinion_version, int)
            or self.opinion_version < 1
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if not self.reviewer_id or not self.reviewer_id.strip():
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if not self.approver_id or not self.approver_id.strip():
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )


@dataclass(frozen=True, slots=True)
class PublicationAnalystOpinionBlockV1:
    block_type: OpinionBlockType
    heading: str
    text: str
    qualification: OpinionClaimQualification
    certainty: float
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.block_type, OpinionBlockType)
            or not isinstance(self.qualification, OpinionClaimQualification)
            or self.heading != _OPINION_BLOCK_HEADINGS.get(self.block_type)
            or not isinstance(self.text, str)
            or not self.text.strip()
            or isinstance(self.certainty, bool)
            or not isinstance(self.certainty, (int, float))
            or not math.isfinite(self.certainty)
            or not 0.0 <= self.certainty <= 1.0
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        object.__setattr__(self, "finding_refs", tuple(self.finding_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if any(
            not isinstance(reference, UUID) for reference in self.finding_refs + self.evidence_refs
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )


@dataclass(frozen=True, slots=True)
class PublicationAnalystOpinionSectionV1:
    opinion_id: UUID
    opinion_version: int
    content_sha256: str
    blocks: tuple[PublicationAnalystOpinionBlockV1, ...]
    unresolved_points: tuple[str, ...] = ()
    protocol_figures: tuple[OpinionProtocolFigureV1, ...] = ()

    def __post_init__(self) -> None:
        validate_sha256(self.content_sha256, "V6 opinion content SHA-256")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "unresolved_points", tuple(self.unresolved_points))
        object.__setattr__(self, "protocol_figures", tuple(self.protocol_figures))
        if (
            not isinstance(self.opinion_id, UUID)
            or isinstance(self.opinion_version, bool)
            or not isinstance(self.opinion_version, int)
            or self.opinion_version < 1
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if not self.blocks:
            raise PublicationDocumentV6ValidationError(PublicationDocumentV6ErrorCode.OPINION_EMPTY)
        if any(not isinstance(block, PublicationAnalystOpinionBlockV1) for block in self.blocks):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if any(not isinstance(item, OpinionProtocolFigureV1) for item in self.protocol_figures):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if len({item.kind for item in self.protocol_figures}) != len(self.protocol_figures):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if any(not isinstance(point, str) or not point.strip() for point in self.unresolved_points):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )


class PublicationDetectionStatus(StrEnum):
    VALIDATED = "validated"
    CANDIDATE = "candidate"
    UNTESTED = "untested"


class PublicationDetectionReason(StrEnum):
    VALIDATED = "real_positives_and_goodware_tested"
    CORPUS_EMPTY = "corpus_empty"
    POSITIVES_MISSING = "real_positive_samples_missing"
    GOODWARE_MISSING = "goodware_corpus_empty"
    SAMPLE_BYTES_MISSING = "sample_bytes_missing_or_unverified"
    SYNTHETIC_POSITIVES = "positive_samples_are_synthetic"
    FALSE_NEGATIVES = "real_positive_samples_not_matched"
    FALSE_POSITIVES = "goodware_false_positive_observed"
    EXECUTION_INCOMPLETE = "analysis_incomplete"


@dataclass(frozen=True, slots=True)
class PublicationDetectionV1:
    """Canonical YARA rule and test snapshot pinned into one V6 publication."""

    candidate_id: UUID
    version_id: UUID
    version: int
    source: str
    source_sha256: str
    candidate_status: DetectionCandidateStatus
    rule_validation_status: RuleValidationStatus
    validation_status: PublicationDetectionStatus
    validation_reason: PublicationDetectionReason
    positive_real_count: int
    positive_synthetic_count: int
    goodware_tested_count: int
    false_positive_count: int
    engine_version: str | None
    coverage_statement: str | None
    limitations: tuple[str, ...]
    created_at: datetime
    tested_at: datetime | None
    test_report_observation_id: UUID | None
    test_report_sha256: str | None
    retrohunt_submitted: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID) or not isinstance(self.version_id, UUID):
            raise TypeError("Published detection identities must be UUIDs")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Published detection version must be positive")
        validate_text(self.source, "Published YARA source")
        if not self.source.strip():
            raise ValueError("Published YARA source must not be empty")
        validate_sha256(self.source_sha256, "Published YARA source SHA-256")
        if hashlib.sha256(self.source.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("Published YARA source SHA-256 does not match source")
        if not isinstance(self.candidate_status, DetectionCandidateStatus):
            raise TypeError(
                "Published detection candidate status must use DetectionCandidateStatus"
            )
        if not isinstance(self.rule_validation_status, RuleValidationStatus):
            raise TypeError("Published rule validation status must use RuleValidationStatus")
        if not isinstance(self.validation_status, PublicationDetectionStatus):
            raise TypeError("Published detection status must use PublicationDetectionStatus")
        if not isinstance(self.validation_reason, PublicationDetectionReason):
            raise TypeError("Published detection reason must use PublicationDetectionReason")
        for name in (
            "positive_real_count",
            "positive_synthetic_count",
            "goodware_tested_count",
            "false_positive_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.engine_version is not None:
            validate_text(self.engine_version, "YARA engine version")
        if self.coverage_statement is not None:
            validate_text(self.coverage_statement, "YARA coverage statement")
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if any(not isinstance(item, str) for item in self.limitations):
            raise TypeError("Published detection limitations must be strings")
        _aware_publication_datetime(self.created_at, "Published rule creation date")
        if self.tested_at is not None:
            _aware_publication_datetime(self.tested_at, "Published rule test date")
        if (self.test_report_observation_id is None) != (self.test_report_sha256 is None):
            raise ValueError("Published test report identity and hash must be supplied together")
        if self.test_report_sha256 is not None:
            validate_sha256(self.test_report_sha256, "Published test report SHA-256")
        if self.retrohunt_submitted is not None and not isinstance(self.retrohunt_submitted, bool):
            raise TypeError("Published Retrohunt state must be true, false, or unknown")

    @property
    def validation_status_is_coherent(self) -> bool:
        expected_status = {
            RuleValidationStatus.REAL_VALIDATED: PublicationDetectionStatus.VALIDATED,
            RuleValidationStatus.UNTESTED: PublicationDetectionStatus.UNTESTED,
            RuleValidationStatus.SYNTHETIC_ONLY: PublicationDetectionStatus.CANDIDATE,
            RuleValidationStatus.REAL_PARTIAL: PublicationDetectionStatus.CANDIDATE,
        }[self.rule_validation_status]
        if self.validation_status is not expected_status:
            return False
        if self.rule_validation_status is RuleValidationStatus.REAL_VALIDATED:
            return (
                self.validation_reason is PublicationDetectionReason.VALIDATED
                and self.positive_real_count > 0
                and self.goodware_tested_count > 0
                and self.false_positive_count == 0
            )
        return self.validation_reason is not PublicationDetectionReason.VALIDATED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "publication-detection.v1",
            "candidate_id": str(self.candidate_id),
            "version_id": str(self.version_id),
            "version": self.version,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "candidate_status": self.candidate_status.value,
            "rule_validation_status": self.rule_validation_status.value,
            "validation_status": self.validation_status.value,
            "validation_reason": self.validation_reason.value,
            "positive_real_count": self.positive_real_count,
            "positive_synthetic_count": self.positive_synthetic_count,
            "goodware_tested_count": self.goodware_tested_count,
            "false_positive_count": self.false_positive_count,
            "engine_version": self.engine_version,
            "coverage_statement": self.coverage_statement,
            "limitations": list(self.limitations),
            "created_at": self.created_at.isoformat(),
            "tested_at": self.tested_at.isoformat() if self.tested_at is not None else None,
            "test_report_observation_id": (
                str(self.test_report_observation_id)
                if self.test_report_observation_id is not None
                else None
            ),
            "test_report_sha256": self.test_report_sha256,
            "retrohunt_submitted": self.retrohunt_submitted,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> PublicationDetectionV1:
        fields = frozenset(
            {
                "schema",
                "candidate_id",
                "version_id",
                "version",
                "source",
                "source_sha256",
                "candidate_status",
                "rule_validation_status",
                "validation_status",
                "validation_reason",
                "positive_real_count",
                "positive_synthetic_count",
                "goodware_tested_count",
                "false_positive_count",
                "engine_version",
                "coverage_statement",
                "limitations",
                "created_at",
                "tested_at",
                "test_report_observation_id",
                "test_report_sha256",
                "retrohunt_submitted",
            }
        )
        item = json_object(raw, fields, "Publication detection")
        if item["schema"] != "publication-detection.v1":
            raise ValueError("Publication detection schema is unsupported")
        raw_observation_id = item["test_report_observation_id"]
        raw_tested_at = item["tested_at"]
        return cls(
            candidate_id=json_uuid(item["candidate_id"], "Detection candidate ID"),
            version_id=json_uuid(item["version_id"], "Detection version ID"),
            version=json_int(item["version"], "Detection version"),
            source=json_text(item["source"], "YARA source"),
            source_sha256=json_sha256(item["source_sha256"], "YARA source SHA-256"),
            candidate_status=json_enum(
                DetectionCandidateStatus, item["candidate_status"], "Detection candidate status"
            ),
            rule_validation_status=json_enum(
                RuleValidationStatus, item["rule_validation_status"], "Rule validation status"
            ),
            validation_status=json_enum(
                PublicationDetectionStatus, item["validation_status"], "Published detection status"
            ),
            validation_reason=json_enum(
                PublicationDetectionReason, item["validation_reason"], "Published detection reason"
            ),
            positive_real_count=json_int(item["positive_real_count"], "Real positive count"),
            positive_synthetic_count=json_int(
                item["positive_synthetic_count"], "Synthetic positive count"
            ),
            goodware_tested_count=json_int(item["goodware_tested_count"], "Goodware test count"),
            false_positive_count=json_int(item["false_positive_count"], "False positive count"),
            engine_version=(
                json_text(item["engine_version"], "YARA engine version")
                if item["engine_version"] is not None
                else None
            ),
            coverage_statement=(
                json_text(item["coverage_statement"], "YARA coverage statement")
                if item["coverage_statement"] is not None
                else None
            ),
            limitations=tuple(
                json_text(value, "YARA limitation")
                for value in json_array(item["limitations"], "YARA limitations")
            ),
            created_at=_publication_datetime_from_json(item["created_at"], "YARA creation date"),
            tested_at=(
                _publication_datetime_from_json(raw_tested_at, "YARA test date")
                if raw_tested_at is not None
                else None
            ),
            test_report_observation_id=(
                json_uuid(raw_observation_id, "YARA test report observation ID")
                if raw_observation_id is not None
                else None
            ),
            test_report_sha256=(
                json_sha256(item["test_report_sha256"], "YARA test report SHA-256")
                if item["test_report_sha256"] is not None
                else None
            ),
            retrohunt_submitted=item["retrohunt_submitted"],
        )


def _aware_publication_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _publication_datetime_from_json(value: Any, label: str) -> datetime:
    text = json_text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    _aware_publication_datetime(parsed, label)
    return parsed


@dataclass(frozen=True, slots=True)
class PublicationDocumentV6:
    """Additive opinion wrapper around an immutable, exact V5 publication."""

    base_publication: PublicationDocumentV5
    provenance: PublicationDocumentV6ProvenanceV1
    analyst_opinion: AnalystOpinionV1 | None = None
    rendered_opinion: PublicationAnalystOpinionSectionV1 | None = None
    infrastructure_graph: InvestigationGraph | None = None
    detection: PublicationDetectionV1 | None = None

    @property
    def schema_version(self) -> str:
        return PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base_publication, name)

    def __post_init__(self) -> None:
        if not isinstance(self.base_publication, PublicationDocumentV5):
            raise PublicationDocumentV6BaseMismatchError()
        if not isinstance(self.provenance, PublicationDocumentV6ProvenanceV1):
            raise PublicationDocumentV6BaseMismatchError()
        if publication_document_v5_sha256(self.base_publication) != (
            self.provenance.base_publication_sha256
        ):
            raise PublicationDocumentV6BaseMismatchError()

        opinion = self.analyst_opinion
        section = self.rendered_opinion
        graph = self.infrastructure_graph
        detection = self.detection
        if detection is not None and not isinstance(detection, PublicationDetectionV1):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.DETECTION_INVALID
            )
        if graph is not None and not isinstance(graph, InvestigationGraph):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        provenance = self.provenance
        if opinion is None:
            if (
                graph is not None
                or section is not None
                or any(
                    value is not None
                    for value in (
                        provenance.investigation_id,
                        provenance.opinion_id,
                        provenance.opinion_version,
                        provenance.opinion_content_sha256,
                        provenance.reviewer_id,
                        provenance.approver_id,
                    )
                )
            ):
                raise PublicationDocumentV6ValidationError(
                    PublicationDocumentV6ErrorCode.OPINION_INVALID
                )
            return
        if opinion.status is not AnalystOpinionStatus.APPROVED:
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_NOT_APPROVED
            )
        anchors = publication_document_text_anchors(
            self.base_publication.document,
            references=self.base_publication.references,
        )
        if any(
            observation.anchor not in anchors
            or not observation.anchor.startswith(("lead:", "section:"))
            for observation in opinion.observations
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if hashlib.sha256(opinion.canonical_bytes()).hexdigest() != opinion.content_sha256:
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        if (
            provenance.base_publication_artifact_id != opinion.base_publication_artifact_id
            or provenance.base_publication_sha256 != opinion.base_publication_sha256
        ):
            raise PublicationDocumentV6BaseMismatchError()
        if (
            provenance.investigation_id != opinion.investigation_id
            or provenance.opinion_id != opinion.id
            or provenance.opinion_version != opinion.version
            or provenance.opinion_content_sha256 != opinion.content_sha256
            or provenance.reviewer_id != opinion.reviewer_id
            or section is None
            or section.opinion_id != opinion.id
            or section.opinion_version != opinion.version
            or section.content_sha256 != opinion.content_sha256
            or section.protocol_figures != opinion.protocol_figures
            or not opinion.renderable_blocks
        ):
            code = (
                PublicationDocumentV6ErrorCode.OPINION_EMPTY
                if not opinion.renderable_blocks
                else PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
            raise PublicationDocumentV6ValidationError(code)
        if graph is not None and (
            graph.investigation_id != opinion.investigation_id
            or graph.publication_sha256 != provenance.base_publication_sha256
        ):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )

        expected_blocks = tuple(
            sorted(
                opinion.renderable_blocks, key=lambda block: _OPINION_BLOCK_ORDER[block.block_type]
            )
        )
        if len(expected_blocks) != len(section.blocks):
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )
        for source, rendered in zip(expected_blocks, section.blocks, strict=True):
            if (
                rendered.block_type is not source.block_type
                or rendered.text != source.text
                or rendered.qualification is not source.qualification
                or rendered.certainty != source.certainty
                or rendered.finding_refs != source.finding_refs
                or rendered.evidence_refs != source.evidence_refs
            ):
                raise PublicationDocumentV6ValidationError(
                    PublicationDocumentV6ErrorCode.OPINION_INVALID
                )
        if section.unresolved_points != opinion.unresolved_points:
            raise PublicationDocumentV6ValidationError(
                PublicationDocumentV6ErrorCode.OPINION_INVALID
            )

    def _to_json(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "base_publication": publication_document_v5_to_json(self.base_publication),
            "provenance": _publication_v6_provenance_to_json(self.provenance),
            "analyst_opinion": (
                self.analyst_opinion.to_payload() if self.analyst_opinion is not None else None
            ),
            "rendered_opinion": (
                _rendered_opinion_to_json(self.rendered_opinion)
                if self.rendered_opinion is not None
                else None
            ),
        }
        if self.infrastructure_graph is not None:
            payload["infrastructure_graph"] = self.infrastructure_graph.to_payload()
        if self.detection is not None:
            payload["detection"] = self.detection.to_payload()
        return payload

    @classmethod
    def _from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV6:
        fields = {
            "schema_version",
            "base_publication",
            "provenance",
            "analyst_opinion",
            "rendered_opinion",
        }
        if "infrastructure_graph" in payload:
            fields.add("infrastructure_graph")
        if "detection" in payload:
            fields.add("detection")
        document = json_object(
            payload,
            frozenset(fields),
            "PublicationDocumentV6",
        )
        if document["schema_version"] != PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION:
            raise ValueError("PublicationDocumentV6 schema version is unsupported")
        raw_base = document["base_publication"]
        if (
            not isinstance(raw_base, Mapping)
            or raw_base.get("schema_version") != PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION
        ):
            raise PublicationDocumentV6BaseMismatchError()
        base = publication_document_v5_from_json(raw_base)
        raw_opinion = document["analyst_opinion"]
        raw_rendered = document["rendered_opinion"]
        return cls(
            base_publication=base,
            provenance=_publication_v6_provenance_from_json(document["provenance"]),
            analyst_opinion=_analyst_opinion_from_json(raw_opinion)
            if raw_opinion is not None
            else None,
            rendered_opinion=_rendered_opinion_from_json(raw_rendered)
            if raw_rendered is not None
            else None,
            infrastructure_graph=InvestigationGraph.from_payload(document["infrastructure_graph"])
            if document.get("infrastructure_graph") is not None
            else None,
            detection=PublicationDetectionV1.from_payload(document["detection"])
            if document.get("detection") is not None
            else None,
        )


_OPINION_BLOCK_ORDER = {item: index for index, item in enumerate(OpinionBlockType)}
_OPINION_BLOCK_HEADINGS = {
    OpinionBlockType.KEY_FINDINGS: "Apports de l'enquête",
    OpinionBlockType.ADDITIONAL_SAMPLES: "Échantillons complémentaires",
    OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES: "Chaîne d'infection et capacités",
    OpinionBlockType.NETWORK_AND_INFRASTRUCTURE: "Activité réseau et infrastructure",
    OpinionBlockType.CLAIM_VERIFICATION: "Vérification des affirmations",
    OpinionBlockType.PROPOSED_DETECTIONS: "Détections proposées",
    OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS: "Incertitudes et prochaines pistes",
}


def publication_document_v5_sha256(document: PublicationDocumentV5) -> str:
    """Hash the canonical JSON bytes used for a PUBLICATION V5 blob."""
    encoded = json.dumps(
        publication_document_v5_to_json(document),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publication_v6_provenance_to_json(
    value: PublicationDocumentV6ProvenanceV1,
) -> dict[str, Any]:
    return {
        "base_publication_artifact_id": str(value.base_publication_artifact_id),
        "base_publication_sha256": value.base_publication_sha256,
        "investigation_id": str(value.investigation_id) if value.investigation_id else None,
        "opinion_id": str(value.opinion_id) if value.opinion_id else None,
        "opinion_version": value.opinion_version,
        "opinion_content_sha256": value.opinion_content_sha256,
        "reviewer_id": value.reviewer_id,
        "approver_id": value.approver_id,
    }


def _publication_v6_provenance_from_json(raw: Any) -> PublicationDocumentV6ProvenanceV1:
    item = json_object(
        raw,
        frozenset(
            {
                "base_publication_artifact_id",
                "base_publication_sha256",
                "investigation_id",
                "opinion_id",
                "opinion_version",
                "opinion_content_sha256",
                "reviewer_id",
                "approver_id",
            }
        ),
        "PublicationDocumentV6 provenance",
    )
    return PublicationDocumentV6ProvenanceV1(
        base_publication_artifact_id=json_uuid(
            item["base_publication_artifact_id"], "V6 base publication artifact ID"
        ),
        base_publication_sha256=json_sha256(
            item["base_publication_sha256"], "V6 base publication SHA-256"
        ),
        investigation_id=json_uuid(item["investigation_id"], "V6 investigation ID")
        if item["investigation_id"] is not None
        else None,
        opinion_id=json_uuid(item["opinion_id"], "V6 opinion ID")
        if item["opinion_id"] is not None
        else None,
        opinion_version=json_int(item["opinion_version"], "V6 opinion version")
        if item["opinion_version"] is not None
        else None,
        opinion_content_sha256=json_sha256(
            item["opinion_content_sha256"], "V6 opinion content SHA-256"
        )
        if item["opinion_content_sha256"] is not None
        else None,
        reviewer_id=json_text(item["reviewer_id"], "V6 reviewer ID")
        if item["reviewer_id"] is not None
        else None,
        approver_id=json_text(item["approver_id"], "V6 approver ID")
        if item["approver_id"] is not None
        else None,
    )


def _opinion_certainty(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError("Opinion certainty must be between 0 and 1")
    return float(value)


def _analyst_opinion_from_json(raw: Any) -> AnalystOpinionV1:
    fields = frozenset(
        {
            "schema",
            "id",
            "version",
            "investigation_id",
            "synthesis_artifact_id",
            "synthesis_sha256",
            "base_publication_artifact_id",
            "base_publication_sha256",
            "blocks",
            "unresolved_points",
            "author_id",
            "reviewer_id",
            "status",
            "created_at",
            "content_sha256",
        }
    )
    if isinstance(raw, Mapping) and "protocol_figures" in raw:
        fields = fields | {"protocol_figures"}
    item = json_object(raw, fields, "AnalystOpinionV1")
    if item["schema"] != "analyst-opinion.v1":
        raise ValueError("AnalystOpinionV1 schema is unsupported")
    blocks: list[OpinionBlock] = []
    for raw_block in json_array(item["blocks"], "Opinion blocks"):
        block = json_object(
            raw_block,
            frozenset(
                {
                    "type",
                    "text",
                    "finding_refs",
                    "evidence_refs",
                    "qualification",
                    "certainty",
                }
            ),
            "Opinion block",
        )
        blocks.append(
            OpinionBlock(
                block_type=json_enum(OpinionBlockType, block["type"], "Opinion block type"),
                text=json_text(block["text"], "Opinion block text"),
                finding_refs=tuple(
                    json_uuid(value, "Opinion finding reference")
                    for value in json_array(block["finding_refs"], "Opinion finding references")
                ),
                evidence_refs=tuple(
                    json_uuid(value, "Opinion evidence reference")
                    for value in json_array(block["evidence_refs"], "Opinion evidence references")
                ),
                qualification=json_enum(
                    OpinionClaimQualification, block["qualification"], "Opinion qualification"
                ),
                certainty=_opinion_certainty(block["certainty"]),
            )
        )
    unresolved = tuple(
        json_text(value, "Opinion unresolved point")
        for value in json_array(item["unresolved_points"], "Opinion unresolved points")
    )
    try:
        return AnalystOpinionV1(
            id=json_uuid(item["id"], "Opinion ID"),
            version=json_int(item["version"], "Opinion version"),
            investigation_id=json_uuid(item["investigation_id"], "Opinion investigation ID"),
            synthesis_artifact_id=json_uuid(
                item["synthesis_artifact_id"], "Opinion synthesis artifact ID"
            ),
            synthesis_sha256=json_sha256(item["synthesis_sha256"], "Opinion synthesis SHA-256"),
            base_publication_artifact_id=json_uuid(
                item["base_publication_artifact_id"], "Opinion base publication artifact ID"
            ),
            base_publication_sha256=json_sha256(
                item["base_publication_sha256"], "Opinion base publication SHA-256"
            ),
            blocks=tuple(blocks),
            unresolved_points=unresolved,
            protocol_figures=tuple(
                OpinionProtocolFigureV1.from_payload(value)
                for value in json_array(
                    item.get("protocol_figures", []), "Opinion protocol figures"
                )
            ),
            author_id=json_text(item["author_id"], "Opinion author ID"),
            reviewer_id=json_text(item["reviewer_id"], "Opinion reviewer ID")
            if item["reviewer_id"] is not None
            else None,
            status=json_enum(AnalystOpinionStatus, item["status"], "Opinion status"),
            created_at=datetime.fromisoformat(json_text(item["created_at"], "Opinion created_at")),
            content_sha256=json_sha256(item["content_sha256"], "Opinion content SHA-256"),
        )
    except (TypeError, ValueError) as exc:
        raise PublicationDocumentV6ValidationError(
            PublicationDocumentV6ErrorCode.OPINION_INVALID
        ) from exc


def _rendered_opinion_to_json(
    value: PublicationAnalystOpinionSectionV1,
) -> dict[str, Any]:
    payload = {
        "opinion_id": str(value.opinion_id),
        "opinion_version": value.opinion_version,
        "content_sha256": value.content_sha256,
        "blocks": [
            {
                "type": block.block_type.value,
                "heading": block.heading,
                "text": block.text,
                "qualification": block.qualification.value,
                "certainty": block.certainty,
                "finding_refs": [str(ref) for ref in block.finding_refs],
                "evidence_refs": [str(ref) for ref in block.evidence_refs],
            }
            for block in value.blocks
        ],
        "unresolved_points": list(value.unresolved_points),
    }
    if value.protocol_figures:
        payload["protocol_figures"] = [item.to_payload() for item in value.protocol_figures]
    return payload


def _rendered_opinion_from_json(raw: Any) -> PublicationAnalystOpinionSectionV1:
    fields = {"opinion_id", "opinion_version", "content_sha256", "blocks", "unresolved_points"}
    if isinstance(raw, Mapping) and "protocol_figures" in raw:
        fields.add("protocol_figures")
    item = json_object(
        raw,
        frozenset(fields),
        "Rendered analyst opinion",
    )
    blocks: list[PublicationAnalystOpinionBlockV1] = []
    for raw_block in json_array(item["blocks"], "Rendered opinion blocks"):
        block = json_object(
            raw_block,
            frozenset(
                {
                    "type",
                    "heading",
                    "text",
                    "qualification",
                    "certainty",
                    "finding_refs",
                    "evidence_refs",
                }
            ),
            "Rendered opinion block",
        )
        blocks.append(
            PublicationAnalystOpinionBlockV1(
                block_type=json_enum(
                    OpinionBlockType, block["type"], "Rendered opinion block type"
                ),
                heading=json_text(block["heading"], "Rendered opinion heading"),
                text=json_text(block["text"], "Rendered opinion text"),
                qualification=json_enum(
                    OpinionClaimQualification,
                    block["qualification"],
                    "Rendered opinion qualification",
                ),
                certainty=_opinion_certainty(block["certainty"]),
                finding_refs=tuple(
                    json_uuid(value, "Rendered finding reference")
                    for value in json_array(block["finding_refs"], "Rendered finding references")
                ),
                evidence_refs=tuple(
                    json_uuid(value, "Rendered evidence reference")
                    for value in json_array(block["evidence_refs"], "Rendered evidence references")
                ),
            )
        )
    return PublicationAnalystOpinionSectionV1(
        opinion_id=json_uuid(item["opinion_id"], "Rendered opinion ID"),
        opinion_version=json_int(item["opinion_version"], "Rendered opinion version"),
        content_sha256=json_sha256(item["content_sha256"], "Rendered opinion content SHA-256"),
        blocks=tuple(blocks),
        unresolved_points=tuple(
            json_text(value, "Rendered unresolved point")
            for value in json_array(item["unresolved_points"], "Rendered unresolved points")
        ),
        protocol_figures=tuple(
            OpinionProtocolFigureV1.from_payload(value)
            for value in json_array(item.get("protocol_figures", []), "Rendered protocol figures")
        ),
    )


type CanonicalPublicationDocument = (
    PublicationDocumentV4 | PublicationDocumentV5 | PublicationDocumentV6
)


def publication_document_v4_to_json(document: PublicationDocumentV4) -> dict[str, Any]:
    if not isinstance(document, PublicationDocumentV4):
        raise ValueError("Expected a PublicationDocumentV4")
    return document._to_json()


def publication_document_v4_from_json(payload: Mapping[str, Any]) -> PublicationDocumentV4:
    return PublicationDocumentV4._from_json(payload)


def publication_document_v5_to_json(document: PublicationDocumentV5) -> dict[str, Any]:
    if not isinstance(document, PublicationDocumentV5):
        raise ValueError("Expected a PublicationDocumentV5")
    return document._to_json()


def publication_document_v5_from_json(payload: Mapping[str, Any]) -> PublicationDocumentV5:
    return PublicationDocumentV5._from_json(payload)


def publication_document_v6_from_json(payload: Mapping[str, Any]) -> PublicationDocumentV6:
    return PublicationDocumentV6._from_json(payload)


def publication_document_from_json(payload: Mapping[str, Any]) -> CanonicalPublicationDocument:
    version = payload.get("schema_version")
    if version == PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
        return publication_document_v4_from_json(payload)
    if version == PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION:
        return publication_document_v5_from_json(payload)
    if version == PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION:
        return publication_document_v6_from_json(payload)
    raise ValueError(f"unsupported publication document schema_version={version!r}")


def validate_publication_document(
    value: Mapping[str, Any] | CanonicalPublicationDocument,
) -> CanonicalPublicationDocument:
    """Validate one parsed model or canonical JSON payload."""
    if isinstance(value, (PublicationDocumentV4, PublicationDocumentV5, PublicationDocumentV6)):
        if value.schema_version not in {
            PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
            PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
            PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION,
        }:
            raise ValueError(
                f"unsupported publication document schema_version={value.schema_version!r}"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError("publication document must be a JSON object")
    return publication_document_from_json(value)


def parse_publication_document(payload: Mapping[str, Any]) -> CanonicalPublicationDocument:
    """Parse and validate the canonical publication JSON payload."""
    return validate_publication_document(payload)


def serialize_publication_document(
    document: CanonicalPublicationDocument,
) -> dict[str, Any]:
    """Validate and serialize the canonical publication model."""
    validated = validate_publication_document(document)
    if isinstance(validated, PublicationDocumentV6):
        return validated._to_json()
    if isinstance(validated, PublicationDocumentV5):
        return publication_document_v5_to_json(validated)
    return publication_document_v4_to_json(validated)


__all__ = [
    "PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION",
    "PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION",
    "PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION",
    "CanonicalPublicationDocument",
    "PublicationAnalystOpinionBlockV1",
    "PublicationAnalystOpinionSectionV1",
    "PublicationChartV1",
    "PublicationDiagramV1",
    "PublicationDocumentV4",
    "PublicationDocumentV5",
    "PublicationDocumentV6",
    "PublicationDocumentV6BaseMismatchError",
    "PublicationDocumentV6ErrorCode",
    "PublicationDocumentV6ProvenanceV1",
    "PublicationDocumentV6ValidationError",
    "PublicationSourceFigureV1",
    "PublicationTableColumnV1",
    "PublicationTableRowV1",
    "PublicationTableV1",
    "order_publication_references",
    "parse_publication_document",
    "publication_document_from_json",
    "publication_document_text_anchors",
    "publication_document_v4_from_json",
    "publication_document_v4_to_json",
    "publication_document_v5_from_json",
    "publication_document_v5_sha256",
    "publication_document_v5_to_json",
    "publication_document_v6_from_json",
    "serialize_publication_document",
    "validate_publication_document",
]
