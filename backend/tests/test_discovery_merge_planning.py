from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery.cumulative.service import (
    ChatGptMergePlanner,
    DiscoveryBlockingStrategy,
    HumanMergeDecision,
    HumanMergePlanner,
    MergeModelUnavailableError,
    TargetedMergePlanner,
    apply_discovery_merge_plan,
    build_discovery_delta,
    build_merge_handles,
    make_merge_run,
    project_merge_subject,
)
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelRequest,
)
from cti_app.domain.discovery import CandidateTopic
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
    discovery_candidate_key,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelRunStatus
from tests.test_discovery_cumulative import _batch, _candidate, _intake


class RecordingBridgeCapabilitiesProvider:
    """Stands in for the bridge transport just to record archive calls.

    DELETE_ON_SUCCESS is declared on every merge conversation; this is what
    proves the planner actually closes them rather than only declaring it.
    """

    def __init__(self) -> None:
        self.archived: list[UUID] = []

    async def capabilities(self) -> dict[str, object]:
        return {}

    async def archive_conversation(self, conversation_id: UUID) -> None:
        self.archived.append(conversation_id)

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, object]:
        return {}


class RecordingDraftingModel:
    def __init__(self, outputs: Sequence[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[ModelRequest] = []

    async def draft(
        self, request: ModelRequest, output_schema: type[object] | None = None
    ) -> ModelExecution:
        del output_schema
        self.requests.append(request)
        output = self.outputs.pop(0)
        run = ModelRun(
            provider=ModelProvider.OPENAI,
            model_role=ModelRole.DRAFTING,
            requested_model="chatgpt-web",
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash="a" * 64,
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=request.parameters,
            id=request.run_id or uuid4(),
            status=ModelRunStatus.SUCCEEDED,
            output_references=(f"memory://output/{len(self.requests)}",),
        )
        return ModelExecution(run, output_text=output)


@pytest.mark.asyncio
async def test_chatgpt_merge_uses_fresh_non_web_request_and_opaque_handles() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id, [_candidate("APT42 SpearSpecter", "https://example.test/a")]
    )
    batch = _batch(
        edition_id,
        [_candidate("APT42 SpearSpecter cloud update", "https://example.test/b")],
    )
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    plan = DiscoveryMergePlanV1(
        groups=[
            DiscoveryMergeGroup(
                existing_subject_handles=["X1"],
                incoming_candidate_handles=["C1"],
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="same campaign",
                evidence=MergeEvidence(shared_campaigns=["SpearSpecter"]),
            )
        ]
    )
    model = RecordingDraftingModel([plan.model_dump_json()])

    outcome = await ChatGptMergePlanner(model).plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )

    assert outcome.plan.groups[0].existing_subject_handles == ["X1"]
    request = model.requests[0]
    assert request.routing_hint.value == "discovery_merge"
    assert request.conversation is not None and request.conversation.mode == "fresh"
    assert str(parent.subjects[0].subject_id) not in request.text
    assert str(delta.candidates[0].candidate_key) not in request.text
    assert "web_search" not in request.text


@pytest.mark.asyncio
async def test_chatgpt_merge_repairs_structure_once_and_preserves_distinct_subject() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id, [_candidate("Screening Serpens MiniUpdate", "https://example.test/a")]
    )
    batch = _batch(edition_id, [_candidate("Nimbus Manticore MiniFast", "https://example.test/b")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    distinct = DiscoveryMergePlanV1(
        groups=[
            DiscoveryMergeGroup(
                existing_subject_handles=[],
                incoming_candidate_handles=["C1"],
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="distinct campaigns and malware",
                evidence=MergeEvidence(semantic_basis=["distinct"]),
            )
        ]
    )
    model = RecordingDraftingModel(["{}", distinct.model_dump_json()])

    outcome = await ChatGptMergePlanner(model).plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )

    assert len(model.requests) == 2
    assert outcome.validation_status is MergeValidationStatus.REPAIRED
    assert outcome.plan.groups[0].existing_subject_handles == []


@pytest.mark.asyncio
async def test_chatgpt_merge_archives_its_conversation_on_direct_success() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id, [_candidate("APT42 SpearSpecter", "https://example.test/a")]
    )
    batch = _batch(
        edition_id,
        [_candidate("APT42 SpearSpecter cloud update", "https://example.test/b")],
    )
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    plan = DiscoveryMergePlanV1(
        groups=[
            DiscoveryMergeGroup(
                existing_subject_handles=["X1"],
                incoming_candidate_handles=["C1"],
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="same campaign",
                evidence=MergeEvidence(shared_campaigns=["SpearSpecter"]),
            )
        ]
    )
    model = RecordingDraftingModel([plan.model_dump_json()])
    bridge = RecordingBridgeCapabilitiesProvider()

    await ChatGptMergePlanner(model, bridge_capabilities_provider=bridge).plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )

    conversation = model.requests[0].conversation
    assert conversation is not None
    assert bridge.archived == [conversation.id]


@pytest.mark.asyncio
async def test_chatgpt_merge_archives_both_conversations_after_repair() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id, [_candidate("Screening Serpens MiniUpdate", "https://example.test/a")]
    )
    batch = _batch(edition_id, [_candidate("Nimbus Manticore MiniFast", "https://example.test/b")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    distinct = DiscoveryMergePlanV1(
        groups=[
            DiscoveryMergeGroup(
                existing_subject_handles=[],
                incoming_candidate_handles=["C1"],
                confidence=MergeConfidence.HIGH,
                disposition=MergeDisposition.APPLY,
                rationale="distinct campaigns and malware",
                evidence=MergeEvidence(semantic_basis=["distinct"]),
            )
        ]
    )
    model = RecordingDraftingModel(["{}", distinct.model_dump_json()])
    bridge = RecordingBridgeCapabilitiesProvider()

    await ChatGptMergePlanner(model, bridge_capabilities_provider=bridge).plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )

    assert len(model.requests) == 2
    first_conversation = model.requests[0].conversation
    second_conversation = model.requests[1].conversation
    assert first_conversation is not None
    assert second_conversation is not None
    assert bridge.archived == [first_conversation.id, second_conversation.id]


@pytest.mark.asyncio
async def test_chatgpt_merge_leaves_conversation_for_debugging_when_unresolved() -> None:
    """A failure that never reaches a valid plan keeps its transcript around —
    deleting it would remove the only way to see what went wrong."""
    edition_id = uuid4()
    batch = _batch(edition_id, [_candidate("A", "https://example.test/a")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    model = StalledDraftingModel()
    bridge = RecordingBridgeCapabilitiesProvider()

    with pytest.raises(MergeModelUnavailableError):
        await ChatGptMergePlanner(model, bridge_capabilities_provider=bridge).plan(
            None,
            delta,
            build_merge_handles(None, delta),
            edition_id=edition_id,
            external_llm_allowed=True,
            sensitivity="internal",
        )

    assert bridge.archived == []


@pytest.mark.asyncio
async def test_chatgpt_merge_does_not_call_model_when_external_policy_blocks_it() -> None:
    edition_id = uuid4()
    batch = _batch(edition_id, [_candidate("A", "https://example.test/a")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    model = RecordingDraftingModel([])

    with pytest.raises(ExternalModelBlockedError, match="external_merge_not_allowed"):
        await ChatGptMergePlanner(model).plan(
            None,
            delta,
            build_merge_handles(None, delta),
            edition_id=edition_id,
            external_llm_allowed=False,
            sensitivity="internal",
        )
    assert model.requests == []


class StalledDraftingModel:
    """Reproduces a bridge that stops without ever producing a final answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def draft(
        self, request: ModelRequest, output_schema: type[object] | None = None
    ) -> ModelExecution:
        del output_schema
        self.calls += 1
        run = ModelRun(
            provider=ModelProvider.OPENAI,
            model_role=ModelRole.DRAFTING,
            requested_model="chatgpt-web",
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash="a" * 64,
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=request.parameters,
            id=request.run_id or uuid4(),
            status=ModelRunStatus.NEEDS_REVIEW,
            error_code="active_signal_stalled",
            error_message="ChatGPT s'est arrêté sans produire de réponse finale.",
        )
        return ModelExecution(run, output_text=None)


@pytest.mark.asyncio
async def test_a_stalled_merge_model_is_not_reported_as_an_invalid_plan() -> None:
    """A silent bridge is an incident, not an editorial decision.

    Reporting it as `plan_invalid_after_repair` used to persist a merge run with
    zero groups, which no human could resolve and which blocked every later
    contribution behind it.
    """
    edition_id = uuid4()
    batch = _batch(edition_id, [_candidate("A", "https://example.test/a")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    model = StalledDraftingModel()

    with pytest.raises(MergeModelUnavailableError) as raised:
        await ChatGptMergePlanner(model).plan(
            None,
            delta,
            build_merge_handles(None, delta),
            edition_id=edition_id,
            external_llm_allowed=True,
            sensitivity="internal",
        )

    assert raised.value.code == "active_signal_stalled"
    # No repair attempt: there was no answer to repair.
    assert model.calls == 1


def test_merge_projection_is_an_explicit_allowlist_without_internal_fields() -> None:
    candidate = _candidate("A", "https://example.test/a")
    projection = project_merge_subject("C1", candidate)

    assert set(projection) == {
        "handle",
        "title",
        "summary",
        "actors",
        "campaigns",
        "malware",
        "cves",
        "victims",
        "sectors",
        "countries",
        "likely_artifacts",
        "technical_potential",
        "uncertainties",
        "sources",
    }
    sources = projection["sources"]
    assert isinstance(sources, list)
    assert set(sources[0]) == {
        "canonical_url",
        "title",
        "publisher",
        "role",
        "published_at",
        "event_date",
    }
    assert "id" not in str(projection)
    assert "external_llm_allowed" not in str(projection)


@pytest.mark.asyncio
async def test_human_planner_confirms_existing_subject_fusion_through_same_applier() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id,
        [
            _candidate("Campaign A", "https://example.test/a"),
            _candidate("Campaign A duplicate", "https://example.test/b"),
        ],
    )
    batch = _batch(edition_id, [_candidate("Campaign A update", "https://example.test/c")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    original = DiscoveryMergePlanV1(
        groups=[
            DiscoveryMergeGroup(
                existing_subject_handles=["X1", "X2"],
                incoming_candidate_handles=["C1"],
                confidence=MergeConfidence.MEDIUM,
                disposition=MergeDisposition.REVIEW,
                rationale="possible duplicate",
                evidence=MergeEvidence(),
            )
        ]
    )
    planner = HumanMergePlanner(
        original,
        [HumanMergeDecision(0, "merge_existing", target_subject_handle="X1")],
    )
    outcome = await planner.plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=False,
        sensitivity="internal",
    )
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=parent,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
        outcome=outcome,
    )

    applied = apply_discovery_merge_plan(
        parent,
        delta,
        outcome.plan,
        resolved_handles=handles,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
        actor_id="analyst",
    )

    assert len(applied.snapshot.subjects) == 1
    assert applied.snapshot.subjects[0].subject_id == handles.existing["X1"]
    assert applied.merge_events[0].from_subject_id == handles.existing["X2"]
    assert applied.merge_events[0].into_subject_id == handles.existing["X1"]


@pytest.mark.asyncio
async def test_targeted_planner_deterministically_targets_the_known_subject() -> None:
    """The manual-URL-attach path already knows exactly which subject an
    incoming candidate belongs to; TargetedMergePlanner must group it there
    regardless of what other subjects exist, without needing
    `candidates_match_strongly` to (dis)agree — unlike `HeuristicMergePlanner`,
    which could refuse to pick a subject, or pick the wrong one, if identity
    matching is ambiguous."""
    edition_id = uuid4()
    parent = await _bootstrap(
        edition_id,
        [
            _candidate("Campaign A", "https://example.test/a"),
            _candidate("Campaign B", "https://example.test/b"),
        ],
    )
    assert len(parent.subjects) == 2
    target_subject_id = next(
        s.subject_id for s in parent.subjects if s.canonical_title == "Campaign A"
    )

    # Deliberately a title that would NOT deterministically match "Campaign A"
    # under HeuristicMergePlanner's identity rules — TargetedMergePlanner must
    # still land it on the requested subject.
    batch = _batch(edition_id, [_candidate("Unrelated headline", "https://example.test/c")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    incoming_candidate_key = discovery_candidate_key(intake.id, "S1")

    planner = TargetedMergePlanner(target_subject_id, incoming_candidate_key)
    outcome = await planner.plan(
        parent,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=False,
        sensitivity="internal",
    )

    assert len(outcome.plan.groups) == 1
    group = outcome.plan.groups[0]
    assert handles.existing[group.existing_subject_handles[0]] == target_subject_id
    assert group.disposition is MergeDisposition.APPLY

    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=parent,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
        outcome=outcome,
    )
    applied = apply_discovery_merge_plan(
        parent,
        delta,
        outcome.plan,
        resolved_handles=handles,
        planner_kind=DiscoveryPlannerKind.HUMAN,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    )

    assert len(applied.snapshot.subjects) == 2
    updated = next(s for s in applied.snapshot.subjects if s.subject_id == target_subject_id)
    assert {source.canonical_url for source in updated.candidate.sources} == {
        "https://example.test/a",
        "https://example.test/c",
    }


@pytest.mark.asyncio
async def test_targeted_planner_rejects_a_subject_that_is_not_in_scope() -> None:
    edition_id = uuid4()
    parent = await _bootstrap(edition_id, [_candidate("Campaign A", "https://example.test/a")])
    batch = _batch(edition_id, [_candidate("Campaign A", "https://example.test/b")])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(parent, delta)
    incoming_candidate_key = discovery_candidate_key(intake.id, "S1")

    planner = TargetedMergePlanner(uuid4(), incoming_candidate_key)
    with pytest.raises(ValueError):
        await planner.plan(
            parent,
            delta,
            handles,
            edition_id=edition_id,
            external_llm_allowed=False,
            sensitivity="internal",
        )


@pytest.mark.asyncio
async def test_blocking_keeps_strict_shared_entity_when_snapshot_is_large() -> None:
    edition_id = uuid4()
    candidates = [
        _candidate(f"Unrelated subject {index}", f"https://example.test/{index}")
        for index in range(31)
    ]
    candidates[17].malware = ("Dindoor",)
    parent = await _bootstrap(edition_id, candidates)
    incoming = _candidate("Different wording", "https://new.example.test/report")
    incoming.malware = ("Dindoor",)
    batch = _batch(edition_id, [incoming])
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)

    selected = DiscoveryBlockingStrategy().select(parent, delta)

    expected = next(
        subject for subject in parent.subjects if "Dindoor" in subject.candidate.malware
    )
    assert expected.subject_id in {subject.subject_id for subject in selected}
    assert len(selected) < len(parent.subjects)


async def _bootstrap(edition_id: UUID, candidates: list[CandidateTopic]) -> DiscoverySnapshot:
    batch = _batch(edition_id, candidates)
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    from cti_app.application.discovery.cumulative.service import HeuristicMergePlanner

    planner = HeuristicMergePlanner()
    outcome = await planner.plan(
        None,
        delta,
        handles,
        edition_id=edition_id,
        external_llm_allowed=True,
        sensitivity="internal",
    )
    run = make_merge_run(
        edition_id=edition_id,
        parent_snapshot=None,
        intake=intake,
        delta=delta,
        planner=planner,
        handles=handles,
        outcome=outcome,
    )
    return apply_discovery_merge_plan(
        None,
        delta,
        outcome.plan,
        resolved_handles=handles,
        planner_kind=run.planner_kind,
        edition_id=edition_id,
        intake_id=intake.id,
        merge_run_id=run.id,
    ).snapshot
