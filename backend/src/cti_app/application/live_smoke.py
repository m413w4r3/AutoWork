"""Operator-approved live smoke of the external providers AutoWork depends on.

The plan lists every request and every byte of submitted data; the operator
approves its exact SHA-256 (``run_live_smoke``). Nothing here submits Retrohunt,
downloads a sample or scans: VirusTotal and Shodan are read-only GET calls on
public, non-sensitive subjects, and the Bridge receives one synthetic prompt.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from cti_app.application.provider_probes import (
    LiveSmokeEndpoint,
    LiveSmokePlan,
    ProviderProbeMode,
    ProviderProbeResult,
    ProviderProbeStatus,
    _redact_json,
    _redact_text,
)
from cti_app.application.shodan import ShodanError
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.investigation import TLPLevel
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalFileRelationship,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.shodan import create_shodan_http_adapter, create_shodan_http_client
from cti_app.infrastructure.virustotal import (
    VirusTotalHttpAdapter,
    create_virustotal_direct_http_client,
    create_virustotal_http_adapter,
    create_virustotal_http_client,
)
from cti_app.integrations.model_factory import create_bridge_capabilities_provider
from cti_app.integrations.models import ChatGPTBridgeClient, _responses_output_text

# The public EICAR test file and a public DNS resolver: neither is sensitive.
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"
SHODAN_HOST_IP = "8.8.8.8"
SHODAN_COUNT_QUERY = "port:53 country:US"
BRIDGE_TURN_PROMPT = "AutoWork connectivity check. Reply with the single word OK."
LOCAL_BINARIES = ("typst", "pdftotext", "pdftoppm", "tshark", "bwrap", "yara", "floss")

_PROXY_ROUTE = VirusTotalOperationRoute(
    primary=VirusTotalRouteStep(
        transport=VirusTotalTransportKind.PROXY, variant=VirusTotalEndpointVariant.V3
    )
)
_VT_CAPABILITIES = (
    VirusTotalCapability.FILE_REPORT,
    VirusTotalCapability.FILE_RELATIONSHIPS,
    VirusTotalCapability.INTELLIGENCE_SEARCH,
)


def build_live_smoke_plan(settings: Settings) -> LiveSmokePlan:
    """Describe exactly what the smoke sends; deterministic for a given settings."""
    vt = settings.virustotal_base_url.rstrip("/")
    shodan = settings.shodan_base_url.rstrip("/")
    bridge = settings.openai_bridge_base_url.rstrip("/")
    return LiveSmokePlan(
        endpoints=(
            LiveSmokeEndpoint(
                provider="bridge",
                operation="capabilities",
                method="GET",
                endpoint=f"{bridge}/bridge/capabilities",
                submitted_data={},
                effect="Read the Bridge capability document; no model turn.",
                expected_cost="None.",
            ),
            LiveSmokeEndpoint(
                provider="bridge",
                operation="turn",
                method="POST",
                endpoint=f"{bridge}/responses",
                submitted_data={"prompt": BRIDGE_TURN_PROMPT},
                effect="One synthetic ChatGPT turn with a fixed non-sensitive prompt.",
                expected_cost="One Bridge turn.",
            ),
            LiveSmokeEndpoint(
                provider="virustotal",
                operation="file_report",
                method="GET",
                endpoint=f"{vt}/files/{EICAR_SHA256}",
                submitted_data={"sha256": EICAR_SHA256},
                effect="Read the public EICAR test-file report through the proxy.",
                expected_cost="One VT quota unit.",
            ),
            LiveSmokeEndpoint(
                provider="virustotal",
                operation="file_relationships",
                method="GET",
                endpoint=f"{vt}/files/{EICAR_SHA256}/contacted_ips",
                submitted_data={"sha256": EICAR_SHA256, "relationship": "contacted_ips"},
                effect="Read at most 5 contacted IPs of the EICAR test file.",
                expected_cost="One VT quota unit.",
            ),
            LiveSmokeEndpoint(
                provider="virustotal",
                operation="intelligence_search",
                method="GET",
                endpoint=f"{vt}/intelligence/search",
                submitted_data={"query": f"sha256:{EICAR_SHA256}", "limit": "1"},
                effect="One bounded intelligence search (limit 1) for the EICAR hash.",
                expected_cost="One VT intelligence quota unit; needs a Premium plan.",
            ),
            LiveSmokeEndpoint(
                provider="shodan",
                operation="host",
                method="GET",
                endpoint=f"{shodan}/shodan/host/{SHODAN_HOST_IP}",
                submitted_data={"ip": SHODAN_HOST_IP},
                effect="Passive indexed lookup of a public DNS resolver; no scan.",
                expected_cost="No query credit.",
            ),
            LiveSmokeEndpoint(
                provider="shodan",
                operation="count",
                method="GET",
                endpoint=f"{shodan}/shodan/host/count",
                submitted_data={"query": SHODAN_COUNT_QUERY},
                effect="Passive count of indexed hosts; no scan.",
                expected_cost="No query credit.",
            ),
        ),
        tlp=TLPLevel.CLEAR,
        operator_effect=(
            "Read-only VirusTotal/Shodan lookups on public subjects and one synthetic "
            "Bridge turn. No Retrohunt, no download, no upload, no scan, no case data."
        ),
        expected_total_cost="3 VT quota units, 1 Bridge turn, 0 Shodan credits.",
        vt_routing_policy=VirusTotalRoutingPolicy(
            routes={capability: _PROXY_ROUTE for capability in _VT_CAPABILITIES}
        ),
    )


def local_tool_inventory() -> dict[str, bool]:
    """Informational only: local binaries visible to this process."""
    return {name: shutil.which(name) is not None for name in LOCAL_BINARIES}


@dataclass(slots=True)
class LiveSmokeClients:
    bridge: ChatGPTBridgeClient | None
    vt: VirusTotalHttpAdapter | None
    shodan: Any | None  # ShodanHttpAdapter; None when no key is configured


class SettingsLiveSmokeExecutor:
    """Run the plan with the same adapters and settings as the API and worker."""

    def __init__(
        self,
        clients: LiveSmokeClients,
        *,
        bridge_model: str,
        bridge_turn_timeout_seconds: float = 300.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._clients = clients
        self._bridge_model = bridge_model
        self._turn_timeout = bridge_turn_timeout_seconds
        self._clock = clock

    async def execute(self, plan: LiveSmokePlan) -> Sequence[ProviderProbeResult]:
        results: list[ProviderProbeResult] = []
        for endpoint in plan.endpoints:
            handler = self._handlers().get((endpoint.provider.lower(), endpoint.operation.lower()))
            if handler is None:
                results.append(
                    self._result(
                        endpoint,
                        ProviderProbeStatus.FAIL,
                        0.0,
                        None,
                        "No smoke handler exists for this planned operation.",
                        "smoke_handler_missing",
                    )
                )
                continue
            results.append(await self._run(endpoint, handler))
        return tuple(results)

    def _handlers(
        self,
    ) -> dict[tuple[str, str], Callable[[], Awaitable[tuple[int | None, str, str | None]]]]:
        return {
            ("bridge", "capabilities"): self._bridge_capabilities,
            ("bridge", "turn"): self._bridge_turn,
            ("virustotal", "file_report"): self._vt_file_report,
            ("virustotal", "file_relationships"): self._vt_relationships,
            ("virustotal", "intelligence_search"): self._vt_search,
            ("shodan", "host"): self._shodan_host,
            ("shodan", "count"): self._shodan_count,
        }

    async def _run(
        self,
        endpoint: LiveSmokeEndpoint,
        handler: Callable[[], Awaitable[tuple[int | None, str, str | None]]],
    ) -> ProviderProbeResult:
        started = self._clock()
        try:
            http_status, summary, failure = await handler()
        except _NotConfiguredError as exc:
            return self._result(
                endpoint,
                ProviderProbeStatus.NOT_VALIDATED,
                (self._clock() - started) * 1_000,
                None,
                str(exc),
                "provider_not_configured",
            )
        except (VirusTotalError, ShodanError) as exc:
            return self._result(
                endpoint,
                ProviderProbeStatus.FAIL,
                (self._clock() - started) * 1_000,
                getattr(exc, "status_code", None),
                _redact_text(str(exc), limit=300),
                str(getattr(exc, "code", "provider_error")),
            )
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            return self._result(
                endpoint,
                ProviderProbeStatus.FAIL,
                (self._clock() - started) * 1_000,
                getattr(exc, "status_code", None),
                _redact_text(str(exc), limit=300),
                code,
            )
        status = ProviderProbeStatus.FAIL if failure else ProviderProbeStatus.PASS
        return self._result(
            endpoint,
            status,
            (self._clock() - started) * 1_000,
            http_status,
            summary,
            failure,
        )

    @staticmethod
    def _result(
        endpoint: LiveSmokeEndpoint,
        status: ProviderProbeStatus,
        duration_ms: float,
        http_status: int | None,
        summary: str,
        error_code: str | None,
    ) -> ProviderProbeResult:
        return ProviderProbeResult(
            provider=endpoint.provider,
            operation=endpoint.operation,
            mode=ProviderProbeMode.LIVE_SMOKE,
            status=status,
            endpoint=f"{endpoint.method.upper()} {endpoint.endpoint}",
            duration_ms=max(duration_ms, 0.0),
            http_status=http_status,
            summary=summary,
            error_code=error_code,
            response_preview=summary,
        )

    async def _bridge_capabilities(self) -> tuple[int | None, str, str | None]:
        bridge = self._require(self._clients.bridge, "Bridge")
        document = await bridge.capabilities()
        preview = json.dumps(_redact_json(document), ensure_ascii=False, sort_keys=True)
        if not document:
            return 200, "Bridge returned an empty capability document.", "bridge_empty"
        return 200, f"capabilities: {_redact_text(preview, limit=400)}", None

    async def _bridge_turn(self) -> tuple[int | None, str, str | None]:
        bridge = self._require(self._clients.bridge, "Bridge")
        raw = await bridge.create(
            {
                "model": self._bridge_model,
                "input": [{"role": "user", "content": BRIDGE_TURN_PROMPT}],
                "background": False,
            },
            idempotency_key="live-smoke-bridge-turn",
            timeout_seconds=self._turn_timeout,
        )
        status = str(raw.get("status", "completed"))
        if status != "completed":
            return 200, f"Bridge turn ended with status {status}.", f"bridge_turn_{status}"
        text = _responses_output_text(raw)
        archived = await self._archive(bridge, raw)
        return (
            200,
            f"turn completed; reply={_redact_text(text, limit=80)!r}; archived={archived}",
            None,
        )

    @staticmethod
    async def _archive(bridge: ChatGPTBridgeClient, raw: Mapping[str, Any]) -> bool:
        metadata = raw.get("metadata")
        conversation = metadata.get("conversation") if isinstance(metadata, dict) else None
        identifier = conversation.get("id") if isinstance(conversation, dict) else None
        if not isinstance(identifier, str):
            return False
        try:
            await bridge.archive_conversation(UUID(identifier))
        except (ValueError, RuntimeError, httpx.HTTPError):
            return False
        return True

    async def _vt_file_report(self) -> tuple[int | None, str, str | None]:
        vt = self._require(self._clients.vt, "VirusTotal proxy")
        report = await vt.file_report(EICAR_SHA256)
        matched = report.file.id == EICAR_SHA256
        return (
            report.http_status,
            f"file report received; id_matches={matched}",
            None if matched else "provider_response_partial",
        )

    async def _vt_relationships(self) -> tuple[int | None, str, str | None]:
        vt = self._require(self._clients.vt, "VirusTotal proxy")
        relationship = await vt.file_relationship(
            EICAR_SHA256, VirusTotalFileRelationship.CONTACTED_IPS, limit=5
        )
        status = relationship.http_statuses[0] if relationship.http_statuses else None
        return status, f"relationship page received; items={len(relationship.items)}", None

    async def _vt_search(self) -> tuple[int | None, str, str | None]:
        vt = self._require(self._clients.vt, "VirusTotal proxy")
        found = await vt.intelligence_search(f"sha256:{EICAR_SHA256}", limit=1)
        status = found.http_statuses[0] if found.http_statuses else None
        return status, f"intelligence search answered; items={len(found.items)}", None

    async def _shodan_host(self) -> tuple[int | None, str, str | None]:
        shodan = self._require(self._clients.shodan, "Shodan API key")
        host = await shodan.host(SHODAN_HOST_IP)
        failure = None if host.http_status == 200 else "provider_response_partial"
        return host.http_status, f"host lookup answered; services={len(host.services)}", failure

    async def _shodan_count(self) -> tuple[int | None, str, str | None]:
        shodan = self._require(self._clients.shodan, "Shodan API key")
        counted = await shodan.count(SHODAN_COUNT_QUERY)
        failure = None if counted.http_status == 200 else "provider_response_partial"
        return counted.http_status, f"count answered; total={counted.total}", failure

    @staticmethod
    def _require[Client](client: Client | None, name: str) -> Client:
        if client is None:
            raise _NotConfiguredError(f"{name} is not configured in this process.")
        return client


class _NotConfiguredError(Exception):
    pass


async def open_live_smoke_clients(settings: Settings, stack: AsyncExitStack) -> LiveSmokeClients:
    """Build the production adapters; the stack owns every HTTP client lifecycle."""
    bridge: ChatGPTBridgeClient | None = None
    if settings.openai_bridge_base_url:
        bridge = create_bridge_capabilities_provider(settings)
        # A turn can take long; the capabilities provider only budgets a probe.
        bridge._timeout = settings.openai_bridge_wait_timeout_seconds

    vt: VirusTotalHttpAdapter | None = None
    if settings.virustotal_proxy_url:
        vt_client = create_virustotal_http_client(settings)
        await stack.enter_async_context(vt_client)
        # Same fallback wiring as the API/worker roots: the smoke must exercise
        # the production file_report route, not a proxy-only simplification.
        vt_direct_client: httpx.AsyncClient | None = None
        if (
            settings.virustotal_api_key is not None
            and settings.virustotal_file_report_legacy_fallback_enabled
        ):
            vt_direct_client = create_virustotal_direct_http_client(settings)
            await stack.enter_async_context(vt_direct_client)
        smoke_routes = {
            capability: _PROXY_ROUTE
            for capability in (
                VirusTotalCapability.FILE_REPORT,
                VirusTotalCapability.FILE_RELATIONSHIPS,
                VirusTotalCapability.INTELLIGENCE_SEARCH,
            )
        }
        vt = create_virustotal_http_adapter(
            client=vt_client,
            settings=settings,
            direct_client=vt_direct_client,
            capabilities=VirusTotalCapabilities(
                file_report=True, file_relationships=True, intelligence_search=True
            ),
            routing_policy=VirusTotalRoutingPolicy(routes=smoke_routes),
        )

    shodan = None
    if settings.shodan_api_key is not None:
        shodan_client = create_shodan_http_client(settings)
        await stack.enter_async_context(shodan_client)
        passive = settings.model_copy(
            update={"shodan_host_enabled": True, "shodan_count_enabled": True}
        )
        shodan = create_shodan_http_adapter(client=shodan_client, settings=passive)
    return LiveSmokeClients(bridge=bridge, vt=vt, shodan=shodan)
