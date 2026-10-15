"""Offline provider contract probes and an approval-gated live-smoke contract.

The offline runner owns its MockTransport instances, so it has no route to a
socket. Its PASS values describe parser/contract fixtures only; ``mode`` keeps
that fact attached to every result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from cti_app.application.shodan import ShodanCapabilities, ShodanError
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.domain.investigation import TLPLevel
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalFileRelationship,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.shodan import ShodanHttpAdapter
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter

logger = logging.getLogger(__name__)

_FILE_SHA256 = "a" * 64
_SHODAN_IP = "203.0.113.10"
_VT_BASE_URL = "https://www.virustotal.com/api/v3"
_SHODAN_BASE_URL = "https://api.shodan.io"
_SMOKE_VT_CAPABILITIES = {
    "file_report": VirusTotalCapability.FILE_REPORT,
    "file_relationships": VirusTotalCapability.FILE_RELATIONSHIPS,
    "intelligence_search": VirusTotalCapability.INTELLIGENCE_SEARCH,
    "behaviour": VirusTotalCapability.BEHAVIOUR_REPORTS,
}


class ProviderProbeMode(StrEnum):
    OFFLINE_SIMULATED = "offline_simulated"
    LIVE_SMOKE = "live_smoke"


class ProviderProbeStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VALIDATED = "NOT_VALIDATED"


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    provider: str
    operation: str
    mode: ProviderProbeMode
    status: ProviderProbeStatus
    endpoint: str
    duration_ms: float
    http_status: int | None
    summary: str
    error_code: str | None = None
    response_preview: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.operation.strip() or not self.endpoint.strip():
            raise ValueError("provider probe identity fields must be non-empty")
        if self.duration_ms < 0:
            raise ValueError("provider probe duration cannot be negative")
        object.__setattr__(self, "mode", ProviderProbeMode(self.mode))
        object.__setattr__(self, "status", ProviderProbeStatus(self.status))


@dataclass(frozen=True, slots=True)
class ProviderPreflightReport:
    mode: ProviderProbeMode
    probes: tuple[ProviderProbeResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ProviderProbeMode(self.mode))
        if any(probe.mode is not self.mode for probe in self.probes):
            raise ValueError("all probes must use the report's mode")

    @property
    def passed(self) -> bool:
        return bool(self.probes) and all(
            probe.status is ProviderProbeStatus.PASS for probe in self.probes
        )


async def offline_preflight() -> ProviderPreflightReport:
    """Exercise VT read routes and Shodan passive routes with local fixtures only."""
    vt_paths = {
        f"/api/v3/files/{_FILE_SHA256}": {
            "data": {
                "id": _FILE_SHA256,
                "type": "file",
                "attributes": {
                    "sha256": _FILE_SHA256,
                    "last_analysis_stats": {"malicious": 1, "undetected": 2},
                },
            }
        },
        f"/api/v3/files/{_FILE_SHA256}/contacted_ips": {
            "data": [{"id": _SHODAN_IP, "type": "ip_address"}],
            "meta": {"count": 1},
        },
        "/api/v3/intelligence/search": {
            "data": [{"id": _FILE_SHA256, "type": "file"}],
            "meta": {"count": 1},
        },
        f"/api/v3/files/{_FILE_SHA256}/behaviours": {
            "data": [{"id": "sandbox-report-1", "type": "file_behaviour"}],
            "meta": {"count": 1},
        },
    }

    async def vt_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host not in {"www.virustotal.com", "virustotal.com"}:
            raise AssertionError("offline VT transport received an unexpected host")
        payload = vt_paths.get(request.url.path)
        if payload is None:
            raise AssertionError(
                f"offline VT transport received an unexpected path: {request.url.path}"
            )
        if request.url.path.endswith("/intelligence/search") and (
            request.url.params.get("query") != "type:file"
        ):
            raise AssertionError("offline intelligence query did not match the probe contract")
        return httpx.Response(200, json=payload, request=request)

    shodan_host = {
        "ip_str": _SHODAN_IP,
        "org": "Offline fixture",
        "ports": [443],
        "data": [{"port": 443, "transport": "tcp", "data": "fixture banner"}],
    }

    async def shodan_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.shodan.io":
            raise AssertionError("offline Shodan transport received an unexpected host")
        if request.url.params.get("key") != "offline-preflight-key":
            raise AssertionError("offline Shodan key was not isolated in the mock transport")
        if request.url.path == f"/shodan/host/{_SHODAN_IP}":
            payload: Mapping[str, object] = shodan_host
        elif request.url.path == "/shodan/host/search":
            if request.url.params.get("query") != "port:443 product:fixture":
                raise AssertionError("offline Shodan query did not match the probe contract")
            payload = {"total": 1, "matches": [shodan_host]}
        elif request.url.path == "/api-info":
            payload = {"query_credits": 1, "plan": "offline_simulated"}
        else:
            raise AssertionError(
                f"offline Shodan transport received an unexpected path: {request.url.path}"
            )
        return httpx.Response(200, json=payload, request=request)

    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    vt_routes = {
        capability: route
        for capability in (
            VirusTotalCapability.FILE_REPORT,
            VirusTotalCapability.FILE_RELATIONSHIPS,
            VirusTotalCapability.INTELLIGENCE_SEARCH,
            VirusTotalCapability.BEHAVIOUR_REPORTS,
        )
    }
    probes: list[ProviderProbeResult] = []
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(vt_handler),
            follow_redirects=False,
            trust_env=False,
        ) as vt_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(shodan_handler),
            follow_redirects=False,
            trust_env=False,
        ) as shodan_client,
    ):
        vt = VirusTotalHttpAdapter(
            client=vt_client,
            base_url=_VT_BASE_URL,
            capabilities=VirusTotalCapabilities(
                file_report=True,
                file_relationships=True,
                intelligence_search=True,
                behaviour_reports=True,
            ),
            routing_policy=VirusTotalRoutingPolicy(routes=vt_routes),
        )
        shodan = ShodanHttpAdapter(
            client=shodan_client,
            base_url=_SHODAN_BASE_URL,
            capabilities=ShodanCapabilities(host=True, search=True),
            api_key=SecretStr("offline-preflight-key"),
        )
        probes.append(
            await _run_probe(
                provider="virustotal",
                operation="file_report",
                endpoint=f"GET {_VT_BASE_URL}/files/{_FILE_SHA256}",
                call=lambda: vt.file_report(_FILE_SHA256),
                validate=lambda result: result.file.id == _FILE_SHA256,
                status=lambda result: result.http_status,
            )
        )
        probes.append(
            await _run_probe(
                provider="virustotal",
                operation="file_relationships",
                endpoint=f"GET {_VT_BASE_URL}/files/{_FILE_SHA256}/contacted_ips",
                call=lambda: vt.file_relationship(
                    _FILE_SHA256, VirusTotalFileRelationship.CONTACTED_IPS, limit=10
                ),
                validate=lambda result: bool(result.items),
                status=lambda result: result.http_statuses[0] if result.http_statuses else None,
            )
        )
        probes.append(
            await _run_probe(
                provider="virustotal",
                operation="intelligence_search",
                endpoint=f"GET {_VT_BASE_URL}/intelligence/search",
                call=lambda: vt.intelligence_search("type:file", limit=10),
                validate=lambda result: bool(result.items),
                status=lambda result: result.http_statuses[0] if result.http_statuses else None,
            )
        )
        probes.append(
            await _run_probe(
                provider="virustotal",
                operation="behaviour",
                endpoint=f"GET {_VT_BASE_URL}/files/{_FILE_SHA256}/behaviours",
                call=lambda: vt.behaviours(_FILE_SHA256, limit=10),
                validate=lambda result: bool(result.items),
                status=lambda result: result.http_statuses[0] if result.http_statuses else None,
            )
        )
        probes.append(
            await _run_probe(
                provider="shodan",
                operation="host",
                endpoint=f"GET {_SHODAN_BASE_URL}/shodan/host/{_SHODAN_IP}",
                call=lambda: shodan.host(_SHODAN_IP),
                validate=lambda result: result.http_status == 200 and bool(result.services),
                status=lambda result: result.http_status,
            )
        )
        probes.append(
            await _run_probe(
                provider="shodan",
                operation="search",
                endpoint=f"GET {_SHODAN_BASE_URL}/shodan/host/search",
                call=lambda: shodan.search("port:443 product:fixture"),
                validate=lambda result: result.http_status == 200 and bool(result.matches),
                status=lambda result: result.http_status,
            )
        )
    return ProviderPreflightReport(
        mode=ProviderProbeMode.OFFLINE_SIMULATED,
        probes=tuple(probes),
    )


async def _run_probe[ProbeValue](
    *,
    provider: str,
    operation: str,
    endpoint: str,
    call: Callable[[], Awaitable[ProbeValue]],
    validate: Callable[[ProbeValue], bool],
    status: Callable[[ProbeValue], int | None],
) -> ProviderProbeResult:
    started = time.perf_counter()
    try:
        result = await call()
    except (VirusTotalError, ShodanError) as exc:
        http_status = getattr(exc, "status_code", None)
        probe_status = (
            ProviderProbeStatus.NOT_VALIDATED if http_status == 404 else ProviderProbeStatus.FAIL
        )
        return ProviderProbeResult(
            provider=provider,
            operation=operation,
            mode=ProviderProbeMode.OFFLINE_SIMULATED,
            status=probe_status,
            endpoint=endpoint,
            duration_ms=(time.perf_counter() - started) * 1_000,
            http_status=http_status,
            summary="No provider conclusion was produced.",
            error_code=getattr(exc, "code", "provider_error"),
        )
    elapsed_ms = (time.perf_counter() - started) * 1_000
    try:
        is_complete = bool(validate(result))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        is_complete = False
    observed_status = status(result)
    if observed_status == 404:
        result_status = ProviderProbeStatus.NOT_VALIDATED
        code = "provider_not_found"
        summary = "Provider returned 404; no negative CTI observation was produced."
    elif not is_complete:
        result_status = ProviderProbeStatus.FAIL
        code = "provider_response_partial"
        summary = "The provider response did not satisfy the probe's minimum shape."
    else:
        result_status = ProviderProbeStatus.PASS
        code = None
        summary = "Simulated response matched the adapter contract."
    return ProviderProbeResult(
        provider=provider,
        operation=operation,
        mode=ProviderProbeMode.OFFLINE_SIMULATED,
        status=result_status,
        endpoint=endpoint,
        duration_ms=elapsed_ms,
        http_status=observed_status,
        summary=summary,
        error_code=code,
        response_preview=(
            "HTTP 404 response omitted from evidence." if observed_status == 404 else None
        ),
    )


@dataclass(frozen=True, slots=True)
class LiveSmokeEndpoint:
    provider: str
    operation: str
    method: str
    endpoint: str
    submitted_data: Mapping[str, str]
    effect: str
    expected_cost: str

    def __post_init__(self) -> None:
        bridge_turn = (
            self.method.upper() == "POST"
            and self.provider.lower() == "bridge"
            and self.operation.lower() == "turn"
        )
        if self.method.upper() != "GET" and not bridge_turn:
            raise ValueError(
                "live smoke endpoints may only use passive GET requests "
                "(plus the synthetic Bridge turn)"
            )
        if not all(
            value.strip()
            for value in (
                self.provider,
                self.operation,
                self.endpoint,
                self.effect,
                self.expected_cost,
            )
        ):
            raise ValueError("live smoke endpoint fields must be non-empty")
        operation = self.operation.lower().replace("-", "_")
        if operation in {"retrohunt_submit", "file_download", "active_scan"}:
            raise ValueError("live smoke cannot submit Retrohunt, download samples, or scan")


@dataclass(frozen=True, slots=True)
class LiveSmokePlan:
    endpoints: tuple[LiveSmokeEndpoint, ...]
    tlp: TLPLevel
    operator_effect: str
    expected_total_cost: str
    vt_routing_policy: VirusTotalRoutingPolicy

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("a live smoke plan must list at least one endpoint")
        identities = {(item.provider.lower(), item.operation.lower()) for item in self.endpoints}
        if len(identities) != len(self.endpoints):
            raise ValueError("a live smoke plan cannot repeat an endpoint operation")
        if not self.operator_effect.strip() or not self.expected_total_cost.strip():
            raise ValueError("a live smoke plan must state effects and expected cost")
        object.__setattr__(self, "tlp", TLPLevel(self.tlp))
        for endpoint in self.endpoints:
            if endpoint.provider.lower() != "virustotal":
                continue
            capability = _SMOKE_VT_CAPABILITIES.get(endpoint.operation.lower())
            if capability is None:
                raise ValueError("live smoke names an unsupported VirusTotal operation")
            route = self.vt_routing_policy.route_for(capability)
            if route is None or any(
                step.transport is not VirusTotalTransportKind.PROXY for step in route.steps()
            ):
                raise ValueError(
                    "live smoke requires an explicit proxy-only VirusTotal route per operation"
                )

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            {
                "endpoints": [
                    {
                        "provider": item.provider,
                        "operation": item.operation,
                        "method": item.method.upper(),
                        "endpoint": item.endpoint,
                        "submitted_data": dict(item.submitted_data),
                        "effect": item.effect,
                        "expected_cost": item.expected_cost,
                    }
                    for item in self.endpoints
                ],
                "tlp": self.tlp.value,
                "operator_effect": self.operator_effect,
                "expected_total_cost": self.expected_total_cost,
                "vt_routing_policy": {
                    capability.value: [
                        {"transport": step.transport.value, "variant": step.variant.value}
                        for step in route.steps()
                    ]
                    for capability, route in sorted(
                        self.vt_routing_policy.routes.items(), key=lambda item: item[0].value
                    )
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveSmokeApproval:
    operator_id: str
    approved_at: datetime
    plan_sha256: str

    def __post_init__(self) -> None:
        if not self.operator_id.strip() or not _is_aware(self.approved_at):
            raise ValueError("live smoke approval requires an operator and aware timestamp")
        if len(self.plan_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.plan_sha256
        ):
            raise ValueError("live smoke approval must bind to a plan SHA-256")


class LiveSmokeApprovalRequiredError(PermissionError):
    pass


class LiveSmokePlanExecutor(Protocol):
    async def execute(self, plan: LiveSmokePlan) -> Sequence[ProviderProbeResult]: ...


async def run_live_smoke(
    plan: LiveSmokePlan,
    *,
    operator_approval: LiveSmokeApproval | None,
    executor: LiveSmokePlanExecutor,
) -> tuple[ProviderProbeResult, ...]:
    """Run a caller-supplied live plan only after approval of that exact plan."""
    if operator_approval is None:
        raise LiveSmokeApprovalRequiredError("Live smoke requires explicit operator approval.")
    if operator_approval.plan_sha256 != plan.sha256:
        raise LiveSmokeApprovalRequiredError("Operator approval does not match this smoke plan.")
    results = tuple(await executor.execute(plan))
    expected = {(item.provider.lower(), item.operation.lower()) for item in plan.endpoints}
    actual = {(item.provider.lower(), item.operation.lower()) for item in results}
    if actual != expected or len(actual) != len(results):
        raise ValueError(
            "live smoke executor did not return exactly one result per planned operation"
        )
    for result in results:
        if result.mode is not ProviderProbeMode.LIVE_SMOKE:
            raise ValueError("live smoke executor returned a non-live result")
        if result.response_preview is None:
            raise ValueError("live smoke executor must provide a response for redacted logging")
        logger.info(
            "provider_live_smoke endpoint=%s status=%s duration_ms=%.3f response=%s",
            _redact_endpoint(result.endpoint),
            result.http_status,
            result.duration_ms,
            _redact_text(result.response_preview or result.summary, limit=2_000),
        )
    return results


def _redact_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.netloc:
        return endpoint[:1_000]
    safe_netloc = parts.netloc.rsplit("@", 1)[-1]
    if "@" in parts.netloc:
        safe_netloc = f"[REDACTED]@{safe_netloc}"
    safe_query = [
        (key, "[REDACTED]" if _is_sensitive_field(key) else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, safe_netloc, parts.path, urlencode(safe_query), ""))[:1_000]


def _redact_text(value: str, *, limit: int) -> str:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        redacted = _redact_unstructured(value)
    else:
        redacted = json.dumps(_redact_json(payload), ensure_ascii=False, separators=(",", ":"))
    return redacted[:limit]


def _redact_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_field(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_unstructured(value)
    return value


def _is_sensitive_field(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return normalized in {"key", "url"} or any(
        marker in normalized
        for marker in (
            "apikey",
            "authorization",
            "password",
            "secret",
            "token",
            "credential",
            "cookie",
            "signature",
            "signedurl",
        )
    )


def _redact_unstructured(value: str) -> str:
    return re.sub(
        r"(?i)(api[_-]?key|x-apikey|authorization|token|password|key|"
        r"credential|cookie|signature|x-amz-signature)(\s*[=:]\s*)[^&;,\s\"}]+",
        r"\1\2[REDACTED]",
        value,
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
