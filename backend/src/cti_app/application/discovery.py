from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import (
    ConversationContext,
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ResearchModel,
    StructuredExtractionModel,
)
from cti_app.application.model_output_normalization import (
    JsonEnvelopeError,
    NormalizedModelOutput,
    normalize_discovery_output,
)
from cti_app.application.persistence import DiscoveryUnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
    SourceVerificationStatus,
    canonicalize_http_url,
    deduplicate_sources,
)
from cti_app.domain.model_runs import ModelProvider, ModelRun
from cti_app.logging import get_correlation_id

DISCOVERY_JOB_KIND = "discover_edition"
RETRY_STRUCTURING_JOB_KIND = "retry_discovery_structuring"
PROMPT_TEMPLATE_ID = "monthly-cti-discovery"
PROMPT_TEMPLATE_VERSION = "2.0"
COMPACT_CONTRACT_VERSION = "research-batch-compact-v1"
logger = logging.getLogger(__name__)


class ResearchCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2048)
    excerpt: str | None = Field(default=None, max_length=2_000)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        canonicalize_http_url(value)
        return value


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=1_000)
    publisher: str = Field(min_length=1, max_length=500)
    published_at: date | None
    event_date: date | None
    source_role: SourceRole
    citation: str | None = Field(default=None, max_length=2_000)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        canonicalize_http_url(value)
        return value


class ArtifactAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ioc: Literal["yes", "no", "probable", "unknown"]
    samples: Literal["yes", "no", "probable", "unknown"]
    configurations: Literal["yes", "no", "probable", "unknown"]
    pcap: Literal["yes", "no", "probable", "unknown"]
    rules: Literal["yes", "no", "probable", "unknown"]


class ResearchTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provisional_title: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(min_length=1, max_length=8_000)
    novelty: str = Field(min_length=1, max_length=2_000)
    technical_potential: int = Field(ge=0, le=4)
    event_date: date | None
    actors: list[str] = Field(max_length=100)
    campaigns: list[str] = Field(max_length=100)
    malware: list[str] = Field(max_length=100)
    cves: list[str] = Field(max_length=100)
    victims: list[str] = Field(max_length=100)
    sectors: list[str] = Field(max_length=100)
    countries: list[str] = Field(max_length=100)
    iocs: list[str] = Field(default_factory=list, max_length=500)
    artifact_availability: ArtifactAvailability
    uncertainties: list[str] = Field(max_length=100)
    reasons_for_relevance: list[str] = Field(max_length=100)
    sources: list[ResearchSource] = Field(min_length=1, max_length=100)


class ResearchBatch(BaseModel):
    """Strict, provider-facing output. It remains a proposal, never evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    queries: list[str] = Field(max_length=50)
    citations: list[ResearchCitation] = Field(max_length=500)
    topics: list[ResearchTopic] = Field(max_length=200)


class DiscoverEditionParameters(JobParameters):
    edition_id: UUID
    country: str = Field(min_length=2, max_length=100)
    country_aliases: list[str] = Field(min_length=1, max_length=30)
    period_start: date
    period_end: date
    languages: list[str] = Field(min_length=1, max_length=10)
    source_profile: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    tlp: TLP
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True

    @field_validator("edition_id", mode="before")
    @classmethod
    def parse_edition_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value

    @field_validator("period_start", "period_end", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("tlp", mode="before")
    @classmethod
    def parse_tlp(cls, value: object) -> object:
        return TLP(value) if isinstance(value, str) else value


class RetryStructuringParameters(JobParameters):
    discovery: DiscoverEditionParameters
    research_model_run_id: UUID
    retry_nonce: UUID

    @field_validator("research_model_run_id", "retry_nonce", mode="before")
    @classmethod
    def parse_uuid(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value


class SourceCandidateNotFoundError(LookupError):
    pass


class BridgeCapabilitiesProvider(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    async def archive_conversation(self, conversation_id: UUID) -> None: ...


class ModelOutputArchive(Protocol):
    async def get_run(self, run_id: UUID) -> ModelRun | None: ...

    async def read_output(self, reference: str, *, max_bytes: int = ...) -> bytes: ...

    async def archive_output(self, content: bytes, *, mime_type: str) -> str: ...

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
    ) -> None: ...


class DiscoveryStructuringError(ModelGatewayError):
    code = "discovery_structuring_invalid"
    phase = "pydantic_validation"

    def __init__(
        self,
        message: str,
        *,
        run_id: UUID | None,
        valid_count: int,
        rejected_count: int,
        diagnostic_available: bool,
        parser_stage: str,
        research_model_run_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.valid_count = valid_count
        self.rejected_count = rejected_count
        self.diagnostic_available = diagnostic_available
        self.phase = parser_stage
        self.research_model_run_id = research_model_run_id


class DiscoveryStructuredModelUnavailable(ModelGatewayError):
    code = "structured_model_unavailable"
    retryable = False
    phase = "structuring"

    def __init__(self, research_model_run_id: UUID) -> None:
        super().__init__("Le modèle local de structuration est indisponible.")
        self.research_model_run_id = research_model_run_id


@dataclass(frozen=True, slots=True)
class StructuringResult:
    batch: ResearchBatch
    normalized: NormalizedModelOutput | None
    rejected: tuple[dict[str, Any], ...]
    run_id: UUID


class DiscoveryService:
    def __init__(
        self,
        uow_factory: DiscoveryUnitOfWorkFactory,
        research_model: ResearchModel,
        structured_model: StructuredExtractionModel,
        *,
        bridge_capabilities: Mapping[str, object] | None = None,
        bridge_capabilities_provider: BridgeCapabilitiesProvider | None = None,
        after_discovery: Callable[[UUID], Awaitable[object]] | None = None,
        allow_chatgpt_structuring_fallback: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._research_model = research_model
        self._structured_model = structured_model
        self._bridge_capabilities_provider = bridge_capabilities_provider
        self._after_discovery = after_discovery
        self._allow_chatgpt_structuring_fallback = allow_chatgpt_structuring_fallback
        self._output_archive = (
            cast(ModelOutputArchive, structured_model)
            if all(
                callable(getattr(structured_model, name, None))
                for name in (
                    "get_run",
                    "read_output",
                    "archive_output",
                    "record_output_diagnostics",
                )
            )
            else None
        )
        self._bridge_capabilities = dict(
            bridge_capabilities
            or {
                "transport": "chatgpt_web_ui",
                "web_search": "prompt_instructed",
                "structured_output": "prompt_and_client_validation",
                "background": "memory_only",
                "native_usage": False,
                "native_sources": False,
            }
        )

    async def discover_edition(
        self, parameters: DiscoverEditionParameters, context: JobExecutionContext
    ) -> DiscoveryBatch:
        request_hash = discovery_request_hash(parameters)
        async with self._uow_factory() as uow:
            existing = await uow.discovery_batches.get_by_request_hash(
                parameters.edition_id, request_hash
            )
            if existing is not None:
                return existing

        await context.report_progress(1, 4, "Préparation de la recherche sourcée")
        bridge_capabilities = await self._capabilities_snapshot()
        ephemeral_conversation_id = (
            uuid4() if self._allow_chatgpt_structuring_fallback else None
        )
        research_request = ModelRequest(
            text=_research_prompt(parameters),
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            evidence_pack_hash=request_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=ModelProvider.OPENAI,
            sensitivity=parameters.sensitivity,
            metadata={
                "edition_id": str(parameters.edition_id),
                "tlp": parameters.tlp.value,
                "source_profile_id": parameters.source_profile,
                "collected_at": datetime.now(UTC).isoformat(),
            },
            parameters={"reasoning": {"effort": "high"}},
            conversation=(
                ConversationContext(mode="fresh", id=ephemeral_conversation_id)
                if ephemeral_conversation_id is not None
                else None
            ),
        )
        await context.report_progress(2, 4, "Recherche web en cours")
        research = await self._research_model.research(research_request)
        if not research.output_text:
            raise ModelGatewayError("Research model returned no text")

        await context.report_progress(3, 4, "Structuration et validation des propositions")
        raw_hash = hashlib.sha256(research.output_text.encode()).hexdigest()
        fallback_conversation = None
        if ephemeral_conversation_id is not None:
            if (
                research.conversation is None
                or not research.conversation.verified
                or not research.conversation.external_locator
            ):
                raise ModelGatewayError(
                    "La conversation éphémère de recherche n'a pas été vérifiée."
                )
            fallback_conversation = ConversationContext(
                mode="continue",
                id=ephemeral_conversation_id,
                external_locator=research.conversation.external_locator,
            )
        try:
            structured = await self._structure(
                parameters,
                research.output_text,
                research.metadata.get("visible_citations", []),
                research.run.id,
                raw_hash,
                fallback_conversation=fallback_conversation,
            )
        except Exception:
            await self._archive_ephemeral_conversation(ephemeral_conversation_id)
            raise
        await self._archive_ephemeral_conversation(ephemeral_conversation_id)
        batch = _to_domain_batch(
            parameters,
            request_hash,
            structured.batch,
            research.run.id,
            structured.run_id,
            bridge_capabilities,
        )
        async with self._uow_factory() as uow:
            batches = list(await uow.discovery_batches.list_for_edition(parameters.edition_id))
            _merge_existing_candidates(batch, batches)
            for existing_batch in batches:
                await uow.discovery_batches.save(existing_batch)
            inserted = await uow.discovery_batches.add_if_absent(batch)
            if not inserted:
                existing = await uow.discovery_batches.get_by_request_hash(
                    parameters.edition_id, request_hash
                )
                if existing is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                batch = existing
            await uow.commit()
        if self._after_discovery is not None:
            await self._after_discovery(parameters.edition_id)
        await context.report_progress(4, 4, "Candidats proposés — vérification humaine requise")
        return batch

    async def _structure(
        self,
        parameters: DiscoverEditionParameters,
        research_text: str,
        visible_citations: object,
        research_run_id: UUID,
        research_hash: str,
        *,
        repair_of: NormalizedModelOutput | None = None,
        validation_errors: tuple[dict[str, Any], ...] = (),
        fallback_conversation: ConversationContext | None = None,
    ) -> StructuringResult:
        citations = visible_citations if isinstance(visible_citations, list) else []
        prompt = (
            _repair_prompt(repair_of, validation_errors)
            if repair_of is not None
            else _structuring_prompt(
                research_text,
                citations,
                parameters,
                research_run_id,
                research_hash,
            )
        )
        request = ModelRequest(
            text=prompt,
            prompt_template_id=f"{PROMPT_TEMPLATE_ID}-structure",
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            evidence_pack_hash=research_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.BULK_EXTRACTION,
            provider=ModelProvider.QWEN,
            sensitivity=parameters.sensitivity,
            metadata={
                "research_model_run_id": str(research_run_id),
                "compact_contract": _compact_contract(),
                "compact_contract_version": COMPACT_CONTRACT_VERSION,
                "defer_validation": True,
                "repair": repair_of is not None,
            },
        )
        try:
            execution = await self._structured_model.extract(request, ResearchBatch)
        except ModelGatewayError as exc:
            if not self._allow_chatgpt_structuring_fallback:
                if getattr(exc, "code", None) == "structured_model_unavailable":
                    raise DiscoveryStructuredModelUnavailable(research_run_id) from exc
                raise
            if fallback_conversation is None:
                raise DiscoveryStructuredModelUnavailable(research_run_id) from exc
            fallback = replace(
                request,
                text=_fallback_continuation_prompt(parameters, research_run_id, research_hash),
                provider=ModelProvider.OPENAI,
                conversation=fallback_conversation,
                metadata={
                    **request.metadata,
                    "ephemeral_batch_conversation": True,
                    "research_output_reused_from_conversation": True,
                },
            )
            execution = await self._structured_model.extract(fallback, ResearchBatch)
        if isinstance(execution.structured_output, ResearchBatch):
            return StructuringResult(execution.structured_output, None, (), execution.run.id)
        raw = execution.output_text
        if raw is None:
            raise DiscoveryStructuringError(
                "La structuration n'a produit aucun objet exploitable.",
                run_id=execution.run.id,
                valid_count=0,
                rejected_count=1,
                diagnostic_available=bool(execution.run.output_references),
                parser_stage="empty_output",
                research_model_run_id=research_run_id,
            )
        try:
            normalized = normalize_discovery_output(raw)
        except JsonEnvelopeError as exc:
            if self._output_archive is not None:
                await self._output_archive.record_output_diagnostics(
                    execution.run.id,
                    normalized_reference=None,
                    normalized_sha256=None,
                    parser_stage="json_parse",
                    normalization_version=None,
                    transformations=(),
                    validation_errors=(),
                    json_error_line=exc.line,
                    json_error_column=exc.column,
                )
            self._log_parse_failure(execution, raw, "json_parse", 0, 1)
            raise DiscoveryStructuringError(
                "La sortie de structuration ne contient pas un JSON valide.",
                run_id=execution.run.id,
                valid_count=0,
                rejected_count=1,
                diagnostic_available=bool(execution.run.output_references),
                parser_stage="json_parse",
                research_model_run_id=research_run_id,
            ) from exc
        result, rejected = _validate_partially(normalized.value)
        normalized_reference = None
        if self._output_archive is not None and normalized.normalized_text != raw:
            normalized_reference = await self._output_archive.archive_output(
                normalized.normalized_text.encode(), mime_type="application/json"
            )
        if self._output_archive is not None:
            await self._output_archive.record_output_diagnostics(
                execution.run.id,
                normalized_reference=normalized_reference,
                normalized_sha256=normalized.normalized_sha256,
                parser_stage="completed" if result.topics else "pydantic_validation",
                normalization_version=normalized.version,
                transformations=normalized.transformations,
                validation_errors=rejected,
            )
        if rejected and repair_of is None:
            try:
                repaired = await self._structure(
                    parameters,
                    research_text,
                    citations,
                    research_run_id,
                    research_hash,
                    repair_of=normalized,
                    validation_errors=rejected,
                    fallback_conversation=fallback_conversation,
                )
                if repaired.batch.topics:
                    return repaired
            except ModelGatewayError:
                if not result.topics:
                    raise
                logger.warning(
                    "discovery_repair_failed research_model_run_id=%s correlation_id=%s "
                    "phase=repair valid=%s rejected=%s",
                    research_run_id,
                    get_correlation_id(),
                    len(result.topics),
                    len(rejected),
                )
        if not result.topics:
            self._log_parse_failure(execution, raw, "pydantic_validation", 0, len(rejected))
            raise DiscoveryStructuringError(
                "Aucun topic valide n'a été produit par la structuration.",
                run_id=execution.run.id,
                valid_count=0,
                rejected_count=len(rejected),
                diagnostic_available=bool(execution.run.output_references),
                parser_stage="pydantic_validation",
                research_model_run_id=research_run_id,
            )
        logger.info(
            "discovery_structuring_parsed model_run_id=%s correlation_id=%s raw_sha=%s "
            "normalized_sha=%s raw_chars=%s valid=%s rejected=%s phase=completed",
            execution.run.id,
            get_correlation_id(),
            normalized.raw_sha256[:12],
            normalized.normalized_sha256[:12],
            len(raw),
            len(result.topics),
            len(rejected),
        )
        return StructuringResult(result, normalized, rejected, execution.run.id)

    @staticmethod
    def _log_parse_failure(
        execution: ModelExecution, raw: str, phase: str, valid: int, rejected: int
    ) -> None:
        logger.warning(
            "discovery_structuring_failed model_run_id=%s correlation_id=%s raw_sha=%s "
            "raw_chars=%s phase=%s valid=%s rejected=%s diagnostic=true",
            execution.run.id,
            get_correlation_id(),
            hashlib.sha256(raw.encode()).hexdigest()[:12],
            len(raw),
            phase,
            valid,
            rejected,
        )

    async def retry_structuring(
        self,
        parameters: DiscoverEditionParameters,
        research_run_id: UUID,
        context: JobExecutionContext,
    ) -> DiscoveryBatch:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        run = await self._output_archive.get_run(research_run_id)
        if run is None or not run.output_references:
            raise ModelGatewayError("Archived research ModelRun is unavailable")
        research_text = (
            await self._output_archive.read_output(run.output_references[0], max_bytes=10_000_000)
        ).decode()
        await context.report_progress(1, 2, "Reprise du résultat de recherche archivé")
        structured = await self._structure(
            parameters,
            research_text,
            list(run.visible_citations),
            research_run_id,
            hashlib.sha256(research_text.encode()).hexdigest(),
        )
        await context.report_progress(2, 2, "Structuration relancée sans recherche web")
        batch = _to_domain_batch(
            parameters,
            discovery_request_hash(parameters),
            structured.batch,
            research_run_id,
            structured.run_id,
            await self._capabilities_snapshot(),
        )
        async with self._uow_factory() as uow:
            existing = await uow.discovery_batches.get_by_request_hash(
                parameters.edition_id, batch.request_hash
            )
            if existing is not None:
                return existing
            await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()
        if self._after_discovery is not None:
            await self._after_discovery(parameters.edition_id)
        return batch

    async def _capabilities_snapshot(self) -> dict[str, object]:
        if self._bridge_capabilities_provider is None:
            return dict(self._bridge_capabilities)
        try:
            capabilities = await self._bridge_capabilities_provider.capabilities()
        except Exception as exc:
            return {
                **self._bridge_capabilities,
                "snapshot_available": False,
                "snapshot_error_type": type(exc).__name__,
            }
        return {**capabilities, "snapshot_available": True}

    async def _archive_ephemeral_conversation(self, conversation_id: UUID | None) -> None:
        if conversation_id is None or self._bridge_capabilities_provider is None:
            return
        try:
            await self._bridge_capabilities_provider.archive_conversation(conversation_id)
        except Exception as exc:
            logger.warning(
                "discovery_ephemeral_conversation_archive_failed conversation_id=%s "
                "correlation_id=%s error_type=%s",
                conversation_id,
                get_correlation_id(),
                type(exc).__name__,
            )

    async def list_batches(self, edition_id: UUID) -> list[DiscoveryBatch]:
        async with self._uow_factory() as uow:
            return list(await uow.discovery_batches.list_for_edition(edition_id))

    async def mark_source(
        self,
        edition_id: UUID,
        source_id: UUID,
        status: SourceVerificationStatus,
        *,
        actor_id: str,
    ) -> SourceCandidate:
        async with self._uow_factory() as uow:
            batches = await uow.discovery_batches.list_for_edition(edition_id)
            for batch in batches:
                source = batch.source(source_id)
                if source is not None:
                    source.mark(status, actor_id=actor_id)
                    await uow.discovery_batches.save(batch)
                    await uow.commit()
                    return source
        raise SourceCandidateNotFoundError(str(source_id))


def register_discovery_jobs(registry: JobRegistry, service: DiscoveryService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, DiscoverEditionParameters):
            raise TypeError("Invalid discovery parameters")
        try:
            batch = await service.discover_edition(parameters, context)
        except ModelGatewayError as exc:
            details = None
            if isinstance(exc, DiscoveryStructuringError):
                details = {
                    "phase": exc.phase,
                    "validation_kind": (
                        "json_invalid" if exc.phase == "json_parse" else "pydantic_validation"
                    ),
                    "valid_count": exc.valid_count,
                    "rejected_count": exc.rejected_count,
                    "model_run_id": str(exc.run_id) if exc.run_id else None,
                    "research_model_run_id": (
                        str(exc.research_model_run_id) if exc.research_model_run_id else None
                    ),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": exc.diagnostic_available,
                    "can_retry_structuring": exc.research_model_run_id is not None,
                }
            elif isinstance(exc, DiscoveryStructuredModelUnavailable):
                details = {
                    "phase": exc.phase,
                    "validation_kind": "model_unavailable",
                    "valid_count": 0,
                    "rejected_count": 0,
                    "research_model_run_id": str(exc.research_model_run_id),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": True,
                    "can_retry_structuring": True,
                }
            raise JobHandlerError(
                str(getattr(exc, "code", "discovery_model_failed")),
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(DISCOVERY_JOB_KIND, DiscoverEditionParameters, handler)

    async def retry_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, RetryStructuringParameters):
            raise TypeError("Invalid discovery structuring retry parameters")
        try:
            batch = await service.retry_structuring(
                parameters.discovery, parameters.research_model_run_id, context
            )
        except ModelGatewayError as exc:
            details = {
                "phase": str(getattr(exc, "phase", "structuring")),
                "research_model_run_id": str(parameters.research_model_run_id),
                "correlation_id": get_correlation_id(),
                "diagnostic_available": True,
                "can_retry_structuring": True,
            }
            raise JobHandlerError(
                str(getattr(exc, "code", "discovery_model_failed")),
                str(exc),
                transient=False,
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(RETRY_STRUCTURING_JOB_KIND, RetryStructuringParameters, retry_handler)


def discovery_request_hash(parameters: DiscoverEditionParameters) -> str:
    value = parameters.model_dump(mode="json")
    for key in ("country_aliases", "languages", "keywords", "exclusions"):
        value[key] = sorted(dict.fromkeys(item.strip() for item in value[key] if item.strip()))
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def discovery_idempotency_key(parameters: DiscoverEditionParameters) -> str:
    return f"discover-edition:{parameters.edition_id}:{discovery_request_hash(parameters)}"


def _research_prompt(parameters: DiscoverEditionParameters) -> str:
    collected_at = datetime.now(UTC).date()
    future_limit = (
        "La fin de l'édition est future : ne recherche et n'infère aucun événement postérieur "
        f"au {collected_at.isoformat()}."
        if parameters.period_end > collected_at
        else ""
    )
    return f"""Mission : rechercher les publications CTI significatives concernant
{parameters.country} et ses alias
{", ".join(parameters.country_aliases)}, entre {parameters.period_start.isoformat()} et
{parameters.period_end.isoformat()}, dans les langues {", ".join(parameters.languages)}.
Date réelle de collecte (as_of_date) : {collected_at.isoformat()}. {future_limit}
Profil effectif de sources : {_source_profile_description(parameters.source_profile)}.
Axe complémentaire : {parameters.complementary_axis}.
Mots-clés : {", ".join(parameters.keywords) or "aucun"}. Exclusions :
{", ".join(parameters.exclusions) or "aucune"}.

Priorise les activités APT étatiques ou supposées étatiques et les rapports techniques riches
en IOC, échantillons, configurations, PCAP, règles ou TTP. Pour chaque publication, donne la
source originale, les sources réellement indépendantes, les simples reprises/agrégateurs,
la date de l'événement et de publication, les acteurs, campagnes, malwares, CVE, victimes,
secteurs et pays, la présence probable d'artefacts techniques, les incertitudes et les raisons
de pertinence. Cite explicitement chaque source effectivement utilisée et fournis son URL
HTTP(S) dans la réponse lorsque cette URL est visible. N'invente jamais une URL absente.
La liste peut être incomplète : ne présente aucun ensemble de sources comme exhaustif, ne
déduis jamais qu'une source n'existe pas parce qu'elle n'est pas visible. Ne formule aucune
attribution nouvelle et ne sélectionne aucun sujet pour publication.

Retourne un compte rendu Markdown lisible avec les champs demandés et les URLs visibles ;
ne retourne pas de JSON strict."""


def _source_profile_description(profile_id: str) -> str:
    profiles = {
        "iran-default": (
            "sources CTI primaires, CERT nationaux, chercheurs techniques indépendants, "
            "puis relais et agrégateurs explicitement étiquetés"
        )
    }
    return profiles.get(
        profile_id,
        "sources primaires et institutionnelles, corroborations techniques indépendantes, "
        "puis relais explicitement étiquetés",
    )


def _structuring_prompt(
    raw: str,
    visible_citations: list[object],
    parameters: DiscoverEditionParameters,
    research_run_id: UUID,
    research_hash: str,
) -> str:
    original = {
        "edition_id": str(parameters.edition_id),
        "country": parameters.country,
        "period_start": parameters.period_start.isoformat(),
        "period_end": parameters.period_end.isoformat(),
        "languages": parameters.languages,
        "source_profile_id": parameters.source_profile,
        "complementary_axis": parameters.complementary_axis,
    }
    return (
        "Structure localement le compte rendu en respectant le contrat compact fourni par le "
        "système. N'ajoute aucune source, aucun IOC et aucune attribution. Distingue les sources "
        "primary/independent des relay/aggregator et conserve les incertitudes.\n\n"
        f"ModelRun de recherche : {research_run_id}\n"
        f"SHA-256 recherche : {research_hash}\n"
        f"Paramètres originaux : {json.dumps(original, ensure_ascii=False, sort_keys=True)}\n"
        f"Citations visibles séparées : {json.dumps(visible_citations, ensure_ascii=False)}\n\n"
        "Compte rendu Markdown nettoyé :\n" + raw
    )


def _compact_contract() -> dict[str, object]:
    return {
        "version": COMPACT_CONTRACT_VERSION,
        "required": ["queries", "citations", "topics"],
        "types": {
            "queries": "array<string,0..50>",
            "citations": "array<{label:string,url:https-url,excerpt:string|null},0..500>",
            "topics": "array<topic,0..200>",
            "topic": {
                "required": [
                    "provisional_title",
                    "summary",
                    "novelty",
                    "technical_potential",
                    "event_date",
                    "actors",
                    "campaigns",
                    "malware",
                    "cves",
                    "victims",
                    "sectors",
                    "countries",
                    "iocs",
                    "artifact_availability",
                    "uncertainties",
                    "reasons_for_relevance",
                    "sources",
                ],
                "technical_potential": "integer 0..4",
                "event_date": "YYYY-MM-DD|null",
                "sources": "array<source,1..100>",
            },
            "source": {
                "required": [
                    "url",
                    "title",
                    "publisher",
                    "published_at",
                    "event_date",
                    "source_role",
                    "citation",
                ],
                "source_role": [
                    "primary",
                    "independent",
                    "relay",
                    "aggregator",
                    "social",
                    "unknown",
                ],
            },
            "artifact_availability_values": ["yes", "no", "probable", "unknown"],
        },
        "minimal_example": {
            "queries": [],
            "citations": [],
            "topics": [],
        },
    }


def _repair_prompt(
    previous: NormalizedModelOutput, errors: tuple[dict[str, Any], ...]
) -> str:
    return (
        "Répare une seule fois le JSON ci-dessous en corrigeant uniquement les erreurs listées. "
        "Il est interdit d'ajouter une source, un IOC ou une attribution. Supprime une valeur "
        "irrécupérable plutôt que de l'inventer.\n\n"
        f"Erreurs structurées : {json.dumps(errors, ensure_ascii=False, sort_keys=True)}\n"
        f"JSON précédent : {previous.normalized_text}"
    )


def _fallback_continuation_prompt(
    parameters: DiscoverEditionParameters, research_run_id: UUID, research_hash: str
) -> str:
    return (
        "Continue dans cette conversation et structure le résultat de recherche du tour précédent "
        "selon le contrat compact. Ne recopie pas la recherche et n'ajoute aucune source, aucun "
        "IOC ou attribution. Retourne uniquement l'objet JSON. "
        f"Édition={parameters.edition_id}, ModelRun={research_run_id}, SHA-256={research_hash}."
    )


def _validate_partially(value: dict[str, Any]) -> tuple[ResearchBatch, tuple[dict[str, Any], ...]]:
    rejected: list[dict[str, Any]] = []
    allowed = {"queries", "citations", "topics"}
    for key in value.keys() - allowed:
        rejected.append(_rejection((key,), "extra_forbidden", value[key]))
    queries_value = value.get("queries", [])
    queries: list[str] = []
    if isinstance(queries_value, list):
        for index, query in enumerate(queries_value):
            if isinstance(query, str) and query.strip():
                queries.append(query.strip())
            elif query != "":
                rejected.append(_rejection(("queries", index), "string_type", query))
    else:
        rejected.append(_rejection(("queries",), "list_type", queries_value))

    citations: list[ResearchCitation] = []
    citations_value = value.get("citations", [])
    if isinstance(citations_value, list):
        for index, citation in enumerate(citations_value):
            try:
                citations.append(
                    ResearchCitation.model_validate_json(
                        json.dumps(citation, ensure_ascii=False)
                    )
                )
            except ValidationError as exc:
                rejected.extend(_pydantic_rejections(("citations", index), exc, citation))
    else:
        rejected.append(_rejection(("citations",), "list_type", citations_value))

    topics: list[ResearchTopic] = []
    topics_value = value.get("topics", [])
    if not isinstance(topics_value, list):
        rejected.append(_rejection(("topics",), "list_type", topics_value))
        topics_value = []
    for topic_index, topic_value in enumerate(topics_value):
        if not isinstance(topic_value, dict):
            rejected.append(_rejection(("topics", topic_index), "dict_type", topic_value))
            continue
        sources_value = topic_value.get("sources", [])
        valid_sources: list[ResearchSource] = []
        if isinstance(sources_value, list):
            for source_index, source_value in enumerate(sources_value):
                try:
                    valid_sources.append(
                        ResearchSource.model_validate_json(
                            json.dumps(source_value, ensure_ascii=False)
                        )
                    )
                except ValidationError as exc:
                    rejected.extend(
                        _pydantic_rejections(
                            ("topics", topic_index, "sources", source_index), exc, source_value
                        )
                    )
        else:
            rejected.append(
                _rejection(("topics", topic_index, "sources"), "list_type", sources_value)
            )
        candidate = {
            **topic_value,
            "sources": [item.model_dump(mode="json") for item in valid_sources],
        }
        try:
            topics.append(
                ResearchTopic.model_validate_json(json.dumps(candidate, ensure_ascii=False))
            )
        except ValidationError as exc:
            rejected.extend(_pydantic_rejections(("topics", topic_index), exc, topic_value))
    return (
        ResearchBatch(queries=queries, citations=citations, topics=topics),
        tuple(rejected),
    )


def _pydantic_rejections(
    prefix: tuple[str | int, ...], error: ValidationError, raw: object
) -> list[dict[str, Any]]:
    return [
        _rejection((*prefix, *item["loc"]), str(item["type"]), raw)
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    ]


def _rejection(path: tuple[str | int, ...], code: str, raw: object) -> dict[str, Any]:
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode()
    return {
        "path": [str(part) for part in path],
        "code": code,
        "value_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _to_domain_batch(
    parameters: DiscoverEditionParameters,
    request_hash: str,
    result: ResearchBatch,
    research_run_id: UUID,
    structuring_run_id: UUID,
    bridge_capabilities: Mapping[str, object],
) -> DiscoveryBatch:
    candidates = []
    for topic in result.topics:
        artifacts = tuple(
            name
            for name, availability in topic.artifact_availability.model_dump().items()
            if availability in {"yes", "probable"}
        )
        sources = [
            SourceCandidate(
                url=source.url,
                title=source.title,
                publisher=source.publisher,
                published_at=source.published_at,
                event_date=source.event_date,
                role=source.source_role,
                citation=source.citation,
                tlp=parameters.tlp,
                sensitivity=parameters.sensitivity,
                external_llm_allowed=parameters.external_llm_allowed,
            )
            for source in topic.sources
        ]
        candidates.append(
            CandidateTopic(
                title=topic.provisional_title,
                summary=topic.summary,
                novelty=topic.novelty,
                technical_potential=topic.technical_potential,
                event_date=topic.event_date,
                uncertainties=tuple(topic.uncertainties),
                relevance_reasons=tuple(topic.reasons_for_relevance),
                actors=tuple(topic.actors),
                campaigns=tuple(topic.campaigns),
                malware=tuple(topic.malware),
                cves=tuple(topic.cves),
                victims=tuple(topic.victims),
                sectors=tuple(topic.sectors),
                countries=tuple(topic.countries),
                likely_artifacts=artifacts,
                sources=sources,
                tlp=parameters.tlp,
                sensitivity=parameters.sensitivity,
                external_llm_allowed=parameters.external_llm_allowed,
                iocs=tuple(topic.iocs),
            )
        )
    return DiscoveryBatch(
        edition_id=parameters.edition_id,
        request_hash=request_hash,
        complementary_axis=parameters.complementary_axis,
        queries=tuple(result.queries),
        citations=tuple(citation.model_dump() for citation in result.citations),
        candidates=candidates,
        discovery_model_run_id=research_run_id,
        structuring_model_run_id=structuring_run_id,
        tlp=parameters.tlp,
        sensitivity=parameters.sensitivity,
        external_llm_allowed=parameters.external_llm_allowed,
        source_mode=DiscoverySourceMode.VISIBLE_CITATIONS_ONLY,
        bridge_capabilities=dict(bridge_capabilities),
        citation_count=len(result.citations),
        source_coverage_complete=False,
        source_coverage_incomplete_reason=(
            "Recherche effectuée depuis les citations visibles de ChatGPT ; "
            "la liste native complète des sources consultées n'est pas disponible."
        ),
    )


def _merge_existing_candidates(batch: DiscoveryBatch, existing: list[DiscoveryBatch]) -> None:
    topic_by_title = {
        topic.title_fingerprint: topic for item in existing for topic in item.candidates
    }
    topic_by_source = {
        source.canonical_url: topic
        for item in existing
        for topic in item.candidates
        for source in topic.sources
    }
    fresh: list[CandidateTopic] = []
    for candidate in batch.candidates:
        target = topic_by_title.get(candidate.title_fingerprint)
        if target is None:
            target = next(
                (
                    topic_by_source[source.canonical_url]
                    for source in candidate.sources
                    if source.canonical_url in topic_by_source
                ),
                None,
            )
        if target is None:
            fresh.append(candidate)
            continue
        target.sources = deduplicate_sources([*target.sources, *candidate.sources])
        target.technical_potential = max(target.technical_potential, candidate.technical_potential)
        target.uncertainties = tuple(
            dict.fromkeys((*target.uncertainties, *candidate.uncertainties))
        )
        target.relevance_reasons = tuple(
            dict.fromkeys((*target.relevance_reasons, *candidate.relevance_reasons))
        )
    batch.candidates = fresh
