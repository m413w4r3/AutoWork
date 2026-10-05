"""Canonical, renderer-independent Editorial Enrichment contract."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator, model_validator

from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
)
from cti_app.domain.semantic_annotation import (
    SemanticAnnotationProposalV1,
    semantic_annotation_proposal_from_json,
    semantic_annotation_proposal_to_json,
)

EDITORIAL_ENRICHMENT_SCHEMA_VERSION = 6
EDITORIAL_ENRICHMENT_V1_POLICY_VERSION = "editorial-enrichment-v1"
EDITORIAL_ENRICHMENT_V2_POLICY_VERSION = "editorial-enrichment-v2-semantic-annotations"
EDITORIAL_ENRICHMENT_V3_POLICY_VERSION = "editorial-enrichment-v3-figures-resource-proposals"
EDITORIAL_ENRICHMENT_V4_POLICY_VERSION = "editorial-enrichment-v4-analytic-purpose"
EDITORIAL_ENRICHMENT_V5_POLICY_VERSION = "editorial-enrichment-v5-diagram-node-roles"
EDITORIAL_ENRICHMENT_POLICY_VERSION = "editorial-enrichment-v6-source-figure-provenance"
EDITORIAL_FIGURE_DECISION_POLICY_VERSION = "editorial-figure-selection-v3-source-context"
EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION = "editorial-resource-proposal-v1"


class EditorialEnrichmentRevisionAction(StrEnum):
    IMPROVE_TABLE = "improve_table"
    DETAIL_DIAGRAM = "detail_diagram"
    CHANGE_CAPTION_PLACEMENT = "change_caption_placement"
    CHOOSE_ANOTHER_FIGURE = "choose_another_figure"
    CUSTOM = "custom"


class EditorialEnrichmentElementKind(StrEnum):
    TABLE = "table"
    DIAGRAM = "diagram"
    FIGURE = "figure"


class EditorialEnrichmentRevisionOutcome(StrEnum):
    REVISED = "revised"
    NEEDS_NEW_EVIDENCE = "needs_new_evidence"


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


def normalize_analytic_question(value: str) -> str:
    """Return a stable, punctuation-insensitive key for analytic questions."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(char if char.isalnum() or char == "_" else " " for char in normalized)
    return " ".join(normalized.split())


@dataclass(frozen=True, slots=True)
class EditorialAnalyticPurposeV1:
    question: str
    available_data: str
    comprehension_gain: str
    scope: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]
    knowledge_limits: str
    placement_reason: str

    def __post_init__(self) -> None:
        for name in (
            "question",
            "available_data",
            "comprehension_gain",
            "scope",
            "knowledge_limits",
            "placement_reason",
        ):
            value = _text(getattr(self, name), f"Analytic purpose {name}", semantic=True)
            if len(value.strip()) > 1000:
                raise ValueError(f"Analytic purpose {name} must be at most 1000 characters")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_evidence_refs(self.evidence_refs, "Analytic purpose evidence references"),
        )


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
    purpose: EditorialAnalyticPurposeV1 | None = None

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
        if self.purpose is not None and not isinstance(self.purpose, EditorialAnalyticPurposeV1):
            raise ValueError("Table analytic purpose is invalid")


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


# A wide strip of boxes is scaled down to the page width and its text becomes unreadable:
# beyond these budgets a diagram is laid out top to bottom whatever direction was asked.
DIAGRAM_VERTICAL_AFTER_NODES = 3
DIAGRAM_VERTICAL_AFTER_LABEL_CHARACTERS = 30


def diagram_requires_vertical_layout(node_count: int, labels: Iterable[str]) -> bool:
    """True when node count or label length makes a horizontal layout unreadable in print."""
    return node_count > DIAGRAM_VERTICAL_AFTER_NODES or any(
        len(label) > DIAGRAM_VERTICAL_AFTER_LABEL_CHARACTERS for label in labels
    )


class DiagramRelationType(StrEnum):
    FACTUAL = "factual"
    INFERENCE = "inference"
    COMPARISON = "comparison"


class DiagramNodeRole(StrEnum):
    ACTOR = "actor"
    VICTIM = "victim"
    MALWARE_TOOL = "malware_tool"
    INFRASTRUCTURE = "infrastructure"
    DATA_ARTIFACT = "data_artifact"
    TECHNIQUE_STEP = "technique_step"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DiagramNodeV1:
    node_id: str
    label: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]
    role: DiagramNodeRole = DiagramNodeRole.UNKNOWN

    def __post_init__(self) -> None:
        _text(self.node_id, "Diagram node ID", semantic=True)
        _diagram_label(self.label, "Diagram node label")
        if not isinstance(self.role, DiagramNodeRole):
            raise ValueError("Diagram node role is invalid")
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
    relation_type: DiagramRelationType = DiagramRelationType.FACTUAL

    def __post_init__(self) -> None:
        _text(self.source_node_id, "Diagram edge source node ID", semantic=True)
        _text(self.target_node_id, "Diagram edge target node ID", semantic=True)
        if self.label is not None:
            _diagram_label(self.label, "Diagram edge label", semantic=False)
        if not isinstance(self.relation_type, DiagramRelationType):
            raise ValueError("Diagram edge relation type is invalid")
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
    purpose: EditorialAnalyticPurposeV1 | None = None

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
        if self.purpose is not None and not isinstance(self.purpose, EditorialAnalyticPurposeV1):
            raise ValueError("Diagram analytic purpose is invalid")


class SourceFigureInclusionStatus(StrEnum):
    PROPOSED = "proposed"
    INCLUDED = "included"
    EXCLUDED = "excluded"


class EditorialFigureDecision(StrEnum):
    INCLUDED_BY_MODEL = "included_by_model"
    NOT_SELECTED_BY_MODEL = "not_selected_by_model"
    EXCLUDED_BY_RULE = "excluded_by_rule"
    PENDING_ARCHIVE = "pending_archive"


class EditorialFigureDecisionActor(StrEnum):
    MODEL_PROPOSAL = "model_proposal"
    DETERMINISTIC_RULE = "deterministic_rule"


@dataclass(frozen=True, slots=True)
class EditorialFigureDecisionTraceV1:
    handle: str
    figure_id: UUID
    source_document_id: UUID
    decision: EditorialFigureDecision
    actor: EditorialFigureDecisionActor
    reason_code: str
    reason: str
    policy_version: str
    prompt_version: str
    contract_version: str
    parser_version: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"F[0-9]{3,}", self.handle) is None:
            raise ValueError("Editorial figure handle is invalid")
        if not isinstance(self.figure_id, UUID):
            raise ValueError("Editorial figure decision identity is invalid")
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Editorial figure source identity is invalid")
        if not isinstance(self.decision, EditorialFigureDecision) or not isinstance(
            self.actor, EditorialFigureDecisionActor
        ):
            raise ValueError("Editorial figure decision type is invalid")
        for name in (
            "reason_code",
            "reason",
            "policy_version",
            "prompt_version",
            "contract_version",
            "parser_version",
        ):
            _text(getattr(self, name), f"Editorial figure decision {name}", semantic=True)
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(ref, ExtractionEvidenceRefV1) for ref in self.evidence_refs
        ):
            raise ValueError("Editorial figure decision evidence references are invalid")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("Editorial figure decision evidence references must be unique")


class ResourceNeedKind(StrEnum):
    MEDIA = "MEDIA"
    TECHNICAL_ANALYSIS = "TECHNICAL_ANALYSIS"


@dataclass(frozen=True, slots=True)
class EditorialResourceNeedV1:
    key: str
    kind: ResourceNeedKind
    reason: str
    query_hint: str
    policy_version: str = EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION

    def __post_init__(self) -> None:
        if re.fullmatch(r"N[0-9]{3,}", self.key) is None:
            raise ValueError("Editorial resource need key is invalid")
        if not isinstance(self.kind, ResourceNeedKind):
            raise ValueError("Editorial resource need kind is invalid")
        if not (1 <= len(self.reason.strip()) <= 500):
            raise ValueError("Editorial resource need reason must be bounded text")
        if not (1 <= len(self.query_hint.strip()) <= 240):
            raise ValueError("Editorial resource query hint must be bounded text")
        _text(self.policy_version, "Editorial resource policy version", semantic=True)


@dataclass(frozen=True, slots=True)
class EditorialResourceProposalV1:
    need_key: str
    url: str
    justification: str
    source_model_run_id: UUID
    policy_version: str = EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION

    def __post_init__(self) -> None:
        if re.fullmatch(r"N[0-9]{3,}", self.need_key) is None:
            raise ValueError("Editorial resource proposal need key is invalid")
        if not (1 <= len(self.url.strip()) <= 2048):
            raise ValueError("Editorial resource proposal URL must be bounded text")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Editorial resource proposal URL must be an HTTP(S) URL")
        if not (1 <= len(self.justification.strip()) <= 500):
            raise ValueError("Editorial resource proposal justification must be bounded text")
        if not isinstance(self.source_model_run_id, UUID):
            raise ValueError("Editorial resource proposal model run identity is invalid")
        _text(self.policy_version, "Editorial resource policy version", semantic=True)


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
    annotations: tuple[SemanticAnnotationProposalV1, ...] = ()
    figure_decisions: tuple[EditorialFigureDecisionTraceV1, ...] = ()
    resource_needs: tuple[EditorialResourceNeedV1, ...] = ()
    resource_proposals: tuple[EditorialResourceProposalV1, ...] = ()

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2, 3, 4, 5, 6}:
            raise ValueError("Editorial enrichment schema version is unsupported")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Editorial enrichment subject identity must be a UUID")
        for field_name in ("production_input_hash", "extraction_hash", "synthesis_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"Editorial enrichment {field_name} must be a lowercase SHA-256")
        _text(self.publication_language, "Publication language", semantic=True)
        expected_policy = (
            EDITORIAL_ENRICHMENT_V1_POLICY_VERSION
            if self.schema_version == 1
            else EDITORIAL_ENRICHMENT_V2_POLICY_VERSION
            if self.schema_version == 2
            else EDITORIAL_ENRICHMENT_V3_POLICY_VERSION
            if self.schema_version == 3
            else EDITORIAL_ENRICHMENT_V4_POLICY_VERSION
            if self.schema_version == 4
            else EDITORIAL_ENRICHMENT_V5_POLICY_VERSION
            if self.schema_version == 5
            else EDITORIAL_ENRICHMENT_POLICY_VERSION
        )
        if self.enrichment_policy_version != expected_policy:
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
        if not isinstance(self.annotations, tuple) or any(
            not isinstance(annotation, SemanticAnnotationProposalV1)
            for annotation in self.annotations
        ):
            raise ValueError("Editorial enrichment annotations have an invalid type")
        if self.schema_version == 1 and self.annotations:
            raise ValueError("V1 editorial enrichment cannot contain semantic annotations")
        if self.schema_version < 3 and (
            self.figure_decisions or self.resource_needs or self.resource_proposals
        ):
            raise ValueError("Legacy editorial enrichment cannot contain L7b review data")
        item_checks: tuple[tuple[str, tuple[object, ...], type[object]], ...] = (
            ("figure decisions", self.figure_decisions, EditorialFigureDecisionTraceV1),
            ("resource needs", self.resource_needs, EditorialResourceNeedV1),
            ("resource proposals", self.resource_proposals, EditorialResourceProposalV1),
        )
        for check_label, check_values, check_item_type in item_checks:
            if not isinstance(check_values, tuple) or any(
                not isinstance(item, check_item_type) for item in check_values
            ):
                raise ValueError(f"Editorial enrichment {check_label} have an invalid type")
        decision_handles = [item.handle for item in self.figure_decisions]
        decision_ids = [item.figure_id for item in self.figure_decisions]
        if len(decision_handles) != len(set(decision_handles)) or len(decision_ids) != len(
            set(decision_ids)
        ):
            raise ValueError("Editorial figure decisions must have unique handles and identities")
        need_keys = [item.key for item in self.resource_needs]
        if len(need_keys) != len(set(need_keys)):
            raise ValueError("Editorial resource needs must have unique keys")
        if any(item.need_key not in set(need_keys) for item in self.resource_proposals):
            raise ValueError("Editorial resource proposal references an unknown need")
        keys = [
            *(table.key for table in self.tables),
            *(diagram.key for diagram in self.diagrams),
            *(figure.key for figure in self.source_figures),
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Editorial enrichment keys must be globally unique")
        if self.schema_version >= 4:
            purposes = [table.purpose for table in self.tables] + [
                diagram.purpose for diagram in self.diagrams
            ]
            typed_purposes = tuple(purpose for purpose in purposes if purpose is not None)
            if len(typed_purposes) != len(purposes):
                raise ValueError("V4 tables and diagrams require analytic purposes")
            questions = [
                normalize_analytic_question(purpose.question) for purpose in typed_purposes
            ]
            if any(not question for question in questions) or len(questions) != len(set(questions)):
                raise ValueError("Editorial enrichment analytic questions must be distinct")
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))


def editorial_enrichment_evidence_refs(
    enrichment: EditorialEnrichmentV1,
) -> frozenset[ExtractionEvidenceRefV1]:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    refs = {ref for table in enrichment.tables for row in table.rows for ref in row.evidence_refs}
    refs.update(
        ref
        for table in enrichment.tables
        if table.purpose is not None
        for ref in table.purpose.evidence_refs
    )
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
    refs.update(
        ref
        for diagram in enrichment.diagrams
        if diagram.purpose is not None
        for ref in diagram.purpose.evidence_refs
    )
    refs.update(ref for decision in enrichment.figure_decisions for ref in decision.evidence_refs)
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


def _purpose_to_json(purpose: EditorialAnalyticPurposeV1) -> dict[str, Any]:
    return {
        "question": purpose.question,
        "available_data": purpose.available_data,
        "comprehension_gain": purpose.comprehension_gain,
        "scope": purpose.scope,
        "evidence_refs": [_ref_to_json(ref) for ref in purpose.evidence_refs],
        "knowledge_limits": purpose.knowledge_limits,
        "placement_reason": purpose.placement_reason,
    }


def _table_to_json(table: TableSpecV1, *, include_analytic: bool = False) -> dict[str, Any]:
    payload = {
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
    if include_analytic:
        if table.purpose is None:
            raise ValueError("V4 editorial table is missing its analytic purpose")
        payload["purpose"] = _purpose_to_json(table.purpose)
    return payload


def _diagram_to_json(
    diagram: DiagramSpecV1, *, include_analytic: bool = False, include_roles: bool = True
) -> dict[str, Any]:
    payload = {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [diagram_node_to_json(node, include_role=include_roles) for node in diagram.nodes],
        "edges": [
            diagram_edge_to_json(edge, include_relation_type=include_analytic)
            for edge in diagram.edges
        ],
        "groups": [diagram_group_to_json(group) for group in diagram.groups],
        "placement": _placement_to_json(diagram.placement),
    }
    if diagram.compiled_asset_id is not None:
        payload["compiled_asset_id"] = str(diagram.compiled_asset_id)
    if include_analytic:
        if diagram.purpose is None:
            raise ValueError("V4 editorial diagram is missing its analytic purpose")
        payload["purpose"] = _purpose_to_json(diagram.purpose)
    return payload


def diagram_node_to_json(node: DiagramNodeV1, *, include_role: bool = True) -> dict[str, Any]:
    payload = {
        "node_id": node.node_id,
        "label": node.label,
        "evidence_refs": [evidence_ref_to_json(ref) for ref in node.evidence_refs],
    }
    if include_role:
        payload["role"] = node.role.value
    return payload


def diagram_edge_to_json(
    edge: DiagramEdgeV1, *, include_relation_type: bool = False
) -> dict[str, Any]:
    payload = {
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "label": edge.label,
        "evidence_refs": [evidence_ref_to_json(ref) for ref in edge.evidence_refs],
    }
    if include_relation_type:
        payload["relation_type"] = edge.relation_type.value
    return payload


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


def _figure_decision_to_json(item: EditorialFigureDecisionTraceV1) -> dict[str, Any]:
    return {
        "handle": item.handle,
        "figure_id": str(item.figure_id),
        "source_document_id": str(item.source_document_id),
        "decision": item.decision.value,
        "actor": item.actor.value,
        "reason_code": item.reason_code,
        "reason": item.reason,
        "policy_version": item.policy_version,
        "prompt_version": item.prompt_version,
        "contract_version": item.contract_version,
        "parser_version": item.parser_version,
        "evidence_refs": [_ref_to_json(ref) for ref in item.evidence_refs],
    }


def _resource_need_to_json(item: EditorialResourceNeedV1) -> dict[str, Any]:
    return {
        "key": item.key,
        "kind": item.kind.value,
        "reason": item.reason,
        "query_hint": item.query_hint,
        "policy_version": item.policy_version,
    }


def _resource_proposal_to_json(item: EditorialResourceProposalV1) -> dict[str, Any]:
    return {
        "need_key": item.need_key,
        "url": item.url,
        "justification": item.justification,
        "source_model_run_id": str(item.source_model_run_id),
        "policy_version": item.policy_version,
    }


def editorial_enrichment_to_json(enrichment: EditorialEnrichmentV1) -> dict[str, Any]:
    """Return the strict JSON-compatible canonical representation."""
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    payload = {
        "schema_version": enrichment.schema_version,
        "subject_id": str(enrichment.subject_id),
        "production_input_hash": enrichment.production_input_hash,
        "extraction_hash": enrichment.extraction_hash,
        "synthesis_hash": enrichment.synthesis_hash,
        "publication_language": enrichment.publication_language,
        "enrichment_policy_version": enrichment.enrichment_policy_version,
        "tables": [
            _table_to_json(table, include_analytic=enrichment.schema_version >= 4)
            for table in enrichment.tables
        ],
        "diagrams": [
            _diagram_to_json(
                diagram,
                include_analytic=enrichment.schema_version >= 4,
                include_roles=enrichment.schema_version >= 5,
            )
            for diagram in enrichment.diagrams
        ],
        "source_figures": [_figure_to_json(figure) for figure in enrichment.source_figures],
        "warnings": list(enrichment.warnings),
    }
    if enrichment.schema_version >= 2:
        payload["annotations"] = [
            semantic_annotation_proposal_to_json(item) for item in enrichment.annotations
        ]
    if enrichment.schema_version >= 3:
        payload["figure_decisions"] = [
            _figure_decision_to_json(item) for item in enrichment.figure_decisions
        ]
        payload["resource_needs"] = [
            _resource_need_to_json(item) for item in enrichment.resource_needs
        ]
        payload["resource_proposals"] = [
            _resource_proposal_to_json(item) for item in enrichment.resource_proposals
        ]
    return payload


_ROOT_KEYS_V1 = frozenset(
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
_ROOT_KEYS_V2 = _ROOT_KEYS_V1 | {"annotations"}
_ROOT_KEYS_V3 = _ROOT_KEYS_V2 | {"figure_decisions", "resource_needs", "resource_proposals"}
_ROOT_KEYS_V4 = _ROOT_KEYS_V3
_ROOT_KEYS_V5 = _ROOT_KEYS_V4
_ROOT_KEYS_V6 = _ROOT_KEYS_V5
_REF_KEYS = frozenset({"source_document_id", "kind", "evidence_key"})
_PLACEMENT_KEYS = frozenset({"kind", "section_index"})
_COLUMN_KEYS = frozenset({"key", "label"})
_ROW_KEYS = frozenset({"cells", "evidence_refs"})
_TABLE_BASE_KEYS = frozenset({"key", "kind", "title", "caption", "columns", "rows", "placement"})
_TABLE_KEYS = _TABLE_BASE_KEYS | {"purpose"}
_PURPOSE_KEYS = frozenset(
    {
        "question",
        "available_data",
        "comprehension_gain",
        "scope",
        "evidence_refs",
        "knowledge_limits",
        "placement_reason",
    }
)
_NODE_BASE_KEYS = frozenset({"node_id", "label", "evidence_refs"})
_NODE_KEYS = _NODE_BASE_KEYS | {"role"}
_EDGE_BASE_KEYS = frozenset({"source_node_id", "target_node_id", "label", "evidence_refs"})
_EDGE_KEYS = _EDGE_BASE_KEYS | {"relation_type"}
_GROUP_KEYS = frozenset({"group_id", "label", "node_ids"})
_DIAGRAM_BASE_KEYS = frozenset(
    {"key", "kind", "title", "caption", "direction", "nodes", "edges", "groups", "placement"}
)
_DIAGRAM_KEYS = _DIAGRAM_BASE_KEYS | {"compiled_asset_id"}
_DIAGRAM_ANALYTIC_BASE_KEYS = _DIAGRAM_BASE_KEYS | {"purpose"}
_DIAGRAM_ANALYTIC_KEYS = _DIAGRAM_KEYS | {"purpose"}
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
_FIGURE_DECISION_KEYS = frozenset(
    {
        "handle",
        "figure_id",
        "source_document_id",
        "decision",
        "actor",
        "reason_code",
        "reason",
        "policy_version",
        "prompt_version",
        "contract_version",
        "parser_version",
        "evidence_refs",
    }
)
_RESOURCE_NEED_KEYS = frozenset({"key", "kind", "reason", "query_hint", "policy_version"})
_RESOURCE_PROPOSAL_KEYS = frozenset(
    {"need_key", "url", "justification", "source_model_run_id", "policy_version"}
)


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


def _purpose_from_json(raw: Any) -> EditorialAnalyticPurposeV1:
    payload = _object(raw, _PURPOSE_KEYS, "Editorial analytic purpose")
    return EditorialAnalyticPurposeV1(
        question=_text(payload["question"], "Analytic purpose question", semantic=True),
        available_data=_text(
            payload["available_data"], "Analytic purpose available data", semantic=True
        ),
        comprehension_gain=_text(
            payload["comprehension_gain"], "Analytic purpose comprehension gain", semantic=True
        ),
        scope=_text(payload["scope"], "Analytic purpose scope", semantic=True),
        evidence_refs=_refs_from_json(
            payload["evidence_refs"], "Analytic purpose evidence references"
        ),
        knowledge_limits=_text(
            payload["knowledge_limits"], "Analytic purpose knowledge limits", semantic=True
        ),
        placement_reason=_text(
            payload["placement_reason"], "Analytic purpose placement reason", semantic=True
        ),
    )


def _table_from_json(raw: Any, *, require_analytic: bool = False) -> TableSpecV1:
    allowed_keys = _TABLE_KEYS if require_analytic else _TABLE_BASE_KEYS
    payload = _object(raw, allowed_keys, "Editorial table")
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
        purpose=_purpose_from_json(payload["purpose"]) if require_analytic else None,
    )


def _node_from_json(raw: Any, *, require_role: bool = False) -> DiagramNodeV1:
    valid_keys = _NODE_KEYS if require_role else _NODE_BASE_KEYS
    if not isinstance(raw, Mapping) or frozenset(raw) != valid_keys:
        raise ValueError("Diagram node has missing or extra fields")
    payload = raw
    return DiagramNodeV1(
        node_id=_text(payload["node_id"], "Diagram node ID", semantic=True),
        label=_text(payload["label"], "Diagram node label", semantic=True),
        evidence_refs=_refs_from_json(payload["evidence_refs"], "Diagram node evidence references"),
        role=(
            _enum(DiagramNodeRole, payload["role"], "Diagram node role")
            if "role" in payload
            else DiagramNodeRole.UNKNOWN
        ),
    )


def _edge_from_json(raw: Any, *, require_relation_type: bool = False) -> DiagramEdgeV1:
    allowed_keys = _EDGE_KEYS if require_relation_type else _EDGE_BASE_KEYS
    payload = _object(raw, allowed_keys, "Diagram edge")
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
        relation_type=(
            _enum(DiagramRelationType, payload["relation_type"], "Diagram relation type")
            if require_relation_type
            else DiagramRelationType.FACTUAL
        ),
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


def _diagram_from_json(
    raw: Any, *, require_analytic: bool = False, require_roles: bool = False
) -> DiagramSpecV1:
    allowed_key_sets = (
        {_DIAGRAM_ANALYTIC_BASE_KEYS, _DIAGRAM_ANALYTIC_KEYS}
        if require_analytic
        else {_DIAGRAM_BASE_KEYS, _DIAGRAM_KEYS}
    )
    if not isinstance(raw, Mapping) or frozenset(raw) not in allowed_key_sets:
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
        nodes=tuple(
            _node_from_json(value, require_role=require_roles)
            for value in _array(payload["nodes"], "Diagram nodes")
        ),
        edges=tuple(
            _edge_from_json(value, require_relation_type=require_analytic)
            for value in _array(payload["edges"], "Diagram edges")
        ),
        groups=tuple(
            _group_from_json(value) for value in _array(payload["groups"], "Diagram groups")
        ),
        placement=_placement_from_json(payload["placement"]),
        compiled_asset_id=(
            _uuid(payload["compiled_asset_id"], "Compiled diagram asset ID")
            if "compiled_asset_id" in payload
            else None
        ),
        purpose=_purpose_from_json(payload["purpose"]) if require_analytic else None,
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


def _figure_decision_from_json(raw: Any) -> EditorialFigureDecisionTraceV1:
    payload = _object(raw, _FIGURE_DECISION_KEYS, "Editorial figure decision")
    return EditorialFigureDecisionTraceV1(
        handle=_text(payload["handle"], "Editorial figure handle"),
        figure_id=_uuid(payload["figure_id"], "Editorial figure identity"),
        source_document_id=_uuid(payload["source_document_id"], "Editorial figure source identity"),
        decision=_enum(EditorialFigureDecision, payload["decision"], "Editorial figure decision"),
        actor=_enum(
            EditorialFigureDecisionActor, payload["actor"], "Editorial figure decision actor"
        ),
        reason_code=_text(
            payload["reason_code"], "Editorial figure decision reason", semantic=True
        ),
        reason=_text(payload["reason"], "Editorial figure decision explanation", semantic=True),
        policy_version=_text(
            payload["policy_version"], "Editorial figure decision policy", semantic=True
        ),
        prompt_version=_text(
            payload["prompt_version"], "Editorial figure prompt version", semantic=True
        ),
        contract_version=_text(
            payload["contract_version"], "Editorial figure contract version", semantic=True
        ),
        parser_version=_text(
            payload["parser_version"], "Editorial figure parser version", semantic=True
        ),
        evidence_refs=_refs_from_json(
            payload["evidence_refs"], "Editorial figure evidence references"
        ),
    )


def _resource_need_from_json(raw: Any) -> EditorialResourceNeedV1:
    payload = _object(raw, _RESOURCE_NEED_KEYS, "Editorial resource need")
    return EditorialResourceNeedV1(
        key=_text(payload["key"], "Editorial resource need key"),
        kind=_enum(ResourceNeedKind, payload["kind"], "Editorial resource need kind"),
        reason=_text(payload["reason"], "Editorial resource need reason", semantic=True),
        query_hint=_text(payload["query_hint"], "Editorial resource query hint", semantic=True),
        policy_version=_text(
            payload["policy_version"], "Editorial resource policy version", semantic=True
        ),
    )


def _resource_proposal_from_json(raw: Any) -> EditorialResourceProposalV1:
    payload = _object(raw, _RESOURCE_PROPOSAL_KEYS, "Editorial resource proposal")
    return EditorialResourceProposalV1(
        need_key=_text(payload["need_key"], "Editorial resource proposal need key"),
        url=_text(payload["url"], "Editorial resource proposal URL", semantic=True),
        justification=_text(
            payload["justification"], "Editorial resource proposal justification", semantic=True
        ),
        source_model_run_id=_uuid(
            payload["source_model_run_id"], "Editorial resource proposal model run identity"
        ),
        policy_version=_text(
            payload["policy_version"], "Editorial resource policy version", semantic=True
        ),
    )


def editorial_enrichment_from_json(payload: Mapping[str, Any]) -> EditorialEnrichmentV1:
    """Decode strict V1-V5 payloads; older artifacts retain empty review data."""
    if not isinstance(payload, Mapping):
        raise ValueError("Editorial enrichment must be an object")
    raw_version = payload.get("schema_version")
    if type(raw_version) is not int:
        raise ValueError("Editorial enrichment schema version must be an integer")
    root_keys = {
        1: _ROOT_KEYS_V1,
        2: _ROOT_KEYS_V2,
        3: _ROOT_KEYS_V3,
        4: _ROOT_KEYS_V4,
        5: _ROOT_KEYS_V5,
        6: _ROOT_KEYS_V6,
    }.get(raw_version)
    if root_keys is None:
        raise ValueError("Editorial enrichment schema version is unsupported")
    body = _object(payload, root_keys, "Editorial enrichment")
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
            _table_from_json(value, require_analytic=schema_version >= 4)
            for value in _array(body["tables"], "Editorial tables")
        ),
        diagrams=tuple(
            _diagram_from_json(
                value, require_analytic=schema_version >= 4, require_roles=schema_version >= 5
            )
            for value in _array(body["diagrams"], "Editorial diagrams")
        ),
        source_figures=tuple(
            _figure_from_json(value) for value in _array(body["source_figures"], "Source figures")
        ),
        warnings=warnings,
        annotations=(
            tuple(
                semantic_annotation_proposal_from_json(value)
                for value in _array(body["annotations"], "Semantic annotations")
            )
            if schema_version >= 2
            else ()
        ),
        figure_decisions=(
            tuple(
                _figure_decision_from_json(value)
                for value in _array(body["figure_decisions"], "Editorial figure decisions")
            )
            if schema_version >= 3
            else ()
        ),
        resource_needs=(
            tuple(
                _resource_need_from_json(value)
                for value in _array(body["resource_needs"], "Editorial resource needs")
            )
            if schema_version >= 3
            else ()
        ),
        resource_proposals=(
            tuple(
                _resource_proposal_from_json(value)
                for value in _array(body["resource_proposals"], "Editorial resource proposals")
            )
            if schema_version >= 3
            else ()
        ),
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
