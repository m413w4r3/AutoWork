from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.discovery import candidate_router as discovery_candidate_router
from cti_app.api.discovery import router as discovery_router
from cti_app.api.discovery_recovery import router as discovery_recovery_router
from cti_app.api.jobs import router as jobs_router
from cti_app.application.discovery.runs import DiscoveryRunService
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryRequestSnapshot,
    DiscoveryRun,
    DiscoveryRunInputMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelBackend, ModelRunStatus
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from cti_app.logging import CorrelationIdMiddleware
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_discovery import DeferredResearchAdapter, research_markdown_fixture


async def test_discovery_run_creation_is_transport_idempotent_and_allows_repeated_configuration(
) -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture(),
    )
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        actor_id="dev-analyst",
        correlation_id="create",
    )
    job_uow = shared_uow
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(discovery_router)
    application.include_router(discovery_recovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = jobs
    application.state.job_dispatcher = dispatcher
    application.state.discovery_run_service = DiscoveryRunService(shared_uow, jobs, dispatcher)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "transport-1"},
            json={
                "source_profile": "iran-default",
                "aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )
        job_id = launched.json()["execution"]["job_id"]
        job = await client.get(f"/api/jobs/{job_id}")
        candidates = await client.get(
            f"/api/editions/{edition.id}/discovery/candidates?sort=technical"
        )
        research_run_id = candidates.json()["batches"][0]["discovery_model_run_id"]
        report = await client.get(f"/api/editions/{edition.id}/discovery/reports/{research_run_id}")
        duplicate = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "transport-1"},
            json={
                "source_profile": "iran-default",
                "aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )
        repeated = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "transport-2"},
            json={
                "source_profile": "iran-default",
                "aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )
        # La frontière d'idempotence est le contrat : sans elle, on ne peut pas
        # distinguer un retry transport d'une nouvelle vague volontaire.
        without_key = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            json={
                "source_profile": "iran-default",
                "aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )
        listed = await client.get(f"/api/editions/{edition.id}/discovery/runs")

    assert without_key.status_code == 422
    assert len(listed.json()) == 2
    assert launched.status_code == 202
    assert job.json()["status"] == "succeeded"
    assert job.json()["max_attempts"] == 1
    assert candidates.json()["total"] == 1
    assert candidates.json()["warning"] == (
        "Les métadonnées et comptes IOC de découverte sont provisoires. Ils seront vérifiés "
        "depuis les documents archivés après la sélection."
    )
    assert report.status_code == 200
    assert report.text == research_markdown_fixture()
    assert candidates.json()["batches"][0]["source_coverage_complete"] is False
    assert candidates.json()["candidates"][0]["sources"][0]["relationship_status"] == (
        "provisional"
    )
    assert "editorial_status" not in candidates.json()["candidates"][0]
    assert candidates.json()["candidates"][0]["discovery_batch_id"]
    assert candidates.json()["candidates"][0]["discovery_run_id"]
    assert candidates.json()["candidates"][0]["selectable"] is True
    assert candidates.json()["candidates"][0]["valid_publication_count"] == 3
    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == launched.json()["run_id"]
    assert duplicate.json()["execution"]["job_id"] == job_id
    assert repeated.status_code == 202
    assert repeated.json()["run_id"] != launched.json()["run_id"]
    assert repeated.json()["execution"]["job_id"] != job_id
    assert job.json()["aggregate_type"] == "discovery_run"
    assert job.json()["aggregate_id"] == launched.json()["run_id"]
    assert len(fake.calls) == 2


class _SnapshotMustNotBeRead:
    async def active_snapshot(self, edition_id: UUID) -> None:
        raise AssertionError("raw candidate reads must not consult the DiscoverySnapshot")


def _seeded_run(edition_id: UUID, key: str) -> DiscoveryRun:
    return DiscoveryRun(
        edition_id=edition_id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile="iran-default",
        complementary_axis=key,
        request_snapshot=DiscoveryRequestSnapshot(
            country="Iran",
            country_code="IR",
            country_aliases=("Iran",),
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            as_of_date=date(2026, 8, 1),
            languages=("fr",),
            source_profile="iran-default",
            keywords=(),
            exclusions=(),
            complementary_axis=key,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        ),
        idempotency_key=key,
        created_by="dev-analyst",
    )


def _seeded_topic(title: str, url: str | None, *, context_only: bool = False) -> CandidateTopic:
    sources = (
        [
            SourceCandidate(
                url=url,
                title=f"{title} source",
                publisher="Vendor",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ]
        if url
        else []
    )
    return CandidateTopic(
        title=title,
        summary=f"{title} summary",
        novelty="Novel.",
        technical_potential=3,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=sources,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        context_only=context_only,
    )


def _seeded_batch(
    run: DiscoveryRun,
    candidates: list[CandidateTopic],
    *,
    request_hash: str,
    created_at: datetime,
) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=run.edition_id,
        discovery_run_id=run.id,
        request_hash=request_hash,
        complementary_axis=run.complementary_axis,
        queries=(),
        citations=(),
        candidates=candidates,
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test",
        created_at=created_at,
        updated_at=created_at,
    )


async def test_raw_candidate_reads_by_edition_run_and_id_ignore_the_snapshot() -> None:
    fake = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    edition_id = uuid4()
    run_a = _seeded_run(edition_id, "wave-a")
    run_b = _seeded_run(edition_id, "wave-b")
    foreign_run = _seeded_run(uuid4(), "foreign")
    for run in (run_a, run_b, foreign_run):
        shared_uow.runs[run.id] = run

    # Two waves proposing the same campaign stay two raw candidates.
    historical = _seeded_topic("Same campaign", "https://vendor.example/a-v1")
    revised = _seeded_topic("Same campaign", "https://vendor.example/a-v2")
    wave_b = _seeded_topic("Same campaign", "https://vendor.example/b")
    # Same title inside one batch: two parsed proposals, never fused on ingestion.
    wave_b_twin = _seeded_topic("Same campaign", "https://vendor.example/b-twin")
    context = _seeded_topic("Background context", None, context_only=True)
    batch_a1 = _seeded_batch(
        run_a, [historical], request_hash="a" * 64, created_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    batch_a2 = _seeded_batch(
        run_a, [revised], request_hash="b" * 64, created_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    batch_a2.parsing_revision = 2
    batch_a2.supersedes_batch_id = batch_a1.id
    batch_a1.replaced_by_batch_id = batch_a2.id
    batch_b = _seeded_batch(
        run_b,
        [wave_b, wave_b_twin, context],
        request_hash="c" * 64,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    async with shared_uow() as uow:
        for batch in (batch_a1, batch_a2, batch_b):
            assert await uow.discovery_batches.add_if_absent(batch)

    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(discovery_candidate_router)
    application.state.discovery_service = DiscoveryService(shared_uow, gateway, archive=gateway)
    application.state.cumulative_discovery_service = _SnapshotMustNotBeRead()
    application.state.identity_provider = LocalIdentityProvider()
    base = f"/api/editions/{edition_id}/discovery"

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        active = await client.get(f"{base}/candidates")
        with_history = await client.get(f"{base}/candidates?include_replaced=true")
        filtered = await client.get(f"{base}/candidates?search=background")
        run_a_active = await client.get(f"{base}/runs/{run_a.id}/candidates")
        run_a_history = await client.get(
            f"{base}/runs/{run_a.id}/candidates?include_replaced=true"
        )
        foreign = await client.get(f"{base}/runs/{foreign_run.id}/candidates")
        by_id = await client.get(f"/api/discovery/candidates/{historical.id}")
        missing = await client.get(f"/api/discovery/candidates/{uuid4()}")
        wave_b_source_id = wave_b.sources[0].id
        marked = await client.patch(
            f"{base}/candidates/{wave_b.id}/sources/{wave_b_source_id}",
            json={"status": "invalid"},
        )
        wrong_candidate = await client.patch(
            f"{base}/candidates/{revised.id}/sources/{wave_b_source_id}",
            json={"status": "unavailable"},
        )
        after_mark = await client.get(f"{base}/candidates")

    assert active.status_code == 200
    raw = active.json()
    assert set(raw) == {"batches", "candidates", "total", "warning"}
    assert {item["id"] for item in raw["candidates"]} == {
        str(revised.id),
        str(wave_b.id),
        str(wave_b_twin.id),
        str(context.id),
    }
    assert raw["total"] == 4
    same_campaign = [item for item in raw["candidates"] if item["title"] == "Same campaign"]
    assert {item["discovery_run_id"] for item in same_campaign} == {str(run_a.id), str(run_b.id)}
    # The two same-title proposals of batch_b keep their own identity and evidence.
    twins = [item for item in same_campaign if item["discovery_batch_id"] == str(batch_b.id)]
    assert [item["id"] for item in twins] == [str(wave_b.id), str(wave_b_twin.id)]
    assert [len(item["sources"]) for item in twins] == [1, 1]
    assert {item["sources"][0]["url"] for item in twins} == {
        "https://vendor.example/b",
        "https://vendor.example/b-twin",
    }
    for item in raw["candidates"]:
        for merge_field in (
            "member_references",
            "contribution_count",
            "duplicate_publication_count",
            "merge_warnings",
            "editorial_status",
            "batch_id",
        ):
            assert merge_field not in item
    context_view = next(item for item in raw["candidates"] if item["id"] == str(context.id))
    assert context_view["context_only"] is True
    assert context_view["selectable"] is False

    assert {item["id"] for item in with_history.json()["candidates"]} == {
        str(historical.id),
        str(revised.id),
        str(wave_b.id),
        str(wave_b_twin.id),
        str(context.id),
    }
    assert [item["id"] for item in filtered.json()["candidates"]] == [str(context.id)]

    assert run_a_active.status_code == 200
    assert [item["id"] for item in run_a_active.json()] == [str(revised.id)]
    assert [item["id"] for item in run_a_history.json()] == [str(historical.id), str(revised.id)]
    assert foreign.status_code == 404

    assert by_id.status_code == 200
    assert by_id.json()["discovery_batch_id"] == str(batch_a1.id)
    assert by_id.json()["discovery_run_id"] == str(run_a.id)
    assert missing.status_code == 404

    assert marked.status_code == 200
    assert marked.json()["verification_status"] == "invalid"
    assert wrong_candidate.status_code == 404
    marked_view = next(
        item for item in after_mark.json()["candidates"] if item["id"] == str(wave_b.id)
    )
    assert marked_view["sources"][0]["verification_status"] == "invalid"
    revised_view = next(
        item for item in after_mark.json()["candidates"] if item["id"] == str(revised.id)
    )
    assert revised_view["sources"][0]["verification_status"] == "unverified"


async def test_discovery_request_snapshot_keyword_and_exclusion_boundaries() -> None:
    fake = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        actor_id="dev-analyst",
        correlation_id="snapshot-boundaries",
    )
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(shared_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(shared_uow, registry))
    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = jobs
    application.state.job_dispatcher = dispatcher
    application.state.discovery_run_service = DiscoveryRunService(shared_uow, jobs, dispatcher)
    application.state.identity_provider = LocalIdentityProvider()

    accepted: dict[int, tuple[list[str], list[str], str]] = {}
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        for size in (64, 65, 100):
            keywords = [f"keyword-{size}-{index}" for index in range(size)]
            exclusions = [f"exclusion-{size}-{index}" for index in range(size)]
            response = await client.post(
                f"/api/editions/{edition.id}/discovery/runs",
                headers={"Idempotency-Key": f"snapshot-boundary-{size}"},
                json={
                    "source_profile": "iran-default",
                    "keywords": keywords,
                    "exclusions": exclusions,
                    "complementary_axis": "initial",
                },
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            persisted = await client.get(
                f"/api/editions/{edition.id}/discovery/runs/{run_id}"
            )
            assert persisted.status_code == 200
            snapshot = persisted.json()["request_snapshot"]
            assert snapshot["keywords"] == keywords
            assert snapshot["exclusions"] == exclusions
            accepted[size] = (keywords, exclusions, run_id)

        too_many_keywords = [f"keyword-101-{index}" for index in range(101)]
        too_many_exclusions = [f"exclusion-101-{index}" for index in range(101)]
        rejected = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "snapshot-boundary-101"},
            json={
                "source_profile": "iran-default",
                "keywords": too_many_keywords,
                "exclusions": too_many_exclusions,
                "complementary_axis": "initial",
            },
        )
        runs = await client.get(f"/api/editions/{edition.id}/discovery/runs")

    assert set(accepted) == {64, 65, 100}
    assert rejected.status_code == 422
    assert runs.status_code == 200
    assert len(runs.json()) == 3
    assert all(item["run_id"] in {value[2] for value in accepted.values()} for item in runs.json())


async def test_discovery_run_listing_is_newest_first_and_uses_job_projection() -> None:
    fake = FakeModelAdapter(research_text=research_markdown_fixture())
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        actor_id="dev-analyst",
        correlation_id="create-listing",
    )
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(shared_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(shared_uow, registry))
    application = FastAPI()
    application.include_router(discovery_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.discovery_run_service = DiscoveryRunService(
        shared_uow, jobs, dispatcher
    )
    application.state.job_service = jobs
    application.state.job_dispatcher = dispatcher
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "listing-1"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
        second = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "listing-2"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
        first_detail = await client.get(
            f"/api/editions/{edition.id}/discovery/runs/{first.json()['run_id']}"
        )
        listing = await client.get(f"/api/editions/{edition.id}/discovery/runs")

    assert first.status_code == second.status_code == 202
    assert listing.status_code == first_detail.status_code == 200
    assert [item["run_id"] for item in listing.json()] == [
        second.json()["run_id"],
        first.json()["run_id"],
    ]
    assert first_detail.json()["request_snapshot"] == first.json()["request_snapshot"]
    assert first_detail.json()["execution"]["status"] == "succeeded"
    assert first_detail.json()["result"] is not None

    await edition_service.update(
        edition.id,
        expected_version=edition.version,
        country="Iran Renamed",
        country_code="IR",
        period_start=edition.period_start,
        period_end=edition.period_end,
        tlp=TLP.RED,
        languages=("en",),
        actor_id="dev-analyst",
        correlation_id="change-metadata",
    )
    archived = await edition_service.archive(
        edition.id,
        expected_version=2,
        actor_id="dev-analyst",
        correlation_id="archive",
    )
    assert archived.state.value == "archived"
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        after_archive = await client.get(f"/api/editions/{edition.id}/discovery/runs")
        rejected = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "listing-new"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
    assert after_archive.status_code == 200
    assert after_archive.json()[0]["request_snapshot"]["country"] == "Iran"
    assert rejected.status_code == 422


async def test_manual_recovery_previews_then_resumes_the_original_run_job() -> None:
    adapter = DeferredResearchAdapter(needs_review=True)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        output_store,
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        actor_id="dev-analyst",
        correlation_id="create-recovery",
    )
    job_uow = shared_uow
    registry = create_job_registry(gateway, discovery)
    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(discovery_recovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = JobService(job_uow, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application.state.identity_provider = LocalIdentityProvider()
    application.state.discovery_run_service = DiscoveryRunService(
        shared_uow, application.state.job_service, application.state.job_dispatcher
    )
    markdown = research_markdown_fixture() + "\n<!-- import manuel exact -->\n"

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "recovery-transport-1"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
        run_id = launched.json()["run_id"]
        job_id = launched.json()["execution"]["job_id"]
        waiting = await client.get(f"/api/jobs/{job_id}")
        model_run_id = waiting.json()["error_details"]["model_run_id"]
        preview = await client.post(
            f"/api/editions/{edition.id}/discovery/recovery/{model_run_id}/manual/preview",
            json={"job_id": job_id, "markdown": markdown},
        )
        still_waiting = await client.get(f"/api/jobs/{job_id}")
        confirmed = await client.post(
            f"/api/editions/{edition.id}/discovery/recovery/{model_run_id}/manual/confirm",
            json={
                "job_id": job_id,
                "markdown": markdown,
                "expected_sha256": preview.json()["sha256"],
            },
        )
        completed = await client.get(f"/api/jobs/{job_id}")
        candidates = await client.get(f"/api/editions/{edition.id}/discovery/candidates")

    assert waiting.json()["status"] == "waiting_human"
    assert waiting.json()["aggregate_type"] == "discovery_run"
    assert waiting.json()["aggregate_id"] == run_id
    assert launched.json()["run_id"] == run_id
    assert preview.status_code == 200
    assert preview.json()["subject_count"] == 1
    assert preview.json()["publication_count"] == 3
    assert still_waiting.json()["status"] == "waiting_human"
    assert confirmed.status_code == 202
    assert completed.json()["status"] == "succeeded"
    assert candidates.json()["total"] == 1
    run = model_uow.state[next(iter(model_uow.state))]
    assert run.status is ModelRunStatus.SUCCEEDED
    assert run.error_details is not None
    assert run.error_details["recovery"]["provenance"] == "manual_import"
    assert run.raw_output_reference is not None
    assert (
        await output_store.read(run.raw_output_reference, max_bytes=10_000_000)
    ).decode() == markdown


async def _recovery_application() -> tuple[
    FastAPI, Edition, InMemoryDiscoveryUnitOfWorkFactory, DiscoveryService
]:
    """Application minimale exposant découverte + jobs, ChatGPT en needs_review."""
    adapter = DeferredResearchAdapter(needs_review=True)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        actor_id="dev-analyst",
        correlation_id="create-recovery",
    )
    job_uow = shared_uow
    registry = create_job_registry(gateway, discovery)
    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(discovery_recovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = JobService(job_uow, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application.state.identity_provider = LocalIdentityProvider()
    application.state.discovery_run_service = DiscoveryRunService(
        shared_uow, application.state.job_service, application.state.job_dispatcher
    )
    return application, edition, job_uow, discovery


async def test_recovery_of_cancelled_job_returns_original_job() -> None:
    """After removing the structuring pipeline, manual recovery returns the original job."""
    application, edition, _job_uow, _ = await _recovery_application()
    markdown = research_markdown_fixture()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "cancelled-recovery-1"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
        job_id = launched.json()["execution"]["job_id"]
        waiting = await client.get(f"/api/jobs/{job_id}")
        model_run_id = waiting.json()["error_details"]["model_run_id"]

        cancelled = await client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code in {200, 202}

        preview = await client.post(
            f"/api/editions/{edition.id}/discovery/recovery/{model_run_id}/manual/preview",
            json={"job_id": job_id, "markdown": markdown},
        )
        confirmed = await client.post(
            f"/api/editions/{edition.id}/discovery/recovery/{model_run_id}/manual/confirm",
            json={
                "job_id": job_id,
                "markdown": markdown,
                "expected_sha256": preview.json()["sha256"],
            },
        )
        original = await client.get(f"/api/jobs/{job_id}")

    assert preview.status_code == 200
    assert confirmed.status_code == 202

    returned_job_id = confirmed.json()["job_id"]
    assert returned_job_id == job_id
    assert original.json()["status"] == "cancelled"


async def test_discovery_import_preview_creates_no_run_and_confirm_is_idempotent_per_key() -> None:
    """§32.5 : l'import initial ne dépend d'aucun job ni ModelRun préalable."""
    application, edition, job_uow, _ = await _recovery_application()
    markdown = research_markdown_fixture()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        preview = await client.post(
            f"/api/editions/{edition.id}/discovery/import/preview",
            json={"source_profile": "iran-default", "markdown": markdown},
        )
        before_confirm = await client.get(f"/api/editions/{edition.id}/discovery/runs")
        confirmed = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            headers={"Idempotency-Key": "import-transport-1"},
            json={
                "source_profile": "iran-default",
                "markdown": markdown,
                "expected_sha256": preview.json()["sha256"],
            },
        )
        replay = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            headers={"Idempotency-Key": "import-transport-1"},
            json={
                "source_profile": "iran-default",
                "markdown": markdown,
                "expected_sha256": preview.json()["sha256"],
            },
        )
        second_key = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            headers={"Idempotency-Key": "import-transport-2"},
            json={
                "source_profile": "iran-default",
                "markdown": markdown,
                "expected_sha256": preview.json()["sha256"],
            },
        )
        candidates = await client.get(f"/api/editions/{edition.id}/discovery/candidates")

    assert preview.status_code == 200
    assert before_confirm.status_code == 200
    assert before_confirm.json() == []
    assert preview.json()["subject_count"] == 1
    assert preview.json()["publication_count"] == 3
    assert confirmed.status_code == 200
    assert confirmed.json()["source_mode"] == "manual_import"
    assert confirmed.json()["reused"] is False
    assert confirmed.json()["run_id"]
    # Réimport idempotent, sans second batch.
    assert replay.json()["reused"] is True
    assert replay.json()["batch_id"] == confirmed.json()["batch_id"]
    assert replay.json()["run_id"] == confirmed.json()["run_id"]
    assert second_key.status_code == 200
    assert second_key.json()["run_id"] != confirmed.json()["run_id"]
    assert len(candidates.json()["batches"]) == 2
    # Aucun job n'a été créé par ce chemin.
    assert not job_uow.jobs


async def test_archived_edition_rejects_launch_and_import() -> None:
    application, edition, _job_uow, _ = await _recovery_application()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        before_archive = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "pre-archive-launch"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
    assert before_archive.status_code == 202
    archived_run_id = before_archive.json()["run_id"]
    edition = await application.state.edition_service.get(edition.id)

    await application.state.edition_service.archive(
        edition.id,
        expected_version=edition.version,
        actor_id="dev-analyst",
        correlation_id="archive-discovery-test",
    )

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "archived-launch"},
            json={"source_profile": "iran-default"},
        )
        preview = await client.post(
            f"/api/editions/{edition.id}/discovery/import/preview",
            json={"source_profile": "iran-default", "markdown": "# import"},
        )
        confirmed = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            headers={"Idempotency-Key": "archived-import"},
            json={
                "source_profile": "iran-default",
                "markdown": "# import",
                "expected_sha256": "0" * 64,
            },
        )
        # Read-only depuis AW-002/AW-004 : l'historique reste consultable.
        listed = await client.get(f"/api/editions/{edition.id}/discovery/runs")
        detail = await client.get(
            f"/api/editions/{edition.id}/discovery/runs/{archived_run_id}"
        )

    for response in (launched, preview, confirmed):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_discovery"
        assert "archived" in response.json()["detail"]["message"]

    assert listed.status_code == 200
    assert [item["run_id"] for item in listed.json()] == [archived_run_id]
    assert detail.status_code == 200
    assert detail.json()["execution"]["job_id"] is not None


async def test_reprocess_job_uses_discovery_run_aggregate_and_keeps_run_identity() -> None:
    fake = FakeModelAdapter(research_text=research_markdown_fixture())
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    shared_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(shared_uow, gateway, archive=gateway)
    edition_service = EditionService(shared_uow)
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        actor_id="dev-analyst",
        correlation_id="create-reprocess",
    )
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(shared_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(shared_uow, registry))
    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = jobs
    application.state.job_dispatcher = dispatcher
    application.state.discovery_run_service = DiscoveryRunService(shared_uow, jobs, dispatcher)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery/runs",
            headers={"Idempotency-Key": "reprocess-launch"},
            json={"source_profile": "iran-default", "complementary_axis": "initial"},
        )
        run_id = launched.json()["run_id"]
        initial_batch_id = launched.json()["result"]["batch_id"]
        research_model_run_id = launched.json()["result"]["research_model_run_id"]
        first = await client.post(
            f"/api/editions/{edition.id}/discovery/reports/reprocess",
            headers={"Idempotency-Key": "revision-1"},
            json={"run_id": run_id, "research_model_run_id": research_model_run_id},
        )
        repeated = await client.post(
            f"/api/editions/{edition.id}/discovery/reports/reprocess",
            headers={"Idempotency-Key": "revision-1"},
            json={"run_id": run_id, "research_model_run_id": research_model_run_id},
        )
        run = await client.get(f"/api/editions/{edition.id}/discovery/runs/{run_id}")
        job = await client.get(f"/api/jobs/{first.json()['job_id']}")
        runs = await client.get(f"/api/editions/{edition.id}/discovery/runs")

    assert first.status_code == repeated.status_code == 202
    assert first.json()["reused"] is False
    assert repeated.json()["reused"] is True
    assert repeated.json()["job_id"] == first.json()["job_id"]
    assert run.json()["run_id"] == run_id
    assert run.json()["result"]["batch_id"] != initial_batch_id
    assert job.json()["aggregate_type"] == "discovery_run"
    assert job.json()["aggregate_id"] == run_id
    assert len(runs.json()) == 1

    await edition_service.archive(
        edition.id,
        expected_version=1,
        actor_id="dev-analyst",
        correlation_id="archive-reprocess",
    )
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        rejected = await client.post(
            f"/api/editions/{edition.id}/discovery/reports/reprocess",
            headers={"Idempotency-Key": "revision-2"},
            json={"run_id": run_id, "research_model_run_id": research_model_run_id},
        )
    assert rejected.status_code == 422
    assert "archived" in rejected.json()["detail"]["message"]
