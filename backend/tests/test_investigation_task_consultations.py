"""Task-scoped advisor consultation oracles (M24.1 F4-D B3).

No live Bridge: the composition's invocation service is a scripted in-memory
advisor that mirrors the real attribution and idempotency contract.  The route
must reuse exactly one ``advisor.consult`` invocation, attribute it to the
human requester plus ``context_task_id``, and never mutate the task.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation_task_consultations import (
    _invocation_idempotency_prefix,
)
from cti_app.api.investigation_task_consultations import (
    router as task_consultations_router,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_advisor_tools import (
    _sanitize_advisor_task_context,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkContributionRepository,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationResult,
)
from cti_app.config import Settings
from cti_app.domain.investigation import (
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.work_contribution import (
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionOrigin,
    WorkContributionV1,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000801")
ADVISOR_SCHEMA_VERSION = "advisor-consult.v1"


def _make_task(
    *,
    task_id: UUID | None = None,
    task_key: str = "lead-chargeur",
    status: TaskStatus = TaskStatus.READY,
    investigation_id: UUID = INVESTIGATION_ID,
) -> InvestigationTaskV1:
    return InvestigationTaskV1(
        id=task_id or uuid4(),
        investigation_id=investigation_id,
        task_key=task_key,
        title="Analyser le chargeur",
        question="Quel chargeur produit le second étage ?",
        objective="Confirmer la chaîne d'infection depuis les octets observés.",
        priority=TaskPriority.HIGH,
        status=status,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test"),
        claim_refs=("S1P1",),
        expected_outputs=("note de synthèse",),
        created_at=NOW,
        updated_at=NOW,
    )


def _make_contribution(*, task_id: UUID, key: str, relpath: str) -> WorkContributionV1:
    file_sha256 = hashlib.sha256(key.encode()).hexdigest()
    return WorkContributionV1(
        investigation_id=INVESTIGATION_ID,
        workspace_id="workspace-1",
        workspace_revision_id=f"revision-{key}",
        file_relpath=relpath,
        file_sha256=file_sha256,
        observed_at=NOW,
        origin=WorkContributionOrigin.HUMAN,
        idempotency_key=f"contribution-{key}",
        category=WorkContributionCategory.WORK_CONTRIBUTION,
        task_id=task_id,
        content_blob_id=uuid4(),
        content_sha256=file_sha256,
        import_status=WorkContributionImportStatus.IMPORTED,
    )


class _Investigations:
    def __init__(self) -> None:
        self.row = SimpleNamespace(
            id=INVESTIGATION_ID,
            subject_id=uuid4(),
            production_run_id=uuid4(),
            synthesis_artifact_id=uuid4(),
        )

    async def get(self, investigation_id: UUID) -> Any:
        return self.row if investigation_id == INVESTIGATION_ID else None


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def append(self, decision: Any) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


class _ArtifactStore:
    async def read_json(self, blob_id: UUID, *, max_bytes: int | None = None) -> dict[str, Any]:
        del blob_id, max_bytes
        return {}


class _ArticleSnapshots:
    def __init__(self, anchors: tuple[Any, ...]) -> None:
        self.snapshot = SimpleNamespace(
            article=SimpleNamespace(publication_anchors=anchors),
        )

    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        del synthesis_artifact_id
        return self.snapshot


class _Store:
    def __init__(self, *, anchors: tuple[Any, ...] = ()) -> None:
        self.investigations = _Investigations()
        self.invocations = InMemoryToolInvocationRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.tasks = InMemoryInvestigationTaskRepository()
        self.contributions = InMemoryWorkContributionRepository()
        self.decisions = _Decisions()
        self.artifact_store = _ArtifactStore()
        self.article_snapshots = _ArticleSnapshots(anchors)

    def uow(self) -> _Uow:
        return _Uow(self)


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.analyst_investigations = store.investigations
        self.tool_invocations = store.invocations
        self.evidence_observations = store.observations
        self.investigation_tasks = store.tasks
        self.work_contributions = store.contributions
        self.analyst_decisions = store.decisions

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class _TaskAdvisorService:
    """Scripted ``advisor.consult``: real attribution, idempotency and journal."""

    def __init__(self, store: _Store) -> None:
        self.store = store
        self.calls: list[dict[str, Any]] = []
        self._by_key: dict[str, ToolInvocation] = {}

    async def invoke(self, **kwargs: Any) -> OperatorToolInvocationResult:
        self.calls.append(dict(kwargs))
        idempotency_key = kwargs["idempotency_key"]
        prefix = _invocation_idempotency_prefix(INVESTIGATION_ID, idempotency_key)
        payload_sha256 = hashlib.sha256(canonical_json_bytes(dict(kwargs["arguments"]))).hexdigest()
        operation_key = f"{prefix}{payload_sha256}"
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            if existing.operation_key != operation_key:
                raise OperatorToolInvocationError("idempotency_conflict", 409)
            return self._result(existing, kwargs=kwargs, replay=True)
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key=operation_key,
            tool_name=kwargs["tool_name"],
            schema_version=kwargs["schema_version"],
            normalized_parameters={
                "consultation_type": kwargs["arguments"]["consultation_type"],
                "question": kwargs["arguments"]["question"],
                "context_task_id": (
                    str(kwargs["context_task_id"])
                    if kwargs.get("context_task_id") is not None
                    else None
                ),
            },
            purpose=kwargs["purpose"],
            status=ToolInvocationStatus.SUCCEEDED,
            actor_kind=kwargs.get("requester_actor_kind"),
            actor_id=kwargs.get("actor_id"),
            task_id=kwargs.get("task_id"),
            agent_session_id=kwargs.get("agent_session_id"),
            context_task_id=kwargs.get("context_task_id"),
            created_at=NOW,
            updated_at=NOW,
        )
        await self.store.invocations.add_if_absent(invocation)
        self._by_key[idempotency_key] = invocation
        return self._result(invocation, kwargs=kwargs, replay=False)

    def _result(
        self,
        invocation: ToolInvocation,
        *,
        kwargs: dict[str, Any],
        replay: bool,
    ) -> OperatorToolInvocationResult:
        return OperatorToolInvocationResult(
            investigation_id=INVESTIGATION_ID,
            working_state_version=kwargs["expected_version"],
            working_state_sha256="d" * 64,
            invocation=invocation,
            error_code=None,
            job_id=None,
            idempotent_replay=replay,
            output={},
        )


class _CompositionFactory:
    def __init__(self, store: _Store, service: _TaskAdvisorService) -> None:
        self.store = store
        self.service = service
        self.artifact_store = store.artifact_store
        self.article_snapshots = store.article_snapshots

    def build(self, uow: _Uow, investigation_id: UUID, *, operator_mode: bool = False) -> Any:
        del uow, investigation_id, operator_mode
        return SimpleNamespace(operator_tool_invocations=self.service)


class _SyncClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)


def _client(store: _Store) -> tuple[_SyncClient, _TaskAdvisorService]:
    service = _TaskAdvisorService(store)
    app = FastAPI()
    app.include_router(task_consultations_router)
    app.state.uow_factory = store.uow
    app.state.investigation_composition_factory = _CompositionFactory(store, service)
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None)
    return _SyncClient(app), service


def _seed_prior_consultation(store: _Store, *, task_id: UUID) -> ToolInvocation:
    prior = ToolInvocation(
        investigation_id=INVESTIGATION_ID,
        operation_key="operator:prior-consultation:payload",
        tool_name="advisor.consult",
        schema_version=ADVISOR_SCHEMA_VERSION,
        normalized_parameters={"consultation_type": "critique", "question": "Question précédente"},
        purpose="Consultation précédente",
        status=ToolInvocationStatus.SUCCEEDED,
        actor_kind=ToolInvocationActorKind.HUMAN,
        actor_id="analyst:prior",
        context_task_id=task_id,
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5),
    )
    asyncio.run(store.invocations.add_if_absent(prior))
    return prior


def test_task_consultation_reuses_advisor_consult_and_attribution() -> None:
    anchor = SimpleNamespace(anchor="lead:0001", excerpt="Extrait canonique de l'article.")
    store = _Store(anchors=(anchor,))
    task = _make_task()
    other_task = _make_task(task_key="lead-outre")
    asyncio.run(store.tasks.add(task))
    asyncio.run(store.tasks.add(other_task))
    linked = _make_contribution(task_id=task.id, key="linked", relpath="notes/chargeur.md")
    unrelated = _make_contribution(task_id=other_task.id, key="outre", relpath="notes/outre.md")
    asyncio.run(store.contributions.append_if_absent(linked))
    asyncio.run(store.contributions.append_if_absent(unrelated))
    prior = _seed_prior_consultation(store, task_id=task.id)
    client, service = _client(store)
    path = f"/api/investigations/{INVESTIGATION_ID}/tasks/{task.id}/consultations"
    body = {
        "consultation_type": "interpretation",
        "question": "  Interprète les faits observés de cette tâche.  ",
        "include_previous_consultation": True,
        "anchor": "lead:0001",
        "expected_working_state_version": 1,
    }

    response = client.post(path, json=body, headers={"Idempotency-Key": "task-consult-1"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["tool_name"] == "advisor.consult"
    assert call["schema_version"] == ADVISOR_SCHEMA_VERSION
    assert call["requester_actor_kind"] is ToolInvocationActorKind.HUMAN
    assert call["actor_id"] == "analyst:test"
    assert call["context_task_id"] == task.id
    assert call.get("task_id") is None and call.get("agent_session_id") is None
    assert call["arguments"]["consultation_type"] == "interpretation"
    assert call["arguments"]["question"] == "Interprète les faits observés de cette tâche."
    context = call["arguments"]["task_context"]
    assert context["investigation_id"] == str(INVESTIGATION_ID)
    assert context["task_id"] == str(task.id)
    assert context["task_key"] == task.task_key
    assert context["status"] == "ready"
    assert context["priority"] == "high"
    assert [row["id"] for row in context["human_contributions"]] == [str(linked.id)]
    assert context["previous_consultation"]["invocation_id"] == str(prior.id)
    assert context["previous_consultation"]["type"] == "critique"
    assert context["editorial_anchor"]["anchor"] == "lead:0001"
    assert (
        context["editorial_anchor"]["text_sha256"]
        == hashlib.sha256(anchor.excerpt.encode("utf-8")).hexdigest()
    )

    invocation = asyncio.run(store.invocations.get(UUID(payload["invocation_id"])))
    assert invocation is not None
    assert invocation.context_task_id == task.id
    assert invocation.actor_kind is ToolInvocationActorKind.HUMAN
    assert invocation.actor_id == "analyst:test"
    # The agentic attribution pair stays untouched: this is a human request.
    assert invocation.task_id is None and invocation.agent_session_id is None
    assert payload["context_task_id"] == str(task.id)
    assert payload["invocation_status"] == "succeeded"
    assert payload["consultation"]["invocation_id"] == payload["invocation_id"]
    assert payload["consultation"]["context_task_id"] == str(task.id)
    assert payload["consultation"]["requester_actor_kind"] == "human"
    sanitized = _sanitize_advisor_task_context(
        context, investigation_id=INVESTIGATION_ID, warnings=[]
    )
    assert sanitized is not None
    assert (
        payload["task_context_sha256"]
        == hashlib.sha256(canonical_json_bytes(sanitized)).hexdigest()
    )

    # A replay with the same key and body rebuilds the exact same arguments.
    replay = client.post(path, json=body, headers={"Idempotency-Key": "task-consult-1"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["invocation_id"] == payload["invocation_id"]
    assert service.calls[1]["arguments"]["task_context"] == context
    assert (
        len(asyncio.run(store.invocations.list_for_investigation(INVESTIGATION_ID))) == 2
    )  # the prior seeded consultation plus this one


def test_task_consultation_never_mutates_task_status() -> None:
    store = _Store()
    task = _make_task()
    terminal = _make_task(task_key="lead-annule", status=TaskStatus.CANCELLED)
    asyncio.run(store.tasks.add(task))
    asyncio.run(store.tasks.add(terminal))
    client, service = _client(store)
    path = f"/api/investigations/{INVESTIGATION_ID}/tasks/{task.id}/consultations"
    body = {
        "consultation_type": "critique",
        "question": "Critique la solidité des faits observés.",
        "expected_working_state_version": 1,
    }

    response = client.post(path, json=body, headers={"Idempotency-Key": "task-consult-2"})

    assert response.status_code == 200, response.text
    assert len(service.calls) == 1
    unchanged = asyncio.run(store.tasks.get(task.id))
    assert unchanged == task
    assert unchanged is not None
    assert unchanged.status is TaskStatus.READY
    assert unchanged.status_version == 1
    assert unchanged.result_verification_status is ResultVerificationStatus.UNVERIFIED
    assert asyncio.run(store.tasks.list_events(task.id)) == ()
    assert asyncio.run(store.decisions.list_for_investigation(INVESTIGATION_ID)) == ()

    # A terminal task is refused before any advisor call.
    terminal_path = f"/api/investigations/{INVESTIGATION_ID}/tasks/{terminal.id}/consultations"
    refused = client.post(
        terminal_path, json=body, headers={"Idempotency-Key": "task-consult-terminal"}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "task_consultation_task_terminal"
    assert len(service.calls) == 1
    assert asyncio.run(store.tasks.get(terminal.id)) == terminal

    # A task of another investigation is not addressable from this one.
    foreign = _make_task(task_key="lead-etrangere", investigation_id=uuid4())
    asyncio.run(store.tasks.add(foreign))
    foreign_path = f"/api/investigations/{INVESTIGATION_ID}/tasks/{foreign.id}/consultations"
    foreign_refused = client.post(
        foreign_path, json=body, headers={"Idempotency-Key": "task-consult-foreign"}
    )
    assert foreign_refused.status_code == 404, foreign_refused.text
    assert foreign_refused.json()["detail"]["code"] == "task_not_found"
    assert len(service.calls) == 1
