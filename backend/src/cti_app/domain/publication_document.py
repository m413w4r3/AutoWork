"""Canonical PublicationDocumentV4 model, validation and strict JSON codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from cti_app.domain.discovery import SourceRole
from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production_editorial_enrichment import (
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
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import ExtractionEvidenceRefV1
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
)

PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION = "4"


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


def _diagram_to_json(diagram: PublicationDiagramV1) -> dict[str, Any]:
    return {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [diagram_node_to_json(node) for node in diagram.nodes],
        "edges": [diagram_edge_to_json(edge) for edge in diagram.edges],
        "groups": [diagram_group_to_json(group) for group in diagram.groups],
        "placement": placement_to_json(diagram.placement),
        "asset_id": str(diagram.asset_id),
    }


def _diagram_from_json(raw: Any) -> PublicationDiagramV1:
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
            diagram_node_from_json(value) for value in json_array(item["nodes"], "Diagram nodes")
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
        }

    @classmethod
    def _from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV4:
        document = json_object(
            payload,
            frozenset(
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
            ),
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
            )
            for item in (
                json_object(
                    raw,
                    frozenset(
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
                    ),
                    "Publication source",
                )
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
                _diagram_from_json(raw) for raw in json_array(document["diagrams"], "Diagrams")
            ),
            figures=tuple(
                _figure_from_json(raw) for raw in json_array(document["figures"], "Figures")
            ),
        )


type CanonicalPublicationDocument = PublicationDocumentV4


def publication_document_v4_to_json(document: PublicationDocumentV4) -> dict[str, Any]:
    if not isinstance(document, PublicationDocumentV4):
        raise ValueError("Expected a PublicationDocumentV4")
    return document._to_json()


def publication_document_v4_from_json(payload: Mapping[str, Any]) -> PublicationDocumentV4:
    return PublicationDocumentV4._from_json(payload)


def validate_publication_document(
    value: Mapping[str, Any] | CanonicalPublicationDocument,
) -> CanonicalPublicationDocument:
    """Validate one parsed model or canonical JSON payload."""
    if isinstance(value, PublicationDocumentV4):
        if value.schema_version != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported publication document schema_version={value.schema_version!r}"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError("publication document must be a JSON object")
    schema_version = value.get("schema_version")
    if schema_version != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
        raise ValueError(f"unsupported publication document schema_version={schema_version!r}")
    return publication_document_v4_from_json(value)


def parse_publication_document(payload: Mapping[str, Any]) -> CanonicalPublicationDocument:
    """Parse and validate the canonical publication JSON payload."""
    return validate_publication_document(payload)


def serialize_publication_document(
    document: CanonicalPublicationDocument,
) -> dict[str, Any]:
    """Validate and serialize the canonical publication model."""
    return publication_document_v4_to_json(validate_publication_document(document))


__all__ = [
    "PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION",
    "CanonicalPublicationDocument",
    "PublicationDiagramV1",
    "PublicationDocumentV4",
    "PublicationSourceFigureV1",
    "PublicationTableColumnV1",
    "PublicationTableRowV1",
    "PublicationTableV1",
    "parse_publication_document",
    "publication_document_v4_from_json",
    "publication_document_v4_to_json",
    "serialize_publication_document",
    "validate_publication_document",
]
