"""Read-only typed registry of Bridge consultations (ADR-0011 D9, lot 04A2).

A consultation is not a new aggregate: its authority is the persisted
``tool_invocations`` row plus the ``advisor_call`` and
``advisor_response_received`` evidence observations the advisor tool already
writes.  This module is a *view*: it never inserts, never re-parses and never
turns advisory text into evidence.  A partially parsed but readable answer
stays usable advice; ``interpretation`` and ``critique`` have no editorial
target and can never carry publishable prose.

The typed payload (``parse_status``, ``usable``, extracted elements) lives in
the archived JSON of the ``advisor_call`` observation, so the view reads it
through the same artifact store that recorded it.  A missing or unreadable
payload is reported as ``unparsed``/``usable=False`` rather than raising: the
registry is an observability surface, not a write path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cti_app.application.blob_storage import BlobStorageUnavailableError
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    ToolInvocationRepository,
)
from cti_app.application.persistence import AnalystDecisionRepository
from cti_app.application.production_artifact_store import (
    ProductionReuseStorageUnavailableError,
)
from cti_app.domain.editorial import AnalystDecisionTargetType
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
    canonical_json_bytes,
)

ADVISOR_TOOL_NAME = "advisor.consult"
ADVISOR_CALL_SOURCE = "advisor.consult"
ADVISOR_CALL_OBJECT_TYPE = "advisor_call"
ADVISOR_RESPONSE_SOURCE = "advisor.consult.response"
ADVISOR_RESPONSE_OBJECT_TYPE = "advisor_response_received"
ADVISOR_TEXT_PREFIX = "[UNTRUSTED ADVISORY]"

_EDITORIAL_CONSULTATION_TYPES = frozenset({"draft_text", "review_text"})
_CONTRIBUTION_KEYS = frozenset(
    {
        "action_items",
        "advisory_sections",
        "alternatives",
        "critique_sections",
        "draft_text",
        "next_steps",
        "recommendations",
        "remarks",
        "rule_source",
        "rules",
        "suggested_pivots",
        "warning_sections",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConsultationParseStatus(StrEnum):
    """Persisted advisor parse status; ``unparsed`` never produces a plan."""

    PARSED = "parsed"
    PARTIALLY_PARSED = "partially_parsed"
    UNPARSED = "unparsed"


class ConsultationReconciliationState(StrEnum):
    """How the invocation's ambiguity is accounted for in ``analyst_decisions``."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    RECORDED = "recorded"
    TERMINAL = "terminal"


class AdvisorPayloadStore(Protocol):
    async def read_json(self, blob_id: UUID, *, max_bytes: int | None = None) -> dict[str, Any]: ...


class InvestigationConsultationV1(BaseModel):
    """One read-only ledger row: an advisor invocation and its observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: UUID
    operation_key: str
    consultation_type: str
    invocation_status: ToolInvocationStatus
    requester_actor_kind: ToolInvocationActorKind | None = None
    requester_actor_id: str | None = None
    task_id: UUID | None = None
    agent_session_id: UUID | None = None
    call_observation_id: UUID | None = None
    response_observation_id: UUID | None = None
    output_sha256: str | None = None
    parse_status: ConsultationParseStatus
    readable: bool
    usable: bool
    advisory_only: bool
    editorial_target: str | None = None
    is_evidence: bool = False
    extracted_elements: tuple[str, ...] = ()
    contributions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    reconciliation_state: ConsultationReconciliationState
    created_at: datetime
    updated_at: datetime
    response_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AdvisorConsultationExtractV1:
    """Internal extraction shared by the ledger and plan materialisation."""

    consultation_type: str
    call_observation_id: UUID | None
    response_observation_id: UUID | None
    response_observed_at: datetime | None
    parse_status: ConsultationParseStatus
    readable: bool
    usable: bool
    output_sha256: str | None
    advisory_text: str | None
    extracted_elements: Mapping[str, Any]
    contributions: tuple[str, ...]
    limitations: tuple[str, ...]
    output: Mapping[str, Any]
    parameters: Mapping[str, Any]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _sha256(value: object) -> str | None:
    if isinstance(value, str) and _SHA256_RE.fullmatch(value):
        return value
    return None


def _strip_advisory_prefix(text: str) -> str:
    normalized = text.lstrip()
    if normalized.startswith(ADVISOR_TEXT_PREFIX):
        normalized = normalized[len(ADVISOR_TEXT_PREFIX) :]
    return normalized.lstrip("\r\n").strip()


def _consultation_type(
    invocation: ToolInvocation, payload: Mapping[str, Any], output: Mapping[str, Any]
) -> str:
    for candidate in (
        invocation.normalized_parameters.get("consultation_type"),
        payload.get("consultation_type"),
        output.get("consultation_type"),
    ):
        value = _text(candidate)
        if value is not None:
            return value
    return "unknown"


def _parse_status(output: Mapping[str, Any]) -> ConsultationParseStatus:
    value = output.get("parse_status")
    if isinstance(value, str):
        try:
            return ConsultationParseStatus(value)
        except ValueError:
            return ConsultationParseStatus.UNPARSED
    return ConsultationParseStatus.UNPARSED


def _flatten_contributions(extracted: Mapping[str, Any]) -> tuple[str, ...]:
    collected: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text not in collected:
                collected.append(text)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        if isinstance(value, Mapping):
            for key in ("text", "statement", "summary", "value"):
                if key in value:
                    add(value[key])
                    return

    for key, value in extracted.items():
        if key in _CONTRIBUTION_KEYS:
            add(value)
    return tuple(collected)


def _limitations(observations: Sequence[EvidenceObservation]) -> tuple[str, ...]:
    collected: list[str] = []
    for observation in observations:
        for limitation in observation.limitations:
            if limitation not in collected:
                collected.append(limitation)
    return tuple(collected)


def _candidate_observations(
    invocation: ToolInvocation, observations: Sequence[EvidenceObservation]
) -> tuple[EvidenceObservation, ...]:
    response_refs = set(invocation.response_refs)
    return tuple(
        observation
        for observation in observations
        if observation.investigation_id == invocation.investigation_id
        and (observation.invocation_ref == invocation.id or observation.id in response_refs)
    )


def _latest(
    observations: Sequence[EvidenceObservation],
) -> EvidenceObservation | None:
    if not observations:
        return None
    return max(observations, key=lambda observation: (observation.observed_at, str(observation.id)))


async def _read_payload(
    observation: EvidenceObservation | None, artifact_store: AdvisorPayloadStore
) -> Mapping[str, Any]:
    if observation is None:
        return {}
    try:
        return await artifact_store.read_json(observation.blob_id, max_bytes=None)
    except (
        BlobStorageUnavailableError,
        OSError,
        ProductionReuseStorageUnavailableError,
        TypeError,
        ValueError,
    ):
        return {}


async def extract_advisor_consultation(
    invocation: ToolInvocation,
    observations: Sequence[EvidenceObservation],
    artifact_store: AdvisorPayloadStore,
) -> AdvisorConsultationExtractV1:
    """Extract one invocation's persisted advice without writing anything."""
    candidates = _candidate_observations(invocation, observations)
    call_observation = _latest(
        [
            item
            for item in candidates
            if item.source == ADVISOR_CALL_SOURCE and item.object_type == ADVISOR_CALL_OBJECT_TYPE
        ]
    )
    response_observation = _latest(
        [
            item
            for item in candidates
            if item.source == ADVISOR_RESPONSE_SOURCE
            and item.object_type == ADVISOR_RESPONSE_OBJECT_TYPE
        ]
    )
    call_payload = await _read_payload(call_observation, artifact_store)
    response_payload = await _read_payload(response_observation, artifact_store)
    output = _mapping(call_payload.get("output"))
    extracted = _mapping(output.get("extracted_elements"))
    parse_status = _parse_status(output)
    raw_text = _text(output.get("advisory_text"))
    readable_value = output.get("readable")
    readable = (
        readable_value
        if isinstance(readable_value, bool)
        else bool(raw_text) and parse_status is not ConsultationParseStatus.UNPARSED
    )
    usable = bool(output.get("usable", False))
    output_sha256 = _sha256(output.get("output_sha256")) or _sha256(
        response_payload.get("raw_response_sha256")
    )
    return AdvisorConsultationExtractV1(
        consultation_type=_consultation_type(invocation, call_payload, output),
        call_observation_id=call_observation.id if call_observation is not None else None,
        response_observation_id=(
            response_observation.id if response_observation is not None else None
        ),
        response_observed_at=(
            response_observation.observed_at if response_observation is not None else None
        ),
        parse_status=parse_status,
        readable=readable,
        usable=usable,
        output_sha256=output_sha256,
        advisory_text=_strip_advisory_prefix(raw_text) if raw_text else None,
        extracted_elements=extracted,
        contributions=_flatten_contributions(extracted),
        limitations=_limitations(candidates),
        output=output,
        parameters=invocation.normalized_parameters,
    )


def _editorial_target(consultation_type: str, extract: AdvisorConsultationExtractV1) -> str | None:
    if consultation_type not in _EDITORIAL_CONSULTATION_TYPES:
        return None
    opinion_id = _text(extract.parameters.get("opinion_id"))
    if opinion_id is not None:
        return opinion_id
    target = extract.output.get("review_target")
    if isinstance(target, Mapping):
        return canonical_json_bytes(dict(target)).decode("utf-8")
    if isinstance(target, (list, tuple)):
        return json.dumps(list(target), sort_keys=True, separators=(",", ":"))
    return None


def _reconciliation_state(
    invocation: ToolInvocation, reconciled_ids: frozenset[UUID]
) -> ConsultationReconciliationState:
    if invocation.status is ToolInvocationStatus.NEEDS_REVIEW:
        return (
            ConsultationReconciliationState.RECORDED
            if invocation.id in reconciled_ids
            else ConsultationReconciliationState.REQUIRED
        )
    if invocation.status is ToolInvocationStatus.FAILED:
        return ConsultationReconciliationState.TERMINAL
    return ConsultationReconciliationState.NOT_REQUIRED


class InvestigationConsultationLedger:
    """Read-only D9 view over invocations and their advisor observations."""

    def __init__(
        self,
        *,
        invocations: ToolInvocationRepository,
        observations: EvidenceObservationRepository,
        artifact_store: AdvisorPayloadStore,
        decisions: AnalystDecisionRepository | None = None,
    ) -> None:
        self._invocations = invocations
        self._observations = observations
        self._artifact_store = artifact_store
        self._decisions = decisions

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationConsultationV1, ...]:
        invocations = tuple(
            sorted(
                (
                    invocation
                    for invocation in await self._invocations.list_for_investigation(
                        investigation_id
                    )
                    if invocation.tool_name == ADVISOR_TOOL_NAME
                ),
                key=lambda invocation: (invocation.created_at, str(invocation.id)),
            )
        )
        if not invocations:
            return ()
        observations = await self._observations.list_for_investigation(investigation_id)
        reconciled_ids = await self._reconciled_invocation_ids(investigation_id)
        entries: list[InvestigationConsultationV1] = []
        for invocation in invocations:
            extract = await extract_advisor_consultation(
                invocation, observations, self._artifact_store
            )
            editorial_target = _editorial_target(extract.consultation_type, extract)
            entries.append(
                InvestigationConsultationV1(
                    invocation_id=invocation.id,
                    operation_key=invocation.operation_key,
                    consultation_type=extract.consultation_type,
                    invocation_status=invocation.status,
                    requester_actor_kind=invocation.actor_kind,
                    requester_actor_id=invocation.actor_id,
                    task_id=invocation.task_id,
                    agent_session_id=getattr(invocation, "agent_session_id", None),
                    call_observation_id=extract.call_observation_id,
                    response_observation_id=extract.response_observation_id,
                    output_sha256=extract.output_sha256,
                    parse_status=extract.parse_status,
                    readable=extract.readable,
                    usable=extract.usable,
                    advisory_only=editorial_target is None,
                    editorial_target=editorial_target,
                    extracted_elements=tuple(sorted(extract.extracted_elements)),
                    contributions=extract.contributions,
                    limitations=extract.limitations,
                    reconciliation_state=_reconciliation_state(invocation, reconciled_ids),
                    created_at=invocation.created_at,
                    updated_at=invocation.updated_at,
                    response_observed_at=extract.response_observed_at,
                )
            )
        return tuple(entries)

    async def _reconciled_invocation_ids(self, investigation_id: UUID) -> frozenset[UUID]:
        if self._decisions is None:
            return frozenset()
        decisions = await self._decisions.list_for_investigation(investigation_id)
        return frozenset(
            decision.target_id
            for decision in decisions
            if decision.target_type is AnalystDecisionTargetType.TOOL
            and decision.correlation_id.startswith("reconcile:")
        )
