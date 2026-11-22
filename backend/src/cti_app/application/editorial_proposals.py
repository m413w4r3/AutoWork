"""Prepare immutable editorial proposal patches without writing canonical artifacts.

Online orphan cleanup removes unreferenced catalog rows but retains their
content-addressed bytes. Physical object purging belongs to an offline
retention sweep.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import traceback
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_conversation_turns import (
    EditorialDiscussionContext,
    EditorialParagraphEvidenceContext,
)
from cti_app.application.editorial_enrichment_revision import (
    EditorialEnrichmentRevisionValidationError,
    assert_requested_placement,
    targeted_action,
    validate_editorial_enrichment_patch,
    validate_targeted_action,
    validate_targeted_request,
)
from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    ManualSourceFigureArchiveReference,
    archive_reference_for_catalog_item,
    validate_figure_insertion_limit,
)
from cti_app.application.editorial_synthesis import candidate as synthesis_candidate
from cti_app.application.editorial_synthesis.patch_validation import (
    TargetedSynthesisRevisionValidationError,
    assert_non_target_unchanged,
    parse_wire_patch,
    parse_wire_patch_strict,
)
from cti_app.application.editorial_synthesis.targets import (
    resolve_target,
)
from cti_app.application.editorial_text_policy import (
    editorial_caption_text_violation as _editorial_caption_text_violation,
)
from cti_app.application.editorial_text_policy import (
    editorial_patch_text_violation as _editorial_patch_text_violation,
)
from cti_app.application.editorial_visual_revision import (
    VISUAL_REVISION_ERROR_CODES,
    VisualRevisionContext,
    VisualRevisionContextLoader,
    VisualRevisionRejected,
    plan_visual_revision,
    replace_element,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    read_pointer,
    resolve_enrichment_element_target,
    resolve_enrichment_order_target,
    resolve_occurrence,
    revision_target_path,
)
from cti_app.application.media_assets import MediaAssetStore, compile_and_store_charts
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    ChartProposalV1,
    DiagramProposalV1,
    EditorialEnrichmentEvidencePackV1,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentProposalV1,
    EditorialEnrichmentStageErrorCode,
    TableProposalV1,
    bar_chart_record_documents_value_and_unit,
    validate_editorial_enrichment,
)
from cti_app.application.production_synthesis import (
    _all_evidence_entries,
    _technical_literals,
    parse_synthesis_proposal_wire,
)
from cti_app.application.production_synthesis_revision import _apply_wire_patch
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_enrichment_patch import (
    IMPLEMENTED_VISUAL_REVISION_OPERATIONS,
    VISUAL_REVISION_PATCH_SCHEMA_VERSION,
    MoveEnrichmentElementPatch,
    ReorderEnrichmentElementsPatch,
    ReviseChartPatch,
    ReviseDiagramPatch,
    UpdateVisualCaptionPatch,
    VisualRevisionPatch,
    visual_patch_element_kind,
    visual_patch_to_json,
)
from cti_app.domain.editorial_figure_keys import adopted_source_figure_key
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalConflict,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalModelRun,
    EditorialProposalOrigin,
    EditorialProposalState,
    ManualBarChartPatch,
    ManualPatch,
    ManualSourceFigurePatch,
    ManualSourceFigureSelection,
    ManualTablePatch,
    ManualTextPatch,
    variant_group_identity,
)
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialReadOnlyError,
    EditorialSourceStage,
    EditorialWorkspaceError,
)
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.production_editorial_enrichment import (
    BAR_CHART_INSUFFICIENT_DATA_MESSAGE,
    EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
    MAX_BAR_CHART_ADJUSTED_EXPONENT,
    MAX_BAR_CHART_SIGNIFICANT_DIGITS,
    MAX_DIAGRAM_CAPTION_CHARS,
    MAX_DIAGRAM_EDGES,
    MAX_DIAGRAM_GROUP_MEMBERS,
    MAX_DIAGRAM_GROUPS,
    MAX_DIAGRAM_ID_CHARS,
    MAX_DIAGRAM_LABEL_CHARS,
    MAX_DIAGRAM_NODES,
    MAX_DIAGRAM_TITLE_CHARS,
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentElementRef,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    TableSpecV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
    normalize_analytic_question,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionConflictError,
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    production_synthesis_from_json,
    production_synthesis_to_json,
)
from cti_app.domain.publication_kind import PublicationKind

_PATCH_BUCKET = "editorial-proposal-patches"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATCH_BYTES = 256 * 1024
_CLOSED_EDITORIAL_VALIDATOR_CODES = frozenset(
    {
        *(code.value for code in EditorialEnrichmentStageErrorCode),
        "bar_chart_insufficient_data",
        "bar_chart_value_missing",
        "bar_chart_evidence_unadmitted",
        "bar_chart_value_not_evidenced",
        "bar_chart_markup_forbidden",
        "bar_chart_raw_url_forbidden",
        "bar_chart_question_duplicate",
        "bar_chart_local_compilation_failed",
        "bar_chart_media_manifest_missing",
        "chart_purpose_evidence_not_used_by_points",
        "manual_bar_chart_analytic_question_duplicate",
        "manual_edit_patch_invalid",
        "manual_edit_new_fact_unadmitted",
        "manual_table_analytic_question_duplicate",
        "manual_table_row_width_mismatch",
        "figure_caption_markup_forbidden",
        "brief_figure_limit_exceeded",
        "source_figure_inventory_limit_exceeded",
        "editorial_figure_not_eligible",
        "editorial_figure_not_found",
        "figure_catalog_unavailable",
        "table_raw_url_forbidden",
        "proposal_base_unavailable",
        "proposal_patch_invalid",
        "proposal_target_mismatch",
        "editorial_anchor_stale",
        "revision_wire_markup_forbidden",
    }
)
_EDITORIAL_VALIDATOR_CODE_PREFIXES = ("chart_purpose_evidence_not_used_by_points",)


def _parse_editorial_patch_payload(value: str, *, exact_chart_numbers: bool = False) -> Any:
    if exact_chart_numbers:
        return json.loads(value, parse_float=Decimal, parse_int=Decimal)
    return json.loads(value)


_VISUAL_PROPOSAL_OPERATION_VALUES = frozenset(
    {
        TargetedEditorialRevisionOperation.REWRITE_CHART.value,
        TargetedEditorialRevisionOperation.REWRITE_DIAGRAM.value,
        TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION.value,
    }
)
# Codes the model may carry in a REFUSAL block for a visual revision. The
# planner already owns this closed set; the parser only has to accept it.
_VISUAL_MODEL_REFUSAL_CODES = frozenset({"revision_not_applicable", *VISUAL_REVISION_ERROR_CODES})
# Every code a visual proposal may persist as a refused outcome. Wire parser
# reason codes are bounded here so a malformed Bridge answer never becomes a
# free-form diagnostic.
_VISUAL_REFUSAL_CODES = frozenset(
    {
        *_VISUAL_MODEL_REFUSAL_CODES,
        "revision_wire_invalid",
        "revision_wire_duplicate_block",
        "revision_wire_duplicate_end",
        "revision_wire_data_after_end",
        "revision_wire_marker_invalid",
        "revision_wire_unexpected_text",
        "revision_wire_end_missing",
        "revision_wire_refusal_shape_invalid",
        "revision_refusal_code_invalid",
        "revision_refusal_reason_missing",
        "revision_wire_missing_block",
        "revision_wire_shape_invalid",
        "revision_wire_action_invalid",
        "revision_wire_hash_invalid",
        "revision_wire_rationale_missing",
        "revision_wire_after_invalid",
        "revision_wire_after_shape_invalid",
        "proposal_operation_unsupported",
        "proposal_target_mismatch",
        "proposal_base_stale",
    }
)
_FENCED_AFTER_JSON = re.compile(r"```(?:json)?[ \t]*\n?(?P<body>.*?)\n?[ \t]*```", re.DOTALL)
_CAPTION_LINE = re.compile(r"^\s*CAPTION\s*:\s*(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)


class _VisualProposalRefused(Exception):
    """A visual Bridge answer is unusable; persisted as a closed refusal."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _VISUAL_REFUSAL_CODES else "enrichment_revision_patch_invalid"
        super().__init__(self.code)


def _visual_wire_refusal_code(reason_code: str) -> str:
    return reason_code if reason_code in _VISUAL_REFUSAL_CODES else "revision_wire_invalid"


def _after_json_text(value: str) -> str:
    """Strip one Markdown JSON fence, keeping the fenced body when present."""
    match = _FENCED_AFTER_JSON.search(value)
    return (match.group("body") if match is not None else value).strip()


def _parse_visual_json_after(value: str, *, exact_numbers: bool) -> Any:
    try:
        return _parse_editorial_patch_payload(
            _after_json_text(value), exact_chart_numbers=exact_numbers
        )
    except (TypeError, ValueError):
        raise _VisualProposalRefused("revision_wire_after_invalid") from None


def _parse_visual_caption_after(value: str) -> str | None:
    text = _after_json_text(value)
    if text.startswith("{"):
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            raw = None
        if isinstance(raw, Mapping) and "caption" in raw:
            caption = raw.get("caption")
            if caption is not None and not isinstance(caption, str):
                raise _VisualProposalRefused("revision_wire_after_invalid")
            return caption
    match = _CAPTION_LINE.search(text)
    if match is None:
        raise _VisualProposalRefused("revision_wire_after_invalid")
    return match.group("value").strip() or None


def _resolved_visual_refs(
    handles: tuple[str, ...],
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    *,
    unknown_code: str,
) -> tuple[ExtractionEvidenceRefV1, ...]:
    references: dict[ExtractionEvidenceRefV1, None] = {}
    for handle in handles:
        try:
            references[evidence_pack.resolve_handle(handle)] = None
        except EditorialEnrichmentProposalControlError:
            raise _VisualProposalRefused(unknown_code) from None
    return tuple(references)


def _canonical_visual_placement(placement: Any) -> EnrichmentPlacementV1:
    return EnrichmentPlacementV1(
        placement.kind,
        section_index=placement.section_index,
        anchor_block_id=placement.anchor_block_id,
    )


def _canonical_visual_purpose(
    purpose: Any,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    *,
    unknown_code: str,
) -> EditorialAnalyticPurposeV1:
    return EditorialAnalyticPurposeV1(
        question=purpose.question,
        available_data=purpose.available_data,
        comprehension_gain=purpose.comprehension_gain,
        scope=purpose.scope,
        evidence_refs=_resolved_visual_refs(
            purpose.evidence_handles, evidence_pack, unknown_code=unknown_code
        ),
        knowledge_limits=purpose.knowledge_limits,
        placement_reason=purpose.placement_reason,
    )


def _chart_spec_from_visual_proposal(
    chart: ChartProposalV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> ChartSpecV1:
    """Resolve one proposal chart's handles into canonical references.

    Only structural conversion happens here; the planner remains the single
    semantic validator (value/unit proof, text policy, placement, no-op).
    """
    try:
        return ChartSpecV1(
            key=chart.key,
            kind=chart.kind,
            title=chart.title,
            caption=chart.caption,
            placement=_canonical_visual_placement(chart.placement),
            purpose=_canonical_visual_purpose(
                chart.purpose, evidence_pack, unknown_code="bar_chart_insufficient_data"
            ),
            points=tuple(
                ChartPointV1(
                    label=point.label,
                    date=point.date,
                    series=point.series,
                    evidence_refs=_resolved_visual_refs(
                        point.evidence_handles,
                        evidence_pack,
                        unknown_code="bar_chart_insufficient_data",
                    ),
                    value=point.value,
                )
                for point in chart.points
            ),
            category_axis_label=chart.category_axis_label,
            value_axis_label=chart.value_axis_label,
            unit=chart.unit,
        )
    except (TypeError, ValueError):
        raise _VisualProposalRefused("enrichment_revision_patch_invalid") from None


def _bound_bridge_diagram(diagram: DiagramProposalV1) -> None:
    """Refuse an oversize Bridge diagram before any typed construction.

    Bridge output is untrusted: the manual request already caps the same shape,
    so the wire conversion applies the shared domain limits before the planner
    spends CPU on it.
    """

    def refuse() -> None:
        raise _VisualProposalRefused("enrichment_revision_patch_invalid")

    if (
        len(diagram.nodes) > MAX_DIAGRAM_NODES
        or len(diagram.edges) > MAX_DIAGRAM_EDGES
        or len(diagram.groups) > MAX_DIAGRAM_GROUPS
        or len(diagram.title) > MAX_DIAGRAM_TITLE_CHARS
        or (diagram.caption is not None and len(diagram.caption) > MAX_DIAGRAM_CAPTION_CHARS)
    ):
        refuse()
    for node in diagram.nodes:
        if len(node.node_id) > MAX_DIAGRAM_ID_CHARS or len(node.label) > MAX_DIAGRAM_LABEL_CHARS:
            refuse()
    for edge in diagram.edges:
        if (
            len(edge.source_node_id) > MAX_DIAGRAM_ID_CHARS
            or len(edge.target_node_id) > MAX_DIAGRAM_ID_CHARS
            or (edge.label is not None and len(edge.label) > MAX_DIAGRAM_LABEL_CHARS)
        ):
            refuse()
    for group in diagram.groups:
        if (
            len(group.group_id) > MAX_DIAGRAM_ID_CHARS
            or len(group.label) > MAX_DIAGRAM_LABEL_CHARS
            or len(group.node_ids) > MAX_DIAGRAM_GROUP_MEMBERS
        ):
            refuse()


def _diagram_spec_from_visual_proposal(
    diagram: DiagramProposalV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> DiagramSpecV1:
    """Resolve one proposal diagram's handles; the planner validates the rest."""
    unknown_code = "enrichment_revision_patch_invalid"
    _bound_bridge_diagram(diagram)
    try:
        return DiagramSpecV1(
            key=diagram.key,
            kind=diagram.kind,
            title=diagram.title,
            caption=diagram.caption,
            direction=diagram.direction,
            nodes=tuple(
                DiagramNodeV1(
                    node.node_id,
                    node.label,
                    _resolved_visual_refs(
                        node.evidence_handles, evidence_pack, unknown_code=unknown_code
                    ),
                    role=node.role,
                )
                for node in diagram.nodes
            ),
            edges=tuple(
                DiagramEdgeV1(
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.label,
                    _resolved_visual_refs(
                        edge.evidence_handles, evidence_pack, unknown_code=unknown_code
                    ),
                    relation_type=edge.relation_type,
                    direction=edge.direction,
                )
                for edge in diagram.edges
            ),
            groups=tuple(
                DiagramGroupV1(group.group_id, group.label, tuple(group.node_ids))
                for group in diagram.groups
            ),
            placement=_canonical_visual_placement(diagram.placement),
            purpose=_canonical_visual_purpose(
                diagram.purpose, evidence_pack, unknown_code=unknown_code
            ),
            profile=diagram.profile,
        )
    except (TypeError, ValueError):
        raise _VisualProposalRefused(unknown_code) from None


def _single_visual_proposal_element(
    raw: object,
    *,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> ChartSpecV1 | DiagramSpecV1:
    """Convert one one-element proposal wire into its canonical typed spec."""
    if not isinstance(raw, Mapping):
        raise _VisualProposalRefused("revision_wire_after_invalid")
    try:
        proposal = EditorialEnrichmentProposalV1.model_validate(raw)
    except (TypeError, ValueError):
        raise _VisualProposalRefused("revision_wire_after_shape_invalid") from None
    if proposal.annotations or proposal.resource_needs:
        raise _VisualProposalRefused("enrichment_revision_patch_invalid")
    if element_kind is EditorialEnrichmentElementKind.CHART:
        if len(proposal.charts) != 1 or proposal.tables or proposal.diagrams or proposal.figures:
            raise _VisualProposalRefused("revision_wire_after_shape_invalid")
        chart = proposal.charts[0]
        if chart.key != element_key:
            raise _VisualProposalRefused("enrichment_revision_patch_invalid")
        return _chart_spec_from_visual_proposal(chart, evidence_pack)
    if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
        if len(proposal.diagrams) != 1 or proposal.tables or proposal.charts or proposal.figures:
            raise _VisualProposalRefused("revision_wire_after_shape_invalid")
        diagram = proposal.diagrams[0]
        if diagram.key != element_key:
            raise _VisualProposalRefused("enrichment_revision_patch_invalid")
        return _diagram_spec_from_visual_proposal(diagram, evidence_pack)
    raise _VisualProposalRefused("enrichment_revision_patch_invalid")


@dataclass(frozen=True, slots=True)
class _ThreadVisualRevisionParse:
    operation: TargetedEditorialRevisionOperation
    patch: VisualRevisionPatch | None = None
    refusal_code: str | None = None
    rationale: str = ""


def _parse_thread_visual_revision(
    raw_response: str,
    *,
    allowed_operations: set[str],
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
    before_hash: str,
    target_path: str | None,
    base_canonical_sha256: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
) -> _ThreadVisualRevisionParse:
    fallback = next(
        (
            operation
            for operation in TargetedEditorialRevisionOperation
            if operation.value in _VISUAL_PROPOSAL_OPERATION_VALUES
            and operation.value in allowed_operations
        ),
        None,
    )
    if fallback is None:
        return _ThreadVisualRevisionParse(
            TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION,
            refusal_code="proposal_operation_unsupported",
        )
    try:
        parsed = parse_wire_patch(raw_response, allowed_refusal_codes=_VISUAL_MODEL_REFUSAL_CODES)
    except TargetedSynthesisRevisionValidationError as error:
        return _ThreadVisualRevisionParse(
            fallback, refusal_code=_visual_wire_refusal_code(error.reason_code)
        )
    if parsed.refusal_code is not None:
        return _ThreadVisualRevisionParse(
            fallback, refusal_code=_visual_wire_refusal_code(parsed.refusal_code)
        )
    operation = parsed.action
    if (
        operation.value not in _VISUAL_PROPOSAL_OPERATION_VALUES
        or operation.value not in allowed_operations
    ):
        return _ThreadVisualRevisionParse(operation, refusal_code="proposal_operation_unsupported")
    if parsed.base_artifact_hash != base_canonical_sha256:
        return _ThreadVisualRevisionParse(operation, refusal_code="proposal_base_stale")
    if target_path is None or parsed.target != target_path:
        return _ThreadVisualRevisionParse(operation, refusal_code="proposal_target_mismatch")
    if parsed.before_hash != before_hash:
        return _ThreadVisualRevisionParse(operation, refusal_code="proposal_base_stale")
    try:
        if operation is TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION:
            typed_patch: VisualRevisionPatch = UpdateVisualCaptionPatch(
                element_kind=element_kind,
                element_key=element_key,
                caption=_parse_visual_caption_after(parsed.after),
            )
        elif operation is TargetedEditorialRevisionOperation.REWRITE_CHART:
            chart_spec = _single_visual_proposal_element(
                _parse_visual_json_after(parsed.after, exact_numbers=True),
                element_kind=element_kind,
                element_key=element_key,
                evidence_pack=evidence_pack,
            )
            assert isinstance(chart_spec, ChartSpecV1)
            typed_patch = ReviseChartPatch(element_key=element_key, chart=chart_spec)
        else:
            diagram_spec = _single_visual_proposal_element(
                _parse_visual_json_after(parsed.after, exact_numbers=False),
                element_kind=element_kind,
                element_key=element_key,
                evidence_pack=evidence_pack,
            )
            assert isinstance(diagram_spec, DiagramSpecV1)
            typed_patch = ReviseDiagramPatch(element_key=element_key, diagram=diagram_spec)
    except _VisualProposalRefused as refusal:
        return _ThreadVisualRevisionParse(operation, refusal_code=refusal.code)
    except ValueError:
        return _ThreadVisualRevisionParse(
            operation, refusal_code="enrichment_revision_patch_invalid"
        )
    return _ThreadVisualRevisionParse(operation, typed_patch, rationale=parsed.rationale)


def _thread_turn_request_identity(
    *,
    thread_id: UUID,
    model_turn_id: UUID,
    model_run_id: UUID,
    raw_response: str,
    snapshot_id: UUID,
    occurrence_id: UUID,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "thread_id": str(thread_id),
                "turn_id": str(model_turn_id),
                "model_run_id": str(model_run_id),
                "response_sha256": hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
                "snapshot_id": str(snapshot_id),
                "occurrence_id": str(occurrence_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _ThreadRefusalEvent:
    """Pre-uploaded refused-event body, appended atomically with its proposal."""

    blob_id: UUID
    payload_sha256: str
    code: str


class EditorialTableDiagnosticReason(StrEnum):
    IDEMPOTENCY_KEY_INVALID = "manual_table:idempotency_key_invalid"
    DEPENDENCIES_UNAVAILABLE = "manual_table:dependencies_unavailable"
    ANCHOR_MISMATCH = "manual_table:anchor_mismatch"
    MARKUP_REJECTED = "manual_table:markup_rejected"
    REQUEST_TOO_LARGE = "manual_table:request_too_large"
    IDEMPOTENCY_CONFLICT = "manual_table:idempotency_conflict"
    CONTEXT_UNAVAILABLE = "manual_table:context_unavailable"
    SNAPSHOT_MISMATCH = "manual_table:snapshot_mismatch"
    OPERATION_UNAVAILABLE = "manual_table:operation_unavailable"
    BASE_CONTENT_UNAVAILABLE = "manual_table:base_content_unavailable"
    DUPLICATE_QUESTION = "manual_table:duplicate_question"
    EVIDENCE_PACK_UNAVAILABLE = "manual_table:evidence_pack_unavailable"
    EVIDENCE_UNADMITTED = "manual_table:evidence_unadmitted"
    TARGET_UNAVAILABLE = "manual_table:target_unavailable"
    ANCHOR_STALE = "manual_table:anchor_stale"
    PIN_STALE = "manual_table:pin_stale"
    ROW_WIDTH_MISMATCH = "manual_table:row_width_mismatch"
    REQUEST_SCHEMA_INVALID = "manual_table:request_schema_invalid"
    VALIDATOR_REJECTED = "manual_table:validator_rejected"


class EditorialBarChartDiagnosticReason(StrEnum):
    REQUEST_INVALID = "manual_bar_chart:request_invalid"
    ANCHOR_STALE = "manual_bar_chart:anchor_stale"
    DEPENDENCIES_UNAVAILABLE = "manual_bar_chart:dependencies_unavailable"
    EVIDENCE_UNADMITTED = "manual_bar_chart:evidence_unadmitted"
    QUESTION_DUPLICATE = "manual_bar_chart:question_duplicate"
    MARKUP_REJECTED = "manual_bar_chart:markup_rejected"
    VALIDATOR_REJECTED = "manual_bar_chart:validator_rejected"
    IDEMPOTENCY_CONFLICT = "manual_bar_chart:idempotency_conflict"


class EditorialFigureDiagnosticReason(StrEnum):
    REQUEST_INVALID = "manual_source_figure:request_invalid"
    ANCHOR_STALE = "manual_source_figure:anchor_stale"
    DEPENDENCIES_UNAVAILABLE = "manual_source_figure:dependencies_unavailable"
    CATALOG_UNAVAILABLE = "manual_source_figure:catalog_unavailable"
    NOT_ELIGIBLE = "manual_source_figure:not_eligible"
    BRIEF_LIMIT = "manual_source_figure:brief_limit"
    INVENTORY_LIMIT = "manual_source_figure:inventory_limit"
    MARKUP_REJECTED = "manual_source_figure:markup_rejected"
    IDEMPOTENCY_CONFLICT = "manual_source_figure:idempotency_conflict"
    VALIDATOR_REJECTED = "manual_source_figure:validator_rejected"


def _manual_figure_error_reason(code: str) -> EditorialFigureDiagnosticReason:
    return {
        "editorial_anchor_stale": EditorialFigureDiagnosticReason.ANCHOR_STALE,
        "proposal_base_unavailable": EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
        "proposal_workspace_unavailable": EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
        "editorial_figure_not_found": EditorialFigureDiagnosticReason.CATALOG_UNAVAILABLE,
        "figure_catalog_unavailable": EditorialFigureDiagnosticReason.CATALOG_UNAVAILABLE,
        "editorial_figure_not_eligible": EditorialFigureDiagnosticReason.NOT_ELIGIBLE,
        "brief_figure_limit_exceeded": EditorialFigureDiagnosticReason.BRIEF_LIMIT,
        "source_figure_inventory_limit_exceeded": EditorialFigureDiagnosticReason.INVENTORY_LIMIT,
        "manual_edit_patch_invalid": EditorialFigureDiagnosticReason.MARKUP_REJECTED,
        "proposal_operation_unsupported": EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
    }.get(code, EditorialFigureDiagnosticReason.VALIDATOR_REJECTED)


def _manual_figure_failure(
    code: str,
    reason: EditorialFigureDiagnosticReason,
    *,
    frame: str | None = None,
    validator_code: str | None = None,
) -> EditorialProposalApplicationError:
    if frame is None:
        caller = sys._getframe(1)
        frame = _frame_string(caller.f_code.co_filename, caller.f_lineno, caller.f_code.co_name)
    return EditorialProposalApplicationError(
        code,
        reason=reason.value,
        frame=frame,
        validator_code=validator_code or code,
    )


def _manual_bar_chart_error_reason(code: str) -> EditorialBarChartDiagnosticReason:
    return {
        "editorial_anchor_stale": EditorialBarChartDiagnosticReason.ANCHOR_STALE,
        "proposal_base_unavailable": (EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE),
        "proposal_workspace_unavailable": (
            EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE
        ),
        "manual_edit_new_fact_unadmitted": EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED,
        "bar_chart_insufficient_data": EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED,
        "manual_bar_chart_analytic_question_duplicate": (
            EditorialBarChartDiagnosticReason.QUESTION_DUPLICATE
        ),
        "manual_edit_patch_invalid": EditorialBarChartDiagnosticReason.MARKUP_REJECTED,
        "proposal_operation_unsupported": (
            EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE
        ),
    }.get(code, EditorialBarChartDiagnosticReason.VALIDATOR_REJECTED)


def _manual_bar_chart_failure(
    code: str,
    reason: EditorialBarChartDiagnosticReason,
    *,
    frame: str | None = None,
    validator_code: str | None = None,
) -> EditorialProposalApplicationError:
    if frame is None:
        caller = sys._getframe(1)
        frame = _frame_string(caller.f_code.co_filename, caller.f_lineno, caller.f_code.co_name)
    return EditorialProposalApplicationError(
        code,
        reason=reason.value,
        frame=frame,
        validator_code=validator_code or code,
    )


def _frame_string(filename: str, lineno: int, function: str) -> str:
    return f"{filename.rsplit('/', 1)[-1]}:{lineno}:{function}"


def _exception_origin_frame(error: BaseException) -> str | None:
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return _frame_string(frame.filename, frame.lineno or 0, frame.name)


def _editorial_table_text_violation(table: TableSpecV1 | TableProposalV1) -> bool:
    text_values = [table.key, table.title]
    if table.caption is not None:
        if _editorial_caption_text_violation(table.caption):
            return True
    text_values.extend(value for column in table.columns for value in (column.key, column.label))
    text_values.extend(cell for row in table.rows for cell in row.cells)
    if table.purpose is not None:
        text_values.extend(
            (
                table.purpose.question,
                table.purpose.available_data,
                table.purpose.comprehension_gain,
                table.purpose.scope,
                table.purpose.knowledge_limits,
                table.purpose.placement_reason,
            )
        )
    return any(_editorial_patch_text_violation(value) for value in text_values)


def _editorial_bar_chart_text_violation(chart: ChartSpecV1 | ChartProposalV1) -> bool:
    text_values = [
        chart.key,
        chart.title,
        chart.category_axis_label or "",
        chart.value_axis_label or "",
        chart.unit or "",
        *(point.label for point in chart.points),
        *(point.series for point in chart.points),
    ]
    if chart.caption is not None:
        if _editorial_caption_text_violation(chart.caption):
            return True
    text_values.extend(
        (
            chart.purpose.question,
            chart.purpose.available_data,
            chart.purpose.comprehension_gain,
            chart.purpose.scope,
            chart.purpose.knowledge_limits,
            chart.purpose.placement_reason,
        )
    )
    return any(_editorial_patch_text_violation(value) for value in text_values)


def _validate_bar_chart_evidence(
    chart: ChartSpecV1,
    *,
    evidence_entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    admitted_refs: frozenset[ExtractionEvidenceRefV1],
) -> None:
    """Require every displayed number and unit in the cited admitted source record."""
    if chart.kind is not ChartKind.BAR:
        raise EditorialProposalApplicationError("proposal_operation_unsupported")
    for point in chart.points:
        if point.value is None or not point.evidence_refs:
            raise _manual_bar_chart_failure(
                "bar_chart_insufficient_data",
                EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED,
                validator_code="bar_chart_value_missing",
            )
        if any(reference not in admitted_refs for reference in point.evidence_refs):
            raise _manual_bar_chart_failure(
                "bar_chart_insufficient_data",
                EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED,
                validator_code="bar_chart_evidence_unadmitted",
            )
        value_and_unit_share_evidence = False
        for reference in point.evidence_refs:
            if reference.kind not in {EvidenceKind.FACT, EvidenceKind.EVENT}:
                continue
            payload = evidence_entries.get(reference)
            if payload is None:
                continue
            value_fields = (
                ("value", "evidence_quote")
                if reference.kind is EvidenceKind.FACT
                else ("text", "evidence_quote")
            )
            if bar_chart_record_documents_value_and_unit(
                payload,
                value=point.value,
                unit=chart.unit,
                text_fields=value_fields,
            ):
                value_and_unit_share_evidence = True
                break
        if not value_and_unit_share_evidence:
            raise _manual_bar_chart_failure(
                "bar_chart_insufficient_data",
                EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED,
                validator_code="bar_chart_value_not_evidenced",
            )


def _normalize_editorial_grounding_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\bhxxps(?=://)", "https", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bhxxp(?=://)", "http", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\})\s*",
        ".",
        normalized,
    )
    return re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)


def _validate_editorial_text_grounding(
    value: str,
    *,
    admitted_evidence_texts: tuple[str, ...] | list[str],
    allow_newline_tab: bool = False,
) -> None:
    """Apply stricter local grounding to editorial patches without changing legacy synthesis."""
    if _editorial_patch_text_violation(value, allow_newline_tab=allow_newline_tab):
        raise EditorialProposalApplicationError("manual_edit_patch_invalid")
    candidate_literals = _technical_literals(_normalize_editorial_grounding_text(value))
    if not candidate_literals:
        return
    admitted_literals = {
        literal
        for evidence_text in admitted_evidence_texts
        for literal in _technical_literals(_normalize_editorial_grounding_text(evidence_text))
    }
    if not candidate_literals <= admitted_literals:
        raise EditorialProposalApplicationError("manual_edit_new_fact_unadmitted")


def _validate_editorial_paragraph_wire_grounding(
    value: str,
    *,
    admitted_evidence_texts: tuple[str, ...] | list[str],
) -> None:
    parsed = parse_synthesis_proposal_wire(value, require_lead=True)
    if parsed.proposal is None or parsed.error_code is not None:
        return
    claims = (
        *parsed.proposal.lead,
        *(claim for section in parsed.proposal.sections for claim in section.claims),
    )
    for claim in claims:
        _validate_editorial_text_grounding(
            claim.text,
            admitted_evidence_texts=admitted_evidence_texts,
            allow_newline_tab=True,
        )


def _admitted_evidence_texts(
    context: EditorialParagraphEvidenceContext | None,
) -> tuple[str, ...]:
    if context is None or not context.evidence_handles:
        return ()
    admitted_handles = set(context.evidence_handles)
    entries = (
        *context.evidence_pack.narrative_evidence,
        *context.evidence_pack.technical_evidence,
        *context.evidence_pack.reserve_evidence,
    )
    return tuple(
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for entry in entries
        if isinstance(entry.get("handle"), str) and entry["handle"] in admitted_handles
    )


def _editorial_evidence_reference_values(
    context: EditorialParagraphEvidenceContext | None,
) -> list[dict[str, str]]:
    if context is None:
        return []
    return [
        {
            "source_document_id": str(reference.source_document_id),
            "kind": reference.kind.value,
            "evidence_key": reference.evidence_key,
        }
        for reference in context.evidence_refs
    ]


def _manual_idempotency_key(purpose: str, client_key: str) -> str:
    digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
    return f"manual_{purpose}_{digest}"


def editorial_manual_decision_idempotency_key(client_key: str) -> str:
    """Derive a decision key distinct from the preparation event key."""
    return _manual_idempotency_key("accept", client_key)


def _manual_request_sha256(
    *,
    subject_id: UUID,
    snapshot_id: UUID,
    occurrence_id: UUID,
    operation: TargetedEditorialRevisionOperation,
    text: str,
) -> str:
    identity = {
        "subject_id": str(subject_id),
        "snapshot_id": str(snapshot_id),
        "occurrence_id": str(occurrence_id),
        "operation": operation.value,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _visual_revision_request_sha256(
    *,
    subject_id: UUID,
    snapshot_id: UUID,
    occurrence_id: UUID,
    patch: VisualRevisionPatch,
) -> str:
    identity = {
        "action": "prepare_visual_revision",
        "subject_id": str(subject_id),
        "snapshot_id": str(snapshot_id),
        "occurrence_id": str(occurrence_id),
        "patch": visual_patch_to_json(patch),
    }
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(identity)).hexdigest()


def _bind_visual_revision_base_placement(
    enrichment: EditorialEnrichmentV1,
    patch: VisualRevisionPatch,
) -> VisualRevisionPatch:
    """Bind the server-owned placement of a chart/diagram revision to the base.

    The manual request body carries no placement: a visual revision never moves
    its element (move is a separate variant), so the exact pinned placement is
    authoritative. Binding here, once the base is loaded, keeps prepare, the
    persisted patch and adoption's replan on the same typed value; the planner
    still validates every other field against the same base element.
    """
    if isinstance(patch, ReviseChartPatch):
        base_chart = next(
            (chart for chart in enrichment.charts if chart.key == patch.element_key), None
        )
        if base_chart is None:
            return patch
        return replace(patch, chart=replace(patch.chart, placement=base_chart.placement))
    if isinstance(patch, ReviseDiagramPatch):
        base_diagram = next(
            (diagram for diagram in enrichment.diagrams if diagram.key == patch.element_key),
            None,
        )
        if base_diagram is None:
            return patch
        return replace(patch, diagram=replace(patch.diagram, placement=base_diagram.placement))
    return patch


def _enrichment_element_keys(
    enrichment: EditorialEnrichmentV1,
    kind: EditorialEnrichmentElementKind,
) -> frozenset[str]:
    """The element keys of one family in the pinned base enrichment."""
    if kind is EditorialEnrichmentElementKind.TABLE:
        return frozenset(table.key for table in enrichment.tables)
    if kind is EditorialEnrichmentElementKind.CHART:
        return frozenset(chart.key for chart in enrichment.charts)
    if kind is EditorialEnrichmentElementKind.DIAGRAM:
        return frozenset(diagram.key for diagram in enrichment.diagrams)
    return frozenset(figure.key for figure in enrichment.source_figures)


def _visual_revision_failure(
    error: VisualRevisionRejected,
) -> EditorialProposalApplicationError:
    diagnostics = dict(error.diagnostics)
    if error.code == "proposal_base_stale":
        raw_reason = diagnostics.get("reason")
        reason = (
            raw_reason
            if isinstance(raw_reason, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", raw_reason) is not None
            else "workspace:visual_revision"
        )
        return EditorialProposalApplicationError(error.code, reason=reason, diagnostics=diagnostics)
    return EditorialProposalApplicationError(error.code, diagnostics=diagnostics)


def _evidence_reference_json(reference: Any) -> dict[str, object]:
    return {
        "source_document_id": str(reference.source_document_id),
        "kind": reference.kind.value,
        "evidence_key": reference.evidence_key,
    }


def _manual_table_domain_json(table: Any) -> dict[str, object]:
    purpose = table.purpose
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
                    _evidence_reference_json(reference) for reference in row.evidence_refs
                ],
            }
            for row in table.rows
        ],
        "placement": {
            "kind": table.placement.kind.value,
            "anchor_block_id": str(table.placement.anchor_block_id),
        },
        "purpose": (
            {
                "question": purpose.question,
                "available_data": purpose.available_data,
                "comprehension_gain": purpose.comprehension_gain,
                "scope": purpose.scope,
                "evidence_refs": [
                    _evidence_reference_json(reference) for reference in purpose.evidence_refs
                ],
                "knowledge_limits": purpose.knowledge_limits,
                "placement_reason": purpose.placement_reason,
            }
            if purpose is not None
            else None
        ),
    }


def _manual_table_proposal_json(
    table: Any,
    *,
    row_handles: list[str],
    purpose_handles: list[str],
) -> dict[str, object]:
    purpose = table.purpose
    row_offset = 0
    rows: list[dict[str, object]] = []
    for row in table.rows:
        count = len(row.evidence_refs)
        rows.append(
            {
                "cells": list(row.cells),
                "evidence_handles": row_handles[row_offset : row_offset + count],
            }
        )
        row_offset += count
    return {
        "key": table.key,
        "kind": table.kind.value,
        "title": table.title,
        "caption": table.caption,
        "columns": [{"key": column.key, "label": column.label} for column in table.columns],
        "rows": rows,
        "placement": {
            "kind": "after_paragraph",
            "section_index": None,
            "anchor_block_id": str(table.placement.anchor_block_id),
        },
        "purpose": {
            "question": purpose.question,
            "available_data": purpose.available_data,
            "comprehension_gain": purpose.comprehension_gain,
            "scope": purpose.scope,
            "evidence_handles": purpose_handles,
            "knowledge_limits": purpose.knowledge_limits,
            "placement_reason": purpose.placement_reason,
        },
    }


def _manual_bar_chart_proposal_json(
    chart: ChartSpecV1,
    *,
    point_handles: list[list[str]],
    purpose_handles: list[str],
) -> dict[str, object]:
    purpose = chart.purpose
    return {
        "key": chart.key,
        "kind": ChartKind.BAR.value,
        "title": chart.title,
        "caption": chart.caption,
        "category_axis_label": chart.category_axis_label,
        "value_axis_label": chart.value_axis_label,
        "unit": chart.unit,
        "placement": {
            "kind": "after_paragraph",
            "section_index": None,
            "anchor_block_id": str(chart.placement.anchor_block_id),
        },
        "purpose": {
            "question": purpose.question,
            "available_data": purpose.available_data,
            "comprehension_gain": purpose.comprehension_gain,
            "scope": purpose.scope,
            "evidence_handles": purpose_handles,
            "knowledge_limits": purpose.knowledge_limits,
            "placement_reason": purpose.placement_reason,
        },
        "points": [
            {
                "label": point.label,
                "series": point.series,
                "value": format(point.value, "f") if point.value is not None else None,
                "evidence_handles": point_handles[index],
            }
            for index, point in enumerate(chart.points)
        ],
    }


def _manual_bar_chart_domain_json(chart: ChartSpecV1) -> dict[str, object]:
    return {
        "key": chart.key,
        "kind": ChartKind.BAR.value,
        "title": chart.title,
        "caption": chart.caption,
        "placement": {
            "kind": chart.placement.kind.value,
            "section_index": chart.placement.section_index,
            "anchor_block_id": str(chart.placement.anchor_block_id),
        },
        "purpose": {
            "question": chart.purpose.question,
            "available_data": chart.purpose.available_data,
            "comprehension_gain": chart.purpose.comprehension_gain,
            "scope": chart.purpose.scope,
            "evidence_refs": [
                _evidence_reference_json(reference) for reference in chart.purpose.evidence_refs
            ],
            "knowledge_limits": chart.purpose.knowledge_limits,
            "placement_reason": chart.purpose.placement_reason,
        },
        "points": [
            {
                "label": point.label,
                "series": point.series,
                "value": format(point.value, "f") if point.value is not None else None,
                "evidence_refs": [
                    _evidence_reference_json(reference) for reference in point.evidence_refs
                ],
            }
            for point in chart.points
        ],
        "category_axis_label": chart.category_axis_label,
        "value_axis_label": chart.value_axis_label,
        "unit": chart.unit,
    }


def _manual_bar_chart_evidence_handles(
    chart: ChartSpecV1, *, handles_by_ref: Mapping[Any, str]
) -> tuple[list[list[str]], list[dict[str, object]]]:
    point_handles: list[list[str]] = []
    refs: list[dict[str, object]] = []
    for point in chart.points:
        handles = [handles_by_ref.get(reference) for reference in point.evidence_refs]
        if not handles or any(handle is None for handle in handles):
            raise EditorialProposalApplicationError(
                "manual_edit_new_fact_unadmitted", validator_code="bar_chart_evidence_unadmitted"
            )
        point_handles.append([str(handle) for handle in handles])
        refs.extend(_evidence_reference_json(reference) for reference in point.evidence_refs)
    purpose_handles = [handles_by_ref.get(reference) for reference in chart.purpose.evidence_refs]
    if not purpose_handles or any(handle is None for handle in purpose_handles):
        raise EditorialProposalApplicationError(
            "manual_edit_new_fact_unadmitted", validator_code="bar_chart_evidence_unadmitted"
        )
    refs.extend(_evidence_reference_json(reference) for reference in chart.purpose.evidence_refs)
    return point_handles, refs


def _manual_bar_chart_request_sha256(
    *, subject_id: UUID, snapshot_id: UUID, occurrence_id: UUID, chart: ChartSpecV1
) -> str:
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(
            {
                "subject_id": str(subject_id),
                "snapshot_id": str(snapshot_id),
                "occurrence_id": str(occurrence_id),
                "operation": TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
                "chart": _manual_bar_chart_domain_json(chart),
            }
        )
    ).hexdigest()


def _manual_figure_request_sha256(
    *,
    subject_id: UUID,
    snapshot_id: UUID,
    occurrence_id: UUID,
    figures: tuple[ManualSourceFigureSelection, ...],
    references: tuple[ManualSourceFigureArchiveReference, ...],
) -> str:
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(
            {
                "subject_id": str(subject_id),
                "snapshot_id": str(snapshot_id),
                "anchor_block_id": str(occurrence_id),
                "figures": [
                    {
                        "figure_id": str(reference.figure_id),
                        "source_figure_id": str(reference.source_figure_id),
                        "caption": selection.caption.strip(),
                        "order": selection.order,
                        "production_run_id": str(reference.production_run_id),
                        "source_document_id": str(reference.source_document_id),
                        "source_url": reference.source_url,
                        "sha256": reference.sha256,
                        "mime_type": reference.mime_type,
                        "byte_size": reference.byte_size,
                        "width": reference.width,
                        "height": reference.height,
                        "provenance": reference.provenance,
                    }
                    for selection, reference in zip(figures, references, strict=True)
                ],
            }
        )
    ).hexdigest()


def _manual_table_evidence_handles(
    table: Any, *, handles_by_ref: Mapping[Any, str]
) -> tuple[list[str], list[dict[str, object]]]:
    table_handles: list[str] = []
    refs: list[dict[str, object]] = []
    for row in table.rows:
        row_handles = [handles_by_ref.get(reference) for reference in row.evidence_refs]
        if not row_handles or any(handle is None for handle in row_handles):
            raise EditorialProposalApplicationError("manual_edit_new_fact_unadmitted")
        table_handles.extend(str(handle) for handle in row_handles)
        refs.extend(_evidence_reference_json(reference) for reference in row.evidence_refs)

    purpose_handles = [handles_by_ref.get(reference) for reference in table.purpose.evidence_refs]
    if not purpose_handles or any(handle is None for handle in purpose_handles):
        raise EditorialProposalApplicationError("manual_edit_new_fact_unadmitted")
    refs.extend(_evidence_reference_json(reference) for reference in table.purpose.evidence_refs)
    return table_handles, refs


def _manual_table_question_conflicts_with_base(
    question: str,
    base_content: Mapping[str, Any],
) -> bool:
    normalized_question = normalize_analytic_question(question)
    if not normalized_question:
        return False
    for collection_name in ("tables", "diagrams", "charts", "source_figures"):
        entries = base_content.get(collection_name)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            purpose = entry.get("purpose")
            if not isinstance(purpose, Mapping):
                continue
            existing_question = purpose.get("question")
            if (
                isinstance(existing_question, str)
                and normalize_analytic_question(existing_question) == normalized_question
            ):
                return True
    return False


def _manual_table_request_sha256(
    *, subject_id: UUID, snapshot_id: UUID, occurrence_id: UUID, table: Any
) -> str:
    identity = {
        "subject_id": str(subject_id),
        "snapshot_id": str(snapshot_id),
        "occurrence_id": str(occurrence_id),
        "operation": TargetedEditorialRevisionOperation.CREATE_TABLE.value,
        "table": _manual_table_domain_json(table),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _editorial_workspace_error_code(error: EditorialWorkspaceError) -> str:
    return error.code if error.code != "editorial_workspace_error" else str(error)


def _editorial_read_only_failure(
    error: EditorialWorkspaceError,
) -> EditorialProposalApplicationError | None:
    if isinstance(error, EditorialReadOnlyError):
        return EditorialProposalApplicationError("editorial_read_only", reason=error.reason)
    return None


def _manual_wire_response(
    *,
    target_path: str,
    operation: TargetedEditorialRevisionOperation,
    base_sha256: str,
    before_hash: str,
    after: str,
) -> str:
    return "\n".join(
        (
            "@@ TARGET @@",
            target_path,
            "@@ ACTION @@",
            operation.value,
            "@@ BASE_ARTIFACT_HASH @@",
            base_sha256,
            "@@ BEFORE_HASH @@",
            before_hash,
            "@@ AFTER @@",
            after,
            "@@ RATIONALE @@",
            "Modification manuelle validée par l’analyste.",  # noqa: RUF001
            "@@ END @@",
        )
    )


def _manual_paragraph_wire(
    *,
    after: str,
    target: TargetedEditorialRevisionTarget,
    target_indexes: Any,
    canonical_content: Mapping[str, Any],
    evidence_context: EditorialParagraphEvidenceContext,
) -> str:
    handles = tuple(
        dict.fromkeys(
            handle
            for reference in evidence_context.evidence_refs
            if (handle := evidence_context.evidence_pack.handle_for(reference)) is not None
        )
    )
    if not handles:
        return after
    evidence_line = " AND ".join(handles)
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
        group_open, group_close = "@@LEAD@@", "@@END LEAD@@"
    elif target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
        sections = canonical_content.get("sections")
        parent_index = target_indexes.parent_index
        if (
            not isinstance(sections, list)
            or parent_index is None
            or parent_index >= len(sections)
            or not isinstance(sections[parent_index], Mapping)
        ):
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        section_kind = sections[parent_index].get("kind")
        if not isinstance(section_kind, str):
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        group_open, group_close = f"@@SECTION {section_kind}@@", "@@END SECTION@@"
    else:
        raise EditorialProposalApplicationError("proposal_operation_unsupported")
    return "\n".join(
        (
            group_open,
            "@@CLAIM@@",
            f"EVIDENCE: {evidence_line}",
            f"TEXT: {after}",
            "@@END CLAIM@@",
            group_close,
        )
    )


class EditorialProposalApplicationError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        reason: str | None = None,
        frame: str | None = None,
        validator_code: str | None = None,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        if code == "proposal_base_stale" and (
            reason is None or re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", reason) is None
        ):
            raise ValueError("proposal_base_stale requires a closed reason")
        self.code = code
        self.reason = reason
        self.frame = frame
        self.validator_code = validator_code
        self.diagnostics: dict[str, object] = dict(diagnostics or {})
        super().__init__(code)


def _manual_table_failure(
    code: str,
    reason: EditorialTableDiagnosticReason,
    *,
    frame: str | None = None,
    validator_code: str | None = None,
) -> EditorialProposalApplicationError:
    if frame is None:
        caller = sys._getframe(1)
        frame = _frame_string(
            caller.f_code.co_filename,
            caller.f_lineno,
            caller.f_code.co_name,
        )
    return EditorialProposalApplicationError(
        code,
        reason=reason.value,
        frame=frame,
        validator_code=validator_code,
    )


def _manual_table_error_reason(code: str) -> EditorialTableDiagnosticReason:
    return {
        "idempotency_key_invalid": EditorialTableDiagnosticReason.IDEMPOTENCY_KEY_INVALID,
        "manual_edit_request_too_large": EditorialTableDiagnosticReason.REQUEST_TOO_LARGE,
        "editorial_anchor_stale": EditorialTableDiagnosticReason.ANCHOR_STALE,
        "proposal_base_stale": EditorialTableDiagnosticReason.PIN_STALE,
        "editorial_snapshot_stale": EditorialTableDiagnosticReason.PIN_STALE,
        "proposal_workspace_unavailable": EditorialTableDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
        "proposal_base_unavailable": EditorialTableDiagnosticReason.CONTEXT_UNAVAILABLE,
        "proposal_operation_unsupported": EditorialTableDiagnosticReason.OPERATION_UNAVAILABLE,
        "manual_table_analytic_question_duplicate": (
            EditorialTableDiagnosticReason.DUPLICATE_QUESTION
        ),
        "manual_edit_new_fact_unadmitted": EditorialTableDiagnosticReason.EVIDENCE_UNADMITTED,
        "proposal_target_mismatch": EditorialTableDiagnosticReason.TARGET_UNAVAILABLE,
    }.get(code, EditorialTableDiagnosticReason.VALIDATOR_REJECTED)


def _manual_table_validation_diagnostics(
    preparation: EditorialProposalPreparation,
    error: BaseException | None,
    *,
    code: str,
) -> dict[str, object]:
    if (
        preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
        and preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
    ):
        frame_value = (
            getattr(error, "frame", None) or _exception_origin_frame(error)
            if error is not None
            else None
        )
        frame = frame_value if isinstance(frame_value, str) else None
        if frame is None:
            caller = sys._getframe(1)
            frame = _frame_string(caller.f_code.co_filename, caller.f_lineno, caller.f_code.co_name)
        return {
            "code": code,
            "reason": _manual_figure_error_reason(code).value,
            "validator_code": _manual_table_validator_code(error, fallback=code),
            "frame": frame,
        }
    if (
        preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
        and preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
    ):
        bar_reason = _manual_bar_chart_error_reason(code)
        frame_value = (
            getattr(error, "frame", None) or _exception_origin_frame(error)
            if error is not None
            else None
        )
        frame = frame_value if isinstance(frame_value, str) else None
        if frame is None:
            caller = sys._getframe(1)
            frame = _frame_string(caller.f_code.co_filename, caller.f_lineno, caller.f_code.co_name)
        return {
            "code": code,
            "reason": bar_reason.value,
            "validator_code": _manual_table_validator_code(error, fallback=code),
            "frame": frame,
        }
    if (
        preparation.origin is not EditorialProposalOrigin.MANUAL_PATCH
        or preparation.operation is not TargetedEditorialRevisionOperation.CREATE_TABLE
    ):
        return {"code": code}
    table_reason = _manual_table_error_reason(code)
    frame = (
        getattr(error, "frame", None) or _exception_origin_frame(error)
        if error is not None
        else None
    )
    if frame is None:
        caller = sys._getframe(1)
        frame = _frame_string(caller.f_code.co_filename, caller.f_lineno, caller.f_code.co_name)
    assert frame is not None
    return {
        "code": code,
        "reason": table_reason.value,
        "validator_code": _manual_table_validator_code(error, fallback=code),
        "frame": frame,
    }


def _closed_editorial_validator_code(value: object) -> str | None:
    candidate = getattr(value, "value", value)
    if not isinstance(candidate, str):
        return None
    if candidate in _CLOSED_EDITORIAL_VALIDATOR_CODES:
        return candidate
    for prefix in _EDITORIAL_VALIDATOR_CODE_PREFIXES:
        if candidate.startswith(f"{prefix}:"):
            return prefix
    return None


def _manual_table_validator_code(error: BaseException | None, *, fallback: str) -> str:
    """Return a stable internal code, never an exception message or user text."""
    candidates: list[object] = []
    if isinstance(error, EditorialEnrichmentRevisionValidationError):
        candidates.extend(
            value
            for rejection in error.rejections
            for value in (rejection.get("diagnostic_code"), rejection.get("reason_code"))
        )
    if error is not None:
        diagnostics = getattr(error, "diagnostics", None)
        if isinstance(diagnostics, Mapping):
            candidates.append(diagnostics.get("code"))
        candidates.extend(
            (
                getattr(error, "reason_code", None),
                getattr(error, "diagnostic_code", None),
                getattr(error, "validator_code", None),
                getattr(error, "code", None),
            )
        )
    candidates.append(fallback)
    for candidate in candidates:
        closed_code = _closed_editorial_validator_code(candidate)
        if closed_code is not None:
            return closed_code
    return _closed_editorial_validator_code(fallback) or "proposal_patch_invalid"


def _dropped_table_validator_code(warnings: tuple[str, ...]) -> str | None:
    prefix = "editorial_enrichment_validation_block_dropped:TABLE:"
    for warning in warnings:
        if not warning.startswith(prefix):
            continue
        candidate = warning.rsplit(":", 1)[-1]
        if re.fullmatch(r"[a-z][a-z0-9_]{2,}", candidate):
            return candidate
    return None


def _manual_table_failure_from_diagnostics(
    preparation: EditorialProposalPreparation,
    code: str,
    diagnostics: Mapping[str, object],
) -> EditorialProposalApplicationError:
    if (
        preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
        and preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
    ):
        raw_reason = diagnostics.get("reason")
        try:
            figure_reason = EditorialFigureDiagnosticReason(str(raw_reason))
        except ValueError:
            figure_reason = _manual_figure_error_reason(code)
        frame_value = diagnostics.get("frame")
        return _manual_figure_failure(
            code,
            figure_reason,
            frame=frame_value if isinstance(frame_value, str) else None,
            validator_code=_closed_editorial_validator_code(diagnostics.get("validator_code"))
            or _manual_table_validator_code(None, fallback=code),
        )
    if (
        preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
        and preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
    ):
        raw_reason = diagnostics.get("reason")
        try:
            bar_reason = EditorialBarChartDiagnosticReason(str(raw_reason))
        except ValueError:
            bar_reason = _manual_bar_chart_error_reason(code)
        validator_code = _closed_editorial_validator_code(diagnostics.get("validator_code"))
        frame_value = diagnostics.get("frame")
        return _manual_bar_chart_failure(
            code,
            bar_reason,
            frame=frame_value if isinstance(frame_value, str) else None,
            validator_code=validator_code or _manual_table_validator_code(None, fallback=code),
        )
    if (
        preparation.origin is not EditorialProposalOrigin.MANUAL_PATCH
        or preparation.operation is not TargetedEditorialRevisionOperation.CREATE_TABLE
    ):
        return EditorialProposalApplicationError(
            code,
            reason="validation:proposal_base_stale" if code == "proposal_base_stale" else None,
        )
    raw_reason = diagnostics.get("reason")
    try:
        table_reason = EditorialTableDiagnosticReason(str(raw_reason))
    except ValueError:
        table_reason = _manual_table_error_reason(code)
    if code == "proposal_base_stale":
        table_reason = EditorialTableDiagnosticReason.PIN_STALE
    table_frame_value = diagnostics.get("frame")
    table_validator_code = diagnostics.get("validator_code")
    return _manual_table_failure(
        code,
        table_reason,
        frame=table_frame_value if isinstance(table_frame_value, str) else None,
        validator_code=_closed_editorial_validator_code(table_validator_code),
    )


class _ProposalPatchBlobMissing(Exception):
    """Signal that the locked blob address must be repopulated outside the UOW."""


@dataclass(frozen=True, slots=True)
class EditorialProposalPreparation:
    """Server-resolved input captured for Bridge or manual proposal preparation."""

    subject_id: UUID
    production_run_id: UUID
    snapshot_id: UUID
    occurrence_id: UUID
    thread_id: UUID | None
    base_artifact_id: UUID
    base_artifact_version: int
    base_input_hash: str
    base_canonical_sha256: str
    base_pin_hash: str
    routing_decision_hash: str | None
    target_id: str
    target: TargetedEditorialRevisionTarget
    operation: TargetedEditorialRevisionOperation
    before_hash: str
    base_content: Mapping[str, Any]
    raw_response: str
    model_run_id: UUID | None
    model_turn_id: UUID | None
    actor_id: UUID
    idempotency_key: str
    request_sha256: str
    evidence_refs: tuple[dict[str, object], ...] = ()
    instruction: str = ""
    requester: str = "bridge"
    enrichment_context: Mapping[str, Any] | None = None
    strict_wire: bool = False
    paragraph_evidence_context: EditorialParagraphEvidenceContext | None = None
    origin: EditorialProposalOrigin = EditorialProposalOrigin.BRIDGE_DRAFT
    manual_patch: ManualPatch | None = None
    manual_figure_references: tuple[ManualSourceFigureArchiveReference, ...] = ()
    manual_figure_candidates: tuple[SourceFigureCandidateV1, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorialProposalRead:
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    thread_id: UUID | None
    origin: str
    state: str
    state_version: int
    operation: str
    base_artifact_id: UUID
    base_artifact_version: int
    base_canonical_sha256: str
    base_text: str | None
    patch_sha256: str | None
    patch: dict[str, object] | None
    diagnostics: dict[str, object]
    created_at: str


class EditorialProposalService:
    """Persist validated proposal state while keeping canonical artifacts read-only."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_store: BlobStore | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        workspace_service: Any | None = None,
        discussion_context_builder: Any | None = None,
        chart_compiler: Any | None = None,
        media_asset_store: MediaAssetStore | None = None,
        figure_catalog_service: Any | None = None,
        visual_revision_loader: VisualRevisionContextLoader | None = None,
        diagram_preview_compiler: Any | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service
        self._discussion_context_builder = discussion_context_builder
        self._chart_compiler = chart_compiler
        self._media_asset_store = media_asset_store
        self._figure_catalog_service = figure_catalog_service
        self._visual_revision_loader = visual_revision_loader
        self._diagram_preview_compiler = diagram_preview_compiler or getattr(
            visual_revision_loader, "diagram_preview_compiler", None
        )

    async def prepare_thread_turn(
        self,
        *,
        thread_id: UUID,
        actor_id: UUID,
        model_run_id: UUID,
        model_turn_id: UUID,
        instruction: str,
        raw_response: str,
        context: EditorialDiscussionContext,
    ) -> EditorialProposal:
        """Prepare one proposal from the verified output and exact thread turn."""
        if self._workspace_service is None or context.proposal_snapshot_id is None:
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        if not context.proposal_operations:
            raise EditorialProposalApplicationError("proposal_operation_unsupported")
        allowed_operations = set(context.proposal_operations)
        supported_operations = {
            TargetedEditorialRevisionOperation.REWRITE_TITLE.value,
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH.value,
            TargetedEditorialRevisionOperation.CREATE_TABLE.value,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
            *_VISUAL_PROPOSAL_OPERATION_VALUES,
        }
        if not allowed_operations <= supported_operations:
            raise EditorialProposalApplicationError("proposal_operation_unsupported")
        if (
            context.proposal_enrichment_context is not None
            and context.proposal_enrichment_context.get("mode") == "element"
        ):
            return await self._prepare_thread_element_revision(
                thread_id=thread_id,
                actor_id=actor_id,
                model_run_id=model_run_id,
                model_turn_id=model_turn_id,
                instruction=instruction,
                raw_response=raw_response,
                context=context,
            )
        fallback_operation = None
        for value in context.proposal_operations:
            try:
                fallback_operation = TargetedEditorialRevisionOperation(value)
            except ValueError:
                continue
            break
        if fallback_operation is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        try:
            parsed = parse_wire_patch_strict(raw_response)
        except TargetedSynthesisRevisionValidationError:
            parsed = None
        operation = (
            parsed.action
            if parsed is not None
            and parsed.refusal is None
            and parsed.action.value in allowed_operations
            else fallback_operation
        )
        # The thread origin supplies the subject and run scope; the workspace resolver
        # returns the exact current base that the proposal validator fences again.
        async with self._uow_factory() as uow:
            thread_record = await uow.editorial_threads.get(thread_id)
        if thread_record is None or thread_record.thread.origin.subject_id is None:
            raise EditorialProposalApplicationError("proposal_not_found")
        origin = thread_record.thread.origin
        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=origin.subject_id,
                snapshot_id=context.proposal_snapshot_id,
                block_id=origin.occurrence_id,
                operation=operation.value,
            )
        except EditorialReadOnlyError as error:
            raise EditorialProposalApplicationError(
                "editorial_read_only", reason=error.reason
            ) from error
        except Exception:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="workspace:resolve_revision_target"
            ) from None
        record = resolved.record
        table_context = context.proposal_enrichment_context
        is_enrichment_insertion = operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
        }
        if is_enrichment_insertion and (
            table_context is None
            or table_context.get("anchor_block_id") != str(origin.occurrence_id)
        ):
            raise EditorialProposalApplicationError("proposal_operation_unsupported")
        base_artifact_id = (
            record.enrichment_artifact_id
            if is_enrichment_insertion
            else record.synthesis_artifact_id
        )
        base_artifact_version = (
            record.enrichment_version if is_enrichment_insertion else record.synthesis_version
        )
        base_input_hash = (
            record.enrichment_input_hash if is_enrichment_insertion else record.synthesis_input_hash
        )
        base_sha256 = (
            record.enrichment_canonical_sha256
            if is_enrichment_insertion
            else record.synthesis_canonical_sha256
        )
        base_content = (
            table_context.get("base_content")
            if is_enrichment_insertion and table_context is not None
            else resolved.canonical_content
        )
        if (
            base_artifact_id is None
            or base_artifact_version is None
            or base_input_hash is None
            or base_sha256 is None
            or not isinstance(base_content, Mapping)
        ):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        request_identity = _thread_turn_request_identity(
            thread_id=thread_id,
            model_turn_id=model_turn_id,
            model_run_id=model_run_id,
            raw_response=raw_response,
            snapshot_id=context.proposal_snapshot_id,
            occurrence_id=origin.occurrence_id,
        )
        preparation = EditorialProposalPreparation(
            subject_id=origin.subject_id,
            production_run_id=origin.production_run_id,
            snapshot_id=context.proposal_snapshot_id,
            occurrence_id=origin.occurrence_id,
            thread_id=thread_id,
            base_artifact_id=base_artifact_id,
            base_artifact_version=base_artifact_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_sha256,
            base_pin_hash=record.workspace_pin_hash,
            routing_decision_hash=record.routing_decision_hash,
            target_id=resolved.target_id,
            target=resolved.target,
            operation=operation,
            before_hash=resolved.before_hash,
            base_content=base_content,
            raw_response=raw_response,
            model_run_id=model_run_id,
            model_turn_id=model_turn_id,
            actor_id=actor_id,
            idempotency_key=f"editorial-proposal-turn-{model_turn_id}",
            request_sha256=request_identity,
            instruction=instruction,
            strict_wire=True,
            paragraph_evidence_context=context.paragraph_evidence_context,
            enrichment_context=table_context if is_enrichment_insertion else None,
        )
        return await self.prepare_bridge_result(preparation)

    async def _prepare_thread_element_revision(
        self,
        *,
        thread_id: UUID,
        actor_id: UUID,
        model_run_id: UUID,
        model_turn_id: UUID,
        instruction: str,
        raw_response: str,
        context: EditorialDiscussionContext,
    ) -> EditorialProposal:
        """Prepare one Bridge visual correction through the shared planner.

        The registry occurrence supplies the element identity, the thread
        context supplies the exact pinned artifacts, and ``plan_visual_revision``
        is the only semantic validator. Refusals persist a closed code and an
        explicit thread event without any canonical write.
        """
        element_context = context.proposal_enrichment_context
        artifact_store = self._artifact_store
        if (
            self._workspace_service is None
            or artifact_store is None
            or context.proposal_snapshot_id is None
            or element_context is None
            or element_context.get("mode") != "element"
        ):
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        entry = element_context.get("entry")
        base_content = element_context.get("base_content")
        visual_context = element_context.get("visual_context")
        evidence_pack = element_context.get("evidence_pack")
        extraction = element_context.get("extraction")
        synthesis = element_context.get("synthesis")
        element_key = element_context.get("element_key")
        base_canonical_sha256 = element_context.get("base_canonical_sha256")
        target_path = element_context.get("target_path")
        base_artifact_id = element_context.get("base_artifact_id")
        base_artifact_version = element_context.get("base_artifact_version")
        base_input_hash = element_context.get("base_input_hash")
        base_pin_hash = element_context.get("base_pin_hash")
        routing_decision_hash = element_context.get("routing_decision_hash")
        try:
            element_kind = EditorialEnrichmentElementKind(str(element_context.get("element_kind")))
        except ValueError:
            raise EditorialProposalApplicationError("proposal_patch_invalid") from None
        if (
            not isinstance(entry, BlockRegistryEntry)
            or not isinstance(base_content, Mapping)
            or not isinstance(visual_context, VisualRevisionContext)
            or not isinstance(evidence_pack, EditorialEnrichmentEvidencePackV1)
            or not isinstance(extraction, ProductionExtractionV1)
            or not isinstance(synthesis, ProductionSynthesisV1)
            or not isinstance(element_key, str)
            or not element_key
            or not isinstance(base_canonical_sha256, str)
            or not isinstance(base_artifact_id, UUID)
            or not isinstance(base_artifact_version, int)
            or not isinstance(base_input_hash, str)
            or not isinstance(base_pin_hash, str)
            or (target_path is not None and not isinstance(target_path, str))
            or (routing_decision_hash is not None and not isinstance(routing_decision_hash, str))
        ):
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        async with self._uow_factory() as uow:
            thread_record = await uow.editorial_threads.get(thread_id)
        if thread_record is None or thread_record.thread.origin.subject_id is None:
            raise EditorialProposalApplicationError("proposal_not_found")
        origin = thread_record.thread.origin
        # The element branch persists a proposal and a thread event even when
        # its patch is later refused, so it must apply the same read-only policy
        # as the manual branch before any write. The text branch gets the same
        # refusal through ``resolve_revision_target``.
        await self._refuse_read_only_context(origin.subject_id, origin.occurrence_id)
        try:
            # Enum order makes the witness operation deterministic; every
            # offered visual operation resolves the same occurrence element.
            operation_value = next(
                operation.value
                for operation in TargetedEditorialRevisionOperation
                if operation.value in _VISUAL_PROPOSAL_OPERATION_VALUES
                and operation.value in set(context.proposal_operations)
            )
            resolved = resolve_enrichment_element_target(
                entry=entry,
                enrichment=base_content,
                operation=operation_value,
            )
        except (EditorialWorkspaceError, ValueError, StopIteration):
            raise EditorialProposalApplicationError("enrichment_element_not_found") from None
        parsed = _parse_thread_visual_revision(
            raw_response,
            allowed_operations=set(context.proposal_operations),
            element_kind=element_kind,
            element_key=element_key,
            before_hash=resolved.before_hash,
            target_path=target_path,
            base_canonical_sha256=base_canonical_sha256,
            evidence_pack=evidence_pack,
        )
        request_identity = _thread_turn_request_identity(
            thread_id=thread_id,
            model_turn_id=model_turn_id,
            model_run_id=model_run_id,
            raw_response=raw_response,
            snapshot_id=context.proposal_snapshot_id,
            occurrence_id=origin.occurrence_id,
        )
        target_id = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(resolved.target.to_json())
        ).hexdigest()
        preparation = EditorialProposalPreparation(
            subject_id=origin.subject_id,
            production_run_id=origin.production_run_id,
            snapshot_id=context.proposal_snapshot_id,
            occurrence_id=origin.occurrence_id,
            thread_id=thread_id,
            base_artifact_id=base_artifact_id,
            base_artifact_version=base_artifact_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_canonical_sha256,
            base_pin_hash=base_pin_hash,
            routing_decision_hash=routing_decision_hash,
            target_id=target_id,
            target=resolved.target,
            operation=parsed.operation,
            before_hash=resolved.before_hash,
            base_content=base_content,
            raw_response=raw_response,
            model_run_id=model_run_id,
            model_turn_id=model_turn_id,
            actor_id=actor_id,
            idempotency_key=f"editorial-proposal-turn-{model_turn_id}",
            request_sha256=request_identity,
            instruction=instruction,
            requester="bridge",
            origin=EditorialProposalOrigin.BRIDGE_DRAFT,
        )

        async def refuse(
            code: str, diagnostics: Mapping[str, object] | None = None
        ) -> EditorialProposal:
            body = ProductionArtifactStore.canonical_json_bytes(
                {"outcome": "refused", "code": code}
            )
            blob_id = await artifact_store.put_bytes(
                body,
                bucket="editorial-thread-events",
                mime_type="text/plain; charset=utf-8",
            )
            refusal_event = _ThreadRefusalEvent(
                blob_id=blob_id,
                payload_sha256=hashlib.sha256(body).hexdigest(),
                code=code,
            )
            return await self._persist_prepared_result(
                preparation,
                encoded_patch=None,
                descriptor=None,
                state=EditorialProposalState.REFUSED,
                kind=EditorialProposalEventKind.REFUSED,
                diagnostics={**dict(diagnostics or {}), "code": code},
                refusal_event=refusal_event,
            )

        if parsed.patch is None:
            return await refuse(parsed.refusal_code or "enrichment_revision_patch_invalid")
        try:
            plan = plan_visual_revision(visual_context, parsed.patch)
        except VisualRevisionRejected as error:
            return await refuse(error.code, error.diagnostics)
        compiled_chart_asset: dict[str, object] | None = None
        candidate_enrichment = plan.revised_enrichment
        if plan.compile_chart is not None:
            compiled_chart_asset, candidate_enrichment = await self._compile_revised_chart(
                chart=plan.compile_chart,
                element_key=plan.compile_chart.key,
                revised_enrichment=candidate_enrichment,
                production_run_id=origin.production_run_id,
            )
        diagram_preview: dict[str, object] | None = None
        if plan.materialize_diagram is not None:
            diagram_preview = await self._compile_diagram_preview(plan.materialize_diagram)
        patch_payload: dict[str, object] = {
            "schema_version": VISUAL_REVISION_PATCH_SCHEMA_VERSION,
            "operation": parsed.operation.value,
            "target": resolved.target.to_json(),
            "target_id": target_id,
            "occurrence_id": str(origin.occurrence_id),
            "before_hash": resolved.before_hash,
            "visual_patch": visual_patch_to_json(parsed.patch),
            "rationale": parsed.rationale,
        }
        if compiled_chart_asset is not None:
            patch_payload["compiled_chart_asset"] = compiled_chart_asset
        candidate_payload = editorial_enrichment_to_json(candidate_enrichment)
        preview_details = dict(plan.preview)
        if diagram_preview is not None:
            preview_details["diagram"] = diagram_preview
        if compiled_chart_asset is not None:
            preview_details["chart_asset"] = {
                "asset_id": compiled_chart_asset["asset_id"],
                "sha256": compiled_chart_asset["sha256"],
            }
        diagnostics: dict[str, object] = {
            "preview": preview_details,
            "canonical_candidate_sha256": hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(candidate_payload)
            ).hexdigest(),
        }
        encoded_patch = ProductionArtifactStore.canonical_json_bytes(patch_payload)
        if len(encoded_patch) > _MAX_PATCH_BYTES:
            raise EditorialProposalApplicationError("proposal_patch_too_large")
        descriptor = await self._upload_and_verify_patch(encoded_patch)
        try:
            while True:
                try:
                    return await self._persist_prepared_result(
                        preparation,
                        encoded_patch=encoded_patch,
                        descriptor=descriptor,
                        state=EditorialProposalState.PROPOSED,
                        kind=EditorialProposalEventKind.PREPARED,
                        diagnostics=diagnostics,
                    )
                except _ProposalPatchBlobMissing:
                    descriptor = await self._upload_and_verify_patch(encoded_patch)
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def prepare_bridge_result(
        self, preparation: EditorialProposalPreparation
    ) -> EditorialProposal:
        return await self._prepare_result(preparation, reject_invalid_manual=False)

    async def _refuse_read_only_context(self, subject_id: UUID, occurrence_id: UUID) -> None:
        """Refuse a manual write while the current snapshot policy is read-only.

        The reason is the one the workspace already derives from live pinned
        artifacts and exposes on every occurrence resolution; the discussion
        context rejects read-only blocks with a generic error, so consulting the
        policy first keeps the stable `editorial_read_only` code visible.
        """
        if self._workspace_service is None:
            return
        try:
            resolution = await self._workspace_service.resolve_occurrence(subject_id, occurrence_id)
        except EditorialWorkspaceError as error:
            raise EditorialProposalApplicationError(
                "proposal_base_unavailable", reason="workspace:resolve_occurrence"
            ) from error
        if resolution.read_only_reason is not None:
            raise EditorialProposalApplicationError(
                "editorial_read_only", reason=resolution.read_only_reason.value
            )

    async def prepare_visual_revision(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        patch: VisualRevisionPatch,
    ) -> EditorialProposal:
        """Prepare one typed visual revision without writing canonical artifacts.

        The shared planner is the only validation path; the proposal stores the
        reparsed patch plus its bounded preview, and adoption calls the same
        planner against the fenced current state.
        """
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        operation = patch.operation
        if self._workspace_service is None or self._visual_revision_loader is None:
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        preparation_key = _manual_idempotency_key("prepare", idempotency_key)
        request_sha256 = _visual_revision_request_sha256(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            patch=patch,
        )
        async with self._uow_factory() as uow:
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                subject_id, actor_id, preparation_key
            )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            return existing

        await self._refuse_read_only_context(subject_id, occurrence_id)
        try:
            loaded = await self._visual_revision_loader.load(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                occurrence_id=occurrence_id,
            )
        except VisualRevisionRejected as error:
            raise _visual_revision_failure(error) from None
        patch = _bind_visual_revision_base_placement(loaded.context.base_enrichment, patch)
        entry = loaded.entry
        base_payload = editorial_enrichment_to_json(loaded.context.base_enrichment)
        if isinstance(patch, ReorderEnrichmentElementsPatch):
            if (
                entry.source_stage is not EditorialSourceStage.SYNTHESIS
                or entry.kind
                not in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
                or patch.anchor_block_id != occurrence_id
            ):
                raise _visual_revision_failure(VisualRevisionRejected("editorial_anchor_stale", {}))
            try:
                resolved = resolve_enrichment_order_target(
                    entry=entry,
                    enrichment=base_payload,
                    operation=operation.value,
                )
            except (EditorialWorkspaceError, ValueError):
                raise _visual_revision_failure(
                    VisualRevisionRejected("editorial_anchor_stale", {})
                ) from None
            target = resolved.target
            before_hash = resolved.before_hash
        else:
            try:
                element_kind = visual_patch_element_kind(patch)
            except ValueError:
                raise _visual_revision_failure(
                    VisualRevisionRejected(
                        "enrichment_revision_patch_invalid",
                        {"variant": type(patch).__name__},
                    )
                ) from None
            if (
                entry.source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT
                or entry.kind.value != element_kind.value
                or entry.block_id != occurrence_id
            ):
                raise _visual_revision_failure(
                    VisualRevisionRejected(
                        "enrichment_element_kind_mismatch",
                        {"element_kind": element_kind.value},
                    )
                )
            try:
                resolved = resolve_enrichment_element_target(
                    entry=entry,
                    enrichment=base_payload,
                    operation=operation.value,
                )
            except (EditorialWorkspaceError, ValueError):
                raise _visual_revision_failure(
                    VisualRevisionRejected("enrichment_element_not_found", {})
                ) from None
            # The occurrence's registry pointer is the only authority for the
            # element identity; a same-family key from another occurrence would
            # make the review card and the adoption target diverge.
            if (
                resolved.target.element_kind is not element_kind
                or resolved.target.element_key != patch.element_key
            ):
                known_keys = _enrichment_element_keys(loaded.context.base_enrichment, element_kind)
                if patch.element_key not in known_keys:
                    # Backward-compatible closed code for a key that exists
                    # nowhere in the artifact.
                    raise _visual_revision_failure(
                        VisualRevisionRejected("enrichment_element_not_found", {})
                    )
                raise _visual_revision_failure(
                    VisualRevisionRejected(
                        "enrichment_target_mismatch",
                        {
                            "element_kind": element_kind.value,
                            "element_key": resolved.target.element_key,
                        },
                    )
                )
            target = resolved.target
            before_hash = resolved.before_hash
            if isinstance(patch, MoveEnrichmentElementPatch):
                target = replace(
                    target,
                    placement=TargetedEditorialRevisionPlacement(
                        kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
                        anchor_block_id=patch.anchor_block_id,
                    ),
                )
        target_id = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(target.to_json())
        ).hexdigest()
        try:
            plan = plan_visual_revision(loaded.context, patch)
        except VisualRevisionRejected as error:
            raise _visual_revision_failure(error) from None
        candidate_enrichment = plan.revised_enrichment
        compiled_chart_asset: dict[str, object] | None = None
        if plan.compile_chart is not None:
            compiled_chart_asset, candidate_enrichment = await self._compile_revised_chart(
                chart=plan.compile_chart,
                element_key=plan.compile_chart.key,
                revised_enrichment=candidate_enrichment,
                production_run_id=loaded.production_run_id,
            )
        diagram_preview: dict[str, object] | None = None
        if plan.materialize_diagram is not None:
            diagram_preview = await self._compile_diagram_preview(plan.materialize_diagram)
        patch_payload: dict[str, object] = {
            "schema_version": VISUAL_REVISION_PATCH_SCHEMA_VERSION,
            "operation": operation.value,
            "target": target.to_json(),
            "target_id": target_id,
            "occurrence_id": str(occurrence_id),
            "before_hash": before_hash,
            "visual_patch": visual_patch_to_json(patch),
            "rationale": "Révision visuelle manuelle validée par l’analyste.",  # noqa: RUF001
        }
        if compiled_chart_asset is not None:
            patch_payload["compiled_chart_asset"] = compiled_chart_asset
        candidate_payload = editorial_enrichment_to_json(candidate_enrichment)
        preview_details = dict(plan.preview)
        if diagram_preview is not None:
            preview_details["diagram"] = diagram_preview
        if compiled_chart_asset is not None:
            preview_details["chart_asset"] = {
                "asset_id": compiled_chart_asset["asset_id"],
                "sha256": compiled_chart_asset["sha256"],
            }
        diagnostics: dict[str, object] = {
            "preview": preview_details,
            "canonical_candidate_sha256": hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(candidate_payload)
            ).hexdigest(),
        }
        encoded_patch = ProductionArtifactStore.canonical_json_bytes(patch_payload)
        if len(encoded_patch) > _MAX_PATCH_BYTES:
            raise EditorialProposalApplicationError("proposal_patch_too_large")
        preparation = EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=loaded.production_run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=None,
            base_artifact_id=loaded.base_artifact.id,
            base_artifact_version=loaded.base_artifact.version,
            base_input_hash=loaded.base_artifact.input_hash,
            base_canonical_sha256=loaded.base_canonical_sha256,
            base_pin_hash=loaded.base_pin_hash,
            routing_decision_hash=loaded.routing_decision_hash,
            target_id=target_id,
            target=target,
            operation=operation,
            before_hash=before_hash,
            base_content=editorial_enrichment_to_json(loaded.context.base_enrichment),
            raw_response=encoded_patch.decode("utf-8"),
            model_run_id=None,
            model_turn_id=None,
            actor_id=actor_id,
            idempotency_key=preparation_key,
            request_sha256=request_sha256,
            instruction="Révision visuelle manuelle",
            requester=str(actor_id),
            origin=EditorialProposalOrigin.MANUAL_PATCH,
        )
        descriptor = await self._upload_and_verify_patch(encoded_patch)
        try:
            while True:
                try:
                    return await self._persist_prepared_result(
                        preparation,
                        encoded_patch=encoded_patch,
                        descriptor=descriptor,
                        state=EditorialProposalState.PROPOSED,
                        kind=EditorialProposalEventKind.PREPARED,
                        diagnostics=diagnostics,
                    )
                except _ProposalPatchBlobMissing:
                    descriptor = await self._upload_and_verify_patch(encoded_patch)
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def _compile_revised_chart(
        self,
        *,
        chart: ChartSpecV1,
        element_key: str,
        revised_enrichment: EditorialEnrichmentV1,
        production_run_id: UUID,
    ) -> tuple[dict[str, object], EditorialEnrichmentV1]:
        """Compile the preview SVG and return the element with its new asset id.

        The manifest is persisted with the proposal so adoption can recompile
        the same typed spec and prove byte equality under the run lock; the
        canonical artifact is untouched at prepare.
        """
        if self._chart_compiler is None or self._media_asset_store is None:
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        batch = await compile_and_store_charts(
            (chart,),
            compiler=self._chart_compiler,
            media_asset_store=self._media_asset_store,
            production_run_id=production_run_id,
        )
        if batch.rejections or len(batch.charts) != 1 or batch.charts[0].compiled_asset_id is None:
            raise EditorialProposalApplicationError(
                "editorial_enrichment_chart_render_failed",
                validator_code="bar_chart_local_compilation_failed",
            )
        compiled = batch.charts[0]
        asset_id = compiled.compiled_asset_id
        assert asset_id is not None
        manifest = await self._media_asset_store.get(asset_id)
        if manifest is None:
            raise EditorialProposalApplicationError(
                "editorial_enrichment_chart_render_failed",
                validator_code="bar_chart_media_manifest_missing",
            )
        revised = replace_element(
            revised_enrichment,
            EnrichmentElementRef(EditorialEnrichmentElementKind.CHART, element_key),
            compiled,
        )
        return (
            {
                "asset_id": str(manifest.asset_id),
                "sha256": manifest.sha256,
                "compiler_name": str(manifest.compiler_name),
                "compiler_version": str(manifest.compiler_version),
                "policy_version": manifest.policy_version,
            },
            revised,
        )

    async def _compile_diagram_preview(self, diagram: DiagramSpecV1) -> dict[str, object]:
        """Compile the non-canonical PNG preview of a typed revised diagram."""
        compiler = self._diagram_preview_compiler
        if compiler is None:
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        try:
            preview = await compiler.compile_preview(diagram)
        except Exception:
            raise EditorialProposalApplicationError(
                "editorial_enrichment_diagram_render_failed",
                validator_code="diagram_preview_compilation_failed",
            ) from None
        if preview.diagram_key != diagram.key:
            raise EditorialProposalApplicationError(
                "editorial_enrichment_diagram_render_failed",
                validator_code="diagram_preview_identity_mismatch",
            )
        return {
            "media_type": preview.media_type,
            "media_sha256": preview.media_sha256,
            "byte_size": len(preview.media_bytes),
            "source_sha256": preview.source_sha256,
            "compiler": preview.compiler,
            "compiler_version": preview.compiler_version,
            "compiler_policy_version": preview.compiler_policy_version,
        }

    async def prepare_manual_patch(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        patch: ManualPatch,
    ) -> EditorialProposal:
        """Persist a validated manual text patch without creating a model run."""
        typed_patch = patch.typed_patch
        is_manual_table = isinstance(typed_patch, ManualTablePatch)
        is_manual_bar_chart = isinstance(typed_patch, ManualBarChartPatch)
        is_manual_source_figure = isinstance(typed_patch, ManualSourceFigurePatch)
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            if is_manual_table:
                raise _manual_table_failure(
                    "idempotency_key_invalid",
                    EditorialTableDiagnosticReason.IDEMPOTENCY_KEY_INVALID,
                )
            if is_manual_bar_chart:
                raise _manual_bar_chart_failure(
                    "idempotency_key_invalid",
                    EditorialBarChartDiagnosticReason.REQUEST_INVALID,
                )
            if is_manual_source_figure:
                raise _manual_figure_failure(
                    "idempotency_key_invalid", EditorialFigureDiagnosticReason.REQUEST_INVALID
                )
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        if self._workspace_service is None:
            if is_manual_table:
                raise _manual_table_failure(
                    "proposal_workspace_unavailable",
                    EditorialTableDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
                )
            if is_manual_bar_chart:
                raise _manual_bar_chart_failure(
                    "proposal_workspace_unavailable",
                    EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
                )
            if is_manual_source_figure:
                raise _manual_figure_failure(
                    "proposal_workspace_unavailable",
                    EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
                )
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")

        if isinstance(typed_patch, ManualTablePatch):
            return await self._prepare_manual_table_patch(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                occurrence_id=occurrence_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                typed_patch=typed_patch,
            )
        if isinstance(typed_patch, ManualBarChartPatch):
            return await self._prepare_manual_bar_chart_patch(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                occurrence_id=occurrence_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                typed_patch=typed_patch,
            )
        if isinstance(typed_patch, ManualSourceFigurePatch):
            return await self._prepare_manual_source_figure_patch(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                occurrence_id=occurrence_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                typed_patch=typed_patch,
            )
        operation = typed_patch.operation
        received_text = typed_patch.text
        if len(received_text.encode("utf-8")) > _MAX_PATCH_BYTES:
            raise EditorialProposalApplicationError("manual_edit_request_too_large")
        after = received_text.strip()
        if (
            not after
            or _editorial_patch_text_violation(
                after,
                allow_newline_tab=(
                    operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
                ),
            )
            or re.search(r"(?m)^\s*@@.*@@\s*$", after)
        ):
            raise EditorialProposalApplicationError("manual_edit_patch_invalid")

        preparation_key = _manual_idempotency_key("prepare", idempotency_key)
        request_sha256 = _manual_request_sha256(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            operation=operation,
            text=received_text,
        )
        async with self._uow_factory() as uow:
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                subject_id, actor_id, preparation_key
            )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            return existing

        await self._refuse_read_only_context(subject_id, occurrence_id)
        paragraph_evidence_context = None
        if (
            operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
            and self._discussion_context_builder is None
        ):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        if (
            operation
            in {
                TargetedEditorialRevisionOperation.REWRITE_TITLE,
                TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
            }
            and self._discussion_context_builder is not None
        ):
            try:
                context = await self._discussion_context_builder.build_manual(
                    subject_id, occurrence_id
                )
            except Exception as error:
                raise EditorialProposalApplicationError("proposal_base_unavailable") from error
            if context.proposal_snapshot_id != snapshot_id:
                raise EditorialProposalApplicationError(
                    "proposal_base_stale", reason="context:proposal_snapshot_id"
                )
            paragraph_evidence_context = context.paragraph_evidence_context
            if operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH and (
                paragraph_evidence_context is None
            ):
                raise EditorialProposalApplicationError("proposal_base_unavailable")

        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                block_id=occurrence_id,
                operation=operation.value,
            )
        except EditorialWorkspaceError as error:
            read_only = _editorial_read_only_failure(error)
            if read_only is not None:
                raise read_only from error
            workspace_code = _editorial_workspace_error_code(error)
            code = (
                "proposal_base_stale"
                if workspace_code == "editorial_snapshot_stale"
                else workspace_code
                if workspace_code == "publication_kind_unresolved"
                else "proposal_target_mismatch"
            )
            reason = "workspace:snapshot_stale" if code == "proposal_base_stale" else None
            raise EditorialProposalApplicationError(code, reason=reason) from error
        entry = resolved.occurrence.entry
        if entry is None:
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        current_value = read_pointer(resolved.canonical_content, entry.structural_pointer)
        current_text = (
            current_value
            if isinstance(current_value, str)
            else (current_value.get("text") if isinstance(current_value, Mapping) else None)
        )
        if not isinstance(current_text, str):
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        if after == current_text.strip():
            raise EditorialProposalApplicationError("manual_edit_no_change")

        if operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
            if paragraph_evidence_context is None:
                raise EditorialProposalApplicationError("manual_edit_new_fact_unadmitted")
            after = _manual_paragraph_wire(
                after=after,
                target=resolved.target,
                target_indexes=resolve_target(
                    resolved.canonical_content, resolved.target, operation
                ),
                canonical_content=resolved.canonical_content,
                evidence_context=paragraph_evidence_context,
            )
        raw_response = _manual_wire_response(
            target_path=resolve_target(resolved.canonical_content, resolved.target, operation).path,
            operation=operation,
            base_sha256=resolved.record.synthesis_canonical_sha256,
            before_hash=resolved.before_hash,
            after=after,
        )
        preparation = EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=resolved.record.production_run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=None,
            base_artifact_id=resolved.record.synthesis_artifact_id,
            base_artifact_version=resolved.record.synthesis_version,
            base_input_hash=resolved.record.synthesis_input_hash,
            base_canonical_sha256=resolved.record.synthesis_canonical_sha256,
            base_pin_hash=resolved.record.workspace_pin_hash,
            routing_decision_hash=resolved.record.routing_decision_hash,
            target_id=resolved.target_id,
            target=resolved.target,
            operation=operation,
            before_hash=resolved.before_hash,
            base_content=resolved.canonical_content,
            raw_response=raw_response,
            model_run_id=None,
            model_turn_id=None,
            actor_id=actor_id,
            idempotency_key=preparation_key,
            request_sha256=request_sha256,
            strict_wire=True,
            paragraph_evidence_context=paragraph_evidence_context,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            manual_patch=ManualPatch(typed_patch=ManualTextPatch(operation, typed_patch.text)),
        )
        return await self._prepare_result(preparation, reject_invalid_manual=True)

    async def _prepare_manual_table_patch(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        typed_patch: ManualTablePatch,
    ) -> EditorialProposal:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise _manual_table_failure(
                "idempotency_key_invalid", EditorialTableDiagnosticReason.IDEMPOTENCY_KEY_INVALID
            )
        if (
            self._discussion_context_builder is None
            or self._artifact_store is None
            or self._workspace_service is None
        ):
            raise _manual_table_failure(
                "proposal_base_unavailable",
                EditorialTableDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
            )
        if occurrence_id != typed_patch.anchor_block_id:
            raise _manual_table_failure(
                "editorial_anchor_stale", EditorialTableDiagnosticReason.ANCHOR_MISMATCH
            )
        table = typed_patch.table
        if table.placement.anchor_block_id != occurrence_id:
            raise _manual_table_failure(
                "editorial_anchor_stale", EditorialTableDiagnosticReason.ANCHOR_MISMATCH
            )
        if table.purpose is None:
            raise _manual_table_failure(
                "manual_edit_new_fact_unadmitted",
                EditorialTableDiagnosticReason.EVIDENCE_UNADMITTED,
            )
        if _editorial_table_text_violation(table):
            raise _manual_table_failure(
                "manual_edit_patch_invalid", EditorialTableDiagnosticReason.MARKUP_REJECTED
            )
        if (
            len(ProductionArtifactStore.canonical_json_bytes(_manual_table_domain_json(table)))
            > _MAX_PATCH_BYTES
        ):
            raise _manual_table_failure(
                "manual_edit_request_too_large", EditorialTableDiagnosticReason.REQUEST_TOO_LARGE
            )

        preparation_key = _manual_idempotency_key("prepare", idempotency_key)
        request_sha256 = _manual_table_request_sha256(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            table=table,
        )
        async with self._uow_factory() as uow:
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                subject_id, actor_id, preparation_key
            )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            return existing

        await self._refuse_read_only_context(subject_id, occurrence_id)
        try:
            context = await self._discussion_context_builder.build_manual(subject_id, occurrence_id)
        except Exception as error:
            raise _manual_table_failure(
                "proposal_base_unavailable",
                EditorialTableDiagnosticReason.CONTEXT_UNAVAILABLE,
                frame=_exception_origin_frame(error),
            ) from None
        if (
            context.proposal_snapshot_id != snapshot_id
            or context.proposal_enrichment_context is None
            or "create_table" not in context.proposal_operations
        ):
            reason = (
                EditorialTableDiagnosticReason.SNAPSHOT_MISMATCH
                if context.proposal_snapshot_id != snapshot_id
                else EditorialTableDiagnosticReason.OPERATION_UNAVAILABLE
            )
            raise _manual_table_failure("proposal_operation_unsupported", reason)
        enrichment_context = context.proposal_enrichment_context
        base_content = enrichment_context.get("base_content")
        if not isinstance(base_content, Mapping):
            raise _manual_table_failure(
                "proposal_base_unavailable",
                EditorialTableDiagnosticReason.BASE_CONTENT_UNAVAILABLE,
            )
        if _manual_table_question_conflicts_with_base(table.purpose.question, base_content):
            raise _manual_table_failure(
                "manual_table_analytic_question_duplicate",
                EditorialTableDiagnosticReason.DUPLICATE_QUESTION,
            )
        evidence_pack = enrichment_context.get("evidence_pack")
        if evidence_pack is None:
            raise _manual_table_failure(
                "proposal_base_unavailable",
                EditorialTableDiagnosticReason.EVIDENCE_PACK_UNAVAILABLE,
            )
        handles_by_ref = {
            reference: handle
            for handle, reference in getattr(evidence_pack, "_handle_to_ref", {}).items()
        }
        try:
            table_handles, refs = _manual_table_evidence_handles(
                table,
                handles_by_ref=handles_by_ref,
            )
        except EditorialProposalApplicationError as error:
            raise _manual_table_failure(
                error.code,
                _manual_table_error_reason(error.code),
                frame=_exception_origin_frame(error),
            ) from error
        purpose_handles = [handles_by_ref[reference] for reference in table.purpose.evidence_refs]

        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                block_id=occurrence_id,
                operation=TargetedEditorialRevisionOperation.CREATE_TABLE.value,
            )
        except EditorialWorkspaceError as error:
            read_only = _editorial_read_only_failure(error)
            if read_only is not None:
                raise read_only from error
            raise _manual_table_failure(
                _editorial_workspace_error_code(error),
                EditorialTableDiagnosticReason.TARGET_UNAVAILABLE,
                frame=_exception_origin_frame(error),
            ) from error
        record = resolved.record
        anchor_entry = resolved.occurrence.entry
        if (
            anchor_entry is None
            or resolved.occurrence.binding_state != "valid"
            or anchor_entry.kind not in {"paragraph", "lead_paragraph"}
            or enrichment_context.get("anchor_block_id") != str(occurrence_id)
            or enrichment_context.get("target_path") != revision_target_path(anchor_entry)
            or enrichment_context.get("base_artifact_id") != str(record.enrichment_artifact_id)
            or enrichment_context.get("base_artifact_version") != record.enrichment_version
            or enrichment_context.get("base_input_hash") != record.enrichment_input_hash
            or enrichment_context.get("base_canonical_sha256") != record.enrichment_canonical_sha256
        ):
            raise _manual_table_failure(
                "editorial_anchor_stale", EditorialTableDiagnosticReason.ANCHOR_STALE
            )
        target_path = revision_target_path(anchor_entry)
        raw_table = _manual_table_proposal_json(
            table,
            row_handles=table_handles,
            purpose_handles=[str(handle) for handle in purpose_handles],
        )
        raw_patch: dict[str, object] = {
            "tables": [raw_table],
            "charts": [],
            "diagrams": [],
            "annotations": [],
            "figures": [],
            "resource_needs": [],
        }
        raw_response = _manual_wire_response(
            target_path=target_path,
            operation=TargetedEditorialRevisionOperation.CREATE_TABLE,
            base_sha256=record.enrichment_canonical_sha256 or "",
            before_hash=resolved.before_hash,
            after=json.dumps(raw_patch, ensure_ascii=True, separators=(",", ":")),
        )
        preparation = EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=record.production_run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=None,
            base_artifact_id=record.enrichment_artifact_id,
            base_artifact_version=record.enrichment_version,
            base_input_hash=record.enrichment_input_hash,
            base_canonical_sha256=record.enrichment_canonical_sha256,
            base_pin_hash=record.workspace_pin_hash,
            routing_decision_hash=record.routing_decision_hash,
            target_id=resolved.target_id,
            target=resolved.target,
            operation=TargetedEditorialRevisionOperation.CREATE_TABLE,
            before_hash=resolved.before_hash,
            base_content=base_content,
            raw_response=raw_response,
            model_run_id=None,
            model_turn_id=None,
            actor_id=actor_id,
            idempotency_key=preparation_key,
            request_sha256=request_sha256,
            evidence_refs=tuple(refs),
            instruction="Manual typed table insertion",
            requester=str(actor_id),
            enrichment_context=enrichment_context,
            strict_wire=True,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            manual_patch=ManualPatch(typed_patch=typed_patch),
        )
        return await self._prepare_result(preparation, reject_invalid_manual=True)

    async def _prepare_manual_source_figure_patch(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        typed_patch: ManualSourceFigurePatch,
    ) -> EditorialProposal:
        if (
            self._discussion_context_builder is None
            or self._artifact_store is None
            or self._workspace_service is None
            or self._figure_catalog_service is None
        ):
            raise _manual_figure_failure(
                "proposal_base_unavailable",
                EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
            )
        if occurrence_id != typed_patch.anchor_block_id:
            raise _manual_figure_failure(
                "editorial_anchor_stale", EditorialFigureDiagnosticReason.ANCHOR_STALE
            )
        if any(
            _editorial_caption_text_violation(selection.caption.strip())
            or not selection.caption.strip()
            for selection in typed_patch.figures
        ):
            raise _manual_figure_failure(
                "manual_edit_patch_invalid",
                EditorialFigureDiagnosticReason.MARKUP_REJECTED,
                validator_code="figure_caption_markup_forbidden",
            )
        try:
            items = await self._figure_catalog_service.resolve_many_for_insertion(
                subject_id=subject_id,
                figure_ids=tuple(selection.figure_id for selection in typed_patch.figures),
            )
            archive_references = tuple(archive_reference_for_catalog_item(item) for item in items)
        except EditorialFigureCatalogError as error:
            reason = _manual_figure_error_reason(error.code)
            raise _manual_figure_failure(
                error.code, reason, frame=_exception_origin_frame(error)
            ) from None

        preparation_key = _manual_idempotency_key("prepare", idempotency_key)
        request_sha256 = _manual_figure_request_sha256(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            figures=typed_patch.figures,
            references=archive_references,
        )
        async with self._uow_factory() as uow:
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                subject_id, actor_id, preparation_key
            )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise _manual_figure_failure(
                    "proposal_idempotency_conflict",
                    EditorialFigureDiagnosticReason.IDEMPOTENCY_CONFLICT,
                )
            return existing

        await self._refuse_read_only_context(subject_id, occurrence_id)
        try:
            context = await self._discussion_context_builder.build_manual(subject_id, occurrence_id)
        except Exception as error:
            raise _manual_figure_failure(
                "proposal_base_unavailable",
                EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
                frame=_exception_origin_frame(error),
            ) from None
        if (
            context.proposal_snapshot_id != snapshot_id
            or context.proposal_enrichment_context is None
            or TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value
            not in context.proposal_operations
        ):
            raise _manual_figure_failure(
                "proposal_operation_unsupported",
                EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
            )
        enrichment_context = context.proposal_enrichment_context
        base_content = enrichment_context.get("base_content")
        if not isinstance(base_content, Mapping):
            raise _manual_figure_failure(
                "proposal_base_unavailable",
                EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
            )
        try:
            base_enrichment = editorial_enrichment_from_json(base_content)
        except (TypeError, ValueError):
            raise _manual_figure_failure(
                "proposal_base_unavailable",
                EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE,
            ) from None
        existing_figure_ids = {
            item.resolved_figure.figure_id
            for item in base_enrichment.source_figures
            if item.resolved_figure is not None
        }
        if any(
            item._resolved_figure is not None
            and item._resolved_figure.figure_id in existing_figure_ids
            for item in items
        ):
            raise _manual_figure_failure(
                "editorial_figure_not_eligible", EditorialFigureDiagnosticReason.NOT_ELIGIBLE
            )
        integrated_count = sum(
            item.inclusion_status is SourceFigureInclusionStatus.INCLUDED
            for item in base_enrichment.source_figures
        )
        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                block_id=occurrence_id,
                operation=TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value,
            )
        except EditorialWorkspaceError as error:
            read_only = _editorial_read_only_failure(error)
            if read_only is not None:
                raise read_only from error
            raise _manual_figure_failure(
                _editorial_workspace_error_code(error),
                EditorialFigureDiagnosticReason.ANCHOR_STALE,
                frame=_exception_origin_frame(error),
            ) from error
        record = resolved.record
        anchor_entry = resolved.occurrence.entry
        if (
            record.publication_kind not in {PublicationKind.ARTICLE, PublicationKind.BRIEF}
            or anchor_entry is None
            or resolved.occurrence.binding_state != "valid"
            or anchor_entry.kind not in {"paragraph", "lead_paragraph"}
            or enrichment_context.get("anchor_block_id") != str(occurrence_id)
            or enrichment_context.get("target_path") != revision_target_path(anchor_entry)
            or enrichment_context.get("base_artifact_id") != str(record.enrichment_artifact_id)
            or enrichment_context.get("base_artifact_version") != record.enrichment_version
            or enrichment_context.get("base_input_hash") != record.enrichment_input_hash
            or enrichment_context.get("base_canonical_sha256") != record.enrichment_canonical_sha256
            or any(item._run_id != record.production_run_id for item in items)
        ):
            raise _manual_figure_failure(
                "editorial_anchor_stale", EditorialFigureDiagnosticReason.ANCHOR_STALE
            )
        try:
            inventory_limits = {item._inventory_max_figures for item in items}
            if len(inventory_limits) != 1:
                raise EditorialFigureCatalogError("figure_catalog_unavailable")
            validate_figure_insertion_limit(
                publication_kind=record.publication_kind,
                integrated_count=integrated_count,
                requested_count=len(items),
                inventory_limit=next(iter(inventory_limits)),
            )
        except EditorialFigureCatalogError as error:
            raise _manual_figure_failure(
                error.code,
                _manual_figure_error_reason(error.code),
                frame=_exception_origin_frame(error),
            ) from None

        placement = EnrichmentPlacementV1(
            kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=occurrence_id,
        )
        extraction_sources = {
            source.source_document_id: source
            for source in getattr(enrichment_context.get("extraction"), "sources", ())
        }
        candidates: list[SourceFigureCandidateV1] = []
        for selection, item, reference in zip(
            typed_patch.figures, items, archive_references, strict=True
        ):
            resolved_figure = item._resolved_figure
            if (
                resolved_figure is None
                or resolved_figure.decision is not SourceFigureDecision.ACCEPTED
            ):
                raise _manual_figure_failure(
                    "editorial_figure_not_eligible", EditorialFigureDiagnosticReason.NOT_ELIGIBLE
                )
            extraction_source = extraction_sources.get(resolved_figure.source_document_id)
            if extraction_source is None:
                raise _manual_figure_failure(
                    "editorial_figure_not_eligible", EditorialFigureDiagnosticReason.NOT_ELIGIBLE
                )
            if resolved_figure.source != extraction_source.canonical_url:
                raise _manual_figure_failure(
                    "editorial_figure_not_eligible", EditorialFigureDiagnosticReason.NOT_ELIGIBLE
                )
            candidates.append(
                SourceFigureCandidateV1(
                    key=adopted_source_figure_key(resolved_figure.figure_id),
                    source_document_id=resolved_figure.source_document_id,
                    source_url=extraction_source.canonical_url,
                    caption=selection.caption.strip(),
                    provenance=reference.provenance,
                    locator=resolved_figure.locator,
                    inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                    placement=placement,
                    resolved_figure=resolved_figure,
                    width=reference.width,
                    height=reference.height,
                    order=len(base_enrichment.source_figures) + selection.order,
                )
            )
        try:
            resolved_target = await self._workspace_service.resolve_revision_target(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                block_id=occurrence_id,
                operation=TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value,
            )
        except EditorialWorkspaceError as error:
            read_only = _editorial_read_only_failure(error)
            if read_only is not None:
                raise read_only from error
            raise _manual_figure_failure(
                _editorial_workspace_error_code(error),
                EditorialFigureDiagnosticReason.ANCHOR_STALE,
                frame=_exception_origin_frame(error),
            ) from error
        raw_response = _manual_wire_response(
            target_path=revision_target_path(anchor_entry),
            operation=TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
            base_sha256=record.enrichment_canonical_sha256 or "",
            before_hash=resolved_target.before_hash,
            after="{}",
        )
        preparation = EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=record.production_run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=None,
            base_artifact_id=record.enrichment_artifact_id,
            base_artifact_version=record.enrichment_version,
            base_input_hash=record.enrichment_input_hash,
            base_canonical_sha256=record.enrichment_canonical_sha256,
            base_pin_hash=record.workspace_pin_hash,
            routing_decision_hash=record.routing_decision_hash,
            target_id=resolved_target.target_id,
            target=resolved_target.target,
            operation=TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
            before_hash=resolved_target.before_hash,
            base_content=base_content,
            raw_response=raw_response,
            model_run_id=None,
            model_turn_id=None,
            actor_id=actor_id,
            idempotency_key=preparation_key,
            request_sha256=request_sha256,
            instruction="Manual archived source figure insertion",
            requester=str(actor_id),
            enrichment_context=enrichment_context,
            strict_wire=True,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            manual_patch=ManualPatch(typed_patch=typed_patch),
            manual_figure_references=archive_references,
            manual_figure_candidates=tuple(candidates),
        )
        return await self._prepare_result(preparation, reject_invalid_manual=True)

    async def _prepare_manual_bar_chart_patch(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        typed_patch: ManualBarChartPatch,
    ) -> EditorialProposal:
        chart = typed_patch.chart
        if (
            self._discussion_context_builder is None
            or self._artifact_store is None
            or self._workspace_service is None
        ):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        if occurrence_id != typed_patch.anchor_block_id:
            raise EditorialProposalApplicationError("editorial_anchor_stale")
        if _editorial_bar_chart_text_violation(chart):
            raise EditorialProposalApplicationError(
                "manual_edit_patch_invalid", validator_code="bar_chart_markup_forbidden"
            )
        if (
            len(ProductionArtifactStore.canonical_json_bytes(_manual_bar_chart_domain_json(chart)))
            > _MAX_PATCH_BYTES
        ):
            raise EditorialProposalApplicationError("manual_edit_request_too_large")

        preparation_key = _manual_idempotency_key("prepare", idempotency_key)
        request_sha256 = _manual_bar_chart_request_sha256(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            chart=chart,
        )
        async with self._uow_factory() as uow:
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                subject_id, actor_id, preparation_key
            )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            return existing

        await self._refuse_read_only_context(subject_id, occurrence_id)
        try:
            context = await self._discussion_context_builder.build_manual(subject_id, occurrence_id)
        except Exception as error:
            raise EditorialProposalApplicationError(
                "proposal_base_unavailable", frame=_exception_origin_frame(error)
            ) from None
        if (
            context.proposal_snapshot_id != snapshot_id
            or context.proposal_enrichment_context is None
            or "create_bar_chart" not in context.proposal_operations
        ):
            raise EditorialProposalApplicationError("proposal_operation_unsupported")
        enrichment_context = context.proposal_enrichment_context
        base_content = enrichment_context.get("base_content")
        if not isinstance(base_content, Mapping):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        if _manual_table_question_conflicts_with_base(chart.purpose.question, base_content):
            raise EditorialProposalApplicationError(
                "manual_bar_chart_analytic_question_duplicate",
                validator_code="bar_chart_question_duplicate",
            )
        evidence_pack = enrichment_context.get("evidence_pack")
        extraction = enrichment_context.get("extraction")
        if evidence_pack is None or extraction is None:
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        handles_by_ref = {
            reference: handle
            for handle, reference in getattr(evidence_pack, "_handle_to_ref", {}).items()
        }
        point_handles, refs = _manual_bar_chart_evidence_handles(
            chart, handles_by_ref=handles_by_ref
        )
        _validate_bar_chart_evidence(
            chart,
            evidence_entries=_all_evidence_entries(extraction),
            admitted_refs=frozenset(handles_by_ref),
        )
        purpose_handles = [handles_by_ref[reference] for reference in chart.purpose.evidence_refs]

        resolved = await self._workspace_service.resolve_revision_target(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
            block_id=occurrence_id,
            operation=TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
        )
        record = resolved.record
        anchor_entry = resolved.occurrence.entry
        if (
            anchor_entry is None
            or resolved.occurrence.binding_state != "valid"
            or anchor_entry.kind not in {"paragraph", "lead_paragraph"}
            or enrichment_context.get("anchor_block_id") != str(occurrence_id)
            or enrichment_context.get("target_path") != revision_target_path(anchor_entry)
            or enrichment_context.get("base_artifact_id") != str(record.enrichment_artifact_id)
            or enrichment_context.get("base_artifact_version") != record.enrichment_version
            or enrichment_context.get("base_input_hash") != record.enrichment_input_hash
            or enrichment_context.get("base_canonical_sha256") != record.enrichment_canonical_sha256
            or chart.placement.anchor_block_id != occurrence_id
        ):
            raise EditorialProposalApplicationError("editorial_anchor_stale")
        raw_chart = _manual_bar_chart_proposal_json(
            chart,
            point_handles=point_handles,
            purpose_handles=[str(handle) for handle in purpose_handles],
        )
        raw_patch: dict[str, object] = {
            "tables": [],
            "charts": [raw_chart],
            "diagrams": [],
            "annotations": [],
            "figures": [],
            "resource_needs": [],
        }
        raw_response = _manual_wire_response(
            target_path=revision_target_path(anchor_entry),
            operation=TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
            base_sha256=record.enrichment_canonical_sha256 or "",
            before_hash=resolved.before_hash,
            after=json.dumps(raw_patch, ensure_ascii=True, separators=(",", ":")),
        )
        preparation = EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=record.production_run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=None,
            base_artifact_id=record.enrichment_artifact_id,
            base_artifact_version=record.enrichment_version,
            base_input_hash=record.enrichment_input_hash,
            base_canonical_sha256=record.enrichment_canonical_sha256,
            base_pin_hash=record.workspace_pin_hash,
            routing_decision_hash=record.routing_decision_hash,
            target_id=resolved.target_id,
            target=resolved.target,
            operation=TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
            before_hash=resolved.before_hash,
            base_content=base_content,
            raw_response=raw_response,
            model_run_id=None,
            model_turn_id=None,
            actor_id=actor_id,
            idempotency_key=preparation_key,
            request_sha256=request_sha256,
            evidence_refs=tuple(refs),
            instruction="Manual typed evidence-backed bar chart insertion",
            requester=str(actor_id),
            enrichment_context=enrichment_context,
            strict_wire=True,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            manual_patch=ManualPatch(typed_patch=typed_patch),
        )
        return await self._prepare_result(preparation, reject_invalid_manual=True)

    async def _prepare_result(
        self,
        preparation: EditorialProposalPreparation,
        *,
        reject_invalid_manual: bool,
    ) -> EditorialProposal:
        if not _IDEMPOTENCY_KEY.fullmatch(preparation.idempotency_key):
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_TABLE
            ):
                raise _manual_table_failure(
                    "idempotency_key_invalid",
                    EditorialTableDiagnosticReason.IDEMPOTENCY_KEY_INVALID,
                )
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
            ):
                raise _manual_bar_chart_failure(
                    "idempotency_key_invalid",
                    EditorialBarChartDiagnosticReason.REQUEST_INVALID,
                )
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
            ):
                raise _manual_figure_failure(
                    "idempotency_key_invalid", EditorialFigureDiagnosticReason.REQUEST_INVALID
                )
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        try:
            preparation = await self._resolve_authoritative_preparation(preparation)
        except EditorialProposalApplicationError as error:
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_TABLE
            ):
                raise _manual_table_failure(
                    error.code,
                    _manual_table_error_reason(error.code),
                    frame=error.frame or _exception_origin_frame(error),
                ) from error
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
            ):
                raise _manual_bar_chart_failure(
                    error.code,
                    _manual_bar_chart_error_reason(error.code),
                    frame=error.frame or _exception_origin_frame(error),
                    validator_code=error.validator_code or error.code,
                ) from error
            if (
                preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
                and preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
            ):
                raise _manual_figure_failure(
                    error.code,
                    _manual_figure_error_reason(error.code),
                    frame=error.frame or _exception_origin_frame(error),
                    validator_code=error.validator_code or error.code,
                ) from error
            raise
        patch_payload, state, kind, diagnostics = await self._validate_result(preparation)
        if reject_invalid_manual and state is not EditorialProposalState.PROPOSED:
            code = str(diagnostics.get("code", "proposal_patch_invalid"))
            if code in {
                "synthesis_relation_not_evidenced",
                "synthesis_unknown_evidence",
                "synthesis_unknown_technical_value",
                "synthesis_unknown_date",
                "proposal_patch_requires_review",
                "proposal_evidence_not_admissible",
                "manual_edit_new_fact_unadmitted",
            }:
                raise _manual_table_failure_from_diagnostics(
                    preparation, "manual_edit_new_fact_unadmitted", diagnostics
                )
            if code == "proposal_base_stale":
                raise _manual_table_failure_from_diagnostics(
                    preparation, "proposal_base_stale", diagnostics
                )
            raise _manual_table_failure_from_diagnostics(
                preparation, "manual_edit_patch_invalid", diagnostics
            )
        evidence_refs = patch_payload.get("evidence_refs") if patch_payload is not None else None
        if isinstance(evidence_refs, list):
            preparation = replace(
                preparation,
                evidence_refs=tuple(
                    item
                    for item in evidence_refs
                    if isinstance(item, dict)
                    and all(
                        isinstance(item.get(key), str)
                        for key in (
                            "source_document_id",
                            "kind",
                            "evidence_key",
                        )
                    )
                ),
            )
        encoded_patch = (
            ProductionArtifactStore.canonical_json_bytes(patch_payload)
            if patch_payload is not None
            else None
        )
        if encoded_patch is not None and len(encoded_patch) > _MAX_PATCH_BYTES:
            encoded_patch = None
            state = EditorialProposalState.NEEDS_REVIEW
            kind = EditorialProposalEventKind.NEEDS_REVIEW
            diagnostics = {"code": "proposal_patch_too_large"}

        descriptor: BlobDescriptor | None = None
        if encoded_patch is not None:
            descriptor = await self._upload_and_verify_patch(encoded_patch)

        try:
            while True:
                try:
                    stored = await self._persist_prepared_result(
                        preparation,
                        encoded_patch=encoded_patch,
                        descriptor=descriptor,
                        state=state,
                        kind=kind,
                        diagnostics=diagnostics,
                    )
                except _ProposalPatchBlobMissing:
                    if encoded_patch is None:
                        raise EditorialProposalApplicationError(
                            "proposal_patch_payload_missing"
                        ) from None
                    # Cleanup may have won the address lock after the initial read-back.
                    # Restore and verify outside the relational UOW, then retry under lock.
                    descriptor = await self._upload_and_verify_patch(encoded_patch)
                    continue
                return stored
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def _resolve_authoritative_preparation(
        self, preparation: EditorialProposalPreparation
    ) -> EditorialProposalPreparation:
        if self._workspace_service is None:
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=preparation.subject_id,
                snapshot_id=preparation.snapshot_id,
                block_id=preparation.occurrence_id,
                operation=preparation.operation.value,
            )
        except EditorialWorkspaceError as error:
            read_only = _editorial_read_only_failure(error)
            if read_only is not None:
                raise read_only from error
            workspace_code = _editorial_workspace_error_code(error)
            code = (
                "proposal_base_stale"
                if workspace_code == "editorial_snapshot_stale"
                else "proposal_target_ambiguous"
                if workspace_code == "editorial_identity_ambiguous"
                else workspace_code
                if workspace_code == "publication_kind_unresolved"
                else "proposal_target_mismatch"
            )
            reason = "workspace:snapshot_stale" if code == "proposal_base_stale" else None
            raise EditorialProposalApplicationError(code, reason=reason) from error

        record = resolved.record
        is_enrichment_insertion = (
            preparation.operation
            in {
                TargetedEditorialRevisionOperation.CREATE_TABLE,
                TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
                TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
            }
            and preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT
        )
        if record.snapshot_id != preparation.snapshot_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:id_mismatch"
            )
        if record.subject_id != preparation.subject_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:subject_id"
            )
        if record.production_run_id != preparation.production_run_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:production_run_id"
            )
        if record.workspace_pin_hash != preparation.base_pin_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:workspace_pin_hash"
            )
        if record.routing_decision_hash != preparation.routing_decision_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="routing:decision_hash"
            )
        expected_artifact_id = (
            record.enrichment_artifact_id
            if is_enrichment_insertion
            else record.synthesis_artifact_id
        )
        expected_artifact_version = (
            record.enrichment_version if is_enrichment_insertion else record.synthesis_version
        )
        expected_input_hash = (
            record.enrichment_input_hash if is_enrichment_insertion else record.synthesis_input_hash
        )
        expected_sha256 = (
            record.enrichment_canonical_sha256
            if is_enrichment_insertion
            else record.synthesis_canonical_sha256
        )
        if expected_artifact_id != preparation.base_artifact_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_artifact_id"
                if is_enrichment_insertion
                else "pins:synthesis_artifact_id",
            )
        if expected_artifact_version != preparation.base_artifact_version:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_version"
                if is_enrichment_insertion
                else "pins:synthesis_version",
            )
        if expected_input_hash != preparation.base_input_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_input_hash"
                if is_enrichment_insertion
                else "pins:synthesis_input_hash",
            )
        if expected_sha256 != preparation.base_canonical_sha256:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_canonical_sha256"
                if is_enrichment_insertion
                else "pins:synthesis_canonical_sha256",
            )

        authoritative_content: Mapping[str, Any] = resolved.canonical_content
        if is_enrichment_insertion:
            if (
                preparation.target.placement is None
                or preparation.target.placement.anchor_block_id != preparation.occurrence_id
                or preparation.enrichment_context is None
                or preparation.enrichment_context.get("anchor_block_id")
                != str(preparation.occurrence_id)
                or resolved.occurrence.entry is None
                or resolved.occurrence.binding_state != "valid"
                or getattr(resolved.occurrence.entry.kind, "value", resolved.occurrence.entry.kind)
                not in {"paragraph", "lead_paragraph"}
                or resolved.occurrence.entry.content_sha256 != preparation.before_hash
                or resolved.occurrence.canonical_sha256 != record.synthesis_canonical_sha256
            ):
                raise EditorialProposalApplicationError("editorial_anchor_stale")
            if self._artifact_store is None or record.enrichment_artifact_id is None:
                raise EditorialProposalApplicationError("proposal_base_unavailable")
            async with self._uow_factory() as uow:
                enrichment_artifact = await uow.production_artifacts.get(
                    record.enrichment_artifact_id
                )
            if (
                enrichment_artifact is None
                or enrichment_artifact.production_run_id != preparation.production_run_id
                or enrichment_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
                or enrichment_artifact.version != record.enrichment_version
                or enrichment_artifact.input_hash != record.enrichment_input_hash
                or enrichment_artifact.status is not ProductionArtifactStatus.VERIFIED
                or enrichment_artifact.canonical_blob_id is None
            ):
                raise EditorialProposalApplicationError(
                    "proposal_base_stale", reason="pins:enrichment_artifact"
                )
            try:
                enrichment_content = await self._artifact_store.read_json(
                    enrichment_artifact.canonical_blob_id
                )
            except Exception:
                raise EditorialProposalApplicationError("proposal_base_unavailable") from None
            if not isinstance(enrichment_content, Mapping):
                raise EditorialProposalApplicationError("proposal_base_hash_mismatch")
            authoritative_content = enrichment_content

        supplied_base_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(dict(preparation.base_content))
        ).hexdigest()
        resolved_base_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(dict(authoritative_content))
        ).hexdigest()
        if supplied_base_sha256 != expected_sha256:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="base:supplied_sha256"
            )
        if resolved_base_sha256 != expected_sha256:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="base:resolved_sha256"
            )
        if (
            resolved.occurrence.entry is None
            or resolved.occurrence.binding_state != "valid"
            or resolved.occurrence.artifact_id != record.synthesis_artifact_id
            or resolved.occurrence.artifact_version != record.synthesis_version
            or resolved.occurrence.canonical_sha256 != record.synthesis_canonical_sha256
            or preparation.target != resolved.target
            or preparation.target_id != resolved.target_id
            or preparation.before_hash != resolved.before_hash
        ):
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        return replace(
            preparation,
            target=resolved.target,
            target_id=resolved.target_id,
            before_hash=resolved.before_hash,
            base_content=authoritative_content,
        )

    async def _upload_and_verify_patch(self, encoded_patch: bytes) -> BlobDescriptor:
        if self._blob_store is None:
            raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
        descriptor: BlobDescriptor | None = None
        try:
            for attempt in range(2):
                descriptor = await self._blob_store.put(
                    BytesIO(encoded_patch),
                    logical_bucket=_PATCH_BUCKET,
                    mime_type="application/vnd.autowork.editorial-patch+json",
                )
                try:
                    await self._verify_patch_blob(descriptor, encoded_patch)
                except FileNotFoundError:
                    if attempt == 0:
                        # A concurrent collector may delete the object between put and
                        # read-back. Re-upload once before opening any relational UOW.
                        continue
                    raise
                return descriptor
            raise EditorialProposalApplicationError("proposal_patch_upload_retry_exhausted")
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def _verify_patch_blob(self, descriptor: BlobDescriptor, encoded_patch: bytes) -> None:
        if self._blob_store is None:
            raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
        stored_bytes = await self._blob_store.read(descriptor, max_bytes=max(descriptor.size, 1))
        digest = hashlib.sha256(stored_bytes).hexdigest()
        if (
            stored_bytes != encoded_patch
            or digest != descriptor.sha256
            or len(stored_bytes) != descriptor.size
        ):
            raise BlobIntegrityError("Editorial proposal patch failed read-back verification")

    async def _persist_prepared_result(
        self,
        preparation: EditorialProposalPreparation,
        *,
        encoded_patch: bytes | None,
        descriptor: BlobDescriptor | None,
        state: EditorialProposalState,
        kind: EditorialProposalEventKind,
        diagnostics: dict[str, object],
        refusal_event: _ThreadRefusalEvent | None = None,
    ) -> EditorialProposal:
        async with self._uow_factory() as uow:
            await uow.editorial_proposals.lock_adoption_for_run(preparation.production_run_id)
            if descriptor is not None:
                await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
                if encoded_patch is None or self._blob_store is None:
                    raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
                try:
                    await self._verify_patch_blob(descriptor, encoded_patch)
                except FileNotFoundError:
                    # The UOW exits without registering a relational reference. The caller
                    # uploads and verifies again only after this transaction releases lock.
                    raise _ProposalPatchBlobMissing from None

            await self._assert_exact_origin(uow, preparation)
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                preparation.subject_id,
                preparation.actor_id,
                preparation.idempotency_key,
            )
            patch_sha256 = descriptor.sha256 if descriptor is not None else None
            if existing is not None:
                if not _replay_matches(existing, preparation, patch_sha256):
                    raise EditorialProposalConflict("proposal_idempotency_conflict")
                if existing.origin is EditorialProposalOrigin.BRIDGE_DRAFT:
                    await self._append_thread_reference(uow, existing)
                if (
                    refusal_event is not None
                    and existing.origin is EditorialProposalOrigin.BRIDGE_DRAFT
                    and existing.state is EditorialProposalState.REFUSED
                ):
                    await self._append_refusal_event(uow, existing, refusal_event)
                await uow.commit()
                return existing

            blob_record: BlobRecord | None = None
            if descriptor is not None:
                blob_record = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
                if (
                    blob_record.descriptor.size != descriptor.size
                    or blob_record.descriptor.mime_type != descriptor.mime_type
                ):
                    raise BlobIntegrityError("Editorial proposal blob metadata conflicts")

            proposal = EditorialProposal(
                id=uuid4(),
                subject_id=preparation.subject_id,
                production_run_id=preparation.production_run_id,
                origin=preparation.origin,
                snapshot_id=preparation.snapshot_id,
                occurrence_id=preparation.occurrence_id,
                base_artifact_id=preparation.base_artifact_id,
                base_artifact_version=preparation.base_artifact_version,
                base_input_hash=preparation.base_input_hash,
                base_canonical_sha256=preparation.base_canonical_sha256,
                base_routing_hash=preparation.routing_decision_hash,
                base_pin_hash=preparation.base_pin_hash,
                target_id=preparation.target_id,
                operation=preparation.operation.value,
                before_hash=preparation.before_hash,
                patch_blob_ref=blob_record.id if blob_record is not None else None,
                patch_sha256=patch_sha256,
                evidence_refs=preparation.evidence_refs,
                diagnostics=diagnostics,
                thread_id=preparation.thread_id,
                variant_group_id=variant_group_identity(
                    base_pin_hash=preparation.base_pin_hash,
                    occurrence_id=preparation.occurrence_id,
                    operation=preparation.operation.value,
                    thread_id=preparation.thread_id,
                ),
                model_run_id=preparation.model_run_id,
                model_turn_id=preparation.model_turn_id,
                state=state,
                state_version=1,
                actor_id=preparation.actor_id,
                idempotency_key=preparation.idempotency_key,
                request_sha256=preparation.request_sha256,
            )
            event = EditorialProposalEvent(
                id=uuid4(),
                proposal_id=proposal.id,
                sequence=1,
                kind=kind,
                state=state,
                state_version=1,
                actor_id=preparation.actor_id,
                idempotency_key=preparation.idempotency_key,
                request_sha256=preparation.request_sha256,
                patch_sha256=patch_sha256,
                diagnostics=diagnostics,
            )
            model_run = (
                EditorialProposalModelRun(
                    proposal_id=proposal.id,
                    thread_id=preparation.thread_id,
                    model_run_id=preparation.model_run_id,
                    model_turn_id=preparation.model_turn_id,
                )
                if preparation.origin is EditorialProposalOrigin.BRIDGE_DRAFT
                and preparation.thread_id is not None
                and preparation.model_run_id is not None
                and preparation.model_turn_id is not None
                else None
            )
            stored, inserted = await uow.editorial_proposals.add_if_absent(
                proposal, event=event, model_run=model_run
            )
            if not inserted and not _replay_matches(stored, preparation, patch_sha256):
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            if stored.origin is EditorialProposalOrigin.BRIDGE_DRAFT:
                await self._append_thread_reference(uow, stored)
            if (
                refusal_event is not None
                and stored.origin is EditorialProposalOrigin.BRIDGE_DRAFT
                and stored.state is EditorialProposalState.REFUSED
            ):
                await self._append_refusal_event(uow, stored, refusal_event)
            await uow.commit()
            return stored

    @staticmethod
    async def _append_refusal_event(
        uow: UnitOfWork,
        proposal: EditorialProposal,
        refusal_event: _ThreadRefusalEvent,
    ) -> None:
        """Expose the closed refusal code on the append-only thread timeline."""
        if proposal.thread_id is None or proposal.model_turn_id is None:
            raise EditorialProposalApplicationError("proposal_bridge_origin_invalid")
        await uow.editorial_threads.append_event(
            thread_id=proposal.thread_id,
            kind=EditorialThreadEventKind.REFUSED,
            actor_id=None,
            idempotency_key=f"proposal-refused:{proposal.id}",
            request_sha256=hashlib.sha256(
                f"editorial-proposal-refused:{proposal.id}:{refusal_event.code}".encode()
            ).hexdigest(),
            payload_blob_ref=refusal_event.blob_id,
            payload_sha256=refusal_event.payload_sha256,
            proposal_id=proposal.id,
            model_turn_id=proposal.model_turn_id,
        )

    @staticmethod
    async def _append_thread_reference(uow: UnitOfWork, proposal: EditorialProposal) -> None:
        """Make the persisted proposal discoverable from its append-only thread timeline."""
        if proposal.thread_id is None or proposal.model_turn_id is None:
            raise EditorialProposalApplicationError("proposal_bridge_origin_invalid")
        await uow.editorial_threads.append_event(
            thread_id=proposal.thread_id,
            kind=EditorialThreadEventKind.PROPOSAL,
            actor_id=proposal.actor_id,
            idempotency_key=f"proposal:{proposal.id}",
            request_sha256=proposal.request_sha256,
            proposal_id=proposal.id,
            model_turn_id=proposal.model_turn_id,
        )

    async def inspect(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        workspace_service: Any,
    ) -> EditorialProposalRead:
        try:
            resolution = await workspace_service.resolve_occurrence(subject_id, occurrence_id)
        except Exception:
            raise EditorialProposalApplicationError("proposal_not_found") from None
        if (
            resolution.entry is None
            or resolution.binding_state != "valid"
            or resolution.record.subject_id != subject_id
        ):
            raise EditorialProposalApplicationError("proposal_not_found")

        base_artifact = None
        base_snapshot = None
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, proposal_id
            )
            if (
                proposal is None
                or proposal.production_run_id != resolution.record.production_run_id
            ):
                raise EditorialProposalApplicationError("proposal_not_found")
            blob = (
                await uow.blobs.get(proposal.patch_blob_ref)
                if proposal.patch_blob_ref is not None
                else None
            )
            if proposal.patch_blob_ref is not None and blob is None:
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            if self._artifact_store is not None:
                artifact_repository = getattr(uow, "production_artifacts", None)
                snapshot_repository = getattr(uow, "editorial_workspace_snapshots", None)
                if artifact_repository is not None and snapshot_repository is not None:
                    base_artifact = await artifact_repository.get(proposal.base_artifact_id)
                    base_snapshot = await snapshot_repository.get_by_id(proposal.snapshot_id)

        patch = None
        if blob is not None:
            if (
                blob.descriptor.logical_bucket != _PATCH_BUCKET
                or blob.descriptor.mime_type != "application/vnd.autowork.editorial-patch+json"
                or blob.descriptor.size > _MAX_PATCH_BYTES
            ):
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            try:
                if self._blob_store is not None:
                    raw = await self._blob_store.read(
                        blob.descriptor, max_bytes=blob.descriptor.size
                    )
                elif self._artifact_store is not None:
                    raw = await self._artifact_store.read_bytes(
                        blob.id, max_bytes=blob.descriptor.size
                    )
                else:
                    raise EditorialProposalApplicationError("proposal_patch_unavailable")
            except Exception:
                raise EditorialProposalApplicationError("proposal_patch_unavailable") from None
            digest = hashlib.sha256(raw).hexdigest()
            if (
                len(raw) != blob.descriptor.size
                or digest != proposal.patch_sha256
                or digest != blob.descriptor.sha256
            ):
                raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise EditorialProposalApplicationError("proposal_patch_unavailable") from None
            if (
                not isinstance(decoded, dict)
                or decoded.get("schema_version")
                not in {"editorial-patch-v1", VISUAL_REVISION_PATCH_SCHEMA_VERSION}
                or decoded.get("operation") != proposal.operation
                or decoded.get("occurrence_id") != str(proposal.occurrence_id)
            ):
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            patch = decoded
        base_text = await self._read_base_text(proposal, base_artifact, base_snapshot)
        if proposal.state is EditorialProposalState.PROPOSED and not _proposal_pins_match(
            proposal, resolution
        ):
            proposal = await self._persist_stale_transition(proposal)
        return EditorialProposalRead(
            id=proposal.id,
            subject_id=proposal.subject_id,
            production_run_id=proposal.production_run_id,
            occurrence_id=proposal.occurrence_id,
            thread_id=proposal.thread_id,
            origin=proposal.origin.value,
            state=proposal.state.value,
            state_version=proposal.state_version,
            operation=proposal.operation,
            base_artifact_id=proposal.base_artifact_id,
            base_artifact_version=proposal.base_artifact_version,
            base_canonical_sha256=proposal.base_canonical_sha256,
            base_text=base_text,
            patch_sha256=proposal.patch_sha256,
            patch=patch,
            diagnostics=dict(proposal.diagnostics),
            created_at=proposal.created_at.isoformat(),
        )

    async def _read_base_text(
        self,
        proposal: EditorialProposal,
        artifact: Any | None,
        snapshot: Any | None,
    ) -> str | None:
        if self._artifact_store is None:
            return None
        if artifact is None or artifact.canonical_blob_id is None or snapshot is None:
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        try:
            canonical_content = await self._artifact_store.read_json(artifact.canonical_blob_id)
        except Exception:
            raise EditorialProposalApplicationError("proposal_base_unavailable") from None
        digest = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(canonical_content)
        ).hexdigest()
        if digest != proposal.base_canonical_sha256:
            raise EditorialProposalApplicationError("proposal_base_hash_mismatch")
        operation = TargetedEditorialRevisionOperation(proposal.operation)
        if (
            operation.paragraph_enrichment_element_kind is not None
            or operation is TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS
        ):
            if (
                snapshot.enrichment_artifact_id != proposal.base_artifact_id
                or snapshot.enrichment_version != proposal.base_artifact_version
                or snapshot.enrichment_input_hash != proposal.base_input_hash
                or snapshot.enrichment_canonical_sha256 != proposal.base_canonical_sha256
            ):
                raise EditorialProposalApplicationError("proposal_base_unavailable")
            return None
        occurrence = resolve_occurrence(snapshot, proposal.occurrence_id)
        if (
            occurrence.entry is None
            or occurrence.binding_state != "valid"
            or occurrence.artifact_id != proposal.base_artifact_id
            or occurrence.artifact_version != proposal.base_artifact_version
            or occurrence.canonical_sha256 != proposal.base_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        try:
            value = read_pointer(canonical_content, occurrence.entry.structural_pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            raise EditorialProposalApplicationError("proposal_base_unavailable") from None
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("text", "heading", "title"):
                text_value = value.get(key)
                if isinstance(text_value, str):
                    return text_value
        return None

    async def _persist_stale_transition(self, proposal: EditorialProposal) -> EditorialProposal:
        event = _stale_event(proposal)
        async with self._uow_factory() as uow:
            transitioned, changed = await uow.editorial_proposals.transition_if_current(
                subject_id=proposal.subject_id,
                occurrence_id=proposal.occurrence_id,
                proposal_id=proposal.id,
                expected_state=EditorialProposalState.PROPOSED,
                expected_state_version=proposal.state_version,
                state=EditorialProposalState.STALE,
                event=event,
            )
            if transitioned is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            if changed:
                await uow.commit()
            return transitioned

    async def reject(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        actor_id: UUID,
        expected_state_version: int,
        patch_sha256: str | None,
        idempotency_key: str,
        workspace_service: Any,
    ) -> EditorialProposal:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        if patch_sha256 is None or not _SHA256.fullmatch(patch_sha256):
            raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
        if expected_state_version < 1:
            raise EditorialProposalApplicationError("proposal_state_conflict")

        request_sha256 = _decision_request_sha256(
            proposal_id=proposal_id,
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            expected_state_version=expected_state_version,
            patch_sha256=patch_sha256,
        )
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, proposal_id
            )
            if proposal is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            existing_event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal_id, idempotency_key
            )
            if existing_event is not None:
                if _decision_event_matches(existing_event, actor_id, request_sha256):
                    return proposal
                raise EditorialProposalApplicationError("proposal_idempotency_conflict")

        inspected = await self.inspect(
            subject_id,
            occurrence_id,
            proposal_id,
            workspace_service=workspace_service,
        )
        if inspected.state != EditorialProposalState.PROPOSED.value:
            raise EditorialProposalApplicationError(
                "proposal_stale"
                if inspected.state == EditorialProposalState.STALE.value
                else "proposal_state_conflict"
            )
        if inspected.state_version != expected_state_version:
            raise EditorialProposalApplicationError("proposal_state_conflict")
        if inspected.patch_sha256 != patch_sha256:
            raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")

        stale = False
        async with self._uow_factory() as uow:
            await uow.editorial_proposals.lock_adoption_for_run(proposal.production_run_id)
            proposal = await uow.editorial_proposals.get_for_occurrence_for_update(
                subject_id, occurrence_id, proposal_id
            )
            if proposal is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            existing_event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal_id, idempotency_key
            )
            if existing_event is not None:
                if _decision_event_matches(existing_event, actor_id, request_sha256):
                    return proposal
                raise EditorialProposalApplicationError("proposal_idempotency_conflict")
            if proposal.patch_sha256 != patch_sha256:
                raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
            if proposal.state is EditorialProposalState.STALE:
                stale = True
            elif (
                proposal.state is not EditorialProposalState.PROPOSED
                or proposal.state_version != expected_state_version
            ):
                raise EditorialProposalApplicationError("proposal_state_conflict")

            if not stale:
                run = await uow.production_runs.get_for_update(proposal.production_run_id)
                resolution = (
                    await workspace_service.resolve_occurrence_in_uow(
                        uow, subject_id, occurrence_id
                    )
                    if run is not None
                    else None
                )
                pins_match = (
                    resolution is not None
                    and resolution.entry is not None
                    and resolution.binding_state == "valid"
                    and _proposal_pins_match(proposal, resolution)
                )
                if not pins_match:
                    stale_event = _stale_event(proposal)
                    stored_stale, changed = await uow.editorial_proposals.transition_if_current(
                        subject_id=subject_id,
                        occurrence_id=occurrence_id,
                        proposal_id=proposal_id,
                        expected_state=EditorialProposalState.PROPOSED,
                        expected_state_version=proposal.state_version,
                        state=EditorialProposalState.STALE,
                        event=stale_event,
                    )
                    if stored_stale is None:
                        raise EditorialProposalApplicationError("proposal_not_found")
                    if changed:
                        await uow.commit()
                    stale = True

            if not stale:
                rejected = proposal.transition(EditorialProposalState.REJECTED)
                event = EditorialProposalEvent(
                    id=uuid4(),
                    proposal_id=proposal.id,
                    sequence=rejected.state_version,
                    kind=EditorialProposalEventKind.STATE_CHANGED,
                    state=EditorialProposalState.REJECTED,
                    state_version=rejected.state_version,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    patch_sha256=patch_sha256,
                )
                stored, changed = await uow.editorial_proposals.transition_if_current(
                    subject_id=subject_id,
                    occurrence_id=occurrence_id,
                    proposal_id=proposal_id,
                    expected_state=EditorialProposalState.PROPOSED,
                    expected_state_version=expected_state_version,
                    state=EditorialProposalState.REJECTED,
                    event=event,
                )
                if stored is None:
                    raise EditorialProposalApplicationError("proposal_not_found")
                if not changed:
                    replay = await uow.editorial_proposals.get_event_by_idempotency(
                        proposal_id, idempotency_key
                    )
                    if replay is not None and _decision_event_matches(
                        replay, actor_id, request_sha256
                    ):
                        return stored
                    raise EditorialProposalApplicationError("proposal_state_conflict")
                if stored.thread_id is not None:
                    await uow.editorial_threads.append_event(
                        thread_id=stored.thread_id,
                        kind=EditorialThreadEventKind.REJECTED,
                        actor_id=actor_id,
                        idempotency_key=f"proposal-rejection:{idempotency_key}",
                        request_sha256=request_sha256,
                        proposal_id=stored.id,
                    )
                await uow.commit()
                return stored

        if stale:
            raise EditorialProposalApplicationError("proposal_stale")
        raise EditorialProposalApplicationError("proposal_state_conflict")

    async def cleanup_orphan_blob(self, descriptor: BlobDescriptor) -> bool:
        """Remove an unreferenced catalog row while retaining its object bytes."""
        async with self._uow_factory() as uow:
            await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
            blob = await uow.blobs.get_by_address(descriptor.logical_bucket, descriptor.sha256)
            if (
                blob is None
                or blob.descriptor.size != descriptor.size
                or blob.descriptor.mime_type != descriptor.mime_type
            ):
                return False
            if await uow.blobs.count_references(blob.id):
                return False
            await uow.blobs.delete(blob.id)
            await uow.commit()
        return True

    async def _best_effort_orphan_cleanup(self, descriptor: BlobDescriptor) -> None:
        try:
            await self.cleanup_orphan_blob(descriptor)
        except Exception:
            # The original persistence or verification failure remains authoritative.
            return

    async def _validate_result(
        self, preparation: EditorialProposalPreparation
    ) -> tuple[
        dict[str, object] | None,
        EditorialProposalState,
        EditorialProposalEventKind,
        dict[str, object],
    ]:
        try:
            patch = (
                parse_wire_patch_strict(preparation.raw_response)
                if preparation.strict_wire
                else parse_wire_patch(preparation.raw_response)
            )
        except TargetedSynthesisRevisionValidationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(preparation, error, code=error.reason_code),
            )
        if patch.refusal is not None:
            return (
                None,
                EditorialProposalState.REFUSED,
                EditorialProposalEventKind.REFUSED,
                {"code": patch.refusal.code.value},
            )
        typed_table_patch = (
            preparation.operation is TargetedEditorialRevisionOperation.CREATE_TABLE
            and preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT
            and preparation.target.element_kind is not None
            and preparation.target.element_kind.value == "table"
        )
        typed_bar_chart_patch = (
            preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
            and preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT
            and preparation.target.element_kind is EditorialEnrichmentElementKind.CHART
        )
        typed_source_figure_patch = (
            preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
            and preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT
            and preparation.target.element_kind is EditorialEnrichmentElementKind.FIGURE
        )
        if preparation.strict_wire and (typed_table_patch or typed_bar_chart_patch):
            try:
                typed_proposal = EditorialEnrichmentProposalV1.model_validate(
                    _parse_editorial_patch_payload(
                        patch.after, exact_chart_numbers=typed_bar_chart_patch
                    )
                )
            except (TypeError, ValueError):
                typed_proposal = None
            markup_violation = typed_proposal is not None and (
                len(typed_proposal.tables) == 1
                and _editorial_table_text_violation(typed_proposal.tables[0])
                if typed_table_patch
                else len(typed_proposal.charts) == 1
                and _editorial_bar_chart_text_violation(typed_proposal.charts[0])
            )
            if markup_violation:
                return (
                    None,
                    EditorialProposalState.NEEDS_REVIEW,
                    EditorialProposalEventKind.NEEDS_REVIEW,
                    _manual_table_validation_diagnostics(
                        preparation, None, code="revision_wire_markup_forbidden"
                    ),
                )
        strict_text_values = (
            (patch.rationale,)
            if (
                preparation.operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
                or typed_table_patch
                or typed_bar_chart_patch
                or typed_source_figure_patch
            )
            else (patch.after, patch.rationale)
        )
        if preparation.strict_wire and any(
            _editorial_patch_text_violation(value) for value in strict_text_values
        ):
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(
                    preparation, None, code="revision_wire_markup_forbidden"
                ),
            )
        try:
            if patch.action is not preparation.operation:
                raise EditorialProposalApplicationError("proposal_operation_unsupported")
            if (
                patch.base_artifact_hash != preparation.base_canonical_sha256
                or patch.before_hash != preparation.before_hash
            ):
                raise EditorialProposalApplicationError(
                    "proposal_base_stale", reason="patch:base_hash"
                )
            if preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
                return await self._validate_enrichment_result(preparation, patch)
            resolved = resolve_target(
                preparation.base_content, preparation.target, preparation.operation
            )
            if patch.target != resolved.path or resolved.before_hash != preparation.before_hash:
                raise EditorialProposalApplicationError("proposal_target_ambiguous")
            if preparation.operation is not TargetedEditorialRevisionOperation.REWRITE_TITLE:
                if preparation.operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
                    return self._validate_rewrite_paragraph_result(preparation, patch)
                raise EditorialProposalApplicationError("proposal_operation_unsupported")
            _validate_editorial_text_grounding(
                patch.after,
                admitted_evidence_texts=_admitted_evidence_texts(
                    preparation.paragraph_evidence_context
                ),
            )
            revised = dict(preparation.base_content)
            revised["title"] = patch.after
            assert_non_target_unchanged(
                preparation.base_content,
                revised,
                preparation.target,
                preparation.operation,
                resolved,
            )
            return (
                {
                    "schema_version": "editorial-patch-v1",
                    "operation": preparation.operation.value,
                    "target": preparation.target.to_json(),
                    "target_id": preparation.target_id,
                    "occurrence_id": str(preparation.occurrence_id),
                    "before_hash": resolved.before_hash,
                    "after": patch.after,
                    "rationale": patch.rationale,
                    "evidence_refs": _editorial_evidence_reference_values(
                        preparation.paragraph_evidence_context
                    ),
                },
                EditorialProposalState.PROPOSED,
                EditorialProposalEventKind.PREPARED,
                {},
            )
        except EditorialProposalApplicationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(preparation, error, code=error.code),
            )
        except TargetedEditorialRevisionConflictError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(
                    preparation, error, code="proposal_base_stale"
                ),
            )
        except TargetedSynthesisRevisionValidationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(preparation, error, code=error.reason_code),
            )
        except EditorialEnrichmentRevisionValidationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(
                    preparation, error, code="proposal_patch_invalid"
                ),
            )
        except (TypeError, ValueError) as error:
            code = getattr(error, "reason_code", "proposal_patch_invalid")
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                _manual_table_validation_diagnostics(preparation, error, code=str(code)),
            )

    def _validate_rewrite_paragraph_result(
        self, preparation: EditorialProposalPreparation, patch: Any
    ) -> tuple[
        dict[str, object] | None,
        EditorialProposalState,
        EditorialProposalEventKind,
        dict[str, object],
    ]:
        context = preparation.paragraph_evidence_context
        if (
            context is None
            or preparation.operation is not TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
            or preparation.target.kind
            not in {
                TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH,
                TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH,
            }
        ):
            raise EditorialProposalApplicationError("proposal_operation_unsupported")

        if (
            preparation.origin is EditorialProposalOrigin.MANUAL_PATCH
            and not context.evidence_handles
        ):
            _validate_editorial_text_grounding(
                patch.after,
                admitted_evidence_texts=(),
                allow_newline_tab=True,
            )
            base_payload = dict(preparation.base_content)
            synthesis = production_synthesis_from_json(base_payload)
            target_resolution = resolve_target(
                base_payload, preparation.target, preparation.operation
            )
            if preparation.target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
                target_index = target_resolution.indexes[0]
                paragraphs = list(synthesis.lead)
                paragraphs[target_index] = replace(
                    paragraphs[target_index], text=patch.after, evidence_refs=()
                )
                revised_synthesis = replace(synthesis, lead=tuple(paragraphs))
            else:
                assert target_resolution.parent_index is not None
                section_index = target_resolution.parent_index
                paragraph_index = target_resolution.indexes[0]
                sections = list(synthesis.sections)
                paragraphs = list(sections[section_index].paragraphs)
                paragraphs[paragraph_index] = replace(
                    paragraphs[paragraph_index], text=patch.after, evidence_refs=()
                )
                sections[section_index] = replace(
                    sections[section_index], paragraphs=tuple(paragraphs)
                )
                revised_synthesis = replace(synthesis, sections=tuple(sections))
            revised_payload = production_synthesis_to_json(revised_synthesis)
            return (
                {
                    "schema_version": "editorial-patch-v1",
                    "operation": preparation.operation.value,
                    "target": preparation.target.to_json(),
                    "target_id": preparation.target_id,
                    "occurrence_id": str(preparation.occurrence_id),
                    "before_hash": target_resolution.before_hash,
                    "after": patch.after,
                    "rationale": patch.rationale,
                    "evidence_refs": [],
                    "manual_text_without_evidence": True,
                },
                EditorialProposalState.PROPOSED,
                EditorialProposalEventKind.PREPARED,
                {
                    "canonical_candidate_sha256": hashlib.sha256(
                        ProductionArtifactStore.canonical_json_bytes(revised_payload)
                    ).hexdigest()
                },
            )

        parsed_after = parse_synthesis_proposal_wire(
            patch.after,
            require_lead=(
                preparation.target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH
            ),
        )
        if parsed_after.proposal is not None and parsed_after.error_code is None:
            claims = (
                *parsed_after.proposal.lead,
                *(claim for section in parsed_after.proposal.sections for claim in section.claims),
            )
            admitted_texts = _admitted_evidence_texts(context)
            for claim in claims:
                _validate_editorial_text_grounding(
                    claim.text,
                    admitted_evidence_texts=admitted_texts,
                    allow_newline_tab=True,
                )
            admitted_handles = set(context.evidence_handles)
            if any(
                handle not in admitted_handles
                for claim in claims
                for handle in claim.evidence_handles
            ):
                raise TargetedSynthesisRevisionValidationError("synthesis_unknown_evidence")

        base_payload = dict(preparation.base_content)
        base_synthesis = production_synthesis_from_json(base_payload)
        candidate = synthesis_candidate.build_candidate_from_validated_patch(
            base_artifact=context.base_artifact,
            base_payload=base_payload,
            base_synthesis=base_synthesis,
            base_canonical_sha256=preparation.base_canonical_sha256,
            patch=patch,
            target=preparation.target,
            operation=preparation.operation,
            evidence_pack=context.evidence_pack,
            extraction=context.extraction,
            snapshot=context.snapshot,
            apply_patch=_apply_wire_patch,
            publication_kind=context.publication_kind,
        )
        if candidate.before_hash != preparation.before_hash:
            raise TargetedEditorialRevisionConflictError("The targeted element changed")

        if preparation.target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
            paragraph = candidate.synthesis.lead[
                resolve_target(base_payload, preparation.target, preparation.operation).indexes[0]
            ]
        else:
            target_resolution = resolve_target(
                base_payload, preparation.target, preparation.operation
            )
            assert target_resolution.parent_index is not None
            paragraph = candidate.synthesis.sections[target_resolution.parent_index].paragraphs[
                target_resolution.indexes[0]
            ]
        return (
            {
                "schema_version": "editorial-patch-v1",
                "operation": preparation.operation.value,
                "target": preparation.target.to_json(),
                "target_id": preparation.target_id,
                "occurrence_id": str(preparation.occurrence_id),
                "before_hash": candidate.before_hash,
                "after": patch.after,
                "rationale": patch.rationale,
                "evidence_refs": [
                    {
                        "source_document_id": str(reference.source_document_id),
                        "kind": reference.kind.value,
                        "evidence_key": reference.evidence_key,
                    }
                    for reference in paragraph.evidence_refs
                ],
            },
            EditorialProposalState.PROPOSED,
            EditorialProposalEventKind.PREPARED,
            {},
        )

    async def _validate_enrichment_result(
        self, preparation: EditorialProposalPreparation, patch: Any
    ) -> tuple[
        dict[str, object] | None,
        EditorialProposalState,
        EditorialProposalEventKind,
        dict[str, object],
    ]:
        context = preparation.enrichment_context
        if context is None:
            raise EditorialProposalApplicationError("proposal_patch_requires_review")
        request = TargetedEditorialRevisionRequest(
            subject_id=preparation.subject_id,
            base_artifact_id=preparation.base_artifact_id,
            base_version=preparation.base_artifact_version,
            base_input_hash=preparation.base_input_hash,
            base_canonical_sha256=preparation.base_canonical_sha256,
            target=preparation.target,
            operation=preparation.operation,
            instruction=preparation.instruction,
            mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
            requester=preparation.requester,
            before_hash=preparation.before_hash,
        )
        validate_targeted_request(request)
        if preparation.target.element_kind is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        validate_targeted_action(
            preparation.target.element_kind, targeted_action(preparation.operation)
        )
        create_table = preparation.operation is TargetedEditorialRevisionOperation.CREATE_TABLE
        create_bar_chart = (
            preparation.operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
        )
        create_source_figure = (
            preparation.operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
        )
        raw = _parse_editorial_patch_payload(patch.after, exact_chart_numbers=create_bar_chart)

        if create_source_figure:
            target = preparation.target
            placement = target.placement
            candidates = preparation.manual_figure_candidates
            references = preparation.manual_figure_references
            typed_patch = (
                preparation.manual_patch.typed_patch
                if preparation.manual_patch is not None
                else None
            )
            selections = (
                typed_patch.figures if isinstance(typed_patch, ManualSourceFigurePatch) else ()
            )
            if (
                target.element_kind is not EditorialEnrichmentElementKind.FIGURE
                or target.element_key is not None
                or placement is None
                or placement.kind is not TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH
                or placement.anchor_block_id != preparation.occurrence_id
                or patch.target != context.get("target_path")
                or not candidates
                or len(candidates) != len(references)
                or len(candidates) != len(selections)
                or any(
                    candidate.placement.kind is not EnrichmentPlacementKind.AFTER_PARAGRAPH
                    or candidate.placement.anchor_block_id != preparation.occurrence_id
                    or candidate.resolved_figure is None
                    or selection.figure_id != reference.figure_id
                    or selection.order != position
                    or candidate.caption != selection.caption.strip()
                    or candidate.resolved_figure.figure_id != reference.source_figure_id
                    or candidate.resolved_figure.source_document_id != reference.source_document_id
                    or candidate.source_url != reference.source_url
                    or candidate.provenance != reference.provenance
                    or candidate.resolved_figure.sha256 != reference.sha256
                    or candidate.resolved_figure.mime_type != reference.mime_type
                    or candidate.resolved_figure.byte_size != reference.byte_size
                    or candidate.width != reference.width
                    or candidate.height != reference.height
                    for position, (candidate, reference, selection) in enumerate(
                        zip(candidates, references, selections, strict=True)
                    )
                )
            ):
                raise EditorialProposalApplicationError("proposal_target_mismatch")
            base_enrichment = editorial_enrichment_from_json(context["base_content"])
            if base_enrichment.schema_version < 8:
                raise EditorialProposalApplicationError("proposal_operation_unsupported")
            if base_enrichment.schema_version < 9:
                base_enrichment = replace(
                    base_enrichment,
                    schema_version=9,
                    enrichment_policy_version=EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
                )
            candidate_enrichment = replace(
                base_enrichment,
                tables=(),
                diagrams=(),
                charts=(),
                source_figures=candidates,
                figure_decisions=(),
                annotations=(),
            )
            validate_editorial_enrichment(
                candidate_enrichment,
                extraction=context["extraction"],
                synthesis=context["synthesis"],
            )
            return (
                {
                    "schema_version": "editorial-patch-v1",
                    "operation": preparation.operation.value,
                    "target": target.to_json(),
                    "target_id": preparation.target_id,
                    "occurrence_id": str(preparation.occurrence_id),
                    "before_hash": preparation.before_hash,
                    "enrichment_patch": {
                        "manual_source_figures": [
                            {
                                "figure_id": str(reference.figure_id),
                                "caption": candidate.caption,
                                "order": position,
                                "source_document_id": str(reference.source_document_id),
                                "source_url": candidate.source_url,
                                "sha256": reference.sha256,
                                "mime_type": reference.mime_type,
                                "byte_size": reference.byte_size,
                                "width": reference.width,
                                "height": reference.height,
                                "provenance": reference.provenance,
                            }
                            for position, (candidate, reference) in enumerate(
                                zip(candidates, references, strict=True)
                            )
                        ]
                    },
                    "rationale": "Manual archived source figure insertion",
                    "evidence_refs": [],
                },
                EditorialProposalState.PROPOSED,
                EditorialProposalEventKind.PREPARED,
                {},
            )

        def refuse_bar_chart_for_insufficient_data() -> tuple[
            None,
            EditorialProposalState,
            EditorialProposalEventKind,
            dict[str, object],
        ]:
            return (
                None,
                EditorialProposalState.REFUSED,
                EditorialProposalEventKind.REFUSED,
                {
                    "code": "bar_chart_insufficient_data",
                    "message": BAR_CHART_INSUFFICIENT_DATA_MESSAGE,
                },
            )

        if create_bar_chart:
            raw_charts = raw.get("charts") if isinstance(raw, dict) else None
            points = (
                raw_charts[0].get("points")
                if isinstance(raw_charts, list)
                and len(raw_charts) == 1
                and isinstance(raw_charts[0], dict)
                else None
            )
            admitted_handles = getattr(context.get("evidence_pack"), "_handle_to_ref", {})
            if not isinstance(points, list) or len(points) < 2:
                return refuse_bar_chart_for_insufficient_data()
            for point in points:
                if not isinstance(point, Mapping):
                    return refuse_bar_chart_for_insufficient_data()
                raw_value = point.get("value")
                try:
                    value = (
                        raw_value
                        if isinstance(raw_value, Decimal)
                        else (Decimal(raw_value) if isinstance(raw_value, str) else None)
                    )
                except InvalidOperation:
                    value = None
                handles = point.get("evidence_handles")
                if (
                    value is None
                    or not value.is_finite()
                    or not isinstance(handles, list)
                    or not handles
                    or any(
                        not isinstance(handle, str) or handle not in admitted_handles
                        for handle in handles
                    )
                ):
                    return refuse_bar_chart_for_insufficient_data()
                normalized_value = value.normalize() if value else Decimal(0)
                if (
                    len(normalized_value.as_tuple().digits) > MAX_BAR_CHART_SIGNIFICANT_DIGITS
                    or abs(normalized_value.adjusted()) > MAX_BAR_CHART_ADJUSTED_EXPONENT
                ):
                    return refuse_bar_chart_for_insufficient_data()
        table_key = None
        if create_table:
            tables = raw.get("tables") if isinstance(raw, dict) else None
            table_key = (
                tables[0].get("key")
                if isinstance(tables, list) and len(tables) == 1 and isinstance(tables[0], dict)
                else None
            )
            if (
                preparation.target.element_kind.value != "table"
                or preparation.target.element_key is not None
                or preparation.target.placement is None
                or preparation.target.placement.anchor_block_id != preparation.occurrence_id
                or patch.target != context.get("target_path")
                or not isinstance(table_key, str)
            ):
                raise EditorialProposalApplicationError("proposal_target_mismatch")
        chart_key = None
        if create_bar_chart:
            charts = raw.get("charts") if isinstance(raw, dict) else None
            chart_key = (
                charts[0].get("key")
                if isinstance(charts, list) and len(charts) == 1 and isinstance(charts[0], dict)
                else None
            )
            if (
                preparation.target.element_kind is not EditorialEnrichmentElementKind.CHART
                or preparation.target.element_key is not None
                or preparation.target.placement is None
                or preparation.target.placement.anchor_block_id != preparation.occurrence_id
                or patch.target != context.get("target_path")
                or not isinstance(chart_key, str)
            ):
                raise EditorialProposalApplicationError("proposal_target_mismatch")
        try:
            candidate = validate_editorial_enrichment_patch(
                raw,
                element_kind=preparation.target.element_kind,
                element_key=(
                    table_key
                    if create_table
                    else chart_key
                    if create_bar_chart
                    else preparation.target.element_key
                )
                or "new-element",
                evidence_pack=context["evidence_pack"],
                extraction=context["extraction"],
                synthesis=context["synthesis"],
                figure_catalog=tuple(context.get("figure_catalog", ())),
            )
        except EditorialEnrichmentRevisionValidationError as error:
            if create_bar_chart and any(
                rejection.get("diagnostic_code")
                in {"bar_chart_value_not_finite", "bar_chart_value_not_evidenced"}
                for rejection in error.rejections
            ):
                return refuse_bar_chart_for_insufficient_data()
            raise
        if create_table and (len(candidate.tables) != 1 or candidate.tables[0].key != table_key):
            raise EditorialProposalApplicationError(
                "proposal_patch_invalid",
                validator_code=(
                    _dropped_table_validator_code(candidate.warnings) or "proposal_patch_invalid"
                ),
            )
        if create_bar_chart and (
            len(candidate.charts) != 1
            or candidate.charts[0].kind is not ChartKind.BAR
            or candidate.charts[0].key != chart_key
        ):
            return refuse_bar_chart_for_insufficient_data()
        assert_requested_placement(
            candidate=candidate,
            element_kind=preparation.target.element_kind,
            targeted_request=request,
            synthesis=context["synthesis"],
            evidence_pack=context.get("evidence_pack")
            if create_table or create_bar_chart
            else None,
        )
        evidence_refs: list[dict[str, object]] = []
        if create_table:
            purpose = candidate.tables[0].purpose
            if purpose is None:
                raise EditorialProposalApplicationError("proposal_patch_invalid")
            for row in candidate.tables[0].rows:
                if not row.evidence_refs:
                    raise EditorialProposalApplicationError("manual_edit_new_fact_unadmitted")
                evidence_refs.extend(
                    _evidence_reference_json(reference) for reference in row.evidence_refs
                )
            evidence_refs.extend(
                _evidence_reference_json(reference) for reference in purpose.evidence_refs
            )
        compiled_chart_asset: dict[str, str] | None = None
        if create_bar_chart:
            chart = candidate.charts[0]
            if self._chart_compiler is None or self._media_asset_store is None:
                raise EditorialProposalApplicationError("proposal_workspace_unavailable")
            batch = await compile_and_store_charts(
                (chart,),
                compiler=self._chart_compiler,
                media_asset_store=self._media_asset_store,
                production_run_id=preparation.production_run_id,
            )
            if (
                batch.rejections
                or len(batch.charts) != 1
                or batch.charts[0].compiled_asset_id is None
            ):
                raise EditorialProposalApplicationError(
                    "editorial_enrichment_chart_render_failed",
                    validator_code="bar_chart_local_compilation_failed",
                )
            manifest = await self._media_asset_store.get(batch.charts[0].compiled_asset_id)
            if manifest is None:
                raise EditorialProposalApplicationError(
                    "editorial_enrichment_chart_render_failed",
                    validator_code="bar_chart_media_manifest_missing",
                )
            compiled_chart_asset = {
                "asset_id": str(manifest.asset_id),
                "sha256": manifest.sha256,
                "compiler_name": str(manifest.compiler_name),
                "compiler_version": str(manifest.compiler_version),
                "policy_version": manifest.policy_version,
            }
            evidence_refs.extend(
                _evidence_reference_json(reference)
                for point in chart.points
                for reference in point.evidence_refs
            )
            evidence_refs.extend(
                _evidence_reference_json(reference) for reference in chart.purpose.evidence_refs
            )
        return (
            {
                "schema_version": "editorial-patch-v1",
                "operation": preparation.operation.value,
                "target": preparation.target.to_json(),
                "target_id": preparation.target_id,
                "occurrence_id": str(preparation.occurrence_id),
                "before_hash": preparation.before_hash,
                "enrichment_patch": raw,
                **(
                    {"compiled_chart_asset": compiled_chart_asset}
                    if compiled_chart_asset is not None
                    else {}
                ),
                "rationale": patch.rationale,
                "evidence_refs": evidence_refs,
            },
            EditorialProposalState.PROPOSED,
            EditorialProposalEventKind.PREPARED,
            {},
        )

    async def _assert_exact_origin(
        self, uow: UnitOfWork, preparation: EditorialProposalPreparation
    ) -> None:
        is_enrichment_insertion = (
            preparation.operation
            in {
                TargetedEditorialRevisionOperation.CREATE_TABLE,
                TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
                TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
            }
            and preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT
        )
        is_enrichment_order = (
            preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER
        )
        is_enrichment_target = preparation.target.kind in {
            TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
            TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
        }
        occurrence_is_synthesis = is_enrichment_insertion or is_enrichment_order
        await uow.production_runs.lock_creation_for_subject(preparation.subject_id)
        snapshot = await uow.editorial_workspace_snapshots.get_by_id(preparation.snapshot_id)
        latest_snapshot = await uow.editorial_workspace_snapshots.latest_for_run(
            preparation.production_run_id
        )
        if snapshot is None or latest_snapshot is None:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:latest_missing"
            )
        if latest_snapshot.snapshot_id != preparation.snapshot_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:id_mismatch"
            )
        if snapshot.subject_id != preparation.subject_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:subject_id"
            )
        if snapshot.production_run_id != preparation.production_run_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:production_run_id"
            )
        if snapshot.workspace_pin_hash != preparation.base_pin_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:workspace_pin_hash"
            )
        if snapshot.routing_decision_hash != preparation.routing_decision_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="routing:decision_hash"
            )
        expected_artifact_id = (
            snapshot.enrichment_artifact_id
            if is_enrichment_target
            else snapshot.synthesis_artifact_id
        )
        expected_artifact_version = (
            snapshot.enrichment_version if is_enrichment_target else snapshot.synthesis_version
        )
        expected_input_hash = (
            snapshot.enrichment_input_hash
            if is_enrichment_target
            else snapshot.synthesis_input_hash
        )
        expected_sha256 = (
            snapshot.enrichment_canonical_sha256
            if is_enrichment_target
            else snapshot.synthesis_canonical_sha256
        )
        if expected_artifact_id != preparation.base_artifact_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_artifact_id"
                if is_enrichment_target
                else "pins:synthesis_artifact_id",
            )
        if expected_artifact_version != preparation.base_artifact_version:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_version"
                if is_enrichment_target
                else "pins:synthesis_version",
            )
        if expected_input_hash != preparation.base_input_hash:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_input_hash"
                if is_enrichment_target
                else "pins:synthesis_input_hash",
            )
        if expected_sha256 != preparation.base_canonical_sha256:
            raise EditorialProposalApplicationError(
                "proposal_base_stale",
                reason="pins:enrichment_canonical_sha256"
                if is_enrichment_target
                else "pins:synthesis_canonical_sha256",
            )
        resolution = resolve_occurrence(snapshot, preparation.occurrence_id)
        if resolution.entry is None or resolution.binding_state != "valid":
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="snapshot:occurrence_binding"
            )
        expected_occurrence_artifact_id = (
            snapshot.synthesis_artifact_id
            if occurrence_is_synthesis
            else preparation.base_artifact_id
        )
        expected_occurrence_version = (
            snapshot.synthesis_version
            if occurrence_is_synthesis
            else preparation.base_artifact_version
        )
        expected_occurrence_sha256 = (
            snapshot.synthesis_canonical_sha256
            if occurrence_is_synthesis
            else preparation.base_canonical_sha256
        )
        if resolution.artifact_id != expected_occurrence_artifact_id:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:artifact_id"
            )
        if resolution.artifact_version != expected_occurrence_version:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:artifact_version"
            )
        if resolution.canonical_sha256 != expected_occurrence_sha256:
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:canonical_sha256"
            )
        if is_enrichment_insertion and (
            resolution.entry.source_stage.value != "synthesis"
            or resolution.entry.content_sha256 != preparation.before_hash
            or preparation.target.placement is None
            or preparation.target.placement.anchor_block_id != preparation.occurrence_id
        ):
            raise EditorialProposalApplicationError("editorial_anchor_stale")
        if is_enrichment_order and (
            resolution.entry.source_stage.value != "synthesis"
            or resolution.entry.kind
            not in {
                EditorialBlockKind.PARAGRAPH,
                EditorialBlockKind.LEAD_PARAGRAPH,
            }
            or preparation.target.placement is None
            or preparation.target.placement.anchor_block_id != preparation.occurrence_id
        ):
            raise EditorialProposalApplicationError("editorial_anchor_stale")
        if (
            is_enrichment_target
            and not is_enrichment_insertion
            and not is_enrichment_order
            and (
                resolution.entry.source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT
                or resolution.entry.content_sha256 != preparation.before_hash
            )
        ):
            raise EditorialProposalApplicationError(
                "proposal_base_stale", reason="pins:before_hash"
            )
        if preparation.origin is EditorialProposalOrigin.MANUAL_PATCH:
            if (
                preparation.thread_id is not None
                or preparation.model_run_id is not None
                or preparation.model_turn_id is not None
                or (
                    preparation.manual_patch is None
                    and preparation.operation not in IMPLEMENTED_VISUAL_REVISION_OPERATIONS
                )
            ):
                raise EditorialProposalApplicationError("proposal_model_run_reference_invalid")
            return
        if (
            preparation.thread_id is None
            or preparation.model_run_id is None
            or preparation.model_turn_id is None
        ):
            raise EditorialProposalApplicationError("proposal_bridge_origin_invalid")
        thread_record = await uow.editorial_threads.get(preparation.thread_id)
        if thread_record is None:
            raise EditorialProposalApplicationError("proposal_not_found")
        thread = thread_record.thread
        origin = thread.origin
        if (
            origin.subject_id != preparation.subject_id
            or origin.production_run_id != preparation.production_run_id
            or origin.occurrence_id != preparation.occurrence_id
        ):
            raise EditorialProposalApplicationError("proposal_not_found")
        turn = await uow.model_conversation_turns.get(preparation.model_turn_id)
        model_run = await uow.model_runs.get(preparation.model_run_id)
        if (
            turn is None
            or model_run is None
            or turn.model_run_id != preparation.model_run_id
            or turn.objective != "editorial_proposal"
            or turn.conversation_id != thread.model_conversation_id
            or model_run.id != preparation.model_run_id
            or turn.status is not ConversationTurnStatus.SUCCEEDED
            or model_run.status is not ModelRunStatus.SUCCEEDED
            or turn.output_sha256
            != hashlib.sha256(preparation.raw_response.encode("utf-8")).hexdigest()
        ):
            raise EditorialProposalApplicationError("proposal_model_run_mismatch")


def _replay_matches(
    existing: EditorialProposal,
    preparation: EditorialProposalPreparation,
    patch_sha256: str | None,
) -> bool:
    return (
        existing.request_sha256 == preparation.request_sha256
        and existing.origin is preparation.origin
        and existing.patch_sha256 == patch_sha256
        and existing.production_run_id == preparation.production_run_id
        and existing.snapshot_id == preparation.snapshot_id
        and existing.occurrence_id == preparation.occurrence_id
        and existing.thread_id == preparation.thread_id
        and existing.base_artifact_id == preparation.base_artifact_id
        and existing.base_artifact_version == preparation.base_artifact_version
        and existing.base_input_hash == preparation.base_input_hash
        and existing.base_canonical_sha256 == preparation.base_canonical_sha256
        and existing.base_routing_hash == preparation.routing_decision_hash
        and existing.base_pin_hash == preparation.base_pin_hash
        and existing.target_id == preparation.target_id
        and existing.operation == preparation.operation.value
        and existing.before_hash == preparation.before_hash
        and existing.evidence_refs == preparation.evidence_refs
        and existing.model_run_id == preparation.model_run_id
        and existing.model_turn_id == preparation.model_turn_id
    )


def _proposal_pins_match(
    proposal: EditorialProposal, resolution: WorkspaceOccurrenceResolution
) -> bool:
    record = resolution.record
    if (
        resolution.entry is None
        or resolution.binding_state != "valid"
        or record.subject_id != proposal.subject_id
        or record.production_run_id != proposal.production_run_id
        or record.snapshot_id != proposal.snapshot_id
        or record.workspace_pin_hash != proposal.base_pin_hash
        or record.routing_decision_hash != proposal.base_routing_hash
    ):
        return False

    operation = TargetedEditorialRevisionOperation(proposal.operation)
    if operation.paragraph_enrichment_element_kind is not None:
        return (
            resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS
            and resolution.entry.kind
            in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
            and resolution.artifact_id == record.synthesis_artifact_id
            and resolution.artifact_version == record.synthesis_version
            and resolution.canonical_sha256 == record.synthesis_canonical_sha256
            and record.enrichment_artifact_id == proposal.base_artifact_id
            and record.enrichment_version == proposal.base_artifact_version
            and record.enrichment_input_hash == proposal.base_input_hash
            and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
        )

    if operation is TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS:
        return (
            resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS
            and resolution.entry.kind
            in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
            and resolution.artifact_id == record.synthesis_artifact_id
            and resolution.artifact_version == record.synthesis_version
            and resolution.canonical_sha256 == record.synthesis_canonical_sha256
            and record.enrichment_artifact_id == proposal.base_artifact_id
            and record.enrichment_version == proposal.base_artifact_version
            and record.enrichment_input_hash == proposal.base_input_hash
            and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
        )

    if resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS:
        return (
            resolution.artifact_id == proposal.base_artifact_id
            and resolution.artifact_version == proposal.base_artifact_version
            and resolution.canonical_sha256 == proposal.base_canonical_sha256
            and record.synthesis_artifact_id == proposal.base_artifact_id
            and record.synthesis_version == proposal.base_artifact_version
            and record.synthesis_input_hash == proposal.base_input_hash
            and record.synthesis_canonical_sha256 == proposal.base_canonical_sha256
        )

    return (
        resolution.entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
        and resolution.artifact_id == proposal.base_artifact_id
        and resolution.artifact_version == proposal.base_artifact_version
        and resolution.canonical_sha256 == proposal.base_canonical_sha256
        and record.enrichment_artifact_id == proposal.base_artifact_id
        and record.enrichment_version == proposal.base_artifact_version
        and record.enrichment_input_hash == proposal.base_input_hash
        and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
    )


def _stale_event(proposal: EditorialProposal) -> EditorialProposalEvent:
    return EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=proposal.state_version + 1,
        kind=EditorialProposalEventKind.STATE_CHANGED,
        state=EditorialProposalState.STALE,
        state_version=proposal.state_version + 1,
        actor_id=proposal.actor_id,
        idempotency_key=f"stale-{proposal.id.hex}-{proposal.state_version}",
        request_sha256=_state_request_sha256(
            action="stale",
            proposal_id=proposal.id,
            state_version=proposal.state_version,
            patch_sha256=proposal.patch_sha256,
        ),
        patch_sha256=proposal.patch_sha256,
        diagnostics={"code": "proposal_base_stale"},
    )


def _state_request_sha256(
    *,
    action: str,
    proposal_id: UUID,
    state_version: int,
    patch_sha256: str | None,
) -> str:
    request = {
        "action": action,
        "proposal_id": str(proposal_id),
        "state_version": state_version,
        "patch_sha256": patch_sha256,
    }
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(request)).hexdigest()


def _decision_request_sha256(
    *,
    proposal_id: UUID,
    subject_id: UUID,
    occurrence_id: UUID,
    actor_id: UUID,
    expected_state_version: int,
    patch_sha256: str,
) -> str:
    request = {
        "action": "reject",
        "proposal_id": str(proposal_id),
        "subject_id": str(subject_id),
        "occurrence_id": str(occurrence_id),
        "actor_id": str(actor_id),
        "expected_state_version": expected_state_version,
        "patch_sha256": patch_sha256,
    }
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(request)).hexdigest()


def _decision_event_matches(
    event: EditorialProposalEvent, actor_id: UUID, request_sha256: str
) -> bool:
    return (
        event.kind is EditorialProposalEventKind.STATE_CHANGED
        and event.state is EditorialProposalState.REJECTED
        and event.actor_id == actor_id
        and event.request_sha256 == request_sha256
    )
