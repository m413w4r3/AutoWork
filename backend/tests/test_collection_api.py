from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from cti_app.api.collection import router
from cti_app.application.collection import SubjectCollectionService, register_collection_jobs
from cti_app.application.collection_review import CollectionReviewService
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
from cti_app.domain.collection import CollectionState
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.test_collection import HTML, selected_subject


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("93.184.216.34",)


class Transport:
    def __init__(self, responses: list[RawHttpResponse] | None = None) -> None:
        self.responses = responses or []
        self.calls = 0

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        del request
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return RawHttpResponse(200, {"content-type": "text/html"}, HTML)


async def test_api_and_synchronous_worker_collect_selected_subject(tmp_path: Path) -> None:
    collection_uow = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(collection_uow, ("https://one.example/report",))
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    service = SubjectCollectionService(
        collection_uow,
        SafeHttpCollector(Transport(), Resolver()),
        blob_store,
    )
    review_service = CollectionReviewService(collection_uow, blob_store)
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    job_service = JobService(jobs_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(jobs_uow, registry))
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.collection_review_service = review_service
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
        source_payload = workbench.json()["sources"][0]
        download = await client.get(
            f"/api/subjects/{subject.id}/sources/{source_payload['id']}/download"
        )
        wrong_subject = await client.get(
            f"/api/subjects/{uuid4()}/sources/{source_payload['id']}/download"
        )

    assert workbench.status_code == 200
    source = workbench.json()["sources"][0]
    assert source["state"] == "archived"
    assert source["latest_attempt"]["encoded_sha256"]
    assert source["latest_attempt"]["decoded_sha256"]
    assert source["relationship_status"] == "provisional"
    assert source["title"] == "Report 1"
    assert source["logical_filename"].endswith(".html")
    assert download.status_code == 200
    assert download.content == HTML
    assert download.headers["content-type"] == "text/html; charset=utf-8"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert wrong_subject.status_code == 404


async def test_manual_content_endpoint_accepts_multipart_and_refuses_replacement(
    tmp_path: Path,
) -> None:
    collection_uow = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(collection_uow, ("https://blocked.example/report",))
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    service = SubjectCollectionService(
        collection_uow,
        SafeHttpCollector(Transport(), Resolver()),
        blob_store,
    )
    source = (await service.initialize(subject.id))[0]
    collection_uow.collections[source.id].state = source.state = CollectionState.BLOCKED
    review_service = CollectionReviewService(collection_uow, blob_store)
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    job_service = JobService(jobs_uow, registry)
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.collection_review_service = review_service
    app.state.job_service = job_service
    app.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(jobs_uow, registry))
    app.state.identity_provider = LocalIdentityProvider("analyst-1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/subjects/{subject.id}/sources/{source.id}/content"
        archived = await client.post(
            path,
            files={"file": ("capture.html", HTML, "text/html")},
            data={"declared_mime_type": "text/html"},
        )
        replacement = await client.post(
            path,
            json={"content": HTML.decode(), "declared_mime_type": "text/html"},
        )

    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    assert replacement.status_code == 409
    assert replacement.json()["detail"] == "source_already_archived"
    assert any(
        event.event_type == "source.archived_manually" and event.actor_id == "analyst-1"
        for event in collection_uow.provenance
    )


async def test_retry_endpoint_processes_only_requested_source(tmp_path: Path) -> None:
    collection_uow = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        collection_uow,
        ("https://one.example/report", "https://two.example/report"),
    )
    transport = Transport(
        [
            RawHttpResponse(200, {"content-type": "text/html"}, HTML),
            RawHttpResponse(404, {"content-type": "text/html"}, b"missing"),
            RawHttpResponse(200, {"content-type": "text/html"}, HTML),
        ]
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    service = SubjectCollectionService(
        collection_uow,
        SafeHttpCollector(transport, Resolver()),
        blob_store,
    )
    review_service = CollectionReviewService(collection_uow, blob_store)
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    job_service = JobService(jobs_uow, registry)
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.collection_review_service = review_service
    app.state.job_service = job_service
    app.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(jobs_uow, registry))
    app.state.identity_provider = LocalIdentityProvider()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(f"/api/subjects/{subject.id}/collection")).status_code == 202
        sources = (await client.get(f"/api/subjects/{subject.id}/workbench")).json()["sources"]
        completed = next(item for item in sources if item["state"] == "archived")
        unavailable = next(item for item in sources if item["state"] == "unavailable")

        retried = await client.post(f"/api/subjects/{subject.id}/sources/{unavailable['id']}/retry")

    assert retried.status_code == 202
    assert transport.calls == 3
    assert collection_uow.collections[UUID(completed["id"])].attempt_count == 1
    assert collection_uow.collections[UUID(unavailable["id"])].state.value == "archived"
