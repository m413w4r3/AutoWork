import json
import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.logging import CorrelationIdMiddleware, JsonFormatter


def test_json_formatter_emits_structured_record() -> None:
    record = logging.LogRecord("cti.test", logging.INFO, __file__, 1, "service ready", (), None)
    record.correlation_id = "corr-123"
    record.http_status = 200

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "service ready"
    assert payload["correlation_id"] == "corr-123"
    assert payload["http_status"] == 200


async def test_an_unhandled_request_failure_reaches_the_diagnostics_trail(tmp_path) -> None:
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
