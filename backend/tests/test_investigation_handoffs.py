"""Domain/application oracles for M24 investigation handoffs (ADR-0011 D5).

The five ADR-named oracles cover the canonical hash, per-task idempotency,
freshness diffs, the start gate and the enforced content exclusions.  A
harness of canonical in-memory ports exercises the service without a database;
the PostgreSQL-only append-only/idempotency race lives in
``tests/integration/test_investigation_handoffs_postgres.py``.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cti_app.application.analyst_input_pack import canonical_json_bytes as pack_canonical_bytes
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    HandoffIdempotencyConflictError,
    HandoffRefForbiddenError,
    HandoffTaskStaleError,
    InvestigationHandoffService,
    assert_startable,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationHandoffRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileRevisionRepository,
    InvestigationHandoffRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    ToolInvocationRepository,
    WorkspaceBindingRepository,
    WorkspaceFileRevisionRepository,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    HandoffContentRefused,
    HandoffWork,
    HandoffWorkItem,
    InvestigationHandoffV1,
    assert_handoff_content_allowed,
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
LATER = NOW + timedelta(minutes=5)
INVESTIGATION_ID = uuid4()
TASK_ID = uuid4()
SUBJECT_ID = uuid4()
WORKSPACE_ID = "ws-" + "1" * 32
NOTE_PATH = "notes/humaines/analyste.md"
HUMAN_ACTOR = HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test")
OPERATOR_ACTOR = HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id="codex:session-1")


class _InvestigationRepository:
    def __init__(self, investigation_id: UUID) -> None:
        self._investigation = SimpleNamespace(id=investigation_id)

    async def get(self, investigation_id: UUID) -> Any:
        return self._investigation if investigation_id == self._investigation.id else None


class _DecisionRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, AnalystDecision] = {}

    async def append(self, decision: AnalystDecision) -> None:
        self._items[decision.id] = decision

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[AnalystDecision, ...]:
        return tuple(
            decision
            for decision in self._items.values()
            if decision.investigation_id == investigation_id
        )


class _EvidenceRepository(InMemoryEvidenceObservationRepository):
    """Mutable evidence feed so the freshness oracle can change a stored hash."""

    def change_sha256(self, observation_id: UUID, sha256: str) -> None:
        observation = self._items[observation_id]
        self._items[observation_id] = replace(observation, sha256=sha256)


class _FakeUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.investigation_handoffs: InvestigationHandoffRepository = harness.handoffs
        self.investigation_tasks: InvestigationTaskRepository = harness.tasks
        self.analyst_investigations: AnalystInvestigationRepository = harness.investigations
        self.analyst_decisions: AnalystDecisionRepository = harness.decisions
        self.evidence_observations: EvidenceObservationRepository = harness.observations
        self.investigation_findings: FindingRepository = harness.findings
        self.tool_invocations: ToolInvocationRepository = harness.invocations
        self.investigation_plans: InvestigationPlanRepository = harness.plans
        self.workspace_bindings: WorkspaceBindingRepository = harness.bindings
        self.workspace_file_revisions: WorkspaceFileRevisionRepository = harness.revisions

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Harness:
    def __init__(self) -> None:
        self.handoffs = InMemoryInvestigationHandoffRepository()
        self.tasks = InMemoryInvestigationTaskRepository()
        self.observations = _EvidenceRepository()
        self.findings = InMemoryFindingRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.plans = InMemoryInvestigationPlanRepository()
        self.bindings = InMemoryWorkspaceBindingRepository()
        self.revisions = InMemoryWorkspaceFileRevisionRepository()
        self.decisions = _DecisionRepository()
        self.investigations = _InvestigationRepository(INVESTIGATION_ID)
        self.uow = _FakeUnitOfWork(self)
        self.service = InvestigationHandoffService(uow_factory=lambda: self.uow, clock=lambda: NOW)

    async def seed_task(self, **overrides: Any) -> InvestigationTaskV1:
        values: dict[str, Any] = {
            "id": TASK_ID,
            "investigation_id": INVESTIGATION_ID,
            "task_key": "lead-loader",
            "title": "Inspect the loader",
            "question": "Which network gate does the loader use?",
            "objective": "Identify the first-stage C2 gate.",
            "priority": TaskPriority.HIGH,
            "status": TaskStatus.READY,
            "status_version": 3,
            "result_verification_status": ResultVerificationStatus.UNVERIFIED,
            "created_by": TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            "created_at": NOW,
            "updated_at": NOW,
            "assigned_actor_kind": TaskActorKind.HUMAN,
            "assigned_actor_id": "analyst:test",
        }
        values.update(overrides)
        task = InvestigationTaskV1(**values)
        await self.tasks.add(task)
        return task

    async def add_observation(
        self, *, sha256: str = "b" * 64, **overrides: Any
    ) -> EvidenceObservation:
        values: dict[str, Any] = {
            "investigation_id": INVESTIGATION_ID,
            "kind": EvidenceKind.OTHER,
            "blob_id": uuid4(),
            "sha256": sha256,
            "source": "workspace_import",
            "source_version": "work-contribution.v1",
            "observed_at": NOW,
            "object_type": "sample",
            "object_id": "loader",
        }
        values.update(overrides)
        observation = EvidenceObservation(**values)
        return await self.observations.append_if_absent(observation)

    async def bind_workspace(self) -> WorkspaceBindingV1:
        binding = WorkspaceBindingV1(
            workspace_id=WORKSPACE_ID,
            investigation_id=INVESTIGATION_ID,
            subject_id=SUBJECT_ID,
            delivery_code="IR202611",
            article_slug="loader-article",
            synthesis_sha256="e" * 64,
            created_at=NOW,
        )
        stored, _created = await self.bindings.add_if_absent(binding)
        return stored

    async def add_note_revision(
        self,
        *,
        revision_id: str,
        sha256: str,
        observed_at: datetime,
        path: str = NOTE_PATH,
    ) -> WorkspaceFileRevisionV1:
        await self.bind_workspace()
        revision = WorkspaceFileRevisionV1(
            workspace_id=WORKSPACE_ID,
            revision_id=revision_id,
            path=path,
            sha256=sha256,
            size=128,
            mode="0644",
            mtime_ns=0,
            observed_at=observed_at,
            origin="filesystem",
        )
        stored, _created = await self.revisions.append_if_absent(revision)
        return stored

    async def add_consultation(self) -> tuple[ToolInvocation, EvidenceObservation]:
        observation = await self.add_observation(
            source="advisor.consult.response",
            source_version="advisor-consult.v1",
            object_type="advisor_response_received",
            object_id="advice",
        )
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key=f"handoff-consult-{uuid4()}",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={
                "consultation_type": "interpretation",
                "question": "Interpret the loader gate.",
            },
            purpose="Ask Bridge how to read the loader gate",
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=(observation.id,),
            created_at=NOW,
            updated_at=NOW,
        )
        stored = await self.invocations.add_if_absent(invocation)
        return stored, observation

    async def add_decision(self) -> AnalystDecision:
        decision = AnalystDecision(
            investigation_id=INVESTIGATION_ID,
            decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
            target_type=AnalystDecisionTargetType.TASK,
            target_id=TASK_ID,
            actor_id="analyst:test",
            reason="Accept the interim result for transfer",
            correlation_id=f"handoff-{uuid4()}",
            occurred_at=NOW,
        )
        await self.decisions.append(decision)
        return decision

    def request(self, **overrides: Any) -> HandoffCreateRequest:
        values: dict[str, Any] = {
            "task_id": TASK_ID,
            "to_actor": OPERATOR_ACTOR,
            "next_action": "Confirm the first-stage gate and report back.",
        }
        values.update(overrides)
        return HandoffCreateRequest(**values)


async def _create_sample_handoff(harness: _Harness, **request_overrides: Any):
    await harness.seed_task()
    return await harness.service.create(
        INVESTIGATION_ID,
        actor=HUMAN_ACTOR,
        idempotency_key="handoff-create",
        request=harness.request(**request_overrides),
    )


def _payload_with(**handoff_overrides: Any) -> dict[str, Any]:
    payload = InvestigationHandoffV1(
        handoff_id=str(uuid4()),
        investigation_id=str(INVESTIGATION_ID),
        task_id=str(TASK_ID),
        version=1,
        created_at=NOW,
        from_actor=HUMAN_ACTOR,
        to_actor=OPERATOR_ACTOR,
        task={
            "status": TaskStatus.READY,
            "status_version": 3,
            "question": "q",
            "objective": "o",
        },
        next_action="Continue the analysis.",
    ).to_payload()
    payload.update(handoff_overrides)
    return payload


def test_canonical_json_hash_is_stable() -> None:
    payload = _payload_with()
    reordered = dict(reversed(list(payload.items())))
    first = InvestigationHandoffV1.model_validate(payload)
    second = InvestigationHandoffV1.model_validate(reordered)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.content_sha256 == second.content_sha256
    assert first.content_sha256 == hashlib.sha256(first.canonical_bytes).hexdigest()
    # Byte-identical to the AnalystInputPackV1 canonicalisation this contract
    # is required to reuse.
    assert first.canonical_bytes == pack_canonical_bytes(payload)
    assert first.canonical_bytes == canonical_json_bytes(payload)
    assert InvestigationHandoffV1.from_payload(first.to_payload()) == first


async def test_handoff_idempotent_retry_returns_same_version() -> None:
    harness = _Harness()
    await harness.seed_task()
    request = harness.request()

    first = await harness.service.create(
        INVESTIGATION_ID, actor=HUMAN_ACTOR, idempotency_key="key-1", request=request
    )
    retried = await harness.service.create(
        INVESTIGATION_ID, actor=HUMAN_ACTOR, idempotency_key="key-1", request=request
    )

    assert retried == first
    assert first.handoff.version == 1
    assert len(await harness.handoffs.list_for_investigation(INVESTIGATION_ID)) == 1
    with pytest.raises(HandoffIdempotencyConflictError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=HUMAN_ACTOR,
            idempotency_key="key-1",
            request=request.model_copy(update={"next_action": "A different next action."}),
        )
    second = await harness.service.create(
        INVESTIGATION_ID, actor=HUMAN_ACTOR, idempotency_key="key-2", request=request
    )
    assert second.handoff.version == 2


async def test_freshness_diff_lists_changed_note_and_evidence() -> None:
    harness = _Harness()
    revision = await harness.add_note_revision(
        revision_id="rev-" + "a" * 32, sha256="a" * 64, observed_at=NOW
    )
    observation = await harness.add_observation(sha256="b" * 64)
    record = await _create_sample_handoff(
        harness,
        observation_ids=(observation.id,),
        file_revision_ids=(revision.revision_id,),
    )

    fresh = await harness.service.freshness(INVESTIGATION_ID, UUID(record.handoff.handoff_id))
    assert fresh.state == "fresh"
    assert fresh.diffs == ()

    await harness.add_note_revision(
        revision_id="rev-" + "c" * 32, sha256="c" * 64, observed_at=LATER
    )
    harness.observations.change_sha256(observation.id, "d" * 64)

    stale = await harness.service.freshness(INVESTIGATION_ID, UUID(record.handoff.handoff_id))
    assert stale.state == "stale"
    diffs = {(diff.kind.value, diff.id): diff for diff in stale.diffs}
    note_diff = diffs[("file", revision.revision_id)]
    assert note_diff.recorded == "a" * 64
    assert note_diff.current == "c" * 64
    evidence_diff = diffs[("observation", str(observation.id))]
    assert evidence_diff.recorded == "b" * 64
    assert evidence_diff.current == "d" * 64


async def test_changed_task_status_blocks_session_start() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    record = await _create_sample_handoff(harness)

    assert_startable(record.handoff, task)

    moved = replace(task, status_version=task.status_version + 1, updated_at=LATER)
    with pytest.raises(HandoffTaskStaleError) as status_error:
        assert_startable(record.handoff, moved)
    assert status_error.value.code == "handoff_task_stale"

    reassigned = replace(
        task,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id="codex:session-9",
        updated_at=LATER,
    )
    with pytest.raises(HandoffTaskStaleError) as assignment_error:
        assert_startable(record.handoff, reassigned)
    assert assignment_error.value.code == "handoff_task_stale"


@pytest.mark.parametrize(
    ("label", "update", "code"),
    (
        (
            "credentials",
            {"next_action": "Use api_key=SUPERSECRET123 to reach the console."},
            "handoff_secret_material_forbidden",
        ),
        (
            "signed-url",
            {
                "next_action": (
                    "Download from https://vtsamples.example/f?id=1&X-Amz-Signature=deadbeef"
                )
            },
            "handoff_signed_url_forbidden",
        ),
        (
            "transcript",
            {
                "work": HandoffWork(
                    attempts=(
                        HandoffWorkItem(
                            text=(
                                '{"role": "user", "content": "inspect"} '
                                '{"role": "assistant", "content": "done"}'
                            )
                        ),
                    )
                )
            },
            "handoff_transcript_forbidden",
        ),
        (
            "malware-bytes",
            {
                "work": HandoffWork(
                    established_results=(
                        HandoffWorkItem(
                            text=base64.b64encode(b"MZ" + b"\x90" * 4_000).decode("ascii")
                        ),
                    )
                )
            },
            "handoff_binary_content_forbidden",
        ),
        (
            "host-path",
            {"next_action": "Open /home/analyst/work/autowork/loader.bin directly."},
            "handoff_host_path_forbidden",
        ),
    ),
)
def test_handoff_refuses_credentials_signed_urls_transcripts_and_malware_bytes(
    label: str, update: dict[str, Any], code: str
) -> None:
    payload = _payload_with(**update)

    with pytest.raises(HandoffContentRefused) as direct:
        assert_handoff_content_allowed(payload)
    assert direct.value.code == code, label

    with pytest.raises(ValidationError, match=code):
        InvestigationHandoffV1.model_validate(payload)


def test_handoff_refuses_raw_log_and_transcript_fields_and_unbounded_text() -> None:
    for key, code in (
        ("logs", "handoff_secret_or_raw_material_forbidden"),
        ("raw_transcript", "handoff_secret_or_raw_material_forbidden"),
        ("sample_bytes", "handoff_secret_or_raw_material_forbidden"),
    ):
        with pytest.raises(HandoffContentRefused) as refused:
            assert_handoff_content_allowed({"work": {key: "anything"}})
        assert refused.value.code == code

    with pytest.raises(HandoffContentRefused) as unbounded:
        assert_handoff_content_allowed({"next_action": "log line\n" * 20_000})
    assert unbounded.value.code == "handoff_unbounded_log_forbidden"


async def test_advisory_observation_can_only_be_cited_as_a_consultation_ref() -> None:
    harness = _Harness()
    await harness.seed_task()
    invocation, advisory = await harness.add_consultation()

    with pytest.raises(HandoffRefForbiddenError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=HUMAN_ACTOR,
            idempotency_key="advisory-as-evidence",
            request=harness.request(observation_ids=(advisory.id,)),
        )

    record = await harness.service.create(
        INVESTIGATION_ID,
        actor=HUMAN_ACTOR,
        idempotency_key="advisory-as-consultation",
        request=harness.request(consultation_invocation_ids=(invocation.id,)),
    )
    refs = record.handoff.refs
    assert refs.observations == ()
    assert len(refs.consultations) == 1
    assert refs.consultations[0].invocation_id == str(invocation.id)
    assert refs.consultations[0].consultation_type == "interpretation"
    assert refs.consultations[0].output_sha256 == advisory.sha256


async def test_handoff_refs_must_belong_to_the_same_investigation() -> None:
    from cti_app.application.investigation_handoffs import HandoffRefUnresolvedError

    harness = _Harness()
    await harness.seed_task()
    foreign = await harness.observations.append_if_absent(
        EvidenceObservation(
            investigation_id=uuid4(),
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256="f" * 64,
            source="workspace_import",
            source_version="work-contribution.v1",
            observed_at=NOW,
            object_type="sample",
            object_id="foreign",
        )
    )
    with pytest.raises(HandoffRefUnresolvedError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=HUMAN_ACTOR,
            idempotency_key="foreign-ref",
            request=harness.request(observation_ids=(foreign.id,)),
        )


async def test_handoff_decision_and_finding_refs_resolve_and_are_fresh() -> None:
    from cti_app.application.investigation_handoffs import HandoffRefUnresolvedError

    harness = _Harness()
    await harness.seed_task()
    decision = await harness.add_decision()
    record = await harness.service.create(
        INVESTIGATION_ID,
        actor=HUMAN_ACTOR,
        idempotency_key="decision-ref",
        request=harness.request(decision_ids=(decision.id,)),
    )
    assert record.handoff.refs.decisions[0].analyst_decision_id == str(decision.id)
    report = await harness.service.freshness(INVESTIGATION_ID, UUID(record.handoff.handoff_id))
    assert report.state == "fresh"

    with pytest.raises(HandoffRefUnresolvedError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=HUMAN_ACTOR,
            idempotency_key="missing-decision",
            request=harness.request(decision_ids=(uuid4(),)),
        )


async def test_work_item_refs_are_verified() -> None:
    from cti_app.application.investigation_handoffs import HandoffRefUnresolvedError
    from cti_app.domain.investigation_handoff import HandoffRef, HandoffRefKind

    harness = _Harness()
    await harness.seed_task()
    observation = await harness.add_observation()
    record = await harness.service.create(
        INVESTIGATION_ID,
        actor=HUMAN_ACTOR,
        idempotency_key="work-ref",
        request=harness.request(
            work=HandoffWork(
                hypotheses=(
                    HandoffWorkItem(
                        text="The gate is resolved on demand.",
                        refs=(HandoffRef(kind=HandoffRefKind.OBSERVATION, id=str(observation.id)),),
                    ),
                )
            )
        ),
    )
    assert len(record.handoff.work.hypotheses) == 1

    with pytest.raises(HandoffRefUnresolvedError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=HUMAN_ACTOR,
            idempotency_key="work-ref-missing",
            request=harness.request(
                work=HandoffWork(
                    attempts=(
                        HandoffWorkItem(
                            text="Blocked attempt.",
                            refs=(HandoffRef(kind=HandoffRefKind.OBSERVATION, id=str(uuid4())),),
                        ),
                    )
                )
            ),
        )


async def test_operator_handoff_requires_task_ownership() -> None:
    from cti_app.application.investigation_handoffs import HandoffForbiddenError

    harness = _Harness()
    await harness.seed_task()
    with pytest.raises(HandoffForbiddenError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=OPERATOR_ACTOR,
            idempotency_key="operator-not-owner",
            request=harness.request(),
        )

    # Seed a dedicated task the operator owns; the fake has no assignment
    # primitive and the production rule under test is ownership, not CAS.
    operator_task_id = uuid4()
    operator_task = InvestigationTaskV1(
        id=operator_task_id,
        investigation_id=INVESTIGATION_ID,
        task_key="lead-operator",
        title="Operator task",
        question="q",
        objective="o",
        priority=TaskPriority.NORMAL,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=NOW,
        updated_at=NOW,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=OPERATOR_ACTOR.actor_id,
    )
    await harness.tasks.add(operator_task)
    record = await harness.service.create(
        INVESTIGATION_ID,
        actor=OPERATOR_ACTOR,
        idempotency_key="operator-owner",
        request=harness.request(task_id=operator_task_id),
    )
    assert record.handoff.from_actor.kind is HandoffActorKind.OPERATOR


async def test_handoff_api_serves_verified_humans_and_leaves_operator_seam() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cti_app.api.investigation_handoffs import router
    from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider

    harness = _Harness()
    await harness.seed_task()
    app = FastAPI()
    app.include_router(router)
    app.state.uow_factory = lambda: harness.uow
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    body = harness.request().model_dump(mode="json")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/investigations/{INVESTIGATION_ID}/handoffs",
            json=body,
            headers={"Idempotency-Key": "api-key"},
        )
        assert created.status_code == 200, created.text
        handoff_id = created.json()["handoff_id"]
        listed = await client.get(f"/api/investigations/{INVESTIGATION_ID}/handoffs")
        assert listed.status_code == 200, listed.text
        assert [item["handoff_id"] for item in listed.json()] == [handoff_id]
        fetched = await client.get(f"/api/investigations/{INVESTIGATION_ID}/handoffs/{handoff_id}")
        assert fetched.status_code == 200, fetched.text
        fresh = await client.get(
            f"/api/investigations/{INVESTIGATION_ID}/handoffs/{handoff_id}/freshness"
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["state"] == "fresh"

    class _OperatorClient:
        async def current(self) -> Identity:
            return Identity(
                actor_id="operator:claude-client",
                scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
            )

    app.state.identity_provider = _OperatorClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/api/investigations/{INVESTIGATION_ID}/handoffs",
            json=body,
            headers={"Idempotency-Key": "api-key-operator"},
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["code"] == "agent_session_required"
