from __future__ import annotations

from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_conversation_turns import EditorialDiscussionObjective
from cti_app.domain.editorial_thread import EditorialThreadJobState
from tests.test_editorial_thread_message import make_worker_runtime


@pytest.mark.asyncio
async def test_propose_refused_without_unique_adoption_and_enabled_after_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = uuid4()
    occurrence_id = uuid4()
    url = f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/entries"

    unavailable_app = FastAPI()
    unavailable_app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unavailable_app), base_url="http://editorial.test"
    ) as client:
        unavailable = await client.post(
            url,
            json={"kind": "propose", "text": "Clarifier le passage."},
            headers={"Idempotency-Key": "proposal-key-unavailable-01"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "propose_not_enabled"

    queued: list[tuple[UUID, UUID, str, str]] = []

    class ThreadService:
        async def send_proposal(
            self,
            requested_subject_id: UUID,
            requested_occurrence_id: UUID,
            *,
            text: str,
            actor_id: UUID,
            idempotency_key: str,
        ):
            queued.append((requested_subject_id, requested_occurrence_id, text, idempotency_key))
            return SimpleNamespace(
                kind="accepted",
                thread_id=uuid4(),
                entry_id=uuid4(),
                generation=EditorialThreadJobState.QUEUED,
            )

    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        lambda _request: SimpleNamespace(adopt=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "_thread_service",
        lambda _request: ThreadService(),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "_thread_actor_id",
        lambda _request: _actor_id(),
    )
    enabled_app = FastAPI()
    enabled_app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=enabled_app), base_url="http://editorial.test"
    ) as client:
        enabled = await client.post(
            url,
            json={"kind": "propose", "text": "Clarifier le passage."},
            headers={"Idempotency-Key": "proposal-key-enabled-0001"},
        )

    assert enabled.status_code == 202
    assert queued == [
        (subject_id, occurrence_id, "Clarifier le passage.", "proposal-key-enabled-0001")
    ]


async def _actor_id() -> UUID:
    return UUID("00000000-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_non_uuid_dev_actor_is_stably_mapped_to_uuid() -> None:
    request = Request({"type": "http", "headers": []})
    request.state.identity = SimpleNamespace(actor_id="dev-analyst")

    actor_id = await editorial_workspace_api._thread_actor_id(request)

    assert actor_id == uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")


@pytest.mark.asyncio
async def test_message_objective_still_never_creates_proposal() -> None:
    worker, repository, _store, model_conversations, _message = make_worker_runtime()
    job_id = next(iter(repository.jobs))

    await worker.process(job_id)

    assert model_conversations.add_arguments is not None
    assert (
        model_conversations.add_arguments["editorial_objective"]
        is EditorialDiscussionObjective.MESSAGE
    )
    assert repository.proposal_writes == 0
    assert repository.canonical_writes == 0


@pytest.mark.asyncio
async def test_propose_uses_exact_thread_turn_without_second_gateway_call() -> None:
    captured: list[tuple[UUID, UUID, str]] = []

    class ProposalCompletion:
        async def prepare_thread_turn(
            self,
            *,
            thread_id: UUID,
            model_turn_id: UUID,
            model_run_id: UUID,
            raw_response: str,
            actor_id: UUID,
            instruction: str,
            context,
        ):
            assert thread_id != UUID(int=0)
            assert actor_id != UUID(int=0)
            assert instruction
            assert context.proposal_snapshot_id is not None
            captured.append((model_turn_id, model_run_id, raw_response))
            return SimpleNamespace(state="proposed")

    worker, repository, _store, model_conversations, _instruction = make_worker_runtime(
        objective=EditorialDiscussionObjective.PROPOSAL,
        proposal_service=ProposalCompletion(),
    )
    job_id = next(iter(repository.jobs))

    await worker.process(job_id)

    assert model_conversations.gateway_calls == 1
    assert model_conversations.turn is not None
    assert captured == [
        (
            model_conversations.turn.id,
            model_conversations.turn.model_run_id,
            "@@ TARGET @@\nsynthesis/title\n@@ ACTION @@\nrewrite_title\n"
            "@@ BASE_ARTIFACT_HASH @@\n"
            + "a" * 64
            + "\n@@ BEFORE_HASH @@\n"
            + "b" * 64
            + "\n@@ AFTER @@\nTitre proposé synthétique\n@@ RATIONALE @@\n"
            "Motif synthétique\n@@ END @@",
        )
    ]
