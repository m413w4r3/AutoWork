from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from cti_app.api.investigation import router
from cti_app.application.investigation_experiments import OperatorExperimentToolAdapter
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import (
    AgendaItem,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentInputRefV1,
    ExperimentObservationRefV1,
    ExperimentVerdict,
    InvestigationExperimentV1,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.domain.investigation_state import Hypothesis, InvestigationWorkingState
from cti_app.infrastructure.database.models.investigation import InvestigationExperimentRow
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from tests.integration.test_investigation_persistence_postgres import _make_investigation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CLAIM_ID = "claim:network-behaviour"


@pytest.mark.asyncio
async def test_operator_record_postgres_uses_persisted_hypothesis_refs_and_stream_cas(
    migrated_postgres_url: str,
    uow_factory,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    hypothesis = Hypothesis(
        id="hypothesis-network",
        statement="The proxy stage retrieves the payload through the observed route.",
    )
    observation_bytes = b"operator-led integration observation"
    observation_sha256 = hashlib.sha256(observation_bytes).hexdigest()
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=observation_sha256,
            size=len(observation_bytes),
            mime_type="application/octet-stream",
            logical_bucket="investigation-test",
        )
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=observation_sha256,
        source="network.capture.inspect",
        source_version="network-capture-inspect.v1",
        observed_at=NOW,
        object_type="capture",
        object_id="capture-operator-led",
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.GREEN),
        approval_decision_id=uuid4(),
        approval_actor_id="integration-operator",
    )
    state = InvestigationWorkingState(
        investigation_id=investigation.id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem("Which stage retrieves the payload?", 0.9, 1),
                AgendaItem("Which route carries the payload?", 0.8, 2),
            )
        ),
        hypotheses=(hypothesis,),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )

    try:
        async with uow_factory() as uow:
            await uow.blobs.add(blob)
            await uow.evidence_observations.append_if_absent(observation)
            await uow.investigation_states.save_if_version(state, expected_version=None)
            await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
            await uow.commit()

            class Claims:
                async def get_claims(self, investigation_id, claim_ids):
                    assert investigation_id == investigation.id
                    return tuple(SimpleNamespace(id=item) for item in claim_ids if item == CLAIM_ID)

            adapter = OperatorExperimentToolAdapter(
                experiments=uow.investigation_experiments,
                observations=uow.evidence_observations,
                invocations=uow.tool_invocations,
                states=uow.investigation_states,
                envelopes=uow.investigation_envelopes,
                article_snapshots=Claims(),
            )
            started_at = NOW + timedelta(minutes=10)
            arguments = {
                "expected_version": 0,
                "hypothesis_id": hypothesis.id,
                "claim_ids": [CLAIM_ID],
                "prediction": "The persisted capture contains the proxy route marker.",
                "falsification_criterion": "A complete capture contains no proxy route marker.",
                "method": "Passively inspect the persisted capture with pinned parser 4.4.1.",
                "method_mode": "passive",
                "input_type": "capture_sha256",
                "input_refs": [
                    {
                        "input_type": "capture_sha256",
                        "input_id": observation_sha256,
                        "sha256": observation_sha256,
                    }
                ],
                "preconditions": ["The cited capture is persisted for this investigation."],
                "availability": "available",
                "tool_name": "network.capture.inspect",
                "tool_version": "4.4.1",
                "tool_schema_version": "network-capture-inspect.v1",
                "tool_arguments": {"max_packets": 500},
                "step_ids": ["step-operator-led-1"],
                "tool_invocation_refs": [],
                "observation_refs": [
                    {"observation_id": str(observation.id), "sha256": observation_sha256}
                ],
                "expected": "The proxy route marker is present in the capture.",
                "actual": "The proxy route marker was observed in the persisted capture.",
                "actual_outcome": "observed",
                "observation_coverage": "complete",
                "started_at": started_at.isoformat(),
                "completed_at": (started_at + timedelta(seconds=1)).isoformat(),
                "scope": "Static capture inspection; victim execution is not established.",
                "limitations": [],
                "verdict": "supports",
                "implication": "The capture supports the bounded proxy-route claim.",
                "next_pivots": ["c" * 64],
            }
            context = ToolExecutionContext(
                investigation_id=investigation.id,
                envelope=envelope,
                budget=envelope.budget,
                idempotency_key="pg-operator-experiment-1",
            )
            result = await adapter.execute(arguments, context)
            assert result.status is ToolInvocationStatus.SUCCEEDED
            saved = await uow.investigation_experiments.list_for_investigation(investigation.id)
            assert len(saved) == 1
            assert saved[0].version == 1
            assert saved[0].observation_refs[0].observation_id == observation.id
            assert saved[0].observation_refs[0].sha256 == observation_sha256
            assert saved[0].working_state_version == state.version
            with pytest.raises(VersionConflictError):
                await uow.investigation_experiments.append_if_version(
                    replace(saved[0], id=uuid4(), version=2), expected_version=0
                )
            assert await uow.investigation_experiments.list_for_investigation(
                investigation.id
            ) == tuple(saved)
            await uow.commit()
    finally:
        await engine.dispose()


def _experiment(
    investigation_id: UUID,
    version: int,
    *,
    verdict: ExperimentVerdict,
    outcome: ObservationOutcome,
    coverage: ObservationCoverage,
    supersedes_experiment_ids: tuple[UUID, ...] = (),
) -> InvestigationExperimentV1:
    limited = coverage is not ObservationCoverage.COMPLETE
    return InvestigationExperimentV1(
        id=uuid4(),
        investigation_id=investigation_id,
        version=version,
        hypothesis_id="hypothesis-network",
        claim_ids=(CLAIM_ID,),
        working_state_version=2,
        g2_envelope_version=1,
        prediction="The sample emits the documented network marker.",
        falsification_criterion="A complete capture contains no marker.",
        method="Passively inspect the persisted capture with pinned parser 4.4.1.",
        input_type="sample_sha256",
        input_refs=(ExperimentInputRefV1("sample_sha256", "a" * 64, "a" * 64),),
        preconditions=("The capture and sample are bound to this investigation.",),
        availability=ExperimentAvailability.LIMITED
        if limited
        else ExperimentAvailability.AVAILABLE,
        tool_name="network.capture.inspect",
        tool_version="4.4.1",
        tool_schema_version="network-capture-inspect.v1",
        tool_arguments={"max_packets": 500},
        step_ids=(f"step-{version}",),
        tool_invocation_refs=(),
        observation_refs=(ExperimentObservationRefV1(uuid4(), f"{version:x}" * 64),),
        expected="The marker is present in the captured stream.",
        actual=(
            "Marker found in the persisted capture."
            if outcome is ObservationOutcome.OBSERVED
            else "Marker not observed in the persisted capture."
        ),
        actual_outcome=outcome,
        observation_coverage=coverage,
        started_at=NOW + timedelta(minutes=version),
        completed_at=NOW + timedelta(minutes=version, seconds=20),
        recorded_at=NOW + timedelta(minutes=version, seconds=21),
        scope="Static capture inspection; it does not establish victim execution.",
        limitations=("Provider PCAP is partial and omits the victim time window.",)
        if limited
        else (),
        verdict=verdict,
        implication="Keep this result as a separately scoped evidence line.",
        next_pivots=("c" * 64,),
        supersedes_experiment_ids=supersedes_experiment_ids,
    )


@pytest.mark.asyncio
async def test_experiment_postgres_cas_api_timeline_and_append_only(
    migrated_postgres_url: str, uow_factory
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        investigation = await _make_investigation(session_factory)
        initial_support = _experiment(
            investigation.id,
            1,
            verdict=ExperimentVerdict.SUPPORTS,
            outcome=ObservationOutcome.OBSERVED,
            coverage=ObservationCoverage.COMPLETE,
        )
        refutation = _experiment(
            investigation.id,
            2,
            verdict=ExperimentVerdict.REFUTES,
            outcome=ObservationOutcome.NOT_OBSERVED,
            coverage=ObservationCoverage.COMPLETE,
        )
        inconclusive_replacement = _experiment(
            investigation.id,
            3,
            verdict=ExperimentVerdict.INCONCLUSIVE,
            outcome=ObservationOutcome.NOT_OBSERVED,
            coverage=ObservationCoverage.LIMITED,
            supersedes_experiment_ids=(initial_support.id,),
        )
        latest_support = _experiment(
            investigation.id,
            4,
            verdict=ExperimentVerdict.SUPPORTS,
            outcome=ObservationOutcome.OBSERVED,
            coverage=ObservationCoverage.COMPLETE,
            supersedes_experiment_ids=(inconclusive_replacement.id,),
        )
        experiments = (initial_support, refutation, inconclusive_replacement, latest_support)

        async with uow_factory() as uow:
            for expected_version, experiment in enumerate(experiments):
                assert (
                    await uow.investigation_experiments.append_if_version(
                        experiment, expected_version=expected_version
                    )
                    == experiment
                )
            with pytest.raises(VersionConflictError):
                await uow.investigation_experiments.append_if_version(
                    _experiment(
                        investigation.id,
                        5,
                        verdict=ExperimentVerdict.INCONCLUSIVE,
                        outcome=ObservationOutcome.UNKNOWN,
                        coverage=ObservationCoverage.LIMITED,
                    ),
                    expected_version=1,
                )
            await uow.commit()

        app = FastAPI()
        app.include_router(router)
        app.state.uow_factory = uow_factory
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/investigations/{investigation.id}/experiments")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema_version"] == "investigation-experiment-timeline.v1"
        assert body["stream_version"] == 4
        assert [item["version"] for item in body["experiments"]] == [1, 2, 3, 4]
        assert [item["method"] for item in body["experiments"]] == [
            item.method for item in experiments
        ]
        assert [item["supersedes_experiment_ids"] for item in body["experiments"]] == [
            [],
            [],
            [str(initial_support.id)],
            [str(inconclusive_replacement.id)],
        ]
        assert [item["superseded_by"] for item in body["experiments"]] == [
            [str(inconclusive_replacement.id)],
            [],
            [str(latest_support.id)],
            [],
        ]
        expected_superseded_by = {
            initial_support.id: [str(inconclusive_replacement.id)],
            inconclusive_replacement.id: [str(latest_support.id)],
        }
        assert body["claims"] == [
            {
                "claim_id": CLAIM_ID,
                "status": "qualified",
                "verdicts": ["refutes", "supports"],
                "experiment_ids": [str(item.id) for item in experiments],
                "superseded_by": [
                    str(inconclusive_replacement.id),
                    str(latest_support.id),
                ],
                "scopes": [
                    {
                        "experiment_id": str(item.id),
                        "scope": item.scope,
                        "verdict": item.verdict.value,
                        "observation_ids": [
                            str(ref.observation_id) for ref in item.observation_refs
                        ],
                        "superseded_by": expected_superseded_by.get(item.id, []),
                    }
                    for item in experiments
                ],
                "contradictory": True,
            }
        ]

        async with uow_factory() as uow:
            persisted = await uow.investigation_experiments.list_for_investigation(investigation.id)
        assert persisted[-1].supersedes_experiment_ids == (inconclusive_replacement.id,)

        with pytest.raises(DBAPIError) as update_error:
            async with engine.begin() as connection:
                await connection.execute(
                    update(InvestigationExperimentRow)
                    .where(InvestigationExperimentRow.id == experiments[0].id)
                    .values(payload_sha256="0" * 64)
                )
        assert getattr(update_error.value.orig, "sqlstate", None) == "55000"
        with pytest.raises(DBAPIError) as delete_error:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(InvestigationExperimentRow).where(
                        InvestigationExperimentRow.id == experiments[0].id
                    )
                )
        assert getattr(delete_error.value.orig, "sqlstate", None) == "55000"
    finally:
        await engine.dispose()


def _experiment_row_values(
    investigation_id: UUID,
    version: int,
    *,
    schema_version: str = "investigation-experiment.v1",
    payload_sha256: str = "a" * 64,
    claim_ids: str = "[]",
    payload_identity: dict[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = uuid4()
    identity = {
        "schema_version": schema_version,
        "id": str(experiment_id),
        "investigation_id": str(investigation_id),
        "version": str(version),
    }
    if payload_identity is not None:
        identity.update(payload_identity)
    return {
        "id": experiment_id,
        "investigation_id": investigation_id,
        "version": version,
        "hypothesis_id": "hypothesis-checks",
        "claim_ids": claim_ids,
        "schema_version": schema_version,
        "payload": json.dumps(identity),
        "payload_sha256": payload_sha256,
        "recorded_at": NOW,
    }


@pytest.mark.asyncio
async def test_experiment_postgres_check_and_unique_constraints(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        investigation = await _make_investigation(session_factory)
        insert = text(
            "INSERT INTO investigation_experiments ("
            "id, investigation_id, version, hypothesis_id, claim_ids, schema_version, "
            "payload, payload_sha256, recorded_at"
            ") VALUES ("
            ":id, :investigation_id, :version, :hypothesis_id, "
            "CAST(:claim_ids AS jsonb), :schema_version, CAST(:payload AS jsonb), "
            ":payload_sha256, :recorded_at"
            ")"
        )
        check_cases = (
            _experiment_row_values(investigation.id, 0),
            _experiment_row_values(investigation.id, 2, schema_version="invalid.v0"),
            _experiment_row_values(investigation.id, 3, payload_sha256="not-a-hash"),
            _experiment_row_values(investigation.id, 4, claim_ids="{}"),
            _experiment_row_values(investigation.id, 5, payload_identity={"version": "6"}),
        )
        async with engine.begin() as connection:
            await connection.execute(insert, _experiment_row_values(investigation.id, 1))

            duplicate_version = _experiment_row_values(investigation.id, 1)
            with pytest.raises(DBAPIError) as unique_error:
                async with connection.begin_nested():
                    await connection.execute(insert, duplicate_version)
            assert getattr(unique_error.value.orig, "sqlstate", None) == "23505"

            for invalid_row in check_cases:
                with pytest.raises(DBAPIError) as check_error:
                    async with connection.begin_nested():
                        await connection.execute(insert, invalid_row)
                assert getattr(check_error.value.orig, "sqlstate", None) == "23514"
    finally:
        await engine.dispose()
