from __future__ import annotations

# ruff: noqa: RUF001 - The exact French business prompt intentionally uses typographic apostrophes.
import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cti_app.application.discovery_report_parser import (
    PARSER_VERSION,
    ParsedDiscoveryReport,
    ReportParsingError,
    parse_discovery_report,
)
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import (
    BackgroundResponsePendingError,
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
)
from cti_app.domain.model_runs import ModelProvider, ModelRun, ModelRunStatus
from cti_app.logging import get_correlation_id

DISCOVERY_JOB_KIND = "discover_edition"
REPROCESS_DISCOVERY_REPORT_JOB_KIND = "reprocess_discovery_report"
# Compatibility import for callers compiled against the previous name.
RETRY_STRUCTURING_JOB_KIND = REPROCESS_DISCOVERY_REPORT_JOB_KIND
PROMPT_TEMPLATE_ID = "monthly-cti-discovery"
PROMPT_TEMPLATE_VERSION = "4.1"
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
    as_of_date: date = Field(default_factory=date.today)
    languages: list[str] = Field(min_length=1, max_length=10)
    source_profile: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    tlp: TLP
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True
    research_nonce: UUID | None = None

    @field_validator("edition_id", "research_nonce", mode="before")
    @classmethod
    def parse_edition_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) and value else value

    @field_validator("period_start", "period_end", "as_of_date", mode="before")
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

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]: ...


class ModelOutputArchive(Protocol):
    async def get_run(self, run_id: UUID) -> ModelRun | None: ...

    async def read_output(self, reference: str, *, max_bytes: int = ...) -> bytes: ...

    async def archive_output(self, content: bytes, *, mime_type: str) -> str: ...

    async def resume(self, run_id: UUID) -> ModelExecution: ...

    async def adopt_recovery_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        provenance: str,
        actor_id: str,
        source_model_run_id: UUID | None = None,
    ) -> ModelRun: ...

    async def link_recovery_child(self, parent_run_id: UUID, child_run_id: UUID) -> None: ...

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
        background_poll_interval_seconds: float = 5.0,
        background_waiter: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._uow_factory = uow_factory
        self._research_model = research_model
        # Kept as an archive provider for historical ModelRun outputs. Discovery never
        # calls its structured-extraction method.
        self._structured_model = structured_model
        self._bridge_capabilities_provider = bridge_capabilities_provider
        self._after_discovery = after_discovery
        self._allow_chatgpt_structuring_fallback = allow_chatgpt_structuring_fallback
        self._background_poll_interval_seconds = background_poll_interval_seconds
        self._background_waiter = background_waiter
        self._output_archive = (
            cast(ModelOutputArchive, structured_model)
            if all(
                callable(getattr(structured_model, name, None))
                for name in (
                    "get_run",
                    "read_output",
                    "archive_output",
                    "resume",
                    "adopt_recovery_output",
                    "link_recovery_child",
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
        research_run_id = uuid5(NAMESPACE_URL, f"cti-discovery-model-run:{request_hash}")
        fresh_conversation_id = uuid5(NAMESPACE_URL, f"cti-discovery-conversation:{request_hash}")
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
            background=True,
            conversation=ConversationContext(mode="fresh", id=fresh_conversation_id),
            run_id=research_run_id,
        )
        await context.report_progress(2, 4, "ChatGPT recherche et analyse les sources")
        research = await self._research_or_resume(research_request, context)
        if not research.output_text:
            raise ModelGatewayError("Research model returned no text")

        await context.report_progress(3, 4, "Analyse locale du rapport archivé")
        try:
            parsed = parse_discovery_report(
                research.output_text,
                visible_citations=research.metadata.get("visible_citations", []),
                period_start=parameters.period_start,
                period_end=parameters.period_end,
                tlp=parameters.tlp,
                sensitivity=parameters.sensitivity,
                external_llm_allowed=parameters.external_llm_allowed,
                research_model_run_id=research.run.id,
            )
        except ReportParsingError as exc:
            exc.research_model_run_id = research.run.id
            raise
        await self._record_parser_diagnostics(research.run.id, parsed)
        batch = _parsed_to_domain_batch(
            parameters,
            request_hash,
            parsed,
            research.run.id,
            bridge_capabilities,
        )
        async with self._uow_factory() as uow:
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

    async def _research_or_resume(
        self,
        request: ModelRequest,
        context: JobExecutionContext,
    ) -> ModelExecution:
        """Submit once, then durably poll the persisted background ModelRun."""
        if request.run_id is None:
            raise ModelGatewayError("Discovery research requires a stable ModelRun id")
        existing = (
            await self._output_archive.get_run(request.run_id)
            if self._output_archive is not None
            else None
        )
        if existing is not None:
            if existing.status is ModelRunStatus.SUCCEEDED:
                return await self._completed_execution_from_archive(existing)
            if existing.status is ModelRunStatus.WAITING_BACKGROUND:
                return await self._poll_background_research(existing.id, context)
            if existing.status is ModelRunStatus.NEEDS_REVIEW:
                recovered = await self._resume_recovery_child(existing, context)
                if recovered is not None:
                    return recovered
                await self._wait_for_incomplete_review(existing, context)
            if existing.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
                error = ModelGatewayError(existing.error_message or "Research ModelRun failed")
                error.code = existing.error_code or "research_failed"
                raise error

        # RUNNING signifie que l'identité a pu être persistée avant une réponse
        # HTTP incertaine. Le POST idempotent avec le même run id peut alors
        # uniquement rejoindre le run bridge ; il ne produit jamais un second clic.
        execution = await self._research_model.research(request)
        if execution.run.status is ModelRunStatus.WAITING_BACKGROUND:
            return await self._poll_background_research(execution.run.id, context)
        if execution.run.status is ModelRunStatus.SUCCEEDED and not execution.output_text:
            return await self._completed_execution_from_archive(execution.run)
        return execution

    async def _resume_recovery_child(
        self, parent: ModelRun, context: JobExecutionContext
    ) -> ModelExecution | None:
        if self._output_archive is None:
            return None
        raw_child_id = (parent.error_details or {}).get("recovery_child_model_run_id")
        if not isinstance(raw_child_id, str):
            return None
        try:
            child_id = UUID(raw_child_id)
        except ValueError:
            return None
        child = await self._output_archive.get_run(child_id)
        if child is None:
            raise ModelGatewayError("Recovery child ModelRun is unavailable")
        if child.status is ModelRunStatus.WAITING_BACKGROUND:
            execution = await self._poll_background_research(child.id, context)
        elif child.status is ModelRunStatus.SUCCEEDED:
            execution = await self._completed_execution_from_archive(child)
        elif child.status is ModelRunStatus.NEEDS_REVIEW:
            await self._wait_for_incomplete_review(child, context)
        else:
            raise ModelGatewayError(child.error_message or "Recovery child failed")
        if not execution.output_text:
            raise ModelGatewayError("Recovery child produced no final output")
        adopted = await self._output_archive.adopt_recovery_output(
            parent.id,
            execution.output_text.encode(),
            provenance="recovery_continuation",
            actor_id="system:recovery",
            source_model_run_id=child.id,
        )
        return ModelExecution(
            run=adopted,
            output_text=execution.output_text,
            metadata=execution.metadata,
        )

    async def preview_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> dict[str, Any]:
        if self._output_archive is None or self._bridge_capabilities_provider is None:
            raise ModelGatewayError("Recovery infrastructure is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)
        if (
            parent is None
            or not parent.response_id
            or (
                parent.status is not ModelRunStatus.NEEDS_REVIEW
                and not _has_recovery_provenance(parent, "visible_recovery")
            )
        ):
            raise ModelGatewayError("ModelRun is not waiting for recovery")
        recovered = await self._bridge_capabilities_provider.preview_visible_recovery(
            parent.response_id
        )
        text = recovered.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ModelGatewayError("No visible final response is recoverable")
        return {
            **self._preview_report(parameters, parent_run_id, text),
            "report_markdown": text,
        }

    async def preview_manual_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
    ) -> dict[str, Any]:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)
        if parent is None or (
            parent.status is not ModelRunStatus.NEEDS_REVIEW
            and not _has_recovery_provenance(parent, "manual_import")
        ):
            raise ModelGatewayError("ModelRun is not waiting for recovery")
        return self._preview_report(parameters, parent_run_id, text)

    async def adopt_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        *,
        expected_sha256: str,
        actor_id: str,
    ) -> None:
        preview = await self.preview_visible_recovery(parameters, parent_run_id)
        text = preview.get("report_markdown")
        if not isinstance(text, str) or preview["sha256"] != expected_sha256:
            raise ValueError("Recovery preview no longer matches the confirmed report")
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        await self._output_archive.adopt_recovery_output(
            parent_run_id,
            text.encode(),
            provenance="visible_recovery",
            actor_id=actor_id,
        )

    def _preview_report(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
    ) -> dict[str, Any]:
        parsed = parse_discovery_report(
            text,
            visible_citations=[],
            period_start=parameters.period_start,
            period_end=parameters.period_end,
            tlp=parameters.tlp,
            sensitivity=parameters.sensitivity,
            external_llm_allowed=parameters.external_llm_allowed,
            research_model_run_id=parent_run_id,
        )
        iocs = [ioc for candidate in parsed.candidates for ioc in candidate.provisional_iocs]
        counts: dict[str, int] = {}
        for ioc in iocs:
            counts[ioc.proposed_type.value] = counts.get(ioc.proposed_type.value, 0) + 1
        return {
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "subject_count": len(parsed.candidates),
            "publication_count": sum(
                len(candidate.sources) + len(candidate.incomplete_sources)
                for candidate in parsed.candidates
            ),
            "ioc_count": len(iocs),
            "ioc_type_counts": counts,
            "warnings": list(parsed.warnings),
            "subjects": [candidate.title for candidate in parsed.candidates],
        }

    async def adopt_recovery_report(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
        *,
        expected_sha256: str,
        provenance: str,
        actor_id: str,
    ) -> None:
        preview = self._preview_report(parameters, parent_run_id, text)
        if preview["sha256"] != expected_sha256:
            raise ValueError("Recovery preview no longer matches the confirmed report")
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        await self._output_archive.adopt_recovery_output(
            parent_run_id,
            text.encode(),
            provenance=provenance,
            actor_id=actor_id,
        )

    async def start_completion_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> UUID:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)
        details = parent.error_details if parent else None
        if parent is not None and _has_recovery_provenance(parent, "recovery_continuation"):
            recovery = (parent.error_details or {}).get("recovery")
            source_id = recovery.get("source_model_run_id") if isinstance(recovery, dict) else None
            if isinstance(source_id, str):
                return UUID(source_id)
        conversation = details.get("conversation") if isinstance(details, dict) else None
        if (
            parent is None
            or parent.status is not ModelRunStatus.NEEDS_REVIEW
            or not isinstance(conversation, dict)
            or not isinstance(conversation.get("id"), str)
            or not isinstance(conversation.get("external_locator"), str)
        ):
            raise ModelGatewayError("Verified discovery conversation is unavailable")
        child_id = uuid5(NAMESPACE_URL, f"{parent_run_id}:complete-initial-response:v1")
        request = ModelRequest(
            text=(
                "Ta réponse précédente ne contient pas de résultat final. Termine maintenant "
                "la mission initiale et fournis directement le rapport Markdown demandé, sans "
                "recommencer toute la recherche."
            ),
            prompt_template_id="monthly-cti-discovery-recovery",
            prompt_template_version="1.0",
            evidence_pack_hash=parent.evidence_pack_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=parent.provider,
            sensitivity=parameters.sensitivity,
            parameters={
                "bridge_recovery": True,
                "recovery_parent_model_run_id": str(parent_run_id),
            },
            background=True,
            conversation=ConversationContext(
                mode="continue",
                id=UUID(conversation["id"]),
                external_locator=conversation["external_locator"],
            ),
            run_id=child_id,
        )
        child = await self._output_archive.get_run(child_id)
        if child is None:
            await self._research_model.research(request)
        elif child.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
            raise ModelGatewayError(child.error_message or "Recovery child failed")
        await self._output_archive.link_recovery_child(parent_run_id, child_id)
        return child_id

    async def _poll_background_research(
        self,
        model_run_id: UUID,
        context: JobExecutionContext,
    ) -> ModelExecution:
        if self._output_archive is None:
            raise ModelGatewayError("Background research cannot be resumed")
        started = time.monotonic()
        polls = 0
        while True:
            await context.check_cancelled()
            await context.heartbeat()
            current = await self._output_archive.get_run(model_run_id)
            if current is None:
                raise ModelGatewayError(f"Model run {model_run_id} does not exist")
            if current.status is ModelRunStatus.SUCCEEDED:
                return await self._completed_execution_from_archive(current)
            if current.status is ModelRunStatus.NEEDS_REVIEW:
                await self._wait_for_incomplete_review(current, context)
            if current.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
                error = ModelGatewayError(current.error_message or "Research ModelRun failed")
                error.code = current.error_code or "research_failed"
                raise error
            try:
                execution = await self._output_archive.resume(model_run_id)
            except BackgroundResponsePendingError as exc:
                polls += 1
                await self._record_background_observation(
                    context,
                    model_run_id=model_run_id,
                    bridge_run_id=exc.response_id or current.response_id,
                    bridge_state=exc.background_status,
                    polls=polls,
                    elapsed_seconds=time.monotonic() - started,
                    progress=exc.progress or {},
                )
                await self._background_waiter(self._background_poll_interval_seconds)
                continue
            polls += 1
            await self._record_background_observation(
                context,
                model_run_id=model_run_id,
                bridge_run_id=execution.run.response_id or current.response_id,
                bridge_state="completed",
                polls=polls,
                elapsed_seconds=time.monotonic() - started,
                progress={},
            )
            if execution.run.status is not ModelRunStatus.SUCCEEDED:
                if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
                    await self._wait_for_incomplete_review(execution.run, context)
                raise ModelGatewayError("Background research returned a non-terminal result")
            if execution.output_text:
                return execution
            return await self._completed_execution_from_archive(execution.run)

    @staticmethod
    async def _wait_for_incomplete_review(
        run: ModelRun,
        context: JobExecutionContext,
    ) -> NoReturn:
        details = {
            "phase": "chatgpt_incomplete",
            "reason": run.error_code or "no_final_answer",
            "model_run_id": str(run.id),
            "bridge_run_id": run.response_id,
            "correlation_id": get_correlation_id(),
            **(run.error_details or {}),
        }
        await context.wait_for_human(
            "ChatGPT s'est arrêté sans produire de réponse finale. "
            "La conversation a été conservée et peut être reprise.",
            details,
        )

    @staticmethod
    async def _record_background_observation(
        context: JobExecutionContext,
        *,
        model_run_id: UUID,
        bridge_run_id: str | None,
        bridge_state: str,
        polls: int,
        elapsed_seconds: float,
        progress: dict[str, Any],
    ) -> None:
        job_heartbeat_at = datetime.now(UTC).isoformat()
        correlation_id = get_correlation_id()
        await context.record_diagnostics(
            {
                "phase": "background_bridge_wait",
                "model_run_id": str(model_run_id),
                "bridge_run_id": bridge_run_id,
                "last_job_heartbeat": job_heartbeat_at,
                "bridge_state": bridge_state,
                "poll_count": polls,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "correlation_id": correlation_id,
                "chatgpt_phase": progress.get("phase"),
                "chatgpt_output_chars": progress.get("output_chars"),
                "chatgpt_stable_for_ms": progress.get("stable_for_ms"),
                "chatgpt_completion_signal": progress.get("completion_signal"),
            }
        )
        logger.info(
            "discovery_background_poll model_run_id=%s bridge_run_id=%s "
            "job_heartbeat_at=%s bridge_state=%s poll_count=%s elapsed_seconds=%.3f "
            "correlation_id=%s",
            model_run_id,
            bridge_run_id or "pending",
            job_heartbeat_at,
            bridge_state,
            polls,
            elapsed_seconds,
            correlation_id,
        )

    async def _completed_execution_from_archive(self, run: ModelRun) -> ModelExecution:
        if self._output_archive is None or not run.output_references:
            raise ModelGatewayError("Completed research has no archived output")
        reference = run.raw_output_reference or run.output_references[-1]
        output = await self._output_archive.read_output(reference, max_bytes=10_000_000)
        text = output.decode("utf-8")
        if not text.strip():
            raise ModelGatewayError("Completed research output is empty")
        return ModelExecution(
            run=run,
            output_text=text,
            metadata={"visible_citations": list(run.visible_citations)},
        )

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
        retry_nonce: UUID,
        context: JobExecutionContext,
    ) -> DiscoveryBatch:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        run = await self._output_archive.get_run(research_run_id)
        if run is None or not run.output_references:
            raise ModelGatewayError("Archived research ModelRun is unavailable")
        research_text = (
            await self._output_archive.read_output(
                run.raw_output_reference or run.output_references[-1],
                max_bytes=10_000_000,
            )
        ).decode()
        await context.report_progress(1, 2, "Le rapport ChatGPT archivé est réutilisé")
        parsed = parse_discovery_report(
            research_text,
            visible_citations=list(run.visible_citations),
            period_start=parameters.period_start,
            period_end=parameters.period_end,
            tlp=parameters.tlp,
            sensitivity=parameters.sensitivity,
            external_llm_allowed=parameters.external_llm_allowed,
            research_model_run_id=research_run_id,
        )
        await self._record_parser_diagnostics(research_run_id, parsed)
        await context.report_progress(2, 2, "Analyse locale terminée sans appel au bridge")
        reparse_hash = hashlib.sha256(
            f"reparse:{discovery_request_hash(parameters)}:{research_run_id}:{retry_nonce}".encode()
        ).hexdigest()
        batch = _parsed_to_domain_batch(
            parameters,
            reparse_hash,
            parsed,
            research_run_id,
            await self._capabilities_snapshot(),
        )
        async with self._uow_factory() as uow:
            revisions = [
                item
                for item in await uow.discovery_batches.list_for_edition(parameters.edition_id)
                if item.discovery_model_run_id == research_run_id
            ]
            active = next((item for item in reversed(revisions) if item.is_active_revision), None)
            batch.parsing_revision = (
                max((item.parsing_revision for item in revisions), default=0) + 1
            )
            if active is not None:
                batch.supersedes_batch_id = active.id
                active.replaced_by_batch_id = batch.id
            inserted = await uow.discovery_batches.add_if_absent(batch)
            if not inserted:
                raise ModelGatewayError("A parsing revision already exists for this request")
            if active is not None:
                await uow.discovery_batches.save(active)
            await uow.commit()
        if self._after_discovery is not None:
            await self._after_discovery(parameters.edition_id)
        return batch

    async def read_archived_report(self, edition_id: UUID, research_run_id: UUID) -> str:
        if self._output_archive is None:
            raise ReportParsingError("report_unavailable", "Archive de rapports indisponible.")
        batches = await self.list_batches(edition_id, include_replaced=True)
        if not any(batch.discovery_model_run_id == research_run_id for batch in batches):
            raise ReportParsingError("report_unavailable", "Rapport archivé introuvable.")
        run = await self._output_archive.get_run(research_run_id)
        if run is None or not run.output_references:
            raise ReportParsingError("report_unavailable", "Rapport archivé introuvable.")
        content = await self._output_archive.read_output(
            run.raw_output_reference or run.output_references[-1], max_bytes=10_000_000
        )
        if not content:
            raise ReportParsingError("report_empty", "Le rapport archivé est vide.")
        return content.decode(errors="replace")

    async def _record_parser_diagnostics(self, run_id: UUID, parsed: ParsedDiscoveryReport) -> None:
        if self._output_archive is None:
            return
        validation_errors = tuple(
            {
                "path": ["report"],
                "code": warning.split(":", 1)[0][:128],
                "value_sha256": hashlib.sha256(warning.encode()).hexdigest(),
            }
            for warning in parsed.warnings
        )
        await self._output_archive.record_output_diagnostics(
            run_id,
            normalized_reference=None,
            normalized_sha256=parsed.report_sha256,
            parser_stage=("report_parsing_partial" if parsed.status == "partial" else "completed"),
            normalization_version=PARSER_VERSION,
            transformations=("deterministic_markdown_parsing",),
            validation_errors=validation_errors,
        )

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

    async def list_batches(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryBatch]:
        async with self._uow_factory() as uow:
            batches = list(await uow.discovery_batches.list_for_edition(edition_id))
            return (
                batches
                if include_replaced
                else [item for item in batches if item.is_active_revision]
            )

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
        except (ModelGatewayError, ReportParsingError) as exc:
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
            elif isinstance(exc, ReportParsingError):
                details = {
                    "phase": "local_parsing",
                    "research_model_run_id": (
                        str(exc.research_model_run_id)
                        if exc.research_model_run_id is not None
                        else None
                    ),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": exc.research_model_run_id is not None,
                    "can_retry_structuring": exc.research_model_run_id is not None,
                }
            error_code = str(getattr(exc, "code", "research_failed"))
            if error_code == "bridge_unreachable":
                error_code = "bridge_unavailable"
            raise JobHandlerError(
                error_code,
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(
        DISCOVERY_JOB_KIND,
        DiscoverEditionParameters,
        handler,
        resume_after_worker_loss=True,
    )

    async def retry_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, RetryStructuringParameters):
            raise TypeError("Invalid discovery structuring retry parameters")
        try:
            batch = await service.retry_structuring(
                parameters.discovery,
                parameters.research_model_run_id,
                parameters.retry_nonce,
                context,
            )
        except (ModelGatewayError, ReportParsingError) as exc:
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
        cleaned = [item.strip() for item in value[key] if item.strip()]
        value[key] = (
            sorted({item.casefold() for item in cleaned})
            if key in {"country_aliases", "languages"}
            else sorted(dict.fromkeys(cleaned))
        )
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def discovery_idempotency_key(parameters: DiscoverEditionParameters) -> str:
    return f"discover-edition:{parameters.edition_id}:{discovery_request_hash(parameters)}"


def _research_prompt(parameters: DiscoverEditionParameters) -> str:
    aliases: list[str] = []
    seen_aliases = {parameters.country.strip().casefold()}
    for value in parameters.country_aliases:
        alias = value.strip()
        fingerprint = alias.casefold()
        if alias and fingerprint not in seen_aliases:
            aliases.append(alias)
            seen_aliases.add(fingerprint)
    formatted_aliases = f" (alias : {', '.join(aliases)})" if aliases else ""
    languages: list[str] = []
    seen_languages: set[str] = set()
    for value in parameters.languages:
        language = value.strip()
        fingerprint = language.casefold()
        if language and fingerprint not in seen_languages:
            languages.append(language)
            seen_languages.add(fingerprint)
    observable_end = min(parameters.period_end, parameters.as_of_date)
    return f"""Mission : rechercher les publications CTI significatives concernant
{parameters.country}{formatted_aliases}.

Date de recherche : {parameters.as_of_date.isoformat()}
Période demandée : {parameters.period_start.isoformat()} au {parameters.period_end.isoformat()}
Période observable : {parameters.period_start.isoformat()} au {observable_end.isoformat()}
Langues : {", ".join(languages)}
Axe complémentaire : {parameters.complementary_axis}

Ne recherche pas de publication postérieure à la date de recherche.

Priorise les activités APT étatiques ou supposées étatiques et les publications
techniques comportant des IOC, des échantillons, des configurations, une chaîne
d’infection, des outils, des TTP ou des règles de détection.

Propose tous les sujets significatifs retrouvés. Il n’existe aucune limite ni
quota de sujets, de brèves ou d’articles approfondis. La sélection finale sera
effectuée par un analyste humain.

Regroupe dans un même SUBJECT les publications décrivant manifestement la même
campagne, le même incident ou la même recherche.

Une synthèse mensuelle ou trimestrielle peut être liée à plusieurs SUBJECT.
Ne fusionne pas des campagnes différentes uniquement parce qu’elles sont
mentionnées dans la même synthèse.

Chaque SUBJECT doit normalement comporter au moins une publication dans la
période observable. Les publications antérieures peuvent être ajoutées comme
rapport original, analyse indépendante ou contexte technique.

Limite cette phase à la sélection éditoriale. N’effectue pas encore l’analyse
exhaustive de la chaîne d’infection, des TTP, des outils ou de la victimologie.

Pour les IOC :

- signale uniquement les IOC explicitement visibles dans les pages consultées ;
- reproduis leurs valeurs exactes sans les corriger ni les compléter ;
- indique leur type lorsqu’il est identifiable ;
- distingue un total annoncé par l’éditeur des valeurs effectivement visibles ;
- n’estime jamais un nombre d’IOC ;
- utilise `unknown` si tu ne peux pas déterminer l’information ;
- utilise `none` seulement si la publication indique clairement qu’aucun IOC
  n’est fourni ou si son contenu visible permet de l’établir ;
- une URL normale de publication ou de navigation n’est pas un IOC ;
- un domaine d’éditeur ou de CDN n’est pas un IOC sauf s’il est explicitement
  présenté comme tel dans la source.

N’invente aucune URL, date, attribution, disponibilité d’artefact ou valeur
d’IOC.

Retourne uniquement du Markdown, sans bloc de code et sans texte avant le titre.
N’échappe pas les tirets des noms de champs.
N’insère pas de citation Markdown dans les champs de description.
Toutes les URL de référence doivent apparaître dans un bloc PUBLICATION.

# SUJETS CANDIDATS

## SUBJECT S1

title: <intitulé proposé>
presentation: <deux phrases neutres maximum>
actor-campaign: <acteur ou campagne explicitement rapporté, sinon unknown>
technical-potential: <entier de 0 à 4>
technical-reason: <raison en une phrase>
artifacts: <liste parmi ioc, samples, configurations, pcap, yara, suricata, none, unknown>
uncertainty: <une ou deux incertitudes courtes>

### PUBLICATION P1

title: <titre exact>
url: <URL HTTP(S) exacte>
publisher: <éditeur ou unknown>
published-at: <YYYY-MM-DD ou unknown>
role: <primary, independent, relay, aggregator ou unknown>
ioc-visibility: <none, declared, visible ou unknown>
visible-ioc-types: <liste des types visibles ou none/unknown>
visible-iocs: <jusqu’à 10 valeurs exactes explicitement visibles ou none/unknown>
publisher-ioc-count: <entier explicitement annoncé ou unknown>
ioc-note: <une phrase courte ou none>

### PUBLICATION P2

...

## SUBJECT S2

...

# LIMITES

<limites principales de la recherche et de l’accès aux sources>"""


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


def _has_recovery_provenance(run: ModelRun, provenance: str) -> bool:
    recovery = (run.error_details or {}).get("recovery")
    return (
        run.status is ModelRunStatus.SUCCEEDED
        and isinstance(recovery, dict)
        and recovery.get("provenance") == provenance
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


def _repair_prompt(previous: NormalizedModelOutput, errors: tuple[dict[str, Any], ...]) -> str:
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
                    ResearchCitation.model_validate_json(json.dumps(citation, ensure_ascii=False))
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


def _parsed_to_domain_batch(
    parameters: DiscoverEditionParameters,
    request_hash: str,
    result: ParsedDiscoveryReport,
    research_run_id: UUID,
    bridge_capabilities: Mapping[str, object],
) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=parameters.edition_id,
        request_hash=request_hash,
        complementary_axis=parameters.complementary_axis,
        queries=(),
        citations=result.citations,
        candidates=result.candidates,
        discovery_model_run_id=research_run_id,
        # The historical column remains non-null for backwards compatibility. Reusing
        # the research run ID explicitly means that no structuring ModelRun was created.
        structuring_model_run_id=research_run_id,
        tlp=parameters.tlp,
        sensitivity=parameters.sensitivity,
        external_llm_allowed=parameters.external_llm_allowed,
        report_sha256=result.report_sha256,
        parser_version=PARSER_VERSION,
        parsing_status=("report_parsing_partial" if result.status == "partial" else "completed"),
        parsing_warnings=result.warnings,
        unattached_visible_citations=result.unattached_visible_citations,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
        bridge_capabilities=dict(bridge_capabilities),
        citation_count=len(result.citations),
        source_coverage_complete=False,
        source_coverage_incomplete_reason=(
            "Le rapport Markdown et les citations visibles ne constituent pas une liste "
            "exhaustive des sources consultées."
        ),
    )
