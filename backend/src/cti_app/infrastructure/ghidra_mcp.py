from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TypedDict
from urllib.parse import urlsplit

import httpx

from cti_app.application.local_analysis import (
    GhidraPolicyDeniedError,
    GhidraResponseError,
    GhidraResponseTooLargeError,
    GhidraUnavailableError,
    GhidraVersionMismatchError,
)
from cti_app.config import Settings
from cti_app.domain.local_analysis import GhidraObservation, GhidraOperation
from cti_app.ghidra_service.contract import CONTRACT_VERSION

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ADDRESS = re.compile(r"^(?:0x)?[0-9a-fA-F]{1,16}$")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b(?:"
    r"api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|password|passwd|"
    r"authorization|private[_ -]?key)\b[\"']?\s*[:=]\s*[\"']?[^\"'\s,;}]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=-]{40,}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "bytes",
        "binary",
        "raw",
        "raw_bytes",
        "sample_bytes",
        "api_key",
        "access_token",
        "auth_token",
        "authorization",
        "secret",
        "password",
        "private_key",
    }
)
_OPERATION_NAMES = {
    GhidraOperation.FUNCTIONS: "list_functions",
    GhidraOperation.IMPORTS: "list_imports",
    GhidraOperation.XREF_TO: "xrefs_to",
    GhidraOperation.XREF_FROM: "xrefs_from",
    GhidraOperation.DECOMPILE_FUNCTION: "decompile_function",
    GhidraOperation.SEARCH: "search",
}
_MAX_COLLECTION_ITEMS = 100
_MAX_STRING_CHARS = 16_384
_MAX_NORMALIZED_RESPONSE_BYTES = 64 * 1024
_SampleBytesLoader = Callable[[str], Awaitable[bytes]]


class GhidraMcpHttpAdapter:
    """HTTP client for the fixed, read-only AutoWork Ghidra headless service."""

    def __init__(
        self,
        *,
        base_url: str = "http://ghidra:8787",
        enabled: bool = False,
        expected_version: str = "12.1.4",
        expected_java_version: str = "21.0.12.1",
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 150.0,
        max_sample_bytes: int = 200 * 1024 * 1024,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_observation_bytes: int = _MAX_NORMALIZED_RESPONSE_BYTES,
        sample_bytes_loader: _SampleBytesLoader | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if not expected_version.strip() or not expected_java_version.strip():
            raise ValueError("Ghidra and Java versions must be pinned")
        self._enabled = enabled
        self._expected_version = expected_version
        self._expected_java_version = expected_java_version
        self._max_sample_bytes = _positive(max_sample_bytes, "max_sample_bytes")
        self._max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._max_observation_bytes = _positive(max_observation_bytes, "max_observation_bytes")
        self._sample_bytes_loader = sample_bytes_loader
        self._attested = False
        self._attestation_reason: str | None = None
        self._ghidra_version: str | None = None
        self._java_version: str | None = None
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                read=read_timeout_seconds,
                connect=connect_timeout_seconds,
                write=connect_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            trust_env=False,
            follow_redirects=False,
        )
        self._probe_timeout_seconds = connect_timeout_seconds + 2.0

    @property
    def available(self) -> bool:
        return self._enabled and self._attested and self._sample_bytes_loader is not None

    @property
    def available_reason(self) -> str | None:
        if not self._enabled:
            return "ghidra_disabled_by_configuration"
        if self._sample_bytes_loader is None:
            return "ghidra_canonical_sample_reader_not_configured"
        if not self._attested:
            return self._attestation_reason or "ghidra_not_attested"
        return None

    def set_sample_bytes_loader(self, loader: _SampleBytesLoader) -> None:
        self._sample_bytes_loader = loader

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> bool:
        """Attest the exact service, Ghidra, Java and contract versions once at startup."""
        self._attested = False
        self._attestation_reason = None
        if not self._enabled:
            self._attestation_reason = "ghidra_disabled_by_configuration"
            return False
        try:
            body, status = await self._request_bounded(
                "GET",
                "/v1/probe",
                request_timeout=self._probe_timeout_seconds,
                max_body_bytes=4096,
            )
        except GhidraUnavailableError:
            self._attestation_reason = "ghidra_attestation_invalid"
            return False
        except (httpx.HTTPError, TimeoutError, GhidraResponseTooLargeError):
            self._attestation_reason = "ghidra_service_unreachable"
            return False
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._attestation_reason = "ghidra_attestation_invalid"
            return False
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("contract_version"), str)
            or not isinstance(payload.get("ghidra_version"), str)
            or not isinstance(payload.get("java_version"), str)
            or payload.get("status") not in {"ok", "unavailable"}
        ):
            self._attestation_reason = "ghidra_attestation_invalid"
            return False
        ghidra_version = _metadata(payload.get("ghidra_version"))
        java_version = _metadata(payload.get("java_version"))
        contract_version = payload.get("contract_version")
        self._ghidra_version = ghidra_version
        self._java_version = java_version
        if contract_version != CONTRACT_VERSION:
            self._attestation_reason = "ghidra_contract_version_mismatch"
            return False
        if ghidra_version != self._expected_version or java_version != self._expected_java_version:
            self._attestation_reason = "ghidra_attestation_version_mismatch"
            return False
        if status != 200 or payload.get("status") != "ok":
            self._attestation_reason = "ghidra_attestation_unavailable"
            return False
        self._attested = True
        return True

    async def analyze(
        self,
        operation: GhidraOperation | str,
        sample_sha256: str,
        *,
        address: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> GhidraObservation:
        try:
            normalized_operation = GhidraOperation(operation)
        except (TypeError, ValueError):
            raise GhidraPolicyDeniedError(
                "Only the fixed Ghidra read catalog is allowed."
            ) from None
        if not self._enabled:
            raise GhidraUnavailableError("ghidra_disabled_by_configuration")
        if not self._attested:
            raise GhidraUnavailableError(self.available_reason or "ghidra_not_attested")
        if self._sample_bytes_loader is None:
            raise GhidraUnavailableError("ghidra_canonical_sample_reader_not_configured")
        if not isinstance(sample_sha256, str) or not _SHA256.fullmatch(sample_sha256):
            raise GhidraPolicyDeniedError("A lowercase SHA-256 is required.")
        normalized_address: str | None = None
        if normalized_operation in {
            GhidraOperation.XREF_TO,
            GhidraOperation.XREF_FROM,
            GhidraOperation.DECOMPILE_FUNCTION,
        }:
            normalized_address = _normalize_address(address)
        elif address is not None:
            raise GhidraPolicyDeniedError("This operation does not accept an address.")
        normalized_query: str | None = None
        if normalized_operation is GhidraOperation.SEARCH:
            normalized_query = _normalize_query(query)
        elif query is not None:
            raise GhidraPolicyDeniedError("This operation does not accept a search query.")
        max_limit = 1 if normalized_operation is GhidraOperation.DECOMPILE_FUNCTION else 100
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
            raise GhidraPolicyDeniedError("The result limit is outside the fixed bounds.")

        try:
            sample_bytes = await self._sample_bytes_loader(sample_sha256)
        except Exception:
            raise GhidraUnavailableError("canonical_sample_bytes_unavailable") from None
        if not isinstance(sample_bytes, bytes) or not sample_bytes:
            raise GhidraUnavailableError("canonical_sample_bytes_unavailable")
        if len(sample_bytes) > self._max_sample_bytes:
            raise GhidraPolicyDeniedError("canonical_sample_bytes_exceed_limit")
        if hashlib.sha256(sample_bytes).hexdigest() != sample_sha256:
            raise GhidraResponseError("canonical_sample_bytes_hash_mismatch")

        operation_name = _OPERATION_NAMES[normalized_operation]
        parameters: dict[str, str | int] = {"operation": operation_name}
        if normalized_address is not None:
            parameters["address"] = normalized_address
        if normalized_query is not None:
            parameters["query"] = normalized_query
        parameters["limit"] = limit
        try:
            body, status = await self._request_bounded(
                "POST",
                "/v1/analyze",
                params=parameters,
                headers={
                    "X-Ghidra-Contract": CONTRACT_VERSION,
                    "X-Ghidra-SHA256": sample_sha256,
                    "Content-Type": "application/octet-stream",
                },
                content=sample_bytes,
            )
        except GhidraUnavailableError:
            raise
        except httpx.TimeoutException:
            raise GhidraUnavailableError("ghidra_service_timeout") from None
        except httpx.HTTPError:
            raise GhidraUnavailableError("ghidra_service_unreachable") from None
        if status == 422:
            raise GhidraResponseError("ghidra_service_rejected_canonical_sha256")
        if status == 413:
            raise GhidraPolicyDeniedError("ghidra_service_sample_limit_exceeded")
        if status < 200 or status >= 300:
            raise GhidraUnavailableError("ghidra_service_refused_analysis")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GhidraResponseError("ghidra_service_response_invalid_json") from None
        if not isinstance(payload, dict):
            raise GhidraResponseError("ghidra_service_response_not_object")
        ghidra_version = _metadata(payload.get("ghidra_version"))
        java_version = _metadata(payload.get("java_version"))
        _check_attested_versions(
            ghidra_version,
            java_version,
            self._expected_version,
            self._expected_java_version,
        )
        if (
            payload.get("schema_version") != "ghidra-service-result.v1"
            or payload.get("contract_version") != CONTRACT_VERSION
            or payload.get("operation") != operation_name
            or payload.get("sample_sha256") != sample_sha256
        ):
            raise GhidraResponseError("ghidra_service_response_identity_mismatch")
        data, truncated = _normalize_payload(payload.get("data"), self._max_observation_bytes)
        if not isinstance(data, dict):
            data = {"result": data}
        return GhidraObservation(
            operation=normalized_operation,
            sample_sha256=sample_sha256,
            ghidra_version=ghidra_version,
            # Compatibility field: this is the AutoWork service contract, not a GUI plugin.
            plugin_version=CONTRACT_VERSION,
            address=normalized_address,
            observed_at=datetime.now(UTC),
            data=data,
            truncated=bool(payload.get("truncated", False)) or truncated,
        )

    async def list_functions(self, sample_sha256: str, *, limit: int = 100) -> GhidraObservation:
        return await self.analyze(GhidraOperation.FUNCTIONS, sample_sha256, limit=limit)

    async def list_imports(self, sample_sha256: str, *, limit: int = 100) -> GhidraObservation:
        return await self.analyze(GhidraOperation.IMPORTS, sample_sha256, limit=limit)

    async def xrefs_to(
        self, sample_sha256: str, address: str, *, limit: int = 100
    ) -> GhidraObservation:
        return await self.analyze(
            GhidraOperation.XREF_TO, sample_sha256, address=address, limit=limit
        )

    async def xrefs_from(
        self, sample_sha256: str, address: str, *, limit: int = 100
    ) -> GhidraObservation:
        return await self.analyze(
            GhidraOperation.XREF_FROM, sample_sha256, address=address, limit=limit
        )

    async def decompile_function(self, sample_sha256: str, address: str) -> GhidraObservation:
        return await self.analyze(
            GhidraOperation.DECOMPILE_FUNCTION, sample_sha256, address=address, limit=1
        )

    async def search(
        self, sample_sha256: str, query: str, *, limit: int = 100
    ) -> GhidraObservation:
        return await self.analyze(GhidraOperation.SEARCH, sample_sha256, query=query, limit=limit)

    async def _request_bounded(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        request_timeout: float | None = None,
        max_body_bytes: int | None = None,
    ) -> tuple[bytes, int]:
        url = f"{self._base_url}{path}"
        async with self._client.stream(
            method,
            url,
            params=params,
            headers=headers,
            content=content,
            follow_redirects=False,
            timeout=request_timeout,
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise GhidraUnavailableError("Ghidra service redirects are refused.")
            body = bytearray()
            response_limit = self._max_response_bytes if max_body_bytes is None else max_body_bytes
            async for chunk in response.aiter_bytes():
                if len(chunk) > response_limit - len(body):
                    raise GhidraResponseTooLargeError("Ghidra service response exceeds its limit.")
                body.extend(chunk)
            return bytes(body), status


def create_ghidra_mcp_http_adapter(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sample_bytes_loader: _SampleBytesLoader | None = None,
) -> GhidraMcpHttpAdapter:
    """Construct the fixed service client; no MCP route paths are configurable."""
    return GhidraMcpHttpAdapter(
        base_url=settings.ghidra_base_url,
        enabled=settings.ghidra_enabled,
        expected_version=settings.ghidra_expected_version,
        expected_java_version=settings.ghidra_expected_java_version,
        connect_timeout_seconds=settings.ghidra_connect_timeout_seconds,
        read_timeout_seconds=settings.ghidra_read_timeout_seconds,
        max_sample_bytes=settings.ghidra_max_sample_bytes,
        max_response_bytes=settings.ghidra_max_response_bytes,
        sample_bytes_loader=sample_bytes_loader,
        transport=transport,
    )


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise GhidraPolicyDeniedError("The Ghidra base URL must be a plain internal HTTP URL.")
    hostname = parts.hostname.lower()
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if not loopback and hostname != "ghidra":
        raise GhidraPolicyDeniedError("The Ghidra service host must be loopback or compose ghidra.")
    if any(unicodedata.category(character) == "Cc" for character in base_url):
        raise GhidraPolicyDeniedError("The Ghidra base URL contains a control character.")
    return f"http://{hostname}:{parts.port or 8787}"


def _normalize_address(address: str | None) -> str:
    if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
        raise GhidraPolicyDeniedError("A hexadecimal address is required for this operation.")
    return f"0x{int(address, 16):x}"


def _normalize_query(query: str | None) -> str:
    if not isinstance(query, str):
        raise GhidraPolicyDeniedError("A search query is required.")
    normalized = query.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(unicodedata.category(character) == "Cc" for character in normalized)
    ):
        raise GhidraPolicyDeniedError("The Ghidra search query is outside the fixed bounds.")
    return normalized


def _metadata(value: object) -> str:
    return value.strip()[:128] if isinstance(value, str) and value.strip() else "unverified"


def _check_attested_versions(
    actual_ghidra: str, actual_java: str, expected_ghidra: str, expected_java: str
) -> None:
    if actual_ghidra != expected_ghidra or actual_java != expected_java:
        raise GhidraVersionMismatchError("Ghidra service version attestation changed.")


class _NormalizationState(TypedDict):
    remaining: int
    truncated: bool


def _normalize_payload(value: object, max_bytes: int) -> tuple[object, bool]:
    state: _NormalizationState = {"remaining": max_bytes, "truncated": False}
    normalized = _normalize_value(value, state, depth=0)
    if not isinstance(normalized, (dict, list)):
        normalized = {"value": normalized}
    encoded_length = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode())
    if encoded_length > max_bytes:
        state["truncated"] = True
        normalized = {"truncated": True}
    return normalized, bool(state["truncated"])


def _normalize_value(value: object, state: _NormalizationState, *, depth: int) -> object:
    if depth > 8:
        state["truncated"] = True
        return "[depth-limit]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        cleaned = _URL_IN_TEXT.sub("<url-redacted>", value)
        cleaned = "".join(
            character for character in cleaned if unicodedata.category(character) != "Cc"
        )
        cleaned = _CREDENTIAL_ASSIGNMENT.sub("<credential-redacted>", cleaned)
        cleaned = _BEARER_TOKEN.sub("Bearer <credential-redacted>", cleaned)
        cleaned = _JWT_TOKEN.sub("<token-redacted>", cleaned)
        cleaned = _LONG_TOKEN.sub("<token-redacted>", cleaned)
        cleaned = _PRIVATE_KEY_BLOCK.sub("<private-key-redacted>", cleaned)
        if len(cleaned) > _MAX_STRING_CHARS:
            cleaned = cleaned[:_MAX_STRING_CHARS]
            state["truncated"] = True
        remaining = state["remaining"]
        if len(cleaned.encode("utf-8")) > remaining:
            cleaned = cleaned.encode("utf-8")[: max(0, remaining)].decode("utf-8", errors="ignore")
            state["truncated"] = True
        state["remaining"] = max(0, remaining - len(cleaned.encode("utf-8")))
        return cleaned
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, child in list(value.items())[:_MAX_COLLECTION_ITEMS]:
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_FIELDS:
                state["truncated"] = True
                continue
            if state["remaining"] <= 0:
                state["truncated"] = True
                break
            output[key[:128]] = _normalize_value(child, state, depth=depth + 1)
        if len(value) > _MAX_COLLECTION_ITEMS:
            state["truncated"] = True
        return output
    if isinstance(value, (list, tuple)):
        output_list = []
        for child in value[:_MAX_COLLECTION_ITEMS]:
            if state["remaining"] <= 0:
                state["truncated"] = True
                break
            output_list.append(_normalize_value(child, state, depth=depth + 1))
        if len(value) > _MAX_COLLECTION_ITEMS:
            state["truncated"] = True
        return output_list
    state["truncated"] = True
    return "[unsupported-value]"


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
