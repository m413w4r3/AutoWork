from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus, unquote, urlsplit

import httpx
from pydantic import SecretStr

from cti_app.application.shodan import (
    ShodanBudgetEstimate,
    ShodanCapabilities,
    ShodanCapabilityDisabledError,
    ShodanCountPort,
    ShodanHttpError,
    ShodanInvalidInputError,
    ShodanJsonError,
    ShodanNotConfiguredError,
    ShodanPayloadError,
    ShodanPort,
    ShodanQuotaError,
    ShodanResponseTooLargeError,
    ShodanTransportError,
    validate_shodan_domain,
    validate_shodan_ip,
    validate_shodan_page,
    validate_shodan_search_query,
)
from cti_app.config import Settings
from cti_app.domain.shodan import (
    ShodanCapability,
    ShodanCertificate,
    ShodanCountResult,
    ShodanDomainView,
    ShodanHostView,
    ShodanObservationMeta,
    ShodanRoute,
    ShodanSearchResult,
    ShodanService,
    ShodanServiceCapture,
)

DEFAULT_SHODAN_BASE_URL = "https://api.shodan.io"
MAX_BANNER_CHARACTERS = 4096
MAX_RETRY_AFTER_SECONDS = 300.0
_ACTIVE_SHODAN_KEY: ContextVar[str | None] = ContextVar("active_shodan_key", default=None)


class _ShodanHttpLogRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secret = _ACTIVE_SHODAN_KEY.get()
        if not secret:
            return True
        message = record.getMessage()
        for encoded_secret in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            message = message.replace(encoded_secret, "[REDACTED]")
        if message != record.getMessage():
            record.msg = message
            record.args = ()
        return True


# Retain adapter-level protection for standalone Shodan clients that run before
# application logging is configured. The central filter also redacts query keys.
_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(item, _ShodanHttpLogRedactor) for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_ShodanHttpLogRedactor())


def create_shodan_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create a non-redirecting client; its caller owns and closes its lifecycle."""
    timeout = httpx.Timeout(
        settings.shodan_read_timeout_seconds,
        connect=settings.shodan_connect_timeout_seconds,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"accept": "application/json"},
        trust_env=False,
    )


def create_shodan_http_adapter(
    *, client: httpx.AsyncClient, settings: Settings
) -> ShodanHttpAdapter:
    """Bind the explicit Shodan settings to a passive-only adapter."""
    return ShodanHttpAdapter(
        client=client,
        capabilities=ShodanCapabilities(
            host=settings.shodan_host_enabled,
            search=settings.shodan_search_enabled,
            domain=settings.shodan_domain_enabled,
            count=settings.shodan_count_enabled,
        ),
        api_key=settings.shodan_api_key,
        base_url=settings.shodan_base_url,
        max_response_bytes=settings.shodan_max_response_bytes,
        max_pages=settings.shodan_max_pages,
        max_results=settings.shodan_max_results,
    )


class ShodanHttpAdapter(ShodanPort, ShodanCountPort):
    """Read-only HTTP adapter for Shodan's indexed, passive observations."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        capabilities: ShodanCapabilities,
        api_key: SecretStr | None,
        base_url: str = DEFAULT_SHODAN_BASE_URL,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_pages: int = 10,
        max_results: int = 1000,
        budget_estimate: ShodanBudgetEstimate | None = None,
    ) -> None:
        self._client = client
        self._base_url = _validate_base_url(base_url)
        self._capabilities = capabilities
        self._api_key = api_key
        self._max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._max_pages = _positive(max_pages, "max_pages")
        self._max_results = _positive(max_results, "max_results")
        self._budget_estimate = budget_estimate or ShodanBudgetEstimate()

    async def host(self, ip: str) -> ShodanHostView:
        normalized_ip = validate_shodan_ip(ip)
        self._require(ShodanCapability.HOST)
        response = await self._get(f"/shodan/host/{quote(normalized_ip, safe=':')}")
        payload = {} if response.status_code == 404 else _json_object(response.body)
        result = _parse_host(
            payload,
            requested_ip=normalized_ip,
            raw_response=response.body,
            http_status=response.status_code,
            meta=_meta(
                route=ShodanRoute.HOST,
                credits=self._budget_estimate.credits_for(ShodanCapability.HOST),
                coverage_limits=(
                    "Les services représentent l'index Shodan, pas un scan en temps réel.",
                    f"Chaque bannière est limitée à {MAX_BANNER_CHARACTERS} caractères.",
                ),
            ),
        )
        account = await self._api_info()
        if account is not None:
            result = replace(result, meta=_with_account(result.meta, account))
        return result

    async def search(self, query: str, page: int = 1) -> ShodanSearchResult:
        normalized_query = validate_shodan_search_query(query)
        normalized_page = validate_shodan_page(page, max_pages=self._max_pages)
        self._require(ShodanCapability.SEARCH)
        response = await self._get(
            "/shodan/host/search",
            params={"query": normalized_query, "page": normalized_page},
        )
        payload = {} if response.status_code == 404 else _json_object(response.body)
        result = _parse_search(
            payload,
            query=normalized_query,
            page=normalized_page,
            max_results=self._max_results,
            raw_response=response.body,
            http_status=response.status_code,
            meta=_meta(
                route=ShodanRoute.SEARCH,
                credits=self._budget_estimate.credits_for(ShodanCapability.SEARCH),
                coverage_limits=(
                    f"La page demandée est bornée par shodan_max_pages={self._max_pages}.",
                    f"Les matches sont bornés à shodan_max_results={self._max_results}.",
                    "Les résultats proviennent de l'index Shodan passif.",
                ),
            ),
        )
        account = await self._api_info()
        if account is not None:
            result = _search_with_account(result, account)
        return result

    async def domain(self, domain: str) -> ShodanDomainView:
        normalized_domain = validate_shodan_domain(domain)
        self._require(ShodanCapability.DOMAIN)
        response = await self._get(
            f"/dns/domain/{quote(normalized_domain, safe='.-')}",
        )
        payload = {} if response.status_code == 404 else _json_object(response.body)
        result = _parse_domain(
            payload,
            requested_domain=normalized_domain,
            raw_response=response.body,
            http_status=response.status_code,
            meta=_meta(
                route=ShodanRoute.DOMAIN,
                credits=self._budget_estimate.credits_for(ShodanCapability.DOMAIN),
                coverage_limits=(
                    "Seuls les sous-domaines et enregistrements DNS renvoyés par Shodan sont "
                    "présents.",
                    "Les enregistrements reflètent l'index Shodan au moment de la réponse.",
                ),
            ),
        )
        account = await self._api_info()
        if account is not None:
            result = replace(result, meta=_with_account(result.meta, account))
        return result

    async def count(self, query: str) -> ShodanCountResult:
        normalized_query = validate_shodan_search_query(query)
        self._require(ShodanCapability.COUNT)
        response = await self._get(
            "/shodan/host/count",
            params={"query": normalized_query},
        )
        payload = {} if response.status_code == 404 else _json_object(response.body)
        total = _non_negative_int(payload.get("total"), "total", default=0)
        result = ShodanCountResult(
            total=total,
            query=normalized_query,
            meta=_meta(
                route=ShodanRoute.COUNT,
                credits=self._budget_estimate.credits_for(ShodanCapability.COUNT),
                coverage_limits=("Le total mesure l'index Shodan à l'instant de la réponse.",),
            ),
            raw_response=response.body,
            raw_sha256=_sha256(response.body),
            http_status=response.status_code,
        )
        account = await self._api_info()
        if account is not None:
            result = replace(result, meta=_with_account(result.meta, account))
        return result

    def _require(self, capability: ShodanCapability) -> None:
        if not self._capabilities.is_enabled(capability):
            raise ShodanCapabilityDisabledError(
                "La capacité Shodan demandée est désactivée localement."
            )
        if self._api_key is None or not self._api_key.get_secret_value():
            raise ShodanNotConfiguredError("La clé API Shodan n'est pas configurée.")

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> _RawResponse:
        _validate_passive_path(path)
        if self._api_key is None:
            raise ShodanNotConfiguredError("La clé API Shodan n'est pas configurée.")
        request_params: dict[str, str | int] = {"key": self._api_key.get_secret_value()}
        if params:
            request_params.update(params)
        log_token = _ACTIVE_SHODAN_KEY.set(self._api_key.get_secret_value())
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}{path}",
                params=request_params,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise ShodanHttpError(
                        "Shodan a renvoyé une redirection inattendue.",
                        code="shodan_unexpected_redirect",
                        status_code=status,
                    )
                if status in {401, 403}:
                    detail = "unauthorized" if status == 401 else "forbidden"
                    raise ShodanCapabilityDisabledError(
                        "La capacité Shodan n'est pas autorisée par les identifiants configurés.",
                        code=f"shodan_upstream_{detail}",
                        status_code=status,
                    )
                if status == 402:
                    raise ShodanQuotaError(
                        "Le quota de crédits Shodan est épuisé.",
                        status_code=status,
                    )
                if status == 429:
                    raise ShodanHttpError(
                        "Shodan limite temporairement les requêtes.",
                        code="shodan_rate_limited",
                        retryable=True,
                        status_code=status,
                        retry_after=_retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 400 and status != 404:
                    raise ShodanHttpError(
                        "Shodan a refusé la requête.",
                        code="shodan_upstream_error",
                        retryable=status >= 500,
                        status_code=status,
                    )
                body = await _read_bounded(response, self._max_response_bytes)
                return _RawResponse(body=body, status_code=status)
        except (ShodanHttpError, ShodanQuotaError, ShodanCapabilityDisabledError):
            raise
        except ShodanResponseTooLargeError:
            raise
        except httpx.TimeoutException:
            raise ShodanTransportError(
                "La requête Shodan a expiré.",
                code="shodan_timeout",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            # httpx exceptions can include the request URL, which contains the key.
            raise ShodanTransportError(
                "Le transport Shodan a échoué.",
                code="shodan_transport_error",
                retryable=True,
            ) from None
        finally:
            _ACTIVE_SHODAN_KEY.reset(log_token)

    async def _api_info(self) -> tuple[int | None, str | None] | None:
        try:
            response = await self._get("/api-info")
            payload = _json_object(response.body)
        except (
            ShodanHttpError,
            ShodanQuotaError,
            ShodanCapabilityDisabledError,
            ShodanJsonError,
            ShodanPayloadError,
            ShodanResponseTooLargeError,
            ShodanTransportError,
        ):
            return None
        credits = payload.get("query_credits")
        plan = payload.get("plan")
        return (
            credits
            if isinstance(credits, int) and not isinstance(credits, bool) and credits >= 0
            else None,
            plan.strip() if isinstance(plan, str) and plan.strip() else None,
        )


@dataclass(frozen=True, slots=True)
class _RawResponse:
    body: bytes
    status_code: int


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ShodanInvalidInputError(
            "La base URL Shodan doit être une origine HTTP(S) sans identifiants ni paramètres."
        )
    return value.rstrip("/")


def _validate_passive_path(path: str) -> None:
    if path in {
        "/shodan/host/search",
        "/shodan/host/count",
        "/api-info",
    }:
        return
    if path.startswith("/shodan/host/"):
        validate_shodan_ip(unquote(path.removeprefix("/shodan/host/")))
        return
    if path.startswith("/dns/domain/"):
        validate_shodan_domain(unquote(path.removeprefix("/dns/domain/")))
        return
    raise ShodanInvalidInputError("Cette route Shodan n'est pas une lecture passive autorisée.")


def _positive(value: int, name: str) -> int:
    if value < 1:
        raise ValueError(f"{name} doit être positif")
    return value


def _meta(
    *,
    route: ShodanRoute,
    credits: int,
    coverage_limits: tuple[str, ...],
    service_captures: tuple[ShodanServiceCapture, ...] = (),
) -> ShodanObservationMeta:
    return ShodanObservationMeta(
        captured_at=datetime.now(UTC),
        service_captures=service_captures,
        coverage_limits=coverage_limits,
        credits_consumed=credits,
        query_credits=None,
        plan=None,
        route=route,
    )


def _with_account(
    meta: ShodanObservationMeta, account: tuple[int | None, str | None]
) -> ShodanObservationMeta:
    return replace(meta, query_credits=account[0], plan=account[1])


def _search_with_account(
    result: ShodanSearchResult, account: tuple[int | None, str | None]
) -> ShodanSearchResult:
    updated_meta = _with_account(result.meta, account)
    matches = tuple(
        replace(match, meta=_with_account(match.meta, account)) for match in result.matches
    )
    return replace(result, matches=matches, meta=updated_meta)


def _parse_host(
    payload: dict[str, object],
    *,
    requested_ip: str,
    raw_response: bytes,
    http_status: int,
    meta: ShodanObservationMeta,
) -> ShodanHostView:
    services_raw = payload.get("data", [])
    if isinstance(services_raw, str):
        # A search match carries one service banner in `data`; a host lookup
        # carries the service list there.
        service = _parse_service(payload)
        services = [service] if service is not None else []
    elif isinstance(services_raw, list):
        services = []
        captures: list[ShodanServiceCapture] = []
        captured_at = meta.captured_at
        for item in services_raw:
            if not isinstance(item, dict):
                raise ShodanPayloadError("Un service Shodan n'est pas un objet JSON.")
            service = _parse_service(item)
            if service is None:
                continue
            services.append(service)
            age = (
                max(0.0, (captured_at - service.timestamp).total_seconds())
                if service.timestamp is not None
                else None
            )
            captures.append(
                ShodanServiceCapture(
                    port=service.port,
                    transport=service.transport,
                    timestamp=service.timestamp,
                    age_seconds=age,
                )
            )
    else:
        raise ShodanPayloadError("Shodan n'a pas renvoyé une liste de services.")
    if isinstance(services_raw, str):
        captures = []
        if services:
            service = services[0]
            age = (
                max(0.0, (meta.captured_at - service.timestamp).total_seconds())
                if service.timestamp is not None
                else None
            )
            captures.append(
                ShodanServiceCapture(
                    port=service.port,
                    transport=service.transport,
                    timestamp=service.timestamp,
                    age_seconds=age,
                )
            )
    service_tuple = tuple(services)
    ports_raw = payload.get("ports")
    if isinstance(ports_raw, list):
        ports = tuple(sorted({port for port in ports_raw if _is_port(port)}))
    else:
        ports = tuple(sorted({service.port for service in service_tuple}))
    enriched_meta = replace(meta, service_captures=tuple(captures))
    return ShodanHostView(
        ip=requested_ip,
        organization=_string(payload.get("org")),
        asn=_string(payload.get("asn")),
        hostnames=_string_tuple(payload.get("hostnames")),
        ports=ports,
        tags=_string_tuple(payload.get("tags")),
        services=service_tuple,
        meta=enriched_meta,
        raw_response=raw_response,
        raw_sha256=_sha256(raw_response),
        http_status=http_status,
    )


def _parse_search(
    payload: dict[str, object],
    *,
    query: str,
    page: int,
    max_results: int,
    raw_response: bytes,
    http_status: int,
    meta: ShodanObservationMeta,
) -> ShodanSearchResult:
    del query
    matches_raw = payload.get("matches", [])
    if not isinstance(matches_raw, list):
        raise ShodanPayloadError("Shodan n'a pas renvoyé une liste de matches.")
    total = _non_negative_int(payload.get("total"), "total", default=len(matches_raw))
    raw_matches = matches_raw[:max_results]
    matches: list[ShodanHostView] = []
    all_captures: list[ShodanServiceCapture] = []
    for match in raw_matches:
        if not isinstance(match, dict):
            raise ShodanPayloadError("Un match Shodan n'est pas un objet JSON.")
        ip_value = match.get("ip_str")
        if not isinstance(ip_value, str):
            raise ShodanPayloadError("Un match Shodan ne contient pas d'adresse IP.")
        try:
            normalized_ip = validate_shodan_ip(ip_value)
        except ShodanInvalidInputError:
            raise ShodanPayloadError("Un match Shodan contient une adresse IP invalide.") from None
        parsed = _parse_host(
            match,
            requested_ip=normalized_ip,
            raw_response=raw_response,
            http_status=http_status,
            meta=meta,
        )
        matches.append(parsed)
        all_captures.extend(parsed.meta.service_captures)
    result_meta = replace(meta, service_captures=tuple(all_captures))
    return ShodanSearchResult(
        matches=tuple(matches),
        total=total,
        page=page,
        meta=result_meta,
        raw_response=raw_response,
        raw_sha256=_sha256(raw_response),
        http_status=http_status,
    )


def _parse_domain(
    payload: dict[str, object],
    *,
    requested_domain: str,
    raw_response: bytes,
    http_status: int,
    meta: ShodanObservationMeta,
) -> ShodanDomainView:
    subdomains_raw = payload.get("subdomains", [])
    if not isinstance(subdomains_raw, list) or not all(
        isinstance(item, str) for item in subdomains_raw
    ):
        raise ShodanPayloadError("Shodan n'a pas renvoyé une liste de sous-domaines.")
    records_raw = payload.get("data", payload.get("records", []))
    if not isinstance(records_raw, list) or not all(isinstance(item, dict) for item in records_raw):
        raise ShodanPayloadError("Shodan n'a pas renvoyé une liste d'enregistrements DNS.")
    return ShodanDomainView(
        domain=requested_domain,
        subdomains=tuple(subdomains_raw),
        dns_records=tuple(dict(record) for record in records_raw),
        meta=meta,
        raw_response=raw_response,
        raw_sha256=_sha256(raw_response),
        http_status=http_status,
    )


def _parse_service(payload: dict[str, object]) -> ShodanService | None:
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        return None
    banner_value = payload.get("data")
    banner: str | None = banner_value if isinstance(banner_value, str) else None
    truncated = False
    if banner is not None and len(banner) > MAX_BANNER_CHARACTERS:
        banner = banner[:MAX_BANNER_CHARACTERS]
        truncated = True
    return ShodanService(
        port=port,
        transport=_string(payload.get("transport")),
        product=_string(payload.get("product")),
        banner=banner,
        banner_truncated=truncated,
        timestamp=_parse_datetime(payload.get("timestamp")),
        certificate=_parse_certificate(payload.get("ssl")),
    )


def _parse_certificate(value: object) -> ShodanCertificate | None:
    if not isinstance(value, dict):
        return None
    cert_raw = value.get("cert")
    cert = cert_raw if isinstance(cert_raw, dict) else value
    fingerprint_raw = cert.get("fingerprint")
    fingerprint = None
    if isinstance(fingerprint_raw, dict):
        fingerprint = fingerprint_raw.get("sha256")
    if fingerprint is None:
        fingerprint = cert.get("fingerprint_sha256")
    normalized_fingerprint = None
    if isinstance(fingerprint, str):
        candidate = fingerprint.replace(":", "").strip().lower()
        if len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate):
            normalized_fingerprint = candidate
    issuer = _subject_name(cert.get("issuer"))
    subject = _subject_name(cert.get("subject"))
    issued_at = _parse_datetime(cert.get("issued"))
    expires_at = _parse_datetime(cert.get("expires"))
    if not any((normalized_fingerprint, issuer, subject, issued_at, expires_at)):
        return None
    return ShodanCertificate(
        fingerprint_sha256=normalized_fingerprint,
        issuer=issuer,
        subject=subject,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _subject_name(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    values = [f"{key}={item}" for key, item in sorted(value.items()) if isinstance(item, str)]
    return ", ".join(values) or None


def _json_object(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ShodanJsonError("Shodan a renvoyé un JSON invalide.") from None
    if not isinstance(payload, dict):
        raise ShodanPayloadError("La réponse Shodan n'est pas un objet JSON.")
    return payload


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise ShodanResponseTooLargeError("La réponse Shodan dépasse la limite configurée.")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    seconds: float | None = None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            seconds = (parsed - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _non_negative_int(value: object, name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShodanPayloadError(f"Le champ Shodan {name} est invalide.")
    return value


def _is_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
