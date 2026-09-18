from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelBackend, ModelRunStatus
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from cti_app.logging import CorrelationIdMiddleware
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
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
    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == launched.json()["run_id"]
    assert duplicate.json()["execution"]["job_id"] == job_id
    assert repeated.status_code == 202
    assert repeated.json()["run_id"] != launched.json()["run_id"]
    assert repeated.json()["execution"]["job_id"] != job_id
    assert job.json()["aggregate_type"] == "discovery_run"
    assert job.json()["aggregate_id"] == launched.json()["run_id"]
    assert len(fake.calls) == 2


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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
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
    application.state.cumulative_discovery_service = SnapshotProjectionForApiTests(discovery)
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

    for response in (launched, preview, confirmed):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_discovery"
        assert "archived" in response.json()["detail"]["message"]


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
