from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryDetectionCandidateRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationBudgetRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
    InvestigationBudgetSnapshot,
    VersionConflictError,
)
from cti_app.application.investigation_state import (
    InMemoryInvestigationStateRepository,
    InvestigationStateVersionConflict,
)
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    InvestigationFinding,
    MotifProvenance,
    NoveltyClass,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    tool_operation_key,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
    StopReason,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.models.investigation import (
    InvestigationWorkingStateRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class _InMemoryPersistence:
    tool_invocations: InMemoryToolInvocationRepository
    evidence_observations: InMemoryEvidenceObservationRepository
    investigation_findings: InMemoryFindingRepository
    sample_associations: InMemorySampleAssociationRepository
    detection_candidates: InMemoryDetectionCandidateRepository
    analyst_opinions: InMemoryAnalystOpinionRepository
    investigation_envelopes: InMemoryInvestigationEnvelopeRepository
    investigation_budgets: InMemoryInvestigationBudgetRepository
    investigation_states: InMemoryInvestigationStateRepository

    @classmethod
    def create(cls) -> _InMemoryPersistence:
        return cls(
            tool_invocations=InMemoryToolInvocationRepository(),
            evidence_observations=InMemoryEvidenceObservationRepository(),
            investigation_findings=InMemoryFindingRepository(),
            sample_associations=InMemorySampleAssociationRepository(),
            detection_candidates=InMemoryDetectionCandidateRepository(),
            analyst_opinions=InMemoryAnalystOpinionRepository(),
            investigation_envelopes=InMemoryInvestigationEnvelopeRepository(),
            investigation_budgets=InMemoryInvestigationBudgetRepository(),
            investigation_states=InMemoryInvestigationStateRepository(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "postgres"))
async def test_investigation_repositories_have_memory_postgres_parity(
    backend: str, migrated_postgres_url: str
) -> None:
    blob_data = uuid4().bytes
    blob_sha256 = hashlib.sha256(blob_data).hexdigest()
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=blob_sha256,
            size=len(blob_data),
            mime_type="application/octet-stream",
            logical_bucket="investigation-test",
        )
    )

    if backend == "memory":
        await _assert_repository_contract(_InMemoryPersistence.create(), uuid4(), blob)
        return

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.blobs.add(blob)
            await uow.commit()
            await _assert_repository_contract(uow, investigation.id, blob)
            state_row = await uow._session.scalar(
                select(InvestigationWorkingStateRow).where(
                    InvestigationWorkingStateRow.investigation_id == investigation.id
                )
            )
            assert state_row is not None
            assert state_row.version == 4
            assert state_row.turn_index == 1
            assert state_row.stop_reason is None
            assert state_row.state["no_progress_streak"] == 1
            assert state_row.state["hypotheses"][0]["status"] == "supported"
            assert (
                state_row.state["execution_mode"] == InvestigationExecutionMode.OPERATOR_LED.value
            )
            legacy_payload = dict(state_row.state)
            legacy_payload.pop("execution_mode")
            state_row.state = legacy_payload
            await uow.commit()
            uow._session.expire_all()
            legacy_state = await uow.investigation_states.get(investigation.id)
            assert legacy_state is not None
            assert legacy_state.execution_mode is InvestigationExecutionMode.BRIDGE_LED

            observation = await uow.evidence_observations.get(
                (await uow.evidence_observations.list_for_investigation(investigation.id))[0].id
            )
            assert observation is not None
            tuple_replay = replace(observation, id=uuid4(), observed_at=NOW + timedelta(minutes=5))
            assert await uow.evidence_observations.append_if_absent(tuple_replay) == observation
    finally:
        await engine.dispose()


async def _assert_repository_contract(
    persistence: _InMemoryPersistence | SqlAlchemyUnitOfWork,
    investigation_id: UUID,
    blob: BlobRecord,
) -> None:
    hypothesis = Hypothesis(id="hypothesis-1", statement="A loader persists between runs.")
    working_state = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="How does the sample load?", potential=0.9, rank=1),
                AgendaItem(
                    question="What evidence supports the attribution?", potential=0.6, rank=2
                ),
            )
        ),
        hypotheses=(hypothesis,),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    state_repository = persistence.investigation_states
    assert (
        await state_repository.save_if_version(working_state, expected_version=None)
        == working_state
    )
    assert (
        await state_repository.save_if_version(working_state, expected_version=99) == working_state
    )
    reloaded_state = await state_repository.get(investigation_id)
    assert reloaded_state == working_state
    assert reloaded_state is not None
    with pytest.raises(InvestigationStateVersionConflict):
        await state_repository.save_if_version(
            reloaded_state.with_pending_context(uuid4(), "a" * 64), expected_version=0
        )

    supported_hypothesis = replace(
        hypothesis,
        status=HypothesisStatus.SUPPORTED,
        evidence_refs=(uuid4(),),
        finding_refs=(uuid4(),),
    )
    state_v2 = reloaded_state.with_turn(
        context_sha256="b" * 64,
        decision_sha256="c" * 64,
        progress=False,
        evidence_ids=supported_hypothesis.evidence_refs,
        finding_ids=supported_hypothesis.finding_refs,
        action_summaries=("Reviewed loader evidence",),
        hypotheses=(supported_hypothesis,),
    )
    assert await state_repository.save_if_version(state_v2, expected_version=1) == state_v2
    reloaded_state_v2 = await state_repository.get(investigation_id)
    assert reloaded_state_v2 == state_v2
    assert reloaded_state_v2 is not None
    assert reloaded_state_v2.no_progress_streak == 1
    assert reloaded_state_v2.hypotheses == (supported_hypothesis,)
    with pytest.raises(InvestigationStateVersionConflict):
        await state_repository.save_if_version(
            reloaded_state_v2.conclude(StopReason.NO_NEW_INFORMATION), expected_version=1
        )

    stopped_state = reloaded_state_v2.conclude(StopReason.NO_NEW_INFORMATION)
    assert (
        await state_repository.save_if_version(stopped_state, expected_version=2) == stopped_state
    )
    reloaded_stopped_state = await state_repository.get(investigation_id)
    assert reloaded_stopped_state == stopped_state
    assert reloaded_stopped_state is not None
    resumed_state = reloaded_stopped_state.resume()
    assert (
        await state_repository.save_if_version(resumed_state, expected_version=3) == resumed_state
    )
    assert await state_repository.get(investigation_id) == resumed_state

    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"operation-{uuid4().hex}",
        tool_name="virustotal.file_report",
        schema_version="v1",
        normalized_parameters={"sha256": blob.descriptor.sha256},
        purpose="collect a file report",
        reserved_budget={BudgetDimension.VT_UNITS: 3},
        created_at=NOW,
        updated_at=NOW,
    )
    assert await persistence.tool_invocations.add_if_absent(invocation) == invocation
    replay = replace(invocation, id=uuid4())
    assert await persistence.tool_invocations.add_if_absent(replay) == invocation
    assert await persistence.tool_invocations.get_by_operation_key(invocation.operation_key) == (
        invocation
    )
    running = invocation.transition(ToolInvocationStatus.RUNNING, now=NOW + timedelta(seconds=1))
    assert (
        await persistence.tool_invocations.update_if_version(running, expected_version=1)
    ) == running
    completed = running.transition(
        ToolInvocationStatus.SUCCEEDED,
        response_refs=(uuid4(),),
        consumed_budget={BudgetDimension.VT_UNITS: 2},
        now=NOW + timedelta(seconds=2),
    )
    assert (
        await persistence.tool_invocations.update_if_version(completed, expected_version=2)
    ) == completed
    with pytest.raises(VersionConflictError):
        await persistence.tool_invocations.update_if_version(running, expected_version=1)

    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=blob.id,
        sha256=blob.descriptor.sha256,
        source="VirusTotal",
        source_version="api-v3",
        observed_at=NOW,
        object_type="file",
        object_id=blob.descriptor.sha256,
        limitations=("partial report",),
        invocation_ref=invocation.id,
    )
    assert await persistence.evidence_observations.append_if_absent(observation) == observation
    assert await persistence.evidence_observations.append_if_absent(observation) == observation
    assert await persistence.evidence_observations.get(observation.id) == observation
    assert await persistence.evidence_observations.list_for_investigation(investigation_id) == (
        observation,
    )
    with pytest.raises(ValueError, match="immutable observation"):
        await persistence.evidence_observations.append_if_absent(
            replace(observation, source="altered")
        )

    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The sample contains a capability marker.",
        observation_refs=(observation.id,),
        claim_refs=("claim:network-capability",),
        novelty=NoveltyClass.NEW_TO_ARTICLE,
        scope="static analysis",
        limitations=("execution was not observed",),
        created_at=NOW,
    )
    assert (
        await persistence.investigation_findings.append_version(finding, expected_previous_id=None)
    ) == finding
    assert await persistence.investigation_findings.get(finding.id) == finding
    assert (
        await persistence.investigation_findings.append_version(finding, expected_previous_id=None)
    ) == finding
    with pytest.raises(ValueError, match="id is already used"):
        await persistence.investigation_findings.append_version(
            replace(finding, statement="rewritten finding"), expected_previous_id=None
        )
    finding_v2 = replace(
        finding,
        id=uuid4(),
        version=2,
        assessment=FindingAssessment.QUALIFIED,
        statement="The marker suggests a capability, with execution unverified.",
        supersedes=finding.id,
        created_at=NOW + timedelta(seconds=1),
    )
    assert (
        await persistence.investigation_findings.append_version(
            finding_v2, expected_previous_id=finding.id
        )
    ) == finding_v2
    assert (
        len(await persistence.investigation_findings.list_for_investigation(investigation_id)) == 2
    )

    association = SampleAssociation(
        investigation_id=investigation_id,
        sample_sha256="a" * 64,
        origin=SampleOrigin.VT_PIVOT,
    )
    assert (
        await persistence.sample_associations.append_version(association, expected_version=0)
    ) == association
    confirmed = association.change_qualification(
        SampleQualification.CONFIRMED,
        actor_kind=QualificationActorKind.HUMAN,
        actor_id="analyst-42",
        reason="Compared the sample with the cited evidence.",
        changed_at=NOW + timedelta(minutes=1),
    )
    assert (
        await persistence.sample_associations.append_version(confirmed, expected_version=1)
    ) == confirmed
    assert await persistence.sample_associations.get(association.id) == confirmed
    assert await persistence.sample_associations.list_for_investigation(investigation_id) == (
        confirmed,
    )
    with pytest.raises(VersionConflictError):
        await persistence.sample_associations.append_version(
            replace(confirmed, related_sample_sha256=("b" * 64,)), expected_version=2
        )
    with pytest.raises(ValueError, match="Only an identified human actor"):
        association.change_qualification(
            SampleQualification.CONFIRMED,
            actor_kind=QualificationActorKind.AGENT,
            actor_id="bridge-agent",
            reason="Model suggested it.",
        )

    yara_source = "rule sample { condition: true }"
    candidate = DetectionCandidateVersion(
        investigation_id=investigation_id,
        source=yara_source,
        source_sha256=hashlib.sha256(yara_source.encode()).hexdigest(),
        motif_provenance=(
            MotifProvenance(
                motif="network marker",
                observation_refs=(observation.id,),
                rationale="The cited report contains the marker.",
            ),
        ),
        status=DetectionCandidateStatus.CANDIDATE,
        test_refs=(uuid4(),),
        created_at=NOW,
    )
    assert (
        await persistence.detection_candidates.append_version(candidate, expected_previous_id=None)
    ) == candidate
    assert await persistence.detection_candidates.get(candidate.id) == candidate
    with pytest.raises(ValueError, match="id is already used"):
        await persistence.detection_candidates.append_version(
            replace(candidate, status=DetectionCandidateStatus.TESTED),
            expected_previous_id=None,
        )
    candidate_v2 = DetectionCandidateVersion(
        investigation_id=investigation_id,
        candidate_id=candidate.candidate_id,
        source="rule sample_v2 { condition: true }",
        source_sha256=hashlib.sha256(b"rule sample_v2 { condition: true }").hexdigest(),
        motif_provenance=candidate.motif_provenance,
        status=DetectionCandidateStatus.TESTED,
        version=2,
        parent_version_id=candidate.id,
        test_refs=(uuid4(),),
        created_at=NOW + timedelta(seconds=2),
    )
    assert (
        await persistence.detection_candidates.append_version(
            candidate_v2, expected_previous_id=candidate.id
        )
    ) == candidate_v2
    assert await persistence.detection_candidates.list_for_candidate(candidate.candidate_id) == (
        candidate,
        candidate_v2,
    )

    opinion = AnalystOpinionV1(
        investigation_id=investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="b" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256="c" * 64,
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="The marker is supported by the observation.",
                finding_refs=(finding.id,),
                evidence_refs=(observation.id,),
                qualification=OpinionClaimQualification.FACT,
                certainty=0.9,
            ),
        ),
        author_id="analyst-42",
        created_at=NOW,
    )
    assert await persistence.analyst_opinions.append_version(opinion, expected_version=0) == opinion
    assert await persistence.analyst_opinions.get(opinion.id, version=1) == opinion
    with pytest.raises(ValueError, match="append-only"):
        edited_block = replace(opinion.blocks[0], text="Edited after persistence.")
        await persistence.analyst_opinions.append_version(
            replace(opinion, blocks=(edited_block,), content_sha256=""), expected_version=0
        )
    approved = replace(
        opinion,
        version=2,
        status=AnalystOpinionStatus.APPROVED,
        reviewer_id="reviewer-17",
        created_at=NOW + timedelta(minutes=2),
        content_sha256="",
    )
    assert (
        await persistence.analyst_opinions.append_version(approved, expected_version=1)
    ) == approved
    assert await persistence.analyst_opinions.get(opinion.id) == approved

    envelope_budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: 4})
    envelope_budget.reserve("envelope-budget", {BudgetDimension.VT_UNITS: 1})
    envelope_budget.commit("envelope-budget", {BudgetDimension.VT_UNITS: 1})
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("virustotal", "shodan"),
        budget=envelope_budget,
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.AMBER,
            do_not_submit=True,
            external_llm_allowed=False,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-42",
        approved_at=NOW,
    )
    assert (
        await persistence.investigation_envelopes.save_if_version(envelope, expected_version=None)
    ) == envelope
    assert await persistence.investigation_envelopes.get(investigation_id) == envelope
    envelope_v2 = replace(envelope, version=2, allowed_providers=("virustotal",))
    assert (
        await persistence.investigation_envelopes.save_if_version(envelope_v2, expected_version=1)
    ) == envelope_v2
    with pytest.raises(VersionConflictError):
        await persistence.investigation_envelopes.save_if_version(
            replace(envelope, version=2, acquire_hits_allowed=False), expected_version=1
        )

    budget = InvestigationBudget(limits={BudgetDimension.DOWNLOADS: 5, BudgetDimension.VT_UNITS: 7})
    budget.reserve(
        "committed-op",
        {BudgetDimension.DOWNLOADS: 2, BudgetDimension.VT_UNITS: 3},
    )
    budget.commit(
        "committed-op",
        {BudgetDimension.DOWNLOADS: 1, BudgetDimension.VT_UNITS: 2},
    )
    budget.reserve("released-op", {BudgetDimension.DOWNLOADS: 1})
    budget.release("released-op")
    budget_snapshot = InvestigationBudgetSnapshot(
        investigation_id=investigation_id, version=1, budget=budget
    )
    assert (
        await persistence.investigation_budgets.save_if_version(
            budget_snapshot, expected_version=None
        )
    ) == budget_snapshot
    stored_budget = await persistence.investigation_budgets.get(investigation_id)
    assert stored_budget == budget_snapshot
    assert stored_budget is not None
    assert stored_budget.budget.reservations == budget.reservations
    assert stored_budget.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert stored_budget.budget.reserved[BudgetDimension.DOWNLOADS] == 0
    updated_budget = stored_budget.budget
    updated_budget.reserve("next-op", {BudgetDimension.DOWNLOADS: 4})
    updated_budget.commit("next-op", {BudgetDimension.DOWNLOADS: 2})
    budget_v2 = InvestigationBudgetSnapshot(
        investigation_id=investigation_id, version=2, budget=updated_budget
    )
    assert (
        await persistence.investigation_budgets.save_if_version(budget_v2, expected_version=1)
    ) == budget_v2
    with pytest.raises(VersionConflictError):
        await persistence.investigation_budgets.save_if_version(
            replace(budget_v2, budget=InvestigationBudget()), expected_version=1
        )


@pytest.mark.asyncio
async def test_concurrent_operation_key_replay_creates_one_invocation(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    parameters = {"sha256": hashlib.sha256(uuid4().bytes).hexdigest()}
    operation_key = tool_operation_key(
        investigation_id=investigation.id,
        tool_name="virustotal.file_report",
        schema_version="v1",
        normalized_parameters=parameters,
        purpose="shared concurrent replay",
    )
    calls = tuple(
        ToolInvocation(
            investigation_id=investigation.id,
            operation_key=operation_key,
            tool_name="virustotal.file_report",
            schema_version="v1",
            normalized_parameters=parameters,
            purpose="shared concurrent replay",
            created_at=NOW,
            updated_at=NOW,
        )
        for _ in range(2)
    )

    async def persist(invocation: ToolInvocation) -> ToolInvocation:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            existing = await uow.tool_invocations.add_if_absent(invocation)
            await uow.commit()
            return existing

    try:
        results = await asyncio.gather(*(persist(invocation) for invocation in calls))
        assert results[0] == results[1]
        async with engine.connect() as connection:
            count = await connection.scalar(
                select(func.count())
                .select_from(ToolInvocationRow)
                .where(ToolInvocationRow.operation_key == operation_key)
            )
        assert count == 1
    finally:
        await engine.dispose()


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    from tests.integration.edition_codes import reserve_edition_code

    suffix = uuid4().hex
    edition = Edition(
        country=f"Lot A2 {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Investigation persistence fixture",
            slug=f"investigation-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="d" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation
