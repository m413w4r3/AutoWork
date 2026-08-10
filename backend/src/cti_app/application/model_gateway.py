from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ValidationError

from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelUsage,
)


class ModelGatewayError(RuntimeError):
    pass


class ExternalModelBlockedError(ModelGatewayError):
    pass


class BinaryModelInputError(ModelGatewayError):
    pass


class StructuredOutputError(ModelGatewayError):
    pass


class BackgroundResponsePendingError(ModelGatewayError):
    pass


class ModelRoutingHint(StrEnum):
    WEB_RESEARCH = "web_research"
    BULK_EXTRACTION = "bulk_extraction"
    AMBIGUOUS_CLUSTERING = "ambiguous_clustering"
    STANDARD_DRAFT = "standard_draft"
    PREMIUM_SYNTHESIS = "premium_synthesis"
    CRITIQUE = "critique"


class AdapterResultStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_BACKGROUND = "waiting_background"


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
    background: bool = False

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
    background: bool
    authorized_input_hash: str


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


@dataclass(frozen=True, slots=True)
class ModelExecution:
    run: ModelRun
    output_text: str | None = None
    structured_output: BaseModel | None = None


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


class ModelRunRepository(Protocol):
    async def add(self, run: ModelRun) -> None: ...

    async def get(self, run_id: UUID) -> ModelRun | None: ...

    async def get_for_update(self, run_id: UUID) -> ModelRun | None: ...

    async def save(self, run: ModelRun) -> None: ...


class ModelRunUnitOfWork(Protocol):
    model_runs: ModelRunRepository

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
        if self._forced_provider is not None:
            return self.by_provider(self._forced_provider, role)
        if role is ModelRole.RESEARCH or request.routing_hint in {
            ModelRoutingHint.WEB_RESEARCH,
            ModelRoutingHint.AMBIGUOUS_CLUSTERING,
            ModelRoutingHint.PREMIUM_SYNTHESIS,
            ModelRoutingHint.CRITIQUE,
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
    ) -> None:
        self._router = router
        self._uow_factory = uow_factory
        self._output_store = output_store

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

    async def resume(
        self, run_id: UUID, *, output_schema: type[BaseModel] | None = None
    ) -> ModelExecution:
        async with self._uow_factory() as uow:
            run = await uow.model_runs.get_for_update(run_id)
            if run is None:
                raise ModelGatewayError(f"Model run {run_id} does not exist")
            if run.status is ModelRunStatus.SUCCEEDED:
                return ModelExecution(run)
            if run.status is not ModelRunStatus.WAITING_BACKGROUND or not run.response_id:
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
                    raise BackgroundResponsePendingError("Background response is still pending")
                execution = await self._complete_run(run, result, duration_ms=elapsed_ms)
                await uow.model_runs.save(run)
                await uow.commit()
                return execution
            except BackgroundResponsePendingError:
                raise
            except Exception as exc:
                run.fail("model_resume_failed", _public_error(exc))
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
        run = ModelRun(
            provider=adapter.provider,
            model_role=role,
            requested_model=adapter.requested_model,
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash=safe_request.authorized_input_hash,
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=safe_request.parameters,
        )
        async with self._uow_factory() as uow:
            await uow.model_runs.add(run)
            if adapter.is_external and not request.external_llm_allowed:
                run.fail(
                    "external_llm_blocked",
                    "La politique de diffusion interdit cet appel externe.",
                    blocked=True,
                )
                await uow.model_runs.save(run)
                await uow.commit()
                raise ExternalModelBlockedError(run.error_message)
            await uow.commit()

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
                execution = await self._complete_run(
                    persisted,
                    result,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
                await uow.model_runs.save(persisted)
                await uow.commit()
                return execution
        except Exception as exc:
            async with self._uow_factory() as uow:
                persisted = await uow.model_runs.get_for_update(run.id)
                if persisted and persisted.status in {
                    ModelRunStatus.RUNNING,
                    ModelRunStatus.WAITING_BACKGROUND,
                }:
                    persisted.fail("model_call_failed", _public_error(exc))
                    await uow.model_runs.save(persisted)
                    await uow.commit()
            raise

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
        "prompt_template_id": request.prompt_template_id,
        "prompt_template_version": request.prompt_template_version,
        "evidence_pack_hash": request.evidence_pack_hash,
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
        background=request.background,
        authorized_input_hash=digest,
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
