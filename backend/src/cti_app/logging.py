import json
import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
_http_logger = logging.getLogger("cti_app.http")


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str]:
    return _correlation_id.set(value[:128])


def reset_correlation_id(token: Token[str]) -> None:
    _correlation_id.reset(token)


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", get_correlation_id()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for field in (
            "http_method",
            "http_path",
            "http_status",
            "event",
            "job_id",
            "subject_id",
            "source_collection_id",
            "source_candidate_id",
            "requested_url",
            "phase",
            "state",
            "duration_ms",
            "size",
            "error_code",
            "summary",
        ):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level.upper())

    for logger_name in ("uvicorn", "uvicorn.error", "dramatiq"):
        named_logger = logging.getLogger(logger_name)
        named_logger.handlers = []
        named_logger.propagate = True

    # The middleware below provides an access event with the request correlation ID.
    logging.getLogger("uvicorn.access").disabled = True


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Tags every request, and gives unhandled failures somewhere durable to go.

    `on_failure` receives the request and the exception that escaped the
    endpoint. The container log holds the traceback too, but it is gone on the
    next rebuild — which is why an API error that produced a generic message in
    the browser used to leave nothing behind to diagnose it with.
    """

    def __init__(
        self,
        app: Any,
        on_failure: Callable[[Request, BaseException], None] | None = None,
    ) -> None:
        super().__init__(app)
        self._on_failure = on_failure

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_id = request.headers.get(CORRELATION_HEADER, "").strip()
        correlation_id = supplied_id[:128] if supplied_id else str(uuid4())
        token: Token[str] = _correlation_id.set(correlation_id)
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                _http_logger.exception(
                    "http_request_failed",
                    extra={"http_method": request.method, "http_path": request.url.path},
                )
                if self._on_failure is not None:
                    # A diagnostics sink must never turn a 500 into a crash.
                    try:
                        self._on_failure(request, exc)
                    except Exception:
                        _http_logger.warning("http_failure_trail_unavailable")
                raise
            # Les probes Compose/Kubernetes sont très fréquents : conserver les
            # échecs, mais ne pas noyer les événements métier avec les succès.
            is_successful_probe = (
                request.url.path
                in {
                    "/api/health/live",
                    "/api/health/ready",
                }
                and response.status_code < 400
            )
            if not is_successful_probe:
                _http_logger.info(
                    "http_request_completed",
                    extra={
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "http_status": response.status_code,
                    },
                )
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            _correlation_id.reset(token)
