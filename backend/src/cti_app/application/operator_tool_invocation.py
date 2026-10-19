"""Durable operator-led tool calls and their bounded research context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from cti_app.application.evidence_recorder import _sanitize_json, sanitize_evidence_text
from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_BYTES,
    InvestigationContextV1,
    InvestigationOperatorContextV1,
    OperatorContextSectionPageV1,
)
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
from cti_app.application.investigation_tools import ToolEffect, ToolExecutionContext, ToolRegistry
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.application.research_context_builder import (
    CanonicalEvidenceSummaryPort,
    InvestigationContextTooLarge,
    ResearchContextBuilder,
)
from cti_app.domain.investigation import (
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)

_LEGACY_SCHEMA_V1_TOOLS = frozenset(
    {
        "finding.propose",
        "finding.revise",
        "opinion.compose",
        "opinion.note",
        "opinion.revise",
        "advisor.consult",
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

    async def get_context_section(
        self,
        section: str,
        *,
        offset: int = 0,
        limit: int = 20,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
    ) -> OperatorContextSectionPageV1:
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
        return OperatorContextSectionPageV1(
            investigation_id=self._investigation_id,
            section=cast(Any, section),
            offset=offset,
            limit=limit,
            total=total,
            next_offset=next_offset,
            items=items,
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
        advisor_disclosure_denied = tool_name == "advisor.consult" and not (
            InvestigationService.external_llm_allowed(snapshot, envelope)
        )
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

        if state.version != expected_version:
            raise OperatorToolInvocationError("stale_working_state", 409)
        updated_state = state.with_operator_tool_action(
            f"operator_tool:{tool_name}:{operation_key[-16:]}"
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
                purpose=purpose,
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


__all__ = [
    "OperatorToolInvocationError",
    "OperatorToolInvocationResult",
    "OperatorToolInvocationService",
]
