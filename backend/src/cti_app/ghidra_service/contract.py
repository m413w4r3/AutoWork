from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

CONTRACT_VERSION = "ghidra-readonly.v1"
OPERATIONS = frozenset(
    {
        "list_functions",
        "list_imports",
        "xrefs_to",
        "xrefs_from",
        "decompile_function",
        "search",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^(?:0x)?[0-9a-fA-F]{1,16}$")


class ContractError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    operation: str
    sample_sha256: str
    address: str | None = None
    query: str | None = None
    limit: int = 100


def parse_analysis_request(path: str, headers: object) -> AnalysisRequest:
    """Parse only the fixed v1 POST route and its closed query/header schema."""
    parsed = urlsplit(path)
    if parsed.path != "/v1/analyze":
        raise ContractError("unknown_path", status_code=404)
    try:
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ContractError("invalid_parameters") from None
    allowed_parameters = {"operation", "address", "query", "limit"}
    if set(parameters) - allowed_parameters or any(
        len(values) != 1 for values in parameters.values()
    ):
        raise ContractError("invalid_parameters")
    operation = parameters.get("operation", [None])[0]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ContractError("operation_not_allowlisted")

    address = parameters.get("address", [None])[0]
    query = parameters.get("query", [None])[0]
    if operation in {"xrefs_to", "xrefs_from", "decompile_function"}:
        if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
            raise ContractError("address_required")
        address = f"0x{int(address, 16):x}"
    elif address is not None:
        raise ContractError("address_not_supported")
    if operation == "search":
        if (
            not isinstance(query, str)
            or not 1 <= len(query) <= 512
            or any(unicodedata.category(character) == "Cc" for character in query)
        ):
            raise ContractError("query_required")
    elif query is not None:
        raise ContractError("query_not_supported")

    raw_limit = parameters.get("limit", ["1" if operation == "decompile_function" else "100"])[0]
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ContractError("limit_invalid") from None
    if not 1 <= limit <= 100 or (operation == "decompile_function" and limit != 1):
        raise ContractError("limit_out_of_bounds")

    contract_version = _header(headers, "X-Ghidra-Contract")
    if contract_version != CONTRACT_VERSION:
        raise ContractError("contract_version_mismatch", status_code=409)
    sample_sha256 = _header(headers, "X-Ghidra-SHA256")
    if not _SHA256.fullmatch(sample_sha256):
        raise ContractError("sample_sha256_invalid")
    content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise ContractError("content_type_invalid", status_code=415)
    return AnalysisRequest(operation, sample_sha256, address, query, limit)


def _header(headers: object, name: str) -> str:
    getter = getattr(headers, "get", None)
    value = getter(name) if callable(getter) else None
    return value if isinstance(value, str) else ""
