"""Canonical Editorial Enrichment generation (AW-016)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
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

from cti_app.application.diagram_compilation import DiagramCompiler
from cti_app.application.media_assets import (
    MAX_SOURCE_FIGURE_BYTES,
    MediaAssetStore,
    SourceFigureIngestor,
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
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
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
    SourceFigureInventoryResult,
    load_archived_source_figure_inventory,
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
    EDITORIAL_ENRICHMENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_SCHEMA_VERSION,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EditorialEnrichmentV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
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
    SemanticAnnotationProposalV1,
    SemanticRole,
    lead_paragraph_anchor,
    section_paragraph_anchor,
)

if TYPE_CHECKING:
    from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
    from cti_app.application.production_stages import EditorialEnrichmentService

EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "model-text-blocks-v2-semantic-annotations"
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION = 3
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION = (
    "editorial-enrichment-evidence-pack-v5-paragraph-anchors"
)
EDITORIAL_ENRICHMENT_VALIDATOR_VERSION = "editorial-enrichment-validator-v3-semantic-annotations"
EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION = "editorial-enrichment-model-policy-v1"
EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION = "editorial-enrichment-routing-policy-v1"

MAX_ENRICHMENT_TABLES = 8
MAX_ENRICHMENT_DIAGRAMS = 6
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 50
MAX_DIAGRAM_NODES = 50
MAX_DIAGRAM_EDGES = 100
MAX_DIAGRAM_GROUPS = 8
MAX_EDITORIAL_ENRICHMENT_TECHNICAL_EVIDENCE = 128

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


class TableProposalV1(_StrictEnrichmentProposalModel):
    key: StrictStr
    kind: EnrichmentTableKind
    title: StrictStr
    caption: StrictStr | None = None
    columns: tuple[TableColumnProposalV1, ...]
    rows: tuple[TableRowProposalV1, ...]
    placement: EnrichmentPlacementProposalV1

    @field_validator("key", "title")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return _nonempty_proposal_text(value)

    @field_validator("caption")
    @classmethod
    def _caption_text(cls, value: str | None) -> str | None:
        return _nonempty_proposal_text(value) if value is not None else None


class DiagramNodeProposalV1(_StrictEnrichmentProposalModel):
    node_id: StrictStr
    label: StrictStr
    evidence_handles: tuple[StrictStr, ...]

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
    diagrams: tuple[DiagramProposalV1, ...] = ()
    annotations: tuple[AnnotationProposalV1, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentWireRejection:
    block_id: str
    reason_code: str
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentWireParseResult:
    proposal: EditorialEnrichmentProposalV1 | None
    rejections: tuple[EditorialEnrichmentWireRejection, ...] = ()
    error_code: str | None = None
    explicit_empty: bool = False
    transformations: tuple[str, ...] = ()


@dataclass(slots=True)
class _EditorialEnrichmentWireBlock:
    kind: str
    block_id: str
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
    r"^(TABLE|DIAGRAM|ANNOTATION|COLUMN|ROW|NODE|RELATION|EDGE|GROUP)"
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
    "placement": "placement",
    "section index": "section_index",
    "section_index": "section_index",
    "column key": "key",
    "label": "label",
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
    "exact text": "text",
    "segment": "text",
    "text": "text",
}
_ENRICHMENT_CHILD_KINDS = frozenset({"COLUMN", "ROW", "NODE", "RELATION", "EDGE", "GROUP"})
_ENRICHMENT_CHILD_PARENT = {
    "COLUMN": "TABLE",
    "ROW": "TABLE",
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
    return result


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


def parse_editorial_enrichment_proposal_wire(
    raw_text: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> EditorialEnrichmentWireParseResult:
    """Recover independent text blocks, leaving canonical validation strict."""
    if not isinstance(raw_text, str):
        return EditorialEnrichmentWireParseResult(
            None, error_code="editorial_enrichment_unintelligible_response"
        )
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    transformations: list[str] = []
    if sanitized != raw_text.replace("\r\n", "\n").replace("\r", "\n"):
        transformations.append("bridge_ui_markers_removed")
    top_blocks: list[_EditorialEnrichmentWireBlock] = []
    rejections: list[EditorialEnrichmentWireRejection] = []
    current_top: _EditorialEnrichmentWireBlock | None = None
    current_child: _EditorialEnrichmentWireBlock | None = None
    last_field: tuple[_EditorialEnrichmentWireBlock, str] | None = None
    local_ids: set[str] = set()
    sequences: dict[str, int] = defaultdict(int)
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
        sequences[kind] += 1
        prefix = {
            "TABLE": "T",
            "DIAGRAM": "D",
            "ANNOTATION": "A",
            "COLUMN": "C",
            "ROW": "R",
            "NODE": "N",
            "RELATION": "L",
            "EDGE": "L",
            "GROUP": "G",
        }[kind]
        block_id = local_id or f"{prefix}{sequences[kind]:03d}"
        error_code = None
        if block_id in local_ids:
            error_code = "editorial_enrichment_duplicate_local_block_id"
        else:
            local_ids.add(block_id)
        return _EditorialEnrichmentWireBlock(
            kind=kind,
            block_id=block_id,
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
            if kind in {"TABLE", "DIAGRAM", "ANNOTATION"}:
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
            r"END(?:\s+(TABLE|DIAGRAM|ANNOTATION|COLUMN|ROW|NODE|RELATION|EDGE|GROUP|ITEM))?",
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
                r"PARAGRAPH\s+ANCHOR|EXACT\s+TEXT|CATEGORY|ROLE|ANCHOR|SEGMENT|TEXT|"
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
            "TABLE": {"key", "kind", "title", "caption", "placement", "section_index"},
            "DIAGRAM": {
                "key",
                "kind",
                "title",
                "caption",
                "placement",
                "section_index",
                "direction",
            },
            "COLUMN": {"key", "label"},
            "ROW": {"cell", "evidence_handles"},
            "NODE": {"node_id", "id", "label", "evidence_handles"},
            "RELATION": {"source_node_id", "target_node_id", "label", "evidence_handles"},
            "EDGE": {"source_node_id", "target_node_id", "label", "evidence_handles"},
            "GROUP": {"group_id", "id", "label", "node_ids"},
            "ANNOTATION": {"role", "paragraph_anchor", "text"},
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
        )
    if explicit_empty and not top_blocks and not rejections:
        return EditorialEnrichmentWireParseResult(
            proposal=EditorialEnrichmentProposalV1(),
            explicit_empty=True,
            transformations=tuple(transformations),
        )

    tables: list[TableProposalV1] = []
    diagrams: list[DiagramProposalV1] = []
    annotations: list[AnnotationProposalV1] = []
    canonical_keys: set[str] = set()
    for top in top_blocks:
        if top.error_code is not None:
            reject(top, top.error_code)
            continue
        if top.kind == "TABLE":
            table = _parse_enrichment_wire_table(top, evidence_pack, rejections)
            if table is None:
                continue
            if table.key in canonical_keys:
                reject(top, "editorial_enrichment_duplicate_key")
            elif len(tables) >= MAX_ENRICHMENT_TABLES:
                reject(top, "editorial_enrichment_table_limit_exceeded")
            else:
                canonical_keys.add(table.key)
                tables.append(table)
        elif top.kind == "ANNOTATION":
            annotation = _parse_annotation_wire_block(top, evidence_pack, rejections)
            if annotation is not None:
                annotations.append(annotation)
        else:
            diagram = _parse_enrichment_wire_diagram(top, evidence_pack, rejections)
            if diagram is None:
                continue
            if diagram.key in canonical_keys:
                reject(top, "editorial_enrichment_duplicate_key")
            elif len(diagrams) >= MAX_ENRICHMENT_DIAGRAMS:
                reject(top, "editorial_enrichment_diagram_limit_exceeded")
            else:
                canonical_keys.add(diagram.key)
                diagrams.append(diagram)
    if not tables and not diagrams and not annotations:
        return EditorialEnrichmentWireParseResult(
            None,
            tuple(rejections),
            error_code="editorial_enrichment_no_valid_blocks",
            transformations=tuple(transformations),
        )
    return EditorialEnrichmentWireParseResult(
        proposal=EditorialEnrichmentProposalV1(
            tables=tuple(tables), diagrams=tuple(diagrams), annotations=tuple(annotations)
        ),
        rejections=tuple(rejections),
        transformations=tuple(transformations),
    )


def _parse_enrichment_wire_table(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
) -> TableProposalV1 | None:
    def reject(item: _EditorialEnrichmentWireBlock, code: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, code, item.raw_lines))

    allowed_top_fields = {"key", "kind", "title", "caption", "placement", "section_index"}
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
    if not 2 <= len(columns) <= MAX_TABLE_COLUMNS:
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
        if len(rows) >= MAX_TABLE_ROWS:
            reject(child, "editorial_enrichment_table_row_limit_exceeded")
            continue
        try:
            rows.append(TableRowProposalV1(cells=cells, evidence_handles=handles))
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_table_row_invalid")
    if not rows:
        reject(block, "editorial_enrichment_table_has_no_valid_rows")
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
        )
    except (TypeError, ValueError, ValidationError):
        reject(block, "editorial_enrichment_table_invalid")
        return None


def _parse_enrichment_wire_diagram(
    block: _EditorialEnrichmentWireBlock,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    rejections: list[EditorialEnrichmentWireRejection],
) -> DiagramProposalV1 | None:
    def reject(item: _EditorialEnrichmentWireBlock, code: str) -> None:
        rejections.append(_enrichment_wire_rejection(item, item.block_id, code, item.raw_lines))

    allowed_top_fields = {
        "key",
        "kind",
        "title",
        "caption",
        "placement",
        "section_index",
        "direction",
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
    if not key or not title or kind is None or direction is None or placement is None:
        reject(block, "editorial_enrichment_diagram_missing_or_invalid_field")
        return None

    nodes: list[DiagramNodeProposalV1] = []
    for child in block.children:
        if child.kind != "NODE":
            continue
        error = _block_child_error(child, frozenset({"id", "node_id", "label", "evidence_handles"}))
        node_id = _wire_scalar(child, "node_id") or _wire_scalar(child, "id")
        label = _wire_scalar(child, "label")
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None:
            reject(child, error)
            continue
        if not node_id or not label:
            reject(child, "editorial_enrichment_diagram_node_missing_field")
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
                DiagramNodeProposalV1(node_id=node_id, label=label, evidence_handles=handles)
            )
        except (TypeError, ValueError, ValidationError):
            reject(child, "editorial_enrichment_diagram_node_invalid")
    known_nodes = {item.node_id for item in nodes}

    edges: list[DiagramEdgeProposalV1] = []
    for child in block.children:
        if child.kind not in {"RELATION", "EDGE"}:
            continue
        error = _block_child_error(
            child,
            frozenset({"source_node_id", "target_node_id", "label", "evidence_handles"}),
        )
        source = _wire_scalar(child, "source_node_id")
        target = _wire_scalar(child, "target_node_id")
        handles = _wire_handles(_wire_scalar(child, "evidence_handles"))
        if error is not None:
            reject(child, error)
            continue
        if not source or not target:
            reject(child, "editorial_enrichment_diagram_relation_missing_endpoint")
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
        try:
            edges.append(
                DiagramEdgeProposalV1(
                    source_node_id=source,
                    target_node_id=target,
                    label=_wire_scalar(child, "label"),
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
    if len(nodes) < 2 or len(nodes) > MAX_DIAGRAM_NODES or not edges:
        reject(block, "editorial_enrichment_diagram_incomplete_after_rejections")
        return None
    if len(edges) > MAX_DIAGRAM_EDGES or len(groups) > MAX_DIAGRAM_GROUPS:
        reject(block, "editorial_enrichment_diagram_item_limit_exceeded")
        return None
    try:
        return DiagramProposalV1(
            key=key,
            kind=kind,
            title=title,
            caption=_wire_caption(block),
            direction=direction,
            nodes=tuple(nodes),
            edges=tuple(edges),
            groups=tuple(groups),
            placement=placement,
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

    def resolve_handle(self, handle: str) -> ExtractionEvidenceRefV1:
        try:
            return self._handle_to_ref[handle]
        except (KeyError, TypeError) as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.UNKNOWN_EVIDENCE
            ) from exc


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


def _source_figure_inventory_warnings(
    inventory: SourceFigureInventoryResult,
) -> tuple[str, ...]:
    warnings = set(inventory.warnings)
    if any(figure.decision is not SourceFigureDecision.ACCEPTED for figure in inventory.figures):
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
    def paragraph(item: Any, anchor: str) -> dict[str, Any]:
        return {
            "anchor": anchor,
            "text": item.text,
            "evidence_handles": [handle_for_ref[ref] for ref in item.evidence_refs],
        }

    return {
        "language": synthesis.publication_language,
        "title": synthesis.title,
        "lead": [
            paragraph(item, lead_paragraph_anchor(index))
            for index, item in enumerate(synthesis.lead, start=1)
        ],
        "sections": [
            {
                "section_index": index,
                "kind": section.kind.value,
                "heading": section.heading,
                "paragraphs": [
                    paragraph(item, section_paragraph_anchor(index, paragraph_index))
                    for paragraph_index, item in enumerate(section.paragraphs, start=1)
                ],
            }
            for index, section in enumerate(synthesis.sections)
        ],
        "timeline": [
            {
                "event_date": item.event_date.isoformat() if item.event_date else None,
                "date_text": item.date_text,
                "text": item.text,
                "evidence_handles": [handle_for_ref[ref] for ref in item.evidence_refs],
            }
            for item in synthesis.timeline
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
        and projection.classification_for(ref).classification
        is RelevanceClassification.COUNTER_INDICATION
    }
    relation_refs = (
        {
            ref
            for relation in projection.source_pair_relations
            for ref in relation.supporting_evidence_refs
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
    reserve_evidence = tuple(
        MappingProxyType(
            _prompt_evidence_record(
                reserve_handle_for_ref[ref],
                ref.kind,
                entries[ref],
                source=source_by_id[ref.source_document_id],
            )
        )
        for ref in ordered_reserve_refs
    )
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


def editorial_enrichment_output_contract_example() -> str:
    """The text-block contract shown to the model; it is not a schema payload."""
    return """Return independent plain-text blocks. Give every block a local id.
Use these fields and keep every literal value verbatim:

TABLE T001
KEY: commands
KIND: commands
TITLE: Commands observed
CAPTION: optional text, or omit this field
PLACEMENT: after_lead
SECTION_INDEX: omit unless placement is after_section
COLUMN C001
KEY: command
LABEL: Command
END COLUMN
COLUMN C002
KEY: purpose
LABEL: Purpose
END COLUMN
ROW R001
CELL: exact command literal
CELL: evidence-grounded purpose
EVIDENCE: E001
END ROW
END TABLE

DIAGRAM D001
KEY: infection_chain
KIND: infection_chain
TITLE: Infection chain
DIRECTION: left_to_right
PLACEMENT: after_section
SECTION_INDEX: 0
NODE N001
ID: step_1
LABEL: Observed initial step
EVIDENCE: E001
END NODE
NODE N002
ID: step_2
LABEL: Observed following step
EVIDENCE: E002
END NODE
RELATION L001
FROM: step_1
TO: step_2
LABEL: leads to
EVIDENCE: E003
END RELATION
GROUP G001
ID: initial-stage
LABEL: Initial stage
NODES: step_1, step_2
END GROUP
END DIAGRAM

ANNOTATION A001
CATEGORY: actor
PARAGRAPH_ANCHOR: lead:0001
EXACT_TEXT: exact actor name copied from the anchored paragraph
END ANNOTATION

For no useful enrichment, return exactly: NO USEFUL ENRICHMENT. Do not use
JSON, Markdown tables, D2, Mermaid, code, HTML, SVG, Typst, or generated render
syntax. Annotation categories are actor, campaign, malware, tool, product,
english_term, technical, technical_literal, ioc, path, command, protocol_field,
source, and proof.
Only annotate text that appears verbatim in the named paragraph. Copy its stable
anchor from current_synthesis. Repeated exact text is applied to every exact
occurrence in that paragraph; punctuation outside the copied text is preserved.
Each table column is a COLUMN block; each row is a ROW block with one CELL line
per column. Each diagram uses NODE, RELATION, and optional GROUP blocks. Every
row, node, and relation must cite existing evidence handles from the input. A
relation's handles must support that relation. Never invent evidence or handles.
Keep titles/captions descriptive, and preserve placement anchors and section
indexes from the provided guidance."""


def build_editorial_enrichment_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    access_policy: SynthesisAccessPolicyV1,
    source_figure_inventory_hash: str | None = None,
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
            "Tu es un planificateur de représentations et d'annotations éditoriales, "
            "pas un chercheur "
            "ni un renderer. Décide uniquement si des informations de la synthèse "
            "gagneraient à être structurées en "
            "tableaux ou diagrammes sémantiques, et propose des rôles typographiques uniquement "
            "pour les segments exacts et évidents d'un paragraphe ancré. N'ajoute aucun fait, "
            "n'effectue aucune recherche, "
            "et utilise uniquement les preuves fournies. Chaque ligne, nœud et arête doit citer au "
            "moins un evidence handle exact. Une arête exprime une relation factuelle "
            "et doit avoir "
            "sa propre preuve. Conserve chaque commande, chemin, nom, date, adresse, hash et autre "
            "littéral exactement comme dans la preuve. Si aucune représentation n'améliore la "
            "compréhension, renvoie le marqueur explicite prévu. Ne génère ni JSON, tableau "
            "Markdown, HTML, Mermaid, D2, DOT, TikZ, Typst, SVG, image source, ni corps de règle. "
            "Les diagrammes décrivent seulement une spécification sémantique en blocs. Titres et "
            "captions restent descriptifs."
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
        "editorial_guidance": {
            "table_kinds": [item.value for item in EnrichmentTableKind],
            "diagram_kinds": [item.value for item in EnrichmentDiagramKind],
            "diagram_directions": [item.value for item in EnrichmentDiagramDirection],
            "placements": [item.value for item in EnrichmentPlacementKind],
            "section_indexes": [
                int(item["section_index"]) for item in evidence_pack.current_synthesis["sections"]
            ],
            "limits": {
                "tables": MAX_ENRICHMENT_TABLES,
                "diagrams": MAX_ENRICHMENT_DIAGRAMS,
                "table_columns": MAX_TABLE_COLUMNS,
                "table_rows": MAX_TABLE_ROWS,
                "diagram_nodes": MAX_DIAGRAM_NODES,
                "diagram_edges": MAX_DIAGRAM_EDGES,
                "diagram_groups": MAX_DIAGRAM_GROUPS,
            },
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
        web_search=False,
        background=False,
        conversation=None,
        run_id=editorial_enrichment_model_run_id(run, invocation_hash),
        allow_failed_resubmit=True,
        metadata=metadata,
        parameters={
            "evidence_pack_schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
            "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
            "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        },
    )


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


def validate_editorial_enrichment_proposal(
    proposal: EditorialEnrichmentProposalV1 | Mapping[str, Any],
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    *,
    source_figures: tuple[SourceFigureCandidateV1, ...] = (),
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
    if len(parsed.tables) > MAX_ENRICHMENT_TABLES or len(parsed.diagrams) > MAX_ENRICHMENT_DIAGRAMS:
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
    paragraph_text_by_anchor = {
        **{
            lead_paragraph_anchor(index): item.text
            for index, item in enumerate(synthesis.lead, start=1)
        },
        **{
            section_paragraph_anchor(section_index, paragraph_index): paragraph.text
            for section_index, section in enumerate(synthesis.sections)
            for paragraph_index, paragraph in enumerate(section.paragraphs, start=1)
        },
    }
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
    diagrams: list[DiagramSpecV1] = []
    for table in parsed.tables:
        if not 2 <= len(table.columns) <= MAX_TABLE_COLUMNS or not (
            1 <= len(table.rows) <= MAX_TABLE_ROWS
        ):
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
                )
            )
        except ValueError as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            ) from exc

    for diagram in parsed.diagrams:
        if (
            len(diagram.nodes) < 2
            or len(diagram.nodes) > MAX_DIAGRAM_NODES
            or not diagram.edges
            or len(diagram.edges) > MAX_DIAGRAM_EDGES
            or len(diagram.groups) > MAX_DIAGRAM_GROUPS
        ):
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
                    DiagramNodeV1(node_id=node.node_id, label=node.label, evidence_refs=refs)
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
                )
            )
        except ValueError as exc:
            raise EditorialEnrichmentProposalControlError(
                EditorialEnrichmentStageErrorCode.OUTPUT_INVALID
            ) from exc

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
            diagrams=tuple(diagrams),
            source_figures=source_figures,
            warnings=warnings,
            annotations=tuple(annotations),
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
                        snapshot, extraction, uow.source_documents
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
                )
            await self._ingest_source_figures(source_figure_inventory)
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
        if access_policy.do_not_submit:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=None,
                model_calls=0,
                error_code=EditorialEnrichmentStageErrorCode.POLICY_BLOCKED,
                error_message="The source access policy forbids any model submission.",
                details={
                    "do_not_submit": True,
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
        parsed = parse_editorial_enrichment_proposal_wire(raw_text, evidence_pack)
        parse_identity = await self._record_wire_parse(model_run, evidence_pack, parsed)
        wire_details: dict[str, Any] = {
            "parse_identity": parse_identity,
            "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
            "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
            "rejections": [
                {
                    "block_id": item.block_id,
                    "reason_code": item.reason_code,
                    "raw_sha256": item.raw_sha256,
                }
                for item in parsed.rejections
            ],
        }
        if parsed.error_code is not None:
            wire_details["wire_error_code"] = parsed.error_code
        if parsed.proposal is None:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                error_message="The Editorial Enrichment response contained no valid text blocks",
                details=wire_details,
                model_calls=model_calls,
            )
        try:
            enrichment = validate_editorial_enrichment_proposal(
                parsed.proposal,
                evidence_pack,
                extraction,
                synthesis,
                source_figures=_source_figure_candidates(source_figure_inventory),
                warnings=_source_figure_inventory_warnings(source_figure_inventory),
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
                        f"editorial_enrichment_block_rejected:{item.block_id}:{item.reason_code}"
                        for item in parsed.rejections
                    ),
                ),
            )
        if enrichment.diagrams:
            if self._diagram_compiler is None or self._media_asset_store is None:
                raise RuntimeError("Diagram compilation requires a compiler and media asset store")
            compiled_diagrams = await compile_and_store_diagrams(
                enrichment.diagrams,
                compiler=self._diagram_compiler,
                media_asset_store=self._media_asset_store,
                production_run_id=run.id,
            )
            enrichment = replace(enrichment, diagrams=compiled_diagrams)
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
            warnings=enrichment.warnings,
            details=wire_details if parsed.rejections else None,
        )

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
    ) -> str:
        """Persist parse identity and the normalized strict proposal beside raw bytes."""
        assert run.raw_output_sha256 is not None
        identity = editorial_enrichment_parse_identity(run.raw_output_sha256, evidence_pack)
        validation_errors: list[dict[str, Any]] = [
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
                    "value_sha256": run.raw_output_sha256,
                }
            )
        transformations = [
            *parsed.transformations,
            f"editorial_enrichment_parser:{EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION}",
            f"editorial_enrichment_contract:{EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION}",
            f"editorial_enrichment_prompt:{EDITORIAL_ENRICHMENT_PROMPT_VERSION}",
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
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def editorial_enrichment_parse_identity(
    raw_output_sha256: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    *,
    prompt_version: str | None = None,
    contract_version: str | None = None,
    parser_version: str | None = None,
) -> str:
    """Bind normalized output to verified response bytes and request handles."""
    if _SHA256_RE.fullmatch(raw_output_sha256) is None:
        raise ValueError("Raw Editorial Enrichment output hash must be lowercase SHA-256")
    payload = {
        "raw_output_sha256": raw_output_sha256,
        "parser_version": parser_version or EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
        "contract_version": contract_version or EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
        "prompt_version": prompt_version or EDITORIAL_ENRICHMENT_PROMPT_VERSION,
        "request_handle_mapping": [
            {"handle": handle, "evidence_ref": repr(ref)}
            for handle, ref in sorted(evidence_pack._handle_to_ref.items())
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

    synthesis_paragraphs = {
        **{
            lead_paragraph_anchor(index): item.text
            for index, item in enumerate(synthesis.lead, start=1)
        },
        **{
            section_paragraph_anchor(section_index, paragraph_index): paragraph.text
            for section_index, section in enumerate(synthesis.sections)
            for paragraph_index, paragraph in enumerate(section.paragraphs, start=1)
        },
    }
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
