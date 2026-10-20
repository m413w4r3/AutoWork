from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cti_app.api.investigation import (
    InvestigationExperimentTimelineResponseV1,
    router,
)
from cti_app.domain.investigation_experiment import (
    ExperimentVerdict,
)

INVESTIGATION_ID = UUID("10000000-0000-4000-8000-000000000001")
OLD_EXPERIMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
NEW_EXPERIMENT_ID = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _Experiment:
    def __init__(
        self,
        *,
        experiment_id: UUID,
        version: int,
        verdict: ExperimentVerdict,
        supersedes_experiment_ids: tuple[UUID, ...] = (),
    ) -> None:
        self.id = experiment_id
        self.investigation_id = INVESTIGATION_ID
        self.version = version
        self.claim_ids = ("S3P3.1",)
        self.verdict = verdict
        self.scope = f"Scope for experiment v{version}"
        self.observation_refs: tuple[SimpleNamespace, ...] = ()
        self.supersedes_experiment_ids = supersedes_experiment_ids

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "investigation-experiment.v1",
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "version": self.version,
            "hypothesis_id": "H1",
            "claim_ids": list(self.claim_ids),
            "working_state_version": 451,
            "g2_envelope_version": 3,
            "prediction": "The claim is supported.",
            "falsification_criterion": "A complete observation contradicts the claim.",
            "method": "Inspect the persisted observation.",
            "method_mode": "passive",
            "input_type": "sample_sha256",
            "input_refs": [],
            "preconditions": [],
            "availability": "available",
            "tool_name": "network.capture.inspect",
            "tool_version": "1.0.0",
            "tool_schema_version": "network-capture-inspect.v1",
            "tool_arguments": {},
            "step_ids": [],
            "tool_invocation_refs": [],
            "observation_refs": [],
            "expected": "Expected outcome.",
            "actual": "Observed outcome.",
            "actual_outcome": "observed",
            "observation_coverage": "complete",
            "started_at": NOW.isoformat(),
            "completed_at": NOW.isoformat(),
            "recorded_at": NOW.isoformat(),
            "scope": self.scope,
            "limitations": [],
            "verdict": self.verdict.value,
            "implication": "Update the claim assessment.",
            "next_pivots": [],
            "author": "operator-led",
            "origin": "experiment.record",
        }
        if self.supersedes_experiment_ids:
            payload["supersedes_experiment_ids"] = [
                str(item) for item in self.supersedes_experiment_ids
            ]
        return payload


class _Experiments:
    def __init__(self, rows: tuple[_Experiment, ...]) -> None:
        self.rows = rows

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[_Experiment, ...]:
        return tuple(row for row in self.rows if row.investigation_id == investigation_id)

    async def version(self, investigation_id: UUID) -> int:
        del investigation_id
        return len(self.rows)


class _UnitOfWork:
    def __init__(self, experiments: _Experiments) -> None:
        self.analyst_investigations = SimpleNamespace(
            get=self._get_investigation,
        )
        self.investigation_experiments = experiments

    @staticmethod
    async def _get_investigation(investigation_id: UUID) -> SimpleNamespace | None:
        if investigation_id == INVESTIGATION_ID:
            return SimpleNamespace(id=INVESTIGATION_ID)
        return None

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _client(experiments: tuple[_Experiment, ...]) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    repository = _Experiments(experiments)
    app.state.uow_factory = lambda: _UnitOfWork(repository)
    return TestClient(app)


def _experiment(
    *,
    experiment_id: UUID,
    version: int,
    verdict: ExperimentVerdict,
    supersedes_experiment_ids: tuple[UUID, ...] = (),
) -> _Experiment:
    return _Experiment(
        experiment_id=experiment_id,
        version=version,
        verdict=verdict,
        supersedes_experiment_ids=supersedes_experiment_ids,
    )


def test_get_experiments_exposes_replacement_links_in_timeline_and_claims() -> None:
    old_experiment = _experiment(
        experiment_id=OLD_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
    )
    replacement = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=2,
        verdict=ExperimentVerdict.SUPPORTS,
        supersedes_experiment_ids=(OLD_EXPERIMENT_ID,),
    )

    with _client((old_experiment, replacement)) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    previous, current = payload["experiments"]
    assert previous["supersedes_experiment_ids"] == []
    assert previous["superseded_by"] == [str(NEW_EXPERIMENT_ID)]
    assert current["supersedes_experiment_ids"] == [str(OLD_EXPERIMENT_ID)]
    assert current["superseded_by"] == []

    claim = payload["claims"][0]
    assert claim["status"] == "supported"
    assert claim["verdicts"] == ["supports"]
    assert claim["superseded_by"] == [str(NEW_EXPERIMENT_ID)]
    assert claim["scopes"][0]["superseded_by"] == [str(NEW_EXPERIMENT_ID)]
    assert claim["scopes"][1]["superseded_by"] == []


def test_get_experiments_accepts_legacy_payloads_without_replacement_fields() -> None:
    legacy_experiment = _experiment(
        experiment_id=OLD_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.SUPPORTS,
    )

    with _client((legacy_experiment,)) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    assert payload["experiments"][0]["supersedes_experiment_ids"] == []
    assert payload["experiments"][0]["superseded_by"] == []
    assert payload["claims"][0]["superseded_by"] == []
    assert payload["claims"][0]["scopes"][0]["superseded_by"] == []

    payload["experiments"][0]["unknown_field"] = True
    with pytest.raises(ValidationError):
        InvestigationExperimentTimelineResponseV1.model_validate(payload, strict=False)
