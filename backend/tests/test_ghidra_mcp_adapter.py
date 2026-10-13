from __future__ import annotations

import json
from datetime import UTC

import httpx
import pytest

from cti_app.application.local_analysis import (
    GhidraPolicyDeniedError,
    GhidraResponseTooLargeError,
    GhidraUnavailableError,
    GhidraVersionMismatchError,
)
from cti_app.config import Settings
from cti_app.domain.local_analysis import GhidraOperation
from cti_app.infrastructure.ghidra_mcp import (
    GhidraMcpHttpAdapter,
    create_ghidra_mcp_http_adapter,
)

SAMPLE_SHA256 = "a" * 64


@pytest.mark.asyncio
async def test_function_fixture_returns_traceable_normalized_observation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ghidra_version": "11.3",
                "plugin_version": "0.4.0",
                "data": {
                    "functions": [{"name": "entry", "address": "0x401000"}],
                    "raw_bytes": "must-not-be-kept",
                    "note": "https://example.invalid/sample?token=secret",
                },
            },
        )

    adapter = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        expected_version="11.3",
        expected_plugin_version="0.4.0",
        transport=httpx.MockTransport(handler),
    )
    try:
        observation = await adapter.list_functions(SAMPLE_SHA256, limit=10)
    finally:
        await adapter.aclose()

    assert observation.operation is GhidraOperation.FUNCTIONS
    assert observation.sample_sha256 == SAMPLE_SHA256
    assert observation.ghidra_version == "11.3"
    assert observation.plugin_version == "0.4.0"
    assert observation.address is None
    assert observation.observed_at.tzinfo is UTC
    assert observation.data["functions"] == [{"name": "entry", "address": "0x401000"}]
    assert "raw_bytes" not in observation.data
    assert "secret" not in json.dumps(observation.data)
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/read/functions"
    assert requests[0].url.params["sample_sha256"] == SAMPLE_SHA256


@pytest.mark.asyncio
async def test_address_operations_normalize_address_and_carry_hash() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "ghidra_version": "11.3",
                "plugin_version": "0.4.0",
                "data": {"function": "entry", "decompilation": "int entry(void) { return 0; }"},
            },
        )

    adapter = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.DECOMPILE_FUNCTION: "/read/decompile"},
        transport=httpx.MockTransport(handler),
    )
    try:
        observation = await adapter.decompile_function(SAMPLE_SHA256, "0x00401000")
    finally:
        await adapter.aclose()

    assert observation.address == "0x401000"
    assert seen[0].url.params["address"] == "0x401000"
    assert seen[0].url.params["sample_sha256"] == SAMPLE_SHA256


@pytest.mark.asyncio
async def test_write_operation_is_refused_without_http_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    adapter = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(GhidraPolicyDeniedError):
            await adapter.analyze("rename_function", SAMPLE_SHA256)
        with pytest.raises(GhidraPolicyDeniedError):
            await adapter.analyze("set_comment", SAMPLE_SHA256)
    finally:
        await adapter.aclose()

    assert requests == []


def test_non_loopback_host_requires_allowlist_and_warns_for_isolated_deployment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(GhidraPolicyDeniedError):
        GhidraMcpHttpAdapter(
            base_url="http://ghidra.internal:8080",
            enabled=True,
        )

    allowed = GhidraMcpHttpAdapter(
        base_url="http://ghidra.internal:8080",
        allowed_hosts=("ghidra.internal",),
        enabled=True,
    )
    assert allowed
    assert any(
        record.message == "ghidra_non_loopback_host_allowed_for_isolated_deployment"
        and record.ghidra_host == "ghidra.internal"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_ghidra_disabled_or_unconfigured_is_local_unavailable() -> None:
    defaults = Settings(_env_file=None)
    adapter = create_ghidra_mcp_http_adapter(
        defaults,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(GhidraUnavailableError):
            await adapter.list_functions(SAMPLE_SHA256)
    finally:
        await adapter.aclose()

    enabled_without_endpoint = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(GhidraUnavailableError):
            await enabled_without_endpoint.list_functions(SAMPLE_SHA256)
    finally:
        await enabled_without_endpoint.aclose()


@pytest.mark.asyncio
async def test_redirect_response_and_oversized_body_are_refused() -> None:
    redirect = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/other"})
        ),
    )
    try:
        with pytest.raises(GhidraUnavailableError):
            await redirect.list_functions(SAMPLE_SHA256)
    finally:
        await redirect.aclose()

    oversized = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        max_response_bytes=16,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"data":"' + b"x" * 32 + b'"}')
        ),
    )
    try:
        with pytest.raises(GhidraResponseTooLargeError):
            await oversized.list_functions(SAMPLE_SHA256)
    finally:
        await oversized.aclose()


@pytest.mark.asyncio
async def test_version_mismatch_is_typed_and_large_observation_is_truncated() -> None:
    mismatch = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        expected_version="11.3",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"ghidra_version": "10.4", "plugin_version": "0.4", "data": {}},
            )
        ),
    )
    try:
        with pytest.raises(GhidraVersionMismatchError):
            await mismatch.list_functions(SAMPLE_SHA256)
    finally:
        await mismatch.aclose()

    truncated = GhidraMcpHttpAdapter(
        enabled=True,
        endpoint_paths={GhidraOperation.FUNCTIONS: "/read/functions"},
        max_observation_bytes=128,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "ghidra_version": "11.3",
                    "plugin_version": "0.4",
                    "data": {"functions": [{"name": f"function_{index}"} for index in range(100)]},
                },
            )
        ),
    )
    try:
        observation = await truncated.list_functions(SAMPLE_SHA256)
    finally:
        await truncated.aclose()

    assert observation.truncated is True
    assert len(json.dumps(observation.data).encode()) <= 128
