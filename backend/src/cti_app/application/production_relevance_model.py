"""Provider-agnostic model proposals for subject relevance projections."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_parsers import sanitize_bridge_output_text
from cti_app.application.production_prompts import (
    RELEVANCE_CLASSIFIER_CONTRACT_VERSION,
    RELEVANCE_CLASSIFIER_PROMPT_VERSION,
    RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION,
)
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.production_wire_archive import (
    record_wire_parse_diagnostics,
    verified_raw_output_text,
)
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionInputSnapshot,
    ProductionRun,
    model_run_awaits_reconciliation,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProposalRejectionReason,
    RelevanceProposalRejectionV1,
    RelevanceReasonCode,
    RelevanceSourcePairRelation,
    RelevanceSourcePairRelationV1,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
    extraction_evidence_elements,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEADER = re.compile(
    r"^(?:@@\s*)?(CLASSIFICATION|RELATION)(?:\s+([A-Za-z0-9._-]+))?\s*(?:@@)?$", re.I
)
_END = re.compile(r"^(?:@@\s*)?END(?:\s+(CLASSIFICATION|RELATION))?\s*(?:@@)?$", re.I)
_FENCE = re.compile(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*$")
_WRAPPER = re.compile(r"^@@\s*(.*?)\s*@@$")
_HANDLE = re.compile(r"\bE\d{3,}\b", re.I)
_FIELD = re.compile(r"^([A-Z][A-Z _-]*)\s*:\s*(.*)$", re.I)
_FIELD_ALIASES = {
    "HANDLE": "handle",
    "ITEM HANDLE": "handle",
    "CLASSIFICATION": "classification",
    "REASON": "reason_code",
    "REASON CODE": "reason_code",
    "SUPPORT": "supporting_handles",
    "SUPPORTING HANDLES": "supporting_handles",
    "EVIDENCE HANDLES": "supporting_handles",
    "RELATION": "relation",
    "WHY": "reason",
}
_REASON_CODES: dict[RelevanceClassification, frozenset[RelevanceReasonCode]] = {
    RelevanceClassification.DIRECT: frozenset(
        {
            RelevanceReasonCode.PRIMARY_CORE_DEFAULT,
            RelevanceReasonCode.SUBJECT_MATCHED_PRIMARY,
            RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
        }
    ),
    RelevanceClassification.CORROBORATION: frozenset(
        {
            RelevanceReasonCode.SUBJECT_MATCHED_CORROBORATION,
            RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
        }
    ),
    RelevanceClassification.CONTEXT: frozenset(
        {
            RelevanceReasonCode.CONTEXT_SOURCE_WITHOUT_RELATION,
            RelevanceReasonCode.MALICIOUS_ROLE_NOT_DEMONSTRATED,
        }
    ),
    RelevanceClassification.COUNTER_INDICATION: frozenset(
        {
            RelevanceReasonCode.EXPLICIT_COUNTER_ANALYSIS,
            RelevanceReasonCode.EXPLICIT_SUBJECT_DENIAL,
        }
    ),
    RelevanceClassification.OUT_OF_SCOPE: frozenset(
        {
            RelevanceReasonCode.EXPLICIT_OTHER_ACTOR,
            RelevanceReasonCode.INDICATOR_SECTION_OTHER_CASE,
        }
    ),
    RelevanceClassification.INDETERMINATE: frozenset(
        {
            RelevanceReasonCode.RELATION_NOT_ESTABLISHED,
            RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
        }
    ),
}

_REASON_CLASSIFICATIONS: dict[RelevanceReasonCode, frozenset[RelevanceClassification]] = {
    reason: frozenset(
        classification for classification, reasons in _REASON_CODES.items() if reason in reasons
    )
    for reason in RelevanceReasonCode
}
_REPAIR_REASON_BY_CLASSIFICATION = {
    RelevanceClassification.DIRECT: RelevanceReasonCode.PRIMARY_CORE_DEFAULT,
    RelevanceClassification.CORROBORATION: RelevanceReasonCode.SUBJECT_MATCHED_CORROBORATION,
    RelevanceClassification.CONTEXT: RelevanceReasonCode.CONTEXT_SOURCE_WITHOUT_RELATION,
    RelevanceClassification.COUNTER_INDICATION: RelevanceReasonCode.EXPLICIT_COUNTER_ANALYSIS,
    RelevanceClassification.OUT_OF_SCOPE: RelevanceReasonCode.EXPLICIT_OTHER_ACTOR,
    RelevanceClassification.INDETERMINATE: RelevanceReasonCode.RELATION_NOT_ESTABLISHED,
}


class RelevanceProposalStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class RelevanceModelEvidencePack:
    subject_context: Mapping[str, str]
    evidence_items: tuple[Mapping[str, str], ...]
    evidence_pack_hash: str
    _handle_to_ref: Mapping[str, ExtractionEvidenceRefV1] = field(
        default_factory=dict, repr=False, compare=False
    )
    _handle_for_ref: Mapping[ExtractionEvidenceRefV1, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    def resolve_handle(self, handle: str) -> ExtractionEvidenceRefV1:
        try:
            return self._handle_to_ref[handle]
        except (KeyError, TypeError) as exc:
            raise ValueError("unknown_handle") from exc


@dataclass(frozen=True, slots=True)
class RelevanceModelClassificationProposal:
    block_id: str
    evidence_ref: ExtractionEvidenceRefV1
    classification: RelevanceClassification
    reason_code: RelevanceReasonCode
    supporting_evidence_refs: tuple[ExtractionEvidenceRefV1, ...]
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class RelevanceWireRejection:
    block_id: str
    reason_code: RelevanceProposalRejectionReason
    raw_sha256: str

    def as_domain_rejection(self) -> RelevanceProposalRejectionV1:
        return RelevanceProposalRejectionV1(
            block_id=self.block_id,
            reason_code=self.reason_code,
            raw_sha256=self.raw_sha256,
        )


@dataclass(frozen=True, slots=True)
class RelevanceWireParseResult:
    classifications: tuple[RelevanceModelClassificationProposal, ...]
    source_pair_relations: tuple[RelevanceSourcePairRelationV1, ...]
    rejections: tuple[RelevanceWireRejection, ...]
    error_code: str | None = None
    transformations: tuple[str, ...] = ()
    #: The model explicitly proposed no change (``@@NONE@@``): a valid empty answer.
    explicit_none: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelRelevanceProposalExecution:
    status: RelevanceProposalStatus
    classifications: tuple[RelevanceModelClassificationProposal, ...] = ()
    source_pair_relations: tuple[RelevanceSourcePairRelationV1, ...] = ()
    rejections: tuple[RelevanceProposalRejectionV1, ...] = ()
    model_calls: int = 0
    model_run_id: UUID | None = None
    invocation_hash: str | None = None
    parse_identity: str | None = None
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    reconciliation_required: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _WireBlock:
    kind: str
    block_id: str
    raw_lines: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    error: bool = False


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _source_display(source: Any) -> str:
    publisher = str(getattr(source, "publisher", None) or "").strip()
    title = str(getattr(source, "title", None) or "").strip()
    if not publisher:
        publisher = urlsplit(str(getattr(source, "canonical_url", ""))).hostname or ""
    return " — ".join(value for value in (publisher, title) if value)


def build_relevance_model_evidence_pack(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
) -> RelevanceModelEvidencePack:
    """Assign prompt-local handles without serializing canonical source IDs."""
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Relevance model snapshot and extraction subjects differ")
    entries: dict[ExtractionEvidenceRefV1, Mapping[str, Any]] = {}
    for ref, payload in extraction_evidence_elements(extraction):
        entries.setdefault(ref, payload)
    ordered_refs = tuple(sorted(entries, key=evidence_ref_sort_key))
    sources = {source.source_document_id: source for source in extraction.sources}
    source_ids = tuple(sorted(sources, key=str))
    source_handles = {source_id: f"S{index:03d}" for index, source_id in enumerate(source_ids, 1)}
    handle_to_ref = {f"E{index:03d}": ref for index, ref in enumerate(ordered_refs, 1)}
    handle_for_ref = {ref: handle for handle, ref in handle_to_ref.items()}
    private_handle_mapping = {
        handle: {
            "source_document_id": str(ref.source_document_id),
            "kind": ref.kind.value,
            "evidence_key": ref.evidence_key,
        }
        for handle, ref in handle_to_ref.items()
    }
    records: list[Mapping[str, str]] = []
    for handle, ref in handle_to_ref.items():
        source = sources[ref.source_document_id]
        payload = entries[ref]
        fields = {
            "category": "category",
            "value": "value",
            "text": "text",
            "name": "name",
            "attack_id": "attack_id",
            "event_date": "event_date",
            "artifact_type": "artifact_type",
            "indicator_status": "indicator_status",
            "rule_type": "rule_type",
            "sha256": "sha256",
            "context": "context",
            "evidence_quote": "evidence_quote",
            "body": "body",
            "date_text": "date_text",
        }
        record: dict[str, str] = {
            "handle": handle,
            "kind": ref.kind.value,
            "source_handle": source_handles[ref.source_document_id],
            "source_label": _source_display(source),
            "source_tier": source.tier.value,
            "editorial_role": source.editorial_role.value if source.editorial_role else "",
            "profile": source.profile.value,
        }
        for output_key, payload_key in fields.items():
            value = payload.get(payload_key)
            if value is not None and str(value).strip():
                record[output_key] = str(value)
        records.append(MappingProxyType(record))

    context = MappingProxyType(
        {
            "title": snapshot.subject_title,
            "actor_or_campaign": snapshot.actor_or_campaign or "",
            "period_start": snapshot.period_start.isoformat(),
            "period_end": snapshot.period_end.isoformat(),
            "summary": snapshot.discovery_summary,
        }
    )
    hash_payload = {
        "contract_version": RELEVANCE_CLASSIFIER_CONTRACT_VERSION,
        "prompt_version": RELEVANCE_CLASSIFIER_PROMPT_VERSION,
        "subject_input_hash": snapshot.input_hash,
        "subject_context": dict(context),
        "evidence_items": [dict(record) for record in records],
        "handle_mapping_sha256": hashlib.sha256(
            _canonical_json(private_handle_mapping)
        ).hexdigest(),
    }
    pack_hash = hashlib.sha256(_canonical_json(hash_payload)).hexdigest()
    return RelevanceModelEvidencePack(
        subject_context=context,
        evidence_items=tuple(records),
        evidence_pack_hash=pack_hash,
        _handle_to_ref=MappingProxyType(handle_to_ref),
        _handle_for_ref=MappingProxyType(handle_for_ref),
    )


def relevance_classifier_invocation_hash(
    pack: RelevanceModelEvidencePack,
    access_policy: SynthesisAccessPolicyV1,
    *,
    prompt_version: str | None = None,
    contract_version: str | None = None,
) -> str:
    payload = {
        "evidence_pack_hash": pack.evidence_pack_hash,
        "prompt_version": prompt_version or RELEVANCE_CLASSIFIER_PROMPT_VERSION,
        "contract_version": contract_version or RELEVANCE_CLASSIFIER_CONTRACT_VERSION,
        "access_policy_hash": synthesis_access_policy_hash(access_policy),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def relevance_classifier_model_run_id(run: ProductionRun, invocation_hash: str) -> UUID:
    if not isinstance(run, ProductionRun) or _SHA256.fullmatch(invocation_hash) is None:
        raise ValueError("Relevance classifier invocation identity is invalid")
    return uuid5(NAMESPACE_URL, f"production-relevance-classifier:{run.id}:{invocation_hash}")


def relevance_classifier_parse_identity(
    raw_output_sha256: str,
    pack: RelevanceModelEvidencePack,
    *,
    parser_version: str | None = None,
    contract_version: str | None = None,
) -> str:
    if _SHA256.fullmatch(raw_output_sha256) is None:
        raise ValueError("Relevance classifier raw output hash is invalid")
    handle_mapping = {
        handle: {
            "source_document_id": str(ref.source_document_id),
            "kind": ref.kind.value,
            "evidence_key": ref.evidence_key,
        }
        for handle, ref in pack._handle_to_ref.items()
    }
    payload = {
        "raw_output_sha256": raw_output_sha256,
        "parser_version": parser_version or RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION,
        "contract_version": contract_version or RELEVANCE_CLASSIFIER_CONTRACT_VERSION,
        "evidence_pack_hash": pack.evidence_pack_hash,
        "handle_mapping_sha256": hashlib.sha256(_canonical_json(handle_mapping)).hexdigest(),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def build_relevance_classifier_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    pack: RelevanceModelEvidencePack,
    access_policy: SynthesisAccessPolicyV1,
    *,
    extraction_hash: str,
    prompt_version: str | None = None,
    contract_version: str | None = None,
) -> ModelRequest:
    selected_prompt = prompt_version or RELEVANCE_CLASSIFIER_PROMPT_VERSION
    selected_contract = contract_version or RELEVANCE_CLASSIFIER_CONTRACT_VERSION
    invocation_hash = relevance_classifier_invocation_hash(
        pack,
        access_policy,
        prompt_version=selected_prompt,
        contract_version=selected_contract,
    )
    categories = ", ".join(item.value.upper() for item in RelevanceClassification)
    reason_codes = ", ".join(item.value for item in RelevanceReasonCode)
    relations = ", ".join(item.value.upper() for item in RelevanceSourcePairRelation)
    lines = [
        "Propose relevance classifications for the frozen CTI subject below.",
        f"Prompt version: {selected_prompt}",
        f"Contract version: {selected_contract}",
        "Use only the supplied extraction items. Do not browse or add facts.",
        "Source handles and evidence handles are temporary; return handles only.",
        (
            "For every decision use one CLASSIFICATION block. "
            "Omitted items keep the deterministic baseline."
        ),
        "Allowed classifications: " + categories,
        "Allowed reason codes: " + reason_codes,
        "VALID CLASSIFICATION x REASON_CODE PAIRS (use only these exact pairs):",
        "DIRECT: primary_core_default, subject_matched_primary, malicious_subject_relation",
        "CORROBORATION: subject_matched_corroboration, malicious_subject_corroboration",
        "CONTEXT: context_source_without_relation, malicious_role_not_demonstrated",
        "COUNTER_INDICATION: explicit_counter_analysis, explicit_subject_denial",
        "OUT_OF_SCOPE: explicit_other_actor, indicator_section_other_case",
        "INDETERMINATE: relation_not_established, subject_link_not_demonstrated",
        (
            "For DIRECT or CORROBORATION on an indicator, cite at least one additional "
            "supporting evidence handle that documents the subject relation."
        ),
        (
            "For source-pair notes use a RELATION block, cite evidence from exactly two "
            "sources, including a primary FULL source. A non-primary source may use "
            "IOC_RULES when its uncertainty bears on the primary claim."
        ),
        "Allowed source-pair relations: " + relations,
        (
            "Evaluate uncertainties and limitations from counter_analysis sources and "
            "other non-primary sources against the relevant primary claims. Keep their "
            "evidence as CONTEXT or COUNTER_INDICATION and relate it to the primary claim."
        ),
        (
            "Example: Chainalysis attributes an Iranian operation through malware/MOIS; "
            "Bitquery says the Bitcoin OP_RETURN pattern predates the operation and is "
            "not attributed to an actor. Record a COUNTER_INDICATION relation against "
            "the Iranian OP_RETURN attribution claim; do not claim the transactions match."
        ),
        (
            "Example: if a Bitquery uncertainty says an OP_RETURN pattern is unattributed, "
            "cite both that uncertainty and the Chainalysis OP_RETURN claim in the RELATION block."
        ),
        "If you propose no classification and no relation, return only @@NONE@@.",
        "Output text blocks only; do not output JSON.",
        "",
        "FROZEN SUBJECT CONTEXT",
    ]
    for key, value in pack.subject_context.items():
        lines.append(f"{key.upper()}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(("", "HANDLE-ADDRESSED EXTRACTION ITEMS"))
    for record in pack.evidence_items:
        lines.append(f"ITEM {record['handle']}")
        for key, value in record.items():
            if key == "handle":
                continue
            lines.append(f"{key.upper()}: {json.dumps(value, ensure_ascii=False)}")
        lines.append("END ITEM")
    lines.extend(
        (
            "",
            "BLOCK FORMAT",
            "@@ CLASSIFICATION C001 @@",
            "handle: E001",
            "classification: DIRECT",
            "reason_code: malicious_subject_relation",
            "supporting_handles: E002, E003",
            "END CLASSIFICATION",
            "@@ RELATION R001 @@",
            "relation: LINK_NOT_DEMONSTRATED",
            "reason: The reports do not share a demonstrated transaction or wallet pivot.",
            "supporting_handles: E003, E014",
            "END RELATION",
        )
    )
    return ModelRequest(
        text="\n".join(lines),
        prompt_template_id="production-relevance-classifier",
        prompt_template_version=selected_prompt,
        evidence_pack_hash=pack.evidence_pack_hash,
        external_llm_allowed=(
            access_policy.external_llm_allowed and not access_policy.do_not_submit
        ),
        routing_hint=ModelRoutingHint.PREMIUM_SYNTHESIS,
        sensitivity=access_policy.effective_tlp.value,
        metadata={
            "relevance_classifier_invocation_hash": invocation_hash,
            "production_input_hash": snapshot.input_hash,
            "extraction_hash": extraction_hash,
            "access_policy_hash": synthesis_access_policy_hash(access_policy),
            "model_policy_version": selected_contract,
            "external_llm_allowed": access_policy.external_llm_allowed,
            "do_not_submit": access_policy.do_not_submit,
        },
        web_search=False,
        background=False,
        conversation=None,
        run_id=relevance_classifier_model_run_id(run, invocation_hash),
        allow_failed_resubmit=True,
    )


def _clean_wire_line(line: str) -> str:
    value = line.strip()
    if value.startswith("#"):
        value = re.sub(r"^#{1,6}\s*", "", value)
    value = re.sub(r"^[-*+]\s+", "", value).strip()
    if len(value) >= 4 and value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    wrapped = _WRAPPER.fullmatch(value)
    return wrapped.group(1).strip() if wrapped else value


def _handles(value: str) -> tuple[str, ...] | None:
    raw = value.strip()
    found = tuple(item.upper() for item in _HANDLE.findall(raw))
    residue = _HANDLE.sub("", raw)
    residue = re.sub(r"\band\b", "", residue, flags=re.I)
    residue = re.sub(r"[\[\](){}\s,;./&|]+", "", residue)
    return found if found and not residue else None


def _block_digest(block: _WireBlock) -> str:
    return hashlib.sha256("\n".join(block.raw_lines).encode("utf-8")).hexdigest()


_NONE_MARKERS = frozenset({"@@none@@", "@@ none @@", "none", "no proposals"})


def parse_relevance_classifier_wire(
    raw_text: str,
    pack: RelevanceModelEvidencePack,
    extraction: ProductionExtractionV1,
) -> RelevanceWireParseResult:
    if not isinstance(raw_text, str):
        return RelevanceWireParseResult((), (), (), "relevance_classifier_unintelligible_response")
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    if sanitized.startswith("\ufeff"):
        sanitized = sanitized[1:]
    transformations = (
        ()
        if sanitized == raw_text.replace("\r\n", "\n").replace("\r", "\n")
        else ("bridge_markers_or_byte_order_mark_removed",)
    )
    blocks: list[_WireBlock] = []
    current: _WireBlock | None = None
    recognized = False
    explicit_none = False
    sequence = 0
    for raw_line in sanitized.splitlines():
        if _FENCE.fullmatch(raw_line):
            continue
        line = _clean_wire_line(raw_line)
        if not line or line in {"---", "***"}:
            continue
        if line.casefold() in _NONE_MARKERS:
            recognized = True
            explicit_none = True
            continue
        end = _END.fullmatch(line)
        if end is not None:
            if current is not None:
                current.raw_lines.append(raw_line)
                blocks.append(current)
                current = None
            recognized = True
            continue
        header = _HEADER.fullmatch(line)
        wrapped = _WRAPPER.fullmatch(raw_line.strip())
        if header is None and wrapped is not None:
            header = _HEADER.fullmatch(_clean_wire_line(wrapped.group(1)))
        if header is not None:
            if current is not None:
                blocks.append(current)
            sequence += 1
            kind, supplied_id = header.groups()
            current = _WireBlock(
                kind=kind.upper(),
                block_id=(supplied_id or f"{kind[0]}{sequence:03d}")[:80],
                raw_lines=[raw_line],
            )
            recognized = True
            continue
        if current is None:
            # Bridge citations and other short preambles have already been
            # normalized; unrelated prose does not poison independent blocks.
            continue
        field_match = _FIELD.fullmatch(line)
        if field_match is None:
            current.error = True
            current.raw_lines.append(raw_line)
            continue
        raw_name, raw_value = field_match.groups()
        alias = re.sub(r"[\s_-]+", " ", raw_name.strip().upper())
        name = _FIELD_ALIASES.get(alias)
        if current.kind == "RELATION" and alias in {"REASON", "WHY"}:
            name = "reason"
        if name is None or name in current.fields:
            current.error = True
        else:
            current.fields[name] = raw_value.strip()
        current.raw_lines.append(raw_line)
    if current is not None:
        blocks.append(current)
    if not recognized:
        return RelevanceWireParseResult(
            (), (), (), "relevance_classifier_unintelligible_response", transformations
        )

    sources = {source.source_document_id: source for source in extraction.sources}
    classifications: list[RelevanceModelClassificationProposal] = []
    relations: list[RelevanceSourcePairRelationV1] = []
    rejections: list[RelevanceWireRejection] = []
    warnings: list[str] = []
    seen_block_ids: set[str] = set()
    seen_targets: set[ExtractionEvidenceRefV1] = set()
    seen_pairs: set[tuple[UUID, UUID]] = set()

    def reject(block: _WireBlock, reason: RelevanceProposalRejectionReason) -> None:
        rejections.append(RelevanceWireRejection(block.block_id, reason, _block_digest(block)))

    for block in blocks:
        if block.block_id in seen_block_ids:
            reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
            continue
        seen_block_ids.add(block.block_id)
        if block.error:
            reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
            continue
        if block.kind == "CLASSIFICATION":
            if not {"handle", "classification", "reason_code"} <= block.fields.keys():
                reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
                continue
            target_handle = block.fields["handle"].upper()
            try:
                target = pack.resolve_handle(target_handle)
            except ValueError:
                reject(block, RelevanceProposalRejectionReason.UNKNOWN_HANDLE)
                continue
            try:
                classification = RelevanceClassification(
                    block.fields["classification"].strip().casefold().replace("-", "_")
                )
            except ValueError:
                reject(block, RelevanceProposalRejectionReason.UNKNOWN_CLASSIFICATION)
                continue
            try:
                reason = RelevanceReasonCode(
                    block.fields["reason_code"].strip().casefold().replace("-", "_")
                )
            except ValueError:
                reject(block, RelevanceProposalRejectionReason.UNKNOWN_REASON_CODE)
                continue
            if reason not in _REASON_CODES[classification]:
                valid_classifications = _REASON_CLASSIFICATIONS[reason]
                if len(valid_classifications) != 1 or classification in valid_classifications:
                    reject(
                        block,
                        RelevanceProposalRejectionReason.INVALID_REASON_FOR_CLASSIFICATION,
                    )
                    continue
                repaired_reason = _REPAIR_REASON_BY_CLASSIFICATION[classification]
                warnings.append(
                    f"relevance_reason_repaired:{block.block_id}:"
                    f"{reason.value}->{repaired_reason.value}"
                )
                reason = repaired_reason
            supporting: set[ExtractionEvidenceRefV1] = set()
            support_text = block.fields.get("supporting_handles", "")
            if support_text:
                support_handles = _handles(support_text)
                if support_handles is None:
                    reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
                    continue
                try:
                    supporting.update(pack.resolve_handle(handle) for handle in support_handles)
                except ValueError:
                    reject(block, RelevanceProposalRejectionReason.UNKNOWN_HANDLE)
                    continue
            if target in seen_targets:
                reject(block, RelevanceProposalRejectionReason.DUPLICATE_TARGET)
                continue
            seen_targets.add(target)
            classifications.append(
                RelevanceModelClassificationProposal(
                    block_id=block.block_id,
                    evidence_ref=target,
                    classification=classification,
                    reason_code=reason,
                    supporting_evidence_refs=tuple(sorted(supporting, key=evidence_ref_sort_key)),
                    raw_sha256=_block_digest(block),
                )
            )
            continue

        if not {"relation", "reason", "supporting_handles"} <= block.fields.keys():
            reason_code = (
                RelevanceProposalRejectionReason.RELATION_MISSING_REASON
                if "reason" not in block.fields
                else RelevanceProposalRejectionReason.MALFORMED_BLOCK
            )
            reject(block, reason_code)
            continue
        try:
            relation = RelevanceSourcePairRelation(
                block.fields["relation"].strip().casefold().replace("-", "_")
            )
        except ValueError:
            reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
            continue
        reason_text = block.fields["reason"].strip()
        support_handles = _handles(block.fields["supporting_handles"])
        if not reason_text:
            reject(block, RelevanceProposalRejectionReason.RELATION_MISSING_REASON)
            continue
        if support_handles is None:
            reject(block, RelevanceProposalRejectionReason.MALFORMED_BLOCK)
            continue
        try:
            support_refs = tuple(
                sorted(
                    {pack.resolve_handle(handle) for handle in support_handles},
                    key=evidence_ref_sort_key,
                )
            )
        except ValueError:
            reject(block, RelevanceProposalRejectionReason.UNKNOWN_HANDLE)
            continue
        source_ids = {ref.source_document_id for ref in support_refs}
        if len(source_ids) != 2:
            reject(block, RelevanceProposalRejectionReason.RELATION_REQUIRES_TWO_FULL_SOURCES)
            continue
        pair_sources = tuple(sources[source_id] for source_id in source_ids)
        both_full = all(source.profile.value == "full" for source in pair_sources)
        primary_full_with_secondary = any(
            source.profile.value == "full"
            and getattr(source.editorial_role, "value", source.editorial_role) == "primary"
            for source in pair_sources
        ) and any(
            getattr(source.editorial_role, "value", source.editorial_role) != "primary"
            for source in pair_sources
        )
        if not (both_full or primary_full_with_secondary):
            reject(block, RelevanceProposalRejectionReason.RELATION_SOURCES_NOT_ELIGIBLE)
            continue
        ordered_source_ids = sorted(source_ids, key=str)
        pair = (ordered_source_ids[0], ordered_source_ids[1])
        if pair in seen_pairs:
            reject(block, RelevanceProposalRejectionReason.RELATION_DUPLICATE_SOURCE_PAIR)
            continue
        seen_pairs.add(pair)
        relations.append(
            RelevanceSourcePairRelationV1(
                relation=relation,
                reason=reason_text,
                supporting_evidence_refs=support_refs,
            )
        )

    return RelevanceWireParseResult(
        tuple(classifications),
        tuple(relations),
        tuple(rejections),
        transformations=transformations,
        explicit_none=explicit_none,
        warnings=tuple(warnings),
    )


def _normalized_parse_payload(parsed: RelevanceWireParseResult) -> bytes:
    payload = {
        "classifications": [
            {
                "block_id": item.block_id,
                "evidence_ref": {
                    "source_document_id": str(item.evidence_ref.source_document_id),
                    "kind": item.evidence_ref.kind.value,
                    "evidence_key": item.evidence_ref.evidence_key,
                },
                "classification": item.classification.value,
                "reason_code": item.reason_code.value,
                "supporting_evidence_refs": [
                    {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    }
                    for ref in item.supporting_evidence_refs
                ],
            }
            for item in parsed.classifications
        ],
        "source_pair_relations": [
            {
                "relation": item.relation.value,
                "reason": item.reason,
                "supporting_evidence_refs": [
                    {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    }
                    for ref in item.supporting_evidence_refs
                ],
            }
            for item in parsed.source_pair_relations
        ],
    }
    return _canonical_json(payload)


class ModelRelevanceClassifier:
    """Async proposal path plus the L3b deterministic classifier protocol."""

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        parser_version: str | None = None,
        prompt_version: str | None = None,
        contract_version: str | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._parser_version = parser_version or RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION
        self._prompt_version = prompt_version or RELEVANCE_CLASSIFIER_PROMPT_VERSION
        self._contract_version = contract_version or RELEVANCE_CLASSIFIER_CONTRACT_VERSION

    @property
    def version(self) -> str:
        return ":".join(
            (
                "model-subject-scope-v4-case-sections",
                self._contract_version,
                self._prompt_version,
                self._parser_version,
            )
        )

    def classify(
        self,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        evidence: tuple[tuple[ExtractionEvidenceRefV1, Mapping[str, Any]], ...],
    ) -> tuple[Any, ...]:
        # The sync classifier port continues to provide the conservative
        # baseline; async model proposals are merged by the projection service.
        from cti_app.application.production_relevance import DeterministicRelevanceClassifier

        return DeterministicRelevanceClassifier().classify(snapshot, extraction, evidence)

    async def propose(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        access_policy: SynthesisAccessPolicyV1,
    ) -> ModelRelevanceProposalExecution:
        pack = build_relevance_model_evidence_pack(snapshot, extraction)
        request = build_relevance_classifier_model_request(
            run,
            snapshot,
            pack,
            access_policy,
            extraction_hash=canonical_extraction_hash(extraction),
            prompt_version=self._prompt_version,
            contract_version=self._contract_version,
        )
        invocation_hash = str(request.metadata["relevance_classifier_invocation_hash"])
        model_calls = 0
        try:
            execution, archive_error = await self._verified_existing_execution(request)
            if archive_error is not None:
                return ModelRelevanceProposalExecution(
                    status=RelevanceProposalStatus.NEEDS_REVIEW,
                    model_calls=0,
                    model_run_id=request.run_id,
                    invocation_hash=invocation_hash,
                    error_code="relevance_classifier_raw_output_invalid",
                    error="The archived relevance response could not be verified.",
                    details=archive_error,
                )
            if execution is None:
                model_calls = 1
                execution = await self._model_gateway.draft(request)
        except ModelSubmissionReconciliationRequiredError as exc:
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=exc.model_run_id or request.run_id,
                invocation_hash=invocation_hash,
                error_code=PRODUCTION_RECONCILIATION_ERROR_CODE,
                error="A provider submission may have been accepted and must be reconciled.",
                details={**exc.details, "error_code": exc.code},
                reconciliation_required=True,
            )
        except ExternalModelBlockedError as exc:
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=request.run_id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_policy_blocked",
                error="The source access policy blocked the model route.",
                details={"error_code": exc.code},
            )
        except ModelGatewayError as exc:
            if exc.retryable:
                raise
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=request.run_id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_model_call_failed",
                error="The relevance classifier model call failed.",
                details={"error_code": exc.code},
            )

        model_run = execution.run
        if model_run.status is ModelRunStatus.NEEDS_REVIEW:
            if model_run_awaits_reconciliation(model_run.error_code):
                return ModelRelevanceProposalExecution(
                    status=RelevanceProposalStatus.NEEDS_REVIEW,
                    model_calls=model_calls,
                    model_run_id=model_run.id,
                    invocation_hash=invocation_hash,
                    error_code=PRODUCTION_RECONCILIATION_ERROR_CODE,
                    error="A provider submission may have been accepted and must be reconciled.",
                    details=dict(model_run.error_details or {}),
                    reconciliation_required=True,
                )
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=model_run.id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_model_call_failed",
                error="The model run needs review without a usable response.",
                details={"error_code": model_run.error_code},
            )
        if model_run.status is not ModelRunStatus.SUCCEEDED:
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=model_run.id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_model_call_failed",
                error=f"The model run reached status {model_run.status.value}.",
                details={"error_code": model_run.error_code},
            )
        raw_text, raw_error = await verified_raw_output_text(
            self._model_gateway,
            model_run,
            error_prefix="relevance_classifier",
            expected_text=execution.output_text,
        )
        if raw_error is not None or raw_text is None:
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=model_run.id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_raw_output_invalid",
                error="The model response is not backed by verified archived bytes.",
                details=raw_error or {},
            )

        raw_sha256 = model_run.raw_output_sha256
        if raw_sha256 is None:
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                model_calls=model_calls,
                model_run_id=model_run.id,
                invocation_hash=invocation_hash,
                error_code="relevance_classifier_raw_output_invalid",
                error="The model response has no archived output digest.",
            )
        parsed = parse_relevance_classifier_wire(raw_text, pack, extraction)
        parse_identity = relevance_classifier_parse_identity(
            raw_sha256,
            pack,
            parser_version=self._parser_version,
            contract_version=self._contract_version,
        )
        validation_errors = [
            {
                "path": ["blocks", item.block_id],
                "code": item.reason_code.value,
                "value_sha256": item.raw_sha256,
            }
            for item in parsed.rejections
        ]
        if parsed.error_code is not None:
            validation_errors.append(
                {
                    "path": ["response"],
                    "code": str(parsed.error_code),
                    "value_sha256": raw_sha256,
                }
            )
        normalized_output = _normalized_parse_payload(parsed) if parsed.error_code is None else None
        await record_wire_parse_diagnostics(
            self._model_gateway,
            model_run,
            parser_stage="relevance_classifier",
            parse_identity=parse_identity,
            validation_errors=validation_errors,
            transformations=(
                *parsed.transformations,
                f"relevance_classifier_parser:{self._parser_version}",
                f"relevance_classifier_contract:{self._contract_version}",
                f"relevance_classifier_prompt:{self._prompt_version}",
            ),
            normalized_output=normalized_output,
        )
        if parsed.error_code is not None or not (
            parsed.classifications
            or parsed.source_pair_relations
            or (parsed.explicit_none and not parsed.rejections)
        ):
            error_code = parsed.error_code or "relevance_classifier_no_valid_blocks"
            return ModelRelevanceProposalExecution(
                status=RelevanceProposalStatus.NEEDS_REVIEW,
                rejections=tuple(item.as_domain_rejection() for item in parsed.rejections),
                warnings=parsed.warnings,
                model_calls=model_calls,
                model_run_id=model_run.id,
                invocation_hash=invocation_hash,
                parse_identity=parse_identity,
                error_code=error_code,
                error="The relevance classifier response contains no usable proposal blocks.",
                details={
                    "parse_identity": parse_identity,
                    "rejection_count": len(parsed.rejections),
                    "rejection_codes": sorted(
                        {item.reason_code.value for item in parsed.rejections}
                    ),
                    "warnings": list(parsed.warnings),
                    "raw_output_sha256": model_run.raw_output_sha256,
                },
            )
        return ModelRelevanceProposalExecution(
            status=RelevanceProposalStatus.SUCCEEDED,
            classifications=parsed.classifications,
            source_pair_relations=parsed.source_pair_relations,
            rejections=tuple(item.as_domain_rejection() for item in parsed.rejections),
            warnings=parsed.warnings,
            model_calls=model_calls,
            model_run_id=model_run.id,
            invocation_hash=invocation_hash,
            parse_identity=parse_identity,
            details={
                "raw_output_sha256": model_run.raw_output_sha256,
                "rejection_count": len(parsed.rejections),
                "rejection_codes": sorted({item.reason_code.value for item in parsed.rejections}),
                "warnings": list(parsed.warnings),
            },
        )

    async def _verified_existing_execution(
        self, request: ModelRequest
    ) -> tuple[ModelExecution | None, Mapping[str, Any] | None]:
        if request.run_id is None:
            return None, {"error_code": "relevance_classifier_invocation_identity_missing"}
        model_run = await self._model_gateway.get_run(request.run_id)
        if model_run is None or model_run.status is not ModelRunStatus.SUCCEEDED:
            return None, None
        if (
            model_run.id != request.run_id
            or model_run.prompt_template_id != request.prompt_template_id
            or model_run.prompt_template_version != request.prompt_template_version
            or model_run.evidence_pack_hash != request.evidence_pack_hash
        ):
            return None, {"error_code": "relevance_classifier_invocation_identity_mismatch"}
        text, error = await verified_raw_output_text(
            self._model_gateway,
            model_run,
            error_prefix="relevance_classifier",
        )
        if error is not None or text is None:
            return None, error
        return (
            ModelExecution(
                run=model_run,
                output_text=text,
                structured_output=None,
                metadata={"checkpoint": "verified_raw_output_reparse"},
            ),
            None,
        )
