from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from typing import Any, Protocol

from cti_app.domain.native_analysis import (
    NativeAnalysisObservationV1,
    NativeAnalysisOperation,
    NativeAnalysisProvider,
    NativeAnalysisRequestV1,
    NativeSearchKind,
    NativeValueType,
)

NATIVE_ANALYSIS_CATALOG_VERSION = "autowork-native-read.v1"
NATIVE_ANALYSIS_CAPABILITY = "native.inspect"
MAX_NATIVE_RESULT_ITEMS = 100
MAX_NATIVE_OUTPUT_BYTES = 32 * 1024

NATIVE_READ_OPERATIONS = frozenset(NativeAnalysisOperation)


class NativeAnalysisError(RuntimeError):
    code = "native_analysis_error"


class NativeAnalysisUnavailable(NativeAnalysisError):
    code = "native_analysis_unavailable"


class NativeAnalysisPolicyDenied(NativeAnalysisError):
    code = "native_analysis_policy_denied"


class NativeAnalysisTimeout(NativeAnalysisError):
    code = "native_analysis_timeout"


class NativeAnalysisResponseError(NativeAnalysisError):
    code = "native_analysis_response_invalid"


class NativeAnalysisVersionMismatch(NativeAnalysisError):
    code = "native_analysis_version_mismatch"


class NativeAnalysisPort(Protocol):
    """Idempotent, read-only boundary for one versioned native-analysis request."""

    @property
    def provider(self) -> NativeAnalysisProvider: ...

    @property
    def available(self) -> bool: ...

    async def inspect(self, request: NativeAnalysisRequestV1) -> NativeAnalysisObservationV1: ...


class UnavailableNativeAnalysisPort:
    def __init__(self, provider: NativeAnalysisProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> NativeAnalysisProvider:
        return self._provider

    @property
    def available(self) -> bool:
        return False

    async def inspect(self, request: NativeAnalysisRequestV1) -> NativeAnalysisObservationV1:
        del request
        raise NativeAnalysisUnavailable(f"provider_unavailable:{self._provider.value}")


def select_native_analysis_port(
    provider: NativeAnalysisProvider | str,
    *,
    rea: NativeAnalysisPort | None,
    ghidra_mcp: NativeAnalysisPort | None,
) -> NativeAnalysisPort:
    """Resolve the configured provider without changing other investigation tools."""
    try:
        selected = NativeAnalysisProvider(provider)
    except (TypeError, ValueError):
        raise ValueError("native analysis provider must be rea, ghidra_mcp, or none") from None
    if selected is NativeAnalysisProvider.REA:
        return (
            rea
            if rea is not None and rea.provider is selected
            else UnavailableNativeAnalysisPort(selected)
        )
    if selected is NativeAnalysisProvider.GHIDRA_MCP:
        return (
            ghidra_mcp
            if ghidra_mcp is not None and ghidra_mcp.provider is selected
            else UnavailableNativeAnalysisPort(selected)
        )
    return UnavailableNativeAnalysisPort(selected)


def build_native_analysis_request(arguments: Mapping[str, Any]) -> NativeAnalysisRequestV1:
    allowed = {
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
    if set(arguments) - allowed:
        raise NativeAnalysisPolicyDenied("native_analysis_unknown_input")
    if not isinstance(arguments.get("sample_sha256"), str):
        raise NativeAnalysisPolicyDenied("native_analysis_sample_sha256_required")
    raw_operation = arguments.get("operation")
    if not isinstance(raw_operation, str):
        raise NativeAnalysisPolicyDenied("native_analysis_operation_not_allowlisted")
    try:
        operation = NativeAnalysisOperation(raw_operation)
    except (TypeError, ValueError):
        raise NativeAnalysisPolicyDenied("native_analysis_operation_not_allowlisted") from None

    try:
        search_kind_value = arguments.get("search_kind")
        search_kind = NativeSearchKind(search_kind_value) if search_kind_value is not None else None
        value_type_value = arguments.get("value_type")
        value_type = NativeValueType(value_type_value) if value_type_value is not None else None
    except (TypeError, ValueError):
        raise NativeAnalysisPolicyDenied("native_analysis_input_enum_not_allowlisted") from None

    for key in ("address", "symbol", "query"):
        value = arguments.get(key)
        if value is not None and not isinstance(value, str):
            raise NativeAnalysisPolicyDenied(f"native_analysis_{key}_invalid")
    limit = arguments.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise NativeAnalysisPolicyDenied("native_analysis_limit_invalid")
    size = arguments.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        raise NativeAnalysisPolicyDenied("native_analysis_size_invalid")

    try:
        return NativeAnalysisRequestV1(
            sample_sha256=arguments["sample_sha256"],
            operation=operation,
            address=arguments.get("address"),
            symbol=arguments.get("symbol"),
            search_kind=search_kind,
            query=arguments.get("query"),
            value_type=value_type,
            size=size,
            limit=limit,
        )
    except (TypeError, ValueError) as exc:
        raise NativeAnalysisPolicyDenied(f"native_analysis_request_invalid:{exc}") from None


class NativeAnalysisService:
    def __init__(
        self,
        port: NativeAnalysisPort | None,
        *,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = MAX_NATIVE_OUTPUT_BYTES,
        max_result_items: int = MAX_NATIVE_RESULT_ITEMS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_bytes < 256:
            raise ValueError("max_output_bytes must be at least 256")
        if not 1 <= max_result_items <= MAX_NATIVE_RESULT_ITEMS:
            raise ValueError("max_result_items must be between 1 and 100")
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._max_result_items = max_result_items

    async def inspect(self, request: NativeAnalysisRequestV1) -> NativeAnalysisObservationV1:
        if self._port is None or not self._port.available:
            provider = self._port.provider.value if self._port is not None else "none"
            raise NativeAnalysisUnavailable(f"provider_unavailable:{provider}")
        try:
            result = await asyncio.wait_for(
                self._port.inspect(request), timeout=self._timeout_seconds
            )
        except TimeoutError:
            raise NativeAnalysisTimeout("native_analysis_timeout") from None
        if (
            result.sample_sha256 != request.sample_sha256
            or result.operation is not request.operation
            or result.request_fingerprint != request.fingerprint
            or result.provider is not self._port.provider
        ):
            raise NativeAnalysisResponseError("native_analysis_response_identity_mismatch")
        data, truncated = normalize_native_data(
            result.data,
            max_output_bytes=self._max_output_bytes,
            max_items=min(self._max_result_items, request.limit),
        )
        return NativeAnalysisObservationV1(
            sample_sha256=result.sample_sha256,
            operation=result.operation,
            request_fingerprint=result.request_fingerprint,
            provider=result.provider,
            provider_version=result.provider_version,
            provider_plugin_version=result.provider_plugin_version,
            catalog_version=result.catalog_version,
            data=data,
            truncated=result.truncated or truncated,
        )


def normalize_native_data(
    value: Mapping[str, Any],
    *,
    max_output_bytes: int = MAX_NATIVE_OUTPUT_BYTES,
    max_items: int = MAX_NATIVE_RESULT_ITEMS,
) -> tuple[dict[str, Any], bool]:
    """Normalize provider output into a bounded, stable JSON object."""
    if not isinstance(value, Mapping):
        raise NativeAnalysisResponseError("native_analysis_result_must_be_object")
    if max_output_bytes < 64 or not 1 <= max_items <= MAX_NATIVE_RESULT_ITEMS:
        raise ValueError("invalid native result bounds")
    truncated = False

    def normalize(item: Any, depth: int = 0) -> Any:
        nonlocal truncated
        if depth > 16:
            truncated = True
            return "[depth-limit]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                truncated = True
                return None
            return item
        if isinstance(item, str):
            if len(item) > 16_384:
                truncated = True
                return item[:16_384]
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            keys = sorted(key for key in item if isinstance(key, str))
            if len(keys) > MAX_NATIVE_RESULT_ITEMS:
                keys = keys[:MAX_NATIVE_RESULT_ITEMS]
                truncated = True
            for key in keys:
                result[key] = normalize(item[key], depth + 1)
            if len(result) != len(item):
                truncated = True
            return result
        if isinstance(item, (list, tuple)):
            items = [normalize(child, depth + 1) for child in item]
            items.sort(key=_canonical_json)
            if len(items) > max_items:
                items = items[:max_items]
                truncated = True
            return items
        raise NativeAnalysisResponseError("native_analysis_result_contains_non_json_value")

    data = normalize(value)
    if not isinstance(data, dict):
        raise NativeAnalysisResponseError("native_analysis_result_must_be_object")
    while len(_canonical_json(data).encode("utf-8")) > max_output_bytes:
        if not _shrink_once(data):
            data = {"result": "[output-limit]"}
            truncated = True
            break
        truncated = True
    return data, truncated


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _shrink_once(value: dict[str, Any]) -> bool:
    candidates: list[tuple[int, int, tuple[str, ...], Any, str]] = []

    def collect(item: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                collect(item[key], (*path, key))
            if len(item) > 1:
                candidates.append(
                    (
                        sum(len(_canonical_json(child)) for child in item.values()),
                        0,
                        path,
                        item,
                        "mapping",
                    )
                )
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, (*path, str(index)))
            if item:
                candidates.append(
                    (sum(len(_canonical_json(child)) for child in item), 2, path, item, "list")
                )
        elif isinstance(item, str) and len(item) > 1:
            candidates.append((len(item), 3, path, item, "string"))

    collect(value)
    if not candidates:
        return False
    _size, _priority, _path, target, kind = max(
        candidates, key=lambda candidate: (candidate[0], candidate[1], candidate[2])
    )
    if kind == "list":
        target.pop()
    elif kind == "string":
        parent: Any = value
        for key in _path[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = _path[-1]
        shortened = target[: len(target) // 2]
        if isinstance(parent, list):
            parent[int(key)] = shortened
        else:
            parent[key] = shortened
    else:
        target.pop(sorted(target)[-1])
    return True
