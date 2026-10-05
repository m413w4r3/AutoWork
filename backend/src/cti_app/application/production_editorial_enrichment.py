"""Canonical Editorial Enrichment generation (AW-016)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from cti_app.application.analytic_chart_compilation import (
    AnalyticChartCompiler,
)
from cti_app.application.diagram_compilation import DiagramCompiler
from cti_app.application.media_assets import (
    MAX_SOURCE_FIGURE_BYTES,
    MediaAssetStore,
    SourceFigureIngestor,
    compile_and_store_charts,
    compile_and_store_diagrams,
)
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
    StructuredOutputError,
)
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_parsers import sanitize_bridge_output_text
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REPAIR_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
    EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_RESOURCE_PROPOSAL_PROMPT_VERSION,
    EDITORIAL_RESOURCE_PROPOSAL_WIRE_PARSER_VERSION,
    SEMANTIC_ANNOTATION_CONTRACT_VERSION,
    SEMANTIC_ANNOTATION_OUTPUT_CONTRACT,
    SEMANTIC_ANNOTATION_PROMPT_VERSION,
    SEMANTIC_ANNOTATION_ROLE_GUIDANCE,
    SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
)
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    _all_evidence_entries,
    _date_literals,
    _date_supported_by_payload,
    _string_values,
    _technical_literals,
    _validate_plain_text,
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.production_wire_archive import (
    record_wire_parse_diagnostics,
    verified_raw_output_text,
)
from cti_app.application.source_figure_inventory import (
    SourceFigureCatalogMetadata,
    SourceFigureInventoryResult,
    load_archived_source_figure_inventory,
)
from cti_app.application.source_media_collection import (
    SourceMediaArchiveService,
)
from cti_app.domain.model_runs import ModelRun, ModelRunStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    model_run_awaits_reconciliation,
)
from cti_app.domain.production_editorial_enrichment import (
    DIAGRAM_VERTICAL_AFTER_LABEL_CHARACTERS,
    DIAGRAM_VERTICAL_AFTER_NODES,
    EDITORIAL_ENRICHMENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
    EDITORIAL_FIGURE_DECISION_POLICY_VERSION,
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramProfile,
    DiagramRelationDirection,
    DiagramRelationType,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentV1,
    EditorialFigureDecision,
    EditorialFigureDecisionActor,
    EditorialFigureDecisionTraceV1,
    EditorialMediaType,
    EditorialResourceNeedV1,
    EditorialResourceProposalV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    ResolvedSourceFigureV1,
    ResourceNeedKind,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    diagram_requires_vertical_layout,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
    normalize_analytic_question,
    validate_chart_date,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.production_references import ProductionEditorialRole, ProductionReferenceTier
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    relevance_projection_from_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    evidence_ref_sort_key,
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
    production_synthesis_to_json,
    synthesis_evidence_refs,
    validate_synthesis_lineage,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ROLE_PRIORITY,
    SemanticAnnotationProposalV1,
    SemanticRole,
    lead_paragraph_anchor,
    section_paragraph_anchor,
    timeline_anchor,
)
from cti_app.domain.source_media import (
    SourceFigureProvenanceDiagnostic,
    SourceFigureProvenanceStage,
)

if TYPE_CHECKING:
    from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
    from cti_app.application.production_stages import EditorialEnrichmentService

EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "model-text-blocks-v6-dedicated-annotations"
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION = 5
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION = (
    "editorial-enrichment-evidence-pack-v7-timeline-anchors"
)
EDITORIAL_ENRICHMENT_VALIDATOR_VERSION = "editorial-enrichment-validator-v9-d2-diagram-profiles"
EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION = (
    "editorial-enrichment-analytic-policy-v4-d2-diagram-profiles"
)
TABLE_PARAPHRASE_TOKEN_DICE_THRESHOLD = 0.80
EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION = (
    "editorial-enrichment-model-policy-v6-d2-diagram-profiles"
)
_ENRICHMENT_MEDIA_TYPE_BY_BLOCK = {
    "FIGURE": EditorialMediaType.SOURCE_FIGURE,
    "CHART": EditorialMediaType.CHART,
    "DIAGRAM": EditorialMediaType.DIAGRAM,
    "TABLE": EditorialMediaType.TABLE,
}
_ENRICHMENT_MEDIA_ARBITRATION_PRIORITY = {
    EditorialMediaType.SOURCE_FIGURE: 0,
    EditorialMediaType.CHART: 1,
    EditorialMediaType.DIAGRAM: 1,
    EditorialMediaType.TABLE: 1,
    EditorialMediaType.NONE: 2,
}
SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION = "source-figure-provenance-v1"
EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION = (
    "editorial-enrichment-routing-policy-v2-resource-search-off"
)
EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION = "editorial-resource-proposal-v1-bounded"

MAX_EDITORIAL_ENRICHMENT_TECHNICAL_EVIDENCE = 128
MAX_ENRICHMENT_FIGURE_PROPOSALS = 3
MAX_ENRICHMENT_RESOURCE_NEEDS = 4
MAX_ENRICHMENT_RESOURCE_PROPOSALS = 12

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RENDERER_SYNTAX = re.compile(
    r"(?im)(?:```|~~~|<\s*/?\s*[a-z][^>]*>|^\s*(?:graph\s+|digraph\b|subgraph\b|flowchart\b|sequenceDiagram\b|classDiagram\b|stateDiagram\b|erDiagram\b|mindmap\b|architecture-beta\b)|^\s*(?:direction\s*:\s*(?:right|down|left|up)|[\w.-]+\s*(?:->|<-|<->|-->|\.\.>|--|\|>))|\\begin\s*\{|\\(?:tikz|node|draw|path)\b|#(?:set|let|show|import)\b|<svg\b|^\s*\|.*\|\s*$)"
)
_EVIDENCE_HANDLE_RE = re.compile(r"^E[0-9]{3,}$")


def _canonical_json_bytes(payload: Any) -> bytes:
    return ProductionArtifactStore.canonical_json_bytes(payload)


class _StrictEnrichmentProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _nonempty_proposal_text(value: str) -> str:
    if not value.strip():
        raise ValueError("Proposal text must be non-empty")
    return value


def _nonempty_evidence_handles(value: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not value
        or any(
            not handle.strip() or _EVIDENCE_HANDLE_RE.fullmatch(handle) is None for handle in value
        )
        or len(value) != len(set(value))
    ):
        raise ValueError("Evidence handles must be a non-empty tuple of unique handles")
    return value


def _editorial_enrichment_media_priority(block_kind: str) -> int:
    media_type = _ENRICHMENT_MEDIA_TYPE_BY_BLOCK.get(block_kind)
    if media_type is None:
        return _ENRICHMENT_MEDIA_ARBITRATION_PRIORITY[EditorialMediaType.NONE]
    return _ENRICHMENT_MEDIA_ARBITRATION_PRIORITY[media_type]


class EnrichmentPlacementProposalV1(_StrictEnrichmentProposalModel):
    kind: EnrichmentPlacementKind
    section_index: StrictInt | None = None

    @model_validator(mode="after")
    def _validate_section_index(self) -> EnrichmentPlacementProposalV1:
        if self.kind is EnrichmentPlacementKind.AFTER_SECTION:
            if self.section_index is None or self.section_index < 0:
                raise ValueError("AFTER_SECTION requires a non-negative section_index")
        elif self.section_index is not None:
            raise ValueError("section_index is only valid for AFTER_SECTION")
        return self


class TableColumnProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    label: StrictStr

    @field_validator("key", "label")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)


class TableRowProposalV1(_StrictEnrichmentProposalModel):
    cells: tuple[StrictStr, ...]
    evidence_handles: tuple[StrictStr, ...]

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class AnalyticPurposeProposalV1(_StrictEnrichmentProposalModel):
    question: StrictStr
    available_data: StrictStr
    comprehension_gain: StrictStr
    scope: StrictStr
    evidence_handles: tuple[StrictStr, ...]
    knowledge_limits: StrictStr
    placement_reason: StrictStr

    @field_validator(
        "question",
        "available_data",
        "comprehension_gain",
        "scope",
        "knowledge_limits",
        "placement_reason",
    )
    @classmethod
    def _nonempty_bounded(cls, value: str) -> str:
        cleaned = _nonempty_proposal_text(value)
        if len(cleaned) > 1000:
            raise ValueError("Analytic purpose fields must be at most 1000 characters")
        return cleaned

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class TableProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    kind: EnrichmentTableKind
    title: StrictStr
    caption: StrictStr | None = None
    columns: tuple[TableColumnProposalV1, ...]
    rows: tuple[TableRowProposalV1, ...]
    placement: EnrichmentPlacementProposalV1
    purpose: AnalyticPurposeProposalV1

    @field_validator("key", "title")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("caption")
    @classmethod
    def _caption_text(cls, value: str | None) -> str | None:
        return _nonempty_proposal_text(value) if value is not None else None


class ChartPointProposalV1(_StrictEnrichmentProposalModel):
    label: StrictStr
    date: StrictStr
    series: StrictStr
    evidence_handles: tuple[StrictStr, ...]

    @field_validator("label", "series")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("date")
    @classmethod
    def _exact_iso_date(cls, value: str) -> str:
        return validate_chart_date(value)

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class ChartProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    kind: ChartKind
    title: StrictStr
    caption: StrictStr | None = None
    placement: EnrichmentPlacementProposalV1
    purpose: AnalyticPurposeProposalV1
    points: tuple[ChartPointProposalV1, ...]

    @field_validator("key", "title")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("caption")
    @classmethod
    def _caption_text(cls, value: str | None) -> str | None:
        return _nonempty_proposal_text(value) if value is not None else None

    @field_validator("points")
    @classmethod
    def _points_required(
        cls, value: tuple[ChartPointProposalV1, ...]
    ) -> tuple[ChartPointProposalV1, ...]:
        if not value:
            raise ValueError("A timeline chart requires at least one point")
        return value


class DiagramNodeProposalV1(_StrictEnrichmentProposalModel):
    node_id: StrictStr
    label: StrictStr
    evidence_handles: tuple[StrictStr, ...]
    role: DiagramNodeRole = DiagramNodeRole.UNKNOWN

    @field_validator("node_id", "label")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class DiagramEdgeProposalV1(_StrictEnrichmentProposalModel):
    source_node_id: StrictStr
    target_node_id: StrictStr
    label: StrictStr | None = None
    relation_type: DiagramRelationType
    direction: DiagramRelationDirection
    evidence_handles: tuple[StrictStr, ...]

    @field_validator("source_node_id", "target_node_id")
    @classmethod
    def _nonempty_endpoint(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("label")
    @classmethod
    def _label_text(cls, value: str | None) -> str | None:
        return _nonempty_proposal_text(value) if value is not None else None

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class DiagramGroupProposalV1(_StrictEnrichmentProposalModel):
    group_id: StrictStr
    label: StrictStr
    node_ids: tuple[StrictStr, ...]

    @field_validator("group_id", "label")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)


class DiagramProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    kind: EnrichmentDiagramKind
    title: StrictStr
    caption: StrictStr | None = None
    direction: EnrichmentDiagramDirection
    nodes: tuple[DiagramNodeProposalV1, ...]
    edges: tuple[DiagramEdgeProposalV1, ...]
    groups: tuple[DiagramGroupProposalV1, ...] = ()
    placement: EnrichmentPlacementProposalV1
    purpose: AnalyticPurposeProposalV1
    profile: DiagramProfile

    @field_validator("key", "title")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("caption")
    @classmethod
    def _caption_text(cls, value: str | None) -> str | None:
        return _nonempty_proposal_text(value) if value is not None else None


class AnnotationProposalV1(_StrictEnrichmentProposalModel):
    role: SemanticRole
    paragraph_anchor: StrictStr
    text: StrictStr

    @field_validator("paragraph_anchor")
    @classmethod
    def _validate_anchor(cls, value: str) -> str:
        if not value or len(value) > 128 or re.fullmatch(r"[a-z][a-z0-9:_-]*", value) is None:
            raise ValueError("Annotation paragraph anchor is invalid")
        return value

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not value:
            raise ValueError("Annotation text must be non-empty")
        return value


class FigureProposalV1(_StrictEnrichmentProposalModel):
    figure_handle: StrictStr
    caption: StrictStr
    evidence_handles: tuple[StrictStr, ...]
    reason: StrictStr
    placement: EnrichmentPlacementProposalV1
    purpose: AnalyticPurposeProposalV1

    @field_validator("figure_handle")
    @classmethod
    def _handle(cls, value: str) -> str:
        if re.fullmatch(r"F[0-9]{3,}", value) is None:
            raise ValueError("Figure handle is invalid")
        return value

    @field_validator("caption", "reason")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not (1 <= len(value.strip()) <= 500):
            raise ValueError("Figure caption and reason must be bounded text")
        return value

    @field_validator("evidence_handles")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _nonempty_evidence_handles(value)


class ResourceNeedProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    kind: ResourceNeedKind
    reason: StrictStr
    query_hint: StrictStr

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if re.fullmatch(r"N[0-9]{3,}", value) is None:
            raise ValueError("Resource need key is invalid")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not (1 <= len(value.strip()) <= 500):
            raise ValueError("Resource need reason must be bounded text")
        return value

    @field_validator("query_hint")
    @classmethod
    def _query_hint(cls, value: str) -> str:
        if not (1 <= len(value.strip()) <= 240):
            raise ValueError("Resource need query hint must be bounded text")
        return value


class ResourceProposalV1(_StrictEnrichmentProposalModel):
    need_key: StrictStr
    url: StrictStr
    justification: StrictStr

    @field_validator("need_key")
    @classmethod
    def _need_key(cls, value: str) -> str:
        if re.fullmatch(r"N[0-9]{3,}", value) is None:
            raise ValueError("Resource proposal need key is invalid")
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not (1 <= len(value.strip()) <= 2048):
            raise ValueError("Resource proposal URL must be bounded text")
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Resource proposal URL must be an HTTP(S) URL")
        return value

    @field_validator("justification")
    @classmethod
    def _justification(cls, value: str) -> str:
        if not (1 <= len(value.strip()) <= 500):
            raise ValueError("Resource proposal justification must be bounded text")
        return value


@dataclass(frozen=True, slots=True)
class EditorialFigureCatalogEntry:
    handle: str
    source_role: str
    editorial_role: str
    figure: ResolvedSourceFigureV1
    metadata: SourceFigureCatalogMetadata

    @property
    def source_caption(self) -> str | None:
        for value in (self.metadata.caption_text, self.metadata.alt_text):
            if value is not None and value.strip():
                return value.strip()
        return None

    def prompt_record(
        self,
        *,
        include_source_page: bool = False,
        include_asset_url: bool = False,
        evidence_handles: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        context_before = _safe_figure_prompt_text(_tail(self.metadata.context_before, 400))
        context_after = _safe_figure_prompt_text(_bounded_catalog_text(self.metadata.context_after))
        source_caption = _safe_figure_prompt_text(self.metadata.caption_text)
        alt_text = _safe_figure_prompt_text(self.metadata.alt_text)
        nearby_heading = _safe_figure_prompt_text(
            self.metadata.nearby_heading_text or self.figure.locator.section
        )
        return {
            "handle": self.handle,
            "source_role": self.source_role,
            "editorial_role": self.editorial_role,
            "source_caption": source_caption,
            "caption": source_caption,
            "alt": alt_text,
            "nearby_heading": nearby_heading,
            "figure_label": _safe_figure_prompt_text(self.figure.locator.figure_label),
            "source_page_url": self.figure.source if include_source_page else None,
            "asset_url": (
                _safe_asset_prompt_url(self.figure.locator.original_asset_url)
                if include_asset_url
                else None
            ),
            "image_file": _image_file_name(self.figure.locator.original_asset_url),
            # The model finds the image on the live page through the text around
            # it; the nearest words are the most discriminating.
            "context_before": context_before,
            "context_after": context_after,
            "text_before_image": context_before,
            "text_after_image": context_after,
            "page": self.figure.locator.page,
            "anchor": _safe_figure_prompt_text(self.metadata.anchor),
            "width": self.metadata.width,
            "height": self.metadata.height,
            "dimensions": {
                "width": self.metadata.width,
                "height": self.metadata.height,
            },
            "aspect_ratio_warning": self.metadata.aspect_ratio_warning,
            "evidence": list(evidence_handles),
            "in_article_body": self.metadata.in_article_body,
            "mime_type": self.figure.mime_type,
            "provenance_summary": _bounded_catalog_text(
                f"Archived source media; role={self.source_role}"
            ),
            "decision": self.figure.decision.value,
            "decision_reason": _safe_figure_prompt_text(self.figure.decision_reason),
        }


def _tail(value: str | None, limit: int) -> str | None:
    return value[-limit:] if value else None


def _image_file_name(asset_url: str | None) -> str | None:
    if not asset_url or asset_url.casefold().startswith("data:"):
        return None
    name = urlsplit(asset_url).path.rsplit("/", 1)[-1]
    return name or None


def _safe_asset_prompt_url(asset_url: str | None) -> str | None:
    if not asset_url or asset_url.casefold().startswith("data:"):
        return None
    try:
        parts = urlsplit(asset_url)
    except ValueError:
        return None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    return urlunsplit((parts.scheme.casefold(), parts.netloc, parts.path, "", ""))


def _bounded_catalog_text(value: str | None, limit: int = 400) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _safe_figure_prompt_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"https?://\S+", "[redacted URL]", value, flags=re.IGNORECASE)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "[redacted ID]",
        text,
        flags=re.IGNORECASE,
    )
    return _bounded_catalog_text(text)


def _proposal_annotation_to_domain(
    annotation: AnnotationProposalV1,
) -> SemanticAnnotationProposalV1:
    return SemanticAnnotationProposalV1(
        paragraph_anchor=annotation.paragraph_anchor,
        role=annotation.role,
        text=annotation.text,
    )


class EditorialEnrichmentProposalV1(_StrictEnrichmentProposalModel):
    tables: tuple[TableProposalV1, ...] = ()
    charts: tuple[ChartProposalV1, ...] = ()
    diagrams: tuple[DiagramProposalV1, ...] = ()
    annotations: tuple[AnnotationProposalV1, ...] = ()
    figures: tuple[FigureProposalV1, ...] = ()
    resource_needs: tuple[ResourceNeedProposalV1, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentWireRejection:
    block_id: str
    reason_code: str
    raw_sha256: str
    scope_id: str | None = None
    parent_scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentRejectedBlock:
    """A rejected top-level block available to the bounded repair pass."""

    kind: str
    block_id: str
    scope_id: str
    raw_text: str
    raw_sha256: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentAcceptedBlock:
    """Wire identity for one accepted top-level proposal."""

    kind: str
    block_id: str
    scope_id: str
    proposal_key: str


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentWireParseResult:
    proposal: EditorialEnrichmentProposalV1 | None
    rejections: tuple[EditorialEnrichmentWireRejection, ...] = ()
    error_code: str | None = None
    explicit_empty: bool = False
    transformations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rejected_blocks: tuple[EditorialEnrichmentRejectedBlock, ...] = ()
    accepted_blocks: tuple[EditorialEnrichmentAcceptedBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticAnnotationWireParseResult:
    proposals: tuple[SemanticAnnotationProposalV1, ...] = ()
    rejections: tuple[tuple[str, str], ...] = ()
    error_code: str | None = None


def _merge_semantic_annotation_proposals(
    *proposal_groups: tuple[SemanticAnnotationProposalV1, ...],
) -> tuple[SemanticAnnotationProposalV1, ...]:
    """Keep one stable provenance anchor per exact term, resolving role conflicts."""
    selected: dict[str, SemanticAnnotationProposalV1] = {}
    for proposal in (item for group in proposal_groups for item in group):
        current = selected.get(proposal.text)
        if (
            current is None
            or SEMANTIC_ROLE_PRIORITY[proposal.role] > SEMANTIC_ROLE_PRIORITY[current.role]
            or (
                SEMANTIC_ROLE_PRIORITY[proposal.role] == SEMANTIC_ROLE_PRIORITY[current.role]
                and proposal.role.value < current.role.value
            )
            or (
                proposal.role is current.role
                and proposal.paragraph_anchor < current.paragraph_anchor
            )
        ):
            selected[proposal.text] = proposal
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (item.paragraph_anchor, item.text, item.role.value),
        )
    )


@dataclass(frozen=True, slots=True)
class EditorialResourceWireParseResult:
    proposals: tuple[ResourceProposalV1, ...]
    rejections: tuple[EditorialEnrichmentWireRejection, ...] = ()
    error_code: str | None = None
    transformations: tuple[str, ...] = ()


@dataclass(slots=True)
class _EditorialEnrichmentWireBlock:
    kind: str
    block_id: str
    scope_id: str = ""
    parent_scope_id: str | None = None
    raw_lines: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    children: list[_EditorialEnrichmentWireBlock] = field(default_factory=list)
    error_code: str | None = None

    def append_field(self, name: str, value: str, *, repeated: bool = False) -> None:
        values = self.fields.setdefault(name, [])
        if values and not repeated:
            self.error_code = self.error_code or "editorial_enrichment_duplicate_field"
        values.append(value)


_ENRICHMENT_FENCE = re.compile(r"^\s*(?:```|~~~)")
_ENRICHMENT_BLOCK_WRAPPER = re.compile(r"^@@\s*(.*?)\s*@@$")
_ENRICHMENT_HEADER = re.compile(
    r"^(TABLE|CHART|DIAGRAM|ANNOTATION|FIGURE|NEEDS|COLUMN|ROW|POINT|NODE|RELATION|EDGE|GROUP)"
    r"(?:(?:\s*:\s*|\s+)([A-Za-z0-9][A-Za-z0-9._-]*))?\s*:?$",
    re.IGNORECASE,
)
_ENRICHMENT_FIELD = re.compile(r"^\*{0,2}([A-Za-z][A-Za-z0-9 _-]*?)\*{0,2}\s*:\s?(.*)$")
_ENRICHMENT_HANDLE = re.compile(r"\bE\d{3,}\b")
_ENRICHMENT_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENRICHMENT_FIELD_ALIASES = {
    "key": "key",
    "kind": "kind",
    "title": "title",
    "caption": "caption",
    "profile": "profile",
    "diagram profile": "profile",
    "placement": "placement",
    "section index": "section_index",
    "section_index": "section_index",
    "purpose": "purpose",
    "question": "purpose",
    "data": "available_data",
    "available data": "available_data",
    "gain": "comprehension_gain",
    "comprehension gain": "comprehension_gain",
    "scope": "scope",
    "purpose evidence": "purpose_evidence_handles",
    "purpose evidence handles": "purpose_evidence_handles",
    "limits": "knowledge_limits",
    "knowledge limits": "knowledge_limits",
    "placement reason": "placement_reason",
    "relation type": "relation_type",
    "relation_type": "relation_type",
    "column key": "key",
    "label": "label",
    "date": "date",
    "series": "series",
    "cell": "cell",
    "evidence": "evidence_handles",
    "evidence handle": "evidence_handles",
    "evidence handles": "evidence_handles",
    "handles": "evidence_handles",
    "direction": "direction",
    "id": "id",
    "node id": "node_id",
    "node_id": "node_id",
    "source": "source_node_id",
    "from": "source_node_id",
    "source node": "source_node_id",
    "source node id": "source_node_id",
    "target": "target_node_id",
    "to": "target_node_id",
    "target node": "target_node_id",
    "target node id": "target_node_id",
    "group id": "group_id",
    "nodes": "node_ids",
    "node ids": "node_ids",
    "category": "role",
    "role": "role",
    "anchor": "paragraph_anchor",
    "paragraph": "paragraph_anchor",
    "paragraph anchor": "paragraph_anchor",
    "paragraph_anchor": "paragraph_anchor",
    "figure handle": "figure_handle",
    "figure_handle": "figure_handle",
    "need key": "need_key",
    "need_key": "need_key",
    "query hint": "query_hint",
    "query_hint": "query_hint",
    "reason": "reason",
    "need": "need_key",
    "url": "url",
    "justification": "justification",
    "exact text": "text",
    "segment": "text",
    "text": "text",
}
_ENRICHMENT_CHILD_KINDS = frozenset({"COLUMN", "ROW", "POINT", "NODE", "RELATION", "EDGE", "GROUP"})
_ENRICHMENT_CHILD_PARENT = {
    "COLUMN": "TABLE",
    "ROW": "TABLE",
    "POINT": "CHART",
    "NODE": "DIAGRAM",
    "RELATION": "DIAGRAM",
    "EDGE": "DIAGRAM",
    "GROUP": "DIAGRAM",
}


def _enrichment_wire_line(raw_line: str) -> str:
    value = raw_line.strip()
    value = re.sub(r"^#{1,6}\s*", "", value)
    value = re.sub(r"^[-*+]\s+", "", value).strip()
    if len(value) >= 4 and value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    wrapped = _ENRICHMENT_BLOCK_WRAPPER.fullmatch(value)
    return wrapped.group(1).strip() if wrapped is not None else value


def _enrichment_wire_rejection(
    block: _EditorialEnrichmentWireBlock | None,
    block_id: str,
    reason_code: str,
    raw_lines: tuple[str, ...] | list[str],
) -> EditorialEnrichmentWireRejection:
    raw = "\n".join(raw_lines).encode("utf-8")
    return EditorialEnrichmentWireRejection(
        block_id=block.block_id if block is not None else block_id,
        reason_code=reason_code,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        scope_id=block.scope_id if block is not None and block.scope_id else None,
        parent_scope_id=block.parent_scope_id if block is not None else None,
    )


def _wire_scalar(block: _EditorialEnrichmentWireBlock, field_name: str) -> str | None:
    values = block.fields.get(field_name)
    return values[0] if values else None


def _wire_caption(block: _EditorialEnrichmentWireBlock) -> str | None:
    value = _wire_scalar(block, "caption")
    return None if value is None or value.strip().casefold() in {"", "-", "none", "null"} else value


def _wire_handles(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    handles = tuple(_ENRICHMENT_HANDLE.findall(value))
    residue = _ENRICHMENT_HANDLE.sub("", value)
    residue = re.sub(r"\band\b", "", residue, flags=re.IGNORECASE)
    residue = re.sub(r"[\[\](){}\s,;./&|]+", "", residue)
    return handles if handles and not residue and len(set(handles)) == len(handles) else None


def _wire_node_ids(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    node_ids = tuple(_ENRICHMENT_NODE_ID.findall(value))
    residue = _ENRICHMENT_NODE_ID.sub("", value)
    residue = re.sub(r"[\s,;|]+", "", residue)
    return node_ids if node_ids and not residue else None


def _parse_analytic_purpose(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
) -> AnalyticPurposeProposalV1 | None:
    values = {
        name: _wire_scalar(block, name)
        for name in (
            "purpose",
            "available_data",
            "comprehension_gain",
            "scope",
            "purpose_evidence_handles",
            "knowledge_limits",
            "placement_reason",
        )
    }
    handles = _wire_handles(values["purpose_evidence_handles"])
    if any(not isinstance(value, str) or not value.strip() for value in values.values()) or (
        handles is None
    ):
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_analytic_purpose_missing_or_invalid",
                block.raw_lines,
            )
        )
        return None
    if not normalize_analytic_question(values["purpose"] or ""):
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_analytic_purpose_invalid",
                block.raw_lines,
            )
        )
        return None
    if any(handle not in evidence_pack._handle_to_ref for handle in handles):
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_unknown_evidence_handle",
                block.raw_lines,
            )
        )
        return None
    try:
        return AnalyticPurposeProposalV1(
            question=values["purpose"],
            available_data=values["available_data"],
            comprehension_gain=values["comprehension_gain"],
            scope=values["scope"],
            evidence_handles=handles,
            knowledge_limits=values["knowledge_limits"],
            placement_reason=values["placement_reason"],
        )
    except (TypeError, ValueError, ValidationError):
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_analytic_purpose_invalid",
                block.raw_lines,
            )
        )
        return None


def _trim_analytic_purpose_evidence(
    block: _EditorialEnrichmentWireBlock,
    purpose: AnalyticPurposeProposalV1,
    carried_handles: set[str],
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> AnalyticPurposeProposalV1 | None:
    retained = tuple(handle for handle in purpose.evidence_handles if handle in carried_handles)
    dropped = tuple(handle for handle in purpose.evidence_handles if handle not in carried_handles)
    if not retained:
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_purpose_evidence_not_relevant",
                block.raw_lines,
            )
        )
        return None
    if dropped:
        warnings.append(
            "editorial_enrichment_purpose_evidence_trimmed:"
            f"{block.kind}:{block.block_id}:" + ",".join(dropped)
        )
        return purpose.model_copy(update={"evidence_handles": retained})
    return purpose


def _wire_placement(block: _EditorialEnrichmentWireBlock) -> EnrichmentPlacementProposalV1 | None:
    raw_kind = _wire_scalar(block, "placement")
    if raw_kind is None:
        return None
    kind = next(
        (
            item
            for item in EnrichmentPlacementKind
            if item.value.casefold() == raw_kind.strip().casefold()
        ),
        None,
    )
    if kind is None:
        return None
    raw_index = _wire_scalar(block, "section_index")
    section_index: int | None = None
    if raw_index is not None and raw_index.strip().casefold() not in {"", "-", "none", "null"}:
        if not re.fullmatch(r"\d+", raw_index.strip()):
            return None
        section_index = int(raw_index.strip())
    try:
        return EnrichmentPlacementProposalV1(kind=kind, section_index=section_index)
    except ValueError:
        return None


def _block_child_error(
    child: _EditorialEnrichmentWireBlock,
    allowed_fields: frozenset[str],
) -> str | None:
    if child.error_code is not None:
        return child.error_code
    if set(child.fields) - allowed_fields:
        return "editorial_enrichment_unknown_field"
    return None


def _synthesis_anchor_texts(
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> dict[str, str]:
    current = evidence_pack.current_synthesis
    result: dict[str, str] = {}
    title = current.get("title")
    if isinstance(title, str):
        result["title"] = title
    for paragraph in current.get("lead", ()):
        if isinstance(paragraph, Mapping):
            anchor, text = paragraph.get("anchor"), paragraph.get("text")
            if isinstance(anchor, str) and isinstance(text, str):
                result[anchor] = text
    for section in current.get("sections", ()):
        if not isinstance(section, Mapping):
            continue
        for paragraph in section.get("paragraphs", ()):
            if isinstance(paragraph, Mapping):
                anchor, text = paragraph.get("anchor"), paragraph.get("text")
                if isinstance(anchor, str) and isinstance(text, str):
                    result[anchor] = text
    for index, timeline_entry in enumerate(current.get("timeline", ()), start=1):
        if isinstance(timeline_entry, Mapping):
            anchor = timeline_entry.get("anchor")
            text = timeline_entry.get("text")
            if isinstance(anchor, str) and isinstance(text, str):
                result[anchor] = text
            elif isinstance(text, str):
                result[timeline_anchor(index)] = text
    return result


def semantic_annotation_anchor_texts(
    synthesis: ProductionSynthesisV1,
    enrichment: EditorialEnrichmentV1 | None = None,
) -> dict[str, str]:
    """Mirror the text-bearing V5 publication anchors from its canonical inputs."""
    result: dict[str, str] = {}
    result.update(
        {
            lead_paragraph_anchor(index): paragraph.text
            for index, paragraph in enumerate(synthesis.lead, start=1)
        }
    )
    for section_index, section in enumerate(synthesis.sections):
        for paragraph_index, paragraph in enumerate(section.paragraphs, start=1):
            result[section_paragraph_anchor(section_index, paragraph_index)] = paragraph.text
    result.update(
        {
            timeline_anchor(index): entry.text
            for index, entry in enumerate(synthesis.timeline, start=1)
        }
    )
    if enrichment is None:
        return result
    for table in enrichment.tables:
        result[f"table:{table.key}:title"] = table.title
        if table.caption is not None:
            result[f"table:{table.key}:caption"] = table.caption
        for column_index, column in enumerate(table.columns, start=1):
            result[f"table:{table.key}:column:{column_index:04d}"] = column.label
        for row_index, row in enumerate(table.rows, start=1):
            for cell_index, cell in enumerate(row.cells, start=1):
                result[f"table:{table.key}:row:{row_index:04d}:cell:{cell_index:04d}"] = cell
    for diagram in enrichment.diagrams:
        result[f"diagram:{diagram.key}:title"] = diagram.title
        if diagram.caption is not None:
            result[f"diagram:{diagram.key}:caption"] = diagram.caption
    for chart in enrichment.charts:
        result[f"chart:{chart.key}:title"] = chart.title
        if chart.caption is not None:
            result[f"chart:{chart.key}:caption"] = chart.caption
    for figure in enrichment.source_figures:
        result[f"figure:{figure.key}:caption"] = figure.caption
        result[f"figure:{figure.key}:provenance"] = figure.provenance
    return result


# The prompt frames each anchor as `@@ANCHOR <id>@@`; models copy the frame.
_ANCHOR_MARKER = re.compile(r"^@@\s*ANCHOR\s+(.+?)\s*@@$", re.IGNORECASE)

_SEMANTIC_ANNOTATION_BLOCK = re.compile(
    r"^(?:TERM|ANNOTATION)\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*:?$",
    re.IGNORECASE,
)
_SEMANTIC_ANNOTATION_FIELD = re.compile(
    r"^(TERM|EXACT\s+TEXT|TEXT|ROLE|PARAGRAPH(?:\s+|_)ANCHOR|ANCHOR)\s*:\s?(.*)$",
    re.IGNORECASE,
)


def parse_semantic_annotation_wire(
    raw_text: str,
    anchor_texts: Mapping[str, str],
) -> SemanticAnnotationWireParseResult:
    """Parse independent term blocks and reject each invalid item separately."""
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    proposals: list[SemanticAnnotationProposalV1] = []
    rejections: list[tuple[str, str]] = []
    current: dict[str, str] | None = None
    item_number = 0
    recognized = False
    explicit_empty = False

    def finish() -> None:
        nonlocal current, item_number
        if current is None:
            return
        item_number += 1
        item_id = current.get("id", f"A{item_number:03d}")
        text = current.get("term", "").strip()
        role_text = current.get("role", "").strip().casefold()
        anchor = _ANCHOR_MARKER.sub(r"\1", current.get("anchor", "").strip()).strip()
        if not text or not role_text or not anchor:
            rejections.append((item_id, "semantic_annotation_missing_field"))
            current = None
            return
        try:
            role = SemanticRole(role_text)
        except ValueError:
            rejections.append((item_id, "semantic_annotation_role_unknown"))
            current = None
            return
        if role is SemanticRole.TEXT:
            rejections.append((item_id, "semantic_annotation_role_unknown"))
            current = None
            return
        anchored_text = anchor_texts.get(anchor)
        if anchored_text is None:
            rejections.append((item_id, "semantic_annotation_anchor_unknown"))
            current = None
            return
        if text not in anchored_text:
            rejections.append((item_id, "semantic_annotation_text_not_found"))
            current = None
            return
        try:
            proposals.append(SemanticAnnotationProposalV1(anchor, role, text))
        except (TypeError, ValueError):
            rejections.append((item_id, "semantic_annotation_item_invalid"))
        current = None

    for raw_line in sanitized.splitlines():
        line = _enrichment_wire_line(raw_line)
        if not line or _ENRICHMENT_FENCE.match(raw_line):
            continue
        if line.casefold().rstrip(".! ") in {"no annotations", "no semantic annotations"}:
            finish()
            recognized = True
            explicit_empty = True
            continue
        wrapped = _ENRICHMENT_BLOCK_WRAPPER.fullmatch(raw_line.strip())
        header_value = _enrichment_wire_line(wrapped.group(1)) if wrapped else line
        header = _SEMANTIC_ANNOTATION_BLOCK.fullmatch(header_value)
        if header is not None:
            finish()
            current = {"id": header.group(1) or f"A{item_number + 1:03d}"}
            recognized = True
            continue
        if re.fullmatch(r"END(?:\s+(?:TERM|ANNOTATION))?", line, re.IGNORECASE):
            finish()
            recognized = True
            continue
        match = _SEMANTIC_ANNOTATION_FIELD.fullmatch(line)
        if match is None:
            if current is not None:
                rejections.append(
                    (current.get("id", "unknown"), "semantic_annotation_line_invalid")
                )
                current = None
            continue
        recognized = True
        field_name = re.sub(r"[_\s]+", " ", match.group(1).casefold())
        field_key = {
            "term": "term",
            "exact text": "term",
            "text": "term",
            "role": "role",
            "paragraph anchor": "anchor",
            "anchor": "anchor",
        }[field_name]
        if field_key == "term" and current is not None and current.get("term"):
            finish()
        if current is None:
            current = {}
        if field_key in current:
            rejections.append((current.get("id", "unknown"), "semantic_annotation_duplicate_field"))
            current = None
            continue
        current[field_key] = match.group(2).strip()
    finish()
    if not recognized:
        return SemanticAnnotationWireParseResult(
            tuple(proposals), tuple(rejections), "semantic_annotation_unrecognized_response"
        )
    if not proposals and not rejections and not explicit_empty:
        return SemanticAnnotationWireParseResult(
            tuple(proposals), tuple(rejections), "semantic_annotation_no_items"
        )
    return SemanticAnnotationWireParseResult(tuple(proposals), tuple(rejections))


def semantic_annotation_input_hash(
    *,
    anchor_texts: Mapping[str, str],
    access_policy_hash: str,
) -> str:
    if _SHA256_RE.fullmatch(access_policy_hash) is None:
        raise ValueError("Semantic annotation request hashes must be lowercase SHA-256")
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "anchors": dict(sorted(anchor_texts.items())),
                "access_policy_hash": access_policy_hash,
                "prompt_version": SEMANTIC_ANNOTATION_PROMPT_VERSION,
                "contract_version": SEMANTIC_ANNOTATION_CONTRACT_VERSION,
                "parser_version": SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
                "policy_version": SEMANTIC_ANNOTATION_POLICY_VERSION,
            }
        )
    ).hexdigest()


def build_semantic_annotation_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    access_policy: SynthesisAccessPolicyV1,
    anchor_texts: Mapping[str, str],
    *,
    enrichment_input_hash: str,
    attempt: int = 0,
    repair_items: tuple[str, ...] = (),
) -> ModelRequest:
    if run.id != snapshot.production_run_id or run.subject_id != snapshot.subject_id:
        raise ValueError("Semantic annotation request identities differ")
    if attempt not in {0, 1}:
        raise ValueError("Semantic annotation permits only one bounded retry")
    if not anchor_texts:
        raise ValueError("Semantic annotation requires anchored publication text")
    access_hash = synthesis_access_policy_hash(access_policy)
    annotation_hash = semantic_annotation_input_hash(
        anchor_texts=anchor_texts,
        access_policy_hash=access_hash,
    )
    anchor_payload_hash = hashlib.sha256(
        _canonical_json_bytes(dict(sorted(anchor_texts.items())))
    ).hexdigest()
    evidence_hash = hashlib.sha256(
        _canonical_json_bytes(
            {
                "semantic_annotation_input_hash": annotation_hash,
                "anchor_payload_hash": anchor_payload_hash,
            }
        )
    ).hexdigest()
    prompt_parts = [
        "Identify exact semantic terms in the final publication anchors below.",
        "Do not add facts or do research. Return term blocks only.",
        SEMANTIC_ANNOTATION_ROLE_GUIDANCE,
        SEMANTIC_ANNOTATION_OUTPUT_CONTRACT,
    ]
    if attempt:
        prompt_parts.extend(
            (
                "BOUNDED FORMAT REPAIR: correct only the invalid items below. Use the same",
                "anchored publication text. Do not introduce any new term or fact.",
                "INVALID ITEMS:",
                *repair_items,
            )
        )
    prompt_parts.append("FINAL PUBLICATION TEXT ANCHORS")
    for anchor, text in sorted(anchor_texts.items()):
        prompt_parts.extend((f"@@ANCHOR {anchor}@@", text, "@@END ANCHOR@@"))
    request_identity = hashlib.sha256(
        _canonical_json_bytes(
            {
                "annotation_input_hash": annotation_hash,
                "attempt": attempt,
                "repair_items": repair_items,
            }
        )
    ).hexdigest()
    request_id = uuid5(
        NAMESPACE_URL,
        ":".join(
            (
                "production-semantic-annotation-v1",
                str(run.id),
                str(run.pipeline_generation),
                request_identity,
            )
        ),
    )
    return ModelRequest(
        text="\n\n".join(prompt_parts),
        prompt_template_id="production-semantic-annotation",
        prompt_template_version=SEMANTIC_ANNOTATION_PROMPT_VERSION,
        evidence_pack_hash=evidence_hash,
        external_llm_allowed=access_policy.external_llm_allowed and not access_policy.do_not_submit,
        routing_hint=ModelRoutingHint.EDITORIAL_ENRICHMENT,
        sensitivity=access_policy.effective_tlp.value,
        web_search=False,
        background=False,
        conversation=None,
        run_id=request_id,
        allow_failed_resubmit=True,
        metadata={
            "editorial_enrichment_input_hash": enrichment_input_hash,
            "semantic_annotation_input_hash": annotation_hash,
            "semantic_annotation_anchor_hash": anchor_payload_hash,
            "semantic_annotation_access_policy_hash": access_hash,
            "semantic_annotation_contract_version": SEMANTIC_ANNOTATION_CONTRACT_VERSION,
            "semantic_annotation_parser_version": SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
            "semantic_annotation_attempt": attempt,
        },
        parameters={
            "contract_version": SEMANTIC_ANNOTATION_CONTRACT_VERSION,
            "parser_version": SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
            "attempt": attempt,
        },
    )


def _parse_annotation_wire_block(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
) -> AnnotationProposalV1 | None:
    def reject(reason: str) -> None:
        rejections.append(
            _enrichment_wire_rejection(block, block.block_id, reason, block.raw_lines)
        )

    allowed = {"role", "paragraph_anchor", "text"}
    if set(block.fields) - allowed:
        reject("editorial_enrichment_unknown_field")
        return None
    raw_role = _wire_scalar(block, "role")
    anchor = _wire_scalar(block, "paragraph_anchor")
    text = _wire_scalar(block, "text")
    if not raw_role or not anchor or not text:
        reject("editorial_enrichment_annotation_missing_field")
        return None
    try:
        role = SemanticRole(raw_role.strip().casefold())
    except ValueError:
        reject("editorial_enrichment_annotation_category_unknown")
        return None
    if role is SemanticRole.TEXT:
        reject("editorial_enrichment_annotation_category_unknown")
        return None
    anchored_text = _synthesis_anchor_texts(evidence_pack).get(anchor)
    if anchored_text is None:
        reject("editorial_enrichment_annotation_anchor_unknown")
        return None
    if text not in anchored_text:
        reject("editorial_enrichment_annotation_text_not_found")
        return None
    try:
        return AnnotationProposalV1(role=role, paragraph_anchor=anchor, text=text)
    except (TypeError, ValueError, ValidationError):
        reject("editorial_enrichment_annotation_invalid")
        return None


def _parse_figure_wire_block(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    catalog_by_handle: Mapping[str, EditorialFigureCatalogEntry],
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> FigureProposalV1 | None:
    def reject(reason: str) -> None:
        rejections.append(
            _enrichment_wire_rejection(block, block.block_id, reason, block.raw_lines)
        )

    if block.error_code is not None:
        reject(block.error_code)
        return None
    handle = _wire_scalar(block, "figure_handle")
    if not handle:
        reject("editorial_enrichment_figure_handle_missing")
        return None
    catalog_entry = catalog_by_handle.get(handle.strip())
    if catalog_entry is None:
        reject("editorial_enrichment_unknown_figure_handle")
        return None
    if catalog_entry.figure.decision is SourceFigureDecision.REJECTED:
        reject("editorial_enrichment_figure_excluded_by_rule")
        return None
    if catalog_entry.figure.decision is not SourceFigureDecision.ACCEPTED:
        reject("editorial_enrichment_figure_not_archived")
        return None
    raw_handles = _wire_scalar(block, "evidence_handles")
    evidence_handles = _wire_handles(raw_handles)
    if evidence_handles is None:
        reject("editorial_enrichment_figure_evidence_missing")
        return None
    try:
        refs = _all_refs_for_handles(evidence_handles, evidence_pack)
    except EditorialEnrichmentProposalControlError:
        reject("editorial_enrichment_unknown_evidence")
        return None
    if not refs or any(
        ref.source_document_id != catalog_entry.figure.source_document_id for ref in refs
    ):
        reject("editorial_enrichment_figure_evidence_source_mismatch")
        return None
    purpose = _parse_analytic_purpose(block, evidence_pack, rejections)
    if purpose is None:
        return None
    purpose = _trim_analytic_purpose_evidence(
        block, purpose, set(evidence_handles), rejections, warnings
    )
    if purpose is None:
        return None
    raw_placement = _wire_scalar(block, "placement")
    placement = _wire_placement(block)
    if raw_placement is None:
        reject("editorial_enrichment_figure_placement_missing")
        return None
    if placement is None:
        reject("editorial_enrichment_figure_placement_invalid")
        return None
    if placement.kind is EnrichmentPlacementKind.AFTER_SECTION and (
        placement.section_index is None
        or placement.section_index >= len(evidence_pack.current_synthesis.get("sections", ()))
    ):
        reject("editorial_enrichment_figure_placement_anchor_unknown")
        return None
    caption = _wire_caption(block)
    reason = _wire_scalar(block, "reason")
    if reason is None or not reason.strip():
        reject("editorial_enrichment_figure_reason_missing")
        return None
    if caption is None:
        caption = catalog_entry.source_caption
    if caption is None or not caption.strip():
        reject("editorial_enrichment_figure_caption_missing")
        return None
    try:
        return FigureProposalV1(
            figure_handle=handle.strip(),
            caption=caption.strip(),
            evidence_handles=evidence_handles,
            reason=reason.strip(),
            placement=placement,
            purpose=purpose,
        )
    except (TypeError, ValueError, ValidationError):
        reject("editorial_enrichment_figure_invalid")
        return None


def _parse_resource_need_wire_block(
    block: _EditorialEnrichmentWireBlock,
    rejections: list[EditorialEnrichmentWireRejection],
) -> ResourceNeedProposalV1 | None:
    def reject(reason: str) -> None:
        rejections.append(
            _enrichment_wire_rejection(block, block.block_id, reason, block.raw_lines)
        )

    if block.error_code is not None:
        reject(block.error_code)
        return None
    raw_kind = _wire_scalar(block, "kind")
    kind = next(
        (
            item
            for item in ResourceNeedKind
            if raw_kind and item.value.casefold() == raw_kind.strip().casefold()
        ),
        None,
    )
    reason = _wire_scalar(block, "reason")
    query_hint = _wire_scalar(block, "query_hint")
    try:
        return ResourceNeedProposalV1(
            key=block.block_id,
            kind=kind,
            reason=(reason or "").strip(),
            query_hint=(query_hint or "").strip(),
        )
    except (TypeError, ValueError, ValidationError):
        reject("editorial_enrichment_resource_need_invalid")
        return None


_ANALYTIC_TOKEN = re.compile(r"[^\W_]+(?:_[^\W_]+)*", re.UNICODE)
_ANALYTIC_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "and",
        "or",
        "to",
        "in",
        "on",
        "de",
        "du",
        "des",
        "le",
        "la",
        "les",
        "et",
        "un",
        "une",
        "au",
        "aux",
    }
)


def _analytic_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return frozenset(_ANALYTIC_TOKEN.findall(normalized))


def _endpoint_tokens(label: str) -> frozenset[str]:
    return frozenset(
        token for token in _analytic_tokens(label) if token not in _ANALYTIC_STOP_WORDS
    )


def _record_mentions(record: Mapping[str, Any], label: str) -> bool:
    terms = _endpoint_tokens(label)
    return bool(terms) and terms <= _analytic_tokens(" ".join(_string_values(record)))


# Soft lexical grounding. A diagram label is written by the model in its own words, in
# the publication language, while a record mixes translated fields and a verbatim
# source-language quote. Matching is therefore tolerant: accents are ignored, long words
# compare by stem, and a label is covered when enough of its words are, not all of them.
_SOFT_STOP_WORDS = frozenset(
    "se par que qui dans pour sur avec est sont ses "
    "is by are with that from this its as at it".split()
)
_SOFT_STEM_LENGTH = 5
_SOFT_COVERAGE_THRESHOLD = 0.5


def _soft_tokens(value: str) -> frozenset[str]:
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return frozenset(
        token[:_SOFT_STEM_LENGTH]
        for token in _ANALYTIC_TOKEN.findall(plain)
        if token not in _SOFT_STOP_WORDS
        and token not in _ANALYTIC_STOP_WORDS
        and (len(token) > 2 or any(char.isdigit() for char in token))
    )


def _record_covers(record: Mapping[str, Any], label: str) -> bool:
    """True when most of a label's words (by stem) appear in a record, in any language."""
    terms = _soft_tokens(label)
    if not terms:
        return False
    present = _soft_tokens(" ".join(_string_values(record)))
    return len(terms & present) / len(terms) >= _SOFT_COVERAGE_THRESHOLD


# Fallback only, for records with no translated field to compare their quote against.
_ENGLISH_FUNCTION_WORDS = frozenset(
    "the of and to is are that with from by which this was it its into what for in their "
    "has have been be other once not can will an".split()
)


def _record_is_foreign_to(record: Mapping[str, Any], publication_language: str) -> bool:
    """True when a record is written in a language other than the publication's.

    Extraction writes ``value``, ``text`` and ``context`` in the publication language and
    keeps ``evidence`` verbatim, so a quote sharing almost no stem with them is in another
    language: deduced from the data, whatever the language pair. Only a record lacking
    that split falls back on an English function-word profile.
    """
    if publication_language.casefold().startswith("en"):
        return False
    quote = _soft_tokens(str(record.get("evidence") or ""))
    translated_fields = (
        ("value", "context") if record.get("kind") == "fact" else ("text", "context")
    )
    translated = _soft_tokens(" ".join(str(record.get(key) or "") for key in translated_fields))
    if quote and translated:
        return len(translated & quote) / len(translated) < _SOFT_COVERAGE_THRESHOLD
    tokens = _analytic_tokens(" ".join(_string_values(record)))
    return len(tokens & _ENGLISH_FUNCTION_WORDS) >= 3


def _synthesis_sentences(pack: EditorialEnrichmentEvidencePackV1) -> tuple[str, ...]:
    current = pack.current_synthesis
    paragraphs: list[Mapping[str, Any]] = []
    for value in current.get("lead", ()):
        if isinstance(value, Mapping):
            paragraphs.append(value)
    for section in current.get("sections", ()):
        if isinstance(section, Mapping):
            paragraphs.extend(
                item for item in section.get("paragraphs", ()) if isinstance(item, Mapping)
            )
    sentences: list[str] = []
    for paragraph in paragraphs:
        value = paragraph.get("text")
        if isinstance(value, str):
            sentences.extend(
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", value)
                if sentence.strip()
            )
    return tuple(sentences)


def _token_dice(left: str, right: str) -> float:
    left_tokens = _analytic_tokens(left)
    right_tokens = _analytic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return 2 * len(left_tokens & right_tokens) / (len(left_tokens) + len(right_tokens))


def _reject_paraphrase_rows(
    block: _EditorialEnrichmentWireBlock,
    table: TableProposalV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> TableProposalV1 | None:
    sentences = _synthesis_sentences(evidence_pack)
    if not sentences:
        return table
    kept_rows: list[TableRowProposalV1] = []
    duplicate_count = 0
    for row in table.rows:
        row_text = " ".join(row.cells)
        if any(
            _token_dice(row_text, sentence) >= TABLE_PARAPHRASE_TOKEN_DICE_THRESHOLD
            for sentence in sentences
        ):
            duplicate_count += 1
            rejections.append(
                _enrichment_wire_rejection(
                    block,
                    f"{block.block_id}:row:{duplicate_count:03d}",
                    "editorial_enrichment_table_row_paraphrase_only",
                    block.raw_lines,
                )
            )
        else:
            kept_rows.append(row)
    if not kept_rows:
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_table_paraphrase_only",
                block.raw_lines,
            )
        )
        return None
    purpose = _trim_analytic_purpose_evidence(
        block,
        table.purpose,
        {handle for row in kept_rows for handle in row.evidence_handles},
        rejections,
        warnings,
    )
    if purpose is None:
        return None
    if duplicate_count:
        return table.model_copy(update={"rows": tuple(kept_rows), "purpose": purpose})
    if purpose != table.purpose:
        return table.model_copy(update={"purpose": purpose})
    return table


def _edge_projection_rejection(
    diagram: DiagramProposalV1,
    edge: DiagramEdgeProposalV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> str | None:
    node_by_id = {item.node_id: item for item in diagram.nodes}
    source_label = node_by_id[edge.source_node_id].label
    target_label = node_by_id[edge.target_node_id].label
    records_by_handle = {
        str(item.get("handle")): item
        for item in (*evidence_pack.narrative_evidence, *evidence_pack.technical_evidence)
        if isinstance(item.get("handle"), str)
    }
    edge_records = [
        records_by_handle[handle] for handle in edge.evidence_handles if handle in records_by_handle
    ]
    # Evidence is quoted in the source language while labels are written in the
    # publication language, so a lexical match alone rejects translated diagrams. For
    # such a foreign record an endpoint or relation is also supported when its
    # node cites a handle the edge cites too: the model attests the grounding explicitly.
    foreign = {
        handle
        for handle, item in records_by_handle.items()
        if _record_is_foreign_to(item, evidence_pack.publication_language)
    }
    edge_handles = set(edge.evidence_handles) & foreign
    source_cited = edge_handles & set(node_by_id[edge.source_node_id].evidence_handles)
    target_cited = edge_handles & set(node_by_id[edge.target_node_id].evidence_handles)
    source_supported = bool(source_cited) or any(
        _record_covers(item, source_label) for item in edge_records
    )
    target_supported = bool(target_cited) or any(
        _record_covers(item, target_label) for item in edge_records
    )
    shared_cited = source_cited & target_cited
    same_record_support = bool(shared_cited) or any(
        _record_covers(item, source_label) and _record_covers(item, target_label)
        for item in edge_records
    )
    label = edge.label or ""
    relation_text_support = bool(_soft_tokens(label)) and (
        bool(shared_cited)
        or any(
            _record_covers(item, source_label)
            and _record_covers(item, target_label)
            and _record_covers(item, label)
            for item in edge_records
        )
    )
    if not source_supported or not target_supported:
        return "editorial_enrichment_diagram_relation_endpoint_unsupported"
    if edge.relation_type is DiagramRelationType.COMPARISON:
        if diagram.kind is EnrichmentDiagramKind.INFECTION_CHAIN:
            return "editorial_enrichment_comparison_cannot_be_infection_chain"
        if edge.label is None or "comparison" not in _analytic_tokens(edge.label):
            return "editorial_enrichment_comparison_relation_not_labelled"
    if diagram.kind is EnrichmentDiagramKind.INFECTION_CHAIN and (
        edge.relation_type is not DiagramRelationType.FACTUAL or not relation_text_support
    ):
        return "editorial_enrichment_infection_chain_sequence_not_documented"
    if edge.relation_type is DiagramRelationType.FACTUAL and not same_record_support:
        return "editorial_enrichment_diagram_relation_without_endpoint_support"
    if edge.relation_type is DiagramRelationType.FACTUAL and not relation_text_support:
        return "editorial_enrichment_diagram_relation_text_not_supported"
    if edge.relation_type is not DiagramRelationType.COMPARISON:
        source_node_source_ids = {
            evidence_pack._handle_to_ref[handle].source_document_id
            for handle in node_by_id[edge.source_node_id].evidence_handles
            if handle in evidence_pack._handle_to_ref
        }
        target_node_source_ids = {
            evidence_pack._handle_to_ref[handle].source_document_id
            for handle in node_by_id[edge.target_node_id].evidence_handles
            if handle in evidence_pack._handle_to_ref
        }
        counter_records = [
            item
            for item in evidence_pack.reserve_evidence
            if item.get("projection_classification")
            == RelevanceClassification.COUNTER_INDICATION.value
        ]
        if any(
            _record_mentions(item, source_label) and _record_mentions(item, target_label)
            for item in counter_records
        ):
            return "editorial_enrichment_relation_counter_indicated"

        for relation in evidence_pack.source_pair_relations:
            if relation.get("relation") != "link_not_demonstrated":
                continue
            reserve_handles = relation.get("supporting_handles", ())
            reserve_refs = {
                evidence_pack._reserve_handle_to_ref[handle]
                for handle in reserve_handles
                if isinstance(handle, str) and handle in evidence_pack._reserve_handle_to_ref
            }
            if not reserve_refs:
                continue
            pair_source_ids = {ref.source_document_id for ref in reserve_refs}
            edge_source_ids = {
                evidence_pack._handle_to_ref[handle].source_document_id
                for handle in edge.evidence_handles
                if handle in evidence_pack._handle_to_ref
            }
            related_reserve_records = [
                item
                for item in evidence_pack.reserve_evidence
                if isinstance(item.get("handle"), str) and item.get("handle") in reserve_handles
            ]
            endpoints_appear_in_pair = any(
                _record_mentions(item, source_label) for item in related_reserve_records
            ) and any(_record_mentions(item, target_label) for item in related_reserve_records)
            spans_sources_in_pair = any(
                source_id != target_id
                and source_id in pair_source_ids
                and target_id in pair_source_ids
                for source_id in source_node_source_ids
                for target_id in target_node_source_ids
            )
            if (
                endpoints_appear_in_pair
                and spans_sources_in_pair
                and edge_source_ids & pair_source_ids
            ):
                return "editorial_enrichment_relation_contradicts_projection"
    return None


def _filter_diagram_relations(
    block: _EditorialEnrichmentWireBlock,
    diagram: DiagramProposalV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> DiagramProposalV1 | None:
    valid_edges: list[DiagramEdgeProposalV1] = []
    rejected_count = 0
    for edge in diagram.edges:
        reason = _edge_projection_rejection(diagram, edge, evidence_pack)
        if reason is None:
            valid_edges.append(edge)
            continue
        rejected_count += 1
        rejections.append(
            _enrichment_wire_rejection(
                block,
                f"{block.block_id}:relation:{rejected_count:03d}",
                reason,
                block.raw_lines,
            )
        )
    if not valid_edges:
        rejections.append(
            _enrichment_wire_rejection(
                block,
                block.block_id,
                "editorial_enrichment_diagram_has_no_supported_relations",
                block.raw_lines,
            )
        )
        return None
    valid_handles = {handle for edge in valid_edges for handle in edge.evidence_handles}
    valid_handles.update(handle for node in diagram.nodes for handle in node.evidence_handles)
    purpose = _trim_analytic_purpose_evidence(
        block, diagram.purpose, valid_handles, rejections, warnings
    )
    if purpose is None:
        return None
    return diagram.model_copy(update={"edges": tuple(valid_edges), "purpose": purpose})


def parse_editorial_enrichment_proposal_wire(
    raw_text: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    *,
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = (),
) -> EditorialEnrichmentWireParseResult:
    """Recover independent text blocks, leaving canonical validation strict."""
    if not isinstance(raw_text, str):
        return EditorialEnrichmentWireParseResult(
            None, error_code="editorial_enrichment_unintelligible_response"
        )
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    transformations: list[str] = []
    warnings: list[str] = []
    if sanitized != raw_text.replace("\r\n", "\n").replace("\r", "\n"):
        transformations.append("bridge_ui_markers_removed")
    top_blocks: list[_EditorialEnrichmentWireBlock] = []
    rejections: list[EditorialEnrichmentWireRejection] = []
    current_top: _EditorialEnrichmentWireBlock | None = None
    current_child: _EditorialEnrichmentWireBlock | None = None
    last_field: tuple[_EditorialEnrichmentWireBlock, str] | None = None
    local_ids: set[tuple[str, str]] = set()
    sequences: dict[tuple[str, str], int] = defaultdict(int)
    block_sequence = 0
    recognized = False
    explicit_empty = False

    def reject(block: _EditorialEnrichmentWireBlock, reason: str) -> None:
        rejections.append(
            _enrichment_wire_rejection(block, block.block_id, reason, block.raw_lines)
        )

    def finish_child() -> None:
        nonlocal current_child, last_field
        child = current_child
        current_child = None
        last_field = None
        if child is None:
            return
        if current_top is None:
            reject(child, "editorial_enrichment_orphan_block")
            return
        if _ENRICHMENT_CHILD_PARENT.get(child.kind) != current_top.kind:
            reject(child, "editorial_enrichment_unexpected_block_kind")
            return
        current_top.children.append(child)
        current_top.raw_lines.extend(child.raw_lines)

    def finish_top() -> None:
        nonlocal current_top, last_field
        finish_child()
        if current_top is not None:
            top_blocks.append(current_top)
        current_top = None
        last_field = None

    def new_block(kind: str, local_id: str | None, raw_line: str) -> _EditorialEnrichmentWireBlock:
        nonlocal block_sequence
        prefix = {
            "TABLE": "T",
            "CHART": "C",
            "DIAGRAM": "D",
            "ANNOTATION": "A",
            "FIGURE": "P",
            "NEEDS": "N",
            "COLUMN": "C",
            "ROW": "R",
            "POINT": "P",
            "NODE": "N",
            "RELATION": "L",
            "EDGE": "L",
            "GROUP": "G",
        }[kind]
        child = kind in _ENRICHMENT_CHILD_KINDS
        parent_scope_id = current_top.scope_id if child and current_top is not None else None
        namespace = parent_scope_id or "<top-level>"
        if local_id is None:
            sequence_key = (namespace, prefix)
            while True:
                sequences[sequence_key] += 1
                generated_id = f"{prefix}{sequences[sequence_key]:03d}"
                if (namespace, generated_id) not in local_ids:
                    local_id = generated_id
                    break
        block_id = local_id
        assert block_id is not None
        error_code = None
        if (namespace, block_id) in local_ids:
            error_code = "editorial_enrichment_duplicate_local_block_id"
        else:
            local_ids.add((namespace, block_id))
        block_sequence += 1
        return _EditorialEnrichmentWireBlock(
            kind=kind,
            block_id=block_id,
            scope_id=f"B{block_sequence:06d}",
            parent_scope_id=parent_scope_id,
            raw_lines=[raw_line],
            error_code=error_code,
        )

    for line_number, raw_line in enumerate(sanitized.splitlines(), start=1):
        line = _enrichment_wire_line(raw_line)
        if not line or _ENRICHMENT_FENCE.match(raw_line):
            if _ENRICHMENT_FENCE.match(raw_line):
                if "markdown_fences_removed" not in transformations:
                    transformations.append("markdown_fences_removed")
            continue
        empty_marker = re.sub(r"[.!]+$", "", line).strip().casefold()
        if empty_marker in {"no useful enrichment", "no enrichment", "empty"}:
            finish_top()
            recognized = True
            if explicit_empty or top_blocks:
                marker = _EditorialEnrichmentWireBlock(
                    kind="EMPTY", block_id=f"EMPTY-{line_number}", raw_lines=[raw_line]
                )
                reject(marker, "editorial_enrichment_empty_marker_conflict")
            else:
                explicit_empty = True
            continue

        wrapped = _ENRICHMENT_BLOCK_WRAPPER.fullmatch(raw_line.strip())
        header_value = _enrichment_wire_line(wrapped.group(1)) if wrapped else line
        header = _ENRICHMENT_HEADER.fullmatch(header_value)
        if header is not None:
            kind, local_id = header.groups()
            kind = kind.upper()
            recognized = True
            if explicit_empty:
                marker = _EditorialEnrichmentWireBlock(
                    kind="EMPTY", block_id=f"EMPTY-{line_number}", raw_lines=[raw_line]
                )
                reject(marker, "editorial_enrichment_empty_marker_conflict")
                explicit_empty = False
            if kind in {"TABLE", "CHART", "DIAGRAM", "ANNOTATION", "FIGURE", "NEEDS"}:
                finish_top()
                current_top = new_block(kind, local_id, raw_line)
            else:
                finish_child()
                current_child = new_block(kind, local_id, raw_line)
                if current_top is None:
                    reject(current_child, "editorial_enrichment_orphan_block")
                    current_child = None
                elif _ENRICHMENT_CHILD_PARENT.get(kind) != current_top.kind:
                    reject(current_child, "editorial_enrichment_unexpected_block_kind")
                    current_child = None
            last_field = None
            continue

        end_match = re.fullmatch(
            r"END(?:\s+(TABLE|CHART|DIAGRAM|ANNOTATION|FIGURE|NEEDS|COLUMN|ROW|POINT|NODE|RELATION|EDGE|GROUP|ITEM))?",
            line,
            re.I,
        )
        if end_match is not None:
            recognized = True
            last_field = None
            end_kind = (end_match.group(1) or "").upper()
            if current_top is not None and end_kind == current_top.kind:
                finish_top()
            elif current_child is not None:
                finish_child()
            elif current_top is not None:
                finish_top()
            continue

        target = current_child or current_top
        field_line = re.sub(r"^\*\*(.+?)\*\*\s*:", r"\1:", line)
        field_match = _ENRICHMENT_FIELD.fullmatch(field_line)
        if field_match is None:
            # Also accept common bare labels (for example ``TITLE text``).
            bare_match = re.match(
                r"^(EVIDENCE\s+HANDLES?|SECTION\s+INDEX|NODE\s+IDS|SOURCE\s+NODE\s+ID|"
                r"TARGET\s+NODE\s+ID|GROUP\s+ID|NODE\s+ID|COLUMN\s+KEY|"
                r"PARAGRAPH\s+ANCHOR|EXACT\s+TEXT|FIGURE\s+HANDLE|NEED\s+KEY|QUERY\s+HINT|"
                r"DATE|SERIES|"
                r"CATEGORY|ROLE|ANCHOR|SEGMENT|TEXT|"
                r"KEY|KIND|TITLE|CAPTION|PLACEMENT|LABEL|CELL|HANDLES|DIRECTION|ID|"
                r"FROM|TO|SOURCE|TARGET|NODES)\s+(.+)$",
                line,
                re.I,
            )
            field_name, field_value = (
                bare_match.groups() if bare_match is not None else (None, None)
            )
        else:
            field_name, field_value = field_match.groups()
        if target is None:
            continue
        if field_name is None or field_value is None:
            if last_field is not None:
                owner, name = last_field
                current_values = owner.fields[name]
                current_values[-1] = (
                    f"{current_values[-1]}\n{raw_line.strip()}"
                    if current_values[-1]
                    else raw_line.strip()
                )
                owner.raw_lines.append(raw_line)
            else:
                target.error_code = target.error_code or "editorial_enrichment_unrecognized_line"
                target.raw_lines.append(raw_line)
            continue
        recognized = True
        alias = re.sub(r"[\s_-]+", " ", field_name.strip().casefold())
        canonical = _ENRICHMENT_FIELD_ALIASES.get(alias)
        if canonical is None:
            target.error_code = target.error_code or "editorial_enrichment_unknown_field"
            target.raw_lines.append(raw_line)
            last_field = None
            continue
        allowed = {
            "TABLE": {
                "key",
                "kind",
                "title",
                "caption",
                "placement",
                "section_index",
                "purpose",
                "available_data",
                "comprehension_gain",
                "scope",
                "purpose_evidence_handles",
                "knowledge_limits",
                "placement_reason",
            },
            "CHART": {
                "key",
                "kind",
                "title",
                "caption",
                "placement",
                "section_index",
                "purpose",
                "available_data",
                "comprehension_gain",
                "scope",
                "purpose_evidence_handles",
                "knowledge_limits",
                "placement_reason",
            },
            "DIAGRAM": {
                "key",
                "kind",
                "title",
                "caption",
                "profile",
                "placement",
                "section_index",
                "direction",
                "purpose",
                "available_data",
                "comprehension_gain",
                "scope",
                "purpose_evidence_handles",
                "knowledge_limits",
                "placement_reason",
            },
            "COLUMN": {"key", "label"},
            "ROW": {"cell", "evidence_handles"},
            "POINT": {"date", "label", "series", "evidence_handles"},
            "NODE": {"node_id", "id", "label", "role", "evidence_handles"},
            "RELATION": {
                "source_node_id",
                "target_node_id",
                "label",
                "relation_type",
                "direction",
                "evidence_handles",
            },
            "EDGE": {
                "source_node_id",
                "target_node_id",
                "label",
                "relation_type",
                "direction",
                "evidence_handles",
            },
            "GROUP": {"group_id", "id", "label", "node_ids"},
            "ANNOTATION": {"role", "paragraph_anchor", "text"},
            "FIGURE": {
                "figure_handle",
                "caption",
                "evidence_handles",
                "reason",
                "placement",
                "section_index",
                "purpose",
                "available_data",
                "comprehension_gain",
                "scope",
                "purpose_evidence_handles",
                "knowledge_limits",
                "placement_reason",
            },
            "NEEDS": {"kind", "reason", "query_hint"},
        }[target.kind]
        if canonical not in allowed:
            target.error_code = target.error_code or "editorial_enrichment_unknown_field"
            target.raw_lines.append(raw_line)
            last_field = None
            continue
        repeated = canonical == "cell"
        target.append_field(canonical, field_value, repeated=repeated)
        target.raw_lines.append(raw_line)
        last_field = (target, canonical)

    finish_top()
    if not recognized:
        return EditorialEnrichmentWireParseResult(
            None,
            tuple(rejections),
            error_code="editorial_enrichment_unintelligible_response",
            transformations=tuple(transformations),
            warnings=tuple(warnings),
        )
    if explicit_empty and not top_blocks and not rejections:
        return EditorialEnrichmentWireParseResult(
            proposal=EditorialEnrichmentProposalV1(),
            explicit_empty=True,
            transformations=tuple(transformations),
        )

    tables: list[TableProposalV1] = []
    charts: list[ChartProposalV1] = []
    diagrams: list[DiagramProposalV1] = []
    annotations: list[AnnotationProposalV1] = []
    figures: list[FigureProposalV1] = []
    resource_needs: list[ResourceNeedProposalV1] = []
    accepted_blocks: list[EditorialEnrichmentAcceptedBlock] = []
    rejected_blocks: list[EditorialEnrichmentRejectedBlock] = []
    canonical_keys: set[str] = set()
    analytic_questions: set[str] = set()
    source_figure_questions: dict[str, str] = {}
    catalog_by_handle = {entry.handle: entry for entry in figure_catalog}
    proposed_figure_handles: set[str] = set()
    proposed_need_keys: set[str] = set()
    # Source figures are considered first so an equivalent reconstructed form
    # cannot win just because the model emitted it earlier in the response.
    processing_blocks = sorted(
        top_blocks, key=lambda item: _editorial_enrichment_media_priority(item.kind)
    )
    for top in processing_blocks:
        rejection_start = len(rejections)
        accepted = False
        proposal_key = ""
        if top.error_code is not None:
            reject(top, top.error_code)
        elif top.kind == "TABLE":
            table = _parse_enrichment_wire_table(top, evidence_pack, rejections, warnings)
            if table is not None:
                table = _reject_paraphrase_rows(top, table, evidence_pack, rejections, warnings)
            if table is not None:
                question_key = normalize_analytic_question(table.purpose.question)
                preferred_figure = source_figure_questions.get(question_key)
                if preferred_figure is not None:
                    reject(top, "editorial_enrichment_source_figure_preferred_over_reconstruction")
                    warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{preferred_figure}:{top.kind}:{top.block_id}"
                    )
                elif not question_key or question_key in analytic_questions:
                    reject(top, "editorial_enrichment_duplicate_analytic_purpose")
                elif table.key in canonical_keys:
                    reject(top, "editorial_enrichment_duplicate_key")
                else:
                    canonical_keys.add(table.key)
                    analytic_questions.add(question_key)
                    tables.append(table)
                    accepted = True
                    proposal_key = table.key
        elif top.kind == "CHART":
            chart = _parse_enrichment_wire_chart(top, evidence_pack, rejections, warnings)
            if chart is not None:
                question_key = normalize_analytic_question(chart.purpose.question)
                preferred_figure = source_figure_questions.get(question_key)
                if preferred_figure is not None:
                    reject(top, "editorial_enrichment_source_figure_preferred_over_reconstruction")
                    warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{preferred_figure}:{top.kind}:{top.block_id}"
                    )
                elif not question_key or question_key in analytic_questions:
                    reject(top, "editorial_enrichment_duplicate_analytic_purpose")
                elif chart.key in canonical_keys:
                    reject(top, "editorial_enrichment_duplicate_key")
                else:
                    canonical_keys.add(chart.key)
                    analytic_questions.add(question_key)
                    charts.append(chart)
                    accepted = True
                    proposal_key = chart.key
        elif top.kind == "ANNOTATION":
            annotation = _parse_annotation_wire_block(top, evidence_pack, rejections)
            if annotation is not None:
                annotations.append(annotation)
                accepted = True
                proposal_key = f"{annotation.paragraph_anchor}:{annotation.text}"
        elif top.kind == "FIGURE":
            figure = _parse_figure_wire_block(
                top, evidence_pack, catalog_by_handle, rejections, warnings
            )
            if figure is not None:
                question_key = normalize_analytic_question(figure.purpose.question)
                if figure.figure_handle in proposed_figure_handles:
                    reject(top, "editorial_enrichment_duplicate_figure_handle")
                elif not question_key or question_key in analytic_questions:
                    reject(top, "editorial_enrichment_duplicate_analytic_purpose")
                elif len(figures) >= MAX_ENRICHMENT_FIGURE_PROPOSALS:
                    reject(top, "editorial_enrichment_figure_limit_exceeded")
                else:
                    proposed_figure_handles.add(figure.figure_handle)
                    figures.append(figure)
                    analytic_questions.add(question_key)
                    source_figure_questions[question_key] = figure.figure_handle
                    accepted = True
                    proposal_key = figure.figure_handle
        elif top.kind == "NEEDS":
            need = _parse_resource_need_wire_block(top, rejections)
            if need is not None:
                if need.key in proposed_need_keys:
                    reject(top, "editorial_enrichment_duplicate_resource_need")
                elif len(resource_needs) >= MAX_ENRICHMENT_RESOURCE_NEEDS:
                    reject(top, "editorial_enrichment_resource_need_limit_exceeded")
                else:
                    proposed_need_keys.add(need.key)
                    resource_needs.append(need)
                    accepted = True
                    proposal_key = need.key
        else:
            diagram = _parse_enrichment_wire_diagram(top, evidence_pack, rejections, warnings)
            if diagram is not None:
                diagram = _filter_diagram_relations(
                    top, diagram, evidence_pack, rejections, warnings
                )
            if diagram is not None:
                question_key = normalize_analytic_question(diagram.purpose.question)
                preferred_figure = source_figure_questions.get(question_key)
                if preferred_figure is not None:
                    reject(top, "editorial_enrichment_source_figure_preferred_over_reconstruction")
                    warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{preferred_figure}:{top.kind}:{top.block_id}"
                    )
                elif not question_key or question_key in analytic_questions:
                    reject(top, "editorial_enrichment_duplicate_analytic_purpose")
                elif diagram.key in canonical_keys:
                    reject(top, "editorial_enrichment_duplicate_key")
                else:
                    canonical_keys.add(diagram.key)
                    analytic_questions.add(question_key)
                    diagrams.append(diagram)
                    accepted = True
                    proposal_key = diagram.key
        if accepted:
            accepted_blocks.append(
                EditorialEnrichmentAcceptedBlock(top.kind, top.block_id, top.scope_id, proposal_key)
            )
        elif top.kind in {"TABLE", "CHART", "DIAGRAM", "FIGURE"}:
            block_rejections = tuple(
                dict.fromkeys(
                    item.reason_code
                    for item in rejections[rejection_start:]
                    if item.scope_id == top.scope_id or item.parent_scope_id == top.scope_id
                )
            )
            rejected_blocks.append(
                EditorialEnrichmentRejectedBlock(
                    kind=top.kind,
                    block_id=top.block_id,
                    scope_id=top.scope_id,
                    raw_text="\n".join(top.raw_lines),
                    raw_sha256=hashlib.sha256("\n".join(top.raw_lines).encode()).hexdigest(),
                    reason_codes=block_rejections,
                )
            )
    if not (tables or charts or diagrams or annotations or figures or resource_needs):
        return EditorialEnrichmentWireParseResult(
            None,
            tuple(rejections),
            error_code="editorial_enrichment_no_valid_blocks",
            transformations=tuple(transformations),
            warnings=tuple(dict.fromkeys(warnings)),
            rejected_blocks=tuple(rejected_blocks),
            accepted_blocks=tuple(accepted_blocks),
        )
    return EditorialEnrichmentWireParseResult(
        proposal=EditorialEnrichmentProposalV1(
            tables=tuple(tables),
            charts=tuple(charts),
            diagrams=tuple(diagrams),
            annotations=tuple(annotations),
            figures=tuple(figures),
            resource_needs=tuple(resource_needs),
        ),
        rejections=tuple(rejections),
        transformations=tuple(transformations),
        warnings=tuple(dict.fromkeys(warnings)),
        rejected_blocks=tuple(rejected_blocks),
        accepted_blocks=tuple(accepted_blocks),
    )


def _merge_repaired_editorial_enrichment_proposal(
    first_pass: EditorialEnrichmentWireParseResult,
    repair_pass: EditorialEnrichmentWireParseResult,
    repair_targets: tuple[EditorialEnrichmentRejectedBlock, ...],
) -> tuple[
    EditorialEnrichmentWireParseResult,
    tuple[tuple[str, str], ...],
    tuple[str, ...],
]:
    """Merge only validated replacements whose top-level wire identity was targeted."""
    empty = EditorialEnrichmentProposalV1()
    original = first_pass.proposal or empty
    repaired = repair_pass.proposal or empty
    target_by_identity = {(item.kind, item.block_id): item for item in repair_targets}
    target_counts: dict[tuple[str, str], int] = defaultdict(int)
    for target in repair_targets:
        target_counts[(target.kind, target.block_id)] += 1
    accepted_top_level_ids = {item.block_id for item in first_pass.accepted_blocks}
    repaired_by_identity: dict[tuple[str, str], Any] = {}
    unexpected: list[str] = []

    proposal_values: dict[str, dict[str, Any]] = {
        "TABLE": {item.key: item for item in repaired.tables},
        "CHART": {item.key: item for item in repaired.charts},
        "DIAGRAM": {item.key: item for item in repaired.diagrams},
        "FIGURE": {item.figure_handle: item for item in repaired.figures},
    }
    for block in repair_pass.accepted_blocks:
        identity = (block.kind, block.block_id)
        candidate: Any = proposal_values.get(block.kind, {}).get(block.proposal_key)
        if identity not in target_by_identity or candidate is None:
            unexpected.append(f"{block.kind}:{block.block_id}")
            continue
        if target_counts[identity] != 1 or block.block_id in accepted_top_level_ids:
            unexpected.append(f"{block.kind}:{block.block_id}:ambiguous_or_duplicate_id")
            continue
        repaired_by_identity[identity] = candidate

    tables = list(original.tables)
    charts = list(original.charts)
    diagrams = list(original.diagrams)
    figures = list(original.figures)
    keys = (
        {item.key for item in tables}
        | {item.key for item in charts}
        | {item.key for item in diagrams}
    )
    questions = (
        {normalize_analytic_question(item.purpose.question) for item in tables}
        | {normalize_analytic_question(item.purpose.question) for item in diagrams}
        | {normalize_analytic_question(item.purpose.question) for item in charts}
    )
    source_figure_questions = {
        normalize_analytic_question(item.purpose.question): item.figure_handle for item in figures
    }
    questions.update(source_figure_questions)
    figure_handles = {item.figure_handle for item in figures}
    used_top_level_ids = set(accepted_top_level_ids)
    new_rejections = list(first_pass.rejections)
    new_warnings = list(first_pass.warnings)
    discarded_accepted_blocks: set[tuple[str, str]] = set()
    repaired_identities: list[tuple[str, str]] = []
    repaired_blocks: list[EditorialEnrichmentAcceptedBlock] = []

    for target in sorted(repair_targets, key=lambda item: item.kind != "FIGURE"):
        identity = (target.kind, target.block_id)
        candidate = repaired_by_identity.get(identity)
        if candidate is None:
            continue
        reason: str | None = None
        if target.block_id in used_top_level_ids:
            reason = "editorial_enrichment_duplicate_local_block_id"
        if target.kind == "TABLE":
            assert isinstance(candidate, TableProposalV1)
            question = normalize_analytic_question(candidate.purpose.question)
            if reason is not None:
                pass
            elif candidate.key in keys:
                reason = "editorial_enrichment_duplicate_key"
            elif question in source_figure_questions:
                reason = "editorial_enrichment_source_figure_preferred_over_reconstruction"
                new_warnings.append(
                    "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                    f"{source_figure_questions[question]}:TABLE:{candidate.key}"
                )
            elif not question or question in questions:
                reason = "editorial_enrichment_duplicate_analytic_purpose"
            else:
                tables.append(candidate)
                keys.add(candidate.key)
                questions.add(question)
        elif target.kind == "DIAGRAM":
            assert isinstance(candidate, DiagramProposalV1)
            question = normalize_analytic_question(candidate.purpose.question)
            if reason is not None:
                pass
            elif candidate.key in keys:
                reason = "editorial_enrichment_duplicate_key"
            elif question in source_figure_questions:
                reason = "editorial_enrichment_source_figure_preferred_over_reconstruction"
                new_warnings.append(
                    "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                    f"{source_figure_questions[question]}:DIAGRAM:{candidate.key}"
                )
            elif not question or question in questions:
                reason = "editorial_enrichment_duplicate_analytic_purpose"
            else:
                diagrams.append(candidate)
                keys.add(candidate.key)
                questions.add(question)
        elif target.kind == "CHART":
            assert isinstance(candidate, ChartProposalV1)
            question = normalize_analytic_question(candidate.purpose.question)
            if reason is not None:
                pass
            elif candidate.key in keys:
                reason = "editorial_enrichment_duplicate_key"
            elif question in source_figure_questions:
                reason = "editorial_enrichment_source_figure_preferred_over_reconstruction"
                new_warnings.append(
                    "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                    f"{source_figure_questions[question]}:CHART:{candidate.key}"
                )
            elif not question or question in questions:
                reason = "editorial_enrichment_duplicate_analytic_purpose"
            else:
                charts.append(candidate)
                keys.add(candidate.key)
                questions.add(question)
        elif target.kind == "FIGURE":
            assert isinstance(candidate, FigureProposalV1)
            question = normalize_analytic_question(candidate.purpose.question)
            if reason is not None:
                pass
            elif candidate.figure_handle in figure_handles:
                reason = "editorial_enrichment_duplicate_figure_handle"
            elif not question:
                reason = "editorial_enrichment_analytic_purpose_invalid"
            elif question in source_figure_questions:
                reason = "editorial_enrichment_duplicate_analytic_purpose"
            else:
                # A figure made valid through repair retains priority over an
                # already accepted reconstruction of the same question.
                colliding_tables = [
                    table_item
                    for table_item in tables
                    if normalize_analytic_question(table_item.purpose.question) == question
                ]
                colliding_diagrams = [
                    diagram_item
                    for diagram_item in diagrams
                    if normalize_analytic_question(diagram_item.purpose.question) == question
                ]
                colliding_charts = [
                    chart_item
                    for chart_item in charts
                    if normalize_analytic_question(chart_item.purpose.question) == question
                ]
                for table_item in colliding_tables:
                    keys.discard(table_item.key)
                    new_warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{candidate.figure_handle}:TABLE:{table_item.key}"
                    )
                    discarded_accepted_blocks.update(
                        (block.kind, block.block_id)
                        for block in first_pass.accepted_blocks
                        if block.kind == "TABLE" and block.proposal_key == table_item.key
                    )
                for diagram_item in colliding_diagrams:
                    keys.discard(diagram_item.key)
                    new_warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{candidate.figure_handle}:DIAGRAM:{diagram_item.key}"
                    )
                for chart_item in colliding_charts:
                    keys.discard(chart_item.key)
                    new_warnings.append(
                        "editorial_enrichment_source_figure_preferred_over_reconstruction:"
                        f"{candidate.figure_handle}:CHART:{chart_item.key}"
                    )
                    discarded_accepted_blocks.update(
                        (block.kind, block.block_id)
                        for block in first_pass.accepted_blocks
                        if block.kind == "CHART" and block.proposal_key == chart_item.key
                    )
                    discarded_accepted_blocks.update(
                        (block.kind, block.block_id)
                        for block in first_pass.accepted_blocks
                        if block.kind == "DIAGRAM" and block.proposal_key == diagram_item.key
                    )
                if colliding_tables or colliding_diagrams or colliding_charts:
                    tables = [
                        table_item for table_item in tables if table_item not in colliding_tables
                    ]
                    diagrams = [
                        diagram_item
                        for diagram_item in diagrams
                        if diagram_item not in colliding_diagrams
                    ]
                    charts = [
                        chart_item for chart_item in charts if chart_item not in colliding_charts
                    ]
                    questions.discard(question)
                figures.append(candidate)
                figure_handles.add(candidate.figure_handle)
                source_figure_questions[question] = candidate.figure_handle
                questions.add(question)
        if reason is not None:
            new_rejections.append(
                EditorialEnrichmentWireRejection(
                    block_id=target.block_id,
                    reason_code=reason,
                    raw_sha256=target.raw_sha256,
                    scope_id=target.scope_id,
                )
            )
            continue
        repaired_identities.append(identity)
        used_top_level_ids.add(target.block_id)
        repaired_blocks.append(
            EditorialEnrichmentAcceptedBlock(
                kind=target.kind,
                block_id=target.block_id,
                scope_id=target.scope_id,
                proposal_key=(
                    candidate.figure_handle if target.kind == "FIGURE" else candidate.key
                ),
            )
        )

    repaired_identity_set = set(repaired_identities)
    remaining_rejections = tuple(
        item
        for item in first_pass.rejected_blocks
        if (item.kind, item.block_id) not in repaired_identity_set
    )
    proposal = EditorialEnrichmentProposalV1(
        tables=tuple(tables),
        charts=tuple(charts),
        diagrams=tuple(diagrams),
        annotations=original.annotations,
        figures=tuple(figures),
        resource_needs=original.resource_needs,
    )
    has_content = any(
        (
            proposal.tables,
            proposal.charts,
            proposal.diagrams,
            proposal.annotations,
            proposal.figures,
            proposal.resource_needs,
        )
    )
    transformations = first_pass.transformations
    warnings = tuple(dict.fromkeys(new_warnings))
    accepted_blocks = (
        *(
            block
            for block in first_pass.accepted_blocks
            if (block.kind, block.block_id) not in discarded_accepted_blocks
        ),
        *repaired_blocks,
    )
    if repaired_identities:
        transformations = (
            *transformations,
            *repair_pass.transformations,
            f"editorial_enrichment_repair_blocks_repaired:{len(repaired_identities)}",
        )
        warnings = (*warnings, *repair_pass.warnings)
    return (
        EditorialEnrichmentWireParseResult(
            proposal=proposal if has_content else None,
            rejections=tuple((*new_rejections, *repair_pass.rejections)),
            error_code=None if has_content else first_pass.error_code or repair_pass.error_code,
            explicit_empty=first_pass.explicit_empty,
            transformations=tuple(dict.fromkeys(transformations)),
            warnings=tuple(dict.fromkeys(warnings)),
            rejected_blocks=remaining_rejections,
            accepted_blocks=accepted_blocks,
        ),
        tuple(repaired_identities),
        tuple(unexpected),
    )


def parse_editorial_resource_proposals_wire(
    raw_text: str,
    needs: tuple[EditorialResourceNeedV1, ...],
) -> EditorialResourceWireParseResult:
    """Parse bounded candidate URLs from a separate, web-search-enabled call."""
    if not isinstance(raw_text, str):
        return EditorialResourceWireParseResult((), error_code="resource_response_unintelligible")
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    transformations: list[str] = []
    if sanitized != raw_text.replace("\r\n", "\n").replace("\r", "\n"):
        transformations.append("bridge_ui_markers_removed")
    need_keys = {item.key for item in needs}
    rejections: list[EditorialEnrichmentWireRejection] = []
    proposals: list[ResourceProposalV1] = []
    block: _EditorialEnrichmentWireBlock | None = None
    last_field: str | None = None
    recognized = False
    local_ids: set[str] = set()

    def reject(item: _EditorialEnrichmentWireBlock, reason: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, reason, item.raw_lines))

    def finish() -> None:
        nonlocal block, last_field
        current = block
        block = None
        last_field = None
        if current is None:
            return
        if current.error_code is not None:
            reject(current, current.error_code)
            return
        need_key = _wire_scalar(current, "need_key")
        url = _wire_scalar(current, "url")
        justification = _wire_scalar(current, "justification")
        if need_key is None or need_key.strip() not in need_keys:
            reject(current, "editorial_resource_proposal_unknown_need")
            return
        try:
            proposal = ResourceProposalV1(
                need_key=need_key.strip(),
                url=(url or "").strip(),
                justification=(justification or "").strip(),
            )
        except (TypeError, ValueError, ValidationError):
            reject(current, "editorial_resource_proposal_invalid")
            return
        if any(
            item.need_key == proposal.need_key and item.url == proposal.url for item in proposals
        ):
            reject(current, "editorial_resource_proposal_duplicate")
            return
        if len(proposals) >= MAX_ENRICHMENT_RESOURCE_PROPOSALS:
            reject(current, "editorial_resource_proposal_limit_exceeded")
            return
        proposals.append(proposal)

    for raw_line in sanitized.splitlines():
        line = _enrichment_wire_line(raw_line)
        if not line:
            continue
        if _ENRICHMENT_FENCE.match(raw_line):
            if "markdown_fences_removed" not in transformations:
                transformations.append("markdown_fences_removed")
            continue
        if line.casefold().rstrip(".! ") in {"no resource proposals", "no proposals", "empty"}:
            finish()
            recognized = True
            continue
        header = re.fullmatch(
            r"RESOURCE(?:\s*:\s*|\s+)([A-Za-z0-9][A-Za-z0-9._-]*)\s*:?", line, re.I
        )
        if header is not None:
            finish()
            recognized = True
            block_id = header.group(1)
            error_code = None
            if block_id in local_ids:
                error_code = "editorial_resource_proposal_duplicate_block_id"
            local_ids.add(block_id)
            block = _EditorialEnrichmentWireBlock(
                kind="RESOURCE",
                block_id=block_id,
                raw_lines=[raw_line],
                error_code=error_code,
            )
            continue
        if re.fullmatch(r"END(?:\s+RESOURCE)?", line, re.I):
            recognized = True
            finish()
            continue
        if block is None:
            continue
        match = _ENRICHMENT_FIELD.fullmatch(re.sub(r"^\*\*(.+?)\*\*\s*:", r"\1:", line))
        if match is None:
            if last_field is not None:
                values = block.fields[last_field]
                values[-1] = f"{values[-1]}\n{raw_line.strip()}"
                block.raw_lines.append(raw_line)
            else:
                block.error_code = (
                    block.error_code or "editorial_resource_proposal_unrecognized_line"
                )
                block.raw_lines.append(raw_line)
            continue
        field_name, field_value = match.groups()
        canonical = re.sub(r"[\s_-]+", " ", field_name.strip().casefold())
        canonical = {"need": "need_key", "need key": "need_key", "need_key": "need_key"}.get(
            canonical, canonical
        )
        if canonical not in {"need_key", "url", "justification"}:
            block.error_code = block.error_code or "editorial_resource_proposal_unknown_field"
            block.raw_lines.append(raw_line)
            last_field = None
            continue
        block.append_field(canonical, field_value)
        block.raw_lines.append(raw_line)
        last_field = canonical
    finish()
    if not recognized:
        return EditorialResourceWireParseResult(
            tuple(proposals),
            tuple(rejections),
            error_code="editorial_resource_proposal_unintelligible_response",
            transformations=tuple(transformations),
        )
    return EditorialResourceWireParseResult(
        tuple(proposals), tuple(rejections), transformations=tuple(transformations)
    )


def _parse_enrichment_wire_table(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> TableProposalV1 | None:
    def reject(item: _EditorialEnrichmentWireBlock, code: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, code, item.raw_lines))

    allowed_top_fields = {
        "key",
        "kind",
        "title",
        "caption",
        "profile",
        "placement",
        "section_index",
        "purpose",
        "available_data",
        "comprehension_gain",
        "scope",
        "purpose_evidence_handles",
        "knowledge_limits",
        "placement_reason",
    }
    if set(block.fields) - allowed_top_fields:
        reject(block, "editorial_enrichment_unknown_field")
        return None
    raw_kind = _wire_scalar(block, "kind")
    kind = next(
        (
            item
            for item in EnrichmentTableKind
            if raw_kind and item.value.casefold() == raw_kind.strip().casefold()
        ),
        None,
    )
    placement = _wire_placement(block)
    key = _wire_scalar(block, "key")
    title = _wire_scalar(block, "title")
    if not key or not title or kind is None or placement is None:
        reject(block, "editorial_enrichment_table_missing_or_invalid_field")
        return None
    purpose = _parse_analytic_purpose(block, evidence_pack, rejections)
    if purpose is None:
        return None
    columns: list[TableColumnProposalV1] = []
    for child in block.children:
        if child.kind != "COLUMN":
            continue
        error = _block_child_error(child, frozenset({"key", "label"}))
        column_key = _wire_scalar(child, "key")
        label = _wire_scalar(child, "label")
        if error is not None or not column_key or not label:
            reject(child, error or "editorial_enrichment_column_missing_field")
            continue
        try:
            columns.append(TableColumnProposalV1(key=column_key, label=label))
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_column_invalid")
    column_keys = [item.key for item in columns]
    if len(column_keys) != len(set(column_keys)):
        reject(block, "editorial_enrichment_duplicate_column_key")
        return None
    if len(columns) < 2:
        reject(block, "editorial_enrichment_table_column_count_invalid")
        return None
    rows: list[TableRowProposalV1] = []
    for child in block.children:
        if child.kind != "ROW":
            continue
        error = _block_child_error(child, frozenset({"cell", "evidence_handles"}))
        cells = tuple(child.fields.get("cell", ()))
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None:
            reject(child, error)
            continue
        if len(cells) != len(columns):
            reject(child, "editorial_enrichment_table_row_column_count_mismatch")
            continue
        if handles is None:
            reject(child, "editorial_enrichment_table_row_evidence_handles_invalid")
            continue
        if any(handle not in evidence_pack._handle_to_ref for handle in handles):
            reject(child, "editorial_enrichment_unknown_evidence_handle")
            continue
        try:
            rows.append(TableRowProposalV1(cells=cells, evidence_handles=handles))
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_table_row_invalid")
    if not rows:
        reject(block, "editorial_enrichment_table_has_no_valid_rows")
        return None
    row_handles = {handle for row in rows for handle in row.evidence_handles}
    purpose = _trim_analytic_purpose_evidence(block, purpose, row_handles, rejections, warnings)
    if purpose is None:
        return None
    try:
        return TableProposalV1(
            key=key,
            kind=kind,
            title=title,
            caption=_wire_caption(block),
            columns=tuple(columns),
            rows=tuple(rows),
            placement=placement,
            purpose=purpose,
        )
    except (TypeError, ValueError, ValidationError):
        reject(block, "editorial_enrichment_table_invalid")
        return None


def _parse_enrichment_wire_chart(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> ChartProposalV1 | None:
    def reject(item: _EditorialEnrichmentWireBlock, code: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, code, item.raw_lines))

    allowed_top_fields = {
        "key",
        "kind",
        "title",
        "caption",
        "profile",
        "placement",
        "section_index",
        "purpose",
        "available_data",
        "comprehension_gain",
        "scope",
        "purpose_evidence_handles",
        "knowledge_limits",
        "placement_reason",
    }
    if set(block.fields) - allowed_top_fields:
        reject(block, "editorial_enrichment_unknown_field")
        return None
    raw_kind = _wire_scalar(block, "kind")
    kind = next(
        (
            item
            for item in ChartKind
            if raw_kind and item.value.casefold() == raw_kind.strip().casefold()
        ),
        None,
    )
    placement = _wire_placement(block)
    key = _wire_scalar(block, "key")
    title = _wire_scalar(block, "title")
    if not key or not title or kind is None or placement is None:
        reject(block, "editorial_enrichment_chart_missing_or_invalid_field")
        return None
    purpose = _parse_analytic_purpose(block, evidence_pack, rejections)
    if purpose is None:
        return None

    points: list[ChartPointProposalV1] = []
    point_handles: set[str] = set()
    invalid_point = False
    for child in block.children:
        if child.kind != "POINT":
            continue
        error = _block_child_error(
            child, frozenset({"date", "label", "series", "evidence_handles"})
        )
        raw_date = _wire_scalar(child, "date")
        label = _wire_scalar(child, "label")
        series = _wire_scalar(child, "series")
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None or not raw_date or not label or not series or handles is None:
            reject(child, error or "editorial_enrichment_chart_point_missing_or_invalid_field")
            invalid_point = True
            continue
        if any(handle not in evidence_pack._handle_to_ref for handle in handles):
            reject(child, "editorial_enrichment_unknown_evidence_handle")
            invalid_point = True
            continue
        try:
            points.append(
                ChartPointProposalV1(
                    date=raw_date,
                    label=label,
                    series=series,
                    evidence_handles=handles,
                )
            )
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_chart_point_invalid")
            invalid_point = True
            continue
        point_handles.update(handles)
    if invalid_point:
        reject(block, "editorial_enrichment_chart_has_invalid_points")
        return None
    if not points:
        reject(block, "editorial_enrichment_chart_has_no_points")
        return None
    purpose = _trim_analytic_purpose_evidence(block, purpose, point_handles, rejections, warnings)
    if purpose is None:
        return None
    try:
        return ChartProposalV1(
            key=key,
            kind=kind,
            title=title,
            caption=_wire_caption(block),
            placement=placement,
            purpose=purpose,
            points=tuple(points),
        )
    except (TypeError, ValueError, ValidationError):
        reject(block, "editorial_enrichment_chart_invalid")
        return None


def _parse_enrichment_wire_diagram(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
    warnings: list[str],
) -> DiagramProposalV1 | None:
    def reject(item: _EditorialEnrichmentWireBlock, code: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, code, item.raw_lines))

    allowed_top_fields = {
        "key",
        "kind",
        "profile",
        "title",
        "caption",
        "placement",
        "section_index",
        "direction",
        "purpose",
        "available_data",
        "comprehension_gain",
        "scope",
        "purpose_evidence_handles",
        "knowledge_limits",
        "placement_reason",
    }
    if set(block.fields) - allowed_top_fields:
        reject(block, "editorial_enrichment_unknown_field")
        return None
    raw_kind = _wire_scalar(block, "kind")
    kind = next(
        (
            item
            for item in EnrichmentDiagramKind
            if raw_kind and item.value.casefold() == raw_kind.strip().casefold()
        ),
        None,
    )
    raw_profile = _wire_scalar(block, "profile")
    profile = next(
        (
            item
            for item in DiagramProfile
            if raw_profile and item.value.casefold() == raw_profile.strip().casefold()
        ),
        None,
    )
    if raw_profile is None and kind is not None:
        # Archived pre-v8 responses did not carry a composition profile. Keep them
        # parseable with a deterministic, kind-based hint while new prompts require it.
        profile = (
            DiagramProfile.FLOW
            if kind
            in {
                EnrichmentDiagramKind.INFECTION_CHAIN,
                EnrichmentDiagramKind.NETWORK_FLOW,
                EnrichmentDiagramKind.EXECUTION_SEQUENCE,
            }
            else DiagramProfile.ARCHITECTURE
            if kind is EnrichmentDiagramKind.INFRASTRUCTURE
            else DiagramProfile.RELATIONSHIP
        )
        warnings.append(f"editorial_enrichment_diagram_profile_missing:{block.block_id}")
    raw_direction = _wire_scalar(block, "direction")
    direction = next(
        (
            item
            for item in EnrichmentDiagramDirection
            if raw_direction and item.value.casefold() == raw_direction.strip().casefold()
        ),
        None,
    )
    placement = _wire_placement(block)
    key = _wire_scalar(block, "key")
    title = _wire_scalar(block, "title")
    if (
        not key
        or not title
        or kind is None
        or direction is None
        or profile is None
        or placement is None
    ):
        reject(block, "editorial_enrichment_diagram_missing_or_invalid_field")
        return None
    caption = _wire_caption(block)
    if raw_profile is not None and caption is None:
        reject(block, "editorial_enrichment_diagram_caption_missing")
        return None
    if caption is None:
        caption = title
        warnings.append(f"editorial_enrichment_diagram_caption_missing:{block.block_id}")
    purpose = _parse_analytic_purpose(block, evidence_pack, rejections)
    if purpose is None:
        return None

    nodes: list[DiagramNodeProposalV1] = []
    for child in block.children:
        if child.kind != "NODE":
            continue
        error = _block_child_error(
            child, frozenset({"id", "node_id", "label", "role", "evidence_handles"})
        )
        node_id = _wire_scalar(child, "node_id") or _wire_scalar(child, "id")
        label = _wire_scalar(child, "label")
        raw_role = _wire_scalar(child, "role")
        role = next(
            (
                item
                for item in DiagramNodeRole
                if raw_role and item.value.casefold() == raw_role.strip().casefold()
            ),
            None,
        )
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None:
            reject(child, error)
            continue
        if not node_id or not label:
            reject(child, "editorial_enrichment_diagram_node_missing_field")
            continue
        if len(label.split()) > 6 or len(label) > 40:
            reject(child, "editorial_enrichment_diagram_node_label_over_budget")
            continue
        if raw_role is None:
            role = DiagramNodeRole.UNKNOWN
            warnings.append(
                f"editorial_enrichment_diagram_node_role_missing:{block.block_id}/{child.block_id}"
            )
        elif role is None:
            reject(child, "editorial_enrichment_diagram_node_role_invalid")
            continue
        if handles is None:
            reject(child, "editorial_enrichment_diagram_node_evidence_handles_invalid")
            continue
        if any(handle not in evidence_pack._handle_to_ref for handle in handles):
            reject(child, "editorial_enrichment_unknown_evidence_handle")
            continue
        if any(item.node_id == node_id for item in nodes):
            reject(child, "editorial_enrichment_duplicate_diagram_node_id")
            continue
        try:
            nodes.append(
                DiagramNodeProposalV1(
                    node_id=node_id,
                    label=label,
                    evidence_handles=handles,
                    role=role,
                )
            )
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_diagram_node_invalid")
    if len(nodes) > 8:
        reject(block, "editorial_enrichment_diagram_node_count_over_budget")
        return None
    known_nodes = {item.node_id for item in nodes}

    edges: list[DiagramEdgeProposalV1] = []
    for child in block.children:
        if child.kind not in {"RELATION", "EDGE"}:
            continue
        error = _block_child_error(
            child,
            frozenset(
                {
                    "source_node_id",
                    "target_node_id",
                    "label",
                    "relation_type",
                    "direction",
                    "evidence_handles",
                }
            ),
        )
        source = _wire_scalar(child, "source_node_id")
        target = _wire_scalar(child, "target_node_id")
        raw_relation_type = _wire_scalar(child, "relation_type")
        relation_type = next(
            (
                item
                for item in DiagramRelationType
                if raw_relation_type
                and item.value.casefold() == raw_relation_type.strip().casefold()
            ),
            None,
        )
        if relation_type is None and kind is EnrichmentDiagramKind.INFECTION_CHAIN:
            normalized_relation = (
                " ".join(raw_relation_type.casefold().replace("_", " ").replace("-", " ").split())
                if raw_relation_type
                else ""
            )
            if normalized_relation in {
                "infection chain",
                "infection sequence",
                "sequence",
                "sequential",
            }:
                relation_type = DiagramRelationType.FACTUAL
                warnings.append(
                    "editorial_enrichment_relation_type_normalized:"
                    f"{block.block_id}/{child.block_id}:{normalized_relation}->factual"
                )
        raw_edge_direction = _wire_scalar(child, "direction")
        edge_direction = next(
            (
                item
                for item in DiagramRelationDirection
                if raw_edge_direction
                and item.value.casefold() == raw_edge_direction.strip().casefold()
            ),
            None,
        )
        if raw_edge_direction is None and relation_type is not None:
            edge_direction = (
                DiagramRelationDirection.UNDIRECTED
                if relation_type is DiagramRelationType.COMPARISON
                else DiagramRelationDirection.DIRECTED
            )
            warnings.append(
                f"editorial_enrichment_diagram_relation_direction_missing:"
                f"{block.block_id}/{child.block_id}"
            )
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None:
            reject(child, error)
            continue
        if not source or not target:
            reject(child, "editorial_enrichment_diagram_relation_missing_endpoint")
            continue
        if relation_type is None:
            reject(child, "editorial_enrichment_diagram_relation_type_invalid")
            continue
        if edge_direction is None:
            reject(child, "editorial_enrichment_diagram_relation_direction_invalid")
            continue
        if (
            relation_type is DiagramRelationType.COMPARISON
            and edge_direction is not DiagramRelationDirection.UNDIRECTED
        ):
            reject(child, "editorial_enrichment_comparison_direction_invalid")
            continue
        if source not in known_nodes or target not in known_nodes:
            reject(child, "editorial_enrichment_diagram_relation_unknown_node")
            continue
        if handles is None:
            reject(child, "editorial_enrichment_diagram_relation_evidence_handles_invalid")
            continue
        if any(handle not in evidence_pack._handle_to_ref for handle in handles):
            reject(child, "editorial_enrichment_unknown_evidence_handle")
            continue
        relation_label = _wire_scalar(child, "label")
        if relation_label and len(relation_label.split()) > 5:
            reject(child, "editorial_enrichment_diagram_relation_label_over_budget")
            continue
        try:
            edges.append(
                DiagramEdgeProposalV1(
                    source_node_id=source,
                    target_node_id=target,
                    label=relation_label,
                    relation_type=relation_type,
                    direction=edge_direction,
                    evidence_handles=handles,
                )
            )
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_diagram_relation_invalid")

    groups: list[DiagramGroupProposalV1] = []
    grouped_nodes: set[str] = set()
    group_ids: set[str] = set()
    for child in block.children:
        if child.kind != "GROUP":
            continue
        error = _block_child_error(child, frozenset({"id", "group_id", "label", "node_ids"}))
        group_id = _wire_scalar(child, "group_id") or _wire_scalar(child, "id")
        label = _wire_scalar(child, "label")
        node_ids = _wire_node_ids(_wire_scalar(child, "node_ids"))
        if error is not None:
            reject(child, error)
            continue
        if not group_id or not label or node_ids is None:
            reject(child, "editorial_enrichment_diagram_group_missing_field")
            continue
        if any(node_id not in known_nodes for node_id in node_ids):
            reject(child, "editorial_enrichment_diagram_group_unknown_node")
            continue
        if group_id in group_ids:
            reject(child, "editorial_enrichment_duplicate_diagram_group_id")
            continue
        if grouped_nodes.intersection(node_ids) or len(set(node_ids)) != len(node_ids):
            reject(child, "editorial_enrichment_diagram_group_overlapping_nodes")
            continue
        try:
            groups.append(DiagramGroupProposalV1(group_id=group_id, label=label, node_ids=node_ids))
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_diagram_group_invalid")
            continue
        group_ids.add(group_id)
        grouped_nodes.update(node_ids)
    layout_labels = [item.label for item in nodes]
    layout_labels.extend(item.label for item in edges if item.label is not None)
    layout_labels.extend(item.label for item in groups)
    if diagram_requires_vertical_layout(len(nodes), layout_labels) and (
        direction is not EnrichmentDiagramDirection.TOP_TO_BOTTOM
    ):
        reject(block, "editorial_enrichment_diagram_direction_requires_top_to_bottom")
        return None
    if len(nodes) < 2 or not edges:
        reject(block, "editorial_enrichment_diagram_incomplete_after_rejections")
        return None
    diagram_handles = {handle for node in nodes for handle in node.evidence_handles} | {
        handle for edge in edges for handle in edge.evidence_handles
    }
    purpose = _trim_analytic_purpose_evidence(block, purpose, diagram_handles, rejections, warnings)
    if purpose is None:
        return None
    try:
        return DiagramProposalV1(
            key=key,
            kind=kind,
            title=title,
            caption=caption,
            direction=direction,
            nodes=tuple(nodes),
            edges=tuple(edges),
            groups=tuple(groups),
            placement=placement,
            purpose=purpose,
            profile=profile,
        )
    except (TypeError, ValueError, ValidationError):
        reject(block, "editorial_enrichment_diagram_invalid")
        return None


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentEvidencePackV1:
    """Stable renderer-free prompt projection with a private exact ref map."""

    publication_language: str
    current_synthesis: Mapping[str, Any]
    narrative_evidence: tuple[Mapping[str, Any], ...]
    technical_evidence: tuple[Mapping[str, Any], ...]
    reserve_evidence: tuple[Mapping[str, Any], ...]
    source_pair_relations: tuple[Mapping[str, Any], ...]
    projection_hash: str | None = None
    policy_version: str = EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION
    _handle_to_ref: Mapping[str, ExtractionEvidenceRefV1] = field(
        default_factory=dict, repr=False, compare=False
    )
    _reserve_handle_to_ref: Mapping[str, ExtractionEvidenceRefV1] = field(
        default_factory=dict, repr=False, compare=False
    )

    def resolve_handle(self, handle: str) -> ExtractionEvidenceRefV1:
        try:
            return self._handle_to_ref[handle]
        except (KeyError, TypeError) as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE
            ) from exc

    def evidence_handles_for_source(self, source_document_id: UUID) -> tuple[str, ...]:
        return tuple(
            sorted(
                handle
                for handle, ref in self._handle_to_ref.items()
                if ref.source_document_id == source_document_id
            )
        )


class EditorialEnrichmentExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REUSED = "reused"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class EditorialEnrichmentStageErrorCode(StrEnum):
    INPUTS_MISSING = "editorial_enrichment_inputs_missing"
    INPUTS_MISMATCH = "editorial_enrichment_inputs_mismatch"
    ACCESS_POLICY_UNAVAILABLE = "editorial_enrichment_access_policy_unavailable"
    POLICY_BLOCKED = "editorial_enrichment_policy_blocked"
    OUTPUT_INVALID = "editorial_enrichment_output_invalid"
    EMPTY_AFTER_REJECTIONS = "editorial_enrichment_empty_after_rejections"
    UNKNOWN_EVIDENCE = "editorial_enrichment_unknown_evidence"
    UNKNOWN_TECHNICAL_VALUE = "editorial_enrichment_unknown_technical_value"
    PLACEMENT_INVALID = "editorial_enrichment_placement_invalid"
    REUSE_INVALID = "editorial_enrichment_reuse_invalid"
    MODEL_FAILED = "editorial_enrichment_model_failed"
    # The production state machine keys provider reconciliation on one shared
    # code; a stage-specific code would let an ordinary retry resubmit.
    RECONCILIATION_REQUIRED = PRODUCTION_RECONCILIATION_ERROR_CODE


class EditorialEnrichmentProposalControlError(RuntimeError):
    def __init__(self, code: EditorialEnrichmentStageErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ProductionEditorialEnrichmentExecution:
    status: EditorialEnrichmentExecutionStatus
    input_hash: str | None
    artifact: ProductionArtifact | None
    model_run_id: UUID | None
    model_calls: int
    table_count: int = 0
    diagram_count: int = 0
    source_figure_count: int = 0
    chart_count: int = 0
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    details: Mapping[str, Any] | None = None


class EditorialEnrichmentValidationError(ValueError):
    """A cross-artifact Editorial Enrichment invariant failed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _source_figure_candidates(
    inventory: SourceFigureInventoryResult,
) -> tuple[SourceFigureCandidateV1, ...]:
    return tuple(
        SourceFigureCandidateV1(
            key=f"source_figure_{figure.figure_id.hex}",
            source_document_id=figure.source_document_id,
            source_url=figure.source,
            caption=figure.locator.figure_label or "Archived source figure",
            provenance=figure.provenance,
            locator=figure.locator,
            inclusion_status=SourceFigureInclusionStatus.PROPOSED,
            placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
            resolved_figure=figure,
        )
        for figure in inventory.accepted
    )


def build_editorial_figure_catalog(
    extraction: ProductionExtractionV1,
    inventory: SourceFigureInventoryResult,
) -> tuple[EditorialFigureCatalogEntry, ...]:
    """Assign prompt-local handles to every full-source media decision."""
    full_sources = {
        source.source_document_id: source
        for source in extraction.sources
        if source.profile is ExtractionProfile.FULL
    }
    figures = [figure for figure in inventory.figures if figure.source_document_id in full_sources]
    figures.sort(
        key=lambda item: (
            full_sources[item.source_document_id].canonical_url,
            full_sources[item.source_document_id].role.value,
            item.locator.page or 0,
            item.locator.section or "",
            item.locator.figure_label or "",
            item.locator.original_asset_url or "",
            item.figure_id.hex,
        )
    )
    entries: list[EditorialFigureCatalogEntry] = []
    for index, figure in enumerate(figures, start=1):
        source = full_sources[figure.source_document_id]
        editorial_role = source.editorial_role or source.role
        entries.append(
            EditorialFigureCatalogEntry(
                handle=f"F{index:03d}",
                source_role=source.role.value,
                editorial_role=editorial_role.value,
                figure=figure,
                metadata=inventory.catalog_metadata.get(
                    figure.figure_id, SourceFigureCatalogMetadata()
                ),
            )
        )
    return tuple(entries)


def build_source_figure_provenance_diagnostics(
    inventory: SourceFigureInventoryResult,
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...],
    enrichment: EditorialEnrichmentV1,
) -> list[dict[str, str | None]]:
    diagnostics = list(inventory.provenance_diagnostics)
    catalog_by_id = {entry.figure.figure_id: entry for entry in figure_catalog}
    for entry in figure_catalog:
        diagnostics.append(
            SourceFigureProvenanceDiagnostic(
                stage=SourceFigureProvenanceStage.CATALOGUED,
                source_document_id=entry.figure.source_document_id,
                figure_id=entry.figure.figure_id,
                figure_handle=entry.handle,
                blob_id=(
                    entry.figure.blob_id
                    if entry.figure.decision is SourceFigureDecision.ACCEPTED
                    else None
                ),
                sha256=(
                    entry.figure.sha256
                    if entry.figure.decision is SourceFigureDecision.ACCEPTED
                    else None
                ),
                reason_code=(
                    entry.figure.decision_reason
                    if entry.figure.decision is not SourceFigureDecision.ACCEPTED
                    else None
                ),
            )
        )

    for candidate in enrichment.source_figures:
        if candidate.inclusion_status is not SourceFigureInclusionStatus.INCLUDED:
            continue
        resolved = candidate.resolved_figure
        selected_entry = catalog_by_id.get(resolved.figure_id) if resolved is not None else None
        if (
            resolved is None
            or selected_entry is None
            or resolved.decision is not SourceFigureDecision.ACCEPTED
            or resolved.source_document_id != selected_entry.figure.source_document_id
            or resolved.blob_id != selected_entry.figure.blob_id
            or resolved.sha256 != selected_entry.figure.sha256
        ):
            raise ValueError("source_figure_published_media_mismatch")
        diagnostics.append(
            SourceFigureProvenanceDiagnostic(
                stage=SourceFigureProvenanceStage.SELECTED,
                source_document_id=resolved.source_document_id,
                figure_id=resolved.figure_id,
                figure_handle=selected_entry.handle,
                blob_id=resolved.blob_id,
                sha256=resolved.sha256,
            )
        )
    return [item.to_json() for item in diagnostics]


def _source_figure_inventory_warnings(
    inventory: SourceFigureInventoryResult,
) -> tuple[str, ...]:
    warnings = set(inventory.warnings)
    pending_inventory_warnings = {
        "source_figure_pdf_page_excerpt_needed",
        "source_figure_source_exceeds_byte_limit",
        "source_figure_source_document_unavailable",
        "source_figure_source_blob_unavailable",
    }
    if any(figure.decision is SourceFigureDecision.PENDING for figure in inventory.figures) or (
        pending_inventory_warnings & warnings
    ):
        warnings.add("source_figure_inventory_contains_unresolved_items")
    if inventory.truncated:
        warnings.add("source_figure_inventory_truncated")
    return tuple(sorted(warnings))


def canonical_synthesis_hash(synthesis: ProductionSynthesisV1) -> str:
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis))
    ).hexdigest()


def _prompt_evidence_record(
    handle: str,
    kind: EvidenceKind,
    payload: Mapping[str, Any],
    *,
    source: Any,
) -> dict[str, Any]:
    """Expose only safe extraction metadata; detection rule bodies stay local."""
    if kind is EvidenceKind.FACT:
        values = {
            "category": payload["category"],
            "value": payload["value"],
            "attack_id": payload["attack_id"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    elif kind is EvidenceKind.EVENT:
        values = {
            "event_date": payload["event_date"],
            "date_text": payload["date_text"],
            "text": payload["text"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    elif kind is EvidenceKind.INDICATOR:
        values = {
            "value": payload["value"],
            "type": payload["artifact_type"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    elif kind is EvidenceKind.UNCERTAINTY:
        values = {"text": payload["text"]}
    else:
        values = {
            "type": payload["rule_type"],
            "name": payload["name"],
            "sha256": payload["sha256"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    return {
        "handle": handle,
        "kind": kind.value,
        "source_role": source.role.value,
        "editorial_role": source.editorial_role.value,
        **values,
    }


def _synthesis_prompt_projection(
    synthesis: ProductionSynthesisV1,
    handle_for_ref: Mapping[ExtractionEvidenceRefV1, str],
) -> dict[str, Any]:
    def _catalogue_handles(refs: Iterable[ExtractionEvidenceRefV1]) -> list[str]:
        # The synthesis may cite reserve-only evidence (e.g. a fact supporting a
        # link_not_demonstrated relation, possibly classified out of scope); it has no E
        # handle and must not become citable by the enrichment model.
        return [handle_for_ref[ref] for ref in refs if ref in handle_for_ref]

    def paragraph(item: Any, anchor: str) -> dict[str, Any] | None:
        if any(
            ref.kind is EvidenceKind.UNCERTAINTY and ref not in handle_for_ref
            for ref in item.evidence_refs
        ):
            return None
        return {
            "anchor": anchor,
            "text": item.text,
            "evidence_handles": _catalogue_handles(item.evidence_refs),
        }

    lead = [
        projected
        for index, item in enumerate(synthesis.lead, start=1)
        if (projected := paragraph(item, lead_paragraph_anchor(index))) is not None
    ]
    sections: list[dict[str, Any]] = []
    for index, section in enumerate(synthesis.sections):
        paragraphs = [
            projected
            for paragraph_index, item in enumerate(section.paragraphs, start=1)
            if (projected := paragraph(item, section_paragraph_anchor(index, paragraph_index)))
            is not None
        ]
        if paragraphs:
            sections.append(
                {
                    "section_index": index,
                    "kind": section.kind.value,
                    "heading": section.heading,
                    "paragraphs": paragraphs,
                }
            )

    return {
        "language": synthesis.publication_language,
        "title": synthesis.title,
        "lead": lead,
        "sections": sections,
        "timeline": [
            {
                "anchor": timeline_anchor(index),
                "event_date": item.event_date.isoformat() if item.event_date else None,
                "date_text": item.date_text,
                "text": item.text,
                "evidence_handles": _catalogue_handles(item.evidence_refs),
            }
            for index, item in enumerate(synthesis.timeline, start=1)
            if not any(
                ref.kind is EvidenceKind.UNCERTAINTY and ref not in handle_for_ref
                for ref in item.evidence_refs
            )
        ],
    }


def build_editorial_enrichment_evidence_pack(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    projection: RelevanceProjectionV1 | None = None,
) -> EditorialEnrichmentEvidencePackV1:
    """Build a stable source-free model projection and exact handle resolver."""
    if not isinstance(snapshot, ProductionInputSnapshot):
        raise ValueError("Expected a ProductionInputSnapshot")
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    if not (
        snapshot.subject_id == extraction.subject_id == synthesis.subject_id
        and snapshot.input_hash
        == extraction.production_input_hash
        == synthesis.production_input_hash
        and synthesis.extraction_hash == canonical_extraction_hash(extraction)
        and synthesis.publication_language == snapshot.publication_language
    ):
        raise ValueError("Editorial enrichment evidence inputs have mismatched lineage")
    if projection is not None:
        validate_relevance_projection_lineage(
            projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
        )
        if projection.production_input_hash != snapshot.input_hash:
            raise ValueError("Editorial relevance projection does not match its snapshot")

    entries = _all_evidence_entries(extraction)
    source_by_id = {source.source_document_id: source for source in extraction.sources}

    def admitted(ref: ExtractionEvidenceRefV1) -> bool:
        if (
            ref.kind is EvidenceKind.UNCERTAINTY
            and source_by_id[ref.source_document_id].profile is not ExtractionProfile.FULL
        ):
            return False
        if projection is None:
            return True
        return projection.classification_for(ref).classification not in {
            RelevanceClassification.OUT_OF_SCOPE,
            RelevanceClassification.INDETERMINATE,
        }

    counter_refs = {
        ref
        for ref in entries
        if projection is not None
        and (
            ref.kind is not EvidenceKind.UNCERTAINTY
            or source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
        )
        and projection.classification_for(ref).classification
        is RelevanceClassification.COUNTER_INDICATION
    }
    relation_refs = (
        {
            ref
            for relation in projection.source_pair_relations
            for ref in relation.supporting_evidence_refs
            if (
                ref.kind is not EvidenceKind.UNCERTAINTY
                or source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
            )
        }
        if projection is not None
        else set()
    )
    reserve_refs = counter_refs | relation_refs
    narrative_refs = {
        ref
        for ref in entries
        if ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
        and source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
        and admitted(ref)
        and ref not in counter_refs
    }
    technical_candidates = sorted(
        (
            ref
            for ref, payload in entries.items()
            if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
            and admitted(ref)
            and ref not in counter_refs
            and (
                str(payload.get("context") or "").strip()
                or str(payload.get("evidence_quote") or "").strip()
            )
        ),
        key=evidence_ref_sort_key,
    )[:MAX_EDITORIAL_ENRICHMENT_TECHNICAL_EVIDENCE]
    included_refs = (
        narrative_refs
        | set(technical_candidates)
        | {ref for ref in synthesis_evidence_refs(synthesis) if admitted(ref)}
    )

    def authority_key(ref: ExtractionEvidenceRefV1) -> tuple[int, int, str, str]:
        source = source_by_id[ref.source_document_id]
        editorial_role = source.editorial_role
        assert editorial_role is not None
        role_order = {
            ProductionEditorialRole.PRIMARY: 0,
            ProductionEditorialRole.CORROBORATION: 1,
            ProductionEditorialRole.COUNTER_ANALYSIS: 2,
            ProductionEditorialRole.CONTEXT: 3,
        }
        return (
            0 if source.tier is ProductionReferenceTier.CORE else 1,
            role_order[editorial_role],
            source.canonical_url,
            str(source.source_document_id),
        )

    ordered_refs = tuple(
        sorted(
            included_refs,
            key=lambda ref: (authority_key(ref), evidence_ref_sort_key(ref)),
        )
    )
    handle_to_ref = {f"E{index:03d}": ref for index, ref in enumerate(ordered_refs, start=1)}
    handle_for_ref = {ref: handle for handle, ref in handle_to_ref.items()}
    ordered_reserve_refs = tuple(
        sorted(
            reserve_refs,
            key=lambda ref: (authority_key(ref), evidence_ref_sort_key(ref)),
        )
    )
    reserve_handle_for_ref = {
        ref: f"R{index:03d}" for index, ref in enumerate(ordered_reserve_refs, start=1)
    }
    narrative_evidence = tuple(
        _prompt_evidence_record(
            handle_for_ref[ref],
            ref.kind,
            entries[ref],
            source=source_by_id[ref.source_document_id],
        )
        for ref in ordered_refs
        if ref in narrative_refs
    )
    technical_evidence = tuple(
        _prompt_evidence_record(
            handle_for_ref[ref],
            ref.kind,
            entries[ref],
            source=source_by_id[ref.source_document_id],
        )
        for ref in ordered_refs
        if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
    )
    reserve_records: list[Mapping[str, Any]] = []
    for ref in ordered_reserve_refs:
        record = _prompt_evidence_record(
            reserve_handle_for_ref[ref],
            ref.kind,
            entries[ref],
            source=source_by_id[ref.source_document_id],
        )
        if projection is not None and ref in counter_refs:
            classification = projection.classification_for(ref)
            record["projection_classification"] = classification.classification.value
            record["projection_reason_code"] = classification.reason_code.value
        reserve_records.append(MappingProxyType(record))
    reserve_evidence = tuple(reserve_records)
    source_pair_relations = tuple(
        MappingProxyType(
            {
                "relation": relation.relation.value,
                "reason": relation.reason,
                "provenance": relation.provenance.value,
                "supporting_handles": tuple(
                    reserve_handle_for_ref[ref] for ref in relation.supporting_evidence_refs
                ),
            }
        )
        for relation in (projection.source_pair_relations if projection is not None else ())
    )
    return EditorialEnrichmentEvidencePackV1(
        publication_language=synthesis.publication_language,
        current_synthesis=MappingProxyType(_synthesis_prompt_projection(synthesis, handle_for_ref)),
        narrative_evidence=tuple(MappingProxyType(record) for record in narrative_evidence),
        technical_evidence=tuple(MappingProxyType(record) for record in technical_evidence),
        reserve_evidence=reserve_evidence,
        source_pair_relations=source_pair_relations,
        projection_hash=projection.projection_hash if projection is not None else None,
        _handle_to_ref=MappingProxyType(handle_to_ref),
        _reserve_handle_to_ref=MappingProxyType(
            {handle: ref for ref, handle in reserve_handle_for_ref.items()}
        ),
    )


def editorial_enrichment_evidence_pack_hash(
    pack: EditorialEnrichmentEvidencePackV1,
) -> str:
    if not isinstance(pack, EditorialEnrichmentEvidencePackV1):
        raise ValueError("Expected an EditorialEnrichmentEvidencePackV1")
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
                "policy_version": pack.policy_version,
                "publication_language": pack.publication_language,
                "current_synthesis": dict(pack.current_synthesis),
                "narrative_evidence": [dict(item) for item in pack.narrative_evidence],
                "technical_evidence": [dict(item) for item in pack.technical_evidence],
                "reserve_evidence": [dict(item) for item in pack.reserve_evidence],
                "source_pair_relations": [dict(item) for item in pack.source_pair_relations],
                "relevance_projection_hash": pack.projection_hash,
            }
        )
    ).hexdigest()


def editorial_enrichment_model_run_id(run: ProductionRun, invocation_hash: str) -> UUID:
    """Derive the provider identity from inputs that require a new invocation."""
    if not isinstance(run, ProductionRun) or _SHA256_RE.fullmatch(invocation_hash) is None:
        raise ValueError("Editorial enrichment ModelRun identity inputs are invalid")
    identity = ":".join(
        (
            "production-editorial-enrichment-model-run-v1",
            str(run.id),
            str(run.pipeline_generation),
            invocation_hash,
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def build_editorial_resource_proposal_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    access_policy: SynthesisAccessPolicyV1,
    needs: tuple[EditorialResourceNeedV1, ...],
    *,
    enrichment_input_hash: str,
) -> ModelRequest:
    """Build the separate, explicitly web-search-enabled proposal request."""
    if not needs or len(needs) > MAX_ENRICHMENT_RESOURCE_NEEDS:
        raise ValueError("A resource proposal request requires bounded typed needs")
    if run.id != snapshot.production_run_id or run.subject_id != snapshot.subject_id:
        raise ValueError("Editorial resource proposal request identities differ")
    needs_payload = [
        {
            "key": need.key,
            "kind": need.kind.value,
            "reason": need.reason,
            "query_hint": need.query_hint,
        }
        for need in needs
    ]
    needs_hash = hashlib.sha256(_canonical_json_bytes(needs_payload)).hexdigest()
    access_hash = synthesis_access_policy_hash(access_policy)
    request_identity = hashlib.sha256(
        _canonical_json_bytes(
            {
                "stage": "editorial_resource_proposal",
                "run_id": str(run.id),
                "pipeline_generation": run.pipeline_generation,
                "enrichment_input_hash": enrichment_input_hash,
                "needs_hash": needs_hash,
                "access_policy_hash": access_hash,
                "prompt_version": EDITORIAL_RESOURCE_PROPOSAL_PROMPT_VERSION,
                "contract_version": EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION,
            }
        )
    ).hexdigest()
    evidence_pack_hash = hashlib.sha256(
        f"editorial-resource-proposals:{enrichment_input_hash}:{needs_hash}".encode()
    ).hexdigest()
    prompt_payload = {
        "instructions": (
            "Propose candidate resource URLs for the listed missing needs only. "
            "This is a subject-bounded search: use only the supplied subject scope and each "
            "query hint. Return URLs with short justifications as RESOURCE blocks. Do not "
            "summarize findings, assert facts, or create sources. Every URL is only a proposal "
            "for later REFERENCES/collector review; do not claim it was collected or verified."
        ),
        "subject_scope": {
            "title": snapshot.subject_title,
            "actor_or_campaign": snapshot.actor_or_campaign,
            "period_start": snapshot.period_start.isoformat(),
            "period_end": snapshot.period_end.isoformat(),
        },
        "needs": needs_payload,
        "output_contract": (
            "RESOURCE R001\nNEED: N001\nURL: https://example.org/resource\n"
            "JUSTIFICATION: why this URL may address the named need\nEND RESOURCE\n"
            "Use NO RESOURCE PROPOSALS when no suitable candidate exists."
        ),
        "contract_version": EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION,
    }
    return ModelRequest(
        text=json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        prompt_template_id="production-editorial-resource-proposals",
        prompt_template_version=EDITORIAL_RESOURCE_PROPOSAL_PROMPT_VERSION,
        evidence_pack_hash=evidence_pack_hash,
        external_llm_allowed=(
            access_policy.external_llm_allowed and not access_policy.do_not_submit
        ),
        routing_hint=ModelRoutingHint.EDITORIAL_ENRICHMENT,
        sensitivity=access_policy.effective_tlp.value,
        web_search=True,
        background=False,
        conversation=None,
        run_id=uuid5(NAMESPACE_URL, f"production-editorial-resource-proposal:{request_identity}"),
        allow_failed_resubmit=True,
        metadata={
            "resource_proposal_request_hash": request_identity,
            "resource_needs_hash": needs_hash,
            "enrichment_input_hash": enrichment_input_hash,
            "access_policy_hash": access_hash,
            "resource_policy_version": EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
        },
        parameters={"contract_version": EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION},
    )


def editorial_enrichment_output_contract_example() -> str:
    """The text-block contract shown to the model; it is not a schema payload."""
    return (
        """Return independent plain-text blocks. Give every block a local id.
Commence par identifier une question analytique pour chaque besoin.

Pour chaque question analytique :
1. Une figure de la publication source répond-elle déjà correctement
   à la question ?
      OUI → FIGURE SOURCE
2. Sinon, les informations sont-elles principalement temporelles
   ou quantitatives ?
      OUI → CHART
3. Sinon, faut-il montrer des relations, une séquence,
   une architecture, un routage ou un flux ?
      OUI → DIAGRAM
4. Sinon, faut-il comparer plusieurs objets selon les mêmes champs
   ou représenter une correspondance structurée ?
      OUI → TABLE
5. Sinon → aucun enrichissement.

CHART accepte uniquement le KIND timeline. Chaque POINT doit citer une date ISO
exacte, un LABEL et une SERIES présents dans ses preuves, plus au moins un
EVIDENCE handle. N'invente pas de dates, n'utilise pas de dates relatives et
n'interpole aucun point. Le renderer choisit les couleurs.
"""
        + "Une représentation existante de la source est prioritaire sur une représentation "
        + "reconstruite, lorsque les deux répondent à la même question analytique.\n"
        + """Deux enrichissements ne doivent pas répondre à la même question analytique.
Aucun quota minimal de médias ne s'applique; RIEN est une décision valide.

For each FIGURE, CHART, TABLE, and DIAGRAM, fill every typed analytic-purpose field:
PURPOSE (the reader's question), DATA (what the evidence contains), GAIN (why
this form is clearer than prose), SCOPE, PURPOSE_EVIDENCE, LIMITS, PLACEMENT,
and PLACEMENT_REASON. Values and handles must be non-empty.

CHART C001
KEY: domains_created_over_time
KIND: timeline
TITLE: Domain registrations by actor
CAPTION: Exact documented registration dates for observed actors.
PURPOSE: When were the documented domains registered, and which actors show bursts?
DATA: The exact dates, domain labels and actors stated in E014 and E021.
GAIN: A shared time axis makes bursts and differences between actors visible.
SCOPE: Only registrations with exact dates in the cited evidence.
PURPOSE_EVIDENCE: E014, E021
LIMITS: The chart does not imply connections between domains or actors.
PLACEMENT: after_section
SECTION_INDEX: 2
PLACEMENT_REASON: Place beside the section discussing domain registrations.
POINT P001
DATE: 2026-06-22
LABEL: msbenefit.com
SERIES: APT31
EVIDENCE: E014
END POINT
POINT P002
DATE: 2026-07-27T10:13:40+02:00
LABEL: example-domain.test
SERIES: UTA0560
EVIDENCE: E021
END POINT
END CHART

TABLE T001
KEY: mechanism_comparison
KIND: custom
TITLE: Reported mechanism observations
PURPOSE: Which reported mechanism details can be compared?
DATA: Placeholder field names and values from E001 and E002.
GAIN: Adjacent cells expose differences that are easy to miss in prose.
SCOPE: Only the two observations cited below.
PURPOSE_EVIDENCE: E001, E002
LIMITS: The sources do not establish that the mechanisms are linked.
PLACEMENT: after_section
SECTION_INDEX: 0
PLACEMENT_REASON: Place beside the paragraph introducing the observations.
COLUMN C001
KEY: mechanism
LABEL: Reported mechanism
END COLUMN
COLUMN C002
KEY: observation
LABEL: Observed detail
END COLUMN
ROW R001
CELL: Mechanism A
CELL: [verbatim placeholder detail from E001]
EVIDENCE: E001
END ROW
ROW R002
CELL: Mechanism B
CELL: [verbatim placeholder detail from E002]
EVIDENCE: E002
END ROW
END TABLE

DIAGRAM D001
KEY: flux_bitcoin
KIND: network_flow
PROFILE: FLOW
TITLE: Résolution via une adresse Bitcoin
CAPTION: Le malware extrait une destination C2 d'un champ OP_RETURN.
PURPOSE: Comment une graine mène-t-elle à une destination ?
DATA: Les deux étapes documentées dans E001.
GAIN: Deux étapes courtes rendent le flux visible.
SCOPE: Les deux étapes citées dans E001.
PURPOSE_EVIDENCE: E001
LIMITS: Le schéma ne montre que les étapes documentées.
DIRECTION: top_to_bottom
PLACEMENT: after_lead
PLACEMENT_REASON: Après le paragraphe qui présente le flux.
NODE N001
ID: seed
ROLE: infrastructure
LABEL: Graine de résolution
EVIDENCE: E001
END NODE
NODE N002
ID: destination
ROLE: data_artifact
LABEL: Destination Bitcoin
EVIDENCE: E001
END NODE
RELATION L001
FROM: seed
TO: destination
RELATION_TYPE: factual
DIRECTION: directed
LABEL: résout vers
EVIDENCE: E001
END RELATION
END DIAGRAM

No enrichment is required when paragraphs suffice. The exact standalone empty
marker is:
NO USEFUL ENRICHMENT
Do not add rows, nodes, groups, or visual detail for decoration; no row or node target exists.
A table that restates synthesis sentences is rejected.
Include only data that answers the stated analytic question.

Neutral intent examples (bracketed details are placeholders, not facts):
- Compare a command with its documented effect: [command] produces [effect].
- Compare component functions or observations from different sources: [role A]
  and [role B]; label this comparison, not an execution sequence.
- Compare documented protocol channels/fields or control-plane and data-plane
  flows; show an infrastructure timeline or annotated sequence only when the
  roles, dates, endpoints, and relations are documented in cited evidence.

Do not use JSON, Markdown tables, D2, Mermaid, code, HTML, SVG, Typst, or
generated render syntax. Keep every chart point, table cell, diagram element,
and figure label grounded in the supplied text and evidence handles.

A CHART has only KIND: timeline. Use one POINT block per documented observation.
DATE must be an exact ISO date or timezone-qualified ISO timestamp; LABEL and
SERIES must occur in that point's evidence. Every point needs EVIDENCE handles.
Do not estimate dates, turn relative wording into exact dates, interpolate
between observations, or provide colors.

Each column is a COLUMN block; each row is a ROW block with one CELL per column.
Each diagram uses NODE, RELATION, and optional GROUP blocks. Every row, node,
and relation needs existing evidence handles. Each cell must be grounded by its
row's handles. Each NODE requires ROLE: actor | victim | malware_tool |
infrastructure | data_artifact | technique_step | unknown. RELATION_TYPE is required;
choose one value from factual | inference | comparison. A factual relation needs a cited
evidence item
whose text/context mentions both endpoints and supports the relation label. If
evidence supports endpoints separately but the relationship is analysis, type it
as inference and cite supporting handles. Type and label comparisons as
comparison; do not present them as infection sequences. infection_chain is a
DIAGRAM KIND only; use RELATION_TYPE: factual only when cited evidence documents
the stated sequence. Reject links marked counter-indicated or
LINK_NOT_DEMONSTRATED in reserve context. Never invent evidence or handles.
Preserve supplied placement anchors and section indexes.

FIGURE P001
FIGURE_HANDLE: F001
CAPTION: legende que tu rediges, dans la langue de publication, de ce que montre l'image
PURPOSE: What does this source figure already explain?
DATA: The evidence-supported content shown by this source figure.
GAIN: The source figure makes this information clearer than prose.
SCOPE: Only the information shown by the selected source figure.
PURPOSE_EVIDENCE: E001
LIMITS: Do not infer details that are not documented by the evidence.
EVIDENCE: E001
PLACEMENT: after_section
SECTION_INDEX: 2
PLACEMENT_REASON: Place beside the paragraph that discusses this information.
REASON: pourquoi cette image aide ce sujet et pourquoi ici
END FIGURE

NEEDS N001
KIND: MEDIA
REASON: what relevant media is missing
QUERY_HINT: short query bounded to the current subject
END NEEDS

Figures : sélectionne zéro ou une image seulement si elle améliore réellement la compréhension;
plusieurs images ne sont permises que si chacune répond à un besoin analytique distinct. Quand
la recherche Web est disponible, consulte SOURCE_PAGE_URL et identifie ce que montre réellement
chaque figure. Évalue FIGURE_HANDLE avec SOURCE_PAGE_URL, ASSET_URL, ALT, SOURCE_CAPTION,
NEARBY_HEADING, CONTEXT_BEFORE, CONTEXT_AFTER, WIDTH, HEIGHT et EVIDENCE. Les dimensions
extrêmes sont une alerte de forme, pas une raison de rejet : une timeline ou une architecture
panoramique peut être informative.

Critères positifs : architecture, chaîne d'exploitation ou chaîne malware, infrastructure,
capture réseau, capture de code ou artefact directement informative, screenshot de leurre,
visualisation d'analyse et graphique source. La capture de journalisation BlueMoon est utile
parce que le contenu technique de la capture constitue lui-même l'information.

Critères négatifs : logo, header/footer, bannière, hero décoratif, illustration marketing,
portrait sans valeur analytique, image d'un article adjacent ou figure sans lien direct avec les
evidence handles. Une figure de la mauvaise source ne peut pas être rattachée à ce sujet.

Sélectionne uniquement un FIGURE_HANDLE fourni par AutoWork, jamais une URL d'image. Une URL,
un média ou une description inventés ne sont pas des preuves. Utilise uniquement les evidence
handles listés pour cette figure et vérifie que chaque preuve vient du même article. Le purpose
de la figure décrit la question analytique à laquelle le média répond. Zéro figure est une
décision valide.

Rédige une caption courte et descriptive, dans la langue de publication, qui dit ce que montre
l'image sans renforcer une attribution ni ajouter de détail non vérifié. Si tu ne peux pas
proposer de caption fiable, omets la figure; le code peut utiliser SOURCE_CAPTION puis ALT comme
repli. Sans caption modèle, caption source ou alt exploitable, la figure ne sera pas publiée.
Choisis un emplacement valide avec PLACEMENT et SECTION_INDEX. NEEDS est facultatif et ne doit
décrire qu'un média analytique pertinent manquant. Ne renvoie aucune URL dans la réponse."""
    )


def build_editorial_enrichment_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    access_policy: SynthesisAccessPolicyV1,
    source_figure_inventory_hash: str | None = None,
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = (),
    resource_search_enabled: bool = False,
) -> ModelRequest:
    if (
        run.id != snapshot.production_run_id
        or run.subject_id != snapshot.subject_id
        or extraction.subject_id != snapshot.subject_id
        or synthesis.subject_id != snapshot.subject_id
    ):
        raise ValueError("Editorial enrichment request identities differ")
    expected_source_ids = tuple(
        sorted((source.source_document_id for source in extraction.sources), key=str)
    )
    policy_source_ids = tuple(record.source_document_id for record in access_policy.sources)
    if (
        access_policy.subject_tlp is not snapshot.subject_tlp
        or policy_source_ids != expected_source_ids
        or evidence_pack.publication_language != synthesis.publication_language
    ):
        raise ValueError("Editorial enrichment request policy does not match its inputs")
    pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    access_hash = synthesis_access_policy_hash(access_policy)
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=pack_hash,
        access_policy_hash=access_hash,
        source_figure_inventory_hash=source_figure_inventory_hash,
        projection_hash=evidence_pack.projection_hash,
        resource_search_enabled=resource_search_enabled,
    )
    invocation_hash = compute_editorial_enrichment_invocation_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=pack_hash,
        access_policy_hash=access_hash,
        source_figure_inventory_hash=source_figure_inventory_hash,
        projection_hash=evidence_pack.projection_hash,
    )
    prompt_payload = {
        "instructions": (
            "Tu es un arbitre de représentations éditoriales, pas un chercheur ni un renderer. "
            "Commence par identifier une question analytique pour chaque besoin. Pour chaque "
            "question analytique : 1. Une figure de la publication source répond-elle déjà "
            "correctement à la question ? OUI → FIGURE SOURCE. 2. Sinon, les informations "
            "sont-elles principalement temporelles ou quantitatives ? OUI → CHART. 3. Sinon, "
            "faut-il montrer des relations, une séquence, une architecture, un routage "
            "ou un flux ? "
            "OUI → DIAGRAM. "
            "4. Sinon, faut-il comparer plusieurs objets selon les mêmes champs ou représenter une "
            "correspondance structurée ? OUI → TABLE. 5. Sinon → aucun enrichissement. CHART "
            "accepte uniquement KIND: timeline, avec des POINT blocks; chaque date ISO exacte, "
            "label et série doit être présente dans les preuves du point, chaque point cite au "
            "moins un evidence handle, et aucune date relative ou interpolation n'est permise. "
            "Les couleurs sont choisies par le renderer. "
            "Une représentation existante de la source est prioritaire sur une représentation "
            "reconstruite, lorsque les deux répondent à la même question analytique. Deux "
            "enrichissements ne doivent pas répondre à la même question analytique. Aucun quota "
            "minimal de médias ne s'applique; RIEN est une décision valide. Choisis une "
            "représentation uniquement quand elle clarifie les preuves mieux que la prose. Chaque "
            "FIGURE, chart, table et "
            "diagramme doit renseigner PURPOSE (question du lecteur), DATA, GAIN "
            "(comprehension gain : avantage sur le paragraphe), SCOPE, "
            "PURPOSE_EVIDENCE, LIMITS, PLACEMENT et "
            "PLACEMENT_REASON; tous sont obligatoires et non vides. Refuse un tableau qui "
            "reformule seulement les phrases de synthèse. N'impose aucun minimum de lignes ou de "
            "nœuds et n'ajoute aucune complexité décorative. Zéro table et zéro diagramme sont "
            "valides lorsque la prose suffit. N'ajoute aucun fait, "
            "n'effectue aucune recherche pendant cet appel, et utilise uniquement les preuves "
            "fournies. Chaque point, ligne, nœud et arête cite des evidence handles existants et "
            "pertinents. Une relation factuelle exige un handle dont le texte/contexte mentionne "
            "les deux endpoints et soutient le libellé de la relation. RELATION_TYPE est "
            "obligatoire : RELATION_TYPE: factual | inference | comparison, avec une seule "
            "valeur choisie. Une inférence doit être typée inference et citée par ses handles "
            "de support. Une comparaison doit être typée et étiquetée comparison; ne la présente "
            "jamais comme une séquence d'infection. infection_chain est uniquement une valeur de "
            "KIND pour un DIAGRAM. Dans ce diagramme, utilise RELATION_TYPE factual seulement si "
            "la preuve citée documente les deux endpoints et la séquence affirmée. Pour les "
            "diagrammes : choisis PROFILE parmi FLOW, ARCHITECTURE et RELATIONSHIP. Ce profil "
            "indique seulement la composition; le code local applique les formes et couleurs "
            "sémantiques dans le même renderer D2. FLOW convient aux chaînes d'infection, "
            "d'exploitation, d'exfiltration, au C2 et au chargement de malware. ARCHITECTURE "
            "convient à une victime, un reverse proxy, des services backend, un C2 ou les "
            "composants d'un framework. RELATIONSHIP convient aux graphes document→fichier, "
            "fichier→URL, domaine→IP, échantillon→parent et pivots de hunting; prends comme "
            "référence de composition la figure 21 du bulletin RU, qui relie documents, fichiers "
            "déposés et URL. Cette référence n'ajoute aucune preuve au dossier courant. D2 sert "
            "aux relations, séquences, architectures et flux; les séries temporelles vont dans "
            "CHART et les correspondances exactes dans TABLE. Chaque diagramme fournit une "
            "CAPTION qui porte le détail utile. Un nœud est un nom court et une relation une "
            "action courte : bons nœuds « Malware », « OP_RETURN », « C2 », « Serveur de dépôt »; "
            "bonnes relations « lit », « extrait », « résout vers ». Mauvais libellé de nœud : "
            "« Serveur utilisé afin de récupérer dynamiquement "
            "l'adresse de commande et contrôle »; "
            "place ce détail dans la CAPTION et garde le nom du nœud court. Une comparaison "
            "s'utilise pour des objets comparés, jamais pour affirmer une séquence. Pour chaque "
            "RELATION, indique DIRECTION: directed uniquement si les preuves attestent le sens "
            "FROM vers TO; sinon indique undirected. Les comparaisons sont toujours undirected. "
            "Les relations factuelles sont pleines, les inférences pointillées. N'ajoute un GROUP "
            "que s'il nomme un vrai périmètre fonctionnel (par exemple Victime, Blockchain "
            "publique, Infrastructure opérateur ou Backend); aucun groupe décoratif. N'utilise "
            "pas de pictogrammes. Le modèle ne fournit jamais shape, fill, stroke, coordonnées, "
            "x, y, SVG ou D2; le code local décide la forme et les couleurs. "
            "graphes : limite-toi à 8 nœuds; chaque nœud exprime une seule idée, avec "
            "6 mots et environ 40 caractères au maximum; un libellé d'arête a au plus 5 mots. "
            "Choisis DIRECTION parmi left_to_right et top_to_bottom selon les seuils de "
            "diagram_layout_budgets; ils s'appliquent aux libellés des nœuds, relations et "
            "groupes. Choisis le rôle parmi actor, victim, malware_tool, "
            "infrastructure, data_artifact, technique_step et unknown. Utilise network_flow pour "
            "un flux de résolution, de données ou de paiement; infrastructure pour les hôtes, "
            "services et connexions; component_relationship pour les liens entre composants. "
            "Réserve infection_chain aux étapes ordonnées d'une intrusion explicitement "
            "documentées. Écris le TITLE comme une courte légende descriptive en français, sans "
            "préfixe « Figure » ni point final. Respecte les réserves counter_indicated et "
            "LINK_NOT_DEMONSTRATED; elles ne prouvent aucun lien. Conserve chaque commande, "
            "chemin, nom, date, adresse, hash et autre littéral exactement comme dans la preuve. "
            "Si aucune représentation n'améliore la compréhension, renvoie le marqueur explicite "
            "prévu. Ne génère ni JSON, tableau Markdown, HTML, Mermaid, D2, DOT, TikZ, Typst, "
            "SVG, image source, ni corps de règle. Les diagrammes décrivent seulement une "
            "spécification sémantique en blocs. Titres et captions restent descriptifs. Le "
            "catalogue figure_catalog contient des médias archivés : propose uniquement un "
            "handle accepté, avec une caption que tu rédiges toi-même dans la langue de "
            "publication, des evidence handles de la même source, un placement et une raison. "
            "Juge chaque figure du catalogue d'après son contexte dans l'article (titre de "
            "section, texte avant et après l'image, nom de fichier) et ouvre source_page_url "
            "pour la confirmer; ne retiens que celles qui illustrent ce sujet précis. N'inclus "
            "pas un média rejeté ou en attente d'archivage. Zéro figure est valide. Si une figure "
            "ou une analyse manque, tu peux émettre un bloc NEEDS MEDIA ou TECHNICAL_ANALYSIS; "
            "indique une raison et un "
            "query_hint borné au sujet. N'inclus aucune URL dans NEEDS."
        ),
        "publication_language": evidence_pack.publication_language,
        "current_synthesis": dict(evidence_pack.current_synthesis),
        "current_evidence_pack": {
            "schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
            "policy_version": evidence_pack.policy_version,
            "relevance_projection_hash": evidence_pack.projection_hash,
            "narrative_evidence": [dict(record) for record in evidence_pack.narrative_evidence],
            "technical_evidence": [dict(record) for record in evidence_pack.technical_evidence],
            "reserves_and_contradictions_non_authoritative": {
                "evidence": [dict(record) for record in evidence_pack.reserve_evidence],
                "source_pair_relations": [
                    dict(record) for record in evidence_pack.source_pair_relations
                ],
            },
        },
        "figure_catalog": [
            entry.prompt_record(
                include_source_page=True,
                include_asset_url=True,
                evidence_handles=evidence_pack.evidence_handles_for_source(
                    entry.figure.source_document_id
                ),
            )
            for entry in figure_catalog
        ],
        "editorial_guidance": {
            "table_kinds": [item.value for item in EnrichmentTableKind],
            "diagram_kinds": [item.value for item in EnrichmentDiagramKind],
            "diagram_directions": [item.value for item in EnrichmentDiagramDirection],
            "diagram_node_roles": [item.value for item in DiagramNodeRole],
            "diagram_profiles": {
                "flow": (
                    "Documented infection, exploitation, exfiltration, C2, and malware-loading "
                    "sequences."
                ),
                "architecture": "Victim, proxy, backend, C2, and framework components.",
                "relationship": (
                    "Document, file, URL, domain, IP, sample, parent, and hunting pivots; "
                    "composition reference: figure 21 of the Russian bulletin."
                ),
            },
            "diagram_layout_budgets": {
                "maximum_nodes": 8,
                "maximum_node_words": 6,
                "maximum_node_characters": 40,
                "maximum_edge_label_words": 5,
                "vertical_when_nodes_over": DIAGRAM_VERTICAL_AFTER_NODES,
                "vertical_when_label_characters_over": DIAGRAM_VERTICAL_AFTER_LABEL_CHARACTERS,
            },
            "placements": [item.value for item in EnrichmentPlacementKind],
            "section_indexes": [
                int(item["section_index"]) for item in evidence_pack.current_synthesis["sections"]
            ],
            "analytic_validation_policy_version": EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION,
        },
        "output_contract": editorial_enrichment_output_contract_example(),
        "output_contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    }
    prompt = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(str(record.source_document_id) in prompt for record in access_policy.sources):
        raise ValueError("Source document identities cannot appear in Editorial Enrichment prompt")
    metadata = {
        "editorial_enrichment_input_hash": input_hash,
        "editorial_enrichment_invocation_hash": invocation_hash,
        "access_policy_hash": access_hash,
        "effective_tlp": access_policy.effective_tlp.value,
        "external_llm_allowed": access_policy.external_llm_allowed,
        "do_not_submit": access_policy.do_not_submit,
        "evidence_pack_hash": pack_hash,
        "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        "routing_policy_version": EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
        "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
        "analytic_validation_policy_version": EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION,
    }
    if source_figure_inventory_hash is not None:
        metadata["source_figure_inventory_hash"] = source_figure_inventory_hash
    return ModelRequest(
        text=prompt,
        prompt_template_id="production-editorial-enrichment",
        prompt_template_version=EDITORIAL_ENRICHMENT_PROMPT_VERSION,
        evidence_pack_hash=pack_hash,
        external_llm_allowed=access_policy.external_llm_allowed and not access_policy.do_not_submit,
        routing_hint=ModelRoutingHint.EDITORIAL_ENRICHMENT,
        sensitivity=access_policy.effective_tlp.value,
        # The model must look at the live article's images to pick figures.
        web_search=any(
            entry.figure.decision is SourceFigureDecision.ACCEPTED for entry in figure_catalog
        ),
        background=False,
        conversation=None,
        run_id=editorial_enrichment_model_run_id(run, invocation_hash),
        allow_failed_resubmit=True,
        metadata=metadata,
        parameters={
            "evidence_pack_schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
            "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
            "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
            "analytic_validation_policy_version": EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION,
        },
    )


def build_editorial_enrichment_repair_request(
    base_request: ModelRequest,
    *,
    raw_output_sha256: str,
    rejected_blocks: tuple[EditorialEnrichmentRejectedBlock, ...],
) -> tuple[ModelRequest, str]:
    """Build one deterministic repair request with the original admitted proofs."""
    if not rejected_blocks or base_request.run_id is None:
        raise ValueError("Editorial enrichment repair requires rejected blocks and a base run")
    if len(raw_output_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in raw_output_sha256
    ):
        raise ValueError("Editorial enrichment repair requires the first output hash")
    base_payload = json.loads(base_request.text)
    identity_payload = {
        "base_run_id": str(base_request.run_id),
        "base_output_sha256": raw_output_sha256,
        "evidence_pack_hash": base_request.evidence_pack_hash,
        "repair_prompt_version": EDITORIAL_ENRICHMENT_REPAIR_PROMPT_VERSION,
        "repair_contract_version": EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION,
        "prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
        "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
        "rejected_blocks": [
            {
                "kind": item.kind,
                "block_id": item.block_id,
                "scope_id": item.scope_id,
                "raw_sha256": item.raw_sha256,
                "reason_codes": list(item.reason_codes),
            }
            for item in rejected_blocks
        ],
    }
    identity = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(identity_payload)
    ).hexdigest()
    repair_run_id = uuid5(NAMESPACE_URL, f"production-editorial-enrichment-repair:{identity}")
    prompt_payload = {
        "instructions": (
            "Repair only the rejected TABLE, CHART, DIAGRAM, or FIGURE blocks listed below. Return "
            "corrected replacements in the same plain-text block format and preserve each exact "
            "analytic question and all required purpose fields, including for FIGURE blocks. "
            "Respect the source-figure priority and never make two repaired or existing blocks "
            "answer the same normalized analytic question. "
            "block kind and local header id. Use only the current synthesis, evidence pack, "
            "figure catalog, and exact evidence handles already supplied here. Add no facts, "
            "handles, sources, or blocks; do not return accepted siblings. Fix only the listed "
            "machine error codes. If a block cannot be corrected from this evidence, omit it."
        ),
        "publication_language": base_payload["publication_language"],
        "current_synthesis": base_payload["current_synthesis"],
        "current_evidence_pack": base_payload["current_evidence_pack"],
        "figure_catalog": base_payload["figure_catalog"],
        "editorial_guidance": base_payload["editorial_guidance"],
        "output_contract": base_payload["output_contract"],
        "output_contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        "repair_contract_version": EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION,
        "rejected_blocks": [
            {
                "kind": item.kind,
                "block_id": item.block_id,
                "reason_codes": list(item.reason_codes),
                "wire_block": item.raw_text,
            }
            for item in rejected_blocks
        ],
    }
    request = ModelRequest(
        text=json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        prompt_template_id="production-editorial-enrichment-repair",
        prompt_template_version=EDITORIAL_ENRICHMENT_REPAIR_PROMPT_VERSION,
        evidence_pack_hash=base_request.evidence_pack_hash,
        external_llm_allowed=base_request.external_llm_allowed,
        routing_hint=base_request.routing_hint,
        sensitivity=base_request.sensitivity,
        metadata={
            **base_request.metadata,
            "editorial_enrichment_repair_identity": identity,
            "editorial_enrichment_repair_base_run_id": str(base_request.run_id),
            "editorial_enrichment_repair_base_output_sha256": raw_output_sha256,
            "editorial_enrichment_repair_contract_version": (
                EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION
            ),
        },
        parameters={
            **base_request.parameters,
            "contract_version": EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION,
            "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
        },
        web_search=base_request.web_search,
        background=False,
        conversation=None,
        run_id=repair_run_id,
    )
    return request, identity


def _validate_plain_editorial_text(value: str) -> None:
    try:
        _validate_plain_text(value)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        ) from exc
    if _RENDERER_SYNTAX.search(value):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )


def _validate_ungrounded_editorial_text(value: str) -> None:
    """Column and group labels carry no evidence, so they cannot state a fact."""
    _validate_plain_editorial_text(value)
    if _technical_literals(value) or _date_literals(value):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE
        )


def _all_refs_for_handles(
    handles: tuple[str, ...], pack: EditorialEnrichmentEvidencePackV1
) -> tuple[ExtractionEvidenceRefV1, ...]:
    return tuple(pack.resolve_handle(handle) for handle in handles)


def _validate_grounded_editorial_text(
    value: str,
    refs: tuple[ExtractionEvidenceRefV1, ...],
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    technical_support: Mapping[tuple[str, str], set[ExtractionEvidenceRefV1]],
) -> None:
    _validate_plain_editorial_text(value)
    ref_set = set(refs)
    for literal in _technical_literals(value):
        support = technical_support.get(literal, set())
        if not support or not support.intersection(ref_set):
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE
            )
    for date_key in _date_literals(value):
        if not any(_date_supported_by_payload(entries[ref], date_key) for ref in refs):
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE
            )


def _validate_exact_evidence_text(
    value: str,
    refs: tuple[ExtractionEvidenceRefV1, ...],
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
) -> None:
    needle = " ".join(value.split()).casefold()
    if not needle or not any(
        needle in " ".join(text.split()).casefold()
        for ref in refs
        for text in _string_values(entries[ref])
    ):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.UNKNOWN_TECHNICAL_VALUE
        )


def validate_editorial_enrichment_proposal(
    proposal: EditorialEnrichmentProposalV1 | Mapping[str, Any],
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    *,
    source_figures: tuple[SourceFigureCandidateV1, ...] = (),
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = (),
    resource_proposals: tuple[ResourceProposalV1, ...] = (),
    resource_model_run_id: UUID | None = None,
    warnings: tuple[str, ...] = (),
) -> EditorialEnrichmentV1:
    """Resolve exact handles, ground technical literals, and build canonical V1."""
    try:
        parsed = (
            proposal
            if isinstance(proposal, EditorialEnrichmentProposalV1)
            else EditorialEnrichmentProposalV1.model_validate(proposal)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        ) from exc
    if not isinstance(evidence_pack, EditorialEnrichmentEvidencePackV1):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )
    if (
        len(parsed.figures) > MAX_ENRICHMENT_FIGURE_PROPOSALS
        or len(parsed.resource_needs) > MAX_ENRICHMENT_RESOURCE_NEEDS
        or len(resource_proposals) > MAX_ENRICHMENT_RESOURCE_PROPOSALS
    ):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )

    entries = _all_evidence_entries(extraction)
    handle_refs = set(evidence_pack._handle_to_ref.values())
    if not handle_refs <= set(entries):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE
        )
    technical_support_mutable: dict[tuple[str, str], set[ExtractionEvidenceRefV1]] = defaultdict(
        set
    )
    for ref, payload in entries.items():
        for value in _string_values(payload):
            for literal in _technical_literals(value):
                technical_support_mutable[literal].add(ref)
    technical_support = dict(technical_support_mutable)
    section_count = len(synthesis.sections)
    paragraph_text_by_anchor = semantic_annotation_anchor_texts(synthesis)
    annotations: list[SemanticAnnotationProposalV1] = []
    for annotation in parsed.annotations:
        anchored_text = paragraph_text_by_anchor.get(annotation.paragraph_anchor)
        if anchored_text is None:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        if annotation.text not in anchored_text or annotation.role is SemanticRole.TEXT:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        annotations.append(_proposal_annotation_to_domain(annotation))

    def placement(value: EnrichmentPlacementProposalV1) -> EnrichmentPlacementV1:
        if value.kind is EnrichmentPlacementKind.AFTER_SECTION and (
            value.section_index is None or value.section_index >= section_count
        ):
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.PLACEMENT_INVALID
            )
        return EnrichmentPlacementV1(kind=value.kind, section_index=value.section_index)

    tables: list[TableSpecV1] = []
    charts: list[ChartSpecV1] = []
    diagrams: list[DiagramSpecV1] = []
    for table in parsed.tables:
        if len(table.columns) < 2 or not table.rows:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        for column in table.columns:
            _validate_ungrounded_editorial_text(column.label)
        rows: list[TableRowV1] = []
        table_refs: set[ExtractionEvidenceRefV1] = set()
        for row in table.rows:
            if len(row.cells) != len(table.columns):
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
                )
            refs = _all_refs_for_handles(row.evidence_handles, evidence_pack)
            table_refs.update(refs)
            for cell in row.cells:
                _validate_grounded_editorial_text(cell, refs, entries, technical_support)
            rows.append(TableRowV1(cells=row.cells, evidence_refs=refs))
        for value in (table.title, table.caption or ""):
            _validate_grounded_editorial_text(
                value,
                tuple(sorted(table_refs, key=evidence_ref_sort_key)),
                entries,
                technical_support,
            )
        purpose_refs = _all_refs_for_handles(table.purpose.evidence_handles, evidence_pack)
        if not set(purpose_refs) <= table_refs:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        for value in (
            table.purpose.question,
            table.purpose.available_data,
            table.purpose.comprehension_gain,
            table.purpose.scope,
            table.purpose.knowledge_limits,
            table.purpose.placement_reason,
        ):
            _validate_grounded_editorial_text(value, purpose_refs, entries, technical_support)
        try:
            tables.append(
                TableSpecV1(
                    key=table.key,
                    kind=table.kind,
                    title=table.title,
                    caption=table.caption,
                    columns=tuple(
                        TableColumnV1(key=item.key, label=item.label) for item in table.columns
                    ),
                    rows=tuple(rows),
                    placement=placement(table.placement),
                    purpose=EditorialAnalyticPurposeV1(
                        question=table.purpose.question,
                        available_data=table.purpose.available_data,
                        comprehension_gain=table.purpose.comprehension_gain,
                        scope=table.purpose.scope,
                        evidence_refs=purpose_refs,
                        knowledge_limits=table.purpose.knowledge_limits,
                        placement_reason=table.purpose.placement_reason,
                    ),
                )
            )
        except ValueError as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            ) from exc

    for diagram in parsed.diagrams:
        if len(diagram.nodes) < 2 or not diagram.edges:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        # Domain invariants of nodes, edges and groups are model output defects too.
        try:
            nodes: list[DiagramNodeV1] = []
            edges: list[DiagramEdgeV1] = []
            groups: list[DiagramGroupV1] = []
            diagram_refs: set[ExtractionEvidenceRefV1] = set()
            for node in diagram.nodes:
                refs = _all_refs_for_handles(node.evidence_handles, evidence_pack)
                diagram_refs.update(refs)
                _validate_grounded_editorial_text(node.label, refs, entries, technical_support)
                nodes.append(
                    DiagramNodeV1(
                        node_id=node.node_id,
                        label=node.label,
                        evidence_refs=refs,
                        role=node.role,
                    )
                )
            for edge in diagram.edges:
                refs = _all_refs_for_handles(edge.evidence_handles, evidence_pack)
                diagram_refs.update(refs)
                if edge.label is not None:
                    _validate_grounded_editorial_text(edge.label, refs, entries, technical_support)
                edges.append(
                    DiagramEdgeV1(
                        source_node_id=edge.source_node_id,
                        target_node_id=edge.target_node_id,
                        label=edge.label,
                        evidence_refs=refs,
                        relation_type=edge.relation_type,
                        direction=edge.direction,
                    )
                )
            for group in diagram.groups:
                _validate_ungrounded_editorial_text(group.label)
                groups.append(
                    DiagramGroupV1(
                        group_id=group.group_id,
                        label=group.label,
                        node_ids=group.node_ids,
                    )
                )
            for value in (diagram.title, diagram.caption or ""):
                _validate_grounded_editorial_text(
                    value,
                    tuple(sorted(diagram_refs, key=evidence_ref_sort_key)),
                    entries,
                    technical_support,
                )
            purpose_refs = _all_refs_for_handles(diagram.purpose.evidence_handles, evidence_pack)
            if not set(purpose_refs) <= diagram_refs:
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
                )
            for value in (
                diagram.purpose.question,
                diagram.purpose.available_data,
                diagram.purpose.comprehension_gain,
                diagram.purpose.scope,
                diagram.purpose.knowledge_limits,
                diagram.purpose.placement_reason,
            ):
                _validate_grounded_editorial_text(value, purpose_refs, entries, technical_support)
            diagrams.append(
                DiagramSpecV1(
                    key=diagram.key,
                    kind=diagram.kind,
                    title=diagram.title,
                    caption=diagram.caption,
                    direction=diagram.direction,
                    nodes=tuple(nodes),
                    edges=tuple(edges),
                    groups=tuple(groups),
                    placement=placement(diagram.placement),
                    purpose=EditorialAnalyticPurposeV1(
                        question=diagram.purpose.question,
                        available_data=diagram.purpose.available_data,
                        comprehension_gain=diagram.purpose.comprehension_gain,
                        scope=diagram.purpose.scope,
                        evidence_refs=purpose_refs,
                        knowledge_limits=diagram.purpose.knowledge_limits,
                        placement_reason=diagram.purpose.placement_reason,
                    ),
                    profile=diagram.profile,
                )
            )
        except ValueError as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            ) from exc

    for chart in parsed.charts:
        if not chart.points:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        points: list[ChartPointV1] = []
        chart_refs: set[ExtractionEvidenceRefV1] = set()
        for point in chart.points:
            refs = _all_refs_for_handles(point.evidence_handles, evidence_pack)
            if not refs:
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE
                )
            _validate_grounded_editorial_text(point.date, refs, entries, technical_support)
            _validate_exact_evidence_text(point.date, refs, entries)
            _validate_grounded_editorial_text(point.label, refs, entries, technical_support)
            _validate_exact_evidence_text(point.label, refs, entries)
            _validate_grounded_editorial_text(point.series, refs, entries, technical_support)
            _validate_exact_evidence_text(point.series, refs, entries)
            chart_refs.update(refs)
            points.append(
                ChartPointV1(
                    label=point.label,
                    date=point.date,
                    series=point.series,
                    evidence_refs=refs,
                )
            )
        ordered_chart_refs = tuple(sorted(chart_refs, key=evidence_ref_sort_key))
        _validate_grounded_editorial_text(
            chart.title, ordered_chart_refs, entries, technical_support
        )
        if chart.caption is not None:
            _validate_grounded_editorial_text(
                chart.caption, ordered_chart_refs, entries, technical_support
            )
        purpose_refs = _all_refs_for_handles(chart.purpose.evidence_handles, evidence_pack)
        if not purpose_refs or not set(purpose_refs) <= chart_refs:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        for value in (
            chart.purpose.question,
            chart.purpose.available_data,
            chart.purpose.comprehension_gain,
            chart.purpose.scope,
            chart.purpose.knowledge_limits,
            chart.purpose.placement_reason,
        ):
            _validate_grounded_editorial_text(value, purpose_refs, entries, technical_support)
        try:
            charts.append(
                ChartSpecV1(
                    key=chart.key,
                    kind=chart.kind,
                    title=chart.title,
                    caption=chart.caption,
                    placement=placement(chart.placement),
                    purpose=EditorialAnalyticPurposeV1(
                        question=chart.purpose.question,
                        available_data=chart.purpose.available_data,
                        comprehension_gain=chart.purpose.comprehension_gain,
                        scope=chart.purpose.scope,
                        evidence_refs=purpose_refs,
                        knowledge_limits=chart.purpose.knowledge_limits,
                        placement_reason=chart.purpose.placement_reason,
                    ),
                    points=tuple(points),
                )
            )
        except ValueError as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            ) from exc

    catalog_by_handle = {entry.handle: entry for entry in figure_catalog}
    proposed_by_handle = {item.figure_handle: item for item in parsed.figures}
    if len(proposed_by_handle) != len(parsed.figures) or not set(proposed_by_handle) <= set(
        catalog_by_handle
    ):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )
    figure_decisions: list[EditorialFigureDecisionTraceV1] = []
    selected_source_figures = list(source_figures)
    local_warnings = set(warnings)
    if not parsed.figures and any(
        entry.figure.decision is SourceFigureDecision.ACCEPTED for entry in figure_catalog
    ):
        local_warnings.add("editorial_enrichment_no_figure_selected")
    for catalog_entry in figure_catalog:
        proposed = proposed_by_handle.get(catalog_entry.handle)
        figure = catalog_entry.figure
        evidence_refs: tuple[ExtractionEvidenceRefV1, ...] = ()
        if proposed is not None:
            if figure.decision is not SourceFigureDecision.ACCEPTED:
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
                )
            evidence_refs = _all_refs_for_handles(proposed.evidence_handles, evidence_pack)
            if not evidence_refs or any(
                ref.source_document_id != figure.source_document_id for ref in evidence_refs
            ):
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE
                )
            purpose_refs = _all_refs_for_handles(proposed.purpose.evidence_handles, evidence_pack)
            if not purpose_refs or not set(purpose_refs) <= set(evidence_refs):
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
                )
            for value in (
                proposed.purpose.question,
                proposed.purpose.available_data,
                proposed.purpose.comprehension_gain,
                proposed.purpose.scope,
                proposed.purpose.knowledge_limits,
                proposed.purpose.placement_reason,
            ):
                _validate_grounded_editorial_text(value, purpose_refs, entries, technical_support)
            figure_purpose = EditorialAnalyticPurposeV1(
                question=proposed.purpose.question,
                available_data=proposed.purpose.available_data,
                comprehension_gain=proposed.purpose.comprehension_gain,
                scope=proposed.purpose.scope,
                evidence_refs=purpose_refs,
                knowledge_limits=proposed.purpose.knowledge_limits,
                placement_reason=proposed.purpose.placement_reason,
            )
            # The model writes the caption after looking at the image; only an
            # empty or non-plain one falls back to the archived source caption.
            caption = proposed.caption.strip()
            if not caption:
                source_caption = catalog_entry.source_caption
                if source_caption is None:
                    raise EditorialEnrichmentProposalControlError(
                        EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
                    )
                caption = source_caption
                local_warnings.add(
                    f"editorial_enrichment_figure_caption_downgraded:{catalog_entry.handle}"
                )
            _validate_plain_editorial_text(caption)
            try:
                selected_source_figures.append(
                    SourceFigureCandidateV1(
                        key=f"source_figure_{figure.figure_id.hex}",
                        source_document_id=figure.source_document_id,
                        source_url=figure.source,
                        caption=caption,
                        provenance=figure.provenance,
                        locator=figure.locator,
                        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                        placement=placement(proposed.placement),
                        resolved_figure=figure,
                        purpose=figure_purpose,
                    )
                )
            except ValueError as exc:
                raise EditorialEnrichmentProposalControlError(
                    EditorialEnrichmentStageErrorCode.PLACEMENT_INVALID
                ) from exc
            trace_decision = EditorialFigureDecision.INCLUDED_BY_MODEL
            trace_actor = EditorialFigureDecisionActor.MODEL_PROPOSAL
            trace_reason_code = "model_selected"
            trace_reason = proposed.reason
        elif figure.decision is SourceFigureDecision.REJECTED:
            trace_decision = EditorialFigureDecision.EXCLUDED_BY_RULE
            trace_actor = EditorialFigureDecisionActor.DETERMINISTIC_RULE
            trace_reason_code = figure.decision_reason
            trace_reason = figure.decision_reason
        elif figure.decision is SourceFigureDecision.PENDING:
            trace_decision = EditorialFigureDecision.PENDING_ARCHIVE
            trace_actor = EditorialFigureDecisionActor.DETERMINISTIC_RULE
            trace_reason_code = figure.decision_reason
            trace_reason = figure.decision_reason
        else:
            trace_decision = EditorialFigureDecision.NOT_SELECTED_BY_MODEL
            trace_actor = EditorialFigureDecisionActor.MODEL_PROPOSAL
            trace_reason_code = "not_proposed"
            trace_reason = "No FIGURE block selected this accepted catalog item."
        figure_decisions.append(
            EditorialFigureDecisionTraceV1(
                handle=catalog_entry.handle,
                figure_id=figure.figure_id,
                source_document_id=figure.source_document_id,
                decision=trace_decision,
                actor=trace_actor,
                reason_code=trace_reason_code,
                reason=trace_reason,
                policy_version=EDITORIAL_FIGURE_DECISION_POLICY_VERSION,
                prompt_version=EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                contract_version=EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
                parser_version=EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
                evidence_refs=evidence_refs,
            )
        )

    needs = tuple(
        EditorialResourceNeedV1(
            key=item.key,
            kind=item.kind,
            reason=item.reason,
            query_hint=item.query_hint,
            policy_version=EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
        )
        for item in parsed.resource_needs
    )
    if resource_proposals:
        if resource_model_run_id is None:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            )
        persisted_resource_proposals = tuple(
            EditorialResourceProposalV1(
                need_key=item.need_key,
                url=item.url,
                justification=item.justification,
                source_model_run_id=resource_model_run_id,
            )
            for item in resource_proposals
        )
    else:
        persisted_resource_proposals = ()
    if any(
        item.need_key not in {need.key for need in needs} for item in persisted_resource_proposals
    ):
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )

    # Root invariants (e.g. globally unique table/diagram keys) are a model
    # output defect, never an infrastructure error.
    try:
        enrichment = EditorialEnrichmentV1(
            schema_version=EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
            subject_id=synthesis.subject_id,
            production_input_hash=synthesis.production_input_hash,
            extraction_hash=canonical_extraction_hash(extraction),
            synthesis_hash=canonical_synthesis_hash(synthesis),
            publication_language=synthesis.publication_language,
            enrichment_policy_version=EDITORIAL_ENRICHMENT_POLICY_VERSION,
            tables=tuple(tables),
            charts=tuple(charts),
            diagrams=tuple(diagrams),
            source_figures=tuple(selected_source_figures),
            warnings=tuple(local_warnings),
            annotations=tuple(annotations),
            figure_decisions=tuple(figure_decisions),
            resource_needs=needs,
            resource_proposals=persisted_resource_proposals,
        )
        validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
    except EditorialEnrichmentValidationError as exc:
        code = (
            EditorialEnrichmentStageErrorCode.PLACEMENT_INVALID
            if exc.code == "editorial_enrichment_placement_invalid"
            else EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        )
        raise EditorialEnrichmentProposalControlError(code) from exc
    except ValueError as exc:
        raise EditorialEnrichmentProposalControlError(
            EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
        ) from exc
    return enrichment


class _EditorialEnrichmentInputControl(RuntimeError):
    def __init__(
        self,
        code: EditorialEnrichmentStageErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class EditorialEnrichmentRevisionConflictError(RuntimeError):
    """The requested revision no longer targets the current enrichment artifact."""

    code = "editorial_enrichment_stale_base"


class ProductionEditorialEnrichmentService:
    """The canonical stateless Editorial Enrichment drafting service."""

    def __init__(
        self,
        *,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        model_gateway: ModelGateway,
        editorial_enrichment_service: EditorialEnrichmentService,
        artifact_reuse: ProductionArtifactReuseService | None = None,
        media_asset_store: MediaAssetStore | None = None,
        diagram_compiler: DiagramCompiler | None = None,
        chart_compiler: AnalyticChartCompiler | None = None,
        source_media_archiver: SourceMediaArchiveService | None = None,
        resource_search_enabled: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._model_gateway = model_gateway
        self._editorial_enrichment_service = editorial_enrichment_service
        self._artifact_reuse = artifact_reuse
        self._media_asset_store = media_asset_store
        self._source_figure_ingestor = (
            SourceFigureIngestor(media_asset_store) if media_asset_store is not None else None
        )
        self._diagram_compiler = diagram_compiler
        self._chart_compiler = chart_compiler
        self._source_media_archiver = source_media_archiver
        self._resource_search_enabled = resource_search_enabled

    async def execute(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None = None,
    ) -> ProductionEditorialEnrichmentExecution:
        try:
            extraction = await self._load_extraction(run, snapshot, extraction_artifact)
            projection = (
                await self._load_projection(run, snapshot, extraction, projection_artifact)
                if projection_artifact is not None
                else None
            )
            synthesis = await self._load_synthesis(run, snapshot, synthesis_artifact, extraction)
            async with self._uow_factory() as uow:
                try:
                    access_policy = await build_synthesis_access_policy(
                        snapshot,
                        extraction,
                        uow.source_documents,
                        uow.source_collections,
                    )
                except ProductionReuseStorageUnavailableError:
                    raise
                except (TypeError, ValueError) as exc:
                    raise _EditorialEnrichmentInputControl(
                        EditorialEnrichmentStageErrorCode.ACCESS_POLICY_UNAVAILABLE,
                        "The exact source access policy for Editorial Enrichment is unavailable",
                        details={"reason": str(exc)},
                    ) from exc
                source_figure_inventory = await load_archived_source_figure_inventory(
                    subject_id=snapshot.subject_id,
                    extraction_sources=extraction.sources,
                    source_document_repository=uow.source_documents,
                    blob_repository=uow.blobs,
                    artifact_store=self._artifact_store,
                    media_archiver=self._source_media_archiver,
                )
            await self._ingest_source_figures(source_figure_inventory)
            figure_catalog = build_editorial_figure_catalog(extraction, source_figure_inventory)
            evidence_pack = build_editorial_enrichment_evidence_pack(
                snapshot, extraction, synthesis, projection
            )
            evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
            access_policy_hash = synthesis_access_policy_hash(access_policy)
            input_hash = compute_editorial_enrichment_input_hash(
                extraction=extraction,
                synthesis=synthesis,
                evidence_pack_hash=evidence_pack_hash,
                access_policy_hash=access_policy_hash,
                projection_hash=projection.projection_hash if projection is not None else None,
                source_figure_inventory_hash=source_figure_inventory.functional_hash(),
                resource_search_enabled=self._resource_search_enabled,
            )
        except _EditorialEnrichmentInputControl as control:
            return ProductionEditorialEnrichmentExecution(
                status=EditorialEnrichmentExecutionStatus.BLOCKED,
                input_hash=None,
                artifact=None,
                model_run_id=None,
                model_calls=0,
                error_code=control.code.value,
                error_message=str(control),
                details=control.details,
            )

        reused = await self._reuse_exact(
            run=run,
            snapshot=snapshot,
            extraction=extraction,
            synthesis=synthesis,
            input_hash=input_hash,
        )
        if reused is not None:
            return reused
        if access_policy.do_not_submit or not access_policy.external_llm_allowed:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=None,
                model_calls=0,
                error_code=EditorialEnrichmentStageErrorCode.POLICY_BLOCKED,
                error_message="The source access policy forbids any model submission.",
                details={
                    "do_not_submit": access_policy.do_not_submit,
                    "external_llm_allowed": access_policy.external_llm_allowed,
                    "access_policy_hash": access_policy_hash,
                },
            )
        request = build_editorial_enrichment_model_request(
            run,
            snapshot,
            extraction,
            synthesis,
            evidence_pack,
            access_policy,
            source_figure_inventory_hash=source_figure_inventory.functional_hash(),
            figure_catalog=figure_catalog,
            resource_search_enabled=self._resource_search_enabled,
        )
        model_run_id = request.run_id
        if model_run_id is None:
            raise ValueError("Editorial Enrichment request has no invocation identity")
        if (
            request.metadata.get("editorial_enrichment_input_hash") != input_hash
            or request.metadata.get("editorial_enrichment_invocation_hash") is None
            or request.run_id != model_run_id
        ):
            raise ValueError("Editorial Enrichment request identity is inconsistent")
        model_calls = 0
        try:
            execution, archive_error = await self._verified_existing_execution(request)
            if archive_error is not None:
                return self._needs_review(
                    input_hash=input_hash,
                    model_run_id=model_run_id,
                    model_calls=0,
                    error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                    error_message=(
                        "The existing Editorial Enrichment response could not be verified"
                    ),
                    details=archive_error,
                )
            if execution is None:
                execution = await self._model_gateway.draft(request)
                model_calls = 1
        except ModelSubmissionReconciliationRequiredError as exc:
            return self._reconciliation_required(
                input_hash=input_hash,
                model_run_id=exc.model_run_id or model_run_id,
                error_message=str(exc),
                details=exc.details,
            )
        except StructuredOutputError as exc:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run_id,
                error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                error_message=str(exc),
            )
        except ExternalModelBlockedError as exc:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run_id,
                error_code=EditorialEnrichmentStageErrorCode.POLICY_BLOCKED,
                error_message=str(exc),
            )
        except ModelGatewayError as exc:
            if exc.retryable:
                raise
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run_id,
                error_code=EditorialEnrichmentStageErrorCode.MODEL_FAILED,
                error_message=str(exc),
                details={"gateway_error_code": exc.code},
            )

        model_run = execution.run
        if model_run_awaits_reconciliation(model_run.error_code):
            return self._reconciliation_required(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_message=model_run.error_message or "Model submission requires reconciliation",
                details=model_run.error_details,
            )
        if model_run.status is not ModelRunStatus.SUCCEEDED:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.MODEL_FAILED,
                error_message=model_run.error_message or "Model run did not succeed",
                details={"model_run_status": model_run.status.value},
            )
        raw_text, raw_error = await self._verified_raw_text(
            model_run,
            expected_text=execution.output_text if model_calls else None,
        )
        if raw_error is not None or raw_text is None:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                error_message="The raw Editorial Enrichment response is not verifiable",
                details={"wire_error_code": (raw_error or {}).get("error_code")},
                model_calls=model_calls,
            )
        parsed = parse_editorial_enrichment_proposal_wire(
            raw_text, evidence_pack, figure_catalog=figure_catalog
        )
        parse_identity = await self._record_wire_parse(
            model_run, evidence_pack, parsed, figure_catalog=figure_catalog
        )
        wire_details: dict[str, Any] = {
            "parse_identity": parse_identity,
            "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
            "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
            "rejections": [
                {
                    "block_id": item.block_id,
                    "scope_id": item.scope_id,
                    "reason_code": item.reason_code,
                    "raw_sha256": item.raw_sha256,
                }
                for item in parsed.rejections
            ],
            "warnings": list(parsed.warnings),
            "transformations": list(parsed.transformations),
        }
        if parsed.error_code is not None:
            wire_details["wire_error_code"] = parsed.error_code
        first_pass = parsed
        if first_pass.rejected_blocks:
            parsed, repair_details, repair_calls = await self._repair_rejected_blocks(
                request=request,
                first_model_run=model_run,
                first_pass=first_pass,
                evidence_pack=evidence_pack,
                figure_catalog=figure_catalog,
            )
            model_calls += repair_calls
            wire_details["repair"] = repair_details
            wire_details["repaired_block_count"] = repair_details["blocks_repaired"]
            wire_details["final_rejections"] = [
                {
                    "block_id": item.block_id,
                    "reason_code": item.reason_code,
                    "raw_sha256": item.raw_sha256,
                    "scope_id": item.scope_id,
                }
                for item in parsed.rejections
            ]
            wire_details["warnings"] = list(parsed.warnings)
            wire_details["transformations"] = list(parsed.transformations)

        target_kinds = {"TABLE", "CHART", "DIAGRAM", "FIGURE"}
        initially_proposed_targets = tuple(
            item for item in first_pass.accepted_blocks if item.kind in target_kinds
        ) + tuple(item for item in first_pass.rejected_blocks if item.kind in target_kinds)
        finally_accepted_targets = tuple(
            item for item in parsed.accepted_blocks if item.kind in target_kinds
        )
        if initially_proposed_targets and not finally_accepted_targets:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.EMPTY_AFTER_REJECTIONS,
                error_message=(
                    "Every proposed table, chart, diagram, or figure was rejected after the "
                    "repair attempt."
                ),
                details=wire_details,
                model_calls=model_calls,
            )
        if parsed.proposal is None:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                error_message="The Editorial Enrichment response contained no valid text blocks",
                details=wire_details,
                model_calls=model_calls,
            )
        resource_proposals: tuple[ResourceProposalV1, ...] = ()
        resource_model_run_id: UUID | None = None
        if parsed.proposal.resource_needs:
            wire_details["resource_needs"] = [
                item.model_dump(mode="json") for item in parsed.proposal.resource_needs
            ]
            if not self._resource_search_enabled:
                wire_details["resource_proposal_search"] = {"status": "disabled_by_configuration"}
            elif not access_policy.external_llm_allowed or access_policy.do_not_submit:
                wire_details["resource_proposal_search"] = {"status": "blocked_by_access_policy"}
            else:
                (
                    resource_model_run_id,
                    resource_proposals,
                    resource_calls,
                    resource_details,
                ) = await self._search_resource_proposals(
                    run,
                    snapshot,
                    access_policy,
                    tuple(
                        EditorialResourceNeedV1(
                            key=item.key,
                            kind=item.kind,
                            reason=item.reason,
                            query_hint=item.query_hint,
                        )
                        for item in parsed.proposal.resource_needs
                    ),
                    enrichment_input_hash=input_hash,
                )
                model_calls += resource_calls
                wire_details["resource_proposal_search"] = resource_details
                wire_details["resource_proposals"] = [
                    {
                        "need_key": item.need_key,
                        "url": item.url,
                        "justification": item.justification,
                    }
                    for item in resource_proposals
                ]
        try:
            enrichment = validate_editorial_enrichment_proposal(
                parsed.proposal,
                evidence_pack,
                extraction,
                synthesis,
                figure_catalog=figure_catalog,
                resource_proposals=resource_proposals,
                resource_model_run_id=resource_model_run_id,
                warnings=(
                    *_source_figure_inventory_warnings(source_figure_inventory),
                    *parsed.warnings,
                ),
            )
        except EditorialEnrichmentProposalControlError as exc:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=exc.code,
                error_message=str(exc),
                details={**wire_details, "validation_error_code": exc.code.value},
                model_calls=model_calls,
            )
        if parsed.rejections:
            enrichment = replace(
                enrichment,
                warnings=(
                    *enrichment.warnings,
                    *(
                        "editorial_enrichment_block_rejected:"
                        f"{item.scope_id or 'unknown'}:{item.block_id}:{item.reason_code}"
                        for item in parsed.rejections
                    ),
                ),
            )
        repaired_block_count = int(wire_details.get("repaired_block_count", 0))
        if repaired_block_count:
            enrichment = replace(
                enrichment,
                warnings=(
                    *enrichment.warnings,
                    f"editorial_enrichment_repair_blocks_repaired:{repaired_block_count}",
                ),
            )
        resource_rejections = (
            wire_details.get("resource_proposal_search", {}).get("rejections", [])
            if isinstance(wire_details.get("resource_proposal_search"), Mapping)
            else []
        )
        resource_error = (
            wire_details.get("resource_proposal_search", {}).get("wire_error_code")
            if isinstance(wire_details.get("resource_proposal_search"), Mapping)
            else None
        )
        if resource_rejections or resource_error:
            enrichment = replace(
                enrichment,
                warnings=(
                    *enrichment.warnings,
                    *(
                        f"editorial_resource_proposal_rejected:{item.get('block_id')}:{item.get('reason_code')}"
                        for item in resource_rejections
                        if isinstance(item, Mapping)
                    ),
                    *(
                        (f"editorial_resource_proposal_error:{resource_error}",)
                        if isinstance(resource_error, str)
                        else ()
                    ),
                ),
            )
        if enrichment.diagrams:
            if self._diagram_compiler is None or self._media_asset_store is None:
                raise RuntimeError("Diagram compilation requires a compiler and media asset store")
            compilation = await compile_and_store_diagrams(
                enrichment.diagrams,
                compiler=self._diagram_compiler,
                media_asset_store=self._media_asset_store,
                production_run_id=run.id,
            )
            enrichment = replace(
                enrichment,
                diagrams=compilation.diagrams,
                warnings=(
                    *enrichment.warnings,
                    *(
                        f"{item.warning_code}:{item.diagram_key}:{item.reason_code}"
                        for item in compilation.rejections
                    ),
                ),
            )
            if compilation.rejections:
                wire_details["diagram_rejections"] = [
                    {
                        "diagram_key": item.diagram_key,
                        "reason_code": item.warning_code,
                        "compiler_error_code": item.reason_code,
                    }
                    for item in compilation.rejections
                ]
            validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
        if enrichment.charts:
            if self._chart_compiler is None or self._media_asset_store is None:
                raise RuntimeError("Chart compilation requires a compiler and media asset store")
            chart_compilation = await compile_and_store_charts(
                enrichment.charts,
                compiler=self._chart_compiler,
                media_asset_store=self._media_asset_store,
                production_run_id=run.id,
            )
            enrichment = replace(
                enrichment,
                charts=chart_compilation.charts,
                warnings=(
                    *enrichment.warnings,
                    *(
                        f"editorial_enrichment_chart_render_failed:{item.chart_key}:"
                        f"{item.reason_code}"
                        for item in chart_compilation.rejections
                    ),
                ),
            )
            if chart_compilation.rejections:
                wire_details["chart_rejections"] = [
                    {
                        "chart_key": item.chart_key,
                        "reason_code": item.reason_code,
                    }
                    for item in chart_compilation.rejections
                ]
            validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
        (
            annotation_proposals,
            annotation_calls,
            annotation_details,
        ) = await self._propose_semantic_annotations(
            run=run,
            snapshot=snapshot,
            access_policy=access_policy,
            enrichment_input_hash=input_hash,
            anchor_texts=semantic_annotation_anchor_texts(synthesis, enrichment),
        )
        model_calls += annotation_calls
        enrichment = replace(
            enrichment,
            annotations=_merge_semantic_annotation_proposals(
                enrichment.annotations, annotation_proposals
            ),
            warnings=(*enrichment.warnings, *annotation_details["warnings"]),
        )
        wire_details["semantic_annotation"] = {
            key: value for key, value in annotation_details.items() if key != "warnings"
        }
        if annotation_details["warnings"]:
            wire_details["semantic_annotation"]["warnings"] = list(annotation_details["warnings"])
        validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
        artifact = await self._editorial_enrichment_service.store_editorial_enrichment_result(
            run_id=run.id,
            subject_id=snapshot.subject_id,
            input_hash=input_hash,
            enrichment=enrichment,
            extraction=extraction,
            synthesis=synthesis,
            raw_result=raw_text,
            model_run_id=model_run.id,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=access_policy_hash,
            projection_hash=projection.projection_hash if projection is not None else None,
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
            source_figure_inventory_hash=source_figure_inventory.functional_hash(),
            metadata_extra={
                "editorial_enrichment_wire_details": wire_details,
                "repaired_block_count": repaired_block_count,
                "source_figure_provenance_version": SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
                "source_figure_provenance_diagnostics": build_source_figure_provenance_diagnostics(
                    source_figure_inventory, figure_catalog, enrichment
                ),
            },
        )
        return ProductionEditorialEnrichmentExecution(
            status=EditorialEnrichmentExecutionStatus.SUCCEEDED,
            input_hash=input_hash,
            artifact=artifact,
            model_run_id=model_run.id,
            model_calls=model_calls,
            table_count=len(enrichment.tables),
            diagram_count=len(enrichment.diagrams),
            source_figure_count=len(enrichment.source_figures),
            chart_count=len(enrichment.charts),
            warnings=enrichment.warnings,
            details=(
                wire_details
                if parsed.rejections
                or parsed.warnings
                or parsed.proposal.resource_needs
                or wire_details.get("repair")
                or wire_details.get("diagram_rejections")
                or wire_details.get("chart_rejections")
                or annotation_details["warnings"]
                or annotation_details["proposal_count"]
                else None
            ),
        )

    async def _propose_semantic_annotations(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        access_policy: SynthesisAccessPolicyV1,
        enrichment_input_hash: str,
        anchor_texts: Mapping[str, str],
    ) -> tuple[
        tuple[SemanticAnnotationProposalV1, ...],
        int,
        dict[str, Any],
    ]:
        """Make an independent, idempotent annotation call with one repair attempt."""
        proposals: list[SemanticAnnotationProposalV1] = []
        calls = 0
        warnings: list[str] = []
        run_ids: list[str] = []
        repair_items: tuple[str, ...] = ()
        status = "empty"
        for attempt in range(2):
            request = build_semantic_annotation_model_request(
                run,
                snapshot,
                access_policy,
                anchor_texts,
                enrichment_input_hash=enrichment_input_hash,
                attempt=attempt,
                repair_items=repair_items,
            )
            if request.run_id is None:
                warnings.append("semantic_annotation_request_identity_missing")
                status = "failed"
                break
            run_ids.append(str(request.run_id))
            try:
                execution, archive_error = await self._verified_existing_execution(request)
                if archive_error is not None:
                    warnings.append("semantic_annotation_existing_output_unverified")
                    status = "unverified_output"
                    if attempt == 0:
                        repair_items = ("The prior archived output could not be verified.",)
                        continue
                    break
                drafted = execution is None
                if execution is None:
                    execution = await self._model_gateway.draft(request)
                    calls += 1
                model_run = execution.run
                if model_run_awaits_reconciliation(model_run.error_code):
                    warnings.append("semantic_annotation_reconciliation_required")
                    status = "reconciliation_required"
                    break
                if model_run.status is not ModelRunStatus.SUCCEEDED:
                    warnings.append("semantic_annotation_model_run_failed")
                    status = "model_failed"
                    if attempt == 0:
                        repair_items = ("The prior model attempt did not complete successfully.",)
                        continue
                    break
                raw_text, raw_error = await self._verified_raw_text(
                    model_run,
                    expected_text=execution.output_text if drafted else None,
                )
                if raw_error is not None or raw_text is None:
                    warnings.append("semantic_annotation_output_unverified")
                    status = "unverified_output"
                    if attempt == 0:
                        repair_items = ("The prior response could not be verified.",)
                        continue
                    break
                parsed = parse_semantic_annotation_wire(raw_text, anchor_texts)
                proposals.extend(parsed.proposals)
                if parsed.error_code is None and not parsed.rejections:
                    status = "accepted" if parsed.proposals else "empty"
                    break
                if parsed.error_code is not None:
                    warnings.append(parsed.error_code)
                warnings.extend(f"{reason}:{item_id}" for item_id, reason in parsed.rejections)
                status = "partial" if parsed.proposals else "rejected"
                if attempt == 0:
                    repair_items = (raw_text[:8000],)
            except ModelSubmissionReconciliationRequiredError:
                warnings.append("semantic_annotation_reconciliation_required")
                status = "reconciliation_required"
                break
            except Exception as exc:
                warnings.append(f"semantic_annotation_call_failed:{type(exc).__name__}")
                status = "model_failed"
                if attempt == 0:
                    repair_items = ("The prior annotation call failed.",)
                    continue
                break
        return (
            _merge_semantic_annotation_proposals((), tuple(proposals)),
            calls,
            {
                "status": status,
                "model_run_ids": run_ids,
                "attempt_count": len(run_ids),
                "proposal_count": len(proposals),
                "warnings": tuple(dict.fromkeys(warnings)),
                "prompt_version": SEMANTIC_ANNOTATION_PROMPT_VERSION,
                "contract_version": SEMANTIC_ANNOTATION_CONTRACT_VERSION,
                "parser_version": SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
            },
        )

    async def _repair_rejected_blocks(
        self,
        *,
        request: ModelRequest,
        first_model_run: ModelRun,
        first_pass: EditorialEnrichmentWireParseResult,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        figure_catalog: tuple[EditorialFigureCatalogEntry, ...],
    ) -> tuple[EditorialEnrichmentWireParseResult, dict[str, Any], int]:
        targets = first_pass.rejected_blocks
        details: dict[str, Any] = {
            "status": "unavailable",
            "blocks_sent": [
                {
                    "kind": item.kind,
                    "block_id": item.block_id,
                    "scope_id": item.scope_id,
                    "raw_sha256": item.raw_sha256,
                    "reason_codes": list(item.reason_codes),
                }
                for item in targets
            ],
            "blocks_repaired": 0,
        }
        if not targets or first_model_run.raw_output_sha256 is None:
            details["reason"] = "repair_identity_unavailable"
            return first_pass, details, 0
        model_calls = 0
        try:
            repair_request, repair_identity = build_editorial_enrichment_repair_request(
                request,
                raw_output_sha256=first_model_run.raw_output_sha256,
                rejected_blocks=targets,
            )
            details["repair_identity"] = repair_identity
            details["prompt_version"] = EDITORIAL_ENRICHMENT_REPAIR_PROMPT_VERSION
            details["contract_version"] = EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION
            assert repair_request.run_id is not None
            details["model_run_id"] = str(repair_request.run_id)
            prior_run = await self._model_gateway.get_run(repair_request.run_id)
            if prior_run is not None:
                if prior_run.status is not ModelRunStatus.SUCCEEDED:
                    details["status"] = "prior_attempt_not_succeeded"
                    details["model_run_status"] = prior_run.status.value
                    return first_pass, details, model_calls
                execution, archive_error = await self._verified_existing_execution(repair_request)
                if archive_error is not None or execution is None:
                    details["status"] = "prior_output_unverified"
                    details["error"] = archive_error
                    return first_pass, details, model_calls
            else:
                model_calls = 1
                execution = await self._model_gateway.draft(repair_request)
        except ModelSubmissionReconciliationRequiredError as exc:
            details.update(
                {
                    "status": "submission_requires_reconciliation",
                    "error_code": exc.code,
                    "model_run_id": str(exc.model_run_id)
                    if exc.model_run_id is not None
                    else details.get("model_run_id"),
                }
            )
            return first_pass, details, 1
        except Exception as exc:
            details.update(
                {"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__)}
            )
            return first_pass, details, model_calls

        details["model_run_id"] = str(execution.run.id)
        if execution.run.status is not ModelRunStatus.SUCCEEDED:
            details.update(
                {
                    "status": "failed",
                    "model_run_status": execution.run.status.value,
                    "error_code": execution.run.error_code,
                }
            )
            return first_pass, details, model_calls
        raw_text, raw_error = await self._verified_raw_text(
            execution.run,
            expected_text=execution.output_text if model_calls else None,
        )
        if raw_error is not None or raw_text is None:
            details.update({"status": "raw_output_unverified", "error": raw_error})
            return first_pass, details, model_calls
        try:
            repair_pass = parse_editorial_enrichment_proposal_wire(
                raw_text, evidence_pack, figure_catalog=figure_catalog
            )
            parse_identity = await self._record_wire_parse(
                execution.run,
                evidence_pack,
                repair_pass,
                figure_catalog=figure_catalog,
            )
            merged, repaired_identities, unexpected = _merge_repaired_editorial_enrichment_proposal(
                first_pass, repair_pass, targets
            )
        except Exception as exc:
            details.update({"status": "invalid_repair_result", "error_code": type(exc).__name__})
            return first_pass, details, model_calls
        details.update(
            {
                "status": "repaired" if repaired_identities else "no_blocks_repaired",
                "parse_identity": parse_identity,
                "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
                "repaired_blocks": [
                    {"kind": kind, "block_id": block_id} for kind, block_id in repaired_identities
                ],
                "blocks_repaired": len(repaired_identities),
                "unexpected_accepted_blocks": list(unexpected),
                "rejections": [
                    {
                        "block_id": item.block_id,
                        "scope_id": item.scope_id,
                        "reason_code": item.reason_code,
                        "raw_sha256": item.raw_sha256,
                    }
                    for item in repair_pass.rejections
                ],
                "wire_error_code": repair_pass.error_code,
                "warnings": list(repair_pass.warnings),
                "transformations": list(repair_pass.transformations),
            }
        )
        return (merged if repaired_identities else first_pass), details, model_calls

    async def _verified_existing_execution(
        self, request: ModelRequest
    ) -> tuple[ModelExecution | None, dict[str, Any] | None]:
        """Reuse exact archived invocation bytes when only parsing has changed."""
        if request.run_id is None:
            return None, {"wire_error_code": "editorial_enrichment_invocation_identity_missing"}
        run = await self._model_gateway.get_run(request.run_id)
        if run is None or run.status is not ModelRunStatus.SUCCEEDED:
            return None, None
        if (
            run.id != request.run_id
            or run.prompt_template_id != request.prompt_template_id
            or run.prompt_template_version != request.prompt_template_version
            or run.evidence_pack_hash != request.evidence_pack_hash
        ):
            return None, {"wire_error_code": "editorial_enrichment_invocation_identity_mismatch"}
        text, error = await self._verified_raw_text(run)
        if error is not None:
            return None, error
        assert text is not None
        return (
            ModelExecution(
                run=run,
                output_text=text,
                structured_output=None,
                metadata={"checkpoint": "verified_raw_output_reparse"},
            ),
            None,
        )

    async def _search_resource_proposals(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        access_policy: SynthesisAccessPolicyV1,
        needs: tuple[EditorialResourceNeedV1, ...],
        *,
        enrichment_input_hash: str,
    ) -> tuple[UUID | None, tuple[ResourceProposalV1, ...], int, dict[str, Any]]:
        request = build_editorial_resource_proposal_model_request(
            run,
            snapshot,
            access_policy,
            needs,
            enrichment_input_hash=enrichment_input_hash,
        )
        model_calls = 0
        try:
            execution, archive_error = await self._verified_existing_execution(request)
            if archive_error is not None:
                return None, (), 0, {"status": "raw_output_unverified", **archive_error}
            if execution is None:
                execution = await self._model_gateway.draft(request)
                model_calls = 1
        except ModelSubmissionReconciliationRequiredError as exc:
            return (
                exc.model_run_id or request.run_id,
                (),
                0,
                {"status": "submission_requires_reconciliation", **exc.details},
            )
        except ModelGatewayError as exc:
            if exc.retryable:
                raise
            return (
                request.run_id,
                (),
                model_calls,
                {"status": "failed", "gateway_error_code": exc.code},
            )

        model_run = execution.run
        if model_run_awaits_reconciliation(model_run.error_code):
            return (
                model_run.id,
                (),
                model_calls,
                {
                    "status": "submission_requires_reconciliation",
                    "model_run_id": str(model_run.id),
                    "model_error_code": model_run.error_code,
                },
            )
        if model_run.status is not ModelRunStatus.SUCCEEDED:
            return (
                model_run.id,
                (),
                model_calls,
                {"status": "failed", "model_run_status": model_run.status.value},
            )
        raw_text, raw_error = await self._verified_raw_text(
            model_run,
            expected_text=execution.output_text if model_calls else None,
        )
        if raw_error is not None or raw_text is None:
            return (
                model_run.id,
                (),
                model_calls,
                {"status": "raw_output_unverified", **(raw_error or {})},
            )
        parsed = parse_editorial_resource_proposals_wire(raw_text, needs)
        assert model_run.raw_output_sha256 is not None
        parse_identity = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "raw_output_sha256": model_run.raw_output_sha256,
                    "parser_version": EDITORIAL_RESOURCE_PROPOSAL_WIRE_PARSER_VERSION,
                    "contract_version": EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION,
                    "prompt_version": EDITORIAL_RESOURCE_PROPOSAL_PROMPT_VERSION,
                    "need_mapping": [item.key for item in needs],
                }
            )
        ).hexdigest()
        validation_errors = [
            {
                "path": ["blocks", item.block_id],
                "code": item.reason_code,
                "value_sha256": item.raw_sha256,
            }
            for item in parsed.rejections
        ]
        if parsed.error_code is not None:
            validation_errors.append(
                {
                    "path": ["proposal"],
                    "code": parsed.error_code,
                    "value_sha256": model_run.raw_output_sha256,
                }
            )
        normalized = json.dumps(
            [item.model_dump(mode="json") for item in parsed.proposals],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        await record_wire_parse_diagnostics(
            self._model_gateway,
            model_run,
            parser_stage="editorial_resource_proposals",
            parse_identity=parse_identity,
            validation_errors=validation_errors,
            transformations=(
                *parsed.transformations,
                f"editorial_resource_parser:{EDITORIAL_RESOURCE_PROPOSAL_WIRE_PARSER_VERSION}",
                f"editorial_resource_contract:{EDITORIAL_RESOURCE_PROPOSAL_CONTRACT_VERSION}",
                f"editorial_resource_prompt:{EDITORIAL_RESOURCE_PROPOSAL_PROMPT_VERSION}",
                "editorial_resource_blocks_to_candidate_proposals",
            ),
            normalized_output=normalized,
        )
        return (
            model_run.id,
            parsed.proposals,
            model_calls,
            {
                "status": "proposed" if parsed.proposals else "no_candidates",
                "model_run_id": str(model_run.id),
                "parse_identity": parse_identity,
                "parser_version": EDITORIAL_RESOURCE_PROPOSAL_WIRE_PARSER_VERSION,
                "rejections": [
                    {"block_id": item.block_id, "reason_code": item.reason_code}
                    for item in parsed.rejections
                ],
                "wire_error_code": parsed.error_code,
            },
        )

    async def _verified_raw_text(
        self, run: ModelRun, *, expected_text: str | None = None
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Verify archived output bytes before normalizing or parsing them."""
        return await verified_raw_output_text(
            self._model_gateway,
            run,
            error_prefix="editorial_enrichment",
            error_field="wire_error_code",
            expected_text=expected_text,
        )

    async def _record_wire_parse(
        self,
        run: ModelRun,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        parsed: EditorialEnrichmentWireParseResult,
        *,
        figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = (),
    ) -> str:
        """Persist parse identity and the normalized strict proposal beside raw bytes."""
        assert run.raw_output_sha256 is not None
        is_repair = run.prompt_template_id == "production-editorial-enrichment-repair"
        prompt_version = (
            EDITORIAL_ENRICHMENT_REPAIR_PROMPT_VERSION
            if is_repair
            else EDITORIAL_ENRICHMENT_PROMPT_VERSION
        )
        contract_version = (
            EDITORIAL_ENRICHMENT_REPAIR_CONTRACT_VERSION
            if is_repair
            else EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION
        )
        identity = editorial_enrichment_parse_identity(
            run.raw_output_sha256,
            evidence_pack,
            prompt_version=prompt_version,
            contract_version=contract_version,
            parser_version=EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
            figure_catalog=figure_catalog,
        )
        validation_errors: list[dict[str, Any]] = [
            {
                "path": ["blocks", item.scope_id or item.block_id],
                "block_id": item.block_id,
                "code": item.reason_code,
                "value_sha256": item.raw_sha256,
            }
            for item in parsed.rejections
        ]
        if parsed.error_code is not None:
            validation_errors.append(
                {
                    "path": ["proposal"],
                    "code": parsed.error_code,
                    "value_sha256": run.raw_output_sha256,
                }
            )
        transformations = [
            *parsed.transformations,
            *parsed.warnings,
            f"editorial_enrichment_parser:{EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION}",
            f"editorial_enrichment_contract:{contract_version}",
            f"editorial_enrichment_prompt:{prompt_version}",
        ]
        normalized = None
        if parsed.proposal is not None:
            normalized = parsed.proposal.model_dump_json().encode("utf-8")
            transformations.append("editorial_enrichment_text_blocks_to_strict_proposal")
        return await record_wire_parse_diagnostics(
            self._model_gateway,
            run,
            parser_stage="editorial_enrichment",
            parse_identity=identity,
            validation_errors=validation_errors,
            transformations=tuple(transformations),
            normalized_output=normalized,
        )

    async def _ingest_source_figures(self, inventory: SourceFigureInventoryResult) -> None:
        if not inventory.accepted:
            return
        if self._source_figure_ingestor is None:
            raise RuntimeError("Source figure ingestion requires a media asset store")
        for figure in inventory.accepted:
            if figure.blob_id is None:
                raise ValueError("Accepted source figure does not reference an archived blob")
            content = await self._artifact_store.read_bytes(
                figure.blob_id, max_bytes=MAX_SOURCE_FIGURE_BYTES
            )
            await self._source_figure_ingestor.ingest(figure, content)

    async def _load_extraction(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        artifact: ProductionArtifact,
    ) -> ProductionExtractionV1:
        if (
            not isinstance(artifact, ProductionArtifact)
            or artifact.stage is not ProductionArtifactStage.EXTRACTION
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION artifact is missing",
            )
        if (
            run.id != snapshot.production_run_id
            or run.subject_id != snapshot.subject_id
            or artifact.production_run_id != run.id
            or artifact.subject_id != snapshot.subject_id
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "Production run, snapshot and EXTRACTION identities differ",
            )
        if (
            artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION artifact is not verified or has no canonical blob",
            )
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            extraction = production_extraction_from_json(payload)
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception as exc:
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION payload is unreadable or invalid",
            ) from exc
        if (
            extraction.subject_id != snapshot.subject_id
            or extraction.production_input_hash != snapshot.input_hash
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "The canonical extraction does not match its frozen snapshot",
            )
        return extraction

    async def _load_synthesis(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        artifact: ProductionArtifact,
        extraction: ProductionExtractionV1,
    ) -> ProductionSynthesisV1:
        if (
            not isinstance(artifact, ProductionArtifact)
            or artifact.stage is not ProductionArtifactStage.SYNTHESIS
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The canonical SYNTHESIS artifact is missing",
            )
        if artifact.production_run_id != run.id or artifact.subject_id != snapshot.subject_id:
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "Production run, snapshot and SYNTHESIS identities differ",
            )
        if (
            artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The canonical SYNTHESIS artifact is not verified or has no canonical blob",
            )
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            synthesis = production_synthesis_from_json(payload)
            validate_synthesis_lineage(synthesis, snapshot, canonical_extraction_hash(extraction))
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception as exc:
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "The canonical SYNTHESIS payload is invalid for the current extraction",
            ) from exc
        if synthesis.production_input_hash != snapshot.input_hash:
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "The canonical synthesis does not match its frozen snapshot",
            )
        return synthesis

    async def _load_projection(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        artifact: ProductionArtifact,
    ) -> RelevanceProjectionV1:
        if (
            not isinstance(artifact, ProductionArtifact)
            or artifact.stage is not ProductionArtifactStage.RELEVANCE_PROJECTION
            or artifact.production_run_id != run.id
            or artifact.subject_id != snapshot.subject_id
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISSING,
                "The verified subject relevance projection is missing",
            )
        try:
            projection = relevance_projection_from_json(
                await self._artifact_store.read_json(artifact.canonical_blob_id)
            )
            validate_relevance_projection_lineage(
                projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
            )
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception as exc:
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "The subject relevance projection does not match canonical extraction",
            ) from exc
        if (
            projection.production_input_hash != snapshot.input_hash
            or artifact.input_hash != projection.input_hash
        ):
            raise _EditorialEnrichmentInputControl(
                EditorialEnrichmentStageErrorCode.INPUTS_MISMATCH,
                "The subject relevance projection has different functional inputs",
            )
        return projection

    async def _reuse_exact(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
        input_hash: str,
    ) -> ProductionEditorialEnrichmentExecution | None:
        if self._artifact_reuse is None:
            return None
        reuse = await self._artifact_reuse.find_or_reuse(
            run=run,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            input_hash=input_hash,
        )
        if reuse is None:
            return None
        artifact = reuse.artifact
        if (
            artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.subject_id != snapshot.subject_id
            or artifact.input_hash != input_hash
            or artifact.canonical_blob_id is None
        ):
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=None,
                model_calls=0,
                error_code=EditorialEnrichmentStageErrorCode.REUSE_INVALID,
                error_message="Reusable Editorial Enrichment is not an exact verified artifact",
                details={"artifact_id": str(artifact.id)},
            )
        try:
            enrichment = editorial_enrichment_from_json(
                await self._artifact_store.read_json(artifact.canonical_blob_id)
            )
            validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception as exc:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=None,
                model_calls=0,
                error_code=EditorialEnrichmentStageErrorCode.REUSE_INVALID,
                error_message="Reusable Editorial Enrichment payload is invalid for current inputs",
                details={"artifact_id": str(artifact.id), "reason": str(exc)},
            )
        return ProductionEditorialEnrichmentExecution(
            status=EditorialEnrichmentExecutionStatus.REUSED,
            input_hash=input_hash,
            artifact=artifact,
            model_run_id=artifact.model_run_id,
            model_calls=0,
            table_count=len(enrichment.tables),
            diagram_count=len(enrichment.diagrams),
            source_figure_count=len(enrichment.source_figures),
            chart_count=len(enrichment.charts),
            warnings=enrichment.warnings,
            details={"reused": reuse.reused},
        )

    @classmethod
    def _reconciliation_required(
        cls,
        *,
        input_hash: str,
        model_run_id: UUID,
        error_message: str,
        details: Mapping[str, Any] | None,
    ) -> ProductionEditorialEnrichmentExecution:
        # The exact ModelRun identity travels with the shared reconciliation
        # code so the run records what to adopt instead of resubmitting.
        return cls._needs_review(
            input_hash=input_hash,
            model_run_id=model_run_id,
            error_code=EditorialEnrichmentStageErrorCode.RECONCILIATION_REQUIRED,
            error_message=error_message,
            details={
                **(details or {}),
                "error_code": EditorialEnrichmentStageErrorCode.RECONCILIATION_REQUIRED.value,
                "model_run_id": str(model_run_id),
            },
        )

    @staticmethod
    def _needs_review(
        *,
        input_hash: str,
        model_run_id: UUID | None,
        error_code: EditorialEnrichmentStageErrorCode,
        error_message: str,
        details: Mapping[str, Any] | None = None,
        model_calls: int = 1,
    ) -> ProductionEditorialEnrichmentExecution:
        return ProductionEditorialEnrichmentExecution(
            status=EditorialEnrichmentExecutionStatus.NEEDS_REVIEW,
            input_hash=input_hash,
            artifact=None,
            model_run_id=model_run_id,
            model_calls=model_calls,
            error_code=error_code.value,
            error_message=error_message,
            details=details,
        )


def canonical_editorial_enrichment_hash(enrichment: EditorialEnrichmentV1) -> str:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(editorial_enrichment_to_json(enrichment))
    ).hexdigest()


def _editorial_enrichment_identity_payload(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack_hash: str,
    access_policy_hash: str,
    projection_hash: str | None = None,
    source_figure_inventory_hash: str | None = None,
    prompt_version: str | None = None,
    contract_version: str | None = None,
) -> dict[str, Any]:
    if _SHA256_RE.fullmatch(evidence_pack_hash) is None:
        raise ValueError("Editorial enrichment evidence pack hash must be lowercase SHA-256")
    if _SHA256_RE.fullmatch(access_policy_hash) is None:
        raise ValueError("Editorial enrichment access policy hash must be lowercase SHA-256")
    if (
        source_figure_inventory_hash is not None
        and _SHA256_RE.fullmatch(source_figure_inventory_hash) is None
    ):
        raise ValueError("Source figure inventory hash must be lowercase SHA-256")
    if projection_hash is not None and _SHA256_RE.fullmatch(projection_hash) is None:
        raise ValueError("Relevance projection hash must be lowercase SHA-256")
    payload = {
        "stage": "editorial_enrichment",
        "production_input_hash": synthesis.production_input_hash,
        "extraction_hash": canonical_extraction_hash(extraction),
        "synthesis_hash": canonical_synthesis_hash(synthesis),
        "schema_version": EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
        "policy_version": EDITORIAL_ENRICHMENT_POLICY_VERSION,
        "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
        "evidence_pack_schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
        "evidence_pack_policy_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION,
        "evidence_pack_hash": evidence_pack_hash,
        "relevance_projection_hash": projection_hash,
        "access_policy_hash": access_policy_hash,
        "prompt_version": prompt_version or EDITORIAL_ENRICHMENT_PROMPT_VERSION,
        "contract_version": contract_version or EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        "validator_version": EDITORIAL_ENRICHMENT_VALIDATOR_VERSION,
        "analytic_validation_policy_version": EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION,
        "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        "routing_policy_version": EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
    }
    if source_figure_inventory_hash is not None:
        payload["source_figure_inventory_hash"] = source_figure_inventory_hash
    return payload


def compute_editorial_enrichment_invocation_hash(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack_hash: str,
    access_policy_hash: str,
    projection_hash: str | None = None,
    source_figure_inventory_hash: str | None = None,
    prompt_version: str | None = None,
    contract_version: str | None = None,
) -> str:
    """Hash request inputs; parser-only changes must not cause a new call."""
    payload = _editorial_enrichment_identity_payload(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        projection_hash=projection_hash,
        source_figure_inventory_hash=source_figure_inventory_hash,
        prompt_version=prompt_version,
        contract_version=contract_version,
    )
    for local_only_version in (
        "schema_version",
        "policy_version",
        "validator_version",
        "analytic_validation_policy_version",
    ):
        payload.pop(local_only_version, None)
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def compute_editorial_enrichment_input_hash(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack_hash: str,
    access_policy_hash: str,
    projection_hash: str | None = None,
    source_figure_inventory_hash: str | None = None,
    prompt_version: str | None = None,
    contract_version: str | None = None,
    parser_version: str | None = None,
    resource_search_enabled: bool = False,
) -> str:
    """Hash canonical enrichment inputs, including the local wire parser."""
    payload = _editorial_enrichment_identity_payload(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        projection_hash=projection_hash,
        source_figure_inventory_hash=source_figure_inventory_hash,
        prompt_version=prompt_version,
        contract_version=contract_version,
    )
    payload["parser_version"] = parser_version or EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION
    payload["semantic_annotation_prompt_version"] = SEMANTIC_ANNOTATION_PROMPT_VERSION
    payload["semantic_annotation_contract_version"] = SEMANTIC_ANNOTATION_CONTRACT_VERSION
    payload["semantic_annotation_parser_version"] = SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION
    payload["semantic_annotation_policy_version"] = SEMANTIC_ANNOTATION_POLICY_VERSION
    if type(resource_search_enabled) is not bool:
        raise ValueError("Resource search enablement must be a boolean")
    payload["resource_search_enabled"] = resource_search_enabled
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def editorial_enrichment_parse_identity(
    raw_output_sha256: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    *,
    prompt_version: str | None = None,
    contract_version: str | None = None,
    parser_version: str | None = None,
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...] = (),
) -> str:
    """Bind normalized output to verified response bytes and request handles."""
    if _SHA256_RE.fullmatch(raw_output_sha256) is None:
        raise ValueError("Raw Editorial Enrichment output hash must be lowercase SHA-256")
    payload = {
        "raw_output_sha256": raw_output_sha256,
        "parser_version": parser_version or EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
        "contract_version": contract_version or EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        "prompt_version": prompt_version or EDITORIAL_ENRICHMENT_PROMPT_VERSION,
        "analytic_validation_policy_version": EDITORIAL_ENRICHMENT_ANALYTIC_POLICY_VERSION,
        "request_handle_mapping": [
            {"handle": handle, "evidence_ref": repr(ref)}
            for handle, ref in sorted(evidence_pack._handle_to_ref.items())
        ],
        "figure_handle_mapping": [
            {
                "handle": entry.handle,
                "figure_id": str(entry.figure.figure_id),
                "sha256": entry.figure.sha256,
                "decision": entry.figure.decision.value,
            }
            for entry in figure_catalog
        ],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def validate_editorial_enrichment(
    enrichment: EditorialEnrichmentV1,
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> None:
    if not isinstance(enrichment, EditorialEnrichmentV1):
        raise ValueError("Expected an EditorialEnrichmentV1")
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")

    if (
        enrichment.subject_id != extraction.subject_id
        or enrichment.subject_id != synthesis.subject_id
    ):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment subject does not match its extraction and synthesis",
        )
    if not (
        enrichment.production_input_hash
        == extraction.production_input_hash
        == synthesis.production_input_hash
    ):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment production input does not match its extraction and synthesis",
        )
    if enrichment.extraction_hash != canonical_extraction_hash(extraction):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment extraction hash does not match the canonical extraction",
        )
    if enrichment.synthesis_hash != canonical_synthesis_hash(synthesis):
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment synthesis hash does not match the canonical synthesis",
        )
    if enrichment.publication_language != synthesis.publication_language:
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_lineage_mismatch",
            "Editorial enrichment publication language does not match the canonical synthesis",
        )

    synthesis_paragraphs = semantic_annotation_anchor_texts(synthesis, enrichment)
    for annotation in enrichment.annotations:
        paragraph_text = synthesis_paragraphs.get(annotation.paragraph_anchor)
        if paragraph_text is None or annotation.text not in paragraph_text:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_annotation_invalid",
                "Semantic annotation does not match its anchored synthesis paragraph",
            )

    unknown_refs = editorial_enrichment_evidence_refs(enrichment) - set(
        extraction_evidence_refs_v1(extraction)
    )
    if unknown_refs:
        raise EditorialEnrichmentValidationError(
            "editorial_enrichment_evidence_missing",
            "Editorial enrichment references evidence absent from the extraction",
        )

    placements = (
        *(table.placement for table in enrichment.tables),
        *(chart.placement for chart in enrichment.charts),
        *(diagram.placement for diagram in enrichment.diagrams),
        *(figure.placement for figure in enrichment.source_figures),
    )
    for placement in placements:
        if (
            placement.kind.value == "after_section"
            and placement.section_index is not None
            and placement.section_index >= len(synthesis.sections)
        ):
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_placement_invalid",
                "Editorial enrichment placement references a missing synthesis section",
            )

    sources_by_id = {source.source_document_id: source for source in extraction.sources}
    for figure in enrichment.source_figures:
        source = sources_by_id.get(figure.source_document_id)
        if source is None:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "Source figure references a document absent from the extraction",
            )
        if figure.source_url != source.canonical_url:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "Source figure URL differs from the canonical extraction source URL",
            )
    extraction_refs = set(extraction_evidence_refs_v1(extraction))
    figures_by_id = {
        figure.resolved_figure.figure_id: figure
        for figure in enrichment.source_figures
        if figure.resolved_figure is not None
    }
    for decision in enrichment.figure_decisions:
        if decision.source_document_id not in sources_by_id:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "Figure decision references a document absent from the extraction",
            )
        if any(
            ref not in extraction_refs or ref.source_document_id != decision.source_document_id
            for ref in decision.evidence_refs
        ):
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_evidence_missing",
                "Figure decision evidence must belong to its source and extraction",
            )
        selected = figures_by_id.get(decision.figure_id)
        if decision.decision is EditorialFigureDecision.INCLUDED_BY_MODEL:
            if (
                selected is None
                or selected.inclusion_status is not SourceFigureInclusionStatus.INCLUDED
            ):
                raise EditorialEnrichmentValidationError(
                    "editorial_enrichment_source_figure_invalid",
                    "A model-selected figure must be included in the enrichment",
                )
        elif selected is not None:
            raise EditorialEnrichmentValidationError(
                "editorial_enrichment_source_figure_invalid",
                "A figure with a non-included decision cannot be assembled",
            )
