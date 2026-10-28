from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
import time
import zipfile

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisTool,
    validate_archive_tool_request,
)
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.workers import archive_commands
from cti_app.workers.archive_commands import run_7z_archive_command, run_zip_members


def _zip_fixture(
    members: tuple[tuple[str, bytes], ...], *, compression: int = zipfile.ZIP_STORED
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return output.getvalue()


def _read_result(result: dict[str, object]) -> dict[str, object]:
    return json.loads(str(result["stdout"]))


def _redirect_archive_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[str | None]:
    original = archive_commands.tempfile.TemporaryDirectory
    requested_dirs: list[str | None] = []

    def temporary_directory(*args, **kwargs):
        requested_dirs.append(kwargs.get("dir"))
        kwargs["dir"] = tmp_path
        return original(*args, **kwargs)

    monkeypatch.setattr(archive_commands.tempfile, "TemporaryDirectory", temporary_directory)
    return requested_dirs


async def _run_zip(payload: bytes, **options: object) -> dict[str, object]:
    return await run_zip_members(
        payload,
        member_prefix=options.get("member_prefix"),  # type: ignore[arg-type]
        max_members=int(options.get("max_members", 2000)),
        max_output_bytes=int(options.get("max_output_bytes", 512 * 1024)),
        tool_version="autowork-zip-members-v1",
    )


def _set_member_flags(payload: bytes, mask: int) -> bytes:
    patched = bytearray(payload)
    local_offset = patched.find(b"PK\x03\x04")
    central_offset = patched.find(b"PK\x01\x02")
    assert local_offset >= 0 and central_offset >= 0
    flags = struct.unpack_from("<H", patched, local_offset + 6)[0]
    struct.pack_into("<H", patched, local_offset + 6, flags | mask)
    flags = struct.unpack_from("<H", patched, central_offset + 8)[0]
    struct.pack_into("<H", patched, central_offset + 8, flags | mask)
    return bytes(patched)


def _set_declared_member_size(payload: bytes, size: int) -> bytes:
    patched = bytearray(payload)
    local_offset = patched.find(b"PK\x03\x04")
    central_offset = patched.find(b"PK\x01\x02")
    assert local_offset >= 0 and central_offset >= 0
    struct.pack_into("<I", patched, local_offset + 22, size)
    struct.pack_into("<I", patched, central_offset + 24, size)
    return bytes(patched)


def _set_declared_compressed_size(payload: bytes, size: int) -> bytes:
    patched = bytearray(payload)
    local_offset = patched.find(b"PK\x03\x04")
    central_offset = patched.find(b"PK\x01\x02")
    assert local_offset >= 0 and central_offset >= 0
    struct.pack_into("<I", patched, local_offset + 18, size)
    struct.pack_into("<I", patched, central_offset + 20, size)
    return bytes(patched)


def _corrupt_stored_member(payload: bytes) -> bytes:
    patched = bytearray(payload)
    local_offset = patched.find(b"PK\x03\x04")
    name_length = struct.unpack_from("<H", patched, local_offset + 26)[0]
    extra_length = struct.unpack_from("<H", patched, local_offset + 28)[0]
    data_offset = local_offset + 30 + name_length + extra_length
    patched[data_offset] ^= 0x01
    return bytes(patched)


@pytest.mark.asyncio
async def test_zip_members_lists_and_hashes_members_in_central_directory_order() -> None:
    payload = _zip_fixture(
        (
            ("Resources/", b""),
            ("Resources/PsProxy.dll", b"proxy bytes"),
            ("RuntimeBroker.exe", b"runtime bytes"),
        )
    )

    result = _read_result(await _run_zip(payload))

    assert result["archive_status"] == "ok"
    assert [item["name"] for item in result["members"]] == [
        "Resources/",
        "Resources/PsProxy.dll",
        "RuntimeBroker.exe",
    ]
    assert result["members"][0]["is_dir"] is True
    assert result["members"][0]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert result["members"][1]["sha256"] == hashlib.sha256(b"proxy bytes").hexdigest()
    assert result["members"][1]["status"] == "hashed"
    assert result["members"][1]["compress_type"] == zipfile.ZIP_STORED
    assert result["members"][1]["crc32"] == zipfile.crc32(b"proxy bytes")
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_zip_members_filters_prefix_and_caps_member_count_deterministically() -> None:
    payload = _zip_fixture(
        (("Resources/one.dll", b"one"), ("Resources/two.dll", b"two"), ("other.dll", b"three"))
    )

    first = _read_result(await _run_zip(payload, member_prefix="Resources/", max_members=1))
    second = _read_result(await _run_zip(payload, member_prefix="Resources/", max_members=1))

    assert first == second
    assert first["matching_members_total"] == 2
    assert [item["name"] for item in first["members"]] == ["Resources/one.dll"]
    assert first["truncated"] is True
    assert first["truncation_reasons"] == ["member_limit"]


@pytest.mark.asyncio
async def test_zip_members_flags_output_limit_and_stays_within_json_cap() -> None:
    payload = _zip_fixture(((f"{'a' * 400}.dll", b"content"),))

    raw_result = await _run_zip(payload, max_output_bytes=512)
    report = _read_result(raw_result)

    assert len(str(raw_result["stdout"]).encode()) <= 512
    assert report["truncated"] is True
    assert report["truncation_reasons"] == ["output_limit"]


@pytest.mark.asyncio
async def test_zip_members_rejects_overlarge_directory_before_materializing_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _zip_fixture(tuple((f"{index}.bin", b"") for index in range(2001)))

    def forbidden(*_args, **_kwargs):
        pytest.fail("over-cap ZIP must be rejected before ZipFile materializes entries")

    monkeypatch.setattr(archive_commands.zipfile, "ZipFile", forbidden)
    report = _read_result(await _run_zip(payload, max_members=1))

    assert report["archive_status"] == "member_limit"
    assert report["archive_members_total"] == 2001
    assert report["members"] == []
    assert report["truncated"] is True
    assert report["truncation_reasons"] == ["member_limit"]


@pytest.mark.asyncio
async def test_zip_members_marks_encrypted_members_without_reading_them() -> None:
    payload = _set_member_flags(_zip_fixture((("secret.dll", b"secret"),)), 0x1)

    member = _read_result(await _run_zip(payload))["members"][0]

    assert member["status"] == "encrypted"
    assert member["sha256"] is None
    assert member["actual_size"] == 0


@pytest.mark.asyncio
async def test_zip_members_refuses_declared_zip_bomb_size_before_reading() -> None:
    payload = _set_declared_member_size(
        _zip_fixture((("large.bin", b"small"),)), 64 * 1024 * 1024 + 1
    )

    member = _read_result(await _run_zip(payload))["members"][0]

    assert member["status"] == "declared_member_size_limit"
    assert member["actual_size"] is None
    assert member["sha256"] is None


@pytest.mark.asyncio
async def test_zip_members_enforces_aggregate_uncompressed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _zip_fixture((("first.bin", b"1234"), ("second.bin", b"567")))
    monkeypatch.setattr(archive_commands, "LOCAL_ARCHIVE_MAX_MEMBER_BYTES", 4)
    monkeypatch.setattr(archive_commands, "LOCAL_ARCHIVE_MAX_TOTAL_BYTES", 6)

    members = _read_result(await _run_zip(payload))["members"]

    assert [item["status"] for item in members] == ["hashed", "declared_total_size_limit"]
    assert members[1]["actual_size"] is None


@pytest.mark.asyncio
async def test_zip_members_refuses_excessive_compression_ratio_before_reading() -> None:
    payload = _set_declared_compressed_size(_zip_fixture((("ratio.bin", b"A" * 1001),)), 1)

    member = _read_result(await _run_zip(payload))["members"][0]

    assert member["status"] == "compression_ratio_limit"
    assert member["actual_size"] is None
    assert member["sha256"] is None


@pytest.mark.asyncio
async def test_zip_members_yields_between_bounded_reads_for_worker_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _zip_fixture((("large.bin", b"A" * (2 * 64 * 1024)),))
    original_read = zipfile.ZipExtFile.read

    def slow_read(member: zipfile.ZipExtFile, size: int = -1) -> bytes:
        time.sleep(0.01)
        return original_read(member, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", slow_read)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.001):
            await _run_zip(payload)


@pytest.mark.asyncio
async def test_zip_members_returns_typed_non_zip_truncated_and_bad_crc_statuses() -> None:
    non_zip = _read_result(await _run_zip(b"plain text"))
    truncated = _read_result(await _run_zip(_zip_fixture((("file.bin", b"content"),))[:-8]))
    corrupt = _read_result(
        await _run_zip(_corrupt_stored_member(_zip_fixture((("file.bin", b"content"),))))
    )

    assert non_zip["archive_status"] == "not_a_zip"
    assert truncated["archive_status"] == "corrupt_archive"
    assert corrupt["members"][0]["status"] == "bad_crc"


@pytest.mark.asyncio
async def test_zip_members_writes_no_files_and_never_calls_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("zip_members must use zipfile in memory")

    monkeypatch.setattr(archive_commands.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", forbidden)

    await _run_zip(_zip_fixture((("inside.dll", b"bytes"),)))

    assert list(tmp_path.iterdir()) == []


def test_archive_preset_and_member_options_are_strictly_allowlisted() -> None:
    validate_archive_tool_request(
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        (),
        {"max_members": "12", "archive_preset": "infected"},
    )

    with pytest.raises(ValueError, match="preset"):
        validate_archive_tool_request(
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
            (),
            {"max_members": "12", "archive_preset": "custom"},
        )
    with pytest.raises(ValueError, match="prefix"):
        validate_archive_tool_request(
            ConfinedAnalysisTool.ARCHIVE_LIST,
            (),
            {"member_prefix": "../\n", "max_members": "12"},
        )


_SEVEN_Z_LISTING = (
    "Path = sample.bin\nType = zip\nPhysical Size = 123\n\n----------\n"
    "Path = ../outside.dll\nFolder = -\nSize = 6\nPacked Size = 6\n"
    "CRC = 8D777F38\nEncrypted = -\nMethod = Copy\n\n"
    "Path = secret.dat\nFolder = -\nSize = 6\nPacked Size = 3\n"
    "CRC = 8D777F38\nEncrypted = +\nMethod = AES-256\n\n"
)


@pytest.mark.asyncio
async def test_7z_extract_hash_streams_member_to_stdout_with_no_path_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[list[str]] = []

    async def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "l":
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.SUCCEEDED,
                0,
                _SEVEN_Z_LISTING.encode(),
                b"",
            )
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, b"abcdef", b"")

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", fake_run)
    requested_tempdirs = _redirect_archive_tempdir(monkeypatch, tmp_path)

    result = await run_7z_archive_command(
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        b"archive bytes",
        member_prefix="../",
        max_members=10,
        archive_preset="infected",
        max_output_bytes=32 * 1024,
        timeout_seconds=20,
        memory_limit_bytes=128 * 1024 * 1024,
        tool_version="16.02",
    )
    report = _read_result(result)

    assert report["archive_type"] == "zip"
    assert report["members"][0]["name"] == "../outside.dll"
    assert report["members"][0]["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
    assert report["members"][0]["status"] == "hashed"
    extract_argv = calls[1]
    assert "-so" in extract_argv
    assert "-spd" in extract_argv
    assert "-pinfected" in extract_argv
    assert extract_argv[-1] == "../outside.dll"
    assert not any("outside.dll" in path.name for path in tmp_path.iterdir())
    assert list(tmp_path.iterdir()) == []
    assert requested_tempdirs == ["/tmp"]


@pytest.mark.asyncio
async def test_7z_list_returns_bounded_metadata_without_extracting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[list[str]] = []

    async def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED,
            0,
            _SEVEN_Z_LISTING.encode(),
            b"",
        )

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", fake_run)
    requested_tempdirs = _redirect_archive_tempdir(monkeypatch, tmp_path)

    result = await run_7z_archive_command(
        ConfinedAnalysisTool.ARCHIVE_LIST,
        b"archive bytes",
        member_prefix=None,
        max_members=1,
        archive_preset="none",
        max_output_bytes=32 * 1024,
        timeout_seconds=20,
        memory_limit_bytes=128 * 1024 * 1024,
        tool_version="16.02",
    )
    report = _read_result(result)

    assert report["archive_status"] == "ok"
    assert report["matching_members_total"] == 2
    assert len(report["members"]) == 1
    assert report["members"][0]["name"] == "../outside.dll"
    assert report["members"][0]["status"] == "listed"
    assert report["members"][0]["sha256"] is None
    assert report["truncated"] is True
    assert len(calls) == 1
    assert calls[0][1] == "l"
    assert list(tmp_path.iterdir()) == []
    assert requested_tempdirs == ["/tmp"]


@pytest.mark.asyncio
async def test_7z_extract_hash_reports_encrypted_and_oversized_members_without_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    listing = (
        "Path = sample.bin\nType = 7z\nPhysical Size = 100\n\n----------\n"
        "Path = encrypted.dll\nFolder = -\nSize = 10\nPacked Size = 10\nEncrypted = +\n\n"
        "Path = bomb.bin\nFolder = -\nSize = 67108865\nPacked Size = 100\nEncrypted = -\n\n"
    )
    calls: list[list[str]] = []

    async def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED, 0, listing.encode(), b""
        )

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", fake_run)
    requested_tempdirs = _redirect_archive_tempdir(monkeypatch, tmp_path)

    result = await run_7z_archive_command(
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        b"archive bytes",
        member_prefix=None,
        max_members=10,
        archive_preset="none",
        max_output_bytes=32 * 1024,
        timeout_seconds=20,
        memory_limit_bytes=128 * 1024 * 1024,
        tool_version="16.02",
    )
    report = _read_result(result)

    assert [item["status"] for item in report["members"]] == [
        "encrypted_password_required",
        "declared_member_size_limit",
    ]
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []
    assert requested_tempdirs == ["/tmp"]


@pytest.mark.asyncio
async def test_7z_extract_hash_enforces_aggregate_uncompressed_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    listing = (
        "Path = sample.bin\nType = 7z\nPhysical Size = 100\n\n----------\n"
        "Path = first.bin\nFolder = -\nSize = 4\nPacked Size = 4\nEncrypted = -\n\n"
        "Path = second.bin\nFolder = -\nSize = 3\nPacked Size = 3\nEncrypted = -\n\n"
    )
    calls: list[list[str]] = []

    async def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "l":
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.SUCCEEDED, 0, listing.encode(), b""
            )
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, b"1234", b"")

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", fake_run)
    requested_tempdirs = _redirect_archive_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(archive_commands, "LOCAL_ARCHIVE_MAX_MEMBER_BYTES", 4)
    monkeypatch.setattr(archive_commands, "LOCAL_ARCHIVE_MAX_TOTAL_BYTES", 6)

    result = await run_7z_archive_command(
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        b"archive bytes",
        member_prefix=None,
        max_members=10,
        archive_preset="none",
        max_output_bytes=32 * 1024,
        timeout_seconds=20,
        memory_limit_bytes=128 * 1024 * 1024,
        tool_version="16.02",
    )
    report = _read_result(result)

    assert [item["status"] for item in report["members"]] == [
        "hashed",
        "declared_total_size_limit",
    ]
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []
    assert requested_tempdirs == ["/tmp"]


@pytest.mark.asyncio
async def test_7z_extract_hash_reports_bad_crc_and_timeout_statuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    listing = (
        "Path = sample.bin\nType = zip\nPhysical Size = 100\n\n----------\n"
        "Path = broken.dll\nFolder = -\nSize = 4\nPacked Size = 4\nEncrypted = -\n\n"
    )
    calls: list[list[str]] = []

    async def bad_crc(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "l":
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.SUCCEEDED, 0, listing.encode(), b""
            )
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.NON_ZERO_EXIT, 2, b"", b"CRC Failed"
        )

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", bad_crc)
    requested_tempdirs = _redirect_archive_tempdir(monkeypatch, tmp_path)
    corrupt_report = _read_result(
        await run_7z_archive_command(
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
            b"archive bytes",
            member_prefix=None,
            max_members=10,
            archive_preset="none",
            max_output_bytes=32 * 1024,
            timeout_seconds=20,
            memory_limit_bytes=128 * 1024 * 1024,
            tool_version="16.02",
        )
    )

    async def listing_only(argv, **_kwargs):
        calls.append(list(argv))
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED, 0, listing.encode(), b""
        )

    monkeypatch.setattr(archive_commands, "run_analysis_subprocess", listing_only)
    timed_out_report = _read_result(
        await run_7z_archive_command(
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
            b"archive bytes",
            member_prefix=None,
            max_members=10,
            archive_preset="none",
            max_output_bytes=32 * 1024,
            timeout_seconds=0.000001,
            memory_limit_bytes=128 * 1024 * 1024,
            tool_version="16.02",
        )
    )

    assert corrupt_report["members"][0]["status"] == "bad_crc"
    assert timed_out_report["members"][0]["status"] == "timeout"
    assert list(tmp_path.iterdir()) == []
    assert requested_tempdirs == ["/tmp", "/tmp"]


def test_seven_zip_version_reads_the_p7zip_package_version_from_a_double_banner(
    monkeypatch,
) -> None:
    import subprocess
    from types import SimpleNamespace

    from cti_app.workers import confined_analysis

    banner = (
        "\n7-Zip [64] 26.02 : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25\n"
        "p7zip Version 16.02 (locale=C.UTF-8,Utf16=on,HugeFiles=on,64 bits)\n"
    )
    monkeypatch.setattr(confined_analysis.shutil, "which", lambda name: "/usr/bin/7z")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=banner, stderr=""),
    )

    assert confined_analysis._seven_zip_version() == "16.02"
