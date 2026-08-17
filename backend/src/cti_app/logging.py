import json
import logging
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
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_id = request.headers.get(CORRELATION_HEADER, "").strip()
        correlation_id = supplied_id[:128] if supplied_id else str(uuid4())
        token: Token[str] = _correlation_id.set(correlation_id)
        try:
            try:
                response = await call_next(request)
            except Exception:
                _http_logger.exception(
                    "http_request_failed",
                    extra={"http_method": request.method, "http_path": request.url.path},
                )
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
