"""Versioned worker contract for bounded, offline jadx analysis of APK files."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

JADX_VERSION = "1.5.6"
JADX_ZIP_SHA256 = "545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974"
JADX_URL = "https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip"
JADX_PROJECT_URL = "https://github.com/skylot/jadx/"
JADX_LICENSE = "Apache-2.0"
JADX_JRE_VERSION = "17"
APK_ANALYSIS_RESULT_SCHEMA = "apk-analysis-result.v1"

APK_MAX_SAMPLE_BYTES = 128 * 1024 * 1024
APK_MAX_OUTPUT_TREE_BYTES = 128 * 1024 * 1024
APK_MAX_OUTPUT_FILES = 10_000
APK_MAX_ARCHIVE_ENTRIES = 20_000
APK_MAX_ARCHIVE_UNPACKED_BYTES = 512 * 1024 * 1024
APK_MAX_PAGE_SIZE = 200
APK_MAX_PAGE_OFFSET = 10_000_000
APK_MAX_SOURCE_LINES = 200
APK_MAX_SOURCE_BYTES = 64 * 1024
APK_MAX_PATTERN_CHARS = 256
APK_MAX_STRING_CANDIDATES = 50_000
APK_MAX_SEARCH_MATCHES = 50_000
APK_MAX_CONTEXT_LINES = 5

_SAFE_CLASS_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*){0,63}$")
_SAFE_PACKAGE_PREFIX = re.compile(r"^[A-Za-z0-9_$.]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ApkAnalysisOperation(StrEnum):
    MANIFEST = "apk.manifest"
    CLASSES_LIST = "apk.classes.list"
    STRINGS_EXTRACT = "apk.strings.extract"
    SOURCE_READ = "apk.source.read"
    SOURCE_SEARCH = "apk.source.search"


class ApkAnalysisStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    POLICY_DENIED = "POLICY_DENIED"


@dataclass(frozen=True, slots=True)
class ApkAnalysisParameters:
    """Validated operation parameters shared by the API layer and worker."""

    operation: ApkAnalysisOperation
    offset: int = 0
    limit: int = APK_MAX_PAGE_SIZE
    prefix: str | None = None
    class_name: str | None = None
    source_path: str | None = None
    start_line: int = 1
    line_count: int = APK_MAX_SOURCE_LINES
    pattern: str | None = None
    regex: bool = False
    context_lines: int = 2

    def __post_init__(self) -> None:
        if self.offset < 0 or self.offset > APK_MAX_PAGE_OFFSET:
            raise ValueError("APK page offset is invalid")
        if not 1 <= self.limit <= APK_MAX_PAGE_SIZE:
            raise ValueError("APK page size is invalid")
        if self.operation is ApkAnalysisOperation.CLASSES_LIST:
            if self.prefix is not None and _SAFE_PACKAGE_PREFIX.fullmatch(self.prefix) is None:
                raise ValueError("APK package prefix is invalid")
        elif self.prefix is not None:
            raise ValueError("APK package prefix requires apk.classes.list")

        if self.operation is ApkAnalysisOperation.SOURCE_READ:
            if (self.class_name is None) == (self.source_path is None):
                raise ValueError("APK source selector must be exactly one class or source path")
            if self.class_name is not None and _SAFE_CLASS_NAME.fullmatch(self.class_name) is None:
                raise ValueError("APK class name is invalid")
            if self.source_path is not None:
                _validate_source_path(self.source_path)
            if not 1 <= self.start_line <= 1_000_000:
                raise ValueError("APK source line is invalid")
            if not 1 <= self.line_count <= APK_MAX_SOURCE_LINES:
                raise ValueError("APK source line count is invalid")
        elif (
            any(value is not None for value in (self.class_name, self.source_path))
            or self.start_line != 1
            or self.line_count != APK_MAX_SOURCE_LINES
        ):
            raise ValueError("APK source range requires apk.source.read")

        if self.operation is ApkAnalysisOperation.SOURCE_SEARCH:
            if not isinstance(self.pattern, str) or not (
                1 <= len(self.pattern) <= APK_MAX_PATTERN_CHARS
            ):
                raise ValueError("APK search pattern is invalid")
            if "\x00" in self.pattern:
                raise ValueError("APK search pattern is invalid")
            if not 0 <= self.context_lines <= APK_MAX_CONTEXT_LINES:
                raise ValueError("APK search context is invalid")
            if self.regex:
                _validate_safe_regex(self.pattern)
        elif self.pattern is not None or self.regex or self.context_lines != 2:
            raise ValueError("APK search options require apk.source.search")

        if self.operation not in {
            ApkAnalysisOperation.CLASSES_LIST,
            ApkAnalysisOperation.STRINGS_EXTRACT,
            ApkAnalysisOperation.SOURCE_SEARCH,
        } and (self.offset != 0 or self.limit != APK_MAX_PAGE_SIZE):
            raise ValueError("APK pagination requires a paged operation")

    def to_worker_parameters(self) -> dict[str, str]:
        values: dict[str, str] = {"operation": self.operation.value}
        if self.operation in {
            ApkAnalysisOperation.CLASSES_LIST,
            ApkAnalysisOperation.STRINGS_EXTRACT,
            ApkAnalysisOperation.SOURCE_SEARCH,
        }:
            values.update(offset=str(self.offset), limit=str(self.limit))
        if self.prefix is not None:
            values["prefix"] = self.prefix
        if self.class_name is not None:
            values["class_name"] = self.class_name
        if self.source_path is not None:
            values["source_path"] = self.source_path
        if self.operation is ApkAnalysisOperation.SOURCE_READ:
            values.update(start_line=str(self.start_line), line_count=str(self.line_count))
        if self.pattern is not None:
            values.update(
                pattern=self.pattern,
                regex="true" if self.regex else "false",
                context_lines=str(self.context_lines),
            )
        return values

    @classmethod
    def from_worker_parameters(cls, values: dict[str, str]) -> ApkAnalysisParameters:
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
        ):
            raise ValueError("APK analysis parameters must be string fields")
        try:
            operation = ApkAnalysisOperation(values.get("operation", ""))
        except ValueError as exc:
            raise ValueError("APK operation is invalid") from exc
        allowed = {
            ApkAnalysisOperation.MANIFEST: {"operation"},
            ApkAnalysisOperation.CLASSES_LIST: {"operation", "offset", "limit", "prefix"},
            ApkAnalysisOperation.STRINGS_EXTRACT: {"operation", "offset", "limit"},
            ApkAnalysisOperation.SOURCE_READ: {
                "operation",
                "class_name",
                "source_path",
                "start_line",
                "line_count",
            },
            ApkAnalysisOperation.SOURCE_SEARCH: {
                "operation",
                "offset",
                "limit",
                "pattern",
                "regex",
                "context_lines",
            },
        }[operation]
        if set(values) - allowed:
            raise ValueError("APK analysis parameters are not supported for this operation")

        def decimal(key: str, default: int) -> int:
            raw = values.get(key)
            if raw is None:
                return default
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", raw):
                raise ValueError(f"APK parameter {key} is invalid")
            return int(raw)

        regex_value = values.get("regex", "false")
        if regex_value not in {"true", "false"}:
            raise ValueError("APK regex flag is invalid")
        return cls(
            operation=operation,
            offset=decimal("offset", 0),
            limit=decimal("limit", APK_MAX_PAGE_SIZE),
            prefix=values.get("prefix"),
            class_name=values.get("class_name"),
            source_path=values.get("source_path"),
            start_line=decimal("start_line", 1),
            line_count=decimal("line_count", APK_MAX_SOURCE_LINES),
            pattern=values.get("pattern"),
            regex=regex_value == "true",
            context_lines=decimal("context_lines", 2),
        )


@dataclass(frozen=True, slots=True)
class ApkPage[T]:
    offset: int
    limit: int
    total: int
    items: tuple[T, ...]
    has_more: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ApkIntentFilter:
    actions: tuple[str, ...]
    categories: tuple[str, ...]
    data: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ApkComponent:
    component_type: str
    name: str
    exported: bool | None
    intent_filters: tuple[ApkIntentFilter, ...] = ()


@dataclass(frozen=True, slots=True)
class ApkManifestData:
    package_name: str | None
    version_code: str | None
    version_name: str | None
    min_sdk: str | None
    target_sdk: str | None
    max_sdk: str | None
    permissions: tuple[str, ...]
    components: tuple[ApkComponent, ...]
    debuggable: bool | None
    allow_backup: bool | None
    uses_cleartext_traffic: bool | None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ApkClassItem:
    package_name: str
    class_name: str
    source_path: str


@dataclass(frozen=True, slots=True)
class ApkStringItem:
    value: str
    kind: str
    class_name: str
    source_path: str
    offset: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ApkSourceReadData:
    class_name: str
    source_path: str
    start_line: int
    line_count: int
    total_lines: int
    content: str
    has_more: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class ApkSearchMatch:
    class_name: str
    source_path: str
    line: int
    offset: int
    match: str
    context: tuple[str, ...]
    text_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ApkAnalysisErrorData:
    available_reason: str
    detail_code: str | None = None


ApkAnalysisData = (
    ApkManifestData
    | ApkPage[ApkClassItem]
    | ApkPage[ApkStringItem]
    | ApkSourceReadData
    | ApkPage[ApkSearchMatch]
    | ApkAnalysisErrorData
)


@dataclass(frozen=True, slots=True)
class ApkAnalysisResult:
    operation: ApkAnalysisOperation
    status: ApkAnalysisStatus
    sample_sha256: str
    jadx_version: str
    jadx_zip_sha256: str
    jre_version: str | None
    data: ApkAnalysisData

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.sample_sha256) is None:
            raise ValueError("APK result sample SHA-256 is invalid")
        if self.jadx_zip_sha256 != JADX_ZIP_SHA256:
            raise ValueError("APK result jadx archive hash is invalid")

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": APK_ANALYSIS_RESULT_SCHEMA,
            "operation": self.operation.value,
            "status": self.status.value,
            "sample_sha256": self.sample_sha256,
            "engine": "jadx",
            "jadx_version": self.jadx_version,
            "jadx_zip_sha256": self.jadx_zip_sha256,
            "jre_version": self.jre_version,
            "data": _json_value(asdict(self.data)),
        }


def _validate_source_path(value: str) -> None:
    if (
        not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or value.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("APK source path is invalid")
    parts = value.split("/")
    if (
        len(parts) < 2
        or parts[0] != "sources"
        or any(part in {"", ".", ".."} for part in parts)
        or not value.endswith(".java")
    ):
        raise ValueError("APK source path is invalid")


def _validate_safe_regex(pattern: str) -> None:
    """Allow a bounded regex subset that cannot trigger catastrophic backtracking."""
    if re.search(r"\\(?:[1-9]|g<|k<)", pattern):
        raise ValueError("APK regex backreferences are unsupported")

    if "(?" in pattern:
        raise ValueError("APK regex lookarounds and extensions are unsupported")

    quantifier_count = 0
    branch_factor = 1
    group_quantifiers: list[int] = []
    closed_group_quantifiers: int | None = None
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            closed_group_quantifiers = None
            index += 2
            continue
        if character == "[":
            closed_group_quantifiers = None
            index += 1
            while index < len(pattern):
                if pattern[index] == "\\":
                    index += 2
                elif pattern[index] == "]":
                    index += 1
                    break
                else:
                    index += 1
            continue
        if character == "(":
            group_quantifiers.append(0)
            closed_group_quantifiers = None
            index += 1
            continue
        if character == ")":
            if not group_quantifiers:
                raise ValueError("APK regex grouping is invalid")
            closed_group_quantifiers = group_quantifiers.pop()
            if group_quantifiers and closed_group_quantifiers > 0:
                group_quantifiers[-1] += 1
            index += 1
            continue
        if character == "|":
            raise ValueError("APK regex alternation is unsupported")
        if character in "*+":
            raise ValueError("APK regex unbounded repetition is unsupported")

        span = 1
        if character == "?":
            lower, upper = 0, 1
        elif character == "{":
            bounded = re.match(r"\{([0-9]+)(?:,([0-9]*))?\}", pattern[index:])
            if bounded is None:
                index += 1
                continue
            lower = int(bounded.group(1))
            upper_text = bounded.group(2)
            if "," in bounded.group(0) and not upper_text:
                raise ValueError("APK regex unbounded repetition is unsupported")
            upper = int(upper_text) if upper_text is not None else lower
            if lower > upper or upper > 32:
                raise ValueError("APK regex repetition bound is invalid")
            span = len(bounded.group(0))
        else:
            closed_group_quantifiers = None
            index += 1
            continue

        if closed_group_quantifiers:
            raise ValueError("APK regex cannot repeat a group containing repetition")
        quantifier_count += 1
        branch_factor *= upper - lower + 1
        if group_quantifiers:
            group_quantifiers[-1] += 1
        closed_group_quantifiers = None
        index += span

    if group_quantifiers:
        raise ValueError("APK regex grouping is invalid")
    if quantifier_count > 3:
        raise ValueError("APK regex has too many repetition operators")
    if branch_factor > 256:
        raise ValueError("APK regex repetition combination is too large")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError("APK regex is invalid") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value
