"""Durable operator-led tool calls and their bounded research context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from cti_app.application.advisor_checkpoint import (
    format_advisor_trace_purpose,
    split_advisor_trace_purpose,
)
from cti_app.application.context_pagination import (
    ContextOmissionManifestEntryV1,
    ContextOmissionReason,
    canonical_page_sha256,
    decode_keyset_cursor,
    decode_offset_cursor,
    encode_offset_cursor,
)
from cti_app.application.evidence_recorder import _sanitize_json, sanitize_evidence_text
from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_BYTES,
    InvestigationContextV1,
    InvestigationOperatorContextV1,
    OperatorContextSectionPageV1,
)
from cti_app.application.investigation_experiments import HYPOTHESIS_DECLARE_TOOL
from cti_app.application.investigation_persistence import (
    InvestigationEnvelopeRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_state import (
    InvestigationStateRepository,
    InvestigationStateVersionConflict,
)
from cti_app.application.investigation_tools import (
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.operator_context_summary import (
    InvestigationOperatorContextSummaryV1,
    OperatorContextItemPageV1,
    build_operator_context_summary,
)
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.application.research_context_builder import (
    CanonicalEvidenceSummaryPort,
    InvestigationContextTooLarge,
    ResearchContextBuilder,
)
from cti_app.domain.analyst_opinion import investigation_opinion_id
from cti_app.domain.investigation import (
    BudgetReservationStatus,
    FrameworkPreSubmissionProof,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_experiment import (
    HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_ID_PATTERN,
)
from cti_app.domain.investigation_state import Hypothesis

_LEGACY_SCHEMA_V1_TOOLS = frozenset(
    {
        "finding.propose",
        "finding.revise",
        "opinion.compose",
        "opinion.note",
        "opinion.revise",
        "advisor.consult",
        HYPOTHESIS_DECLARE_TOOL,
    }
)

_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|bearer|"
    r"token|secret|password|authorization|credential|cookie)",
    re.I,
)
_CREDENTIAL_TEXT = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~-]{20,}|(?:api[_-]?key|access[_-]?token|secret|"
    r"password|authorization)\s*[:=]\s*\S+)",
    re.I,
)
_SIGNED_QUERY = frozenset(
    {
        "signature",
        "sig",
        "token",
        "access_token",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "x-goog-security-token",
    }
)
_ACTIVE_OPERATOR_INVOCATIONS: dict[str, asyncio.Lock] = {}
_ADDITIONAL_CONTEXT_ITEM_SECTIONS = frozenset(
    {
        "finding_versions",
        "available_tools",
        "available_pivots",
        "latest_tool_results",
        "previous_actions",
        "operator_requests",
        "agenda",
        "blocked_capabilities",
        "budget_remaining",
        "advisor_guidance",
        "unavailable_tools",
        "indicator_inventory",
        "current_opinion",
        "truncation",
        "source",
    }
)
_SOURCE_PAGED_CONTEXT_SECTIONS = frozenset(
    {"new_evidence", "evidence_index", "findings", "finding_versions", "tool_coverage"}
)


def _materialized_page_proof(
    *,
    section: str,
    investigation_id: UUID,
    context_sha256: str,
    offset: int,
    limit: int,
    total: int,
    items: tuple[Any, ...],
    cursor: str | None,
) -> tuple[str, str, str | None, ContextOmissionManifestEntryV1, dict[str, str], bool]:
    previous_page_sha256 = (
        decode_offset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            context_sha256=context_sha256,
        )["previous_page_sha256"]
        if cursor
        else "0" * 64
    )
    next_offset = offset + len(items)
    has_more = next_offset < total
    omitted = total - len(items)
    omission_reasons = {ContextOmissionReason.SOURCE_NOT_PAGINATED: omitted} if omitted else {}
    pivot = {
        "kind": "offset",
        "section": section,
        "investigation_id": str(investigation_id),
        "context_sha256": context_sha256,
        "offset": str(next_offset),
    }
    omission_summary = {
        "section": section,
        "total": total,
        "selected": len(items),
        "omitted": omitted,
        "source_bounded": False,
        "omission_reasons": omission_reasons,
        "unbounded_reason": "The context section was materialized before page selection",
    }
    page_sha256 = canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=False,
        limit=limit,
        total=total,
        has_more=has_more,
        items=items,
        previous_page_sha256=previous_page_sha256,
        omission_manifest=omission_summary,
    )
    source_ref = f"operator-context:{context_sha256}:{section}:offset={offset}:sha256={page_sha256}"
    next_cursor = (
        encode_offset_cursor(
            section=section,
            investigation_id=investigation_id,
            context_sha256=context_sha256,
            offset=next_offset,
            previous_page_sha256=page_sha256,
        )
        if next_offset < total
        else None
    )
    return (
        page_sha256,
        source_ref,
        next_cursor,
        ContextOmissionManifestEntryV1(
            section=section,
            total=total,
            selected=len(items),
            omitted=omitted,
            source_bounded=False,
            source_ref=source_ref,
            page_sha256=page_sha256,
            omission_reasons=(omission_reasons),
            unbounded_reason=("The context section was materialized before page selection"),
            context_sha256=context_sha256,
            cursor=cursor,
            pivot=pivot,
            descending=False,
            limit=limit,
            has_more=has_more,
        ),
        pivot,
        has_more,
    )


class OperatorToolInvocationError(RuntimeError):
    def __init__(
        self,
        code: str,
        status_code: int,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class OperatorToolInvocationResult:
    investigation_id: UUID
    working_state_version: int
    invocation: ToolInvocation
    error_code: str | None
    job_id: UUID | None
    idempotent_replay: bool
    output: Mapping[str, Any]


class OperatorToolInvocationService:
    def __init__(
        self,
        *,
        investigation_id: UUID,
        investigations: AnalystInvestigationRepository,
        states: InvestigationStateRepository,
        envelopes: InvestigationEnvelopeRepository,
        invocations: ToolInvocationRepository,
        context_builder: ResearchContextBuilder,
        execution: InvestigationService,
        registry: ToolRegistry,
        available_capabilities: frozenset[str],
        unavailable_tool_reasons: Mapping[str, str],
        observations: Any | None = None,
        evidence_summaries: CanonicalEvidenceSummaryPort | None = None,
    ) -> None:
        self._investigation_id = investigation_id
        self._investigations = investigations
        self._states = states
        self._envelopes = envelopes
        self._invocations = invocations
        self._context_builder = context_builder
        self._execution = execution
        self._registry = registry
        self._available_capabilities = available_capabilities
        self._unavailable_tool_reasons = unavailable_tool_reasons
        self._observations = observations
        self._evidence_summaries = evidence_summaries

    async def get_context(
        self,
        *,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
    ) -> InvestigationOperatorContextV1:
        state = await self._states.get(self._investigation_id)
        if state is None:
            raise OperatorToolInvocationError("investigation_not_started", 409)
        self._require_operator_mode(state.execution_mode)
        if expected_version is not None and state.version != expected_version:
            raise OperatorToolInvocationError("stale_working_state", 409)
        if expected_sha256 is not None and state.sha256 != expected_sha256:
            raise OperatorToolInvocationError("stale_working_state", 409)
        if state.pending_context_sha256 is not None:
            raise OperatorToolInvocationError("bridge_checkpoint_pending", 409)
        investigation = await self._investigations.get(self._investigation_id)
        if investigation is None:
            raise OperatorToolInvocationError("investigation_not_found", 404)
        envelope = await self._envelopes.get(self._investigation_id)
        snapshot = await self._context_builder.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        budget = (
            envelope.budget
            if envelope is not None
            else self._execution.unapproved_budget_for(investigation)
        )
        tool_context = ToolExecutionContext(
            investigation_id=self._investigation_id,
            envelope=envelope,
            budget=budget,
            idempotency_key="operator-context-read",
            available_capabilities=self._available_capabilities,
            unavailable_tool_reasons=self._unavailable_tool_reasons,
            blocked_capabilities=(
                ("bridge.advisor:external_llm_disclosure_denied",)
                if not InvestigationService.external_llm_allowed(snapshot, envelope)
                else ()
            ),
        )
        try:
            context = await self._context_builder.build(
                investigation_id=self._investigation_id,
                synthesis_artifact_id=investigation.synthesis_artifact_id,
                state=state,
                tool_context=tool_context,
                budget_remaining={
                    dimension.value: amount for dimension, amount in budget.remaining.items()
                },
                registry=self._registry,
            )
        except InvestigationContextTooLarge as exc:
            raise OperatorToolInvocationError(
                "operator_context_too_large",
                413,
                details={"dominant_sections": list(exc.dominant_sections)},
            ) from None
        context_bytes = context.canonical_bytes()
        if len(context_bytes) > MAX_CONTEXT_BYTES:
            raise OperatorToolInvocationError(
                "operator_context_too_large",
                413,
                details={
                    "dominant_sections": list(
                        self._context_builder._dominant_context_sections(context)
                    )
                },
            )
        return InvestigationOperatorContextV1(
            investigation_id=self._investigation_id,
            working_state_version=state.version,
            working_state_sha256=state.sha256,
            context_sha256=hashlib.sha256(context_bytes).hexdigest(),
            context_bytes=len(context_bytes),
            max_context_bytes=MAX_CONTEXT_BYTES,
            truncation=_truncation_flags(context),
            truncation_counts=dict(context.investigation.truncation_counts),
            truncation_pointers=_truncation_pointers(
                self._investigation_id,
                _truncation_flags(context),
            ),
            current_opinion=context.current_opinion,
            context=context,
        )

    async def get_context_summary(
        self,
        *,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
        since_version: int | None = None,
    ) -> InvestigationOperatorContextSummaryV1:
        state = await self._states.get(self._investigation_id)
        if state is None:
            raise OperatorToolInvocationError("investigation_not_started", 409)
        self._require_operator_mode(state.execution_mode)
        if expected_version is not None and state.version != expected_version:
            raise OperatorToolInvocationError("stale_working_state", 409)
        if expected_sha256 is not None and state.sha256 != expected_sha256:
            raise OperatorToolInvocationError("stale_working_state", 409)
        if since_version is not None and since_version > state.version:
            raise OperatorToolInvocationError("operator_context_delta_base_invalid", 422)
        operator_context = await self.get_context(
            expected_version=state.version,
            expected_sha256=state.sha256,
        )
        try:
            return build_operator_context_summary(
                operator_context.context,
                investigation_id=self._investigation_id,
                working_state_version=state.version,
                working_state_sha256=state.sha256,
                state=state,
                since_version=since_version,
            )
        except ValueError as exc:
            raise OperatorToolInvocationError("operator_context_summary_invalid", 503) from exc

    async def get_context_section(
        self,
        section: str,
        *,
        offset: int = 0,
        limit: int = 20,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
        cursor: str | int | None = None,
    ) -> OperatorContextSectionPageV1 | OperatorContextItemPageV1:
        state = await self._states.get(self._investigation_id)
        if state is None:
            raise OperatorToolInvocationError("investigation_not_started", 409)
        self._require_operator_mode(state.execution_mode)
        if expected_version is not None and state.version != expected_version:
            raise OperatorToolInvocationError("stale_working_state", 409)
        if expected_sha256 is not None and state.sha256 != expected_sha256:
            raise OperatorToolInvocationError("stale_working_state", 409)
        legacy_offset_cursor = False
        if cursor is not None:
            if expected_version is None or expected_sha256 is None:
                raise OperatorToolInvocationError("operator_context_cursor_requires_cas", 422)
            if isinstance(cursor, bool):
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
            if isinstance(cursor, int) or (isinstance(cursor, str) and cursor.isdecimal()):
                legacy_offset_cursor = True
                try:
                    cursor_offset = int(cursor)
                except ValueError:
                    raise OperatorToolInvocationError(
                        "operator_context_cursor_invalid", 422
                    ) from None
                if cursor_offset < 0 or offset not in {0, cursor_offset}:
                    raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
                offset = cursor_offset
            elif cursor == "":
                if offset != 0:
                    raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
                cursor = None
            elif offset != 0:
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
        if state.pending_context_sha256 is not None:
            raise OperatorToolInvocationError("bridge_checkpoint_pending", 409)
        investigation = await self._investigations.get(self._investigation_id)
        if investigation is None:
            raise OperatorToolInvocationError("investigation_not_found", 404)
        if offset < 0 or not 1 <= limit <= 20:
            raise OperatorToolInvocationError("operator_context_page_invalid", 422)

        if section in _SOURCE_PAGED_CONTEXT_SECTIONS:
            keyset_cursor = cursor if isinstance(cursor, str) else None
            if keyset_cursor is not None:
                try:
                    decode_keyset_cursor(
                        keyset_cursor,
                        section=section,
                        investigation_id=self._investigation_id,
                        descending=section != "finding_versions",
                        context_sha256=state.sha256,
                    )
                except ValueError:
                    raise OperatorToolInvocationError(
                        "operator_context_cursor_invalid", 422
                    ) from None
            try:
                page = await self._context_builder.read_context_section_page(
                    section=section,
                    investigation_id=self._investigation_id,
                    state=state,
                    cursor=keyset_cursor,
                    offset=offset if legacy_offset_cursor or cursor is None else 0,
                    limit=limit,
                    context_sha256=state.sha256,
                )
            except ValueError as exc:
                if str(exc) == "Section does not use source-bounded keyset pagination":
                    raise OperatorToolInvocationError(
                        "operator_context_section_unknown", 404
                    ) from None
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422) from None
            omission = page.omission_manifest[0]
            next_offset = (
                offset + len(page.items)
                if page.has_more and (legacy_offset_cursor or cursor is None)
                else None
            )
            if section == "finding_versions":
                operator_context = await self.get_context(
                    expected_version=state.version,
                    expected_sha256=state.sha256,
                )
                return OperatorContextItemPageV1(
                    investigation_id=self._investigation_id,
                    working_state_version=operator_context.working_state_version,
                    working_state_sha256=operator_context.working_state_sha256,
                    context_sha256=operator_context.context_sha256,
                    section=section,
                    offset=offset,
                    limit=limit,
                    total=page.total,
                    next_offset=next_offset,
                    next_cursor=page.next_cursor,
                    page_sha256=page.page_sha256,
                    source_ref=page.source_ref,
                    omission_manifest=(omission,),
                    items=page.items,
                    cursor=page.cursor,
                    pivot=page.pivot,
                    descending=page.descending,
                    has_more=page.has_more,
                    source_bounded=page.source_bounded,
                    unbounded_reason=page.unbounded_reason,
                    cursor_authentication=page.cursor_authentication,
                    section_truncated=page.total > len(page.items),
                )
            return OperatorContextSectionPageV1(
                investigation_id=self._investigation_id,
                working_state_version=state.version,
                working_state_sha256=state.sha256,
                context_sha256=page.context_sha256,
                section=cast(Any, section),
                offset=offset,
                limit=limit,
                total=page.total,
                next_offset=next_offset,
                next_cursor=page.next_cursor,
                page_sha256=page.page_sha256,
                source_ref=page.source_ref,
                omission_manifest=(omission,),
                items=page.items,
                cursor=page.cursor,
                pivot=page.pivot,
                descending=page.descending,
                has_more=page.has_more,
                source_bounded=page.source_bounded,
                unbounded_reason=page.unbounded_reason,
                cursor_authentication=page.cursor_authentication,
            )

        if section in _ADDITIONAL_CONTEXT_ITEM_SECTIONS:
            operator_context = await self.get_context(
                expected_version=state.version,
                expected_sha256=state.sha256,
            )
            if isinstance(cursor, str) and cursor:
                try:
                    offset = decode_offset_cursor(
                        cursor,
                        section=section,
                        investigation_id=self._investigation_id,
                        context_sha256=operator_context.context_sha256,
                    )["offset"]
                except ValueError:
                    raise OperatorToolInvocationError(
                        "operator_context_cursor_invalid", 422
                    ) from None
            all_items = _additional_context_items(operator_context, section)
            total = len(all_items)
            page_items = all_items[offset : offset + limit]
            next_offset = offset + len(page_items) if offset + len(page_items) < total else None
            (
                page_sha256,
                source_ref,
                next_cursor,
                omission,
                pivot,
                has_more,
            ) = _materialized_page_proof(
                section=section,
                investigation_id=self._investigation_id,
                context_sha256=operator_context.context_sha256,
                offset=offset,
                limit=limit,
                total=total,
                items=tuple(_context_item_json(item) for item in page_items),
                cursor=cursor if isinstance(cursor, str) and cursor else None,
            )
            return OperatorContextItemPageV1(
                investigation_id=self._investigation_id,
                working_state_version=operator_context.working_state_version,
                working_state_sha256=operator_context.working_state_sha256,
                context_sha256=operator_context.context_sha256,
                section=section,
                offset=offset,
                limit=limit,
                total=total,
                next_offset=next_offset,
                next_cursor=next_cursor,
                page_sha256=page_sha256,
                source_ref=source_ref,
                omission_manifest=(omission,),
                items=tuple(_context_item_json(item) for item in page_items),
                cursor=cursor if isinstance(cursor, str) and cursor else None,
                pivot=pivot,
                descending=False,
                has_more=has_more,
                source_bounded=False,
                unbounded_reason=omission.unbounded_reason,
                cursor_authentication=None,
                section_truncated=_additional_context_section_truncated(operator_context, section),
            )
        if isinstance(cursor, str) and cursor:
            try:
                offset = decode_offset_cursor(
                    cursor,
                    section=section,
                    investigation_id=self._investigation_id,
                    context_sha256=state.sha256,
                )["offset"]
            except ValueError:
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422) from None
        try:
            total, items = await self._context_builder.read_context_section(
                section=section,
                investigation_id=self._investigation_id,
                state=state,
                offset=offset,
                limit=limit,
                synthesis_artifact_id=investigation.synthesis_artifact_id,
            )
        except ValueError as exc:
            if str(exc) == "Unsupported operator context section":
                raise OperatorToolInvocationError("operator_context_section_unknown", 404) from None
            raise
        next_offset = offset + len(items) if offset + len(items) < total else None
        page_sha256, source_ref, next_cursor, omission, pivot, has_more = _materialized_page_proof(
            section=section,
            investigation_id=self._investigation_id,
            context_sha256=state.sha256,
            offset=offset,
            limit=limit,
            total=total,
            items=items,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
        )
        return OperatorContextSectionPageV1(
            investigation_id=self._investigation_id,
            working_state_version=state.version,
            working_state_sha256=state.sha256,
            section=cast(Any, section),
            offset=offset,
            limit=limit,
            total=total,
            next_offset=next_offset,
            next_cursor=next_cursor,
            page_sha256=page_sha256,
            source_ref=source_ref,
            omission_manifest=(omission,),
            items=items,
            context_sha256=state.sha256,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            pivot=pivot,
            descending=False,
            has_more=has_more,
            source_bounded=False,
            unbounded_reason=omission.unbounded_reason,
            cursor_authentication=None,
        )

    async def invoke(
        self,
        *,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OperatorToolInvocationResult:
        if not 1 <= len(idempotency_key) <= 255:
            raise OperatorToolInvocationError("idempotency_key_invalid", 422)
        schema_version = self._canonical_schema_version(tool_name, schema_version)
        try:
            trace, _human_purpose = split_advisor_trace_purpose(
                purpose, tool_name=tool_name, allow_server_denied=False
            )
            _require_safe_operator_payload({"arguments": arguments, "purpose": purpose})
            payload = canonical_json_bytes(
                {
                    "tool_name": tool_name,
                    "schema_version": schema_version,
                    "arguments": arguments,
                    "purpose": purpose,
                    "expected_version": expected_version,
                }
            )
        except (TypeError, ValueError):
            raise OperatorToolInvocationError(
                "operator_payload_invalid_or_sensitive", 422
            ) from None

        if trace is not None and "recommendation" in trace:
            try:
                await self._validate_recommendation_trace(
                    trace["recommendation"], tool_name=tool_name, schema_version=schema_version
                )
            except ValueError as exc:
                raise OperatorToolInvocationError(str(exc), 422) from None
        if trace is not None and isinstance(trace.get("checkpoint"), Mapping):
            checkpoint_id = str(trace["checkpoint"].get("id", ""))
            if not checkpoint_id.endswith(f"@{expected_version}"):
                raise OperatorToolInvocationError("advisor_checkpoint_stale", 409)

        key_digest = hashlib.sha256(
            f"{self._investigation_id}\0{idempotency_key}".encode()
        ).hexdigest()
        payload_digest = hashlib.sha256(payload).hexdigest()
        operation_prefix = f"operator:{key_digest}:"
        operation_key = f"{operation_prefix}{payload_digest}"
        lock = _ACTIVE_OPERATOR_INVOCATIONS.setdefault(operation_prefix, asyncio.Lock())
        async with lock:
            return await self._invoke_locked(
                tool_name=tool_name,
                schema_version=schema_version,
                arguments=arguments,
                purpose=purpose,
                expected_version=expected_version,
                operation_prefix=operation_prefix,
                operation_key=operation_key,
                trace=trace,
            )

    def _canonical_schema_version(self, tool_name: str, schema_version: str) -> str:
        """Accept the operator API's legacy generic v1 for typed operator tools.

        These tools were introduced with descriptive schema identifiers while
        the operator client sends ``1`` for version-one schemas. Resolve that
        alias against the live registry before hashing the idempotency payload,
        so the journal and dispatcher use the same canonical schema version.
        """
        if schema_version != "1" or tool_name not in _LEGACY_SCHEMA_V1_TOOLS:
            return schema_version
        definitions = tuple(
            item for item in self._registry.list_registered() if item.name == tool_name
        )
        return definitions[0].schema_version if len(definitions) == 1 else schema_version

    async def _framework_pre_submission_proof(
        self, invocation: ToolInvocation
    ) -> FrameworkPreSubmissionProof | None:
        if self._observations is None or self._evidence_summaries is None:
            return None
        envelope = await self._envelopes.get(self._investigation_id)
        if envelope is None:
            return None
        reservation = next(
            (
                item
                for item in envelope.budget.reservations
                if item.operation_key == invocation.operation_key
            ),
            None,
        )
        if (
            reservation is None
            or reservation.status is not BudgetReservationStatus.RELEASED
            or dict(reservation.amounts) != dict(invocation.reserved_budget)
        ):
            return None
        for observation_id in invocation.response_refs:
            observation = await self._observations.get(observation_id)
            if (
                observation is None
                or observation.investigation_id != self._investigation_id
                or observation.invocation_ref != invocation.id
                or observation.source != invocation.tool_name
                or observation.source_version != invocation.schema_version
                or observation.object_id != invocation.operation_key
            ):
                continue
            try:
                proof = FrameworkPreSubmissionProof(observation.object_type)
            except ValueError:
                continue
            try:
                summary = await self._evidence_summaries.summary(observation)
            except Exception:
                continue
            if isinstance(summary, Mapping):
                return proof
        return None

    async def _invoke_locked(
        self,
        *,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        expected_version: int,
        operation_prefix: str,
        operation_key: str,
        trace: Mapping[str, Any] | None,
    ) -> OperatorToolInvocationResult:
        state = await self._states.get(self._investigation_id)
        if state is None:
            raise OperatorToolInvocationError("investigation_not_started", 409)
        self._require_operator_mode(state.execution_mode)
        investigation = await self._investigations.get(self._investigation_id)
        if investigation is None:
            raise OperatorToolInvocationError("investigation_not_found", 404)
        envelope = await self._envelopes.get(self._investigation_id)
        snapshot = await self._context_builder.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        advisor_external_allowed = InvestigationService.external_llm_allowed(snapshot, envelope)
        advisor_disclosure_denied = tool_name == "advisor.consult" and not advisor_external_allowed
        registered = self._registry.get(tool_name, schema_version)
        definition = registered[0] if registered is not None else None
        requires_external_disclosure = definition is not None and definition.effect in {
            ToolEffect.EXTERNAL_READ,
            ToolEffect.ACQUIRE,
            ToolEffect.EXTERNAL_ACTIVE,
            ToolEffect.EXTERNAL_SUBMIT,
        }
        if (
            not advisor_disclosure_denied
            and requires_external_disclosure
            and not InvestigationService.external_llm_allowed(snapshot, envelope)
        ):
            self._require_external_disclosure(snapshot, envelope)

        prior = tuple(
            item
            for item in await self._invocations.list_for_investigation(self._investigation_id)
            if item.operation_key.startswith(operation_prefix)
        )
        if prior:
            existing = prior[0]
            if existing.operation_key != operation_key:
                raise OperatorToolInvocationError("idempotency_conflict", 409)
            if existing.status in {ToolInvocationStatus.PENDING, ToolInvocationStatus.RUNNING}:
                reviewed = existing.transition(ToolInvocationStatus.NEEDS_REVIEW)
                existing = await self._invocations.update_if_version(
                    reviewed, expected_version=existing.version
                )
                await self._execution.commit_current_uow()
            return await self._result(existing, state.version, replay=True)

        semantic_parameters = _semantic_parameters_fingerprint(
            tool_name, arguments, self._investigation_id
        )
        for existing in await self._invocations.list_for_investigation(self._investigation_id):
            if (
                existing.tool_name != tool_name
                or _semantic_parameters_fingerprint(
                    existing.tool_name,
                    existing.normalized_parameters,
                    self._investigation_id,
                )
                != semantic_parameters
                or existing.status
                not in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.NEEDS_REVIEW}
            ):
                continue
            proof = await self._framework_pre_submission_proof(existing)
            if existing.status is ToolInvocationStatus.NEEDS_REVIEW and proof is not None:
                resolved = existing.resolve_pre_submission_failure()
                existing = await self._invocations.update_if_version(
                    resolved, expected_version=existing.version
                )
                failure = ToolResult(
                    ToolInvocationStatus.FAILED,
                    error_code=proof.value,
                    message=(
                        "Persisted evidence proves the prior invocation did not reach "
                        "external submission."
                    ),
                    framework_pre_submission_proof=proof,
                )
                await self._execution._complete_tool_reservation(
                    investigation_id=self._investigation_id,
                    operation_key=existing.operation_key,
                    result=failure,
                    reserved_amounts=existing.reserved_budget,
                    uncertain=False,
                )
                await self._execution.commit_current_uow()
                continue
            raise OperatorToolInvocationError(
                "ambiguous_tool_invocation_requires_reconciliation",
                409,
                details={"existing_invocation_id": str(existing.id)},
            )

        if state.version != expected_version:
            raise OperatorToolInvocationError("stale_working_state", 409)

        record_purpose = purpose
        checkpoint = trace.get("checkpoint") if isinstance(trace, Mapping) else None
        guidance = None
        try:
            operator_context = await self.get_context(expected_version=state.version)
            guidance = operator_context.context.advisor_guidance
        except OperatorToolInvocationError as exc:
            if exc.code == "stale_working_state":
                raise
        except Exception:
            # Checkpoint guidance is advisory and must not make a locally
            # authorized operator action unavailable if the recap is unreadable.
            guidance = None

        advisor_denial_reason = None
        if not advisor_external_allowed:
            advisor_denial_reason = "external_disclosure_denied"
        elif (
            guidance is not None
            and guidance.g2_bridge_turns_remaining is not None
            and guidance.g2_bridge_turns_remaining < 1
        ):
            advisor_denial_reason = "g2_bridge_budget_exhausted"
        elif "bridge.advisor" not in self._available_capabilities:
            advisor_denial_reason = "advisor_capability_unavailable"

        if guidance is not None:
            due_reason = guidance.reason if guidance.checkpoint_due else None
            expected_checkpoint_id = (
                f"{due_reason}@{state.version}" if due_reason is not None else None
            )
            if isinstance(checkpoint, Mapping) and tool_name != "advisor.consult":
                checkpoint_id = checkpoint.get("id")
                if checkpoint_id != expected_checkpoint_id and not (
                    tool_name == "advisor.consult"
                    and not guidance.checkpoint_due
                    and checkpoint_id == f"operator_requested@{state.version}"
                ):
                    raise OperatorToolInvocationError("advisor_checkpoint_stale", 409)
            elif due_reason is not None:
                if tool_name == "advisor.consult":
                    status = "denied" if advisor_denial_reason else "consulted"
                    checkpoint = {
                        "id": expected_checkpoint_id,
                        "status": status,
                        **({"reason": advisor_denial_reason} if advisor_denial_reason else {}),
                    }
                else:
                    checkpoint = {
                        "id": expected_checkpoint_id,
                        "status": "denied" if advisor_denial_reason else "skipped",
                        "reason": advisor_denial_reason or "operator_proceeded_without_advisor",
                    }
                _trace = dict(trace or {})
                _trace["checkpoint"] = checkpoint
                record_purpose = format_advisor_trace_purpose(
                    _human_purpose_from_purpose(purpose),
                    checkpoint=checkpoint,
                    recommendation=_trace.get("recommendation"),
                )
            elif tool_name == "advisor.consult" and checkpoint is None:
                checkpoint = {
                    "id": f"operator_requested@{state.version}",
                    "status": "denied" if advisor_denial_reason else "consulted",
                    **({"reason": advisor_denial_reason} if advisor_denial_reason else {}),
                }
                _trace = dict(trace or {})
                _trace["checkpoint"] = checkpoint
                record_purpose = format_advisor_trace_purpose(
                    _human_purpose_from_purpose(purpose),
                    checkpoint=checkpoint,
                    recommendation=_trace.get("recommendation"),
                )
        elif tool_name == "advisor.consult" and checkpoint is None:
            checkpoint = {
                "id": f"operator_requested@{state.version}",
                "status": "denied" if advisor_denial_reason else "consulted",
                **({"reason": advisor_denial_reason} if advisor_denial_reason else {}),
            }
            _trace = dict(trace or {})
            _trace["checkpoint"] = checkpoint
            record_purpose = format_advisor_trace_purpose(
                _human_purpose_from_purpose(purpose),
                checkpoint=checkpoint,
                recommendation=_trace.get("recommendation"),
            )

        if trace is not None and isinstance(trace.get("checkpoint"), Mapping):
            if advisor_denial_reason and (
                tool_name == "advisor.consult" or (guidance is not None and guidance.checkpoint_due)
            ):
                checkpoint = {
                    "id": trace["checkpoint"]["id"],
                    "status": "denied",
                    "reason": advisor_denial_reason,
                }
                record_purpose = format_advisor_trace_purpose(
                    _human_purpose_from_purpose(purpose),
                    checkpoint=checkpoint,
                    recommendation=trace.get("recommendation"),
                )

        updated_state = state.with_operator_tool_action(
            f"operator_tool:{tool_name}:{operation_key[-16:]}"
        )
        declaration_authorized = (
            tool_name == HYPOTHESIS_DECLARE_TOOL
            and schema_version == HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION
            and definition is not None
            and definition.capability_key == "operator.write"
            and "operator.write" in self._available_capabilities
            and envelope is not None
        )
        if declaration_authorized and envelope is not None:
            current_envelope = await self._envelopes.get(self._investigation_id)
            declaration_authorized = (
                current_envelope is not None
                and current_envelope.version == envelope.version
                and current_envelope.approval_decision_id == envelope.approval_decision_id
            )
        if declaration_authorized:
            hypothesis_id = arguments.get("hypothesis_id")
            statement = arguments.get("statement")
            if (
                isinstance(hypothesis_id, str)
                and re.fullmatch(HYPOTHESIS_ID_PATTERN, hypothesis_id) is not None
                and isinstance(statement, str)
                and 1 <= len(statement) <= 8_000
                and set(arguments) == {"hypothesis_id", "statement"}
            ):
                try:
                    hypothesis = Hypothesis(id=hypothesis_id, statement=statement)
                except (TypeError, ValueError):
                    hypothesis = None
                if hypothesis is not None and not any(
                    item.id == hypothesis.id for item in state.hypotheses
                ):
                    updated_state = replace(
                        updated_state,
                        hypotheses=(*state.hypotheses, hypothesis),
                    )
        try:
            updated_state = await self._states.save_if_version(
                updated_state, expected_version=state.version
            )
        except (VersionConflictError, InvestigationStateVersionConflict):
            raise OperatorToolInvocationError("stale_working_state", 409) from None

        budget = (
            envelope.budget
            if envelope is not None
            else self._execution.unapproved_budget_for(investigation)
        )
        try:
            original_capabilities = self._execution._available_capabilities
            if advisor_disclosure_denied:
                # The operator dispatcher is the same registry dispatcher used by
                # inventory projection. Temporarily remove this one capability so
                # dispatch records an UNAVAILABLE result without reaching Bridge.
                self._execution._available_capabilities = frozenset(
                    item for item in original_capabilities if item != "bridge.advisor"
                )
            invocation, result, _refs, _uncertain = await self._execution.execute_tool_call(
                investigation=investigation,
                envelope=envelope,
                budget=budget,
                state=updated_state,
                tool_name=tool_name,
                schema_version=schema_version,
                arguments=arguments,
                purpose=record_purpose,
                operation_key=operation_key,
                check_stop_before_dispatch=False,
            )
            if advisor_disclosure_denied:
                self._execution._available_capabilities = original_capabilities
        except (Exception, asyncio.CancelledError):
            if "original_capabilities" in locals() and advisor_disclosure_denied:
                self._execution._available_capabilities = original_capabilities
            current = await self._invocations.get_by_operation_key(operation_key)
            if current is not None and current.status in {
                ToolInvocationStatus.PENDING,
                ToolInvocationStatus.RUNNING,
            }:
                reviewed = current.transition(ToolInvocationStatus.NEEDS_REVIEW)
                await self._invocations.update_if_version(
                    reviewed, expected_version=current.version
                )
                await self._execution.commit_current_uow()
            raise
        return await self._result(
            invocation,
            updated_state.version,
            replay=False,
            tool_result=result,
            unavailable_reason=(
                "external_llm_disclosure_denied" if advisor_disclosure_denied else None
            ),
        )

    async def _validate_recommendation_trace(
        self,
        recommendation: Mapping[str, Any],
        *,
        tool_name: str,
        schema_version: str,
    ) -> None:
        try:
            advisor_invocation = await self._invocations.get(
                UUID(str(recommendation["invocation_id"]))
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("advisor_recommendation_unavailable") from None
        if (
            advisor_invocation is None
            or advisor_invocation.investigation_id != self._investigation_id
            or advisor_invocation.tool_name != "advisor.consult"
            or advisor_invocation.status
            not in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
            or self._observations is None
            or self._evidence_summaries is None
        ):
            raise ValueError("advisor_recommendation_unavailable")

        recommendation_index = recommendation.get("index")
        stored_output: Mapping[str, Any] | None = None
        for observation_id in advisor_invocation.response_refs:
            observation = await self._observations.get(observation_id)
            if (
                observation is None
                or observation.investigation_id != self._investigation_id
                or observation.source != "advisor.consult"
            ):
                continue
            try:
                payload = await self._evidence_summaries.summary(observation)
            except Exception:
                continue
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != "operator-advisor-call.v1"
            ):
                continue
            if not isinstance(payload.get("output"), Mapping):
                continue
            stored_output = payload
            break
        if stored_output is None:
            raise ValueError("advisor_recommendation_unavailable")

        output = stored_output["output"]
        if (
            output.get("untrusted") is not True
            or output.get("usable", True) is False
            or output.get("advisor_status") == "refused"
        ):
            raise ValueError("advisor_recommendation_unavailable")
        recommendations = output.get("recommended_next_tools", ())
        if (
            not isinstance(recommendations, (list, tuple))
            or not isinstance(recommendation_index, int)
            or recommendation_index >= len(recommendations)
        ):
            raise ValueError("advisor_recommendation_unavailable")
        proposed = recommendations[recommendation_index]
        if not isinstance(proposed, Mapping):
            raise ValueError("advisor_recommendation_unavailable")
        proposed_tool = proposed.get("tool")
        proposed_schema = proposed.get("schema_version")
        basis_refs = proposed.get("basis_observation_refs", ())
        input_refs = stored_output.get("input_observation_refs", ())
        if (
            not isinstance(proposed_tool, str)
            or not isinstance(proposed_schema, str)
            or not isinstance(basis_refs, (list, tuple))
            or not basis_refs
            or not isinstance(input_refs, (list, tuple))
            or not set(basis_refs).issubset({str(item) for item in input_refs})
            or output.get("untrusted") is not True
            or self._registry.get(proposed_tool, proposed_schema) is None
        ):
            raise ValueError("advisor_recommendation_not_observation_validated")
        for basis_ref in basis_refs:
            try:
                basis_observation = await self._observations.get(UUID(str(basis_ref)))
            except (TypeError, ValueError):
                basis_observation = None
            if (
                basis_observation is None
                or basis_observation.investigation_id != self._investigation_id
                or basis_observation.source == "advisor.consult"
            ):
                raise ValueError("advisor_recommendation_not_observation_validated")

        decision = recommendation.get("decision")
        if decision == "accepted":
            if proposed_tool != tool_name or proposed_schema != schema_version:
                raise ValueError("advisor_recommendation_action_mismatch")
        elif decision == "refused" and proposed_tool == tool_name:
            raise ValueError("advisor_recommendation_refusal_action_mismatch")

    @staticmethod
    def _require_operator_mode(mode: InvestigationExecutionMode) -> None:
        if mode is not InvestigationExecutionMode.OPERATOR_LED:
            raise OperatorToolInvocationError("operator_mode_required", 409)

    @staticmethod
    def _require_external_disclosure(snapshot: Any, envelope: Any) -> None:
        if not InvestigationService.external_llm_allowed(snapshot, envelope):
            raise OperatorToolInvocationError("external_disclosure_denied", 403)

    async def _result(
        self,
        invocation: ToolInvocation,
        state_version: int,
        *,
        replay: bool,
        tool_result: Any | None = None,
        unavailable_reason: str | None = None,
    ) -> OperatorToolInvocationResult:
        error_code = {
            ToolInvocationStatus.UNAVAILABLE: "tool_unavailable",
            ToolInvocationStatus.POLICY_DENIED: "tool_policy_denied",
            ToolInvocationStatus.NEEDS_REVIEW: "invocation_needs_review",
            ToolInvocationStatus.FAILED: "tool_failed",
        }.get(invocation.status)
        job_id = None
        if tool_result is not None:
            raw_job_id = tool_result.output.get("job_id")
            if isinstance(raw_job_id, str):
                try:
                    job_id = UUID(raw_job_id)
                except ValueError:
                    job_id = None
        output_data: Any = None
        if self._observations is not None and self._evidence_summaries is not None:
            persisted: list[dict[str, Any]] = []
            for observation_id in invocation.response_refs[:8]:
                observation = await self._observations.get(observation_id)
                if observation is None or observation.investigation_id != self._investigation_id:
                    continue
                summary = await self._evidence_summaries.summary(observation)
                if summary is None:
                    continue
                persisted.append(
                    {
                        "observation_id": str(observation.id),
                        "sha256": observation.sha256,
                        "url": (
                            f"/api/investigations/{self._investigation_id}/operator/observations/"
                            f"{observation.id}?offset=0&length=16384"
                        ),
                        "summary": summary,
                    }
                )
            if persisted:
                output_data = {"observations": persisted}
        if output_data is None and tool_result is not None:
            output_data = {"result": tool_result.output}
        safe_data = _sanitize_json(output_data or {})
        safe_data = _bounded_json_value(safe_data)
        output: dict[str, Any] = {
            "kind": _operator_output_kind(invocation.tool_name),
            "status": invocation.status.value,
            "untrusted_data": True,
            "data": safe_data,
        }
        if invocation.status in {
            ToolInvocationStatus.UNAVAILABLE,
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }:
            raw_reason = (
                unavailable_reason
                or getattr(tool_result, "message", None)
                or (tool_result.output.get("reason") if tool_result is not None else None)
                or invocation.status.value
            )
            output["error"] = {
                "code": error_code or invocation.status.value,
                "reason": sanitize_evidence_text(raw_reason, max_chars=512),
            }
        return OperatorToolInvocationResult(
            investigation_id=invocation.investigation_id,
            working_state_version=state_version,
            invocation=invocation,
            error_code=error_code,
            job_id=job_id,
            idempotent_replay=replay,
            output=output,
        )


def _canonical_uuid_reference(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _semantic_parameters_fingerprint(
    tool_name: str,
    parameters: Mapping[str, Any],
    investigation_id: UUID,
) -> bytes:
    """Return the conservative effect identity of a tool request.

    The fingerprint folds representations that the adapters treat as
    equivalent. Over-blocking a genuinely different request only forces an
    explicit reconciliation; under-blocking would allow a second external call.
    """
    normalized = dict(parameters)
    if tool_name == "advisor.consult":
        question = normalized.get("question", "")
        if isinstance(question, str):
            question = question.strip()
            if question:
                normalized["question"] = question
            else:
                normalized.pop("question", None)

        evidence_refs = normalized.get("evidence_refs", ())
        if isinstance(evidence_refs, list | tuple):
            canonical_refs: list[str] = []
            for item in evidence_refs:
                canonical = _canonical_uuid_reference(item)
                canonical_refs.append(canonical if canonical is not None else str(item))
            if canonical_refs:
                normalized["evidence_refs"] = sorted(canonical_refs)
            else:
                normalized.pop("evidence_refs", None)

        consultation_type = normalized.get("consultation_type")
        if consultation_type == "figure_plan":
            finding_refs = normalized.get("finding_refs", ())
            if isinstance(finding_refs, list | tuple):
                canonical_findings: list[str] = []
                for item in finding_refs:
                    canonical = _canonical_uuid_reference(item)
                    canonical_findings.append(canonical if canonical is not None else str(item))
                if canonical_findings:
                    normalized["finding_refs"] = sorted(canonical_findings)
                else:
                    normalized.pop("finding_refs", None)
        elif consultation_type == "review_text":
            opinion_id = normalized.get("opinion_id")
            default_opinion_id = str(investigation_opinion_id(investigation_id))
            if opinion_id is None:
                normalized["opinion_id"] = default_opinion_id
            else:
                canonical_opinion = _canonical_uuid_reference(opinion_id)
                normalized["opinion_id"] = (
                    canonical_opinion if canonical_opinion is not None else default_opinion_id
                )
            # Omitted means "latest"; an explicit version is resolved by the
            # adapter. Treating all versions as equivalent is conservative: it
            # blocks a fresh key until reconciliation and closes the omitted
            # versus explicit bypass.
            normalized.pop("opinion_version", None)
            form = normalized.get("form")
            if form is None:
                normalized["form"] = "body"
            elif isinstance(form, str):
                normalized["form"] = form.strip().lower() or "body"
    elif tool_name == "vt.file_report":
        file_hash = normalized.get("file_hash")
        if isinstance(file_hash, str):
            normalized["file_hash"] = file_hash.strip().lower()
    return canonical_json_bytes(normalized)


def _operator_output_kind(tool_name: str) -> str:
    if tool_name == "sample.analyze":
        return "sample_analysis"
    if tool_name == "local.run_command":
        return "local_command"
    if tool_name.startswith("ghidra."):
        return "ghidra_analysis"
    if tool_name == "vt.behaviour":
        return "sandbox_behavior"
    if tool_name == "signatures.candidates":
        return "signature_candidates"
    if tool_name == "sample.get_feature_pack":
        return "sample_feature_pack"
    return tool_name.replace(".", "_")[:96]


def _human_purpose_from_purpose(purpose: str) -> str:
    try:
        _trace, human_purpose = split_advisor_trace_purpose(purpose, allow_server_denied=True)
    except ValueError:
        return purpose
    return human_purpose


def _bounded_json_value(value: Any, *, max_bytes: int = 32 * 1024) -> Any:
    """Keep operator-facing tool data valid JSON and below a strict byte ceiling."""

    def clip(item: Any, *, string_limit: int, array_limit: int) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): clip(child, string_limit=string_limit, array_limit=array_limit)
                for key, child in list(item.items())[:128]
            }
        if isinstance(item, list | tuple):
            return [
                clip(child, string_limit=string_limit, array_limit=array_limit)
                for child in item[:array_limit]
            ]
        if isinstance(item, str) and len(item) > string_limit:
            return item[:string_limit] + " [truncated]"
        return item

    for string_limit, array_limit in (
        (8_000, 64),
        (4_000, 32),
        (2_000, 16),
        (1_000, 8),
        (512, 4),
        (256, 1),
    ):
        bounded = clip(value, string_limit=string_limit, array_limit=array_limit)
        encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= max_bytes:
            return bounded
    return {"truncated": True, "serialized_bytes": len(encoded), "limit_bytes": max_bytes}


def _require_safe_operator_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_FIELD.search(key):
                raise ValueError("Sensitive tool argument is not accepted")
            _require_safe_operator_payload(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _require_safe_operator_payload(item)
    elif isinstance(value, str):
        if _CREDENTIAL_TEXT.search(value):
            raise ValueError("Credential-like tool input is not accepted")
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            if parsed.username or parsed.password:
                raise ValueError("Credential-bearing URLs are not accepted")
            query_names = {
                name.casefold() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
            }
            if query_names.intersection(_SIGNED_QUERY):
                raise ValueError("Signed URLs are not accepted")


def _truncation_flags(context: InvestigationContextV1) -> dict[str, bool]:
    payload = context.model_dump(mode="python", by_alias=True)
    flags: dict[str, bool] = {}

    def collect(value: Any, prefix: str = "") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                if str(key).endswith("_truncated") and isinstance(item, bool):
                    flags[name] = item
                else:
                    collect(item, name)
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                collect(item, f"{prefix}[{index}]")

    collect(payload)
    return dict(sorted(flags.items()))


def _truncation_pointers(
    investigation_id: UUID,
    flags: Mapping[str, bool],
) -> dict[str, tuple[str, ...]]:
    section_by_flag = {
        "investigation.new_evidence_truncated": ("new_evidence",),
        "investigation.evidence_index_truncated": ("evidence_index",),
        "investigation.findings_truncated": ("findings",),
        "investigation.opinion_notes_truncated": ("opinion_notes",),
        "investigation.tool_coverage_truncated": ("tool_coverage",),
        "investigation.sample_deltas_truncated": ("sample_deltas",),
        "article.publication_anchors_truncated": ("publication_anchors",),
        "article.claims_truncated": ("article_claims",),
        "article.long_texts_truncated": ("article_long_texts",),
        "article.documented_samples_truncated": ("documented_samples",),
        "article.known_chain_truncated": ("known_chain",),
        "article.open_questions_truncated": ("open_questions",),
        "investigation.hypotheses_truncated": ("hypotheses",),
        "investigation.rule_deltas_truncated": ("rule_deltas",),
        "investigation.long_texts_truncated": (
            "new_evidence",
            "findings",
            "opinion_notes",
            "tool_coverage",
            "rule_deltas",
        ),
    }
    base = f"/api/investigations/{investigation_id}/operator/context/section"
    return {
        flag: tuple(f"{base}/{section}?offset=0&limit=20" for section in sections)
        for flag, sections in section_by_flag.items()
        if flags.get(flag) is True
    }


def _additional_context_items(
    operator_context: InvestigationOperatorContextV1,
    section: str,
) -> tuple[Any, ...]:
    context = operator_context.context
    section_values: dict[str, Any] = {
        "available_tools": context.available_tools,
        "available_pivots": context.available_pivots,
        "latest_tool_results": context.investigation.latest_tool_results,
        "previous_actions": context.investigation.previous_actions,
        "operator_requests": context.operator_requests,
        "agenda": context.investigation.agenda,
        "blocked_capabilities": context.investigation.blocked_capabilities,
        "budget_remaining": tuple(
            {"dimension": key, "remaining": value}
            for key, value in sorted(context.investigation.budget_remaining.items())
        ),
        "advisor_guidance": (context.advisor_guidance,),
        "unavailable_tools": context.unavailable_tools,
        "indicator_inventory": context.indicator_inventory.items,
        "current_opinion": (
            (operator_context.current_opinion,)
            if operator_context.current_opinion is not None
            else ()
        ),
        "truncation": (
            {
                "flags": operator_context.truncation,
                "counts": operator_context.truncation_counts,
                "pointers": operator_context.truncation_pointers,
            },
        ),
        "source": (context.source,),
    }
    value = section_values.get(section)
    if value is None:
        raise OperatorToolInvocationError("operator_context_section_unknown", 404)
    if isinstance(value, Mapping):
        return tuple({"key": key, "value": item} for key, item in sorted(value.items()))
    if isinstance(value, tuple | list):
        return tuple(value)
    return (value,)


def _additional_context_section_truncated(
    operator_context: InvestigationOperatorContextV1,
    section: str,
) -> bool:
    context = operator_context.context
    if section == "available_pivots":
        return context.available_pivots_truncated
    if section == "unavailable_tools":
        return context.unavailable_tools_truncated
    if section == "indicator_inventory":
        return context.indicator_inventory.truncated
    return False


def _context_item_json(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True)
    return value


__all__ = [
    "OperatorToolInvocationError",
    "OperatorToolInvocationResult",
    "OperatorToolInvocationService",
]
