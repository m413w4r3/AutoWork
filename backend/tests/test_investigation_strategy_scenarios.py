from __future__ import annotations

import hashlib
import socket
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_experiments import (
    build_investigation_strategy_projection,
)
from cti_app.application.investigation_operator_view import (
    build_investigation_operator_strategy_checkpoint,
)
from cti_app.application.rule_workbench import RuleWorkbench
from cti_app.domain.goodware import NonDiscriminantPatternRegistry
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentInputRefV1,
    ExperimentObservationRefV1,
    ExperimentVerdict,
    InvestigationExperimentV1,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.domain.investigation_state import Hypothesis, StopReason
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.rule_workbench import (
    RuleSampleProvenance,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleValidationStatus,
    YaraEngineResult,
    YaraExecutionStatus,
)

INVESTIGATION_ID = UUID("10000000-0000-4000-8000-000000000001")
SUBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
RUN_ID = UUID("40000000-0000-4000-8000-000000000001")
SYNTHESIS_ID = UUID("50000000-0000-4000-8000-000000000001")
CLAIM_ID = "fixture:C1"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def offline_only(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fail_network(*_args: object, **_kwargs: object) -> None:
        calls.append("network")
        raise AssertionError("network access is forbidden in strategy scenarios")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    return calls


class _ProviderPortGuard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, provider: str) -> None:
        self.calls.append(provider)
        raise AssertionError(f"live provider {provider} must not be called")


def _snapshot(*, version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        synthesis_artifact_id=SYNTHESIS_ID,
        synthesis_version=version,
        synthesis_sha256="a" * 64,
        article=SimpleNamespace(
            claims=(SimpleNamespace(id=CLAIM_ID, source_refs=("fixture:passage-sha1",)),)
        ),
    )


def _experiment(
    version: int,
    *,
    verdict: ExperimentVerdict,
    outcome: ObservationOutcome = ObservationOutcome.UNKNOWN,
    coverage: ObservationCoverage = ObservationCoverage.NONE,
    availability: ExperimentAvailability = ExperimentAvailability.AVAILABLE,
    observation_refs: tuple[ExperimentObservationRefV1, ...] = (),
) -> InvestigationExperimentV1:
    return InvestigationExperimentV1(
        id=UUID(f"20000000-0000-4000-8000-{version:012x}"),
        investigation_id=INVESTIGATION_ID,
        version=version,
        hypothesis_id="H1",
        claim_ids=(CLAIM_ID,),
        working_state_version=1,
        g2_envelope_version=1,
        prediction="A synthetic marker is present.",
        falsification_criterion="A complete scan finds no marker.",
        method="Run the fake local analyzer over the exact synthetic bytes.",
        input_type="sample_sha256",
        input_refs=(ExperimentInputRefV1("sample_sha256", "b" * 64, "b" * 64),),
        preconditions=("Synthetic fixture only.",),
        availability=availability,
        tool_name="local.sample.analyze",
        tool_version="fake-1",
        tool_schema_version="local-analysis.v1",
        tool_arguments={},
        step_ids=(f"step-{version}",),
        tool_invocation_refs=(),
        observation_refs=observation_refs,
        expected="Marker appears in the synthetic input.",
        actual=f"Fake result {version}.",
        actual_outcome=outcome,
        observation_coverage=coverage,
        started_at=NOW + timedelta(minutes=version),
        completed_at=NOW + timedelta(minutes=version, seconds=1),
        recorded_at=NOW + timedelta(minutes=version, seconds=2),
        scope="Synthetic fixture; no claim about real-world samples.",
        limitations=("No live or real samples were used.",)
        if coverage is not ObservationCoverage.COMPLETE
        else (),
        verdict=verdict,
    )


def _projection(
    experiments: tuple[InvestigationExperimentV1, ...],
    observations: tuple[object, ...] = (),
) -> dict[str, object]:
    return build_investigation_strategy_projection(
        investigation_id=INVESTIGATION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        synthesis_artifact_id=SYNTHESIS_ID,
        snapshot=_snapshot(),
        snapshot_version=1,
        hypotheses=(Hypothesis("H1", "Synthetic fixture hypothesis."),),
        experiments=experiments,
        observations=observations,
    )


@pytest.mark.asyncio
async def test_no_sample_keeps_claim_unknown_and_coverage_empty(
    offline_only: list[str],
) -> None:
    providers = _ProviderPortGuard()
    strategy = _projection(())
    checkpoint = build_investigation_operator_strategy_checkpoint(
        strategy, state=SimpleNamespace(stop_reason=None)
    )

    claim = strategy["claims"][0]
    assert claim["status"] == "UNKNOWN"
    assert claim["coverage"] == "none"
    assert checkpoint["next_action"]["code"] == "select_discriminating_experiment"
    assert providers.calls == []
    assert offline_only == []


@pytest.mark.asyncio
async def test_synthetic_acquisition_and_confined_analysis_use_fake_ports_only(
    offline_only: list[str],
) -> None:
    providers = _ProviderPortGuard()

    class FakeSyntheticAcquisition:
        calls = 0

        async def acquire(self) -> bytes:
            self.calls += 1
            return b"synthetic-only:RareMarkerQz"

    class FakeConfinedAnalysis:
        calls = 0

        async def analyze(self, sample: bytes) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(
                sha256=hashlib.sha256(sample).hexdigest(),
                outcome=ObservationOutcome.OBSERVED,
                coverage=ObservationCoverage.COMPLETE,
            )

    acquisition = FakeSyntheticAcquisition()
    analyzer = FakeConfinedAnalysis()
    sample = await acquisition.acquire()
    measurement = await analyzer.analyze(sample)
    observation_id = uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        investigation_id=INVESTIGATION_ID,
        sha256=measurement.sha256,
    )
    experiment = _experiment(
        1,
        verdict=ExperimentVerdict.SUPPORTS,
        outcome=measurement.outcome,
        coverage=measurement.coverage,
        observation_refs=(ExperimentObservationRefV1(observation_id, measurement.sha256),),
    )
    strategy = _projection((experiment,), (observation,))

    assert acquisition.calls == analyzer.calls == 1
    assert strategy["claims"][0]["status"] == "supported"
    assert strategy["claims"][0]["attempted_experiments"][0]["coverage"] == "complete"
    assert providers.calls == []
    assert offline_only == []


def test_support_refute_contradiction_stays_unknown(offline_only: list[str]) -> None:
    providers = _ProviderPortGuard()
    positive_id, negative_id = uuid4(), uuid4()
    positive_sha, negative_sha = "c" * 64, "d" * 64
    experiments = (
        _experiment(
            1,
            verdict=ExperimentVerdict.SUPPORTS,
            outcome=ObservationOutcome.OBSERVED,
            coverage=ObservationCoverage.COMPLETE,
            observation_refs=(ExperimentObservationRefV1(positive_id, positive_sha),),
        ),
        _experiment(
            2,
            verdict=ExperimentVerdict.REFUTES,
            outcome=ObservationOutcome.NOT_OBSERVED,
            coverage=ObservationCoverage.COMPLETE,
            observation_refs=(ExperimentObservationRefV1(negative_id, negative_sha),),
        ),
    )
    observations = (
        SimpleNamespace(id=positive_id, investigation_id=INVESTIGATION_ID, sha256=positive_sha),
        SimpleNamespace(id=negative_id, investigation_id=INVESTIGATION_ID, sha256=negative_sha),
    )
    strategy = _projection(experiments, observations)

    assert strategy["claims"][0]["status"] == "UNKNOWN"
    assert [item["matrix_status"] for item in strategy["claims"][0]["attempted_experiments"]] == [
        "supported",
        "refuted",
    ]
    assert providers.calls == []
    assert offline_only == []


def test_blocked_acquisition_is_not_a_refutation(offline_only: list[str]) -> None:
    providers = _ProviderPortGuard()
    experiment = _experiment(
        1,
        verdict=ExperimentVerdict.BLOCKED,
        availability=ExperimentAvailability.UNAVAILABLE,
    )
    strategy = _projection((experiment,))
    checkpoint = build_investigation_operator_strategy_checkpoint(
        strategy,
        state=SimpleNamespace(stop_reason=StopReason.PERMISSION_REQUIRED),
        g2_blocked=True,
    )

    assert strategy["claims"][0]["status"] == "UNKNOWN"
    assert set(checkpoint["checkpoint"]["blocking_reasons"]) == {
        "indisponible",
        "refus_g2",
    }
    assert providers.calls == []
    assert offline_only == []


@pytest.mark.asyncio
async def test_yara_synthetic_goodware_and_holdout_keep_explicit_denominators(
    offline_only: list[str],
) -> None:
    providers = _ProviderPortGuard()
    positive = b"synthetic positive:RareMarkerQz"
    goodware = b"synthetic goodware:ordinary content"
    holdout_positive = b"synthetic holdout positive:RareMarkerQz"
    holdout_negative = b"synthetic holdout negative:ordinary content"
    samples = {
        hashlib.sha256(data).hexdigest(): data
        for data in (positive, goodware, holdout_positive, holdout_negative)
    }

    class FakeCorpusBytes:
        async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
            return samples[sample_sha256]

        async def get_artifact_id_for_sample(self, _sample_sha256: str) -> UUID:
            return uuid4()

    class FakeYara:
        def compile(self, _source: str) -> YaraEngineResult:
            return YaraEngineResult(
                status=YaraExecutionStatus.OK,
                engine_version="fake-yara-1",
            )

        def match(self, _source: str, corpus: tuple[object, ...]) -> YaraEngineResult:
            return YaraEngineResult(
                status=YaraExecutionStatus.OK,
                engine_version="fake-yara-1",
                matches_sha256=tuple(
                    sample.sample_sha256 for sample in corpus if b"RareMarkerQz" in sample.data
                ),
            )

    workbench = RuleWorkbench(
        corpus_bytes=FakeCorpusBytes(),
        yara_engine=FakeYara(),
        detection_candidates=SimpleNamespace(),
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    by_content = {data: digest for digest, data in samples.items()}
    holdout_date = datetime(2026, 10, 1, tzinfo=UTC)
    corpus = (
        RuleTestCase(
            sample_sha256=by_content[positive],
            role=RuleTestCorpusRole.HUMAN_CONFIRMED,
            provenance=RuleSampleProvenance.SYNTHETIC,
        ),
        RuleTestCase(
            sample_sha256=by_content[goodware],
            role=RuleTestCorpusRole.GOODWARE,
            provenance=RuleSampleProvenance.SYNTHETIC,
        ),
        RuleTestCase(
            sample_sha256=by_content[holdout_positive],
            role=RuleTestCorpusRole.HOLDOUT_POSITIVE,
            provenance=RuleSampleProvenance.SYNTHETIC,
            holdout_designated_at=holdout_date,
        ),
        RuleTestCase(
            sample_sha256=by_content[holdout_negative],
            role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
            provenance=RuleSampleProvenance.SYNTHETIC,
            holdout_designated_at=holdout_date,
        ),
    )
    report = await workbench.compile_and_test(
        'rule synthetic_strategy { strings: $marker = "RareMarkerQz" condition: $marker }',
        corpus,
        selection_bias="Synthetic fixtures selected to exercise the rule path.",
        coverage_statement="Synthetic positives, goodware and holdout fixtures only.",
        investigation_id=INVESTIGATION_ID,
    )
    metrics = report.metrics
    roles = {item.role: item for item in report.roles}

    assert report.validation_status is RuleValidationStatus.SYNTHETIC_ONLY
    assert report.validation_status is not RuleValidationStatus.REAL_VALIDATED
    assert roles[RuleTestCorpusRole.GOODWARE].provenance_counts["synthetic"] == 1
    assert roles[RuleTestCorpusRole.HOLDOUT_POSITIVE].provenance_counts["synthetic"] == 1
    assert roles[RuleTestCorpusRole.HOLDOUT_NEGATIVE].provenance_counts["synthetic"] == 1
    for metric in metrics.values():
        assert "numerator" in metric and "denominator" in metric
    assert metrics["real_goodware_false_positive_rate"]["denominator"] == 0
    assert metrics["real_holdout_sensitivity"]["denominator"] == 0
    assert providers.calls == []
    assert offline_only == []


def test_checkpoint_reports_budget_and_duplicate_attempt_only_from_persisted_facts() -> None:
    strategy = _projection(
        (
            _experiment(1, verdict=ExperimentVerdict.INCONCLUSIVE),
            _experiment(2, verdict=ExperimentVerdict.INCONCLUSIVE),
        )
    )
    checkpoint = build_investigation_operator_strategy_checkpoint(
        strategy,
        state=SimpleNamespace(stop_reason=StopReason.BUDGET_EXHAUSTED),
    )

    assert checkpoint["checkpoint"]["blocking_reasons"] == ["budget", "deja_testee"]
    assert checkpoint["next_action"]["status"] == "blocked"
