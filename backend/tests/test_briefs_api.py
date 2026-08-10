from pathlib import Path

import httpx
from fastapi import FastAPI

from cti_app.api.briefs import router
from cti_app.application.briefs import BriefService, register_brief_jobs
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import (
    JobExecutor,
    JobRegistry,
    JobService,
    SynchronousJobDispatcher,
)
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.test_briefs import DraftModel, _context, _output


async def test_brief_api_freeze_generate_approve_and_export(tmp_path: Path) -> None:
    factory, store, subject, claim, indicator, source_id = await _context(tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = LocalIdentityProvider()
    service = BriefService(
        factory,
        store,
        DraftModel(_output(claim.id, indicator.id, source_id, text="MuddyWater cible l'Iran.")),
    )
    app.state.brief_service = service
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_brief_jobs(registry, service)
    app.state.job_service = JobService(jobs_uow, registry)
    app.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(jobs_uow, registry))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        frozen = await client.post(f"/api/subjects/{subject.id}/brief/freeze")
        generated = await client.post(
            f"/api/subjects/{subject.id}/brief/generate", json={"provider": "qwen"}
        )
        current = await client.get(f"/api/subjects/{subject.id}/brief")
        approved = await client.post(f"/api/subjects/{subject.id}/brief/approve")
        exported = await client.get(f"/api/subjects/{subject.id}/brief/export.md")

    assert frozen.status_code == 200
    assert frozen.json()["pack"]["version"] == 1
    assert generated.status_code == 202
    assert generated.json()["duplicate"] is False
    assert current.json()["qa"]["factual_sentences_covered"] is True
    assert current.json()["blocks"][0]["sentences"][0]["evidence"][0]["id"] == str(claim.id)
    assert approved.json()["status"] == "approved"
    assert exported.status_code == 200
    assert exported.text.startswith("# MuddyWater cible l'Iran")
