from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.investigation_contracts import (
    ArticleContextV1,
    ContextInvestigationV1,
    InvestigationContextV1,
    InvestigationSourceV1,
)
from cti_app.application.investigation_jobs import (
    INVESTIGATION_RUN_AGGREGATE_TYPE,
    InvestigationRunParameters,
    register_investigation_jobs,
)
from cti_app.application.jobs import (
    JobExecutor,
    JobRegistry,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.domain.investigation import AgendaItem, InvestigationAgenda
from cti_app.domain.investigation_state import InvestigationWorkingState, StopReason
from cti_app.domain.jobs import INVESTIGATION_RUN_JOB_KIND, JobStatus
from tests.job_support import InMemoryJobUnitOfWorkFactory


def _context(investigation_id):
    return InvestigationContextV1(
        view="full",
        investigation_id=investigation_id,
        source=InvestigationSourceV1(
            subject_id=uuid4(),
            production_run_id=uuid4(),
            synthesis_artifact_id=uuid4(),
            synthesis_sha256="a" * 64,
        ),
        article=ArticleContextV1(
            short_summary="A fixed context for a crash replay.",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
        investigation=ContextInvestigationV1(
            agenda=("Check the one fixed question", "Check for a contradiction"),
            hypotheses=(),
            new_evidence=(),
            findings=(),
            sample_deltas=(),
            rule_deltas=(),
            previous_actions=(),
            blocked_capabilities=(),
            budget_remaining={},
        ),
        available_tools=(),
    )


def _state(investigation_id, stop_reason):
    return InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Check the one fixed question", potential=1.0, rank=1),
                AgendaItem(question="Check for a contradiction", potential=0.5, rank=2),
            )
        ),
        stop_reason=stop_reason,
    )


def _decision():
    return {
        "reasoning_summary": "The available evidence requires a permission decision.",
        "goal": "Request permission before external research.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": "request_permission",
    }


@pytest.mark.asyncio
async def test_job_replay_after_simulated_worker_crash_reuses_bridge_turn() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    investigation_id = uuid4()
    transport = ScriptedTurnTransport([_decision()])
    bridge = BridgeInvestigator(transport)
    context = _context(investigation_id)
    crash_once = True

    async def run(investigation_id_arg):
        nonlocal crash_once
        await bridge.turn(
            investigation_id=investigation_id_arg,
            subject_id=context.source.subject_id,
            turn_index=0,
            context=context,
            external_llm_allowed=True,
        )
        if crash_once:
            crash_once = False
            raise asyncio.CancelledError
        return _state(investigation_id_arg, StopReason.PERMISSION_REQUIRED)

    registry = create_job_registry(investigation_runner=run)
    assert registry.resumes_after_worker_loss(INVESTIGATION_RUN_JOB_KIND)
    service = JobService(factory, registry)
    job = await service.submit(
        kind=INVESTIGATION_RUN_JOB_KIND,
        aggregate_type=INVESTIGATION_RUN_AGGREGATE_TYPE,
        aggregate_id=investigation_id,
        idempotency_key=f"investigation:{investigation_id}:run:1",
        correlation_id="test-investigation-crash",
        input_parameters=InvestigationRunParameters(
            investigation_id=investigation_id,
            run_generation=1,
            request_key_sha256="b" * 64,
        ).model_dump(mode="json"),
    )
    executor = JobExecutor(factory, registry)

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(job.id)
    persisted = factory.state[job.id]
    persisted.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
    factory.state[job.id] = persisted
    recovered = await service.recover_abandoned(
        timedelta(seconds=1),
        resume_current_attempt_kinds=frozenset({INVESTIGATION_RUN_JOB_KIND}),
    )
    assert [item.id for item in recovered] == [job.id]

    await SynchronousJobDispatcher(executor).dispatch(job.id)

    assert transport.attempt_count == 2
    assert transport.submission_count == 1
    assert (await service.get(job.id)).status is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_permission_required_state_completes_the_job_normally() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    investigation_id = uuid4()

    async def run(_investigation_id):
        return _state(investigation_id, StopReason.PERMISSION_REQUIRED)

    registry = JobRegistry()
    register_investigation_jobs(registry, run)
    service = JobService(factory, registry)
    job = await service.submit(
        kind=INVESTIGATION_RUN_JOB_KIND,
        aggregate_type=INVESTIGATION_RUN_AGGREGATE_TYPE,
        aggregate_id=investigation_id,
        idempotency_key=f"investigation:{investigation_id}:run:1",
        correlation_id="test-permission-required",
        input_parameters={
            "investigation_id": str(investigation_id),
            "run_generation": 1,
            "request_key_sha256": "c" * 64,
        },
    )

    await SynchronousJobDispatcher(JobExecutor(factory, registry)).dispatch(job.id)

    assert (await service.get(job.id)).status is JobStatus.SUCCEEDED
