from __future__ import annotations

import hashlib
import json
from datetime import UTC

import httpx
import pytest

from cti_app.application.local_analysis import (
    GhidraPolicyDeniedError,
    GhidraResponseError,
    GhidraResponseTooLargeError,
    GhidraUnavailableError,
    GhidraVersionMismatchError,
)
from cti_app.config import Settings
from cti_app.domain.local_analysis import GhidraOperation
from cti_app.ghidra_service.contract import CONTRACT_VERSION
from cti_app.infrastructure.ghidra_mcp import (
    GhidraMcpHttpAdapter,
    create_ghidra_mcp_http_adapter,
)

SAMPLE = b"verified-canonical-sample"
SAMPLE_SHA256 = hashlib.sha256(SAMPLE).hexdigest()


def _probe(
    *,
    ghidra_version: str = "12.1.4",
    java_version: str = "21.0.12.1",
    status: str = "ok",
) -> dict[str, object]:
    return {
        "contract_version": CONTRACT_VERSION,
        "ghidra_version": ghidra_version,
        "java_version": java_version,
        "status": status,
        "available_reason": None,
    }


def _result(operation: str, data: object, *, sha256: str = SAMPLE_SHA256) -> dict[str, object]:
    return {
        "schema_version": "ghidra-service-result.v1",
        "contract_version": CONTRACT_VERSION,
        "ghidra_version": "12.1.4",
        "java_version": "21.0.12.1",
        "operation": operation,
        "sample_sha256": sha256,
        "data": data,
        "truncated": False,
    }


def _adapter(
    handler: httpx.MockTransport,
    *,
    loader: bytes = SAMPLE,
    **kwargs: object,
) -> GhidraMcpHttpAdapter:
    async def read_sample(sample_sha256: str) -> bytes:
        assert sample_sha256 == SAMPLE_SHA256
        return loader

    return GhidraMcpHttpAdapter(
        enabled=True,
        expected_version="12.1.4",
        sample_bytes_loader=read_sample,
        transport=handler,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_client_attests_and_sends_verified_canonical_bytes_to_fixed_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_probe())
        return httpx.Response(
            200,
            json=_result(
                "list_functions",
                {
                    "functions": [{"name": "entry", "address": "0x401000"}],
                    "raw_bytes": "must-not-be-kept",
                    "note": "https://example.invalid/sample?token=secret",
                },
            ),
        )

    adapter = _adapter(httpx.MockTransport(handler))
    try:
        assert adapter.available is False
        assert await adapter.probe() is True
        assert adapter.available is True
        observation = await adapter.list_functions(SAMPLE_SHA256, limit=10)
    finally:
        await adapter.aclose()

    assert observation.operation is GhidraOperation.FUNCTIONS
    assert observation.sample_sha256 == SAMPLE_SHA256
    assert observation.ghidra_version == "12.1.4"
    assert observation.plugin_version == CONTRACT_VERSION
    assert observation.address is None
    assert observation.observed_at.tzinfo is UTC
    assert observation.data["functions"] == [{"name": "entry", "address": "0x401000"}]
    assert "raw_bytes" not in observation.data
    assert "secret" not in json.dumps(observation.data)
    assert requests[0].method == "GET" and requests[0].url.path == "/v1/probe"
    assert requests[1].method == "POST" and requests[1].url.path == "/v1/analyze"
    assert requests[1].content == SAMPLE
    assert requests[1].headers["X-Ghidra-SHA256"] == SAMPLE_SHA256
    assert requests[1].headers["X-Ghidra-Contract"] == CONTRACT_VERSION
    assert requests[1].url.params["operation"] == "list_functions"
    assert requests[1].url.params["limit"] == "10"


@pytest.mark.asyncio
async def test_address_operations_use_contract_operation_and_normalized_address() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_probe())
        return httpx.Response(
            200,
            json=_result(
                "decompile_function",
                {"function": {"name": "entry", "decompilation": "int entry(void) { return 0; }"}},
            ),
        )

    adapter = _adapter(httpx.MockTransport(handler))
    try:
        await adapter.probe()
        observation = await adapter.decompile_function(SAMPLE_SHA256, "0x00401000")
    finally:
        await adapter.aclose()

    assert observation.address == "0x401000"
    assert seen[1].url.params["operation"] == "decompile_function"
    assert seen[1].url.params["address"] == "0x401000"


@pytest.mark.asyncio
async def test_bad_local_sample_hash_is_refused_before_http_analysis() -> None:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_probe())

    adapter = _adapter(httpx.MockTransport(record), loader=b"wrong bytes")
    try:
        await adapter.probe()
        with pytest.raises(GhidraResponseError, match="hash_mismatch"):
            await adapter.list_functions(SAMPLE_SHA256)
    finally:
        await adapter.aclose()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/probe"


@pytest.mark.asyncio
async def test_write_operation_and_unknown_operation_are_refused_without_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_probe())

    adapter = _adapter(httpx.MockTransport(handler))
    try:
        await adapter.probe()
        with pytest.raises(GhidraPolicyDeniedError):
            await adapter.analyze("rename_function", SAMPLE_SHA256)
        with pytest.raises(GhidraPolicyDeniedError):
            await adapter.analyze("set_comment", SAMPLE_SHA256)
    finally:
        await adapter.aclose()

    assert [request.url.path for request in requests] == ["/v1/probe"]


def test_client_only_accepts_loopback_or_compose_service_host() -> None:
    with pytest.raises(GhidraPolicyDeniedError):
        GhidraMcpHttpAdapter(base_url="http://ghidra.internal:8787", enabled=True)
    with pytest.raises(GhidraPolicyDeniedError):
        GhidraMcpHttpAdapter(base_url="https://ghidra:8787", enabled=True)


@pytest.mark.asyncio
async def test_disabled_or_unreachable_service_has_an_exact_unavailable_reason() -> None:
    defaults = Settings(_env_file=None)
    disabled = create_ghidra_mcp_http_adapter(
        defaults,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_probe())),
    )
    try:
        assert disabled.available is False
        assert disabled.available_reason == "ghidra_disabled_by_configuration"
        assert await disabled.probe() is False
        with pytest.raises(GhidraUnavailableError):
            await disabled.list_functions(SAMPLE_SHA256)
    finally:
        await disabled.aclose()

    unreachable = _adapter(httpx.MockTransport(lambda request: httpx.Response(503, json={})))
    try:
        assert await unreachable.probe() is False
        assert unreachable.available_reason == "ghidra_attestation_invalid"
    finally:
        await unreachable.aclose()


@pytest.mark.asyncio
async def test_version_mismatch_is_not_attested_and_response_version_is_rechecked() -> None:
    mismatch = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(
                503, json=_probe(ghidra_version="12.1.3", status="unavailable")
            )
        )
    )
    try:
        assert await mismatch.probe() is False
        assert mismatch.available_reason == "ghidra_attestation_version_mismatch"
    finally:
        await mismatch.aclose()

    java_mismatch = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json=_probe(java_version="21.0.11", status="unavailable"),
            )
        )
    )
    try:
        assert await java_mismatch.probe() is False
        assert java_mismatch.available_reason == "ghidra_attestation_version_mismatch"
    finally:
        await java_mismatch.aclose()

    requests = 0

    def changed_version(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json=_probe())
        payload = _result("list_functions", {})
        payload["ghidra_version"] = "12.1.3"
        return httpx.Response(200, json=payload)

    adapter = _adapter(httpx.MockTransport(changed_version))
    try:
        await adapter.probe()
        with pytest.raises(GhidraVersionMismatchError):
            await adapter.list_functions(SAMPLE_SHA256)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_redirect_oversized_and_truncated_results_are_bounded() -> None:
    redirect = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/v1/probe"})
        )
    )
    try:
        assert await redirect.probe() is False
        assert redirect.available_reason == "ghidra_attestation_invalid"
    finally:
        await redirect.aclose()

    requests = 0

    def oversized_response(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json=_probe())
        return httpx.Response(200, content=b"x" * 64)

    oversized = _adapter(
        httpx.MockTransport(oversized_response),
        max_response_bytes=32,
    )
    try:
        await oversized.probe()
        with pytest.raises(GhidraResponseTooLargeError):
            await oversized.list_functions(SAMPLE_SHA256)
    finally:
        await oversized.aclose()

    requests = 0

    def large_result(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json=_probe())
        return httpx.Response(
            200,
            json=_result(
                "list_functions",
                {"functions": [{"name": f"function_{index}"} for index in range(100)]},
            ),
        )

    truncated = _adapter(
        httpx.MockTransport(large_result),
        max_observation_bytes=128,
    )
    try:
        await truncated.probe()
        observation = await truncated.list_functions(SAMPLE_SHA256)
    finally:
        await truncated.aclose()

    assert observation.truncated is True
    assert len(json.dumps(observation.data).encode()) <= 128
