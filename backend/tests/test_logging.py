import json
import logging
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.logging import (
    CorrelationIdMiddleware,
    JsonFormatter,
    configure_logging,
    request_query_parameter_names,
    reset_correlation_id,
    set_correlation_id,
)


def test_json_formatter_emits_structured_record() -> None:
    record = logging.LogRecord("cti.test", logging.INFO, __file__, 1, "service ready", (), None)
    record.correlation_id = "corr-123"
    record.http_status = 200

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "service ready"
    assert payload["correlation_id"] == "corr-123"
    assert payload["http_status"] == 200


def test_configure_logging_preserves_external_handlers_and_is_idempotent(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level
    original_record_factory = logging.getLogRecordFactory()
    names = ("uvicorn", "uvicorn.error", "dramatiq", "uvicorn.access")
    named_loggers = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in names
    }

    class RecordingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    external_handler = RecordingHandler()
    try:
        root_logger.addHandler(external_handler)

        configure_logging("INFO")
        configure_logging("INFO")

        assert external_handler in root_logger.handlers
        assert caplog.handler in root_logger.handlers
        assert (
            len(
                [
                    handler
                    for handler in root_logger.handlers
                    if isinstance(handler.formatter, JsonFormatter)
                ]
            )
            == 1
        )

        capsys.readouterr()
        correlation_token = set_correlation_id("corr-configure")
        try:
            logging.getLogger("cti_app.test_logging").warning(
                "configuration_probe",
                extra={"event": "test.configure_logging", "error_code": "probe"},
            )
        finally:
            reset_correlation_id(correlation_token)

        assert [record.event for record in external_handler.records] == ["test.configure_logging"]
        assert [record.event for record in caplog.records if hasattr(record, "event")] == [
            "test.configure_logging"
        ]
        output = capsys.readouterr().err.splitlines()
        assert len(output) == 1
        payload = json.loads(output[0])
        assert payload["event"] == "test.configure_logging"
        assert payload["error_code"] == "probe"
        assert payload["correlation_id"] == "corr-configure"
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
        logging.setLogRecordFactory(original_record_factory)
        for name, (handlers, propagate, disabled) in named_loggers.items():
            named_logger = logging.getLogger(name)
            named_logger.handlers[:] = handlers
            named_logger.propagate = propagate
            named_logger.disabled = disabled


def test_httpx_and_httpcore_records_redact_query_urls_and_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level
    original_record_factory = logging.getLogRecordFactory()
    try:
        configure_logging("INFO")
        caplog.set_level(logging.INFO, logger="httpx")
        caplog.set_level(logging.INFO, logger="httpcore.connection")
        caplog.set_level(logging.INFO, logger="cti_app.test")
        logging.getLogger("httpx").info(
            "HTTP Request: GET %s",
            httpx.URL("https://vt.example/files?apikey=vt-secret&safe=visible"),
            extra={
                "headers": {
                    "x-apikey": "header-secret",
                    "Authorization": "Bearer bearer-secret",
                },
                "raw_headers": (
                    (b"x-apikey", b"header-secret"),
                    (b"authorization", b"Bearer bearer-secret"),
                ),
            },
        )
        logging.getLogger("httpcore.connection").info(
            "request %s",
            "https://download.example/object?X-Amz-Signature=aws-secret"
            "&X-Goog-Credential=google-secret&Expires=123456",
        )
        logging.getLogger("cti_app.test").warning(
            "application response",
            extra={"summary": "Authorization: Bearer application-secret"},
        )

        rendered = "\n".join(
            record.getMessage()
            + "".join(
                str(getattr(record, field, "")) for field in ("headers", "raw_headers", "summary")
            )
            for record in caplog.records
        )

        for secret in (
            "vt-secret",
            "header-secret",
            "bearer-secret",
            "aws-secret",
            "google-secret",
            "123456",
            "application-secret",
        ):
            assert secret not in rendered
        assert "apikey=[REDACTED]" in rendered
        assert "safe=visible" in rendered
        assert "X-Amz-Signature=[REDACTED]" in rendered
        assert "X-Goog-Credential=[REDACTED]" in rendered
        assert "Expires=[REDACTED]" in rendered
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
        logging.setLogRecordFactory(original_record_factory)


def test_diagnostic_query_helper_keeps_names_without_values() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/diagnostics",
            "query_string": b"token=SECRET&visible=value&token=second-secret",
        }
    )

    assert request_query_parameter_names(request) == "token&visible"


def test_workers_use_the_shared_logging_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from cti_app.workers import tasks

    configured_levels: list[str] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(log_level="DEBUG"))
    monkeypatch.setattr(tasks, "configure_logging", configured_levels.append)

    tasks._configure_worker_logging()

    assert configured_levels == ["DEBUG"]


async def test_an_unhandled_request_failure_reaches_the_diagnostics_trail(tmp_path: Path) -> None:
    # The browser only ever sees "une erreur interne est survenue", and the
    # container log is wiped by the next rebuild. Without this hook a failing
    # endpoint leaves nothing behind to diagnose it with.
    application = FastAPI()
    trail = DiagnosticsLog.from_env(tmp_path)

    def record(request: Request, error: BaseException) -> None:
        trail.record_failure(
            event="http.request_failed",
            run_id=uuid4(),
            stage="http",
            error=error,
            http_path=request.url.path,
        )

    application.add_middleware(CorrelationIdMiddleware, on_failure=record)

    @application.get("/boom")
    async def boom() -> None:
        raise RuntimeError("la fusion a explosé")

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/boom")

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["http.request_failed"]
    assert events[0]["error"] == "la fusion a explosé"
    assert events[0]["http_path"] == "/boom"
    # The traceback is the point: the message alone does not locate the fault.
    traceback_file = tmp_path / events[0]["payload_file"]
    assert "RuntimeError: la fusion a explosé" in traceback_file.read_text(encoding="utf-8")


async def test_a_broken_diagnostics_sink_does_not_replace_the_original_failure() -> None:
    application = FastAPI()

    def record(request: Request, error: BaseException) -> None:
        raise OSError("disque plein")

    application.add_middleware(CorrelationIdMiddleware, on_failure=record)

    @application.get("/boom")
    async def boom() -> None:
        raise RuntimeError("la vraie cause")

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")
    assert response.status_code == 500
