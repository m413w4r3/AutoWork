from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from cti_app.application.investigation_native_tools import (
    NATIVE_INSPECT_SCHEMA,
    native_tool_capabilities,
    register_native_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.native_analysis import (
    NATIVE_ANALYSIS_CATALOG_VERSION,
    NativeAnalysisPolicyDenied,
    NativeAnalysisService,
    NativeAnalysisTimeout,
    NativeAnalysisUnavailable,
    NativeAnalysisVersionMismatch,
    build_native_analysis_request,
    normalize_native_data,
    select_native_analysis_port,
)
from cti_app.config import Settings
from cti_app.domain.local_analysis import GhidraOperation
from cti_app.domain.native_analysis import (
    NativeAnalysisOperation,
    NativeAnalysisProvider,
    NativeAnalysisRequestV1,
)
from cti_app.infrastructure.ghidra_mcp import GhidraMcpHttpAdapter
from cti_app.infrastructure.rea_adapter import (
    REA_READ_ONLY_CATALOG,
    GhidraMcpNativeAnalysisAdapter,
    ReaNativeAnalysisAdapter,
    ReaReadOnlyRequestV1,
)

SAMPLE_SHA256 = "a" * 64
REA_VERSION = "rea-fixture-1.0.0"
SYNTHETIC_ELF = b"\x7fELF\x02\x01\x01" + (b"\x00" * 57)


class StrictFakeReaRunner:
    available = True
    worker_confined = True
    dedicated_user = True
    network_disabled = True
    filesystem_confined = True
    no_host_privileges = True

    def __init__(
        self,
        *,
        delay: float = 0.0,
        large_output: bool = False,
        provider_version: str = REA_VERSION,
        sample_sha256: str | None = None,
    ) -> None:
        self.delay = delay
        self.large_output = large_output
        self.provider_version = provider_version
        self.sample_sha256 = sample_sha256
        self.requests: list[ReaReadOnlyRequestV1] = []
        self.bounds: list[tuple[float, int]] = []

    async def inspect(
        self,
        request: ReaReadOnlyRequestV1,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        payload = request.as_json()
        assert payload["schema_version"] == "autowork-rea-read-request.v1"
        assert payload["catalog_version"] == NATIVE_ANALYSIS_CATALOG_VERSION
        assert payload["operation"] in {item.value for item in REA_READ_ONLY_CATALOG}
        if self.sample_sha256 is not None:
            assert payload["sample_sha256"] == self.sample_sha256
        assert set(payload) == {
            "schema_version",
            "catalog_version",
            "sample_sha256",
            "operation",
            "address",
            "symbol",
            "search_kind",
            "query",
            "value_type",
            "size",
            "limit",
        }
        self.requests.append(request)
        self.bounds.append((timeout_seconds, max_response_bytes))
        if self.delay:
            await asyncio.sleep(self.delay)
        functions = [
            {"address": "0x20", "name": "second"},
            {"address": "0x10", "name": "entry"},
        ]
        if self.large_output:
            functions = [{"address": "0x10", "name": "x" * 1000} for _ in range(20)]
        return {
            "schema_version": "autowork-rea-read-response.v1",
            "provider_version": self.provider_version,
            "catalog_version": NATIVE_ANALYSIS_CATALOG_VERSION,
            "sample_sha256": request.request.sample_sha256,
            "operation": request.request.operation.value,
            "request_fingerprint": request.request.fingerprint,
            "data": {"functions": functions},
        }


def _request(operation: str = "list_functions", **extra: Any) -> NativeAnalysisRequestV1:
    return build_native_analysis_request(
        {"sample_sha256": SAMPLE_SHA256, "operation": operation, **extra}
    )


@pytest.mark.asyncio
async def test_rea_smoke_uses_pinned_read_catalog_and_returns_sorted_provenance() -> None:
    synthetic_sha256 = hashlib.sha256(SYNTHETIC_ELF).hexdigest()
    runner = StrictFakeReaRunner(sample_sha256=synthetic_sha256)
    adapter = ReaNativeAnalysisAdapter(
        runner,
        enabled=True,
        expected_version=REA_VERSION,
        catalog_version=NATIVE_ANALYSIS_CATALOG_VERSION,
    )

    request = build_native_analysis_request(
        {"sample_sha256": synthetic_sha256, "operation": "list_functions"}
    )
    result = await adapter.inspect(request)

    assert result.provider is NativeAnalysisProvider.REA
    assert result.provider_version == REA_VERSION
    assert result.catalog_version == NATIVE_ANALYSIS_CATALOG_VERSION
    assert result.operation is NativeAnalysisOperation.LIST_FUNCTIONS
    assert [item["address"] for item in result.data["functions"]] == ["0x10", "0x20"]
    assert runner.bounds == [(30.0, 64 * 1024)]


@pytest.mark.asyncio
async def test_provider_limit_is_enforced_even_if_the_client_returns_more_items() -> None:
    adapter = ReaNativeAnalysisAdapter(
        StrictFakeReaRunner(),
        enabled=True,
        expected_version=REA_VERSION,
    )
    service = NativeAnalysisService(adapter)

    result = await service.inspect(_request(limit=1))

    assert result.truncated
    assert result.data["functions"] == [{"address": "0x10", "name": "entry"}]


@pytest.mark.asyncio
async def test_rea_provider_version_must_match_the_configured_pin() -> None:
    adapter = ReaNativeAnalysisAdapter(
        StrictFakeReaRunner(provider_version="rea-unpinned-version"),
        enabled=True,
        expected_version=REA_VERSION,
    )

    with pytest.raises(NativeAnalysisVersionMismatch):
        await adapter.inspect(_request())


@pytest.mark.asyncio
async def test_unsafe_or_missing_rea_worker_is_cleanly_unavailable() -> None:
    adapter = ReaNativeAnalysisAdapter(
        None,
        enabled=True,
        expected_version=REA_VERSION,
    )
    service = NativeAnalysisService(adapter)

    with pytest.raises(NativeAnalysisUnavailable):
        await service.inspect(_request())
    assert not adapter.available
    assert (
        native_tool_capabilities(
            enabled=True,
            provider="rea",
            port=adapter,
            recorder=object(),
            sample_associations=object(),
        )
        == frozenset()
    )

    unconfined_runner = StrictFakeReaRunner()
    unconfined_runner.network_disabled = False
    unconfined = ReaNativeAnalysisAdapter(
        unconfined_runner,
        enabled=True,
        expected_version=REA_VERSION,
    )
    assert not unconfined.available
    with pytest.raises(NativeAnalysisUnavailable):
        await unconfined.inspect(_request())


@pytest.mark.asyncio
async def test_rea_timeout_is_bounded_and_reported_as_timeout() -> None:
    adapter = ReaNativeAnalysisAdapter(
        StrictFakeReaRunner(delay=0.05),
        enabled=True,
        expected_version=REA_VERSION,
        timeout_seconds=0.005,
    )

    with pytest.raises(NativeAnalysisTimeout):
        await adapter.inspect(_request())


@pytest.mark.asyncio
async def test_large_rea_output_is_sorted_and_truncated_to_configured_bound() -> None:
    adapter = ReaNativeAnalysisAdapter(
        StrictFakeReaRunner(large_output=True),
        enabled=True,
        expected_version=REA_VERSION,
        max_output_bytes=256,
        max_response_bytes=4096,
    )

    result = await adapter.inspect(_request())

    encoded = json.dumps(result.as_json(), sort_keys=True, separators=(",", ":")).encode()
    assert result.truncated
    assert len(json.dumps(result.data, sort_keys=True, separators=(",", ":")).encode()) <= 256
    assert len(encoded) > 256  # provenance is outside the provider data budget


def test_active_operations_and_unlisted_inputs_are_rejected_before_provider_call() -> None:
    runner = StrictFakeReaRunner()
    for operation in (
        "edit",
        "write",
        "execute",
        "run_process",
        "capture_live_site",
        "capture_live_traffic",
        "launch_payload",
        "host_privilege",
    ):
        with pytest.raises(NativeAnalysisPolicyDenied):
            _request(operation)
    with pytest.raises(NativeAnalysisPolicyDenied):
        build_native_analysis_request(
            {"sample_sha256": SAMPLE_SHA256, "operation": "list_functions", "command": "sh"}
        )
    assert runner.requests == []


def test_domain_requests_enforce_closed_inputs_and_bounds() -> None:
    with pytest.raises(NativeAnalysisPolicyDenied):
        _request("read_value", address="401000")
    with pytest.raises(NativeAnalysisPolicyDenied):
        _request("search", search_kind="bytes", query="0f1")
    with pytest.raises(NativeAnalysisPolicyDenied):
        _request("read_value", address="401000", value_type="bytes", size=65)
    with pytest.raises(NativeAnalysisPolicyDenied):
        _request("list_functions", limit=101)


def test_native_result_normalization_is_deterministic_and_bounded() -> None:
    result, truncated = normalize_native_data(
        {"results": [{"name": "long", "value": "x" * 1024}, {"name": "short"}]},
        max_output_bytes=128,
    )

    assert truncated
    assert list(result) == ["results"]
    assert len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()) <= 128


def test_native_analysis_is_off_and_unselected_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.native_analysis_enabled is False
    assert settings.native_analysis_provider == "none"
    assert settings.native_analysis_rea_expected_version is None


def test_provider_selection_returns_an_unavailable_port_when_provider_is_missing() -> None:
    selected = select_native_analysis_port("rea", rea=None, ghidra_mcp=None)

    assert selected.provider is NativeAnalysisProvider.REA
    assert not selected.available


def test_registration_exposes_one_tool_with_only_the_closed_native_enum() -> None:
    runner = StrictFakeReaRunner()
    port = ReaNativeAnalysisAdapter(runner, enabled=True, expected_version=REA_VERSION)
    registry = InMemoryToolRegistry()

    register_native_tools(
        registry,
        service=NativeAnalysisService(port),
        port=port,
        sample_associations=object(),
        recorder=object(),
        enabled=True,
        provider=NativeAnalysisProvider.REA,
    )
    registered = registry.get("native.inspect", "1")

    assert registered is not None
    definition, _tool = registered
    assert definition.input_schema["additionalProperties"] is False
    assert list(definition.input_schema["properties"]["operation"]["enum"]) == list(
        NATIVE_INSPECT_SCHEMA["properties"]["operation"]["enum"]
    )
    assert set(definition.input_schema["properties"]["operation"]["enum"]) == {
        operation.value for operation in NativeAnalysisOperation
    }
    assert registry.get("other.tool", "1") is None
    assert native_tool_capabilities(
        enabled=True,
        provider=NativeAnalysisProvider.REA,
        port=port,
        recorder=object(),
        sample_associations=object(),
    ) == frozenset({"native.inspect"})


@pytest.mark.asyncio
async def test_legacy_ghidra_adapter_is_wrapped_without_changing_http_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "ghidra_version": "11.3",
                "plugin_version": "0.4.0",
                "data": {"xrefs": [{"address": "0x30"}, {"address": "0x20"}]},
            },
        )

    legacy = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.XREF_TO: "/read/xrefs-to"},
        expected_version="11.3",
        expected_plugin_version="0.4.0",
        transport=httpx.MockTransport(handler),
    )
    wrapper = GhidraMcpNativeAnalysisAdapter(
        legacy,
        enabled=True,
        catalog_version=NATIVE_ANALYSIS_CATALOG_VERSION,
    )
    try:
        result = await wrapper.inspect(_request("callers", address="401000", limit=10))
    finally:
        await legacy.aclose()

    assert result.provider is NativeAnalysisProvider.GHIDRA_MCP
    assert result.provider_version == "11.3"
    assert result.provider_plugin_version == "0.4.0"
    assert [item["address"] for item in result.data["xrefs"]] == ["0x20", "0x30"]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/read/xrefs-to"
    assert seen[0].url.params["address"] == "0x401000"
    assert seen[0].url.params["sample_sha256"] == SAMPLE_SHA256
