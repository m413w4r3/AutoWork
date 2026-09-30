"""Canonical, structured Editorial Enrichment generation (AW-016)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
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

from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
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
from cti_app.domain.model_runs import ModelRunStatus
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
from cti_app.domain.production_references import ProductionReferenceTier
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

if TYPE_CHECKING:
    from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
    from cti_app.application.production_stages import EditorialEnrichmentService

EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "model-structured-v1"
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION = 1
EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION = "editorial-enrichment-evidence-pack-v1"
EDITORIAL_ENRICHMENT_PROMPT_VERSION = "editorial-enrichment-draft-v2"
EDITORIAL_ENRICHMENT_VALIDATOR_VERSION = "editorial-enrichment-validator-v2"
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


class EditorialEnrichmentProposalV1(_StrictEnrichmentProposalModel):
    tables: tuple[TableProposalV1, ...] = ()
    diagrams: tuple[DiagramProposalV1, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentEvidencePackV1:
    """Stable renderer-free prompt projection with a private exact ref map."""

    publication_language: str
    current_synthesis: Mapping[str, Any]
    narrative_evidence: tuple[Mapping[str, Any], ...]
    technical_evidence: tuple[Mapping[str, Any], ...]
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


def canonical_synthesis_hash(synthesis: ProductionSynthesisV1) -> str:
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis))
    ).hexdigest()


def _prompt_evidence_record(
    handle: str, kind: EvidenceKind, payload: Mapping[str, Any]
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
    else:
        values = {
            "type": payload["rule_type"],
            "name": payload["name"],
            "sha256": payload["sha256"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    return {"handle": handle, "kind": kind.value, **values}


def _synthesis_prompt_projection(
    synthesis: ProductionSynthesisV1,
    handle_for_ref: Mapping[ExtractionEvidenceRefV1, str],
) -> dict[str, Any]:
    def paragraph(item: Any) -> dict[str, Any]:
        return {
            "text": item.text,
            "evidence_handles": [handle_for_ref[ref] for ref in item.evidence_refs],
        }

    return {
        "language": synthesis.publication_language,
        "title": synthesis.title,
        "lead": [paragraph(item) for item in synthesis.lead],
        "sections": [
            {
                "section_index": index,
                "kind": section.kind.value,
                "heading": section.heading,
                "paragraphs": [paragraph(item) for item in section.paragraphs],
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

    entries = _all_evidence_entries(extraction)
    source_by_id = {source.source_document_id: source for source in extraction.sources}
    narrative_refs = {
        ref
        for ref in entries
        if ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
        and source_by_id[ref.source_document_id].tier is ProductionReferenceTier.CORE
        and source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
    }
    technical_candidates = sorted(
        (
            ref
            for ref, payload in entries.items()
            if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
            and (
                str(payload.get("context") or "").strip()
                or str(payload.get("evidence_quote") or "").strip()
            )
        ),
        key=evidence_ref_sort_key,
    )[:MAX_EDITORIAL_ENRICHMENT_TECHNICAL_EVIDENCE]
    included_refs = (
        narrative_refs | set(technical_candidates) | set(synthesis_evidence_refs(synthesis))
    )
    ordered_refs = tuple(sorted(included_refs, key=evidence_ref_sort_key))
    handle_to_ref = {f"E{index:03d}": ref for index, ref in enumerate(ordered_refs, start=1)}
    handle_for_ref = {ref: handle for handle, ref in handle_to_ref.items()}
    narrative_evidence = tuple(
        _prompt_evidence_record(handle_for_ref[ref], ref.kind, entries[ref])
        for ref in ordered_refs
        if ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
    )
    technical_evidence = tuple(
        _prompt_evidence_record(handle_for_ref[ref], ref.kind, entries[ref])
        for ref in ordered_refs
        if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
    )
    return EditorialEnrichmentEvidencePackV1(
        publication_language=synthesis.publication_language,
        current_synthesis=MappingProxyType(_synthesis_prompt_projection(synthesis, handle_for_ref)),
        narrative_evidence=tuple(MappingProxyType(record) for record in narrative_evidence),
        technical_evidence=tuple(MappingProxyType(record) for record in technical_evidence),
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
            }
        )
    ).hexdigest()


def editorial_enrichment_model_run_id(run: ProductionRun, input_hash: str) -> UUID:
    if not isinstance(run, ProductionRun) or _SHA256_RE.fullmatch(input_hash) is None:
        raise ValueError("Editorial enrichment ModelRun identity inputs are invalid")
    identity = ":".join(
        (
            "production-editorial-enrichment-model-run-v1",
            str(run.id),
            str(run.pipeline_generation),
            input_hash,
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def editorial_enrichment_output_contract_example() -> dict[str, Any]:
    """Shape example injected in the prompt; it must satisfy the enforced contract."""
    return {
        "tables": [
            {
                "key": "commands",
                "kind": "commands",
                "title": "Commands observed",
                "caption": None,
                "columns": [
                    {"key": "command", "label": "Command"},
                    {"key": "purpose", "label": "Purpose"},
                ],
                "rows": [{"cells": ["...", "..."], "evidence_handles": ["E001"]}],
                "placement": {"kind": "after_lead", "section_index": None},
            }
        ],
        "diagrams": [
            {
                "key": "infection_chain",
                "kind": "infection_chain",
                "title": "Infection chain",
                "caption": None,
                "direction": "left_to_right",
                "nodes": [
                    {
                        "node_id": "step_1",
                        "label": "Observed initial step",
                        "evidence_handles": ["E001"],
                    },
                    {
                        "node_id": "step_2",
                        "label": "Observed following step",
                        "evidence_handles": ["E002"],
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "step_1",
                        "target_node_id": "step_2",
                        "label": "leads to",
                        "evidence_handles": ["E003"],
                    }
                ],
                "groups": [],
                "placement": {"kind": "after_section", "section_index": 0},
            }
        ],
    }


def build_editorial_enrichment_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    access_policy: SynthesisAccessPolicyV1,
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
    )
    prompt_payload = {
        "instructions": (
            "Tu es un planificateur de représentations éditoriales, pas un chercheur "
            "ni un renderer. Décide uniquement si des informations de la synthèse "
            "gagneraient à être structurées en "
            "tableaux ou diagrammes sémantiques. N'ajoute aucun fait, n'effectue aucune recherche, "
            "et utilise uniquement les preuves fournies. Chaque ligne, nœud et arête doit citer au "
            "moins un evidence handle exact. Une arête exprime une relation factuelle "
            "et doit avoir "
            "sa propre preuve. Si aucune représentation n'améliore la compréhension, renvoie des "
            "tableaux et diagrammes vides. Ne génère ni Markdown, HTML, Mermaid, D2, DOT, TikZ, "
            "Typst, SVG, image source, ni corps de règle. Titres et captions restent descriptifs."
        ),
        "publication_language": evidence_pack.publication_language,
        "current_synthesis": dict(evidence_pack.current_synthesis),
        "current_evidence_pack": {
            "schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
            "policy_version": evidence_pack.policy_version,
            "narrative_evidence": [dict(record) for record in evidence_pack.narrative_evidence],
            "technical_evidence": [dict(record) for record in evidence_pack.technical_evidence],
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
    }
    prompt = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(str(record.source_document_id) in prompt for record in access_policy.sources):
        raise ValueError("Source document identities cannot appear in Editorial Enrichment prompt")
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
        run_id=editorial_enrichment_model_run_id(run, input_hash),
        allow_failed_resubmit=True,
        metadata={
            "editorial_enrichment_input_hash": input_hash,
            "access_policy_hash": access_hash,
            "effective_tlp": access_policy.effective_tlp.value,
            "external_llm_allowed": access_policy.external_llm_allowed,
            "do_not_submit": access_policy.do_not_submit,
            "evidence_pack_hash": pack_hash,
            "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            "routing_policy_version": EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
            "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
        },
        parameters={
            "evidence_pack_schema_version": EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SCHEMA_VERSION,
            "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
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
        nodes: list[DiagramNodeV1] = []
        edges: list[DiagramEdgeV1] = []
        groups: list[DiagramGroupV1] = []
        diagram_refs: set[ExtractionEvidenceRefV1] = set()
        for node in diagram.nodes:
            refs = _all_refs_for_handles(node.evidence_handles, evidence_pack)
            diagram_refs.update(refs)
            _validate_grounded_editorial_text(node.label, refs, entries, technical_support)
            nodes.append(DiagramNodeV1(node_id=node.node_id, label=node.label, evidence_refs=refs))
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
        try:
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
            source_figures=(),
            warnings=(),
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
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._model_gateway = model_gateway
        self._editorial_enrichment_service = editorial_enrichment_service
        self._artifact_reuse = artifact_reuse

    async def execute(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
    ) -> ProductionEditorialEnrichmentExecution:
        try:
            extraction = await self._load_extraction(run, snapshot, extraction_artifact)
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
            evidence_pack = build_editorial_enrichment_evidence_pack(
                snapshot, extraction, synthesis
            )
            evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
            access_policy_hash = synthesis_access_policy_hash(access_policy)
            input_hash = compute_editorial_enrichment_input_hash(
                extraction=extraction,
                synthesis=synthesis,
                evidence_pack_hash=evidence_pack_hash,
                access_policy_hash=access_policy_hash,
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
        )
        model_run_id = editorial_enrichment_model_run_id(run, input_hash)
        if (
            request.metadata.get("editorial_enrichment_input_hash") != input_hash
            or request.run_id != model_run_id
        ):
            raise ValueError("Editorial Enrichment request identity is inconsistent")
        try:
            execution = await self._model_gateway.draft(
                request, output_schema=EditorialEnrichmentProposalV1
            )
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
        if not isinstance(execution.structured_output, EditorialEnrichmentProposalV1):
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=EditorialEnrichmentStageErrorCode.OUTPUT_INVALID,
                error_message="The model did not return the required structured proposal",
            )
        try:
            enrichment = validate_editorial_enrichment_proposal(
                execution.structured_output,
                evidence_pack,
                extraction,
                synthesis,
            )
        except EditorialEnrichmentProposalControlError as exc:
            return self._needs_review(
                input_hash=input_hash,
                model_run_id=model_run.id,
                error_code=exc.code,
                error_message=str(exc),
            )
        artifact = await self._editorial_enrichment_service.store_editorial_enrichment_result(
            run_id=run.id,
            subject_id=snapshot.subject_id,
            input_hash=input_hash,
            enrichment=enrichment,
            extraction=extraction,
            synthesis=synthesis,
            raw_result=execution.output_text,
            model_run_id=model_run.id,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=access_policy_hash,
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
        )
        return ProductionEditorialEnrichmentExecution(
            status=EditorialEnrichmentExecutionStatus.SUCCEEDED,
            input_hash=input_hash,
            artifact=artifact,
            model_run_id=model_run.id,
            model_calls=1,
            table_count=len(enrichment.tables),
            diagram_count=len(enrichment.diagrams),
            source_figure_count=0,
            warnings=enrichment.warnings,
        )

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
            source_figure_count=0,
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


def compute_editorial_enrichment_input_hash(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    evidence_pack_hash: str,
    access_policy_hash: str,
) -> str:
    """Hash every functional input and policy version for this stage."""
    if _SHA256_RE.fullmatch(evidence_pack_hash) is None:
        raise ValueError("Editorial enrichment evidence pack hash must be lowercase SHA-256")
    if _SHA256_RE.fullmatch(access_policy_hash) is None:
        raise ValueError("Editorial enrichment access policy hash must be lowercase SHA-256")
    return hashlib.sha256(
        _canonical_json_bytes(
            {
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
                "access_policy_hash": access_policy_hash,
                "prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                "validator_version": EDITORIAL_ENRICHMENT_VALIDATOR_VERSION,
                "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                "routing_policy_version": EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
            }
        )
    ).hexdigest()


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
