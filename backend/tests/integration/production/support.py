"""Business-test support for the complete Production pipeline.

The doubles in this module live only at the two external boundaries used by
Production: HTTP collection and the model provider.  All persistence and
workflow objects below are the application implementations.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.http_collection import (
    CollectionPolicy,
    HttpTransport,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.jobs import (
    JobDispatcher,
    JobExecutor,
    JobRegistry,
    JobService,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationResult,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRole,
    ModelRouter,
    ModelRoutingHint,
    ModelUsage,
    SafeModelRequest,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageChain,
    ProductionStageParameters,
    production_stage_idempotency_key,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.subject_production import EditionProductionService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.entities import Subject
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelProvider, ModelRun
from cti_app.domain.production import SubjectProductionRun, SubjectProductionStage
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


class DeterministicSourceTransport(HttpTransport):
    """HTTP transport fake; SafeHttpCollector still owns all collection rules."""

    def __init__(self, sources: Mapping[str, Mapping[str, object]]) -> None:
        self._sources = dict(sources)
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        source = self._sources.get(request.url)
        if source is None:
            raise AssertionError(f"Unscripted HTTP request: {request.url}")
        body = source["body"]
        encoded_body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        status = source.get("status", 200)
        if not isinstance(status, int):
            raise AssertionError(f"Invalid scripted HTTP status for {request.url}")
        return RawHttpResponse(
            status=status,
            headers={"content-type": str(source.get("mime", "text/html"))},
            encoded_body=encoded_body,
        )


class DeterministicDnsResolver:
    """DNS fake used by SafeHttpCollector's two-answer pinning check."""

    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        self.hosts.append(hostname)
        return ("93.184.216.34",)


class _CatalogModelOutputStore:
    """ModelOutputStore backed by the real canonical blob catalog."""

    def __init__(self, catalog: BlobCatalogService) -> None:
        self._catalog = catalog

    async def store(self, content: bytes, *, mime_type: str) -> str:
        record = await self._catalog.ingest(
            BytesIO(content), logical_bucket="model-outputs", mime_type=mime_type
        )
        return f"blob://{record.id}"

    async def read(self, reference: str, *, max_bytes: int) -> bytes:
        try:
            blob_id = UUID(reference.removeprefix("blob://"))
        except ValueError as exc:
            raise ValueError(f"Invalid model output reference: {reference}") from exc
        return await self._catalog.read(blob_id, max_bytes=max_bytes)


@dataclass(frozen=True, slots=True)
class ScriptedModelCall:
    stage: str
    source_url: str | None
    source_urls: tuple[str, ...]
    web_search: bool
    conversation_id: UUID | None
    prompt_version: str
    model_run_id: UUID | None
    request: ModelRequest


class ScriptedModelScript:
    """Functional scenario routing for the fake model boundary."""

    def __init__(self) -> None:
        self._references: str | None = None
        self._synthesis: str | None = None
        self._q2: dict[tuple[str, str], str | Exception] = {}

    def references(self, response: str) -> None:
        self._references = response

    def q2(
        self,
        *,
        source_url: str,
        access_mode: str,
        response: str | Exception,
    ) -> None:
        self._q2[(source_url, access_mode)] = response

    def synthesis(self, response: str) -> None:
        self._synthesis = response

    def response_for(self, request: SafeModelRequest) -> str | Exception:
        if request.prompt_template_id in {
            "production-q2-url",
            "production-q2-url-archive-fallback",
        }:
            source_url = request.metadata.get("source_url")
            if not isinstance(source_url, str):
                raise AssertionError("Q2 request has no source_url metadata")
            access_mode = request.metadata.get("access_mode", "live_url")
            if not isinstance(access_mode, str):
                raise AssertionError("Q2 request has an invalid access_mode metadata")
            return self._q2_response(source_url, access_mode)

        if request.prompt_template_id == "production-q2-ioc-batch":
            source_urls = request.metadata.get("batch_source_urls")
            if not isinstance(source_urls, list) or not all(
                isinstance(url, str) for url in source_urls
            ):
                raise AssertionError("Q2 batch request has no source URL metadata")
            batch_sources = request.parameters.get("q2_batch_sources")
            if not isinstance(batch_sources, list):
                raise AssertionError("Q2 batch request has no B# mapping")
            blocks = []
            for index, source_url in enumerate(source_urls, start=1):
                response = self._q2_response(source_url, "live_url")
                if isinstance(response, Exception):
                    raise response
                blocks.append(f"@@Q2:B{index}@@\n{response}")
            return "\n\n".join(blocks)

        if request.prompt_template_id == "analyst-conversation":
            if request.routing_hint is ModelRoutingHint.WEB_RESEARCH:
                if self._references is None:
                    raise AssertionError("No scripted references response")
                return self._references
            if request.routing_hint is ModelRoutingHint.STANDARD_DRAFT:
                if self._synthesis is None:
                    raise AssertionError("No scripted synthesis response")
                return self._synthesis

        raise AssertionError(
            "No scripted model response for "
            f"{request.prompt_template_id}/{request.routing_hint.value}"
        )

    def _q2_response(self, source_url: str, access_mode: str) -> str | Exception:
        try:
            return self._q2[(source_url, access_mode)]
        except KeyError as exc:
            raise AssertionError(f"No scripted Q2 response for {source_url}") from exc


class _ScriptedModelAdapter:
    provider = ModelProvider.FAKE
    requested_model = "scripted-production-model"
    is_external = False

    def __init__(self, script: ScriptedModelScript) -> None:
        self._script = script

    async def invoke(
        self,
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        output_text = self._script.response_for(request)
        if isinstance(output_text, Exception):
            raise output_text
        conversation = request.conversation
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            response_id=f"scripted-response-{request.request_id or uuid4()}",
            output_text=output_text,
            conversation=(
                ConversationResult(
                    id=str(conversation.id),
                    mode=conversation.mode,
                    external_locator=f"scripted://conversation/{conversation.id}",
                    turn_id=f"scripted-turn-{request.request_id or uuid4()}",
                    verified=True,
                )
                if conversation is not None
                else None
            ),
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise ModelGatewayError("Scripted adapter does not support background responses")


class ScriptedModelGateway(ModelGateway):
    """Real ModelGateway persistence with a scripted provider adapter."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        output_store: _CatalogModelOutputStore,
        diagnostics: DiagnosticsLog | None = None,
    ) -> None:
        self.script = ScriptedModelScript()
        self.calls: list[ScriptedModelCall] = []
        adapter = _ScriptedModelAdapter(self.script)
        router = ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            openai_drafting=adapter,
            openai_critic=adapter,
            qwen=adapter,
            fake=adapter,
        )
        super().__init__(router, uow_factory, output_store, diagnostics=diagnostics)

    async def execute(self, request: ModelRequest, role: ModelRole) -> ModelExecution:
        source_urls = _request_source_urls(request)
        if request.prompt_template_id.startswith("production-q2"):
            stage = "extraction"
        elif request.routing_hint is ModelRoutingHint.WEB_RESEARCH:
            stage = "references"
        elif request.routing_hint is ModelRoutingHint.STANDARD_DRAFT:
            stage = "synthesis"
        else:
            stage = request.routing_hint.value
        self.calls.append(
            ScriptedModelCall(
                stage=stage,
                source_url=source_urls[0] if len(source_urls) == 1 else None,
                source_urls=source_urls,
                web_search=request.web_search,
                conversation_id=request.conversation.id if request.conversation else None,
                prompt_version=request.prompt_template_version,
                model_run_id=request.run_id,
                request=request,
            )
        )
        return await super().execute(request, role)


def _request_source_urls(request: ModelRequest) -> tuple[str, ...]:
    source_url = request.metadata.get("source_url")
    if isinstance(source_url, str):
        return (source_url,)
    source_urls = request.metadata.get("batch_source_urls")
    if isinstance(source_urls, list) and all(isinstance(url, str) for url in source_urls):
        return tuple(source_urls)
    return ()


class DeterministicProductionJobRunner(JobDispatcher):
    """FIFO dispatcher backed by the real JobExecutor and registered handlers."""

    def __init__(self, uow_factory: UnitOfWorkFactory, registry: JobRegistry) -> None:
        self._pending: deque[UUID] = deque()
        self.enqueued: list[UUID] = []
        self._executor = JobExecutor(
            uow_factory,
            registry,
            retry_base_seconds=0,
            retry_max_seconds=0,
            heartbeat_interval_seconds=3600,
        )

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self._pending.append(job_id)
        self.enqueued.append(job_id)

    async def run_until_idle(self) -> None:
        while self._pending:
            await self.run_next()

    async def run_next(self) -> bool:
        """Execute exactly one queued job through the real JobExecutor."""
        if not self._pending:
            return False
        job = await self._executor.execute(self._pending.popleft(), allow_early_retry=True)
        if job.status is JobStatus.QUEUED and job.next_retry_at is not None:
            self._pending.append(job.id)
        return True


@dataclass
class ProductionScenario:
    """Compact fixture builder for a selected article and its live pipeline."""

    uow_factory: UnitOfWorkFactory
    blob_root: Path
    sources: Mapping[str, Mapping[str, object]]
    edition: Edition = field(init=False)
    subject: Subject = field(init=False)
    discovery_batch: DiscoveryBatch = field(init=False)
    editorial_group: EditorialGroup = field(init=False)
    source_candidates: tuple[SourceCandidate, ...] = field(init=False)
    artifact_store: ProductionArtifactStore = field(init=False)
    model_output_store: _CatalogModelOutputStore = field(init=False)
    model: ScriptedModelGateway = field(init=False)
    diagnostics: DiagnosticsLog = field(init=False)
    model_service: ModelConversationService = field(init=False)
    collection_transport: DeterministicSourceTransport = field(init=False)
    collection_service: SubjectCollectionService = field(init=False)
    jobs: JobService = field(init=False)
    runner: DeterministicProductionJobRunner = field(init=False)
    batch_id: UUID | None = field(default=None, init=False)
    run_id: UUID | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        canonical_sources = {
            _canonical_url(url): dict(spec) for url, spec in self.sources.items()
        }
        self.sources = canonical_sources
        edition_token = uuid4().int
        country_code = "".join(
            chr(65 + (edition_token // (26**offset)) % 26) for offset in (0, 1)
        )
        self.edition = Edition(
            country=f"Business Test {country_code}",
            country_code=country_code,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            tlp=TLP.AMBER,
            languages=("fr",),
            target_articles=1,
            source_profile="business-test",
            status=EditionStatus.SELECTION,
        )
        self.subject = Subject(
            external_id=f"BUSINESS-{uuid4().hex}",
            slug=f"production-{uuid4().hex[:12]}",
            tlp=TLP.AMBER,
        )
        source_candidates = []
        for index, url in enumerate(canonical_sources, start=1):
            source_candidates.append(
                SourceCandidate(
                    url=url,
                    title=str(canonical_sources[url].get("title", f"Example source {index}")),
                    publisher=str(
                        canonical_sources[url].get("publisher", f"Example publisher {index}")
                    ),
                    role=SourceRole.PRIMARY if index == 1 else SourceRole.INDEPENDENT,
                    published_at=date(2026, 8, min(10 + index, 28)),
                    tlp=TLP.AMBER,
                    sensitivity="public",
                    external_llm_allowed=True,
                )
            )
        self.source_candidates = tuple(source_candidates)
        candidate = CandidateTopic(
            title="ExampleRAT campaign activity",
            summary="A selected CTI subject backed by two core publications.",
            novelty="The reports expose a shared execution pattern.",
            technical_potential=4,
            uncertainties=(),
            relevance_reasons=("technical depth",),
            actors=("Example actor",),
            campaigns=("Example campaign",),
            malware=("ExampleRAT",),
            cves=(),
            victims=("industrial organizations",),
            sectors=("industry",),
            countries=("France",),
            likely_artifacts=("domain",),
            sources=list(self.source_candidates),
            tlp=TLP.AMBER,
            sensitivity="public",
            external_llm_allowed=True,
            actor_or_campaign="Example actor",
            event_date=date(2026, 8, 15),
        )
        discovery_model_run_id = uuid4()
        self.discovery_batch = DiscoveryBatch(
            edition_id=self.edition.id,
            request_hash=sha256(self.subject.id.bytes).hexdigest(),
            complementary_axis="business pipeline",
            queries=("ExampleRAT",),
            citations=(),
            discovery_model_run_id=discovery_model_run_id,
            tlp=TLP.AMBER,
            sensitivity="public",
            external_llm_allowed=True,
            parser_version="business-test",
            candidates=[candidate],
            source_mode=DiscoverySourceMode.NATIVE_COMPLETE,
            source_coverage_complete=True,
            source_coverage_incomplete_reason=None,
        )
        self.editorial_group = EditorialGroup(
            edition_id=self.edition.id,
            title=candidate.title,
            candidate_references=(CandidateReference(self.discovery_batch.id, candidate.id),),
            outcome=GroupingOutcome.NEW_SUBJECT,
            score=EditorialScore(
                impact=4,
                novelty=4,
                technical_depth=4,
                hunting_potential=4,
                actionability=4,
                source_quality=4,
                justifications={"business-test": "cross-stage coverage"},
            ),
            source_relationship_status=SourceRelationshipStatus.VERIFIED,
            needs_source_verification=False,
            needs_source_expansion=False,
            grouping_confidence=GroupingConfidence.HIGH,
            grouping_justification="The selected subject is represented by the two core sources.",
        )
        self.editorial_group.select(self.subject.id)

        blob_store = FilesystemBlobStore(self.blob_root)
        catalog = BlobCatalogService(blob_store, self.uow_factory)
        self.artifact_store = ProductionArtifactStore(catalog)
        self.model_output_store = _CatalogModelOutputStore(catalog)
        self.diagnostics = DiagnosticsLog.from_env(self.blob_root.parent / "diagnostics")
        self.model = ScriptedModelGateway(
            self.uow_factory,
            self.model_output_store,
            diagnostics=self.diagnostics,
        )
        self.model_service = ModelConversationService(
            self.uow_factory,
            self.model,
            blob_store,
        )
        allowed_domains = frozenset(
            urlsplit(url).hostname or "" for url in canonical_sources
        )
        policy = CollectionPolicy(allowed_domains=allowed_domains)
        self.collection_transport = DeterministicSourceTransport(canonical_sources)
        collector = SafeHttpCollector(
            self.collection_transport,
            DeterministicDnsResolver(),
            policy,
        )
        self.collection_service = SubjectCollectionService(
            self.uow_factory,
            collector,
            blob_store,
        )

        registry = JobRegistry()
        self.jobs = JobService(self.uow_factory, registry)
        self.runner = DeterministicProductionJobRunner(self.uow_factory, registry)
        chain = ProductionStageChain(ProductionPacingPolicy.zero())
        chain.bind(self.jobs, self.runner)
        register_production_jobs(
            registry,
            self.uow_factory,
            chain=chain,
            model_service=self.model_service,
            model_gateway=self.model,
            collection_service=self.collection_service,
            artifact_store=self.artifact_store,
            diagnostics=self.diagnostics,
            pacing=ProductionPacingPolicy.zero(),
        )

    def restrict_core_sources(self, canonical_urls: Sequence[str]) -> None:
        """Keep only selected discovery sources in the frozen core input.

        The remaining scripted URLs stay available to Q1 as supplemental
        reference sources, which lets business tests exercise the real
        supporting-source/IOC_RULES route.
        """
        allowed = {_canonical_url(url) for url in canonical_urls}
        selected = tuple(
            source for source in self.source_candidates if source.canonical_url in allowed
        )
        if not selected or len(selected) != len(allowed):
            raise ValueError("restrict_core_sources must select known non-empty sources")
        self.source_candidates = selected
        if len(self.discovery_batch.candidates) != 1:
            raise ValueError("ProductionScenario expects one discovery candidate")
        self.discovery_batch.candidates[0].sources = list(selected)

    async def seed(self) -> None:
        discovery_run = ModelRun(
            id=self.discovery_batch.discovery_model_run_id,
            provider=ModelProvider.FAKE,
            model_role=ModelRole.RESEARCH,
            requested_model="seeded-discovery",
            actual_model_version="seeded-discovery",
            prompt_template_id="business-test-discovery",
            prompt_template_version="1",
            authorized_input_hash=sha256(b"business-test-discovery").hexdigest(),
            evidence_pack_hash=sha256(self.discovery_batch.id.bytes).hexdigest(),
            parameters={},
        )
        discovery_output = await self.model_output_store.store(
            b"seeded discovery result", mime_type="text/plain; charset=utf-8"
        )
        discovery_run.succeed(
            actual_model_version="seeded-discovery",
            duration_ms=0,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_references=(discovery_output,),
            response_id=f"seeded-discovery-response-{self.discovery_batch.id}",
        )
        async with self.uow_factory() as uow:
            await uow.editions.add_if_absent(self.edition)
            await uow.subjects.add(self.subject)
            await uow.model_runs.add(discovery_run)
            assert await uow.discovery_batches.add_if_absent(self.discovery_batch)
            await uow.editorial_groups.add(self.editorial_group)
            await uow.commit()

    async def start(self) -> SubjectProductionRun:
        await self.seed()
        batch = await EditionProductionService(
            self.uow_factory, ProductionPacingPolicy.zero()
        ).create_batch(
            self.edition.id,
            [self.subject.id],
            actor_id="business-test",
            correlation_id="business-test",
        )
        self.batch_id = batch.id
        first = await EditionProductionService(
            self.uow_factory, ProductionPacingPolicy.zero()
        ).start_next(batch.id)
        assert first is not None
        self.run_id = first.id
        parameters = ProductionStageParameters(
            run_id=first.id,
            expected_stage=SubjectProductionStage.SOURCES.value,
            pipeline_generation=first.pipeline_generation,
        )
        job = await self.jobs.submit(
            kind=stage_job_kind(SubjectProductionStage.SOURCES),
            aggregate_type="subject",
            aggregate_id=first.subject_id,
            idempotency_key=production_stage_idempotency_key(
                first, SubjectProductionStage.SOURCES
            ),
            correlation_id="business-test",
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
            actor_id="business-test",
        )
        await self.runner.dispatch(job.id)
        return first

    async def run_until_terminal(self) -> SubjectProductionRun:
        await self.runner.run_until_idle()
        assert self.run_id is not None
        async with self.uow_factory() as uow:
            run = await uow.subject_production_runs.get(self.run_id)
        assert run is not None
        return run


def _canonical_url(url: str) -> str:
    from cti_app.domain.discovery import canonicalize_http_url

    return canonicalize_http_url(url)


__all__ = [
    "DeterministicProductionJobRunner",
    "DeterministicSourceTransport",
    "ProductionScenario",
    "ScriptedModelCall",
    "ScriptedModelGateway",
]
