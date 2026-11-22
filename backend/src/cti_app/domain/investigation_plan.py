"""Strict, immutable investigation plan content and version identities.

ADR-0011 D1 makes ``investigation_plan_versions`` the sole authority for plan
content and ``investigation_plans.current_version`` the compare-and-swap head.
The content is canonical JSON, so its SHA-256 is stable across processes and
storage round-trips.  Parsing Bridge text into leads belongs to lot 04A2; this
module only accepts already-structured leads and never interprets prose.

A version is born from exactly one persisted ``plan_investigation`` consultation
whose ``advisor.consult.response`` observation belongs to the same
investigation.  The application service verifies that provenance; the identity
fields below make a mislabelled payload impossible to persist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.investigation import canonical_json_bytes

INVESTIGATION_PLAN_SCHEMA_VERSION = "investigation-plan.v1"

_LEAD_TEXT_FIELDS = (
    "title",
    "objective",
    "cti_justification",
    "interest",
    "conclusion_criteria",
    "limits",
)
_LEAD_REF_FIELDS = (
    "claim_refs",
    "start_evidence_refs",
    "sample_refs",
    "tools",
    "preconditions",
    "depends_on",
)
_LEAD_PAYLOAD_KEYS = frozenset({*_LEAD_TEXT_FIELDS, *_LEAD_REF_FIELDS, "lead_id"})
_PLAN_PAYLOAD_KEYS = frozenset({"schema_version", "investigation_id", "version", "leads"})


class PlanParseStatus(StrEnum):
    """The only two states that can produce a canonical plan version.

    ``unparsed`` is a ledger state, not a version: lot 04A2 keeps such an
    output as advice and never calls this module.
    """

    PARSED = "parsed"
    PARTIALLY_PARSED = "partially_parsed"


def _text(value: object, field_name: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _refs(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field_name} must be a sequence of references")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} entries must be strings")
        reference = item.strip()
        if not reference:
            raise ValueError(f"{field_name} entries must not be empty")
        if reference in seen:
            continue
        seen.add(reference)
        normalized.append(reference)
    return tuple(normalized)


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationLeadV1:
    """One plan lead with the twelve mandate fields plus optional identity.

    ``title`` is the only field a partially parsed plan must provide: the
    parser may leave the remaining prose and reference lists empty while still
    producing a usable lead.  ``lead_id`` is optional metadata used by lot
    04A2's deterministic ``task_key``; it is not one of the twelve fields.
    """

    title: str
    objective: str = ""
    cti_justification: str = ""
    claim_refs: tuple[str, ...] = ()
    start_evidence_refs: tuple[str, ...] = ()
    sample_refs: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    interest: str = ""
    conclusion_criteria: str = ""
    limits: str = ""
    depends_on: tuple[str, ...] = ()
    lead_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _text(self.title, "title", allow_empty=False))
        for name in _LEAD_TEXT_FIELDS[1:]:
            object.__setattr__(self, name, _text(getattr(self, name), name, allow_empty=True))
        for name in _LEAD_REF_FIELDS:
            object.__setattr__(self, name, _refs(getattr(self, name), name))
        if self.lead_id is not None:
            object.__setattr__(self, "lead_id", _text(self.lead_id, "lead_id", allow_empty=False))
            if self.lead_id in self.depends_on:
                raise ValueError("A lead cannot depend on itself")

    @property
    def identity_key(self) -> tuple[Any, ...]:
        """The E10 identity used to derive a stable ``task_key``.

        A declared ``lead_id`` wins; otherwise 04A2 hashes the canonical JSON
        of the title and the sorted start/sample refs.  Two leads of one
        version may not share this identity, because they would materialise the
        same task.
        """
        if self.lead_id is not None:
            return ("lead_id", self.lead_id)
        return (
            "content",
            self.title,
            tuple(sorted(self.start_evidence_refs)),
            tuple(sorted(self.sample_refs)),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "objective": self.objective,
            "cti_justification": self.cti_justification,
            "claim_refs": list(self.claim_refs),
            "start_evidence_refs": list(self.start_evidence_refs),
            "sample_refs": list(self.sample_refs),
            "tools": list(self.tools),
            "preconditions": list(self.preconditions),
            "interest": self.interest,
            "conclusion_criteria": self.conclusion_criteria,
            "limits": self.limits,
            "depends_on": list(self.depends_on),
        }
        if self.lead_id is not None:
            payload["lead_id"] = self.lead_id
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> InvestigationLeadV1:
        if not isinstance(payload, Mapping):
            raise TypeError("A plan lead payload must be an object")
        unknown = set(payload) - _LEAD_PAYLOAD_KEYS
        if unknown:
            raise ValueError(f"A plan lead has unknown fields: {sorted(unknown)}")
        values = dict(payload)
        if values.get("title") in (None, ""):
            raise ValueError("A plan lead requires a title")
        lead_id = values.get("lead_id")
        return cls(
            title=values["title"],
            objective=values.get("objective", ""),
            cti_justification=values.get("cti_justification", ""),
            claim_refs=tuple(values.get("claim_refs", ())),
            start_evidence_refs=tuple(values.get("start_evidence_refs", ())),
            sample_refs=tuple(values.get("sample_refs", ())),
            tools=tuple(values.get("tools", ())),
            preconditions=tuple(values.get("preconditions", ())),
            interest=values.get("interest", ""),
            conclusion_criteria=values.get("conclusion_criteria", ""),
            limits=values.get("limits", ""),
            depends_on=tuple(values.get("depends_on", ())),
            lead_id=lead_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationPlanV1:
    """Canonical content of one plan version."""

    investigation_id: UUID
    version: int
    leads: tuple[InvestigationLeadV1, ...]
    schema_version: str = INVESTIGATION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("version must be an integer")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if self.schema_version != INVESTIGATION_PLAN_SCHEMA_VERSION:
            raise ValueError("Unsupported investigation plan schema version")
        if not isinstance(self.leads, (tuple, list)):
            raise TypeError("leads must be a sequence")
        leads = tuple(self.leads)
        if not leads:
            raise ValueError("A plan version requires at least one lead")
        for lead in leads:
            if not isinstance(lead, InvestigationLeadV1):
                raise TypeError("leads must use InvestigationLeadV1")
        identities = [lead.identity_key for lead in leads]
        if len(identities) != len(set(identities)):
            raise ValueError("Two leads of one plan version share an identity")
        object.__setattr__(self, "leads", leads)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "investigation_id": str(self.investigation_id),
            "version": self.version,
            "leads": [lead.to_payload() for lead in self.leads],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> InvestigationPlanV1:
        if not isinstance(payload, Mapping):
            raise TypeError("A plan payload must be an object")
        unknown = set(payload) - _PLAN_PAYLOAD_KEYS
        if unknown:
            raise ValueError(f"A plan payload has unknown fields: {sorted(unknown)}")
        missing = _PLAN_PAYLOAD_KEYS - set(payload)
        if missing:
            raise ValueError(f"A plan payload is missing fields: {sorted(missing)}")
        raw_leads = payload["leads"]
        if not isinstance(raw_leads, (list, tuple)):
            raise TypeError("leads must be an array")
        try:
            investigation_id = UUID(str(payload["investigation_id"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("investigation_id must be a UUID") from exc
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise TypeError("version must be an integer")
        return cls(
            investigation_id=investigation_id,
            version=version,
            leads=tuple(InvestigationLeadV1.from_payload(item) for item in raw_leads),
            schema_version=payload["schema_version"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationPlanHead:
    """One row of ``investigation_plans``: the CAS head of a plan."""

    investigation_id: UUID
    current_version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if self.current_version < 1:
            raise ValueError("current_version must be >= 1")
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationPlanVersion:
    """One append-only row of ``investigation_plan_versions``."""

    investigation_id: UUID
    version: int
    content: InvestigationPlanV1
    parse_status: PlanParseStatus
    source_invocation_id: UUID
    source_observation_id: UUID
    created_at: datetime
    supersedes_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.content, InvestigationPlanV1):
            raise TypeError("content must use InvestigationPlanV1")
        if self.content.investigation_id != self.investigation_id:
            raise ValueError("Plan content belongs to another investigation")
        if self.content.version != self.version:
            raise ValueError("Plan content version does not match the row version")
        if not isinstance(self.parse_status, PlanParseStatus):
            raise TypeError("parse_status must use PlanParseStatus")
        for name in ("source_invocation_id", "source_observation_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.supersedes_id is not None:
            if not isinstance(self.supersedes_id, UUID):
                raise TypeError("supersedes_id must be a UUID")
            if self.supersedes_id == self.id:
                raise ValueError("A plan version cannot supersede itself")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

    @property
    def schema_version(self) -> str:
        return self.content.schema_version

    @property
    def content_sha256(self) -> str:
        return self.content.content_sha256
