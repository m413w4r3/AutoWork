"""Environment guards for the M23 editorial browser launcher.

The launcher must refuse to boot without the fake-provider attestation or when
the PostgreSQL administration DSN leaves loopback. These checks run before any
database or application object exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_emulator.editorial_browser_server import (
    ATTESTATION_ENV,
    _is_loopback_host,
    validate_environment,
)

pytestmark = pytest.mark.workflow_fast

LOOPBACK_DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres"


def _set_required(monkeypatch: pytest.MonkeyPatch, *, dsn: str = LOOPBACK_DSN) -> None:
    monkeypatch.setenv(ATTESTATION_ENV, "1")
    monkeypatch.setenv("TEST_POSTGRES_ADMIN_DSN", dsn)
    monkeypatch.setenv("WORKFLOW_BROWSER_STATE", "/tmp/editorial-browser-test-state.json")
    monkeypatch.delenv("WORKFLOW_BROWSER_API_PORT", raising=False)


@pytest.mark.parametrize(
    ("host", "expected"),
    (
        (None, True),
        ("", True),
        ("/var/run/postgresql", True),
        ("localhost", True),
        ("LOCALHOST.", True),
        ("127.0.0.1", True),
        ("127.9.9.9", True),
        ("::1", True),
        ("example.com", False),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
    ),
)
def test_is_loopback_host(host: str | None, expected: bool) -> None:
    assert _is_loopback_host(host) is expected


def test_validate_environment_requires_fake_provider_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(ATTESTATION_ENV)

    with pytest.raises(RuntimeError, match=ATTESTATION_ENV):
        validate_environment()


def test_validate_environment_rejects_non_loopback_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch, dsn="postgresql+asyncpg://postgres:postgres@db.example.com/postgres")

    with pytest.raises(RuntimeError, match="local PostgreSQL"):
        validate_environment()


def test_validate_environment_requires_state_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("WORKFLOW_BROWSER_STATE")

    with pytest.raises(RuntimeError, match="WORKFLOW_BROWSER_STATE"):
        validate_environment()


def test_validate_environment_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("WORKFLOW_BROWSER_API_PORT", "not-a-port")

    with pytest.raises(RuntimeError, match="WORKFLOW_BROWSER_API_PORT"):
        validate_environment()


def test_validate_environment_returns_the_loopback_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("WORKFLOW_BROWSER_API_PORT", "18767")

    admin_dsn, state_path, port = validate_environment()

    assert admin_dsn == LOOPBACK_DSN
    assert state_path == Path("/tmp/editorial-browser-test-state.json")
    assert port == 18767
