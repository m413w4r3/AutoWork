from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from typing import Any

import asyncpg
import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "ops" / "supervised_first_run_preflight.py"
)
_SPEC = importlib.util.spec_from_file_location("supervised_first_run_preflight", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def _valid_signals(**overrides: bool | None) -> dict[str, bool | None]:
    signals: dict[str, bool | None] = {
        "python_312": True,
        "conda_active": False,
        "critical_path_only_mocked": False,
        "api_worker_match": True,
        "bridge_accessible": True,
        "live_proxy_present": True,
        "quarantine_ok": True,
        "tlp_known": True,
        "operator_identity_verified": True,
        "database_safe": True,
        "publication_threatens_old_release": False,
    }
    signals.update(overrides)
    return signals


def _reference_args(**overrides: Any) -> Namespace:
    values: dict[str, Any] = {
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "production_run_id": "00000000-0000-0000-0000-000000000002",
        "synthesis_artifact_id": "00000000-0000-0000-0000-000000000003",
        "version": 2,
        "sha256": "a" * 64,
        "tlp": "TLP:AMBER",
    }
    values.update(overrides)
    return Namespace(**values)


def test_not_validated_and_offline_simulated_never_pass() -> None:
    assert preflight.normalize_gate_status("NOT_VALIDATED") == "NOT_VALIDATED"
    assert (
        preflight.normalize_gate_status("PASS", evidence_mode="offline_simulated")
        == "NOT_VALIDATED"
    )
    assert preflight.normalize_gate_status("PASS", evidence_mode="ci") == "NOT_VALIDATED"


def test_ci_green_does_not_supply_live_readiness() -> None:
    assert preflight.calculate_conclusion({}, ci_green=True) == "NO_GO"
    assert preflight.calculate_conclusion(_valid_signals(), ci_green=True) == "GO"
    assert preflight.calculate_conclusion(_valid_signals(conda_active=True)) == "NO_GO"
    assert preflight.calculate_conclusion(_valid_signals(python_312=False)) == "NO_GO"


def test_local_dev_identity_is_explicitly_unverified_and_never_passes_its_gate() -> None:
    environment = preflight._environment_profile({})
    assert preflight._environment_profile({"APP_ENV": "live"})["app_env"] == "production"

    assert environment["operator_identity"] == "LocalIdentityProvider/dev-analyst"
    assert environment["operator_identity_verified"] is False
    assert environment["operator_identity_scope"] == "local-isolated"
    assert "local isolé" in environment["operator_identity_warning"].casefold()

    gates = preflight._build_gates(
        checked_at="2026-10-14T00:00:00Z",
        git_sha="f01acc9",
        environment=environment,
        inventory={},
    )
    identity_gate = next(gate for gate in gates if gate["id"] == "L0")
    assert identity_gate["status"] == "NOT_VALIDATED"
    assert (
        preflight.calculate_conclusion(
            _valid_signals(operator_identity_verified=environment["operator_identity_verified"])
        )
        == "NO_GO"
    )


@pytest.mark.parametrize(
    "signal",
    (
        "critical_path_only_mocked",
        "api_worker_match",
        "bridge_accessible",
        "live_proxy_present",
        "quarantine_ok",
        "tlp_known",
        "operator_identity_verified",
        "database_safe",
        "publication_threatens_old_release",
    ),
)
def test_any_hard_live_blocker_forces_no_go(signal: str) -> None:
    triggered_value = signal in {
        "critical_path_only_mocked",
        "publication_threatens_old_release",
    }
    assert preflight.calculate_conclusion(_valid_signals(**{signal: triggered_value})) == "NO_GO"


def test_partial_go_needs_named_missing_dependency_and_reduced_scope() -> None:
    assert (
        preflight.calculate_conclusion(
            _valid_signals(),
            reduced_scope="passive-lookups-only",
            missing_dependencies=("network capture",),
        )
        == "GO_PARTIEL"
    )
    assert (
        preflight.calculate_conclusion(_valid_signals(), missing_dependencies=("network capture",))
        == "NO_GO"
    )
    assert (
        preflight.calculate_conclusion(
            _valid_signals(bridge_accessible=False),
            reduced_scope="passive-lookups-only",
            missing_dependencies=("network capture",),
        )
        == "NO_GO"
    )


def test_live_database_dsn_is_refused_without_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SUPERVISED_FIRST_RUN_DB_DISPOSABLE", "true")
    result = preflight._database_identity(
        "postgresql://pilot:db-secret@127.0.0.1:5432/autowork_live"
    )
    assert result["safe"] is False
    assert result["reason"] == "live_database_refused"
    assert "db-secret" not in str(result)


def test_pilot_database_without_disposable_marker_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("SUPERVISED_FIRST_RUN_DB_DISPOSABLE", raising=False)
    result = preflight._database_identity(
        "postgresql://pilot:fixture@127.0.0.1:5432/autowork_pilot"
    )
    assert result["safe"] is False
    assert result["reason"] == "explicit_disposable_pilot_identity_required"


def test_non_empty_database_without_alembic_identity_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyTransaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeConnection:
        def transaction(self, *, readonly: bool) -> ReadOnlyTransaction:
            assert readonly is True
            return ReadOnlyTransaction()

        async def fetchval(self, query: str) -> Any:
            if "current_database" in query:
                return "autowork_pilot"
            if "count(*)" in query:
                return 3
            if "to_regclass" in query:
                return False
            raise AssertionError("unexpected database query")

        async def fetch(self, _query: str) -> list[Any]:
            return []

        async def close(self) -> None:
            return None

    async def fake_connect(**_kwargs: Any) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    result = preflight._inspect_safe_local_database(
        "postgresql://pilot:secret@127.0.0.1:5432/autowork_pilot",
        "127.0.0.1",
        5432,
        "autowork_pilot",
        "127.0.0.1",
        "autowork_pilot",
    )
    assert result["safe"] is False
    assert result["reason"] == "non_empty_database_without_alembic_identity"


@pytest.mark.parametrize("selection_flag", ("--title", "--index"))
def test_title_or_index_selection_is_rejected(
    selection_flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        preflight.main([selection_flag, "arbitrary-selection"])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_exact_reference_requires_all_fields_and_canonical_values() -> None:
    valid = preflight._validated_reference(_reference_args())
    assert valid["valid"] is True
    assert valid["subject_id"] == "00000000-0000-0000-0000-000000000001"
    assert valid["sha256"] == "a" * 64
    assert valid["selection_by_title_or_index"] is False

    incomplete = preflight._validated_reference(_reference_args(tlp=None))
    assert incomplete["valid"] is False
    assert incomplete["missing_fields"] == ["tlp"]


def test_report_output_omits_credentials_and_sample_body() -> None:
    dsn = "postgresql://pilot:dsn-secret@127.0.0.1:5432/autowork_live"
    database = preflight._database_identity(dsn)
    report = {
        "checked_at": "2026-10-14T10:00:00Z",
        "git": {"sha": "abc", "tree_dirty": True, "changed_path_count": 1},
        "environment": {
            "app_env": "development",
            "python_version": "3.12.0",
            "conda_active": False,
        },
        "database": database,
        "conclusion": "NO_GO",
        "binaries": {},
        "services": {},
        "tool_inventory": {"reason": "provider adapters omitted"},
        "g2_and_budgets": {},
        "gates": [
            preflight._gate(
                "L0",
                "NOT_VALIDATED",
                checked_at="2026-10-14T10:00:00Z",
                git_sha="abc",
                environment={"app_env": "development"},
                evidence_refs=["exact-reference-input"],
                next_action="compare with canonical trace",
            )
        ],
        "refusals": [database["reason"]],
    }
    output = preflight.render_markdown(report) + preflight.render_text(report)
    assert "dsn-secret" not in output
    assert "sample-body-secret" not in output
    assert "checked_at" in output
    assert "exact-reference-input" in output
    assert "compare with canonical trace" in output
