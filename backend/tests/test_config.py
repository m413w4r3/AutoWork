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
