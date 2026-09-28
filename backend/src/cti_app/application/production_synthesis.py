"""Deterministic evidence inputs and preview for canonical synthesis."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any
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
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    extraction_evidence_refs_v1,
)

SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION = "synthesis-evidence-pack-v1-technical-cap-128"
MAX_TECHNICAL_EVIDENCE_V1 = 128

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
