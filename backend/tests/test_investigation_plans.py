"""Deterministic oracles for durable investigation plans (ADR-0011 D1/D2, 04A1).

The append-only *storage* guarantee is enforced by PostgreSQL triggers and is
exercised by ``tests/integration/test_investigation_plans_postgres.py``; this
module proves the domain and application semantics: a version can only be born
from a verified persisted ``advisor_response_received`` observation of the same
investigation, the head advances by CAS, and replays never rewrite a version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_plans import (
    InvestigationPlanIdempotencyError,
    InvestigationPlanProvenanceError,
    InvestigationPlanService,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    INVESTIGATION_PLAN_SCHEMA_VERSION,
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProseAuthority,
    ProseAuthorityModeError,
    require_prose_authority_mode,
)
from cti_app.infrastructure.database.repositories.production import (
    _analyst_investigation_row,
    _analyst_investigation_values,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000201")
START_EVIDENCE_REF = UUID("00000000-0000-0000-0000-000000000202")


class _Investigations:
    def __init__(self, known: set[UUID]) -> None:
        self.known = known

    async def get(self, investigation_id: UUID):
        return SimpleNamespace(id=investigation_id) if investigation_id in self.known else None


def _lead(title: str = "Inspect the loader") -> InvestigationLeadV1:
    return InvestigationLeadV1(
        title=title,
        objective="Identify the loader's network gates.",
        cti_justification="The article claims a two-stage downloader.",
        claim_refs=("claim-1",),
        start_evidence_refs=(str(START_EVIDENCE_REF),),
        sample_refs=(SHA_A,),
        tools=("pe.strings", "ghidra.decompile"),
        preconditions=("sample available in quarantine",),
        interest="high",
        conclusion_criteria="Two independent C2 gates are proven from bytes.",
        limits="No execution outside the confined profile.",
        depends_on=(),
    )


def _plan(
    *,
    investigation_id: UUID = INVESTIGATION_ID,
    version: int = 1,
    title: str = "Inspect the loader",
) -> InvestigationPlanV1:
    return InvestigationPlanV1(
        investigation_id=investigation_id,
        version=version,
        leads=(_lead(title),),
    )


def _invocation(
    investigation_id: UUID = INVESTIGATION_ID,
    *,
    observation_id: UUID,
    consultation_type: str = "plan_investigation",
    tool_name: str = "advisor.consult",
    status: ToolInvocationStatus = ToolInvocationStatus.SUCCEEDED,
    link: bool = True,
) -> ToolInvocation:
    return ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"advisor-{uuid4()}",
        tool_name=tool_name,
        schema_version="advisor-consult.v1",
        normalized_parameters={"consultation_type": consultation_type, "question": "plan"},
        purpose="Ask Bridge for an investigation plan",
        status=status,
        response_refs=(observation_id,) if link else (),
        created_at=NOW,
        updated_at=NOW,
    )


def _observation(
    investigation_id: UUID = INVESTIGATION_ID,
    *,
    source: str = "advisor.consult.response",
    object_type: str = "advisor_response_received",
    invocation_ref: UUID | None = None,
    sha256: str = SHA_A,
) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=sha256,
        source=source,
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type=object_type,
        object_id=f"advisor-{uuid4()}",
        invocation_ref=invocation_ref,
        limitations=("Raw Bridge text is untrusted advisory material.",),
    )


def _service(
    *,
    investigations: set[UUID] | None = None,
    plans: InMemoryInvestigationPlanRepository | None = None,
    invocations: InMemoryToolInvocationRepository | None = None,
    observations: InMemoryEvidenceObservationRepository | None = None,
) -> tuple[
    InvestigationPlanService,
    InMemoryInvestigationPlanRepository,
    InMemoryToolInvocationRepository,
    InMemoryEvidenceObservationRepository,
]:
    plan_repository = plans or InMemoryInvestigationPlanRepository()
    invocation_repository = invocations or InMemoryToolInvocationRepository()
    observation_repository = observations or InMemoryEvidenceObservationRepository()
    service = InvestigationPlanService(
        plans=plan_repository,
        invocations=invocation_repository,
        observations=observation_repository,
        investigations=_Investigations(investigations or {INVESTIGATION_ID}),
    )
    return service, plan_repository, invocation_repository, observation_repository


async def _advisor_exchange(
    invocations: InMemoryToolInvocationRepository,
    observations: InMemoryEvidenceObservationRepository,
    *,
    investigation_id: UUID = INVESTIGATION_ID,
    consultation_type: str = "plan_investigation",
    source: str = "advisor.consult.response",
    object_type: str = "advisor_response_received",
    invocation_ref: UUID | None = None,
    link: bool = True,
    sha256: str = SHA_A,
) -> tuple[ToolInvocation, EvidenceObservation]:
    observation_id = uuid4()
    invocation = _invocation(
        investigation_id,
        observation_id=observation_id,
        consultation_type=consultation_type,
        link=link,
    )
    observation = _observation(
        investigation_id,
        source=source,
        object_type=object_type,
        invocation_ref=invocation.id if invocation_ref is None and link else invocation_ref,
        sha256=sha256,
    )
    # Keep the cross-reference exact when the test supplies an observation id.
    observation = EvidenceObservation(
        id=observation_id,
        investigation_id=observation.investigation_id,
        kind=observation.kind,
        blob_id=observation.blob_id,
        sha256=observation.sha256,
        source=observation.source,
        source_version=observation.source_version,
        observed_at=observation.observed_at,
        object_type=observation.object_type,
        object_id=observation.object_id,
        limitations=observation.limitations,
        invocation_ref=observation.invocation_ref,
    )
    stored_invocation = await invocations.add_if_absent(invocation)
    stored_observation = await observations.append_if_absent(observation)
    return stored_invocation, stored_observation


async def test_plan_versions_append_only_and_require_advisor_response() -> None:
    service, plans, invocations, observations = _service()
    invocation, observation = await _advisor_exchange(invocations, observations)
    plan_one = _plan(version=1)

    first = await service.create_version(
        plan_one,
        parse_status=PlanParseStatus.PARSED,
        source_invocation_id=invocation.id,
        source_observation_id=observation.id,
        now=NOW,
    )
    head = await plans.get_head(INVESTIGATION_ID)
    assert head is not None and head.current_version == 1
    assert first.version == 1 and first.supersedes_id is None
    assert first.content.content_sha256 == plan_one.content_sha256
    assert first.content_sha256 == first.content.content_sha256
    assert first.parse_status is PlanParseStatus.PARSED

    second_invocation, second_observation = await _advisor_exchange(
        invocations, observations, sha256=SHA_B
    )
    second = await service.create_version(
        _plan(version=2),
        parse_status=PlanParseStatus.PARTIALLY_PARSED,
        source_invocation_id=second_invocation.id,
        source_observation_id=second_observation.id,
        now=NOW,
    )
    head = await plans.get_head(INVESTIGATION_ID)
    assert head is not None and head.current_version == 2
    assert second.supersedes_id == first.id
    versions = await plans.list_for_investigation(INVESTIGATION_ID)
    assert [item.version for item in versions] == [1, 2]
    assert (await plans.get_version(first.id)).content == _plan(version=1)

    replay = await service.create_version(
        _plan(version=2),
        parse_status=PlanParseStatus.PARTIALLY_PARSED,
        source_invocation_id=second_invocation.id,
        source_observation_id=second_observation.id,
        now=NOW,
    )
    assert replay.id == second.id
    assert (await plans.get_head(INVESTIGATION_ID)).current_version == 2

    with pytest.raises(InvestigationPlanIdempotencyError):
        await service.create_version(
            _plan(version=2, title="A different plan"),
            parse_status=PlanParseStatus.PARTIALLY_PARSED,
            source_invocation_id=second_invocation.id,
            source_observation_id=second_observation.id,
            now=NOW,
        )

    foreign_invocation, foreign_observation = await _advisor_exchange(
        invocations, observations, investigation_id=uuid4()
    )
    with pytest.raises(InvestigationPlanProvenanceError):
        await service.create_version(
            _plan(version=3),
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=foreign_invocation.id,
            source_observation_id=foreign_observation.id,
            now=NOW,
        )

    wrong_type_invocation, wrong_type_observation = await _advisor_exchange(
        invocations,
        observations,
        object_type="advisor_call",
        sha256="c" * 64,
    )
    with pytest.raises(InvestigationPlanProvenanceError):
        await service.create_version(
            _plan(version=3),
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=wrong_type_invocation.id,
            source_observation_id=wrong_type_observation.id,
            now=NOW,
        )

    draft_invocation, draft_observation = await _advisor_exchange(
        invocations,
        observations,
        consultation_type="draft_text",
        sha256="d" * 64,
    )
    with pytest.raises(InvestigationPlanProvenanceError):
        await service.create_version(
            _plan(version=3),
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=draft_invocation.id,
            source_observation_id=draft_observation.id,
            now=NOW,
        )

    unlinked_invocation, unlinked_observation = await _advisor_exchange(
        invocations,
        observations,
        link=False,
        sha256="e" * 64,
    )
    with pytest.raises(InvestigationPlanProvenanceError):
        await service.create_version(
            _plan(version=3),
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=unlinked_invocation.id,
            source_observation_id=unlinked_observation.id,
            now=NOW,
        )

    assert (await plans.get_head(INVESTIGATION_ID)).current_version == 2
    assert len(await plans.list_for_investigation(INVESTIGATION_ID)) == 2


async def test_plan_head_advances_by_compare_and_swap() -> None:
    service, plans, invocations, observations = _service()
    invocation, observation = await _advisor_exchange(invocations, observations)
    await service.create_version(
        _plan(version=1),
        parse_status=PlanParseStatus.PARSED,
        source_invocation_id=invocation.id,
        source_observation_id=observation.id,
        now=NOW,
    )

    other_invocation, other_observation = await _advisor_exchange(
        invocations, observations, sha256=SHA_B
    )
    with pytest.raises(VersionConflictError):
        await service.create_version(
            _plan(version=3),
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=other_invocation.id,
            source_observation_id=other_observation.id,
            now=NOW,
        )
    assert (await plans.get_head(INVESTIGATION_ID)).current_version == 1


def test_plan_content_is_canonical_and_strict() -> None:
    plan = _plan()
    payload = plan.to_payload()
    assert payload["schema_version"] == INVESTIGATION_PLAN_SCHEMA_VERSION
    assert payload["investigation_id"] == str(INVESTIGATION_ID)
    assert payload["version"] == 1
    assert len(payload["leads"]) == 1
    assert plan.content_sha256 == InvestigationPlanV1.from_payload(payload).content_sha256

    with pytest.raises(ValueError):
        InvestigationPlanV1(investigation_id=INVESTIGATION_ID, version=0, leads=(_lead(),))
    with pytest.raises(ValueError):
        InvestigationPlanV1(investigation_id=INVESTIGATION_ID, version=1, leads=())
    with pytest.raises(ValueError):
        InvestigationPlanV1(
            investigation_id=INVESTIGATION_ID,
            version=1,
            leads=(_lead(), _lead()),
        )
    with pytest.raises(TypeError):
        InvestigationLeadV1(title=1)  # type: ignore[arg-type]


def test_prose_authority_set_at_insert_and_immutable() -> None:
    synthesis = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="f" * 64,
    )
    legacy = AnalystInvestigation.from_verified_synthesis(synthesis=synthesis, budget=LoopBudget())
    assert legacy.prose_authority is ProseAuthority.LEGACY

    bridge_only = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
        prose_authority=ProseAuthority.BRIDGE_ONLY,
    )
    assert bridge_only.prose_authority is ProseAuthority.BRIDGE_ONLY

    row = _analyst_investigation_row(bridge_only)
    assert row.prose_authority == "bridge_only"
    values = _analyst_investigation_values(bridge_only)
    assert "prose_authority" not in values, (
        "prose_authority is fixed at INSERT and must never appear in an UPDATE"
    )

    require_prose_authority_mode(
        ProseAuthority.BRIDGE_ONLY, InvestigationExecutionMode.OPERATOR_LED
    )
    require_prose_authority_mode(ProseAuthority.LEGACY, InvestigationExecutionMode.BRIDGE_LED)
    with pytest.raises(ProseAuthorityModeError) as exc_info:
        require_prose_authority_mode(
            ProseAuthority.BRIDGE_ONLY, InvestigationExecutionMode.BRIDGE_LED
        )
    assert exc_info.value.code == "bridge_only_requires_operator_led"

    with pytest.raises(TypeError):
        AnalystInvestigation.from_verified_synthesis(
            synthesis=synthesis,
            budget=LoopBudget(),
            prose_authority="bridge_only",  # type: ignore[arg-type]
        )
