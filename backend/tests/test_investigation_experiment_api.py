from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from cti_app.api.investigation import (
    InvestigationExperimentTimelineResponseV1,
    get_investigation_experiments,
)
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentVerdict,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus

INVESTIGATION_ID = UUID("10000000-0000-4000-8000-000000000001")
OLD_EXPERIMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
NEW_EXPERIMENT_ID = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SUBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
RUN_ID = UUID("40000000-0000-4000-8000-000000000001")
SYNTHESIS_ID = UUID("50000000-0000-4000-8000-000000000001")


class _Experiment:
    def __init__(
        self,
        *,
        investigation_id: UUID = INVESTIGATION_ID,
        experiment_id: UUID,
        version: int,
        verdict: ExperimentVerdict,
        supersedes_experiment_ids: tuple[UUID, ...] = (),
        observation_refs: tuple[SimpleNamespace, ...] = (),
        actual_outcome: ObservationOutcome = ObservationOutcome.OBSERVED,
        observation_coverage: ObservationCoverage = ObservationCoverage.COMPLETE,
    ) -> None:
        self.id = experiment_id
        self.investigation_id = investigation_id
        self.hypothesis_id = "H1"
        self.version = version
        self.claim_ids = ("S3P3.1",)
        self.verdict = verdict
        self.scope = f"Scope for experiment v{version}"
        self.observation_refs = observation_refs
        self.supersedes_experiment_ids = supersedes_experiment_ids
        self.availability = ExperimentAvailability.AVAILABLE
        self.actual_outcome = actual_outcome
        self.observation_coverage = observation_coverage
        self.input_refs = ()
        self.tool_name = "local.sample.inspect"
        self.tool_version = "1.0.0"
        self.tool_schema_version = "local-sample-inspect.v1"
        self.method = "Inspect the persisted observation."
        self.method_mode = SimpleNamespace(value="passive")
        self.falsification_criterion = "A complete observation contradicts the claim."
        self.tool_arguments = {}

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
            "observation_refs": [
                {"observation_id": str(item.observation_id), "sha256": item.sha256}
                for item in self.observation_refs
            ],
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
        del investigation_id
        return self.rows

    async def version(self, investigation_id: UUID) -> int:
        del investigation_id
        return len(self.rows)


class _UnitOfWork:
    def __init__(
        self,
        experiments: _Experiments,
        *,
        hypotheses: tuple[str, ...] = ("H1",),
        snapshot_version: int = 1,
        observations: tuple[object, ...] = (),
    ) -> None:
        self.analyst_investigations = SimpleNamespace(
            get=self._get_investigation,
        )
        self.investigation_experiments = experiments
        self.investigation_states = SimpleNamespace(get=lambda _id: self._get_state(hypotheses))
        self.evidence_observations = SimpleNamespace(
            list_for_investigation=lambda _id: self._get_observations(observations)
        )
        self.production_artifacts = SimpleNamespace(
            get=lambda _id: self._get_artifact(snapshot_version)
        )
        self.investigation_envelopes = SimpleNamespace(get=lambda _id: self._get_none())
        self.analyst_decisions = SimpleNamespace(
            list_for_investigation=lambda _id: self._get_decisions()
        )

    @staticmethod
    async def _get_investigation(investigation_id: UUID) -> SimpleNamespace | None:
        if investigation_id == INVESTIGATION_ID:
            return SimpleNamespace(
                id=INVESTIGATION_ID,
                subject_id=SUBJECT_ID,
                production_run_id=RUN_ID,
                synthesis_artifact_id=SYNTHESIS_ID,
            )
        return None

    @staticmethod
    async def _get_state(hypotheses: tuple[str, ...]) -> SimpleNamespace:
        return SimpleNamespace(
            hypotheses=tuple(
                SimpleNamespace(id=item, status=SimpleNamespace(value="open"))
                for item in hypotheses
            ),
            stop_reason=None,
        )

    @staticmethod
    async def _get_observations(observations: tuple[object, ...]) -> tuple[object, ...]:
        return observations

    @staticmethod
    async def _get_artifact(version: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=SYNTHESIS_ID,
            stage=ProductionArtifactStage.SYNTHESIS,
            status=ProductionArtifactStatus.VERIFIED,
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            version=version,
        )

    @staticmethod
    async def _get_none() -> None:
        return None

    @staticmethod
    async def _get_decisions() -> tuple[object, ...]:
        return ()

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ApiClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def __enter__(self) -> _ApiClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, path: str) -> SimpleNamespace:
        investigation_id = UUID(path.split("/")[3])
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "client": ("test", 0),
                "server": ("test", 80),
                "app": self.app,
            }
        )
        response = asyncio.run(get_investigation_experiments(investigation_id, request))
        return SimpleNamespace(
            status_code=200,
            json=lambda: response.model_dump(mode="json"),
        )


def _client(
    experiments: tuple[_Experiment, ...],
    *,
    snapshot_mode: str = "missing",
    hypotheses: tuple[str, ...] = ("H1",),
    snapshot_version: int = 1,
    observations: tuple[object, ...] = (),
) -> _ApiClient:
    app = FastAPI()
    repository = _Experiments(experiments)
    app.state.uow_factory = lambda: _UnitOfWork(
        repository,
        hypotheses=hypotheses,
        snapshot_version=snapshot_version,
        observations=observations,
    )
    if snapshot_mode != "missing":
        snapshot_id = (
            UUID("60000000-0000-4000-8000-000000000001")
            if snapshot_mode == "wrong_snapshot"
            else SYNTHESIS_ID
        )
        version = 2 if snapshot_mode == "version_mismatch" else snapshot_version
        snapshot = SimpleNamespace(
            synthesis_artifact_id=snapshot_id,
            synthesis_version=version,
            synthesis_sha256="not-a-sha" if snapshot_mode == "bad_sha" else "a" * 64,
            subject_id=SUBJECT_ID,
            production_run_id=RUN_ID,
            article=SimpleNamespace(
                claims=(SimpleNamespace(id="S3P3.1", source_refs=("source:passage-1",)),)
            ),
        )

        class Snapshots:
            async def resolve(self, _artifact_id: UUID) -> SimpleNamespace:
                return snapshot

        app.state.investigation_composition_factory = SimpleNamespace(article_snapshots=Snapshots())
    return _ApiClient(app)


def _experiment(
    *,
    experiment_id: UUID,
    version: int,
    verdict: ExperimentVerdict,
    supersedes_experiment_ids: tuple[UUID, ...] = (),
    investigation_id: UUID = INVESTIGATION_ID,
    observation_refs: tuple[SimpleNamespace, ...] = (),
    actual_outcome: ObservationOutcome = ObservationOutcome.OBSERVED,
    observation_coverage: ObservationCoverage = ObservationCoverage.COMPLETE,
) -> _Experiment:
    return _Experiment(
        investigation_id=investigation_id,
        experiment_id=experiment_id,
        version=version,
        verdict=verdict,
        supersedes_experiment_ids=supersedes_experiment_ids,
        observation_refs=observation_refs,
        actual_outcome=actual_outcome,
        observation_coverage=observation_coverage,
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


def test_strategy_projection_resolves_refs_from_exact_snapshot_identity() -> None:
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.SUPPORTS,
    )

    with _client((experiment,), snapshot_mode="verified") as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    strategy = response.json()["strategy"]
    assert strategy["snapshot"] == {
        "artifact_id": str(SYNTHESIS_ID),
        "version": 1,
        "sha256": "a" * 64,
        "verified_identity": True,
    }
    claim = strategy["claims"][0]
    assert claim["source_ref_status"] == "resolved"
    assert claim["source_refs"] == ["source:passage-1"]
    assert claim["status"] == "UNKNOWN"
    assert claim["attempted_experiments"][0]["verdict"] == "supports"


@pytest.mark.parametrize("snapshot_mode", ("wrong_snapshot", "version_mismatch", "bad_sha"))
def test_strategy_projection_does_not_link_refs_for_snapshot_or_version_mismatch(
    snapshot_mode: str,
) -> None:
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
    )

    with _client((experiment,), snapshot_mode=snapshot_mode) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    strategy = response.json()["strategy"]
    claim = strategy["claims"][0]
    assert claim["source_ref_status"] == "UNKNOWN"
    assert claim["source_refs"] == []
    assert claim["status"] == "UNKNOWN"
    assert strategy["checkpoint"]["status"] == "UNKNOWN"


def test_strategy_projection_keeps_undeclared_hypothesis_unknown() -> None:
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
    )

    with _client((experiment,), snapshot_mode="verified", hypotheses=()) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    claim = response.json()["strategy"]["claims"][0]
    assert claim["status"] == "UNKNOWN"
    assert claim["attempted_experiments"][0]["hypothesis_declared"] is False
    assert claim["attempted_experiments"][0]["matrix_status"] == "UNKNOWN"


def test_strategy_projection_ignores_experiment_from_another_investigation() -> None:
    foreign_id = UUID("70000000-0000-4000-8000-000000000001")
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
        investigation_id=foreign_id,
    )

    with _client((experiment,), snapshot_mode="verified") as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    assert payload["experiments"] == []
    assert payload["strategy"]["claims"][0]["status"] == "UNKNOWN"
    assert payload["strategy"]["claims"][0]["attempted_experiments"] == []


def test_strategy_projection_keeps_foreign_observation_reference_unknown() -> None:
    observation_id = UUID("80000000-0000-4000-8000-000000000001")
    foreign_id = UUID("70000000-0000-4000-8000-000000000001")
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
        actual_outcome=ObservationOutcome.NOT_OBSERVED,
        observation_refs=(SimpleNamespace(observation_id=observation_id, sha256="b" * 64),),
    )
    observation = SimpleNamespace(
        id=observation_id,
        investigation_id=foreign_id,
        sha256="b" * 64,
    )

    with _client(
        (experiment,),
        snapshot_mode="verified",
        observations=(observation,),
    ) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    claim = response.json()["strategy"]["claims"][0]
    assert claim["status"] == "UNKNOWN"
    assert claim["attempted_experiments"][0]["matrix_status"] == "UNKNOWN"
