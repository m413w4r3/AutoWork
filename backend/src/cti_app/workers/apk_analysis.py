"""Bounded static APK inspection through the pinned, headless jadx CLI."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from cti_app.application.apk_analysis import (
    APK_MAX_ARCHIVE_ENTRIES,
    APK_MAX_ARCHIVE_UNPACKED_BYTES,
    APK_MAX_OUTPUT_FILES,
    APK_MAX_OUTPUT_TREE_BYTES,
    APK_MAX_SAMPLE_BYTES,
    APK_MAX_SEARCH_MATCHES,
    APK_MAX_SOURCE_BYTES,
    APK_MAX_STRING_CANDIDATES,
    JADX_VERSION,
    JADX_ZIP_SHA256,
    ApkAnalysisErrorData,
    ApkAnalysisOperation,
    ApkAnalysisParameters,
    ApkAnalysisResult,
    ApkAnalysisStatus,
    ApkClassItem,
    ApkComponent,
    ApkIntentFilter,
    ApkManifestData,
    ApkPage,
    ApkSearchMatch,
    ApkSourceReadData,
    ApkStringItem,
)
from cti_app.application.confined_analysis import ConfinedAnalysisJobParameters
from cti_app.config import Settings
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)
from cti_app.infrastructure.jadx import (
    host_test_jre_compatible,
    jadx_provenance,
    jadx_version,
    java_runtime_version,
    resolve_jadx_binary,
    supported_jre_version,
)

_ANDROID = "{http://schemas.android.com/apk/res/android}"
_PACKAGE = re.compile(rb"(?m)^\s*package\s+([A-Za-z0-9_$.]+)\s*;")
_TYPE = re.compile(rb"\b(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)")
_JAVA_STRING = re.compile(rb'"(?:\\.|[^"\\])*"')
_URL = re.compile(r"(?i)https?://[^\s\"'<>]+")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]{1,190}\.[A-Z]{2,63}(?![\w.-])")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{0,4}(?![0-9a-f:])")
_DOMAIN = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|invalid)(?![\w.-])"
)
_KEY_CANDIDATE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|token|secret|password|auth)[\s\"'=:#]+"
    r"([A-Za-z0-9._~+/=-]{8,256})"
)
_SAFE_ARCHIVE_NAME = re.compile(r"^[^\x00-\x1f\x7f]+$")
_MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_RESULT_TEXT_CHARS = 16 * 1024
_JAVA_OPTS = (
    "-Xms128m -Xmx768m -XX:+UseSerialGC -XX:ActiveProcessorCount=1 "
    "-XX:ReservedCodeCacheSize=128m -XX:CompressedClassSpaceSize=256m "
    "-Djava.awt.headless=true -Djava.net.useSystemProxies=false"
)
_SUBPROCESS_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "JAVA_OPTS": _JAVA_OPTS,
}


async def run_apk_analysis(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    settings: Settings,
) -> dict[str, Any]:
    """Run one typed jadx operation without executing the sample.

    There is no persistent decompilation cache: each call uses a bounded temporary
    tree, avoiding unbounded sample-derived output and cross-request reuse state.
    """
    try:
        request = ApkAnalysisParameters.from_worker_parameters(
            dict(parameters.analysis_parameters)
        )
    except (TypeError, ValueError):
        return _failure(
            ApkAnalysisOperation.MANIFEST,
            ApkAnalysisStatus.POLICY_DENIED,
            hashlib.sha256(payload).hexdigest(),
            None,
            "apk_operation_parameters_invalid",
        )

    sample_sha256 = hashlib.sha256(payload).hexdigest()
    if parameters.max_output_bytes < 4096:
        return _failure(
            request.operation,
            ApkAnalysisStatus.POLICY_DENIED,
            sample_sha256,
            None,
            "apk_output_budget_too_small",
        )
    if sample_sha256 != parameters.artifact_sha256:
        return _failure(
            request.operation,
            ApkAnalysisStatus.POLICY_DENIED,
            sample_sha256,
            None,
            "apk_sample_hash_mismatch",
        )
    if len(payload) > min(APK_MAX_SAMPLE_BYTES, settings.analysis_max_sample_bytes):
        return _failure(
            request.operation,
            ApkAnalysisStatus.POLICY_DENIED,
            sample_sha256,
            None,
            "apk_sample_too_large",
        )
    archive_error = _validate_apk_archive(payload)
    if archive_error is not None:
        return _failure(
            request.operation,
            ApkAnalysisStatus.FAILED,
            sample_sha256,
            None,
            archive_error,
        )

    binary = resolve_jadx_binary()
    if binary is None:
        return _failure(
            request.operation,
            ApkAnalysisStatus.UNAVAILABLE,
            sample_sha256,
            None,
            "jadx_binary_unavailable",
        )
    if os.environ.get("AUTOWORK_JADX_BINARY"):
        actual_jadx_version = jadx_version(binary)
        runtime_version = java_runtime_version()
    else:
        provenance = jadx_provenance(
            address_space_limit_bytes=settings.apk_analysis_address_space_limit_bytes
        )
        if provenance is None:
            return _failure(
                request.operation,
                ApkAnalysisStatus.UNAVAILABLE,
                sample_sha256,
                None,
                "jadx_provenance_unavailable",
            )
        actual_jadx_version = provenance["jadx"]
        runtime_version = provenance["jadx_jre_version"]
    if actual_jadx_version != JADX_VERSION:
        return _failure(
            request.operation,
            ApkAnalysisStatus.UNAVAILABLE,
            sample_sha256,
            runtime_version,
            "jadx_version_unattested",
        )
    jre_supported = (
        host_test_jre_compatible(runtime_version)
        if os.environ.get("AUTOWORK_JADX_BINARY")
        else supported_jre_version(runtime_version)
    )
    if not jre_supported:
        return _failure(
            request.operation,
            ApkAnalysisStatus.UNAVAILABLE,
            sample_sha256,
            runtime_version,
            "jadx_jre_unavailable",
        )

    try:
        with tempfile.TemporaryDirectory(prefix="autowork-jadx-", dir="/tmp") as temporary:
            root = Path(temporary)
            sample_path = root / "sample.apk"
            sample_path.write_bytes(payload)
            sample_path.chmod(0o400)
            output_dir = root / "output"
            output_dir.mkdir(mode=0o700)
            execution = await run_analysis_subprocess(
                [binary, "--threads-count", "1", "--output-dir", str(output_dir), str(sample_path)],
                timeout_seconds=min(
                    parameters.timeout_seconds,
                    settings.analysis_apk_timeout_seconds,
                ),
                output_limit=256 * 1024,
                environment=_SUBPROCESS_ENV,
                memory_limit_bytes=settings.apk_analysis_address_space_limit_bytes,
                deny_network=True,
                directory_limit_path=output_dir,
                directory_max_bytes=APK_MAX_OUTPUT_TREE_BYTES,
                directory_max_files=APK_MAX_OUTPUT_FILES,
            )
            if execution.status is not AnalysisSubprocessStatus.SUCCEEDED:
                status, reason = _subprocess_error(execution.status)
                return _failure(
                    request.operation,
                    status,
                    sample_sha256,
                    runtime_version,
                    reason,
                )
            if _tree_exceeds_limits(output_dir):
                return _failure(
                    request.operation,
                    ApkAnalysisStatus.PARTIAL,
                    sample_sha256,
                    runtime_version,
                    "apk_output_tree_limit",
                )
            data = _run_operation(request, output_dir)
            result_status = (
                ApkAnalysisStatus.PARTIAL
                if getattr(data, "truncated", False)
                else ApkAnalysisStatus.SUCCEEDED
            )
            result = ApkAnalysisResult(
                request.operation,
                result_status,
                sample_sha256,
                actual_jadx_version,
                JADX_ZIP_SHA256,
                runtime_version,
                data,
            )
            return _fit_result_budget(result, parameters.max_output_bytes - 2048)
    except (OSError, ValueError, ElementTree.ParseError, zipfile.BadZipFile):
        return _failure(
            request.operation,
            ApkAnalysisStatus.FAILED,
            sample_sha256,
            runtime_version,
            "apk_analysis_output_invalid",
        )
    except Exception:
        return _failure(
            request.operation,
            ApkAnalysisStatus.FAILED,
            sample_sha256,
            runtime_version,
            "apk_analysis_internal_error",
        )


def _failure(
    operation: ApkAnalysisOperation,
    status: ApkAnalysisStatus,
    sample_sha256: str,
    jre_version: str | None,
    reason: str,
    detail_code: str | None = None,
) -> dict[str, Any]:
    return ApkAnalysisResult(
        operation=operation,
        status=status,
        sample_sha256=sample_sha256,
        jadx_version=JADX_VERSION,
        jadx_zip_sha256=JADX_ZIP_SHA256,
        jre_version=jre_version,
        data=ApkAnalysisErrorData(reason, detail_code),
    ).as_json()


def _fit_result_budget(result: ApkAnalysisResult, maximum_bytes: int) -> dict[str, Any]:
    """Shrink paged/text/manifest data before the generic worker envelope is archived."""
    payload = result.as_json()
    if _canonical_size(payload) <= maximum_bytes:
        return payload
    data = result.data
    if isinstance(data, ApkPage):
        while data.items:
            keep = len(data.items) // 2
            data = replace(data, items=data.items[:keep], has_more=True, truncated=True)
            result = replace(result, status=ApkAnalysisStatus.PARTIAL, data=data)
            payload = result.as_json()
            if _canonical_size(payload) <= maximum_bytes:
                return payload
    elif isinstance(data, ApkSourceReadData):
        content = data.content
        while content:
            content = content[: len(content) // 2]
            data = replace(
                data,
                line_count=len(content.splitlines()),
                content=content,
                has_more=True,
                truncated=True,
            )
            result = replace(result, status=ApkAnalysisStatus.PARTIAL, data=data)
            payload = result.as_json()
            if _canonical_size(payload) <= maximum_bytes:
                return payload
    elif isinstance(data, ApkManifestData):
        components = data.components
        permissions = data.permissions
        while components:
            components = components[: len(components) // 2]
            data = replace(data, components=components, truncated=True)
            result = replace(result, status=ApkAnalysisStatus.PARTIAL, data=data)
            payload = result.as_json()
            if _canonical_size(payload) <= maximum_bytes:
                return payload
        while permissions:
            permissions = permissions[: len(permissions) // 2]
            data = replace(data, permissions=permissions, truncated=True)
            result = replace(result, status=ApkAnalysisStatus.PARTIAL, data=data)
            payload = result.as_json()
            if _canonical_size(payload) <= maximum_bytes:
                return payload
    return _failure(
        result.operation,
        ApkAnalysisStatus.PARTIAL,
        result.sample_sha256,
        result.jre_version,
        "apk_result_output_limit",
    )


def _canonical_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _subprocess_error(
    status: AnalysisSubprocessStatus,
) -> tuple[ApkAnalysisStatus, str]:
    return {
        AnalysisSubprocessStatus.TIMED_OUT: (
            ApkAnalysisStatus.PARTIAL,
            "apk_decompilation_timeout",
        ),
        AnalysisSubprocessStatus.OUTPUT_LIMIT: (
            ApkAnalysisStatus.PARTIAL,
            "apk_process_output_limit",
        ),
        AnalysisSubprocessStatus.DIRECTORY_LIMIT: (
            ApkAnalysisStatus.PARTIAL,
            "apk_output_tree_limit",
        ),
        AnalysisSubprocessStatus.FAILED_TO_START: (
            ApkAnalysisStatus.UNAVAILABLE,
            "jadx_process_unavailable",
        ),
        AnalysisSubprocessStatus.NON_ZERO_EXIT: (
            ApkAnalysisStatus.FAILED,
            "apk_decompilation_failed",
        ),
        AnalysisSubprocessStatus.SUCCEEDED: (ApkAnalysisStatus.SUCCEEDED, ""),
    }[status]


def _validate_apk_archive(payload: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if not members or len(members) > APK_MAX_ARCHIVE_ENTRIES:
                return "apk_archive_entry_limit"
            total_unpacked = 0
            has_manifest = False
            has_dex = False
            for item in members:
                name = item.filename
                raw_name = name[:-1] if item.is_dir() and name.endswith("/") else name
                path = PurePosixPath(raw_name)
                mode_type = stat.S_IFMT(item.external_attr >> 16)
                if (
                    not name
                    or "\\" in name
                    or ":" in name
                    or raw_name.startswith("/")
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in raw_name.split("/"))
                    or _SAFE_ARCHIVE_NAME.fullmatch(name) is None
                    or mode_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    return "apk_archive_path_invalid"
                if item.is_dir():
                    continue
                total_unpacked += item.file_size
                if total_unpacked > APK_MAX_ARCHIVE_UNPACKED_BYTES:
                    return "apk_archive_unpacked_size_limit"
                if item.file_size and (
                    not item.compress_size
                    or item.file_size > max(item.compress_size, 1) * 1000
                ):
                    return "apk_archive_compression_ratio_limit"
                if raw_name == "AndroidManifest.xml":
                    has_manifest = True
                if path.name == "classes.dex" or re.fullmatch(r"classes[0-9]+\.dex", path.name):
                    has_dex = True
            if not has_manifest or not has_dex:
                return "apk_manifest_or_dex_missing"
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return "apk_invalid_or_corrupt_archive"
    return None


def _run_operation(request: ApkAnalysisParameters, output_dir: Path) -> Any:
    if request.operation is ApkAnalysisOperation.MANIFEST:
        return _read_manifest(output_dir)
    if request.operation is ApkAnalysisOperation.CLASSES_LIST:
        classes, truncated = _list_classes(output_dir)
        prefix = request.prefix
        if prefix:
            classes = [item for item in classes if item.package_name.startswith(prefix)]
        return _page(classes, request.offset, request.limit, truncated)
    if request.operation is ApkAnalysisOperation.STRINGS_EXTRACT:
        strings, truncated = _extract_strings(output_dir)
        return _page(strings, request.offset, request.limit, truncated)
    if request.operation is ApkAnalysisOperation.SOURCE_READ:
        return _read_source(output_dir, request)
    if request.operation is ApkAnalysisOperation.SOURCE_SEARCH:
        matches, truncated = _search_sources(output_dir, request)
        return _page(matches, request.offset, request.limit, truncated)
    raise ValueError("APK operation is not implemented")


def _read_manifest(output_dir: Path) -> ApkManifestData:
    candidates = sorted(
        (path for path in output_dir.rglob("AndroidManifest.xml") if path.is_file()),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )
    manifest_path = next(
        (path for path in candidates if path.relative_to(output_dir).parts[0] == "resources"),
        candidates[0] if candidates else None,
    )
    if manifest_path is None or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("APK manifest output is unavailable")
    root = ElementTree.fromstring(manifest_path.read_bytes())
    package_name = _bounded_text(root.attrib.get("package"), 256)
    version_code = _bounded_text(root.attrib.get(_ANDROID + "versionCode"), 128)
    version_name = _bounded_text(root.attrib.get(_ANDROID + "versionName"), 256)
    uses_sdk = root.find("uses-sdk")
    sdk = uses_sdk.attrib if uses_sdk is not None else {}
    all_permissions = sorted(
        {
            value
            for node in root
            if node.tag in {"uses-permission", "uses-permission-sdk-23", "uses-permission-sdk-m"}
            if (value := _bounded_text(node.attrib.get(_ANDROID + "name"), 256))
        }
    )
    application = root.find("application")
    components: list[ApkComponent] = []
    if application is not None:
        for component_type in ("activity", "activity-alias", "service", "receiver", "provider"):
            for node in application.findall(component_type):
                raw_name = _bounded_text(node.attrib.get(_ANDROID + "name"), 256)
                if not raw_name:
                    continue
                component_name = _normalize_component_name(package_name, raw_name)
                raw_exported = node.attrib.get(_ANDROID + "exported")
                exported = (
                    True if raw_exported == "true" else False if raw_exported == "false" else None
                )
                filters: list[ApkIntentFilter] = []
                for intent in node.findall("intent-filter"):
                    actions = tuple(
                        sorted(
                            value
                            for child in intent.findall("action")
                            if (value := _bounded_text(child.attrib.get(_ANDROID + "name"), 256))
                        )
                    )
                    categories = tuple(
                        sorted(
                            value
                            for child in intent.findall("category")
                            if (value := _bounded_text(child.attrib.get(_ANDROID + "name"), 256))
                        )
                    )
                    data_rows = []
                    for child in intent.findall("data"):
                        row = {
                            key: value
                            for key in (
                                "scheme",
                                "host",
                                "port",
                                "path",
                                "pathPrefix",
                                "pathPattern",
                                "mimeType",
                            )
                            if (value := _bounded_text(child.attrib.get(_ANDROID + key), 256))
                        }
                        if row:
                            data_rows.append(row)
                    filters.append(ApkIntentFilter(actions, categories, tuple(data_rows)))
                components.append(
                    ApkComponent(component_type, component_name, exported, tuple(filters))
                )
    components.sort(key=lambda item: (item.component_type, item.name))
    truncated = len(all_permissions) > 2000 or len(components) > 2000
    return ApkManifestData(
        package_name=package_name,
        version_code=version_code,
        version_name=version_name,
        min_sdk=_bounded_text(sdk.get(_ANDROID + "minSdkVersion"), 128),
        target_sdk=_bounded_text(sdk.get(_ANDROID + "targetSdkVersion"), 128),
        max_sdk=_bounded_text(sdk.get(_ANDROID + "maxSdkVersion"), 128),
        permissions=tuple(all_permissions[:2000]),
        components=tuple(components[:2000]),
        debuggable=_optional_bool(application.attrib.get(_ANDROID + "debuggable"))
        if application is not None
        else None,
        allow_backup=_optional_bool(application.attrib.get(_ANDROID + "allowBackup"))
        if application is not None
        else None,
        uses_cleartext_traffic=_optional_bool(
            application.attrib.get(_ANDROID + "usesCleartextTraffic")
        )
        if application is not None
        else None,
        truncated=truncated,
    )


def _list_classes(output_dir: Path) -> tuple[list[ApkClassItem], bool]:
    items: list[ApkClassItem] = []
    truncated = False
    for source_path in _source_files(output_dir):
        content = _read_source_file(source_path)
        if content is None:
            truncated = True
            continue
        package = _package_name(content)
        for type_name in _type_names(content):
            items.append(
                ApkClassItem(package, type_name, _relative_source_path(output_dir, source_path))
            )
            if len(items) >= APK_MAX_SEARCH_MATCHES:
                truncated = True
                break
        if len(items) >= APK_MAX_SEARCH_MATCHES:
            break
    items.sort(key=lambda item: (item.package_name, item.class_name, item.source_path))
    return items, truncated


def _extract_strings(output_dir: Path) -> tuple[list[ApkStringItem], bool]:
    items: list[ApkStringItem] = []
    truncated = False
    for source_path in _source_files(output_dir):
        content = _read_source_bytes(source_path)
        if content is None:
            truncated = True
            continue
        package = _package_name(content)
        class_name = source_path.stem
        fqcn = f"{package}.{class_name}" if package else class_name
        relative = _relative_source_path(output_dir, source_path)
        for match in _JAVA_STRING.finditer(content):
            full_value = _decode_java_string(match.group(0))
            if not full_value:
                continue
            value = full_value[:512]
            items.append(
                ApkStringItem(
                    value=value,
                    kind=_string_kind(value),
                    class_name=fqcn,
                    source_path=relative,
                    offset=match.start(),
                    truncated=len(full_value) > len(value),
                )
            )
            if len(items) >= APK_MAX_STRING_CANDIDATES:
                truncated = True
                break
        if len(items) >= APK_MAX_STRING_CANDIDATES:
            break
    items.sort(key=lambda item: (item.source_path, item.offset, item.value))
    return items, truncated


def _read_source(output_dir: Path, request: ApkAnalysisParameters) -> ApkSourceReadData:
    selected_path: Path | None = None
    class_name = request.class_name or ""
    if request.source_path is not None:
        selected_path = _source_path_from_relative(output_dir, request.source_path)
    elif request.class_name is not None:
        classes, _ = _list_classes(output_dir)
        matched = next(
            (
                item
                for item in classes
                if item.package_name + "." + item.class_name == request.class_name
                or item.class_name == request.class_name
            ),
            None,
        )
        if matched is not None:
            class_name = request.class_name
            selected_path = _source_path_from_relative(output_dir, matched.source_path)
    if selected_path is None or not selected_path.is_file() or selected_path.is_symlink():
        raise ValueError("APK source selector did not resolve")
    raw = _read_source_bytes(selected_path)
    if raw is None:
        raise ValueError("APK source file exceeds its read limit")
    text = raw.decode("utf-8", errors="replace")
    if not class_name:
        package = _package_name(raw)
        class_name = f"{package}.{selected_path.stem}" if package else selected_path.stem
    lines = text.splitlines()
    start_index = request.start_line - 1
    selected = lines[start_index : start_index + request.line_count]
    content_lines: list[str] = []
    used_bytes = 0
    line_truncated = False
    for line in selected:
        bounded_line = line[:_MAX_RESULT_TEXT_CHARS]
        line_truncated = line_truncated or len(line) > len(bounded_line)
        line_bytes = len(bounded_line.encode("utf-8", errors="replace"))
        separator = 1 if content_lines else 0
        if used_bytes + line_bytes + separator > APK_MAX_SOURCE_BYTES:
            break
        content_lines.append(bounded_line)
        used_bytes += line_bytes + separator
    return ApkSourceReadData(
        class_name=class_name,
        source_path=_relative_source_path(output_dir, selected_path),
        start_line=request.start_line,
        line_count=len(content_lines),
        total_lines=len(lines),
        content="\n".join(content_lines),
        has_more=start_index + len(content_lines) < len(lines),
        truncated=line_truncated or len(content_lines) < len(selected),
    )


def _search_sources(
    output_dir: Path,
    request: ApkAnalysisParameters,
) -> tuple[list[ApkSearchMatch], bool]:
    assert request.pattern is not None
    pattern = re.compile(request.pattern if request.regex else re.escape(request.pattern))
    matches: list[ApkSearchMatch] = []
    truncated = False
    for source_path in _source_files(output_dir):
        raw = _read_source_bytes(source_path)
        if raw is None:
            truncated = True
            continue
        package = _package_name(raw)
        type_name = source_path.stem
        class_name = f"{package}.{type_name}" if package else type_name
        lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
        byte_offset = 0
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip("\r\n")
            for match in pattern.finditer(line):
                context_start = max(0, line_number - 1 - request.context_lines)
                context_end = min(len(lines), line_number + request.context_lines)
                context = tuple(
                    _truncate_line(lines[index].rstrip("\r\n"), 256)
                    for index in range(context_start, context_end)
                )
                line_byte_offset = len(line[: match.start()].encode("utf-8", errors="replace"))
                raw_match = match.group(0)
                matches.append(
                    ApkSearchMatch(
                        class_name=class_name,
                        source_path=_relative_source_path(output_dir, source_path),
                        line=line_number,
                        offset=byte_offset + line_byte_offset,
                        match=raw_match[:512],
                        context=context,
                        text_truncated=(
                            len(raw_match) > 512
                            or any(
                                len(lines[index].rstrip("\r\n")) > 256
                                for index in range(context_start, context_end)
                            )
                        ),
                    )
                )
                if len(matches) >= APK_MAX_SEARCH_MATCHES:
                    truncated = True
                    break
            if len(matches) >= APK_MAX_SEARCH_MATCHES:
                break
            byte_offset += len(raw_line.encode("utf-8", errors="replace"))
        if len(matches) >= APK_MAX_SEARCH_MATCHES:
            break
    matches.sort(key=lambda item: (item.source_path, item.offset, item.line))
    return matches, truncated


def _page(items: list[Any], offset: int, limit: int, truncated: bool) -> ApkPage[Any]:
    return ApkPage(
        offset=offset,
        limit=limit,
        total=len(items),
        items=tuple(items[offset : offset + limit]),
        has_more=offset + limit < len(items),
        truncated=truncated,
    )


def _source_files(output_dir: Path) -> list[Path]:
    sources = output_dir / "sources"
    if not sources.is_dir() or sources.is_symlink():
        return []
    return sorted(
        (path for path in sources.rglob("*.java") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )


def _source_path_from_relative(output_dir: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != "sources"
        or any(part in {"", ".", ".."} for part in path.parts)
        or not relative.endswith(".java")
    ):
        raise ValueError("APK source path is invalid")
    candidate = output_dir.joinpath(*path.parts)
    if not candidate.resolve(strict=False).is_relative_to(output_dir.resolve()):
        raise ValueError("APK source path escapes its output root")
    return candidate


def _read_source_file(path: Path) -> bytes | None:
    data = _read_source_bytes(path)
    return data


def _read_source_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_SOURCE_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _relative_source_path(output_dir: Path, source_path: Path) -> str:
    relative = source_path.relative_to(output_dir).as_posix()
    if not relative.startswith("sources/"):
        raise ValueError("APK source path is outside the source tree")
    return relative


def _package_name(content: bytes) -> str:
    match = _PACKAGE.search(content)
    return match.group(1).decode("ascii", errors="replace") if match else ""


def _type_names(content: bytes) -> list[str]:
    return sorted(
        {match.group(1).decode("ascii", errors="replace") for match in _TYPE.finditer(content)}
    )


def _decode_java_string(literal: bytes) -> str:
    value = literal[1:-1]
    replacements = {
        b"\\n": b"\n",
        b"\\r": b"\r",
        b"\\t": b"\t",
        b"\\b": b"\b",
        b"\\f": b"\f",
        b'\\"': b'"',
        b"\\'": b"'",
        b"\\\\": b"\\",
    }
    for source, replacement in replacements.items():
        value = value.replace(source, replacement)
    return value.decode("utf-8", errors="replace")


def _string_kind(value: str) -> str:
    if _URL.search(value):
        return "url"
    if _EMAIL.search(value):
        return "email"
    for match in _IPV4.finditer(value):
        try:
            ipaddress.IPv4Address(match.group(0))
            return "ip"
        except ipaddress.AddressValueError:
            continue
    for match in _IPV6.finditer(value):
        try:
            ipaddress.IPv6Address(match.group(0))
            return "ip"
        except ipaddress.AddressValueError:
            continue
    if _DOMAIN.search(value):
        return "domain"
    if _KEY_CANDIDATE.search(value):
        return "key_candidate"
    return "string"


def _normalize_component_name(package: str | None, name: str) -> str:
    if name.startswith("."):
        return f"{package or ''}{name}"[:512]
    if "." not in name and package:
        return f"{package}.{name}"[:512]
    return name[:512]


def _optional_bool(value: str | None) -> bool | None:
    return True if value == "true" else False if value == "false" else None


def _bounded_text(value: str | None, maximum: int) -> str | None:
    return value[:maximum] if value is not None else None


def _truncate_line(value: str, maximum: int) -> str:
    return value[:maximum]


def _tree_exceeds_limits(root: Path) -> bool:
    total = 0
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            for path in directory.iterdir():
                count += 1
                if count > APK_MAX_OUTPUT_FILES or path.is_symlink():
                    return True
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    total += path.stat().st_size
                    if total > APK_MAX_OUTPUT_TREE_BYTES:
                        return True
                else:
                    return True
        except OSError:
            return True
    return False
