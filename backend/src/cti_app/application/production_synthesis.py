"""Deterministic evidence inputs and preview for canonical synthesis."""

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
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from cti_app.domain.production import ExtractionProfile, ProductionInputSnapshot
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    extraction_evidence_refs_v1,
)

SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION = "synthesis-evidence-pack-v1-technical-cap-128"
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


@dataclass(frozen=True, slots=True)
class SynthesisClaimProposalV1:
    text: str
    evidence_handles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Claim text must be non-empty text")
        if (
            not isinstance(self.evidence_handles, tuple)
            or not self.evidence_handles
            or any(not isinstance(handle, str) or not handle for handle in self.evidence_handles)
            or len(set(self.evidence_handles)) != len(self.evidence_handles)
        ):
            raise ValueError("Claim evidence handles must be a non-empty unique tuple")


@dataclass(frozen=True, slots=True)
class SynthesisSectionProposalV1:
    kind: SynthesisSectionKind
    heading: str
    claims: tuple[SynthesisClaimProposalV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SynthesisSectionKind):
            raise ValueError("Section kind is invalid")
        if not isinstance(self.heading, str) or not self.heading.strip():
            raise ValueError("Section heading must be non-empty text")
        if (
            not isinstance(self.claims, tuple)
            or not self.claims
            or any(not isinstance(claim, SynthesisClaimProposalV1) for claim in self.claims)
        ):
            raise ValueError("Section claims must be a non-empty tuple")


@dataclass(frozen=True, slots=True)
class SynthesisProposalV1:
    lead: tuple[SynthesisClaimProposalV1, ...]
    sections: tuple[SynthesisSectionProposalV1, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lead, tuple)
            or not self.lead
            or any(not isinstance(claim, SynthesisClaimProposalV1) for claim in self.lead)
        ):
            raise ValueError("Proposal lead must be a non-empty tuple")
        if not isinstance(self.sections, tuple) or any(
            not isinstance(section, SynthesisSectionProposalV1) for section in self.sections
        ):
            raise ValueError("Proposal sections must be a tuple")


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
