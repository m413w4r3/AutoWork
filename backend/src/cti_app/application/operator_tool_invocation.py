"""Durable operator-led tool calls and their bounded research context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
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
    StaleContextCursorError,
    assert_manifest_consistency,
    canonical_page_sha256,
    context_section_descending,
    decode_keyset_cursor,
    decode_offset_cursor,
    encode_offset_cursor,
)
from cti_app.application.evidence_recorder import _sanitize_json
from cti_app.application.investigation_contracts import (
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
from cti_app.application.investigation_service import (
    _VT_PASSIVE_READ_TOOLS,
    _VT_SOURCE_REFS_PARAMETER,
    InvestigationService,
)
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
from cti_app.application.investigation_vt_tools import is_trusted_vt_passive_read_adapter
from cti_app.application.operator_context_summary import (
    OperatorContextItemPageV1,
)
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.application.research_context_builder import (
    CanonicalEvidenceSummaryPort,
    ResearchContextBuilder,
)
from cti_app.domain.analyst_opinion import investigation_opinion_id
from cti_app.domain.investigation import (
    BudgetReservationStatus,
    FindingAssessment,
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
_OPERATOR_ERROR_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_FINDING_RESULT_FIELDS = (
    "finding_id",
    "finding_version",
    "kind",
    "assessment",
    "assessment_requested",
    "assessment_recorded",
    "assessment_downgrade_code",
    "assessment_prewrite_message",
)
_FINDING_OBJECT_KINDS = frozenset(
    {"new_sample", "network_behaviour", "infection_chain", "capability", "detection"}
)
_FINDING_DOWNGRADE_MESSAGES = {
    "downgraded_no_matching_subject": (
        "The requested supported assessment was saved as inconclusive because subject_ref "
        "was missing. Provide subject_ref and cite an observation whose object_id matches it."
    ),
    "downgraded_without_observation": (
        "The requested supported assessment was saved without cited evidence. Cite an "
        "observation from this investigation before requesting supported."
    ),
    "downgraded_no_matching_observation": (
        "The requested supported assessment was constrained because no cited observation "
        "matched subject_ref. Cite an observation with the same object_id."
    ),
    "downgraded_claim_without_supporting_observation": (
        "The requested supported claim assessment needs a current claim reference and a "
        "supporting non-source observation from this investigation."
    ),
    "downgraded_invalid_sample_subject": (
        "The requested supported new-sample assessment needs a valid sample hash in "
        "subject_ref and matching evidence."
    ),
}
_OPERATOR_ERROR_MESSAGES = {
    "tool_unavailable": (
        "This tool is not registered or unavailable in the current runtime. Select a registered "
        "tool or enable its required capability."
    ),
    "tool_policy_denied": (
        "The current policy refused this action. Refresh the approved investigation scope and "
        "retry only if the action is authorized."
    ),
    "invocation_needs_review": (
        "The prior operation has an unconfirmed outcome. Complete the human reconciliation "
        "step before retrying it."
    ),
    "tool_failed": (
        "The tool could not complete the request. Review this error code and the current "
        "investigation context, correct the inputs, then retry."
    ),
    "external_disclosure_denied": (
        "No external request was authorized. Update the investigation disclosure approval "
        "before retrying this action."
    ),
    "source_references_required": (
        "Add a canonical observation reference from this investigation that is associated with "
        "the exact requested target, then retry."
    ),
    "finding_observation_not_found": (
        "Refresh the observation list and cite an observation that exists in this investigation."
    ),
    "finding_observation_investigation_mismatch": (
        "Choose an observation from this investigation; cross-investigation references cannot "
        "support a finding."
    ),
    "finding_claim_not_found": (
        "Refresh the article context and cite a claim from its current article snapshot."
    ),
    "finding_not_found": (
        "Refresh the finding list and use a finding version that belongs to this investigation."
    ),
    "stale_finding_version": (
        "Reload the current finding and submit its latest expected_finding_version."
    ),
    "finding_invalid": (
        "Check the finding fields and provenance requirements, including subject_ref and "
        "matching observations for object-specific findings."
    ),
}
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
        "truncation",
        "source",
    }
)
_SOURCE_PAGED_CONTEXT_SECTIONS = frozenset(
    {"new_evidence", "evidence_index", "findings", "finding_versions", "tool_coverage"}
)
_CONTEXT_CONTENT_SCHEMA = "operator-context-content.v1"
_CONTEXT_CONTENT_FIELDS_V1 = (
    "schema_version",
    "view",
    "investigation_id",
    "source",
    "article",
    "investigation",
    "operator_requests",
    "indicator_inventory",
    "available_pivots",
    "available_pivots_truncated",
    "delta",
    "available_tools",
    "unavailable_tools",
    "unavailable_tools_truncated",
    "advisor_guidance",
)
_CONTEXT_INVESTIGATION_CONTENT_FIELDS_V1 = (
    "agenda",
    "hypotheses",
    "new_evidence",
    "evidence_index",
    "evidence_index_truncated",
    "findings",
    "findings_truncated",
    "opinion_notes",
    "opinion_notes_truncated",
    "truncation_counts",
    "new_evidence_truncated",
    "hypotheses_truncated",
    "rule_deltas_truncated",
    "previous_actions_truncated",
    "long_texts_truncated",
    "sample_deltas",
    "rule_deltas",
    "previous_actions",
    "user_requests",
    "tool_coverage",
    "tool_coverage_truncated",
    "latest_tool_results",
    "sample_deltas_truncated",
    "blocked_capabilities",
    "budget_remaining",
)


def _canonical_context_content_bytes(context: InvestigationContextV1) -> bytes:
    """Serialize complete operator context content for pagination identity.

    V1 explicitly selects semantic ``investigation-context.v1`` fields and its
    separately exposed ``current_opinion``. The field sets below exclude the
    nested pagination manifest and its page proofs. Working-state metadata,
    page slicing, cursors and pagination proofs are excluded. Changes to this
    selection require a new content schema version.
    """
    current_opinion = context.current_opinion
    include_fields: dict[str, Any] = {
        field_name: True
        for field_name in _CONTEXT_CONTENT_FIELDS_V1
        if field_name != "investigation"
    }
    include_fields["investigation"] = set(_CONTEXT_INVESTIGATION_CONTENT_FIELDS_V1)
    semantic_context = context.model_dump(
        mode="json",
        by_alias=True,
        include=include_fields,
    )
    return canonical_json_bytes(
        {
            "schema": _CONTEXT_CONTENT_SCHEMA,
            "context": semantic_context,
            "current_opinion": (
                current_opinion.model_dump(mode="json", by_alias=True)
                if current_opinion is not None
                else None
            ),
        }
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
    decoded_cursor = (
        decode_offset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            context_sha256=context_sha256,
        )
        if cursor
        else None
    )
    previous_page_sha256 = (
        decoded_cursor["previous_page_sha256"] if decoded_cursor is not None else "0" * 64
    )
    next_offset = offset + len(items)
    has_more = next_offset < total
    omitted = total - len(items)
    omission_reasons: dict[ContextOmissionReason, int] = (
        {ContextOmissionReason.SOURCE_NOT_PAGINATED: omitted} if omitted else {}
    )
    unbounded_reason = "The context section was materialized before page selection"
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
        "unbounded_reason": unbounded_reason,
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
    entry = ContextOmissionManifestEntryV1(
        section=section,
        total=total,
        selected=len(items),
        omitted=omitted,
        source_bounded=False,
        source_ref=source_ref,
        page_sha256=page_sha256,
        omission_reasons=omission_reasons,
        unbounded_reason=unbounded_reason,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=False,
        limit=limit,
        has_more=has_more,
    )
    assert_manifest_consistency(
        section=section,
        investigation_id=investigation_id,
        items=items,
        previous_page_sha256=previous_page_sha256,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=False,
        limit=limit,
        total=total,
        has_more=has_more,
        source_bounded=False,
        omission_reasons=omission_reasons,
        unbounded_reason=unbounded_reason,
        selected=len(items),
        omitted=omitted,
        claimed_page_sha256=page_sha256,
        claimed_source_ref=source_ref,
        manifest=entry,
        source_ref_offset=offset,
        cursor_authentication=None,
    )
    return (
        page_sha256,
        source_ref,
        next_cursor,
        entry,
        pivot,
        has_more,
    )


def _assert_keyset_page_consistency(page: Any, *, section: str, investigation_id: UUID) -> None:
    entries = page.omission_manifest
    if len(entries) != 1:
        raise ValueError("Operator keyset page must have exactly one omission manifest")
    entry = entries[0]
    decoded_cursor = (
        decode_keyset_cursor(
            page.cursor,
            section=section,
            investigation_id=investigation_id,
            descending=page.descending,
            context_sha256=page.context_sha256,
        )
        if page.cursor is not None
        else None
    )
    if page.cursor is not None and decoded_cursor is None:
        raise ValueError("Operator keyset page cursor is missing its decoded payload")
    previous_page_sha256 = (
        decoded_cursor["previous_page_sha256"] if decoded_cursor is not None else "0" * 64
    )
    assert_manifest_consistency(
        section=section,
        investigation_id=investigation_id,
        items=page.items,
        previous_page_sha256=previous_page_sha256,
        context_sha256=page.context_sha256,
        cursor=page.cursor,
        pivot=page.pivot,
        descending=page.descending,
        limit=page.limit,
        total=page.total,
        has_more=page.has_more,
        source_bounded=page.source_bounded,
        omission_reasons=entry.omission_reasons,
        unbounded_reason=page.unbounded_reason,
        selected=len(page.items),
        omitted=page.total - len(page.items),
        claimed_page_sha256=page.page_sha256,
        claimed_source_ref=page.source_ref,
        manifest=entry,
        cursor_authentication="unauthenticated_position",
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
        effective_policy_for: Callable[[UUID], Awaitable[Any | None]] | None = None,
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
        self._effective_policy_for = effective_policy_for

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
                if not InvestigationService.bridge_disclosure_allowed(snapshot, envelope)
                else ()
            ),
        )
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
        context_bytes = context.canonical_bytes()
        content_bytes = _canonical_context_content_bytes(context)
        return InvestigationOperatorContextV1(
            investigation_id=self._investigation_id,
            working_state_version=state.version,
            working_state_sha256=state.sha256,
            context_sha256=hashlib.sha256(content_bytes).hexdigest(),
            context_bytes=len(context_bytes),
            truncation=_truncation_flags(context),
            truncation_counts=dict(context.investigation.truncation_counts),
            truncation_pointers=_truncation_pointers(
                self._investigation_id,
                _truncation_flags(context),
            ),
            current_opinion=context.current_opinion,
            context=context,
        )

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
        if cursor is not None:
            if isinstance(cursor, bool):
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
            if isinstance(cursor, int) or (isinstance(cursor, str) and cursor.isdecimal()):
                raise OperatorToolInvocationError("operator_context_offset_cursor_unsupported", 422)
            if expected_version is None or expected_sha256 is None:
                raise OperatorToolInvocationError("operator_context_cursor_requires_cas", 422)
            elif cursor == "":
                if offset != 0:
                    raise OperatorToolInvocationError(
                        "operator_context_offset_cursor_unsupported", 422
                    )
                cursor = None
            elif offset != 0:
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422)
        if offset > 0 and cursor is None:
            raise OperatorToolInvocationError("operator_context_offset_cursor_unsupported", 422)
        if state.pending_context_sha256 is not None:
            raise OperatorToolInvocationError("bridge_checkpoint_pending", 409)
        investigation = await self._investigations.get(self._investigation_id)
        if investigation is None:
            raise OperatorToolInvocationError("investigation_not_found", 404)
        if offset < 0 or not 1 <= limit <= 20:
            raise OperatorToolInvocationError("operator_context_page_invalid", 422)

        if section in _SOURCE_PAGED_CONTEXT_SECTIONS:
            keyset_cursor = cursor if isinstance(cursor, str) else None
            operator_context = await self.get_context(
                expected_version=state.version,
                expected_sha256=state.sha256,
            )
            context_sha256 = operator_context.context_sha256
            if keyset_cursor is not None:
                try:
                    decode_keyset_cursor(
                        keyset_cursor,
                        section=section,
                        investigation_id=self._investigation_id,
                        descending=context_section_descending(section),
                        context_sha256=context_sha256,
                    )
                except StaleContextCursorError:
                    raise OperatorToolInvocationError(
                        "operator_context_cursor_stale", 409
                    ) from None
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
                    offset=offset if cursor is None else 0,
                    limit=limit,
                    context_sha256=context_sha256,
                )
            except StaleContextCursorError:
                raise OperatorToolInvocationError("operator_context_cursor_stale", 409) from None
            except ValueError as exc:
                if str(exc) == "Section does not use source-bounded keyset pagination":
                    raise OperatorToolInvocationError(
                        "operator_context_section_unknown", 404
                    ) from None
                raise OperatorToolInvocationError("operator_context_cursor_invalid", 422) from None
            latest_operator_context = await self.get_context(
                expected_version=state.version,
                expected_sha256=state.sha256,
            )
            if latest_operator_context.context_sha256 != context_sha256:
                raise OperatorToolInvocationError("operator_context_cursor_stale", 409)
            _assert_keyset_page_consistency(
                page,
                section=section,
                investigation_id=self._investigation_id,
            )
            omission = page.omission_manifest[0]
            next_offset = offset + len(page.items) if page.has_more and cursor is None else None
            if section == "finding_versions":
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
                except StaleContextCursorError:
                    raise OperatorToolInvocationError(
                        "operator_context_cursor_stale", 409
                    ) from None
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
        operator_context = await self.get_context(
            expected_version=state.version,
            expected_sha256=state.sha256,
        )
        context_sha256 = operator_context.context_sha256
        if isinstance(cursor, str) and cursor:
            try:
                offset = decode_offset_cursor(
                    cursor,
                    section=section,
                    investigation_id=self._investigation_id,
                    context_sha256=context_sha256,
                )["offset"]
            except StaleContextCursorError:
                raise OperatorToolInvocationError("operator_context_cursor_stale", 409) from None
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
        latest_operator_context = await self.get_context(
            expected_version=state.version,
            expected_sha256=state.sha256,
        )
        if latest_operator_context.context_sha256 != context_sha256:
            raise OperatorToolInvocationError("operator_context_cursor_stale", 409)
        next_offset = offset + len(items) if offset + len(items) < total else None
        page_sha256, source_ref, next_cursor, omission, pivot, has_more = _materialized_page_proof(
            section=section,
            investigation_id=self._investigation_id,
            context_sha256=context_sha256,
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
            context_sha256=context_sha256,
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
        actor_id: str | None = None,
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
                actor_id=actor_id,
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

    @staticmethod
    def _is_trusted_passive_read(
        tool_name: str,
        definition: Any,
        registered: tuple[Any, Any] | None,
    ) -> bool:
        return (
            definition is not None
            and tool_name in _VT_PASSIVE_READ_TOOLS
            and definition.effect is ToolEffect.EXTERNAL_READ
            and registered is not None
            and is_trusted_vt_passive_read_adapter(tool_name, registered[1])
        )

    async def _settle_passive_read_reservation(self, invocation: ToolInvocation) -> ToolInvocation:
        """Close an uncertain passive GET reservation before a fresh retry."""
        envelope = await self._envelopes.get(self._investigation_id)
        reservation = (
            next(
                (
                    item
                    for item in envelope.budget.reservations
                    if item.operation_key == invocation.operation_key
                ),
                None,
            )
            if envelope is not None
            else None
        )
        if reservation is not None and reservation.status is not BudgetReservationStatus.RESERVED:
            return invocation
        consumed = dict(invocation.consumed_budget) or dict(invocation.reserved_budget)
        reviewed = invocation
        if (
            invocation.status is not ToolInvocationStatus.NEEDS_REVIEW
            or dict(invocation.consumed_budget) != consumed
        ):
            reviewed = invocation.transition(
                ToolInvocationStatus.NEEDS_REVIEW,
                consumed_budget=consumed,
            )
            reviewed = await self._invocations.update_if_version(
                reviewed, expected_version=invocation.version
            )
        await self._execution._complete_tool_reservation(
            investigation_id=self._investigation_id,
            operation_key=reviewed.operation_key,
            result=ToolResult(
                ToolInvocationStatus.NEEDS_REVIEW,
                budget_consumed=consumed,
            ),
            reserved_amounts=reviewed.reserved_budget,
            uncertain=True,
            external_effect=True,
            retryable_external_read=True,
        )
        await self._execution.commit_current_uow()
        return reviewed

    async def _framework_pre_submission_proof(
        self, invocation: ToolInvocation
    ) -> tuple[FrameworkPreSubmissionProof | None, str | None]:
        return await self._execution._framework_pre_submission_proof(invocation)

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
        actor_id: str | None,
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
        advisor_external_allowed = InvestigationService.bridge_disclosure_allowed(
            snapshot, envelope
        )
        advisor_disclosure_denied = tool_name == "advisor.consult" and not advisor_external_allowed
        registered = self._registry.get(tool_name, schema_version)
        definition = registered[0] if registered is not None else None
        requires_external_disclosure = definition is not None and definition.effect in {
            ToolEffect.EXTERNAL_READ,
            ToolEffect.ACQUIRE,
            ToolEffect.EXTERNAL_ACTIVE,
            ToolEffect.EXTERNAL_SUBMIT,
        }
        if requires_external_disclosure and not await self._policy_envelope_authorized(envelope):
            self._require_external_disclosure(snapshot, None)
        # Start-time policy inheritance is the only path to an OSINT_POLICY
        # envelope. An old or unpinned investigation cannot acquire one here.
        if requires_external_disclosure and envelope is None:
            self._require_external_disclosure(snapshot, envelope)
        if (
            not advisor_disclosure_denied
            and requires_external_disclosure
            and tool_name != "advisor.consult"
            and not InvestigationService.external_llm_allowed(snapshot, envelope)
        ):
            self._require_external_disclosure(snapshot, envelope)
        if (
            requires_external_disclosure
            and tool_name.startswith("vt.")
            and not InvestigationService.vt_external_disclosure_allowed(
                snapshot, envelope, tool_name
            )
        ):
            raise OperatorToolInvocationError("external_disclosure_denied", 403)

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
            if existing.status in {
                ToolInvocationStatus.PENDING,
                ToolInvocationStatus.RUNNING,
                ToolInvocationStatus.NEEDS_REVIEW,
            } and self._is_trusted_passive_read(tool_name, definition, registered):
                existing = await self._settle_passive_read_reservation(existing)
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
            proof, proof_rejection = await self._framework_pre_submission_proof(existing)
            if existing.status is ToolInvocationStatus.RUNNING and self._is_trusted_passive_read(
                tool_name, definition, registered
            ):
                reviewed = existing.transition(ToolInvocationStatus.NEEDS_REVIEW)
                existing = await self._invocations.update_if_version(
                    reviewed, expected_version=existing.version
                )
                if proof is not None:
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
                else:
                    existing = await self._settle_passive_read_reservation(existing)
                continue
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
            if (
                existing.status is ToolInvocationStatus.NEEDS_REVIEW
                and self._is_trusted_passive_read(tool_name, definition, registered)
            ):
                # A passive VT GET can safely be attempted under a fresh
                # invocation even if canonical result archival failed; the
                # coordinator still checks current authority, disclosure and
                # budget before each physical request.
                await self._settle_passive_read_reservation(existing)
                continue
            details: dict[str, Any] = {"existing_invocation_id": str(existing.id)}
            if proof_rejection is not None:
                details["framework_proof_rejection"] = proof_rejection
            raise OperatorToolInvocationError(
                "ambiguous_tool_invocation_requires_reconciliation", 409, details=details
            )

        # Settling an orphaned passive-read reservation advances the approved
        # envelope. Reload it and repeat disclosure checks before reserving the
        # fresh invocation so dispatch uses the current budget and authority.
        envelope = await self._envelopes.get(self._investigation_id)
        advisor_external_allowed = InvestigationService.bridge_disclosure_allowed(
            snapshot, envelope
        )
        if requires_external_disclosure and not await self._policy_envelope_authorized(envelope):
            self._require_external_disclosure(snapshot, None)
        if requires_external_disclosure and envelope is None:
            self._require_external_disclosure(snapshot, envelope)
        if (
            requires_external_disclosure
            and tool_name != "advisor.consult"
            and not InvestigationService.external_llm_allowed(snapshot, envelope)
        ):
            self._require_external_disclosure(snapshot, envelope)
        if (
            requires_external_disclosure
            and tool_name.startswith("vt.")
            and not InvestigationService.vt_external_disclosure_allowed(
                snapshot, envelope, tool_name
            )
        ):
            raise OperatorToolInvocationError("external_disclosure_denied", 403)

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
                actor_id=actor_id,
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

    async def _policy_envelope_authorized(self, envelope: Any) -> bool:
        from cti_app.domain.investigation import EnvelopeAuthorityKind

        if envelope is None or envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY:
            return True
        resolver = self._effective_policy_for
        if resolver is None:
            return False
        effective = await resolver(self._investigation_id)
        return bool(effective is not None and effective.authorized)

    async def _result(
        self,
        invocation: ToolInvocation,
        state_version: int,
        *,
        replay: bool,
        tool_result: Any | None = None,
        unavailable_reason: str | None = None,
    ) -> OperatorToolInvocationResult:
        if tool_result is None and invocation.tool_name in {"finding.propose", "finding.revise"}:
            registered = self._registry.get(invocation.tool_name, invocation.schema_version)
            replay_lookup = (
                getattr(registered[1], "result_for_replay", None) if registered else None
            )
            if callable(replay_lookup):
                replay_output = await replay_lookup(
                    invocation.normalized_parameters, self._investigation_id
                )
                if isinstance(replay_output, Mapping):
                    tool_result = ToolResult(
                        status=invocation.status,
                        observation_refs=invocation.response_refs,
                        output=replay_output,
                    )

        status_error_code = {
            ToolInvocationStatus.UNAVAILABLE: "tool_unavailable",
            ToolInvocationStatus.POLICY_DENIED: "tool_policy_denied",
            ToolInvocationStatus.NEEDS_REVIEW: "invocation_needs_review",
            ToolInvocationStatus.FAILED: "tool_failed",
        }.get(invocation.status)
        nested_error_code = getattr(tool_result, "error_code", None) or (
            tool_result.output.get("error_code") if tool_result is not None else None
        )
        safe_nested_error_code = _safe_operator_error_code(
            nested_error_code, invocation.normalized_parameters
        )
        error_code = (
            safe_nested_error_code or status_error_code if status_error_code is not None else None
        )
        job_id = None
        if tool_result is not None:
            raw_job_id = tool_result.output.get("job_id")
            if isinstance(raw_job_id, str):
                try:
                    job_id = UUID(raw_job_id)
                except ValueError:
                    job_id = None
        response_refs: list[UUID] = []
        for observation_id in invocation.response_refs:
            if not isinstance(observation_id, UUID) or observation_id in response_refs:
                continue
            if self._observations is None:
                # execute_tool_call validates refs before persisting the invocation.
                response_refs.append(observation_id)
                continue
            observation = await self._observations.get(observation_id)
            if observation is not None and observation.investigation_id == self._investigation_id:
                response_refs.append(observation_id)
        published_refs = set(response_refs)
        referenced_ids: list[UUID] = [
            item for item in invocation.response_refs if isinstance(item, UUID)
        ]
        if tool_result is not None:
            referenced_ids.extend(
                item
                for item in getattr(tool_result, "observation_refs", ())
                if isinstance(item, UUID)
            )
        omitted_observation_refs = [
            {"observation_ref": str(item), "reason": "not_verified_in_investigation"}
            for item in dict.fromkeys(referenced_ids)
            if isinstance(item, UUID) and item not in published_refs
        ]

        persisted: list[dict[str, Any]] = []
        if self._observations is not None and self._evidence_summaries is not None:
            for observation_id in response_refs[:8]:
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

        result_data: Mapping[str, Any] | None = None
        if tool_result is not None and invocation.status in {
            ToolInvocationStatus.UNAVAILABLE,
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }:
            if safe_nested_error_code is not None:
                result_data = {"error_code": safe_nested_error_code}
        elif tool_result is not None:
            result_data = (
                _finding_tool_result_data(invocation, tool_result)
                if invocation.tool_name in {"finding.propose", "finding.revise"}
                else tool_result.output
            )
        if invocation.tool_name in {"finding.propose", "finding.revise"}:
            output_data: dict[str, Any] = {"result": dict(result_data or {})}
            if persisted:
                output_data["observations"] = persisted
        elif persisted:
            output_data = {"observations": persisted}
        elif result_data is not None:
            output_data = {"result": result_data}
        else:
            output_data = {}
        output_data["omitted_observation_refs"] = omitted_observation_refs
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
            safe_reason_key = _safe_operator_error_code(
                unavailable_reason, invocation.normalized_parameters
            )
            registered = self._registry.get(invocation.tool_name, invocation.schema_version)
            required_capability = (
                getattr(registered[0], "capability_key", None) if registered is not None else None
            )
            output["error"] = {
                "code": error_code or invocation.status.value,
                "reason": _operator_tool_error_message(
                    error_code or invocation.status.value,
                    safe_reason_key=safe_reason_key,
                    required_capability=required_capability,
                ),
            }
        return OperatorToolInvocationResult(
            investigation_id=invocation.investigation_id,
            working_state_version=state_version,
            invocation=replace(invocation, response_refs=tuple(response_refs)),
            error_code=error_code,
            job_id=job_id,
            idempotent_replay=replay,
            output=output,
        )


def _safe_operator_error_code(value: object, parameters: Mapping[str, Any]) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _OPERATOR_ERROR_CODE.fullmatch(value) is None
    ):
        return None
    if _SECRET_FIELD.search(value):
        return None
    if len(value) in {32, 40, 64} and all(character in "0123456789abcdef" for character in value):
        return None
    if value.casefold() in _parameter_string_values(parameters):
        return None
    return value


def _parameter_string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, Mapping):
        result: set[str] = set()
        for item in value.values():
            result.update(_parameter_string_values(item))
        return result
    if isinstance(value, (list, tuple)):
        result = set()
        for item in value:
            result.update(_parameter_string_values(item))
        return result
    return set()


def _operator_tool_error_message(
    code: str,
    *,
    safe_reason_key: str | None = None,
    required_capability: str | None = None,
) -> str:
    if safe_reason_key == "external_llm_disclosure_denied":
        return _OPERATOR_ERROR_MESSAGES["external_disclosure_denied"]
    if (
        code == "tool_unavailable"
        and isinstance(required_capability, str)
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", required_capability) is not None
    ):
        return (
            f"This tool requires the {required_capability} capability, which is unavailable "
            "in the current runtime. Select an available tool or enable that capability."
        )
    return _OPERATOR_ERROR_MESSAGES.get(
        code,
        "The tool could not complete the request. Review this error code and current "
        "investigation context, correct the inputs, then retry.",
    )


def _finding_tool_result_data(invocation: ToolInvocation, tool_result: Any) -> dict[str, Any]:
    raw_output = tool_result.output if isinstance(tool_result.output, Mapping) else {}
    result = {key: raw_output[key] for key in _FINDING_RESULT_FIELDS if key in raw_output}
    valid_assessments = {item.value for item in FindingAssessment}
    requested = result.get(
        "assessment_requested", invocation.normalized_parameters.get("assessment")
    )
    if requested not in valid_assessments:
        requested = None
    recorded = result.get("assessment_recorded", result.get("assessment"))
    if recorded not in valid_assessments:
        recorded = None
    if requested is not None:
        result["assessment_requested"] = requested
    if recorded is not None:
        result["assessment"] = recorded
        result["assessment_recorded"] = recorded

    if requested is not None and recorded is not None and requested != recorded:
        code = result.get("assessment_downgrade_code")
        if not isinstance(code, str) or _OPERATOR_ERROR_CODE.fullmatch(code) is None:
            kind = result.get("kind", invocation.normalized_parameters.get("kind"))
            subject_ref = invocation.normalized_parameters.get("subject_ref")
            code = (
                "downgraded_no_matching_subject"
                if kind in _FINDING_OBJECT_KINDS and not subject_ref
                else "downgraded_by_provenance_guard"
            )
        result["assessment_downgrade_code"] = code
        prewrite_message = result.get("assessment_prewrite_message")
        if isinstance(prewrite_message, str) and len(prewrite_message) <= 512:
            result["assessment_prewrite_message"] = prewrite_message
        else:
            result["assessment_prewrite_message"] = _FINDING_DOWNGRADE_MESSAGES.get(
                code,
                "The requested assessment was constrained by the finding provenance rules. "
                "Review the recorded assessment and cited observations.",
            )
    else:
        result["assessment_downgrade_code"] = None
        result.pop("assessment_prewrite_message", None)
    return result


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
    # Trusted source refs are persisted beside VT parameters for the S3 consume
    # guard, but they are server-derived metadata rather than caller input. Keep
    # them out of semantic replay matching so retries still find the original
    # unresolved operation.
    normalized.pop(_VT_SOURCE_REFS_PARAMETER, None)
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
