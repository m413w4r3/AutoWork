"""Canonical, provider-independent contracts for production synthesis."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_to_json,
)

PRODUCTION_SYNTHESIS_SCHEMA_VERSION = 1
SYNTHESIS_EVIDENCE_REF_ALGORITHM_VERSION = "sha256-canonical-extraction-element-v1"
SYNTHESIS_POLICY_VERSION = "production-synthesis-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    INDICATOR = "indicator"
    RULE = "rule"


class SynthesisSectionKind(StrEnum):
    OVERVIEW = "overview"
    CAMPAIGN = "campaign"
    INFECTION_CHAIN = "infection_chain"
    TECHNICAL = "technical"
    VICTIMOLOGY = "victimology"
    INFRASTRUCTURE = "infrastructure"
    DETECTION = "detection"
    IMPACT = "impact"
    OTHER = "other"


def _require_semantic_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def evidence_ref_sort_key(ref: ExtractionEvidenceRefV1) -> tuple[str, str, str]:
    """The one deterministic ordering of evidence refs."""
    return (str(ref.source_document_id), ref.kind.value, ref.evidence_key)


def _normalize_evidence_refs(
    refs: tuple[ExtractionEvidenceRefV1, ...], label: str
) -> tuple[ExtractionEvidenceRefV1, ...]:
    if not isinstance(refs, tuple) or not refs:
        raise ValueError(f"{label} requires a tuple of evidence references")
    if any(not isinstance(ref, ExtractionEvidenceRefV1) for ref in refs):
        raise ValueError(f"{label} evidence references have an invalid type")
    if len(set(refs)) != len(refs):
        raise ValueError(f"{label} must not repeat evidence references")
    return tuple(sorted(refs, key=evidence_ref_sort_key))


@dataclass(frozen=True, slots=True)
class ExtractionEvidenceRefV1:
    source_document_id: UUID
    kind: EvidenceKind
    evidence_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Evidence source document identity must be a UUID")
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("Evidence kind is invalid")
        if not isinstance(self.evidence_key, str) or _SHA256.fullmatch(self.evidence_key) is None:
            raise ValueError("Evidence key must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class SynthesisParagraphV1:
    text: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        _require_semantic_text(self.text, "Synthesis paragraph text")
        object.__setattr__(
            self, "evidence_refs", _normalize_evidence_refs(self.evidence_refs, "Paragraph")
        )


@dataclass(frozen=True, slots=True)
class SynthesisSectionV1:
    kind: SynthesisSectionKind
    heading: str
    paragraphs: tuple[SynthesisParagraphV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SynthesisSectionKind):
            raise ValueError("Synthesis section kind is invalid")
        _require_semantic_text(self.heading, "Synthesis section heading")
        if not isinstance(self.paragraphs, tuple) or not self.paragraphs:
            raise ValueError("Synthesis section paragraphs must be a non-empty tuple")
        if any(not isinstance(paragraph, SynthesisParagraphV1) for paragraph in self.paragraphs):
            raise ValueError("Synthesis section paragraphs have an invalid type")


@dataclass(frozen=True, slots=True)
class SynthesisTimelineEntryV1:
    event_date: date | None
    date_text: str | None
    text: str
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if self.event_date is not None and type(self.event_date) is not date:
            raise ValueError("Timeline event date must be a date or None")
        if self.date_text is not None:
            _require_semantic_text(self.date_text, "Timeline date text")
        _require_semantic_text(self.text, "Timeline text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_evidence_refs(self.evidence_refs, "Timeline entry"),
        )


@dataclass(frozen=True, slots=True)
class SynthesisUncertaintyV1:
    text: str
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _require_semantic_text(self.text, "Synthesis uncertainty text")
        if not isinstance(self.source_document_ids, tuple) or not self.source_document_ids:
            raise ValueError("Synthesis uncertainty requires source document identities")
        if any(not isinstance(document_id, UUID) for document_id in self.source_document_ids):
            raise ValueError("Synthesis uncertainty source identities must be UUIDs")
        if len(set(self.source_document_ids)) != len(self.source_document_ids):
            raise ValueError("Synthesis uncertainty must not repeat source identities")
        object.__setattr__(
            self,
            "source_document_ids",
            tuple(sorted(self.source_document_ids, key=str)),
        )


_MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "janv": 1,
    "janvier": 1,
    "feb": 2,
    "february": 2,
    "fev": 2,
    "fevr": 2,
    "fevrier": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juin": 6,
    "jul": 7,
    "july": 7,
    "juil": 7,
    "juillet": 7,
    "aug": 8,
    "august": 8,
    "aout": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "octobre": 10,
    "nov": 11,
    "november": 11,
    "novembre": 11,
    "dec": 12,
    "december": 12,
    "decembre": 12,
}
_MONTH_PATTERN = "|".join(
    re.escape(month) for month in sorted(_MONTH_NUMBERS, key=len, reverse=True)
)


_PERIOD_MONTHS = (
    (r"\b[hs]1\b|\bfirst half\b|\bpremier semestre\b", 1),
    (r"\b[hs]2\b|\bsecond half\b|\b(?:second|deuxieme) semestre\b", 7),
    (r"\bspring\b|\bprintemps\b", 3),
    (r"\bsummer\b|\bete\b", 6),
    (r"\bautumn\b|\bfall\b|\bautomne\b", 9),
    (r"\bwinter\b|\bhiver\b", 12),
)


def _fold_temporal_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unaccented).split())


def _date_from_parts(year: str, month: int, day: int) -> date | None:
    try:
        return date(int(year), month, day)
    except ValueError:
        return None


def _year_approximation(year: str, qualifier: str | None) -> date | None:
    month = {"early": 1, "mid": 7, "late": 10, None: 1}.get(qualifier)
    return None if month is None else _date_from_parts(year, month, 1)


def resolve_timeline_date_text(value: str | None) -> date | None:
    """Resolve common English and French date wording to a sorting approximation."""
    if not value:
        return None
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", value)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(0))
        except ValueError:
            return None

    text = _fold_temporal_text(value)
    quarter_match = re.search(r"\b([qt])\s*([1-4])\s+(\d{4})\b", text)
    if quarter_match is None:
        quarter_match = re.search(r"\b(\d{4})\s+[qt]\s*([1-4])\b", text)
        if quarter_match:
            year, quarter = quarter_match.groups()
        else:
            year = quarter = ""
    else:
        _, quarter, year = quarter_match.groups()
    if quarter_match:
        return _date_from_parts(year, (int(quarter) - 1) * 3 + 1, 1)

    period_year = re.search(r"\b(\d{4})\b", text)
    if period_year is not None:
        for pattern, month in _PERIOD_MONTHS:
            if re.search(pattern, text):
                return _date_from_parts(period_year.group(1), month, 1)

    day_month_match = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_PATTERN})\s+(\d{{4}})\b", text)
    if day_month_match:
        day, month_name, year = day_month_match.groups()
        return _date_from_parts(year, _MONTH_NUMBERS[month_name], int(day))
    month_day_match = re.search(rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})\s+(\d{{4}})\b", text)
    if month_day_match:
        month_name, day, year = month_day_match.groups()
        return _date_from_parts(year, _MONTH_NUMBERS[month_name], int(day))

    month_year_match = re.search(rf"\b({_MONTH_PATTERN})\s+(\d{{4}})\b", text)
    if month_year_match:
        month_name, year = month_year_match.groups()
        prefix = text[: month_year_match.start()].strip()
        qualifier = {
            "early": "early",
            "beginning": "early",
            "start": "early",
            "debut": "early",
            "debut de": "early",
            "debut du": "early",
            "mid": "mid",
            "middle": "mid",
            "mi": "mid",
            "milieu": "mid",
            "milieu de": "mid",
            "milieu du": "mid",
            "late": "late",
            "end": "late",
            "fin": "late",
            "fin de": "late",
            "fin du": "late",
        }.get(prefix)
        day = {"early": 1, "mid": 15, "late": 25, None: 1}.get(qualifier)
        return _date_from_parts(year, _MONTH_NUMBERS[month_name], day or 1)

    year_match = re.search(r"\b(\d{4})\b", text)
    if year_match is None:
        return None
    year = year_match.group(1)
    prefix = text[: year_match.start()].strip()
    suffix = text[year_match.end() :].strip()
    if suffix:
        return None
    qualifier = {
        "": None,
        "in": None,
        "during": None,
        "around": None,
        "about": None,
        "circa": None,
        "en": None,
        "vers": None,
        "early": "early",
        "beginning": "early",
        "start": "early",
        "debut": "early",
        "debut de": "early",
        "debut du": "early",
        "mid": "mid",
        "middle": "mid",
        "mi": "mid",
        "milieu": "mid",
        "milieu de": "mid",
        "milieu du": "mid",
        "late": "late",
        "end": "late",
        "fin": "late",
        "fin de": "late",
        "fin du": "late",
    }.get(prefix, "unrecognized")
    if qualifier == "unrecognized":
        return None
    return _year_approximation(year, qualifier)


def _timeline_text_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", unaccented))


def timeline_sort_key(
    entry: SynthesisTimelineEntryV1,
) -> tuple[int, date, int, tuple[tuple[str, str, str], ...], str, str, str, str]:
    """Sort by exact or approximated date, with a deterministic total tie-break."""
    sort_date = entry.event_date or resolve_timeline_date_text(entry.date_text)
    if sort_date is not None:
        date_bucket = 0
    elif entry.date_text is None:
        date_bucket = 1
    else:
        date_bucket = 2
    return (
        date_bucket,
        sort_date or date.max,
        int(entry.event_date is None),
        tuple(evidence_ref_sort_key(ref) for ref in entry.evidence_refs),
        _timeline_text_key(entry.date_text or ""),
        _timeline_text_key(entry.text),
        entry.date_text or "",
        entry.text,
    )


@dataclass(frozen=True, slots=True)
class ProductionSynthesisV1:
    schema_version: int
    subject_id: UUID
    production_input_hash: str
    extraction_hash: str
    publication_language: str
    synthesis_policy_version: str
    title: str
    lead: tuple[SynthesisParagraphV1, ...]
    sections: tuple[SynthesisSectionV1, ...]
    timeline: tuple[SynthesisTimelineEntryV1, ...]
    uncertainties: tuple[SynthesisUncertaintyV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or (
            self.schema_version != PRODUCTION_SYNTHESIS_SCHEMA_VERSION
        ):
            raise ValueError("Production synthesis schema version is unsupported")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Production synthesis subject identity must be a UUID")
        for field_name in ("production_input_hash", "extraction_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"Production synthesis {field_name} must be a lowercase SHA-256")
        _require_semantic_text(self.publication_language, "Publication language")
        _require_semantic_text(self.title, "Synthesis title")
        if self.synthesis_policy_version != SYNTHESIS_POLICY_VERSION:
            raise ValueError("Production synthesis policy version is unsupported")
        for field_name, values, item_type in (
            ("lead", self.lead, SynthesisParagraphV1),
            ("sections", self.sections, SynthesisSectionV1),
            ("timeline", self.timeline, SynthesisTimelineEntryV1),
            ("uncertainties", self.uncertainties, SynthesisUncertaintyV1),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, item_type) for value in values
            ):
                raise ValueError(f"Production synthesis {field_name} have an invalid type")
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) or not warning.strip() for warning in self.warnings
        ):
            raise ValueError("Production synthesis warnings must be non-empty strings")

        object.__setattr__(
            self,
            "timeline",
            tuple(sorted(set(self.timeline), key=timeline_sort_key)),
        )
        normalized_uncertainties = tuple(
            sorted(
                set(self.uncertainties),
                key=lambda item: (item.text, tuple(map(str, item.source_document_ids))),
            )
        )
        object.__setattr__(self, "uncertainties", normalized_uncertainties)
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))


def synthesis_evidence_refs(
    synthesis: ProductionSynthesisV1,
) -> frozenset[ExtractionEvidenceRefV1]:
    """Return every evidence ref cited by the lead, the sections and the timeline."""
    paragraphs = (
        *synthesis.lead,
        *(paragraph for section in synthesis.sections for paragraph in section.paragraphs),
    )
    return frozenset(
        (
            *(ref for paragraph in paragraphs for ref in paragraph.evidence_refs),
            *(ref for entry in synthesis.timeline for ref in entry.evidence_refs),
        )
    )


def extraction_evidence_refs_v1(
    extraction: ProductionExtractionV1,
) -> tuple[ExtractionEvidenceRefV1, ...]:
    """Build stable refs from each canonical source-local extraction element."""
    refs = {ref for ref, _payload in extraction_evidence_elements(extraction)}
    return tuple(sorted(refs, key=evidence_ref_sort_key))


_EVIDENCE_PAYLOAD_KEYS = (
    (EvidenceKind.FACT, "facts"),
    (EvidenceKind.EVENT, "events"),
    (EvidenceKind.INDICATOR, "indicators"),
    (EvidenceKind.RULE, "rules"),
)


def extraction_evidence_ref(
    source_document_id: UUID, kind: EvidenceKind, payload: Mapping[str, Any]
) -> ExtractionEvidenceRefV1:
    """Identify one element by its owning source, kind and canonical payload."""
    encoded = json.dumps(
        {"source_document_id": str(source_document_id), "kind": kind.value, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ExtractionEvidenceRefV1(
        source_document_id=source_document_id,
        kind=kind,
        evidence_key=hashlib.sha256(encoded).hexdigest(),
    )


def extraction_evidence_elements(
    extraction: ProductionExtractionV1,
) -> tuple[tuple[ExtractionEvidenceRefV1, Mapping[str, Any]], ...]:
    """Pair each source-local element with its ref, in canonical extraction order."""
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    payload = production_extraction_to_json(extraction)
    return tuple(
        (extraction_evidence_ref(source.source_document_id, kind, element), element)
        for source, source_payload in zip(extraction.sources, payload["sources"], strict=True)
        for kind, list_key in _EVIDENCE_PAYLOAD_KEYS
        for element in source_payload[list_key]
    )


def _ref_to_json(ref: ExtractionEvidenceRefV1) -> dict[str, str]:
    return {
        "source_document_id": str(ref.source_document_id),
        "kind": ref.kind.value,
        "evidence_key": ref.evidence_key,
    }


def _paragraph_to_json(paragraph: SynthesisParagraphV1) -> dict[str, Any]:
    return {
        "text": paragraph.text,
        "evidence_refs": [_ref_to_json(ref) for ref in paragraph.evidence_refs],
    }


def _section_to_json(section: SynthesisSectionV1) -> dict[str, Any]:
    return {
        "kind": section.kind.value,
        "heading": section.heading,
        "paragraphs": [_paragraph_to_json(paragraph) for paragraph in section.paragraphs],
    }


def _timeline_to_json(entry: SynthesisTimelineEntryV1) -> dict[str, Any]:
    return {
        "event_date": entry.event_date.isoformat() if entry.event_date is not None else None,
        "date_text": entry.date_text,
        "text": entry.text,
        "evidence_refs": [_ref_to_json(ref) for ref in entry.evidence_refs],
    }


def _uncertainty_to_json(uncertainty: SynthesisUncertaintyV1) -> dict[str, Any]:
    return {
        "text": uncertainty.text,
        "source_document_ids": [
            str(document_id) for document_id in uncertainty.source_document_ids
        ],
    }


def production_synthesis_to_json(synthesis: ProductionSynthesisV1) -> dict[str, Any]:
    """Return the strict JSON-compatible canonical representation."""
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    return {
        "schema_version": synthesis.schema_version,
        "subject_id": str(synthesis.subject_id),
        "production_input_hash": synthesis.production_input_hash,
        "extraction_hash": synthesis.extraction_hash,
        "publication_language": synthesis.publication_language,
        "synthesis_policy_version": synthesis.synthesis_policy_version,
        "title": synthesis.title,
        "lead": [_paragraph_to_json(paragraph) for paragraph in synthesis.lead],
        "sections": [_section_to_json(section) for section in synthesis.sections],
        "timeline": [_timeline_to_json(entry) for entry in synthesis.timeline],
        "uncertainties": [_uncertainty_to_json(item) for item in synthesis.uncertainties],
        "warnings": list(synthesis.warnings),
    }


_SYNTHESIS_KEYS = frozenset(
    {
        "schema_version",
        "subject_id",
        "production_input_hash",
        "extraction_hash",
        "publication_language",
        "synthesis_policy_version",
        "title",
        "lead",
        "sections",
        "timeline",
        "uncertainties",
        "warnings",
    }
)
_REF_KEYS = frozenset({"source_document_id", "kind", "evidence_key"})
_PARAGRAPH_KEYS = frozenset({"text", "evidence_refs"})
_SECTION_KEYS = frozenset({"kind", "heading", "paragraphs"})
_TIMELINE_KEYS = frozenset({"event_date", "date_text", "text", "evidence_refs"})
_UNCERTAINTY_KEYS = frozenset({"text", "source_document_ids"})


def _object(raw: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError(f"{label} has missing or extra fields")
    return raw


def _array(raw: Any, label: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a JSON array")
    return raw


def _text(raw: Any, label: str, *, semantic: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be text")
    if semantic:
        _require_semantic_text(raw, label)
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


def _ref_from_json(raw: Any) -> ExtractionEvidenceRefV1:
    payload = _object(raw, _REF_KEYS, "Evidence reference")
    try:
        kind = EvidenceKind(_text(payload["kind"], "Evidence kind"))
    except ValueError as exc:
        raise ValueError("Evidence kind is invalid") from exc
    return ExtractionEvidenceRefV1(
        source_document_id=_uuid(payload["source_document_id"], "Evidence source document ID"),
        kind=kind,
        evidence_key=_sha256(payload["evidence_key"], "Evidence key"),
    )


def _refs_from_json(raw: Any, label: str) -> tuple[ExtractionEvidenceRefV1, ...]:
    refs = tuple(_ref_from_json(value) for value in _array(raw, label))
    if not refs:
        raise ValueError(f"{label} must not be empty")
    return refs


def _paragraph_from_json(raw: Any) -> SynthesisParagraphV1:
    payload = _object(raw, _PARAGRAPH_KEYS, "Synthesis paragraph")
    return SynthesisParagraphV1(
        text=_text(payload["text"], "Paragraph text", semantic=True),
        evidence_refs=_refs_from_json(payload["evidence_refs"], "Paragraph evidence references"),
    )


def _section_from_json(raw: Any) -> SynthesisSectionV1:
    payload = _object(raw, _SECTION_KEYS, "Synthesis section")
    try:
        kind = SynthesisSectionKind(_text(payload["kind"], "Section kind"))
    except ValueError as exc:
        raise ValueError("Synthesis section kind is invalid") from exc
    paragraphs = tuple(
        _paragraph_from_json(value) for value in _array(payload["paragraphs"], "Section paragraphs")
    )
    return SynthesisSectionV1(
        kind=kind,
        heading=_text(payload["heading"], "Section heading", semantic=True),
        paragraphs=paragraphs,
    )


def _date(raw: Any, label: str) -> date | None:
    if raw is None:
        return None
    value = _text(raw, label)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date or null") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use canonical ISO date form")
    return parsed


def _timeline_from_json(raw: Any) -> SynthesisTimelineEntryV1:
    payload = _object(raw, _TIMELINE_KEYS, "Synthesis timeline entry")
    date_text = payload["date_text"]
    if date_text is not None:
        date_text = _text(date_text, "Timeline date text", semantic=True)
    return SynthesisTimelineEntryV1(
        event_date=_date(payload["event_date"], "Timeline event date"),
        date_text=date_text,
        text=_text(payload["text"], "Timeline text", semantic=True),
        evidence_refs=_refs_from_json(payload["evidence_refs"], "Timeline evidence references"),
    )


def _uncertainty_from_json(raw: Any) -> SynthesisUncertaintyV1:
    payload = _object(raw, _UNCERTAINTY_KEYS, "Synthesis uncertainty")
    source_ids = tuple(
        _uuid(value, "Uncertainty source document ID")
        for value in _array(payload["source_document_ids"], "Uncertainty source document IDs")
    )
    return SynthesisUncertaintyV1(
        text=_text(payload["text"], "Uncertainty text", semantic=True),
        source_document_ids=source_ids,
    )


def production_synthesis_from_json(raw: Mapping[str, Any]) -> ProductionSynthesisV1:
    """Decode only exact V1 payloads; no unknown fields or coercion are allowed."""
    payload = _object(raw, _SYNTHESIS_KEYS, "Production synthesis")
    lead = tuple(_paragraph_from_json(value) for value in _array(payload["lead"], "Synthesis lead"))
    sections = tuple(
        _section_from_json(value) for value in _array(payload["sections"], "Synthesis sections")
    )
    timeline = tuple(
        _timeline_from_json(value) for value in _array(payload["timeline"], "Synthesis timeline")
    )
    uncertainties = tuple(
        _uncertainty_from_json(value)
        for value in _array(payload["uncertainties"], "Synthesis uncertainties")
    )
    warnings = tuple(
        _text(value, "Synthesis warning", semantic=True)
        for value in _array(payload["warnings"], "Synthesis warnings")
    )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int:
        raise ValueError("Production synthesis schema version must be an integer")
    return ProductionSynthesisV1(
        schema_version=schema_version,
        subject_id=_uuid(payload["subject_id"], "Subject ID"),
        production_input_hash=_sha256(payload["production_input_hash"], "Production input hash"),
        extraction_hash=_sha256(payload["extraction_hash"], "Extraction hash"),
        publication_language=_text(
            payload["publication_language"], "Publication language", semantic=True
        ),
        synthesis_policy_version=_text(
            payload["synthesis_policy_version"], "Synthesis policy version"
        ),
        title=_text(payload["title"], "Synthesis title", semantic=True),
        lead=lead,
        sections=sections,
        timeline=timeline,
        uncertainties=uncertainties,
        warnings=warnings,
    )


def validate_synthesis_lineage(
    synthesis: ProductionSynthesisV1,
    snapshot: ProductionInputSnapshot,
    canonical_extraction_hash: str,
) -> None:
    """Raise when synthesis lineage differs from its frozen inputs."""
    if not isinstance(synthesis, ProductionSynthesisV1):
        raise ValueError("Expected a ProductionSynthesisV1")
    if not isinstance(snapshot, ProductionInputSnapshot):
        raise ValueError("Expected a ProductionInputSnapshot")
    expected_extraction_hash = _sha256(canonical_extraction_hash, "Canonical extraction hash")
    expected = {
        "subject_id": snapshot.subject_id,
        "production_input_hash": snapshot.input_hash,
        "publication_language": snapshot.publication_language,
        "title": snapshot.subject_title,
        "extraction_hash": expected_extraction_hash,
    }
    for field_name, expected_value in expected.items():
        if getattr(synthesis, field_name) != expected_value:
            raise ValueError(f"Synthesis {field_name} does not match its frozen lineage")
