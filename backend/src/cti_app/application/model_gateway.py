from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.domain.model_runs import (
    ModelOutputRejection,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
    ModelUsage,
)
from cti_app.logging import get_correlation_id


class ModelGatewayError(RuntimeError):
    code = "model_gateway_error"
    retryable = False
    provider = "unknown"
    phase = "model_call"
    attempts = 1


class ExternalModelBlockedError(ModelGatewayError):
    pass


class BinaryModelInputError(ModelGatewayError):
    pass


class StructuredOutputError(ModelGatewayError):
    pass


class StructuredModelUnavailableError(ModelGatewayError):
    code = "structured_model_unavailable"
    retryable = True
    provider = "qwen"
    phase = "structuring"


class BackgroundResponsePendingError(ModelGatewayError):
    def __init__(
        self,
        message: str,
        *,
        response_id: str | None = None,
        background_status: str = "unknown",
        progress: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.response_id = response_id
        self.background_status = background_status
        self.progress = progress or {}


class ModelRoutingHint(StrEnum):
    WEB_RESEARCH = "web_research"
    BULK_EXTRACTION = "bulk_extraction"
    AMBIGUOUS_CLUSTERING = "ambiguous_clustering"
    STANDARD_DRAFT = "standard_draft"
    PREMIUM_SYNTHESIS = "premium_synthesis"
    CRITIQUE = "critique"
    DISCOVERY_MERGE = "discovery_merge"


class AdapterResultStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_BACKGROUND = "waiting_background"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class ConversationContext:
    mode: str
    id: UUID
    # Proves the bound browser tab still holds the expected conversation head.
    # This — not external_locator — is the CONTINUE routing precondition.
    expected_turn_id: str | None = None
    parent_turn_id: UUID | None = None
    previous_head_hash: str | None = None
    expected_profile: str | None = None
    requested_model: str | None = None
    external_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"fresh", "continue"}:
            raise ValueError("Conversation mode must be fresh or continue")
        if self.mode == "fresh" and self.expected_turn_id is not None:
            raise ValueError("Fresh mode must not carry an expected_turn_id")
        if self.mode == "continue" and not self.expected_turn_id:
            raise ValueError("Continue mode requires expected_turn_id")

    def bridge_payload(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "id": str(self.id),
            "expected_turn_id": self.expected_turn_id,
        }


@dataclass(frozen=True, slots=True)
class ConversationResult:
    id: str
    mode: str
    external_locator: str | None
    turn_id: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class ModelRequest:
    text: str
    prompt_template_id: str
    prompt_template_version: str
    evidence_pack_hash: str
    external_llm_allowed: bool
    routing_hint: ModelRoutingHint
    sensitivity: str = "internal"
    metadata: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    # Deliberate turn control. Routing to a research-capable model must never
    # implicitly enable web search in a shared conversation.
    web_search: bool = False
    background: bool = False
    provider: ModelProvider | None = None
    # Stateless requests (bulk extraction, drafting) carry no conversation at all.
    conversation: ConversationContext | None = None
    run_id: UUID | None = None
    # A failed request is never retried implicitly.  This escape hatch is only
    # for a caller that has proved no provider submission took place.
    allow_failed_resubmit: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Model input text must not be empty")
        if len(self.evidence_pack_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.evidence_pack_hash
        ):
            raise ValueError("evidence_pack_hash must be a lowercase SHA-256")
        if _contains_binary(self.metadata) or _contains_binary(self.parameters):
            raise BinaryModelInputError("Binary values cannot be sent to a model")


@dataclass(frozen=True, slots=True)
class SafeModelRequest:
    text: str
    prompt_template_id: str
    prompt_template_version: str
    evidence_pack_hash: str
    routing_hint: ModelRoutingHint
    sensitivity: str
    metadata: dict[str, Any]
    parameters: dict[str, Any]
    web_search: bool
    background: bool
    authorized_input_hash: str
    request_id: str | None = None
    conversation: ConversationContext | None = None


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: AdapterResultStatus
    provider: ModelProvider
    requested_model: str
    actual_model_version: str | None
    usage: ModelUsage
    response_id: str | None = None
    output_text: str | None = None
    structured_output: BaseModel | None = None
    conversation: ConversationResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelExecution:
    run: ModelRun
    output_text: str | None = None
    structured_output: BaseModel | None = None
    conversation: ConversationResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelAdapter(Protocol):
    provider: ModelProvider
    requested_model: str
    is_external: bool

    async def invoke(
        self,
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: type[BaseModel] | None = None,
    ) -> AdapterResult: ...

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[BaseModel] | None = None,
    ) -> AdapterResult: ...


class ResearchModel(Protocol):
    async def research(self, request: ModelRequest) -> ModelExecution: ...


class StructuredExtractionModel(Protocol):
    async def extract(
        self, request: ModelRequest, output_schema: type[BaseModel]
    ) -> ModelExecution: ...


class DraftingModel(Protocol):
    async def draft(
        self, request: ModelRequest, output_schema: type[BaseModel] | None = None
    ) -> ModelExecution: ...


class CriticModel(Protocol):
    async def critique(self, request: ModelRequest) -> ModelExecution: ...


class ModelOutputStore(Protocol):
    async def store(self, content: bytes, *, mime_type: str) -> str: ...

    async def read(self, reference: str, *, max_bytes: int) -> bytes: ...


class ModelRunRepository(Protocol):
    async def add(self, run: ModelRun) -> None: ...

    async def get(self, run_id: UUID) -> ModelRun | None: ...

    async def get_for_update(self, run_id: UUID) -> ModelRun | None: ...

    async def save(self, run: ModelRun) -> None: ...


class ModelRunUnitOfWork(Protocol):
    model_runs: ModelRunRepository
    model_output_rejections: Any

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class ModelRunUnitOfWorkFactory(Protocol):
    def __call__(self) -> ModelRunUnitOfWork: ...


class ModelRouter:
    def __init__(
        self,
        *,
        openai_research: ModelAdapter,
        openai_structured: ModelAdapter,
        openai_drafting: ModelAdapter | None = None,
        openai_critic: ModelAdapter | None = None,
        qwen: ModelAdapter,
        fake: ModelAdapter,
        forced_provider: ModelProvider | None = None,
    ) -> None:
        self._default_adapters = {
            ModelProvider.QWEN: qwen,
            ModelProvider.FAKE: fake,
        }
        self._openai_research = openai_research
        self._openai_structured = openai_structured
        self._openai_drafting = openai_drafting or openai_research
        self._openai_critic = openai_critic or openai_research
        self._forced_provider = forced_provider

    def select(self, request: ModelRequest, role: ModelRole) -> ModelAdapter:
        if request.provider is not None:
            return self.by_provider(request.provider, role)
        if self._forced_provider is not None:
            return self.by_provider(self._forced_provider, role)
        if role is ModelRole.RESEARCH or request.routing_hint in {
            ModelRoutingHint.WEB_RESEARCH,
            ModelRoutingHint.AMBIGUOUS_CLUSTERING,
            ModelRoutingHint.PREMIUM_SYNTHESIS,
            ModelRoutingHint.CRITIQUE,
            ModelRoutingHint.DISCOVERY_MERGE,
        }:
            return self.by_provider(ModelProvider.OPENAI, role)
        return self.by_provider(ModelProvider.QWEN, role)

    def by_provider(self, provider: ModelProvider, role: ModelRole) -> ModelAdapter:
        if provider is ModelProvider.OPENAI:
            if role is ModelRole.STRUCTURED_EXTRACTION:
                return self._openai_structured
            if role is ModelRole.DRAFTING:
                return self._openai_drafting
            if role is ModelRole.CRITIC:
                return self._openai_critic
            return self._openai_research
        return self._default_adapters[provider]


class ModelGateway(ResearchModel, StructuredExtractionModel, DraftingModel, CriticModel):
    def __init__(
        self,
        router: ModelRouter,
        uow_factory: ModelRunUnitOfWorkFactory,
        output_store: ModelOutputStore,
        diagnostics: DiagnosticsLog | None = None,
    ) -> None:
        self._router = router
        self._uow_factory = uow_factory
        self._output_store = output_store
        # Research, merge, extraction and drafting all funnel through _execute,
        # so a bridge or Qwen failure is recorded once here for every caller.
        self._diagnostics = diagnostics or DiagnosticsLog(None)

    async def research(self, request: ModelRequest) -> ModelExecution:
        return await self._execute(request, ModelRole.RESEARCH)

    async def extract(
        self, request: ModelRequest, output_schema: type[BaseModel]
    ) -> ModelExecution:
        return await self._execute(
            request, ModelRole.STRUCTURED_EXTRACTION, output_schema=output_schema
        )

    async def draft(
        self, request: ModelRequest, output_schema: type[BaseModel] | None = None
    ) -> ModelExecution:
        return await self._execute(request, ModelRole.DRAFTING, output_schema=output_schema)

    async def critique(self, request: ModelRequest) -> ModelExecution:
        return await self._execute(request, ModelRole.CRITIC)

    async def execute(self, request: ModelRequest, role: ModelRole) -> ModelExecution:
        return await self._execute(request, role)

    async def get_run(self, run_id: UUID) -> ModelRun | None:
        async with self._uow_factory() as uow:
            return await uow.model_runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        return await self._output_store.read(reference, max_bytes=max_bytes)

    async def archive_output(self, content: bytes, *, mime_type: str) -> str:
        return await self._output_store.store(content, mime_type=mime_type)

    async def adopt_recovery_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        provenance: str,
        actor_id: str,
        source_model_run_id: UUID | None = None,
    ) -> ModelRun:
        digest = hashlib.sha256(content).hexdigest()
        async with self._uow_factory() as uow:
            existing = await uow.model_runs.get(run_id)
            if existing is None:
                raise ModelGatewayError(f"Model run {run_id} does not exist")
            recovery = (existing.error_details or {}).get("recovery")
            if (
                existing.status is ModelRunStatus.SUCCEEDED
                and existing.raw_output_sha256 == digest
                and isinstance(recovery, dict)
                and recovery.get("provenance") == provenance
                and recovery.get("source_model_run_id")
                == (str(source_model_run_id) if source_model_run_id else None)
            ):
                return existing
            allowed = {ModelRunStatus.NEEDS_REVIEW}
            if provenance in {"manual_import", "visible_recovery"}:
                allowed |= {
                    ModelRunStatus.WAITING_BACKGROUND,
                    ModelRunStatus.FAILED,
                }
            if existing.status not in allowed:
                raise ModelGatewayError("ModelRun is not eligible for this recovery")
        reference = await self._output_store.store(
            content, mime_type="text/markdown; charset=utf-8"
        )
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(run_id)
            if run is None:
                raise ModelGatewayError(f"Model run {run_id} does not exist")
            run.adopt_recovery(
                output_reference=reference,
                output_sha256=digest,
                output_chars=len(content.decode(errors="replace")),
                provenance=provenance,
                actor_id=actor_id,
                source_model_run_id=source_model_run_id,
            )
            await uow.model_runs.save(run)
            await uow.commit()
            return run

    async def link_recovery_child(self, parent_run_id: UUID, child_run_id: UUID) -> None:
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(parent_run_id)
            if run is None or run.status is not ModelRunStatus.NEEDS_REVIEW:
                raise ModelGatewayError("Parent ModelRun is not recoverable")
            run.error_details = {
                **(run.error_details or {}),
                "recovery_child_model_run_id": str(child_run_id),
            }
            run.updated_at = datetime.now(UTC)
            await uow.model_runs.save(run)
            await uow.commit()

    async def create_manual_research_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        evidence_pack_hash: str,
        actor_id: str,
    ) -> ModelRun:
        """Record user Markdown as a ModelRun marked manual_import; no API call."""
        digest = hashlib.sha256(content).hexdigest()

        async with self._uow_factory() as uow:
            existing = await uow.model_runs.get(run_id)
            if existing is not None:
                if (
                    existing.raw_output_sha256 == digest
                    and existing.status is ModelRunStatus.SUCCEEDED
                ):
                    recovery = (existing.error_details or {}).get("recovery")
                    if isinstance(recovery, dict) and recovery.get("provenance") == "manual_import":
                        return existing
                raise ModelGatewayError(f"Model run {run_id} already exists with different content")

        reference = await self._output_store.store(
            content, mime_type="text/markdown; charset=utf-8"
        )

        # Close via domain methods rather than setting a dozen terminal fields here.
        async with self._uow_factory() as uow:
            run = ModelRun(
                id=run_id,
                provider=ModelProvider.FAKE,
                model_role=ModelRole.RESEARCH,
                requested_model="manual-import",
                actual_model_version="manual-import",
                prompt_template_id="manual-import",
                prompt_template_version="1.0",
                # No prompt was submitted: hash of the empty string.
                authorized_input_hash=hashlib.sha256(b"").hexdigest(),
                evidence_pack_hash=evidence_pack_hash,
                parameters={},
            )
            run.succeed_manual_import(
                output_reference=reference,
                output_sha256=digest,
                output_chars=len(content.decode(errors="replace")),
                actor_id=actor_id,
            )
            await uow.model_runs.add(run)
            await uow.commit()
            return run

    async def record_output_diagnostics(
        self,
        run_id: UUID,
        *,
        normalized_reference: str | None,
        normalized_sha256: str | None,
        parser_stage: str,
        normalization_version: str | None,
        transformations: tuple[str, ...],
        validation_errors: tuple[dict[str, Any], ...],
        json_error_line: int | None = None,
        json_error_column: int | None = None,
    ) -> None:
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(run_id)
            if run is None:
                raise ModelGatewayError(f"Model run {run_id} does not exist")
            run.normalized_output_reference = normalized_reference
            run.normalized_output_sha256 = normalized_sha256
            run.parser_stage = parser_stage[:64]
            run.normalization_version = normalization_version
            run.transformations = transformations
            run.validation_errors = validation_errors
            run.json_error_line = json_error_line
            run.json_error_column = json_error_column
            await uow.model_runs.save(run)
            if run.raw_output_reference:
                for item in validation_errors:
                    await uow.model_output_rejections.append(
                        ModelOutputRejection(
                            model_run_id=run_id,
                            path=tuple(str(part) for part in item.get("path", [])),
                            error_type=str(item.get("code", "validation_error"))[:128],
                            value_sha256=str(item.get("value_sha256", "0" * 64)),
                            raw_output_reference=run.raw_output_reference,
                        )
                    )
            await uow.commit()

    def build_run(self, request: ModelRequest, role: ModelRole) -> ModelRun:
        adapter = self._router.select(request, role)
        safe_request = sanitize_model_request(request)
        return ModelRun(
            provider=adapter.provider,
            model_role=role,
            requested_model=adapter.requested_model,
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash=safe_request.authorized_input_hash,
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=safe_request.parameters,
            id=request.run_id or uuid4(),
        )

    async def resume(
        self, run_id: UUID, *, output_schema: type[BaseModel] | None = None
    ) -> ModelExecution:
        persisted_success: ModelRun | None = None
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(run_id)
            if run is None:
                raise ModelGatewayError(f"Model run {run_id} does not exist")
            if run.status is ModelRunStatus.SUCCEEDED:
                persisted_success = run
            elif run.status is not ModelRunStatus.WAITING_BACKGROUND or not run.response_id:
                raise ModelGatewayError("Model run is not waiting for a background response")
        if persisted_success is not None:
            return await self._persisted_execution(persisted_success, output_schema=output_schema)
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(run_id)
            if (
                run is None
                or run.status is not ModelRunStatus.WAITING_BACKGROUND
                or not run.response_id
            ):
                raise ModelGatewayError("Model run is not waiting for a background response")
            adapter = self._router.by_provider(run.provider, run.model_role)
            elapsed_ms = max(
                0,
                int((datetime.now(UTC) - run.started_at).total_seconds() * 1000),
            )
            try:
                result = await adapter.resume(
                    run.response_id, role=run.model_role, output_schema=output_schema
                )
                if result.status is AdapterResultStatus.WAITING_BACKGROUND:
                    run.wait_for_background(
                        response_id=result.response_id or run.response_id,
                        actual_model_version=result.actual_model_version,
                        usage=result.usage,
                    )
                    await uow.model_runs.save(run)
                    await uow.commit()
                    raise BackgroundResponsePendingError(
                        "Background response is still pending",
                        response_id=result.response_id or run.response_id,
                        background_status=str(result.metadata.get("background_status", "unknown")),
                        progress=(
                            result.metadata.get("bridge_progress", {})
                            if isinstance(result.metadata.get("bridge_progress"), dict)
                            else {}
                        ),
                    )
                if result.status is AdapterResultStatus.NEEDS_REVIEW:
                    run.require_review(
                        str(result.metadata.get("reason", "no_final_answer")),
                        "ChatGPT s'est arrêté sans produire de réponse finale.",
                        details=result.metadata,
                    )
                    await uow.model_runs.save(run)
                    await uow.commit()
                    return ModelExecution(run, metadata=result.metadata)
                execution = await self._complete_run(run, result, duration_ms=elapsed_ms)
                await uow.model_runs.save(run)
                await uow.commit()
                return execution
            except BackgroundResponsePendingError:
                raise
            except Exception as exc:
                run.fail(
                    str(getattr(exc, "code", "model_resume_failed")),
                    _public_error(exc),
                    details=_error_details(exc),
                )
                await uow.model_runs.save(run)
                await uow.commit()
                raise

    async def _execute(
        self,
        request: ModelRequest,
        role: ModelRole,
        *,
        output_schema: type[BaseModel] | None = None,
    ) -> ModelExecution:
        adapter = self._router.select(request, role)
        safe_request = sanitize_model_request(request)
        run = self.build_run(request, role)
        persisted_success: ModelRun | None = None
        resume_run_id: UUID | None = None
        async with self._uow_factory() as uow:
            existing = await uow.model_runs.get_for_update(run.id) if request.run_id else None
            if existing is None:
                await uow.model_runs.add(run)
            elif (
                existing.authorized_input_hash != run.authorized_input_hash
                or existing.provider is not run.provider
                or existing.model_role is not run.model_role
            ):
                raise ModelGatewayError("Existing ModelRun does not match this request")
            else:
                run = existing
            if existing is not None:
                if run.status is ModelRunStatus.SUCCEEDED:
                    persisted_success = run
                elif run.status is ModelRunStatus.WAITING_BACKGROUND:
                    # A submitted background request is reconciled, never posted again.
                    if run.response_id:
                        resume_run_id = run.id
                    else:
                        raise ModelGatewayError(
                            "Model run needs reconciliation before resubmission"
                        )
                elif run.status is ModelRunStatus.RUNNING:
                    # RUNNING covers two very different situations:
                    # - SUBMITTED_OR_UNKNOWN: the prompt may already be in flight at
                    #   the provider. Posting again could double-submit, so this is
                    #   never resubmitted — it needs reconciliation.
                    # - NOT_SUBMITTED: ModelConversationService.add_turn pre-persists
                    #   the ModelRun (for its FK) before ever calling the gateway.
                    #   Nothing has been sent to a provider yet, so this is a first
                    #   submission, not a replay, and is allowed exactly once.
                    if run.submission_state is not ModelSubmissionState.NOT_SUBMITTED:
                        self._diagnostics.record(
                            event="model.reconciliation_required",
                            run_id=run.id,
                            provider=run.provider.value if run.provider else None,
                            role=run.model_role.value if run.model_role else None,
                            status=run.status.value,
                            submission_state=run.submission_state.value,
                            prompt_template_id=run.prompt_template_id,
                            prompt_template_version=run.prompt_template_version,
                            correlation_id=get_correlation_id(),
                            recovery_action="reconciliation_required",
                        )
                        raise ModelGatewayError(
                            "Model run needs reconciliation before resubmission"
                        )
                    self._diagnostics.record(
                        event="model.initial_submission_claim",
                        run_id=run.id,
                        provider=run.provider.value if run.provider else None,
                        role=run.model_role.value if run.model_role else None,
                        previous_status=run.status.value,
                        previous_submission_state=run.submission_state.value,
                        prompt_template_id=run.prompt_template_id,
                        prompt_template_version=run.prompt_template_version,
                        correlation_id=get_correlation_id(),
                        recovery_action="initial_submission_claim",
                    )
                elif run.status is ModelRunStatus.NEEDS_REVIEW:
                    self._diagnostics.record(
                        event="model.reconciliation_required",
                        run_id=run.id,
                        provider=run.provider.value if run.provider else None,
                        role=run.model_role.value if run.model_role else None,
                        status=run.status.value,
                        submission_state=run.submission_state.value,
                        prompt_template_id=run.prompt_template_id,
                        prompt_template_version=run.prompt_template_version,
                        correlation_id=get_correlation_id(),
                        recovery_action="reconciliation_required",
                    )
                    raise ModelGatewayError("Model run needs reconciliation before resubmission")
                elif run.status is ModelRunStatus.FAILED:
                    if not (
                        request.allow_failed_resubmit
                        and run.submission_state is ModelSubmissionState.NOT_SUBMITTED
                    ):
                        raise ModelGatewayError("Failed ModelRun is not safe to resubmit")
                    run.restart_after_certain_pre_submission_failure()
                    await uow.model_runs.save(run)
                elif run.status is ModelRunStatus.BLOCKED:
                    raise ModelGatewayError("Blocked ModelRun cannot be resubmitted")
            if (
                persisted_success is None
                and resume_run_id is None
                and adapter.is_external
                and not request.external_llm_allowed
            ):
                run.fail(
                    "external_llm_blocked",
                    "La politique de diffusion interdit cet appel externe.",
                    blocked=True,
                )
                await uow.model_runs.save(run)
                await uow.commit()
                raise ExternalModelBlockedError(run.error_message)
            if persisted_success is None and resume_run_id is None:
                run.mark_submission_uncertain()
                await uow.model_runs.save(run)
            await uow.commit()

        if persisted_success is not None:
            return await self._persisted_execution(persisted_success, output_schema=output_schema)
        if resume_run_id is not None:
            return await self.resume(resume_run_id, output_schema=output_schema)

        # L'identité du ModelRun est créée une seule fois et devient la clé
        # stable de toutes les tentatives réseau de ce même appel.
        safe_request = replace(safe_request, request_id=str(run.id))
        started = time.monotonic()
        try:
            result = await adapter.invoke(safe_request, role=role, output_schema=output_schema)
            async with self._uow_factory() as uow:
                persisted = await uow.model_runs.get_for_update(run.id)
                if persisted is None:
                    raise ModelGatewayError(f"Model run {run.id} disappeared")
                if result.status is AdapterResultStatus.WAITING_BACKGROUND:
                    if not result.response_id:
                        raise ModelGatewayError("Background adapter omitted response id")
                    persisted.wait_for_background(
                        response_id=result.response_id,
                        actual_model_version=result.actual_model_version,
                        usage=result.usage,
                    )
                    await uow.model_runs.save(persisted)
                    await uow.commit()
                    return ModelExecution(persisted)
                if result.status is AdapterResultStatus.NEEDS_REVIEW:
                    persisted.require_review(
                        str(result.metadata.get("reason", "no_final_answer")),
                        "ChatGPT s'est arrêté sans produire de réponse finale.",
                        details=result.metadata,
                    )
                    await uow.model_runs.save(persisted)
                    await uow.commit()
                    return ModelExecution(persisted, metadata=result.metadata)
                execution = await self._complete_run(
                    persisted,
                    result,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
                await uow.model_runs.save(persisted)
                await uow.commit()
                return execution
        except Exception as exc:
            self._diagnostics.record_failure(
                event="model.call_failed",
                run_id=run.id,
                stage=request.prompt_template_id,
                correlation_id=get_correlation_id(),
                error=exc,
                error_code=str(getattr(exc, "code", "model_call_failed")),
                provider=run.provider.value if run.provider else None,
                model_role=role.value,
                routing_hint=request.routing_hint.value,
                background=request.background,
                conversation_mode=(request.conversation.mode if request.conversation else None),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            async with self._uow_factory() as uow:
                persisted = await uow.model_runs.get_for_update(run.id)
                if persisted and persisted.status in {
                    ModelRunStatus.RUNNING,
                    ModelRunStatus.WAITING_BACKGROUND,
                }:
                    # The bridge may prove that the browser never reached the
                    # composer click. Preserve that proof so an explicit
                    # allow_failed_resubmit can use the safe NOT_SUBMITTED
                    # path; an attempted or post-submission failure remains
                    # SUBMITTED_OR_UNKNOWN and therefore needs reconciliation.
                    if getattr(exc, "submission_state", None) == "pre_submission":
                        persisted.submission_state = ModelSubmissionState.NOT_SUBMITTED
                    persisted.fail(
                        str(getattr(exc, "code", "model_call_failed")),
                        _public_error(exc),
                        details=_error_details(exc),
                    )
                    await uow.model_runs.save(persisted)
                    await uow.commit()
            raise

    async def _persisted_execution(
        self, run: ModelRun, *, output_schema: type[BaseModel] | None
    ) -> ModelExecution:
        """Return archived output.  Terminal runs never reach an adapter again."""
        reference = run.raw_output_reference or (
            run.output_references[0] if run.output_references else None
        )
        if reference is None:
            raise ModelGatewayError("Succeeded ModelRun has no persisted output")
        content = await self._output_store.read(reference, max_bytes=10_000_000)
        text = content.decode("utf-8")
        structured = validate_structured_output(text, output_schema) if output_schema else None
        return ModelExecution(
            run,
            output_text=text,
            structured_output=structured,
            metadata={"checkpoint": "hit", "recovery_action": "persisted_output"},
        )

    async def _complete_run(
        self, run: ModelRun, result: AdapterResult, *, duration_ms: int
    ) -> ModelExecution:
        if result.structured_output is not None:
            content = result.structured_output.model_dump_json().encode()
            mime_type = "application/json"
        elif result.output_text is not None:
            content = result.output_text.encode()
            mime_type = "text/plain; charset=utf-8"
        else:
            raise ModelGatewayError("Completed adapter response has no output")
        output_reference = await self._output_store.store(content, mime_type=mime_type)
        run.raw_output_reference = output_reference
        run.raw_output_sha256 = hashlib.sha256(content).hexdigest()
        run.raw_output_chars = len(content.decode(errors="replace"))
        run.serializer_version = _optional_metadata_text(result.metadata, "serializer_version")
        citations = result.metadata.get("visible_citations")
        run.visible_citations = (
            tuple(item for item in citations if isinstance(item, dict))
            if isinstance(citations, list)
            else ()
        )
        run.citation_count = len(citations) if isinstance(citations, list) else 0
        run.extracted_url_count = (
            len(
                {
                    item.get("canonical_url")
                    for item in citations
                    if isinstance(item, dict) and isinstance(item.get("canonical_url"), str)
                }
            )
            if isinstance(citations, list)
            else 0
        )
        run.succeed(
            actual_model_version=result.actual_model_version,
            duration_ms=duration_ms,
            usage=result.usage,
            output_references=(output_reference,),
            response_id=result.response_id,
        )
        return ModelExecution(
            run,
            output_text=result.output_text,
            structured_output=result.structured_output,
            conversation=result.conversation,
            metadata=dict(result.metadata),
        )


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*\S+"),
)
_INTERNAL_PATH = re.compile(r"(?<!\w)/(?:home|srv|opt|var|tmp)/[^\s,;]+")
_SENSITIVE_KEYS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "internal_path",
    "actor_id",
    "correlation_id",
    "tenant_id",
)


def sanitize_model_request(request: ModelRequest) -> SafeModelRequest:
    text = _sanitize_text(request.text)
    metadata = _sanitize_mapping(request.metadata)
    parameters = _sanitize_mapping(request.parameters)
    authorized = {
        "text": text,
        "metadata": metadata,
        "parameters": parameters,
        "web_search": request.web_search,
        "prompt_template_id": request.prompt_template_id,
        "prompt_template_version": request.prompt_template_version,
        "evidence_pack_hash": request.evidence_pack_hash,
        "conversation_id": str(request.conversation.id) if request.conversation else None,
        "conversation_mode": request.conversation.mode if request.conversation else "fresh",
        "expected_turn_id": request.conversation.expected_turn_id if request.conversation else None,
        "parent_turn_id": (
            str(request.conversation.parent_turn_id) if request.conversation else None
        ),
        "previous_head_hash": (
            request.conversation.previous_head_hash if request.conversation else None
        ),
        "expected_profile": request.conversation.expected_profile if request.conversation else None,
        "requested_model": request.conversation.requested_model if request.conversation else None,
        "external_id": request.conversation.external_id if request.conversation else None,
    }
    digest = hashlib.sha256(
        json.dumps(authorized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SafeModelRequest(
        text=text,
        prompt_template_id=request.prompt_template_id,
        prompt_template_version=request.prompt_template_version,
        evidence_pack_hash=request.evidence_pack_hash,
        routing_hint=request.routing_hint,
        sensitivity=request.sensitivity,
        metadata=metadata,
        parameters=parameters,
        web_search=request.web_search,
        background=request.background,
        authorized_input_hash=digest,
        conversation=request.conversation,
    )


def validate_structured_output(text: str, schema: type[BaseModel]) -> BaseModel:
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise StructuredOutputError("Model output does not match the required schema") from exc


def _sanitize_mapping(value: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            continue
        if isinstance(item, dict):
            cleaned[key] = _sanitize_mapping(item)
        elif isinstance(item, list):
            cleaned[key] = [
                _sanitize_mapping(part)
                if isinstance(part, dict)
                else _sanitize_text(part)
                if isinstance(part, str)
                else part
                for part in item
            ]
        elif isinstance(item, str):
            cleaned[key] = _sanitize_text(item)
        elif isinstance(item, (int, float, bool)) or item is None:
            cleaned[key] = item
    return cleaned


def _sanitize_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return _INTERNAL_PATH.sub("[INTERNAL_PATH]", value)


def _contains_binary(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, dict):
        return any(_contains_binary(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_binary(item) for item in value)
    return False


def _public_error(exc: Exception) -> str:
    if isinstance(exc, ModelGatewayError):
        return str(exc)[:500]
    return "L'appel au modèle a échoué."


def _error_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {
        "provider": str(getattr(exc, "provider", "unknown"))[:64],
        "phase": str(getattr(exc, "phase", "model_call"))[:64],
        "retryable": bool(getattr(exc, "retryable", False)),
        "attempts": max(1, int(getattr(exc, "attempts", 1))),
    }
    for key in ("bridge_run_id", "bridge_status"):
        value = getattr(exc, key, None)
        if isinstance(value, str) and value:
            details[key] = value[:128]
    submission_state = getattr(exc, "submission_state", None)
    if isinstance(submission_state, str) and submission_state:
        details["submission_state"] = submission_state[:32]
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict) and diagnostics:
        details["bridge_diagnostics"] = _safe_error_diagnostics(diagnostics)
    return details


def _safe_error_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    """Keep bridge diagnostics bounded and exclude any accidental prompt data."""

    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 3:
            return None
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, child in list(item.items())[:50]:
                name = str(key)
                if any(marker in name.casefold() for marker in ("prompt", "composer_text")):
                    continue
                cleaned = clean(child, depth + 1)
                if cleaned is not None:
                    result[name[:64]] = cleaned
            return result
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item[:50]]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item[:256] if isinstance(item, str) else item
        return None

    result = clean(value)
    return result if isinstance(result, dict) else {}


def _optional_metadata_text(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value[:64] if isinstance(value, str) and value else None
