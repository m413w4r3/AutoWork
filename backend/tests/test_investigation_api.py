from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation import router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.jobs import DuplicateJobError
from cti_app.domain.classification import TLP
from cti_app.domain.investigation import (
    AgendaItem,
    EvidenceKind,
    EvidenceObservation,
    InvestigationAgenda,
    InvestigationEnvelope,
    SampleAssociation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import InvestigationWorkingState, StopReason
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import (
    AnalystInvestigationStatus,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

SHA256 = "a" * 64
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
RUN_ID = UUID("00000000-0000-0000-0000-000000000102")
SYNTHESIS_ID = UUID("00000000-0000-0000-0000-000000000103")
INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000104")


class _Rows:
    def __init__(self, rows: dict[UUID, Any]) -> None:
        self.rows = rows

    async def get(self, key: UUID):
        return self.rows.get(key)

    async def get_for_run(self, run_id: UUID):
        return next(
            (
                row
                for row in self.rows.values()
                if getattr(row, "production_run_id", None) == run_id
            ),
            None,
        )

    async def list_for_subject(self, subject_id: UUID):
        return [row for row in self.rows.values() if getattr(row, "subject_id", None) == subject_id]


class _States:
    def __init__(self) -> None:
        self.rows: dict[UUID, InvestigationWorkingState] = {}

    async def get(self, investigation_id: UUID):
        return self.rows.get(investigation_id)

    async def save_if_version(
        self, state: InvestigationWorkingState, *, expected_version: int | None
    ):
        current = self.rows.get(state.investigation_id)
        if (current.version if current else None) != expected_version:
            raise VersionConflictError("stale test state")
        self.rows[state.investigation_id] = state
        return state


class _Envelopes:
    def __init__(self) -> None:
        self.rows: dict[UUID, InvestigationEnvelope] = {}

    async def get(self, investigation_id: UUID):
        return self.rows.get(investigation_id)

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ):
        current = self.rows.get(envelope.investigation_id)
        if (current.version if current else None) != expected_version:
            raise VersionConflictError("stale test envelope")
        self.rows[envelope.investigation_id] = envelope
        return envelope


class _Jobs:
    def __init__(self) -> None:
        self.rows: dict[UUID, Job] = {}

    async def get(self, job_id: UUID):
        return self.rows.get(job_id)

    async def get_for_update(self, job_id: UUID):
        return self.rows.get(job_id)

    async def get_by_idempotency_key(self, key: str):
        return next((job for job in self.rows.values() if job.idempotency_key == key), None)

    async def add_if_absent(self, job: Job) -> bool:
        if await self.get_by_idempotency_key(job.idempotency_key):
            return False
        self.rows[job.id] = job
        return True

    async def save(self, job: Job) -> None:
        self.rows[job.id] = job

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID, *, kind: str | None = None
    ):
        return [
            job
            for job in sorted(self.rows.values(), key=lambda item: item.created_at, reverse=True)
            if job.aggregate_type == aggregate_type
            and job.aggregate_id == aggregate_id
            and (kind is None or job.kind == kind)
        ]


class _Uow:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.production_runs = _Rows(store.runs)
        self.production_artifacts = _Rows(store.artifacts)
        self.analyst_investigations = _Rows(store.investigations)
        self.investigation_states = store.states
        self.investigation_envelopes = store.envelopes
        self.jobs = store.jobs
        self.evidence_observations = _Items(store.observations)
        self.investigation_findings = _Items(store.findings)
        self.sample_associations = _Items(store.associations)
        self.analyst_decisions = _Decisions(store.decisions)
        self.analyst_opinions = _Opinions(store.opinions)
        self.tool_invocations = SimpleNamespace()
        self.production_input_snapshots = _InputSnapshots(store)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self) -> None:
        return None


class _Items:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    async def list_for_investigation(self, _investigation_id: UUID):
        return tuple(self.rows)

    async def get(self, identity: UUID):
        return next((item for item in self.rows if getattr(item, "id", None) == identity), None)

    async def append_version(self, item, *, expected_version: int):
        current = await self.get(item.id)
        if current is None or current.version != expected_version:
            raise VersionConflictError("stale test item")
        self.rows[self.rows.index(current)] = item
        return item


class _Decisions:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)

    async def append(self, decision) -> None:
        self.rows.append(decision)


class _Opinions:
    def __init__(self, rows: dict[UUID, object]) -> None:
        self.rows = rows

    async def get(self, opinion_id: UUID, *, version: int | None = None):
        del version
        return self.rows.get(opinion_id)


class _Store:
    def __init__(self) -> None:
        self.runs: dict[UUID, Any] = {RUN_ID: SimpleNamespace(id=RUN_ID, subject_id=SUBJECT_ID)}
        self.artifacts: dict[UUID, Any] = {
            SYNTHESIS_ID: SimpleNamespace(
                id=SYNTHESIS_ID,
                production_run_id=RUN_ID,
                subject_id=SUBJECT_ID,
                stage=ProductionArtifactStage.SYNTHESIS,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=uuid4(),
            )
        }
        self.investigations: dict[UUID, Any] = {
            INVESTIGATION_ID: SimpleNamespace(
                id=INVESTIGATION_ID,
                subject_id=SUBJECT_ID,
                production_run_id=RUN_ID,
                synthesis_artifact_id=SYNTHESIS_ID,
                budget=SimpleNamespace(
                    max_cycles=4,
                    max_pivot_runs=20,
                    max_hits_acquired=10,
                    max_new_samples=10,
                    max_vt_read_units=50,
                ),
                status=AnalystInvestigationStatus.QUEUED,
                updated_at=datetime.now(UTC),
            )
        }
        self.states = _States()
        self.envelopes = _Envelopes()
        self.jobs = _Jobs()
        self.observations: list[object] = []
        self.findings: list[object] = []
        self.associations: list[object] = []
        self.opinions: dict[UUID, object] = {}
        self.input_snapshots: dict[UUID, Any] = {}
        self.decisions: list[object] = []
        self.reconciliations: list[dict[str, object]] = []

    def uow(self) -> _Uow:
        return _Uow(self)


class _InputSnapshots:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def get_by_run(self, run_id: UUID):
        return self.store.input_snapshots.get(run_id)


class _SnapshotPort:
    def __init__(self) -> None:
        self.sha256 = SHA256

    async def resolve(self, synthesis_artifact_id: UUID):
        assert synthesis_artifact_id == SYNTHESIS_ID
        return SimpleNamespace(
            subject_id=SUBJECT_ID,
            production_run_id=RUN_ID,
            synthesis_sha256=self.sha256,
            tlp="TLP:AMBER",
            do_not_submit=True,
            external_llm_allowed=False,
        )

    async def get_claims(self, _investigation_id: UUID, claim_ids: tuple[str, ...] = ()):
        return ()


class _InvestigationService:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    async def start(self, *, run, synthesis, envelope, **_kwargs):
        assert run.id == RUN_ID and synthesis.id == SYNTHESIS_ID
        existing = await self.uow.investigation_states.get(INVESTIGATION_ID)
        if existing is not None:
            return existing
        assert envelope is None
        state = _working_state()
        await self.uow.investigation_states.save_if_version(state, expected_version=None)
        self.uow._store.investigations[INVESTIGATION_ID].status = AnalystInvestigationStatus.RUNNING
        return state

    async def resume(self, investigation_id: UUID):
        state = await self.uow.investigation_states.get(investigation_id)
        updated = state.resume()
        if updated is not state:
            await self.uow.investigation_states.save_if_version(
                updated, expected_version=state.version
            )
        self.uow._store.investigations[investigation_id].status = AnalystInvestigationStatus.RUNNING
        return updated

    async def interrupt(self, investigation_id: UUID):
        state = await self.uow.investigation_states.get(investigation_id)
        updated = state.conclude(StopReason.USER_INTERRUPT)
        if updated is not state:
            await self.uow.investigation_states.save_if_version(
                updated, expected_version=state.version
            )
        self.uow._store.investigations[
            investigation_id
        ].status = AnalystInvestigationStatus.CANCELLED
        return updated

    async def reconcile_invocation(
        self,
        investigation_id: UUID,
        invocation_id: UUID,
        *,
        outcome,
        actor: str,
        reason: str,
        response_refs: tuple[UUID, ...],
        expected_version: int,
        idempotency_key: str,
    ):
        self.uow._store.reconciliations.append(
            {
                "investigation_id": investigation_id,
                "invocation_id": invocation_id,
                "outcome": outcome.value,
                "actor": actor,
                "reason": reason,
                "response_refs": response_refs,
                "expected_version": expected_version,
                "idempotency_key": idempotency_key,
            }
        )
        return SimpleNamespace(
            id=invocation_id,
            status=ToolInvocationStatus.SUCCEEDED,
            version=expected_version + 1,
            response_refs=response_refs,
        )


class _CompositionFactory:
    def __init__(self, snapshot_port: _SnapshotPort) -> None:
        self.article_snapshots = snapshot_port
        self.store: _Store | None = None
        self.artifact_store: Any = None
        self.settings: Any = None
        self.settings = SimpleNamespace(
            investigation_max_cycles=12,
            investigation_max_vt_read_units=4,
            investigation_max_shodan_credits=4,
            investigation_max_hits_acquired=2,
        )

    def build(self, uow: _Uow, _investigation_id: UUID):
        return SimpleNamespace(
            service=_InvestigationService(uow),
            tool_dependencies=SimpleNamespace(article_snapshots=self.article_snapshots),
            available_tools=(
                "article.get_claims",
                "evidence.get",
                "graph.neighbors",
                "sample.get_feature_pack",
            ),
            unavailable_tools=(
                "acquisition",
                "ghidra",
                "local_commands",
                "rule_workbench",
                "shodan",
                "virustotal",
            ),
        )


class _JobService:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def submit_in_uow(self, uow: _Uow, **kwargs):
        job = Job(
            kind=kwargs["kind"],
            aggregate_type=kwargs["aggregate_type"],
            aggregate_id=kwargs["aggregate_id"],
            idempotency_key=kwargs["idempotency_key"],
            correlation_id=kwargs["correlation_id"],
            input_parameters=kwargs["input_parameters"],
            max_attempts=kwargs["max_attempts"],
        )
        if not await uow.jobs.add_if_absent(job):
            existing = await uow.jobs.get_by_idempotency_key(job.idempotency_key)
            raise DuplicateJobError(existing.id)
        return job

    async def cancel(self, job_id: UUID, *, actor_id: str):
        del actor_id
        job = self.store.jobs.rows[job_id]
        job.request_cancellation()
        self.store.jobs.rows[job_id] = job
        return job


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.job_ids.append(job_id)


class _SyncASGIClient:
    """Keep these synchronous tests portable across Starlette TestClient backends."""

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


def _working_state(*, stop_reason: StopReason | None = None) -> InvestigationWorkingState:
    return InvestigationWorkingState(
        investigation_id=INVESTIGATION_ID,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Verify the article claim", potential=0.9, rank=1),
                AgendaItem(question="Look for contradictory evidence", potential=0.7, rank=2),
            )
        ),
        stop_reason=stop_reason,
    )


def _client(*, store: _Store | None = None, factory: _CompositionFactory | None = None):
    store = store or _Store()
    factory = factory or _CompositionFactory(_SnapshotPort())
    factory.store = store
    app = FastAPI()
    app.include_router(router)
    app.state.uow_factory = store.uow
    app.state.investigation_composition_factory = factory
    app.state.job_service = _JobService(store)
    app.state.job_dispatcher = _Dispatcher()
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    return _SyncASGIClient(app), store


def _start_body() -> dict[str, str]:
    return {
        "production_run_id": str(RUN_ID),
        "synthesis_artifact_id": str(SYNTHESIS_ID),
        "synthesis_sha256": SHA256,
    }


def test_start_is_idempotent_without_manufacturing_an_approval_envelope() -> None:
    client, store = _client()
    first = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())
    second = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())

    assert first.status_code == second.status_code == 200
    first_body, second_body = first.json(), second.json()
    assert first_body["investigation"]["id"] == second_body["investigation"]["id"]
    assert first_body["job_id"] == second_body["job_id"]
    assert first_body["idempotent_replay"] is False
    assert second_body["idempotent_replay"] is True
    assert INVESTIGATION_ID not in store.envelopes.rows
    assert first_body["investigation"]["envelope_approved"] is False


def test_governance_routes_approve_extend_revoke_and_enforce_cas() -> None:
    client, store = _client()
    assert (
        client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body()).status_code
        == 200
    )
    proposal = client.get(f"/api/investigations/{INVESTIGATION_ID}/envelope")
    assert proposal.status_code == 200
    assert proposal.json()["approved"] is False
    assert proposal.json()["version"] == 0
    assert proposal.json()["approval_decision_id"] is None
    assert INVESTIGATION_ID not in store.envelopes.rows

    approval = {
        "allowed_providers": [],
        "budget_limits": {"bridge_turns": 12},
        "acquire_hits_allowed": False,
        "max_acquire_volume": 0,
        "disclosure": {
            "tlp": "amber",
            "do_not_submit": True,
            "external_llm_allowed": False,
        },
        "reason": "Approve a local-only investigation envelope.",
        "correlation_id": "g2-api-approve",
    }
    first = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/approve",
        json=approval,
        headers={"Idempotency-Key": "approve-once"},
    )
    replay = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/approve",
        json=approval,
        headers={"Idempotency-Key": "approve-once"},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["approved"] is True
    assert first.json()["approval_actor_id"] == "analyst:test"
    assert replay.json()["version"] == 1

    extension = {
        "expected_version": 1,
        "budget_limits": {"bridge_turns": 15},
        "reason": "Extend the bounded Bridge turn budget.",
        "correlation_id": "g2-api-extend",
    }
    extended = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/extend",
        json=extension,
        headers={"Idempotency-Key": "extend-once"},
    )
    assert extended.status_code == 200, extended.text
    assert extended.json()["version"] == 2
    assert extended.json()["budget"]["bridge_turns"]["limit"] == 15

    revocation = {
        "expected_version": 2,
        "reason": "Revoke external access after review.",
        "correlation_id": "g2-api-revoke",
    }
    revoked = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/revoke",
        json=revocation,
        headers={"Idempotency-Key": "revoke-once"},
    )
    replayed_revocation = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/revoke",
        json=revocation,
        headers={"Idempotency-Key": "revoke-once"},
    )
    assert revoked.status_code == replayed_revocation.status_code == 200
    assert revoked.json()["version"] == 3
    assert revoked.json()["allowed_providers"] == []
    assert revoked.json()["acquire_hits_allowed"] is False
    assert revoked.json()["disclosure"]["external_llm_allowed"] is False
    assert replayed_revocation.json()["version"] == 3
    assert [item.decision_type.value for item in store.decisions] == [
        "pivot_approve",
        "pivot_approve",
        "pivot_reject",
    ]
    stale = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/revoke",
        json={**revocation, "expected_version": 1, "correlation_id": "g2-stale"},
        headers={"Idempotency-Key": "revoke-stale"},
    )
    assert stale.status_code == 409


def test_qualification_route_records_human_g3_and_replays_without_a_second_decision() -> None:
    from cti_app.domain.investigation import SampleOrigin

    client, store = _client()
    assert (
        client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body()).status_code
        == 200
    )
    association = SampleAssociation(
        investigation_id=INVESTIGATION_ID,
        sample_sha256="b" * 64,
        origin=SampleOrigin.VT_PIVOT,
    )
    store.associations.append(association)
    body = {
        "expected_version": 1,
        "qualification": "confirmed",
        "reason": "The analyst reviewed the cited observation.",
        "evidence_refs": [],
        "correlation_id": "g3-api-confirm",
    }
    path = f"/api/investigations/{INVESTIGATION_ID}/samples/{association.id}/qualify"
    first = client.post(path, json=body, headers={"Idempotency-Key": "qualify-once"})
    replay = client.post(path, json=body, headers={"Idempotency-Key": "qualify-once"})
    assert first.status_code == replay.status_code == 200
    assert first.json()["association"]["qualification"] == "confirmed"
    change = first.json()["association"]["changes"][0]
    assert change["actor_kind"] == "human"
    assert change["actor_id"] == "analyst:test"
    assert replay.json()["idempotent_replay"] is True
    assert [item.decision_type.value for item in store.decisions] == ["member_validate"]
    samples = client.get(f"/api/investigations/{INVESTIGATION_ID}/samples")
    assert samples.status_code == 200
    assert samples.json()[0]["changes"][0]["actor_id"] == "analyst:test"


def test_invocation_reconciliation_route_uses_verified_actor_cas_and_idempotency_key() -> None:
    client, store = _client()
    invocation_id = uuid4()
    response = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/invocations/{invocation_id}/reconcile",
        json={
            "expected_version": 3,
            "outcome": "executed_with_result",
            "reason": "The persisted observations confirm the completed lookup.",
            "response_refs": [],
        },
        headers={"Idempotency-Key": "reconcile-tool-3"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "invocation_id": str(invocation_id),
        "status": "succeeded",
        "version": 4,
        "response_refs": [],
    }
    assert store.reconciliations == [
        {
            "investigation_id": INVESTIGATION_ID,
            "invocation_id": invocation_id,
            "outcome": "executed_with_result",
            "actor": "analyst:test",
            "reason": "The persisted observations confirm the completed lookup.",
            "response_refs": (),
            "expected_version": 3,
            "idempotency_key": "invocation:"
            + str(invocation_id)
            + ":idem:"
            + hashlib.sha256(b"reconcile-tool-3").hexdigest()[:32],
        }
    ]


def test_targeted_request_is_idempotent_and_requeues_a_paused_investigation() -> None:
    client, store = _client()
    started = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())
    start_job = store.jobs.rows[UUID(started.json()["job_id"])]
    start_job.status = JobStatus.SUCCEEDED
    stopped = store.states.rows[INVESTIGATION_ID].conclude(StopReason.AGENT_FINISHED)
    store.states.rows[INVESTIGATION_ID] = stopped
    store.investigations[INVESTIGATION_ID].status = AnalystInvestigationStatus.COMPLETED
    body = {
        "text": "Check whether the cited host contradicts the report.",
        "target": {"kind": "ioc", "value": "203.0.113.20"},
        "expected_version": stopped.version,
    }
    path = f"/api/investigations/{INVESTIGATION_ID}/requests"
    first = client.post(path, json=body, headers={"Idempotency-Key": "request-once"})
    replay = client.post(path, json=body, headers={"Idempotency-Key": "request-once"})
    assert first.status_code == replay.status_code == 200
    assert first.json()["request_id"] == replay.json()["request_id"]
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert len(store.jobs.rows) == 2
    assert (
        sum(
            action.startswith("user-request:")
            for action in store.states.rows[INVESTIGATION_ID].recent_actions
        )
        == 1
    )


def test_start_rejects_an_unverified_synthesis() -> None:
    client, store = _client()
    store.artifacts[SYNTHESIS_ID].status = ProductionArtifactStatus.NEEDS_REVIEW

    response = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "synthesis_not_verified"
    assert not store.states.rows
    assert not store.jobs.rows


def test_http_start_creates_a_missing_handoff_from_exact_verified_synthesis(monkeypatch) -> None:
    import cti_app.api.investigation as investigation_api

    store = _Store()
    store.investigations.clear()
    store.input_snapshots[RUN_ID] = SimpleNamespace(subject_tlp=TLP.AMBER, core_sources=())
    payload = {
        "subject_id": str(SUBJECT_ID),
        "title": "Verified synthesis from HTTP start",
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

    class _ArtifactStore:
        async def read_json(self, _blob_id: UUID):
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
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def ensure_for_verified_synthesis(self, *, run, synthesis, policy, **_kwargs):
            assert run.id == RUN_ID and synthesis.id == SYNTHESIS_ID
            assert policy.tlp == TLP.AMBER.value
            assert policy.external_llm_allowed is False
            store.investigations[INVESTIGATION_ID] = SimpleNamespace(
                id=INVESTIGATION_ID,
                subject_id=SUBJECT_ID,
                production_run_id=RUN_ID,
                synthesis_artifact_id=SYNTHESIS_ID,
                budget=SimpleNamespace(max_cycles=4),
                status=AnalystInvestigationStatus.QUEUED,
                updated_at=datetime.now(UTC),
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
    body = _start_body()
    body["synthesis_sha256"] = synthesis_sha256

    response = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["investigation"]["id"] == str(INVESTIGATION_ID)
    assert store.investigations[INVESTIGATION_ID].synthesis_artifact_id == SYNTHESIS_ID


def test_resume_and_interrupt_enforce_cas_and_idempotency() -> None:
    client, store = _client()
    started = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body()).json()
    start_job = store.jobs.rows[UUID(started["job_id"])]
    start_job.status = JobStatus.SUCCEEDED
    state = store.states.rows[INVESTIGATION_ID]
    stopped = state.conclude(StopReason.MODEL_UNAVAILABLE)
    store.states.rows[INVESTIGATION_ID] = stopped
    store.investigations[INVESTIGATION_ID].status = AnalystInvestigationStatus.AWAITING_REVIEW

    stale = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/resume",
        json={"expected_version": 1},
        headers={"Idempotency-Key": "resume-stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_investigation_version"

    payload = {"expected_version": stopped.version}
    first = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/resume",
        json=payload,
        headers={"Idempotency-Key": "resume-once"},
    )
    second = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/resume",
        json=payload,
        headers={"Idempotency-Key": "resume-once"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True

    resumed_state = store.states.rows[INVESTIGATION_ID]
    resume_job = store.jobs.rows[UUID(first.json()["job_id"])]
    resume_job.status = JobStatus.SUCCEEDED
    interrupted = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/interrupt",
        json={"expected_version": resumed_state.version},
        headers={"Idempotency-Key": "interrupt-once"},
    )
    repeated_interrupt = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/interrupt",
        json={"expected_version": resumed_state.version},
        headers={"Idempotency-Key": "interrupt-once"},
    )
    assert interrupted.status_code == repeated_interrupt.status_code == 200
    assert interrupted.json()["investigation"]["stop_reason"] == "user_interrupt"
    assert repeated_interrupt.json()["idempotent_replay"] is True


def test_read_projection_exposes_metadata_without_blobs_or_raw_outputs() -> None:
    client, store = _client()
    client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())
    blob_id = uuid4()
    observation = EvidenceObservation(
        investigation_id=INVESTIGATION_ID,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=SHA256,
        source="simulated-tool",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="report",
        object_id="safe-object",
        limitations=("coverage is partial",),
    )
    store.observations.append(observation)
    response = client.get(f"/api/investigations/{INVESTIGATION_ID}")

    assert response.status_code == 200
    body = response.json()
    encoded = response.text
    assert body["observations"][0]["id"] == str(observation.id)
    assert body["agenda"][0]["question"] == "Verify the article claim"
    assert body["opinion_draft"] is None
    assert str(blob_id) not in encoded
    assert "raw-provider-secret" not in encoded
    assert "api_key" not in encoded
