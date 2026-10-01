"""Canonical, renderer-independent Editorial Enrichment contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator, model_validator

from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
)

EDITORIAL_ENRICHMENT_SCHEMA_VERSION = 1
EDITORIAL_ENRICHMENT_POLICY_VERSION = "editorial-enrichment-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Diagram labels are displayed text: line breaks and tabs are allowed, other control
# characters and lone surrogates have no visible form and are rejected.
_LABEL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def _text(value: Any, label: str, *, semantic: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if semantic and not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _diagram_label(value: Any, label: str, *, semantic: bool = True) -> str:
    text = _text(value, label, semantic=semantic)
    if _LABEL_CONTROL.search(text):
        raise ValueError(f"{label} must not contain control characters")
    return text


def _key(value: Any, label: str) -> str:
    text = _text(value, label)
    if _KEY.fullmatch(text) is None:
        raise ValueError(f"{label} must match the editorial key format")
    return text


def _normalize_evidence_refs(
    refs: tuple[ExtractionEvidenceRefV1, ...], label: str, *, required: bool = True
) -> tuple[ExtractionEvidenceRefV1, ...]:
    if not isinstance(refs, tuple):
        raise ValueError(f"{label} must be a tuple of evidence references")
    if required and not refs:
        raise ValueError(f"{label} requires at least one evidence reference")
    if any(not isinstance(ref, ExtractionEvidenceRefV1) for ref in refs):
        raise ValueError(f"{label} contains an invalid evidence reference")
    if len(refs) != len(set(refs)):
        raise ValueError(f"{label} must not repeat evidence references")
    return tuple(sorted(refs, key=evidence_ref_sort_key))


class EnrichmentPlacementKind(StrEnum):
    AFTER_LEAD = "after_lead"
    AFTER_SECTION = "after_section"
    AFTER_TIMELINE = "after_timeline"
    END = "end"


@dataclass(frozen=True, slots=True)
class EnrichmentPlacementV1:
    kind: EnrichmentPlacementKind
    section_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EnrichmentPlacementKind):
            raise ValueError("Editorial enrichment placement kind is invalid")
        if self.kind is EnrichmentPlacementKind.AFTER_SECTION:
            if type(self.section_index) is not int or self.section_index < 0:
                raise ValueError("AFTER_SECTION requires a non-negative section_index")
        elif self.section_index is not None:
            raise ValueError("section_index is only valid for AFTER_SECTION")


class EnrichmentTableKind(StrEnum):
    COMMANDS = "commands"
    DROPPED_FILES = "dropped_files"
    TOOLS = "tools"
    TECHNIQUES = "techniques"
    INFRASTRUCTURE = "infrastructure"
    TIMELINE = "timeline"
    CONFIGURATION = "configuration"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class TableColumnV1:
    key: str
    label: str

    def __post_init__(self) -> None:
        _text(self.key, "Table column key", semantic=True)
        _text(self.label, "Table column label", semantic=True)


@dataclass(frozen=True, slots=True)
class TableRowV1:
    cells: tuple[str, ...]
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cells, tuple) or any(
            not isinstance(cell, str) for cell in self.cells
        ):
            raise ValueError("Table row cells must be a tuple of text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_evidence_refs(self.evidence_refs, "Table row evidence references"),
        )


@dataclass(frozen=True, slots=True)
class TableSpecV1:
    key: str
    kind: EnrichmentTableKind
    title: str
    caption: str | None
    columns: tuple[TableColumnV1, ...]
    rows: tuple[TableRowV1, ...]
    placement: EnrichmentPlacementV1

    def __post_init__(self) -> None:
        _key(self.key, "Table key")
        if not isinstance(self.kind, EnrichmentTableKind):
            raise ValueError("Editorial enrichment table kind is invalid")
        _text(self.title, "Table title", semantic=True)
        if self.caption is not None:
            _text(self.caption, "Table caption")
            if not self.caption.strip():
                object.__setattr__(self, "caption", None)
        if not isinstance(self.columns, tuple) or any(
            not isinstance(column, TableColumnV1) for column in self.columns
        ):
            raise ValueError("Table columns must be a tuple of TableColumnV1")
        if len(self.columns) < 2:
            raise ValueError("Editorial enrichment tables require at least two columns")
        column_keys = [column.key for column in self.columns]
        if len(column_keys) != len(set(column_keys)):
            raise ValueError("Table column keys must be unique")
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, TableRowV1) for row in self.rows
        ):
            raise ValueError("Table rows must be a tuple of TableRowV1")
        if not self.rows:
            raise ValueError("Editorial enrichment tables require at least one row")
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError("Table row width must match the number of columns")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Table placement is invalid")


class EnrichmentDiagramKind(StrEnum):
    INFECTION_CHAIN = "infection_chain"
    NETWORK_FLOW = "network_flow"
    EXECUTION_SEQUENCE = "execution_sequence"
    COMPONENT_RELATIONSHIP = "component_relationship"
    INFRASTRUCTURE = "infrastructure"
    CUSTOM = "custom"


class EnrichmentDiagramDirection(StrEnum):
    LEFT_TO_RIGHT = "left_to_right"
    TOP_TO_BOTTOM = "top_to_bottom"


@dataclass(frozen=True, slots=True)
class DiagramNodeV1:
    node_id: str
    label: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        _text(self.node_id, "Diagram node ID", semantic=True)
        _diagram_label(self.label, "Diagram node label")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_evidence_refs(self.evidence_refs, "Diagram node evidence references"),
        )


@dataclass(frozen=True, slots=True)
class DiagramEdgeV1:
    source_node_id: str
    target_node_id: str
    label: str | None
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        _text(self.source_node_id, "Diagram edge source node ID", semantic=True)
        _text(self.target_node_id, "Diagram edge target node ID", semantic=True)
        if self.label is not None:
            _diagram_label(self.label, "Diagram edge label", semantic=False)
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_evidence_refs(self.evidence_refs, "Diagram edge evidence references"),
        )


@dataclass(frozen=True, slots=True)
class DiagramGroupV1:
    group_id: str
    label: str
    node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.group_id, "Diagram group ID", semantic=True)
        _diagram_label(self.label, "Diagram group label")
        if not isinstance(self.node_ids, tuple) or any(
            not isinstance(node_id, str) or not node_id.strip() for node_id in self.node_ids
        ):
            raise ValueError("Diagram group node IDs must be a tuple of non-empty text")
        if not self.node_ids:
            raise ValueError("Diagram groups must contain at least one node")
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("A diagram group must not repeat a node")


@dataclass(frozen=True, slots=True)
class DiagramSpecV1:
    key: str
    kind: EnrichmentDiagramKind
    title: str
    caption: str | None
    direction: EnrichmentDiagramDirection
    nodes: tuple[DiagramNodeV1, ...]
    edges: tuple[DiagramEdgeV1, ...]
    groups: tuple[DiagramGroupV1, ...]
    placement: EnrichmentPlacementV1
    compiled_asset_id: UUID | None = None

    def __post_init__(self) -> None:
        _key(self.key, "Diagram key")
        if not isinstance(self.kind, EnrichmentDiagramKind):
            raise ValueError("Editorial enrichment diagram kind is invalid")
        _text(self.title, "Diagram title", semantic=True)
        if self.caption is not None:
            _text(self.caption, "Diagram caption")
            if not self.caption.strip():
                object.__setattr__(self, "caption", None)
        if not isinstance(self.direction, EnrichmentDiagramDirection):
            raise ValueError("Editorial enrichment diagram direction is invalid")
        for label, values, item_type in (
            ("nodes", self.nodes, DiagramNodeV1),
            ("edges", self.edges, DiagramEdgeV1),
            ("groups", self.groups, DiagramGroupV1),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, item_type) for value in values
            ):
                raise ValueError(f"Diagram {label} must be a tuple of {item_type.__name__}")
        if len(self.nodes) < 2:
            raise ValueError("Editorial enrichment diagrams require at least two nodes")
        if not self.edges:
            raise ValueError("Editorial enrichment diagrams require at least one edge")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Diagram node IDs must be unique")
        group_ids = [group.group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("Diagram group IDs must be unique")
        known_node_ids = set(node_ids)
        if any(
            edge.source_node_id not in known_node_ids or edge.target_node_id not in known_node_ids
            for edge in self.edges
        ):
            raise ValueError("Diagram edge endpoints must reference existing nodes")
        if any(not set(group.node_ids) <= known_node_ids for group in self.groups):
            raise ValueError("Diagram groups must reference existing nodes")
        grouped_node_ids = [node_id for group in self.groups for node_id in group.node_ids]
        if len(grouped_node_ids) != len(set(grouped_node_ids)):
            raise ValueError("Diagram groups must not share nodes")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Diagram placement is invalid")
        if self.compiled_asset_id is not None and not isinstance(self.compiled_asset_id, UUID):
            raise ValueError("Compiled diagram asset identity must be a UUID")


class SourceFigureInclusionStatus(StrEnum):
    PROPOSED = "proposed"
    INCLUDED = "included"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class SourceFigureLocatorV1:
    page: int | None = None
    section: str | None = None
    figure_label: str | None = None
    original_asset_url: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("Figure page must be a positive integer")
        for field_name in ("section", "figure_label", "original_asset_url"):
            value = getattr(self, field_name)
            if value is not None:
                _text(value, f"Figure locator {field_name}", semantic=True)
        if all(
            value is None
            for value in (self.page, self.section, self.figure_label, self.original_asset_url)
        ):
            raise ValueError("A source figure locator requires at least one location")


class SourceFigureDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"


SourceFigureMimeType = str


def source_figure_id(
    *, source_document_id: UUID, sha256: str | None, source: str, locator: SourceFigureLocatorV1
) -> UUID:
    """Return the stable identity for one media hash or unresolved source location."""
    if sha256 is not None:
        identity = f"source-figure-v1:sha256:{sha256}"
    else:
        identity = "source-figure-v1:location:" + ":".join(
            (
                str(source_document_id),
                source,
                str(locator.page or ""),
                locator.section or "",
                locator.figure_label or "",
                locator.original_asset_url or "",
            )
        )
    return uuid5(NAMESPACE_URL, identity)


class ResolvedSourceFigureV1(BaseModel):
    """Strict inventory record for one archived or unresolved source figure."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True
    )

    figure_id: UUID
    blob_id: UUID | None
    sha256: StrictStr | None
    mime_type: SourceFigureMimeType | None
    byte_size: StrictInt | None
    source_document_id: UUID
    source: StrictStr
    provenance: StrictStr
    locator: SourceFigureLocatorV1
    decision: SourceFigureDecision
    decision_reason: StrictStr

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("Source figure SHA-256 must be lowercase hexadecimal")
        return value

    @field_validator("mime_type")
    @classmethod
    def _supported_mime_type(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_MEDIA_MIME_TYPES:
            raise ValueError("Source figure MIME type is not supported")
        return value

    @field_validator("source", "provenance", "decision_reason")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Source figure text fields must not be empty")
        return value

    @field_validator("byte_size")
    @classmethod
    def _valid_byte_size(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("Source figure byte size must not be negative")
        return value

    @model_validator(mode="after")
    def _valid_resolution(self) -> ResolvedSourceFigureV1:
        if not isinstance(self.locator, SourceFigureLocatorV1):
            raise ValueError("Source figure locator is invalid")
        if self.figure_id != source_figure_id(
            source_document_id=self.source_document_id,
            sha256=self.sha256,
            source=self.source,
            locator=self.locator,
        ):
            raise ValueError("Source figure identity is not deterministic")
        metadata = (self.blob_id, self.sha256, self.mime_type, self.byte_size)
        if self.decision is SourceFigureDecision.ACCEPTED and any(
            value is None for value in metadata
        ):
            raise ValueError("An accepted source figure requires complete archived blob metadata")
        if self.blob_id is not None and any(value is None for value in metadata[1:]):
            raise ValueError("An archived source figure blob requires complete metadata")
        return self


@dataclass(frozen=True, slots=True)
class SourceFigureCandidateV1:
    key: str
    source_document_id: UUID
    source_url: str
    caption: str
    provenance: str
    locator: SourceFigureLocatorV1
    inclusion_status: SourceFigureInclusionStatus
    placement: EnrichmentPlacementV1
    resolved_figure: ResolvedSourceFigureV1 | None = None

    def __post_init__(self) -> None:
        _key(self.key, "Source figure key")
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Source figure document identity must be a UUID")
        _text(self.source_url, "Source figure URL", semantic=True)
        _text(self.caption, "Source figure caption", semantic=True)
        _text(self.provenance, "Source figure provenance", semantic=True)
        if not isinstance(self.locator, SourceFigureLocatorV1):
            raise ValueError("Source figure locator is invalid")
        if not isinstance(self.inclusion_status, SourceFigureInclusionStatus):
            raise ValueError("Source figure inclusion status is invalid")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Source figure placement is invalid")
        if self.resolved_figure is not None and (
            not isinstance(self.resolved_figure, ResolvedSourceFigureV1)
            or self.resolved_figure.decision is not SourceFigureDecision.ACCEPTED
            or self.resolved_figure.source_document_id != self.source_document_id
            or self.resolved_figure.source != self.source_url
            or self.resolved_figure.locator != self.locator
        ):
            raise ValueError("A source figure candidate must reference an accepted local figure")


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentV1:
    schema_version: int
    subject_id: UUID
    production_input_hash: str
    extraction_hash: str
    synthesis_hash: str
    publication_language: str
    enrichment_policy_version: str
    tables: tuple[TableSpecV1, ...]
    diagrams: tuple[DiagramSpecV1, ...]
    source_figures: tuple[SourceFigureCandidateV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or (
            self.schema_version != EDITORIAL_ENRICHMENT_SCHEMA_VERSION
        ):
            raise ValueError("Editorial enrichment schema version is unsupported")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Editorial enrichment subject identity must be a UUID")
        for field_name in ("production_input_hash", "extraction_hash", "synthesis_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"Editorial enrichment {field_name} must be a lowercase SHA-256")
        _text(self.publication_language, "Publication language", semantic=True)
        if self.enrichment_policy_version != EDITORIAL_ENRICHMENT_POLICY_VERSION:
            raise ValueError("Editorial enrichment policy version is unsupported")
        for label, values, item_type in (
            ("tables", self.tables, TableSpecV1),
            ("diagrams", self.diagrams, DiagramSpecV1),
            ("source figures", self.source_figures, SourceFigureCandidateV1),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, item_type) for value in values
            ):
                raise ValueError(f"Editorial enrichment {label} have an invalid type")
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) or not warning.strip() for warning in self.warnings
        ):
            raise ValueError("Editorial enrichment warnings must be non-empty strings")
        keys = [
            *(table.key for table in self.tables),
            *(diagram.key for diagram in self.diagrams),
            *(figure.key for figure in self.source_figures),
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Editorial enrichment keys must be globally unique")
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))


def editorial_enrichment_evidence_refs(
    enrichment: EditorialEnrichmentV1,
) -> frozenset[ExtractionEvidenceRefV1]:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    refs = {ref for table in enrichment.tables for row in table.rows for ref in row.evidence_refs}
    refs.update(
        ref
        for diagram in enrichment.diagrams
        for node in diagram.nodes
        for ref in node.evidence_refs
    )
    refs.update(
        ref
        for diagram in enrichment.diagrams
        for edge in diagram.edges
        for ref in edge.evidence_refs
    )
    return frozenset(refs)


def _ref_to_json(ref: ExtractionEvidenceRefV1) -> dict[str, str]:
    return {
        "source_document_id": str(ref.source_document_id),
        "kind": ref.kind.value,
        "evidence_key": ref.evidence_key,
    }


def _placement_to_json(placement: EnrichmentPlacementV1) -> dict[str, Any]:
    return {"kind": placement.kind.value, "section_index": placement.section_index}


def placement_to_json(placement: EnrichmentPlacementV1) -> dict[str, Any]:
    """Serialize a placement shared by enrichment and publication documents."""
    return _placement_to_json(placement)


def _table_to_json(table: TableSpecV1) -> dict[str, Any]:
    return {
        "key": table.key,
        "kind": table.kind.value,
        "title": table.title,
        "caption": table.caption,
        "columns": [{"key": column.key, "label": column.label} for column in table.columns],
        "rows": [
            {
                "cells": list(row.cells),
                "evidence_refs": [_ref_to_json(ref) for ref in row.evidence_refs],
            }
            for row in table.rows
        ],
        "placement": _placement_to_json(table.placement),
    }


def _diagram_to_json(diagram: DiagramSpecV1) -> dict[str, Any]:
    payload = {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [diagram_node_to_json(node) for node in diagram.nodes],
        "edges": [diagram_edge_to_json(edge) for edge in diagram.edges],
        "groups": [diagram_group_to_json(group) for group in diagram.groups],
        "placement": _placement_to_json(diagram.placement),
    }
    if diagram.compiled_asset_id is not None:
        payload["compiled_asset_id"] = str(diagram.compiled_asset_id)
    return payload


def diagram_node_to_json(node: DiagramNodeV1) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "label": node.label,
        "evidence_refs": [evidence_ref_to_json(ref) for ref in node.evidence_refs],
    }


def diagram_edge_to_json(edge: DiagramEdgeV1) -> dict[str, Any]:
    return {
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "label": edge.label,
        "evidence_refs": [evidence_ref_to_json(ref) for ref in edge.evidence_refs],
    }


def diagram_group_to_json(group: DiagramGroupV1) -> dict[str, Any]:
    return {"group_id": group.group_id, "label": group.label, "node_ids": list(group.node_ids)}


def source_figure_locator_to_json(locator: SourceFigureLocatorV1) -> dict[str, Any]:
    return {
        "page": locator.page,
        "section": locator.section,
        "figure_label": locator.figure_label,
        "original_asset_url": locator.original_asset_url,
    }


def _figure_to_json(figure: SourceFigureCandidateV1) -> dict[str, Any]:
    payload = {
        "key": figure.key,
        "source_document_id": str(figure.source_document_id),
        "source_url": figure.source_url,
        "caption": figure.caption,
        "provenance": figure.provenance,
        "locator": source_figure_locator_to_json(figure.locator),
        "inclusion_status": figure.inclusion_status.value,
        "placement": _placement_to_json(figure.placement),
    }
    if figure.resolved_figure is not None:
        resolved = figure.resolved_figure
        payload["resolved_figure"] = {
            "figure_id": str(resolved.figure_id),
            "blob_id": str(resolved.blob_id),
            "sha256": resolved.sha256,
            "mime_type": resolved.mime_type,
            "byte_size": resolved.byte_size,
            "source_document_id": str(resolved.source_document_id),
            "source": resolved.source,
            "provenance": resolved.provenance,
            "locator": source_figure_locator_to_json(resolved.locator),
            "decision": resolved.decision.value,
            "decision_reason": resolved.decision_reason,
        }
    return payload


def editorial_enrichment_to_json(enrichment: EditorialEnrichmentV1) -> dict[str, Any]:
    """Return the strict JSON-compatible canonical representation."""
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    return {
        "schema_version": enrichment.schema_version,
        "subject_id": str(enrichment.subject_id),
        "production_input_hash": enrichment.production_input_hash,
        "extraction_hash": enrichment.extraction_hash,
        "synthesis_hash": enrichment.synthesis_hash,
        "publication_language": enrichment.publication_language,
        "enrichment_policy_version": enrichment.enrichment_policy_version,
        "tables": [_table_to_json(table) for table in enrichment.tables],
        "diagrams": [_diagram_to_json(diagram) for diagram in enrichment.diagrams],
        "source_figures": [_figure_to_json(figure) for figure in enrichment.source_figures],
        "warnings": list(enrichment.warnings),
    }


_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "subject_id",
        "production_input_hash",
        "extraction_hash",
        "synthesis_hash",
        "publication_language",
        "enrichment_policy_version",
        "tables",
        "diagrams",
        "source_figures",
        "warnings",
    }
)
_REF_KEYS = frozenset({"source_document_id", "kind", "evidence_key"})
_PLACEMENT_KEYS = frozenset({"kind", "section_index"})
_COLUMN_KEYS = frozenset({"key", "label"})
_ROW_KEYS = frozenset({"cells", "evidence_refs"})
_TABLE_KEYS = frozenset({"key", "kind", "title", "caption", "columns", "rows", "placement"})
_NODE_KEYS = frozenset({"node_id", "label", "evidence_refs"})
_EDGE_KEYS = frozenset({"source_node_id", "target_node_id", "label", "evidence_refs"})
_GROUP_KEYS = frozenset({"group_id", "label", "node_ids"})
_DIAGRAM_BASE_KEYS = frozenset(
    {"key", "kind", "title", "caption", "direction", "nodes", "edges", "groups", "placement"}
)
_DIAGRAM_KEYS = _DIAGRAM_BASE_KEYS | {"compiled_asset_id"}
_LOCATOR_KEYS = frozenset({"page", "section", "figure_label", "original_asset_url"})
_FIGURE_KEYS = frozenset(
    {
        "key",
        "source_document_id",
        "source_url",
        "caption",
        "provenance",
        "locator",
        "inclusion_status",
        "placement",
        "resolved_figure",
    }
)
_FIGURE_BASE_KEYS = _FIGURE_KEYS - {"resolved_figure"}


def _object(raw: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError(f"{label} has missing or extra fields")
    return raw


def _array(raw: Any, label: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a JSON array")
    return raw


def _uuid(raw: Any, label: str) -> UUID:
    value = _text(raw, label)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return parsed


def _sha256(raw: Any, label: str) -> str:
    value = _text(raw, label)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _enum(enum_type: type[Any], raw: Any, label: str) -> Any:
    value = _text(raw, label)
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc


def _ref_from_json(raw: Any) -> ExtractionEvidenceRefV1:
    payload = _object(raw, _REF_KEYS, "Evidence reference")
    return ExtractionEvidenceRefV1(
        source_document_id=_uuid(payload["source_document_id"], "Evidence source document ID"),
        kind=_enum(EvidenceKind, payload["kind"], "Evidence kind"),
        evidence_key=_sha256(payload["evidence_key"], "Evidence key"),
    )


def _refs_from_json(raw: Any, label: str) -> tuple[ExtractionEvidenceRefV1, ...]:
    return tuple(_ref_from_json(value) for value in _array(raw, label))


def _placement_from_json(raw: Any) -> EnrichmentPlacementV1:
    payload = _object(raw, _PLACEMENT_KEYS, "Editorial placement")
    section_index = payload["section_index"]
    if section_index is not None and type(section_index) is not int:
        raise ValueError("Editorial placement section_index must be an integer or null")
    return EnrichmentPlacementV1(
        kind=_enum(EnrichmentPlacementKind, payload["kind"], "Editorial placement kind"),
        section_index=section_index,
    )


def _table_from_json(raw: Any) -> TableSpecV1:
    payload = _object(raw, _TABLE_KEYS, "Editorial table")
    caption = payload["caption"]
    if caption is not None:
        caption = _text(caption, "Table caption")
    columns = tuple(
        TableColumnV1(
            key=_text(column["key"], "Table column key", semantic=True),
            label=_text(column["label"], "Table column label", semantic=True),
        )
        for column in (
            _object(value, _COLUMN_KEYS, "Table column")
            for value in _array(payload["columns"], "Table columns")
        )
    )
    rows = tuple(
        TableRowV1(
            cells=tuple(
                _text(cell, "Table cell")
                for cell in _array(row_payload["cells"], "Table row cells")
            ),
            evidence_refs=_refs_from_json(
                row_payload["evidence_refs"], "Table row evidence references"
            ),
        )
        for row_payload in (
            _object(value, _ROW_KEYS, "Table row")
            for value in _array(payload["rows"], "Table rows")
        )
    )
    return TableSpecV1(
        key=_text(payload["key"], "Table key"),
        kind=_enum(EnrichmentTableKind, payload["kind"], "Table kind"),
        title=_text(payload["title"], "Table title", semantic=True),
        caption=caption,
        columns=columns,
        rows=rows,
        placement=_placement_from_json(payload["placement"]),
    )


def _node_from_json(raw: Any) -> DiagramNodeV1:
    payload = _object(raw, _NODE_KEYS, "Diagram node")
    return DiagramNodeV1(
        node_id=_text(payload["node_id"], "Diagram node ID", semantic=True),
        label=_text(payload["label"], "Diagram node label", semantic=True),
        evidence_refs=_refs_from_json(payload["evidence_refs"], "Diagram node evidence references"),
    )


def _edge_from_json(raw: Any) -> DiagramEdgeV1:
    payload = _object(raw, _EDGE_KEYS, "Diagram edge")
    label = payload["label"]
    if label is not None:
        label = _text(label, "Diagram edge label")
    return DiagramEdgeV1(
        source_node_id=_text(
            payload["source_node_id"], "Diagram edge source node ID", semantic=True
        ),
        target_node_id=_text(
            payload["target_node_id"], "Diagram edge target node ID", semantic=True
        ),
        label=label,
        evidence_refs=_refs_from_json(payload["evidence_refs"], "Diagram edge evidence references"),
    )


def _group_from_json(raw: Any) -> DiagramGroupV1:
    payload = _object(raw, _GROUP_KEYS, "Diagram group")
    return DiagramGroupV1(
        group_id=_text(payload["group_id"], "Diagram group ID", semantic=True),
        label=_text(payload["label"], "Diagram group label", semantic=True),
        node_ids=tuple(
            _text(value, "Diagram group node ID", semantic=True)
            for value in _array(payload["node_ids"], "Diagram group node IDs")
        ),
    )


def _diagram_from_json(raw: Any) -> DiagramSpecV1:
    if not isinstance(raw, Mapping) or frozenset(raw) not in {
        _DIAGRAM_BASE_KEYS,
        _DIAGRAM_KEYS,
    }:
        raise ValueError("Editorial diagram has missing or extra fields")
    payload = raw
    caption = payload["caption"]
    if caption is not None:
        caption = _text(caption, "Diagram caption")
    return DiagramSpecV1(
        key=_text(payload["key"], "Diagram key"),
        kind=_enum(EnrichmentDiagramKind, payload["kind"], "Diagram kind"),
        title=_text(payload["title"], "Diagram title", semantic=True),
        caption=caption,
        direction=_enum(EnrichmentDiagramDirection, payload["direction"], "Diagram direction"),
        nodes=tuple(_node_from_json(value) for value in _array(payload["nodes"], "Diagram nodes")),
        edges=tuple(_edge_from_json(value) for value in _array(payload["edges"], "Diagram edges")),
        groups=tuple(
            _group_from_json(value) for value in _array(payload["groups"], "Diagram groups")
        ),
        placement=_placement_from_json(payload["placement"]),
        compiled_asset_id=(
            _uuid(payload["compiled_asset_id"], "Compiled diagram asset ID")
            if "compiled_asset_id" in payload
            else None
        ),
    )


def _locator_from_json(raw: Any) -> SourceFigureLocatorV1:
    payload = _object(raw, _LOCATOR_KEYS, "Source figure locator")
    page = payload["page"]
    if page is not None and type(page) is not int:
        raise ValueError("Figure page must be an integer or null")
    values: dict[str, str | None] = {}
    for field_name in ("section", "figure_label", "original_asset_url"):
        value = payload[field_name]
        values[field_name] = None if value is None else _text(value, f"Figure locator {field_name}")
    return SourceFigureLocatorV1(
        page=page,
        section=values["section"],
        figure_label=values["figure_label"],
        original_asset_url=values["original_asset_url"],
    )


def _figure_from_json(raw: Any) -> SourceFigureCandidateV1:
    if not isinstance(raw, Mapping) or frozenset(raw) not in {_FIGURE_BASE_KEYS, _FIGURE_KEYS}:
        raise ValueError("Source figure candidate has missing or extra fields")
    payload = raw
    resolved_figure = None
    if "resolved_figure" in payload:
        resolved_raw = _object(
            payload["resolved_figure"],
            frozenset(
                {
                    "figure_id",
                    "blob_id",
                    "sha256",
                    "mime_type",
                    "byte_size",
                    "source_document_id",
                    "source",
                    "provenance",
                    "locator",
                    "decision",
                    "decision_reason",
                }
            ),
            "Resolved source figure",
        )
        sha256 = resolved_raw["sha256"]
        mime_type = resolved_raw["mime_type"]
        byte_size = resolved_raw["byte_size"]
        resolved_figure = ResolvedSourceFigureV1.model_validate(
            {
                "figure_id": _uuid(resolved_raw["figure_id"], "Figure identity"),
                "blob_id": _uuid(resolved_raw["blob_id"], "Figure blob identity"),
                "sha256": None if sha256 is None else _sha256(sha256, "Figure SHA-256"),
                "mime_type": None if mime_type is None else _text(mime_type, "Figure MIME type"),
                "byte_size": byte_size,
                "source_document_id": _uuid(
                    resolved_raw["source_document_id"], "Figure source document ID"
                ),
                "source": _text(resolved_raw["source"], "Figure source", semantic=True),
                "provenance": _text(resolved_raw["provenance"], "Figure provenance", semantic=True),
                "locator": _locator_from_json(resolved_raw["locator"]),
                "decision": _enum(
                    SourceFigureDecision, resolved_raw["decision"], "Figure decision"
                ),
                "decision_reason": _text(
                    resolved_raw["decision_reason"], "Figure decision reason", semantic=True
                ),
            }
        )
    return SourceFigureCandidateV1(
        key=_text(payload["key"], "Source figure key"),
        source_document_id=_uuid(payload["source_document_id"], "Source figure document ID"),
        source_url=_text(payload["source_url"], "Source figure URL", semantic=True),
        caption=_text(payload["caption"], "Source figure caption", semantic=True),
        provenance=_text(payload["provenance"], "Source figure provenance", semantic=True),
        locator=_locator_from_json(payload["locator"]),
        inclusion_status=_enum(
            SourceFigureInclusionStatus,
            payload["inclusion_status"],
            "Source figure inclusion status",
        ),
        placement=_placement_from_json(payload["placement"]),
        resolved_figure=resolved_figure,
    )


def editorial_enrichment_from_json(payload: Mapping[str, Any]) -> EditorialEnrichmentV1:
    """Decode only exact V1 payloads; no unknown fields or coercion are allowed."""
    body = _object(payload, _ROOT_KEYS, "Editorial enrichment")
    schema_version = body["schema_version"]
    if type(schema_version) is not int:
        raise ValueError("Editorial enrichment schema version must be an integer")
    warnings = tuple(
        _text(value, "Editorial enrichment warning", semantic=True)
        for value in _array(body["warnings"], "Editorial enrichment warnings")
    )
    return EditorialEnrichmentV1(
        schema_version=schema_version,
        subject_id=_uuid(body["subject_id"], "Subject ID"),
        production_input_hash=_sha256(body["production_input_hash"], "Production input hash"),
        extraction_hash=_sha256(body["extraction_hash"], "Extraction hash"),
        synthesis_hash=_sha256(body["synthesis_hash"], "Synthesis hash"),
        publication_language=_text(
            body["publication_language"], "Publication language", semantic=True
        ),
        enrichment_policy_version=_text(
            body["enrichment_policy_version"], "Enrichment policy version"
        ),
        tables=tuple(
            _table_from_json(value) for value in _array(body["tables"], "Editorial tables")
        ),
        diagrams=tuple(
            _diagram_from_json(value) for value in _array(body["diagrams"], "Editorial diagrams")
        ),
        source_figures=tuple(
            _figure_from_json(value) for value in _array(body["source_figures"], "Source figures")
        ),
        warnings=warnings,
    )


# Shared strict JSON and value-object helpers used by PublicationDocumentV4.
# Keep the enrichment implementation as the single source for these rules.
validate_text = _text
validate_editorial_key = _key
normalize_evidence_refs = _normalize_evidence_refs
validate_sha256 = _sha256
json_object = _object
json_array = _array
json_text = _text
json_uuid = _uuid
json_sha256 = _sha256
json_enum = _enum


def json_int(raw: Any, label: str) -> int:
    if type(raw) is not int:
        raise ValueError(f"{label} must be an integer")
    return raw


def publication_json_object(raw: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    """Apply the publication codec's historical strict-object error wording."""
    try:
        return _object(raw, keys, label)
    except ValueError as exc:
        raise ValueError(f"{label} fields are invalid") from exc


evidence_ref_to_json = _ref_to_json
evidence_ref_from_json = _ref_from_json
evidence_refs_from_json = _refs_from_json
placement_from_json = _placement_from_json
diagram_node_from_json = _node_from_json
diagram_edge_from_json = _edge_from_json
diagram_group_from_json = _group_from_json
diagram_from_json = _diagram_from_json
source_figure_locator_from_json = _locator_from_json
