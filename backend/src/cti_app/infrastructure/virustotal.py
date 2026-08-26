from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from cti_app.application.virustotal import (
    FileRelationship,
    VirusTotalCapabilities,
    VirusTotalCapabilityDisabledError,
    VirusTotalConfigurationError,
    VirusTotalError,
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalJsonError,
    VirusTotalOperationRoute,
    VirusTotalPage,
    VirusTotalPayloadError,
    VirusTotalPort,
    VirusTotalRawResponse,
    VirusTotalRelationNotAllowedError,
    VirusTotalResponseTooLargeError,
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
    VirusTotalSearchResult,
    VirusTotalTransportError,
    VirusTotalUnexpectedRedirectError,
    normalize_file_hash,
    validate_search_query,
)
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)

logger = logging.getLogger(__name__)


def create_virustotal_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create the proxy-only client; its caller owns and closes its lifecycle."""
    if not settings.virustotal_proxy_url:
        raise VirusTotalConfigurationError("Le proxy VirusTotal n'est pas configuré.")
    proxy_url = settings.virustotal_proxy_url
    proxy: str | httpx.Proxy = proxy_url
    if settings.virustotal_proxy_insecure and proxy_url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        proxy = httpx.Proxy(url=proxy_url, ssl_context=context)
    timeout = httpx.Timeout(
        settings.virustotal_read_timeout_seconds,
        connect=settings.virustotal_connect_timeout_seconds,
    )
    return httpx.AsyncClient(
        proxy=proxy,
        verify=not settings.virustotal_proxy_insecure,
        timeout=timeout,
        follow_redirects=False,
        headers={"accept": "application/json"},
        trust_env=False,
    )


def create_virustotal_direct_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create the optional direct client used only by explicitly authorized fallbacks.

    Built only when a key exists AND at least one direct route is actually
    enabled — the key's mere presence never authorizes building this client.
    """
    if settings.virustotal_api_key is None:
        raise VirusTotalConfigurationError("La clé API directe VirusTotal n'est pas configurée.")
    if not settings.virustotal_file_report_legacy_fallback_enabled:
        raise VirusTotalConfigurationError(
            "Aucune route directe VirusTotal n'est activée explicitement."
        )
    timeout = httpx.Timeout(
        settings.virustotal_read_timeout_seconds,
        connect=settings.virustotal_connect_timeout_seconds,
    )
    return httpx.AsyncClient(
        headers={
            "accept": "application/json",
            "x-apikey": settings.virustotal_api_key.get_secret_value(),
        },
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


class VirusTotalHttpAdapter(VirusTotalPort):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        capabilities: VirusTotalCapabilities,
        fallback_base_url: str | None = None,
        legacy_base_url: str | None = None,
        direct_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        file_report_proxy_fallback_enabled: bool = False,
        file_report_legacy_fallback_enabled: bool = False,
        routing_policy: VirusTotalRoutingPolicy | None = None,
        max_response_bytes: int = 10 * 1024 * 1024,
        default_page_size: int = 40,
        max_page_size: int = 100,
        max_pages: int = 10,
        max_results: int = 1000,
    ) -> None:
        self._client = client
        self._base_url = _validate_base_url(base_url)
        self._fallback_base_url = (
            _validate_base_url(fallback_base_url) if fallback_base_url else None
        )
        self._legacy_base_url = (
            _validate_legacy_base_url(legacy_base_url) if legacy_base_url else None
        )
        self._direct_client = direct_client
        self._api_key = api_key
        if (direct_client is None) != (api_key is None):
            raise ValueError("direct_client et api_key doivent être fournis ensemble")
        self._capabilities = capabilities
        self._routing_policy = routing_policy or _default_routing_policy(
            base_url=self._base_url,
            fallback_base_url=self._fallback_base_url,
            legacy_base_url=self._legacy_base_url,
            direct_client=self._direct_client,
            api_key=self._api_key,
            proxy_fallback_enabled=file_report_proxy_fallback_enabled,
            legacy_fallback_enabled=file_report_legacy_fallback_enabled,
        )
        self._max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._default_page_size = _bounded(default_page_size, 1, max_page_size, "default_page_size")
        self._max_page_size = _positive(max_page_size, "max_page_size")
        self._max_pages = _positive(max_pages, "max_pages")
        self._max_results = _positive(max_results, "max_results")

    async def file_report(self, file_hash: str) -> VirusTotalFileReport:
        self._require(VirusTotalCapability.FILE_REPORT)
        normalized = normalize_file_hash(file_hash)
        route = self._resolve_route(VirusTotalCapability.FILE_REPORT)
        response, step = await self._execute_route(
            route, lambda step: self._file_report_request(step, normalized)
        )
        payload = _json_object(response.body)
        file = (
            _parse_v2_file(payload, normalized)
            if _is_v2_file_report(payload)
            else _parse_file(payload)
        )
        return VirusTotalFileReport(
            file=file,
            raw_json=response.body,
            http_status=response.status_code,
            transport=step.transport,
            api_generation=step.variant,
        )

    async def _file_report_request(
        self, step: VirusTotalRouteStep, file_hash: str
    ) -> VirusTotalRawResponse:
        client, base_url = self._resolve_transport(step)
        if step.variant is VirusTotalEndpointVariant.LEGACY_V2:
            return await self._get(
                "/file/report",
                client=client,
                base_url=base_url,
                params={"resource": file_hash, "apikey": self._api_key},
            )
        return await self._get(f"/files/{file_hash}", client=client, base_url=base_url)

    async def _execute_route(
        self,
        route: VirusTotalOperationRoute,
        request: Callable[[VirusTotalRouteStep], Awaitable[VirusTotalRawResponse]],
    ) -> tuple[VirusTotalRawResponse, VirusTotalRouteStep]:
        steps = route.steps()
        for index, step in enumerate(steps):
            is_last = index == len(steps) - 1
            try:
                return await request(step), step
            except VirusTotalError as error:
                if is_last or not route.permits_fallback(error):
                    raise
        raise VirusTotalRouteUnavailableError(
            "Aucune route VirusTotal utilisable n'est configurée."
        )

    def _resolve_transport(self, step: VirusTotalRouteStep) -> tuple[httpx.AsyncClient, str]:
        if step.transport is VirusTotalTransportKind.PROXY:
            if step.variant is VirusTotalEndpointVariant.V3_FALLBACK:
                if self._fallback_base_url is None:
                    raise VirusTotalRouteUnavailableError(
                        "La route proxy de secours VirusTotal n'est pas configurée."
                    )
                return self._client, self._fallback_base_url
            return self._client, self._base_url
        if self._direct_client is None or self._legacy_base_url is None or self._api_key is None:
            raise VirusTotalRouteUnavailableError(
                "La route directe VirusTotal n'est pas configurée."
            )
        return self._direct_client, self._legacy_base_url

    def _resolve_route(self, capability: VirusTotalCapability) -> VirusTotalOperationRoute:
        route = self._routing_policy.route_for(capability)
        if route is None:
            raise VirusTotalRouteUnavailableError(
                "Aucune route VirusTotal n'est configurée pour cette opération."
            )
        return route

    async def file_relationship(
        self,
        file_hash: str,
        relation: FileRelationship,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self._require(VirusTotalCapability.FILE_RELATIONSHIPS)
        self._resolve_route(VirusTotalCapability.FILE_RELATIONSHIPS)
        normalized = normalize_file_hash(file_hash)
        if not isinstance(relation, FileRelationship):
            raise VirusTotalRelationNotAllowedError("La relation fichier n'est pas autorisée.")
        return await self._paginate(
            f"/files/{normalized}/{relation.value}", limit=limit, cursor=cursor, paginate=paginate
        )

    async def intelligence_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalSearchResult:
        self._require(VirusTotalCapability.INTELLIGENCE_SEARCH)
        self._resolve_route(VirusTotalCapability.INTELLIGENCE_SEARCH)
        query = validate_search_query(query)
        page = await self._paginate(
            "/intelligence/search",
            limit=limit,
            cursor=cursor,
            paginate=paginate,
            extra_params={"query": query, "descriptors_only": "true"},
        )
        return VirusTotalSearchResult(
            items=page.items,
            next_cursor=page.next_cursor,
            observed_count=page.observed_count,
            stopped_due_to_limit=page.stopped_due_to_limit,
            exhaustive=page.exhaustive,
            raw_json_pages=page.raw_json_pages,
            http_statuses=page.http_statuses,
            limit_used=page.limit_used,
            transport=page.transport,
            api_generation=page.api_generation,
        )

    async def _paginate(
        self,
        path: str,
        *,
        limit: int | None,
        cursor: str | None,
        paginate: bool,
        extra_params: Mapping[str, str] | None = None,
    ) -> VirusTotalPage:
        page_limit = self._page_limit(limit)
        pages: list[bytes] = []
        statuses: list[int] = []
        items: list[dict[str, Any]] = []
        current_cursor = cursor
        stopped = False
        for _ in range(self._max_pages if paginate else 1):
            params: dict[str, Any] = dict(extra_params or {})
            params["limit"] = page_limit
            if current_cursor:
                params["cursor"] = current_cursor
            response = await self._get(path, params=params)
            payload = _json_object(response.body)
            page_items = _parse_items(payload)
            pages.append(response.body)
            statuses.append(response.status_code)
            remaining = self._max_results - len(items)
            items.extend(page_items[:remaining])
            next_cursor = _next_cursor(payload)
            if len(items) >= self._max_results and (len(page_items) > remaining or next_cursor):
                stopped = True
                next_cursor = next_cursor
                break
            if not paginate or not next_cursor:
                current_cursor = next_cursor
                break
            current_cursor = next_cursor
        else:
            stopped = bool(current_cursor)
            next_cursor = current_cursor
        exhaustive = not stopped and next_cursor is None
        return VirusTotalPage(
            items=tuple(items),
            next_cursor=next_cursor,
            observed_count=len(items),
            stopped_due_to_limit=stopped,
            exhaustive=exhaustive,
            raw_json_pages=tuple(pages),
            http_statuses=tuple(statuses),
            limit_used=page_limit,
            transport=VirusTotalTransportKind.PROXY,
            api_generation=VirusTotalEndpointVariant.V3,
        )

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> VirusTotalRawResponse:
        try:
            response = await (client or self._client).get(
                f"{base_url or self._base_url}{path}", params=params
            )
            if 300 <= response.status_code < 400:
                raise VirusTotalUnexpectedRedirectError(
                    "VirusTotal a renvoyé une redirection inattendue.",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                retry_after = _retry_after(response.headers.get("Retry-After"))
                raise _http_error(response.status_code, retry_after)
            body = await _read_bounded(response, self._max_response_bytes)
            return VirusTotalRawResponse(body=body, status_code=response.status_code)
        except VirusTotalError:
            raise
        except httpx.ConnectTimeout as exc:
            raise VirusTotalTransportError(
                "Le proxy VirusTotal a expiré pendant la connexion.",
                code="virustotal_connect_timeout",
                retryable=True,
            ) from exc
        except httpx.ReadTimeout as exc:
            raise VirusTotalTransportError(
                "VirusTotal a expiré pendant la lecture.",
                code="virustotal_read_timeout",
                retryable=True,
            ) from exc
        except httpx.ProxyError as exc:
            raise VirusTotalTransportError(
                "La connexion au proxy VirusTotal a échoué.",
                code="virustotal_proxy_error",
                retryable=True,
            ) from exc
        except httpx.ConnectError as exc:
            if _is_tls_error(exc):
                raise VirusTotalTransportError(
                    "La vérification TLS de VirusTotal a échoué.",
                    code="virustotal_tls_error",
                    retryable=False,
                ) from exc
            raise VirusTotalTransportError(
                "La connexion au proxy VirusTotal a échoué.",
                code="virustotal_connection_error",
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            raise VirusTotalTransportError(
                "La requête VirusTotal a expiré.", code="virustotal_timeout", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise VirusTotalTransportError(
                "Le transport VirusTotal a échoué.",
                code="virustotal_transport_error",
                retryable=True,
            ) from exc

    def _page_limit(self, value: int | None) -> int:
        if value is None:
            return self._default_page_size
        if value < 1 or value > self._max_page_size:
            raise VirusTotalInvalidInputError("La taille de page VirusTotal est hors limites.")
        return value

    def _require(self, capability: VirusTotalCapability) -> None:
        if not self._capabilities.is_enabled(capability):
            raise VirusTotalCapabilityDisabledError("La capability VirusTotal est désactivée.")


def _default_routing_policy(
    *,
    base_url: str,
    fallback_base_url: str | None,
    legacy_base_url: str | None,
    direct_client: httpx.AsyncClient | None,
    api_key: str | None,
    proxy_fallback_enabled: bool,
    legacy_fallback_enabled: bool,
) -> VirusTotalRoutingPolicy:
    """Build the deny-by-default policy this adapter ships with.

    Every step below requires its own explicit enable flag: neither a
    configured fallback base URL nor a present API key is, by itself,
    sufficient to add a step. Only `file_report` carries fallbacks; other
    operations use the proxy primary route only, matching what this adapter
    actually implements for them.
    """
    file_report_steps = [
        VirusTotalRouteStep(VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3)
    ]
    if proxy_fallback_enabled and fallback_base_url is not None and fallback_base_url != base_url:
        file_report_steps.append(
            VirusTotalRouteStep(
                VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3_FALLBACK
            )
        )
    if (
        legacy_fallback_enabled
        and direct_client is not None
        and api_key is not None
        and legacy_base_url is not None
    ):
        file_report_steps.append(
            VirusTotalRouteStep(VirusTotalTransportKind.DIRECT, VirusTotalEndpointVariant.LEGACY_V2)
        )
    file_report_primary, *file_report_fallbacks = file_report_steps
    proxy_only = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3)
    )
    return VirusTotalRoutingPolicy(
        routes={
            VirusTotalCapability.FILE_REPORT: VirusTotalOperationRoute(
                primary=file_report_primary, fallbacks=tuple(file_report_fallbacks)
            ),
            VirusTotalCapability.FILE_RELATIONSHIPS: proxy_only,
            VirusTotalCapability.INTELLIGENCE_SEARCH: proxy_only,
        }
    )


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "virustotal.com",
        "www.virustotal.com",
    }:
        raise VirusTotalConfigurationError("La base VirusTotal doit cibler l'API VirusTotal v3.")
    if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/api/v3"):
        raise VirusTotalConfigurationError(
            "La base VirusTotal doit être un préfixe API v3 sans querystring."
        )
    return value.rstrip("/")


def _validate_legacy_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "virustotal.com",
        "www.virustotal.com",
    }:
        raise VirusTotalConfigurationError("La base legacy VirusTotal est invalide.")
    if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/vtapi/v2"):
        raise VirusTotalConfigurationError("La base legacy doit cibler /vtapi/v2.")
    return value.rstrip("/")


def _positive(value: int, name: str) -> int:
    if value < 1:
        raise ValueError(f"{name} doit être positif")
    return value


def _bounded(value: int, lower: int, upper: int, name: str) -> int:
    if value < lower or value > upper:
        raise ValueError(f"{name} est hors limites")
    return value


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise VirusTotalResponseTooLargeError(
                "La réponse VirusTotal dépasse la limite configurée."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VirusTotalJsonError("VirusTotal a renvoyé un JSON invalide.") from exc
    if not isinstance(payload, dict):
        raise VirusTotalPayloadError("La réponse VirusTotal n'est pas un objet JSON.")
    return payload


def _parse_file(payload: dict[str, Any]) -> VirusTotalFile:
    data_raw = payload.get("data")
    if (
        not isinstance(data_raw, dict)
        or not isinstance(data_raw.get("id"), str)
        or not isinstance(data_raw.get("type"), str)
    ):
        raise VirusTotalPayloadError("La réponse VirusTotal n'identifie pas un fichier.")
    data: dict[str, Any] = data_raw
    attrs_raw = data.get("attributes")
    attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
    stats = attrs.get("last_analysis_stats")
    if stats is not None and (
        not isinstance(stats, dict)
        or not all(isinstance(k, str) and isinstance(v, int) for k, v in stats.items())
    ):
        raise VirusTotalPayloadError("Les statistiques d'analyse VirusTotal sont invalides.")
    tags = attrs.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise VirusTotalPayloadError("Les tags VirusTotal sont invalides.")
    lookup = attrs.get("sha256") or attrs.get("sha1") or attrs.get("md5") or data["id"]
    if not isinstance(lookup, str):
        raise VirusTotalPayloadError("Le fichier VirusTotal n'a pas de valeur d'identification.")
    return VirusTotalFile(
        id=data["id"],
        type=data["type"],
        lookup_value=lookup,
        meaningful_name=attrs.get("meaningful_name")
        if isinstance(attrs.get("meaningful_name"), str)
        else None,
        type_description=attrs.get("type_description")
        if isinstance(attrs.get("type_description"), str)
        else None,
        size=attrs.get("size") if isinstance(attrs.get("size"), int) else None,
        last_analysis_stats=stats,
        first_submission_date=attrs.get("first_submission_date")
        if isinstance(attrs.get("first_submission_date"), int)
        else None,
        last_submission_date=attrs.get("last_submission_date")
        if isinstance(attrs.get("last_submission_date"), int)
        else None,
        last_modification_date=attrs.get("last_modification_date")
        if isinstance(attrs.get("last_modification_date"), int)
        else None,
        tags=tuple(tags),
        vhash=_string_value(attrs.get("vhash")),
        imphash=_string_value(attrs.get("imphash")),
        ssdeep=_string_value(attrs.get("ssdeep")),
        tlsh=_string_value(attrs.get("tlsh")),
        main_icon_dhash=_nested_string(attrs.get("main_icon"), "dhash"),
        rich_header_hash=_nested_string(attrs.get("pe_info"), "rich_header_hash"),
    )


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nested_string(value: object, key: str) -> str | None:
    return value.get(key) if isinstance(value, dict) and isinstance(value.get(key), str) else None


def _is_v2_file_report(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("response_code"), int) and "scans" in payload


def _parse_v2_file(payload: dict[str, Any], lookup: str) -> VirusTotalFile:
    if payload.get("response_code") != 1:
        raise VirusTotalPayloadError("Le rapport fichier v2 VirusTotal est introuvable.")
    values = {
        key: payload.get(key) for key in ("md5", "sha1", "sha256", "verbose_msg", "scan_date")
    }
    stats = {"malicious": payload.get("positives", 0), "total": payload.get("total", 0)}
    if not all(isinstance(stats[key], int) for key in stats):
        raise VirusTotalPayloadError("Les statistiques du rapport v2 sont invalides.")
    identifier = next(
        (values[key] for key in ("sha256", "sha1", "md5") if isinstance(values[key], str)), lookup
    )
    if not isinstance(identifier, str):
        raise VirusTotalPayloadError("Le rapport v2 n'a pas d'identifiant fichier.")
    return VirusTotalFile(
        id=identifier,
        type="file",
        lookup_value=identifier,
        meaningful_name=None,
        type_description=None,
        last_analysis_stats=stats,
    )


def _parse_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise VirusTotalPayloadError("La réponse paginée VirusTotal est incompatible.")
    return data


def _next_cursor(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("cursor"), str) and meta["cursor"]:
        return str(meta["cursor"])
    links = payload.get("links")
    if isinstance(links, dict) and isinstance(links.get("next"), str):
        # A next URL is not a cursor and must never be reconstructed or followed.
        return None
    return None


def _http_error(status: int, retry_after: float | None) -> VirusTotalHttpError:
    mapping = {
        401: ("virustotal_upstream_unauthorized", False),
        403: ("virustotal_upstream_forbidden", False),
        404: ("virustotal_not_found", False),
        407: ("virustotal_proxy_auth_required", False),
        429: ("virustotal_rate_limited", True),
    }
    code, retryable = mapping.get(status, ("virustotal_upstream_error", status >= 500))
    return VirusTotalHttpError(
        "VirusTotal ou le proxy a refusé la requête.",
        code=code,
        retryable=retryable,
        status_code=status,
        retry_after=retry_after,
    )


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _is_tls_error(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(3):
        if current is None:
            return False
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False
