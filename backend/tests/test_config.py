import pytest
from pydantic import ValidationError

from cti_app.config import Settings


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
