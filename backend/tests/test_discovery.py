from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cti_app.application.discovery import (
    DISCOVERY_JOB_KIND,
    ArtifactAvailability,
    DiscoverEditionParameters,
    DiscoveryService,
    ResearchBatch,
    ResearchCitation,
    ResearchSource,
    ResearchTopic,
    _research_prompt,
    discovery_idempotency_key,
)
from cti_app.application.jobs import (
    DuplicateJobError,
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    DiscoverySourceMode,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelProvider
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


class FakeBridgeCapabilities:
    async def capabilities(self) -> dict[str, object]:
        return {
            "web_search": True,
            "native_sources": False,
            "visible_citations": True,
        }

    async def archive_conversation(self, conversation_id: UUID) -> None:
        del conversation_id


def research_fixture() -> ResearchBatch:
    artifacts = ArtifactAvailability(
        ioc="yes", samples="probable", configurations="yes", pcap="unknown", rules="yes"
    )
    primary = ResearchSource(
        url="https://vendor.example/reports/muddywater?utm_source=feed",
        title="MuddyWater technical report",
        publisher="Vendor Research",
        published_at=date(2026, 7, 10),
        event_date=date(2026, 7, 2),
        source_role=SourceRole.PRIMARY,
        citation="Rapport original cité par la recherche.",
    )
    relay = ResearchSource(
        url="https://relay.example/news/muddywater",
        title="A new MuddyWater campaign",
        publisher="Security News",
        published_at=date(2026, 7, 11),
        event_date=date(2026, 7, 2),
        source_role=SourceRole.RELAY,
        citation="Reprise du rapport original.",
    )
    duplicate_primary = ResearchSource(
        url="https://vendor.example/reports/muddywater",
        title="MuddyWater technical report (mirror title)",
        publisher="Vendor Research",
        published_at=date(2026, 7, 10),
        event_date=date(2026, 7, 2),
        source_role=SourceRole.PRIMARY,
        citation="Même URL sans paramètre de suivi.",
    )
    independent = ResearchSource(
        url="https://cert.example/advisories/42",
        title="CERT advisory on the campaign",
        publisher="National CERT",
        published_at=date(2026, 7, 12),
        event_date=date(2026, 7, 3),
        source_role=SourceRole.INDEPENDENT,
        citation="Observation indépendante.",
    )

    def topic(sources: list[ResearchSource], uncertainties: list[str]) -> ResearchTopic:
        return ResearchTopic(
            provisional_title="MuddyWater déploie une nouvelle chaîne d'infection",
            summary=(
                "Une campagne visant plusieurs secteurs iraniens expose une chaîne technique."
            ),
            novelty="Nouvelle configuration et nouvelles TTP documentées.",
            technical_potential=4,
            event_date=date(2026, 7, 2),
            actors=["MuddyWater"],
            campaigns=["Example campaign"],
            malware=["ExampleRAT"],
            cves=["CVE-2026-0001"],
            victims=["organisations publiques"],
            sectors=["gouvernement"],
            countries=["Iran"],
            artifact_availability=artifacts,
            reasons_for_relevance=["Rapport technique original"],
            uncertainties=uncertainties,
            sources=sources,
        )

    return ResearchBatch(
        queries=["Iran APT July 2026 technical report"],
        citations=[
            ResearchCitation(
                label="Vendor report",
                url="https://vendor.example/reports/muddywater",
                excerpt="Technical report with indicators.",
            )
        ],
        topics=[
            topic([primary, relay], ["Attribution reprise de la source, non vérifiée."]),
            topic([duplicate_primary, independent], ["Victimologie encore incomplète."]),
        ],
    )


def research_markdown_fixture() -> str:
    return """# SUJETS CANDIDATS

## SUBJECT S1
title: MuddyWater déploie une nouvelle chaîne d'infection
presentation: Une campagne expose une chaîne technique documentée.
actor_or_campaign: MuddyWater
technical_potential: 4
technical_potential_reason: Configurations et règles sont annoncées.
artifacts: ioc, samples, configurations, yara
uncertainties: Attribution reprise de la source; victimologie incomplète

### PUBLICATION P1
title: MuddyWater technical report
url: https://vendor.example/reports/muddywater?utm_source=feed
publisher: Vendor Research
published_at: 2026-07-10
period_relation: in_period
source_role: primary
ioc_presence: declared
ioc_declared_count: 42
ioc_visible_count: unknown

### PUBLICATION P2
title: A new MuddyWater campaign
url: https://relay.example/news/muddywater
publisher: Security News
published_at: 2026-07-11
period_relation: in_period
source_role: relay
ioc_presence: none
ioc_declared_count: unknown
ioc_visible_count: unknown

### PUBLICATION P3
title: CERT advisory on the campaign
url: https://cert.example/advisories/42
publisher: National CERT
published_at: 2026-07-12
period_relation: in_period
source_role: independent
ioc_presence: visible
ioc_declared_count: unknown
ioc_visible_count: 3

# LIMITES
Recherche non exhaustive.
"""


def parameters(axis: str = "initial") -> DiscoverEditionParameters:
    return DiscoverEditionParameters(
        edition_id=uuid4(),
        country="Iran",
        country_aliases=["Iran", "IR", "République islamique d'Iran"],
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        languages=["fr", "en", "fa"],
        source_profile="iran-default",
        keywords=["APT", "IOC"],
        exclusions=["cryptomonnaie"],
        complementary_axis=axis,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


async def test_complete_discovery_job_with_fake_adapter_is_sourced_and_idempotent() -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture(),
        structured_outputs={
            "ResearchBatch": (
                '{"minimal_example":{"citations":[],"queries":[],"topics":[]},'
                '"version":"research-batch-compact-v1"}'
            )
        },
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_provider=ModelProvider.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    grouped_editions: list[UUID] = []

    async def group_after_discovery(edition_id: UUID) -> None:
        grouped_editions.append(edition_id)

    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
        after_discovery=group_after_discovery,
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()

    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(params),
        correlation_id="test-discovery",
        input_parameters=params.model_dump(mode="json"),
    )
    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert completed.status is JobStatus.SUCCEEDED
    batches = await discovery.list_batches(params.edition_id)
    assert len(batches) == 1
    assert len(batches[0].candidates) == 1
    assert batches[0].source_mode is DiscoverySourceMode.MODEL_DECLARED_URLS
    assert batches[0].source_coverage_complete is False
    assert batches[0].citation_count == 0
    assert batches[0].bridge_capabilities == {
        "web_search": True,
        "native_sources": False,
        "visible_citations": True,
        "snapshot_available": True,
    }
    candidate = batches[0].candidates[0]
    assert candidate.editorial_status == "proposed"
    assert {source.role for source in candidate.sources} == {
        SourceRole.PRIMARY,
        SourceRole.RELAY,
        SourceRole.INDEPENDENT,
    }
    assert all(
        source.verification_status is SourceVerificationStatus.UNVERIFIED
        for source in candidate.sources
    )
    assert all(
        source.relationship_status is SourceRelationshipStatus.PROVISIONAL
        for source in candidate.sources
    )
    assert len({source.canonical_url for source in candidate.sources}) == len(candidate.sources)
    assert len(fake.calls) == 1
    assert fake.calls[0].conversation is not None
    assert fake.calls[0].conversation.mode == "fresh"
    assert all(run.model_role.value == "research" for run in model_uow.state.values())
    assert grouped_editions == [params.edition_id]

    with pytest.raises(DuplicateJobError):
        await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="edition",
            aggregate_id=params.edition_id,
            idempotency_key=discovery_idempotency_key(params),
            correlation_id="test-retry",
            input_parameters=params.model_dump(mode="json"),
        )
    assert len(await discovery.list_batches(params.edition_id)) == 1
    assert len(fake.calls) == 1

    complementary = params.model_copy(update={"complementary_axis": "configurations publiées"})
    second_job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(complementary),
        correlation_id="test-complement",
        input_parameters=complementary.model_dump(mode="json"),
    )
    await dispatcher.dispatch(second_job.id)
    batches = await discovery.list_batches(params.edition_id)
    assert len(batches) == 2
    assert sum(len(batch.candidates) for batch in batches) == 1
    assert len(fake.calls) == 2
    assert fake.calls[1].conversation is not None
    assert fake.calls[1].conversation.mode == "fresh"
    assert fake.calls[1].conversation.id != fake.calls[0].conversation.id
    assert grouped_editions == [params.edition_id, params.edition_id]


async def test_partial_invalid_output_keeps_topics_and_archives_diagnostics() -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture()
        + """
## SUBJECT S2
title: Sujet incomplet
presentation: Publication conservée malgré une URL absente.
technical_potential: unknown
champ_surprise: valeur
### PUBLICATION P4
title: Sans URL
url: ftp://invalid.example/report
""",
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
        ),
        model_uow,
        output_store,
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(params),
        correlation_id="partial-output",
        input_parameters=params.model_dump(mode="json"),
    )

    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert completed.status is JobStatus.SUCCEEDED
    batches = await discovery.list_batches(params.edition_id)
    assert batches[0].candidates
    assert len(fake.calls) == 1
    assert all(run.model_role.value == "research" for run in model_uow.state.values())
    research_run = next(iter(model_uow.state.values()))
    assert research_run.raw_output_reference
    assert research_run.parser_stage == "report_parsing_partial"
    assert research_run.validation_errors


async def test_totally_invalid_output_is_archived_before_safe_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_output = "```json\n{invalid\\_json:SECRET_MODEL_OUTPUT}\n```"
    fake = FakeModelAdapter(
        research_text=raw_output,
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
        ),
        model_uow,
        output_store,
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(params),
        correlation_id="invalid-output",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    failed = await jobs.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "report_parsing_failed"
    assert failed.error_details is not None
    assert failed.error_details["phase"] == "local_parsing"
    research_run = next(iter(model_uow.state.values()))
    assert research_run.raw_output_reference in research_run.output_references
    assert output_store.objects
    assert "SECRET_MODEL_OUTPUT" not in caplog.text
    assert raw_output not in caplog.text


def test_research_batch_rejects_non_http_urls() -> None:
    payload = research_fixture().model_dump()
    payload["topics"][0]["sources"][0]["url"] = "file:///etc/passwd"
    with pytest.raises(ValidationError):
        ResearchBatch.model_validate(payload)


def test_research_prompt_is_the_documented_markdown_contract() -> None:
    prompt = _research_prompt(parameters().model_copy(update={"period_end": date(2027, 7, 31)}))

    assert "# SUJETS CANDIDATS" in prompt
    assert "## SUBJECT S1" in prompt
    assert "### PUBLICATION P1" in prompt
    assert "N’invente jamais une URL, une date, un nombre d’IOC" in prompt  # noqa: RUF001
    assert "ioc_visible_count: <entier ou unknown>" in prompt
    assert "donnée non fiable" not in prompt
