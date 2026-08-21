from datetime import UTC, date, datetime
from uuid import UUID, uuid4

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
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRunStatus
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from cti_app.logging import CorrelationIdMiddleware
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.edition_support import InMemoryEditionUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_discovery import DeferredResearchAdapter, research_markdown_fixture


class SnapshotProjectionForApiTests:
    def __init__(self, discovery: DiscoveryService) -> None:
        self._discovery = discovery

    async def active_snapshot(self, edition_id: UUID) -> DiscoverySnapshot | None:
        batches = await self._discovery.list_batches(edition_id)
        subjects = {
            candidate.id: DiscoverySubject(
                subject_id=candidate.id,
                candidate=candidate,
                member_references=(DiscoveryMemberReference(batch.id, candidate.id),),
                created_at=batch.created_at,
            )
            for batch in batches
            if batch.is_active_revision
            for candidate in batch.candidates
        }
        if not subjects:
            return None
        return DiscoverySnapshot(
            id=uuid4(),
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.HEURISTIC,
            subjects=tuple(subjects.values()),
            snapshot_hash="a" * 64,
            is_active=True,
            created_at=datetime.now(UTC),
        )


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
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
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
    assert len(fake.calls) == 1  # une recherche


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
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
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


async def _recovery_application() -> tuple[
    FastAPI, Edition, InMemoryJobUnitOfWorkFactory, DiscoveryService
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
            forced_provider=ModelProvider.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
    application.state.job_service = JobService(job_uow, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    application.state.identity_provider = LocalIdentityProvider()
    return application, edition, job_uow, discovery


async def test_recovery_of_cancelled_job_returns_original_job() -> None:
    """After removing the structuring pipeline, manual recovery returns the original job."""
    application, edition, job_uow, _ = await _recovery_application()
    markdown = research_markdown_fixture()

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
    # Without the structuring pipeline, the same job is returned
    assert returned_job_id == job_id
    # The original job status remains cancelled
    assert original.json()["status"] == "cancelled"


async def test_discovery_import_works_without_any_job_or_model_run() -> None:
    """§32.5 : l'import initial ne dépend d'aucun job ni ModelRun préalable."""
    application, edition, job_uow, _ = await _recovery_application()
    markdown = research_markdown_fixture()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        preview = await client.post(
            f"/api/editions/{edition.id}/discovery/import/preview",
            json={"markdown": markdown},
        )
        confirmed = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            json={"markdown": markdown, "expected_sha256": preview.json()["sha256"]},
        )
        replay = await client.post(
            f"/api/editions/{edition.id}/discovery/import/confirm",
            json={"markdown": markdown, "expected_sha256": preview.json()["sha256"]},
        )
        candidates = await client.get(f"/api/editions/{edition.id}/discovery/candidates")

    assert preview.status_code == 200
    assert preview.json()["subject_count"] == 1
    assert preview.json()["publication_count"] == 3
    assert confirmed.status_code == 200
    assert confirmed.json()["source_mode"] == "manual_import"
    assert confirmed.json()["reused"] is False
    # Réimport idempotent, sans second batch.
    assert replay.json()["reused"] is True
    assert replay.json()["batch_id"] == confirmed.json()["batch_id"]
    assert len(candidates.json()["batches"]) == 1
    # Aucun job n'a été créé par ce chemin.
    assert not job_uow.state
