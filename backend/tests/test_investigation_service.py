import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.analyst_handoff import AnalystHandoffPolicy
from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_contracts import ArticleClaimV1, ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_service import (
    InvestigationService,
    InvocationReconciliationOutcome,
    ProductionArtifactContextSnapshotRepository,
    _envelope_reduced_since,
)
from cti_app.application.investigation_state import (
    InMemoryInvestigationContextSnapshotRepository,
    InMemoryInvestigationStateRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)
from cti_app.application.research_context_builder import (
    USER_REQUEST_ACTION_PREFIX,
    ArticleSnapshot,
    ResearchContextBuilder,
)
from cti_app.domain.analyst_opinion import (
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    NoveltyClass,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    tool_operation_key,
)
from cti_app.domain.investigation_state import Hypothesis, HypothesisStatus, StopReason
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OBSERVATION_NAMESPACE = UUID("9a5f4af5-e256-45fb-b905-4f913ed229ba")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")


def test_envelope_reduction_uses_only_domain_fields() -> None:
    investigation_id = uuid4()
    baseline = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("network", "vt"),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 20,
                BudgetDimension.DOWNLOADS: 4,
            }
        ),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    identical = replace(
        baseline,
        budget=InvestigationBudget(limits=dict(baseline.budget.limits)),
    )
    reduced = replace(
        baseline,
        version=baseline.version + 1,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 10,
                BudgetDimension.DOWNLOADS: 2,
            }
        ),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.AMBER,
            do_not_submit=True,
            external_llm_allowed=False,
        ),
    )
    extended = replace(
        baseline,
        version=baseline.version + 1,
        allowed_providers=("network", "shodan", "vt"),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 30,
                BudgetDimension.DOWNLOADS: 6,
            }
        ),
    )

    assert _envelope_reduced_since(baseline, identical) is False
    assert _envelope_reduced_since(baseline, reduced) is True
    assert _envelope_reduced_since(baseline, extended) is False
    assert _envelope_reduced_since(baseline, None) is True


class _InvestigationRepository:
    def __init__(self):
        self.items = {}

    async def get(self, investigation_id):
        return self.items.get(investigation_id)

    async def get_for_run(self, run_id):
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation):
        self.items[investigation.id] = investigation

    async def save(self, investigation):
        self.items[investigation.id] = investigation


class _Handoff:
    def __init__(self, repository):
        self.repository = repository
        self.modified_artifacts = []

    async def ensure_for_verified_synthesis(self, *, run, synthesis, **_kwargs):
        existing = await self.repository.get_for_run(run.id)
        if existing is None:
            existing = AnalystInvestigation.from_verified_synthesis(
                synthesis=synthesis,
                budget=LoopBudget(
                    max_cycles=12,
                    max_pivot_runs=20,
                    max_hits_acquired=10,
                    max_new_samples=10,
                    max_vt_read_units=50,
                ),
            )
            await self.repository.add(existing)
        return SimpleNamespace(investigation_id=existing.id)


class _ArticlePort:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id):
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(self, investigation_id, claim_ids=()):
        if not claim_ids:
            return self.snapshot.article.claims
        wanted = set(claim_ids)
        return tuple(claim for claim in self.snapshot.article.claims if claim.id in wanted)


class _StateRepository(InMemoryInvestigationStateRepository):
    def __init__(self):
        super().__init__()
        self.save_count = 0
        self.fail_once_at = None

    async def save_if_version(self, state, *, expected_version):
        self.save_count += 1
        if self.save_count == self.fail_once_at:
            self.fail_once_at = None
            raise RuntimeError("simulated process crash after durable tool result")
        return await super().save_if_version(state, expected_version=expected_version)


class _ObservationWriter:
    def __init__(self, repository):
        self.repository = repository

    async def archive(self, invocation, result):
        encoded = canonical_json_bytes(dict(result.output))
        observation = EvidenceObservation(
            id=uuid5(_OBSERVATION_NAMESPACE, invocation.operation_key),
            investigation_id=invocation.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid5(_OBSERVATION_NAMESPACE, "blob:" + invocation.operation_key),
            sha256=hashlib.sha256(encoded).hexdigest(),
            source=invocation.tool_name,
            source_version=invocation.schema_version,
            observed_at=datetime.now(UTC),
            object_type=str(result.output.get("object_type", "tool_call")),
            object_id=str(result.output.get("object_id", invocation.operation_key)),
            limitations=("Fake tool output is untrusted test data.",),
            invocation_ref=invocation.id,
        )
        return (await self.repository.append_if_absent(observation),)


class _FailingObservationWriter:
    async def archive(self, invocation, result):
        raise RuntimeError("simulated evidence persistence failure")


class _DecisionRepository:
    def __init__(self):
        self.items = []

    async def append(self, decision):
        if not any(item.id == decision.id for item in self.items):
            self.items.append(decision)

    async def list_for_investigation(self, investigation_id):
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _ContextBlobStore:
    def __init__(self):
        self.blob_id = uuid4()
        self.payload: Any = None
        self.bucket = None

    async def put_canonical_json(self, payload, *, bucket):
        self.payload = payload
        self.bucket = bucket
        return self.blob_id, hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    async def read_json(self, blob_id):
        assert blob_id == self.blob_id
        return self.payload


class _FakeTool:
    def __init__(self, name, execution_order):
        self.name = name
        self.execution_order = execution_order
        self.calls = []
        self.contexts = []
        self.after_execute = None
        self.observation_object_type = None
        self.observation_object_id = None

    async def execute(self, arguments, context):
        self.calls.append(dict(arguments))
        self.contexts.append(context)
        self.execution_order.append(self.name)
        if self.after_execute is not None:
            result = self.after_execute()
            if hasattr(result, "__await__"):
                await result
        output = {"summary": f"{self.name} observed the requested relation."}
        if self.observation_object_type is not None:
            output["object_type"] = self.observation_object_type
        if self.observation_object_id is not None:
            output["object_id"] = self.observation_object_id
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=output,
        )


class _CrashAfterDispatch:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.crashed = False

    async def dispatch(self, context, **kwargs):
        result = await self.dispatcher.dispatch(context, **kwargs)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated worker crash after provider call")
        return result


class _RevokeAfterCommittedOperation:
    def __init__(self, base, operation_key):
        self.base = base
        self.operation_key = operation_key
        self.did_revoke = False

    async def get(self, investigation_id):
        return await self.base.get(investigation_id)

    async def save_if_version(self, envelope, *, expected_version):
        saved = await self.base.save_if_version(envelope, expected_version=expected_version)
        committed = any(
            item.operation_key == self.operation_key and item.status.value == "committed"
            for item in saved.budget.reservations
        )
        if committed and not self.did_revoke:
            self.did_revoke = True
            revoked = replace(
                saved,
                version=saved.version + 1,
                allowed_providers=(),
                acquire_hits_allowed=False,
                budget=replace(
                    saved.budget,
                    limits={dimension: 0 for dimension in saved.budget.limits},
                ),
                disclosure=replace(
                    saved.disclosure,
                    do_not_submit=True,
                    external_llm_allowed=False,
                ),
            )
            await self.base.save_if_version(revoked, expected_version=saved.version)
        return saved


class _Fixture:
    def __init__(
        self,
        *,
        responses,
        bridge_turn_budget=8,
        external_allowed=True,
        tools=(),
        no_progress_limit=3,
        narrative_hashes=(),
    ):
        self.run_id = uuid4()
        self.subject_id = uuid4()
        self.synthesis = ProductionArtifact(
            production_run_id=self.run_id,
            subject_id=self.subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="1" * 64,
            status=ProductionArtifactStatus.VERIFIED,
        )
        self.publication_id = uuid4()
        self.snapshot = ArticleSnapshot(
            subject_id=self.subject_id,
            production_run_id=self.run_id,
            synthesis_artifact_id=self.synthesis.id,
            synthesis_sha256="2" * 64,
            article=ArticleContextV1(
                short_summary="A verified campaign narrative.",
                claims=(
                    ArticleClaimV1(
                        id="C1",
                        statement="The article describes a loader chain.",
                        source_refs=("source-1",),
                    ),
                ),
                documented_samples=("a" * 64,),
                known_chain=(),
                open_questions=("Which stage retrieves the payload?",),
            ),
            base_publication_artifact_id=self.publication_id,
            base_publication_sha256="3" * 64,
            tlp="GREEN",
            do_not_submit=False,
            external_llm_allowed=external_allowed,
            narrative_hashes=tuple(narrative_hashes),
        )
        self.investigations = _InvestigationRepository()
        seed_investigation = AnalystInvestigation.from_verified_synthesis(
            synthesis=self.synthesis,
            budget=LoopBudget(
                max_cycles=12,
                max_pivot_runs=20,
                max_hits_acquired=10,
                max_new_samples=10,
                max_vt_read_units=50,
            ),
        )
        self.investigations.items[seed_investigation.id] = seed_investigation
        self.investigation_id = seed_investigation.id
        self.handoff = _Handoff(self.investigations)
        self.states = _StateRepository()
        self.context_snapshots = InMemoryInvestigationContextSnapshotRepository()
        self.envelopes = InMemoryInvestigationEnvelopeRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.analyst_decisions = _DecisionRepository()
        self.commit_count = 0
        self.committed_running_keys = set()
        self.opinions = InMemoryAnalystOpinionRepository()
        self.registry = InMemoryToolRegistry()
        self.tools = {}
        self.execution_order = []
        providers = set()
        capabilities = set()
        for name, provider in tools:
            tool = _FakeTool(name, self.execution_order)
            self.tools[name] = tool
            capability = f"{provider}.read"
            definition = ToolDefinition(
                name=name,
                schema_version="1",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 500}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.EXTERNAL_READ,
                cost_model={BudgetDimension.VT_UNITS: 1},
                confidentiality=ToolConfidentiality.INTERNAL,
                limitations=("Passive fake fixture",),
                capability_key=capability,
            )
            self.registry.register(definition, tool)
            providers.add(provider)
            capabilities.add(capability)
        self.available_capabilities = frozenset(capabilities)
        self.bridge_transport = ScriptedTurnTransport(responses)
        self.bridge = BridgeInvestigator(self.bridge_transport)
        self.context_builder = ResearchContextBuilder(
            article_snapshots=_ArticlePort(self.snapshot),
            observations=self.observations,
            findings=self.findings,
        )
        self.service = InvestigationService(
            post_synthesis=self.handoff,
            investigations=self.investigations,
            states=self.states,
            context_snapshots=self.context_snapshots,
            envelopes=self.envelopes,
            context_builder=self.context_builder,
            bridge=self.bridge,
            registry=self.registry,
            dispatcher=RegistryToolDispatcher(self.registry),
            invocations=self.invocations,
            observations=self.observations,
            findings=self.findings,
            opinions=self.opinions,
            observation_writer=_ObservationWriter(self.observations),
            available_capabilities=self.available_capabilities,
            no_progress_limit=no_progress_limit,
            commit_uow=self._commit,
        )
        self.envelope = InvestigationEnvelope(
            investigation_id=self.investigation_id,
            allowed_providers=tuple(sorted(providers)),
            budget=InvestigationBudget(
                limits={
                    BudgetDimension.BRIDGE_TURNS: bridge_turn_budget,
                    BudgetDimension.VT_UNITS: 20,
                }
            ),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst-1",
        )
        self.run = SimpleNamespace(id=self.run_id, subject_id=self.subject_id)

    async def _commit(self):
        self.commit_count += 1
        current = await self.invocations.list_for_investigation(self.investigation_id)
        self.committed_running_keys.update(
            item.operation_key for item in current if item.status is ToolInvocationStatus.RUNNING
        )

    async def start(self, *, hypotheses=()):
        return await self.service.start(
            run=self.run,
            synthesis=self.synthesis,
            extraction_artifacts=(),
            extraction_items=(),
            policy=AnalystHandoffPolicy(
                tlp="GREEN", do_not_submit=False, external_llm_allowed=True
            ),
            envelope=self.envelope,
            hypotheses=tuple(hypotheses),
        )


def _tool_call(name, query, purpose):
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": {"query": query},
        "purpose": purpose,
    }


def _proposal(
    kind,
    assessment,
    statement,
    observation_id,
    claim_refs=(),
    subject_ref=None,
    novelty=None,
):
    return {
        "kind": kind,
        "assessment": assessment,
        "statement": statement,
        "subject_ref": subject_ref,
        "observation_refs": [str(observation_id)] if observation_id else [],
        "claim_refs": list(claim_refs),
        "novelty": novelty,
        "scope": "investigation article",
        "limitations": [],
    }


def _operation_and_observation_id(investigation_id, call):
    operation_key = tool_operation_key(
        investigation_id=investigation_id,
        tool_name=call["tool_name"],
        schema_version=call["schema_version"],
        normalized_parameters=call["arguments"],
        purpose=call["purpose"],
    )
    return operation_key, uuid5(_OBSERVATION_NAMESPACE, operation_key)


def _finding_id(investigation_id, proposal):
    digest = hashlib.sha256(canonical_json_bytes(proposal)).hexdigest()
    return uuid5(_FINDING_NAMESPACE, f"{investigation_id}:{digest}")


def _decision(*, goal, next_action, tools=(), findings=(), opinion=None, hypothesis_refs=()):
    return {
        "reasoning_summary": f"Selected next action for {goal}.",
        "goal": goal,
        "hypothesis_refs": list(hypothesis_refs),
        "proposed_tool_calls": list(tools),
        "finding_proposals": list(findings),
        "opinion_delta": opinion,
        "next_action": next_action,
    }


def _opinion_delta(finding_id, observation_id, *, text="The observation supports this conclusion."):
    return {
        "blocks": [
            {
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "text": text,
                "finding_refs": [str(finding_id)],
                "evidence_refs": [str(observation_id)],
                "qualification": OpinionClaimQualification.FACT.value,
                "certainty": 0.9,
            }
        ],
        "unresolved_points": [],
    }


@pytest.mark.asyncio
async def test_start_is_idempotent_and_uses_verified_synthesis_handoff() -> None:
    fixture = _Fixture(responses=[], tools=())
    first = await fixture.start()
    second = await fixture.start()

    assert first.investigation_id == second.investigation_id
    assert first.version == second.version == 1
    investigation = await fixture.investigations.get(first.investigation_id)
    assert investigation is not None
    assert investigation.status is AnalystInvestigationStatus.RUNNING
    assert fixture.handoff.modified_artifacts == []


@pytest.mark.asyncio
async def test_production_context_snapshot_adapter_round_trips_immutable_json() -> None:
    blob_store = _ContextBlobStore()
    snapshots = ProductionArtifactContextSnapshotRepository(blob_store)
    canonical_context = canonical_json_bytes({"view": "delta", "changes": []}).decode()

    snapshot_id = await snapshots.store(canonical_context)

    assert snapshot_id == blob_store.blob_id
    assert blob_store.bucket == "investigation-context-snapshots"
    assert await snapshots.load(snapshot_id) == canonical_context


@pytest.mark.asyncio
async def test_budget_exhaustion_is_a_local_stop_without_model_error() -> None:
    fixture = _Fixture(responses=[], bridge_turn_budget=0, tools=())
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert fixture.bridge_transport.attempt_count == 0
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.EXHAUSTED


@pytest.mark.parametrize(
    ("dimension", "limit"),
    (
        (BudgetDimension.CONTEXT_BYTES, 1),
        (BudgetDimension.CONTEXT_TOKENS, 0),
    ),
)
@pytest.mark.asyncio
async def test_context_budget_exhaustion_is_a_local_stop(dimension, limit) -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    fixture.envelope = replace(
        fixture.envelope,
        budget=InvestigationBudget(
            limits={
                **dict(fixture.envelope.budget.limits),
                dimension: limit,
            }
        ),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert fixture.bridge_transport.attempt_count == 0
    persisted = await fixture.envelopes.get(state.investigation_id)
    assert persisted is not None
    assert persisted.budget.consumed[dimension] == 0


@pytest.mark.asyncio
async def test_context_budget_replay_does_not_double_consume_committed_turn() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()
    fixture.states.fail_once_at = fixture.states.save_count + 2

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await fixture.service.run(state.investigation_id)

    after_crash = await fixture.envelopes.get(state.investigation_id)
    assert after_crash is not None
    consumed_after_crash = dict(after_crash.budget.consumed)
    pending = await fixture.states.get(state.investigation_id)
    assert pending is not None and pending.pending_context_snapshot_id is not None
    pending_context = await fixture.context_snapshots.load(pending.pending_context_snapshot_id)
    assert pending_context is not None
    context_size_bytes = len(pending_context.encode("utf-8"))
    assert consumed_after_crash[BudgetDimension.CONTEXT_BYTES] == context_size_bytes
    assert consumed_after_crash[BudgetDimension.CONTEXT_TOKENS] == (context_size_bytes + 3) // 4
    assert fixture.bridge_transport.submission_count == 1

    await fixture.service.resume(state.investigation_id)
    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    after_replay = await fixture.envelopes.get(state.investigation_id)
    assert after_replay is not None
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_BYTES]
        == (consumed_after_crash[BudgetDimension.CONTEXT_BYTES])
    )
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (consumed_after_crash[BudgetDimension.CONTEXT_TOKENS])
    )
    assert after_replay.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (after_replay.budget.consumed[BudgetDimension.CONTEXT_BYTES] + 3) // 4
    )
    assert fixture.bridge_transport.submission_count == 1


@pytest.mark.asyncio
async def test_unlimited_context_dimensions_are_consumed_without_blocking() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    persisted = await fixture.envelopes.get(state.investigation_id)
    assert persisted is not None
    assert persisted.budget.limits.get(BudgetDimension.CONTEXT_BYTES) is None
    assert persisted.budget.limits.get(BudgetDimension.CONTEXT_TOKENS) is None
    assert persisted.budget.consumed[BudgetDimension.CONTEXT_BYTES] > 0
    assert (
        persisted.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (persisted.budget.consumed[BudgetDimension.CONTEXT_BYTES] + 3) // 4
    )


@pytest.mark.asyncio
async def test_request_permission_pauses_for_review_then_resumes_on_a_new_turn() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="request approval for a new query", next_action="request_permission"),
            _decision(goal="finish after review", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    paused = await fixture.service.run(state.investigation_id)

    assert paused.stop_reason is StopReason.PERMISSION_REQUIRED
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.AWAITING_REVIEW
    resumed = await fixture.service.resume(state.investigation_id)
    assert resumed.turn_index == 1
    completed = await fixture.service.run(state.investigation_id)

    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2


@pytest.mark.asyncio
async def test_no_new_information_stops_after_configured_streak() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="inspect evidence", next_action="continue"),
            _decision(goal="inspect evidence again", next_action="continue"),
            _decision(goal="try another angle", next_action="continue"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.NO_NEW_INFORMATION
    assert result.no_progress_streak == 3
    assert fixture.bridge_transport.submission_count == 3


@pytest.mark.asyncio
async def test_no_progress_allows_hypothesis_switch_at_the_limit() -> None:
    fixture = _Fixture(
        no_progress_limit=1,
        responses=[
            _decision(goal="switch to another hypothesis", next_action="switch_hypothesis"),
            _decision(goal="finish after the switch", next_action="finish"),
        ],
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2


@pytest.mark.asyncio
async def test_no_progress_treats_a_new_operation_as_a_path_forward_once() -> None:
    repeated = _tool_call("tool.not_registered", "new-query", "try an unseen lookup")
    fixture = _Fixture(
        no_progress_limit=2,
        responses=[
            _decision(goal="inspect current context", next_action="continue"),
            _decision(goal="try an unseen lookup", next_action="continue", tools=(repeated,)),
            _decision(goal="repeat the same lookup", next_action="continue", tools=(repeated,)),
        ],
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.NO_NEW_INFORMATION
    assert result.no_progress_streak == 3
    assert fixture.bridge_transport.submission_count == 3


@pytest.mark.asyncio
async def test_switch_hypothesis_abandons_the_referenced_open_hypothesis() -> None:
    fixture = _Fixture(
        responses=[
            _decision(
                goal="leave the unsupported attribution hypothesis",
                next_action="switch_hypothesis",
                hypothesis_refs=("ip-attribution",),
            ),
            _decision(goal="finish with the alternative line", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start(
        hypotheses=(Hypothesis(id="ip-attribution", statement="The IP belongs to campaign C2."),)
    )

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert result.hypotheses[0].status is HypothesisStatus.ABANDONED


@pytest.mark.asyncio
async def test_worker_turn_slice_yields_active_state_without_declaring_budget_stop() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="continue research", next_action="continue"),
            _decision(goal="finish after resumed slice", next_action="finish"),
        ],
        tools=(),
    )
    fixture.service._max_turns_per_run = 1
    state = await fixture.start()

    yielded = await fixture.service.run(state.investigation_id)

    assert yielded.stop_reason is None
    assert yielded.turn_index == 1
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.RUNNING
    completed = await fixture.service.run(state.investigation_id)
    assert completed.stop_reason is StopReason.AGENT_FINISHED


@pytest.mark.asyncio
async def test_unavailable_tool_does_not_abort_later_agent_decision() -> None:
    unavailable = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="check a payload", next_action="continue", tools=(unavailable,)),
            _decision(goal="finish with current evidence", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    invocation = await fixture.invocations.get_by_operation_key(
        tool_operation_key(
            investigation_id=state.investigation_id,
            tool_name="vt.search",
            schema_version="1",
            normalized_parameters=unavailable["arguments"],
            purpose=unavailable["purpose"],
        )
    )
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_tool_outside_envelope_is_denied_with_alternative_and_budget_release() -> None:
    denied = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="search VT", next_action="continue", tools=(denied,)),
            _decision(goal="finish with network alternative", next_action="finish"),
        ],
        tools=(("vt.search", "vt"), ("network.lookup", "network")),
    )
    state = await fixture.start()
    envelope = await fixture.envelopes.get(state.investigation_id)
    assert envelope is not None
    restricted = replace(
        envelope,
        allowed_providers=("network",),
        version=envelope.version + 1,
    )
    await fixture.envelopes.save_if_version(restricted, expected_version=envelope.version)

    result = await fixture.service.run(state.investigation_id)

    operation_key = tool_operation_key(
        investigation_id=state.investigation_id,
        tool_name=denied["tool_name"],
        schema_version=denied["schema_version"],
        normalized_parameters=denied["arguments"],
        purpose=denied["purpose"],
    )
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert latest_envelope is not None
    assert (
        next(
            item
            for item in latest_envelope.budget.reservations
            if item.operation_key == operation_key
        ).status.value
        == "released"
    )
    assert "network.lookup" in fixture.bridge_transport.prompts[1]


@pytest.mark.asyncio
async def test_observation_archive_failure_requires_review_without_tool_replay() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="check payload", next_action="finish", tools=(call,)),
            _decision(goal="do not repeat ambiguous tool", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    fixture.service._observation_writer = _FailingObservationWriter()
    state = await fixture.start()

    first = await fixture.service.run(state.investigation_id)
    pending_state = await fixture.states.get(state.investigation_id)
    assert pending_state is not None
    assert pending_state.pending_context_snapshot_id is not None
    pending_json = await fixture.context_snapshots.load(pending_state.pending_context_snapshot_id)
    assert pending_json is not None
    assert hashlib.sha256(pending_json.encode()).hexdigest() == pending_state.pending_context_sha256
    assert len(pending_state.canonical_bytes()) < len(pending_json.encode())
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    resolved = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The execution result cannot be recovered; close the ambiguity.",
        expected_version=invocation.version,
        idempotency_key="reconcile-ambiguous-call",
    )
    assert resolved.status is ToolInvocationStatus.FAILED
    replay = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The execution result cannot be recovered; close the ambiguity.",
        expected_version=invocation.version,
        idempotency_key="reconcile-ambiguous-call",
    )
    assert replay.version == resolved.version
    assert len(fixture.analyst_decisions.items) == 1
    await fixture.service.resume(state.investigation_id)
    second = await fixture.service.run(state.investigation_id)

    assert first.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert second.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1


@pytest.mark.asyncio
async def test_running_invocation_is_committed_before_dispatch_and_never_replayed() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    decision = _decision(goal="check payload", next_action="continue", tools=(call,))
    fixture = _Fixture(
        responses=[decision, decision, _decision(goal="finish", next_action="finish")],
        tools=(("vt.search", "vt"),),
    )
    fixture.service._dispatcher = _CrashAfterDispatch(fixture.service._dispatcher)
    state = await fixture.start()
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)

    with pytest.raises(RuntimeError, match="simulated worker crash after provider call"):
        await fixture.service.run(state.investigation_id)

    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.RUNNING
    assert operation_key in fixture.committed_running_keys
    assert fixture.tools["vt.search"].calls == [call["arguments"]]

    ambiguous = await fixture.service.run(state.investigation_id)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert ambiguous.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)

    await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The provider may have executed the operation.",
        expected_version=invocation.version,
        idempotency_key="crash-after-dispatch",
    )
    envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status.value == "committed"
    assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 1
    await fixture.service.resume(state.investigation_id)
    finished = await fixture.service.run(state.investigation_id)

    assert finished.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1
    assert fixture.tools["vt.search"].contexts[0].idempotency_key == operation_key
    assert (
        len([item for item in envelope.budget.reservations if item.operation_key == operation_key])
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        InvocationReconciliationOutcome.EXECUTED_WITH_RESULT,
        InvocationReconciliationOutcome.NOT_EXECUTED,
    ],
)
async def test_reconciliation_uses_cas_and_commits_or_releases_ambiguous_budget(outcome) -> None:
    call = _tool_call("vt.search", "content:ambiguous", "resolve an ambiguous lookup")
    fixture = _Fixture(responses=[], tools=(("vt.search", "vt"),))
    state = await fixture.start()
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    envelope = await fixture.envelopes.get(state.investigation_id)
    cost = {BudgetDimension.VT_UNITS: 1}
    envelope.budget.reserve(operation_key, cost)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )
    invocation = ToolInvocation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        tool_name=call["tool_name"],
        schema_version=call["schema_version"],
        normalized_parameters=call["arguments"],
        purpose=call["purpose"],
        reserved_budget=cost,
    )
    invocation = await fixture.invocations.add_if_absent(invocation)
    invocation = await fixture.invocations.update_if_version(
        invocation.transition(ToolInvocationStatus.RUNNING), expected_version=invocation.version
    )
    invocation = await fixture.invocations.update_if_version(
        invocation.transition(ToolInvocationStatus.NEEDS_REVIEW),
        expected_version=invocation.version,
    )

    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    with pytest.raises(VersionConflictError, match="version changed"):
        await fixture.service.reconcile_invocation(
            state.investigation_id,
            invocation.id,
            outcome=outcome,
            actor="analyst-1",
            reason="Reject the stale reconciliation attempt.",
            expected_version=invocation.version - 1,
            idempotency_key="reconcile-stale-cas",
        )

    response_refs: tuple[UUID, ...] = ()
    if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT:
        observation = EvidenceObservation(
            investigation_id=state.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256="f" * 64,
            source="analyst-review",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="ip",
            object_id="203.0.113.25",
        )
        await fixture.observations.append_if_absent(observation)
        response_refs = (observation.id,)
    resolved = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=outcome,
        actor="analyst-1",
        reason="Resolve the ambiguous provider outcome after review.",
        response_refs=response_refs,
        expected_version=invocation.version,
        idempotency_key="reconcile-cas-valid",
    )
    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )

    assert resolved.response_refs == response_refs
    assert len(fixture.analyst_decisions.items) == 1
    if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT:
        assert resolved.status is ToolInvocationStatus.SUCCEEDED
        assert reservation.status.value == "committed"
        assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 1
    else:
        assert resolved.status is ToolInvocationStatus.FAILED
        assert reservation.status.value == "released"
        assert latest_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    await fixture.service.resume(state.investigation_id)


@pytest.mark.asyncio
async def test_g2_revocation_between_external_tools_blocks_the_second_call() -> None:
    first = _tool_call("vt.search", "content:one", "check the first payload")
    second = _tool_call("network.lookup", "203.0.113.9", "check the endpoint")
    fixture = _Fixture(
        responses=[
            _decision(goal="check both sources", next_action="continue", tools=(first, second)),
            _decision(goal="finish after revocation", next_action="finish"),
        ],
        tools=(("vt.search", "vt"), ("network.lookup", "network")),
    )
    state = await fixture.start()
    first_key, _ = _operation_and_observation_id(state.investigation_id, first)
    second_key, _ = _operation_and_observation_id(state.investigation_id, second)
    fixture.service._envelopes = _RevokeAfterCommittedOperation(fixture.envelopes, first_key)
    local_tool = _FakeTool("article.get_claims", fixture.execution_order)
    fixture.registry.register(
        ToolDefinition(
            name="article.get_claims",
            schema_version="1",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Local article context",),
            capability_key="article.read",
        ),
        local_tool,
    )
    fixture.service._available_capabilities = frozenset(
        (*fixture.available_capabilities, "article.read")
    )

    result = await fixture.service.run(state.investigation_id)

    latest = await fixture.envelopes.get(state.investigation_id)
    denied = await fixture.invocations.get_by_operation_key(
        f"{second_key}:g2-denied:{latest.version}"
    )
    assert result.stop_reason is StopReason.PERMISSION_REQUIRED
    assert fixture.execution_order == ["vt.search"]
    assert denied is not None
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert any("article.get_claims" in action for action in result.recent_actions)


@pytest.mark.asyncio
async def test_invalid_bridge_output_stops_after_two_corrections_until_new_request() -> None:
    invalid = {"reasoning_summary": "missing required decision fields"}
    fixture = _Fixture(
        responses=[
            invalid,
            invalid,
            invalid,
            _decision(goal="finish after the user request", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    rejected = await fixture.service.run(state.investigation_id)

    assert rejected.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 3
    assert any(action.startswith("bridge_invalid_output:") for action in rejected.recent_actions)
    with pytest.raises(ValueError, match="new user request"):
        await fixture.service.resume(state.investigation_id)

    request_action = USER_REQUEST_ACTION_PREFIX + json.dumps(
        {"request_id": str(uuid4()), "text": "Continue with a corrected new request."},
        separators=(",", ":"),
    )
    updated = replace(
        rejected,
        version=rejected.version + 1,
        recent_actions=(*rejected.recent_actions, request_action),
    )
    await fixture.states.save_if_version(updated, expected_version=rejected.version)
    await fixture.service.resume(state.investigation_id)
    resumed = await fixture.service.run(state.investigation_id)

    assert resumed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 4


@pytest.mark.asyncio
async def test_corrected_bridge_turn_progresses_and_persists_the_rejection_trace() -> None:
    fixture = _Fixture(
        responses=[
            {"reasoning_summary": "missing required decision fields"},
            _decision(goal="finish after correcting the schema", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert result.turn_index == 1
    assert fixture.bridge_transport.submission_count == 2
    assert any(
        action.startswith("bridge_invalid_output:") and "schema validation" in action
        for action in result.recent_actions
    )


@pytest.mark.asyncio
async def test_new_sample_novelty_is_recalculated_against_narrative_hashes() -> None:
    sample_hash = "e" * 40
    fixture = _Fixture(
        responses=[],
        tools=(),
        narrative_hashes=(sample_hash.upper(),),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="analyst-test",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id=sample_hash,
    )
    await fixture.observations.append_if_absent(observation)
    proposal = _proposal(
        FindingKind.NEW_SAMPLE.value,
        FindingAssessment.SUPPORTED.value,
        "The article narrative already names this sample; the observation adds a detail.",
        observation.id,
        subject_ref=sample_hash,
        novelty=NoveltyClass.NEW_TO_ARTICLE.value,
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="record the sample", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].novelty is NoveltyClass.ALREADY_DOCUMENTED_BUT_NEW_DETAIL
    assert any("recalculée côté serveur" in item for item in findings[0].limitations)
    assert any("snapshot" in item for item in findings[0].limitations)


@pytest.mark.asyncio
@pytest.mark.parametrize("subject_ref", [None, "not-a-hash"])
async def test_new_sample_without_valid_subject_ref_is_inconclusive(subject_ref) -> None:
    fixture = _Fixture(
        responses=[],
        tools=(),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="analyst-test",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id="d" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    proposal = _proposal(
        FindingKind.NEW_SAMPLE.value,
        FindingAssessment.SUPPORTED.value,
        "An unidentified sample may be relevant.",
        observation.id,
        subject_ref=subject_ref,
        novelty=NoveltyClass.NEW_TO_ARTICLE.value,
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="record a possible sample", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert findings[0].novelty is NoveltyClass.NOT_CONFIRMED
    assert any("subject_ref hash valide" in item for item in findings[0].limitations)


@pytest.mark.asyncio
async def test_unrelated_observation_downgrades_supported_and_withholds_fact_opinion() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    _, observation_id = _operation_and_observation_id(state.investigation_id, call)
    proposal = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "The cited object was observed to contact this endpoint.",
        observation_id,
        subject_ref="203.0.113.9",
    )
    opinion = _opinion_delta(_finding_id(state.investigation_id, proposal), observation_id)
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="test a proposed relationship",
                next_action="finish",
                tools=(call,),
                findings=(proposal,),
                opinion=opinion,
            )
        ]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.QUALIFIED
    assert findings[0].limitations[-1] == "l'observation citée ne porte pas sur l'objet visé"
    analyst_opinion = await fixture.opinions.get(
        uuid5(_OPINION_NAMESPACE, str(state.investigation_id))
    )
    assert analyst_opinion is not None
    assert analyst_opinion.blocks == ()
    assert any("citation validation failed" in item for item in analyst_opinion.unresolved_points)


@pytest.mark.asyncio
async def test_claim_verification_requires_non_claim_source_evidence() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    claim_source = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="article-source",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="claim",
        object_id="C1",
    )
    await fixture.observations.append_if_absent(claim_source)
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The cited article claim has independent evidence.",
        claim_source.id,
        claim_refs=("C1",),
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="verify the claim", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert any("observation de preuve" in item for item in findings[0].limitations)


@pytest.mark.asyncio
async def test_removed_external_permission_marks_model_unavailable_without_call() -> None:
    fixture = _Fixture(responses=[], tools=(), external_allowed=False)
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.MODEL_UNAVAILABLE
    assert fixture.bridge_transport.attempt_count == 0
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_ambiguous_bridge_resume_reuses_submission_identity() -> None:
    fixture = _Fixture(
        responses=[
            TurnSubmission(TurnSubmissionStatus.NEEDS_REVIEW, detail="simulated uncertain submit"),
            _decision(goal="this response must not be submitted", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()
    first = await fixture.service.run(state.investigation_id)
    await fixture.service.resume(state.investigation_id)
    second = await fixture.service.run(state.investigation_id)

    assert first.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert second.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 1
    assert len(fixture.bridge_transport._responses) == 1


@pytest.mark.asyncio
async def test_crash_replay_does_not_execute_succeeded_tool_twice() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(
                goal="check a payload",
                next_action="finish",
                tools=(call,),
                findings=(),
            )
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    fixture.tools["vt.search"].observation_object_type = "ip"
    fixture.tools["vt.search"].observation_object_id = "203.0.113.9"
    _, observation_id = _operation_and_observation_id(state.investigation_id, call)
    proposal = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "A passive relationship result connects the endpoint.",
        observation_id,
        subject_ref="203.0.113.9",
    )
    # Use a new idempotent scripted response with the evidence reference known before execution.
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="check a payload",
                next_action="finish",
                tools=(call,),
                findings=(proposal,),
            )
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge
    fixture.states.fail_once_at = fixture.states.save_count + 2

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await fixture.service.run(state.investigation_id)
    replayed = await fixture.service.run(state.investigation_id)

    assert replayed.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1
    assert fixture.bridge_transport.submission_count == 1
    assert await fixture.observations.get(observation_id) is not None


@pytest.mark.asyncio
async def test_supported_finding_with_nonexistent_citations_is_downgraded() -> None:
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The address is confirmed.",
        uuid4(),
        claim_refs=("not-a-claim",),
    )
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish", findings=(proposal,))],
        tools=(),
    )
    state = await fixture.start()

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert findings[0].observation_refs == ()
    assert any("downgraded" in item for item in findings[0].limitations)


def test_tool_operation_and_finding_id_helpers_are_deterministic() -> None:
    investigation_id = uuid4()
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    operation_a = _operation_and_observation_id(investigation_id, call)
    operation_b = _operation_and_observation_id(investigation_id, call)
    proposal = _proposal("network_behaviour", "supported", "Observed endpoint.", uuid4())

    assert operation_a == operation_b
    assert _finding_id(investigation_id, proposal) == _finding_id(investigation_id, proposal)
