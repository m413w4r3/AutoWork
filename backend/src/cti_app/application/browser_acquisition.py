from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_CONTROL_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CAPTURE_JSON_COUNT = 10
MAX_CAPTURE_JSON_BODY_BYTES = 512 * 1024
MAX_CAPTURE_JSON_TOTAL_BYTES = 2 * 1024 * 1024
MAX_TIMEOUT_MS = 45_000
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "key",
        "password",
        "refresh_token",
        "secret",
        "session",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
)


class BrowserControlReason(StrEnum):
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    SIZE_LIMIT = "size_limit"
    CHALLENGE = "challenge"


class BrowserControlError(RuntimeError):
    reason_code: BrowserControlReason

    def __init__(self, reason_code: BrowserControlReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class BrowserUnconfiguredError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(BrowserControlReason.UNCONFIGURED, "Browser control is not configured")


class BrowserUnavailableError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(BrowserControlReason.UNAVAILABLE, "Browser renderer is unavailable")


class BrowserTimeoutError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(BrowserControlReason.TIMEOUT, "Browser renderer timed out")


class BrowserInvalidResponseError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(
            BrowserControlReason.INVALID_RESPONSE,
            "Browser renderer returned an invalid response",
        )


class BrowserSizeLimitError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(
            BrowserControlReason.SIZE_LIMIT,
            "Browser renderer response exceeded a size limit",
        )


class BrowserChallengeError(BrowserControlError):
    def __init__(self) -> None:
        super().__init__(
            BrowserControlReason.CHALLENGE,
            "Browser renderer reported a blocking challenge",
        )


@dataclass(frozen=True, slots=True)
class CapturedJson:
    url: str
    content_type: str
    body: bytes
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    rendered_html: str
    final_url: str
    status: int | None
    captured_json: tuple[CapturedJson, ...]
    challenge_detected: bool
    paywall_detected: bool
    engine: str
    duration_ms: int


class SidecarBrowserAdapter:
    def __init__(
        self,
        service_url: str | None = None,
        socket_path: str | None = None,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_url = service_url.strip().rstrip("/") if service_url else None
        self.socket_path = socket_path
        if not 0.2 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0.2 and 120")
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def render_offline(self, html: str, url: str) -> BrowserObservation:
        if not isinstance(html, str) or not html.strip():
            raise BrowserInvalidResponseError()
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise BrowserSizeLimitError()
        _validate_http_url(url)
        payload = {
            "html": html,
            "url": url,
            "timeout_ms": self._timeout_ms(),
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                value = await self._post_render(payload)
        except TimeoutError as exc:
            raise BrowserTimeoutError() from exc
        return self._parse_observation(value, default_url=url)

    async def navigate(self, url: str) -> BrowserObservation:
        _validate_http_url(url)
        if not self.socket_path and not self.service_url:
            raise BrowserUnconfiguredError()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                health = await self._request_json("GET", "/health")
                if (
                    not isinstance(health, dict)
                    or health.get("status") != "ready"
                    or health.get("browser") != "chromium"
                ):
                    raise BrowserInvalidResponseError()
                if health.get("navigation") is not True:
                    raise BrowserUnconfiguredError()
                value = await self._post_render(
                    {"url": url, "timeout_ms": self._timeout_ms(), "capture_json": True}
                )
        except TimeoutError as exc:
            raise BrowserTimeoutError() from exc
        observation = self._parse_observation(value, default_url=url)
        if value.get("error") == "challenge":
            raise BrowserChallengeError()
        return observation

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> SidecarBrowserAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _timeout_ms(self) -> int:
        return max(500, min(MAX_TIMEOUT_MS, int(self.timeout_seconds * 1000)))

    async def _post_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._request_json("POST", "/render", json_body=payload)
        except TimeoutError as exc:
            raise BrowserTimeoutError() from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._get_client()
        try:
            async with client.stream(method, path, json=json_body) as response:
                body = await _read_bounded(response)
                if response.status_code >= 400:
                    _raise_for_status(response.status_code, body)
                try:
                    value = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BrowserInvalidResponseError() from exc
                if not isinstance(value, dict):
                    raise BrowserInvalidResponseError()
                return value
        except BrowserControlError:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise BrowserTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise BrowserUnavailableError() from exc

    def _get_client(self) -> httpx.AsyncClient:
        if not self.socket_path and not self.service_url:
            raise BrowserUnconfiguredError()
        if self._client is None:
            if self.socket_path:
                base_url = "http://playwright-acquirer"
                transport = self._transport or httpx.AsyncHTTPTransport(
                    uds=self.socket_path,
                    retries=0,
                )
            else:
                assert self.service_url is not None
                parsed = urlsplit(self.service_url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.hostname is None
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise BrowserUnconfiguredError()
                base_url = self.service_url
                transport = self._transport or httpx.AsyncHTTPTransport(retries=0)
            self._client = httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    def _parse_observation(self, value: dict[str, Any], *, default_url: str) -> BrowserObservation:
        if value.get("error") == "challenge":
            raise BrowserChallengeError()
        rendered_html = value.get("rendered_html")
        if not isinstance(rendered_html, str) or not rendered_html.strip():
            raise BrowserInvalidResponseError()
        if len(rendered_html.encode("utf-8")) > MAX_HTML_BYTES:
            raise BrowserSizeLimitError()
        final_url = value.get("final_url", default_url)
        if not isinstance(final_url, str):
            raise BrowserInvalidResponseError()
        safe_final_url = _credential_free_url(final_url)
        if not _is_http_url(safe_final_url):
            raise BrowserInvalidResponseError()
        status = value.get("status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise BrowserInvalidResponseError()
        challenge_detected = value.get("challenge_detected", False)
        paywall_detected = value.get("paywall_detected", False)
        engine = value.get("engine", "chromium")
        duration_ms = value.get("duration_ms", 0)
        if (
            type(challenge_detected) is not bool
            or type(paywall_detected) is not bool
            or engine != "chromium"
            or type(duration_ms) is not int
            or duration_ms < 0
            or duration_ms > MAX_TIMEOUT_MS + 500
        ):
            raise BrowserInvalidResponseError()
        captured = _parse_captured_json(value.get("captured_json", []))
        return BrowserObservation(
            rendered_html=rendered_html,
            final_url=safe_final_url,
            status=status,
            captured_json=captured,
            challenge_detected=challenge_detected,
            paywall_detected=paywall_detected,
            engine=engine,
            duration_ms=duration_ms,
        )


async def _read_bounded(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_CONTROL_RESPONSE_BYTES:
                raise BrowserSizeLimitError()
        except ValueError as exc:
            raise BrowserInvalidResponseError() from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_CONTROL_RESPONSE_BYTES:
            raise BrowserSizeLimitError()
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_for_status(status: int, body: bytes) -> None:
    if status == 413:
        raise BrowserSizeLimitError()
    if status == 504:
        raise BrowserTimeoutError()
    if status == 422:
        raise BrowserChallengeError()
    if status == 503:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("error") == "secure_egress_unconfigured":
            raise BrowserUnconfiguredError()
        raise BrowserUnavailableError()
    raise BrowserInvalidResponseError()


def _parse_captured_json(value: Any) -> tuple[CapturedJson, ...]:
    if not isinstance(value, list) or len(value) > MAX_CAPTURE_JSON_COUNT:
        raise BrowserInvalidResponseError()
    entries: list[CapturedJson] = []
    total = 0
    for item in value:
        if not isinstance(item, dict):
            raise BrowserInvalidResponseError()
        url = item.get("url")
        content_type = item.get("content_type")
        encoded = item.get("body_base64")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(url, str)
            or not isinstance(content_type, str)
            or not _is_json_content_type(content_type)
            or not isinstance(encoded, str)
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
        ):
            raise BrowserInvalidResponseError()
        safe_url = _credential_free_url(url)
        if not _is_http_url(safe_url):
            raise BrowserInvalidResponseError()
        if size > MAX_CAPTURE_JSON_BODY_BYTES:
            raise BrowserSizeLimitError()
        try:
            body = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BrowserInvalidResponseError() from exc
        actual_digest = hashlib.sha256(body).hexdigest()
        if (
            len(body) != size
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or digest != actual_digest
        ):
            raise BrowserInvalidResponseError()
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserInvalidResponseError() from exc
        total += len(body)
        if total > MAX_CAPTURE_JSON_TOTAL_BYTES:
            raise BrowserSizeLimitError()
        entries.append(
            CapturedJson(
                url=safe_url,
                content_type=content_type.split(";", 1)[0].strip().casefold(),
                body=body,
                sha256=actual_digest,
                size=len(body),
            )
        )
    return tuple(entries)


def _validate_http_url(value: str) -> None:
    if not isinstance(value, str) or not _is_http_url(value):
        raise BrowserInvalidResponseError()
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise BrowserInvalidResponseError()
    except ValueError as exc:
        raise BrowserInvalidResponseError() from exc


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.casefold() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.port not in {0}
        )
    except ValueError:
        return False


def _credential_free_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
    except ValueError as exc:
        raise BrowserInvalidResponseError() from exc
    if port is not None and port not in ({80} if parsed.scheme.casefold() == "http" else {443}):
        netloc += f":{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if _safe_query_key(key)
    ]
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path, urlencode(query, doseq=True), "")
    )


def _safe_query_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    if normalized in {
        re.sub(r"[^a-z0-9]", "", key) for key in _CREDENTIAL_QUERY_KEYS
    }:
        return False
    return not any(
        marker in normalized
        for marker in ("token", "secret", "password", "authorization", "signature", "credential")
    ) and not normalized.startswith("xamz") and not normalized.startswith("xgoog")


def _is_json_content_type(value: str) -> bool:
    normalized = value.split(";", 1)[0].strip().casefold()
    return normalized == "application/json" or normalized.endswith("+json")
