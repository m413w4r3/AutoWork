from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.jobs import router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.logging import CorrelationIdMiddleware
from tests.job_support import InMemoryJobUnitOfWorkFactory


async def test_api_and_inline_worker_complete_demo_job_without_external_service() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry()
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    application.state.job_service = JobService(factory, registry)
    application.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(factory, registry))
    application.state.identity_provider = LocalIdentityProvider()
    aggregate_id = uuid4()
    payload = {
        "kind": "demo.deterministic",
        "aggregate_type": "subject",
        "aggregate_id": str(aggregate_id),
        "idempotency_key": "api-demo",
        "input_parameters": {"steps": 3, "label": "Démo API"},
    }

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        submitted = await client.post(
            "/api/jobs", json=payload, headers={"X-Correlation-ID": "api-test"}
        )
        job_id = submitted.json()["id"]
        fetched = await client.get(f"/api/jobs/{job_id}")
        duplicate = await client.post("/api/jobs", json=payload)
        history = await client.get(f"/api/jobs/{job_id}/history")
        metrics = await client.get("/api/jobs/metrics/operational")
        events = await client.get(f"/api/jobs/{job_id}/events")

    assert submitted.status_code == 202
    assert submitted.json()["status"] == "succeeded"
    assert submitted.json()["progress_current"] == 3
    assert submitted.json()["correlation_id"] == "api-test"
    assert fetched.json()["output_reference"].startswith("demo://completed/")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["existing_job_id"] == job_id
    assert [event["event_type"] for event in history.json()] == [
        "job.submitted",
        "job.started",
        "job.succeeded",
    ]
    assert history.json()[0]["actor_id"] == "dev-analyst"
    assert metrics.json()["counts_by_status"]["succeeded"] == 1
    assert metrics.json()["failure_rate"] == 0.0
    assert events.headers["content-type"].startswith("text/event-stream")
    assert '"status":"succeeded"' in events.text
