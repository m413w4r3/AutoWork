"""API contract for M24 investigation coordination creation (ADR-0011 D4/E3).

Only the start/mode surface belongs to lot 04A1; the plan, task and timeline
routes arrive with their own lots.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI

from cti_app.api import investigation as investigation_api
from cti_app.api.investigation_tasks import router as investigation_tasks_router
from cti_app.application.agent_session_verifier import (
    AgentSessionBinding,
    InMemoryAgentSessionVerifier,
)
from cti_app.application.identity import (
    Identity,
    IdentityScope,
    LocalIdentityProvider,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import AnalystInvestigationStatus, ProseAuthority
from tests.test_investigation_api import (
    INVESTIGATION_ID,
    RUN_ID,
    SUBJECT_ID,
    SYNTHESIS_ID,
    _client,
    _CompositionFactory,
    _SnapshotPort,
    _start_body,
    _Store,
    _SyncASGIClient,
)


def test_bridge_only_bridge_led_creation_refused() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "bridge_led",
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert INVESTIGATION_ID not in store.states.rows


def test_workshop_creation_refuses_bridge_led_without_creating_the_row() -> None:
    client, store = _client()
    store.investigations.clear()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "bridge_led",
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert store.investigations == {}
    assert not store.states.rows


def test_workshop_creation_never_infers_operator_led_from_the_profile() -> None:
    client, store = _client()
    store.investigations.clear()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "creation_profile": "m24_workshop"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert store.investigations == {}


def test_bridge_only_operator_led_creation_succeeds() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "operator_led",
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "operator_led"


def test_legacy_request_cannot_reensure_an_existing_bridge_only_investigation() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "operator_led"},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "prose_authority_conflict"
    assert INVESTIGATION_ID not in store.states.rows


def test_legacy_investigation_keeps_bridge_led_behaviour() -> None:
    client, _store = _client()

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "bridge_led"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "bridge_led"


def test_workshop_creation_requests_bridge_only_from_the_handoff(monkeypatch) -> None:
    store = _Store()
    store.investigations.clear()
    store.input_snapshots[RUN_ID] = SimpleNamespace(subject_tlp=TLP.AMBER, core_sources=())
    payload = {
        "subject_id": str(SUBJECT_ID),
        "title": "Verified synthesis for the workshop",
    }
    synthesis_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    captured: dict[str, Any] = {}

    class _ArtifactStore:
        async def read_json(self, _blob_id: UUID) -> dict[str, Any]:
            return payload

        @staticmethod
        def canonical_json_bytes(value: dict[str, Any]) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

    class _Handoff:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_for_verified_synthesis(self, *, run, synthesis, policy, **kwargs):
            assert run.id == RUN_ID and synthesis.id == SYNTHESIS_ID
            assert policy.tlp == TLP.AMBER.value
            captured["prose_authority"] = kwargs.get("prose_authority")
            store.investigations[INVESTIGATION_ID] = SimpleNamespace(
                id=INVESTIGATION_ID,
                subject_id=SUBJECT_ID,
                production_run_id=RUN_ID,
                synthesis_artifact_id=SYNTHESIS_ID,
                budget=SimpleNamespace(max_cycles=4),
                status=AnalystInvestigationStatus.QUEUED,
                version=1,
                updated_at=datetime.now(UTC),
                prose_authority=kwargs.get("prose_authority"),
            )
            return SimpleNamespace(investigation_id=INVESTIGATION_ID)

    monkeypatch.setattr(investigation_api, "AnalystPostSynthesisService", _Handoff)
    snapshot_port = _SnapshotPort()
    snapshot_port.sha256 = synthesis_sha256
    factory = _CompositionFactory(snapshot_port)
    factory.artifact_store = _ArtifactStore()
    factory.settings = SimpleNamespace(
        investigation_max_cycles=4,
        investigation_max_pivot_runs=0,
        investigation_max_hits_acquired=0,
        investigation_max_new_samples=0,
        investigation_max_vt_read_units=0,
    )
    client, store = _client(store=store, factory=factory)
    body = {
        **_start_body(),
        "synthesis_sha256": synthesis_sha256,
        "creation_profile": "m24_workshop",
        "execution_mode": "operator_led",
    }

    response = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=body)

    assert response.status_code == 200, response.text
    assert captured["prose_authority"] is ProseAuthority.BRIDGE_ONLY
    assert response.json()["investigation"]["execution_mode"] == "operator_led"


def test_creation_profile_validates_unknown_values() -> None:
    client, store = _client()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "creation_profile": "m24_anything"},
    )

    assert refused.status_code == 422, refused.text
    assert INVESTIGATION_ID not in store.states.rows


# ---------------------------------------------------------------------------
# M24-04B3 — task routes, typed actor gates and the operator session binding.
# ---------------------------------------------------------------------------

TASK_INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000902")
TASK_SESSION_ID = UUID("00000000-0000-0000-0000-000000000903")
TASK_OPERATOR_ID = "operator:test"
TASK_COMMANDS = (
    "assign",
    "start",
    "heartbeat",
    "request_input",
    "submit",
    "accept_result",
    "request_changes",
    "block",
    "unblock",
    "resume",
    "cancel",
    "fail",
    "release",
)


class _DecisionRows:
    def __init__(self) -> None:
        self.rows: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> list[AnalystDecision]:
        return [row for row in self.rows if row.investigation_id == investigation_id]


class _InvestigationRows:
    def __init__(self) -> None:
        self.rows = {TASK_INVESTIGATION_ID: object()}

    async def get(self, investigation_id: UUID) -> object | None:
        return self.rows.get(investigation_id)


class _TaskUnitOfWork:
    def __init__(self) -> None:
        self.investigation_tasks = InMemoryInvestigationTaskRepository()
        self.analyst_decisions = _DecisionRows()
        self.tool_invocations = InMemoryToolInvocationRepository()
        self.evidence_observations = InMemoryEvidenceObservationRepository()
        self.investigation_plans = InMemoryInvestigationPlanRepository()
        self.analyst_investigations = _InvestigationRows()

    async def __aenter__(self) -> _TaskUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _OperatorClientIdentity:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:operator-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _UnverifiedHumanIdentity:
    mode = "unverified"
    verified = False

    async def current(self) -> Identity:
        return Identity(actor_id="analyst:unverified")


class _TaskClient:
    def __init__(
        self,
        *,
        verifier: InMemoryAgentSessionVerifier | None = None,
        human: bool = True,
    ) -> None:
        self.uow = _TaskUnitOfWork()
        self.verifier = verifier
        self.app = FastAPI()
        self.app.include_router(investigation_tasks_router)
        self.app.state.uow_factory = lambda: self.uow
        self.app.state.settings = Settings(_env_file=None)
        self.app.state.identity_provider = (
            LocalIdentityProvider("analyst:test") if human else _OperatorClientIdentity()
        )
        if verifier is not None:
            self.app.state.agent_session_verifier = verifier
        self.client = _SyncASGIClient(self.app)

    def as_operator(self) -> None:
        self.app.state.identity_provider = _OperatorClientIdentity()

    def as_human(self) -> None:
        self.app.state.identity_provider = LocalIdentityProvider("analyst:test")


def _seed_task(
    uow: _TaskUnitOfWork,
    *,
    status: TaskStatus = TaskStatus.READY,
    task_key: str = "lead-1",
    assigned_actor_kind: TaskActorKind | None = None,
    assigned_actor_id: str | None = None,
) -> InvestigationTaskV1:
    task = InvestigationTaskV1(
        investigation_id=TASK_INVESTIGATION_ID,
        task_key=task_key,
        title="Inspect the loader",
        question="Which gate contacts the C2?",
        objective="Identify the second-stage downloader.",
        priority=TaskPriority.NORMAL,
        status=status,
        status_version=1,
        result_verification_status=(
            ResultVerificationStatus.PENDING_REVIEW
            if status is TaskStatus.AWAITING_REVIEW
            else ResultVerificationStatus.UNVERIFIED
        ),
        assigned_actor_kind=assigned_actor_kind,
        assigned_actor_id=assigned_actor_id,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    asyncio.run(uow.investigation_tasks.add(task))
    return task


def _binding(task_id: UUID, *, lease_current: bool = True) -> AgentSessionBinding:
    return AgentSessionBinding(
        session_id=TASK_SESSION_ID,
        investigation_id=TASK_INVESTIGATION_ID,
        task_id=task_id,
        operator_id=TASK_OPERATOR_ID,
        lease_current=lease_current,
    )


def _operator_headers() -> dict[str, str]:
    """The three D7 headers; the in-memory fake ignores the signature value."""
    return {
        "X-AutoWork-Agent-Session": str(TASK_SESSION_ID),
        "X-AutoWork-Agent-Timestamp": str(int(datetime.now(UTC).timestamp())),
        "X-AutoWork-Agent-Signature": "v1=" + "a" * 64,
    }


def _command_body(command: str) -> dict[str, object]:
    # One body carrying every optional command field stays valid for all
    # thirteen commands, so the actor gate is what the matrix measures.
    return {
        "command": command,
        "expected_status_version": 1,
        "target": {"actor_kind": "human", "actor_id": "analyst:test"},
        "lease_fence": 1,
        "reason": "operator gate probe",
        "blocked_reason": "capability",
        "result_refs": [],
    }


def test_agent_session_binding_header_required() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier, human=False)
    task = _seed_task(app.uow)
    verifier.bind(_binding(task.id))

    missing = app.client.get(f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks")
    assert missing.status_code == 403, missing.text
    assert missing.json()["detail"]["code"] == "agent_session_required"

    malformed = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        headers={"X-AutoWork-Agent-Session": "not-a-session"},
    )
    assert malformed.status_code == 403
    assert malformed.json()["detail"]["code"] == "agent_session_required"

    # No server key is configured: D7 is fail-closed with the typed 503.
    fail_closed = _TaskClient(human=False)
    _seed_task(fail_closed.uow)
    unverified = fail_closed.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        headers=_operator_headers(),
    )
    assert unverified.status_code == 503, unverified.text
    assert unverified.json()["detail"]["code"] == "agent_sessions_unavailable"

    accepted = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        headers=_operator_headers(),
    )
    assert accepted.status_code == 200, accepted.text


def test_operator_client_token_alone_refused_on_every_command_route() -> None:
    app = _TaskClient(human=False)
    task = _seed_task(app.uow)
    url = f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task.id}/commands"

    for command in TASK_COMMANDS:
        refused = app.client.post(
            url,
            json=_command_body(command),
            headers={"Idempotency-Key": f"operator-alone-{command}"},
        )
        assert refused.status_code == 403, (command, refused.text)
        assert refused.json()["detail"]["code"] == "agent_session_required", command

    # A session the fail-closed default cannot verify is refused as well.
    fail_closed = _TaskClient(human=False)
    fail_closed_task = _seed_task(fail_closed.uow)
    fail_closed_url = (
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{fail_closed_task.id}/commands"
    )
    for command in TASK_COMMANDS:
        refused = fail_closed.client.post(
            fail_closed_url,
            json=_command_body(command),
            headers={
                "Idempotency-Key": f"operator-unverified-{command}",
                **_operator_headers(),
            },
        )
        assert refused.status_code == 503, (command, refused.text)
        assert refused.json()["detail"]["code"] == "agent_sessions_unavailable", command


def test_operator_cannot_assign_or_accept_tasks() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier, human=False)
    ready = _seed_task(app.uow)
    awaiting = _seed_task(
        app.uow,
        status=TaskStatus.AWAITING_REVIEW,
        task_key="lead-2",
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=TASK_OPERATOR_ID,
    )
    verifier.bind(_binding(ready.id))
    assign = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{ready.id}/commands",
        json={**_command_body("assign"), "expected_status_version": ready.status_version},
        headers={"Idempotency-Key": "operator-assign", **_operator_headers()},
    )
    assert assign.status_code == 403, assign.text
    assert assign.json()["detail"]["code"] == "task_actor_not_allowed"
    stored = asyncio.run(app.uow.investigation_tasks.get(ready.id))
    assert stored is not None
    assert stored.status is TaskStatus.READY
    assert stored.status_version == ready.status_version

    verifier.bind(_binding(awaiting.id))
    accept = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{awaiting.id}/commands",
        json={
            **_command_body("accept_result"),
            "expected_status_version": awaiting.status_version,
        },
        headers={"Idempotency-Key": "operator-accept", **_operator_headers()},
    )
    assert accept.status_code == 403, accept.text
    assert accept.json()["detail"]["code"] == "task_actor_not_allowed"
    assert app.uow.analyst_decisions.rows == []
    stored = asyncio.run(app.uow.investigation_tasks.get(awaiting.id))
    assert stored is not None
    assert stored.status is TaskStatus.AWAITING_REVIEW


def test_acceptance_requires_verified_human() -> None:
    app = _TaskClient(human=False)
    app.app.state.identity_provider = _UnverifiedHumanIdentity()
    awaiting = _seed_task(
        app.uow,
        status=TaskStatus.AWAITING_REVIEW,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=TASK_OPERATOR_ID,
    )

    refused = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{awaiting.id}/commands",
        json={
            **_command_body("accept_result"),
            "expected_status_version": awaiting.status_version,
        },
        headers={"Idempotency-Key": "unverified-human-accept"},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "verified_human_operator_required"
    assert app.uow.analyst_decisions.rows == []


def test_human_and_operator_complete_a_task_through_the_api() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier)
    created = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        json={"task_key": "lead-1", "title": "Inspect the loader"},
        headers={"Idempotency-Key": "create-1"},
    )
    assert created.status_code == 200, created.text
    task_id = UUID(created.json()["task"]["id"])
    assert created.json()["task"]["status"] == "proposed"

    promoted = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={"command": "unblock", "expected_status_version": 1},
        headers={"Idempotency-Key": "promote-1"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["task"]["status"] == "ready"

    assigned = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={
            "command": "assign",
            "expected_status_version": 2,
            "target": {"actor_kind": "operator", "actor_id": TASK_OPERATOR_ID},
        },
        headers={"Idempotency-Key": "assign-1"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["task"]["assigned_actor_id"] == TASK_OPERATOR_ID

    verifier.bind(_binding(task_id))
    app.as_operator()
    started = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={"command": "start", "expected_status_version": 3},
        headers={"Idempotency-Key": "start-1", **_operator_headers()},
    )
    assert started.status_code == 200, started.text
    assert started.json()["task"]["status"] == "running"
    assert started.json()["task"]["lease_fence"] == 1
    assert started.json()["task"]["lease_session_id"] == str(TASK_SESSION_ID)
    fence = started.json()["task"]["lease_fence"]

    heartbeat = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={"command": "heartbeat", "expected_status_version": 4, "lease_fence": fence},
        headers={"Idempotency-Key": "heartbeat-1", **_operator_headers()},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["event"]["event_type"] == "lease_renewed"

    submitted = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={"command": "submit", "expected_status_version": 4, "lease_fence": fence},
        headers={"Idempotency-Key": "submit-1", **_operator_headers()},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["task"]["status"] == "awaiting_review"
    assert submitted.json()["task"]["lease_id"] is None

    app.as_human()
    accepted = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={
            "command": "accept_result",
            "expected_status_version": 5,
            "reason": "result verified",
        },
        headers={"Idempotency-Key": "accept-1"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["status"] == "completed"
    assert accepted.json()["task"]["result_verification_status"] == "accepted"
    event_id = accepted.json()["event"]["id"]
    assert len(app.uow.analyst_decisions.rows) == 1

    replayed = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/commands",
        json={
            "command": "accept_result",
            "expected_status_version": 5,
            "reason": "result verified",
        },
        headers={"Idempotency-Key": "accept-1"},
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True
    assert replayed.json()["event"]["id"] == event_id
    assert len(app.uow.analyst_decisions.rows) == 1

    first_page = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/events?limit=2"
    )
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["has_more"] is True
    second_page = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/events",
        params={"limit": 10, "cursor": first_page.json()["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    seen = [item["id"] for item in first_page.json()["items"] + second_page.json()["items"]]
    assert len(seen) == len(set(seen))
    assert event_id in seen

    invalid = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task_id}/events",
        params={"cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_event_cursor"


def test_operator_reads_are_scoped_to_its_tasks() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier, human=False)
    own = _seed_task(
        app.uow,
        status=TaskStatus.ASSIGNED,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=TASK_OPERATOR_ID,
    )
    other = _seed_task(app.uow, task_key="lead-2")
    verifier.bind(_binding(own.id))

    listed = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks", headers=_operator_headers()
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [str(own.id)]

    denied = app.client.get(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{other.id}",
        headers=_operator_headers(),
    )
    assert denied.status_code == 403, denied.text
    # D7 binds the session to one task: reading another task is refused before
    # the operator task-scope projection can even run.
    assert denied.json()["detail"]["code"] == "agent_session_mismatch"


def test_task_api_requires_idempotency_and_expected_version() -> None:
    app = _TaskClient()
    task = _seed_task(app.uow)

    no_key = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        json={"task_key": "lead-2", "title": "Second lead"},
    )
    assert no_key.status_code == 422

    no_command_key = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task.id}/commands",
        json={"command": "release", "expected_status_version": 1},
    )
    assert no_command_key.status_code == 422

    no_version = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{task.id}/commands",
        json={"command": "release"},
        headers={"Idempotency-Key": "missing-version"},
    )
    assert no_version.status_code == 422


def test_operator_session_is_bound_to_its_task() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier, human=False)
    own = _seed_task(
        app.uow,
        status=TaskStatus.ASSIGNED,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=TASK_OPERATOR_ID,
    )
    other = _seed_task(app.uow, task_key="lead-2")
    verifier.bind(_binding(own.id))

    wrong_task = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks/{other.id}/commands",
        json={"command": "release", "expected_status_version": 1},
        headers={"Idempotency-Key": "wrong-task", **_operator_headers()},
    )
    assert wrong_task.status_code == 403, wrong_task.text
    assert wrong_task.json()["detail"]["code"] == "agent_session_mismatch"


def test_operator_creates_only_its_own_proposed_subtask() -> None:
    verifier = InMemoryAgentSessionVerifier()
    app = _TaskClient(verifier=verifier, human=False)
    parent = _seed_task(
        app.uow,
        status=TaskStatus.ASSIGNED,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=TASK_OPERATOR_ID,
    )
    verifier.bind(_binding(parent.id))

    top_level = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        json={"task_key": "lead-top", "title": "Top level"},
        headers={"Idempotency-Key": "top-level", **_operator_headers()},
    )
    assert top_level.status_code == 403, top_level.text
    assert top_level.json()["detail"]["code"] == "agent_session_mismatch"

    subtask = app.client.post(
        f"/api/investigations/{TASK_INVESTIGATION_ID}/tasks",
        json={
            "task_key": "lead-child",
            "title": "Inspect the second stage",
            "parent_task_id": str(parent.id),
        },
        headers={"Idempotency-Key": "subtask", **_operator_headers()},
    )
    assert subtask.status_code == 200, subtask.text
    body = subtask.json()["task"]
    assert body["status"] == "proposed"
    assert body["parent_task_id"] == str(parent.id)
    assert body["created_by"]["actor_kind"] == "operator"
    assert body["created_by"]["actor_id"] == TASK_OPERATOR_ID


# ---------------------------------------------------------------------------
# M24-05B2 — the legacy operator tool route on a bridge_only investigation.
# ---------------------------------------------------------------------------


class _LegacyToolInvocations:
    """Records the invocation the legacy route would perform."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            investigation_id=INVESTIGATION_ID,
            working_state_version=1,
            working_state_sha256="b" * 64,
            error_code=None,
            job_id=None,
            idempotent_replay=False,
            output={},
            invocation=SimpleNamespace(
                id=uuid4(),
                tool_name=kwargs["tool_name"],
                schema_version=kwargs["schema_version"],
                status=ToolInvocationStatus.SUCCEEDED,
                version=1,
                response_refs=(),
            ),
        )


class _LegacyToolFactory:
    def __init__(self) -> None:
        self.invocations = _LegacyToolInvocations()

    def build(self, _uow: Any, _investigation_id: UUID, *, operator_mode: bool = False) -> Any:
        del operator_mode
        return SimpleNamespace(operator_tool_invocations=self.invocations)


def _bridge_only_legacy_client() -> tuple[Any, _LegacyToolFactory]:
    store = _Store()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY
    factory = _LegacyToolFactory()
    client, _store = _client(store=store, factory=factory)
    client.app.state.identity_provider = _OperatorClientIdentity()
    return client, factory


def test_legacy_opinion_route_requires_session_for_bridge_only() -> None:
    client, factory = _bridge_only_legacy_client()
    body = {
        "schema_version": "1",
        "arguments": {"query": "synthetic"},
        "purpose": "legacy operator tool probe",
        "expected_version": 1,
    }

    token_alone = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/operator/tools/opinion.revise",
        json=body,
        headers={"Idempotency-Key": "legacy-token-alone"},
    )
    assert token_alone.status_code == 403, token_alone.text
    assert token_alone.json()["detail"]["code"] == "agent_session_required"
    assert factory.invocations.calls == []

    # A signed session passes the D7 gate; D4 still owns the prose refusal.
    client.app.state.agent_session_verifier = InMemoryAgentSessionVerifier(
        [
            AgentSessionBinding(
                session_id=TASK_SESSION_ID,
                investigation_id=INVESTIGATION_ID,
                task_id=uuid4(),
                operator_id=TASK_OPERATOR_ID,
            )
        ]
    )
    signed = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/operator/tools/opinion.revise",
        json=body,
        headers={"Idempotency-Key": "legacy-signed", **_operator_headers()},
    )
    assert signed.status_code == 200, signed.text
    assert len(factory.invocations.calls) == 1
    assert factory.invocations.calls[0]["actor_id"] == "operator:operator-client"


def test_legacy_operator_route_is_unchanged_for_legacy_investigations() -> None:
    store = _Store()
    factory = _LegacyToolFactory()
    client, _store = _client(store=store, factory=factory)
    client.app.state.identity_provider = _OperatorClientIdentity()

    accepted = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/operator/tools/vt.file_report",
        json={
            "schema_version": "1",
            "arguments": {"query": "synthetic"},
            "purpose": "legacy behaviour probe",
            "expected_version": 1,
        },
        headers={"Idempotency-Key": "legacy-unchanged"},
    )

    assert accepted.status_code == 200, accepted.text
    assert len(factory.invocations.calls) == 1
