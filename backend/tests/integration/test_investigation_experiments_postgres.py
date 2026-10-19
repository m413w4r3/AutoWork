from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError

from cti_app.api.investigation import router
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentInputRefV1,
    ExperimentObservationRefV1,
    ExperimentVerdict,
    InvestigationExperimentV1,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.infrastructure.database.models.investigation import InvestigationExperimentRow
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from tests.integration.test_investigation_persistence_postgres import _make_investigation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CLAIM_ID = "claim:network-behaviour"


def _experiment(
    investigation_id: UUID,
    version: int,
    *,
    verdict: ExperimentVerdict,
    outcome: ObservationOutcome,
    coverage: ObservationCoverage,
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
    )


@pytest.mark.asyncio
async def test_experiment_postgres_cas_api_timeline_and_append_only(
    migrated_postgres_url: str, uow_factory
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        investigation = await _make_investigation(session_factory)
        experiments = (
            _experiment(
                investigation.id,
                1,
                verdict=ExperimentVerdict.SUPPORTS,
                outcome=ObservationOutcome.OBSERVED,
                coverage=ObservationCoverage.COMPLETE,
            ),
            _experiment(
                investigation.id,
                2,
                verdict=ExperimentVerdict.REFUTES,
                outcome=ObservationOutcome.NOT_OBSERVED,
                coverage=ObservationCoverage.COMPLETE,
            ),
            _experiment(
                investigation.id,
                3,
                verdict=ExperimentVerdict.INCONCLUSIVE,
                outcome=ObservationOutcome.NOT_OBSERVED,
                coverage=ObservationCoverage.LIMITED,
            ),
        )

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
                        4,
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
        assert body["stream_version"] == 3
        assert [item["version"] for item in body["experiments"]] == [1, 2, 3]
        assert [item["method"] for item in body["experiments"]] == [
            item.method for item in experiments
        ]
        assert body["claims"] == [
            {
                "claim_id": CLAIM_ID,
                "status": "qualified",
                "verdicts": ["supports", "refutes", "inconclusive"],
                "experiment_ids": [str(item.id) for item in experiments],
                "scopes": [
                    {
                        "experiment_id": str(item.id),
                        "scope": item.scope,
                        "verdict": item.verdict.value,
                        "observation_ids": [
                            str(ref.observation_id) for ref in item.observation_refs
                        ],
                    }
                    for item in experiments
                ],
                "contradictory": True,
            }
        ]

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(InvestigationExperimentRow)
                    .where(InvestigationExperimentRow.id == experiments[0].id)
                    .values(payload_sha256="0" * 64)
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    delete(InvestigationExperimentRow).where(
                        InvestigationExperimentRow.id == experiments[0].id
                    )
                )
    finally:
        await engine.dispose()
