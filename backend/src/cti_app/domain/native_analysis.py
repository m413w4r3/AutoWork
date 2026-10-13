from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^(?:0x)?[0-9a-fA-F]{1,16}$")
_HEX_BYTES = re.compile(r"^(?:[0-9a-fA-F]{2}){1,256}$")


class NativeAnalysisProvider(StrEnum):
    REA = "rea"
    GHIDRA_MCP = "ghidra_mcp"
    NONE = "none"


class NativeAnalysisOperation(StrEnum):
    LIST_FUNCTIONS = "list_functions"
    GET_FUNCTION = "get_function"
    DECOMPILE = "decompile"
    XREFS_TO = "xrefs_to"
    XREFS_FROM = "xrefs_from"
    CALLERS = "callers"
    CALLEES = "callees"
    STRINGS = "strings"
    SEARCH = "search"
    READ_VALUE = "read_value"
    IMPORTS = "imports"
    EXPORTS = "exports"
    SECTIONS = "sections"


class NativeSearchKind(StrEnum):
    BYTES = "bytes"
    VALUE = "value"
    SYMBOL = "symbol"


class NativeValueType(StrEnum):
    U8 = "u8"
    U16 = "u16"
    U32 = "u32"
    U64 = "u64"
    I8 = "i8"
    I16 = "i16"
    I32 = "i32"
    I64 = "i64"
    F32 = "f32"
    F64 = "f64"
    BYTES = "bytes"


@dataclass(frozen=True, slots=True)
class NativeAnalysisRequestV1:
    sample_sha256: str
    operation: NativeAnalysisOperation
    address: str | None = None
    symbol: str | None = None
    search_kind: NativeSearchKind | None = None
    query: str | None = None
    value_type: NativeValueType | None = None
    size: int | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sample_sha256):
            raise ValueError("sample_sha256 must be a lowercase SHA-256 digest")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")

        if self.address is not None:
            if not _ADDRESS.fullmatch(self.address):
                raise ValueError("address must be a hexadecimal address")
            normalized = self.address.lower()
            if not normalized.startswith("0x"):
                normalized = f"0x{normalized}"
            object.__setattr__(self, "address", normalized)
        if self.symbol is not None and not 1 <= len(self.symbol) <= 256:
            raise ValueError("symbol must contain between 1 and 256 characters")

        if self.operation is NativeAnalysisOperation.GET_FUNCTION:
            if (self.address is None) == (self.symbol is None):
                raise ValueError("get_function requires exactly one of address or symbol")
        elif self.operation in {
            NativeAnalysisOperation.DECOMPILE,
            NativeAnalysisOperation.XREFS_TO,
            NativeAnalysisOperation.XREFS_FROM,
            NativeAnalysisOperation.CALLERS,
            NativeAnalysisOperation.CALLEES,
            NativeAnalysisOperation.READ_VALUE,
        }:
            if self.address is None:
                raise ValueError(f"{self.operation.value} requires an address")
            if self.symbol is not None:
                raise ValueError(f"{self.operation.value} does not accept a symbol")
        elif self.address is not None or self.symbol is not None:
            raise ValueError(f"{self.operation.value} does not accept an address or symbol")

        if self.operation is NativeAnalysisOperation.SEARCH:
            if self.search_kind is None or not isinstance(self.query, str):
                raise ValueError("search requires a closed search_kind and query")
            if not 1 <= len(self.query) <= 512:
                raise ValueError("search query must contain between 1 and 512 characters")
            if self.search_kind is NativeSearchKind.BYTES and not _HEX_BYTES.fullmatch(self.query):
                raise ValueError("byte searches require 1 to 256 complete hexadecimal bytes")
            if self.search_kind is not NativeSearchKind.BYTES and self.query.strip() != self.query:
                raise ValueError("search query must not have surrounding whitespace")
        elif self.search_kind is not None or self.query is not None:
            raise ValueError(f"{self.operation.value} does not accept search inputs")

        if self.operation is NativeAnalysisOperation.READ_VALUE:
            if self.value_type is None:
                raise ValueError("read_value requires a closed value_type")
            if self.value_type is NativeValueType.BYTES:
                if isinstance(self.size, bool) or self.size is None or not 1 <= self.size <= 64:
                    raise ValueError("byte reads require a size between 1 and 64")
            elif self.size is not None:
                raise ValueError("size is only accepted for byte reads")
        elif self.value_type is not None or self.size is not None:
            raise ValueError(f"{self.operation.value} does not accept value inputs")

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": "native-analysis-request.v1",
            "sample_sha256": self.sample_sha256,
            "operation": self.operation.value,
            "address": self.address,
            "symbol": self.symbol,
            "search_kind": self.search_kind.value if self.search_kind else None,
            "query": self.query,
            "value_type": self.value_type.value if self.value_type else None,
            "size": self.size,
            "limit": self.limit,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeAnalysisObservationV1:
    sample_sha256: str
    operation: NativeAnalysisOperation
    request_fingerprint: str
    provider: NativeAnalysisProvider
    provider_version: str
    catalog_version: str
    data: Mapping[str, Any]
    truncated: bool = False
    provider_plugin_version: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sample_sha256):
            raise ValueError("sample_sha256 must be a lowercase SHA-256 digest")
        if not _SHA256.fullmatch(self.request_fingerprint):
            raise ValueError("request_fingerprint must be a lowercase SHA-256 digest")
        if self.provider is NativeAnalysisProvider.NONE:
            raise ValueError("a successful observation requires a provider")
        if (
            not self.provider_version.strip()
            or len(self.provider_version) > 128
            or not self.catalog_version.strip()
            or len(self.catalog_version) > 128
        ):
            raise ValueError("provider and catalog versions must be explicit")
        if self.provider_plugin_version is not None and len(self.provider_plugin_version) > 128:
            raise ValueError("provider plugin version must not exceed 128 characters")
        if not isinstance(self.data, Mapping):
            raise ValueError("observation data must be an object")
        object.__setattr__(self, "data", dict(self.data))

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": "native-analysis-observation.v1",
            "sample_sha256": self.sample_sha256,
            "operation": self.operation.value,
            "request_fingerprint": self.request_fingerprint,
            "provider": self.provider.value,
            "provider_version": self.provider_version,
            "provider_plugin_version": self.provider_plugin_version,
            "catalog_version": self.catalog_version,
            "data": dict(self.data),
            "truncated": self.truncated,
        }
