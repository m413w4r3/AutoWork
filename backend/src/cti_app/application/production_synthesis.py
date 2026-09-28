"""Canonical synthesis: deterministic evidence inputs, validation and service."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
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
    StructuredOutputError,
)
from cti_app.application.persistence import ProductionUnitOfWorkFactory, SourceDocumentRepository
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
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
from cti_app.domain.production_references import ProductionReferenceTier
from cti_app.domain.production_synthesis import (
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
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
    validate_synthesis_lineage,
)

if TYPE_CHECKING:
    from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
    from cti_app.application.production_stages import SynthesisService

SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION = "synthesis-evidence-pack-v1-technical-cap-128"
SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION = 1
SYNTHESIS_ACCESS_POLICY_VERSION = "synthesis-access-policy-v1"
SYNTHESIS_PROPOSAL_SCHEMA_VERSION = "synthesis-proposal-v1"
SYNTHESIS_PROMPT_VERSION = "synthesis-draft-v1"
SYNTHESIS_VALIDATOR_VERSION = "synthesis-validator-v1"
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
    heading: StrictStr
    claims: tuple[SynthesisClaimProposalV1, ...]

    @field_validator("heading")
    @classmethod
    def _nonempty_heading(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Section heading must be non-empty text")
        return value

    @field_validator("claims")
    @classmethod
    def _nonempty_claims(
        cls, value: tuple[SynthesisClaimProposalV1, ...]
    ) -> tuple[SynthesisClaimProposalV1, ...]:
        if not value:
            raise ValueError("Section claims must be a non-empty tuple")
        return value


class SynthesisProposalV1(_StrictProposalModel):
    """Structured output schema returned directly by ModelGateway.draft."""

    lead: tuple[SynthesisClaimProposalV1, ...]
    sections: tuple[SynthesisSectionProposalV1, ...]

    @field_validator("lead")
    @classmethod
    def _nonempty_lead(
        cls, value: tuple[SynthesisClaimProposalV1, ...]
    ) -> tuple[SynthesisClaimProposalV1, ...]:
        if not value:
            raise ValueError("Proposal lead must be a non-empty tuple")
        return value


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
    r"(?i)(?:\[(?:s\d+|e\d+|\d+|source\s*\d+|ref(?:erence)?\s*\d+)\]|"
    r"\((?:s\d+|source\s*\d+)\)|\bE\d{3,}\b)"
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
            tlp = document.tlp
            external_llm_allowed = document.external_llm_allowed
            do_not_submit = document.do_not_submit
        except AttributeError as exc:
            raise ValueError("synthesis_access_policy_unavailable") from exc
        if (
            document_id != source_id
            or subject_id != snapshot.subject_id
            or not isinstance(tlp, TLP)
            or type(external_llm_allowed) is not bool
            or type(do_not_submit) is not bool
        ):
            raise ValueError("synthesis_access_policy_unavailable")
        records.append(
            SynthesisAccessSourceV1(
                source_document_id=source_id,
                tlp=tlp,
                external_llm_allowed=external_llm_allowed,
                do_not_submit=do_not_submit,
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


def synthesis_input_hash(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy_hash: str,
    *,
    prompt_version: str = SYNTHESIS_PROMPT_VERSION,
    validator_version: str = SYNTHESIS_VALIDATOR_VERSION,
    model_policy_version: str = SYNTHESIS_MODEL_POLICY_VERSION,
    routing_policy_version: str = SYNTHESIS_ROUTING_POLICY_VERSION,
) -> str:
    """Hash all functional Synthesis inputs and policy/contract versions."""
    if not re.fullmatch(r"[0-9a-f]{64}", access_policy_hash):
        raise ValueError("Synthesis access policy hash must be a lowercase SHA-256")
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Synthesis snapshot and extraction subjects differ")
    if evidence_pack.publication_language != snapshot.publication_language:
        raise ValueError("Synthesis evidence pack language differs from its snapshot")
    payload = {
        "snapshot_input_hash": snapshot.input_hash,
        "extraction_hash": canonical_extraction_hash(extraction),
        "publication_language": snapshot.publication_language,
        "extraction_schema_version": PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        "evidence_pack_schema_version": SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION,
        "evidence_pack_policy_version": evidence_pack.policy_version,
        "proposal_schema_version": SYNTHESIS_PROPOSAL_SCHEMA_VERSION,
        "canonical_schema_version": PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        "prompt_version": prompt_version,
        "validator_version": validator_version,
        "evidence_ref_algorithm_version": SYNTHESIS_EVIDENCE_REF_ALGORITHM_VERSION,
        "synthesis_policy_version": SYNTHESIS_POLICY_VERSION,
        "access_policy_hash": access_policy_hash,
        "model_policy_version": model_policy_version,
        "routing_policy_version": routing_policy_version,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def synthesis_model_run_id(
    run: ProductionRun, synthesis_input_hash: str, mode: SynthesisMode
) -> UUID:
    """Derive a stable provider ModelRun identity for one run generation/mode."""
    if not isinstance(run, ProductionRun) or not isinstance(mode, SynthesisMode):
        raise ValueError("Synthesis ModelRun identity inputs are invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", synthesis_input_hash):
        raise ValueError("Synthesis input hash must be a lowercase SHA-256")
    identity = ":".join(
        (
            "production-synthesis-model-run-v1",
            str(run.id),
            str(run.pipeline_generation),
            synthesis_input_hash,
            mode.value,
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def _evidence_ref_key(ref: ExtractionEvidenceRefV1) -> tuple[str, str, str]:
    return (str(ref.source_document_id), ref.kind.value, ref.evidence_key)


def _element_ref(
    source_document_id: UUID, kind: EvidenceKind, payload: Mapping[str, Any]
) -> ExtractionEvidenceRefV1:
    key_payload = {
        "source_document_id": str(source_document_id),
        "kind": kind.value,
        "payload": payload,
    }
    return ExtractionEvidenceRefV1(
        source_document_id=source_document_id,
        kind=kind,
        evidence_key=hashlib.sha256(_canonical_json_bytes(key_payload)).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class _EvidenceEntry:
    ref: ExtractionEvidenceRefV1
    payload: Mapping[str, Any]


def _all_evidence_entries(
    extraction: ProductionExtractionV1,
) -> dict[ExtractionEvidenceRefV1, _EvidenceEntry]:
    payload = production_extraction_to_json(extraction)
    payload_keys = (
        (EvidenceKind.FACT, "facts"),
        (EvidenceKind.EVENT, "events"),
        (EvidenceKind.INDICATOR, "indicators"),
        (EvidenceKind.RULE, "rules"),
    )
    entries: dict[ExtractionEvidenceRefV1, _EvidenceEntry] = {}
    for source, source_payload in zip(extraction.sources, payload["sources"], strict=True):
        for kind, list_key in payload_keys:
            for element_payload in source_payload[list_key]:
                ref = _element_ref(source.source_document_id, kind, element_payload)
                entries.setdefault(ref, _EvidenceEntry(ref, element_payload))
    return entries


def _prompt_evidence_record(
    handle: str, kind: EvidenceKind, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Project source-local extraction evidence without internal document IDs."""
    if kind is EvidenceKind.FACT:
        return {
            "handle": handle,
            "kind": kind.value,
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
            "value": payload["value"],
            "type": payload["artifact_type"],
            "context": payload["context"],
            "evidence": payload["evidence_quote"],
        }
    return {
        "handle": handle,
        "kind": kind.value,
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
    uncertainties: tuple[str, ...]
    policy_version: str = SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION
    _handle_to_ref: Mapping[str, ExtractionEvidenceRefV1] = field(
        default_factory=dict, repr=False, compare=False
    )

    def resolve_handle(self, handle: str) -> ExtractionEvidenceRefV1:
        """Resolve only an exact prompt handle; no fuzzy or prefix matching."""
        try:
            return self._handle_to_ref[handle]
        except KeyError as exc:
            raise ValueError("synthesis_unknown_evidence") from exc


def build_synthesis_evidence_pack(
    snapshot: ProductionInputSnapshot, extraction: ProductionExtractionV1
) -> SynthesisEvidencePackV1:
    """Build the bounded prompt projection and its private handle resolver."""
    if not isinstance(snapshot, ProductionInputSnapshot):
        raise ValueError("Expected a ProductionInputSnapshot")
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Synthesis snapshot and extraction subjects differ")

    entries = _all_evidence_entries(extraction)
    source_by_id = {source.source_document_id: source for source in extraction.sources}
    narrative_refs = {
        ref
        for ref in entries
        if ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
        and source_by_id[ref.source_document_id].tier is ProductionReferenceTier.CORE
        and source_by_id[ref.source_document_id].profile is ExtractionProfile.FULL
    }
    technical_candidates = [
        ref
        for ref, entry in entries.items()
        if ref.kind in {EvidenceKind.INDICATOR, EvidenceKind.RULE}
        and (str(entry.payload["context"]).strip() or str(entry.payload["evidence_quote"]).strip())
    ]
    technical_refs = set(
        sorted(technical_candidates, key=_evidence_ref_key)[:MAX_TECHNICAL_EVIDENCE_V1]
    )

    catalogue_refs = sorted(narrative_refs | technical_refs, key=_evidence_ref_key)
    handle_to_ref = {f"E{index:03d}": ref for index, ref in enumerate(catalogue_refs, start=1)}
    handle_for_ref = {ref: handle for handle, ref in handle_to_ref.items()}

    narrative_evidence = tuple(
        _prompt_evidence_record(handle_for_ref[ref], ref.kind, entries[ref].payload)
        for ref in catalogue_refs
        if ref in narrative_refs
    )
    technical_evidence = tuple(
        _prompt_evidence_record(handle_for_ref[ref], ref.kind, entries[ref].payload)
        for ref in catalogue_refs
        if ref in technical_refs
    )
    uncertainties = build_synthesis_uncertainties(extraction)

    return SynthesisEvidencePackV1(
        subject_title=snapshot.subject_title,
        publication_language=snapshot.publication_language,
        discovery_summary=snapshot.discovery_summary,
        actor_or_campaign=snapshot.actor_or_campaign,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        narrative_evidence=narrative_evidence,
        technical_evidence=technical_evidence,
        uncertainties=tuple(item.text for item in uncertainties),
        _handle_to_ref=MappingProxyType(handle_to_ref),
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
        "uncertainties": list(evidence_pack.uncertainties),
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def build_synthesis_model_request(
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    evidence_pack: SynthesisEvidencePackV1,
    access_policy: SynthesisAccessPolicyV1,
    mode: SynthesisMode,
) -> ModelRequest:
    """Build a complete stateless, web-disabled Synthesis drafting request."""
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
    pack_hash = synthesis_evidence_pack_hash(evidence_pack)
    prompt_payload = {
        "instructions": (
            "Write in the requested publication language. Paraphrase and organize "
            "only the supplied "
            "evidence; invent no facts, dates, identifiers, causal links, or source details. Omit "
            "unsupported information. Cite every factual claim using one or more exact evidence "
            "handles from this pack. Handles are temporary references; never output handles in "
            "claim text. Keep claims atomic, use plain text without Markdown or HTML, and return "
            "only the requested structured proposal."
        ),
        "publication_language": snapshot.publication_language,
        "frozen_subject_context": {
            "title": snapshot.subject_title,
            "tlp": snapshot.subject_tlp.value,
            "discovery_summary": snapshot.discovery_summary,
            "actor_or_campaign": snapshot.actor_or_campaign,
            "period_start": snapshot.period_start.isoformat(),
            "period_end": snapshot.period_end.isoformat(),
        },
        "current_evidence_pack": {
            "schema_version": SYNTHESIS_EVIDENCE_PACK_SCHEMA_VERSION,
            "policy_version": evidence_pack.policy_version,
            "narrative_evidence": [dict(record) for record in evidence_pack.narrative_evidence],
            "technical_evidence": [dict(record) for record in evidence_pack.technical_evidence],
            "uncertainties": list(evidence_pack.uncertainties),
        },
        "output_contract": {
            "schema_version": SYNTHESIS_PROPOSAL_SCHEMA_VERSION,
            "lead": [{"text": "claim text", "evidence_handles": ["E001"]}],
            "sections": [
                {
                    "kind": "overview",
                    "heading": "Human readable heading",
                    "claims": [{"text": "claim text", "evidence_handles": ["E001"]}],
                }
            ],
            "allowed_section_kinds": [kind.value for kind in SynthesisSectionKind],
        },
    }
    prompt = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
        run_id=synthesis_model_run_id(run, functional_hash, mode),
        # The gateway only resubmits a FAILED run whose submission state
        # proves the provider was never reached.
        allow_failed_resubmit=True,
    )


async def draft_synthesis_proposal(
    model_gateway: ModelGateway, request: ModelRequest
) -> ModelExecution:
    """Submit one stateless structured draft through the provider-agnostic gateway."""
    return await model_gateway.draft(request, SynthesisProposalV1)


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
    payload = _strict_mapping(value, _SECTION_PROPOSAL_KEYS, "Section proposal")
    claims = payload["claims"]
    if not isinstance(claims, list):
        raise ValueError("Section claims must be an array")
    kind_value = payload["kind"]
    if not isinstance(kind_value, str):
        raise ValueError("Section kind must be text")
    return SynthesisSectionProposalV1(
        kind=SynthesisSectionKind(kind_value),
        heading=payload["heading"],
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
        event_date = payload["event_date"]
        date_text = payload["date_text"]
        return any(
            date_key in _date_literals(value)
            for value in (event_date or "", date_text or "")
            if isinstance(value, str)
        )
    if "category" not in payload:
        return False
    fact_value = payload.get("value")
    return isinstance(fact_value, str) and date_key in _date_literals(fact_value)


def _validate_grounded_text(
    text: str,
    refs: tuple[ExtractionEvidenceRefV1, ...],
    entries: Mapping[ExtractionEvidenceRefV1, _EvidenceEntry],
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
        if not any(_date_supported_by_payload(entries[ref].payload, date_key) for ref in refs):
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
        narrative_refs = {evidence_pack.resolve_handle(handle) for handle in narrative_handles}
        technical_refs = {evidence_pack.resolve_handle(handle) for handle in technical_handles}

        known_technical: set[tuple[str, str]] = set()
        technical_support_mutable: dict[tuple[str, str], set[ExtractionEvidenceRefV1]] = (
            defaultdict(set)
        )
        for ref, entry in entries.items():
            for value in _string_values(entry.payload):
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
            _validate_plain_text(section.heading)
            allow_technical = section.kind in _TECHNICAL_SECTION_KINDS
            paragraphs = tuple(
                convert_claim(claim, allow_technical=allow_technical) for claim in section.claims
            )
            section_refs = tuple(ref for paragraph in paragraphs for ref in paragraph.evidence_refs)
            _validate_grounded_text(
                section.heading,
                section_refs,
                entries,
                known_technical,
                technical_support_mutable,
            )
            sections.append(
                SynthesisSectionV1(
                    kind=section.kind,
                    heading=section.heading,
                    paragraphs=paragraphs,
                )
            )
        return lead, tuple(sections)
    except SynthesisProposalControlError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SynthesisProposalControlError(SynthesisProposalErrorCode.OUTPUT_INVALID) from exc


def build_synthesis_timeline(
    extraction: ProductionExtractionV1,
) -> tuple[SynthesisTimelineEntryV1, ...]:
    """Normalize, deduplicate and order the canonical Extraction events."""
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    source_payloads = production_extraction_to_json(extraction)["sources"]
    grouped: dict[bytes, dict[str, Any]] = {}
    for source, source_payload in zip(extraction.sources, source_payloads, strict=True):
        for event, event_payload in zip(source.events, source_payload["events"], strict=True):
            normalized = {
                key: value for key, value in event_payload.items() if key != "source_document_ids"
            }
            identity = _canonical_json_bytes(normalized)
            group = grouped.setdefault(
                identity,
                {
                    "event": event,
                    "refs": set(),
                },
            )
            group["refs"].add(
                _element_ref(source.source_document_id, EvidenceKind.EVENT, event_payload)
            )

    entries = [
        SynthesisTimelineEntryV1(
            event_date=group["event"].event_date,
            date_text=group["event"].date_text,
            text=group["event"].text,
            evidence_refs=tuple(sorted(group["refs"], key=_evidence_ref_key)),
        )
        for group in grouped.values()
    ]
    return tuple(sorted(entries, key=_timeline_sort_key))


def _timeline_sort_key(
    entry: SynthesisTimelineEntryV1,
) -> tuple[bool, date, tuple[tuple[str, str, str], ...], str, str]:
    return (
        entry.event_date is None,
        entry.event_date or date.max,
        tuple(_evidence_ref_key(ref) for ref in entry.evidence_refs),
        entry.text,
        entry.date_text or "",
    )


def build_synthesis_uncertainties(
    extraction: ProductionExtractionV1,
) -> tuple[SynthesisUncertaintyV1, ...]:
    """Union uncertainty text while preserving every source's provenance."""
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    provenance: dict[str, set[UUID]] = defaultdict(set)
    for source in extraction.sources:
        for uncertainty in source.uncertainties:
            provenance[uncertainty].add(source.source_document_id)
    return tuple(
        SynthesisUncertaintyV1(
            text=text,
            source_document_ids=tuple(sorted(source_ids, key=str)),
        )
        for text, source_ids in sorted(provenance.items())
    )


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
            object.__setattr__(self, name, tuple(sorted(refs, key=_evidence_ref_key)))


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
        lines.extend((f"## {section.heading}", ""))
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
    if synthesis.uncertainties:
        lines.extend(("## Uncertainties", ""))
        for item in synthesis.uncertainties:
            urls = sorted(source_urls[document_id] for document_id in item.source_document_ids)
            lines.extend((f"- {item.text}", f"  Sources: {', '.join(urls)}"))
        lines.append("")
    if synthesis.warnings:
        lines.extend(("## Warnings", ""))
        lines.extend(f"- {warning}" for warning in synthesis.warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- Canonical application service -----------------------------------------

# The evidence pack already bounds what reaches the drafter; these caps only
# keep the canonical warning projection small and deterministic.
MAX_SYNTHESIS_WARNINGS = 16
MAX_SYNTHESIS_WARNING_CHARS = 500


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


def _bounded_synthesis_warnings(extraction: ProductionExtractionV1) -> tuple[str, ...]:
    """Project extraction warnings deterministically, with an explicit bound."""
    warnings = sorted({warning.strip() for warning in extraction.warnings if warning.strip()})
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


def _synthesis_counts(synthesis: ProductionSynthesisV1) -> dict[str, Any]:
    """Bounded stage metadata; the canonical body itself is never returned."""
    paragraphs = (
        *synthesis.lead,
        *(item for section in synthesis.sections for item in section.paragraphs),
    )
    evidence_refs = {ref for paragraph in paragraphs for ref in paragraph.evidence_refs}
    evidence_refs.update(ref for entry in synthesis.timeline for ref in entry.evidence_refs)
    return {
        "schema_version": synthesis.schema_version,
        "section_count": len(synthesis.sections),
        "paragraph_count": len(paragraphs),
        "timeline_entry_count": len(synthesis.timeline),
        "evidence_ref_count": len(evidence_refs),
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
    ) -> ProductionSynthesisExecution:
        """Run at most one durable drafting submission for this generation."""
        try:
            extraction, extraction_hash = await self._load_extraction(
                run, snapshot, extraction_artifact
            )
            policy = await self._load_access_policy(snapshot, extraction)
            evidence_pack = build_synthesis_evidence_pack(snapshot, extraction)
            input_hash = synthesis_input_hash(
                snapshot, extraction, evidence_pack, synthesis_access_policy_hash(policy)
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
        if policy.do_not_submit:
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
                    "do_not_submit": True,
                    "external_llm_allowed": policy.external_llm_allowed,
                    "effective_tlp": policy.effective_tlp.value,
                    "synthesis_access_policy_hash": synthesis_access_policy_hash(policy),
                },
            )
        return await self._draft(
            run=run,
            snapshot=snapshot,
            extraction=extraction,
            evidence_pack=evidence_pack,
            policy=policy,
            extraction_hash=extraction_hash,
            input_hash=input_hash,
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

    async def _load_access_policy(
        self, snapshot: ProductionInputSnapshot, extraction: ProductionExtractionV1
    ) -> SynthesisAccessPolicyV1:
        """Resolve the exact archived sources' metadata; never their bodies."""
        async with self._uow_factory() as uow:
            try:
                return await build_synthesis_access_policy(
                    snapshot, extraction, uow.source_documents
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
        if artifact.canonical_blob_id is None:
            raise _SynthesisControlError(
                SynthesisStageErrorCode.REUSE_INVALID,
                "The reusable synthesis artifact carries no canonical synthesis",
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                details={"artifact_id": str(artifact.id)},
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

    async def _draft(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        evidence_pack: SynthesisEvidencePackV1,
        policy: SynthesisAccessPolicyV1,
        extraction_hash: str,
        input_hash: str,
    ) -> ProductionSynthesisExecution:
        """Submit once, validate deterministically, then persist the canonical result."""
        mode = SynthesisMode.FRESH
        request = build_synthesis_model_request(
            run, snapshot, extraction, evidence_pack, policy, mode
        )
        if request.metadata.get("synthesis_input_hash") != input_hash:
            raise ValueError("Synthesis request identity is inconsistent with its inputs")
        model_run_id = request.run_id

        def reviewed(
            error_code: str,
            error: str,
            *,
            run_id: UUID | None,
            details: Mapping[str, Any],
            reconciliation: bool = False,
        ) -> ProductionSynthesisExecution:
            """A terminal outcome of the single submission, never a retry."""
            payload = dict(details)
            if reconciliation:
                payload["error_code"] = SynthesisStageErrorCode.RECONCILIATION_REQUIRED.value
                payload["model_run_id"] = str(run_id) if run_id is not None else None
            return ProductionSynthesisExecution(
                status=SynthesisExecutionStatus.NEEDS_REVIEW,
                mode=mode,
                model_run_id=run_id,
                input_hash=input_hash,
                extraction_hash=extraction_hash,
                model_calls=1,
                error_code=error_code,
                error=error,
                details=payload,
            )

        try:
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
        except StructuredOutputError as exc:
            return reviewed(
                SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                "The provider answer is not a structured synthesis proposal.",
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

        proposal = execution.structured_output
        if not isinstance(proposal, SynthesisProposalV1):
            # A malformed answer is never re-asked and never parsed loosely.
            return reviewed(
                SynthesisProposalErrorCode.OUTPUT_INVALID.value,
                "The provider answer lacks a validated synthesis proposal.",
                run_id=model_run.id,
                details=self._model_evidence(model_run),
            )
        try:
            lead, sections = validate_synthesis_proposal(proposal, evidence_pack, extraction)
        except SynthesisProposalControlError as exc:
            return reviewed(
                exc.code.value,
                f"The synthesis proposal failed {exc.code.value}.",
                run_id=model_run.id,
                details=self._model_evidence(model_run),
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
            timeline=build_synthesis_timeline(extraction),
            uncertainties=build_synthesis_uncertainties(extraction),
            warnings=_bounded_synthesis_warnings(extraction),
        )
        validate_synthesis_lineage(synthesis, snapshot, extraction_hash)
        artifact = await self._synthesis_service.store_synthesis_result(
            run_id=run.id,
            subject_id=snapshot.subject_id,
            input_hash=input_hash,
            synthesis=synthesis,
            extraction=extraction,
            raw_result=execution.output_text,
            model_run_id=model_run.id,
            mode=mode,
            model_policy_version=SYNTHESIS_MODEL_POLICY_VERSION,
            routing_policy_version=SYNTHESIS_ROUTING_POLICY_VERSION,
        )
        return ProductionSynthesisExecution(
            status=SynthesisExecutionStatus.SUCCEEDED,
            mode=mode,
            artifact_id=artifact.id,
            model_run_id=model_run.id,
            input_hash=input_hash,
            extraction_hash=extraction_hash,
            model_calls=1,
            details=_synthesis_counts(synthesis),
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
