"""HTTP client for the verified Retrohunt routes exposed by vtUI."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
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
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_RETRY_AFTER_SECONDS = 300.0
_VIRUSTOTAL_HOSTS = frozenset({"virustotal.com", "www.virustotal.com", "api.virustotal.com"})


class VirusTotalRetrohuntHttpClient(VirusTotalRetrohuntPort):
    """Call vtUI's local ``/api/tools/retrohunt`` routes through an injected client.

    The HTTP client and vtUI origin are supplied by composition. The client
    never adds a VirusTotal API key or constructs a direct VirusTotal route;
    vtUI owns its upstream authentication. ``allowed_hosts`` is an exact host
    allowlist for the vtUI origin, and the existing routing policy must grant
    only proxy transport for Retrohunt.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str | None,
        allowed_hosts: frozenset[str],
        capabilities: VirusTotalCapabilities,
        routing_policy: VirusTotalRoutingPolicy,
        fallback_base_url: str | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        max_result_pages: int = 100,
    ) -> None:
        if max_response_bytes < 1 or max_result_pages < 1:
            raise ValueError("response and page limits must be positive")
        self._client = client
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._base_url = (
            _validate_vtui_base_url(base_url, self._allowed_hosts) if base_url else None
        )
        self._fallback_base_url = (
            _validate_vtui_base_url(fallback_base_url, self._allowed_hosts)
            if fallback_base_url
            else None
        )
        self._capabilities = capabilities
        self._routing_policy = routing_policy
        self._max_response_bytes = max_response_bytes
        self._max_result_pages = max_result_pages

    @property
    def available(self) -> bool:
        return self.available_reason is None

    @property
    def available_reason(self) -> str | None:
        if not self._capabilities.retrohunt:
            return "La capability Retrohunt VirusTotal est désactivée."
        if self._base_url is None:
            return "L'origine vtUI n'est pas configurée."
        route = self._routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        if route is None:
            return "Aucune route Retrohunt explicite n'est configurée."
        if any(step.transport is not VirusTotalTransportKind.PROXY for step in route.steps()):
            return "La route Retrohunt contient un transport autre que le proxy."
        if any(
            step.variant
            not in {
                VirusTotalEndpointVariant.V3,
                VirusTotalEndpointVariant.V3_FALLBACK,
            }
            for step in route.steps()
        ):
            return "La route Retrohunt contient une variante proxy non prise en charge."
        if (
            any(step.variant is VirusTotalEndpointVariant.V3_FALLBACK for step in route.steps())
            and self._fallback_base_url is None
        ):
            return "La route de secours vtUI est déclarée sans origine de secours."
        return None

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        self._require_available()
        if (
            not request.rule_source
            or request.rule_source != request.rule_source.strip()
            or hashlib.sha256(request.rule_source.encode("utf-8")).hexdigest()
            != request.rule_source_sha256
        ):
            raise VirusTotalInvalidInputError(
                "La règle Retrohunt ne correspond pas exactement à son SHA-256 approuvé."
            )
        if request.corpus_id not in {"main", "goodware"}:
            raise VirusTotalInvalidInputError(
                "vtUI accepte uniquement les corpus Retrohunt main et goodware."
            )
        parameters = _submission_parameters(request.parameters)
        payload: dict[str, object] = {
            "rules": request.rule_source,
            "corpus": request.corpus_id,
            **parameters,
        }
        result = await self._request("POST", "/retrohunt/jobs", payload=payload)
        return _parse_job(result)

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        normalized_id = _require_job_id(job_id)
        result = await self._request(
            "POST", "/retrohunt/jobs/get", payload={"job_id": normalized_id}
        )
        return _parse_job(result, expected_job_id=normalized_id)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        normalized_id = _require_job_id(job_id)
        hits: list[RetrohuntHit] = []
        seen_hits: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(self._max_result_pages):
            request: dict[str, object] = {"job_id": normalized_id, "limit": 100}
            if cursor is not None:
                request["cursor"] = cursor
            result = await self._request("POST", "/retrohunt/jobs/matching-files", payload=request)
            page_hits, next_cursor = _parse_results_page(result)
            for hit in page_hits:
                if hit.sha256 in seen_hits:
                    raise VirusTotalPayloadError(
                        "vtUI a renvoyé un SHA-256 dupliqué dans les pages Retrohunt."
                    )
                seen_hits.add(hit.sha256)
                hits.append(hit)
            if next_cursor is None:
                return RetrohuntProviderResults(job_id=normalized_id, hits=tuple(hits))
            if next_cursor in seen_cursors:
                raise VirusTotalPayloadError("vtUI a répété un curseur Retrohunt.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise VirusTotalResponseTooLargeError(
            "Le nombre maximal de pages vtUI a été atteint avant la fin des résultats."
        )

    def _require_available(self) -> None:
        reason = self.available_reason
        if reason is not None:
            raise VirusTotalRouteUnavailableError(reason)

    async def _request(
        self,
        method: str,
        route: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_available()
        route_policy = self._routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        assert route_policy is not None
        for index, step in enumerate(route_policy.steps()):
            base_url = (
                self._fallback_base_url
                if step.variant is VirusTotalEndpointVariant.V3_FALLBACK
                else self._base_url
            )
            assert base_url is not None
            url = f"{base_url}/api/tools{route}"
            try:
                response_body = await self._exchange(method, url, payload=payload)
            except VirusTotalHttpError as exc:
                is_last = index == len(route_policy.steps()) - 1
                if is_last or not route_policy.permits_fallback(exc):
                    raise
                continue
            return _json_object(response_body)
        raise VirusTotalRouteUnavailableError("Aucune route vtUI Retrohunt utilisable.")

    async def _exchange(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, object] | None,
    ) -> bytes:
        try:
            async with self._client.stream(
                method,
                url,
                json=dict(payload) if payload is not None else None,
                headers={"accept": "application/json"},
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise VirusTotalUnexpectedRedirectError(
                        "vtUI a renvoyé une redirection inattendue.", status_code=status
                    )
                if status == 401:
                    raise VirusTotalNotAuthorizedError(
                        "vtUI n'a pas autorisé la requête Retrohunt.",
                        code="vtui_unauthorized",
                        status_code=status,
                    )
                if status == 403:
                    raise VirusTotalNotAuthorizedError(
                        "vtUI a interdit la requête Retrohunt.",
                        code="vtui_forbidden",
                        status_code=status,
                    )
                if status == 429:
                    raise VirusTotalQuotaExceededError(
                        "vtUI limite temporairement les requêtes Retrohunt.",
                        code="vtui_rate_limited",
                        retryable=True,
                        status_code=status,
                        retry_after=_retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 400:
                    raise VirusTotalHttpError(
                        "vtUI a renvoyé une réponse HTTP en erreur.",
                        code=f"vtui_http_{status}",
                        retryable=status >= 500,
                        status_code=status,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise VirusTotalResponseTooLargeError(
                            "La réponse vtUI dépasse la taille autorisée."
                        )
                return bytes(body)
        except (
            VirusTotalHttpError,
            VirusTotalUnexpectedRedirectError,
            VirusTotalResponseTooLargeError,
        ):
            raise
        except httpx.TimeoutException:
            raise VirusTotalTransportError(
                "Le transport vtUI a expiré.", code="vtui_timeout", retryable=True
            ) from None
        except httpx.HTTPError:
            raise VirusTotalTransportError(
                "Le transport vtUI a échoué.", code="vtui_transport_error", retryable=True
            ) from None


def _validate_vtui_base_url(value: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.hostname.lower() in _VIRUSTOTAL_HOSTS
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VirusTotalRouteUnavailableError(
            "L'origine vtUI doit être HTTP(S), sans identifiants, et sur la liste "
            "d'hôtes autorisés."
        )
    return value.rstrip("/")


def _submission_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    supported = {"start", "end", "notification_email"}
    if not isinstance(parameters, Mapping) or set(parameters) - supported:
        raise VirusTotalInvalidInputError(
            "Les paramètres de soumission dépassent le contrat vtUI vérifié."
        )
    result: dict[str, object] = {}
    for key in ("start", "end"):
        value = parameters.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VirusTotalInvalidInputError(
                f"Le paramètre Retrohunt {key} doit être un timestamp positif."
            )
        result[key] = value
    email = parameters.get("notification_email")
    if email is not None:
        if not isinstance(email, str) or len(email) > 320 or not email.strip():
            raise VirusTotalInvalidInputError(
                "Le paramètre notification_email n'est pas valide pour vtUI."
            )
        result["notification_email"] = email.strip()
    return result


def _require_job_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise VirusTotalInvalidInputError("Un identifiant de job vtUI valide est requis.")
    return value.strip()


def _parse_job(
    payload: Mapping[str, object], *, expected_job_id: str | None = None
) -> RetrohuntProviderJob:
    data = payload.get("data")
    if not isinstance(data, dict) or (
        data.get("type") is not None and data.get("type") != "retrohunt_job"
    ):
        raise VirusTotalPayloadError("La réponse vtUI n'identifie pas un job Retrohunt.")
    job_id = data.get("id")
    attributes = data.get("attributes")
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(attributes, dict):
        raise VirusTotalPayloadError("La réponse vtUI du job Retrohunt est partielle.")
    status_value = attributes.get("status") or attributes.get("state")
    if not isinstance(status_value, str) or not status_value.strip():
        raise VirusTotalPayloadError("La réponse vtUI n'inclut pas le statut du job.")
    if expected_job_id is not None and job_id != expected_job_id:
        raise VirusTotalPayloadError("vtUI a renvoyé un autre identifiant de job.")
    state = _job_state(status_value)
    return RetrohuntProviderJob(job_id=job_id, state=state)


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
        raise VirusTotalPayloadError("La réponse vtUI des fichiers matchés est partielle.")
    hits: list[RetrohuntHit] = []
    for row in rows:
        if not isinstance(row, dict):
            raise VirusTotalPayloadError("La réponse vtUI contient un résultat mal formé.")
        if row.get("type") is not None and row.get("type") != "file":
            raise VirusTotalPayloadError("Un résultat vtUI n'est pas un fichier matché.")
        attributes = row.get("attributes")
        sha256 = attributes.get("sha256") if isinstance(attributes, dict) else None
        if not isinstance(sha256, str):
            sha256 = row.get("id")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256.lower()) is None:
            raise VirusTotalPayloadError("Un résultat vtUI ne contient pas de SHA-256 complet.")
        hits.append(RetrohuntHit(sha256=sha256.lower()))
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise VirusTotalPayloadError("Les métadonnées de pagination vtUI sont mal formées.")
    cursor = meta.get("cursor") if isinstance(meta, dict) else None
    if cursor is not None and not isinstance(cursor, str):
        raise VirusTotalPayloadError("Le curseur de pagination vtUI est mal formé.")
    if isinstance(cursor, str):
        cursor = cursor.strip() or None
    return tuple(hits), cursor


def _json_object(body: bytes) -> dict[str, object]:
    if not body:
        raise VirusTotalPayloadError("vtUI a renvoyé un corps JSON vide.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VirusTotalJsonError("vtUI a renvoyé un JSON invalide.") from None
    if not isinstance(payload, dict):
        raise VirusTotalPayloadError("La réponse vtUI n'est pas un objet JSON.")
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
