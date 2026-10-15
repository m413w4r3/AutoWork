"""HTTP client for the restricted VirusTotal v3 Retrohunt API routes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalJsonError,
    VirusTotalNotAuthorizedError,
    VirusTotalPayloadError,
    VirusTotalQuotaExceededError,
    VirusTotalResponseTooLargeError,
    VirusTotalRetrohuntPort,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
    VirusTotalTransportError,
    VirusTotalUnexpectedRedirectError,
)
from cti_app.domain.retrohunt import (
    RetrohuntHit,
    RetrohuntProviderJob,
    RetrohuntProviderJobState,
    RetrohuntProviderResults,
    RetrohuntSubmitRequest,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_MAX_RETRY_AFTER_SECONDS = 300.0
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_RESULT_PAGES = 20
_DEFAULT_MAX_RESULTS = 1000
_VIRUSTOTAL_HOSTS = frozenset({"virustotal.com", "www.virustotal.com", "api.virustotal.com"})
_ACTIVE_VT_API_KEY: ContextVar[str | None] = ContextVar("active_vt_api_key", default=None)


class _VirusTotalHttpLogRedactor(logging.Filter):
    """Redact the active API key from HTTPX/HTTPCore debug messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        secret = _ACTIVE_VT_API_KEY.get()
        if secret:
            message = record.getMessage()
            if secret in message:
                record.msg = message.replace(secret, "[REDACTED]")
                record.args = ()
        return True


_HTTP_LOGGERS = (
    "httpx",
    "httpx._client",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
)
for _logger_name in _HTTP_LOGGERS:
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(item, _VirusTotalHttpLogRedactor) for item in _logger.filters):
        _logger.addFilter(_VirusTotalHttpLogRedactor())


class VirusTotalRetrohuntHttpClient(VirusTotalRetrohuntPort):
    """Call only the v3 Retrohunt job, status, and matching-file endpoints.

    The supplied HTTPX client is lifecycle-owned by the API/worker composition
    root and is already bound to the configured outbound HTTP(S) proxy. The
    API key is sent as ``x-apikey`` on these three routes only.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str | None,
        api_key: str | SecretStr | None,
        proxy_url: str | None,
        capabilities: VirusTotalCapabilities,
        routing_policy: VirusTotalRoutingPolicy,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_result_pages: int = _DEFAULT_MAX_RESULT_PAGES,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> None:
        if min(max_response_bytes, max_result_pages, max_results) < 1:
            raise ValueError("response, result, and page limits must be positive")
        self._client = client
        self._base_url = _validate_base_url(base_url) if base_url else None
        self._api_key = _as_secret(api_key)
        self._proxy_configured = bool(proxy_url and proxy_url.strip())
        self._capabilities = capabilities
        self._routing_policy = routing_policy
        self._max_response_bytes = max_response_bytes
        self._max_result_pages = max_result_pages
        self._max_results = max_results

    @property
    def available(self) -> bool:
        return self.available_reason is None

    @property
    def available_reason(self) -> str | None:
        if not self._capabilities.retrohunt:
            return "La capacité Retrohunt VirusTotal est désactivée."
        if self._base_url is None:
            return "L'URL de l'API VirusTotal v3 n'est pas configurée."
        if self._api_key is None:
            return "La clé API VirusTotal n'est pas configurée."
        if not self._proxy_configured:
            return "Le proxy VirusTotal n'est pas configuré."
        route = self._routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        if route is None:
            return "Aucune route Retrohunt explicite n'est configurée."
        if any(step.transport is not VirusTotalTransportKind.PROXY for step in route.steps()):
            return "La route Retrohunt doit utiliser le proxy HTTP(S)."
        if any(step.variant is not VirusTotalEndpointVariant.V3 for step in route.steps()):
            return "La route Retrohunt contient une variante API non prise en charge."
        if len(route.steps()) != 1:
            return "La route Retrohunt doit cibler une seule origine API v3."
        return None

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        self._require_available()
        if (
            not request.rule_source
            or request.rule_source != request.rule_source.strip()
            or not _is_sha256(request.rule_source_sha256)
            or _sha256(request.rule_source) != request.rule_source_sha256
        ):
            raise VirusTotalInvalidInputError(
                "La règle Retrohunt ne correspond pas exactement à son SHA-256 approuvé."
            )
        if request.corpus_id not in {"main", "goodware"}:
            raise VirusTotalInvalidInputError("Le corpus Retrohunt doit être main ou goodware.")
        parameters = _submission_parameters(request.parameters)
        attributes: dict[str, object] = {
            "rules": request.rule_source,
            "corpus": request.corpus_id,
            **parameters,
        }
        result = await self._request(
            "POST",
            "/intelligence/retrohunt_jobs",
            payload={"data": {"type": "retrohunt_job", "attributes": attributes}},
        )
        return _parse_job(result)

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        normalized_id = self._validated_job_id(job_id)
        result = await self._request(
            "GET", f"/intelligence/retrohunt_jobs/{quote(normalized_id, safe='')}"
        )
        return _parse_job(result, expected_job_id=normalized_id)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        normalized_id = self._validated_job_id(job_id)
        route = f"/intelligence/retrohunt_jobs/{quote(normalized_id, safe='')}/matching_files"
        hits: list[RetrohuntHit] = []
        seen_hits: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(self._max_result_pages):
            params: dict[str, str | int] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("GET", route, params=params)
            page_hits, next_cursor = _parse_results_page(result)
            for hit in page_hits:
                if hit.sha256 in seen_hits:
                    raise VirusTotalPayloadError(
                        "VirusTotal a renvoyé un SHA-256 dupliqué dans les pages Retrohunt."
                    )
                if len(hits) >= self._max_results:
                    raise VirusTotalResponseTooLargeError(
                        "Le nombre maximal de résultats Retrohunt a été atteint."
                    )
                seen_hits.add(hit.sha256)
                hits.append(hit)
            if next_cursor is None:
                return RetrohuntProviderResults(job_id=normalized_id, hits=tuple(hits))
            assert self._api_key is not None
            if self._api_key.get_secret_value() in next_cursor:
                raise VirusTotalPayloadError("Le curseur Retrohunt contient une valeur refusée.")
            if next_cursor in seen_cursors:
                raise VirusTotalPayloadError("VirusTotal a répété un curseur Retrohunt.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise VirusTotalResponseTooLargeError(
            "Le nombre maximal de pages Retrohunt a été atteint avant la fin des résultats."
        )

    def _require_available(self) -> None:
        reason = self.available_reason
        if reason is not None:
            raise VirusTotalRouteUnavailableError(reason)

    def _validated_job_id(self, value: str) -> str:
        normalized = _require_job_id(value)
        if self._api_key is not None and self._api_key.get_secret_value() in normalized:
            raise VirusTotalInvalidInputError("L'identifiant de job Retrohunt est invalide.")
        return normalized

    async def _request(
        self,
        method: str,
        route: str,
        *,
        payload: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, object]:
        self._require_available()
        assert self._base_url is not None and self._api_key is not None
        url = f"{self._base_url}{route}"
        key = self._api_key.get_secret_value()
        active_key = _ACTIVE_VT_API_KEY.set(key)
        try:
            body = await self._exchange(
                method,
                url,
                api_key=key,
                payload=payload,
                params=params,
            )
        finally:
            _ACTIVE_VT_API_KEY.reset(active_key)
        return _json_object(body)

    async def _exchange(
        self,
        method: str,
        url: str,
        *,
        api_key: str,
        payload: Mapping[str, object] | None,
        params: Mapping[str, str | int] | None,
    ) -> bytes:
        try:
            async with self._client.stream(
                method,
                url,
                json=dict(payload) if payload is not None else None,
                params=dict(params) if params is not None else None,
                headers={"accept": "application/json", "x-apikey": api_key},
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise VirusTotalUnexpectedRedirectError(
                        "VirusTotal a renvoyé une redirection inattendue.", status_code=status
                    )
                if status == 401:
                    raise VirusTotalNotAuthorizedError(
                        "VirusTotal n'a pas autorisé la requête Retrohunt.",
                        code="vt_unauthorized",
                        status_code=status,
                    )
                if status == 403:
                    raise VirusTotalNotAuthorizedError(
                        "VirusTotal a interdit la requête Retrohunt.",
                        code="vt_forbidden",
                        status_code=status,
                    )
                if status == 429:
                    raise VirusTotalQuotaExceededError(
                        "VirusTotal limite temporairement les requêtes Retrohunt.",
                        code="vt_rate_limited",
                        retryable=True,
                        status_code=status,
                        retry_after=_retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 400:
                    raise VirusTotalHttpError(
                        "VirusTotal a renvoyé une réponse HTTP en erreur.",
                        code=f"vt_http_{status}",
                        retryable=status >= 500,
                        status_code=status,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise VirusTotalResponseTooLargeError(
                            "La réponse VirusTotal dépasse la taille autorisée."
                        )
                return bytes(body)
        except (VirusTotalError, VirusTotalUnexpectedRedirectError):
            raise
        except httpx.TimeoutException:
            raise VirusTotalTransportError(
                "Le transport VirusTotal a expiré.", code="vt_timeout", retryable=True
            ) from None
        except httpx.HTTPError:
            raise VirusTotalTransportError(
                "Le transport VirusTotal a échoué.",
                code="vt_transport_error",
                retryable=True,
            ) from None


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in _VIRUSTOTAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/api/v3"
        or parsed.query
        or parsed.fragment
    ):
        raise VirusTotalRouteUnavailableError(
            "L'URL VirusTotal doit cibler l'API v3 officielle en HTTPS."
        )
    return value.strip().rstrip("/")


def _as_secret(value: str | SecretStr | None) -> SecretStr | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value if value.get_secret_value() else None
    return SecretStr(value) if value else None


def _submission_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    supported = {"start", "end", "notification_email"}
    if not isinstance(parameters, Mapping) or set(parameters) - supported:
        raise VirusTotalInvalidInputError(
            "Les paramètres de soumission dépassent le contrat Retrohunt autorisé."
        )
    result: dict[str, object] = {}
    for source, target in (("start", "start_date"), ("end", "end_date")):
        value = parameters.get(source)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VirusTotalInvalidInputError(
                f"Le paramètre Retrohunt {source} doit être un timestamp positif."
            )
        try:
            result[target] = datetime.fromtimestamp(value, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            raise VirusTotalInvalidInputError(
                f"Le paramètre Retrohunt {source} dépasse la plage supportée."
            ) from None
    email = parameters.get("notification_email")
    if email is not None:
        if not isinstance(email, str) or len(email) > 320 or not email.strip():
            raise VirusTotalInvalidInputError(
                "Le paramètre notification_email n'est pas valide pour Retrohunt."
            )
        result["notification_email"] = email.strip()
    return result


def _require_job_id(value: str) -> str:
    if not isinstance(value, str) or _JOB_ID.fullmatch(value.strip()) is None:
        raise VirusTotalInvalidInputError("Un identifiant de job Retrohunt valide est requis.")
    return value.strip()


def _parse_job(
    payload: Mapping[str, object], *, expected_job_id: str | None = None
) -> RetrohuntProviderJob:
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("type") != "retrohunt_job":
        raise VirusTotalPayloadError("La réponse VirusTotal n'identifie pas un job Retrohunt.")
    job_id = data.get("id")
    attributes = data.get("attributes")
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(attributes, dict):
        raise VirusTotalPayloadError("La réponse VirusTotal du job Retrohunt est partielle.")
    status_value = attributes.get("status") or attributes.get("state")
    if not isinstance(status_value, str) or not status_value.strip():
        raise VirusTotalPayloadError("La réponse VirusTotal n'inclut pas le statut du job.")
    if expected_job_id is not None and job_id != expected_job_id:
        raise VirusTotalPayloadError("VirusTotal a renvoyé un autre identifiant de job.")
    return RetrohuntProviderJob(job_id=job_id, state=_job_state(status_value))


def _job_state(value: str) -> RetrohuntProviderJobState:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "pending": RetrohuntProviderJobState.QUEUED,
        "queued": RetrohuntProviderJobState.QUEUED,
        "running": RetrohuntProviderJobState.RUNNING,
        "in_progress": RetrohuntProviderJobState.RUNNING,
        "processing": RetrohuntProviderJobState.RUNNING,
        "completed": RetrohuntProviderJobState.COMPLETED,
        "finished": RetrohuntProviderJobState.COMPLETED,
        "failed": RetrohuntProviderJobState.FAILED,
        "error": RetrohuntProviderJobState.FAILED,
    }
    return aliases.get(normalized, RetrohuntProviderJobState.UNKNOWN)


def _parse_results_page(
    payload: Mapping[str, object],
) -> tuple[tuple[RetrohuntHit, ...], str | None]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise VirusTotalPayloadError("La réponse VirusTotal des fichiers matchés est partielle.")
    hits: list[RetrohuntHit] = []
    for row in rows:
        if not isinstance(row, dict):
            raise VirusTotalPayloadError("La réponse VirusTotal contient un résultat mal formé.")
        if row.get("type") is not None and row.get("type") != "file":
            raise VirusTotalPayloadError("Un résultat Retrohunt n'est pas un fichier.")
        attributes = row.get("attributes")
        sha256 = attributes.get("sha256") if isinstance(attributes, dict) else None
        if not isinstance(sha256, str):
            sha256 = row.get("id")
        if not isinstance(sha256, str) or not _is_sha256(sha256):
            raise VirusTotalPayloadError(
                "Un résultat Retrohunt ne contient pas de SHA-256 complet."
            )
        hits.append(RetrohuntHit(sha256=sha256.lower()))
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise VirusTotalPayloadError("Les métadonnées de pagination Retrohunt sont mal formées.")
    cursor = meta.get("cursor") if isinstance(meta, dict) else None
    if cursor is not None and (
        not isinstance(cursor, str)
        or len(cursor) > 1024
        or any(ord(character) < 32 for character in cursor)
    ):
        raise VirusTotalPayloadError("Le curseur de pagination Retrohunt est mal formé.")
    if isinstance(cursor, str):
        cursor = cursor.strip() or None
    return tuple(hits), cursor


def _json_object(body: bytes) -> dict[str, object]:
    if not body:
        raise VirusTotalPayloadError("VirusTotal a renvoyé un corps JSON vide.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VirusTotalJsonError("VirusTotal a renvoyé un JSON invalide.") from None
    if not isinstance(payload, dict):
        raise VirusTotalPayloadError("La réponse VirusTotal n'est pas un objet JSON.")
    return payload


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return _SHA256.fullmatch(value.lower()) is not None
