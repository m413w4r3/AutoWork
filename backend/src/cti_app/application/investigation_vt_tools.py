"""VirusTotal investigation tools implemented only through the shared VT port.

The adapters deliberately expose bounded summaries and evidence references. Raw
provider pages stay in VirusTotal's canonical blob archive and are linked into
the investigation evidence store without copying their bytes.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
)
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalCapabilityDisabledError,
    VirusTotalConfigurationError,
    VirusTotalError,
    VirusTotalFileReport,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalNotAuthorizedError,
    VirusTotalPage,
    VirusTotalPort,
    VirusTotalQuotaExceededError,
    VirusTotalRelationNotAllowedError,
    VirusTotalRouteUnavailableError,
    VirusTotalSearchResult,
    normalize_file_hash,
    validate_search_query,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.virustotal import (
    VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE,
    VirusTotalObjectType,
    VirusTotalObservation,
)

_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HASH_PATTERN = r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
_NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"
_CREDENTIAL_PATTERN = (
    r"\bbearer\s+\S|\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"authorization|credential|password|token)\b\s*[:=]"
)
_CREDENTIAL_CONTENT = re.compile(r"(?i)(?:" + _CREDENTIAL_PATTERN + ")")
_NO_EXTERNAL_SECRET = re.compile(r"(?i)(?:https?://|" + _CREDENTIAL_PATTERN + ")")
_SIGNED_QUERY_KEYS = re.compile(
    r"(?i)(?:^|[&;])(?:x-amz-signature|x-goog-signature|signature|sig|token|"
    r"access_token|api_key|key|expires|credential)="
)
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)


def _object_schema(
    properties: Mapping[str, Mapping[str, Any]], required: tuple[str, ...], description: str
) -> dict[str, Any]:
    return {
        "$schema": _SCHEMA_URI,
        "type": "object",
        "title": description,
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


FILE_REPORT_SCHEMA = _object_schema(
    {
        "file_hash": {
            "type": "string",
            "minLength": 32,
            "maxLength": 64,
            "pattern": _HASH_PATTERN,
        }
    },
    ("file_hash",),
    "VirusTotal file report",
)

RELATIONSHIPS_SCHEMA = _object_schema(
    {
        "object_type": {
            "type": "string",
            "enum": [item.value for item in VirusTotalObjectType],
        },
        "object_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_048,
            "pattern": _NO_CONTROL,
        },
        "relation": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": _NO_CONTROL},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2_048, "pattern": _NO_CONTROL},
    },
    ("object_type", "object_id", "relation"),
    "VirusTotal object relationship",
)

INTELLIGENCE_SEARCH_SCHEMA = _object_schema(
    {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
            "pattern": r"^(?!.*https?://)(?!.*\bbearer\s+\S)(?!.*\b(?:api[_-]?key|"
            r"access[_-]?token|client[_-]?secret|authorization|credential|password|token)"
            r"\b\s*[:=])(?!.*[\x00-\x1f\x7f])[\s\S]*$",
        },
        "justification": {
            "type": "string",
            "minLength": 8,
            "maxLength": 1_000,
            "pattern": r"^(?!.*https?://)(?!.*\bbearer\s+\S)(?!.*\b(?:api[_-]?key|"
            r"access[_-]?token|client[_-]?secret|authorization|credential|password|token)"
            r"\b\s*[:=])(?!.*[\x00-\x1f\x7f])[\s\S]*$",
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 36, "maxLength": 36, "pattern": _UUID_PATTERN},
            "minItems": 1,
            "maxItems": 16,
            "uniqueItems": True,
        },
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2_048, "pattern": _NO_CONTROL},
        "order": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _NO_CONTROL},
    },
    ("query", "justification", "evidence_refs"),
    "VirusTotal Intelligence Search",
)

BEHAVIOUR_SCHEMA = _object_schema(
    {
        "file_hash": {
            "type": "string",
            "minLength": 32,
            "maxLength": 64,
            "pattern": _HASH_PATTERN,
        },
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2_048, "pattern": _NO_CONTROL},
    },
    ("file_hash",),
    "VirusTotal file behaviour reports",
)

_CAPABILITY_NAMES = {
    "vt.file_report": "vt.file_report",
    "vt.relationships": "vt.relationships",
    "vt.intelligence_search": "vt.intelligence_search",
    "vt.behaviour": "vt.behaviour",
}
_ALTERNATIVE_CAPABILITIES = {
    "vt.file_report": ("vt.relationships", "vt.intelligence_search", "vt.behaviour"),
    "vt.relationships": ("vt.file_report", "vt.intelligence_search", "vt.behaviour"),
    "vt.intelligence_search": ("vt.file_report", "vt.relationships", "vt.behaviour"),
    "vt.behaviour": ("vt.file_report", "vt.relationships", "vt.intelligence_search"),
}
_VT_ALTERNATIVES = tuple(_ALTERNATIVE_CAPABILITIES)


def virustotal_tool_capabilities(capabilities: VirusTotalCapabilities) -> frozenset[str]:
    """Project D1 settings capabilities into the four investigation registry keys.

    `file_relationships` enables `vt.relationships`; `behaviour_reports`
    enables `vt.behaviour`. A capability does not imply that its configured
    route exists. The port reports a missing route locally before HTTP.
    """
    enabled = {
        "vt.file_report": capabilities.file_report,
        "vt.relationships": capabilities.file_relationships,
        "vt.intelligence_search": capabilities.intelligence_search,
        "vt.behaviour": capabilities.behaviour_reports,
    }
    return frozenset(name for name, is_enabled in enabled.items() if is_enabled)


@dataclass(slots=True)
class _ExecutionState:
    started_at: float = field(default_factory=time.monotonic)
    api_requests: int = 0
    pages: int = 0
    routes: set[str] = field(default_factory=set)
    observation_refs: list[UUID] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    available_capabilities: frozenset[str] = frozenset()
    object_type: str = "virustotal"
    object_id: str = "virustotal"


_ToolOperation = Callable[[_ExecutionState], Awaitable[ToolResult]]


class _VirusTotalAdapter:
    def __init__(
        self,
        *,
        name: str,
        virustotal: VirusTotalPort,
        observations: VirusTotalObservationService,
        recorder: EvidenceRecorder,
        max_pages: int,
        page_size: int,
        max_results: int,
        max_tool_seconds: int,
        reserved_vt_units: int,
    ) -> None:
        self.name = name
        self.virustotal = virustotal
        self.observations = observations
        self.recorder = recorder
        self.max_pages = max_pages
        self.page_size = page_size
        self.max_results = max_results
        self.max_tool_seconds = max_tool_seconds
        self.reserved_vt_units = reserved_vt_units

    async def _run(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
        operation: _ToolOperation,
    ) -> ToolResult:
        state = _ExecutionState()
        state.available_capabilities = context.available_capabilities
        if context.envelope is None or not _external_disclosure_allowed(context.envelope):
            return self._result(
                state,
                ToolInvocationStatus.POLICY_DENIED,
                reason="disclosure_bounds",
                message="The investigation disclosure bounds do not allow a VirusTotal request.",
                zero_cost=True,
            )
        try:
            return await operation(state)
        except VirusTotalInvalidInputError:
            return self._result(
                state,
                ToolInvocationStatus.FAILED,
                reason="invalid_input",
                message="VirusTotal tool arguments failed operation-specific validation.",
            )
        except VirusTotalRelationNotAllowedError:
            return self._result(
                state,
                ToolInvocationStatus.FAILED,
                reason="relation_not_allowed",
                message="The requested relation is outside the VirusTotal allowlist.",
            )
        except VirusTotalQuotaExceededError as exc:
            extra: dict[str, Any] = {"reason": "quota"}
            if exc.retry_after is not None:
                extra["retry_after"] = max(0.0, float(exc.retry_after))
            return self._result(
                state,
                ToolInvocationStatus.UNAVAILABLE,
                message="VirusTotal quota is unavailable for this request.",
                extra=extra,
            )
        except VirusTotalNotAuthorizedError:
            return self._result(
                state,
                ToolInvocationStatus.UNAVAILABLE,
                reason="provider_authorization_refused",
                message="VirusTotal does not authorize this operation for the configured account.",
            )
        except (VirusTotalCapabilityDisabledError, VirusTotalRouteUnavailableError):
            return self._result(
                state,
                ToolInvocationStatus.UNAVAILABLE,
                reason="capability_unavailable",
                message="The VirusTotal capability or its configured route is unavailable.",
            )
        except VirusTotalConfigurationError:
            return self._result(
                state,
                ToolInvocationStatus.UNAVAILABLE,
                reason="capability_unavailable",
                message="The VirusTotal route is not configured.",
            )
        except VirusTotalHttpError as exc:
            if exc.status_code in {401, 403}:
                return self._result(
                    state,
                    ToolInvocationStatus.UNAVAILABLE,
                    reason="provider_authorization_refused",
                    message="VirusTotal refused authorization for this provider capability.",
                )
            if _explicit_not_found(exc):
                observation = await self._record_negative(
                    context,
                    state,
                    reason="The VirusTotal API explicitly reported that the object was not found.",
                    details={"http_status": 404, "error_code": _safe_error_code(exc)},
                )
                return self._result(
                    state,
                    ToolInvocationStatus.EMPTY,
                    reason="not_found",
                    message="VirusTotal explicitly reported that this object was not found.",
                    extra={"negative_observation_ref": str(observation.id)},
                )
            return self._result(
                state,
                ToolInvocationStatus.FAILED,
                reason="provider_error",
                message="VirusTotal returned an unsuccessful response.",
                extra={"http_status": exc.status_code} if exc.status_code is not None else {},
            )
        except VirusTotalError:
            return self._result(
                state,
                ToolInvocationStatus.FAILED,
                reason="provider_error",
                message="The VirusTotal operation failed.",
            )
        except (TypeError, ValueError):
            return self._result(
                state,
                ToolInvocationStatus.FAILED,
                reason="invalid_input",
                message="VirusTotal tool arguments failed operation-specific validation.",
            )

    async def _record_negative(
        self,
        context: ToolExecutionContext,
        state: _ExecutionState,
        *,
        reason: str,
        details: Mapping[str, Any],
    ) -> EvidenceObservation:
        observation = await self.recorder.record_negative(
            investigation_id=context.investigation_id,
            reason=reason,
            source="VirusTotal",
            source_version="investigation-vt-tools.v1",
            object_type=state.object_type,
            object_id=state.object_id,
            limitations=("Ce résultat décrit uniquement la réponse de cette opération VT.",),
            details=details,
            idempotency_key=self._evidence_key(context, "negative", state.object_id),
        )
        state.observation_refs.append(observation.id)
        return observation

    async def _record_raw_and_summary(
        self,
        context: ToolExecutionContext,
        *,
        kind: EvidenceKind,
        raw_observation: VirusTotalObservation,
        object_type: str,
        object_id: str,
        summary: Mapping[str, Any],
        limitations: tuple[str, ...],
        page_index: int,
        suffix: str = "page",
    ) -> tuple[UUID, UUID]:
        source_version = _route_label_from_observation(raw_observation)
        raw_evidence = await self.recorder.record_existing_blob(
            investigation_id=context.investigation_id,
            kind=kind,
            blob_id=raw_observation.blob_id,
            sha256=raw_observation.raw_sha256,
            source="VirusTotal",
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            idempotency_key=self._evidence_key(context, f"raw:{suffix}:{page_index}", object_id),
        )
        summary_evidence = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=kind,
            payload={"schema": "virustotal-investigation-summary.v1", **dict(summary)},
            source="VirusTotal",
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            idempotency_key=self._evidence_key(
                context, f"summary:{suffix}:{page_index}", object_id
            ),
        )
        return raw_evidence.id, summary_evidence.id

    @staticmethod
    def _evidence_key(context: ToolExecutionContext, purpose: str, object_id: str) -> str:
        digest = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
        return f"{context.idempotency_key}:vt:{purpose}:{digest}"

    def _result(
        self,
        state: _ExecutionState,
        status: ToolInvocationStatus,
        *,
        reason: str | None = None,
        message: str = "",
        extra: Mapping[str, Any] | None = None,
        zero_cost: bool = False,
    ) -> ToolResult:
        output = dict(state.output)
        output["status"] = status.value
        if reason is not None:
            output["reason"] = reason
        if extra:
            output.update(extra)
        consumed = {
            BudgetDimension.VT_UNITS: 0
            if zero_cost
            else min(state.pages or state.api_requests, self.reserved_vt_units),
            BudgetDimension.TOOL_SECONDS: 0
            if zero_cost or state.api_requests == 0
            else min(
                math.ceil(max(0.0, time.monotonic() - state.started_at)), self.max_tool_seconds
            ),
        }
        alternatives: tuple[str, ...] = ()
        if status in {
            ToolInvocationStatus.EMPTY,
            ToolInvocationStatus.UNAVAILABLE,
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.PARTIAL,
        }:
            alternatives = self._alternatives(state)
        return ToolResult(
            status=status,
            observation_refs=tuple(dict.fromkeys(state.observation_refs)),
            budget_consumed=consumed,
            suggested_alternatives=alternatives,
            output=output,
            message=message,
        )

    def _alternatives(self, state: _ExecutionState) -> tuple[str, ...]:
        return tuple(
            name
            for name in _ALTERNATIVE_CAPABILITIES[self.name]
            if name in state.available_capabilities
        )


class _FileReportAdapter(_VirusTotalAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        async def operation(state: _ExecutionState) -> ToolResult:
            file_hash = normalize_file_hash(str(arguments["file_hash"]))
            state.object_type = "file"
            state.object_id = file_hash
            state.output.update({"file_hash": file_hash, "pages": 0, "provider_routes": []})
            state.api_requests += 1
            report = await self.virustotal.file_report(file_hash)
            state.pages = 1
            route = _route_label(report.transport.value, report.api_generation.value)
            state.routes.add(route)
            summary = _file_report_summary(report)
            limitations = (
                "Le rapport VirusTotal peut être ancien ; vérifier les dates de dernière analyse "
                "et de modification.",
                "Les verdicts de moteurs sont des observations datées, pas une preuve isolée "
                "de famille.",
            )
            raw_observation = await self.observations.store_file_report(
                report,
                observed_at=datetime.now(UTC),
                checkpoint_id=context.idempotency_key,
            )
            refs = await self._record_raw_and_summary(
                context,
                kind=EvidenceKind.VT_FILE_REPORT,
                raw_observation=raw_observation,
                object_type="file",
                object_id=file_hash,
                summary=summary,
                limitations=limitations,
                page_index=0,
            )
            state.observation_refs.extend(refs)
            state.output.update(summary)
            state.output["pages"] = 1
            state.output["provider_routes"] = sorted(state.routes)
            state.output["limitations"] = list(limitations)
            return self._result(state, ToolInvocationStatus.SUCCEEDED)

        return await self._run(arguments, context, operation)


class _RelationshipsAdapter(_VirusTotalAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        async def operation(state: _ExecutionState) -> ToolResult:
            object_type = VirusTotalObjectType(str(arguments["object_type"]))
            object_id = _normalize_object_id(object_type, str(arguments["object_id"]))
            relation = str(arguments["relation"]).strip().lower()
            if relation not in VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE[object_type]:
                raise VirusTotalRelationNotAllowedError(
                    "The relation is outside the object type's VirusTotal allowlist."
                )
            cursor = _optional_cursor(arguments.get("cursor"))
            object_anchor_type, object_anchor_id = _evidence_object_anchor(object_type, object_id)
            state.object_type = object_anchor_type
            state.object_id = object_anchor_id
            state.output.update(
                {
                    "object_type": object_type.value,
                    "object_id": object_anchor_id,
                    "relation": relation,
                    "pages": 0,
                    "provider_routes": [],
                }
            )
            items: list[dict[str, Any]] = []
            limitations = (
                "Les relations reflètent l'état connu par VirusTotal à la date de collecte.",
                "Une pagination plafonnée ne représente pas l'ensemble des relations disponibles.",
            )
            next_cursor: str | None = None
            exhaustive = False
            seen_cursors: set[str] = set()
            for page_index in range(self.max_pages):
                state.api_requests += 1
                page = await self.virustotal.object_relationship(
                    object_type,
                    object_id,
                    relation,
                    limit=self.page_size,
                    cursor=cursor,
                    paginate=False,
                )
                _record_page_route(state, page)
                state.pages += max(1, len(page.raw_json_pages))
                raw_observations = await self.observations.store_object_relationship(
                    page,
                    object_type=object_type,
                    object_id=object_id,
                    relation=relation,
                    input_cursor=cursor,
                    observed_at=datetime.now(UTC),
                )
                descriptors = _descriptors(page.items, self.max_results)
                page_summary = {
                    "relation": relation,
                    "observed_count": len(page.items),
                    "descriptors": descriptors,
                    "page": page_index + 1,
                }
                for raw_index, raw_observation in enumerate(raw_observations):
                    state.observation_refs.extend(
                        await self._record_raw_and_summary(
                            context,
                            kind=EvidenceKind.VT_RELATIONSHIP,
                            raw_observation=raw_observation,
                            object_type=object_anchor_type,
                            object_id=object_anchor_id,
                            summary=page_summary,
                            limitations=limitations,
                            page_index=page_index + raw_index,
                        )
                    )
                items.extend(page.items)
                next_cursor = page.next_cursor
                if page.exhaustive or next_cursor is None:
                    exhaustive = True
                    break
                if next_cursor in seen_cursors or next_cursor == cursor:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            partial = not exhaustive
            terminal_empty = not items and exhaustive
            if terminal_empty:
                negative = await self._record_negative(
                    context,
                    state,
                    reason="VirusTotal returned an exhaustive empty relationship result.",
                    details={"relation": relation, "status": "empty"},
                )
                state.output["negative_observation_ref"] = str(negative.id)
            output_descriptors = _descriptors(items, self.max_results)
            state.output.update(
                {
                    "pages": state.pages,
                    "observed_count": len(items),
                    "descriptors": output_descriptors,
                    "provider_routes": sorted(state.routes),
                    "limitations": list(limitations),
                }
            )
            if next_cursor is not None and not exhaustive:
                state.output["next_cursor"] = _safe_cursor(next_cursor)
            status = (
                ToolInvocationStatus.PARTIAL
                if partial
                else ToolInvocationStatus.EMPTY
                if terminal_empty
                else ToolInvocationStatus.SUCCEEDED
            )
            return self._result(state, status, reason="page_limit" if partial else None)

        return await self._run(arguments, context, operation)


class _IntelligenceSearchAdapter(_VirusTotalAdapter):
    def __init__(
        self,
        *,
        evidence_observations: EvidenceObservationRepository,
        findings: FindingRepository,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.evidence_observations = evidence_observations
        self.findings = findings

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        async def operation(state: _ExecutionState) -> ToolResult:
            query = _normalize_dork(str(arguments["query"]))
            justification = _validate_justification(str(arguments["justification"]))
            evidence_refs = await self._validate_evidence_refs(
                context.investigation_id, arguments.get("evidence_refs", ())
            )
            state.object_type = "virustotal_search"
            state.object_id = _query_anchor(query)
            state.output.update(
                {
                    "effective_query": query,
                    "pages": 0,
                    "provider_routes": [],
                    "descriptors": [],
                }
            )
            if not evidence_refs:
                return self._result(
                    state,
                    ToolInvocationStatus.POLICY_DENIED,
                    reason="evidence_refs_unverified",
                    message="Intelligence Search requires evidence from this investigation.",
                    zero_cost=True,
                )
            cursor = _optional_cursor(arguments.get("cursor"))
            order = str(arguments["order"]).strip() if arguments.get("order") is not None else None
            items: list[dict[str, Any]] = []
            output_descriptors: list[dict[str, Any]] = []
            limitations = (
                "content: recherche une signature VT bornée ; ce n'est pas un test YARA complet.",
                "Les résultats dépendent de l'index et des droits VirusTotal à la date de "
                "collecte.",
                "Un résultat présent ou absent ne démontre pas à lui seul l'appartenance à "
                "une famille.",
            )
            next_cursor: str | None = None
            exhaustive = False
            seen_cursors: set[str] = set()
            for page_index in range(self.max_pages):
                state.api_requests += 1
                result = await self.virustotal.intelligence_search(
                    query,
                    limit=self.page_size,
                    cursor=cursor,
                    paginate=False,
                    descriptors_only=True,
                    order=order,
                )
                _record_search_route(state, result)
                state.pages += max(1, len(result.raw_json_pages))
                raw_observations = await self.observations.store_intelligence_search(
                    result,
                    query=query,
                    input_cursor=cursor,
                    observed_at=datetime.now(UTC),
                    descriptors_only=True,
                    order=order,
                )
                page_descriptors = _descriptors(result.items, self.max_results)
                page_candidates = _search_candidates(result.items, self.max_results)
                query_summary = {
                    "effective_query": query,
                    "justification": justification,
                    "evidence_refs": [str(item) for item in evidence_refs],
                    "observed_count": len(result.items),
                    "descriptors": page_descriptors,
                    "page": page_index + 1,
                }
                for raw_index, raw_observation in enumerate(raw_observations):
                    state.observation_refs.extend(
                        await self._record_raw_and_summary(
                            context,
                            kind=EvidenceKind.VT_RELATIONSHIP,
                            raw_observation=raw_observation,
                            object_type="virustotal_search",
                            object_id=state.object_id,
                            summary=query_summary,
                            limitations=limitations,
                            page_index=page_index + raw_index,
                            suffix="search",
                        )
                    )
                    for candidate_index, (candidate_type, candidate_id, descriptor) in enumerate(
                        page_candidates
                    ):
                        if len(output_descriptors) >= self.max_results:
                            break
                        candidate_summary = {
                            "effective_query": query,
                            "candidate": descriptor,
                            "page": page_index + 1,
                        }
                        state.observation_refs.extend(
                            await self._record_raw_and_summary(
                                context,
                                kind=EvidenceKind.VT_RELATIONSHIP,
                                raw_observation=raw_observation,
                                object_type=candidate_type,
                                object_id=candidate_id,
                                summary=candidate_summary,
                                limitations=limitations,
                                page_index=page_index + raw_index,
                                suffix=f"candidate:{candidate_index}",
                            )
                        )
                        if descriptor not in output_descriptors:
                            output_descriptors.append(descriptor)
                items.extend(result.items)
                next_cursor = result.next_cursor
                if result.exhaustive or next_cursor is None:
                    exhaustive = True
                    break
                if next_cursor in seen_cursors or next_cursor == cursor:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            partial = not exhaustive
            terminal_empty = not items and exhaustive
            if terminal_empty:
                negative = await self._record_negative(
                    context,
                    state,
                    reason="VirusTotal Intelligence Search returned an exhaustive empty result.",
                    details={"query_sha256": state.object_id.removeprefix("query-sha256:")},
                )
                state.output["negative_observation_ref"] = str(negative.id)
            state.output.update(
                {
                    "pages": state.pages,
                    "observed_count": len(items),
                    "descriptors": output_descriptors,
                    "provider_routes": sorted(state.routes),
                    "limitations": list(limitations),
                }
            )
            if next_cursor is not None and not exhaustive:
                state.output["next_cursor"] = _safe_cursor(next_cursor)
            status = (
                ToolInvocationStatus.PARTIAL
                if partial
                else ToolInvocationStatus.EMPTY
                if terminal_empty
                else ToolInvocationStatus.SUCCEEDED
            )
            return self._result(state, status, reason="page_limit" if partial else None)

        return await self._run(arguments, context, operation)

    async def _validate_evidence_refs(
        self, investigation_id: UUID, raw_refs: Any
    ) -> tuple[UUID, ...]:
        if not isinstance(raw_refs, list | tuple):
            return ()
        valid: list[UUID] = []
        for raw_ref in raw_refs:
            try:
                ref = UUID(str(raw_ref))
            except (ValueError, TypeError, AttributeError):
                continue
            observation = await self.evidence_observations.get(ref)
            if observation is not None and observation.investigation_id == investigation_id:
                valid.append(ref)
                continue
            finding = await self.findings.get(ref)
            if finding is not None and finding.investigation_id == investigation_id:
                valid.append(ref)
        if len(valid) != len(raw_refs):
            return ()
        return tuple(dict.fromkeys(valid))


class _BehaviourAdapter(_VirusTotalAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        async def operation(state: _ExecutionState) -> ToolResult:
            file_hash = normalize_file_hash(str(arguments["file_hash"]))
            cursor = _optional_cursor(arguments.get("cursor"))
            state.object_type = "file"
            state.object_id = file_hash
            state.output.update(
                {"file_hash": file_hash, "pages": 0, "provider_routes": [], "descriptors": []}
            )
            summaries: list[dict[str, Any]] = []
            limitations = (
                "Les rapports dépendent des sandboxes VT disponibles et ne couvrent pas toutes "
                "les exécutions.",
                "Les comportements peuvent être anciens ; vérifier les dates de sandbox avant "
                "attribution.",
            )
            next_cursor: str | None = None
            exhaustive = False
            observed_count = 0
            seen_cursors: set[str] = set()
            for page_index in range(self.max_pages):
                state.api_requests += 1
                page = await self.virustotal.behaviours(
                    file_hash,
                    limit=self.page_size,
                    cursor=cursor,
                    paginate=False,
                )
                _record_page_route(state, page)
                state.pages += max(1, len(page.raw_json_pages))
                raw_observations = await self.observations.store_behaviours(
                    page,
                    file_hash=file_hash,
                    input_cursor=cursor,
                    observed_at=datetime.now(UTC),
                )
                page_summaries = _behaviour_descriptors(page)
                observed_count += page.observed_count
                page_summary = {
                    "file_hash": file_hash,
                    "observed_count": page.observed_count,
                    "descriptors": page_summaries[: self.max_results],
                    "page": page_index + 1,
                }
                for raw_index, raw_observation in enumerate(raw_observations):
                    state.observation_refs.extend(
                        await self._record_raw_and_summary(
                            context,
                            kind=EvidenceKind.VT_BEHAVIOUR,
                            raw_observation=raw_observation,
                            object_type="file",
                            object_id=file_hash,
                            summary=page_summary,
                            limitations=limitations,
                            page_index=page_index + raw_index,
                        )
                    )
                summaries.extend(page_summaries)
                next_cursor = page.next_cursor
                if page.exhaustive or next_cursor is None:
                    exhaustive = True
                    break
                if next_cursor in seen_cursors or next_cursor == cursor:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            partial = not exhaustive
            terminal_empty = observed_count == 0 and exhaustive
            if terminal_empty:
                negative = await self._record_negative(
                    context,
                    state,
                    reason="VirusTotal returned no behaviour reports for this file.",
                    details={"status": "empty"},
                )
                state.output["negative_observation_ref"] = str(negative.id)
            state.output.update(
                {
                    "pages": state.pages,
                    "observed_count": observed_count,
                    "descriptors": summaries[: self.max_results],
                    "provider_routes": sorted(state.routes),
                    "limitations": list(limitations),
                }
            )
            if next_cursor is not None and not exhaustive:
                state.output["next_cursor"] = _safe_cursor(next_cursor)
            status = (
                ToolInvocationStatus.PARTIAL
                if partial
                else ToolInvocationStatus.EMPTY
                if terminal_empty
                else ToolInvocationStatus.SUCCEEDED
            )
            return self._result(state, status, reason="page_limit" if partial else None)

        return await self._run(arguments, context, operation)


def register_virustotal_tools(
    registry: ToolRegistry,
    *,
    virustotal: VirusTotalPort,
    observations: VirusTotalObservationService,
    recorder: EvidenceRecorder,
    evidence_observations: EvidenceObservationRepository,
    findings: FindingRepository,
    max_pages: int = 3,
    page_size: int = 40,
    max_results: int = 20,
    max_tool_seconds: int = 120,
) -> None:
    """Register the four passive VT tools against shared ports and evidence stores.

    Page count, page size, output descriptors, and worst-case budget reservations
    are controlled by the backend. `evidence_observations` and `findings` are
    required so Intelligence Search can verify every cited reference belongs to
    the current investigation before sending its dork to VT.
    """
    if not 1 <= max_pages <= 10:
        raise ValueError("max_pages must be between 1 and 10")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if not 1 <= max_results <= 50:
        raise ValueError("max_results must be between 1 and 50")
    if not 1 <= max_tool_seconds <= 600:
        raise ValueError("max_tool_seconds must be between 1 and 600")

    adapters: tuple[tuple[str, Mapping[str, Any], ToolAdapter, int], ...] = (
        (
            "vt.file_report",
            FILE_REPORT_SCHEMA,
            _FileReportAdapter(
                name="vt.file_report",
                virustotal=virustotal,
                observations=observations,
                recorder=recorder,
                max_pages=max_pages,
                page_size=page_size,
                max_results=max_results,
                max_tool_seconds=max_tool_seconds,
                reserved_vt_units=1,
            ),
            1,
        ),
        (
            "vt.relationships",
            RELATIONSHIPS_SCHEMA,
            _RelationshipsAdapter(
                name="vt.relationships",
                virustotal=virustotal,
                observations=observations,
                recorder=recorder,
                max_pages=max_pages,
                page_size=page_size,
                max_results=max_results,
                max_tool_seconds=max_tool_seconds,
                reserved_vt_units=max_pages,
            ),
            max_pages,
        ),
        (
            "vt.intelligence_search",
            INTELLIGENCE_SEARCH_SCHEMA,
            _IntelligenceSearchAdapter(
                name="vt.intelligence_search",
                virustotal=virustotal,
                observations=observations,
                recorder=recorder,
                max_pages=max_pages,
                page_size=page_size,
                max_results=max_results,
                max_tool_seconds=max_tool_seconds,
                evidence_observations=evidence_observations,
                findings=findings,
                reserved_vt_units=max_pages,
            ),
            max_pages,
        ),
        (
            "vt.behaviour",
            BEHAVIOUR_SCHEMA,
            _BehaviourAdapter(
                name="vt.behaviour",
                virustotal=virustotal,
                observations=observations,
                recorder=recorder,
                max_pages=max_pages,
                page_size=page_size,
                max_results=max_results,
                max_tool_seconds=max_tool_seconds,
                reserved_vt_units=max_pages,
            ),
            max_pages,
        ),
    )
    for name, schema, adapter, reserved_units in adapters:
        capability_key = _CAPABILITY_NAMES[name]
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=ToolEffect.EXTERNAL_READ,
                cost_model={
                    BudgetDimension.VT_UNITS: reserved_units,
                    BudgetDimension.TOOL_SECONDS: max_tool_seconds,
                },
                confidentiality=ToolConfidentiality.INTERNAL,
                limitations=(
                    "Les sorties sont des résumés bornés de données VT non fiables.",
                    "Les réponses brutes restent dans l'archive VT et sont référencées comme "
                    "preuves.",
                ),
                capability_key=capability_key,
            ),
            adapter,
        )


def _external_disclosure_allowed(envelope: InvestigationEnvelope) -> bool:
    return not envelope.disclosure.do_not_submit and envelope.disclosure.tlp not in {
        TLPLevel.AMBER,
        TLPLevel.RED,
    }


def _explicit_not_found(error: VirusTotalHttpError) -> bool:
    if error.status_code != 404:
        return False
    code = (error.error_code or error.code or "").lower().replace("-", "_")
    return code in {
        "notfound",
        "not_found",
        "object_not_found",
        "file_not_found",
        "virustotal_not_found",
    }


def _safe_error_code(error: VirusTotalHttpError) -> str:
    return sanitize_evidence_text(error.error_code or error.code, max_chars=128)


def _normalize_dork(value: str) -> str:
    candidate = value.strip()
    if len(candidate) > 2_000:
        raise VirusTotalInvalidInputError("Intelligence Search query exceeds 2,000 characters.")
    if any(unicodedata.category(character) == "Cc" for character in candidate):
        raise VirusTotalInvalidInputError("Intelligence Search query contains a control character.")
    try:
        candidate.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise VirusTotalInvalidInputError(
            "Intelligence Search query has invalid UTF-8 text."
        ) from exc
    if _NO_EXTERNAL_SECRET.search(candidate):
        raise VirusTotalInvalidInputError(
            "Intelligence Search query contains a URL or credential-like parameter."
        )
    # Normalize a known spacing variant while leaving VT's open-ended operators native.
    candidate = re.sub(r"(?i)(?<![\w])content\s*:\s*", "content:", candidate)
    return validate_search_query(candidate)


def _validate_justification(value: str) -> str:
    candidate = value.strip()
    if not 8 <= len(candidate) <= 1_000:
        raise VirusTotalInvalidInputError("Intelligence Search requires a bounded justification.")
    if any(unicodedata.category(character) == "Cc" for character in candidate):
        raise VirusTotalInvalidInputError("Justification contains a control character.")
    if _NO_EXTERNAL_SECRET.search(candidate):
        raise VirusTotalInvalidInputError(
            "Justification contains a URL or credential-like parameter."
        )
    return sanitize_evidence_text(candidate, max_chars=1_000)


def _optional_cursor(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2_048:
        raise VirusTotalInvalidInputError("VirusTotal page cursor is invalid.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise VirusTotalInvalidInputError("VirusTotal page cursor contains a control character.")
    if _NO_EXTERNAL_SECRET.search(value):
        raise VirusTotalInvalidInputError("VirusTotal page cursor contains prohibited content.")
    return value


def _safe_cursor(value: str) -> str | None:
    if len(value) > 2_048 or any(unicodedata.category(char) == "Cc" for char in value):
        return None
    if (
        _NO_EXTERNAL_SECRET.search(value)
        or "http://" in value.lower()
        or "https://" in value.lower()
    ):
        return None
    return value


def _normalize_object_id(object_type: VirusTotalObjectType, value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 2_048:
        raise VirusTotalInvalidInputError("VirusTotal object identifier is invalid.")
    if any(unicodedata.category(character) == "Cc" for character in candidate):
        raise VirusTotalInvalidInputError(
            "VirusTotal object identifier contains a control character."
        )
    if object_type is VirusTotalObjectType.FILE:
        return normalize_file_hash(candidate)
    if object_type is VirusTotalObjectType.IP_ADDRESS:
        try:
            return ipaddress.ip_address(candidate).compressed
        except ValueError as exc:
            raise VirusTotalInvalidInputError("VirusTotal IP address is invalid.") from exc
    if object_type is VirusTotalObjectType.DOMAIN:
        return _normalize_domain(candidate)
    if object_type is VirusTotalObjectType.URL:
        return _normalize_url(candidate)
    raise VirusTotalInvalidInputError("VirusTotal object type is unsupported.")


def _normalize_domain(value: str) -> str:
    candidate = value.rstrip(".").lower()
    if not candidate or len(candidate) > 253 or "://" in candidate or "/" in candidate:
        raise VirusTotalInvalidInputError("VirusTotal domain identifier is invalid.")
    try:
        ascii_domain = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise VirusTotalInvalidInputError("VirusTotal domain identifier is invalid.") from exc
    labels = ascii_domain.split(".")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise VirusTotalInvalidInputError("VirusTotal domain identifier is invalid.")
    return ascii_domain


def _normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise VirusTotalInvalidInputError("VirusTotal URL identifier is invalid.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _SIGNED_QUERY_KEYS.search(parsed.query)
    ):
        raise VirusTotalInvalidInputError("Signed or malformed URLs cannot be sent to VirusTotal.")
    if _CREDENTIAL_CONTENT.search(value):
        raise VirusTotalInvalidInputError("URL contains credential-like content.")
    return parsed.geturl()


def _evidence_object_anchor(object_type: VirusTotalObjectType, object_id: str) -> tuple[str, str]:
    if object_type is VirusTotalObjectType.URL:
        digest = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
        return "url", f"url-sha256:{digest}"
    return object_type.value, object_id


def _query_anchor(query: str) -> str:
    return "query-sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()


def _route_label(transport: str, api_generation: str) -> str:
    return f"{transport}:{api_generation}"


def _route_label_from_observation(observation: VirusTotalObservation) -> str:
    transport = observation.safe_parameters.get("transport", "unknown")
    generation = observation.safe_parameters.get("api_generation", "unknown")
    return _route_label(str(transport), str(generation))


def _record_page_route(state: _ExecutionState, page: VirusTotalPage) -> None:
    state.routes.add(_route_label(page.transport.value, page.api_generation.value))


def _record_search_route(state: _ExecutionState, result: VirusTotalSearchResult) -> None:
    state.routes.add(_route_label(result.transport.value, result.api_generation.value))


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _file_report_summary(report: VirusTotalFileReport) -> dict[str, Any]:
    last_analysis_date: int | None = None
    try:
        payload = json.loads(report.raw_json)
        attributes = payload.get("data", {}).get("attributes", {})
        last_analysis_date = _integer(attributes.get("last_analysis_date"))
    except (TypeError, ValueError, AttributeError):
        pass
    file = report.file
    stats = {
        str(key)[:64]: value
        for key, value in (file.last_analysis_stats or {}).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return {
        "file_hash": file.lookup_value,
        "meaningful_name": sanitize_evidence_text(file.meaningful_name or "", max_chars=256),
        "type_description": sanitize_evidence_text(file.type_description or "", max_chars=128),
        "size": _integer(file.size),
        "last_analysis_stats": stats,
        "last_analysis_date": last_analysis_date,
        "last_submission_date": _integer(file.last_submission_date),
        "last_modification_date": _integer(file.last_modification_date),
        "tags": [sanitize_evidence_text(tag, max_chars=80) for tag in file.tags[:12]],
    }


def _descriptors(
    items: tuple[dict[str, Any], ...] | list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for item in items[:limit]:
        item_type = sanitize_evidence_text(item.get("type", ""), max_chars=64)
        identifier = item.get("id")
        if not isinstance(identifier, str):
            continue
        safe_identifier = _safe_identifier(item_type, identifier)
        if safe_identifier is None:
            continue
        attributes = item.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        descriptor: dict[str, Any] = {"type": item_type, "identifier": safe_identifier}
        for name in ("meaningful_name", "name", "type_description"):
            value = attributes.get(name)
            if isinstance(value, str) and value.strip():
                descriptor[name] = sanitize_evidence_text(value, max_chars=256)
                break
        stats = attributes.get("last_analysis_stats")
        if isinstance(stats, Mapping):
            descriptor["last_analysis_stats"] = {
                str(key)[:64]: value
                for key, value in list(stats.items())[:16]
                if isinstance(value, int) and not isinstance(value, bool)
            }
        for date_name in ("last_analysis_date", "last_submission_date", "first_submission_date"):
            date_value = _integer(attributes.get(date_name))
            if date_value is not None:
                descriptor[date_name] = date_value
        descriptors.append(descriptor)
    return descriptors


def _safe_identifier(item_type: str, value: str) -> str | None:
    normalized_type = item_type.lower()
    try:
        if normalized_type in {"file", "files"}:
            return normalize_file_hash(value)
        if normalized_type in {"ip_address", "ip"}:
            return ipaddress.ip_address(value).compressed
        if normalized_type in {"domain", "domains"}:
            return _normalize_domain(value)
        if normalized_type in {"url", "urls"}:
            url = _decode_url_id(value)
            if url is None:
                return None
            return "url-sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
    except (ValueError, VirusTotalInvalidInputError):
        return None
    if len(value) > 256 or any(unicodedata.category(char) == "Cc" for char in value):
        return None
    if (
        _NO_EXTERNAL_SECRET.search(value)
        or "http://" in value.lower()
        or "https://" in value.lower()
    ):
        return None
    return sanitize_evidence_text(value, max_chars=256)


def _decode_url_id(value: str) -> str | None:
    if value.startswith(("http://", "https://")):
        try:
            return _normalize_url(value)
        except VirusTotalInvalidInputError:
            return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded.startswith(("http://", "https://")):
        return None
    try:
        return _normalize_url(decoded)
    except VirusTotalInvalidInputError:
        return None


def _search_candidates(
    items: tuple[dict[str, Any], ...] | list[dict[str, Any]], limit: int
) -> list[tuple[str, str, dict[str, Any]]]:
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for item in items:
        if len(candidates) >= limit:
            break
        item_type = item.get("type")
        identifier = item.get("id")
        if not isinstance(item_type, str) or not isinstance(identifier, str):
            continue
        normalized_type = item_type.lower()
        try:
            if normalized_type in {"file", "files"}:
                candidate_type = VirusTotalObjectType.FILE.value
                candidate_id = normalize_file_hash(identifier)
            elif normalized_type in {"ip_address", "ip"}:
                candidate_type = VirusTotalObjectType.IP_ADDRESS.value
                candidate_id = ipaddress.ip_address(identifier).compressed
            elif normalized_type in {"domain", "domains"}:
                candidate_type = VirusTotalObjectType.DOMAIN.value
                candidate_id = _normalize_domain(identifier)
            elif normalized_type in {"url", "urls"}:
                decoded = _decode_url_id(identifier)
                if decoded is None:
                    continue
                candidate_type = VirusTotalObjectType.URL.value
                candidate_id = "url-sha256:" + hashlib.sha256(decoded.encode("utf-8")).hexdigest()
            else:
                continue
        except (ValueError, VirusTotalInvalidInputError):
            continue
        descriptor = _descriptors([item], 1)
        if descriptor:
            candidates.append((candidate_type, candidate_id, descriptor[0]))
    return candidates


def _behaviour_descriptors(page: VirusTotalPage) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in page.behaviour_summaries:
        descriptor: dict[str, Any] = {
            "sandbox": sanitize_evidence_text(item.sandbox or "", max_chars=128),
            "date": _integer(item.date)
            if _integer(item.date) is not None
            else sanitize_evidence_text(item.date, max_chars=64)
            if isinstance(item.date, str)
            else None,
            "tags": [sanitize_evidence_text(tag, max_chars=80) for tag in item.tags[:12]],
            "has_pcap": item.has_pcap,
            "has_html_report": item.has_html_report,
            "available_sections": [
                sanitize_evidence_text(section, max_chars=80)
                for section in item.available_sections[:16]
            ],
        }
        results.append(descriptor)
    if results:
        return results
    return _descriptors(page.items, 20)


__all__ = [
    "BEHAVIOUR_SCHEMA",
    "FILE_REPORT_SCHEMA",
    "INTELLIGENCE_SEARCH_SCHEMA",
    "RELATIONSHIPS_SCHEMA",
    "register_virustotal_tools",
    "virustotal_tool_capabilities",
]
