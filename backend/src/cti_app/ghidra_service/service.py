from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from cti_app.ghidra_service.contract import CONTRACT_VERSION, AnalysisRequest, ContractError

_MAX_RESULT_ITEMS = 100
_MAX_RESULT_STRING_CHARS = 16_384


class ReadOnlyEngine(Protocol):
    ghidra_version: str
    java_version: str

    def analyze(
        self,
        sample_path: Path,
        *,
        operation: str,
        address: str | None,
        query: str | None,
        limit: int,
        workspace: Path,
    ) -> Mapping[str, Any]: ...


class GhidraReadOnlyService:
    def __init__(
        self,
        engine: ReadOnlyEngine,
        *,
        expected_version: str,
        expected_java_version: str = "21.0.12.1",
        maximum_sample_bytes: int = 200 * 1024 * 1024,
        maximum_response_bytes: int = 64 * 1024,
        timeout_seconds: int = 120,
        workspace_root: Path = Path("/work"),
    ) -> None:
        if not expected_version.strip() or not expected_java_version.strip():
            raise ValueError("Ghidra and Java versions must be pinned")
        if maximum_sample_bytes < 1 or maximum_response_bytes < 256:
            raise ValueError("Ghidra service limits must be positive")
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("Ghidra timeout must be between 1 and 600 seconds")
        self.engine = engine
        self.expected_version = expected_version
        self.expected_java_version = expected_java_version
        self.maximum_sample_bytes = maximum_sample_bytes
        self.maximum_response_bytes = maximum_response_bytes
        self.timeout_seconds = timeout_seconds
        self.workspace_root = workspace_root

    def probe(self) -> dict[str, object]:
        ghidra_ok = self.engine.ghidra_version == self.expected_version
        java_ok = self.engine.java_version == self.expected_java_version
        reason = None
        if not ghidra_ok:
            reason = "ghidra_version_mismatch"
        elif not java_ok:
            reason = "ghidra_java_version_mismatch"
        return {
            "contract_version": CONTRACT_VERSION,
            "ghidra_version": self.engine.ghidra_version,
            "java_version": self.engine.java_version,
            "status": "ok" if reason is None else "unavailable",
            "available_reason": reason,
        }

    def analyze(self, request: AnalysisRequest, payload: bytes) -> dict[str, object]:
        attestation = self.probe()
        if attestation["status"] != "ok":
            raise ContractError("ghidra_attestation_failed", status_code=503)
        if not payload:
            raise ContractError("sample_empty")
        if len(payload) > self.maximum_sample_bytes:
            raise ContractError("sample_too_large", status_code=413)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != request.sample_sha256:
            raise ContractError("sample_sha256_mismatch", status_code=422)

        with tempfile.TemporaryDirectory(prefix="ghidra-session-", dir=self.workspace_root) as name:
            workspace = Path(name)
            sample_path = workspace / "sample.bin"
            sample_path.write_bytes(payload)
            result = self.engine.analyze(
                sample_path,
                operation=request.operation,
                address=request.address,
                query=request.query,
                limit=request.limit,
                workspace=workspace,
            )
            data, truncated = _bounded_result(result, self.maximum_response_bytes)
        return {
            "schema_version": "ghidra-service-result.v1",
            "contract_version": CONTRACT_VERSION,
            "ghidra_version": self.engine.ghidra_version,
            "java_version": self.engine.java_version,
            "operation": request.operation,
            "sample_sha256": actual_sha256,
            "data": data,
            "truncated": truncated,
        }


def _bounded_result(value: Mapping[str, Any], max_bytes: int) -> tuple[dict[str, Any], bool]:
    truncated = False

    def normalize(item: Any, depth: int = 0) -> Any:
        nonlocal truncated
        if depth > 12:
            truncated = True
            return "[depth-limit]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if item == item and abs(item) != float("inf") else None
        if isinstance(item, str):
            if len(item) > _MAX_RESULT_STRING_CHARS:
                truncated = True
                return item[:_MAX_RESULT_STRING_CHARS]
            return item
        if isinstance(item, Mapping):
            keys = sorted(key for key in item if isinstance(key, str))[:_MAX_RESULT_ITEMS]
            if len(keys) != len(item):
                truncated = True
            return {key: normalize(item[key], depth + 1) for key in keys}
        if isinstance(item, (list, tuple)):
            values = list(item)
            if len(values) > _MAX_RESULT_ITEMS:
                truncated = True
                values = values[:_MAX_RESULT_ITEMS]
            return [normalize(child, depth + 1) for child in values]
        truncated = True
        return "[unsupported-value]"

    normalized = normalize(value)
    if not isinstance(normalized, dict):
        normalized = {"result": normalized}
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > max_bytes:
        normalized = {"result": "[output-limit]"}
        truncated = True
    return normalized, truncated
