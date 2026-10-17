from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from cti_app.application.provider_probes import (
    LiveSmokeApprovalRequiredError,
    LiveSmokeEndpoint,
    LiveSmokePlan,
    ProviderProbeMode,
    ProviderProbeStatus,
    _redact_endpoint,
    _redact_text,
    _run_probe,
    offline_preflight,
    run_live_smoke,
)
from cti_app.application.shodan import (
    ShodanCapabilities,
    ShodanCapabilityDisabledError,
    ShodanError,
    ShodanHttpError,
    ShodanJsonError,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalHttpError,
    VirusTotalJsonError,
    VirusTotalNotAuthorizedError,
    VirusTotalOperationRoute,
    VirusTotalPayloadError,
    VirusTotalQuotaExceededError,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.domain.investigation import TLPLevel
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.shodan import ShodanHttpAdapter
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter

VT_BASE = "https://www.virustotal.com/api/v3"
SHODAN_BASE = "https://api.shodan.io"
FILE_HASH = "a" * 64
TEST_IP = "203.0.113.10"


def _vt_adapter(client: httpx.AsyncClient) -> VirusTotalHttpAdapter:
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    return VirusTotalHttpAdapter(
        client=client,
        base_url=VT_BASE,
        capabilities=VirusTotalCapabilities(file_report=True),
        routing_policy=VirusTotalRoutingPolicy(routes={VirusTotalCapability.FILE_REPORT: route}),
    )


def _shodan_adapter(client: httpx.AsyncClient) -> ShodanHttpAdapter:
    return ShodanHttpAdapter(
        client=client,
        base_url=SHODAN_BASE,
        capabilities=ShodanCapabilities(host=True, search=True),
        api_key=SecretStr("offline-test-key"),
    )


@pytest.mark.asyncio
async def test_offline_preflight_passes_six_mocked_provider_contracts_only() -> None:
    report = await offline_preflight()

    assert report.mode is ProviderProbeMode.OFFLINE_SIMULATED
    assert report.passed
    assert {(probe.provider, probe.operation) for probe in report.probes} == {
        ("virustotal", "file_report"),
        ("virustotal", "file_relationships"),
        ("virustotal", "intelligence_search"),
        ("virustotal", "behaviour"),
        ("shodan", "host"),
        ("shodan", "search"),
    }
    assert all(probe.status is ProviderProbeStatus.PASS for probe in report.probes)
    assert all(probe.mode is ProviderProbeMode.OFFLINE_SIMULATED for probe in report.probes)
    assert all(probe.http_status == 200 for probe in report.probes)
    assert not any(
        probe.operation in {"file_download", "retrohunt", "retrohunt_submit"}
        for probe in report.probes
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type", "error_code"),
    [
        (401, VirusTotalHttpError, "virustotal_upstream_unauthorized"),
        (403, VirusTotalNotAuthorizedError, None),
        (404, VirusTotalHttpError, "virustotal_not_found"),
        (429, VirusTotalQuotaExceededError, None),
    ],
)
async def test_vt_probe_http_failures_are_typed_without_network(
    status: int, error_type: type[Exception], error_code: str | None
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "simulated"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(error_type) as raised:
            await _vt_adapter(client).file_report(FILE_HASH)
    assert isinstance(raised.value, VirusTotalError)
    assert raised.value.status_code == status
    if error_code is not None:
        assert raised.value.code == error_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        (b"not-json", VirusTotalJsonError),
        (b'{"data":{"id":"' + FILE_HASH.encode() + b'"}}', VirusTotalPayloadError),
    ],
)
async def test_vt_malformed_and_partial_responses_are_typed(
    body: bytes, error_type: type[Exception]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(error_type):
            await _vt_adapter(client).file_report(FILE_HASH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ShodanCapabilityDisabledError),
        (403, ShodanCapabilityDisabledError),
        (429, ShodanHttpError),
    ],
)
async def test_shodan_probe_http_failures_are_typed_without_network(
    status: int, error_type: type[Exception]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "simulated"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(error_type) as raised:
            await _shodan_adapter(client).host(TEST_IP)
    assert isinstance(raised.value, ShodanError)
    assert raised.value.status_code == status


@pytest.mark.asyncio
async def test_shodan_404_and_partial_responses_are_not_provider_conclusions() -> None:
    async def not_found(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/shodan/host/203.0.113.10":
            return httpx.Response(404, json={"error": "not found"}, request=request)
        return httpx.Response(200, json={"query_credits": 0}, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(not_found), trust_env=False
    ) as client:
        adapter = _shodan_adapter(client)
        not_found_result = await _run_probe(
            provider="shodan",
            operation="host",
            endpoint=f"GET {SHODAN_BASE}/shodan/host/{TEST_IP}",
            call=lambda: adapter.host(TEST_IP),
            validate=lambda result: result.http_status == 200 and bool(result.services),
            status=lambda result: result.http_status,
        )

    assert not_found_result.status is ProviderProbeStatus.NOT_VALIDATED
    assert not_found_result.error_code == "provider_not_found"
    assert "no negative cti" in not_found_result.summary.lower()

    async def partial(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/shodan/host/203.0.113.10":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(200, json={"query_credits": 0}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(partial), trust_env=False) as client:
        adapter = _shodan_adapter(client)
        partial_result = await _run_probe(
            provider="shodan",
            operation="host",
            endpoint=f"GET {SHODAN_BASE}/shodan/host/{TEST_IP}",
            call=lambda: adapter.host(TEST_IP),
            validate=lambda result: result.http_status == 200 and bool(result.services),
            status=lambda result: result.http_status,
        )
    assert partial_result.status is ProviderProbeStatus.FAIL
    assert partial_result.error_code == "provider_response_partial"


@pytest.mark.asyncio
async def test_shodan_malformed_json_is_a_typed_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(ShodanJsonError):
            await _shodan_adapter(client).host(TEST_IP)


@pytest.mark.asyncio
async def test_live_smoke_refuses_to_execute_without_exact_operator_approval() -> None:
    proxy_route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    plan = LiveSmokePlan(
        endpoints=(
            LiveSmokeEndpoint(
                provider="virustotal",
                operation="file_report",
                method="GET",
                endpoint=f"{VT_BASE}/files/{FILE_HASH}",
                submitted_data={"sha256": FILE_HASH},
                effect="Read one existing file report; no sample is uploaded.",
                expected_cost="One file report quota unit, subject to account plan.",
            ),
        ),
        tlp=TLPLevel.GREEN,
        operator_effect="Passive metadata reads only.",
        expected_total_cost="At most one VT file report quota unit.",
        vt_routing_policy=VirusTotalRoutingPolicy(
            routes={VirusTotalCapability.FILE_REPORT: proxy_route}
        ),
    )

    class _Executor:
        calls = 0

        async def execute(self, _plan):
            self.calls += 1
            raise AssertionError("the unapproved smoke executor must not be called")

    executor = _Executor()
    with pytest.raises(LiveSmokeApprovalRequiredError):
        await run_live_smoke(plan, operator_approval=None, executor=executor)
    assert executor.calls == 0
    assert "sha256" in plan.endpoints[0].submitted_data


def test_live_smoke_plan_rejects_active_or_direct_virustotal_operations() -> None:
    with pytest.raises(ValueError, match="cannot submit Retrohunt"):
        LiveSmokeEndpoint(
            provider="virustotal",
            operation="retrohunt_submit",
            method="GET",
            endpoint="https://vtui.test/api/tools/retrohunt/jobs",
            submitted_data={"rules": "rule x { condition: true }"},
            effect="Submit a Retrohunt job.",
            expected_cost="Unknown.",
        )

    direct_route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.DIRECT,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    with pytest.raises(ValueError, match="proxy-only"):
        LiveSmokePlan(
            endpoints=(
                LiveSmokeEndpoint(
                    provider="virustotal",
                    operation="file_report",
                    method="GET",
                    endpoint=f"{VT_BASE}/files/{FILE_HASH}",
                    submitted_data={"sha256": FILE_HASH},
                    effect="Read metadata.",
                    expected_cost="One query.",
                ),
            ),
            tlp=TLPLevel.GREEN,
            operator_effect="Passive read.",
            expected_total_cost="One query.",
            vt_routing_policy=VirusTotalRoutingPolicy(
                routes={VirusTotalCapability.FILE_REPORT: direct_route}
            ),
        )


def test_live_smoke_log_redaction_removes_keys_and_signed_urls() -> None:
    response = _redact_text(
        '{"api_key":"secret-api-key","signed_url":"https://files.test/x?'
        'X-Amz-Signature=secret-signature"}',
        limit=2_000,
    )
    endpoint = _redact_endpoint("https://vtui.test/result?x-apikey=secret-key")
    assert "secret-api-key" not in response
    assert "secret-signature" not in response
    assert "secret-key" not in endpoint
