import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cti_app.config import Settings


def test_workspace_defaults_are_host_explorable() -> None:
    settings = Settings(_env_file=None)

    assert settings.subject_workspace_root.as_posix() == "var/workspaces/subjects"
    assert settings.edition_workspace_root.as_posix() == "var/workspaces/editions"


def test_investigation_global_budget_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.investigation_max_tool_seconds == 3600
    assert settings.investigation_max_context_bytes == 4_000_000
    assert settings.investigation_no_progress_limit == 3


def test_settings_are_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("READINESS_TIMEOUT_SECONDS", "1.5")

    settings = Settings(_env_file=None)

    assert settings.s3_bucket == "test-bucket"
    assert settings.readiness_timeout_seconds == 1.5


def test_model_api_keys_are_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-only-secret")

    settings = Settings(_env_file=None)

    assert settings.qwen_api_key is not None
    assert "test-only-secret" not in repr(settings)
    assert settings.qwen_api_key.get_secret_value() == "test-only-secret"


def test_webai_defaults_keep_the_verified_gemini_model() -> None:
    settings = Settings(_env_file=None)

    assert settings.webai_base_url == "http://web_ai:6969/v1"
    assert settings.webai_model == "gemini-3-flash"


def test_editorial_resource_search_is_explicitly_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    assert settings.production_editorial_resource_search_enabled is False

    monkeypatch.setenv("PRODUCTION_EDITORIAL_RESOURCE_SEARCH_ENABLED", "true")
    assert Settings(_env_file=None).production_editorial_resource_search_enabled is True


def test_qwen_trust_boundary_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("QWEN_IS_EXTERNAL", "false")

    settings = Settings(_env_file=None)

    assert settings.qwen_is_external is False


def test_discovery_bridge_poll_interval_is_configurable_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCOVERY_BRIDGE_POLL_INTERVAL_SECONDS", "7")
    assert Settings(_env_file=None).discovery_bridge_poll_interval_seconds == 7

    monkeypatch.setenv("DISCOVERY_BRIDGE_POLL_INTERVAL_SECONDS", "11")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_model_wait_budgets_are_configurable_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(_env_file=None)
    assert defaults.openai_bridge_wait_timeout_seconds == 3600
    assert defaults.openai_bridge_wait_timeout_research_seconds == 7200
    assert defaults.model_request_timeout_seconds == 300
    assert defaults.model_request_timeout_research_seconds == 900
    assert defaults.model_background_wait_timeout_seconds == 5400
    assert defaults.model_background_wait_timeout_research_seconds is None
    assert defaults.model_background_idle_timeout_seconds == 1200
    assert defaults.model_background_idle_timeout_research_seconds is None
    assert defaults.job_bridge_ui_retry_base_seconds == 300
    assert defaults.job_bridge_ui_retry_max_seconds == 1800

    monkeypatch.setenv("OPENAI_BRIDGE_WAIT_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("OPENAI_BRIDGE_WAIT_TIMEOUT_RESEARCH_SECONDS", "1500")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_RESEARCH_SECONDS", "1000")
    monkeypatch.setenv("MODEL_BACKGROUND_WAIT_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("MODEL_BACKGROUND_WAIT_TIMEOUT_RESEARCH_SECONDS", "7200")
    monkeypatch.setenv("MODEL_BACKGROUND_IDLE_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("MODEL_BACKGROUND_IDLE_TIMEOUT_RESEARCH_SECONDS", "1800")
    monkeypatch.setenv("JOB_BRIDGE_UI_RETRY_BASE_SECONDS", "420")
    settings = Settings(_env_file=None)

    assert settings.openai_bridge_wait_timeout_seconds == 1200
    assert settings.openai_bridge_wait_timeout_research_seconds == 1500
    assert settings.model_request_timeout_seconds == 240
    assert settings.model_request_timeout_research_seconds == 1000
    assert settings.model_background_wait_timeout_seconds == 5400
    assert settings.model_background_wait_timeout_research_seconds == 7200
    assert settings.model_background_idle_timeout_seconds == 900
    assert settings.model_background_idle_timeout_research_seconds == 1800
    assert settings.job_bridge_ui_retry_base_seconds == 420

    monkeypatch.setenv("OPENAI_BRIDGE_WAIT_TIMEOUT_SECONDS", "86401")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_job_actor_time_limit_outlives_the_dramatiq_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Le défaut Dramatiq de 600 s tuait le worker en pleine attente du bridge.
    # La marge doit aussi couvrir la borne totale du bridge (3600 s) plus le
    # parsing, la persistance et le regroupement éditorial qui la suivent.
    assert Settings(_env_file=None).job_actor_time_limit_seconds >= 4500.0

    monkeypatch.setenv("JOB_ACTOR_TIME_LIMIT_SECONDS", "3600")
    assert Settings(_env_file=None).job_actor_time_limit_seconds == 3600.0

    monkeypatch.setenv("JOB_ACTOR_TIME_LIMIT_SECONDS", "600")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_virustotal_settings_are_optional_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.virustotal_proxy_url is None
    assert settings.virustotal_retrohunt_enabled is False
    assert settings.virustotal_retrohunt_proxy_enabled is False
    assert settings.virustotal_base_url == "https://www.virustotal.com/api/v3"
    assert settings.virustotal_retrohunt_max_result_pages == 20
    assert settings.virustotal_retrohunt_max_results == 1000
    monkeypatch.setenv("VIRUSTOTAL_RETROHUNT_ENABLED", "true")
    assert Settings(_env_file=None).virustotal_retrohunt_proxy_enabled is True
    monkeypatch.setenv("VIRUSTOTAL_MAX_PAGE_SIZE", "101")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_compose_propagates_critical_settings_to_api_and_worker_from_a_fake_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    backend_env = services["backend"]["environment"]
    worker_env = services["worker"]["environment"]

    critical_fields = {
        name
        for name in Settings.model_fields
        if name.startswith(("virustotal_", "shodan_", "openai_bridge_", "investigation_"))
    } | {"operator_api_token", "postgres_dsn"}
    expected_keys = {name.upper() for name in critical_fields}
    compose_keys = {
        name
        for name in backend_env
        if name.startswith(("VIRUSTOTAL_", "SHODAN_", "OPENAI_BRIDGE_", "INVESTIGATION_"))
        or name in {"OPERATOR_API_TOKEN", "POSTGRES_DSN"}
    }
    assert compose_keys == expected_keys
    assert all(key in worker_env for key in expected_keys)
    frontend_env = services["frontend"]["environment"]
    assert not expected_keys.intersection(frontend_env)

    fake_dotenv = tmp_path / ".env"
    fake_dotenv.write_text(
        "\n".join(
            (
                "POSTGRES_DB=fixture_db",
                "POSTGRES_USER=fixture_user",
                "POSTGRES_PASSWORD=fixture-password",
                "OPERATOR_API_TOKEN=operator-test-token-with-more-than-32-characters",
                "OPENAI_BRIDGE_API_KEY=bridge-test-secret",
                "VIRUSTOTAL_API_KEY=virustotal-test-secret",
                "VIRUSTOTAL_PROXY_URL=http://proxy.test:8080",
                "SHODAN_API_KEY=shodan-test-secret",
                "SHODAN_SEARCH_ENABLED=true",
                "INVESTIGATION_MAX_SHODAN_CREDITS=4",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_values = _parse_fake_dotenv(fake_dotenv)
    resolved_api = _resolve_compose_environment(backend_env, fake_values)
    resolved_worker = _resolve_compose_environment(worker_env, fake_values)

    assert {key: resolved_api[key] for key in expected_keys} == {
        key: resolved_worker[key] for key in expected_keys
    }
    assert resolved_api["POSTGRES_DSN"].startswith(
        "postgresql+asyncpg://fixture_user:fixture-password@postgres:5432/fixture_db"
    )

    snapshots: list[Settings] = []
    all_critical_keys = expected_keys
    for environment in (resolved_api, resolved_worker):
        with monkeypatch.context() as scoped:
            for key in all_critical_keys:
                scoped.delenv(key, raising=False)
            for key in expected_keys:
                value = environment[key]
                if value is None:
                    scoped.delenv(key, raising=False)
                else:
                    scoped.setenv(key, str(value))
            snapshots.append(Settings(_env_file=None))

    for name in critical_fields:
        assert getattr(snapshots[0], name) == getattr(snapshots[1], name)
    rendered_settings = repr(snapshots[0])
    for secret in (
        "operator-test-token-with-more-than-32-characters",
        "bridge-test-secret",
        "virustotal-test-secret",
        "shodan-test-secret",
    ):
        assert secret not in rendered_settings


_COMPOSE_VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)(?:(:-|-)([^{}]*))?\}")


def _parse_fake_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _resolve_compose_environment(
    environment: dict[str, object], fake_values: dict[str, str]
) -> dict[str, str | None]:
    def interpolate(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name, operator, fallback = match.groups()
            found = fake_values.get(name)
            if operator == ":-" and (found is None or found == ""):
                return fallback or ""
            if operator == "-" and found is None:
                return fallback or ""
            return found or ""

        return _COMPOSE_VARIABLE.sub(replace, value)

    return {
        name: None if value is None else interpolate(str(value))
        for name, value in environment.items()
    }


def test_local_analysis_and_ghidra_are_disabled_with_bounded_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    assert settings.local_analysis_enabled is False
    assert settings.local_analysis_require_filesystem_confinement is False
    assert settings.local_analysis_timeout_seconds == 30
    assert settings.local_analysis_max_output_bytes == 1_000_000
    assert settings.ghidra_enabled is False
    assert settings.ghidra_base_url == "http://127.0.0.1:8787"
    assert settings.ghidra_functions_path == ""
    assert settings.ghidra_search_path == ""

    monkeypatch.setenv("LOCAL_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT", "true")
    monkeypatch.setenv("LOCAL_ANALYSIS_CPU_LIMIT_SECONDS", "45")
    monkeypatch.setenv("GHIDRA_ENABLED", "true")
    monkeypatch.setenv("GHIDRA_FUNCTIONS_PATH", "/verified/functions")
    configured = Settings(_env_file=None)
    assert configured.local_analysis_enabled is True
    assert configured.local_analysis_require_filesystem_confinement is True
    assert configured.local_analysis_cpu_limit_seconds == 45
    assert configured.ghidra_enabled is True
    assert configured.ghidra_functions_path == "/verified/functions"

    monkeypatch.setenv("GHIDRA_MAX_RESPONSE_BYTES", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_jitter_bounds_allow_zero_but_reject_inverted_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRODUCTION_SUBJECT_JITTER_MIN_SECONDS", "0")
    monkeypatch.setenv("PRODUCTION_SUBJECT_JITTER_MAX_SECONDS", "0")
    monkeypatch.setenv("PRODUCTION_MODEL_JITTER_MIN_SECONDS", "0")
    monkeypatch.setenv("PRODUCTION_MODEL_JITTER_MAX_SECONDS", "0")
    settings = Settings(_env_file=None)
    assert settings.production_subject_jitter_min_seconds == 0
    assert settings.production_model_jitter_max_seconds == 0

    monkeypatch.setenv("PRODUCTION_MODEL_JITTER_MIN_SECONDS", "2")
    monkeypatch.setenv("PRODUCTION_MODEL_JITTER_MAX_SECONDS", "1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_pdf_inspection_cache_root_is_shared_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(_env_file=None).pdf_inspection_cache_root is None

    monkeypatch.setenv("PDF_INSPECTION_CACHE_ROOT", "/var/cache/autowork/pdf-inspections")

    settings = Settings(_env_file=None)

    assert settings.pdf_inspection_cache_root is not None
    assert settings.pdf_inspection_cache_root.as_posix() == "/var/cache/autowork/pdf-inspections"
