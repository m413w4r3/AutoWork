"""Bounded, offline .NET metadata inspection and ILSpy decompilation."""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

import pefile  # type: ignore[import-untyped]

from cti_app.application.confined_analysis import ConfinedAnalysisJobParameters
from cti_app.application.dotnet_analysis import ILSPYCMD_PACKAGE_SHA512, ILSPYCMD_VERSION
from cti_app.config import (
    DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES,
    DOTNET_GC_CONCURRENT,
    DOTNET_GC_HEAP_HARD_LIMIT,
    Settings,
)
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)
from cti_app.infrastructure.ilspycmd import (
    ilspycmd_provenance,
)

_METHOD_ROW = re.compile(r"^\s*(?P<rid>[0-9]+):\s+(?P<signature>.+)$")
_METHOD_NAME = re.compile(r"(?P<name>[^\s]+)\s*\(")
_TYPE_ROW = re.compile(r"^\s*(?P<rid>[0-9]+):\s+(?P<name>.+?)\s+\(flist=")
_TYPE_HEADER = re.compile(r"^#+\s+(?P<name>.+?)\s*$")
_METHOD_END = re.compile(r"^\s*\}\s*// end of method (?P<type>.+)::(?P<name>.+?)\s*$")
_MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_PAGE_SIZE_MAX = 200
_PAGE_CHARS_MAX = 16 * 1024
_METHOD_RID_MAX = 0xFFFFFF
_RESULT_SCHEMA_VERSION = "dotnet-analysis-result.v1"
_MACHINE_ARCHITECTURE = {
    0x014C: "x86",
    0x0166: "MIPS",
    0x01C0: "ARM",
    0x01C4: "ARM-Thumb-2",
    0x0200: "IA-64",
    0x8664: "x86-64",
    0xAA64: "ARM64",
    0x5032: "RISC-V 32",
    0x5064: "RISC-V 64",
    0x5128: "RISC-V 128",
}
_OPERATIONS = frozenset(
    {"assembly", "types", "methods", "decompile", "references", "strings", "resources"}
)
_SUBPROCESS_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "DOTNET_ROOT": "/usr/share/dotnet",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_GCHeapHardLimit": DOTNET_GC_HEAP_HARD_LIMIT,
    "DOTNET_gcConcurrent": DOTNET_GC_CONCURRENT,
    "NUGET_PACKAGES": "/nonexistent",
}


async def run_dotnet_analysis(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    settings: Settings,
    *,
    monodis_version: Callable[[], str | None],
    extract_monodis_method_body: Callable[[str, int], str | None],
) -> dict[str, Any]:
    result = await _run_dotnet_analysis(
        parameters,
        payload,
        settings,
        monodis_version=monodis_version,
        extract_monodis_method_body=extract_monodis_method_body,
    )
    return {"schema_version": _RESULT_SCHEMA_VERSION, **result}


async def _run_dotnet_analysis(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    settings: Settings,
    *,
    monodis_version: Callable[[], str | None],
    extract_monodis_method_body: Callable[[str, int], str | None],
) -> dict[str, Any]:
    """Inspect a PE/CLI payload without loading it; decompile methods with ILSpy."""
    started = time.monotonic()
    operation = parameters.analysis_parameters.get("operation", "")
    if operation not in _OPERATIONS:
        return _failure("FAILED", "dotnet_operation_invalid")
    if len(payload) > min(_MAX_SAMPLE_BYTES, settings.analysis_local_monodis_max_sample_bytes):
        return _failure("POLICY_DENIED", "dotnet_sample_too_large")
    if payload.startswith(b"\x7fELF"):
        return _failure(
            "POLICY_DENIED",
            "dotnet_metadata_not_found",
            file_format="ELF",
            sample_sha256=hashlib.sha256(payload).hexdigest(),
            recommended_native_tool="native.inspect",
        )
    if operation == "decompile":
        method_rid = _bounded_int(
            parameters.analysis_parameters.get("method_rid"), 1, _METHOD_RID_MAX
        )
        if method_rid is None:
            return _failure("POLICY_DENIED", "dotnet_method_rid_invalid")
    else:
        method_rid = None

    try:
        metadata = _inspect_cli_image(payload)
    except Exception as exc:
        return _failure(
            "FAILED",
            "dotnet_invalid_or_missing_cli_metadata",
            error_type=type(exc).__name__,
            sample_sha256=hashlib.sha256(payload).hexdigest(),
            file_format="PE",
        )
    if metadata is None:
        return _failure(
            "POLICY_DENIED",
            "dotnet_metadata_not_found",
            file_format="non_dotnet_or_native_pe",
            sample_sha256=hashlib.sha256(payload).hexdigest(),
            recommended_native_tool="native.inspect",
        )
    source = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "format": metadata["format"],
    }
    if operation == "assembly":
        table = await _run_monodis_operations(
            payload,
            ["--assembly", "--module"],
            parameters,
            settings,
            monodis_version=monodis_version,
        )
        return _page_text_result(
            operation,
            str(table.get("stdout", "")),
            parameters.analysis_parameters,
            complete=table.get("status") == "SUCCEEDED",
            source=source,
            metadata=metadata,
            version=table.get("version"),
            started=started,
        )

    if operation == "decompile":
        return await _decompile_method(
            parameters,
            payload,
            method_rid=method_rid or 0,
            metadata=metadata,
            source=source,
            settings=settings,
            monodis_version=monodis_version,
            extract_monodis_method_body=extract_monodis_method_body,
        )

    if operation == "types":
        table = await _run_monodis_operations(
            payload,
            ["--typedef"],
            parameters,
            settings,
            monodis_version=monodis_version,
        )
        records, complete = _type_records(str(table.get("stdout", "")))
        return _page_result(
            "types",
            records,
            parameters.analysis_parameters,
            complete=complete and table.get("status") == "SUCCEEDED",
            source=source,
            metadata=metadata,
            version=table.get("version"),
            started=started,
        )

    if operation == "methods":
        table = await _run_monodis_operations(
            payload,
            ["--method"],
            parameters,
            settings,
            monodis_version=monodis_version,
        )
        records, complete = _method_records(str(table.get("stdout", "")))
        return _page_result(
            "methods",
            records,
            parameters.analysis_parameters,
            complete=complete and table.get("status") == "SUCCEEDED",
            source=source,
            metadata=metadata,
            version=table.get("version"),
            started=started,
        )

    command_groups = {
        "references": ["--assemblyref", "--memberref", "--implmap", "--moduleref"],
        "strings": ["--userstrings"],
        # List embedded resource identities and offsets only; never dump resource bytes.
        "resources": ["--manifest"],
    }
    table = await _run_monodis_operations(
        payload,
        command_groups[operation],
        parameters,
        settings,
        monodis_version=monodis_version,
    )
    return _page_text_result(
        operation,
        str(table.get("stdout", "")),
        parameters.analysis_parameters,
        complete=table.get("status") == "SUCCEEDED",
        source=source,
        metadata=metadata,
        version=table.get("version"),
        started=started,
    )


def probe_ilspycmd(settings: Settings | None = None) -> tuple[str | None, str | None]:
    address_space_limit_bytes = (
        settings.dotnet_analysis_address_space_limit_bytes
        if settings is not None
        else DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES
    )
    provenance = ilspycmd_provenance(address_space_limit_bytes=address_space_limit_bytes)
    if provenance is None:
        return None, None
    return ILSPYCMD_VERSION, ILSPYCMD_PACKAGE_SHA512


def _inspect_cli_image(payload: bytes) -> dict[str, Any] | None:
    pe = pefile.PE(data=payload, fast_load=False)
    try:
        directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
        cli_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"]
        debug_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]
        if len(directories) <= cli_index:
            return None
        cli_directory = directories[cli_index]
        if not cli_directory.VirtualAddress or cli_directory.Size < 72:
            return None
        cli_offset = int(pe.get_offset_from_rva(cli_directory.VirtualAddress))
        if cli_offset < 0 or cli_offset + 72 > len(payload):
            raise ValueError("CLR header exceeds the file bounds")
        cor_header = struct.unpack_from("<IHH16I", payload, cli_offset)
        if cor_header[0] < 72:
            raise ValueError("CLR header size is invalid")
        metadata_rva = int(cor_header[3])
        metadata_size = int(cor_header[4])
        if metadata_rva <= 0 or metadata_size < 4:
            raise ValueError("CLR metadata directory is missing")
        metadata_offset = int(pe.get_offset_from_rva(metadata_rva))
        if metadata_offset < 0 or metadata_offset + metadata_size > len(payload):
            raise ValueError("CLR metadata directory exceeds the file bounds")
        if payload[metadata_offset : metadata_offset + 4] != b"BSJB":
            raise ValueError("CLR metadata signature is invalid")

        machine = int(pe.FILE_HEADER.Machine)
        flags = int(cor_header[5])
        il_only = bool(flags & 0x1)
        managed_native_header = bool(cor_header[17] and cor_header[18])
        codeview_reference: str | None = None
        embedded_pdb = False
        pe.parse_data_directories(directories=[debug_index])
        for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", ()):
            entry_type = int(getattr(entry.struct, "Type", 0))
            if entry_type == 17:
                embedded_pdb = True
            elif entry_type == 2 and getattr(entry, "entry", None) is not None:
                value = getattr(entry.entry, "PDBFileName", b"")
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                if isinstance(value, str) and value:
                    codeview_reference = PureWindowsPath(value).name or PurePath(value).name

        return {
            "format": "PE/CLI (.NET metadata; CLR/Mono-compatible)",
            "runtime_family": "CLR/Mono-compatible CLI metadata",
            "machine": f"0x{machine:04x}",
            "architecture": _MACHINE_ARCHITECTURE.get(machine, "unknown"),
            "clr_runtime_version": f"{int(cor_header[1])}.{int(cor_header[2])}",
            "metadata_signature": "BSJB",
            "metadata_size_bytes": metadata_size,
            "il_only": il_only,
            "mixed_native_managed": not il_only,
            "managed_native_header_present": managed_native_header,
            "pdb": {
                "debug_directory_present": bool(getattr(pe, "DIRECTORY_ENTRY_DEBUG", ())),
                "embedded_portable_pdb": embedded_pdb,
                "codeview_reference": codeview_reference,
                "external_pdb_payload_provided": False,
            },
            "managed_analysis_path": "ilspycmd (offline IL/CIL static decompilation)",
            "native_analysis_path": (
                "native.inspect (configured Ghidra/native path) for native PE content"
                if not il_only or managed_native_header
                else None
            ),
            "code_execution": False,
        }
    finally:
        pe.close()


async def _run_monodis_operations(
    payload: bytes,
    operations: Sequence[str],
    parameters: ConfinedAnalysisJobParameters,
    settings: Settings,
    *,
    monodis_version: Callable[[], str | None],
) -> dict[str, Any]:
    executable = shutil.which("monodis")
    version = monodis_version()
    if executable is None or version is None:
        return _failure("UNAVAILABLE", "monodis_binary_unavailable")
    if version != settings.analysis_local_monodis_expected_version:
        return _failure("UNAVAILABLE", "monodis_package_version_unattested", version=version)
    per_command_limit = settings.analysis_local_monodis_max_output_bytes
    output: list[str] = []
    total_bytes = 0
    truncated = False
    timeout = min(
        parameters.timeout_seconds,
        settings.analysis_local_monodis_timeout_seconds,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cti-dotnet-", dir="/tmp") as directory:
            sample_path = Path(directory) / "sample.bin"
            sample_path.write_bytes(payload)
            sample_path.chmod(0o400)
            for operation in operations:
                remaining = per_command_limit - total_bytes
                if remaining <= 0:
                    truncated = True
                    break
                result = await run_analysis_subprocess(
                    [executable, operation, str(sample_path)],
                    timeout_seconds=timeout,
                    output_limit=remaining,
                    memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
                    environment=_SUBPROCESS_ENV,
                )
                text = result.stdout.decode("utf-8", "replace")
                total_bytes += len(result.stdout)
                if result.status is AnalysisSubprocessStatus.TIMED_OUT:
                    return _failure(
                        "TIMEOUT", "monodis_metadata_timeout", version=version, operation=operation
                    )
                if result.status in {
                    AnalysisSubprocessStatus.FAILED_TO_START,
                    AnalysisSubprocessStatus.NON_ZERO_EXIT,
                }:
                    detail = result.stderr.decode("utf-8", "replace")[:512]
                    return _failure(
                        "FAILED",
                        "monodis_metadata_failed",
                        version=version,
                        operation=operation,
                        error_detail=detail,
                    )
                if result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT:
                    truncated = True
                output.append(f"[{operation}]\n{text}")
                if total_bytes >= per_command_limit:
                    truncated = True
                    break
    except OSError as exc:
        return _failure(
            "FAILED", "monodis_metadata_failed", version=version, error_type=type(exc).__name__
        )
    return {
        "status": "PARTIAL" if truncated else "SUCCEEDED",
        "version": version,
        "stdout": "\n".join(output),
        "total_bytes_observed": total_bytes,
        "truncated": truncated,
        "normalized_command": ["monodis", *operations, "<sample>"],
    }


async def _decompile_method(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    *,
    method_rid: int,
    metadata: Mapping[str, Any],
    source: Mapping[str, Any],
    settings: Settings,
    monodis_version: Callable[[], str | None],
    extract_monodis_method_body: Callable[[str, int], str | None],
) -> dict[str, Any]:
    method_table = await _run_monodis_operations(
        payload,
        ["--method"],
        parameters,
        settings,
        monodis_version=monodis_version,
    )
    methods, complete = _method_records(str(method_table.get("stdout", "")))
    selected = next((item for item in methods if item["rid"] == method_rid), None)
    if selected is None:
        return _failure(
            "UNAVAILABLE",
            "monodis_method_rid_not_found",
            method_rid=method_rid,
            source=source,
            metadata=metadata,
            method_table_truncated=not complete,
        )
    if method_table.get("status") not in {"SUCCEEDED", "PARTIAL"}:
        return _with_source(method_table, source, metadata)

    provenance = ilspycmd_provenance(
        address_space_limit_bytes=settings.dotnet_analysis_address_space_limit_bytes
    )
    executable = shutil.which("ilspycmd")
    if provenance is None or executable is None:
        decompile_error: str = "ilspycmd_unavailable_or_unattested"
    else:
        ilspy_result = await _try_ilspy_decompile(
            executable,
            payload,
            selected,
            methods,
            parameters,
            settings,
        )
        if isinstance(ilspy_result, dict):
            result = ilspy_result
            result.update(
                {
                    "status": "SUCCEEDED" if complete else "PARTIAL",
                    "engine": "ilspycmd",
                    "version": ILSPYCMD_VERSION,
                    "package_sha512": ILSPYCMD_PACKAGE_SHA512,
                    "source": source,
                    "metadata": metadata,
                    "method_rid": method_rid,
                    "method_token": f"0x{0x06000000 | method_rid:08x}",
                    "type_name": selected["type_name"],
                    "method_name": selected["method_name"],
                    "truncated": result.get("truncated", False) or not complete,
                }
            )
            if not complete:
                result["status"] = "PARTIAL"
                result["available_reason"] = "method_table_truncated"
            return result
        decompile_error = ilspy_result

    # ILSpy could not resolve this method body. Preserve its cause and try the
    # explicit monodis fallback; callers can retry another stable MethodDef RID.
    fallback = await _run_monodis_operations(
        payload,
        ["--show-method-tokens"],
        parameters,
        settings,
        monodis_version=monodis_version,
    )
    fallback_body = extract_monodis_method_body(str(fallback.get("stdout", "")), method_rid)
    if fallback_body is None or re.search(r"\bIL_[0-9a-fA-F]{4,8}:", fallback_body) is None:
        cause = (
            "monodis_method_body_missing"
            if fallback.get("status") in {"SUCCEEDED", "PARTIAL"}
            else str(fallback.get("available_reason", "monodis_method_body_missing"))
        )
        return _failure(
            "FAILED",
            cause,
            ilspy_cause=decompile_error,
            method_rid=method_rid,
            method_token=f"0x{0x06000000 | method_rid:08x}",
            type_name=selected["type_name"],
            method_name=selected["method_name"],
            source=source,
            metadata=metadata,
            next_step="Retry method.decompile with a different methods.list RID.",
        )
    text_offset = _bounded_int(parameters.analysis_parameters.get("offset", "0"), 0, 10**9)
    max_chars = _bounded_int(
        parameters.analysis_parameters.get("max_chars", "8192"), 1, _PAGE_CHARS_MAX
    )
    if text_offset is None or max_chars is None:
        return _failure("POLICY_DENIED", "dotnet_output_range_invalid")
    body_page = fallback_body[text_offset : text_offset + max_chars]
    fallback_truncated = text_offset + len(body_page) < len(fallback_body)
    return {
        "status": "PARTIAL",
        "engine": "monodis-fallback",
        "version": fallback.get("version"),
        "ilspy_cause": decompile_error,
        "method_rid": method_rid,
        "method_token": f"0x{0x06000000 | method_rid:08x}",
        "type_name": selected["type_name"],
        "method_name": selected["method_name"],
        "stdout": body_page,
        "offset": text_offset,
        "length_chars": len(body_page),
        "total_chars": len(fallback_body),
        "next_offset": text_offset + len(body_page) if fallback_truncated else None,
        "source": source,
        "metadata": metadata,
        "truncated": fallback_truncated,
        "interpretation": "Fallback CIL disassembly; decompilation was unavailable.",
    }


async def _try_ilspy_decompile(
    executable: str,
    payload: bytes,
    selected: Mapping[str, Any],
    methods: Sequence[Mapping[str, Any]],
    parameters: ConfinedAnalysisJobParameters,
    settings: Settings,
) -> dict[str, Any] | str:
    type_name = str(selected["type_name"])
    method_name = str(selected["method_name"])
    occurrence = sum(
        1
        for item in methods
        if int(item["rid"]) < int(selected["rid"])
        and item["type_name"] == type_name
        and item["method_name"] == method_name
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cti-ilspy-", dir="/tmp") as directory:
            sample_path = Path(directory) / "sample.bin"
            sample_path.write_bytes(payload)
            sample_path.chmod(0o400)
            result = await run_analysis_subprocess(
                [
                    executable,
                    "--disable-updatecheck",
                    "--ilcode",
                    "--type",
                    type_name,
                    str(sample_path),
                ],
                timeout_seconds=min(parameters.timeout_seconds, 45.0),
                output_limit=min(
                    settings.analysis_local_monodis_max_output_bytes,
                    max(parameters.max_output_bytes * 16, _PAGE_CHARS_MAX),
                ),
                memory_limit_bytes=settings.dotnet_analysis_address_space_limit_bytes,
                environment=_SUBPROCESS_ENV,
            )
            output = result.stdout.decode("utf-8", "replace")
            if result.status is AnalysisSubprocessStatus.TIMED_OUT:
                return "ilspycmd_timeout"
            if result.status not in {
                AnalysisSubprocessStatus.SUCCEEDED,
                AnalysisSubprocessStatus.OUTPUT_LIMIT,
            }:
                return "ilspycmd_failed"
            body = _extract_ilspy_method(output, type_name, method_name, occurrence)
            if body is None or re.search(r"\bIL_[0-9a-fA-F]{4,8}:", body) is None:
                return "ilspycmd_method_body_missing"
            text_offset = _bounded_int(parameters.analysis_parameters.get("offset", "0"), 0, 10**9)
            max_chars = _bounded_int(
                parameters.analysis_parameters.get("max_chars", "8192"), 1, _PAGE_CHARS_MAX
            )
            if text_offset is None or max_chars is None:
                return "dotnet_output_range_invalid"
            page = body[text_offset : text_offset + max_chars]
            truncated = (
                text_offset + len(page) < len(body)
                or result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
            )
            return {
                "stdout": page,
                "offset": text_offset,
                "length_chars": len(page),
                "total_chars": len(body),
                "next_offset": text_offset + len(page) if truncated else None,
                "truncated": truncated,
            }
    except OSError as exc:
        return f"ilspycmd_failed:{type(exc).__name__}"


def _extract_ilspy_method(
    output: str, type_name: str, method_name: str, occurrence: int
) -> str | None:
    lines = output.splitlines(keepends=True)
    blocks: list[str] = []
    current_start: int | None = None
    for index, line in enumerate(lines):
        if ".method " in line:
            current_start = index
        match = _METHOD_END.match(line)
        if match is None:
            continue
        candidate_type = match.group("type").replace("/", "+")
        normalized_type = type_name.replace("/", "+")
        if candidate_type == normalized_type and match.group("name") == method_name:
            start = current_start if current_start is not None else index
            blocks.append("".join(lines[start : index + 1]))
        current_start = None
    return blocks[occurrence] if 0 <= occurrence < len(blocks) else None


def _method_records(output: str) -> tuple[list[dict[str, Any]], bool]:
    current_type: str | None = None
    records: list[dict[str, Any]] = []
    complete = "Method Table" in output and "Typedef Table" not in output
    for line in output.splitlines():
        header = _TYPE_HEADER.match(line)
        if header is not None and not line.lstrip().startswith("Method Table"):
            current_type = header.group("name").lstrip(".")
            continue
        row = _METHOD_ROW.match(line)
        if row is None or current_type is None:
            continue
        rid = int(row.group("rid"))
        signature = row.group("signature").split("(param:", maxsplit=1)[0].strip()
        name_match = _METHOD_NAME.search(signature)
        if name_match is None:
            method_name = "unparsed"
        else:
            method_name = name_match.group("name").strip("'")
        records.append(
            {
                "rid": rid,
                "token": f"0x{0x06000000 | rid:08x}",
                "type_name": current_type,
                "method_name": method_name,
                "signature": signature,
            }
        )
    if "Method Table" not in output:
        complete = False
    return records, complete


def _type_records(output: str) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    complete = "Typedef Table" in output
    for line in output.splitlines():
        row = _TYPE_ROW.match(line)
        if row is None:
            continue
        rid = int(row.group("rid"))
        name = row.group("name").strip()
        if name == "(null)":
            continue
        records.append(
            {"rid": rid, "token": f"0x{0x02000000 | rid:08x}", "type_name": name.lstrip(".")}
        )
    return records, complete


def _page_result(
    operation: str,
    records: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, str],
    *,
    complete: bool,
    source: Mapping[str, Any],
    metadata: Mapping[str, Any],
    version: Any,
    started: float,
) -> dict[str, Any]:
    offset = _bounded_int(parameters.get("offset", "0"), 0, 10**7)
    limit = _bounded_int(parameters.get("limit", "100"), 1, _PAGE_SIZE_MAX)
    if offset is None or limit is None:
        return _failure("POLICY_DENIED", "dotnet_pagination_invalid")
    page = list(records[offset : offset + limit])
    next_offset = offset + len(page) if offset + len(page) < len(records) else None
    obfuscated = _obfuscation_likelihood(records)
    return {
        "status": "OBFUSCATED" if obfuscated else "SUCCEEDED" if complete else "PARTIAL",
        "operation": operation,
        "version": version,
        "source": source,
        "metadata": metadata,
        "records": page,
        "total_items": len(records),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "truncated": next_offset is not None or not complete,
        "obfuscation_assessment": (
            "possible_name_obfuscation_heuristic"
            if obfuscated
            else "not_detected_by_name_heuristic"
        ),
        "elapsed_ms": int(max(0, time.monotonic() - started) * 1000),
    }


def _page_text_result(
    operation: str,
    output: str,
    parameters: Mapping[str, str],
    *,
    complete: bool,
    source: Mapping[str, Any],
    metadata: Mapping[str, Any],
    version: Any,
    started: float,
) -> dict[str, Any]:
    lines = output.splitlines()
    offset = _bounded_int(parameters.get("offset", "0"), 0, 10**7)
    limit = _bounded_int(parameters.get("limit", "100"), 1, _PAGE_SIZE_MAX)
    max_chars = _bounded_int(parameters.get("max_chars", "8192"), 1, _PAGE_CHARS_MAX)
    if offset is None or limit is None or max_chars is None:
        return _failure("POLICY_DENIED", "dotnet_pagination_invalid")
    page_lines = lines[offset : offset + limit]
    page_text = "\n".join(page_lines)[:max_chars]
    next_offset = offset + len(page_lines) if offset + len(page_lines) < len(lines) else None
    truncated = (
        next_offset is not None or len(page_text) < len("\n".join(page_lines)) or not complete
    )
    return {
        "status": "PARTIAL" if truncated else "SUCCEEDED",
        "operation": operation,
        "version": version,
        "source": source,
        "metadata": metadata,
        "text": page_text,
        "offset": offset,
        "limit": limit,
        "total_lines": len(lines),
        "next_offset": next_offset,
        "truncated": truncated,
        "interpretation": (
            "User strings are raw metadata; obfuscated strings and encrypted blobs are unresolved."
            if operation == "strings"
            else "References/imports alone do not establish command-and-control behavior."
            if operation == "references"
            else "Manifest metadata only; embedded resource bytes are not extracted."
            if operation == "resources"
            else None
        ),
        "elapsed_ms": int(max(0, time.monotonic() - started) * 1000),
    }


def _with_source(
    result: Mapping[str, Any], source: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    values = dict(result)
    values["source"] = source
    values["metadata"] = metadata
    return values


def _obfuscation_likelihood(records: Sequence[Mapping[str, Any]]) -> bool:
    names = [
        str(item.get("method_name") or item.get("type_name") or "")
        for item in records
        if str(item.get("method_name") or item.get("type_name") or "")
        not in {".ctor", ".cctor", "<Module>"}
    ]
    if len(names) < 10:
        return False
    short_identifiers = sum(
        1 for name in names if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,1}", name) is not None
    )
    return short_identifiers / len(names) >= 0.8


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _failure(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "available_reason": reason, **extra}
