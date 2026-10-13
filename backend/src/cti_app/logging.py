import json
import logging
import re
from collections.abc import Callable, Mapping
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
_SENSITIVE_QUERY_PARAMETER = re.compile(
    r"(?i)([?&])((?:apikey|api_key|key|token|access_token|signature|sig|"
    r"x-amz-[a-z0-9_-]+|x-goog-[a-z0-9_-]+|expires))=([^&#\s\"'<>]*)"
)
_SENSITIVE_HEADER = re.compile(
    r"(?i)(\b(?:x-apikey|authorization)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,\r\n;}&]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,\"'<>;)}]+")
_SECRET_REDACTING_FACTORY_MARKER = "_cti_secret_redacting_factory"


def _redact_sensitive_text(value: str) -> str:
    value = _SENSITIVE_QUERY_PARAMETER.sub(r"\1\2=[REDACTED]", value)
    value = _SENSITIVE_HEADER.sub(r"\1[REDACTED]", value)
    return _BEARER_VALUE.sub("Bearer [REDACTED]", value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        redacted = _redact_sensitive_text(decoded)
        return redacted.encode("utf-8") if redacted != decoded else value
    if isinstance(value, tuple):
        if len(value) == 2 and _is_sensitive_header_name(value[0]):
            redacted_value = b"[REDACTED]" if isinstance(value[1], bytes) else "[REDACTED]"
            return (_redact_value(value[0]), redacted_value)
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, Mapping):
        redacted_mapping: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _redact_value(key)
            if _is_sensitive_header_name(key):
                redacted_mapping[safe_key] = "[REDACTED]"
            else:
                redacted_mapping[safe_key] = _redact_value(item)
        return redacted_mapping
    return value


def _is_sensitive_header_name(value: Any) -> bool:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    return isinstance(value, str) and value.lower().strip() in {
        "authorization",
        "x-apikey",
        "x-api-key",
    }


def _is_secret_bearing_logger(name: str) -> bool:
    return name in {"httpx", "httpcore"} or name.startswith(("httpx.", "httpcore.", "cti_app."))


class SecretRedactionFilter(logging.Filter):
    """Redact credentials from HTTP and application records before any handler sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _is_secret_bearing_logger(record.name):
            return True
        record.msg = _redact_value(record.msg)
        record.args = _redact_value(record.args)
        for field, value in tuple(record.__dict__.items()):
            if field not in {"msg", "args", "exc_info"}:
                record.__dict__[field] = _redact_value(value)
        try:
            formatted_message = record.getMessage()
        except (TypeError, ValueError):
            formatted_message = None
        if formatted_message is not None:
            record.msg = _redact_sensitive_text(formatted_message)
            record.args = ()
        if record.exc_info:
            record.exc_text = _redact_sensitive_text(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


def _install_secret_redacting_record_factory() -> None:
    factory = logging.getLogRecordFactory()
    if getattr(factory, _SECRET_REDACTING_FACTORY_MARKER, False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        SecretRedactionFilter().filter(record)
        return record

    setattr(redacting_factory, _SECRET_REDACTING_FACTORY_MARKER, True)
    logging.setLogRecordFactory(redacting_factory)


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
            "message": _redact_sensitive_text(record.getMessage()),
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
                payload[field] = _redact_value(getattr(record, field))
        if record.exc_info:
            payload["exception"] = _redact_sensitive_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class _JsonStreamHandler(logging.StreamHandler[Any]):
    """The single root stream handler managed by ``configure_logging``."""


def configure_logging(level: str) -> None:
    _install_secret_redacting_record_factory()
    handler = _JsonStreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(SecretRedactionFilter())

    root_logger = logging.getLogger()
    # Keep handlers owned by pytest, monitoring, or the embedding process.
    # Replace only our own sink so repeated configuration stays idempotent.
    root_logger.handlers[:] = [
        existing
        for existing in root_logger.handlers
        if not isinstance(existing, _JsonStreamHandler)
    ]
    root_logger.addHandler(handler)
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


def request_query_parameter_names(request: Request) -> str | None:
    """Return only the query keys for diagnostics; values can contain credentials."""
    names = tuple(request.query_params.keys())
    return "&".join(names) or None
