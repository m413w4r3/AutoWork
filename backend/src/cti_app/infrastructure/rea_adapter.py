"""Read-only adapters for REA and the historical Ghidra MCP connector.

The REA client contract below belongs to AutoWork. It deliberately does not claim
to implement an unverified REA CLI syntax or MCP endpoint. A production client
must translate this request inside a dedicated, network-isolated worker with a
read-only sample mount; REA itself is not a sandbox.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cti_app.application.local_analysis import (
    GhidraPolicyDeniedError,
    GhidraResponseError,
    GhidraResponseTooLargeError,
    GhidraUnavailableError,
    GhidraVersionMismatchError,
)
from cti_app.application.native_analysis import (
    NATIVE_ANALYSIS_CATALOG_VERSION,
    NATIVE_READ_OPERATIONS,
    NativeAnalysisPolicyDenied,
    NativeAnalysisPort,
    NativeAnalysisResponseError,
    NativeAnalysisRunnerNotConfigured,
    NativeAnalysisTimeout,
    NativeAnalysisUnavailable,
    NativeAnalysisVersionMismatch,
    UnavailableNativeAnalysisPort,
    normalize_native_data,
)
from cti_app.config import Settings
from cti_app.domain.local_analysis import GhidraOperation
from cti_app.domain.native_analysis import (
    NativeAnalysisObservationV1,
    NativeAnalysisOperation,
    NativeAnalysisProvider,
    NativeAnalysisRequestV1,
)
from cti_app.infrastructure.ghidra_mcp import (
    GhidraMcpHttpAdapter,
    create_ghidra_mcp_http_adapter,
)

REA_READ_ONLY_CATALOG = NATIVE_READ_OPERATIONS
REA_READ_ONLY_CATALOG_VERSION = NATIVE_ANALYSIS_CATALOG_VERSION


class NativeAnalysisResponseTooLarge(NativeAnalysisResponseError):
    code = "response_too_large"


@dataclass(frozen=True, slots=True)
class ReaReadOnlyRequestV1:
    """AutoWork-owned request contract passed to a controlled REA client."""

    catalog_version: str
    request: NativeAnalysisRequestV1

    def as_json(self) -> dict[str, Any]:
        return {
            **self.request.as_json(),
            "schema_version": "autowork-rea-read-request.v1",
            "catalog_version": self.catalog_version,
        }


class ReaReadOnlyRunner(Protocol):
    """A controlled CLI/MCP client hosted by a dedicated confined worker."""

    @property
    def available(self) -> bool: ...

    @property
    def worker_confined(self) -> bool: ...

    @property
    def dedicated_user(self) -> bool: ...

    @property
    def network_disabled(self) -> bool: ...

    @property
    def filesystem_confined(self) -> bool: ...

    @property
    def no_host_privileges(self) -> bool: ...

    async def inspect(
        self,
        request: ReaReadOnlyRequestV1,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]: ...


class ReaNativeAnalysisAdapter(NativeAnalysisPort):
    """Translate the closed AutoWork catalog through an injected REA read client."""

    def __init__(
        self,
        runner: ReaReadOnlyRunner | None,
        *,
        enabled: bool = False,
        expected_version: str | None = None,
        catalog_version: str = REA_READ_ONLY_CATALOG_VERSION,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 64 * 1024,
        max_output_bytes: int = 32 * 1024,
        max_result_items: int = 100,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes < 256:
            raise ValueError("max_response_bytes must be at least 256")
        if not 1 <= len(catalog_version.strip()) <= 128:
            raise ValueError("catalog_version must be explicitly pinned and bounded")
        if max_output_bytes < 64:
            raise ValueError("max_output_bytes must be at least 64")
        if not 1 <= max_result_items <= 100:
            raise ValueError("max_result_items must be between 1 and 100")
        if expected_version is not None and not expected_version.strip():
            raise ValueError("expected_version must be non-empty when supplied")
        if expected_version is not None and len(expected_version) > 128:
            raise ValueError("expected_version must not exceed 128 characters")
        self._runner = runner
        self._enabled = enabled
        self._expected_version = expected_version
        self._catalog_version = catalog_version
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_output_bytes = max_output_bytes
        self._max_result_items = max_result_items

    @property
    def provider(self) -> NativeAnalysisProvider:
        return NativeAnalysisProvider.REA

    @property
    def available(self) -> bool:
        runner = self._runner
        return bool(
            self._enabled
            and self._expected_version
            and runner is not None
            and runner.available
            and runner.worker_confined
            and runner.dedicated_user
            and runner.network_disabled
            and runner.filesystem_confined
            and runner.no_host_privileges
        )

    async def inspect(self, request: NativeAnalysisRequestV1) -> NativeAnalysisObservationV1:
        if request.operation not in REA_READ_ONLY_CATALOG:
            raise NativeAnalysisPolicyDenied("rea_operation_not_in_pinned_read_catalog")
        runner = self._runner
        if not self.available or runner is None:
            raise NativeAnalysisUnavailable("rea_provider_or_confined_worker_unavailable")
        contract_request = ReaReadOnlyRequestV1(self._catalog_version, request)
        try:
            response = await asyncio.wait_for(
                runner.inspect(
                    contract_request,
                    timeout_seconds=self._timeout_seconds,
                    max_response_bytes=self._max_response_bytes,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            raise NativeAnalysisTimeout("rea_read_operation_timed_out") from None
        except NativeAnalysisTimeout:
            raise
        except Exception as exc:
            raise NativeAnalysisUnavailable(
                f"rea_read_client_failed:{type(exc).__name__}"
            ) from None

        _enforce_response_size(response, self._max_response_bytes)
        parsed = _parse_rea_response(response, request)
        provider_version = parsed["provider_version"]
        catalog_version = parsed["catalog_version"]
        if provider_version != self._expected_version:
            raise NativeAnalysisVersionMismatch("rea_version_does_not_match_configured_pin")
        if catalog_version != self._catalog_version:
            raise NativeAnalysisVersionMismatch("rea_catalog_does_not_match_configured_pin")
        data, truncated = normalize_native_data(
            parsed["data"],
            max_output_bytes=self._max_output_bytes,
            max_items=min(self._max_result_items, request.limit),
        )
        return NativeAnalysisObservationV1(
            sample_sha256=request.sample_sha256,
            operation=request.operation,
            request_fingerprint=request.fingerprint,
            provider=self.provider,
            provider_version=provider_version,
            catalog_version=catalog_version,
            data=data,
            truncated=truncated,
        )


def _enforce_response_size(response: Mapping[str, Any], max_response_bytes: int) -> None:
    """Reject an oversized serialized runner result before parsing or normalization."""
    try:
        serialized = json.dumps(
            dict(response),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise NativeAnalysisResponseError("rea_response_contract_invalid") from None
    if len(serialized) > max_response_bytes:
        raise NativeAnalysisResponseTooLarge("rea_response_exceeds_configured_byte_limit")


def _parse_rea_response(
    response: Mapping[str, Any], request: NativeAnalysisRequestV1
) -> dict[str, Any]:
    required = {
        "schema_version",
        "provider_version",
        "catalog_version",
        "sample_sha256",
        "operation",
        "request_fingerprint",
        "data",
    }
    if not isinstance(response, Mapping) or set(response) != required:
        raise NativeAnalysisResponseError("rea_response_contract_invalid")
    if response.get("schema_version") != "autowork-rea-read-response.v1":
        raise NativeAnalysisResponseError("rea_response_schema_invalid")
    if (
        response.get("sample_sha256") != request.sample_sha256
        or response.get("operation") != request.operation.value
        or response.get("request_fingerprint") != request.fingerprint
    ):
        raise NativeAnalysisResponseError("rea_response_identity_mismatch")
    if not isinstance(response.get("provider_version"), str) or not isinstance(
        response.get("catalog_version"), str
    ):
        raise NativeAnalysisResponseError("rea_response_version_missing")
    if not isinstance(response.get("data"), Mapping):
        raise NativeAnalysisResponseError("rea_response_data_invalid")
    return dict(response)


class GhidraMcpNativeAnalysisAdapter(NativeAnalysisPort):
    """Versioned native-analysis wrapper around the isolated Ghidra service.

    Unsupported catalog entries remain unavailable; the wrapped client owns the
    service attestation and the verified canonical-byte transfer.
    """

    _OPERATIONS: Mapping[NativeAnalysisOperation, GhidraOperation] = {
        NativeAnalysisOperation.LIST_FUNCTIONS: GhidraOperation.FUNCTIONS,
        NativeAnalysisOperation.IMPORTS: GhidraOperation.IMPORTS,
        NativeAnalysisOperation.XREFS_TO: GhidraOperation.XREF_TO,
        NativeAnalysisOperation.CALLERS: GhidraOperation.XREF_TO,
        NativeAnalysisOperation.XREFS_FROM: GhidraOperation.XREF_FROM,
        NativeAnalysisOperation.CALLEES: GhidraOperation.XREF_FROM,
        NativeAnalysisOperation.DECOMPILE: GhidraOperation.DECOMPILE_FUNCTION,
        NativeAnalysisOperation.SEARCH: GhidraOperation.SEARCH,
    }

    def __init__(
        self,
        adapter: GhidraMcpHttpAdapter | None,
        *,
        enabled: bool = False,
        catalog_version: str = NATIVE_ANALYSIS_CATALOG_VERSION,
        max_output_bytes: int = 32 * 1024,
        max_result_items: int = 100,
    ) -> None:
        if not catalog_version.strip():
            raise ValueError("catalog_version must be explicitly pinned")
        if len(catalog_version) > 128:
            raise ValueError("catalog_version must not exceed 128 characters")
        if max_output_bytes < 64:
            raise ValueError("max_output_bytes must be at least 64")
        if not 1 <= max_result_items <= 100:
            raise ValueError("max_result_items must be between 1 and 100")
        self._adapter = adapter
        self._enabled = enabled
        self._catalog_version = catalog_version
        self._max_output_bytes = max_output_bytes
        self._max_result_items = max_result_items

    @property
    def provider(self) -> NativeAnalysisProvider:
        return NativeAnalysisProvider.GHIDRA_MCP

    @property
    def available(self) -> bool:
        return bool(self._enabled and self._adapter is not None and self._adapter.available)

    async def inspect(self, request: NativeAnalysisRequestV1) -> NativeAnalysisObservationV1:
        adapter = self._adapter
        if not self.available or adapter is None:
            raise NativeAnalysisUnavailable("ghidra_mcp_provider_unavailable")
        operation = self._OPERATIONS.get(request.operation)
        if operation is None:
            raise NativeAnalysisUnavailable(
                f"ghidra_mcp_does_not_support:{request.operation.value}"
            )
        address = request.address
        query = request.query
        if request.operation is NativeAnalysisOperation.GET_FUNCTION:
            raise NativeAnalysisUnavailable("ghidra_mcp_get_function_not_registered")
        if (
            request.operation
            in {
                NativeAnalysisOperation.XREFS_TO,
                NativeAnalysisOperation.XREFS_FROM,
                NativeAnalysisOperation.CALLERS,
                NativeAnalysisOperation.CALLEES,
                NativeAnalysisOperation.DECOMPILE,
            }
            and address is None
        ):
            raise NativeAnalysisPolicyDenied("ghidra_mcp_address_required")
        if request.operation is NativeAnalysisOperation.SEARCH and query is None:
            raise NativeAnalysisPolicyDenied("ghidra_mcp_search_query_required")
        try:
            observation = await adapter.analyze(
                operation,
                request.sample_sha256,
                address=address,
                query=query,
                limit=request.limit,
            )
        except GhidraPolicyDeniedError:
            raise NativeAnalysisPolicyDenied("ghidra_mcp_read_policy_denied") from None
        except (GhidraUnavailableError, GhidraVersionMismatchError):
            raise NativeAnalysisUnavailable("ghidra_mcp_read_unavailable") from None
        except GhidraResponseTooLargeError:
            raise NativeAnalysisResponseError("ghidra_mcp_response_too_large") from None
        except GhidraResponseError:
            raise NativeAnalysisResponseError("ghidra_mcp_response_invalid") from None
        except TimeoutError:
            raise NativeAnalysisTimeout("ghidra_mcp_read_timed_out") from None
        data, truncated = normalize_native_data(
            observation.data,
            max_output_bytes=self._max_output_bytes,
            max_items=min(self._max_result_items, request.limit),
        )
        return NativeAnalysisObservationV1(
            sample_sha256=request.sample_sha256,
            operation=request.operation,
            request_fingerprint=request.fingerprint,
            provider=self.provider,
            provider_version=observation.ghidra_version,
            provider_plugin_version=observation.plugin_version,
            catalog_version=self._catalog_version,
            data=data,
            truncated=observation.truncated or truncated,
        )


def build_native_analysis_port(
    settings: Settings,
    runner: ReaReadOnlyRunner | GhidraMcpHttpAdapter | None = None,
) -> NativeAnalysisPort:
    """Build the configured versioned port; REA still needs a verified worker client.

    The concrete REA CLI/MCP runner is intentionally injected and must be supplied
    and validated outside CI. Its real protocol is not verified in this repository,
    so this factory never invents a CLI command, endpoint, or production fake.
    """
    provider = NativeAnalysisProvider(settings.native_analysis_provider)
    if not settings.native_analysis_enabled or provider is NativeAnalysisProvider.NONE:
        return UnavailableNativeAnalysisPort(provider)
    if provider is NativeAnalysisProvider.REA:
        if runner is None or isinstance(runner, GhidraMcpHttpAdapter):
            return UnavailableNativeAnalysisPort(
                provider,
                reason=NativeAnalysisRunnerNotConfigured(
                    "REA runner must be supplied by a confined deployment worker"
                ),
            )
        return ReaNativeAnalysisAdapter(
            runner,
            enabled=settings.native_analysis_enabled,
            expected_version=settings.native_analysis_rea_expected_version,
            catalog_version=settings.native_analysis_catalog_version,
            timeout_seconds=settings.native_analysis_timeout_seconds,
            max_response_bytes=settings.native_analysis_max_response_bytes,
            max_output_bytes=settings.native_analysis_max_output_bytes,
        )

    ghidra = (
        runner
        if isinstance(runner, GhidraMcpHttpAdapter)
        else create_ghidra_mcp_http_adapter(settings)
    )
    return GhidraMcpNativeAnalysisAdapter(
        ghidra,
        enabled=settings.native_analysis_enabled and settings.ghidra_enabled,
        catalog_version=settings.native_analysis_catalog_version,
        max_output_bytes=settings.native_analysis_max_output_bytes,
    )
