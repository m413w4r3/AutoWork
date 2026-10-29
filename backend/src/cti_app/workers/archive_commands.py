from __future__ import annotations

import asyncio
import hashlib
import json
import lzma
import re
import struct
import tempfile
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any, TypedDict

from cti_app.application.confined_analysis import ConfinedAnalysisTool
from cti_app.domain.local_analysis import (
    LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO,
    LOCAL_ARCHIVE_MAX_MEMBER_BYTES,
    LOCAL_ARCHIVE_MAX_MEMBERS,
    LOCAL_ARCHIVE_MAX_TOTAL_BYTES,
)
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)

_ZIP_OUTPUT_SCHEMA = "zip-members.v1"
_ARCHIVE_OUTPUT_SCHEMA = "archive-members.v1"
_MAX_REPORTED_NAME_CHARS = 512
_CHUNK_BYTES = 64 * 1024
_MAX_ZIP_METADATA_BYTES = 32 * 1024 * 1024
_CRC_ERROR = re.compile(r"crc", re.I)
_PASSWORD_ERROR = re.compile(r"password|encrypted", re.I)


class _ArchiveEntry(TypedDict):
    name: str
    size: int | None
    packed_size: int | None
    crc32: str | None
    is_dir: bool
    encrypted: bool
    method: str | None
    attributes: str | None
    is_link: bool


async def run_zip_members(
    payload: bytes,
    *,
    member_prefix: str | None,
    max_members: int,
    max_output_bytes: int,
    tool_version: str,
) -> dict[str, Any]:
    """Hash bounded ZIP members in memory, preserving central-directory order."""
    archive_members_total, preflight_status = _zip_directory_preflight(
        payload, max_members=LOCAL_ARCHIVE_MAX_MEMBERS
    )
    if preflight_status is not None:
        text, truncated = _encode_report(
            {
                "schema_version": _ZIP_OUTPUT_SCHEMA,
                "archive_status": preflight_status,
                "archive_members_total": archive_members_total or 0,
                "matching_members_total": None,
                "uncompressed_bytes_read": 0,
            },
            (),
            max_output_bytes,
            (preflight_status,),
            0,
        )
        return _worker_result(text, truncated, "zip_members", tool_version)
    try:
        archive = zipfile.ZipFile(BytesIO(payload), mode="r")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError):
        archive_status = "corrupt_archive" if _looks_like_zip(payload) else "not_a_zip"
        text, truncated = _encode_report(
            {
                "schema_version": _ZIP_OUTPUT_SCHEMA,
                "archive_status": archive_status,
                "archive_members_total": 0,
                "matching_members_total": 0,
                "uncompressed_bytes_read": 0,
            },
            (),
            max_output_bytes,
            (),
            0,
        )
        return _worker_result(text, truncated, "zip_members", tool_version)

    with archive:
        infos = archive.infolist()
        selected: list[tuple[int, zipfile.ZipInfo]] = []
        matching_count = 0
        for index, info in enumerate(infos):
            if member_prefix is not None and not info.filename.startswith(member_prefix):
                continue
            matching_count += 1
            if len(selected) < max_members:
                selected.append((index, info))

        records: list[dict[str, object]] = []
        total_read = 0
        for index, info in selected:
            record: dict[str, object] = {
                "index": index,
                "name": info.filename[:_MAX_REPORTED_NAME_CHARS],
                "name_truncated": len(info.filename) > _MAX_REPORTED_NAME_CHARS,
                "header_offset": info.header_offset,
                "compressed_size": info.compress_size,
                "uncompressed_size": info.file_size,
                "actual_size": 0,
                "crc32": info.CRC,
                "compress_type": info.compress_type,
                "is_dir": info.is_dir(),
                "sha256": None,
            }
            if info.flag_bits & 0x1:
                record["status"] = "encrypted"
            elif info.is_dir():
                record["actual_size"] = 0
                record["sha256"] = hashlib.sha256(b"").hexdigest()
                record["status"] = "directory"
            elif info.file_size > LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
                record["status"] = "declared_member_size_limit"
                record["actual_size"] = None
            elif info.file_size > max(1, info.compress_size) * LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO:
                record["status"] = "compression_ratio_limit"
                record["actual_size"] = None
            elif info.file_size > LOCAL_ARCHIVE_MAX_TOTAL_BYTES - total_read:
                record["status"] = "declared_total_size_limit"
                record["actual_size"] = None
            else:
                digest = hashlib.sha256()
                actual_size = 0
                read_limit = min(
                    LOCAL_ARCHIVE_MAX_MEMBER_BYTES,
                    LOCAL_ARCHIVE_MAX_TOTAL_BYTES - total_read,
                )
                try:
                    with archive.open(info, "r") as member:
                        while actual_size < read_limit:
                            chunk = member.read(min(_CHUNK_BYTES, read_limit - actual_size))
                            await asyncio.sleep(0)
                            if not chunk:
                                break
                            digest.update(chunk)
                            actual_size += len(chunk)
                    total_read += actual_size
                    record["actual_size"] = actual_size
                    record["sha256"] = digest.hexdigest()
                    if actual_size != info.file_size:
                        record["status"] = "actual_size_mismatch"
                    else:
                        record["status"] = "hashed"
                except NotImplementedError:
                    total_read += actual_size
                    record["actual_size"] = actual_size
                    record["status"] = "unsupported_compression"
                except (
                    zipfile.BadZipFile,
                    OSError,
                    EOFError,
                    RuntimeError,
                    ValueError,
                    struct.error,
                    lzma.LZMAError,
                    zlib.error,
                ) as exc:
                    total_read += actual_size
                    record["actual_size"] = actual_size
                    record["sha256"] = digest.hexdigest() if actual_size else None
                    record["status"] = (
                        "bad_crc"
                        if isinstance(exc, zipfile.BadZipFile) and _CRC_ERROR.search(str(exc))
                        else "corrupt_member"
                    )
            records.append(record)

        reasons = ("member_limit",) if matching_count > max_members else ()
        text, truncated = _encode_report(
            {
                "schema_version": _ZIP_OUTPUT_SCHEMA,
                "archive_status": "ok",
                "archive_members_total": len(infos),
                "matching_members_total": matching_count,
                "uncompressed_bytes_read": total_read,
            },
            records,
            max_output_bytes,
            reasons,
            total_read,
        )
    return _worker_result(text, truncated, "zip_members", tool_version)


def _zip_directory_preflight(payload: bytes, *, max_members: int) -> tuple[int | None, str | None]:
    """Count central-directory records before zipfile materializes ZipInfo objects."""
    stream = BytesIO(payload)
    try:
        end_record = getattr(zipfile, "_EndRecData")(stream)  # noqa: B009
    except (OSError, ValueError, struct.error, zipfile.BadZipFile):
        end_record = None
    if end_record is None:
        return None, "corrupt_archive" if _looks_like_zip(payload) else "not_a_zip"

    declared_count = int(end_record[4])
    central_size = int(end_record[5])
    central_offset = int(end_record[6])
    end_record_offset = int(end_record[9])
    concat = end_record_offset - central_size - central_offset
    central_start = central_offset + concat
    central_end = central_start + central_size
    if (
        central_start < 0
        or central_end < central_start
        or central_end > len(payload)
        or central_size < declared_count * 46
    ):
        return None, "corrupt_archive"
    if declared_count > max_members:
        return declared_count, "member_limit"

    cursor = central_start
    actual_count = 0
    metadata_bytes = 0
    while cursor < central_end and actual_count <= max_members:
        if payload[cursor : cursor + 4] == b"PK\x05\x05":
            break  # Optional central-directory digital signature record.
        if cursor + 46 > central_end:
            return None, "corrupt_archive"
        try:
            values = struct.unpack_from("<4s6H3I5H2I", payload, cursor)
        except struct.error:
            return None, "corrupt_archive"
        if values[0] != b"PK\x01\x02":
            return None, "corrupt_archive"
        name_size, extra_size, comment_size = values[10:13]
        record_size = 46 + name_size + extra_size + comment_size
        if cursor + record_size > central_end:
            return None, "corrupt_archive"
        metadata_bytes += name_size + extra_size + comment_size
        if metadata_bytes > _MAX_ZIP_METADATA_BYTES:
            return actual_count + 1, "metadata_limit"
        cursor += record_size
        actual_count += 1
    if actual_count > max_members:
        return actual_count, "member_limit"
    if actual_count != declared_count:
        return None, "corrupt_archive"
    return actual_count, None


async def run_7z_archive_command(
    tool: ConfinedAnalysisTool,
    payload: bytes,
    *,
    member_prefix: str | None,
    max_members: int,
    archive_preset: str,
    max_output_bytes: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
    tool_version: str | None,
    member_path: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if tool not in {
        ConfinedAnalysisTool.ARCHIVE_LIST,
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER,
    }:
        raise ValueError("7z archive command is invalid")
    executable = "/usr/bin/7z"
    password_arg = "-p" if archive_preset == "none" else f"-p{archive_preset}"
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="cti-archive-", dir="/tmp") as directory:
        sample_path = Path(directory) / "sample.bin"
        sample_path.write_bytes(payload)
        sample_path.chmod(0o400)
        listing = await run_analysis_subprocess(
            [
                executable,
                "l",
                "-slt",
                "-bd",
                "-sccUTF-8",
                password_arg,
                "--",
                str(sample_path),
            ],
            timeout_seconds=min(timeout_seconds, 30),
            output_limit=max(1, min(max_output_bytes, 1024 * 1024)),
            memory_limit_bytes=memory_limit_bytes,
            environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        archive_type, entries = _parse_7z_listing(listing.stdout, sample_path.name)
        listing_error = _7z_status(listing, listing.stderr)
        listing_truncated = listing.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
        if listing.status not in {
            AnalysisSubprocessStatus.SUCCEEDED,
            AnalysisSubprocessStatus.OUTPUT_LIMIT,
        }:
            report = {
                "schema_version": _ARCHIVE_OUTPUT_SCHEMA,
                "archive_status": listing_error,
                "archive_type": archive_type,
                "archive_members_total": len(entries),
                "uncompressed_bytes_read": 0,
            }
            text, truncated = _encode_report(report, (), max_output_bytes, (), 0)
            return _worker_result(text, truncated, tool.value, tool_version)
        if listing_truncated and tool in {
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER,
        }:
            report = {
                "schema_version": _ARCHIVE_OUTPUT_SCHEMA,
                "archive_status": "listing_output_limit",
                "archive_type": archive_type,
                "archive_members_total": len(entries),
                "uncompressed_bytes_read": 0,
            }
            text, truncated = _encode_report(
                report, (), max_output_bytes, ("listing_output_limit",), 0
            )
            return _worker_result(text, True, tool.value, tool_version)

        if tool is ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER:
            if member_path is None:
                raise ValueError("archive member path is required")
            return await _extract_7z_archive_member(
                executable=executable,
                sample_path=sample_path,
                entries=entries,
                member_path=member_path,
                expected_sha256=expected_sha256,
                archive_preset=archive_preset,
                password_arg=password_arg,
                deadline=deadline,
                timeout_seconds=timeout_seconds,
                memory_limit_bytes=memory_limit_bytes,
                tool_version=tool_version,
            )

        matched = [
            entry
            for entry in entries
            if member_prefix is None or entry["name"].startswith(member_prefix)
        ]
        member_limit = max_members
        selected = matched[:member_limit]
        records: list[dict[str, object]] = []
        total_read = 0
        name_counts = Counter(str(entry["name"]) for entry in matched)
        for entry in selected:
            record = _archive_entry_record(entry)
            size = entry["size"]
            if entry.get("is_dir"):
                record["status"] = "directory"
            elif tool is ConfinedAnalysisTool.ARCHIVE_LIST:
                record["status"] = "encrypted" if entry.get("encrypted") else "listed"
            elif name_counts[str(entry["name"])] > 1:
                record["status"] = "duplicate_name_ambiguous"
            elif entry.get("encrypted") and archive_preset == "none":
                record["status"] = "encrypted_password_required"
            elif size is None or size < 0:
                record["status"] = "invalid_size_metadata"
            elif size > LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
                record["status"] = "declared_member_size_limit"
            elif (
                LOCAL_ARCHIVE_MAX_TOTAL_BYTES - total_read <= 0
                or size > LOCAL_ARCHIVE_MAX_TOTAL_BYTES - total_read
            ):
                record["status"] = "declared_total_size_limit"
            elif size > max(1, entry.get("packed_size") or 0) * (
                LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO
            ):
                record["status"] = "compression_ratio_limit"
            elif "\x00" in str(entry["name"]):
                record["status"] = "invalid_member_name"
            else:
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    record["status"] = "timeout"
                    records.append(record)
                    continue
                extract = await run_analysis_subprocess(
                    [
                        executable,
                        "x",
                        "-so",
                        "-y",
                        "-bd",
                        "-spd",
                        "-sccUTF-8",
                        password_arg,
                        "--",
                        str(sample_path),
                        str(entry["name"]),
                    ],
                    timeout_seconds=min(remaining_timeout, 30),
                    output_limit=max(1, size),
                    memory_limit_bytes=memory_limit_bytes,
                    environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                )
                data = extract.stdout
                total_read += len(data)
                if extract.status is AnalysisSubprocessStatus.SUCCEEDED:
                    if len(data) != size:
                        record["status"] = "actual_size_mismatch"
                    else:
                        record["sha256"] = hashlib.sha256(data).hexdigest()
                        record["status"] = "hashed"
                elif extract.status is AnalysisSubprocessStatus.OUTPUT_LIMIT:
                    record["status"] = "actual_size_or_member_limit"
                elif extract.status is AnalysisSubprocessStatus.TIMED_OUT:
                    record["status"] = "timeout"
                elif extract.status is AnalysisSubprocessStatus.FAILED_TO_START:
                    record["status"] = "7z_unavailable"
                else:
                    record["status"] = _member_failure_status(extract.stderr, entry)
            records.append(record)

    truncation_reasons = []
    if len(matched) > member_limit:
        truncation_reasons.append("member_limit")
    if listing_truncated:
        truncation_reasons.append("listing_output_limit")
    report = {
        "schema_version": _ARCHIVE_OUTPUT_SCHEMA,
        "archive_status": "partial" if listing_truncated else "ok",
        "archive_type": archive_type,
        "archive_members_total": len(entries),
        "matching_members_total": len(matched),
        "uncompressed_bytes_read": total_read,
    }
    text, truncated = _encode_report(
        report,
        records,
        max_output_bytes,
        tuple(truncation_reasons),
        total_read,
    )
    return _worker_result(text, truncated, tool.value, tool_version)


def _parse_7z_listing(
    content: bytes, archive_basename: str
) -> tuple[str | None, list[_ArchiveEntry]]:
    text = content.decode("utf-8", "replace").replace("\r\n", "\n")
    blocks = re.split(r"\n(?:-{5,}\n|\n+)", text)
    archive_type: str | None = None
    entries: list[_ArchiveEntry] = []
    for block_index, block in enumerate(blocks):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if " = " not in line:
                continue
            key, value = line.split(" = ", 1)
            fields[key] = value
        if block_index == 0:
            archive_type = fields.get("Type")
        name = fields.get("Path")
        if not name or (block_index == 0 and name == archive_basename):
            continue
        if "Size" not in fields and "Folder" not in fields:
            continue
        try:
            size = int(fields["Size"]) if "Size" in fields else 0
        except ValueError:
            size = None
        try:
            packed_size = int(fields["Packed Size"]) if "Packed Size" in fields else None
        except ValueError:
            packed_size = None
        entries.append(
            _ArchiveEntry(
                name=name,
                size=size,
                packed_size=packed_size,
                crc32=fields.get("CRC"),
                is_dir=fields.get("Folder") == "+",
                encrypted=fields.get("Encrypted", "-") in {"+", "1", "true"},
                method=fields.get("Method"),
                attributes=fields.get("Attributes"),
                is_link=_is_7z_link_entry(fields),
            )
        )
    return archive_type, entries


def _is_7z_link_entry(fields: Mapping[str, str]) -> bool:
    if any(fields.get(key) for key in ("Symbolic Link", "Hard Link", "Link")):
        return True
    return any(
        re.fullmatch(r"l[rwxStT-]{9}", token, re.I)
        for token in fields.get("Attributes", "").split()
    )


async def _extract_7z_archive_member(
    *,
    executable: str,
    sample_path: Path,
    entries: list[_ArchiveEntry],
    member_path: str,
    expected_sha256: str | None,
    archive_preset: str,
    password_arg: str,
    deadline: float,
    timeout_seconds: float,
    memory_limit_bytes: int,
    tool_version: str | None,
) -> dict[str, Any]:
    def refusal(status: str) -> dict[str, Any]:
        result = _worker_result(
            json.dumps(
                {"schema_version": _ARCHIVE_OUTPUT_SCHEMA, "status": status},
                separators=(",", ":"),
            ),
            False,
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER.value,
            tool_version,
        )
        result["member_status"] = status
        return result

    if not _safe_archive_member_path(member_path):
        return refusal("invalid_member_name")
    matches = [entry for entry in entries if entry["name"] == member_path]
    if not matches:
        return refusal("member_not_found")
    if len(matches) != 1:
        return refusal("duplicate_name_ambiguous")
    entry = matches[0]
    if entry["is_dir"]:
        return refusal("directory")
    if entry["is_link"]:
        return refusal("link_not_allowed")
    if entry["encrypted"] and archive_preset == "none":
        return refusal("encrypted_password_required")
    size = entry["size"]
    packed_size = entry["packed_size"]
    if size is None or size < 0:
        return refusal("invalid_size_metadata")
    if size > LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
        return refusal("declared_member_size_limit")
    if size > LOCAL_ARCHIVE_MAX_TOTAL_BYTES:
        return refusal("declared_total_size_limit")
    if size > max(1, packed_size or 0) * LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO:
        return refusal("compression_ratio_limit")
    remaining_timeout = deadline - time.monotonic()
    if remaining_timeout <= 0:
        return refusal("timeout")
    extracted = await run_analysis_subprocess(
        [
            executable,
            "x",
            "-so",
            "-y",
            "-bd",
            "-spd",
            "-sccUTF-8",
            password_arg,
            "--",
            str(sample_path),
            member_path,
        ],
        timeout_seconds=min(remaining_timeout, timeout_seconds, 30),
        output_limit=max(1, size),
        memory_limit_bytes=memory_limit_bytes,
        environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    data = extracted.stdout
    if extracted.status is AnalysisSubprocessStatus.TIMED_OUT:
        return refusal("timeout")
    if extracted.status is AnalysisSubprocessStatus.FAILED_TO_START:
        return refusal("7z_unavailable")
    if extracted.status is AnalysisSubprocessStatus.OUTPUT_LIMIT:
        return refusal("actual_size_or_member_limit")
    if extracted.status is not AnalysisSubprocessStatus.SUCCEEDED:
        return refusal(_member_failure_status(extracted.stderr, entry))
    if len(data) != size or len(data) > LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
        return refusal("actual_size_mismatch")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        return refusal("expected_sha256_mismatch")
    result = _worker_result(
        json.dumps(
            {
                "schema_version": _ARCHIVE_OUTPUT_SCHEMA,
                "member_path": member_path,
                "member_size": len(data),
                "member_sha256": digest,
                "status": "extracted",
            },
            separators=(",", ":"),
        ),
        False,
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER.value,
        tool_version,
    )
    result["member_status"] = "extracted"
    result["member_path"] = member_path
    result["member_size"] = len(data)
    result["member_sha256"] = digest
    result["_member_bytes"] = data
    return result


def _safe_archive_member_path(value: str) -> bool:
    if (
        not value
        or len(value) > 1024
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _archive_entry_record(entry: _ArchiveEntry) -> dict[str, object]:
    name = str(entry["name"])
    return {
        "name": name[:_MAX_REPORTED_NAME_CHARS],
        "name_truncated": len(name) > _MAX_REPORTED_NAME_CHARS,
        "size": entry.get("size"),
        "compressed_size": entry.get("packed_size"),
        "crc32": entry.get("crc32"),
        "method": entry.get("method"),
        "is_dir": entry.get("is_dir") is True,
        "encrypted": entry.get("encrypted") is True,
        "sha256": None,
        "status": "encrypted" if entry.get("encrypted") is True else "listed",
    }


def _member_failure_status(stderr: bytes, entry: _ArchiveEntry) -> str:
    text = stderr.decode("utf-8", "replace")[:4096]
    if _CRC_ERROR.search(text):
        return "bad_crc"
    if _PASSWORD_ERROR.search(text) or entry.get("encrypted"):
        return "encrypted_or_bad_password"
    if re.search(r"data error|headers error|unexpected end|can not open", text, re.I):
        return "corrupt_member"
    return "extract_failed"


def _7z_status(result: Any, stderr: bytes) -> str:
    if result.status is AnalysisSubprocessStatus.TIMED_OUT:
        return "timeout"
    if result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT:
        return "listing_output_limit"
    if result.status is AnalysisSubprocessStatus.FAILED_TO_START:
        return "7z_unavailable"
    if result.status is AnalysisSubprocessStatus.NON_ZERO_EXIT:
        text = stderr.decode("utf-8", "replace")[:4096]
        if _PASSWORD_ERROR.search(text):
            return "encrypted_password_required"
        return "corrupt_or_unsupported_archive"
    return "ok"


def _encode_report(
    header: Mapping[str, object],
    records: tuple[dict[str, object], ...] | list[dict[str, object]],
    max_output_bytes: int,
    truncation_reasons: tuple[str, ...],
    uncompressed_bytes_read: int,
) -> tuple[str, bool]:
    max_output_bytes = max(128, max_output_bytes)
    prefix = json.dumps(header, ensure_ascii=True, separators=(",", ":"))[:-1]
    prefix += ',"members":['
    current_bytes = len(prefix.encode("utf-8"))
    encoded_records: list[str] = []
    reasons = list(truncation_reasons)
    for record in records:
        encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        separator_bytes = 1 if encoded_records else 0
        prospective_count = len(encoded_records) + 1
        prospective_reasons = sorted(set(reasons))
        suffix = _report_suffix(
            prospective_count,
            prospective_reasons,
            bool(prospective_reasons),
            uncompressed_bytes_read,
        )
        if (
            current_bytes + separator_bytes + len(encoded.encode("utf-8")) + len(suffix)
            > max_output_bytes
        ):
            reasons.append("output_limit")
            break
        encoded_records.append(encoded)
        current_bytes += separator_bytes + len(encoded.encode("utf-8"))

    truncated = bool(reasons)
    suffix = _report_suffix(
        len(encoded_records),
        sorted(set(reasons)),
        truncated,
        uncompressed_bytes_read,
    )
    text = prefix + ",".join(encoded_records) + suffix
    if len(text.encode("utf-8")) > max_output_bytes:
        # The minimal report fits the command's configured output budgets.
        minimal = {
            "schema_version": header.get("schema_version", _ARCHIVE_OUTPUT_SCHEMA),
            "archive_status": header.get("archive_status", "partial"),
            "members": [],
            "members_reported": 0,
            "uncompressed_bytes_read": uncompressed_bytes_read,
            "truncated": True,
            "truncation_reasons": ["output_limit"],
        }
        text = json.dumps(minimal, ensure_ascii=True, separators=(",", ":"))
        truncated = True
    return text, truncated


def _report_suffix(
    members_reported: int,
    truncation_reasons: list[str],
    truncated: bool,
    uncompressed_bytes_read: int,
) -> str:
    return (
        f'],"members_reported":{members_reported}'
        f',"uncompressed_bytes_read":{uncompressed_bytes_read}'
        f',"truncated":{str(truncated).lower()}'
        f',"truncation_reasons":{json.dumps(truncation_reasons, separators=(",", ":"))}}}'
    )


def _worker_result(text: str, truncated: bool, command: str, version: str | None) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "version": version,
        "return_code": 0,
        "stdout": text,
        "stderr": "",
        "normalized_command": [command, "<sample>"],
        "truncated": truncated,
    }


def _looks_like_zip(payload: bytes) -> bool:
    return payload.find(b"PK\x03\x04", 0, 4096) >= 0 or payload.startswith(
        (b"PK\x05\x06", b"PK\x07\x08")
    )
