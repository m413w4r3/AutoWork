from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI

from cti_app.api.collection import router
from cti_app.application.collection import SubjectCollectionService, register_collection_jobs
from cti_app.application.extraction import EvidenceExtractionService
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import (
    JobExecutor,
    JobRegistry,
    JobService,
    SynchronousJobDispatcher,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.test_collection import HTML, selected_subject


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("93.184.216.34",)


class Transport:
    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        del request
        return RawHttpResponse(200, {"content-type": "text/html"}, HTML)


async def test_api_and_synchronous_worker_collect_selected_subject(tmp_path: Path) -> None:
    collection_uow = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(collection_uow, ("https://one.example/report",))
    service = SubjectCollectionService(
        collection_uow,
        SafeHttpCollector(Transport(), Resolver()),
        FilesystemBlobStore(tmp_path / "blobs"),
        EvidenceExtractionService(None),
    )
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    job_service = JobService(jobs_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(jobs_uow, registry))
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.job_service = job_service
    app.state.job_dispatcher = dispatcher
    app.state.identity_provider = LocalIdentityProvider()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        launched = await client.post(f"/api/subjects/{subject.id}/collection")
        assert launched.status_code == 202
        job = await job_service.get(UUID(launched.json()["job_id"]))
        assert job.status.value == "succeeded"

        workbench = await client.get(f"/api/subjects/{subject.id}/workbench")

    assert workbench.status_code == 200
    source = workbench.json()["sources"][0]
    assert source["state"] == "completed"
    assert source["latest_attempt"]["sha256"]
    assert source["relationship_status"] == "provisional"
