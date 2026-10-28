from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class LocalCommandId(StrEnum):
    FILE = "file"
    STRINGS = "strings"
    XXD = "xxd"
    SHA256SUM = "sha256sum"
    READELF = "readelf"
    OBJDUMP = "objdump"
    MONODIS = "monodis"
    YARA = "yara"
    ZIP_MEMBERS = "zip_members"
    ARCHIVE_LIST = "archive_list"
    ARCHIVE_EXTRACT_HASH = "archive_extract_hash"
    # L4: optional extractors remain outside the generic local.run_command catalogue.
    FLOSS = "floss"
    MWCP = "mwcp"


class LocalCommandStatus(StrEnum):
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    FAILED = "failed"
    TIMEOUT = "timeout"
    OUTPUT_TRUNCATED = "output_truncated"


class LocalAnalysisFileType(StrEnum):
    ELF = "elf"
    PE = "pe"
    OTHER = "other"


class LocalCommandOutputKind(StrEnum):
    TEXT = "text"
    LINES = "lines"
    MATCHES = "matches"


class LocalOptionKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    ENUM = "enum"
    STRING_LIST = "string_list"
    STRING = "string"


LOCAL_COMMAND_MAX_RANGE_BYTES = 1 * 1024 * 1024
LOCAL_COMMAND_MAX_XXD_RANGE_BYTES = 4096
LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES = 8 * 1024
LOCAL_ARCHIVE_MAX_MEMBERS = 2_000
LOCAL_ARCHIVE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
LOCAL_ARCHIVE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO = 1_000


@dataclass(frozen=True, slots=True)
class LocalCommandOptionSpec:
    name: str
    kind: LocalOptionKind
    default: object
    allowed_values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    max_items: int | None = None
    max_item_length: int | None = None
    optional: bool = False


@dataclass(frozen=True, slots=True)
class LocalCommandSpec:
    id: LocalCommandId
    binary: str
    allowed_options: tuple[LocalCommandOptionSpec, ...]
    accepted_file_types: frozenset[LocalAnalysisFileType]
    timeout_seconds: float
    max_output_bytes: int
    cpu_limit_seconds: int
    memory_limit_bytes: int
    file_size_limit_bytes: int
    output_kind: LocalCommandOutputKind
    # L4 extractors drain stdout to EOF for a complete digest while retaining only a prefix.
    capture_full_output_hash: bool = False
    requires_filesystem_confinement: bool = False

    def __post_init__(self) -> None:
        if not self.binary or "/" in self.binary or "\\" in self.binary:
            raise ValueError("Local command binaries must be whitelisted executable names")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("Local command time and output limits must be positive")
        if min(self.cpu_limit_seconds, self.memory_limit_bytes, self.file_size_limit_bytes) <= 0:
            raise ValueError("Local command resource limits must be positive")
        option_names = [option.name for option in self.allowed_options]
        if len(option_names) != len(set(option_names)):
            raise ValueError("Local command option names must be unique")


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class LocalCommandResult:
    command_id: LocalCommandId | None
    status: LocalCommandStatus
    sample_sha256: str | None
    output: str = ""
    normalized_command: tuple[str, ...] = ()
    executable_version: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    started_at: datetime | None = None
    detail_code: str | None = None
    # Raw bytes are transient for vendor JSON parsing; archive_record excludes the prefix.
    raw_output_sha256: str | None = None
    raw_output_bytes: int = 0
    raw_output_complete: bool = False
    raw_output_prefix: bytes | None = None

    def __post_init__(self) -> None:
        if self.sample_sha256 is not None and not _SHA256.fullmatch(self.sample_sha256):
            raise ValueError("sample_sha256 must be a lowercase SHA-256 digest")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        if self.started_at is not None and self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        if self.raw_output_sha256 is not None and not _SHA256.fullmatch(self.raw_output_sha256):
            raise ValueError("raw_output_sha256 must be a lowercase SHA-256 digest")
        if self.raw_output_bytes < 0:
            raise ValueError("raw_output_bytes must be non-negative")
        if self.raw_output_complete and self.raw_output_sha256 is None:
            raise ValueError("Complete raw output requires a digest")
        if (
            self.raw_output_prefix is not None
            and len(self.raw_output_prefix) > self.raw_output_bytes
        ):
            raise ValueError("Raw output prefix cannot exceed the captured byte count")

    def archive_record(self) -> dict[str, object]:
        """Return the bounded, byte-free execution journal record."""
        return {
            "command_id": self.command_id.value if self.command_id is not None else None,
            "command": list(self.normalized_command),
            "sample_sha256": self.sample_sha256,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "executable_version": self.executable_version,
            "output": self.output,
            "detail_code": self.detail_code,
            "raw_output_sha256": self.raw_output_sha256,
            "raw_output_bytes": self.raw_output_bytes,
            "raw_output_complete": self.raw_output_complete,
        }


class GhidraOperation(StrEnum):
    FUNCTIONS = "functions"
    IMPORTS = "imports"
    XREF_TO = "xrefs_to"
    XREF_FROM = "xrefs_from"
    DECOMPILE_FUNCTION = "decompile_function"
    SEARCH = "search"


@dataclass(frozen=True, slots=True)
class GhidraObservation:
    operation: GhidraOperation
    sample_sha256: str
    ghidra_version: str
    plugin_version: str
    address: str | None
    observed_at: datetime
    data: Mapping[str, object]
    truncated: bool = False

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sample_sha256):
            raise ValueError("sample_sha256 must be a lowercase SHA-256 digest")
        if not self.ghidra_version.strip() or not self.plugin_version.strip():
            raise ValueError("Ghidra and plugin versions must be explicit")
        if self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


def now_utc() -> datetime:
    return datetime.now(UTC)
