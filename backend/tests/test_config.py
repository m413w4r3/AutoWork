import pytest
from pydantic import ValidationError

from cti_app.config import Settings


def test_workspace_defaults_are_host_explorable() -> None:
    settings = Settings(_env_file=None)

    assert settings.subject_workspace_root.as_posix() == "var/workspaces/subjects"
    assert settings.edition_workspace_root.as_posix() == "var/workspaces/editions"


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
    monkeypatch.setenv("VIRUSTOTAL_MAX_PAGE_SIZE", "101")
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
