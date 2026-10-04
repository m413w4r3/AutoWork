"""Canonical synthesis: deterministic evidence inputs, validation and service."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.persistence import (
    ProductionUnitOfWork,
    ProductionUnitOfWorkFactory,
    SourceCollectionRepository,
    SourceDocumentRepository,
)
from cti_app.application.production_access_policy import resolve_source_policy
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_parsers import sanitize_bridge_output_text
from cti_app.application.production_prompts import (
    SYNTHESIS_EDITORIAL_CONTRACT_V4,
    SYNTHESIS_PROMPT_VERSION,
    SYNTHESIS_PROPOSAL_CONTRACT_VERSION,
    SYNTHESIS_WIRE_PARSER_VERSION,
)
from cti_app.application.production_wire_archive import (
    record_wire_parse_diagnostics,
    verified_raw_output_text,
)
from cti_app.domain.classification import TLP
from cti_app.domain.model_runs import ModelRun, ModelRunStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
    model_run_awaits_reconciliation,
)
from cti_app.domain.production_extraction import (
    PRODUCTION_EXTRACTION_SCHEMA_VERSION,
    ProductionExtractionV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import ProductionEditorialRole, ProductionReferenceTier
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    relevance_projection_from_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    _MONTH_NUMBERS,
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_EVIDENCE_REF_ALGORITHM_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    evidence_ref_sort_key,
    extraction_evidence_elements,
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
    resolve_timeline_date_text,
    synthesis_evidence_refs,
    timeline_sort_key,
    validate_synthesis_lineage,
)

if TYPE_CHECKING:
    from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
    from cti_app.application.production_stages import SynthesisService

SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION = "synthesis-evidence-pack-v6-ranked-uncertainty-handles"
SYNTHESIS_TIMELINE_POLICY_VERSION = "synthesis-timeline-v4-direct-corroboration-only"
SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION = 2
SYNTHESIS_ACCESS_POLICY_VERSION = "synthesis-access-policy-v2-document-collection"
SYNTHESIS_VALIDATOR_VERSION = "synthesis-validator-v2-headingless-reserve-handles"
MAX_SYNTHESIS_UNCERTAINTIES = 10
SYNTHESIS_MODEL_POLICY_VERSION = "synthesis-model-policy-v1"
SYNTHESIS_ROUTING_POLICY_VERSION = "synthesis-routing-policy-v1"
MAX_TECHNICAL_EVIDENCE_V1 = 128


class SynthesisProposalErrorCode(StrEnum):
    UNKNOWN_EVIDENCE = "synthesis_unknown_evidence"
    UNKNOWN_TECHNICAL_VALUE = "synthesis_unknown_technical_value"
    UNKNOWN_DATE = "synthesis_unknown_date"
    OUTPUT_INVALID = "synthesis_output_invalid"


class SynthesisProposalControlError(RuntimeError):
    """A structured proposal failed a deterministic Synthesis control."""

    def __init__(self, code: SynthesisProposalErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SynthesisClaimProposalV1(_StrictProposalModel):
    text: StrictStr
    evidence_handles: tuple[StrictStr, ...]

    @field_validator("text")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Claim text must be non-empty text")
        return value

    @field_validator("evidence_handles")
    @classmethod
    def _unique_handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not handle for handle in value) or len(set(value)) != len(value):
            raise ValueError("Claim evidence handles must be a non-empty unique tuple")
        return value


class SynthesisSectionProposalV1(_StrictProposalModel):
    kind: SynthesisSectionKind
    heading: StrictStr = ""
    claims: tuple[SynthesisClaimProposalV1, ...]

    @field_validator("claims")
    @classmethod
    def _nonempty_claims(
        cls, value: tuple[SynthesisClaimProposalV1, ...]
    ) -> tuple[SynthesisClaimProposalV1, ...]:
        if not value:
            raise ValueError("Section claims must be a non-empty tuple")
        return value


class SynthesisProposalV1(_StrictProposalModel):
    """Strict internal proposal created from the tolerant text wire format."""

    lead: tuple[SynthesisClaimProposalV1, ...]
    sections: tuple[SynthesisSectionProposalV1, ...]


@dataclass(frozen=True, slots=True)
class SynthesisWireRejection:
    block_id: str
    reason_code: str
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class SynthesisWireWarning:
    block_id: str
    warning_code: str


@dataclass(frozen=True, slots=True)
class SynthesisWireParseResult:
    proposal: SynthesisProposalV1 | None
    rejections: tuple[SynthesisWireRejection, ...] = ()
    error_code: str | None = None
    explicit_empty: bool = False
    transformations: tuple[str, ...] = ()
    warnings: tuple[SynthesisWireWarning, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True)
class _WireSection:
    block_id: str
    kind: SynthesisSectionKind | None
    heading_present: bool = False
    claims: list[SynthesisClaimProposalV1] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    error_code: str | None = None


@dataclass(slots=True)
class _WireClaim:
    block_id: str
    group: str
    section: _WireSection | None
    evidence_handles: tuple[str, ...] | None = None
    text_lines: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    text_started: bool = False
    error_code: str | None = None


_SYNTHESIS_FENCE = re.compile(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*$")
_SYNTHESIS_BLOCK_WRAPPERS = re.compile(r"^@@\s*(.*?)\s*@@$")
_SYNTHESIS_SECTION_HEADER = re.compile(
    r"^SECTION\s*:?\s+([A-Za-z][A-Za-z0-9_-]*)(?:\s+([A-Za-z0-9._-]+))?\s*:?$",
    re.IGNORECASE,
)
_SYNTHESIS_CLAIM_HEADER = re.compile(r"^CLAIM(?:\s+([A-Za-z0-9._-]+))?\s*:?$", re.IGNORECASE)
_SYNTHESIS_FIELD = re.compile(
    r"^(EVIDENCE(?:\s+HANDLES?)?|HANDLES|TEXT|HEADING)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_SYNTHESIS_FIELD_BARE = re.compile(
    r"^(EVIDENCE(?:\s+HANDLES?)?|HANDLES|TEXT|HEADING)\s+(.+)$",
    re.IGNORECASE,
)
_SYNTHESIS_HANDLE = re.compile(r"\b[ER]\d{3,}\b")


def _wire_line(line: str) -> str:
    value = line.strip()
    if value.startswith("#"):
        value = re.sub(r"^#{1,6}\s*", "", value)
    value = re.sub(r"^[-*+]\s+", "", value).strip()
    if len(value) >= 4 and value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    wrapped = _SYNTHESIS_BLOCK_WRAPPERS.fullmatch(value)
    if wrapped is not None:
        value = wrapped.group(1).strip()
    return value


def _wire_rejection(
    block_id: str, reason_code: str, raw_lines: Iterable[str]
) -> SynthesisWireRejection:
    raw = "\n".join(raw_lines).encode("utf-8")
    return SynthesisWireRejection(
        block_id=block_id,
        reason_code=reason_code,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _wire_evidence_handles(value: str) -> tuple[str, ...] | None:
    raw = value.strip()
    handles = tuple(_SYNTHESIS_HANDLE.findall(raw))
    residue = _SYNTHESIS_HANDLE.sub("", raw)
    residue = re.sub(r"\band\b", "", residue, flags=re.IGNORECASE)
    residue = re.sub(r"[\[\](){}\s,;./&|]+", "", residue)
    return handles if handles and not residue else None


def parse_synthesis_proposal_wire(raw_text: str) -> SynthesisWireParseResult:
    """Parse independent synthesis blocks without relaxing claim validation."""
    if not isinstance(raw_text, str):
        return SynthesisWireParseResult(None, error_code="synthesis_unintelligible_response")
    sanitized = sanitize_bridge_output_text(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    if sanitized.startswith("\ufeff"):
        sanitized = sanitized[1:]
    normalized_endings = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    transformations = ("bridge_ui_markers_removed",) if sanitized != normalized_endings else ()
    lines = sanitized.splitlines()
    rejected: list[SynthesisWireRejection] = []
    warnings: list[SynthesisWireWarning] = []
    diagnostics: list[str] = []
    lead: list[SynthesisClaimProposalV1] = []
    sections: list[SynthesisSectionProposalV1] = []
    current_group: str | None = None
    current_section: _WireSection | None = None
    current_claim: _WireClaim | None = None
    explicit_empty = False
    recognized = False
    sequence = 0
    seen_ids: set[str] = set()

    def reject(block_id: str, code: str, raw_lines: Iterable[str]) -> None:
        rejected.append(_wire_rejection(block_id, code, raw_lines))

    def finish_claim() -> None:
        nonlocal current_claim
        claim = current_claim
        current_claim = None
        if claim is None:
            return
        if claim.error_code is not None:
            reject(claim.block_id, claim.error_code, claim.raw_lines)
            return
        if claim.group == "section" and (
            claim.section is None or claim.section.error_code is not None
        ):
            reject(claim.block_id, "synthesis_claim_in_invalid_section", claim.raw_lines)
            return
        if claim.evidence_handles is None:
            reject(claim.block_id, "synthesis_claim_missing_evidence_handles", claim.raw_lines)
            return
        if not claim.text_started or not "\n".join(claim.text_lines).strip():
            reject(claim.block_id, "synthesis_claim_missing_text", claim.raw_lines)
            return
        if len(set(claim.evidence_handles)) != len(claim.evidence_handles):
            reject(claim.block_id, "synthesis_claim_duplicate_evidence_handle", claim.raw_lines)
            return
        try:
            parsed = SynthesisClaimProposalV1(
                text="\n".join(claim.text_lines).strip(),
                evidence_handles=claim.evidence_handles,
            )
        except ValueError:
            reject(claim.block_id, "synthesis_claim_schema_invalid", claim.raw_lines)
            return
        if claim.group == "lead":
            lead.append(parsed)
        elif claim.section is not None:
            claim.section.claims.append(parsed)

    def finish_section() -> None:
        nonlocal current_section
        section = current_section
        current_section = None
        if section is None:
            return
        if section.error_code is not None:
            reject(section.block_id, section.error_code, section.raw_lines)
            return
        if section.heading_present:
            warnings.append(
                SynthesisWireWarning(
                    block_id=section.block_id,
                    warning_code="synthesis_section_heading_dropped",
                )
            )
        if not section.claims:
            reject(section.block_id, "synthesis_section_missing_valid_claims", section.raw_lines)
            return
        try:
            sections.append(
                SynthesisSectionProposalV1(
                    kind=section.kind,
                    heading="",
                    claims=tuple(section.claims),
                )
            )
        except ValueError:
            reject(section.block_id, "synthesis_section_schema_invalid", section.raw_lines)

    for index, raw_line in enumerate(lines, start=1):
        if _SYNTHESIS_FENCE.fullmatch(raw_line):
            continue
        line = _wire_line(raw_line)
        if not line or line in {"---", "***"}:
            continue

        if line.casefold() in {"empty", "no claims", "no supported claims"}:
            finish_claim()
            finish_section()
            recognized = True
            if explicit_empty or lead or sections or current_group is not None:
                reject(f"EMPTY-{index}", "synthesis_empty_marker_conflict", (raw_line,))
            else:
                explicit_empty = True
            current_group = None
            continue
        if explicit_empty:
            reject(f"EMPTY-{index}", "synthesis_empty_marker_conflict", (raw_line,))
            explicit_empty = False

        header_value = line.rstrip(":").strip()
        if header_value.casefold() in {"lead", "synthesis lead", "synthesis output"}:
            finish_claim()
            finish_section()
            current_group = "lead" if header_value.casefold() != "synthesis output" else None
            recognized = True
            continue

        if header_value.casefold() in {"diagnostics", "synthesis diagnostics"}:
            finish_claim()
            finish_section()
            current_group = "diagnostics"
            recognized = True
            continue

        if header_value.casefold() in {"end diagnostics", "end synthesis diagnostics"}:
            finish_claim()
            finish_section()
            current_group = None
            recognized = True
            continue

        section_match = _SYNTHESIS_SECTION_HEADER.fullmatch(header_value)
        if section_match is not None:
            finish_claim()
            finish_section()
            kind_text, local_id = section_match.groups()
            sequence += 1
            section_id = local_id or f"S{sequence:03d}"
            kind = next(
                (
                    item
                    for item in SynthesisSectionKind
                    if item.value.casefold() == kind_text.casefold()
                ),
                None,
            )
            current_section = _WireSection(
                block_id=section_id,
                kind=kind,
                raw_lines=[raw_line],
                error_code=None if kind is not None else "synthesis_unknown_section_kind",
            )
            current_group = "section"
            recognized = True
            continue

        if header_value.casefold() in {"end claim", "end item"}:
            if current_claim is not None:
                current_claim.raw_lines.append(raw_line)
            finish_claim()
            recognized = True
            continue
        if header_value.casefold() == "end section":
            finish_claim()
            if current_section is not None:
                current_section.raw_lines.append(raw_line)
            finish_section()
            current_group = None
            recognized = True
            continue

        claim_match = _SYNTHESIS_CLAIM_HEADER.fullmatch(header_value)
        if claim_match is not None:
            finish_claim()
            sequence += 1
            block_id = claim_match.group(1) or f"C{sequence:03d}"
            raw_claim = [raw_line]
            if block_id in seen_ids:
                error_code = "synthesis_duplicate_local_block_id"
            else:
                error_code = None
                seen_ids.add(block_id)
            claim_group = current_group or "orphan"
            current_claim = _WireClaim(
                block_id=block_id,
                group=claim_group,
                section=current_section if claim_group == "section" else None,
                raw_lines=raw_claim,
                error_code=error_code,
            )
            if current_group is None:
                current_claim.error_code = "synthesis_claim_outside_group"
            recognized = True
            continue

        field_match = _SYNTHESIS_FIELD.fullmatch(line) or _SYNTHESIS_FIELD_BARE.fullmatch(line)
        if field_match is not None:
            field_name = re.sub(r"\s+", " ", field_match.group(1).casefold()).strip()
            value = field_match.group(2)
            if current_claim is not None:
                current_claim.raw_lines.append(raw_line)
                if field_name.startswith("evidence") or field_name == "handles":
                    if current_claim.evidence_handles is not None:
                        current_claim.error_code = "synthesis_claim_duplicate_evidence_field"
                    else:
                        current_claim.evidence_handles = _wire_evidence_handles(value)
                        if current_claim.evidence_handles is None:
                            current_claim.error_code = "synthesis_claim_invalid_evidence_handles"
                elif field_name == "text":
                    if current_claim.text_started:
                        current_claim.error_code = "synthesis_claim_duplicate_text_field"
                    else:
                        current_claim.text_started = True
                        current_claim.text_lines.append(value)
                else:
                    current_claim.error_code = "synthesis_claim_unknown_field"
                recognized = True
                continue
            if current_section is not None and field_name == "heading":
                current_section.raw_lines.append(raw_line)
                current_section.heading_present = True
                recognized = True
                continue
            if current_group == "diagnostics":
                diagnostic = re.fullmatch(
                    r"(?:MISSING\s+COVERAGE|DIAGNOSTIC)\s*:\s*(.+)",
                    line,
                    re.IGNORECASE,
                )
                if diagnostic is not None:
                    text = diagnostic.group(1).strip()
                    if text:
                        diagnostics.append(text)
                recognized = True
                continue
            if current_section is not None:
                current_section.raw_lines.append(raw_line)
            recognized = True
            continue

        if current_claim is not None:
            current_claim.raw_lines.append(raw_line)
            if current_claim.text_started:
                current_claim.text_lines.append(raw_line.strip())
            else:
                current_claim.error_code = (
                    current_claim.error_code or "synthesis_claim_unknown_field"
                )
        elif current_section is not None:
            current_section.raw_lines.append(raw_line)
        elif current_group == "diagnostics":
            diagnostic = re.fullmatch(
                r"(?:MISSING\s+COVERAGE|DIAGNOSTIC)\s*:\s*(.+)",
                line,
                re.IGNORECASE,
            )
            if diagnostic is not None:
                text = diagnostic.group(1).strip()
                if text:
                    diagnostics.append(text)
            recognized = True

    finish_claim()
    finish_section()

    if explicit_empty and not lead and not sections and not rejected:
        return SynthesisWireParseResult(
            proposal=SynthesisProposalV1(lead=(), sections=()),
            explicit_empty=True,
            transformations=transformations,
            warnings=tuple(warnings),
            diagnostics=tuple(diagnostics),
        )
    if not recognized:
        return SynthesisWireParseResult(
            None,
            tuple(rejected),
            error_code="synthesis_unintelligible_response",
            transformations=transformations,
            warnings=tuple(warnings),
            diagnostics=tuple(diagnostics),
        )
    if not lead:
        rejected.append(_wire_rejection("LEAD", "synthesis_lead_missing", lines))
        return SynthesisWireParseResult(
            None,
            tuple(rejected),
            error_code="synthesis_lead_missing",
            transformations=transformations,
            warnings=tuple(warnings),
            diagnostics=tuple(diagnostics),
        )
    try:
        proposal = SynthesisProposalV1(lead=tuple(lead), sections=tuple(sections))
    except ValueError:
        return SynthesisWireParseResult(
            None,
            tuple(rejected),
            error_code="synthesis_proposal_schema_invalid",
            transformations=transformations,
            warnings=tuple(warnings),
            diagnostics=tuple(diagnostics),
        )
    return SynthesisWireParseResult(
        proposal,
        tuple(rejected),
        transformations=transformations,
        warnings=tuple(warnings),
        diagnostics=tuple(diagnostics),
    )


@dataclass(frozen=True, slots=True)
class SynthesisAccessSourceV1:
    source_document_id: UUID
    tlp: TLP
    external_llm_allowed: bool
    do_not_submit: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID) or not isinstance(self.tlp, TLP):
            raise ValueError("Synthesis source access identity and TLP must be valid")
        if type(self.external_llm_allowed) is not bool or type(self.do_not_submit) is not bool:
            raise ValueError("Synthesis source access flags must be booleans")


def _access_policy_digest(
    subject_tlp: TLP,
    sources: tuple[SynthesisAccessSourceV1, ...],
    policy_version: str,
) -> str:
    payload = {
        "policy_version": policy_version,
        "subject_tlp": subject_tlp.value,
        "sources": [
            {
                "source_document_id": str(source.source_document_id),
                "tlp": source.tlp.value,
                "external_llm_allowed": source.external_llm_allowed,
                "do_not_submit": source.do_not_submit,
            }
            for source in sources
        ],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class SynthesisAccessPolicyV1:
    subject_tlp: TLP
    effective_tlp: TLP
    external_llm_allowed: bool
    do_not_submit: bool
    sources: tuple[SynthesisAccessSourceV1, ...]
    policy_version: str = SYNTHESIS_ACCESS_POLICY_VERSION
    synthesis_access_policy_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.subject_tlp, TLP) or not isinstance(self.effective_tlp, TLP):
            raise ValueError("Synthesis access policy TLP values are invalid")
        if type(self.external_llm_allowed) is not bool or type(self.do_not_submit) is not bool:
            raise ValueError("Synthesis access policy flags must be booleans")
        if self.policy_version != SYNTHESIS_ACCESS_POLICY_VERSION:
            raise ValueError("Synthesis access policy version is incompatible")
        if any(not isinstance(item, SynthesisAccessSourceV1) for item in self.sources):
            raise ValueError("Synthesis access policy source records are invalid")
        sources = tuple(sorted(self.sources, key=lambda item: str(item.source_document_id)))
        if len({item.source_document_id for item in sources}) != len(sources):
            raise ValueError("Synthesis access policy repeats a source document")
        tlps = (self.subject_tlp, *(item.tlp for item in sources))
        expected_tlp = max(tlps, key=lambda item: tuple(TLP).index(item))
        expected_external = all(item.external_llm_allowed for item in sources)
        expected_do_not_submit = any(item.do_not_submit for item in sources)
        if (
            self.effective_tlp is not expected_tlp
            or self.external_llm_allowed is not expected_external
            or self.do_not_submit is not expected_do_not_submit
        ):
            raise ValueError("Synthesis access policy does not conservatively fold its members")
        object.__setattr__(self, "sources", sources)
        expected_hash = _access_policy_digest(self.subject_tlp, sources, self.policy_version)
        if self.synthesis_access_policy_hash and self.synthesis_access_policy_hash != expected_hash:
            raise ValueError("Synthesis access policy hash is invalid")
        object.__setattr__(self, "synthesis_access_policy_hash", expected_hash)


_PROPOSAL_KEYS = frozenset({"lead", "sections"})
_CLAIM_PROPOSAL_KEYS = frozenset({"text", "evidence_handles"})
_SECTION_PROPOSAL_KEYS = frozenset({"kind", "heading", "claims"})
_TECHNICAL_SECTION_KINDS = frozenset(
    {
        SynthesisSectionKind.TECHNICAL,
        SynthesisSectionKind.INFRASTRUCTURE,
        SynthesisSectionKind.DETECTION,
    }
)
_MARKDOWN_OR_HTML = re.compile(
    r"(?:^\s{0,3}#{1,6}(?:\s|$)|^\s*(?:[-*+]\s+|\d+[.)]\s+)|"
    r"^\s*>|^\s*```|`|\[[^\]]+\]\([^)]*\)|\[[^\]]+\]\[[^\]]*\]|"
    r"<\s*/?\s*[A-Za-z][^>]*>|<!--|\*\*|__|(?<!\w)\*(?=\S)|(?<!\w)_(?=\S))",
    re.MULTILINE,
)
_MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$",
    re.MULTILINE,
)
_MARKDOWN_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_SOURCE_MARKER = re.compile(
    r"(?i)(?:\[(?:s\d+|[er]\d+|\d+|source\s*\d+|ref(?:erence)?\s*\d+)\]|"
    r"\((?:s\d+|source\s*\d+)\)|\b[ER]\d{3,}\b)"
)

_CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
_ATTACK_ID = re.compile(r"\b(?:TA|T|G|S|C)\d{4}(?:\.\d{3})?\b", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]+"
    r"(?:%[A-Za-z0-9_.-]+)?(?![0-9A-Fa-f:])"
)
_URL = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
_DOMAIN = re.compile(
    r"(?i)(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,}|xn--[a-z0-9-]{2,})(?![\w-])"
)
_EMAIL = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,}(?![\w.-])"
)
_HASH = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{128})(?![0-9a-f])"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{4}\b")
_MONTH_NAMES = {
    "january": 1,
    "janvier": 1,
    "february": 2,
    "février": 2,
    "fevrier": 2,
    "march": 3,
    "mars": 3,
    "april": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "june": 6,
    "juin": 6,
    "july": 7,
    "juillet": 7,
    "august": 8,
    "août": 8,
    "aout": 8,
    "september": 9,
    "septembre": 9,
    "october": 10,
    "octobre": 10,
    "november": 11,
    "novembre": 11,
    "december": 12,
    "décembre": 12,
    "decembre": 12,
}
_MONTH_DATE = re.compile(
    r"(?i)\b(?:\d{1,2}\s+(?:"
    + "|".join(map(re.escape, _MONTH_NAMES))
    + r")\s+\d{4}|(?:"
    + "|".join(map(re.escape, _MONTH_NAMES))
    + r")\s+\d{1,2},?\s+\d{4})\b"
)

SYNTHESIS_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "language",
        "mode",
        "section_count",
        "paragraph_count",
        "timeline_entry_count",
        "evidence_ref_count",
        "uncertainty_count",
        "warnings_count",
        "word_count",
        "model_policy_version",
        "routing_policy_version",
        "synthesis_policy_version",
        "diagnostics",
        # Exact reuse requires an identical input hash, which covers the relevance
        # projection; assembly still checks this lineage on the cloned artifact.
        "relevance_projection_hash",
    }
)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_extraction_hash(extraction: ProductionExtractionV1) -> str:
    """Hash the canonical extraction serialization with stable JSON encoding."""
    encoded = _canonical_json_bytes(production_extraction_to_json(extraction))
    return hashlib.sha256(encoded).hexdigest()


def synthesis_access_policy_hash(policy: SynthesisAccessPolicyV1) -> str:
    """Return the deterministic digest for the effective source access policy."""
    if not isinstance(policy, SynthesisAccessPolicyV1):
        raise ValueError("Expected a SynthesisAccessPolicyV1")
    return policy.synthesis_access_policy_hash


async def build_synthesis_access_policy(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    source_documents: SourceDocumentRepository,
    source_collections: SourceCollectionRepository | None = None,
) -> SynthesisAccessPolicyV1:
    """Load exact source metadata and conservatively fold its model access policy."""
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Synthesis snapshot and extraction subjects differ")
    source_ids = tuple(
        sorted((source.source_document_id for source in extraction.sources), key=str)
    )
    records: list[SynthesisAccessSourceV1] = []
    for source_id in source_ids:
        document = await source_documents.get(source_id)
        if document is None:
            raise ValueError("synthesis_access_policy_unavailable")
        try:
            document_id = document.id
            subject_id = document.subject_id
            collection_id = document.source_collection_id
        except AttributeError as exc:
            raise ValueError("synthesis_access_policy_unavailable") from exc
        if document_id != source_id or subject_id != snapshot.subject_id:
            raise ValueError("synthesis_access_policy_unavailable")
        collection = None
        if collection_id is not None:
            if source_collections is None:
                raise ValueError("synthesis_access_policy_unavailable")
            collection = await source_collections.get(collection_id)
            if (
                collection is None
                or collection.id != collection_id
                or collection.subject_id != snapshot.subject_id
            ):
                raise ValueError("synthesis_access_policy_unavailable")
        try:
            policy = resolve_source_policy(document, collection)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("synthesis_access_policy_unavailable") from exc
        if (
            not isinstance(policy.tlp, TLP)
            or type(policy.external_llm_allowed) is not bool
            or type(policy.do_not_submit) is not bool
        ):
            raise ValueError("synthesis_access_policy_unavailable")
        records.append(
            SynthesisAccessSourceV1(
                source_document_id=source_id,
                tlp=policy.tlp,
                external_llm_allowed=policy.external_llm_allowed,
                do_not_submit=policy.do_not_submit,
            )
        )

    tlps = (snapshot.subject_tlp, *(record.tlp for record in records))
    return SynthesisAccessPolicyV1(
        subject_tlp=snapshot.subject_tlp,
        effective_tlp=max(tlps, key=lambda item: tuple(TLP).index(item)),
        external_llm_allowed=all(record.external_llm_allowed for record in records),
        do_not_submit=any(record.do_not_submit for record in records),
        sources=tuple(records),
    )


def _synthesis_identity_payload(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy_hash: str,
    *,
    prompt_version: str | None = None,
    validator_version: str | None = None,
    model_policy_version: str | None = None,
    routing_policy_version: str | None = None,
) -> dict[str, Any]:
    """Return stable request inputs, excluding parser-only implementation state."""
    if not re.fullmatch(r"[0-9a-f]{64}", access_policy_hash):
        raise ValueError("Synthesis access policy hash must be a lowercase SHA-256")
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Synthesis snapshot and extraction subjects differ")
    if evidence_pack.publication_language != snapshot.publication_language:
        raise ValueError("Synthesis evidence pack language differs from its snapshot")
    payload = {
        "snapshot_input_hash": snapshot.input_hash,
        "extraction_hash": canonical_extraction_hash(extraction),
        "relevance_projection_hash": evidence_pack.projection_hash,
        "publication_language": snapshot.publication_language,
        "extraction_schema_version": PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        "evidence_pack_schema_version": SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION,
        "evidence_pack_hash": synthesis_evidence_pack_hash(evidence_pack),
        "evidence_pack_policy_version": evidence_pack.policy_version,
        "timeline_policy_version": SYNTHESIS_TIMELINE_POLICY_VERSION,
        "proposal_contract_version": SYNTHESIS_PROPOSAL_CONTRACT_VERSION,
        "canonical_schema_version": PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        "prompt_version": prompt_version or SYNTHESIS_PROMPT_VERSION,
        "validator_version": validator_version or SYNTHESIS_VALIDATOR_VERSION,
        "evidence_ref_algorithm_version": SYNTHESIS_EVIDENCE_REF_ALGORITHM_VERSION,
        "synthesis_policy_version": SYNTHESIS_POLICY_VERSION,
        "access_policy_hash": access_policy_hash,
        "model_policy_version": model_policy_version or SYNTHESIS_MODEL_POLICY_VERSION,
        "routing_policy_version": routing_policy_version or SYNTHESIS_ROUTING_POLICY_VERSION,
    }
    return payload


def synthesis_invocation_hash(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy_hash: str,
    *,
    prompt_version: str | None = None,
    validator_version: str | None = None,
    model_policy_version: str | None = None,
    routing_policy_version: str | None = None,
) -> str:
    """Identity of the request that may cause a provider submission."""
    payload = _synthesis_identity_payload(
        snapshot,
        extraction,
        evidence_pack,
        access_policy_hash,
        prompt_version=prompt_version,
        validator_version=validator_version,
        model_policy_version=model_policy_version,
        routing_policy_version=routing_policy_version,
    )
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def synthesis_input_hash(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy_hash: str,
    *,
    prompt_version: str | None = None,
    validator_version: str | None = None,
    model_policy_version: str | None = None,
    routing_policy_version: str | None = None,
    parser_version: str | None = None,
) -> str:
    """Identity of a parsed canonical synthesis, including parser behavior."""
    payload = _synthesis_identity_payload(
        snapshot,
        extraction,
        evidence_pack,
        access_policy_hash,
        prompt_version=prompt_version,
        validator_version=validator_version,
        model_policy_version=model_policy_version,
        routing_policy_version=routing_policy_version,
    )
    payload["parser_version"] = parser_version or SYNTHESIS_WIRE_PARSER_VERSION
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def synthesis_model_run_id(run: ProductionRun, invocation_hash: str, mode: SynthesisMode) -> UUID:
    """Derive a stable provider ModelRun identity for one run generation/mode."""
    if not isinstance(run, ProductionRun) or not isinstance(mode, SynthesisMode):
        raise ValueError("Synthesis ModelRun identity inputs are invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", invocation_hash):
        raise ValueError("Synthesis invocation hash must be a lowercase SHA-256")
    identity = ":".join(
        (
            "production-synthesis-model-run-v1",
            str(run.id),
            str(run.pipeline_generation),
            invocation_hash,
            mode.value,
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def _all_evidence_entries(
    extraction: ProductionExtractionV1,
) -> dict[ExtractionEvidenceRefV1, Mapping[str, Any]]:
    """Map each canonical evidence ref to its element payload."""
    entries: dict[ExtractionEvidenceRefV1, Mapping[str, Any]] = {}
    for ref, payload in extraction_evidence_elements(extraction):
        entries.setdefault(ref, payload)
    return entries


def _prompt_evidence_record(
    handle: str,
    kind: EvidenceKind,
    payload: Mapping[str, Any],
    *,
    source: Any,
) -> dict[str, Any]:
    """Project source-local extraction evidence without internal document IDs."""
    if kind is EvidenceKind.FACT:
        return {
            "handle": handle,
            "kind": kind.value,
            "source_role": source.role.value,
            "editorial_role": source.editorial_role.value,
            "category": payload["category"],
            "value": payload["value"],
            "attack_id": payload["attack_id"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    if kind is EvidenceKind.EVENT:
        return {
            "handle": handle,
            "kind": kind.value,
            "source_role": source.role.value,
            "editorial_role": source.editorial_role.value,
            "event_date": payload["event_date"],
            "date_text": payload["date_text"],
            "text": payload["text"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    if kind is EvidenceKind.INDICATOR:
        return {
            "handle": handle,
            "kind": kind.value,
            "source_role": source.role.value,
            "editorial_role": source.editorial_role.value,
            "value": payload["value"],
            "type": payload["artifact_type"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    if kind is EvidenceKind.UNCERTAINTY:
        return {
            "handle": handle,
            "kind": kind.value,
            "source_role": source.role.value,
            "editorial_role": source.editorial_role.value,
            "text": payload["text"],
        }
    return {
        "handle": handle,
        "kind": kind.value,
        "source_role": source.role.value,
        "editorial_role": source.editorial_role.value,
        "type": payload["rule_type"],
        "name": payload["name"],
        "sha256": payload["sha256"],
        "context": payload["context"],
        "evidence": payload["evidence_quote"],
    }


@dataclass(frozen=True, slots=True)
class SynthesisEvidencePackV1:
    subject_title: str
    publication_language: str
    discovery_summary: str
    actor_or_campaign: str
    period_start: date
    period_end: date
    narrative_evidence: tuple[Mapping[str, Any], ...]
    technical_evidence: tuple[Mapping[str, Any], ...]
    reserve_evidence: tuple[Mapping[str, Any], ...]
    source_pair_relations: tuple[Mapping[str, Any], ...]
    uncertainties: tuple[str, ...]
    projection_hash: str | None = None
    policy_version: str = SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION
    _handle_to_ref: Mapping[str, ExtractionEvidenceRefV1] = field(
        default_factory=dict, repr=False, compare=False
    )
    _handle_for_ref: Mapping[ExtractionEvidenceRefV1, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    def resolve_handle(self, handle: str) -> ExtractionEvidenceRefV1:
        """Resolve only an exact prompt handle; no fuzzy or prefix matching."""
        try:
            return self._handle_to_ref[handle]
        except KeyError as exc:
            raise ValueError("synthesis_unknown_evidence") from exc

    def handle_for(self, ref: ExtractionEvidenceRefV1) -> str | None:
        """Return the temporary prompt handle of a ref, or ``None`` when absent.

        A ref that left the current extraction has no handle: revision context
        can never hand a previous, removed evidence identity back to the model.
        """
        return self._handle_for_ref.get(ref)


def build_synthesis_evidence_pack(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    projection: RelevanceProjectionV1 | None = None,
) -> SynthesisEvidencePackV1:
    """Build the bounded prompt projection and its private handle resolver."""
    if not isinstance(snapshot, ProductionInputSnapshot):
        raise ValueError("Expected a ProductionInputSnapshot")
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Synthesis snapshot and extraction subjects differ")
    if projection is not None:
        validate_relevance_projection_lineage(
            projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
        )
        if projection.production_input_hash != snapshot.input_hash:
            raise ValueError("Synthesis relevance projection does not match its snapshot")

    entries = _all_evidence_entries(extraction)
    source_by_id = {source.source_document_id: source for source in extraction.sources}
    uncertainties = build_synthesis_uncertainties(extraction, projection=projection)
    ranked_uncertainty_keys = {
        (source_id, _normalized_synthesis_text(item.text))
        for item in uncertainties
        for source_id in item.source_document_ids
    }
    uncertainty_refs = {
        ref
        for ref, payload in entries.items()
        if ref.kind is EvidenceKind.UNCERTAINTY
        and (ref.source_document_id, _normalized_synthesis_text(str(payload["text"])))
        in ranked_uncertainty_keys
    }

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
        if (
            (
                ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
                and source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
                and admitted(ref)
                and ref not in counter_refs
            )
            or ref in uncertainty_refs
        )
    }
    technical_candidates = [
        ref
        for ref, payload in entries.items()
        if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
        and admitted(ref)
        and ref not in counter_refs
        and (str(payload["context"]).strip() or str(payload["evidence_quote"]).strip())
    ]
    technical_refs = set(
        sorted(technical_candidates, key=evidence_ref_sort_key)[:MAX_TECHNICAL_EVIDENCE_V1]
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

    catalogue_refs = sorted(
        narrative_refs | technical_refs,
        key=lambda ref: (
            0 if ref in narrative_refs else 1,
            authority_key(ref),
            evidence_ref_sort_key(ref),
        ),
    )
    handle_to_ref = {f"E{index:03d}": ref for index, ref in enumerate(catalogue_refs, start=1)}
    handle_for_ref = {ref: handle for handle, ref in handle_to_ref.items()}
    reserve_catalogue_refs = sorted(
        reserve_refs,
        key=lambda ref: (authority_key(ref), evidence_ref_sort_key(ref)),
    )
    reserve_handle_for_ref = {
        ref: f"R{index:03d}" for index, ref in enumerate(reserve_catalogue_refs, start=1)
    }
    handle_to_ref.update({handle: ref for ref, handle in reserve_handle_for_ref.items()})

    narrative_evidence = tuple(
        _prompt_evidence_record(
            handle_for_ref[ref],
            ref.kind,
            entries[ref],
            source=source_by_id[ref.source_document_id],
        )
        for ref in catalogue_refs
        if ref in narrative_refs
    )
    technical_evidence = tuple(
        _prompt_evidence_record(
            handle_for_ref[ref],
            ref.kind,
            entries[ref],
            source=source_by_id[ref.source_document_id],
        )
        for ref in catalogue_refs
        if ref in technical_refs
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
        for ref in reserve_catalogue_refs
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
    return SynthesisEvidencePackV1(
        subject_title=snapshot.subject_title,
        publication_language=snapshot.publication_language,
        discovery_summary=snapshot.discovery_summary,
        actor_or_campaign=snapshot.actor_or_campaign,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        narrative_evidence=narrative_evidence,
        technical_evidence=technical_evidence,
        reserve_evidence=reserve_evidence,
        source_pair_relations=source_pair_relations,
        uncertainties=tuple(item.text for item in uncertainties),
        projection_hash=projection.projection_hash if projection is not None else None,
        _handle_to_ref=MappingProxyType(handle_to_ref),
        _handle_for_ref=MappingProxyType(dict(handle_for_ref)),
    )


def synthesis_evidence_pack_hash(evidence_pack: SynthesisEvidencePackV1) -> str:
    """Hash the complete deterministic evidence payload sent to the drafter."""
    if not isinstance(evidence_pack, SynthesisEvidencePackV1):
        raise ValueError("Expected a SynthesisEvidencePackV1")
    payload = {
        "schema_version": SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION,
        "policy_version": evidence_pack.policy_version,
        "subject_title": evidence_pack.subject_title,
        "publication_language": evidence_pack.publication_language,
        "discovery_summary": evidence_pack.discovery_summary,
        "actor_or_campaign": evidence_pack.actor_or_campaign,
        "period_start": evidence_pack.period_start.isoformat(),
        "period_end": evidence_pack.period_end.isoformat(),
        "narrative_evidence": [dict(record) for record in evidence_pack.narrative_evidence],
        "technical_evidence": [dict(record) for record in evidence_pack.technical_evidence],
        "reserve_evidence": [dict(record) for record in evidence_pack.reserve_evidence],
        "source_pair_relations": [dict(record) for record in evidence_pack.source_pair_relations],
        "uncertainties": list(evidence_pack.uncertainties),
        "relevance_projection_hash": evidence_pack.projection_hash,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def synthesis_parse_identity(
    raw_output_sha256: str,
    evidence_pack: SynthesisEvidencePackV1,
    *,
    prompt_version: str | None = None,
    contract_version: str | None = None,
    parser_version: str | None = None,
) -> str:
    """Bind a parse to verified bytes, the contract, and exact request handles."""
    if not re.fullmatch(r"[0-9a-f]{64}", raw_output_sha256):
        raise ValueError("Raw synthesis output hash must be a lowercase SHA-256")
    handle_mapping = [
        {"handle": handle, "evidence_ref": repr(ref)}
        for handle, ref in sorted(evidence_pack._handle_to_ref.items())
    ]
    payload = {
        "raw_output_sha256": raw_output_sha256,
        "parser_version": parser_version or SYNTHESIS_WIRE_PARSER_VERSION,
        "contract_version": contract_version or SYNTHESIS_PROPOSAL_CONTRACT_VERSION,
        "prompt_version": prompt_version or SYNTHESIS_PROMPT_VERSION,
        "request_handle_mapping": handle_mapping,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _render_evidence_record(record: Mapping[str, Any]) -> list[str]:
    handle = str(record["handle"])
    lines = [f"@@EVIDENCE {handle}@@"]
    for key, value in record.items():
        if key == "handle" or value is None:
            continue
        lines.extend((f"{key.upper()}:", str(value)))
    lines.append("@@END EVIDENCE@@")
    return lines


def _render_revision_context(
    revision: SynthesisRevisionContextV1, evidence_pack: SynthesisEvidencePackV1
) -> list[str]:
    payload = build_synthesis_revision_payload(revision, evidence_pack)
    previous = payload["previous_synthesis_non_authoritative"]
    lines = ["@@PREVIOUS SYNTHESIS: NON-AUTHORITATIVE@@"]
    if not isinstance(previous, Mapping):
        return lines
    for key in ("non_authoritative", "publication_language", "title"):
        lines.extend((f"{key.upper()}:", str(previous.get(key, ""))))
    for group in ("lead", "sections"):
        values = previous.get(group, [])
        if not isinstance(values, list):
            continue
        lines.append(f"{group.upper()}:")
        for index, item in enumerate(values, start=1):
            if not isinstance(item, Mapping):
                continue
            lines.append(f"@@PREVIOUS {group.upper()} P{index:03d}@@")
            if group == "sections":
                lines.extend(
                    (
                        f"KIND: {item.get('kind', '')}",
                        f"HEADING: {item.get('heading', '')}",
                    )
                )
            claims = item.get("claims", []) if group == "sections" else [item]
            if isinstance(claims, list):
                for claim_index, claim in enumerate(claims, start=1):
                    if not isinstance(claim, Mapping):
                        continue
                    lines.append(f"PREVIOUS CLAIM P{index:03d}.{claim_index:03d}:")
                    lines.append(f"TEXT: {claim.get('text', '')}")
                    handles = claim.get("evidence_handles", [])
                    if isinstance(handles, list):
                        lines.append(f"CURRENT HANDLES: {', '.join(str(item) for item in handles)}")
                    lines.append(
                        f"UNSUPPORTED EVIDENCE COUNT: {claim.get('unsupported_evidence_count', 0)}"
                    )
    timeline = previous.get("timeline", [])
    if isinstance(timeline, list) and timeline:
        lines.append("PREVIOUS TIMELINE:")
        for index, event in enumerate(timeline, start=1):
            if not isinstance(event, Mapping):
                continue
            lines.append(f"@@PREVIOUS EVENT T{index:03d}@@")
            for key in ("event_date", "date_text", "text"):
                value = event.get(key)
                if value is not None:
                    lines.extend((f"{key.upper()}:", str(value)))
    uncertainties = previous.get("uncertainties", [])
    if isinstance(uncertainties, list) and uncertainties:
        lines.append("PREVIOUS UNCERTAINTIES:")
        lines.extend(str(value) for value in uncertainties)
    delta = payload.get("evidence_delta")
    if isinstance(delta, Mapping):
        lines.append("CURRENT EVIDENCE DELTA:")
        for key, value in delta.items():
            if isinstance(value, Mapping):
                for field_name, field_value in value.items():
                    if isinstance(field_value, list):
                        rendered = ", ".join(str(item) for item in field_value)
                    else:
                        rendered = str(field_value)
                    lines.append(f"{key.upper()} {field_name.upper()}: {rendered}")
    return lines


def build_synthesis_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy: SynthesisAccessPolicyV1,
    mode: SynthesisMode,
    *,
    revision: SynthesisRevisionContextV1 | None = None,
) -> ModelRequest:
    """Build a complete stateless, web-disabled Synthesis drafting request."""
    if not isinstance(mode, SynthesisMode):
        raise ValueError("Synthesis request mode is invalid")
    if revision is not None and mode is not SynthesisMode.REVISE_PREVIOUS:
        raise ValueError("Synthesis revision context requires REVISE_PREVIOUS mode")
    if revision is None and mode is SynthesisMode.REVISE_PREVIOUS:
        raise ValueError("REVISE_PREVIOUS mode requires a canonical previous synthesis")
    if revision is not None:
        if revision.previous_synthesis.subject_id != snapshot.subject_id:
            raise ValueError("Synthesis revision context belongs to another Subject")
        if revision.previous_extraction.subject_id != snapshot.subject_id:
            raise ValueError("Synthesis revision context extraction belongs to another Subject")
    if (
        run.id != snapshot.production_run_id
        or run.subject_id != snapshot.subject_id
        or extraction.subject_id != snapshot.subject_id
    ):
        raise ValueError("Synthesis run, snapshot and extraction identities differ")
    if access_policy.subject_tlp is not snapshot.subject_tlp:
        raise ValueError("Synthesis access policy differs from the frozen subject TLP")
    expected_source_ids = tuple(
        sorted((source.source_document_id for source in extraction.sources), key=str)
    )
    if tuple(record.source_document_id for record in access_policy.sources) != expected_source_ids:
        raise ValueError("Synthesis access policy does not cover exact extraction sources")
    if (
        evidence_pack.subject_title != snapshot.subject_title
        or evidence_pack.publication_language != snapshot.publication_language
        or evidence_pack.discovery_summary != snapshot.discovery_summary
        or evidence_pack.actor_or_campaign != snapshot.actor_or_campaign
        or evidence_pack.period_start != snapshot.period_start
        or evidence_pack.period_end != snapshot.period_end
    ):
        raise ValueError("Synthesis evidence pack differs from its frozen snapshot")

    policy_hash = synthesis_access_policy_hash(access_policy)
    functional_hash = synthesis_input_hash(snapshot, extraction, evidence_pack, policy_hash)
    invocation_hash = synthesis_invocation_hash(snapshot, extraction, evidence_pack, policy_hash)
    pack_hash = synthesis_evidence_pack_hash(evidence_pack)
    prompt_lines = [
        "SYNTHESIS DRAFT REQUEST",
        f"Prompt version: {SYNTHESIS_PROMPT_VERSION}",
        f"Contract version: {SYNTHESIS_PROPOSAL_CONTRACT_VERSION}",
        f"Publication language: {snapshot.publication_language}",
        f"Subject title: {snapshot.subject_title}",
        f"TLP: {snapshot.subject_tlp.value}",
        f"Discovery summary: {snapshot.discovery_summary}",
        f"Actor or campaign: {snapshot.actor_or_campaign}",
        f"Period: {snapshot.period_start.isoformat()} to {snapshot.period_end.isoformat()}",
        "",
        *SYNTHESIS_EDITORIAL_CONTRACT_V4.strip().splitlines(),
        f"Allowed section kinds: {', '.join(kind.value for kind in SynthesisSectionKind)}",
        "If no narrative claim can be supported, return only @@EMPTY@@.",
        "",
        "CURRENT EVIDENCE PACK",
    ]
    for record in (*evidence_pack.narrative_evidence, *evidence_pack.technical_evidence):
        if record.get("kind") == EvidenceKind.UNCERTAINTY.value:
            continue
        prompt_lines.extend(_render_evidence_record(record))
        prompt_lines.append("")
    if evidence_pack.reserve_evidence or evidence_pack.source_pair_relations:
        prompt_lines.extend(
            (
                "@@RESERVES / CONTRADICTIONS — NON-AUTHORITATIVE CONTEXT@@",
                "These R handles preserve qualifications and source-pair analysis.",
                "They are not claim evidence handles and must not be presented as confirmed facts.",
            )
        )
        for record in evidence_pack.reserve_evidence:
            prompt_lines.extend(_render_evidence_record(record))
            prompt_lines.append("")
        for relation in evidence_pack.source_pair_relations:
            prompt_lines.append(
                "SOURCE PAIR RELATION: "
                f"{relation['relation']} — {relation['reason']} "
                f"(handles: {', '.join(relation['supporting_handles'])})"
            )
        prompt_lines.append("@@END RESERVES / CONTRADICTIONS@@")
    uncertainty_records = tuple(
        record
        for uncertainty in evidence_pack.uncertainties
        for record in evidence_pack.narrative_evidence
        if record.get("kind") == EvidenceKind.UNCERTAINTY.value
        and _normalized_synthesis_text(str(record.get("text", "")))
        == _normalized_synthesis_text(uncertainty)
    )
    if uncertainty_records:
        prompt_lines.extend(
            (
                "@@PROJECTED / RANKED ANALYTICAL UNCERTAINTIES — CONTEXT FOR CLOSING PROSE@@",
                "Use these source-grounded, projected items to shape the final analytic "
                "paragraphs.",
                "Cite their exact E handles when the paragraph relies on them; do not reproduce",
                "this context as a list in the synthesis.",
            )
        )
        for record in uncertainty_records:
            prompt_lines.extend(_render_evidence_record(record))
            prompt_lines.append("")
        prompt_lines.append("@@END PROJECTED / RANKED UNCERTAINTIES@@")
    if revision is not None:
        prompt_lines.extend(("", *_render_revision_context(revision, evidence_pack)))
    prompt = "\n".join(prompt_lines).strip()
    if any(str(record.source_document_id) in prompt for record in access_policy.sources):
        raise ValueError("Source document identities cannot appear in the Synthesis prompt")

    return ModelRequest(
        text=prompt,
        prompt_template_id="production-synthesis",
        prompt_template_version=SYNTHESIS_PROMPT_VERSION,
        evidence_pack_hash=pack_hash,
        external_llm_allowed=(
            access_policy.external_llm_allowed and not access_policy.do_not_submit
        ),
        routing_hint=ModelRoutingHint.PREMIUM_SYNTHESIS,
        sensitivity=access_policy.effective_tlp.value,
        metadata={
            "synthesis_input_hash": functional_hash,
            "synthesis_invocation_hash": invocation_hash,
            "synthesis_mode": mode.value,
            "synthesis_access_policy_hash": policy_hash,
            "effective_tlp": access_policy.effective_tlp.value,
            "external_llm_allowed": access_policy.external_llm_allowed,
            "do_not_submit": access_policy.do_not_submit,
            "model_policy_version": SYNTHESIS_MODEL_POLICY_VERSION,
            "routing_policy_version": SYNTHESIS_ROUTING_POLICY_VERSION,
            "synthesis_policy_version": SYNTHESIS_POLICY_VERSION,
        },
        web_search=False,
        background=False,
        conversation=None,
        run_id=synthesis_model_run_id(run, invocation_hash, mode),
        # The gateway only resubmits a FAILED run whose submission state
        # proves the provider was never reached.
        allow_failed_resubmit=True,
    )


async def draft_synthesis_proposal(
    model_gateway: ModelGateway, request: ModelRequest
) -> ModelExecution:
    """Submit one stateless text draft through the provider-agnostic gateway."""
    return await model_gateway.draft(request)


def _invalid_proposal() -> None:
    raise SynthesisProposalControlError(SynthesisProposalErrorCode.OUTPUT_INVALID)


def _strict_mapping(value: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} fields do not match the strict schema")
    return value


def _claim_proposal_from_payload(value: Any) -> SynthesisClaimProposalV1:
    payload = _strict_mapping(value, _CLAIM_PROPOSAL_KEYS, "Claim proposal")
    handles = payload["evidence_handles"]
    if not isinstance(handles, list):
        raise ValueError("Claim evidence handles must be an array")
    return SynthesisClaimProposalV1(
        text=payload["text"],
        evidence_handles=tuple(handles),
    )


def _section_proposal_from_payload(value: Any) -> SynthesisSectionProposalV1:
    if not isinstance(value, Mapping) or not {"kind", "claims"} <= set(value):
        raise ValueError("Section proposal fields do not match the strict schema")
    if set(value) - _SECTION_PROPOSAL_KEYS:
        raise ValueError("Section proposal fields do not match the strict schema")
    payload = value
    claims = payload["claims"]
    if not isinstance(claims, list):
        raise ValueError("Section claims must be an array")
    kind_value = payload["kind"]
    if not isinstance(kind_value, str):
        raise ValueError("Section kind must be text")
    return SynthesisSectionProposalV1(
        kind=SynthesisSectionKind(kind_value),
        heading=payload.get("heading", ""),
        claims=tuple(_claim_proposal_from_payload(claim) for claim in claims),
    )


def _proposal_from_payload(value: Any) -> SynthesisProposalV1:
    if isinstance(value, SynthesisProposalV1):
        return value
    payload = _strict_mapping(value, _PROPOSAL_KEYS, "Synthesis proposal")
    lead = payload["lead"]
    sections = payload["sections"]
    if not isinstance(lead, list) or not isinstance(sections, list):
        raise ValueError("Proposal lead and sections must be arrays")
    return SynthesisProposalV1(
        lead=tuple(_claim_proposal_from_payload(claim) for claim in lead),
        sections=tuple(_section_proposal_from_payload(section) for section in sections),
    )


def _validate_plain_text(value: str) -> None:
    stripped = value.strip()
    if (
        stripped.startswith(("---", "+++"))
        or _MARKDOWN_OR_HTML.search(value)
        or _MARKDOWN_TABLE_SEPARATOR.search(value)
        or _MARKDOWN_TABLE_ROW.search(value)
        or _SOURCE_MARKER.search(value)
    ):
        _invalid_proposal()


def _trim_literal(value: str) -> str:
    return value.rstrip(".,;:!?)]}")


def _normalized_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not parsed.scheme or not host:
            return value.casefold()
        host_value = host.casefold()
        if ":" in host_value:
            host_value = f"[{host_value}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        user = ""
        if parsed.username is not None:
            user = parsed.username
            if parsed.password is not None:
                user += f":{parsed.password}"
            user += "@"
        return urlunsplit(
            (
                parsed.scheme.casefold(),
                f"{user}{host_value}{port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    except ValueError:
        return value.casefold()


def _technical_literals(text: str) -> set[tuple[str, str]]:
    literals: set[tuple[str, str]] = set()

    def add(kind: str, raw: str) -> None:
        value = _trim_literal(raw)
        if not value:
            return
        if kind == "ipv4" or kind == "ipv6":
            try:
                value = ipaddress.ip_address(value).compressed.casefold()
            except ValueError:
                return
        elif kind == "url":
            value = _normalized_url(value)
        elif kind == "domain":
            value = value.rstrip(".").casefold()
        else:
            value = value.casefold()
        literals.add((kind, value))

    for match in _URL.finditer(text):
        add("url", match.group())
    for match in _CVE.finditer(text):
        add("cve", match.group())
    for match in _ATTACK_ID.finditer(text):
        add("attack", match.group())
    for match in _IPV4.finditer(text):
        add("ipv4", match.group())
    for match in _IPV6.finditer(text):
        add("ipv6", match.group())
    for match in _EMAIL.finditer(text):
        add("email", match.group())
    for match in _DOMAIN.finditer(text):
        add("domain", match.group())
    for match in _HASH.finditer(text):
        add("hash", match.group())
    return literals


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            yield from _string_values(nested)


def _date_key(value: str) -> str:
    stripped = value.strip()
    try:
        parsed = date.fromisoformat(stripped)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.isoformat() == stripped:
        return f"date:{parsed.isoformat()}"

    normalized = re.sub(r"\s+", " ", stripped.casefold()).replace(",", "")
    month_date = re.fullmatch(r"(\d{1,2})\s+([\wéûôàèùîïç]+)\s+(\d{4})", normalized)
    if month_date is None:
        month_date = re.fullmatch(r"([\wéûôàèùîïç]+)\s+(\d{1,2})\s+(\d{4})", normalized)
        if month_date is not None:
            month_name, day_value, year_value = month_date.groups()
        else:
            return f"raw:{normalized}"
    else:
        day_value, month_name, year_value = month_date.groups()
    month = _MONTH_NAMES.get(month_name)
    if month is not None:
        try:
            return f"date:{date(int(year_value), month, int(day_value)).isoformat()}"
        except ValueError:
            pass
    return f"raw:{normalized}"


def _date_literals(text: str) -> set[str]:
    matches = [
        match.group()
        for pattern in (_ISO_DATE, _NUMERIC_DATE, _MONTH_DATE)
        for match in pattern.finditer(text)
    ]
    return {_date_key(value) for value in matches}


def _resolve_claim_refs(
    claim: SynthesisClaimProposalV1,
    evidence_pack: SynthesisEvidencePackV1,
    current_refs: set[ExtractionEvidenceRefV1],
    removed_refs: set[ExtractionEvidenceRefV1],
    removed_handles: set[str],
) -> tuple[ExtractionEvidenceRefV1, ...]:
    refs: list[ExtractionEvidenceRefV1] = []
    for handle in claim.evidence_handles:
        if handle in removed_handles:
            raise SynthesisProposalControlError(SynthesisProposalErrorCode.UNKNOWN_EVIDENCE)
        try:
            ref = evidence_pack.resolve_handle(handle)
        except (KeyError, ValueError) as exc:
            raise SynthesisProposalControlError(
                SynthesisProposalErrorCode.UNKNOWN_EVIDENCE
            ) from exc
        if ref not in current_refs or ref in removed_refs:
            raise SynthesisProposalControlError(SynthesisProposalErrorCode.UNKNOWN_EVIDENCE)
        refs.append(ref)
    if len(refs) != len(set(refs)):
        _invalid_proposal()
    return tuple(refs)


def _date_supported_by_payload(payload: Mapping[str, Any], date_key: str) -> bool:
    if "event_date" in payload:
        date_text = payload["date_text"]
        # event_date is a sort key; date_text preserves the precision asserted
        # by the source, including approximate wording such as "mid-2024".
        if isinstance(date_text, str) and date_text.strip():
            return date_key in _date_literals(date_text)
        event_date = payload["event_date"]
        return any(
            date_key in _date_literals(value)
            for value in (event_date or "", date_text or "")
            if isinstance(value, str)
        )
    if "category" not in payload:
        return False
    return any(
        date_key in _date_literals(value)
        for value in (payload.get("value"), payload.get("context"), payload.get("evidence_quote"))
        if isinstance(value, str)
    )


def _validate_grounded_text(
    text: str,
    refs: tuple[ExtractionEvidenceRefV1, ...],
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    known_technical: set[tuple[str, str]],
    technical_support: Mapping[tuple[str, str], set[ExtractionEvidenceRefV1]],
) -> None:
    ref_set = set(refs)
    for literal in _technical_literals(text):
        if literal not in known_technical:
            raise SynthesisProposalControlError(SynthesisProposalErrorCode.UNKNOWN_TECHNICAL_VALUE)
        support = technical_support.get(literal, set())
        if support and not support.intersection(ref_set):
            raise SynthesisProposalControlError(SynthesisProposalErrorCode.UNKNOWN_TECHNICAL_VALUE)
    for date_key in _date_literals(text):
        if not any(_date_supported_by_payload(entries[ref], date_key) for ref in refs):
            raise SynthesisProposalControlError(SynthesisProposalErrorCode.UNKNOWN_DATE)


def validate_synthesis_proposal(
    proposal: SynthesisProposalV1 | Mapping[str, Any],
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    *,
    removed_evidence: Iterable[ExtractionEvidenceRefV1 | str] = (),
) -> tuple[tuple[SynthesisParagraphV1, ...], tuple[SynthesisSectionV1, ...]]:
    """Validate a model proposal and convert temporary handles to canonical refs."""
    try:
        if not isinstance(evidence_pack, SynthesisEvidencePackV1):
            raise ValueError("Expected a SynthesisEvidencePackV1")
        if not isinstance(extraction, ProductionExtractionV1):
            raise ValueError("Expected a ProductionExtractionV1")
        parsed = _proposal_from_payload(proposal)
        entries = _all_evidence_entries(extraction)
        current_refs = set(entries)

        removed_refs: set[ExtractionEvidenceRefV1] = set()
        removed_handles: set[str] = set()
        for item in removed_evidence:
            if isinstance(item, ExtractionEvidenceRefV1):
                removed_refs.add(item)
            elif isinstance(item, str):
                removed_handles.add(item)
            else:
                raise ValueError("Removed evidence contains an invalid identity")

        narrative_handles = {str(record["handle"]) for record in evidence_pack.narrative_evidence}
        technical_handles = {str(record["handle"]) for record in evidence_pack.technical_evidence}
        reserve_narrative_handles = {
            str(record["handle"])
            for record in evidence_pack.reserve_evidence
            if record.get("kind")
            in {
                EvidenceKind.FACT.value,
                EvidenceKind.EVENT.value,
                EvidenceKind.UNCERTAINTY.value,
            }
        }
        reserve_technical_handles = {
            str(record["handle"])
            for record in evidence_pack.reserve_evidence
            if record.get("kind") in {EvidenceKind.INDICATOR.value, EvidenceKind.RULE.value}
        }
        narrative_refs = {evidence_pack.resolve_handle(handle) for handle in narrative_handles}
        narrative_refs.update(
            evidence_pack.resolve_handle(handle) for handle in reserve_narrative_handles
        )
        technical_refs = {evidence_pack.resolve_handle(handle) for handle in technical_handles}
        technical_refs.update(
            evidence_pack.resolve_handle(handle) for handle in reserve_technical_handles
        )

        known_technical: set[tuple[str, str]] = set()
        technical_support_mutable: dict[tuple[str, str], set[ExtractionEvidenceRefV1]] = (
            defaultdict(set)
        )
        for ref, payload in entries.items():
            for value in _string_values(payload):
                for literal in _technical_literals(value):
                    known_technical.add(literal)
                    technical_support_mutable[literal].add(ref)
        for source in extraction.sources:
            for value in (source.canonical_url, source.content_sha256):
                known_technical.update(_technical_literals(value))

        def convert_claim(
            claim: SynthesisClaimProposalV1, *, allow_technical: bool
        ) -> SynthesisParagraphV1:
            _validate_plain_text(claim.text)
            refs = _resolve_claim_refs(
                claim, evidence_pack, current_refs, removed_refs, removed_handles
            )
            for ref in refs:
                if ref not in narrative_refs and not (allow_technical and ref in technical_refs):
                    _invalid_proposal()
            _validate_grounded_text(
                claim.text,
                refs,
                entries,
                known_technical,
                technical_support_mutable,
            )
            return SynthesisParagraphV1(text=claim.text, evidence_refs=refs)

        lead = tuple(convert_claim(claim, allow_technical=False) for claim in parsed.lead)
        sections: list[SynthesisSectionV1] = []
        for section in parsed.sections:
            allow_technical = section.kind in _TECHNICAL_SECTION_KINDS
            paragraphs = tuple(
                convert_claim(claim, allow_technical=allow_technical) for claim in section.claims
            )
            sections.append(
                SynthesisSectionV1(
                    kind=section.kind,
                    heading="",
                    paragraphs=paragraphs,
                )
            )
        return lead, tuple(sections)
    except SynthesisProposalControlError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SynthesisProposalControlError(SynthesisProposalErrorCode.OUTPUT_INVALID) from exc


def _date_text_has_day_precision(value: str | None) -> bool:
    if value is None:
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", value):
        return True
    if re.search(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b", value):
        return True
    if re.search(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b", value):
        return True
    tokens = _normalized_synthesis_text(value).split()
    has_month = any(token in _MONTH_NUMBERS for token in tokens)
    has_day = any(token.isdigit() and 1 <= int(token) <= 31 for token in tokens)
    return has_month and has_day


def build_synthesis_timeline(
    extraction: ProductionExtractionV1,
    *,
    projection: RelevanceProjectionV1 | None = None,
    warnings: list[str] | None = None,
) -> tuple[SynthesisTimelineEntryV1, ...]:
    """Normalize, deduplicate and order canonical events deterministically.

    If a warning channel is supplied, events whose relative date text cannot be
    resolved are omitted and summarized there. Without one, they sort last.
    """
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if projection is not None:
        validate_relevance_projection_lineage(
            projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
        )
    # Identical events published by several sources collapse into one entry
    # whose refs are the union of every publishing source's event.

    def dedupe_identity(payload: Mapping[str, Any]) -> tuple[tuple[str, str], str]:
        raw_event_date = payload["event_date"]
        resolved_date = resolve_timeline_date_text(payload["date_text"] or "")
        if resolved_date is None and raw_event_date and not payload["date_text"]:
            resolved_date = date.fromisoformat(raw_event_date)
        date_identity = (
            ("resolved", resolved_date.isoformat())
            if resolved_date is not None
            else (
                "unresolved",
                _normalized_synthesis_text(payload["date_text"] or ""),
            )
            if payload["date_text"] is not None
            else ("undated", "")
        )
        return date_identity, _normalized_synthesis_text(payload["text"])

    def representative_key(payload: Mapping[str, Any]) -> tuple[int, str, str, str]:
        return (
            int(payload["event_date"] is None),
            payload["date_text"] or "",
            payload["text"],
            payload["event_date"] or "",
        )

    grouped: dict[
        tuple[tuple[str, str], str],
        tuple[Mapping[str, Any], set[ExtractionEvidenceRefV1]],
    ] = {}
    for ref, payload in extraction_evidence_elements(extraction):
        if ref.kind is not EvidenceKind.EVENT:
            continue
        if projection is not None and projection.classification_for(ref).classification not in {
            RelevanceClassification.DIRECT,
            RelevanceClassification.CORROBORATION,
        }:
            continue
        identity = dedupe_identity(payload)
        existing = grouped.get(identity)
        if existing is None:
            grouped[identity] = (payload, {ref})
            continue
        current_payload, refs = existing
        if representative_key(payload) < representative_key(current_payload):
            current_payload = payload
        grouped[identity] = (current_payload, refs)
        refs.add(ref)

    entries = [
        SynthesisTimelineEntryV1(
            event_date=(
                date.fromisoformat(payload["event_date"])
                if payload["event_date"] and _date_text_has_day_precision(payload["date_text"])
                else None
            ),
            date_text=payload["date_text"],
            text=payload["text"],
            evidence_refs=tuple(refs),
        )
        for payload, refs in grouped.values()
    ]
    unresolved = [
        entry
        for entry in entries
        if entry.event_date is None
        and entry.date_text is not None
        and resolve_timeline_date_text(entry.date_text) is None
    ]
    if warnings is not None and unresolved:
        entries = [entry for entry in entries if entry not in unresolved]
        event_word = "event" if len(unresolved) == 1 else "events"
        warning = f"Dropped {len(unresolved)} timeline {event_word} with unresolvable date wording."
        if warning not in warnings:
            warnings.append(warning)
    return tuple(sorted(entries, key=timeline_sort_key))


def _normalized_synthesis_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", unaccented))


def _uncertainty_noise_search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unaccented).split())


def _is_noise_uncertainty(value: str) -> bool:
    text = _uncertainty_noise_search_text(value)
    has_missing_language = re.search(
        r"\b(?:no|not|none|absent|missing|without|aucun|aucune|absence|manque|pas)\b",
        text,
    )
    has_detection_rule = re.search(r"\b(?:yara|sigma|suricata|snort)\b", text) and re.search(
        r"\b(?:rule|rules|regle|regles|detection|detecteur)\b", text
    )
    if has_missing_language and has_detection_rule:
        return True
    if "schema" in text and re.search(
        r"\b(?:artifact|artefact|type|blockchain|classification)\b", text
    ):
        return True
    has_partial_language = re.search(
        r"\b(?:incomplete|truncated|partial|cut|split|ends|ended|ending|"
        r"incomplet(?:e|s)?|tronque(?:e|es|s)?|partiel(?:le|les)?|coupe(?:e|es)?)\b",
        text,
    )
    has_capture_or_chunk = re.search(r"\b(?:capture|chunk|fragment|extrait)\b", text)
    return bool(has_partial_language and has_capture_or_chunk)


def build_synthesis_uncertainties(
    extraction: ProductionExtractionV1,
    *,
    projection: RelevanceProjectionV1 | None = None,
) -> tuple[SynthesisUncertaintyV1, ...]:
    """Filter boilerplate, normalize duplicates, and preserve useful provenance."""
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if projection is not None:
        validate_relevance_projection_lineage(
            projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
        )
    uncertainty_refs = {
        (ref.source_document_id, str(payload["text"])): ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.UNCERTAINTY
    }
    provenance: dict[str, tuple[str, set[UUID]]] = {}
    for source in extraction.sources:
        if source.profile is not ExtractionProfile.FULL:
            continue
        for uncertainty in source.uncertainties:
            if _is_noise_uncertainty(uncertainty):
                continue
            ref = uncertainty_refs[(source.source_document_id, uncertainty)]
            if projection is not None and projection.classification_for(ref).classification in {
                RelevanceClassification.OUT_OF_SCOPE,
                RelevanceClassification.INDETERMINATE,
            }:
                continue
            key = _normalized_synthesis_text(uncertainty)
            if not key:
                continue
            existing = provenance.get(key)
            if existing is None:
                provenance[key] = (uncertainty, {source.source_document_id})
                continue
            representative, source_ids = existing
            if (uncertainty.casefold(), uncertainty) < (representative.casefold(), representative):
                representative = uncertainty
            source_ids.add(source.source_document_id)
            provenance[key] = (representative, source_ids)
    ranked = sorted(
        provenance,
        key=lambda key: (_uncertainty_impact_rank(provenance[key][0]), key),
    )
    return tuple(
        SynthesisUncertaintyV1(
            text=provenance[key][0],
            source_document_ids=tuple(sorted(provenance[key][1], key=str)),
        )
        for key in ranked[:MAX_SYNTHESIS_UNCERTAINTIES]
    )


def _uncertainty_impact_rank(value: str) -> int:
    """Rank attribution, causality, scope and chronology gaps before other gaps."""
    normalized = _normalized_synthesis_text(value)
    impact_terms = (
        ("attribution", "attributed", "actor", "responsible", "identity"),
        ("causal", "causality", "caused", "mechanism", "because", "link"),
        ("scope", "same campaign", "same operation", "belongs", "related"),
        ("date", "timeline", "period", "when", "chronology", "chronological"),
    )
    for rank, terms in enumerate(impact_terms):
        if any(_normalized_synthesis_text(term) in normalized for term in terms):
            return rank
    return len(impact_terms)


@dataclass(frozen=True, slots=True)
class SynthesisDeltaV1:
    added_evidence: tuple[ExtractionEvidenceRefV1, ...]
    removed_evidence: tuple[ExtractionEvidenceRefV1, ...]
    unchanged_evidence: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        for name in ("added_evidence", "removed_evidence", "unchanged_evidence"):
            refs = getattr(self, name)
            if not isinstance(refs, tuple) or any(
                not isinstance(ref, ExtractionEvidenceRefV1) for ref in refs
            ):
                raise ValueError(f"Synthesis delta {name} must be a tuple of evidence refs")
            if len(refs) != len(set(refs)):
                raise ValueError(f"Synthesis delta {name} must not contain duplicates")
            object.__setattr__(self, name, tuple(sorted(refs, key=evidence_ref_sort_key)))


def build_synthesis_delta(
    previous: ProductionExtractionV1, current: ProductionExtractionV1
) -> SynthesisDeltaV1:
    """Compare exact canonical evidence identities across two extractions."""
    previous_refs = set(extraction_evidence_refs_v1(previous))
    current_refs = set(extraction_evidence_refs_v1(current))
    return SynthesisDeltaV1(
        added_evidence=tuple(current_refs - previous_refs),
        removed_evidence=tuple(previous_refs - current_refs),
        unchanged_evidence=tuple(previous_refs & current_refs),
    )


@dataclass(frozen=True, slots=True)
class SynthesisRevisionContextV1:
    """Non-authoritative previous synthesis plus its exact evidence delta.

    The previous document and its extraction are context only: the current
    evidence pack stays the single factual authority of the revision draft and
    removed evidence can never be cited again.
    """

    previous_artifact_id: UUID
    previous_synthesis: ProductionSynthesisV1
    previous_extraction: ProductionExtractionV1
    delta: SynthesisDeltaV1

    def __post_init__(self) -> None:
        if not isinstance(self.previous_artifact_id, UUID):
            raise ValueError("Revision context requires a previous artifact identity")
        if not isinstance(self.previous_synthesis, ProductionSynthesisV1):
            raise ValueError("Revision context requires a canonical previous synthesis")
        if not isinstance(self.previous_extraction, ProductionExtractionV1):
            raise ValueError("Revision context requires the previous canonical extraction")
        if not isinstance(self.delta, SynthesisDeltaV1):
            raise ValueError("Revision context requires a synthesis evidence delta")
        if (
            self.previous_synthesis.subject_id != self.previous_extraction.subject_id
            or self.previous_synthesis.extraction_hash
            != canonical_extraction_hash(self.previous_extraction)
        ):
            raise ValueError("Revision context previous synthesis lineage is inconsistent")


def _revision_claim_payload(
    paragraph: SynthesisParagraphV1, evidence_pack: SynthesisEvidencePackV1
) -> dict[str, Any]:
    """Project one previous claim with only currently citable prompt handles."""
    handles = [
        handle
        for handle in (evidence_pack.handle_for(ref) for ref in paragraph.evidence_refs)
        if handle is not None
    ]
    return {
        "text": paragraph.text,
        "evidence_handles": handles,
        "unsupported_evidence_count": len(paragraph.evidence_refs) - len(handles),
    }


def build_synthesis_revision_payload(
    context: SynthesisRevisionContextV1, evidence_pack: SynthesisEvidencePackV1
) -> dict[str, Any]:
    """Project the previous document and delta for the revision prompt.

    Previous evidence identities are never sent: a claim keeps only the
    handles that still exist in the current pack, and removed evidence is
    reported as a count so no internal identity can be echoed back.
    """
    previous = context.previous_synthesis
    return {
        "previous_synthesis_non_authoritative": {
            "non_authoritative": True,
            "publication_language": previous.publication_language,
            "title": previous.title,
            "lead": [
                _revision_claim_payload(paragraph, evidence_pack) for paragraph in previous.lead
            ],
            "sections": [
                {
                    "kind": section.kind.value,
                    "heading": section.heading,
                    "claims": [
                        _revision_claim_payload(paragraph, evidence_pack)
                        for paragraph in section.paragraphs
                    ],
                }
                for section in previous.sections
            ],
            "timeline": [
                {
                    "event_date": entry.event_date.isoformat()
                    if entry.event_date is not None
                    else None,
                    "date_text": entry.date_text,
                    "text": entry.text,
                }
                for entry in previous.timeline
            ],
            "uncertainties": [item.text for item in previous.uncertainties],
        },
        "evidence_delta": {
            "added": {
                "count": len(context.delta.added_evidence),
                "handles": [
                    handle
                    for handle in (
                        evidence_pack.handle_for(ref) for ref in context.delta.added_evidence
                    )
                    if handle is not None
                ],
            },
            "removed": {
                "count": len(context.delta.removed_evidence),
                "kinds": sorted({ref.kind.value for ref in context.delta.removed_evidence}),
            },
            "unchanged": {
                "count": len(context.delta.unchanged_evidence),
                "handles": [
                    handle
                    for handle in (
                        evidence_pack.handle_for(ref) for ref in context.delta.unchanged_evidence
                    )
                    if handle is not None
                ],
            },
        },
    }


def _revision_details(context: SynthesisRevisionContextV1) -> dict[str, Any]:
    """Bounded revision provenance for the stage result."""
    return {
        "previous_synthesis_artifact_id": str(context.previous_artifact_id),
        "added_evidence_count": len(context.delta.added_evidence),
        "removed_evidence_count": len(context.delta.removed_evidence),
        "unchanged_evidence_count": len(context.delta.unchanged_evidence),
    }


def render_synthesis_markdown(
    synthesis: ProductionSynthesisV1, extraction: ProductionExtractionV1
) -> str:
    """Render the V1 contract for reading; the canonical blob remains authoritative."""
    if synthesis.subject_id != extraction.subject_id:
        raise ValueError("Synthesis and extraction subjects differ")
    source_urls = {source.source_document_id: source.canonical_url for source in extraction.sources}

    def sources(refs: tuple[ExtractionEvidenceRefV1, ...]) -> str:
        urls = sorted({source_urls[ref.source_document_id] for ref in refs})
        return "Sources: " + ", ".join(urls)

    lines = [f"# {synthesis.title}", ""]
    for paragraph in synthesis.lead:
        lines.extend((paragraph.text, sources(paragraph.evidence_refs), ""))
    for section in synthesis.sections:
        for paragraph in section.paragraphs:
            lines.extend((paragraph.text, sources(paragraph.evidence_refs), ""))
    if synthesis.timeline:
        lines.extend(("## Timeline", ""))
        for entry in synthesis.timeline:
            label = entry.date_text or (
                entry.event_date.isoformat() if entry.event_date else "Undated"
            )
            lines.extend((f"- {label}: {entry.text}", f"  {sources(entry.evidence_refs)}"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- Canonical application service -----------------------------------------

# The evidence pack already bounds what reaches the drafter; these caps only
# keep the canonical warning projection small and deterministic.
MAX_SYNTHESIS_WARNINGS = 16
MAX_SYNTHESIS_WARNING_CHARS = 500
MAX_SYNTHESIS_DIAGNOSTICS = 16


class SynthesisExecutionStatus(StrEnum):
    """Outcome of one canonical synthesis execution."""

    SUCCEEDED = "succeeded"
    #: An exact canonical synthesis already existed; no model call was made.
    REUSED = "reused"
    #: A human decision is required; nothing is resubmitted automatically.
    NEEDS_REVIEW = "needs_review"
    #: A canonical input is absent or inconsistent; drafting never started.
    BLOCKED = "blocked"


class SynthesisStageErrorCode(StrEnum):
    """Control codes of the canonical synthesis boundary."""

    INPUTS_MISSING = "synthesis_inputs_missing"
    INPUTS_MISMATCH = "synthesis_inputs_mismatch"
    ACCESS_POLICY_UNAVAILABLE = "synthesis_access_policy_unavailable"
    POLICY_BLOCKED = "synthesis_policy_blocked"
    REUSE_INVALID = "synthesis_reuse_invalid"
    MODEL_CALL_FAILED = "synthesis_model_call_failed"
    RECONCILIATION_REQUIRED = PRODUCTION_RECONCILIATION_ERROR_CODE


@dataclass(frozen=True, slots=True)
class ProductionSynthesisExecution:
    """The bounded stage result of one canonical synthesis execution."""

    status: SynthesisExecutionStatus
    stage: ProductionArtifactStage = ProductionArtifactStage.SYNTHESIS
    mode: SynthesisMode = SynthesisMode.FRESH
    artifact_id: UUID | None = None
    model_run_id: UUID | None = None
    input_hash: str | None = None
    extraction_hash: str | None = None
    model_calls: int = 0
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, SynthesisExecutionStatus):
            raise ValueError("Synthesis execution status is invalid")
        if self.model_calls < 0:
            raise ValueError("Synthesis model call count cannot be negative")

    @property
    def succeeded(self) -> bool:
        return self.status in {
            SynthesisExecutionStatus.SUCCEEDED,
            SynthesisExecutionStatus.REUSED,
        }


class _SynthesisControlError(RuntimeError):
    """One control invariant failed; no drafting fallback may be attempted."""

    def __init__(
        self,
        code: SynthesisStageErrorCode,
        message: str,
        *,
        status: SynthesisExecutionStatus = SynthesisExecutionStatus.BLOCKED,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details: dict[str, Any] = dict(details or {})

    def result(self) -> ProductionSynthesisExecution:
        return ProductionSynthesisExecution(
            status=self.status,
            error_code=self.code.value,
            error=str(self),
            details=self.details,
        )


def _bounded_synthesis_warnings(
    extraction: ProductionExtractionV1,
    *,
    additional_warnings: Iterable[str] = (),
) -> tuple[str, ...]:
    """Project extraction warnings deterministically, with an explicit bound."""
    warnings = sorted(
        {
            warning.strip()
            for warning in (*extraction.warnings, *additional_warnings)
            if warning.strip()
        }
    )
    bounded = [
        warning
        if len(warning) <= MAX_SYNTHESIS_WARNING_CHARS
        else warning[: MAX_SYNTHESIS_WARNING_CHARS - 1] + "…"
        for warning in warnings[:MAX_SYNTHESIS_WARNINGS]
    ]
    omitted = len(warnings) - len(bounded)
    if omitted > 0:
        bounded.append(f"{omitted} extraction warning(s) omitted from this synthesis projection")
    return tuple(bounded)


def _bounded_synthesis_diagnostics(values: Iterable[str]) -> tuple[str, ...]:
    normalized = sorted({value.strip() for value in values if value.strip()})
    return tuple(
        value if len(value) <= MAX_SYNTHESIS_WARNING_CHARS else value[:499] + "…"
        for value in normalized[:MAX_SYNTHESIS_DIAGNOSTICS]
    )


def _synthesis_counts(synthesis: ProductionSynthesisV1) -> dict[str, Any]:
    """Bounded stage metadata; the canonical body itself is never returned."""
    paragraphs = (
        *synthesis.lead,
        *(item for section in synthesis.sections for item in section.paragraphs),
    )
    return {
        "schema_version": synthesis.schema_version,
        "section_count": len(synthesis.sections),
        "paragraph_count": len(paragraphs),
        "timeline_entry_count": len(synthesis.timeline),
        "evidence_ref_count": len(synthesis_evidence_refs(synthesis)),
        "uncertainty_count": len(synthesis.uncertainties),
        "warnings_count": len(synthesis.warnings),
    }


class ProductionSynthesisService:
    """The canonical, provider-agnostic SYNTHESIS service (AW-012).

    It consumes only the frozen ``ProductionInputSnapshot`` and the canonical
    ``ProductionExtractionV1`` artifact: no reference report, no legacy
    technical extraction, no source body and no web research exist here.  A
    retryable ``ModelGatewayError`` (a proven pre-submission failure) is raised
    to the caller, which retries with the same deterministic ModelRun identity;
    a possibly-submitted request is never replayed and becomes NEEDS_REVIEW.
    """

    def __init__(
        self,
        *,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        model_gateway: ModelGateway,
        synthesis_service: SynthesisService,
        artifact_reuse: ProductionArtifactReuseService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._model_gateway = model_gateway
        self._synthesis_service = synthesis_service
        self._artifact_reuse = artifact_reuse

    async def execute(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None = None,
    ) -> ProductionSynthesisExecution:
        """Run at most one durable drafting submission for this generation."""
        try:
            extraction, extraction_hash = await self._load_extraction(
                run, snapshot, extraction_artifact
            )
            projection = (
                await self._load_projection(
                    run,
                    snapshot,
                    extraction,
                    extraction_hash,
                    projection_artifact,
                )
                if projection_artifact is not None
                else None
            )
            policy = await self._load_access_policy(snapshot, extraction)
            evidence_pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
            access_policy_hash = synthesis_access_policy_hash(policy)
            input_hash = synthesis_input_hash(
                snapshot, extraction, evidence_pack, access_policy_hash
            )
            invocation_hash = synthesis_invocation_hash(
                snapshot, extraction, evidence_pack, access_policy_hash
            )
            reused = await self._reuse_exact(
                run=run,
                snapshot=snapshot,
                extraction_hash=extraction_hash,
                input_hash=input_hash,
            )
        except _SynthesisControlError as control:
            return control.result()
        if reused is not None:
            return reused
        if policy.do_not_submit or not policy.external_llm_allowed:
            # A do_not_submit source forbids every model submission; only the
            # model gateway route could turn this evidence into a draft.
            return ProductionSynthesisExecution(
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                mode=SynthesisMode.FRESH,
                input_hash=input_hash,
                extraction_hash=extraction_hash,
                model_calls=0,
                error_code=SynthesisStageErrorCode.POLICY_BLOCKED.value,
                error="The source access policy forbids any model submission.",
                details={
                    "do_not_submit": policy.do_not_submit,
                    "external_llm_allowed": policy.external_llm_allowed,
                    "effective_tlp": policy.effective_tlp.value,
                    "synthesis_access_policy_hash": synthesis_access_policy_hash(policy),
                },
            )
        revision = await self._find_revision_context(
            run, snapshot, extraction, input_hash, invocation_hash
        )
        return await self._draft(
            run=run,
            snapshot=snapshot,
            extraction=extraction,
            projection=projection,
            evidence_pack=evidence_pack,
            policy=policy,
            extraction_hash=extraction_hash,
            input_hash=input_hash,
            revision=revision,
        )

    async def _load_extraction(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_artifact: ProductionArtifact,
    ) -> tuple[ProductionExtractionV1, str]:
        """Read the canonical EXTRACTION artifact and prove its frozen lineage."""
        if not isinstance(extraction_artifact, ProductionArtifact):
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION artifact of this run is missing",
                details={"run_id": str(run.id)},
            )
        if extraction_artifact.stage is not ProductionArtifactStage.EXTRACTION:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The supplied artifact is not an EXTRACTION artifact",
                details={
                    "artifact_id": str(extraction_artifact.id),
                    "stage": extraction_artifact.stage.value,
                },
            )
        if (
            run.id != snapshot.production_run_id
            or run.subject_id != snapshot.subject_id
            or extraction_artifact.production_run_id != run.id
            or extraction_artifact.subject_id != snapshot.subject_id
        ):
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISMATCH,
                "The production run, snapshot and extraction artifact identities differ",
                details={
                    "run_id": str(run.id),
                    "snapshot_run_id": str(snapshot.production_run_id),
                    "artifact_run_id": str(extraction_artifact.production_run_id),
                    "artifact_id": str(extraction_artifact.id),
                },
            )
        if extraction_artifact.status is not ProductionArtifactStatus.VERIFIED:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The EXTRACTION artifact is not verified",
                details={
                    "artifact_id": str(extraction_artifact.id),
                    "status": extraction_artifact.status.value,
                },
            )
        if extraction_artifact.canonical_blob_id is None:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The EXTRACTION artifact carries no canonical extraction",
                details={"artifact_id": str(extraction_artifact.id)},
            )
        try:
            payload = await self._artifact_store.read_json(extraction_artifact.canonical_blob_id)
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception as exc:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION payload is not readable",
                details={"artifact_id": str(extraction_artifact.id)},
            ) from exc
        try:
            extraction = production_extraction_from_json(payload)
        except (TypeError, ValueError) as exc:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The canonical EXTRACTION payload is invalid",
                details={"artifact_id": str(extraction_artifact.id), "reason": str(exc)},
            ) from exc
        if extraction.subject_id != snapshot.subject_id:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISMATCH,
                "The canonical extraction belongs to another Subject",
                details={
                    "extraction_subject_id": str(extraction.subject_id),
                    "subject_id": str(snapshot.subject_id),
                },
            )
        if extraction.production_input_hash != snapshot.input_hash:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISMATCH,
                "The canonical extraction does not match the production input snapshot",
                details={
                    "extraction_input_hash": extraction.production_input_hash,
                    "snapshot_input_hash": snapshot.input_hash,
                },
            )
        return extraction, canonical_extraction_hash(extraction)

    async def _load_projection(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        extraction_hash: str,
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
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISSING,
                "The verified subject relevance projection is missing",
            )
        try:
            projection = relevance_projection_from_json(
                await self._artifact_store.read_json(artifact.canonical_blob_id)
            )
            validate_relevance_projection_lineage(
                projection, extraction, extraction_hash=extraction_hash
            )
        except (TypeError, ValueError) as exc:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISMATCH,
                "The subject relevance projection does not match canonical extraction",
                details={"artifact_id": str(artifact.id), "reason": str(exc)},
            ) from exc
        if (
            projection.production_input_hash != snapshot.input_hash
            or artifact.input_hash != projection.input_hash
        ):
            raise _SynthesisControlError(
                SynthesisStageErrorCode.INPUTS_MISMATCH,
                "The subject relevance projection has different functional inputs",
                details={"artifact_id": str(artifact.id)},
            )
        return projection

    async def _load_access_policy(
        self, snapshot: ProductionInputSnapshot, extraction: ProductionExtractionV1
    ) -> SynthesisAccessPolicyV1:
        """Resolve the exact archived sources' metadata; never their bodies."""
        async with self._uow_factory() as uow:
            try:
                return await build_synthesis_access_policy(
                    snapshot, extraction, uow.source_documents, uow.source_collections
                )
            except ProductionReuseStorageUnavailableError:
                raise
            except (TypeError, ValueError) as exc:
                raise _SynthesisControlError(
                    SynthesisStageErrorCode.ACCESS_POLICY_UNAVAILABLE,
                    "The exact source access policy of this extraction is unavailable",
                    details={"reason": str(exc)},
                ) from exc

    async def _reuse_exact(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_hash: str,
        input_hash: str,
    ) -> ProductionSynthesisExecution | None:
        """Reuse a verified canonical synthesis of the same functional inputs."""
        if self._artifact_reuse is None:
            return None
        reuse = await self._artifact_reuse.find_or_reuse(
            run=run,
            stage=ProductionArtifactStage.SYNTHESIS,
            input_hash=input_hash,
        )
        if reuse is None:
            return None
        artifact = reuse.artifact
        if (
            artifact.stage is not ProductionArtifactStage.SYNTHESIS
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.subject_id != snapshot.subject_id
            or artifact.input_hash != input_hash
            or artifact.canonical_blob_id is None
        ):
            raise _SynthesisControlError(
                SynthesisStageErrorCode.REUSE_INVALID,
                "The reusable synthesis artifact is not an exact verified canonical synthesis",
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                details={
                    "artifact_id": str(artifact.id),
                    "status": artifact.status.value,
                    "input_hash_match": artifact.input_hash == input_hash,
                },
            )
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            synthesis = production_synthesis_from_json(payload)
            validate_synthesis_lineage(synthesis, snapshot, extraction_hash)
        except ProductionReuseStorageUnavailableError:
            raise
        except (TypeError, ValueError) as exc:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.REUSE_INVALID,
                "The reusable synthesis artifact is not canonical for this run",
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                details={"artifact_id": str(artifact.id), "reason": str(exc)},
            ) from exc
        reused_from = artifact.reused_from_artifact_id
        return ProductionSynthesisExecution(
            status=SynthesisExecutionStatus.REUSED,
            mode=SynthesisMode.REUSE_EXACT,
            artifact_id=artifact.id,
            model_run_id=artifact.model_run_id,
            input_hash=input_hash,
            extraction_hash=extraction_hash,
            model_calls=0,
            details={
                "reused": reuse.reused,
                "reused_from_artifact_id": str(reused_from) if reused_from is not None else None,
            },
        )

    async def _find_revision_context(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        input_hash: str,
        invocation_hash: str,
    ) -> SynthesisRevisionContextV1 | None:
        """Locate the most recent eligible prior canonical synthesis, if any.

        Only a verified canonical ``ProductionSynthesisV1`` whose associated
        canonical extraction is still decodable can become non-authoritative
        revision context; rendered-only legacy artifacts never qualify.
        """
        async with self._uow_factory() as uow:
            candidates = await self._revision_candidates(uow, run, snapshot)
            current_invocation_runs = {
                synthesis_model_run_id(run, invocation_hash, mode)
                for mode in (SynthesisMode.FRESH, SynthesisMode.REVISE_PREVIOUS)
            }
            for artifact in candidates:
                if artifact.model_run_id in current_invocation_runs:
                    continue
                context = await self._decode_revision_candidate(
                    uow, artifact, extraction, input_hash
                )
                if context is not None:
                    return context
        return None

    async def _revision_candidates(
        self,
        uow: ProductionUnitOfWork,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
    ) -> list[ProductionArtifact]:
        """Return eligible prior canonical synthesis artifacts, most recent first."""
        # Imported locally: production_artifact_reuse imports this module.
        from cti_app.application.production_artifact_reuse import cross_run_reuse_allowed

        artifacts_repo = getattr(uow, "production_artifacts", None)
        list_current = getattr(artifacts_repo, "list_current_for_edition", None)
        if list_current is None:
            return []

        cross_run_allowed = cross_run_reuse_allowed(run, ProductionArtifactStage.SYNTHESIS)
        cutoff = await self._revision_invalidation_cutoff(uow, run)
        artifacts = await list_current(run.edition_id, ProductionArtifactStage.SYNTHESIS.value)
        eligible = [
            artifact
            for artifact in artifacts
            if artifact.stage is ProductionArtifactStage.SYNTHESIS
            and artifact.status is ProductionArtifactStatus.VERIFIED
            and artifact.canonical_blob_id is not None
            and artifact.subject_id == snapshot.subject_id
            and (
                artifact.production_run_id == run.id
                or (cross_run_allowed and (cutoff is None or artifact.created_at > cutoff))
            )
        ]
        return sorted(eligible, key=lambda item: (item.created_at, str(item.id)), reverse=True)

    @staticmethod
    async def _revision_invalidation_cutoff(
        uow: ProductionUnitOfWork, run: ProductionRun
    ) -> datetime | None:
        """Latest reuse invalidation that also invalidates a prior synthesis."""
        repository = getattr(uow, "production_reuse_invalidations", None)
        list_for_subject = getattr(repository, "list_for_subject", None)
        if list_for_subject is None:
            return None
        invalidations = await list_for_subject(run.edition_id, run.subject_id)
        applicable = [
            item.occurred_at
            for item in invalidations
            if getattr(getattr(item, "from_stage", None), "value", None)
            in {"references", "extraction", "synthesis"}
        ]
        return max(applicable) if applicable else None

    async def _decode_revision_candidate(
        self,
        uow: ProductionUnitOfWork,
        artifact: ProductionArtifact,
        extraction: ProductionExtractionV1,
        input_hash: str,
    ) -> SynthesisRevisionContextV1 | None:
        """Decode a candidate and its associated extraction evidence identity."""
        if artifact.canonical_blob_id is None:
            return None
        if artifact.input_hash == input_hash:
            # Functionally identical to the current request: the exact-reuse
            # boundary owns it, and a refusal there must not be smuggled in as
            # a revision draft.
            return None
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception:
            return None
        try:
            previous_synthesis = production_synthesis_from_json(payload)
        except (TypeError, ValueError):
            return None
        if previous_synthesis.subject_id != extraction.subject_id:
            return None
        previous_extraction = await self._associated_extraction(
            uow, artifact.production_run_id, previous_synthesis.extraction_hash
        )
        if previous_extraction is None or previous_extraction.subject_id != extraction.subject_id:
            return None
        delta = build_synthesis_delta(previous_extraction, extraction)
        return SynthesisRevisionContextV1(
            previous_artifact_id=artifact.id,
            previous_synthesis=previous_synthesis,
            previous_extraction=previous_extraction,
            delta=delta,
        )

    async def _associated_extraction(
        self, uow: ProductionUnitOfWork, run_id: UUID, extraction_hash: str
    ) -> ProductionExtractionV1 | None:
        """Decode the canonical extraction that produced one prior synthesis."""
        artifacts_repo = getattr(uow, "production_artifacts", None)
        list_for_run = getattr(artifacts_repo, "list_for_run", None)
        if list_for_run is None:
            return None
        artifacts = await list_for_run(run_id)
        ordered = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.EXTRACTION
                and artifact.canonical_blob_id is not None
            ),
            key=lambda item: item.version,
            reverse=True,
        )
        for artifact in ordered:
            try:
                payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
                candidate = production_extraction_from_json(payload)
            except ProductionReuseStorageUnavailableError:
                raise
            except Exception:
                continue
            if canonical_extraction_hash(candidate) == extraction_hash:
                return candidate
        return None

    async def _verified_existing_execution(
        self, request: ModelRequest
    ) -> tuple[ModelExecution | None, dict[str, Any] | None]:
        """Read a completed invocation's exact archived bytes without submitting."""
        if request.run_id is None:
            return None, {"error_code": "synthesis_invocation_identity_missing"}
        run = await self._model_gateway.get_run(request.run_id)
        if run is None or run.status is not ModelRunStatus.SUCCEEDED:
            return None, None
        if (
            run.id != request.run_id
            or run.prompt_template_id != request.prompt_template_id
            or run.prompt_template_version != request.prompt_template_version
            or run.evidence_pack_hash != request.evidence_pack_hash
        ):
            return None, {"error_code": "synthesis_invocation_identity_mismatch"}
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
        """Verify archived bytes against the immutable response digest before parsing."""
        return await verified_raw_output_text(
            self._model_gateway,
            run,
            error_prefix="synthesis",
            expected_text=expected_text,
        )

    async def _record_wire_parse(
        self,
        run: ModelRun,
        evidence_pack: SynthesisEvidencePackV1,
        parsed: SynthesisWireParseResult,
        *,
        validation_error: str | None = None,
    ) -> str:
        """Persist parser identity and normalized strict proposal beside raw output."""
        assert run.raw_output_sha256 is not None
        identity = synthesis_parse_identity(run.raw_output_sha256, evidence_pack)
        validation_errors: list[dict[str, Any]] = [
            {
                "path": ["blocks", item.block_id],
                "code": item.reason_code,
                "value_sha256": item.raw_sha256,
            }
            for item in parsed.rejections
        ]
        if validation_error is not None:
            validation_errors.append(
                {
                    "path": ["proposal"],
                    "code": validation_error,
                    "value_sha256": run.raw_output_sha256,
                }
            )
        transformations = [
            *parsed.transformations,
            *(("synthesis_section_heading_dropped",) if parsed.warnings else ()),
            *(("synthesis_missing_coverage_diagnostics",) if parsed.diagnostics else ()),
            f"synthesis_parser:{SYNTHESIS_WIRE_PARSER_VERSION}",
            f"synthesis_contract:{SYNTHESIS_PROPOSAL_CONTRACT_VERSION}",
            f"synthesis_prompt:{SYNTHESIS_PROMPT_VERSION}",
        ]
        normalized = None
        if parsed.proposal is not None:
            normalized = parsed.proposal.model_dump_json().encode("utf-8")
            transformations.append("synthesis_text_blocks_to_strict_proposal")
        return await record_wire_parse_diagnostics(
            self._model_gateway,
            run,
            parser_stage="synthesis",
            parse_identity=identity,
            validation_errors=validation_errors,
            transformations=tuple(transformations),
            normalized_output=normalized,
        )

    async def _draft(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        projection: RelevanceProjectionV1 | None,
        evidence_pack: SynthesisEvidencePackV1,
        policy: SynthesisAccessPolicyV1,
        extraction_hash: str,
        input_hash: str,
        revision: SynthesisRevisionContextV1 | None,
    ) -> ProductionSynthesisExecution:
        """Submit once, validate deterministically, then persist the canonical result."""
        mode = SynthesisMode.REVISE_PREVIOUS if revision is not None else SynthesisMode.FRESH
        request = build_synthesis_model_request(
            run, snapshot, extraction, evidence_pack, policy, mode, revision=revision
        )
        if request.metadata.get("synthesis_input_hash") != input_hash:
            raise ValueError("Synthesis request identity is inconsistent with its inputs")
        model_run_id = request.run_id
        model_calls = 0

        def reviewed(
            error_code: str,
            error: str,
            *,
            run_id: UUID | None,
            details: Mapping[str, Any],
            reconciliation: bool = False,
            model_call_count: int | None = None,
        ) -> ProductionSynthesisExecution:
            """A terminal outcome of the single submission, never a retry."""
            payload = dict(details)
            if revision is not None:
                payload.update(_revision_details(revision))
            if reconciliation:
                payload["error_code"] = SynthesisStageErrorCode.RECONCILIATION_REQUIRED.value
                payload["model_run_id"] = str(run_id) if run_id is not None else None
            return ProductionSynthesisExecution(
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                mode=mode,
                model_run_id=run_id,
                input_hash=input_hash,
                extraction_hash=extraction_hash,
                model_calls=model_calls if model_call_count is None else model_call_count,
                error_code=error_code,
                error=error,
                details=payload,
            )

        try:
            execution, archive_error = await self._verified_existing_execution(request)
            if archive_error is not None:
                return reviewed(
                    SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                    "The archived synthesis response could not be verified for re-parsing.",
                    run_id=model_run_id,
                    details=archive_error,
                    model_call_count=0,
                )
            if execution is None:
                model_calls = 1
                execution = await draft_synthesis_proposal(self._model_gateway, request)
        except ModelSubmissionReconciliationRequiredError as exc:
            return reviewed(
                SynthesisStageErrorCode.RECONCILIATION_REQUIRED.value,
                "A provider submission may have been accepted and must be reconciled",
                run_id=exc.model_run_id or model_run_id,
                details=exc.details,
                reconciliation=True,
            )
        except ExternalModelBlockedError as exc:
            return reviewed(
                SynthesisStageErrorCode.POLICY_BLOCKED.value,
                "The source access policy blocked the selected model route.",
                run_id=model_run_id,
                details={"error_code": exc.code},
            )
        except ModelGatewayError as exc:
            if exc.retryable:
                # A proven pre-submission failure: the caller retries with the
                # same deterministic ModelRun identity.
                raise
            return reviewed(
                SynthesisStageErrorCode.MODEL_CALL_FAILED.value,
                "The synthesis model call failed.",
                run_id=model_run_id,
                details={"error_code": exc.code},
            )

        model_run = execution.run
        if model_run.status is ModelRunStatus.NEEDS_REVIEW:
            if model_run_awaits_reconciliation(model_run.error_code):
                return reviewed(
                    SynthesisStageErrorCode.RECONCILIATION_REQUIRED.value,
                    "A provider submission may have been accepted and must be reconciled",
                    run_id=model_run.id,
                    details=dict(model_run.error_details or {}),
                    reconciliation=True,
                )
            return reviewed(
                SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                "The synthesis model run needs review without a usable answer.",
                run_id=model_run.id,
                details=self._model_evidence(model_run),
            )
        if model_run.status is not ModelRunStatus.SUCCEEDED:
            return reviewed(
                SynthesisStageErrorCode.MODEL_CALL_FAILED.value,
                f"The synthesis model run reached status {model_run.status.value}.",
                run_id=model_run.id,
                details=self._model_evidence(model_run),
            )

        raw_text, raw_error = await self._verified_raw_text(
            model_run, expected_text=execution.output_text
        )
        if raw_error is not None or raw_text is None:
            return reviewed(
                SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                "The synthesis response is not backed by verified archived bytes.",
                run_id=model_run.id,
                details={**self._model_evidence(model_run), **(raw_error or {})},
            )
        parsed = parse_synthesis_proposal_wire(raw_text)
        parse_identity = synthesis_parse_identity(model_run.raw_output_sha256 or "", evidence_pack)
        rejection_details = [
            {"block_id": item.block_id, "reason_code": item.reason_code}
            for item in parsed.rejections
        ]
        wire_diagnostics: dict[str, Any] = {
            "parse_warnings": [
                {"block_id": item.block_id, "warning_code": item.warning_code}
                for item in parsed.warnings
            ],
            "rejected_blocks": rejection_details,
            "missing_coverage": list(_bounded_synthesis_diagnostics(parsed.diagnostics)),
        }
        if parsed.proposal is None:
            await self._record_wire_parse(model_run, evidence_pack, parsed)
            return reviewed(
                SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                "The synthesis response contains no usable proposal blocks.",
                run_id=model_run.id,
                details={
                    **self._model_evidence(model_run),
                    "parse_identity": parse_identity,
                    "parse_error": parsed.error_code,
                    "rejections": rejection_details,
                    "diagnostics": wire_diagnostics,
                },
            )
        try:
            lead, sections = validate_synthesis_proposal(
                parsed.proposal,
                evidence_pack,
                extraction,
                removed_evidence=() if revision is None else revision.delta.removed_evidence,
            )
        except SynthesisProposalControlError as exc:
            await self._record_wire_parse(
                model_run,
                evidence_pack,
                parsed,
                validation_error=exc.code.value,
            )
            return reviewed(
                exc.code.value,
                f"The synthesis proposal failed {exc.code.value}.",
                run_id=model_run.id,
                details={
                    **self._model_evidence(model_run),
                    "parse_identity": parse_identity,
                    "rejections": rejection_details,
                    "diagnostics": wire_diagnostics,
                },
            )
        await self._record_wire_parse(model_run, evidence_pack, parsed)

        timeline_warnings: list[str] = []
        timeline = build_synthesis_timeline(
            extraction, projection=projection, warnings=timeline_warnings
        )
        synthesis = ProductionSynthesisV1(
            schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
            subject_id=snapshot.subject_id,
            production_input_hash=snapshot.input_hash,
            extraction_hash=extraction_hash,
            publication_language=snapshot.publication_language,
            synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
            title=snapshot.subject_title,
            lead=lead,
            sections=sections,
            timeline=timeline,
            uncertainties=build_synthesis_uncertainties(extraction, projection=projection),
            warnings=_bounded_synthesis_warnings(
                extraction,
                additional_warnings=(
                    *timeline_warnings,
                    *(f"{item.warning_code}:{item.block_id}" for item in parsed.warnings),
                ),
            ),
        )
        validate_synthesis_lineage(synthesis, snapshot, extraction_hash)
        diagnostics = {
            **wire_diagnostics,
            "warnings": list(synthesis.warnings),
        }
        artifact = await self._synthesis_service.store_synthesis_result(
            run_id=run.id,
            subject_id=snapshot.subject_id,
            input_hash=input_hash,
            synthesis=synthesis,
            extraction=extraction,
            raw_result=raw_text,
            model_run_id=model_run.id,
            mode=mode,
            model_policy_version=SYNTHESIS_MODEL_POLICY_VERSION,
            routing_policy_version=SYNTHESIS_ROUTING_POLICY_VERSION,
            projection_hash=projection.projection_hash if projection is not None else None,
            diagnostics=diagnostics,
        )
        return ProductionSynthesisExecution(
            status=SynthesisExecutionStatus.SUCCEEDED,
            mode=mode,
            artifact_id=artifact.id,
            model_run_id=model_run.id,
            input_hash=input_hash,
            extraction_hash=extraction_hash,
            model_calls=model_calls,
            details={
                **_synthesis_counts(synthesis),
                "parse_identity": parse_identity,
                "parse_rejections": rejection_details,
                "diagnostics": diagnostics,
                **({} if revision is None else _revision_details(revision)),
            },
        )

    @staticmethod
    def _model_evidence(model_run: ModelRun) -> dict[str, Any]:
        """Bounded ModelRun evidence; the raw body stays in the model archive."""
        return {
            "model_run_id": str(model_run.id),
            "status": model_run.status.value,
            "error_code": model_run.error_code,
            "raw_output_sha256": model_run.raw_output_sha256,
        }
