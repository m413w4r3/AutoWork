"""Publication domain values and the current renderer-independent document."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier

if TYPE_CHECKING:
    from cti_app.domain.production_editorial_enrichment import (
        DiagramEdgeV1,
        DiagramGroupV1,
        DiagramNodeV1,
        EnrichmentDiagramDirection,
        EnrichmentDiagramKind,
        EnrichmentPlacementKind,
        EnrichmentPlacementV1,
        EnrichmentTableKind,
        SourceFigureLocatorV1,
    )
    from cti_app.domain.production_synthesis import (
        EvidenceKind,
        ExtractionEvidenceRefV1,
        evidence_ref_sort_key,
    )

PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION = "4"


class ArtifactType(StrEnum):
    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    HASH = "hash"
    EMAIL = "email"
    FILEPATH = "filepath"
    FILENAME = "filename"
    CVE = "cve"
    YARA_RULE = "yara_rule"
    SIGMA_RULE = "sigma_rule"
    SURICATA_RULE = "suricata_rule"
    OTHER = "other"


# These are the only artifact types rendered in the publication IOC section.
# The enum is the vocabulary; this set is the single classification used by
# extraction, repair and review projections.
PUBLICATION_IOC_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.IP,
        ArtifactType.DOMAIN,
        ArtifactType.URL,
        ArtifactType.EMAIL,
        ArtifactType.HASH,
    }
)


def is_publication_ioc_artifact_type(value: ArtifactType | str | None) -> bool:
    """Return whether an artifact belongs to the final IOC section."""
    try:
        return ArtifactType(value) in PUBLICATION_IOC_ARTIFACT_TYPES if value is not None else False
    except ValueError:
        return False


class RichSpanKind(StrEnum):
    TEXT = "text"
    EMPHASIS = "emphasis"
    ACTOR = "actor"
    MALWARE = "malware"
    TOOL = "tool"
    PRODUCT = "product"
    TECHNICAL = "technical"
    IOC = "ioc"
    CODE = "code"
    CITATION = "citation"


@dataclass(frozen=True)
class RichSpan:
    kind: RichSpanKind
    text: str
    source_ids: tuple[str, ...] = ()


type RichText = tuple[RichSpan, ...]


class PublicationEvidenceKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    INDICATOR = "indicator"
    RULE = "rule"


class PublicationAssemblyErrorCode(StrEnum):
    INPUTS_MISSING = "assembly_inputs_missing"
    INPUTS_MISMATCH = "assembly_inputs_mismatch"
    EVIDENCE_MISSING = "assembly_evidence_missing"
    SOURCE_MISSING = "assembly_source_missing"
    VALIDATION_FAILED = "assembly_validation_failed"
    DIAGRAM_ASSET_MISSING = "publication_diagram_asset_missing"
    SOURCE_FIGURE_UNRESOLVED = "publication_source_figure_unresolved"
    SOURCE_FIGURE_INVALID = "publication_source_figure_invalid"
    SOURCE_FIGURE_METADATA_MISSING = "publication_source_figure_metadata_missing"


class PublicationSectionKind(StrEnum):
    OVERVIEW = "overview"
    CAMPAIGN = "campaign"
    INFECTION_CHAIN = "infection_chain"
    TECHNICAL = "technical"
    VICTIMOLOGY = "victimology"
    INFRASTRUCTURE = "infrastructure"
    DETECTION = "detection"
    IMPACT = "impact"
    OTHER = "other"


_PUBLICATION_EVIDENCE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _normalize_publication_source_document_ids(
    source_document_ids: tuple[UUID, ...], label: str
) -> tuple[UUID, ...]:
    if not isinstance(source_document_ids, tuple) or not source_document_ids:
        raise ValueError(f"{label} requires a non-empty tuple of source document identities")
    if any(not isinstance(source_document_id, UUID) for source_document_id in source_document_ids):
        raise ValueError(f"{label} source document identities must be UUIDs")
    if len(set(source_document_ids)) != len(source_document_ids):
        raise ValueError(f"{label} must not repeat source document identities")
    return tuple(sorted(source_document_ids, key=str))


def _normalize_publication_evidence_refs(
    refs: tuple[PublicationEvidenceRefV1, ...], label: str
) -> tuple[PublicationEvidenceRefV1, ...]:
    if not isinstance(refs, tuple) or not refs:
        raise ValueError(f"{label} requires a tuple of evidence references")
    if any(not isinstance(ref, PublicationEvidenceRefV1) for ref in refs):
        raise ValueError(f"{label} evidence references have an invalid type")
    if len(set(refs)) != len(refs):
        raise ValueError(f"{label} must not repeat evidence references")
    return tuple(
        sorted(
            refs,
            key=lambda ref: (
                str(ref.source_document_id),
                ref.kind.value,
                ref.evidence_key,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class PublicationEvidenceRefV1:
    source_document_id: UUID
    kind: PublicationEvidenceKind
    evidence_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Evidence source document identity must be a UUID")
        if not isinstance(self.kind, PublicationEvidenceKind):
            raise ValueError("Evidence kind is invalid")
        if (
            not isinstance(self.evidence_key, str)
            or _PUBLICATION_EVIDENCE_SHA256.fullmatch(self.evidence_key) is None
        ):
            raise ValueError("Evidence key must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class PublicationParagraphV1:
    text: str
    evidence_refs: tuple[PublicationEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Publication paragraph text must be non-empty text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_publication_evidence_refs(self.evidence_refs, "Paragraph"),
        )


@dataclass(frozen=True, slots=True)
class PublicationSectionV1:
    kind: PublicationSectionKind
    heading: str
    paragraphs: tuple[PublicationParagraphV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PublicationSectionKind):
            raise ValueError("Publication section kind is invalid")
        if not isinstance(self.heading, str) or not self.heading.strip():
            raise ValueError("Publication section heading must be non-empty text")
        if not isinstance(self.paragraphs, tuple) or not self.paragraphs:
            raise ValueError("Publication section paragraphs must be a non-empty tuple")
        if any(not isinstance(paragraph, PublicationParagraphV1) for paragraph in self.paragraphs):
            raise ValueError("Publication section paragraphs have an invalid type")


@dataclass(frozen=True, slots=True)
class PublicationTimelineEntryV1:
    event_date: date | None
    date_text: str | None
    text: str
    evidence_refs: tuple[PublicationEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if self.event_date is not None and type(self.event_date) is not date:
            raise ValueError("Timeline event date must be a date or None")
        if self.date_text is not None and (
            not isinstance(self.date_text, str) or not self.date_text.strip()
        ):
            raise ValueError("Timeline date text must be non-empty text")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Timeline text must be non-empty text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_publication_evidence_refs(self.evidence_refs, "Timeline entry"),
        )


@dataclass(frozen=True, slots=True)
class PublicationIndicatorV1:
    value: str
    normalized_value: str
    artifact_type: ArtifactType
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Publication indicator value must be non-empty text")
        if not isinstance(self.normalized_value, str) or not self.normalized_value.strip():
            raise ValueError("Publication indicator normalized value must be non-empty text")
        if not isinstance(self.artifact_type, ArtifactType) or (
            self.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
        ):
            raise ValueError("Publication indicator artifact type is not publishable")
        object.__setattr__(
            self,
            "source_document_ids",
            _normalize_publication_source_document_ids(
                self.source_document_ids, "Publication indicator"
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicationIndicatorGroupV1:
    artifact_type: ArtifactType
    indicators: tuple[PublicationIndicatorV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_type, ArtifactType) or (
            self.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
        ):
            raise ValueError("Publication indicator group artifact type is not publishable")
        if not isinstance(self.indicators, tuple) or not self.indicators:
            raise ValueError("Publication indicator group requires a non-empty tuple")
        if any(not isinstance(indicator, PublicationIndicatorV1) for indicator in self.indicators):
            raise ValueError("Publication indicator group contains an invalid indicator")
        if any(indicator.artifact_type is not self.artifact_type for indicator in self.indicators):
            raise ValueError("Publication indicator artifact type must match its group")
        normalized_values = tuple(indicator.normalized_value for indicator in self.indicators)
        if len(set(normalized_values)) != len(normalized_values):
            raise ValueError("Publication indicator group must not repeat normalized values")
        object.__setattr__(
            self,
            "indicators",
            tuple(
                sorted(
                    self.indicators,
                    key=lambda indicator: (
                        indicator.normalized_value,
                        indicator.value,
                        tuple(map(str, indicator.source_document_ids)),
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicationSourceV1:
    source_document_id: UUID
    canonical_url: str
    title: str | None
    publisher: str | None
    published_at: date | None
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Publication source document identity must be a UUID")
        try:
            canonical = canonicalize_http_url(self.canonical_url)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Publication source URL must be a canonical HTTP(S) URL") from exc
        if canonical != self.canonical_url:
            raise ValueError("Publication source URL must be canonical")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("Publication source tier is invalid")
        if not isinstance(self.kind, ProductionReferenceKind):
            raise ValueError("Publication source kind is invalid")
        if not isinstance(self.role, SourceRole):
            raise ValueError("Publication source role is invalid")
        if self.title is not None and not isinstance(self.title, str):
            raise ValueError("Publication source title must be text or None")
        if self.publisher is not None and not isinstance(self.publisher, str):
            raise ValueError("Publication source publisher must be text or None")
        if self.published_at is not None and type(self.published_at) is not date:
            raise ValueError("Publication source date must be a date or None")


@dataclass(frozen=True, slots=True)
class PublicationUncertaintyV1:
    text: str
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Publication uncertainty text must be non-empty text")
        object.__setattr__(
            self,
            "source_document_ids",
            _normalize_publication_source_document_ids(
                self.source_document_ids, "Publication uncertainty"
            ),
        )


def _publication_json_object(value: Any, label: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be text")
    actual_fields = frozenset(value)
    if actual_fields != fields:
        missing = sorted(fields - actual_fields)
        unexpected = sorted(actual_fields - fields)
        raise ValueError(f"{label} fields are invalid (missing={missing}, unexpected={unexpected})")
    return value


def _publication_json_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _publication_json_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _publication_json_uuid(value: Any, label: str) -> UUID:
    text = _publication_json_string(value, label)
    try:
        parsed = UUID(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc
    if str(parsed) != text:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return parsed


def _publication_json_date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    text = _publication_json_string(value, label)
    try:
        parsed = date.fromisoformat(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date string or null") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use canonical ISO date form")
    return parsed


def _publication_json_evidence_refs(value: Any) -> tuple[PublicationEvidenceRefV1, ...]:
    fields = frozenset({"source_document_id", "kind", "evidence_key"})
    return tuple(
        PublicationEvidenceRefV1(
            source_document_id=_publication_json_uuid(
                item["source_document_id"], "Evidence source_document_id"
            ),
            kind=PublicationEvidenceKind(_publication_json_string(item["kind"], "Evidence kind")),
            evidence_key=_publication_json_string(item["evidence_key"], "Evidence key"),
        )
        for item in (
            _publication_json_object(raw, "Evidence reference", fields)
            for raw in _publication_json_array(value, "Evidence references")
        )
    )


def _publication_json_source_ids(value: Any, label: str) -> tuple[UUID, ...]:
    return tuple(
        _publication_json_uuid(item, label) for item in _publication_json_array(value, label)
    )


def _validate_publication_document_core(
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
    """Validate the canonical publication contract and source coverage."""
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


_PUBLICATION_ENRICHMENT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _ensure_publication_v4_dependencies() -> None:
    if "DiagramEdgeV1" in globals():
        return
    from cti_app.domain.production_editorial_enrichment import (
        DiagramEdgeV1 as _DiagramEdgeV1,
    )
    from cti_app.domain.production_editorial_enrichment import (
        DiagramGroupV1 as _DiagramGroupV1,
    )
    from cti_app.domain.production_editorial_enrichment import (
        DiagramNodeV1 as _DiagramNodeV1,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EnrichmentDiagramDirection as _EnrichmentDiagramDirection,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EnrichmentDiagramKind as _EnrichmentDiagramKind,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EnrichmentPlacementKind as _EnrichmentPlacementKind,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EnrichmentPlacementV1 as _EnrichmentPlacementV1,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EnrichmentTableKind as _EnrichmentTableKind,
    )
    from cti_app.domain.production_editorial_enrichment import (
        SourceFigureLocatorV1 as _SourceFigureLocatorV1,
    )
    from cti_app.domain.production_synthesis import EvidenceKind as _EvidenceKind
    from cti_app.domain.production_synthesis import (
        ExtractionEvidenceRefV1 as _ExtractionEvidenceRefV1,
    )
    from cti_app.domain.production_synthesis import (
        evidence_ref_sort_key as _evidence_ref_sort_key,
    )

    globals().update(
        {
            "DiagramEdgeV1": _DiagramEdgeV1,
            "DiagramGroupV1": _DiagramGroupV1,
            "DiagramNodeV1": _DiagramNodeV1,
            "EnrichmentDiagramDirection": _EnrichmentDiagramDirection,
            "EnrichmentDiagramKind": _EnrichmentDiagramKind,
            "EnrichmentPlacementKind": _EnrichmentPlacementKind,
            "EnrichmentPlacementV1": _EnrichmentPlacementV1,
            "EnrichmentTableKind": _EnrichmentTableKind,
            "SourceFigureLocatorV1": _SourceFigureLocatorV1,
            "EvidenceKind": _EvidenceKind,
            "ExtractionEvidenceRefV1": _ExtractionEvidenceRefV1,
            "evidence_ref_sort_key": _evidence_ref_sort_key,
        }
    )


def _publication_v4_key(value: Any, label: str) -> str:
    if not isinstance(value, str) or _PUBLICATION_ENRICHMENT_KEY.fullmatch(value) is None:
        raise ValueError(f"{label} must match the editorial key format")
    return value


def _publication_v4_text(value: Any, label: str, *, semantic: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if semantic and not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _normalize_publication_v4_evidence_refs(
    refs: tuple[ExtractionEvidenceRefV1, ...], label: str
) -> tuple[ExtractionEvidenceRefV1, ...]:
    _ensure_publication_v4_dependencies()
    if not isinstance(refs, tuple) or not refs:
        raise ValueError(f"{label} requires a tuple of evidence references")
    if any(not isinstance(ref, ExtractionEvidenceRefV1) for ref in refs):
        raise ValueError(f"{label} contains an invalid evidence reference")
    if len(refs) != len(set(refs)):
        raise ValueError(f"{label} must not repeat evidence references")
    return tuple(sorted(refs, key=evidence_ref_sort_key))


@dataclass(frozen=True, slots=True)
class PublicationTableColumnV1:
    key: str
    label: str

    def __post_init__(self) -> None:
        _publication_v4_text(self.key, "Publication table column key", semantic=True)
        _publication_v4_text(self.label, "Publication table column label", semantic=True)


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
            _normalize_publication_v4_evidence_refs(
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
        _ensure_publication_v4_dependencies()
        _publication_v4_key(self.key, "Publication table key")
        if not isinstance(self.kind, EnrichmentTableKind):
            raise ValueError("Publication table kind is invalid")
        _publication_v4_text(self.title, "Publication table title", semantic=True)
        if self.caption is not None:
            _publication_v4_text(self.caption, "Publication table caption")
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
        _ensure_publication_v4_dependencies()
        _publication_v4_key(self.key, "Publication diagram key")
        if not isinstance(self.kind, EnrichmentDiagramKind):
            raise ValueError("Publication diagram kind is invalid")
        _publication_v4_text(self.title, "Publication diagram title", semantic=True)
        if self.caption is not None:
            _publication_v4_text(self.caption, "Publication diagram caption")
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
        _ensure_publication_v4_dependencies()
        _publication_v4_key(self.key, "Publication source figure key")
        if not isinstance(self.asset_id, UUID):
            raise ValueError("Publication source figure asset identity must be a UUID")
        if not isinstance(self.sha256, str) or (
            _PUBLICATION_EVIDENCE_SHA256.fullmatch(self.sha256) is None
        ):
            raise ValueError("Publication source figure SHA-256 must be lowercase hexadecimal")
        if not isinstance(self.mime_type, str) or self.mime_type not in SUPPORTED_MEDIA_MIME_TYPES:
            raise ValueError("Publication source figure MIME type is not supported")
        if type(self.byte_size) is not int or self.byte_size <= 0:
            raise ValueError("Publication source figure byte size must be positive")
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Publication source figure document identity must be a UUID")
        _publication_v4_text(self.source_url, "Publication source figure URL", semantic=True)
        _publication_v4_text(self.caption, "Publication source figure caption", semantic=True)
        _publication_v4_text(self.provenance, "Publication source figure provenance", semantic=True)
        if not isinstance(self.locator, SourceFigureLocatorV1):
            raise ValueError("Publication source figure locator is invalid")
        if not isinstance(self.placement, EnrichmentPlacementV1):
            raise ValueError("Publication source figure placement is invalid")


def _publication_v4_object(value: Any, label: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be text")
    actual_fields = frozenset(value)
    if actual_fields != fields:
        missing = sorted(fields - actual_fields)
        unexpected = sorted(actual_fields - fields)
        raise ValueError(f"{label} fields are invalid (missing={missing}, unexpected={unexpected})")
    return value


def _publication_v4_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _publication_v4_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _publication_v4_uuid(value: Any, label: str) -> UUID:
    text = _publication_v4_string(value, label)
    try:
        parsed = UUID(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc
    if str(parsed) != text:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return parsed


def _publication_v4_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _publication_v4_enum(enum_type: type[Any], value: Any, label: str) -> Any:
    try:
        return enum_type(_publication_v4_string(value, label))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc


def _publication_v4_evidence_refs(value: Any) -> tuple[ExtractionEvidenceRefV1, ...]:
    _ensure_publication_v4_dependencies()
    fields = frozenset({"source_document_id", "kind", "evidence_key"})
    return tuple(
        ExtractionEvidenceRefV1(
            source_document_id=_publication_v4_uuid(
                item["source_document_id"], "Evidence source_document_id"
            ),
            kind=_publication_v4_enum(EvidenceKind, item["kind"], "Evidence kind"),
            evidence_key=_publication_v4_string(item["evidence_key"], "Evidence key"),
        )
        for item in (
            _publication_v4_object(raw, "Evidence reference", fields)
            for raw in _publication_v4_array(value, "Evidence references")
        )
    )


def _publication_v4_placement_to_json(placement: EnrichmentPlacementV1) -> dict[str, Any]:
    _ensure_publication_v4_dependencies()
    return {"kind": placement.kind.value, "section_index": placement.section_index}


def _publication_v4_placement_from_json(value: Any) -> EnrichmentPlacementV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value, "Publication placement", frozenset({"kind", "section_index"})
    )
    return EnrichmentPlacementV1(
        kind=_publication_v4_enum(EnrichmentPlacementKind, item["kind"], "Placement kind"),
        section_index=(
            None
            if item["section_index"] is None
            else _publication_v4_int(item["section_index"], "Placement section_index")
        ),
    )


def _publication_v4_locator_to_json(locator: SourceFigureLocatorV1) -> dict[str, Any]:
    _ensure_publication_v4_dependencies()
    return {
        "page": locator.page,
        "section": locator.section,
        "figure_label": locator.figure_label,
        "original_asset_url": locator.original_asset_url,
    }


def _publication_v4_locator_from_json(value: Any) -> SourceFigureLocatorV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value,
        "Publication source figure locator",
        frozenset({"page", "section", "figure_label", "original_asset_url"}),
    )
    return SourceFigureLocatorV1(
        page=(None if item["page"] is None else _publication_v4_int(item["page"], "Figure page")),
        section=(
            None
            if item["section"] is None
            else _publication_v4_string(item["section"], "Figure section")
        ),
        figure_label=(
            None
            if item["figure_label"] is None
            else _publication_v4_string(item["figure_label"], "Figure label")
        ),
        original_asset_url=(
            None
            if item["original_asset_url"] is None
            else _publication_v4_string(item["original_asset_url"], "Figure original asset URL")
        ),
    )


def _publication_v4_table_to_json(table: PublicationTableV1) -> dict[str, Any]:
    return {
        "key": table.key,
        "kind": table.kind.value,
        "title": table.title,
        "caption": table.caption,
        "columns": [{"key": column.key, "label": column.label} for column in table.columns],
        "rows": [
            {
                "cells": list(row.cells),
                "evidence_refs": [
                    {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    }
                    for ref in row.evidence_refs
                ],
            }
            for row in table.rows
        ],
        "placement": _publication_v4_placement_to_json(table.placement),
    }


def _publication_v4_table_from_json(value: Any) -> PublicationTableV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value,
        "Publication table",
        frozenset({"key", "kind", "title", "caption", "columns", "rows", "placement"}),
    )
    columns = tuple(
        PublicationTableColumnV1(
            key=_publication_v4_string(column["key"], "Table column key"),
            label=_publication_v4_string(column["label"], "Table column label"),
        )
        for column in (
            _publication_v4_object(raw, "Publication table column", frozenset({"key", "label"}))
            for raw in _publication_v4_array(item["columns"], "Table columns")
        )
    )
    rows = tuple(
        PublicationTableRowV1(
            cells=tuple(
                _publication_v4_string(cell, "Table cell")
                for cell in _publication_v4_array(row["cells"], "Table row cells")
            ),
            evidence_refs=_publication_v4_evidence_refs(row["evidence_refs"]),
        )
        for row in (
            _publication_v4_object(
                raw, "Publication table row", frozenset({"cells", "evidence_refs"})
            )
            for raw in _publication_v4_array(item["rows"], "Table rows")
        )
    )
    return PublicationTableV1(
        key=_publication_v4_string(item["key"], "Table key"),
        kind=_publication_v4_enum(EnrichmentTableKind, item["kind"], "Table kind"),
        title=_publication_v4_string(item["title"], "Table title"),
        caption=(
            None
            if item["caption"] is None
            else _publication_v4_string(item["caption"], "Table caption")
        ),
        columns=columns,
        rows=rows,
        placement=_publication_v4_placement_from_json(item["placement"]),
    )


def _publication_v4_node_to_json(node: DiagramNodeV1) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "label": node.label,
        "evidence_refs": [
            {
                "source_document_id": str(ref.source_document_id),
                "kind": ref.kind.value,
                "evidence_key": ref.evidence_key,
            }
            for ref in node.evidence_refs
        ],
    }


def _publication_v4_edge_to_json(edge: DiagramEdgeV1) -> dict[str, Any]:
    return {
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "label": edge.label,
        "evidence_refs": [
            {
                "source_document_id": str(ref.source_document_id),
                "kind": ref.kind.value,
                "evidence_key": ref.evidence_key,
            }
            for ref in edge.evidence_refs
        ],
    }


def _publication_v4_diagram_to_json(diagram: PublicationDiagramV1) -> dict[str, Any]:
    return {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [_publication_v4_node_to_json(node) for node in diagram.nodes],
        "edges": [_publication_v4_edge_to_json(edge) for edge in diagram.edges],
        "groups": [
            {"group_id": group.group_id, "label": group.label, "node_ids": list(group.node_ids)}
            for group in diagram.groups
        ],
        "placement": _publication_v4_placement_to_json(diagram.placement),
        "asset_id": str(diagram.asset_id),
    }


def _publication_v4_node_from_json(value: Any) -> DiagramNodeV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value, "Publication diagram node", frozenset({"node_id", "label", "evidence_refs"})
    )
    return DiagramNodeV1(
        node_id=_publication_v4_string(item["node_id"], "Diagram node ID"),
        label=_publication_v4_string(item["label"], "Diagram node label"),
        evidence_refs=_publication_v4_evidence_refs(item["evidence_refs"]),
    )


def _publication_v4_edge_from_json(value: Any) -> DiagramEdgeV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value,
        "Publication diagram edge",
        frozenset({"source_node_id", "target_node_id", "label", "evidence_refs"}),
    )
    return DiagramEdgeV1(
        source_node_id=_publication_v4_string(
            item["source_node_id"], "Diagram edge source node ID"
        ),
        target_node_id=_publication_v4_string(
            item["target_node_id"], "Diagram edge target node ID"
        ),
        label=(
            None
            if item["label"] is None
            else _publication_v4_string(item["label"], "Diagram edge label")
        ),
        evidence_refs=_publication_v4_evidence_refs(item["evidence_refs"]),
    )


def _publication_v4_group_from_json(value: Any) -> DiagramGroupV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value, "Publication diagram group", frozenset({"group_id", "label", "node_ids"})
    )
    return DiagramGroupV1(
        group_id=_publication_v4_string(item["group_id"], "Diagram group ID"),
        label=_publication_v4_string(item["label"], "Diagram group label"),
        node_ids=tuple(
            _publication_v4_string(node_id, "Diagram group node ID")
            for node_id in _publication_v4_array(item["node_ids"], "Diagram group node IDs")
        ),
    )


def _publication_v4_diagram_from_json(value: Any) -> PublicationDiagramV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value,
        "Publication diagram",
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
    )
    return PublicationDiagramV1(
        key=_publication_v4_string(item["key"], "Diagram key"),
        kind=_publication_v4_enum(EnrichmentDiagramKind, item["kind"], "Diagram kind"),
        title=_publication_v4_string(item["title"], "Diagram title"),
        caption=(
            None
            if item["caption"] is None
            else _publication_v4_string(item["caption"], "Diagram caption")
        ),
        direction=_publication_v4_enum(
            EnrichmentDiagramDirection, item["direction"], "Diagram direction"
        ),
        nodes=tuple(
            _publication_v4_node_from_json(node)
            for node in _publication_v4_array(item["nodes"], "Diagram nodes")
        ),
        edges=tuple(
            _publication_v4_edge_from_json(edge)
            for edge in _publication_v4_array(item["edges"], "Diagram edges")
        ),
        groups=tuple(
            _publication_v4_group_from_json(group)
            for group in _publication_v4_array(item["groups"], "Diagram groups")
        ),
        placement=_publication_v4_placement_from_json(item["placement"]),
        asset_id=_publication_v4_uuid(item["asset_id"], "Diagram asset ID"),
    )


def _publication_v4_figure_to_json(figure: PublicationSourceFigureV1) -> dict[str, Any]:
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
        "locator": _publication_v4_locator_to_json(figure.locator),
        "placement": _publication_v4_placement_to_json(figure.placement),
    }


def _publication_v4_figure_from_json(value: Any) -> PublicationSourceFigureV1:
    _ensure_publication_v4_dependencies()
    item = _publication_v4_object(
        value,
        "Publication source figure",
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
    )
    return PublicationSourceFigureV1(
        key=_publication_v4_string(item["key"], "Source figure key"),
        asset_id=_publication_v4_uuid(item["asset_id"], "Source figure asset ID"),
        sha256=_publication_v4_string(item["sha256"], "Source figure SHA-256"),
        mime_type=_publication_v4_string(item["mime_type"], "Source figure MIME type"),
        byte_size=_publication_v4_int(item["byte_size"], "Source figure byte size"),
        source_document_id=_publication_v4_uuid(
            item["source_document_id"], "Source figure source document ID"
        ),
        source_url=_publication_v4_string(item["source_url"], "Source figure URL"),
        caption=_publication_v4_string(item["caption"], "Source figure caption"),
        provenance=_publication_v4_string(item["provenance"], "Source figure provenance"),
        locator=_publication_v4_locator_from_json(item["locator"]),
        placement=_publication_v4_placement_from_json(item["placement"]),
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
        _validate_publication_document_core(
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
                    key=lambda item: (
                        item.text,
                        tuple(map(str, item.source_document_ids)),
                    ),
                )
            ),
        )

    def _to_json(self) -> dict[str, Any]:
        def evidence_refs_json(refs: tuple[PublicationEvidenceRefV1, ...]) -> list[dict[str, str]]:
            return [
                {
                    "source_document_id": str(ref.source_document_id),
                    "kind": ref.kind.value,
                    "evidence_key": ref.evidence_key,
                }
                for ref in refs
            ]

        def paragraph_json(paragraph: PublicationParagraphV1) -> dict[str, Any]:
            return {
                "text": paragraph.text,
                "evidence_refs": evidence_refs_json(paragraph.evidence_refs),
            }

        return {
            "schema_version": self.schema_version,
            "subject_id": str(self.subject_id),
            "publication_language": self.publication_language,
            "title": self.title,
            "lead": [paragraph_json(paragraph) for paragraph in self.lead],
            "sections": [
                {
                    "kind": section.kind.value,
                    "heading": section.heading,
                    "paragraphs": [paragraph_json(paragraph) for paragraph in section.paragraphs],
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
                    "evidence_refs": evidence_refs_json(entry.evidence_refs),
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
            "tables": [_publication_v4_table_to_json(table) for table in self.tables],
            "diagrams": [_publication_v4_diagram_to_json(diagram) for diagram in self.diagrams],
            "figures": [_publication_v4_figure_to_json(figure) for figure in self.figures],
        }

    @classmethod
    def _from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV4:
        document_fields = frozenset(
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

        def paragraph(raw: Any) -> PublicationParagraphV1:
            item = _publication_json_object(
                raw, "Publication paragraph", frozenset({"text", "evidence_refs"})
            )
            return PublicationParagraphV1(
                text=_publication_json_string(item["text"], "Paragraph text"),
                evidence_refs=_publication_json_evidence_refs(item["evidence_refs"]),
            )

        document = _publication_v4_object(payload, "PublicationDocumentV4", document_fields)
        sections = tuple(
            PublicationSectionV1(
                kind=PublicationSectionKind(_publication_json_string(item["kind"], "Section kind")),
                heading=_publication_json_string(item["heading"], "Section heading"),
                paragraphs=tuple(
                    paragraph(nested)
                    for nested in _publication_json_array(item["paragraphs"], "Section paragraphs")
                ),
            )
            for item in (
                _publication_json_object(
                    raw, "Publication section", frozenset({"kind", "heading", "paragraphs"})
                )
                for raw in _publication_json_array(document["sections"], "Sections")
            )
        )
        timeline = tuple(
            PublicationTimelineEntryV1(
                event_date=_publication_json_date(item["event_date"], "Timeline event_date"),
                date_text=(
                    None
                    if item["date_text"] is None
                    else _publication_json_string(item["date_text"], "Timeline date_text")
                ),
                text=_publication_json_string(item["text"], "Timeline text"),
                evidence_refs=_publication_json_evidence_refs(item["evidence_refs"]),
            )
            for item in (
                _publication_json_object(
                    raw,
                    "Publication timeline entry",
                    frozenset({"event_date", "date_text", "text", "evidence_refs"}),
                )
                for raw in _publication_json_array(document["timeline"], "Timeline")
            )
        )
        indicators = tuple(
            PublicationIndicatorGroupV1(
                artifact_type=ArtifactType(
                    _publication_json_string(item["artifact_type"], "Indicator group type")
                ),
                indicators=tuple(
                    PublicationIndicatorV1(
                        value=_publication_json_string(nested["value"], "Indicator value"),
                        normalized_value=_publication_json_string(
                            nested["normalized_value"], "Indicator normalized_value"
                        ),
                        artifact_type=ArtifactType(
                            _publication_json_string(
                                nested["artifact_type"], "Indicator artifact_type"
                            )
                        ),
                        source_document_ids=_publication_json_source_ids(
                            nested["source_document_ids"], "Indicator source_document_ids"
                        ),
                    )
                    for nested in (
                        _publication_json_object(
                            raw_indicator,
                            "Publication indicator",
                            frozenset(
                                {
                                    "value",
                                    "normalized_value",
                                    "artifact_type",
                                    "source_document_ids",
                                }
                            ),
                        )
                        for raw_indicator in _publication_json_array(
                            item["indicators"], "Indicators in group"
                        )
                    )
                ),
            )
            for item in (
                _publication_json_object(
                    raw,
                    "Publication indicator group",
                    frozenset({"artifact_type", "indicators"}),
                )
                for raw in _publication_json_array(document["indicators"], "Indicator groups")
            )
        )
        sources = tuple(
            PublicationSourceV1(
                source_document_id=_publication_json_uuid(
                    item["source_document_id"], "Source source_document_id"
                ),
                canonical_url=_publication_json_string(
                    item["canonical_url"], "Source canonical_url"
                ),
                title=(
                    None
                    if item["title"] is None
                    else _publication_json_string(item["title"], "Source title")
                ),
                publisher=(
                    None
                    if item["publisher"] is None
                    else _publication_json_string(item["publisher"], "Source publisher")
                ),
                published_at=_publication_json_date(item["published_at"], "Source published_at"),
                tier=ProductionReferenceTier(_publication_json_string(item["tier"], "Source tier")),
                kind=ProductionReferenceKind(_publication_json_string(item["kind"], "Source kind")),
                role=SourceRole(_publication_json_string(item["role"], "Source role")),
            )
            for item in (
                _publication_json_object(
                    raw,
                    "Publication source",
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
                )
                for raw in _publication_json_array(document["sources"], "Sources")
            )
        )
        uncertainties = tuple(
            PublicationUncertaintyV1(
                text=_publication_json_string(item["text"], "Uncertainty text"),
                source_document_ids=_publication_json_source_ids(
                    item["source_document_ids"], "Uncertainty source_document_ids"
                ),
            )
            for item in (
                _publication_json_object(
                    raw,
                    "Publication uncertainty",
                    frozenset({"text", "source_document_ids"}),
                )
                for raw in _publication_json_array(document["uncertainties"], "Uncertainties")
            )
        )
        return cls(
            schema_version=_publication_v4_string(
                document["schema_version"], "Publication schema_version"
            ),
            subject_id=_publication_v4_uuid(document["subject_id"], "Publication subject_id"),
            publication_language=_publication_v4_string(
                document["publication_language"], "Publication language"
            ),
            title=_publication_v4_string(document["title"], "Publication title"),
            lead=tuple(paragraph(raw) for raw in _publication_json_array(document["lead"], "Lead")),
            sections=sections,
            timeline=timeline,
            indicators=indicators,
            sources=sources,
            uncertainties=uncertainties,
            tables=tuple(
                _publication_v4_table_from_json(raw)
                for raw in _publication_v4_array(document["tables"], "Tables")
            ),
            diagrams=tuple(
                _publication_v4_diagram_from_json(raw)
                for raw in _publication_v4_array(document["diagrams"], "Diagrams")
            ),
            figures=tuple(
                _publication_v4_figure_from_json(raw)
                for raw in _publication_v4_array(document["figures"], "Figures")
            ),
        )


def publication_document_v4_to_json(document: PublicationDocumentV4) -> dict[str, Any]:
    if not isinstance(document, PublicationDocumentV4):
        raise ValueError("Expected a PublicationDocumentV4")
    return document._to_json()


def publication_document_v4_from_json(payload: Mapping[str, Any]) -> PublicationDocumentV4:
    return PublicationDocumentV4._from_json(payload)
