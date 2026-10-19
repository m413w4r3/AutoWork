from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from cti_app.ghidra_service.contract import CONTRACT_VERSION, ContractError, parse_analysis_request
from cti_app.ghidra_service.server import create_server
from cti_app.ghidra_service.service import GhidraReadOnlyService

SAMPLE = b"canonical sample for in-process contract tests"
SAMPLE_SHA256 = hashlib.sha256(SAMPLE).hexdigest()


class FakeEngine:
    ghidra_version = "12.1.4"
    java_version = "21.0.12.1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def analyze(
        self,
        sample_path: Path,
        *,
        operation: str,
        address: str | None,
        query: str | None,
        limit: int,
        workspace: Path,
    ) -> Mapping[str, Any]:
        assert sample_path.parent == workspace
        payload = sample_path.read_bytes()
        self.calls.append((operation, payload))
        return {
            "functions": [{"name": "entry", "address": "0x400078"}],
            "request": {"address": address, "query": query, "limit": limit},
        }


@pytest.fixture
def running_service(tmp_path: Path):
    engine = FakeEngine()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = GhidraReadOnlyService(
        engine,
        expected_version="12.1.4",
        expected_java_version="21.0.12.1",
        maximum_sample_bytes=64,
        maximum_response_bytes=1024,
        workspace_root=workspace,
    )
    server = create_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            yield client, engine, workspace
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _headers(sample: bytes = SAMPLE) -> dict[str, str]:
    return {
        "Content-Type": "application/octet-stream",
        "X-Ghidra-Contract": CONTRACT_VERSION,
        "X-Ghidra-SHA256": hashlib.sha256(sample).hexdigest(),
    }


def test_in_process_service_attests_and_analyzes_then_purges_workspace(running_service) -> None:
    client, engine, workspace = running_service

    probe = client.get("/v1/probe")
    response = client.post(
        "/v1/analyze?operation=list_functions&limit=10",
        content=SAMPLE,
        headers=_headers(),
    )

    assert probe.status_code == 200
    assert probe.json() == {
        "contract_version": CONTRACT_VERSION,
        "ghidra_version": "12.1.4",
        "java_version": "21.0.12.1",
        "status": "ok",
        "available_reason": None,
    }
    assert response.status_code == 200
    assert response.json()["sample_sha256"] == SAMPLE_SHA256
    assert response.json()["operation"] == "list_functions"
    assert response.json()["data"]["functions"] == [{"name": "entry", "address": "0x400078"}]
    assert engine.calls == [("list_functions", SAMPLE)]
    assert list(workspace.iterdir()) == []


def test_contract_refuses_incorrect_hash_before_engine_invocation(running_service) -> None:
    client, engine, _workspace = running_service
    response = client.post(
        "/v1/analyze?operation=list_functions",
        content=SAMPLE,
        headers=_headers(b"different sample"),
    )

    assert response.status_code == 422
    assert response.json() == {"error": "sample_sha256_mismatch"}
    assert engine.calls == []


def test_contract_refuses_sample_over_limit_before_engine_invocation(running_service) -> None:
    client, engine, _workspace = running_service
    response = client.post(
        "/v1/analyze?operation=list_functions",
        content=b"x" * 65,
        headers=_headers(b"x" * 65),
    )

    assert response.status_code == 413
    assert response.json() == {"error": "sample_too_large"}
    assert engine.calls == []


def test_decompile_contract_only_accepts_a_single_function_limit() -> None:
    headers = _headers()

    request = parse_analysis_request(
        "/v1/analyze?operation=decompile_function&address=402068", headers
    )

    assert request.limit == 1
    with pytest.raises(ContractError, match="limit_out_of_bounds"):
        parse_analysis_request(
            "/v1/analyze?operation=decompile_function&address=402068&limit=2", headers
        )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/analyze?operation=rename_function",
        "/v1/analyze?operation=write_program",
        "/v1/analyze?operation=list_functions&unexpected=value",
        "/v1/analyze?operation",
    ],
)
def test_contract_refuses_unknown_write_and_malformed_operations(
    running_service, path: str
) -> None:
    client, engine, _workspace = running_service
    response = client.post(path, content=SAMPLE, headers=_headers())

    assert response.status_code == 400
    assert engine.calls == []


def test_contract_refuses_http_write_methods(running_service) -> None:
    client, engine, _workspace = running_service

    response = client.put("/v1/analyze", content=SAMPLE)

    assert response.status_code == 405
    assert response.json() == {"error": "read_only_contract"}
    assert engine.calls == []
