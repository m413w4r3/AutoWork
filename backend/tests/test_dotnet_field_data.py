from __future__ import annotations

import asyncio
import hashlib
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.config import Settings
from cti_app.workers import dotnet_analysis as dotnet_analysis_module
from cti_app.workers.confined_analysis import _execute_tool
from cti_app.workers.dotnet_analysis import (
    _FieldDataError,
    _metadata_streams,
    _parse_field_tables,
    _rva_to_file_offset,
    run_dotnet_analysis,
)


def _field_data_image(
    *,
    with_layout: bool = True,
    class_size: int = 16,
    section_raw_size: int | None = None,
    file_overlay_size: int = 0,
) -> bytes:
    """Build a synthetic PE/CLI with one 16-byte FieldRVA value type."""
    strings = bytearray(b"\0")

    def string_index(value: str) -> int:
        index = len(strings)
        strings.extend(value.encode("ascii") + b"\0")
        return index

    module_name = string_index("field-data.dll")
    value_type_name = string_index("ValueType")
    system_namespace = string_index("System")
    module_type_name = string_index("<Module>")
    owner_type_name = string_index("<PrivateImplementationDetails>")
    data_type_name = string_index("__StaticArrayInitTypeSize=16")
    field_name = string_index("3")
    blob = b"\0\x03\x06\x11\x0c"

    valid_tables = (0, 1, 2, 4, 15, 29) if with_layout else (0, 1, 2, 4, 29)
    valid_mask = sum(1 << table for table in valid_tables)
    tables = bytearray(struct.pack("<IBBBBQQ", 0, 2, 0, 0, 1, valid_mask, 0))
    row_counts = [1, 1, 3, 1, 1, 1] if with_layout else [1, 1, 3, 1, 1]
    tables.extend(struct.pack(f"<{len(row_counts)}I", *row_counts))
    tables.extend(struct.pack("<HHHHH", 0, module_name, 1, 0, 0))
    tables.extend(struct.pack("<HHH", 6, value_type_name, system_namespace))
    tables.extend(
        struct.pack(
            "<IHHHHH",
            0,
            module_type_name,
            0,
            0,
            1,
            1,
        )
    )
    tables.extend(struct.pack("<IHHHHH", 0, owner_type_name, 0, 0, 1, 1))
    tables.extend(struct.pack("<IHHHHH", 0x00100000, data_type_name, 0, 5, 2, 1))
    tables.extend(struct.pack("<HHH", 0x0016, field_name, 1))
    if with_layout:
        tables.extend(struct.pack("<HIH", 1, class_size, 3))
    tables.extend(struct.pack("<IH", 0x1800, 1))

    streams = (
        ("#~", bytes(tables)),
        ("#Strings", bytes(strings)),
        ("#Blob", blob),
        ("#GUID", b"g" * 16),
    )
    prefix = bytearray(struct.pack("<IHHII", 0x424A5342, 1, 1, 0, 4) + b"v4.0")
    prefix.extend(struct.pack("<HH", 0, len(streams)))
    header_size = sum(8 + ((len(name.encode("ascii")) + 1 + 3) & ~3) for name, _ in streams)
    data_start = (len(prefix) + header_size + 3) & ~3
    stream_headers = bytearray()
    stream_data = bytearray()
    current_offset = data_start
    for name, data in streams:
        aligned = (current_offset + 3) & ~3
        stream_data.extend(b"\0" * (aligned - current_offset))
        current_offset = aligned
        name_bytes = name.encode("ascii") + b"\0"
        stream_headers.extend(struct.pack("<II", current_offset, len(data)))
        stream_headers.extend(name_bytes)
        stream_headers.extend(b"\0" * ((-len(name_bytes)) & 3))
        stream_data.extend(data)
        current_offset += len(data)
    metadata = bytes(prefix + stream_headers) + bytes(stream_data)

    section_raw_size = section_raw_size or max(0x2000, 0x800 + class_size)
    file_size = 0x200 + section_raw_size + file_overlay_size
    image = bytearray(file_size)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 0x84, 0x14C, 1, 0, 0, 0, 224, 0x2102)
    optional = 0x98
    struct.pack_into("<H", image, optional, 0x10B)
    struct.pack_into("<I", image, optional + 32, 0x1000)
    struct.pack_into("<I", image, optional + 36, 0x200)
    struct.pack_into("<I", image, optional + 56, 0x3000)
    struct.pack_into("<I", image, optional + 60, 0x200)
    struct.pack_into("<I", image, optional + 92, 16)
    struct.pack_into("<II", image, optional + 96 + 14 * 8, 0x1000, 72)
    section = optional + 224
    image[section : section + 8] = b".data\0\0\0"
    struct.pack_into("<IIII", image, section + 8, section_raw_size, 0x1000, section_raw_size, 0x200)
    struct.pack_into("<I", image, section + 36, 0xC0000040)

    struct.pack_into(
        "<IHH16I",
        image,
        0x200,
        72,
        2,
        5,
        0x1100,
        len(metadata),
        1,
        0,
        *([0] * 12),
    )
    image[0x300 : 0x300 + len(metadata)] = metadata
    image[0xA00 : min(file_size, 0xA00 + class_size)] = bytes(
        index % 256 for index in range(1, min(file_size - 0xA00, class_size) + 1)
    )
    return bytes(image)


def _parameters(operation: str, **values: str) -> ConfinedAnalysisJobParameters:
    payload = _field_data_image()
    return ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        operation_key="confined-analysis:" + "a" * 64,
        tool=ConfinedAnalysisTool.DOTNET_ILSPY,
        timeout_seconds=30,
        max_output_bytes=16_384,
        analysis_parameters={"operation": operation, **values},
    )


def test_field_data_reader_resolves_field_rva_and_returns_synthetic_bytes() -> None:
    payload = _field_data_image()
    result = asyncio.run(
        run_dotnet_analysis(
            _parameters("field_data", type_name="<PrivateImplementationDetails>", field_name="3"),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: pytest.fail(
                "field data must not decompile methods"
            ),
        )
    )

    assert result["status"] == "SUCCEEDED"
    assert result["field_token"] == "0x04000001"
    assert result["field_name"] == "3"
    assert result["rva"] == 0x1800
    assert result["section_name"] == ".data"
    assert result["file_offset"] == 0xA00
    assert result["length_returned"] == 16
    assert result["size_known"] is True
    assert result["completeness"] == "complete"
    assert result["sha256"] == hashlib.sha256(bytes(range(1, 17))).hexdigest()
    assert result["hex_dump"] == bytes(range(1, 17)).hex(" ")
    assert result["truncated"] is False

    token_result = asyncio.run(
        run_dotnet_analysis(
            _parameters("field_data", field_token="0x04000001", offset="4", length="4"),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert token_result["length_returned"] == 4
    assert token_result["offset"] == 4
    assert token_result["hex_dump"] == "05 06 07 08"
    assert token_result["truncated"] is True


def test_confined_field_data_parameters_reject_null_selector_keys() -> None:
    valid = _parameters("field_data", field_token="0x04000001")
    with pytest.raises(ValueError, match="dotnet field selector is invalid"):
        ConfinedAnalysisRequest(
            investigation_id=valid.investigation_id,
            artifact_id=valid.artifact_id,
            sha256=valid.artifact_sha256,
            tool=valid.tool,
            timeout_seconds=valid.timeout_seconds,
            max_output_bytes=valid.max_output_bytes,
            association_id=uuid4(),
            sample_id=uuid4(),
            analysis_parameters={
                "operation": "field_data",
                "field_token": "0x04000001",
                "type_name": None,  # type: ignore[dict-item]
            },
        )


def test_confined_worker_dispatch_reads_only_the_synthetic_field_data() -> None:
    payload = _field_data_image()
    result = asyncio.run(
        _execute_tool(
            _parameters(
                "field_data",
                type_name="<PrivateImplementationDetails>",
                field_name="3",
            ),
            sample=object(),
            payload=payload,
            blob_id=uuid4(),
            blobs=None,  # type: ignore[arg-type]
            uow_factory=lambda: None,  # type: ignore[arg-type]
            settings=Settings(_env_file=None),
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["file_offset"] == 0xA00
    assert result["sha256"] == hashlib.sha256(bytes(range(1, 17))).hexdigest()


@pytest.mark.asyncio
async def test_resources_path_only_requests_manifest_and_does_not_expose_rva_or_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_monodis(_payload, operations, *_args, **_kwargs):
        calls.append(tuple(operations))
        return {"status": "SUCCEEDED", "version": "synthetic", "stdout": "Manifestresource table"}

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    result = await run_dotnet_analysis(
        _parameters("resources"),
        _field_data_image(),
        Settings(_env_file=None),
        monodis_version=lambda: "synthetic",
        extract_monodis_method_body=lambda *_args: None,
    )

    assert calls == [("--manifest",)]
    assert "rva" not in result
    assert "file_offset" not in result


def test_unknown_field_size_requires_caller_length() -> None:
    payload = _field_data_image(with_layout=False)
    unknown_size = asyncio.run(
        run_dotnet_analysis(
            _parameters("field_data", type_name="<PrivateImplementationDetails>", field_name="3"),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert unknown_size["status"] == "POLICY_DENIED"
    assert unknown_size["detail_code"] == "dotnet_field_data_length_required"

    bounded_read = asyncio.run(
        run_dotnet_analysis(
            _parameters(
                "field_data",
                type_name="<PrivateImplementationDetails>",
                field_name="3",
                length="4",
            ),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert bounded_read["status"] == "SUCCEEDED"
    assert bounded_read["field_size_bytes"] is None
    assert bounded_read["hex_dump"] == "01 02 03 04"
    assert bounded_read["size_in_file_bounds"]["field_start"] == 0xA00
    assert bounded_read["size_in_file_bounds"]["field_end_exclusive"] is None
    assert bounded_read["size_in_file_bounds"]["read_start"] == 0xA00
    assert bounded_read["size_in_file_bounds"]["read_end_exclusive"] == 0xA04
    assert bounded_read["size_known"] is False
    assert bounded_read["completeness"] == "unknown"
    assert bounded_read["truncated"] is False


def _type_ref_table_stream(*, rows: int, name_index: int) -> bytes:
    valid_mask = 1 << 1
    table_stream = bytearray(struct.pack("<IBBBBQQI", 0, 2, 0, 0, 1, valid_mask, 0, rows))
    table_stream.extend(struct.pack("<HHH", 0, name_index, name_index) * rows)
    return bytes(table_stream)


def test_type_ref_decoding_has_an_aggregate_string_budget() -> None:
    long_heap = b"\0" + (b"N" * 1023) + b"\0"
    repeated_refs = _type_ref_table_stream(rows=9_000, name_index=1)
    with pytest.raises(_FieldDataError) as raised:
        _parse_field_tables(repeated_refs, long_heap, b"\0")
    assert raised.value.detail_code == "dotnet_field_metadata_decoded_strings_limit_exceeded"

    normal_stream = _type_ref_table_stream(rows=1, name_index=1)
    parsed = _parse_field_tables(normal_stream, b"\0Normal\0", b"\0")
    assert parsed[2][0]["name"] == "Normal"


def test_total_metadata_row_cap_is_independent_of_field_rva_cap() -> None:
    total_rows = 200_001
    table_stream = bytearray(
        struct.pack(
            "<IBBBBQQII",
            0,
            2,
            0,
            0,
            1,
            (1 << 1) | (1 << 29),
            0,
            total_rows - 1,
            1,
        )
    )
    with pytest.raises(_FieldDataError) as raised:
        _parse_field_tables(bytes(table_stream), b"\0", b"\0")
    assert raised.value.detail_code == "dotnet_field_metadata_limit_exceeded"


def test_inferred_field_size_is_clamped_to_4096_bytes() -> None:
    payload = _field_data_image(class_size=16_384)
    result = asyncio.run(
        run_dotnet_analysis(
            _parameters("field_data", field_token="0x04000001"),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["length_returned"] == 4096
    assert result["truncated"] is True


def test_inferred_field_size_must_fit_section_raw_bounds() -> None:
    payload = _field_data_image(class_size=8192, section_raw_size=0x2000, file_overlay_size=8192)
    result = asyncio.run(
        run_dotnet_analysis(
            _parameters("field_data", field_token="0x04000001"),
            payload,
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert result["status"] == "FAILED"
    assert result["detail_code"] == "dotnet_field_file_bounds_invalid"


def _fake_pe(sections: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        OPTIONAL_HEADER=SimpleNamespace(SizeOfHeaders=0x200),
        sections=sections,
    )


def _section(
    *,
    virtual_address: int = 0x1000,
    virtual_size: int = 0x100,
    raw_size: int = 0x100,
    raw_offset: int = 0x200,
    name: bytes = b".data\0\0\0",
) -> SimpleNamespace:
    return SimpleNamespace(
        VirtualAddress=virtual_address,
        Misc_VirtualSize=virtual_size,
        SizeOfRawData=raw_size,
        PointerToRawData=raw_offset,
        Name=name,
    )


def test_rva_to_file_offset_maps_section_and_headers() -> None:
    assert _rva_to_file_offset(_fake_pe((_section(),)), 0x500, 0x1010, 4) == (
        0x210,
        ".data",
        0xF0,
    )
    assert _rva_to_file_offset(_fake_pe(()), 0x500, 0x40, 4) == (0x40, "headers", 0x1C0)


@pytest.mark.parametrize(
    ("pe", "file_size", "rva", "length", "detail_code"),
    [
        (
            _fake_pe((_section(),)),
            0x500,
            0x1800,
            1,
            "dotnet_field_rva_not_mapped",
        ),
        (
            _fake_pe((_section(virtual_size=0x200, raw_size=0x100),)),
            0x500,
            0x1150,
            1,
            "dotnet_field_rva_uninitialized",
        ),
        (
            _fake_pe(
                (
                    _section(virtual_size=0x200),
                    _section(virtual_address=0x1100, raw_offset=0x400),
                )
            ),
            0x600,
            0x1010,
            1,
            "dotnet_field_pe_sections_invalid",
        ),
        (
            _fake_pe((_section(raw_size=0x200, raw_offset=0x400),)),
            0x500,
            0x1010,
            1,
            "dotnet_field_pe_sections_invalid",
        ),
        (_fake_pe((_section(),)), 0x500, 0, 1, "dotnet_field_rva_invalid"),
        (
            _fake_pe((_section(),)),
            0x500,
            0x1_0000_0000,
            1,
            "dotnet_field_rva_invalid",
        ),
        (
            _fake_pe((_section(),)),
            0x500,
            0xFFFFFFFF,
            2,
            "dotnet_field_rva_invalid",
        ),
    ],
)
def test_rva_to_file_offset_refuses_unmapped_or_malformed_ranges(
    pe: SimpleNamespace,
    file_size: int,
    rva: int,
    length: int,
    detail_code: str,
) -> None:
    with pytest.raises(_FieldDataError) as raised:
        _rva_to_file_offset(pe, file_size, rva, length)
    assert raised.value.detail_code == detail_code


def test_field_rva_rows_and_metadata_scan_are_bounded() -> None:
    table_stream = bytearray(struct.pack("<IBBBBQQI", 0, 2, 0, 0, 1, 1 << 29, 0, 8193))
    with pytest.raises(_FieldDataError) as raised:
        _parse_field_tables(bytes(table_stream), b"\0", b"\0")
    assert raised.value.detail_code == "dotnet_field_metadata_limit_exceeded"

    with pytest.raises(_FieldDataError) as raised:
        _metadata_streams(b"", 0, 16 * 1024 * 1024 + 1)
    assert raised.value.detail_code == "dotnet_field_metadata_limit_exceeded"


def test_field_data_reader_refuses_lengths_over_the_return_cap() -> None:
    result = asyncio.run(
        run_dotnet_analysis(
            _parameters(
                "field_data",
                type_name="<PrivateImplementationDetails>",
                field_name="3",
                length="4097",
            ),
            _field_data_image(),
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("field data must not invoke monodis"),
            extract_monodis_method_body=lambda *_args: None,
        )
    )
    assert result["status"] == "POLICY_DENIED"
    assert result["detail_code"] == "dotnet_field_data_length_exceeds_cap"
