from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.investigation_persistence import (
    InMemoryFindingRepository,
    InMemoryToolInvocationRepository,
    VersionConflictError,
)
from cti_app.domain.investigation import (
    AgendaItem,
    AgentNextAction,
    BudgetDimension,
    BudgetExceeded,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationFinding,
    NoveltyClass,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocation,
    ToolInvocationStatus,
    investigation_budget_from_loop_budget,
    tool_operation_key,
)


def test_supported_finding_requires_observation() -> None:
    with pytest.raises(ValueError, match="at least one observation"):
        InvestigationFinding(
            investigation_id=uuid4(),
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.SUPPORTED,
            statement="The sample loads a second stage.",
            observation_refs=(),
        )


def test_tool_invocation_has_stable_operation_identity_and_rejects_secrets() -> None:
    investigation_id = uuid4()
    args = {"sha256": "a" * 64, "limit": 5}
    first = tool_operation_key(
        investigation_id=investigation_id,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=args,
        purpose="check seed",
    )
    second = tool_operation_key(
        investigation_id=investigation_id,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={"limit": 5, "sha256": "a" * 64},
        purpose="check seed",
    )
    assert first == second
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=first,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=args,
        purpose="check seed",
    )
    with pytest.raises(TypeError):
        invocation.normalized_parameters["limit"] = 6  # type: ignore[index]
    with pytest.raises(ValueError, match="secret"):
        tool_operation_key(
            investigation_id=investigation_id,
            tool_name="vt.file_report",
            schema_version="1",
            normalized_parameters={"api_key": "never-persist-this"},
            purpose="check seed",
        )


def test_tool_invocation_transitions_are_typed_and_monotonic() -> None:
    invocation = ToolInvocation(
        investigation_id=uuid4(),
        operation_key="operation-1",
        tool_name="local.scan",
        schema_version="1",
        normalized_parameters={},
        purpose="inspect fixture",
    )
    running = invocation.transition(ToolInvocationStatus.RUNNING)
    finished = running.transition(ToolInvocationStatus.EMPTY)
    assert running.version == 2
    assert finished.version == 3
    with pytest.raises(ValueError, match="Invalid tool invocation transition"):
        finished.transition(ToolInvocationStatus.RUNNING)


def test_evidence_observation_is_immutable_and_requires_exact_provenance() -> None:
    observation = EvidenceObservation(
        investigation_id=uuid4(),
        kind=EvidenceKind.BINARY_STRING,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"fixture").hexdigest(),
        source="strings",
        source_version="2.0",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id="b" * 64,
        limitations=("strings are not execution evidence",),
    )
    with pytest.raises(AttributeError):
        observation.sha256 = "0" * 64  # type: ignore[misc]
    assert observation.object_id == "b" * 64
    assert observation.limitations == ("strings are not execution evidence",)


def test_sample_confirmation_requires_human_and_appends_qualification_change() -> None:
    sample = SampleAssociation(
        investigation_id=uuid4(), sample_sha256="c" * 64, origin=SampleOrigin.VT_PIVOT
    )
    with pytest.raises(ValueError, match="identified human"):
        sample.change_qualification(
            SampleQualification.CONFIRMED,
            actor_kind=QualificationActorKind.AGENT,
            actor_id="bridge",
            reason="A model suggested it.",
        )
    related = sample.change_qualification(
        SampleQualification.RELATED,
        actor_kind=QualificationActorKind.AGENT,
        actor_id="bridge",
        reason="A parent-child relation was observed.",
    )
    confirmed = related.change_qualification(
        SampleQualification.CONFIRMED,
        actor_kind=QualificationActorKind.HUMAN,
        actor_id="analyst:42",
        reason="The analyst reviewed the sample comparison.",
    )
    assert confirmed.version == 3
    assert [change.to_qualification for change in confirmed.changes] == [
        SampleQualification.RELATED,
        SampleQualification.CONFIRMED,
    ]


def test_investigation_budget_reserve_commit_release_are_idempotent_and_bounded() -> None:
    budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: 3, BudgetDimension.DOWNLOADS: 1})
    reserved = budget.reserve(
        "lookup-1", {BudgetDimension.VT_UNITS: 2, BudgetDimension.DOWNLOADS: 1}
    )
    assert (
        budget.reserve("lookup-1", {BudgetDimension.VT_UNITS: 2, BudgetDimension.DOWNLOADS: 1})
        == reserved
    )
    with pytest.raises(BudgetExceeded) as raised:
        budget.reserve("lookup-2", {BudgetDimension.VT_UNITS: 2})
    assert raised.value.dimension is BudgetDimension.VT_UNITS
    committed = budget.commit("lookup-1", {BudgetDimension.VT_UNITS: 1})
    assert budget.commit("lookup-1") == committed
    assert budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert budget.consumed[BudgetDimension.DOWNLOADS] == 0

    budget.reserve("lookup-3", {BudgetDimension.VT_UNITS: 1})
    released = budget.release("lookup-3")
    assert budget.release("lookup-3") == released
    assert budget.reserved[BudgetDimension.VT_UNITS] == 0


def test_revoked_budget_can_restore_and_commit_an_in_flight_reservation() -> None:
    approved = InvestigationBudget(
        limits={BudgetDimension.VT_UNITS: 2, BudgetDimension.TOOL_SECONDS: 120}
    )
    reservation = approved.reserve(
        "vt-read-1",
        {BudgetDimension.VT_UNITS: 1, BudgetDimension.TOOL_SECONDS: 120},
    )

    revoked = InvestigationBudget(
        limits={BudgetDimension.VT_UNITS: 0, BudgetDimension.TOOL_SECONDS: 120}
    )
    revoked.restore_reservation(reservation)

    assert revoked.limits[BudgetDimension.VT_UNITS] == 0
    assert revoked.reserved[BudgetDimension.VT_UNITS] == 1
    with pytest.raises(BudgetExceeded) as raised:
        revoked.reserve("vt-read-2", {BudgetDimension.VT_UNITS: 1})
    assert raised.value.dimension is BudgetDimension.VT_UNITS

    revoked.commit(
        "vt-read-1",
        {BudgetDimension.VT_UNITS: 1, BudgetDimension.TOOL_SECONDS: 4},
    )
    assert revoked.consumed[BudgetDimension.VT_UNITS] == 1
    assert revoked.remaining[BudgetDimension.VT_UNITS] == 0


def test_agenda_ranks_questions_by_potential_and_supports_flexible_actions() -> None:
    agenda = InvestigationAgenda(
        (
            AgendaItem("Can the claimed loader behavior be verified?", 0.95, 1),
            AgendaItem("Is the IP attribution time-bounded?", 0.7, 2),
        )
    )
    assert len(agenda.items) == 2
    assert AgentNextAction.SWITCH_HYPOTHESIS.value == "switch_hypothesis"
    with pytest.raises(ValueError, match="descending potential"):
        InvestigationAgenda(
            (
                AgendaItem("Lower value", 0.3, 1),
                AgendaItem("Higher value", 0.8, 2),
            )
        )


def test_detection_versions_are_hash_linked_without_a_sample_threshold() -> None:
    source = 'rule sample { strings: $a = "fixture" condition: $a }'
    v1 = DetectionCandidateVersion(
        investigation_id=uuid4(),
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(),
        status=DetectionCandidateStatus.EXPERIMENTAL,
    )
    v2_source = source + "\n"
    v2 = DetectionCandidateVersion(
        investigation_id=v1.investigation_id,
        candidate_id=v1.candidate_id,
        source=v2_source,
        source_sha256=hashlib.sha256(v2_source.encode()).hexdigest(),
        motif_provenance=(),
        version=2,
        parent_version_id=v1.id,
    )
    assert (v1.version_label, v2.version_label) == ("v1", "v2")
    assert v1.test_refs == ()


def test_loop_budget_projection_is_pure_and_keeps_independent_caps() -> None:
    class LegacyBudget:
        max_cycles = 4

    projected = investigation_budget_from_loop_budget(LegacyBudget())
    assert projected.limits == {BudgetDimension.BRIDGE_TURNS: 4}
    assert projected.limits.get(BudgetDimension.VT_UNITS) is None


def test_supported_novelty_is_explicitly_article_scoped() -> None:
    assert NoveltyClass.NEW_TO_ARTICLE.value == "new_to_article"


@pytest.mark.asyncio
async def test_in_memory_repositories_are_idempotent_and_compare_and_swap_versions() -> None:
    invocations = InMemoryToolInvocationRepository()
    invocation = ToolInvocation(
        investigation_id=uuid4(),
        operation_key="persisted-operation",
        tool_name="local.read",
        schema_version="1",
        normalized_parameters={"sample_sha256": "f" * 64},
        purpose="read cached analysis",
    )
    stored = await invocations.add_if_absent(invocation)
    assert await invocations.add_if_absent(invocation) == stored
    assert await invocations.get_by_operation_key("persisted-operation") == stored
    running = invocation.transition(ToolInvocationStatus.RUNNING)
    await invocations.update_if_version(running, expected_version=1)
    assert await invocations.update_if_version(running, expected_version=1) == running
    assert await invocations.add_if_absent(invocation) == running
    stale = invocation.transition(ToolInvocationStatus.FAILED)
    with pytest.raises(VersionConflictError):
        await invocations.update_if_version(stale, expected_version=1)

    findings = InMemoryFindingRepository()
    first = InvestigationFinding(
        investigation_id=invocation.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.QUALIFIED,
        statement="The report suggests a loader capability.",
        observation_refs=(),
    )
    await findings.append_version(first, expected_previous_id=None)
    second = InvestigationFinding(
        id=uuid4(),
        investigation_id=first.investigation_id,
        kind=first.kind,
        assessment=FindingAssessment.SUPPORTED,
        statement="The report confirms a loader capability.",
        observation_refs=(uuid4(),),
        version=2,
        supersedes=first.id,
    )
    await findings.append_version(second, expected_previous_id=first.id)
    with pytest.raises(VersionConflictError):
        await findings.append_version(
            InvestigationFinding(
                id=uuid4(),
                investigation_id=first.investigation_id,
                kind=first.kind,
                assessment=FindingAssessment.QUALIFIED,
                statement="A competing revision.",
                observation_refs=(),
                version=2,
                supersedes=first.id,
            ),
            expected_previous_id=first.id,
        )
