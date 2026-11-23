"""Plan API oracles for M24-04A2 (ADR-0011 D2/D8).

Covers the human plan request through the existing advisor service, idempotent
acceptance and materialisation, dependency materialisation, pure cycle refusal
and the append-only rule that a later plan version never rewrites an existing
task.  No live Bridge: the transport is a scripted in-memory advisor service.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation_plans import router as plans_router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkContributionRepository,
)
from cti_app.application.investigation_plan_materialization import plan_task_key
from cti_app.application.investigation_plans import InvestigationPlanService
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationResult,
    _require_safe_operator_payload,
)
from cti_app.config import Settings
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000601")
SHA_A = "a" * 64

REQUEST_PLAN_TEXT = """## Lead 1 — Vérifier le chargeur

Objectif: confirmer le second étage depuis les octets.
Justification CTI: l'article affirme une chaîne en deux étapes.
Claims: S1P2
Preuves de départ: 1111111111111111111111111111111111111111111111111111111111111111
Échantillons: 2222222222222222222222222222222222222222222222222222222222222222
Outils: pe.strings
Préconditions: échantillon en quarantaine
Intérêt: élevé
Critères de conclusion: deux gates réseau indépendants prouvés.
Limites: pas d'exécution hors profil confiné.
"""


class _PayloadStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, object]] = {}

    async def read_json(self, blob_id: UUID, *, max_bytes: int | None = None) -> dict[str, object]:
        del max_bytes
        return dict(self.payloads[blob_id])


class _Investigations:
    def __init__(self, rows: dict[UUID, Any]) -> None:
        self.rows = rows

    async def get(self, investigation_id: UUID):
        return self.rows.get(investigation_id)


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


class _Store:
    def __init__(self) -> None:
        self.investigations = _Investigations(
            {
                INVESTIGATION_ID: SimpleNamespace(
                    id=INVESTIGATION_ID,
                    subject_id=uuid4(),
                    production_run_id=uuid4(),
                    synthesis_artifact_id=uuid4(),
                )
            }
        )
        self.invocations = InMemoryToolInvocationRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.plans = InMemoryInvestigationPlanRepository()
        self.tasks = InMemoryInvestigationTaskRepository()
        self.contributions = InMemoryWorkContributionRepository()
        self.decisions = _Decisions()
        self.artifacts = _PayloadStore()

    def uow(self) -> _Uow:
        return _Uow(self)


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.analyst_investigations = store.investigations
        self.tool_invocations = store.invocations
        self.evidence_observations = store.observations
        self.investigation_plans = store.plans
        self.investigation_tasks = store.tasks
        self.work_contributions = store.contributions
        self.analyst_decisions = store.decisions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self) -> None:
        return None


async def _seed_advisor_exchange(
    store: _Store,
    *,
    text: str = REQUEST_PLAN_TEXT,
    consultation_type: str = "plan_investigation",
    status: ToolInvocationStatus = ToolInvocationStatus.SUCCEEDED,
    actor_kind: ToolInvocationActorKind | None = None,
    actor_id: str | None = None,
    task_id: UUID | None = None,
    context_task_id: UUID | None = None,
    normalized_parameters: dict[str, Any] | None = None,
) -> tuple[ToolInvocation, EvidenceObservation]:
    call_id = uuid4()
    response_id = uuid4()
    call_blob_id = uuid4()
    response_blob_id = uuid4()
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store.artifacts.payloads[call_blob_id] = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": consultation_type,
        "output": {
            "parse_status": "partially_parsed",
            "readable": True,
            "usable": True,
            "advisory_text": f"[UNTRUSTED ADVISORY]\n{text}",
            "output_sha256": sha,
            "extracted_elements": {"summary": "Plan"},
        },
    }
    store.artifacts.payloads[response_blob_id] = {
        "schema": "operator-advisor-response-link.v1",
        "stage": "response_received",
        "raw_response_sha256": sha,
    }
    call = EvidenceObservation(
        id=call_id,
        investigation_id=INVESTIGATION_ID,
        kind=EvidenceKind.OTHER,
        blob_id=call_blob_id,
        sha256="b" * 64,
        source="advisor.consult",
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type="advisor_call",
        object_id=consultation_type,
        limitations=("Bridge output is untrusted advisory text and is not canonical analysis.",),
    )
    response = EvidenceObservation(
        id=response_id,
        investigation_id=INVESTIGATION_ID,
        kind=EvidenceKind.OTHER,
        blob_id=response_blob_id,
        sha256="c" * 64,
        source="advisor.consult.response",
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type="advisor_response_received",
        object_id=f"advisor-{uuid4()}",
        limitations=("Raw Bridge text is untrusted advisory material.",),
    )
    invocation = ToolInvocation(
        investigation_id=INVESTIGATION_ID,
        operation_key=f"advisor-{uuid4()}",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters=(
            normalized_parameters
            if normalized_parameters is not None
            else {"consultation_type": consultation_type, "question": "plan"}
        ),
        purpose="Ask Bridge for an investigation plan",
        status=status,
        response_refs=(call_id, response_id),
        actor_kind=actor_kind,
        actor_id=actor_id if actor_kind is not None else None,
        task_id=task_id,
        context_task_id=context_task_id,
        created_at=NOW,
        updated_at=NOW,
    )
    await store.invocations.add_if_absent(invocation)
    await store.observations.append_if_absent(call)
    await store.observations.append_if_absent(response)
    return invocation, response


async def _create_plan(
    store: _Store,
    *,
    version: int,
    leads: tuple[InvestigationLeadV1, ...],
) -> UUID:
    invocation, response = await _seed_advisor_exchange(store)
    service = InvestigationPlanService(
        plans=store.plans,
        invocations=store.invocations,
        observations=store.observations,
        investigations=store.investigations,
    )
    stored = await service.create_version(
        InvestigationPlanV1(
            investigation_id=INVESTIGATION_ID,
            version=version,
            leads=leads,
        ),
        parse_status=PlanParseStatus.PARSED,
        source_invocation_id=invocation.id,
        source_observation_id=response.id,
        now=NOW,
    )
    return stored.id


class _AdvisorInvocationService:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.calls: list[dict[str, Any]] = []
        self._by_key: dict[str, ToolInvocation] = {}

    async def invoke(self, **kwargs: Any) -> OperatorToolInvocationResult:
        self.calls.append(dict(kwargs))
        # Mirror the production payload veto: the persisted invocation must
        # never accept a credential-like argument.
        try:
            _require_safe_operator_payload(
                {"arguments": dict(kwargs["arguments"]), "purpose": kwargs["purpose"]}
            )
        except ValueError:
            raise OperatorToolInvocationError(
                "operator_payload_invalid_or_sensitive", 422
            ) from None
        idempotency_key = kwargs["idempotency_key"]
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return OperatorToolInvocationResult(
                investigation_id=INVESTIGATION_ID,
                working_state_version=kwargs["expected_version"],
                working_state_sha256="d" * 64,
                invocation=existing,
                error_code=None,
                job_id=None,
                idempotent_replay=True,
                output={},
            )
        invocation, _response = await _seed_advisor_exchange(
            self.store,
            text=REQUEST_PLAN_TEXT,
            actor_kind=kwargs.get("requester_actor_kind"),
            actor_id=kwargs.get("actor_id"),
            context_task_id=kwargs.get("context_task_id"),
            normalized_parameters=dict(kwargs["arguments"]),
        )
        self._by_key[idempotency_key] = invocation
        return OperatorToolInvocationResult(
            investigation_id=INVESTIGATION_ID,
            working_state_version=kwargs["expected_version"],
            working_state_sha256="d" * 64,
            invocation=invocation,
            error_code=None,
            job_id=None,
            idempotent_replay=False,
            output={},
        )


class _Composition:
    def __init__(self, store: _Store) -> None:
        self.operator_tool_invocations = _AdvisorInvocationService(store)


class _CompositionFactory:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.artifact_store = store.artifacts
        self.invocations = _AdvisorInvocationService(store)

    def build(self, uow: _Uow, investigation_id: UUID, *, operator_mode: bool = False):
        del uow, investigation_id, operator_mode
        composition = _Composition(self.store)
        composition.operator_tool_invocations = self.invocations
        return composition


class _SyncASGIClient:
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


def _client(store: _Store | None = None) -> tuple[_SyncASGIClient, _Store, _CompositionFactory]:
    store = store or _Store()
    factory = _CompositionFactory(store)
    app = FastAPI()
    app.include_router(plans_router)
    app.state.uow_factory = store.uow
    app.state.investigation_composition_factory = factory
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None)
    return _SyncASGIClient(app), store, factory


def _seed_bootstrap_snapshot(store: _Store) -> EvidenceObservation:
    blob_id = uuid4()
    store.artifacts.payloads[blob_id] = {"schema": "vt-bootstrap-snapshot.v1"}
    observation = EvidenceObservation(
        investigation_id=INVESTIGATION_ID,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256="e" * 64,
        source="investigation.bootstrap",
        source_version="vt-bootstrap-snapshot.v1",
        observed_at=NOW,
        object_type="investigation_bootstrap_snapshot",
        object_id="bootstrap",
    )
    asyncio.run(store.observations.append_if_absent(observation))
    return observation


def test_human_plan_request_uses_advisor_service_and_idempotency() -> None:
    client, store, factory = _client()
    snapshot = _seed_bootstrap_snapshot(store)
    path = f"/api/investigations/{INVESTIGATION_ID}/plans/request"
    headers = {"Idempotency-Key": "plan-request-1"}

    first = client.post(path, json={"expected_version": 1}, headers=headers)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["plan_version_id"] is not None
    assert payload["plan_parse_status"] == "parsed"
    assert payload["advice_only"] is False

    call = factory.invocations.calls[0]
    assert call["tool_name"] == "advisor.consult"
    assert call["arguments"]["consultation_type"] == "plan_investigation"
    assert call["requester_actor_kind"] is ToolInvocationActorKind.HUMAN
    assert call["actor_id"] == "analyst:test"
    assert str(snapshot.id) in call["arguments"]["evidence_refs"]

    invocation = asyncio.run(store.invocations.get(UUID(payload["invocation_id"])))
    assert invocation is not None
    assert invocation.actor_kind is ToolInvocationActorKind.HUMAN
    assert invocation.actor_id == "analyst:test"

    replay = client.post(path, json={"expected_version": 1}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["plan_version_id"] == payload["plan_version_id"]
    assert replay.json()["idempotent_replay"] is True
    versions = asyncio.run(store.plans.list_for_investigation(INVESTIGATION_ID))
    assert [version.version for version in versions] == [1]


def _seed_context_task(store: _Store, *, objective: str | None = None) -> InvestigationTaskV1:
    task = InvestigationTaskV1(
        id=uuid4(),
        investigation_id=INVESTIGATION_ID,
        task_key="lead-context",
        title="Analyser le chargeur",
        question="Quel chargeur produit le second étage ?",
        objective=objective or "Confirmer la chaîne d'infection depuis les octets observés.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test"),
        claim_refs=("S1P1",),
        created_at=NOW,
        updated_at=NOW,
    )
    asyncio.run(store.tasks.add(task))
    return task


def test_plan_request_with_context_task_links_invocation() -> None:
    client, store, factory = _client()
    _seed_bootstrap_snapshot(store)
    task = _seed_context_task(store)
    path = f"/api/investigations/{INVESTIGATION_ID}/plans/request"

    response = client.post(
        path,
        json={"expected_version": 1, "context_task_id": str(task.id)},
        headers={"Idempotency-Key": "plan-task-context"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["plan_version_id"] is not None
    call = factory.invocations.calls[0]
    assert call["context_task_id"] == task.id
    assert call["requester_actor_kind"] is ToolInvocationActorKind.HUMAN
    assert call["arguments"]["task_context"]["task_id"] == str(task.id)
    assert call["arguments"]["task_context"]["task_key"] == task.task_key
    invocation = asyncio.run(store.invocations.get(UUID(response.json()["invocation_id"])))
    assert invocation is not None
    assert invocation.context_task_id == task.id
    assert invocation.task_id is None and invocation.agent_session_id is None

    filtered = client.get(f"/api/investigations/{INVESTIGATION_ID}/consultations?task_id={task.id}")
    assert filtered.status_code == 200, filtered.text
    entries = filtered.json()["consultations"]
    assert [entry["context_task_id"] for entry in entries] == [str(task.id)]
    absent = client.get(f"/api/investigations/{INVESTIGATION_ID}/consultations?task_id={uuid4()}")
    assert absent.status_code == 200, absent.text
    assert absent.json()["consultations"] == []

    # An unknown task is refused before any advisor call.
    refused = client.post(
        path,
        json={"expected_version": 1, "context_task_id": str(uuid4())},
        headers={"Idempotency-Key": "plan-task-unknown"},
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["code"] == "task_not_found"
    assert len(factory.invocations.calls) == 1


def test_plan_request_omits_sensitive_task_context_with_warning() -> None:
    """F1: the plan request carries the sanitised context, never the credential."""
    client, store, factory = _client()
    _seed_bootstrap_snapshot(store)
    task = _seed_context_task(store, objective="api_key=ZZOPERATIONALSECRETZZ")
    path = f"/api/investigations/{INVESTIGATION_ID}/plans/request"

    response = client.post(
        path,
        json={"expected_version": 1, "context_task_id": str(task.id)},
        headers={"Idempotency-Key": "plan-task-context-sensitive"},
    )

    assert response.status_code == 200, response.text
    arguments = factory.invocations.calls[0]["arguments"]
    assert b"ZZOPERATIONALSECRETZZ" not in canonical_json_bytes(dict(arguments))
    assert b"ZZOPERATIONALSECRETZZ" not in canonical_json_bytes(arguments["task_context"])
    assert "objective" not in arguments["task_context"]
    assert arguments["task_context"]["task_key"] == task.task_key
    assert any(
        warning["code"] == "task_context_field_omitted_sensitive"
        and warning["field"] == "task_context.objective"
        for warning in arguments["task_context_warnings"]
    )
    invocation = asyncio.run(store.invocations.get(UUID(response.json()["invocation_id"])))
    assert invocation is not None
    assert b"ZZOPERATIONALSECRETZZ" not in canonical_json_bytes(
        dict(invocation.normalized_parameters)
    )


def test_plan_accept_materializes_tasks_once() -> None:
    client, store, _factory = _client()
    leads = (
        InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur"),
        InvestigationLeadV1(lead_id="lead-b", title="Cartographier le C2"),
    )
    version_id = asyncio.run(_create_plan(store, version=1, leads=leads))
    path = f"/api/investigations/{INVESTIGATION_ID}/plans/{version_id}/accept"
    headers = {"Idempotency-Key": "accept-plan-1"}

    accepted = client.post(path, json={"reason": "Plan accepté"}, headers=headers)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["idempotent_replay"] is False
    assert len(body["tasks"]) == 2
    task_ids = {task["task_id"] for task in body["tasks"]}
    assert len(task_ids) == 2

    tasks = asyncio.run(store.tasks.list_for_investigation(INVESTIGATION_ID))
    assert len(tasks) == 2
    assert {task.status for task in tasks} == {TaskStatus.READY}
    for task in tasks:
        assert task.created_by.kind is TaskActorKind.SYSTEM
        assert task.created_by.actor_id == f"plan_accept:{body['decision_id']}"
        events = asyncio.run(store.tasks.list_events(task.id))
        assert len(events) == 1
        assert events[0].idempotency_key == f"plan:{version_id}:{task.task_key}"
        assert events[0].analyst_decision_id == UUID(body["decision_id"])

    decisions = asyncio.run(store.decisions.list_for_investigation(INVESTIGATION_ID))
    assert len(decisions) == 1
    assert decisions[0].decision_type is AnalystDecisionType.PLAN_ACCEPT
    assert decisions[0].target_type is AnalystDecisionTargetType.PLAN_VERSION
    assert decisions[0].target_id == version_id
    assert decisions[0].actor_id == "analyst:test"

    replay = client.post(path, json={"reason": "Plan accepté"}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert {task["task_id"] for task in replay.json()["tasks"]} == task_ids
    assert len(asyncio.run(store.tasks.list_for_investigation(INVESTIGATION_ID))) == 2
    assert len(asyncio.run(store.decisions.list_for_investigation(INVESTIGATION_ID))) == 1


def test_plan_accept_materializes_dependencies() -> None:
    client, store, _factory = _client()
    leads = (
        InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur"),
        InvestigationLeadV1(
            lead_id="lead-b",
            title="Cartographier le C2",
            depends_on=("lead-a",),
        ),
    )
    version_id = asyncio.run(_create_plan(store, version=1, leads=leads))

    accepted = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_id}/accept",
        json={"reason": "Plan accepté"},
        headers={"Idempotency-Key": "accept-plan-deps"},
    )
    assert accepted.status_code == 200, accepted.text

    key_a = plan_task_key(INVESTIGATION_ID, leads[0])
    key_b = plan_task_key(INVESTIGATION_ID, leads[1])
    task_a = asyncio.run(store.tasks.get_by_task_key(INVESTIGATION_ID, key_a))
    task_b = asyncio.run(store.tasks.get_by_task_key(INVESTIGATION_ID, key_b))
    assert task_a is not None and task_a.status is TaskStatus.READY
    assert task_b is not None and task_b.status is TaskStatus.PROPOSED
    assert asyncio.run(store.tasks.list_dependencies(task_b.id)) == (task_a.id,)


def test_plan_dependency_cycle_rejected() -> None:
    client, store, _factory = _client()
    leads = (
        InvestigationLeadV1(lead_id="lead-a", title="Piste A", depends_on=("lead-b",)),
        InvestigationLeadV1(lead_id="lead-b", title="Piste B", depends_on=("lead-a",)),
    )
    version_id = asyncio.run(_create_plan(store, version=1, leads=leads))

    refused = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_id}/accept",
        json={"reason": "Plan accepté"},
        headers={"Idempotency-Key": "accept-cycle"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "plan_dependency_cycle"
    assert asyncio.run(store.tasks.list_for_investigation(INVESTIGATION_ID)) == ()
    assert asyncio.run(store.decisions.list_for_investigation(INVESTIGATION_ID)) == ()


def test_later_plan_version_never_rewrites_tasks() -> None:
    client, store, _factory = _client()
    lead_a = InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur")
    version_one = asyncio.run(_create_plan(store, version=1, leads=(lead_a,)))

    first = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_one}/accept",
        json={"reason": "Plan v1 accepté"},
        headers={"Idempotency-Key": "accept-v1"},
    )
    assert first.status_code == 200, first.text

    key_a = plan_task_key(INVESTIGATION_ID, lead_a)
    task_a = asyncio.run(store.tasks.get_by_task_key(INVESTIGATION_ID, key_a))
    assert task_a is not None
    events_before = asyncio.run(store.tasks.list_events(task_a.id))
    assert len(events_before) == 1

    lead_b = InvestigationLeadV1(
        lead_id="lead-b",
        title="Cartographier le C2",
        depends_on=("lead-a",),
    )
    version_two = asyncio.run(_create_plan(store, version=2, leads=(lead_a, lead_b)))
    second = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_two}/accept",
        json={"reason": "Plan v2 accepté"},
        headers={"Idempotency-Key": "accept-v2"},
    )
    assert second.status_code == 200, second.text

    unchanged = asyncio.run(store.tasks.get_by_task_key(INVESTIGATION_ID, key_a))
    assert unchanged == task_a
    assert unchanged is not None and unchanged.status is TaskStatus.READY
    assert unchanged.status_version == 1
    assert len(asyncio.run(store.tasks.list_events(task_a.id))) == 1
    tasks = asyncio.run(store.tasks.list_for_investigation(INVESTIGATION_ID))
    assert len(tasks) == 2
    key_b = plan_task_key(INVESTIGATION_ID, lead_b)
    task_b = asyncio.run(store.tasks.get_by_task_key(INVESTIGATION_ID, key_b))
    assert task_b is not None and task_b.status is TaskStatus.PROPOSED

    # The v1 route key replays its recorded acceptance even though the head is v2.
    replayed = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_one}/accept",
        json={"reason": "Plan v1 accepté"},
        headers={"Idempotency-Key": "accept-v1"},
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["idempotent_replay"] is True
    assert {task["task_id"] for task in replayed.json()["tasks"]} == {str(task_a.id)}


def test_plan_materialize_from_persisted_invocation_is_idempotent() -> None:
    client, store, _factory = _client()
    invocation, _response = asyncio.run(_seed_advisor_exchange(store))
    path = f"/api/investigations/{INVESTIGATION_ID}/plans"

    created = client.post(path, json={"source_invocation_id": str(invocation.id)})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["created"] is True
    assert body["lead_count"] == 1
    assert body["parse_status"] == "parsed"

    replay = client.post(path, json={"source_invocation_id": str(invocation.id)})
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["plan_version_id"] == body["plan_version_id"]
    versions = asyncio.run(store.plans.list_for_investigation(INVESTIGATION_ID))
    assert [version.version for version in versions] == [1]

    listed = client.get(f"/api/investigations/{INVESTIGATION_ID}/plans")
    assert listed.status_code == 200, listed.text
    assert listed.json()["current_version"] == 1
    assert len(listed.json()["plans"]) == 1
    detail = client.get(f"/api/investigations/{INVESTIGATION_ID}/plans/{body['plan_version_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["content"]["leads"][0]["title"] == "Vérifier le chargeur"


def test_plan_accept_refuses_stale_version() -> None:
    client, store, _factory = _client()
    lead_a = InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur")
    lead_b = InvestigationLeadV1(lead_id="lead-b", title="Cartographier le C2")
    version_one = asyncio.run(_create_plan(store, version=1, leads=(lead_a,)))
    asyncio.run(_create_plan(store, version=2, leads=(lead_a, lead_b)))

    refused = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/plans/{version_one}/accept",
        json={"reason": "Plan v1 accepté trop tard"},
        headers={"Idempotency-Key": "accept-stale"},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "stale_plan_version"
    assert asyncio.run(store.tasks.list_for_investigation(INVESTIGATION_ID)) == ()
    assert asyncio.run(store.decisions.list_for_investigation(INVESTIGATION_ID)) == ()


def test_ledger_route_lists_consultations_as_non_evidence() -> None:
    client, store, _factory = _client()
    asyncio.run(_seed_advisor_exchange(store))

    response = client.get(f"/api/investigations/{INVESTIGATION_ID}/consultations")

    assert response.status_code == 200, response.text
    consultations = response.json()["consultations"]
    assert len(consultations) == 1
    entry = consultations[0]
    assert entry["consultation_type"] == "plan_investigation"
    assert entry["parse_status"] == "partially_parsed"
    assert entry["is_evidence"] is False
    assert entry["usable"] is True


def test_plan_task_key_matches_the_e10_identity_formula() -> None:
    lead = InvestigationLeadV1(lead_id="lead-a", title="Analyser le chargeur")
    expected = (
        "lead-"
        + hashlib.sha256(str(INVESTIGATION_ID).encode("utf-8") + b"\0" + b"lead-a").hexdigest()[:24]
    )
    assert plan_task_key(INVESTIGATION_ID, lead) == expected

    content_lead = InvestigationLeadV1(
        title="Vérifier les octets",
        start_evidence_refs=("ref-b", "ref-a"),
        sample_refs=("sample-c",),
    )
    identity = canonical_json_bytes(
        {
            "start_evidence_refs": ["ref-a", "ref-b"],
            "sample_refs": ["sample-c"],
            "title": "Vérifier les octets",
        }
    )
    expected_content = (
        "lead-"
        + hashlib.sha256(str(INVESTIGATION_ID).encode("utf-8") + b"\0" + identity).hexdigest()[:24]
    )
    assert plan_task_key(INVESTIGATION_ID, content_lead) == expected_content
