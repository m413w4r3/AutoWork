from __future__ import annotations

import importlib.util
import subprocess
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
        "virustotal_proxy_present": True,
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
        "virustotal_proxy_present",
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


def test_standard_stack_database_requires_compose_identity_and_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def inspect_database(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"state": "SAFE_READ_ONLY", "safe": True, "migrated": True}

    monkeypatch.setattr(preflight, "_inspect_safe_local_database", inspect_database)
    result = preflight._database_identity(
        "postgresql+asyncpg://cti_app:fixture@postgres:5432/cti_app",
        env={
            "APP_ENV": "development",
            "SUPERVISED_FIRST_RUN_COMPOSE_PROJECT": "cti-bulletin",
            "SUPERVISED_FIRST_RUN_DB_DISPOSABLE": "1",
        },
    )

    assert result["safe"] is True
    assert calls["args"][1:4] == ("postgres", 5432, "cti_app")
    assert calls["kwargs"] == {"require_alembic_head": True, "stack_database": True}


@pytest.mark.parametrize(
    ("host", "project", "attestation", "reason"),
    (
        ("postgres", "cti-bulletin", None, "explicit_disposable_stack_attestation_required"),
        ("postgres", "unknown-project", "1", "compose_project_identity_required"),
        ("postgres.internal.example", "cti-bulletin", "1", "database_host_not_loopback"),
    ),
)
def test_stack_database_without_exact_local_identity_is_refused(
    host: str,
    project: str,
    attestation: str | None,
    reason: str,
) -> None:
    env = {
        "APP_ENV": "development",
        "SUPERVISED_FIRST_RUN_COMPOSE_PROJECT": project,
    }
    if attestation is not None:
        env["SUPERVISED_FIRST_RUN_DB_DISPOSABLE"] = attestation
    result = preflight._database_identity(
        f"postgresql://cti_app:fixture@{host}:5432/cti_app", env=env
    )
    assert result["safe"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("revision", "safe", "reason"),
    (
        ("head-revision", True, "explicit_disposable_stack_read_only"),
        ("stale-revision", False, "alembic_revision_not_at_head"),
    ),
)
def test_stack_database_requires_the_current_alembic_head(
    monkeypatch: pytest.MonkeyPatch, revision: str, safe: bool, reason: str
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
                return "cti_app"
            if "count(*)" in query:
                return 3
            if "to_regclass" in query:
                return True
            raise AssertionError("unexpected database query")

        async def fetch(self, _query: str) -> list[dict[str, str]]:
            return [{"version_num": revision}]

        async def close(self) -> None:
            return None

    async def fake_connect(**_kwargs: Any) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(preflight, "_expected_alembic_heads", lambda: ("head-revision",))
    result = preflight._inspect_safe_local_database(
        "postgresql://cti_app:fixture@postgres:5432/cti_app",
        "postgres",
        5432,
        "cti_app",
        "postgres",
        "cti_app",
        require_alembic_head=True,
        stack_database=True,
    )
    assert result["safe"] is safe
    assert result["migrated"] is safe
    assert result["reason"] == reason


def test_make_stack_preflight_executes_script_inside_worker() -> None:
    result = subprocess.run(
        [
            "make",
            "-n",
            "preflight-supervised-first-run-stack",
            "COMPOSE_MODELS=off",
            "SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1",
        ],
        cwd=_SCRIPT_PATH.parents[2],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--project-name cti-bulletin exec -T" in result.stdout
    assert "-e SUPERVISED_FIRST_RUN_COMPOSE_PROJECT=cti-bulletin" in result.stdout
    assert "-e SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1" in result.stdout
    assert "worker python /app/scripts/ops/supervised_first_run_preflight.py" in result.stdout


def test_stack_identity_profile_never_claims_the_human_is_authenticated() -> None:
    environment = preflight._environment_profile({"OPERATOR_API_TOKEN": "x" * 40})
    assert environment["identity_provider_mode"] == "authenticated"
    assert environment["identity_provider_verified"] is True
    assert environment["operator_identity_verified"] is False
    assert "x" * 40 not in str(environment)


def test_stack_redis_probe_uses_ping_without_disclosing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    calls: dict[str, Any] = {}

    class FakeRedis:
        def ping(self) -> bool:
            calls["ping"] = True
            return True

        def close(self) -> None:
            calls["closed"] = True

    def from_url(_cls: type[redis.Redis], value: str, **kwargs: Any) -> FakeRedis:
        calls["url"] = value
        calls["kwargs"] = kwargs
        return FakeRedis()

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(from_url))
    result = preflight._probe_stack_redis("redis://:redis-secret@redis:6379/0")
    assert result["state"] == "AVAILABLE"
    assert result["reason"] == "redis_ping"
    assert calls["ping"] is True
    assert calls["closed"] is True
    assert "redis-secret" not in str(result)


def test_stack_minio_probe_checks_bucket_with_read_only_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import minio

    calls: dict[str, Any] = {}

    def fake_execute(
        _client: Any,
        method: str,
        bucket: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        calls["method"] = method
        calls["bucket"] = bucket

    monkeypatch.setattr(minio.Minio, "_execute", fake_execute)
    result = preflight._probe_stack_minio(
        "minio:9000",
        {
            "S3_ACCESS_KEY": "access-secret",
            "S3_SECRET_KEY": "secret-key",
            "S3_BUCKET": "cti-local",
        },
    )
    assert result["state"] == "AVAILABLE"
    assert result["reason"] == "minio_head_bucket"
    assert calls["method"] == "HEAD"
    assert calls["bucket"] == "cti-local"
    assert "secret-key" not in str(result)


def test_bridge_probe_sends_only_get_models_capability_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            calls["client_kwargs"] = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict[str, str]) -> FakeResponse:
            calls["request"] = {"method": method, "url": url, "headers": headers}
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    result = preflight._probe_stack_bridge("http://chatgpt-bridge:8001/v1", "bridge-secret")
    assert result["state"] == "CAPABLE"
    assert calls["request"]["method"] == "GET"
    assert calls["request"]["url"] == "http://chatgpt-bridge:8001/v1/models"
    assert calls["request"]["headers"] == {"Authorization": "Bearer bridge-secret"}
    assert calls["client_kwargs"] == {
        "timeout": 3,
        "trust_env": False,
        "follow_redirects": False,
    }
    assert "bridge-secret" not in str(result)


def test_stack_identity_probe_reports_only_identity_status_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "mode": "authenticated",
                "verified": True,
                "identity_scope": "authenticated",
                "human_identity": True,
                "approvals_allowed": True,
                "bind_scope": "loopback",
                "token": "must-not-be-reported",
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            assert url == "http://backend:8000/api/identity/status"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    service, identity = preflight._probe_stack_identity()
    assert service["state"] == "AVAILABLE"
    assert identity == {
        "mode": "authenticated",
        "verified": True,
        "identity_scope": "authenticated",
        "human_identity": True,
        "approvals_allowed": True,
        "bind_scope": "loopback",
    }
    assert "must-not-be-reported" not in str(identity)


def test_runtime_inventory_uses_worker_factory_without_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "VIRUSTOTAL_PROXY_URL",
        "VIRUSTOTAL_API_KEY",
        "SHODAN_API_KEY",
        "OPERATOR_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VIRUSTOTAL_FILE_REPORT_ENABLED", "true")
    monkeypatch.setenv("VIRUSTOTAL_FILE_REPORT_LEGACY_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("VIRUSTOTAL_RETROHUNT_ENABLED", "false")

    inventory = preflight._safe_runtime_inventories()

    assert inventory["mode"] == "worker_runtime_registry"
    assert inventory["provider_calls"] == 0
    assert inventory["constructed_inventory_matches"] is True
    assert inventory["live_process_inventory_observed"] is False
    assert inventory["worker"]["families"]["virustotal"]["status"] == "available"


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
