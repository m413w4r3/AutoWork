"""Canonical extraction contracts for editorial production (AW-011).

A model answer is only a source-local proposal.  Everything in this module is
the deterministic result of the extraction stage: exact archived documents,
explicit profiles, local evidence and canonical provenance.  It is pure data —
no I/O, no provider, no run or checkpoint lifecycle state lives here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
)
from cti_app.domain.production_references import (
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.publication import ArtifactType

PRODUCTION_EXTRACTION_SCHEMA_VERSION = 1

#: AW-011 profile policy: ``ProductionReferenceTier`` alone decides FULL vs
#: IOC_RULES, never ``SourceRole``.  It participates in functional hashes.
EXTRACTION_PROFILE_POLICY_VERSION = "production-reference-tier-v1"

EXTRACTION_TIER_ORDER: dict[ProductionReferenceTier, int] = {
    ProductionReferenceTier.CORE: 0,
    ProductionReferenceTier.SUPPORTING: 1,
    ProductionReferenceTier.TECHNICAL: 2,
}

EXTRACTION_PROFILE_BY_TIER: dict[ProductionReferenceTier, ExtractionProfile] = {
    ProductionReferenceTier.CORE: ExtractionProfile.FULL,
    ProductionReferenceTier.SUPPORTING: ExtractionProfile.IOC_RULES,
    ProductionReferenceTier.TECHNICAL: ExtractionProfile.IOC_RULES,
}

#: Categories a FULL extraction must be able to carry.  CVE/IOC technical
#: artefacts live in ``indicators``, published detection rules in ``rules``
#: and chronology in ``events``.
EXTRACTION_FACT_CATEGORIES: tuple[str, ...] = (
    "actors",
    "campaigns",
    "malware",
    "tools",
    "products",
    "infection_chain",
    "ttps",
    "victimology",
    "protocols",
    "infrastructure",
    "files",
    "commands",
    "persistence",
    "detections",
    "sectors",
    "countries",
    "other_technical",
)
_FACT_CATEGORIES = frozenset(EXTRACTION_FACT_CATEGORIES)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTACK_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")


class ExtractionReuseState(StrEnum):
    """How the canonical result of one source was obtained."""

    #: Computed by the model during this run.
    FRESH = "fresh"
    #: Satisfied by a durable source checkpoint; no model call.
    REUSED = "reused"
    #: Another source of the run carries the exact same bytes.
    CONTENT_DUPLICATE = "content_duplicate"


class ExtractionIndicatorStatus(StrEnum):
    """Canonical status of one published technical artefact."""

    CONFIRMED_IOC = "confirmed_ioc"
    CONTEXTUAL = "contextual"


class ProductionExtractionOmissionReason(StrEnum):
    """Why a corpus source stayed out of the extraction plan."""

    #: ``eligible_for_extraction`` was False in the REFERENCES corpus.
    REFERENCE_NOT_ELIGIBLE = "reference_not_eligible"
    #: A SUPPORTING/TECHNICAL source failed source-locally; CORE never omits.
    SOURCE_EXTRACTION_FAILED = "source_extraction_failed"


def extraction_profile_for_tier(tier: ProductionReferenceTier) -> ExtractionProfile:
    """The AW-011 profile policy, keyed exclusively by the frozen tier."""
    try:
        return EXTRACTION_PROFILE_BY_TIER[tier]
    except (KeyError, TypeError) as exc:
        raise ValueError("Extraction tier is invalid") from exc


def _require_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or None")
    return value


def _normalize_provenance(value: Any, *, label: str) -> tuple[UUID, ...]:
    """Canonical provenance: unique source document identities, sorted."""
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{label} requires at least one source document identity")
    if any(not isinstance(item, UUID) for item in value):
        raise ValueError(f"{label} provenance must be UUIDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} provenance must not repeat a source document")
    return tuple(sorted(value, key=str))


def _validate_evidence(
    *,
    evidence_quote: Any,
    evidence_basis: Any,
    source_document_ids: Any,
    label: str,
) -> tuple[tuple[UUID, ...], str]:
    if not isinstance(evidence_basis, ProductionEvidenceBasis):
        raise ValueError(f"{label} evidence basis is invalid")
    if not isinstance(evidence_quote, str):
        raise ValueError(f"{label} evidence quote must be text")
    if evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED and not evidence_quote.strip():
        raise ValueError(f"{label} requires a local source evidence quote")
    return _normalize_provenance(source_document_ids, label=label), evidence_quote


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionFactV1:
    """One source-verified structured fact."""

    category: str
    value: str
    attack_id: str | None
    context: str
    evidence_quote: str
    evidence_basis: ProductionEvidenceBasis
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.category not in _FACT_CATEGORIES:
            raise ValueError("Extraction fact category is invalid")
        _require_text(self.value, label="Extraction fact value")
        if self.attack_id is not None and (
            not isinstance(self.attack_id, str) or _ATTACK_ID.fullmatch(self.attack_id) is None
        ):
            raise ValueError("Extraction fact ATT&CK identifier is invalid")
        if not isinstance(self.context, str):
            raise ValueError("Extraction fact context must be text")
        ids, quote = _validate_evidence(
            evidence_quote=self.evidence_quote,
            evidence_basis=self.evidence_basis,
            source_document_ids=self.source_document_ids,
            label="Extraction fact",
        )
        object.__setattr__(self, "source_document_ids", ids)
        object.__setattr__(self, "evidence_quote", quote)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionEventV1:
    """One dated or undated event of the canonical chronology."""

    event_date: date | None
    date_text: str | None
    text: str
    context: str
    evidence_quote: str
    evidence_basis: ProductionEvidenceBasis
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.event_date is not None and (
            not isinstance(self.event_date, date) or hasattr(self.event_date, "hour")
        ):
            raise ValueError("Extraction event date must be a date or None")
        if self.date_text is not None and not isinstance(self.date_text, str):
            raise ValueError("Extraction event date text must be text or None")
        _require_text(self.text, label="Extraction event text")
        if not isinstance(self.context, str):
            raise ValueError("Extraction event context must be text")
        ids, quote = _validate_evidence(
            evidence_quote=self.evidence_quote,
            evidence_basis=self.evidence_basis,
            source_document_ids=self.source_document_ids,
            label="Extraction event",
        )
        object.__setattr__(self, "source_document_ids", ids)
        object.__setattr__(self, "evidence_quote", quote)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionIndicatorV1:
    """One published technical artefact (IOC or contextual value)."""

    value: str
    artifact_type: ArtifactType
    indicator_status: ExtractionIndicatorStatus
    context: str
    evidence_quote: str
    evidence_basis: ProductionEvidenceBasis
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _require_text(self.value, label="Extraction indicator value")
        if not isinstance(self.artifact_type, ArtifactType):
            raise ValueError("Extraction indicator type is invalid")
        if not isinstance(self.indicator_status, ExtractionIndicatorStatus):
            raise ValueError("Extraction indicator status is invalid")
        if not isinstance(self.context, str):
            raise ValueError("Extraction indicator context must be text")
        ids, quote = _validate_evidence(
            evidence_quote=self.evidence_quote,
            evidence_basis=self.evidence_basis,
            source_document_ids=self.source_document_ids,
            label="Extraction indicator",
        )
        object.__setattr__(self, "source_document_ids", ids)
        object.__setattr__(self, "evidence_quote", quote)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionRuleV1:
    """One detection rule published inside the analysed document."""

    rule_type: DetectionRuleType
    name: str | None
    body: str
    sha256: str
    context: str
    evidence_quote: str
    evidence_basis: ProductionEvidenceBasis
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rule_type, DetectionRuleType):
            raise ValueError("Extraction rule type is invalid")
        _optional_text(self.name, label="Extraction rule name")
        _require_text(self.body, label="Extraction rule body")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("Extraction rule hash must be a lowercase SHA-256")
        if self.sha256 != hashlib.sha256(self.body.encode("utf-8")).hexdigest():
            raise ValueError("Extraction rule hash does not match its body")
        if not isinstance(self.context, str):
            raise ValueError("Extraction rule context must be text")
        ids, quote = _validate_evidence(
            evidence_quote=self.evidence_quote,
            evidence_basis=self.evidence_basis,
            source_document_ids=self.source_document_ids,
            label="Extraction rule",
        )
        object.__setattr__(self, "source_document_ids", ids)
        object.__setattr__(self, "evidence_quote", quote)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionSourceExtractionV1:
    """The canonical result of one exact archived document."""

    source_document_id: UUID
    canonical_url: str
    content_sha256: str
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole
    profile: ExtractionProfile
    checkpoint_id: UUID | None
    reuse_state: ExtractionReuseState
    facts: tuple[ExtractionFactV1, ...]
    events: tuple[ExtractionEventV1, ...]
    indicators: tuple[ExtractionIndicatorV1, ...]
    rules: tuple[ExtractionRuleV1, ...]
    uncertainties: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Extraction source document identity must be a UUID")
        try:
            canonical = canonicalize_http_url(self.canonical_url)
        except (AttributeError, ValueError) as exc:
            raise ValueError("Extraction source URL must be a canonical HTTP(S) URL") from exc
        if canonical != self.canonical_url:
            raise ValueError("Extraction source URL must be canonical")
        if (
            not isinstance(self.content_sha256, str)
            or _SHA256.fullmatch(self.content_sha256) is None
        ):
            raise ValueError("Extraction source content hash must be a lowercase SHA-256")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("Extraction source tier is invalid")
        if not isinstance(self.kind, ProductionReferenceKind):
            raise ValueError("Extraction source kind is invalid")
        if not isinstance(self.role, SourceRole):
            raise ValueError("Extraction source role is invalid")
        if not isinstance(self.profile, ExtractionProfile):
            raise ValueError("Extraction source profile is invalid")
        if self.profile is not extraction_profile_for_tier(self.tier):
            raise ValueError("Extraction source profile does not match its reference tier")
        if self.checkpoint_id is not None and not isinstance(self.checkpoint_id, UUID):
            raise ValueError("Extraction checkpoint identity must be a UUID or None")
        if not isinstance(self.reuse_state, ExtractionReuseState):
            raise ValueError("Extraction reuse state is invalid")
        for label, items, item_type in (
            ("facts", self.facts, ExtractionFactV1),
            ("events", self.events, ExtractionEventV1),
            ("indicators", self.indicators, ExtractionIndicatorV1),
            ("rules", self.rules, ExtractionRuleV1),
        ):
            if not isinstance(items, tuple) or any(
                not isinstance(item, item_type) for item in items
            ):
                raise ValueError(
                    f"Extraction source {label} must be a tuple of {item_type.__name__}"
                )
            for item in items:
                if self.source_document_id not in item.source_document_ids:
                    raise ValueError(
                        f"Extraction source {label} provenance must include its own document"
                    )
        if not isinstance(self.uncertainties, tuple) or any(
            not isinstance(uncertainty, str) for uncertainty in self.uncertainties
        ):
            raise ValueError("Extraction uncertainties must be a tuple of strings")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionExtractionOmissionV1:
    """One corpus source without a canonical extraction, and why."""

    canonical_url: str
    tier: ProductionReferenceTier
    collection_state: CollectionState
    reason: ProductionExtractionOmissionReason
    #: The source-local failure code; set only for a failed extraction.
    error_code: str | None

    def __post_init__(self) -> None:
        try:
            canonical = canonicalize_http_url(self.canonical_url)
        except (AttributeError, ValueError) as exc:
            raise ValueError("Omitted source URL must be a canonical HTTP(S) URL") from exc
        if canonical != self.canonical_url:
            raise ValueError("Omitted source URL must be canonical")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("Omitted source tier is invalid")
        if not isinstance(self.collection_state, CollectionState):
            raise ValueError("Omitted source collection state is invalid")
        if not isinstance(self.reason, ProductionExtractionOmissionReason):
            raise ValueError("Omitted source reason is invalid")
        failed = self.reason is ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED
        if failed and self.tier is ProductionReferenceTier.CORE:
            raise ValueError("A CORE source failure blocks the stage and is never omitted")
        if failed != (isinstance(self.error_code, str) and bool(self.error_code.strip())):
            raise ValueError("Only a failed source omission carries an error code")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionExtractionV1:
    """The canonical, provider-independent artifact of the EXTRACTION stage."""

    schema_version: int
    subject_id: UUID
    production_input_hash: str
    references_corpus_hash: str
    profile_policy_version: str
    sources: tuple[ProductionSourceExtractionV1, ...]
    omitted_sources: tuple[ProductionExtractionOmissionV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or (
            self.schema_version != PRODUCTION_EXTRACTION_SCHEMA_VERSION
        ):
            raise ValueError("Production extraction schema version must be 1")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Production extraction subject identity must be a UUID")
        for field_name in ("production_input_hash", "references_corpus_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"Production extraction {field_name} must be a lowercase SHA-256")
        if self.profile_policy_version != EXTRACTION_PROFILE_POLICY_VERSION:
            raise ValueError("Production extraction profile policy version is incompatible")
        if not isinstance(self.sources, tuple) or any(
            not isinstance(source, ProductionSourceExtractionV1) for source in self.sources
        ):
            raise ValueError("Production extraction sources must be a tuple of V1 sources")
        if not self.sources:
            raise ValueError("Production extraction requires at least one extracted source")
        if not isinstance(self.omitted_sources, tuple) or any(
            not isinstance(omission, ProductionExtractionOmissionV1)
            for omission in self.omitted_sources
        ):
            raise ValueError("Omitted sources must be a tuple of V1 omissions")
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) for warning in self.warnings
        ):
            raise ValueError("Production extraction warnings must be a tuple of strings")

        document_ids = [source.source_document_id for source in self.sources]
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("Production extraction must not repeat a source document")
        urls = [source.canonical_url for source in self.sources]
        if len(set(urls)) != len(urls):
            raise ValueError("Production extraction must not repeat a source URL")
        omitted_urls = [omission.canonical_url for omission in self.omitted_sources]
        if len(set(omitted_urls)) != len(omitted_urls):
            raise ValueError("Production extraction must not repeat an omitted source")
        if set(urls) & set(omitted_urls):
            raise ValueError("A source cannot be both extracted and omitted")
        known_documents = set(document_ids)
        for source in self.sources:
            provenances = (
                *(fact.source_document_ids for fact in source.facts),
                *(event.source_document_ids for event in source.events),
                *(indicator.source_document_ids for indicator in source.indicators),
                *(rule.source_document_ids for rule in source.rules),
            )
            for provenance in provenances:
                if not set(provenance) <= known_documents:
                    raise ValueError("Extraction provenance must name extracted source documents")

        object.__setattr__(
            self,
            "sources",
            tuple(
                sorted(
                    self.sources,
                    key=lambda source: (
                        EXTRACTION_TIER_ORDER[source.tier],
                        source.canonical_url,
                        str(source.source_document_id),
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "omitted_sources",
            tuple(
                sorted(
                    self.omitted_sources,
                    key=lambda omission: (
                        EXTRACTION_TIER_ORDER[omission.tier],
                        omission.canonical_url,
                    ),
                )
            ),
        )


# --- Strict serialization --------------------------------------------------


_EXTRACTION_KEYS = frozenset(
    {
        "schema_version",
        "subject_id",
        "production_input_hash",
        "references_corpus_hash",
        "profile_policy_version",
        "sources",
        "omitted_sources",
        "warnings",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "source_document_id",
        "canonical_url",
        "content_sha256",
        "tier",
        "kind",
        "role",
        "profile",
        "checkpoint_id",
        "reuse_state",
        "facts",
        "events",
        "indicators",
        "rules",
        "uncertainties",
    }
)
_FACT_KEYS = frozenset(
    {
        "category",
        "value",
        "attack_id",
        "context",
        "evidence_quote",
        "evidence_basis",
        "source_document_ids",
    }
)
_EVENT_KEYS = frozenset(
    {
        "event_date",
        "date_text",
        "text",
        "context",
        "evidence_quote",
        "evidence_basis",
        "source_document_ids",
    }
)
_INDICATOR_KEYS = frozenset(
    {
        "value",
        "artifact_type",
        "indicator_status",
        "context",
        "evidence_quote",
        "evidence_basis",
        "source_document_ids",
    }
)
_RULE_KEYS = frozenset(
    {
        "rule_type",
        "name",
        "body",
        "sha256",
        "context",
        "evidence_quote",
        "evidence_basis",
        "source_document_ids",
    }
)
_OMISSION_KEYS = frozenset({"canonical_url", "tier", "collection_state", "reason", "error_code"})


def _uuid(raw: Any, field: str) -> UUID:
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a canonical UUID string")
    try:
        value = UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical UUID string") from exc
    if str(value) != raw:
        raise ValueError(f"{field} must be a canonical UUID string")
    return value


def _optional_uuid(raw: Any, field: str) -> UUID | None:
    if raw is None:
        return None
    return _uuid(raw, field)


def _sha256(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or _SHA256.fullmatch(raw) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return raw


def _enum[E: StrEnum](enum_type: type[E], raw: Any, field: str) -> E:
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be text")
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc


def _optional_date(raw: Any, field: str) -> date | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be an ISO date or None")
    try:
        value = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date or None") from exc
    if value.isoformat() != raw:
        raise ValueError(f"{field} must be an ISO date or None")
    return value


def _text_list(raw: Any, field: str) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be an array of strings")
    return raw


def _uuid_array(raw: Any, field: str) -> tuple[UUID, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be an array of UUID strings")
    return tuple(_uuid(item, field) for item in raw)


def _fact_to_json(fact: ExtractionFactV1) -> dict[str, Any]:
    return {
        "category": fact.category,
        "value": fact.value,
        "attack_id": fact.attack_id,
        "context": fact.context,
        "evidence_quote": fact.evidence_quote,
        "evidence_basis": fact.evidence_basis.value,
        "source_document_ids": [str(document_id) for document_id in fact.source_document_ids],
    }


def _event_to_json(event: ExtractionEventV1) -> dict[str, Any]:
    return {
        "event_date": event.event_date.isoformat() if event.event_date is not None else None,
        "date_text": event.date_text,
        "text": event.text,
        "context": event.context,
        "evidence_quote": event.evidence_quote,
        "evidence_basis": event.evidence_basis.value,
        "source_document_ids": [str(document_id) for document_id in event.source_document_ids],
    }


def _indicator_to_json(indicator: ExtractionIndicatorV1) -> dict[str, Any]:
    return {
        "value": indicator.value,
        "artifact_type": indicator.artifact_type.value,
        "indicator_status": indicator.indicator_status.value,
        "context": indicator.context,
        "evidence_quote": indicator.evidence_quote,
        "evidence_basis": indicator.evidence_basis.value,
        "source_document_ids": [str(document_id) for document_id in indicator.source_document_ids],
    }


def _rule_to_json(rule: ExtractionRuleV1) -> dict[str, Any]:
    return {
        "rule_type": rule.rule_type.value,
        "name": rule.name,
        "body": rule.body,
        "sha256": rule.sha256,
        "context": rule.context,
        "evidence_quote": rule.evidence_quote,
        "evidence_basis": rule.evidence_basis.value,
        "source_document_ids": [str(document_id) for document_id in rule.source_document_ids],
    }


def _source_to_json(source: ProductionSourceExtractionV1) -> dict[str, Any]:
    return {
        "source_document_id": str(source.source_document_id),
        "canonical_url": source.canonical_url,
        "content_sha256": source.content_sha256,
        "tier": source.tier.value,
        "kind": source.kind.value,
        "role": source.role.value,
        "profile": source.profile.value,
        "checkpoint_id": str(source.checkpoint_id) if source.checkpoint_id is not None else None,
        "reuse_state": source.reuse_state.value,
        "facts": [_fact_to_json(fact) for fact in source.facts],
        "events": [_event_to_json(event) for event in source.events],
        "indicators": [_indicator_to_json(indicator) for indicator in source.indicators],
        "rules": [_rule_to_json(rule) for rule in source.rules],
        "uncertainties": list(source.uncertainties),
    }


def _omission_to_json(omission: ProductionExtractionOmissionV1) -> dict[str, Any]:
    return {
        "canonical_url": omission.canonical_url,
        "tier": omission.tier.value,
        "collection_state": omission.collection_state.value,
        "reason": omission.reason.value,
        "error_code": omission.error_code,
    }


def production_extraction_to_json(extraction: ProductionExtractionV1) -> dict[str, Any]:
    """Return the versioned, JSON-compatible canonical extraction payload."""
    if not isinstance(extraction, ProductionExtractionV1):
        raise ValueError("Expected a ProductionExtractionV1")
    return {
        "schema_version": extraction.schema_version,
        "subject_id": str(extraction.subject_id),
        "production_input_hash": extraction.production_input_hash,
        "references_corpus_hash": extraction.references_corpus_hash,
        "profile_policy_version": extraction.profile_policy_version,
        "sources": [_source_to_json(source) for source in extraction.sources],
        "omitted_sources": [_omission_to_json(omission) for omission in extraction.omitted_sources],
        "warnings": list(extraction.warnings),
    }


def _require_mapping(raw: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError(f"{label} has an invalid shape")
    return raw


def _fact_from_json(raw: Any) -> ExtractionFactV1:
    payload = _require_mapping(raw, _FACT_KEYS, "Extraction fact payload")
    return ExtractionFactV1(
        category=payload["category"],
        value=payload["value"],
        attack_id=payload["attack_id"],
        context=payload["context"],
        evidence_quote=payload["evidence_quote"],
        evidence_basis=_enum(ProductionEvidenceBasis, payload["evidence_basis"], "evidence_basis"),
        source_document_ids=_uuid_array(payload["source_document_ids"], "source_document_ids"),
    )


def _event_from_json(raw: Any) -> ExtractionEventV1:
    payload = _require_mapping(raw, _EVENT_KEYS, "Extraction event payload")
    return ExtractionEventV1(
        event_date=_optional_date(payload["event_date"], "event_date"),
        date_text=payload["date_text"],
        text=payload["text"],
        context=payload["context"],
        evidence_quote=payload["evidence_quote"],
        evidence_basis=_enum(ProductionEvidenceBasis, payload["evidence_basis"], "evidence_basis"),
        source_document_ids=_uuid_array(payload["source_document_ids"], "source_document_ids"),
    )


def _indicator_from_json(raw: Any) -> ExtractionIndicatorV1:
    payload = _require_mapping(raw, _INDICATOR_KEYS, "Extraction indicator payload")
    return ExtractionIndicatorV1(
        value=payload["value"],
        artifact_type=_enum(ArtifactType, payload["artifact_type"], "artifact_type"),
        indicator_status=_enum(
            ExtractionIndicatorStatus, payload["indicator_status"], "indicator_status"
        ),
        context=payload["context"],
        evidence_quote=payload["evidence_quote"],
        evidence_basis=_enum(ProductionEvidenceBasis, payload["evidence_basis"], "evidence_basis"),
        source_document_ids=_uuid_array(payload["source_document_ids"], "source_document_ids"),
    )


def _rule_from_json(raw: Any) -> ExtractionRuleV1:
    payload = _require_mapping(raw, _RULE_KEYS, "Extraction rule payload")
    return ExtractionRuleV1(
        rule_type=_enum(DetectionRuleType, payload["rule_type"], "rule_type"),
        name=payload["name"],
        body=payload["body"],
        sha256=payload["sha256"],
        context=payload["context"],
        evidence_quote=payload["evidence_quote"],
        evidence_basis=_enum(ProductionEvidenceBasis, payload["evidence_basis"], "evidence_basis"),
        source_document_ids=_uuid_array(payload["source_document_ids"], "source_document_ids"),
    )


def _source_from_json(raw: Any) -> ProductionSourceExtractionV1:
    payload = _require_mapping(raw, _SOURCE_KEYS, "Extraction source payload")
    return ProductionSourceExtractionV1(
        source_document_id=_uuid(payload["source_document_id"], "source_document_id"),
        canonical_url=payload["canonical_url"],
        content_sha256=_sha256(payload["content_sha256"], "content_sha256"),
        tier=_enum(ProductionReferenceTier, payload["tier"], "tier"),
        kind=_enum(ProductionReferenceKind, payload["kind"], "kind"),
        role=_enum(SourceRole, payload["role"], "role"),
        profile=_enum(ExtractionProfile, payload["profile"], "profile"),
        checkpoint_id=_optional_uuid(payload["checkpoint_id"], "checkpoint_id"),
        reuse_state=_enum(ExtractionReuseState, payload["reuse_state"], "reuse_state"),
        facts=tuple(_fact_from_json(item) for item in _require_array(payload["facts"], "facts")),
        events=tuple(
            _event_from_json(item) for item in _require_array(payload["events"], "events")
        ),
        indicators=tuple(
            _indicator_from_json(item)
            for item in _require_array(payload["indicators"], "indicators")
        ),
        rules=tuple(_rule_from_json(item) for item in _require_array(payload["rules"], "rules")),
        uncertainties=tuple(_text_list(payload["uncertainties"], "uncertainties")),
    )


def _require_array(raw: Any, field: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError(f"Extraction {field} must be an array")
    return raw


def _omission_from_json(raw: Any) -> ProductionExtractionOmissionV1:
    payload = _require_mapping(raw, _OMISSION_KEYS, "Extraction omission payload")
    return ProductionExtractionOmissionV1(
        canonical_url=payload["canonical_url"],
        tier=_enum(ProductionReferenceTier, payload["tier"], "tier"),
        collection_state=_enum(CollectionState, payload["collection_state"], "collection_state"),
        reason=_enum(ProductionExtractionOmissionReason, payload["reason"], "reason"),
        error_code=_optional_text(payload["error_code"], label="Omission error code"),
    )


def production_extraction_from_json(payload: Mapping[str, Any]) -> ProductionExtractionV1:
    """Load an extraction payload, rejecting missing, extra, and mistyped fields."""
    body = _require_mapping(payload, _EXTRACTION_KEYS, "Production extraction payload")
    if type(body["schema_version"]) is not int or (
        body["schema_version"] != PRODUCTION_EXTRACTION_SCHEMA_VERSION
    ):
        raise ValueError("Production extraction schema version must be 1")
    if not isinstance(body["profile_policy_version"], str):
        raise ValueError("profile_policy_version must be text")
    raw_warnings = body["warnings"]
    if not isinstance(raw_warnings, list) or any(
        not isinstance(warning, str) for warning in raw_warnings
    ):
        raise ValueError("Production extraction warnings must be an array of strings")
    return ProductionExtractionV1(
        schema_version=PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        subject_id=_uuid(body["subject_id"], "subject_id"),
        production_input_hash=_sha256(body["production_input_hash"], "production_input_hash"),
        references_corpus_hash=_sha256(body["references_corpus_hash"], "references_corpus_hash"),
        profile_policy_version=body["profile_policy_version"],
        sources=tuple(
            _source_from_json(item) for item in _require_array(body["sources"], "sources")
        ),
        omitted_sources=tuple(
            _omission_from_json(item)
            for item in _require_array(body["omitted_sources"], "omitted_sources")
        ),
        warnings=tuple(raw_warnings),
    )
