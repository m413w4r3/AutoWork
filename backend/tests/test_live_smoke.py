from __future__ import annotations

import importlib.util
import json
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from cti_app.application.live_smoke import (
    EICAR_SHA256,
    LiveSmokeClients,
    SettingsLiveSmokeExecutor,
    build_live_smoke_plan,
    open_live_smoke_clients,
)
from cti_app.application.provider_probes import (
    LiveSmokeApproval,
    LiveSmokeApprovalRequiredError,
    LiveSmokeEndpoint,
    ProviderProbeMode,
    ProviderProbeStatus,
    run_live_smoke,
)
from cti_app.application.shodan import ShodanCapabilities
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.shodan import ShodanHttpAdapter
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter

SECRET = "super-secret-shodan-key"
_ROUTE = VirusTotalOperationRoute(
    primary=VirusTotalRouteStep(
        transport=VirusTotalTransportKind.PROXY, variant=VirusTotalEndpointVariant.V3
    )
)


class _FakeBridge:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status
        self.created: list[dict[str, Any]] = []
        self.archived: list[str] = []

    async def capabilities(self) -> dict[str, Any]:
        return {"profiles": ["chatgpt-web"], "api_key": SECRET}

    async def create(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.created.append(payload)
        return {
            "id": "resp_1",
            "status": self.status,
            "output_text": "OK",
            "metadata": {"conversation": {"id": "3d6f1d1e-8f43-4bf8-bb39-0c4d2e0dbd79"}},
        }

    async def archive_conversation(self, conversation_id: Any) -> None:
        self.archived.append(str(conversation_id))


def _vt_adapter(handler: Any, client_stack: list[httpx.AsyncClient]) -> VirusTotalHttpAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    client_stack.append(client)
    return VirusTotalHttpAdapter(
        client=client,
        base_url="https://www.virustotal.com/api/v3",
        capabilities=VirusTotalCapabilities(
            file_report=True, file_relationships=True, intelligence_search=True
        ),
        offline_probe_mode=True,
        routing_policy=VirusTotalRoutingPolicy(
            routes={
                VirusTotalCapability.FILE_REPORT: _ROUTE,
                VirusTotalCapability.FILE_RELATIONSHIPS: _ROUTE,
                VirusTotalCapability.INTELLIGENCE_SEARCH: _ROUTE,
            }
        ),
    )


def _good_vt(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/api/v3/files/{EICAR_SHA256}":
        body: dict[str, Any] = {
            "data": {
                "id": EICAR_SHA256,
                "type": "file",
                "attributes": {"sha256": EICAR_SHA256, "last_analysis_stats": {"malicious": 60}},
            }
        }
    elif path == f"/api/v3/files/{EICAR_SHA256}/contacted_ips":
        body = {"data": [{"id": "203.0.113.5", "type": "ip_address"}], "meta": {"count": 1}}
    elif path == "/api/v3/intelligence/search":
        body = {"data": [{"id": EICAR_SHA256, "type": "file"}], "meta": {"count": 1}}
    else:
        return httpx.Response(404, json={"error": {"code": "NotFoundError"}})
    return httpx.Response(200, json=body)


def _shodan(handler: Any, client_stack: list[httpx.AsyncClient]) -> ShodanHttpAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    client_stack.append(client)
    return ShodanHttpAdapter(
        client=client,
        base_url="https://api.shodan.io",
        capabilities=ShodanCapabilities(host=True, count=True),
        api_key=SecretStr(SECRET),
    )


def _good_shodan(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/shodan/host/8.8.8.8":
        return httpx.Response(
            200,
            json={
                "ip_str": "8.8.8.8",
                "org": "Google",
                "ports": [53],
                "data": [{"port": 53, "transport": "udp", "data": "banner"}],
            },
        )
    if request.url.path == "/shodan/host/count":
        return httpx.Response(200, json={"total": 42, "matches": [], "facets": {}})
    if request.url.path == "/api-info":
        return httpx.Response(200, json={"query_credits": 10, "plan": "dev"})
    return httpx.Response(404, json={"error": "not found"})


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _approval(plan_sha: str) -> LiveSmokeApproval:
    return LiveSmokeApproval(
        operator_id="tester", approved_at=datetime.now(UTC), plan_sha256=plan_sha
    )


def test_plan_is_deterministic_and_lists_the_synthetic_bridge_turn() -> None:
    plan = build_live_smoke_plan(_settings())
    assert plan.sha256 == build_live_smoke_plan(_settings()).sha256
    operations = {(item.provider, item.operation) for item in plan.endpoints}
    assert operations == {
        ("bridge", "capabilities"),
        ("bridge", "turn"),
        ("virustotal", "file_report"),
        ("virustotal", "file_relationships"),
        ("virustotal", "intelligence_search"),
        ("shodan", "host"),
        ("shodan", "count"),
    }
    assert not any("retrohunt" in item.operation for item in plan.endpoints)
    assert {item.method for item in plan.endpoints if item.provider != "bridge"} == {"GET"}


def test_only_the_bridge_turn_may_use_post() -> None:
    with pytest.raises(ValueError, match="passive GET"):
        LiveSmokeEndpoint(
            provider="shodan",
            operation="host",
            method="POST",
            endpoint="https://api.shodan.io/x",
            submitted_data={},
            effect="x",
            expected_cost="x",
        )


@pytest.mark.asyncio
async def test_unapproved_or_mismatching_plan_never_reaches_the_executor() -> None:
    plan = build_live_smoke_plan(_settings())
    bridge = _FakeBridge()
    executor = SettingsLiveSmokeExecutor(
        LiveSmokeClients(bridge=bridge, vt=None, shodan=None),
        bridge_model="m",  # type: ignore[arg-type]
    )
    with pytest.raises(LiveSmokeApprovalRequiredError):
        await run_live_smoke(plan, operator_approval=None, executor=executor)
    with pytest.raises(LiveSmokeApprovalRequiredError):
        await run_live_smoke(plan, operator_approval=_approval("0" * 64), executor=executor)
    assert bridge.created == []


@pytest.mark.asyncio
async def test_all_probes_pass_and_no_secret_leaks_into_results() -> None:
    clients: list[httpx.AsyncClient] = []
    bridge = _FakeBridge()
    plan = build_live_smoke_plan(_settings())
    executor = SettingsLiveSmokeExecutor(
        LiveSmokeClients(
            bridge=bridge,  # type: ignore[arg-type]
            vt=_vt_adapter(_good_vt, clients),
            shodan=_shodan(_good_shodan, clients),
        ),
        bridge_model="chatgpt-web",
    )
    try:
        results = await run_live_smoke(
            plan, operator_approval=_approval(plan.sha256), executor=executor
        )
    finally:
        for client in clients:
            await client.aclose()
    assert len(results) == 7
    assert all(item.mode is ProviderProbeMode.LIVE_SMOKE for item in results)
    assert [item.status for item in results] == [ProviderProbeStatus.PASS] * 7
    assert len(bridge.created) == 1
    assert bridge.created[0]["input"][0]["content"].startswith("AutoWork connectivity check")
    assert bridge.archived == ["3d6f1d1e-8f43-4bf8-bb39-0c4d2e0dbd79"]
    assert SECRET not in json.dumps([asdict(item) for item in results], default=str)


@pytest.mark.asyncio
async def test_unconfigured_providers_are_not_validated_and_never_called() -> None:
    plan = build_live_smoke_plan(_settings())
    executor = SettingsLiveSmokeExecutor(
        LiveSmokeClients(bridge=None, vt=None, shodan=None), bridge_model="m"
    )
    results = await run_live_smoke(
        plan, operator_approval=_approval(plan.sha256), executor=executor
    )
    assert {item.status for item in results} == {ProviderProbeStatus.NOT_VALIDATED}
    assert {item.error_code for item in results} == {"provider_not_configured"}


@pytest.mark.asyncio
async def test_vt_authorisation_failure_and_bridge_non_completed_turn_fail() -> None:
    clients: list[httpx.AsyncClient] = []

    def denied(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "ForbiddenError"}})

    plan = build_live_smoke_plan(_settings())
    executor = SettingsLiveSmokeExecutor(
        LiveSmokeClients(
            bridge=_FakeBridge(status="needs_review"),  # type: ignore[arg-type]
            vt=_vt_adapter(denied, clients),
            shodan=None,
        ),
        bridge_model="m",
    )
    try:
        results = await run_live_smoke(
            plan, operator_approval=_approval(plan.sha256), executor=executor
        )
    finally:
        for client in clients:
            await client.aclose()
    by_operation = {(item.provider, item.operation): item for item in results}
    assert by_operation[("bridge", "capabilities")].status is ProviderProbeStatus.PASS
    assert by_operation[("bridge", "turn")].status is ProviderProbeStatus.FAIL
    assert by_operation[("bridge", "turn")].error_code == "bridge_turn_needs_review"
    for operation in ("file_report", "file_relationships", "intelligence_search"):
        item = by_operation[("virustotal", operation)]
        assert item.status is ProviderProbeStatus.FAIL
        assert item.http_status == 403
    assert by_operation[("shodan", "host")].status is ProviderProbeStatus.NOT_VALIDATED


@pytest.mark.asyncio
async def test_open_clients_follow_the_configuration() -> None:
    bare = _settings()
    async with AsyncExitStack() as stack:
        clients = await open_live_smoke_clients(bare, stack)
    assert clients.vt is None and clients.shodan is None and clients.bridge is not None

    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        virustotal_proxy_url="http://proxy.invalid:3128",
        shodan_api_key=SecretStr(SECRET),
    )
    async with AsyncExitStack() as stack:
        clients = await open_live_smoke_clients(configured, stack)
        assert clients.vt is not None and clients.shodan is not None


@pytest.mark.asyncio
async def test_live_smoke_keeps_download_and_retrohunt_routes_unavailable() -> None:
    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        virustotal_proxy_url="http://proxy.invalid:3128",
        virustotal_file_download_enabled=True,
        virustotal_retrohunt_enabled=True,
        virustotal_download_allowed_hosts="downloads.example.test",
    )
    async with AsyncExitStack() as stack:
        clients = await open_live_smoke_clients(configured, stack)

    assert clients.vt is not None
    assert not clients.vt._capabilities.file_download
    assert not clients.vt._capabilities.retrohunt
    assert clients.vt._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is None
    assert not any(
        "retrohunt" in endpoint.operation
        for endpoint in build_live_smoke_plan(configured).endpoints
    )


def _load_script() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "live_smoke.py"
    spec = importlib.util.spec_from_file_location("live_smoke_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_prints_the_plan_and_refuses_without_approval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "get_settings", _settings)
    assert script.main(["--plan-only"]) == 0
    printed = capsys.readouterr().out
    plan_sha = build_live_smoke_plan(_settings()).sha256
    assert plan_sha in printed

    assert script.main([]) == 2
    captured = capsys.readouterr()
    assert plan_sha in captured.out
    assert "Approbation requise" in captured.err

    assert script.main(["--approve-sha256", "0" * 64, "--operator", "nill"]) == 2
    assert "Refusé" in capsys.readouterr().err
