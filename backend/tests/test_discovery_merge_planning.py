from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery_cumulative import (
    ChatGptMergePlanner,
    DiscoveryBlockingStrategy,
    HumanMergeDecision,
    HumanMergePlanner,
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
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
    MergeValidationStatus,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelRunStatus
from tests.test_discovery_cumulative import _batch, _candidate, _intake


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
    assert set(projection["sources"][0]) == {
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


async def _bootstrap(edition_id: UUID, candidates: list[object]):
    batch = _batch(edition_id, candidates)
    intake = _intake(batch)
    delta = build_discovery_delta(intake, batch)
    handles = build_merge_handles(None, delta)
    from cti_app.application.discovery_cumulative import HeuristicMergePlanner

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
