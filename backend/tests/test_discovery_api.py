from datetime import date

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.discovery import router as discovery_router
from cti_app.api.jobs import router as jobs_router
from cti_app.application.discovery import DiscoveryService
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
from cti_app.domain.model_runs import ModelProvider, ModelRunStatus
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from cti_app.logging import CorrelationIdMiddleware
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.edition_support import InMemoryEditionUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_discovery import DeferredResearchAdapter, research_markdown_fixture


async def test_discovery_api_launch_follow_read_and_mark_source() -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture(),
    )
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_provider=ModelProvider.FAKE,
        ),
        InMemoryModelRunUnitOfWorkFactory(),
        InMemoryModelOutputStore(),
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, gateway)
    edition_service = EditionService(InMemoryEditionUnitOfWorkFactory())
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=2,
        target_briefs=6,
        previous_edition_id=None,
        source_profile="iran-default",
        actor_id="dev-analyst",
        correlation_id="create",
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(discovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = JobService(job_uow, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery",
            json={
                "country_aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )
        job = await client.get(f"/api/jobs/{launched.json()['job_id']}")
        candidates = await client.get(
            f"/api/editions/{edition.id}/discovery/candidates?sort=technical"
        )
        research_run_id = candidates.json()["batches"][0]["discovery_model_run_id"]
        report = await client.get(f"/api/editions/{edition.id}/discovery/reports/{research_run_id}")
        retried = await client.post(
            f"/api/editions/{edition.id}/discovery/reports/reprocess",
            json={
                "research_model_run_id": research_run_id,
                "complementary_axis": "initial",
            },
        )
        retried_job = await client.get(f"/api/jobs/{retried.json()['job_id']}")
        reprocessed_candidates = await client.get(
            f"/api/editions/{edition.id}/discovery/candidates?sort=technical"
        )
        reprocessed_diagnostic = await client.get(
            f"/api/editions/{edition.id}/discovery/candidates?include_replaced=true"
        )
        source_id = candidates.json()["candidates"][0]["sources"][0]["id"]
        marked = await client.patch(
            f"/api/editions/{edition.id}/discovery/sources/{source_id}",
            json={"status": "verify_later"},
        )
        duplicate = await client.post(
            f"/api/editions/{edition.id}/discovery",
            json={
                "country_aliases": ["République islamique d'Iran"],
                "keywords": ["APT", "IOC"],
                "exclusions": ["crypto scam"],
                "complementary_axis": "initial",
            },
        )

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
    assert candidates.json()["candidates"][0]["editorial_status"] == "proposed"
    assert candidates.json()["candidates"][0]["selectable"] is True
    assert candidates.json()["candidates"][0]["valid_publication_count"] == 3
    assert marked.json()["verification_status"] == "verify_later"
    assert duplicate.json()["reused"] is True
    assert duplicate.json()["job_id"] == launched.json()["job_id"]
    assert retried.status_code == 202
    assert retried_job.json()["status"] == "succeeded"
    assert len(reprocessed_candidates.json()["batches"]) == 1
    assert reprocessed_candidates.json()["batches"][0]["parsing_revision"] == 2
    assert len(reprocessed_diagnostic.json()["batches"]) == 2
    assert reprocessed_diagnostic.json()["batches"][0]["is_active_revision"] is False
    assert (
        reprocessed_diagnostic.json()["batches"][0]["report_sha256"]
        == reprocessed_diagnostic.json()["batches"][1]["report_sha256"]
    )
    assert len(fake.calls) == 1  # une recherche ; retraitement strictement local


async def test_manual_recovery_previews_then_resumes_the_original_job() -> None:
    adapter = DeferredResearchAdapter(needs_review=True)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_provider=ModelProvider.FAKE,
        ),
        model_uow,
        output_store,
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, gateway)
    edition_service = EditionService(InMemoryEditionUnitOfWorkFactory())
    edition = await edition_service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_major_articles=2,
        target_briefs=6,
        previous_edition_id=None,
        source_profile="iran-default",
        actor_id="dev-analyst",
        correlation_id="create-recovery",
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    application = FastAPI()
    application.include_router(discovery_router)
    application.include_router(jobs_router)
    application.state.edition_service = edition_service
    application.state.discovery_service = discovery
    application.state.job_service = JobService(job_uow, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application.state.identity_provider = LocalIdentityProvider()
    markdown = research_markdown_fixture() + "\n<!-- import manuel exact -->\n"

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        launched = await client.post(
            f"/api/editions/{edition.id}/discovery",
            json={"complementary_axis": "initial"},
        )
        job_id = launched.json()["job_id"]
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
