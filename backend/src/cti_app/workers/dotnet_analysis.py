"""Bounded, offline .NET metadata inspection and ILSpy decompilation."""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
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
_METHOD_NAME = re.compile(r"(?P<name>'(?:\\.|[^'])*'|[^\s]+)\s*\(")
_TYPE_ROW = re.compile(r"^\s*(?P<rid>[0-9]+):\s+(?P<name>.+?)\s+\(flist=")
_TYPE_METHOD_LIST_ROW = re.compile(
    r"^\s*(?P<rid>[0-9]+):\s+.+?\(flist=[0-9]+,\s*mlist=(?P<mlist>[0-9]+),"
)
_TYPE_HEADER = re.compile(r"^#+[ \t]+(?P<name>.+?)[ \t]*$")
_METHOD_END = re.compile(r"^\s*\}\s*// end of method (?P<type>.+)::(?P<name>.+?)\s*$")
_IL_CLASS_START = re.compile(r"^\s*\.class\b(?P<header>.*)$")
_IL_CLASS_NAME = re.compile(r"(?:^|[ \t])(?P<name>'(?:\\.|[^'])*'|[^ \t]+)[ \t]*$")
_IL_CLASS_END = re.compile(r"^\s*\}\s*// end of class(?:\s+.*)?$")
_IL_IDENTIFIER_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|.)")
_MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_MAX_METADATA_SCAN_BYTES = 16 * 1024 * 1024
_MAX_METADATA_ROWS = 200_000
_MAX_DECODED_METADATA_STRING_BYTES = 16 * 1024 * 1024
_MAX_FIELD_RVA_ROWS = 8_192
_MAX_PE_SECTIONS = 96
_FIELD_DATA_MAX_BYTES = 4096
_PAGE_SIZE_MAX = 200
_PAGE_CHARS_MAX = 16 * 1024
_METHOD_RID_MAX = 0xFFFFFF
_RESULT_SCHEMA_VERSION = "dotnet-analysis-result.v3"
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
    {
        "assembly",
        "types",
        "methods",
        "decompile",
        "references",
        "strings",
        "resources",
        "field_data",
    }
)
_FIELD_TOKEN = re.compile(r"^0x04[0-9a-fA-F]{6}$")
_FIELD_TYPE_NAME = re.compile(r"^[A-Za-z0-9_$<>{}./+`-]{1,256}$")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_$<>.`-]{1,256}$")

_CODED_INDEX_TABLES: dict[str, tuple[int, tuple[int, ...]]] = {
    "ResolutionScope": (2, (0, 26, 35, 1)),
    "TypeDefOrRef": (2, (2, 1, 27)),
    "MemberRefParent": (3, (2, 1, 26, 6, 27)),
    "HasConstant": (2, (4, 8, 23)),
    "HasCustomAttribute": (
        5,
        (6, 4, 1, 2, 8, 9, 10, 0, 14, 23, 20, 17, 26, 27, 32, 35, 38, 39, 40, 42, 44, 43),
    ),
    "CustomAttributeType": (3, (6, 10)),
    "HasFieldMarshal": (1, (4, 8)),
    "HasDeclSecurity": (2, (2, 6, 32)),
    "HasSemantics": (1, (20, 23)),
    "MethodDefOrRef": (1, (6, 10)),
    "MemberForwarded": (1, (4, 6)),
}
_METADATA_TABLE_COLUMNS: dict[int, tuple[str, ...]] = {
    0: ("u2", "string", "guid", "guid", "guid"),
    1: ("coded:ResolutionScope", "string", "string"),
    2: ("u4", "string", "string", "coded:TypeDefOrRef", "table:4", "table:6"),
    3: ("table:4",),
    4: ("u2", "string", "blob"),
    5: ("table:6",),
    6: ("u4", "u2", "u2", "string", "blob", "table:8"),
    7: ("table:8",),
    8: ("u2", "u2", "string"),
    9: ("table:2", "coded:TypeDefOrRef"),
    10: ("coded:MemberRefParent", "string", "blob"),
    11: ("u1", "u1", "coded:HasConstant", "blob"),
    12: ("coded:HasCustomAttribute", "coded:CustomAttributeType", "blob"),
    13: ("coded:HasFieldMarshal", "blob"),
    14: ("u2", "coded:HasDeclSecurity", "blob"),
    15: ("u2", "u4", "table:2"),
    16: ("u4", "table:4"),
    17: ("blob",),
    18: ("table:2", "table:20"),
    19: ("table:20",),
    20: ("u2", "string", "coded:TypeDefOrRef"),
    21: ("table:2", "table:23"),
    22: ("table:23",),
    23: ("u2", "string", "blob"),
    24: ("u2", "table:6", "coded:HasSemantics"),
    25: ("table:2", "coded:MethodDefOrRef", "coded:MethodDefOrRef"),
    26: ("string",),
    27: ("blob",),
    28: ("u2", "coded:MemberForwarded", "string", "table:26"),
    29: ("u4", "table:4"),
}


class _FieldDataError(ValueError):
    def __init__(self, detail_code: str) -> None:
        super().__init__(detail_code)
        self.detail_code = detail_code


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
    if operation == "field_data":
        return _read_field_data(payload, parameters.analysis_parameters)
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
        engine: str | None = None
        version = table.get("version")
        if (
            table.get("status") != "SUCCEEDED"
            or not records
            or any(not str(record.get("type_name", "")).strip() for record in records)
        ):
            ilspy_metadata = await _try_ilspy_metadata(
                payload,
                parameters,
                settings,
                type_table_output=str(table.get("stdout", "")),
            )
            if not isinstance(ilspy_metadata, str):
                ilspy_types, _ilspy_methods, ilspy_complete = ilspy_metadata
                if ilspy_types:
                    used_fallback = table.get("status") != "SUCCEEDED" or not records
                    if used_fallback:
                        records = ilspy_types
                    else:
                        replacements = {int(item["rid"]): item for item in ilspy_types}
                        records = [
                            {
                                **record,
                                "type_name": (
                                    record.get("type_name")
                                    or replacements.get(int(record["rid"]), {}).get("type_name")
                                    or ""
                                ),
                            }
                            for record in records
                        ]
                    if used_fallback or any(
                        not str(record.get("type_name", "")).strip() for record in records
                    ):
                        engine = "ilspycmd"
                        version = ILSPYCMD_VERSION
                    complete = ilspy_complete if used_fallback else complete and ilspy_complete
        result = _page_result(
            "types",
            records,
            parameters.analysis_parameters,
            complete=complete and (engine == "ilspycmd" or table.get("status") == "SUCCEEDED"),
            source=source,
            metadata=metadata,
            version=version,
            started=started,
            engine=engine,
            monodis_cause=(str(table.get("available_reason")) if engine == "ilspycmd" else None),
            monodis_exit_code=(table.get("exit_code") if engine == "ilspycmd" else None),
        )
        return result

    if operation == "methods":
        table = await _run_monodis_operations(
            payload,
            ["--method"],
            parameters,
            settings,
            monodis_version=monodis_version,
        )
        records, complete = _method_records(str(table.get("stdout", "")))
        engine = None
        version = table.get("version")
        if table.get("status") != "SUCCEEDED" or _needs_ilspy_names(records):
            typedef_table = await _run_monodis_operations(
                payload,
                ["--typedef"],
                parameters,
                settings,
                monodis_version=monodis_version,
            )
            ilspy_metadata = await _try_ilspy_metadata(
                payload,
                parameters,
                settings,
                type_table_output=str(typedef_table.get("stdout", "")),
            )
            if not isinstance(ilspy_metadata, str):
                _ilspy_types, ilspy_methods, ilspy_complete = ilspy_metadata
                if ilspy_methods:
                    used_fallback = table.get("status") != "SUCCEEDED" or not records
                    replaced = False
                    if used_fallback:
                        records = ilspy_methods
                        complete = ilspy_complete
                    else:
                        records, replaced = _merge_ilspy_method_names(records, ilspy_methods)
                        if replaced:
                            complete = complete and ilspy_complete
                    if used_fallback or replaced:
                        engine = "ilspycmd"
                        version = ILSPYCMD_VERSION
        result = _page_result(
            "methods",
            records,
            parameters.analysis_parameters,
            complete=complete and (engine == "ilspycmd" or table.get("status") == "SUCCEEDED"),
            source=source,
            metadata=metadata,
            version=version,
            started=started,
            engine=engine,
            monodis_cause=(str(table.get("available_reason")) if engine == "ilspycmd" else None),
            monodis_exit_code=table.get("exit_code") if engine == "ilspycmd" else None,
        )
        return result

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


def _read_field_data(payload: bytes, parameters: Mapping[str, str]) -> dict[str, Any]:
    try:
        token = parameters.get("field_token")
        type_name = parameters.get("type_name")
        field_name = parameters.get("field_name")
        has_token = "field_token" in parameters
        has_type_name = "type_name" in parameters
        has_field_name = "field_name" in parameters
        has_names = has_type_name and has_field_name
        if (has_token and (has_type_name or has_field_name)) or (not has_token and not has_names):
            raise _FieldDataError("dotnet_field_selector_invalid")
        if has_token and (not isinstance(token, str) or _FIELD_TOKEN.fullmatch(token) is None):
            raise _FieldDataError("dotnet_field_selector_invalid")
        if has_names and (
            not isinstance(type_name, str)
            or not isinstance(field_name, str)
            or _FIELD_TYPE_NAME.fullmatch(type_name) is None
            or _FIELD_NAME.fullmatch(field_name) is None
        ):
            raise _FieldDataError("dotnet_field_selector_invalid")

        field_offset_value = _field_parameter_int(parameters, "offset", 0, 0xFFFFFFFF, default=0)
        if field_offset_value is None:
            raise _FieldDataError("dotnet_field_offset_invalid")
        field_offset = field_offset_value
        requested_length = _field_parameter_int(parameters, "length", 1, 0xFFFFFFFF, required=False)
        if requested_length is not None and requested_length > _FIELD_DATA_MAX_BYTES:
            raise _FieldDataError("dotnet_field_data_length_exceeds_cap")

        pe = pefile.PE(data=payload, fast_load=True)
        try:
            cli_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"]
            directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
            if len(directories) <= cli_index:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            cli_directory = directories[cli_index]
            cli_rva = int(cli_directory.VirtualAddress)
            if cli_rva <= 0 or int(cli_directory.Size) < 72:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            cli_offset, _, _ = _rva_to_file_offset(pe, len(payload), cli_rva, 72)
            cor_header = struct.unpack_from("<IHH16I", payload, cli_offset)
            if cor_header[0] < 72:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            metadata_rva = int(cor_header[3])
            metadata_size = int(cor_header[4])
            if metadata_rva <= 0 or metadata_size < 4:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            if metadata_size > _MAX_METADATA_SCAN_BYTES:
                raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
            metadata_offset, _, _ = _rva_to_file_offset(
                pe, len(payload), metadata_rva, metadata_size
            )
            streams = _metadata_streams(payload, metadata_offset, metadata_size)
            tables = streams.get("#~")
            if tables is None:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            field_records, type_defs, type_refs, layouts, field_rvas = _parse_field_tables(
                tables, streams.get("#Strings", b""), streams.get("#Blob", b"")
            )
            selected = _select_field(
                field_records,
                type_defs,
                field_rvas,
                token=token,
                type_name=type_name,
                field_name=field_name,
            )
            if selected["rva"] == 0:
                raise _FieldDataError("dotnet_field_rva_zero")

            field_type_size = _field_value_type_size(
                bytes(streams["#Blob"]), selected["signature_index"], type_defs, type_refs, layouts
            )
            if field_offset > 0xFFFFFFFF - int(selected["rva"]):
                raise _FieldDataError("dotnet_field_offset_invalid")
            if field_type_size is not None and field_offset >= field_type_size:
                raise _FieldDataError("dotnet_field_range_out_of_bounds")
            if field_type_size is None and requested_length is None:
                raise _FieldDataError("dotnet_field_data_length_required")

            first_offset, section_name, _ = _rva_to_file_offset(
                pe, len(payload), int(selected["rva"]), 1
            )
            if field_type_size is not None:
                _rva_to_file_offset(pe, len(payload), int(selected["rva"]), field_type_size)
                remaining = field_type_size - field_offset
                read_length = min(
                    requested_length if requested_length is not None else remaining,
                    remaining,
                    _FIELD_DATA_MAX_BYTES,
                )
            else:
                read_length = requested_length or 0
            if read_length < 1:
                raise _FieldDataError("dotnet_field_range_out_of_bounds")
            read_rva = int(selected["rva"]) + field_offset
            read_offset, read_section, _ = _rva_to_file_offset(
                pe, len(payload), read_rva, read_length
            )
            if read_section != section_name or read_offset != first_offset + field_offset:
                raise _FieldDataError("dotnet_field_rva_invalid")
            raw = payload[read_offset : read_offset + read_length]
            if len(raw) != read_length:
                raise _FieldDataError("dotnet_field_file_bounds_invalid")
            return {
                "status": "SUCCEEDED",
                "operation": "field_data",
                "engine": "pefile-field-rva",
                "version": pefile.__version__,
                "field_token": f"0x{0x04000000 | int(selected['rid']):08x}",
                "field_name": selected["name"],
                "type_name": selected["type_name"],
                "rva": int(selected["rva"]),
                "section_name": section_name,
                "file_offset": first_offset,
                "field_size_bytes": field_type_size,
                "size_known": field_type_size is not None,
                "completeness": (
                    "unknown"
                    if field_type_size is None
                    else "partial"
                    if field_offset + len(raw) < field_type_size
                    else "complete"
                ),
                "size_in_file_bounds": {
                    "file_size_bytes": len(payload),
                    "field_start": first_offset,
                    # FieldRVA gives an address, not an extent. Report the end
                    # only when the field's explicit value-type layout supplied it.
                    "field_end_exclusive": (
                        first_offset + field_type_size if field_type_size is not None else None
                    ),
                    "read_start": read_offset,
                    "read_end_exclusive": read_offset + len(raw),
                },
                "offset": field_offset,
                "length_returned": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "hex_dump": raw.hex(" "),
                "truncated": (
                    field_offset + len(raw) < field_type_size
                    if field_type_size is not None
                    else False
                ),
            }
        finally:
            pe.close()
    except _FieldDataError as exc:
        denied = exc.detail_code in {
            "dotnet_field_selector_invalid",
            "dotnet_field_selector_ambiguous",
            "dotnet_field_not_found",
            "dotnet_field_rva_missing",
            "dotnet_field_data_length_required",
            "dotnet_field_data_length_exceeds_cap",
            "dotnet_field_offset_invalid",
            "dotnet_field_range_out_of_bounds",
        }
        status = "POLICY_DENIED" if denied else "FAILED"
        return _failure(
            status,
            exc.detail_code,
            detail_code=exc.detail_code,
            operation="field_data",
            engine="pefile-field-rva",
            version=pefile.__version__,
        )
    except Exception as exc:
        return _failure(
            "FAILED",
            "dotnet_field_metadata_invalid",
            detail_code="dotnet_field_metadata_invalid",
            operation="field_data",
            engine="pefile-field-rva",
            version=pefile.__version__,
            error_type=type(exc).__name__,
        )


def _field_parameter_int(
    parameters: Mapping[str, str],
    key: str,
    minimum: int,
    maximum: int,
    *,
    default: int | None = None,
    required: bool = True,
) -> int | None:
    value = parameters.get(key)
    if value is None:
        if required:
            return default
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        code = "dotnet_field_offset_invalid" if key == "offset" else "dotnet_field_length_invalid"
        raise _FieldDataError(code)
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        code = "dotnet_field_offset_invalid" if key == "offset" else "dotnet_field_length_invalid"
        raise _FieldDataError(code)
    return parsed


def _rva_to_file_offset(pe: Any, file_size: int, rva: int, length: int) -> tuple[int, str, int]:
    if (
        isinstance(rva, bool)
        or not isinstance(rva, int)
        or rva <= 0
        or rva > 0xFFFFFFFF
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length < 1
        or length > 0xFFFFFFFF
        or rva + length > 0x1_0000_0000
    ):
        raise _FieldDataError("dotnet_field_rva_invalid")
    try:
        headers_size = int(pe.OPTIONAL_HEADER.SizeOfHeaders)
        sections = tuple(pe.sections)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise _FieldDataError("dotnet_field_pe_sections_invalid") from exc
    if not 0 < headers_size <= file_size or len(sections) > _MAX_PE_SECTIONS:
        raise _FieldDataError("dotnet_field_pe_sections_invalid")

    virtual_ranges: list[tuple[int, int]] = []
    raw_ranges: list[tuple[int, int]] = []
    parsed_sections: list[tuple[int, int, int, int, int, str]] = []
    for section in sections:
        try:
            virtual_address = int(section.VirtualAddress)
            virtual_size = int(section.Misc_VirtualSize)
            raw_size = int(section.SizeOfRawData)
            raw_offset = int(section.PointerToRawData)
            name_bytes = bytes(section.Name)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise _FieldDataError("dotnet_field_pe_sections_invalid") from exc
        span = max(virtual_size, raw_size)
        if min(virtual_address, virtual_size, raw_size, raw_offset) < 0:
            raise _FieldDataError("dotnet_field_pe_sections_invalid")
        if any(
            value > 0xFFFFFFFF for value in (virtual_address, virtual_size, raw_size, raw_offset)
        ):
            raise _FieldDataError("dotnet_field_pe_sections_invalid")
        virtual_end = virtual_address + span
        raw_end = raw_offset + raw_size
        if virtual_end > 0x1_0000_0000 or raw_end > file_size:
            raise _FieldDataError("dotnet_field_pe_sections_invalid")
        if span and virtual_address < headers_size:
            raise _FieldDataError("dotnet_field_pe_sections_invalid")
        if raw_size and raw_offset < headers_size:
            raise _FieldDataError("dotnet_field_pe_sections_invalid")
        if span:
            virtual_ranges.append((virtual_address, virtual_end))
        if raw_size:
            raw_ranges.append((raw_offset, raw_end))
        section_name = name_bytes.split(b"\0", maxsplit=1)[0].decode("ascii", "replace")
        parsed_sections.append(
            (virtual_address, virtual_end, virtual_size, raw_size, raw_offset, section_name)
        )
    for ranges in (virtual_ranges, raw_ranges):
        ordered = sorted(ranges)
        if any(current[0] < previous[1] for previous, current in pairwise(ordered)):
            raise _FieldDataError("dotnet_field_pe_sections_invalid")

    end_rva = rva + length
    if rva < headers_size:
        if end_rva > headers_size or end_rva > file_size:
            raise _FieldDataError("dotnet_field_file_bounds_invalid")
        return rva, "headers", headers_size - rva
    for virtual_start, virtual_end, _virtual_size, raw_size, raw_offset, name in parsed_sections:
        if virtual_start <= rva < virtual_end:
            section_delta = rva - virtual_start
            if section_delta >= raw_size:
                raise _FieldDataError("dotnet_field_rva_uninitialized")
            if section_delta + length > raw_size:
                raise _FieldDataError("dotnet_field_file_bounds_invalid")
            file_offset = raw_offset + section_delta
            file_end = file_offset + length
            if file_end > file_size:
                raise _FieldDataError("dotnet_field_file_bounds_invalid")
            return file_offset, name, raw_size - section_delta
    raise _FieldDataError("dotnet_field_rva_not_mapped")


def _metadata_streams(payload: bytes, offset: int, size: int) -> dict[str, bytes]:
    if size > _MAX_METADATA_SCAN_BYTES or size < 20 or offset < 0 or offset + size > len(payload):
        raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
    data = payload[offset : offset + size]
    if data[:4] != b"BSJB":
        raise _FieldDataError("dotnet_field_metadata_invalid")
    try:
        version_size = struct.unpack_from("<I", data, 12)[0]
        cursor = 16 + version_size
        cursor = (cursor + 3) & ~3
        if cursor + 4 > len(data):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        _flags, stream_count = struct.unpack_from("<HH", data, cursor)
        cursor += 4
        if stream_count > 32:
            raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
        headers: list[tuple[str, int, int]] = []
        for _ in range(stream_count):
            if cursor + 8 > len(data):
                raise _FieldDataError("dotnet_field_metadata_invalid")
            stream_offset, stream_size = struct.unpack_from("<II", data, cursor)
            cursor += 8
            name_end = data.find(b"\0", cursor, min(len(data), cursor + 33))
            if name_end < 0:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            try:
                stream_name = data[cursor:name_end].decode("ascii")
            except UnicodeDecodeError as exc:
                raise _FieldDataError("dotnet_field_metadata_invalid") from exc
            cursor = (name_end + 4) & ~3
            if stream_offset + stream_size > size:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            headers.append((stream_name, stream_offset, stream_size))
        headers_end = cursor
        if len({name for name, _offset, _size in headers}) != len(headers):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        ranges = sorted(
            (stream_offset, stream_offset + stream_size)
            for _name, stream_offset, stream_size in headers
            if stream_size
        )
        if any(start < headers_end for start, _end in ranges) or any(
            current[0] < previous[1] for previous, current in pairwise(ranges)
        ):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        return {
            name: data[stream_offset : stream_offset + stream_size]
            for name, stream_offset, stream_size in headers
        }
    except struct.error as exc:
        raise _FieldDataError("dotnet_field_metadata_invalid") from exc


def _parse_field_tables(
    table_stream: bytes, strings_heap: bytes, blob_heap: bytes
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, int],
    list[dict[str, int]],
]:
    if len(table_stream) > _MAX_METADATA_SCAN_BYTES or len(table_stream) < 24:
        raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
    try:
        heap_sizes = table_stream[6]
        valid_mask = struct.unpack_from("<Q", table_stream, 8)[0]
        count_cursor = 24
        row_counts = [0] * 64
        for table_id in range(64):
            if valid_mask & (1 << table_id):
                if count_cursor + 4 > len(table_stream):
                    raise _FieldDataError("dotnet_field_metadata_invalid")
                row_counts[table_id] = struct.unpack_from("<I", table_stream, count_cursor)[0]
                count_cursor += 4
        if sum(row_counts) > _MAX_METADATA_ROWS:
            raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
        if row_counts[29] > _MAX_FIELD_RVA_ROWS:
            raise _FieldDataError("dotnet_field_metadata_limit_exceeded")
        if row_counts[3] or row_counts[5] or row_counts[7] or row_counts[19] or row_counts[22]:
            raise _FieldDataError("dotnet_field_metadata_invalid")

        table_ranges: dict[int, tuple[int, int, int]] = {}
        cursor = count_cursor
        if heap_sizes & 0x40:
            if cursor + 4 > len(table_stream):
                raise _FieldDataError("dotnet_field_metadata_invalid")
            cursor += 4
        for table_id in range(30):
            count = row_counts[table_id]
            if not count:
                continue
            columns = _METADATA_TABLE_COLUMNS.get(table_id)
            if columns is None:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            row_size = sum(
                _metadata_column_size(column, row_counts, heap_sizes) for column in columns
            )
            table_size = row_size * count
            if cursor + table_size > len(table_stream):
                raise _FieldDataError("dotnet_field_metadata_invalid")
            table_ranges[table_id] = (cursor, row_size, count)
            cursor += table_size

        decoded_string_bytes = 0

        def decode_metadata_string(index: int) -> str:
            nonlocal decoded_string_bytes
            decoded = _metadata_string(strings_heap, index)
            if index:
                end = strings_heap.find(b"\0", index, min(len(strings_heap), index + 1025))
                decoded_string_bytes += end - index
                if decoded_string_bytes > _MAX_DECODED_METADATA_STRING_BYTES:
                    raise _FieldDataError("dotnet_field_metadata_decoded_strings_limit_exceeded")
            return decoded

        type_refs: list[dict[str, Any]] = [
            {
                "rid": rid,
                "name": decode_metadata_string(row[1]),
                "namespace": decode_metadata_string(row[2]),
            }
            for rid, row in _metadata_rows(table_stream, 1, table_ranges, row_counts, heap_sizes)
        ]
        type_defs: list[dict[str, Any]] = []
        for rid, row in _metadata_rows(table_stream, 2, table_ranges, row_counts, heap_sizes):
            type_defs.append(
                {
                    "rid": rid,
                    "name": decode_metadata_string(row[1]),
                    "namespace": decode_metadata_string(row[2]),
                    "extends": row[3],
                    "field_start": row[4],
                }
            )
        field_records = [
            {
                "rid": rid,
                "name": decode_metadata_string(row[1]),
                "signature_index": row[2],
            }
            for rid, row in _metadata_rows(table_stream, 4, table_ranges, row_counts, heap_sizes)
        ]
        layouts: dict[int, int] = {}
        for _rid, row in _metadata_rows(table_stream, 15, table_ranges, row_counts, heap_sizes):
            class_size, parent = row[1], row[2]
            if parent < 1 or parent > len(type_defs) or parent in layouts:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            layouts[parent] = class_size
        field_rvas = [
            {"rva": row[0], "field_rid": row[1]}
            for _rid, row in _metadata_rows(table_stream, 29, table_ranges, row_counts, heap_sizes)
        ]
        if any(row["field_rid"] < 1 or row["field_rid"] > len(field_records) for row in field_rvas):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        if len({row["field_rid"] for row in field_rvas}) != len(field_rvas):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        previous_field_start = 1
        for item in type_defs:
            start = item["field_start"]
            if start < previous_field_start or start > len(field_records) + 1:
                raise _FieldDataError("dotnet_field_metadata_invalid")
            previous_field_start = start
        for index, item in enumerate(type_defs):
            start = item["field_start"]
            end = (
                type_defs[index + 1]["field_start"]
                if index + 1 < len(type_defs)
                else len(field_records) + 1
            )
            item["full_name"] = (
                f"{item['namespace']}.{item['name']}" if item["namespace"] else item["name"]
            )
            for rid in range(start, end):
                field_records[rid - 1]["type_name"] = item["full_name"]
        if any("type_name" not in item for item in field_records):
            raise _FieldDataError("dotnet_field_metadata_invalid")
        return field_records, type_defs, type_refs, layouts, field_rvas
    except struct.error as exc:
        raise _FieldDataError("dotnet_field_metadata_invalid") from exc


def _metadata_column_size(column: str, rows: Sequence[int], heap_sizes: int) -> int:
    if column == "u1":
        return 1
    if column == "u2":
        return 2
    if column == "u4":
        return 4
    if column == "string":
        return 4 if heap_sizes & 0x01 else 2
    if column == "guid":
        return 4 if heap_sizes & 0x02 else 2
    if column == "blob":
        return 4 if heap_sizes & 0x04 else 2
    if column.startswith("table:"):
        return 4 if rows[int(column.partition(":")[2])] >= 0x10000 else 2
    if column.startswith("coded:"):
        name = column.partition(":")[2]
        tag_bits, target_tables = _CODED_INDEX_TABLES[name]
        threshold = 1 << (16 - tag_bits)
        return 4 if max(rows[table] for table in target_tables) >= threshold else 2
    raise _FieldDataError("dotnet_field_metadata_invalid")


def _metadata_rows(
    stream: bytes,
    table_id: int,
    table_ranges: Mapping[int, tuple[int, int, int]],
    row_counts: Sequence[int],
    heap_sizes: int,
) -> list[tuple[int, tuple[int, ...]]]:
    table_range = table_ranges.get(table_id)
    if table_range is None:
        return []
    columns = _METADATA_TABLE_COLUMNS[table_id]
    start, row_size, count = table_range
    widths = [_metadata_column_size(column, row_counts, heap_sizes) for column in columns]
    rows: list[tuple[int, tuple[int, ...]]] = []
    for rid in range(1, count + 1):
        cursor = start + (rid - 1) * row_size
        values: list[int] = []
        for width in widths:
            values.append(int.from_bytes(stream[cursor : cursor + width], "little"))
            cursor += width
        rows.append((rid, tuple(values)))
    return rows


def _metadata_string(heap: bytes, index: int) -> str:
    if index == 0:
        return ""
    if index < 0 or index >= len(heap):
        raise _FieldDataError("dotnet_field_metadata_invalid")
    end = heap.find(b"\0", index, min(len(heap), index + 1025))
    if end < 0:
        raise _FieldDataError("dotnet_field_metadata_invalid")
    try:
        return heap[index:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _FieldDataError("dotnet_field_metadata_invalid") from exc


def _metadata_blob(heap: bytes, index: int) -> bytes:
    if index <= 0 or index >= len(heap):
        raise _FieldDataError("dotnet_field_metadata_invalid")
    size, cursor = _compressed_uint(heap, index)
    if size > _MAX_METADATA_SCAN_BYTES or cursor + size > len(heap):
        raise _FieldDataError("dotnet_field_metadata_invalid")
    return heap[cursor : cursor + size]


def _compressed_uint(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise _FieldDataError("dotnet_field_metadata_invalid")
    first = data[offset]
    if first & 0x80 == 0:
        return first, offset + 1
    if first & 0xC0 == 0x80 and offset + 2 <= len(data):
        return ((first & 0x3F) << 8) | data[offset + 1], offset + 2
    if first & 0xE0 == 0xC0 and offset + 4 <= len(data):
        value = ((first & 0x1F) << 24) | int.from_bytes(data[offset + 1 : offset + 4], "big")
        return value, offset + 4
    raise _FieldDataError("dotnet_field_metadata_invalid")


def _select_field(
    fields: list[dict[str, Any]],
    types: list[dict[str, Any]],
    field_rvas: list[dict[str, int]],
    *,
    token: str | None,
    type_name: str | None,
    field_name: str | None,
) -> dict[str, Any]:
    if token is not None:
        field_rid = int(token[2:], 16) & 0x00FFFFFF
        matches = [item for item in fields if item["rid"] == field_rid]
    else:
        matches = [
            item for item in fields if item["type_name"] == type_name and item["name"] == field_name
        ]
    if not matches:
        raise _FieldDataError("dotnet_field_not_found")
    if len(matches) != 1:
        raise _FieldDataError("dotnet_field_selector_ambiguous")
    selected = matches[0]
    rvas = [item["rva"] for item in field_rvas if item["field_rid"] == selected["rid"]]
    if not rvas:
        raise _FieldDataError("dotnet_field_rva_missing")
    if len(rvas) != 1:
        raise _FieldDataError("dotnet_field_rva_ambiguous")
    return {**selected, "rva": rvas[0]}


def _field_value_type_size(
    blob_heap: bytes,
    signature_index: int,
    type_defs: list[dict[str, Any]],
    type_refs: list[dict[str, Any]],
    layouts: Mapping[int, int],
) -> int | None:
    signature = _metadata_blob(blob_heap, signature_index)
    if len(signature) < 3 or signature[0] != 0x06:
        return None
    cursor = 1
    while cursor < len(signature) and signature[cursor] in {0x1F, 0x20}:
        _modifier, cursor = _compressed_uint(signature, cursor + 1)
    if cursor >= len(signature) or signature[cursor] != 0x11:
        return None
    encoded_type, _cursor = _compressed_uint(signature, cursor + 1)
    tag = encoded_type & 0x03
    type_rid = encoded_type >> 2
    if tag != 0 or type_rid < 1 or type_rid > len(type_defs):
        return None
    definition = type_defs[type_rid - 1]
    extends = definition["extends"]
    base_tag = extends & 0x03
    base_rid = extends >> 2
    if base_tag != 1 or base_rid < 1 or base_rid > len(type_refs):
        return None
    base_type = type_refs[base_rid - 1]
    if base_type["namespace"] != "System" or base_type["name"] not in {"ValueType", "Enum"}:
        return None
    class_size = layouts.get(type_rid)
    return class_size if isinstance(class_size, int) and 0 < class_size <= 0xFFFFFFFF else None


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
                        exit_code=result.exit_code,
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
    metadata_complete = complete and method_table.get("status") == "SUCCEEDED"
    ilspy_metadata_cause: str | None = None
    if (
        selected is None
        or _needs_ilspy_names([selected])
        or method_table.get("status") != "SUCCEEDED"
    ):
        typedef_table = await _run_monodis_operations(
            payload,
            ["--typedef"],
            parameters,
            settings,
            monodis_version=monodis_version,
        )
        ilspy_metadata = await _try_ilspy_metadata(
            payload,
            parameters,
            settings,
            type_table_output=str(typedef_table.get("stdout", "")),
        )
        if isinstance(ilspy_metadata, str):
            ilspy_metadata_cause = ilspy_metadata
        else:
            _ilspy_types, ilspy_methods, ilspy_complete = ilspy_metadata
            methods, replaced = _merge_ilspy_method_names(methods, ilspy_methods)
            replacement = next(
                (item for item in ilspy_methods if int(item["rid"]) == method_rid), None
            )
            if selected is None:
                selected = replacement
            elif replacement is not None:
                selected = {
                    **selected,
                    **{
                        key: replacement[key]
                        for key in ("type_name", "method_name", "signature")
                        if _missing_method_value(selected.get(key), key)
                    },
                }
            if replacement is not None or replaced:
                metadata_complete = ilspy_complete
    if selected is None:
        return _failure(
            "UNAVAILABLE",
            "monodis_method_rid_not_found",
            method_rid=method_rid,
            source=source,
            metadata=metadata,
            method_table_truncated=not complete,
            monodis_cause=method_table.get("available_reason"),
            monodis_exit_code=method_table.get("exit_code"),
            ilspy_metadata_cause=ilspy_metadata_cause,
        )

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
                    "status": "SUCCEEDED" if metadata_complete else "PARTIAL",
                    "engine": "ilspycmd",
                    "version": ILSPYCMD_VERSION,
                    "package_sha512": ILSPYCMD_PACKAGE_SHA512,
                    "source": source,
                    "metadata": metadata,
                    "method_rid": method_rid,
                    "method_token": f"0x{0x06000000 | method_rid:08x}",
                    "type_name": selected["type_name"],
                    "method_name": selected["method_name"],
                    "truncated": result.get("truncated", False) or not metadata_complete,
                }
            )
            if not metadata_complete:
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
            monodis_cause=(
                fallback.get("available_reason") or method_table.get("available_reason")
            ),
            monodis_exit_code=fallback.get("exit_code", method_table.get("exit_code")),
            monodis_error_detail=fallback.get("error_detail"),
            ilspy_metadata_cause=ilspy_metadata_cause,
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
    matched_types: set[str] = set()
    class_stack: list[str] = []
    current_start: int | None = None
    current_method_type: str | None = None
    for index, line in enumerate(lines):
        class_start = _IL_CLASS_START.match(line)
        if class_start is not None:
            name_match = _IL_CLASS_NAME.search(class_start.group("header"))
            if name_match is not None:
                name = _decode_il_identifier(name_match.group("name"))
                is_nested = re.search(r"(?:^|[ \t])nested(?:[ \t]|$)", class_start.group("header"))
                class_stack.append(
                    f"{class_stack[-1]}/{name}" if is_nested and class_stack else name
                )
        if ".method " in line:
            current_start = index
            current_method_type = class_stack[-1] if class_stack else None
        match = _METHOD_END.match(line)
        if match is not None:
            marker_type = _decode_il_identifier(match.group("type"))
            candidate_type = current_method_type or marker_type
            marker_matches_class = current_method_type is None or _type_marker_matches(
                current_method_type, marker_type
            )
            if (
                marker_matches_class
                and _type_names_match(candidate_type, type_name)
                and _decode_il_identifier(match.group("name")) == method_name
            ):
                start = current_start if current_start is not None else index
                blocks.append("".join(lines[start : index + 1]))
                matched_types.add(candidate_type)
            current_start = None
            current_method_type = None
        if _IL_CLASS_END.match(line) and class_stack:
            class_stack.pop()
    if len(matched_types) > 1:
        return None
    return blocks[occurrence] if 0 <= occurrence < len(blocks) else None


def _decode_il_identifier(value: str) -> str:
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]

    def replace_escape(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0] in {"u", "U", "x"}:
            return chr(int(escape[1:], 16))
        return {"\\": "\\", "'": "'", '"': '"', "n": "\n", "r": "\r", "t": "\t"}.get(
            escape, "\\" + escape
        )

    return _IL_IDENTIFIER_ESCAPE.sub(replace_escape, value)


def _type_names_match(candidate: str, requested: str) -> bool:
    candidate_name = _decode_il_identifier(candidate).replace("+", "/")
    requested_name = _decode_il_identifier(requested).replace("+", "/")
    if candidate_name == requested_name:
        return True
    candidate_dotted = candidate_name.replace("/", ".")
    requested_dotted = requested_name.replace("/", ".")
    if candidate_dotted == requested_dotted:
        return True
    if "." not in requested_name and "/" not in requested_name:
        return candidate_name.rsplit("/", 1)[-1].rsplit(".", 1)[-1] == requested_name
    return candidate_dotted.endswith(f".{requested_dotted}")


def _type_marker_matches(candidate: str, marker: str) -> bool:
    candidate_name = _decode_il_identifier(candidate).replace("+", "/")
    marker_name = _decode_il_identifier(marker).replace("+", "/")
    if candidate_name == marker_name or candidate_name.replace("/", ".") == marker_name.replace(
        "/", "."
    ):
        return True
    candidate_leaf = candidate_name.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    marker_leaf = marker_name.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return candidate_leaf == marker_leaf


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
            method_name = _decode_il_identifier(name_match.group("name"))
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


def _needs_ilspy_names(records: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        not str(record.get("type_name", "")).strip()
        or not str(record.get("method_name", "")).strip()
        or record.get("method_name") == "unparsed"
        for record in records
    )


def _missing_method_value(value: Any, key: str) -> bool:
    return not str(value or "").strip() or (key == "method_name" and value == "unparsed")


def _merge_ilspy_method_names(
    records: Sequence[Mapping[str, Any]], ilspy_records: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    replacements = {int(item["rid"]): item for item in ilspy_records}
    if not records:
        return [dict(item) for item in ilspy_records], bool(ilspy_records)

    merged: list[dict[str, Any]] = []
    replaced = False
    seen: set[int] = set()
    for record in records:
        rid = int(record["rid"])
        seen.add(rid)
        updated = dict(record)
        replacement = replacements.get(rid)
        if replacement is not None:
            for key in ("type_name", "method_name", "signature"):
                if _missing_method_value(updated.get(key), key) and replacement.get(key):
                    updated[key] = replacement[key]
                    replaced = True
        merged.append(updated)
    for rid, replacement in replacements.items():
        if rid not in seen:
            merged.append(dict(replacement))
            replaced = True
    return sorted(merged, key=lambda item: int(item["rid"])), replaced


async def _try_ilspy_metadata(
    payload: bytes,
    parameters: ConfinedAnalysisJobParameters,
    settings: Settings,
    *,
    type_table_output: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool] | str:
    provenance = ilspycmd_provenance(
        address_space_limit_bytes=settings.dotnet_analysis_address_space_limit_bytes
    )
    executable = shutil.which("ilspycmd")
    if provenance is None or executable is None:
        return "ilspycmd_unavailable_or_unattested"
    try:
        with tempfile.TemporaryDirectory(prefix="cti-ilspy-metadata-", dir="/tmp") as directory:
            sample_path = Path(directory) / "sample.bin"
            sample_path.write_bytes(payload)
            sample_path.chmod(0o400)
            result = await run_analysis_subprocess(
                [executable, "--disable-updatecheck", "--ilcode", str(sample_path)],
                timeout_seconds=min(parameters.timeout_seconds, 45.0),
                output_limit=min(
                    settings.analysis_local_monodis_max_output_bytes,
                    max(parameters.max_output_bytes * 16, _PAGE_CHARS_MAX),
                ),
                memory_limit_bytes=settings.dotnet_analysis_address_space_limit_bytes,
                environment=_SUBPROCESS_ENV,
            )
    except OSError as exc:
        return f"ilspycmd_metadata_failed:{type(exc).__name__}"
    if result.status is AnalysisSubprocessStatus.TIMED_OUT:
        return "ilspycmd_metadata_timeout"
    if result.status not in {
        AnalysisSubprocessStatus.SUCCEEDED,
        AnalysisSubprocessStatus.OUTPUT_LIMIT,
    }:
        return "ilspycmd_metadata_failed"
    type_records, method_records = _parse_ilspy_metadata(
        result.stdout.decode("utf-8", "replace"), type_table_output
    )
    if not type_records and not method_records:
        return "ilspycmd_metadata_not_parsed"
    return type_records, method_records, result.status is AnalysisSubprocessStatus.SUCCEEDED


def _parse_ilspy_metadata(
    output: str, type_table_output: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    type_rows = [
        (int(match.group("rid")), int(match.group("mlist")))
        for line in type_table_output.splitlines()
        if (match := _TYPE_METHOD_LIST_ROW.match(line)) is not None
    ]
    parsed_types: list[dict[str, Any]] = []
    class_stack: list[dict[str, Any]] = []
    method_header: list[str] | None = None
    method_owner: dict[str, Any] | None = None

    for line in output.splitlines():
        class_start = _IL_CLASS_START.match(line)
        if class_start is not None:
            name_match = _IL_CLASS_NAME.search(class_start.group("header"))
            if name_match is not None:
                name = _decode_il_identifier(name_match.group("name"))
                nested = re.search(r"(?:^|[ \t])nested(?:[ \t]|$)", class_start.group("header"))
                type_name = (
                    f"{class_stack[-1]['type_name']}/{name}"
                    if nested is not None and class_stack
                    else name
                )
                record: dict[str, Any] = {"type_name": type_name, "methods": []}
                parsed_types.append(record)
                class_stack.append(record)
            continue

        if ".method " in line:
            method_header = [line]
            method_owner = class_stack[-1] if class_stack else None
        elif method_header is not None:
            method_header.append(line)

        if method_header is not None and ("cil managed" in line or "{" in line):
            header = " ".join(" ".join(method_header).split()).split("{", maxsplit=1)[0]
            method_match = _METHOD_NAME.search(header)
            if method_match is not None and method_owner is not None:
                method_owner["methods"].append(
                    {
                        "method_name": _decode_il_identifier(method_match.group("name")),
                        "signature": header.strip(),
                    }
                )
            method_header = None
            method_owner = None

        if _IL_CLASS_END.match(line) and class_stack:
            class_stack.pop()

    if not type_rows or len(type_rows) != len(parsed_types):
        return [], []

    type_records: list[dict[str, Any]] = []
    method_records: list[dict[str, Any]] = []
    for index, ((type_rid, method_start), ilspy_type) in enumerate(
        zip(type_rows, parsed_types, strict=True)
    ):
        type_name = str(ilspy_type["type_name"])
        if type_name != "<Module>":
            type_records.append(
                {
                    "rid": type_rid,
                    "token": f"0x{0x02000000 | type_rid:08x}",
                    "type_name": type_name,
                }
            )
        methods = ilspy_type["methods"]
        if not isinstance(methods, list) or method_start < 1:
            continue
        next_method_start = type_rows[index + 1][1] if index + 1 < len(type_rows) else None
        if next_method_start is not None:
            expected_methods = max(0, next_method_start - method_start)
            if expected_methods != len(methods):
                continue
        for method_offset, method in enumerate(methods):
            rid = method_start + method_offset
            if next_method_start is not None and rid >= next_method_start:
                break
            method_records.append(
                {
                    "rid": rid,
                    "token": f"0x{0x06000000 | rid:08x}",
                    "type_name": type_name,
                    "method_name": method["method_name"],
                    "signature": method["signature"],
                }
            )
    return type_records, sorted(method_records, key=lambda item: int(item["rid"]))


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
    engine: str | None = None,
    monodis_cause: str | None = None,
    monodis_exit_code: Any = None,
) -> dict[str, Any]:
    offset = _bounded_int(parameters.get("offset", "0"), 0, 10**7)
    limit = _bounded_int(parameters.get("limit", "100"), 1, _PAGE_SIZE_MAX)
    if offset is None or limit is None:
        return _failure("POLICY_DENIED", "dotnet_pagination_invalid")
    page = list(records[offset : offset + limit])
    next_offset = offset + len(page) if offset + len(page) < len(records) else None
    obfuscated, obfuscation_metrics = _obfuscation_assessment(records)
    result = {
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
            "likely_renamed" if obfuscated else "not_detected_by_name_heuristic"
        ),
        "obfuscation_metrics": obfuscation_metrics,
        "elapsed_ms": int(max(0, time.monotonic() - started) * 1000),
    }
    if engine is not None:
        result["engine"] = engine
    if monodis_cause is not None:
        result["monodis_cause"] = monodis_cause
    if monodis_exit_code is not None:
        result["monodis_exit_code"] = monodis_exit_code
    return result


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


_OBFUSCATION_MIN_NAME_COUNT = 20
_UNICODE_INVISIBLE_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cf", "Cc"})
_GENERATED_SPECIAL_METHOD_PREFIXES = ("get_", "set_", "add_", "remove_", "raise_", "op_")


def _is_compiler_generated_special_name(name: str) -> bool:
    return (
        name in {".ctor", ".cctor", "<Module>", "Finalize"}
        or (name.startswith("<") and ">" in name)
        or name.startswith(_GENERATED_SPECIAL_METHOD_PREFIXES)
    )


def _is_single_character_identifier(name: str) -> bool:
    return len(name) == 1 and (name.isidentifier() or name == "$")


def _is_unicode_invisible_name(name: str) -> bool:
    return bool(name) and all(
        unicodedata.category(character) in _UNICODE_INVISIBLE_CATEGORIES for character in name
    )


def _obfuscation_assessment(
    records: Sequence[Mapping[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    names: list[str] = []
    excluded_special_name_count = 0
    for item in records:
        name = str(item.get("method_name") or item.get("type_name") or "")
        if not name:
            continue
        if _is_compiler_generated_special_name(name):
            excluded_special_name_count += 1
            continue
        names.append(name)

    name_count = len(names)
    population_met = name_count >= _OBFUSCATION_MIN_NAME_COUNT
    single_character_identifier_count = sum(_is_single_character_identifier(name) for name in names)
    unicode_invisible_name_count = sum(_is_unicode_invisible_name(name) for name in names)
    renamed_name_count = single_character_identifier_count + unicode_invisible_name_count
    renamed_name_ratio = renamed_name_count / name_count if name_count else 0.0
    majority_criterion_met = population_met and renamed_name_ratio >= 0.5

    short_ascii_identifier_count = sum(
        re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,1}", name) is not None for name in names
    )
    short_ascii_identifier_ratio = short_ascii_identifier_count / name_count if name_count else 0.0
    short_ascii_criterion_met = population_met and short_ascii_identifier_ratio >= 0.8

    unicode_names = [name for name in names if not name.isascii()]
    short_unicode_names = [name for name in unicode_names if len(name) == 1]
    frequencies: dict[str, int] = {}
    for name in short_unicode_names:
        frequencies[name] = frequencies.get(name, 0) + 1
    most_repeated_unicode_single_character = max(frequencies.values(), default=0)
    repetition_threshold = max(10, (name_count + 9) // 10)
    unicode_single_character_share = len(short_unicode_names) / name_count if name_count else 0.0
    repeated_name_share = (
        most_repeated_unicode_single_character / len(short_unicode_names)
        if short_unicode_names
        else 0.0
    )
    unicode_concentration_criterion_met = population_met and (
        len(unicode_names) >= 10
        and len(unicode_names) / name_count >= 0.1
        and len(short_unicode_names) >= 10
        and unicode_single_character_share >= 0.1
        and most_repeated_unicode_single_character >= repetition_threshold
        and repeated_name_share >= 0.3
    )

    criteria = {
        "single_character_or_invisible_name_majority": majority_criterion_met,
        "short_ascii_identifier_ratio": short_ascii_criterion_met,
        "repeated_unicode_single_character_concentration": unicode_concentration_criterion_met,
    }
    metrics = {
        "eligible_name_count": name_count,
        "excluded_special_name_count": excluded_special_name_count,
        "minimum_name_count": _OBFUSCATION_MIN_NAME_COUNT,
        "minimum_name_count_met": population_met,
        "single_character_identifier_count": single_character_identifier_count,
        "unicode_invisible_name_count": unicode_invisible_name_count,
        "renamed_name_count": renamed_name_count,
        "renamed_name_ratio": round(renamed_name_ratio, 4),
        "short_ascii_identifier_count": short_ascii_identifier_count,
        "short_ascii_identifier_ratio": round(short_ascii_identifier_ratio, 4),
        "unicode_single_character_count": len(short_unicode_names),
        "unicode_single_character_ratio": round(unicode_single_character_share, 4),
        "most_repeated_unicode_single_character_count": most_repeated_unicode_single_character,
        "most_repeated_unicode_single_character_ratio": round(repeated_name_share, 4),
        "criteria": criteria,
    }
    return any(criteria.values()), metrics


def _obfuscation_likelihood(records: Sequence[Mapping[str, Any]]) -> bool:
    return _obfuscation_assessment(records)[0]


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
