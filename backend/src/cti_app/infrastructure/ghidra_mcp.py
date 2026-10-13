from __future__ import annotations

import ipaddress
import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypedDict, cast
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

logger = logging.getLogger(__name__)

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
_MAX_COLLECTION_ITEMS = 100
_MAX_STRING_CHARS = 4096
_MAX_NORMALIZED_RESPONSE_BYTES = 64 * 1024


class GhidraMcpHttpAdapter:
    """Local, read-only HTTP boundary for a deployment-specific GhidraMCP bridge."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8787",
        endpoint_paths: Mapping[GhidraOperation, str] | None = None,
        enabled: bool = False,
        allowed_hosts: tuple[str, ...] = (),
        expected_version: str | None = None,
        expected_plugin_version: str | None = None,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 60.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_observation_bytes: int = _MAX_NORMALIZED_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url, allowed_hosts)
        self._paths = _validate_endpoint_paths(endpoint_paths or {})
        self._enabled = enabled
        self._expected_version = expected_version
        self._expected_plugin_version = expected_plugin_version
        self._max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._max_observation_bytes = _positive(max_observation_bytes, "max_observation_bytes")
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

    async def aclose(self) -> None:
        await self._client.aclose()

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
                "Seules les opérations de lecture enregistrées sont autorisées."
            ) from None
        if not self._enabled:
            raise GhidraUnavailableError("La capacité Ghidra est désactivée localement.")
        if not _SHA256.fullmatch(sample_sha256):
            raise GhidraPolicyDeniedError("Un SHA-256 valide est requis.")
        path = self._paths.get(normalized_operation)
        if path is None:
            raise GhidraUnavailableError("Le chemin de cette capacité Ghidra n'est pas configuré.")
        parameters: dict[str, str | int] = {"sample_sha256": sample_sha256}
        normalized_address: str | None = None
        if normalized_operation in {
            GhidraOperation.XREF_TO,
            GhidraOperation.XREF_FROM,
            GhidraOperation.DECOMPILE_FUNCTION,
        }:
            normalized_address = _normalize_address(address)
            parameters["address"] = normalized_address
        elif address is not None:
            raise GhidraPolicyDeniedError("Cette opération n'accepte pas d'adresse.")
        if normalized_operation is GhidraOperation.SEARCH:
            normalized_query = _normalize_query(query)
            parameters["query"] = normalized_query
        elif query is not None:
            raise GhidraPolicyDeniedError("Cette opération n'accepte pas de recherche libre.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise GhidraPolicyDeniedError("La limite de résultats est hors bornes.")
        parameters["limit"] = limit

        try:
            response_body, status_code = await self._get_bounded(path, parameters)
        except GhidraUnavailableError:
            raise
        except httpx.TimeoutException:
            raise GhidraUnavailableError("L'endpoint Ghidra local a expiré.") from None
        except httpx.HTTPError:
            raise GhidraUnavailableError("L'endpoint Ghidra local est indisponible.") from None
        if status_code < 200 or status_code >= 300:
            raise GhidraUnavailableError("L'endpoint Ghidra local a refusé la lecture.")
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GhidraResponseError("La réponse Ghidra n'est pas un objet JSON valide.") from None
        if not isinstance(payload, dict):
            raise GhidraResponseError("La réponse Ghidra doit être un objet JSON.")

        ghidra_version = _version_value(
            payload, ("ghidra_version", "ghidraVersion"), self._expected_version
        )
        plugin_version = _version_value(
            payload, ("plugin_version", "pluginVersion"), self._expected_plugin_version
        )
        _check_expected_version("Ghidra", ghidra_version, self._expected_version)
        _check_expected_version("plugin", plugin_version, self._expected_plugin_version)
        observation_data = payload.get("data", payload.get("result", payload))
        normalized_data, truncated = _normalize_payload(
            observation_data, self._max_observation_bytes
        )
        if not isinstance(normalized_data, dict):
            normalized_data = {"result": normalized_data}
        return GhidraObservation(
            operation=normalized_operation,
            sample_sha256=sample_sha256,
            ghidra_version=ghidra_version,
            plugin_version=plugin_version,
            address=normalized_address,
            observed_at=datetime.now(UTC),
            data=cast(dict[str, object], normalized_data),
            truncated=truncated,
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

    async def _get_bounded(self, path: str, params: Mapping[str, str | int]) -> tuple[bytes, int]:
        url = f"{self._base_url}{path}"
        try:
            async with self._client.stream(
                "GET", url, params=params, follow_redirects=False
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise GhidraUnavailableError("Une redirection Ghidra a été refusée.")
                if status < 200 or status >= 300:
                    return b"", status
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = self._max_response_bytes - len(body)
                    if len(chunk) > remaining:
                        raise GhidraResponseTooLargeError(
                            "La réponse Ghidra dépasse la taille maximale autorisée."
                        )
                    body.extend(chunk)
                return bytes(body), status
        except (GhidraUnavailableError, GhidraResponseTooLargeError):
            raise


def create_ghidra_mcp_http_adapter(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GhidraMcpHttpAdapter:
    """Bind Settings without assuming any unverified GhidraMCP endpoint paths."""
    paths = {
        GhidraOperation.FUNCTIONS: settings.ghidra_functions_path,
        GhidraOperation.IMPORTS: settings.ghidra_imports_path,
        GhidraOperation.XREF_TO: settings.ghidra_xrefs_to_path,
        GhidraOperation.XREF_FROM: settings.ghidra_xrefs_from_path,
        GhidraOperation.DECOMPILE_FUNCTION: settings.ghidra_decompile_function_path,
        GhidraOperation.SEARCH: settings.ghidra_search_path,
    }
    allowed_hosts = tuple(
        host.strip().lower() for host in settings.ghidra_allowed_hosts.split(",") if host.strip()
    )
    return GhidraMcpHttpAdapter(
        base_url=settings.ghidra_base_url,
        endpoint_paths=paths,
        enabled=settings.ghidra_enabled,
        allowed_hosts=allowed_hosts,
        expected_version=settings.ghidra_expected_version,
        expected_plugin_version=settings.ghidra_expected_plugin_version,
        connect_timeout_seconds=settings.ghidra_connect_timeout_seconds,
        read_timeout_seconds=settings.ghidra_read_timeout_seconds,
        max_response_bytes=settings.ghidra_max_response_bytes,
        transport=transport,
    )


def _validate_base_url(base_url: str, allowed_hosts: tuple[str, ...]) -> str:
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise GhidraPolicyDeniedError("L'URL Ghidra doit être une URL HTTP locale valide.")
    hostname = parts.hostname.lower()
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if not is_loopback and hostname not in allowed_hosts:
        raise GhidraPolicyDeniedError("L'hôte Ghidra n'est pas loopback ni explicitement autorisé.")
    if not is_loopback:
        logger.warning(
            "ghidra_non_loopback_host_allowed_for_isolated_deployment",
            extra={"ghidra_host": hostname},
        )
    if any(unicodedata.category(character) == "Cc" for character in base_url):
        raise GhidraPolicyDeniedError("L'URL Ghidra contient un caractère de contrôle.")
    return base_url.rstrip("/")


def _validate_endpoint_paths(
    paths: Mapping[GhidraOperation, str],
) -> dict[GhidraOperation, str]:
    validated: dict[GhidraOperation, str] = {}
    for raw_operation, path in paths.items():
        try:
            operation = GhidraOperation(raw_operation)
        except (TypeError, ValueError):
            raise GhidraPolicyDeniedError(
                "Une opération Ghidra non autorisée est configurée."
            ) from None
        if not path:
            continue
        parts = urlsplit(path)
        if (
            not path.startswith("/")
            or parts.scheme
            or parts.netloc
            or parts.query
            or parts.fragment
            or any(segment == ".." for segment in parts.path.split("/"))
            or any(unicodedata.category(character) == "Cc" for character in path)
        ):
            raise GhidraPolicyDeniedError("Le chemin Ghidra configuré est invalide.")
        validated[operation] = path
    return validated


def _normalize_address(address: str | None) -> str:
    if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
        raise GhidraPolicyDeniedError("Une adresse hexadécimale est requise pour cette opération.")
    return f"0x{int(address, 16):x}"


def _normalize_query(query: str | None) -> str:
    if not isinstance(query, str):
        raise GhidraPolicyDeniedError("Une requête localisée est requise.")
    normalized = query.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(unicodedata.category(character) == "Cc" for character in normalized)
    ):
        raise GhidraPolicyDeniedError("La requête de recherche Ghidra est hors bornes.")
    return normalized


def _version_value(
    payload: Mapping[str, object], keys: tuple[str, ...], expected: str | None
) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _sanitize_metadata(value.strip())
    return _sanitize_metadata(expected.strip()) if expected and expected.strip() else "unverified"


def _check_expected_version(name: str, actual: str, expected: str | None) -> None:
    if expected and actual != "unverified" and actual != expected:
        raise GhidraVersionMismatchError(
            f"La version {name} Ghidra ne correspond pas à la version attendue."
        )


class _NormalizationState(TypedDict):
    remaining: int
    truncated: bool


def _normalize_payload(value: object, max_bytes: int) -> tuple[object, bool]:
    state: _NormalizationState = {"remaining": max_bytes, "truncated": False}
    normalized = _normalize_value(value, state, depth=0)
    if not isinstance(normalized, (dict, list)):
        normalized = {"value": normalized}
    try:
        encoded_length = len(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
        )
    except (TypeError, ValueError):
        raise GhidraResponseError("La réponse Ghidra contient une valeur non JSON.") from None
    if encoded_length > max_bytes:
        state["truncated"] = True
        normalized = {"truncated": True}
    return normalized, bool(state["truncated"])


def _normalize_value(value: object, state: _NormalizationState, *, depth: int) -> object:
    if depth > 6:
        state["truncated"] = True
        return "<depth-limit>"
    if value is None or isinstance(value, bool) or isinstance(value, int | float):
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
    if isinstance(value, dict):
        output: dict[str, object] = {}
        for key, child in list(value.items())[:_MAX_COLLECTION_ITEMS]:
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_FIELDS:
                state["truncated"] = True
                continue
            if state["remaining"] <= 0:
                state["truncated"] = True
                break
            normalized_key = key[:128]
            output[normalized_key] = _normalize_value(child, state, depth=depth + 1)
        if len(value) > _MAX_COLLECTION_ITEMS:
            state["truncated"] = True
        return output
    if isinstance(value, list):
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
    return "<unsupported-value>"


def _sanitize_metadata(value: str) -> str:
    sanitized = _URL_IN_TEXT.sub("<url-redacted>", value)
    sanitized = _CREDENTIAL_ASSIGNMENT.sub("<credential-redacted>", sanitized)
    sanitized = _BEARER_TOKEN.sub("Bearer <credential-redacted>", sanitized)
    sanitized = _JWT_TOKEN.sub("<token-redacted>", sanitized)
    sanitized = _LONG_TOKEN.sub("<token-redacted>", sanitized)
    sanitized = _PRIVATE_KEY_BLOCK.sub("<private-key-redacted>", sanitized)
    return sanitized[:128] or "unverified"


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
