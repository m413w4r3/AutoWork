from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

import cti_app.api.main as api_main
from cti_app.api.investigation import InvestigationExperimentTimelineResponseV1
from cti_app.api.main import create_app
from cti_app.application.identity import StaticTokenIdentityProvider
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentInputRefV1,
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
        self.next_pivots: tuple[str, ...] = ()
        self.verdict = verdict
        self.scope = f"Scope for experiment v{version}"
        self.observation_refs = observation_refs
        self.supersedes_experiment_ids = supersedes_experiment_ids
        self.availability = ExperimentAvailability.AVAILABLE
        self.actual_outcome = actual_outcome
        self.observation_coverage = observation_coverage
        self.input_refs: tuple[ExperimentInputRefV1, ...] = ()
        self.step_ids: tuple[str, ...] = ()
        self.tool_invocation_refs: tuple[object, ...] = ()
        self.method = "Inspect the persisted observation."
        self.prediction = "The claim is supported."
        self.expected = "Expected outcome."
        self.actual = "Observed outcome."
        self.implication = "Update the claim assessment."
        self.preconditions: tuple[str, ...] = ()
        self.limitations: tuple[str, ...] = ()
        self.method_mode = SimpleNamespace(value="passive")
        self.falsification_criterion = "A complete observation contradicts the claim."
        self.tool_arguments = {}
        self.tool_name = "network.capture.inspect"
        self.tool_version = "1.0.0"
        self.tool_schema_version = "network-capture-inspect.v1"
        self.author = "operator-led"
        self.origin = "experiment.record"

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "investigation-experiment.v1",
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "version": self.version,
            "hypothesis_id": self.hypothesis_id,
            "claim_ids": list(self.claim_ids),
            "working_state_version": 451,
            "g2_envelope_version": 3,
            "prediction": self.prediction,
            "falsification_criterion": self.falsification_criterion,
            "method": self.method,
            "method_mode": "passive",
            "input_type": "sample_sha256",
            "input_refs": [item.to_payload() for item in self.input_refs],
            "preconditions": list(self.preconditions),
            "availability": "available",
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "tool_schema_version": self.tool_schema_version,
            "tool_arguments": self.tool_arguments,
            "step_ids": list(self.step_ids),
            "tool_invocation_refs": [
                item.to_payload() if hasattr(item, "to_payload") else item
                for item in self.tool_invocation_refs
            ],
            "observation_refs": [
                {"observation_id": str(item.observation_id), "sha256": item.sha256}
                for item in self.observation_refs
            ],
            "expected": self.expected,
            "actual": self.actual,
            "actual_outcome": self.actual_outcome.value,
            "observation_coverage": self.observation_coverage.value,
            "started_at": NOW.isoformat(),
            "completed_at": NOW.isoformat(),
            "recorded_at": NOW.isoformat(),
            "scope": self.scope,
            "limitations": list(self.limitations),
            "verdict": self.verdict.value,
            "implication": self.implication,
            "next_pivots": list(self.next_pivots),
            "author": self.author,
            "origin": self.origin,
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
        invocations: tuple[object, ...] = (),
    ) -> None:
        self.analyst_investigations = SimpleNamespace(
            get=self._get_investigation,
        )
        self.investigation_experiments = experiments
        self.investigation_states = SimpleNamespace(get=lambda _id: self._get_state(hypotheses))
        self.evidence_observations = SimpleNamespace(
            list_for_investigation=lambda _id: self._get_observations(observations)
        )
        self.tool_invocations = SimpleNamespace(
            list_for_investigation=lambda _id: self._get_observations(invocations)
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

    async def _get(self, path: str, *, authorized: bool) -> Response:
        headers = {"Authorization": "Bearer " + "c" * 40} if authorized else {}
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://127.0.0.1"
        ) as client:
            return await client.get(path, headers=headers)

    def get(self, path: str) -> Response:
        return asyncio.run(self._get(path, authorized=True))

    def get_without_authorization(self, path: str) -> Response:
        return asyncio.run(self._get(path, authorized=False))


@pytest.fixture(autouse=True)
def _safe_app_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = api_main.settings.model_copy(
        update={
            "app_env": "development",
            "api_bind_host": "127.0.0.1",
            "diagnostics_log_root": None,
            "operator_api_token": None,
            "operator_client_api_token": None,
        }
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)


def _client(
    experiments: tuple[_Experiment, ...],
    *,
    snapshot_mode: str = "missing",
    hypotheses: tuple[str, ...] = ("H1",),
    snapshot_version: int = 1,
    observations: tuple[object, ...] = (),
    invocations: tuple[object, ...] = (),
) -> _ApiClient:
    app = create_app()
    app.state.identity_provider = StaticTokenIdentityProvider(
        "h" * 40, operator_client_token="c" * 40
    )
    repository = _Experiments(experiments)
    app.state.uow_factory = lambda: _UnitOfWork(
        repository,
        hypotheses=hypotheses,
        snapshot_version=snapshot_version,
        observations=observations,
        invocations=invocations,
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
    assert claim["status"] == "inconclusive"
    assert claim["verdicts"] == ["supports"]
    assert claim["superseded_by"] == [str(NEW_EXPERIMENT_ID)]
    assert claim["scopes"][0]["superseded_by"] == [str(NEW_EXPERIMENT_ID)]
    assert claim["scopes"][1]["superseded_by"] == []


def test_mounted_route_applies_identity_middleware_and_response_model() -> None:
    """The lifespan is not entered offline; production UOW installation is UNKNOWN.

    The request uses `_client`'s injected test UOW. This verifies the mounted route,
    identity middleware, and response model only. ASGITransport skips the
    PostgreSQL-dependent lifespan, so production wiring remains UNKNOWN; no fake
    engine represents the production path.
    """
    with _client(()) as client:
        unauthorized = client.get_without_authorization(
            f"/api/investigations/{INVESTIGATION_ID}/experiments"
        )
        authorized = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["schema_version"] == "investigation-experiment-timeline.v1"


@pytest.mark.parametrize(
    ("coverage", "outcome", "observation_sha", "include_observation"),
    (
        (ObservationCoverage.LIMITED, ObservationOutcome.NOT_OBSERVED, "a" * 64, True),
        (ObservationCoverage.COMPLETE, ObservationOutcome.NOT_OBSERVED, "a" * 64, False),
        (ObservationCoverage.COMPLETE, ObservationOutcome.NOT_OBSERVED, "b" * 64, True),
        (ObservationCoverage.COMPLETE, ObservationOutcome.OBSERVED, "a" * 64, True),
    ),
    ids=("partial-coverage", "missing-observation", "sha-mismatch", "incompatible-outcome"),
)
def test_full_api_claim_status_uses_conservative_strategy_projection(
    coverage: ObservationCoverage,
    outcome: ObservationOutcome,
    observation_sha: str,
    include_observation: bool,
) -> None:
    observation_id = UUID("80000000-0000-4000-8000-000000000001")
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
        actual_outcome=outcome,
        observation_coverage=coverage,
        observation_refs=(SimpleNamespace(observation_id=observation_id, sha256="a" * 64),),
    )
    observations = (
        (
            SimpleNamespace(
                id=observation_id,
                investigation_id=INVESTIGATION_ID,
                sha256=observation_sha,
            ),
        )
        if include_observation
        else ()
    )

    with _client((experiment,), snapshot_mode="verified", observations=observations) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    strategy_claim = payload["strategy"]["claims"][0]
    timeline_claim = payload["claims"][0]
    expected_status = "supported" if strategy_claim["status"] == "supported" else "inconclusive"
    assert timeline_claim["status"] == expected_status
    assert timeline_claim["status"] != "refuted"
    assert strategy_claim["status"] == "UNKNOWN"
    assert timeline_claim["verdicts"] == ["refutes"]


def test_timeline_route_redacts_free_text_and_payload_arguments() -> None:
    secret = "SENSITIVE-OPAQUE-HEX-8f30d2"
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.INCONCLUSIVE,
    )
    experiment.prediction = f"payload evidence {secret}"
    experiment.expected = f"expected {secret}"
    experiment.actual = f"actual proof {secret}"
    experiment.scope = f"scope {secret}"
    experiment.implication = f"implication {secret}"
    linked_observation_id = UUID("70000000-0000-4000-8000-000000000001")
    unsafe_reference = "deadbeef" * 16
    experiment.input_refs = (
        ExperimentInputRefV1("sample_sha256", unsafe_reference, "a" * 64),
        ExperimentInputRefV1("capture_sha256", "b" * 64, "b" * 64),
        ExperimentInputRefV1("capture_sha256", "c" * 64, "c" * 64),
    )
    experiment.observation_refs = (
        SimpleNamespace(observation_id=linked_observation_id, sha256="b" * 64),
    )
    experiment.tool_arguments = {
        "payload": f"0000 4d5a {secret}",
        "byte_0": 77,
        "byte_1": 90,
        "byte_2": 0,
        "flag_0": True,
        "mode": f"signed-url https://example.invalid/?token={secret}",
    }

    canonical_observation = SimpleNamespace(
        id=linked_observation_id,
        investigation_id=INVESTIGATION_ID,
        blob_id=UUID("70000000-0000-4000-8000-000000000002"),
        sha256="b" * 64,
        kind="other",
        object_type="capture",
    )
    unlinked_observation = SimpleNamespace(
        id=UUID("70000000-0000-4000-8000-000000000003"),
        investigation_id=INVESTIGATION_ID,
        kind="vt_file_report",
        object_type="vt_file_report",
        sha256="c" * 64,
    )
    with _client(
        (experiment,), observations=(canonical_observation, unlinked_observation)
    ) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    serialized = response.text
    projected = payload["experiments"][0]
    assert secret not in serialized
    assert unsafe_reference not in serialized
    assert projected["tool_arguments"] == {}
    assert projected["input_refs"] == [
        {
            "input_type": "sample_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {"input_type": "capture_sha256", "input_id": "b" * 64, "sha256": "b" * 64},
        {
            "input_type": "capture_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
    ]
    assert projected["observation_refs"] == [
        {"observation_id": str(linked_observation_id), "sha256": "b" * 64}
    ]
    assert projected["prediction"] == "[redacted]"
    assert projected["expected"] == "[redacted]"
    assert projected["actual"] == "[redacted]"
    assert projected["scope"] == "[redacted]"
    assert payload["claims"][0]["scopes"][0]["scope"] == "[redacted]"


def test_timeline_exposes_only_tool_identity_resolved_by_persisted_invocation() -> None:
    invocation_id = UUID("80000000-0000-4000-8000-000000000001")
    invocation = SimpleNamespace(
        id=invocation_id,
        investigation_id=INVESTIGATION_ID,
        version=2,
        tool_name="protocol.validate",
        schema_version="protocol-validation.v1",
    )
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.INCONCLUSIVE,
    )
    experiment.tool_invocation_refs = ({"invocation_id": str(invocation_id), "version": 2},)
    experiment.step_ids = (str(invocation_id),)
    experiment.tool_name = "protocol.validate"
    experiment.tool_schema_version = "protocol-validation.v1"

    with _client((experiment,), invocations=(invocation,)) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    projected = response.json()["experiments"][0]
    assert projected["tool_invocation_refs"] == [
        {"invocation_id": str(invocation_id), "version": 2}
    ]
    assert projected["step_ids"] == [str(invocation_id)]
    assert projected["tool_name"] == "protocol.validate"
    assert projected["tool_schema_version"] == "protocol-validation.v1"
    # ToolInvocation has no canonical tool-version property.
    assert projected["tool_version"] == "[redacted]"


def test_timeline_route_redacts_unvalidated_step_ids_and_labels_refutes_as_assertion() -> None:
    hostile_step_id = "deadbeef" * 8
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.REFUTES,
    )
    experiment.step_ids = (hostile_step_id,)
    experiment.claim_ids = (hostile_step_id,)
    experiment.next_pivots = (hostile_step_id,)
    experiment.supersedes_experiment_ids = (hostile_step_id,)
    experiment.tool_invocation_refs = ({"invocation_id": hostile_step_id, "version": 1},)
    experiment.input_refs = (
        ExperimentInputRefV1("sample_sha256", hostile_step_id, hostile_step_id),
    )
    experiment.observation_refs = (
        SimpleNamespace(observation_id=hostile_step_id, sha256=hostile_step_id),
    )
    experiment.hypothesis_id = hostile_step_id
    experiment.tool_name = "secret.payload"
    experiment.tool_version = "SensitiveData-1"
    experiment.tool_schema_version = "sensitive.payload.v1"
    experiment.author = hostile_step_id
    experiment.origin = hostile_step_id

    with _client((experiment,)) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    payload = response.json()
    projected = payload["experiments"][0]
    assert hostile_step_id not in response.text
    assert projected["step_ids"] == ["[redacted]"]
    assert projected["claim_ids"] == ["[redacted]"]
    assert projected["next_pivots"] == ["[redacted]"]
    assert projected["supersedes_experiment_ids"] == ["[redacted]"]
    assert projected["tool_invocation_refs"] == [
        {"invocation_id": "[redacted]", "version": "[redacted]"}
    ]
    assert projected["observation_refs"] == [
        {"observation_id": "[redacted]", "sha256": "[redacted]"}
    ]
    assert projected["hypothesis_id"] == "[redacted]"
    assert projected["author"] == "[redacted]"
    assert projected["origin"] == "[redacted]"
    assert projected["tool_name"] == "[redacted]"
    assert projected["tool_version"] == "[redacted]"
    assert projected["tool_schema_version"] == "[redacted]"
    assert projected["input_refs"] == [
        {"input_type": "sample_sha256", "input_id": "[redacted]", "sha256": "[redacted]"}
    ]
    assert projected["verdict"] == "refutes"
    assert projected["verdict_assertion_label"] == (
        "Verdict enregistré : refutes (assertion, non conclusion)"
    )
    assert payload["claims"][0]["scopes"][0]["verdict"] == "refutes"
    assert payload["claims"][0]["scopes"][0]["verdict_assertion_label"] == (
        "Verdict enregistré : refutes (assertion, non conclusion)"
    )
    assert payload["claims"][0]["verdicts"] == ["refutes"]
    assert payload["claims"][0]["verdict_assertion_labels"] == [
        "Verdict enregistré : refutes (assertion, non conclusion)"
    ]
    assert all(
        value not in response.text
        for value in (
            "secret.payload",
            "SensitiveData-1",
            "sensitive.payload.v1",
            hostile_step_id,
        )
    )


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
    projected = response.json()["experiments"][0]
    assert projected["observation_refs"] == [
        {"observation_id": "[redacted]", "sha256": "[redacted]"}
    ]
    claim = response.json()["strategy"]["claims"][0]
    assert claim["status"] == "UNKNOWN"
    assert claim["attempted_experiments"][0]["matrix_status"] == "UNKNOWN"


def test_timeline_only_exposes_experiment_linked_and_resolved_references() -> None:
    linked_invocation_id = UUID("80000000-0000-4000-8000-000000000001")
    wrong_version_invocation_id = UUID("80000000-0000-4000-8000-000000000002")
    unlinked_invocation_id = UUID("80000000-0000-4000-8000-000000000003")
    missing_invocation_id = UUID("80000000-0000-4000-8000-000000000004")
    linked_observation_id = UUID("70000000-0000-4000-8000-000000000001")
    linked_capture_observation_id = UUID("70000000-0000-4000-8000-000000000002")
    linked_vt_observation_id = UUID("70000000-0000-4000-8000-000000000003")
    unlinked_observation_id = UUID("70000000-0000-4000-8000-000000000004")
    ambiguous_observation_id = UUID("70000000-0000-4000-8000-000000000005")
    duplicate_observation_id = UUID("70000000-0000-4000-8000-000000000006")
    experiment = _experiment(
        experiment_id=NEW_EXPERIMENT_ID,
        version=1,
        verdict=ExperimentVerdict.INCONCLUSIVE,
        observation_refs=(
            SimpleNamespace(observation_id=linked_observation_id, sha256="b" * 64),
            SimpleNamespace(observation_id=linked_capture_observation_id, sha256="c" * 64),
            SimpleNamespace(observation_id=linked_vt_observation_id, sha256="d" * 64),
            SimpleNamespace(observation_id=ambiguous_observation_id, sha256="f" * 64),
            SimpleNamespace(observation_id=duplicate_observation_id, sha256="f" * 64),
        ),
    )
    experiment.tool_invocation_refs = (
        {"invocation_id": str(linked_invocation_id), "version": 2},
        {"invocation_id": str(wrong_version_invocation_id), "version": 1},
        {"invocation_id": str(missing_invocation_id), "version": 1},
    )
    experiment.step_ids = (
        str(linked_invocation_id),
        str(wrong_version_invocation_id),
        str(unlinked_invocation_id),
        str(missing_invocation_id),
    )
    experiment.input_refs = (
        ExperimentInputRefV1("sample_sha256", "b" * 64, "b" * 64),
        ExperimentInputRefV1("capture_sha256", "b" * 64, "b" * 64),
        ExperimentInputRefV1("capture_sha256", "c" * 64, "c" * 64),
        ExperimentInputRefV1("capture_sha256", "d" * 64, "d" * 64),
        ExperimentInputRefV1("sample_sha256", "e" * 64, "e" * 64),
        ExperimentInputRefV1("sample_sha256", "f" * 64, "f" * 64),
    )
    experiment.next_pivots = ("b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64)
    linked_invocation = SimpleNamespace(
        id=linked_invocation_id,
        investigation_id=INVESTIGATION_ID,
        version=2,
        tool_name="protocol.validate",
        schema_version="protocol-validation.v1",
    )
    wrong_version_invocation = SimpleNamespace(
        id=wrong_version_invocation_id,
        investigation_id=INVESTIGATION_ID,
        version=2,
        tool_name="protocol.validate",
        schema_version="protocol-validation.v1",
    )
    unlinked_invocation = SimpleNamespace(
        id=unlinked_invocation_id,
        investigation_id=INVESTIGATION_ID,
        version=1,
        tool_name="protocol.validate",
        schema_version="protocol-validation.v1",
    )
    linked_observation = SimpleNamespace(
        id=linked_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="b" * 64,
        kind="binary_analysis",
        object_type="sample",
    )
    linked_capture_observation = SimpleNamespace(
        id=linked_capture_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="c" * 64,
        kind="other",
        object_type="network_capture",
    )
    linked_vt_observation = SimpleNamespace(
        id=linked_vt_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="d" * 64,
        kind="vt_file_report",
        object_type="file",
    )
    ambiguous_observation = SimpleNamespace(
        id=ambiguous_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="f" * 64,
        kind="binary_analysis",
        object_type="sample",
    )
    duplicate_observation = SimpleNamespace(
        id=duplicate_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="f" * 64,
        kind="other",
        object_type="capture",
    )
    unlinked_vt_observation = SimpleNamespace(
        id=unlinked_observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256="e" * 64,
        kind="vt_file_report",
        object_type="file",
    )

    with _client(
        (experiment,),
        observations=(
            linked_observation,
            linked_capture_observation,
            linked_vt_observation,
            unlinked_vt_observation,
            ambiguous_observation,
            duplicate_observation,
        ),
        invocations=(linked_invocation, wrong_version_invocation, unlinked_invocation),
    ) as client:
        response = client.get(f"/api/investigations/{INVESTIGATION_ID}/experiments")

    assert response.status_code == 200
    projected = response.json()["experiments"][0]
    assert projected["step_ids"] == [
        str(linked_invocation_id),
        "[redacted]",
        "[redacted]",
        "[redacted]",
    ]
    assert projected["tool_invocation_refs"] == [
        {"invocation_id": str(linked_invocation_id), "version": 2},
        {"invocation_id": "[redacted]", "version": "[redacted]"},
        {"invocation_id": "[redacted]", "version": "[redacted]"},
    ]
    assert projected["observation_refs"] == [
        {"observation_id": str(linked_observation_id), "sha256": "b" * 64},
        {"observation_id": str(linked_capture_observation_id), "sha256": "c" * 64},
        {"observation_id": str(linked_vt_observation_id), "sha256": "d" * 64},
        {"observation_id": str(ambiguous_observation_id), "sha256": "f" * 64},
        {"observation_id": str(duplicate_observation_id), "sha256": "f" * 64},
    ]
    assert projected["input_refs"] == [
        {"input_type": "sample_sha256", "input_id": "b" * 64, "sha256": "b" * 64},
        {
            "input_type": "[redacted]",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {"input_type": "capture_sha256", "input_id": "c" * 64, "sha256": "c" * 64},
        {
            "input_type": "[redacted]",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {
            "input_type": "sample_sha256",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
        {
            "input_type": "[redacted]",
            "input_id": "[redacted]",
            "sha256": "[redacted]",
        },
    ]
    assert projected["next_pivots"] == [
        "b" * 64,
        "c" * 64,
        "[redacted]",
        "[redacted]",
        "[redacted]",
    ]
    assert str(unlinked_invocation_id) not in response.text
    assert str(wrong_version_invocation_id) not in response.text
    assert str(missing_invocation_id) not in response.text
    assert "e" * 64 not in response.text
