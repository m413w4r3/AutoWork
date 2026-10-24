from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.api.investigation import _finding_logical_id, _finding_revision_chain
from cti_app.application.investigation_experiments import (
    EXPERIMENT_RECORD_TOOL,
    EXPERIMENT_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_DECLARE_TOOL,
    InMemoryInvestigationExperimentRepository,
    OperatorExperimentToolAdapter,
    _ExperimentListToolAdapter,
    _validate_supersedes_experiments,
    build_investigation_experiment_timeline,
    register_operator_experiment_tools,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.domain.investigation import (
    DisclosureBounds,
    InvestigationBudget,
    InvestigationEnvelope,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_experiment import (
    HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
    AnalysisMatrixStatus,
    ConsolidatedClaimStatus,
    ExperimentAvailability,
    ExperimentInputRefV1,
    ExperimentObservationRefV1,
    ExperimentVerdict,
    InvestigationExperimentV1,
    ObservationCoverage,
    ObservationOutcome,
    project_claim_matrix_status,
    project_experiment_matrix_status,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CLAIM_ID = "claim:network-behaviour"


def _experiment(
    version: int,
    verdict: ExperimentVerdict,
    *,
    investigation_id=None,
    actual_outcome: ObservationOutcome = ObservationOutcome.OBSERVED,
    coverage: ObservationCoverage = ObservationCoverage.COMPLETE,
    limitations: tuple[str, ...] = (),
    supersedes_experiment_ids: tuple[UUID, ...] = (),
) -> InvestigationExperimentV1:
    return InvestigationExperimentV1(
        id=uuid4(),
        investigation_id=investigation_id or uuid4(),
        version=version,
        hypothesis_id="hypothesis-network",
        claim_ids=(CLAIM_ID,),
        working_state_version=2,
        g2_envelope_version=1,
        prediction="The sample emits the documented marker.",
        falsification_criterion="A complete capture lacks the marker.",
        method="Passively inspect the persisted capture with the pinned parser.",
        input_type="sample_sha256",
        input_refs=(ExperimentInputRefV1("sample_sha256", "a" * 64, "a" * 64),),
        preconditions=("Capture is complete and bound to this investigation.",),
        availability=ExperimentAvailability.AVAILABLE,
        tool_name="network.capture.inspect",
        tool_version="tshark-4.4.1",
        tool_schema_version="network-capture-inspect.v1",
        tool_arguments={"max_packets": 500},
        step_ids=(f"step-{version}",),
        tool_invocation_refs=(),
        observation_refs=(ExperimentObservationRefV1(uuid4(), "b" * 64),),
        expected="Marker should occur in the captured stream.",
        actual="Marker observed."
        if actual_outcome is ObservationOutcome.OBSERVED
        else "No marker observed.",
        actual_outcome=actual_outcome,
        observation_coverage=coverage,
        started_at=NOW + timedelta(minutes=version),
        completed_at=NOW + timedelta(minutes=version, seconds=20),
        recorded_at=NOW + timedelta(minutes=version, seconds=21),
        scope="Static capture inspection; does not establish victim execution.",
        limitations=limitations,
        verdict=verdict,
        implication="Retain this result as one evidence line.",
        next_pivots=("c" * 64,),
        supersedes_experiment_ids=supersedes_experiment_ids,
    )


@pytest.mark.asyncio
async def test_three_experiments_preserve_contradiction_and_scope() -> None:
    investigation_id = uuid4()
    positive = _experiment(1, ExperimentVerdict.SUPPORTS, investigation_id=investigation_id)
    contradictory = _experiment(
        2,
        ExperimentVerdict.REFUTES,
        investigation_id=investigation_id,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
    )
    inconclusive = _experiment(
        3,
        ExperimentVerdict.INCONCLUSIVE,
        investigation_id=investigation_id,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
        coverage=ObservationCoverage.LIMITED,
        limitations=("Provider capture is partial; the relevant time window is absent.",),
    )
    experiments = (positive, contradictory, inconclusive)
    repository = InMemoryInvestigationExperimentRepository()
    for index, experiment in enumerate(experiments):
        await repository.append_if_version(experiment, expected_version=index)

    timeline = await build_investigation_experiment_timeline(repository, investigation_id)
    (verdict,) = timeline.claims

    assert timeline.stream_version == 3
    assert timeline.experiments == experiments
    assert verdict.claim_id == CLAIM_ID
    assert verdict.status is ConsolidatedClaimStatus.QUALIFIED
    assert verdict.contradictory is True
    assert verdict.verdicts == (
        ExperimentVerdict.SUPPORTS,
        ExperimentVerdict.REFUTES,
        ExperimentVerdict.INCONCLUSIVE,
    )
    assert len(verdict.experiment_ids) == len(verdict.scopes) == 3
    assert verdict.scopes[-1].scope == inconclusive.scope


def test_limited_not_observed_cannot_be_marked_refuted() -> None:
    with pytest.raises(ValueError, match="cannot refute"):
        _experiment(
            1,
            ExperimentVerdict.REFUTES,
            actual_outcome=ObservationOutcome.NOT_OBSERVED,
            coverage=ObservationCoverage.LIMITED,
            limitations=("Only a partial capture was available.",),
        )


@pytest.mark.parametrize(
    ("outcome", "coverage", "verdict", "persisted", "expected"),
    (
        (
            ObservationOutcome.NOT_OBSERVED,
            ObservationCoverage.COMPLETE,
            ExperimentVerdict.REFUTES,
            True,
            AnalysisMatrixStatus.UNKNOWN,
        ),
        (
            ObservationOutcome.NOT_OBSERVED,
            ObservationCoverage.LIMITED,
            ExperimentVerdict.INCONCLUSIVE,
            True,
            AnalysisMatrixStatus.UNKNOWN,
        ),
        (
            ObservationOutcome.NOT_OBSERVED,
            ObservationCoverage.COMPLETE,
            ExperimentVerdict.REFUTES,
            False,
            AnalysisMatrixStatus.UNKNOWN,
        ),
        (
            ObservationOutcome.NOT_OBSERVED,
            ObservationCoverage.COMPLETE,
            ExperimentVerdict.BLOCKED,
            True,
            AnalysisMatrixStatus.UNKNOWN,
        ),
    ),
)
def test_matrix_projection_requires_complete_persisted_observation(
    outcome: ObservationOutcome,
    coverage: ObservationCoverage,
    verdict: ExperimentVerdict,
    persisted: bool,
    expected: AnalysisMatrixStatus,
) -> None:
    experiment = _experiment(
        1,
        verdict,
        actual_outcome=outcome,
        coverage=coverage,
        limitations=("Coverage was partial.",) if coverage is ObservationCoverage.LIMITED else (),
    )
    reference = experiment.observation_refs[0]
    observations = {reference.observation_id: reference.sha256} if persisted else {}

    assert (
        project_experiment_matrix_status(experiment, persisted_observations=observations)
        is expected
    )


def test_matrix_projection_keeps_support_refute_conflict_unknown() -> None:
    investigation_id = uuid4()
    supported = _experiment(1, ExperimentVerdict.SUPPORTS, investigation_id=investigation_id)
    refuted = _experiment(
        2,
        ExperimentVerdict.REFUTES,
        investigation_id=investigation_id,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
    )
    observations = {
        item.observation_refs[0].observation_id: item.observation_refs[0].sha256
        for item in (supported, refuted)
    }

    assert (
        project_claim_matrix_status(
            (supported, refuted), CLAIM_ID, persisted_observations=observations
        )
        is AnalysisMatrixStatus.UNKNOWN
    )


def test_free_text_falsification_criterion_cannot_prove_refutation() -> None:
    experiment = replace(
        _experiment(
            1,
            ExperimentVerdict.REFUTES,
            actual_outcome=ObservationOutcome.NOT_OBSERVED,
        ),
        falsification_criterion="The unrelated host has a blue status page.",
    )
    reference = experiment.observation_refs[0]

    assert (
        project_experiment_matrix_status(
            experiment,
            persisted_observations={reference.observation_id: reference.sha256},
        )
        is AnalysisMatrixStatus.UNKNOWN
    )


def test_tool_arguments_reject_secret_fields_and_round_trip_hashes() -> None:
    experiment = _experiment(1, ExperimentVerdict.SUPPORTS)
    legacy_payload = experiment.to_payload()
    assert "supersedes_experiment_ids" not in legacy_payload
    assert InvestigationExperimentV1.from_payload(experiment.to_payload()) == experiment
    assert len(experiment.canonical_sha256) == 64
    assert experiment.to_payload()["method_mode"] == "passive"
    assert (
        InvestigationExperimentV1.from_payload(legacy_payload).identity_sha256
        == experiment.identity_sha256
    )

    with pytest.raises(ValueError, match="secret-bearing keys"):
        replace(experiment, tool_arguments={"api_key": "never-store-this"})
    with pytest.raises(TypeError, match="method_mode"):
        replace(experiment, method_mode="active")


@pytest.mark.asyncio
async def test_superseded_inconclusive_experiment_stays_in_history_but_not_verdict() -> None:
    investigation_id = uuid4()
    inconclusive = _experiment(
        1,
        ExperimentVerdict.INCONCLUSIVE,
        investigation_id=investigation_id,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
        coverage=ObservationCoverage.LIMITED,
        limitations=("The relevant time window was absent.",),
    )
    supports = _experiment(
        2,
        ExperimentVerdict.SUPPORTS,
        investigation_id=investigation_id,
        supersedes_experiment_ids=(inconclusive.id,),
    )
    repository = InMemoryInvestigationExperimentRepository()
    await repository.append_if_version(inconclusive, expected_version=0)
    await repository.append_if_version(supports, expected_version=1)

    timeline = await build_investigation_experiment_timeline(repository, investigation_id)

    (summary,) = timeline.claims
    assert summary.status is ConsolidatedClaimStatus.SUPPORTED
    assert summary.verdicts == (ExperimentVerdict.SUPPORTS,)
    assert summary.experiment_ids == (inconclusive.id, supports.id)
    assert tuple(scope.experiment_id for scope in summary.scopes) == (
        inconclusive.id,
        supports.id,
    )
    assert supports.to_payload()["supersedes_experiment_ids"] == [str(inconclusive.id)]
    assert InvestigationExperimentV1.from_payload(supports.to_payload()) == supports


@pytest.mark.asyncio
async def test_active_support_and_refutation_remain_contradictory() -> None:
    investigation_id = uuid4()
    supports = _experiment(1, ExperimentVerdict.SUPPORTS, investigation_id=investigation_id)
    refutes = _experiment(
        2,
        ExperimentVerdict.REFUTES,
        investigation_id=investigation_id,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
    )
    repository = InMemoryInvestigationExperimentRepository()
    await repository.append_if_version(supports, expected_version=0)
    await repository.append_if_version(refutes, expected_version=1)

    timeline = await build_investigation_experiment_timeline(repository, investigation_id)
    (summary,) = timeline.claims

    assert summary.status is ConsolidatedClaimStatus.QUALIFIED
    assert summary.contradictory is True
    assert summary.verdicts == (ExperimentVerdict.SUPPORTS, ExperimentVerdict.REFUTES)


def test_supersedes_references_must_be_prior_local_and_acyclic() -> None:
    investigation_id = uuid4()
    earlier = _experiment(1, ExperimentVerdict.SUPPORTS, investigation_id=investigation_id)
    current_id = uuid4()
    _validate_supersedes_experiments(
        (earlier,),
        experiment_id=current_id,
        investigation_id=investigation_id,
        version=2,
        supersedes_experiment_ids=(earlier.id,),
    )
    foreign = _experiment(1, ExperimentVerdict.SUPPORTS)
    future = _experiment(2, ExperimentVerdict.SUPPORTS, investigation_id=investigation_id)
    for candidates, candidate_id in (
        ((earlier,), uuid4()),
        ((earlier, foreign), foreign.id),
        ((earlier, future), future.id),
    ):
        with pytest.raises(ValueError, match="must exist earlier"):
            _validate_supersedes_experiments(
                candidates,
                experiment_id=current_id,
                investigation_id=investigation_id,
                version=2,
                supersedes_experiment_ids=(candidate_id,),
            )

    cyclic_earlier = replace(earlier, supersedes_experiment_ids=(current_id,))
    with pytest.raises(ValueError, match="cannot form a cycle"):
        _validate_supersedes_experiments(
            (cyclic_earlier,),
            experiment_id=current_id,
            investigation_id=investigation_id,
            version=2,
            supersedes_experiment_ids=(earlier.id,),
        )
    with pytest.raises(ValueError, match="cannot supersede itself"):
        replace(earlier, supersedes_experiment_ids=(earlier.id,))


@pytest.mark.asyncio
async def test_experiment_repository_uses_append_only_cas_and_timeline_projection() -> None:
    repository = InMemoryInvestigationExperimentRepository()
    experiment = _experiment(1, ExperimentVerdict.SUPPORTS)

    await repository.append_if_version(experiment, expected_version=0)
    with pytest.raises(VersionConflictError):
        await repository.append_if_version(
            replace(
                experiment,
                id=uuid4(),
                version=2,
                verdict=ExperimentVerdict.REFUTES,
                recorded_at=NOW + timedelta(minutes=5),
            ),
            expected_version=0,
        )
    timeline = await build_investigation_experiment_timeline(
        repository, experiment.investigation_id
    )

    assert timeline.stream_version == 1
    assert timeline.experiments == (experiment,)
    assert timeline.claims[0].status is ConsolidatedClaimStatus.SUPPORTED


def test_next_pivots_are_stable_c1_frontier_node_ids() -> None:
    with pytest.raises(ValueError, match="stable C1 frontier node IDs"):
        replace(_experiment(1, ExperimentVerdict.SUPPORTS), next_pivots=("not-a-pivot-id",))


def test_finding_revisions_keep_logical_identity_and_frozen_revision_ids() -> None:
    logical_id = uuid4()
    current_id = uuid4()
    first = SimpleNamespace(id=logical_id, version=1, created_at=NOW, supersedes=None)
    current = SimpleNamespace(
        id=current_id,
        version=2,
        created_at=NOW + timedelta(seconds=1),
        supersedes=logical_id,
    )
    findings = {logical_id: first, current_id: current}

    chain = _finding_revision_chain(current, findings)

    assert _finding_logical_id(current, findings) == logical_id
    assert [revision.id for revision in chain] == [logical_id, current_id]
    assert [revision.version for revision in chain] == [1, 2]


@pytest.mark.asyncio
async def test_record_tool_requires_g2_and_persisted_investigation_observations() -> None:
    investigation_id = uuid4()
    approval_id = uuid4()
    budget = InvestigationBudget()
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(),
        approval_decision_id=approval_id,
        approval_actor_id="analyst:test",
    )
    observation_id = uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        investigation_id=investigation_id,
        sha256="b" * 64,
        kind="other",
        object_type="network_capture",
        invocation_ref=None,
    )
    unlinked_observation = SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        kind="vt_file_report",
        object_type="vt_file_report",
        sha256="c" * 64,
        invocation_ref=None,
    )

    class _Observations:
        items = (observation, unlinked_observation)

        async def get(self, item_id):
            return next((item for item in self.items if item.id == item_id), None)

        async def list_for_investigation(self, item_id):
            assert item_id == investigation_id
            return self.items

    observations = _Observations()

    class _States:
        async def get(self, item_id):
            assert item_id == investigation_id
            return SimpleNamespace(
                version=2,
                hypotheses=(SimpleNamespace(id="hypothesis-network"),),
            )

    class _Envelopes:
        async def get(self, item_id):
            assert item_id == investigation_id
            return envelope

    class _Claims:
        async def get_claims(self, item_id, claim_ids):
            assert item_id == investigation_id
            return tuple(
                SimpleNamespace(id=claim_id) for claim_id in claim_ids if claim_id == CLAIM_ID
            )

    unlinked_invocation_id = uuid4()
    missing_invocation_id = uuid4()
    unlinked_invocation = SimpleNamespace(
        id=unlinked_invocation_id,
        investigation_id=investigation_id,
        version=1,
        tool_name="protocol.validate",
        schema_version="protocol-validation.v1",
    )

    class _Invocations:
        async def get(self, _item_id):
            return None

        async def list_for_investigation(self, item_id):
            assert item_id == investigation_id
            return (unlinked_invocation,)

    experiments = InMemoryInvestigationExperimentRepository()
    adapter = OperatorExperimentToolAdapter(
        experiments=experiments,
        observations=observations,
        invocations=_Invocations(),
        states=_States(),
        envelopes=_Envelopes(),
        article_snapshots=_Claims(),
    )
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key="record-observation-1",
    )
    hostile_step_id = "deadbeef" * 8
    arguments = {
        "expected_version": 0,
        "hypothesis_id": "hypothesis-network",
        "claim_ids": [CLAIM_ID],
        "prediction": "The sample emits the marker.",
        "falsification_criterion": "The unrelated host has a blue status page.",
        "method": "Passively inspect the persisted capture with parser 4.4.1.",
        "method_mode": "passive",
        "input_type": "sample_sha256",
        "input_refs": [
            {
                "input_type": "sample_sha256",
                "input_id": hostile_step_id,
                "sha256": hostile_step_id,
            },
            {
                "input_type": "capture_sha256",
                "input_id": "b" * 64,
                "sha256": "b" * 64,
            },
            {
                "input_type": "capture_sha256",
                "input_id": "c" * 64,
                "sha256": "c" * 64,
            },
        ],
        "preconditions": ["The observation is bound to the current investigation."],
        "availability": "available",
        "tool_name": "secret.payload",
        "tool_version": "SensitiveData-1",
        "tool_schema_version": "sensitive.payload.v1",
        "tool_arguments": {"byte_0": 77, "byte_1": 90, "byte_2": 0, "flag_0": True},
        "step_ids": [
            hostile_step_id,
            str(unlinked_invocation_id),
            str(missing_invocation_id),
        ],
        "tool_invocation_refs": [],
        "observation_refs": [{"observation_id": str(observation_id), "sha256": "b" * 64}],
        "expected": "The marker is present.",
        "actual": "Persisted proof text: OPERATOR_TEST_SECRET",
        "actual_outcome": "not_observed",
        "observation_coverage": "complete",
        "started_at": NOW.isoformat(),
        "completed_at": (NOW + timedelta(seconds=1)).isoformat(),
        "scope": "Static capture inspection; execution remains unverified.",
        "limitations": [],
        "verdict": "refutes",
        "implication": "The cited capture supports this bounded claim.",
        "next_pivots": [hostile_step_id, "c" * 64],
    }

    denied = await adapter.execute(arguments, replace(context, envelope=None))
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert denied.error_code == "g2_approval_required"

    saved = await adapter.execute(arguments, context)
    assert saved.status is ToolInvocationStatus.SUCCEEDED
    claim_verdict = saved.output["claim_verdicts"][0]
    assert claim_verdict["status"] == "inconclusive"
    assert claim_verdict["verdicts"] == ("refutes",)
    assert claim_verdict["verdict_assertion_labels"] == (
        "Verdict enregistré : refutes (assertion, non conclusion)",
    )
    assert saved.output["verdict"] == "refutes"
    assert saved.output["verdict_assertion_label"] == (
        "Verdict enregistré : refutes (assertion, non conclusion)"
    )
    recorded_experiment = saved.output["experiment"]
    assert recorded_experiment["step_ids"] == ("[redacted]", "[redacted]", "[redacted]")
    assert recorded_experiment["next_pivots"] == ("[redacted]", "[redacted]")
    assert recorded_experiment["input_refs"] == (
        {
            "input_type": "sample_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {"input_type": "capture_sha256", "input_id": "b" * 64, "sha256": "b" * 64},
        {
            "input_type": "capture_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
    )
    assert recorded_experiment["observation_refs"] == (
        {"observation_id": str(observation_id), "sha256": "b" * 64},
    )
    assert recorded_experiment["tool_name"] == "[redacted]"
    assert recorded_experiment["tool_version"] == "[redacted]"
    assert recorded_experiment["tool_schema_version"] == "[redacted]"
    assert all(
        value not in repr(saved.output)
        for value in (hostile_step_id, "secret.payload", "SensitiveData-1", "sensitive.payload.v1")
    )
    assert "OPERATOR_TEST_SECRET" not in repr(saved.output)
    listed = await _ExperimentListToolAdapter(
        experiments, observations=observations, invocations=_Invocations()
    ).execute({}, context)
    list_payload = listed.output
    assert list_payload["claims"][0]["status"] == "inconclusive"
    assert list_payload["claims"][0]["verdicts"] == ("refutes",)
    projected_experiment = list_payload["experiments"][0]
    assert projected_experiment["verdict"] == "refutes"
    assert projected_experiment["verdict_assertion_label"] == (
        "Verdict enregistré : refutes (assertion, non conclusion)"
    )
    assert projected_experiment["step_ids"] == (
        "[redacted]",
        "[redacted]",
        "[redacted]",
    )
    assert projected_experiment["next_pivots"] == ("[redacted]", "[redacted]")
    assert projected_experiment["falsification_criterion"] == "[redacted]"
    assert projected_experiment["actual"] == "[redacted]"
    assert projected_experiment["tool_arguments"] == {}
    assert projected_experiment["tool_name"] == "[redacted]"
    assert projected_experiment["tool_version"] == "[redacted]"
    assert projected_experiment["tool_schema_version"] == "[redacted]"
    assert "OPERATOR_TEST_SECRET" not in repr(list_payload)
    assert hostile_step_id not in repr(list_payload)
    assert projected_experiment["input_refs"] == (
        {
            "input_type": "sample_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {"input_type": "capture_sha256", "input_id": "b" * 64, "sha256": "b" * 64},
        {
            "input_type": "capture_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
    )
    assert projected_experiment["observation_refs"] == (
        {"observation_id": str(observation_id), "sha256": "b" * 64},
    )
    assert str(unlinked_invocation_id) not in repr(saved.output)
    assert str(missing_invocation_id) not in repr(saved.output)
    assert str(unlinked_invocation_id) not in repr(list_payload)
    assert str(missing_invocation_id) not in repr(list_payload)
    assert "c" * 64 not in repr(saved.output)
    assert "c" * 64 not in repr(list_payload)
    assert all(
        value not in repr(list_payload)
        for value in ("secret.payload", "SensitiveData-1", "sensitive.payload.v1")
    )
    assert (await experiments.list_for_investigation(investigation_id))[0].observation_refs[
        0
    ].sha256 == "b" * 64

    invalid_reference = dict(arguments)
    invalid_reference["expected_version"] = 1
    invalid_reference["observation_refs"] = [
        {"observation_id": str(observation_id), "sha256": "d" * 64}
    ]
    rejected = await adapter.execute(invalid_reference, replace(context, idempotency_key="bad-sha"))
    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "experiment_observation_ref_mismatch"
    assert "SHA-256" in rejected.message

    unknown_hypothesis = dict(arguments)
    unknown_hypothesis["hypothesis_id"] = "hypothesis-missing"
    rejected_hypothesis = await adapter.execute(
        unknown_hypothesis, replace(context, idempotency_key="unknown-hypothesis")
    )
    assert rejected_hypothesis.error_code == "experiment_hypothesis_unknown"
    assert "hypothesis" in rejected_hypothesis.message.casefold()

    unknown_claim = dict(arguments)
    unknown_claim["claim_ids"] = ["claim:not-in-article"]
    rejected_claim = await adapter.execute(
        unknown_claim, replace(context, idempotency_key="unknown-claim")
    )
    assert rejected_claim.error_code == "experiment_claim_unknown"
    assert "claim" in rejected_claim.message.casefold()

    observations.items = (
        SimpleNamespace(**{**vars(observation), "investigation_id": uuid4()}),
        unlinked_observation,
    )
    rejected_observation = await adapter.execute(
        arguments, replace(context, idempotency_key="foreign-observation")
    )
    assert rejected_observation.error_code == "experiment_observation_ref_mismatch"
    assert "another investigation" in rejected_observation.message


@pytest.mark.asyncio
async def test_record_tool_registry_requires_operator_write_capability() -> None:
    investigation_id = uuid4()
    registry = InMemoryToolRegistry()
    register_operator_experiment_tools(
        registry,
        experiments=InMemoryInvestigationExperimentRepository(),
        observations=SimpleNamespace(),
        invocations=SimpleNamespace(),
        states=SimpleNamespace(),
        envelopes=SimpleNamespace(),
        article_snapshots=SimpleNamespace(),
    )
    definition = registry.get(EXPERIMENT_RECORD_TOOL, EXPERIMENT_TOOL_SCHEMA_VERSION)
    assert definition is not None
    assert definition[0].capability_key == "operator.write"
    assert "supersedes_experiment_ids" in definition[0].input_schema["properties"]
    assert "supersedes_experiment_ids" not in definition[0].input_schema["required"]
    hypothesis_definition = registry.get(
        HYPOTHESIS_DECLARE_TOOL, HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION
    )
    assert hypothesis_definition is not None
    assert hypothesis_definition[0].capability_key == "operator.write"
    dispatcher = RegistryToolDispatcher(registry)
    result = await dispatcher.dispatch(
        ToolExecutionContext(
            investigation_id=investigation_id,
            envelope=None,
            budget=InvestigationBudget(),
            idempotency_key="unauthorized-record",
        ),
        tool_name=EXPERIMENT_RECORD_TOOL,
        schema_version=EXPERIMENT_TOOL_SCHEMA_VERSION,
        arguments={},
        purpose="Record a bounded experiment",
        operation_key="unauthorized-record",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert "operator.write" in result.message

    declaration = await dispatcher.dispatch(
        ToolExecutionContext(
            investigation_id=investigation_id,
            envelope=None,
            budget=InvestigationBudget(),
            idempotency_key="unauthorized-declare",
        ),
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={},
        purpose="Declare a hypothesis without operator.write",
        operation_key="unauthorized-declare",
    )
    assert declaration.status is ToolInvocationStatus.UNAVAILABLE
    assert "operator.write" in declaration.message
