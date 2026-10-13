from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from cti_app.domain.local_analysis import (
    GhidraObservation,
    GhidraOperation,
    LocalAnalysisFileType,
    LocalCommandId,
    LocalCommandOptionSpec,
    LocalCommandOutputKind,
    LocalCommandResult,
    LocalCommandSpec,
    LocalCommandStatus,
    LocalOptionKind,
)


class SampleBytesUnavailableError(RuntimeError):
    """The canonical sample bytes could not be resolved by SHA-256."""


class GhidraAnalysisError(RuntimeError):
    code = "ghidra_error"


class GhidraUnavailableError(GhidraAnalysisError):
    code = "ghidra_unavailable"


class GhidraPolicyDeniedError(GhidraAnalysisError):
    code = "ghidra_policy_denied"


class GhidraTransportError(GhidraAnalysisError):
    code = "ghidra_transport_error"


class GhidraResponseError(GhidraAnalysisError):
    code = "ghidra_response_invalid"


class GhidraResponseTooLargeError(GhidraAnalysisError):
    code = "ghidra_response_too_large"


class GhidraVersionMismatchError(GhidraAnalysisError):
    code = "ghidra_version_mismatch"


class SampleBytesForAnalysisPort(Protocol):
    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LocalCommandCapability:
    spec: LocalCommandSpec
    executable_path: str | None
    version: str | None

    @property
    def available(self) -> bool:
        return self.executable_path is not None and self.version is not None


class LocalCommandRunnerPort(Protocol):
    async def run(
        self,
        capability: LocalCommandCapability,
        *,
        sample_sha256: str,
        sample_bytes: bytes,
        options: Mapping[str, object],
    ) -> LocalCommandResult: ...


@dataclass(frozen=True, slots=True)
class LocalCommandBudgetEstimate:
    """Maximum tool-time reservation before a local command is launched."""

    tool_seconds: float

    def __post_init__(self) -> None:
        if self.tool_seconds < 0:
            raise ValueError("tool_seconds must be non-negative")


class LocalCommandCatalog:
    """The allowlisted local commands that are actually installed and versioned."""

    def __init__(
        self,
        *,
        specs: tuple[LocalCommandSpec, ...] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        version_timeout_seconds: float = 2.0,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        cpu_limit_seconds: int | None = None,
        memory_limit_bytes: int | None = None,
        file_size_limit_bytes: int | None = None,
    ) -> None:
        self._which = which
        self._version_timeout_seconds = version_timeout_seconds
        original_specs = specs or default_local_command_specs()
        self._capabilities = {
            spec.id: self._discover(
                _with_configured_limits(
                    spec,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                    cpu_limit_seconds=cpu_limit_seconds,
                    memory_limit_bytes=memory_limit_bytes,
                    file_size_limit_bytes=file_size_limit_bytes,
                )
            )
            for spec in original_specs
        }

    def get(self, command_id: LocalCommandId) -> LocalCommandCapability | None:
        return self._capabilities.get(command_id)

    def available_commands(self) -> tuple[LocalCommandCapability, ...]:
        return tuple(
            capability for capability in self._capabilities.values() if capability.available
        )

    def all_commands(self) -> tuple[LocalCommandCapability, ...]:
        return tuple(self._capabilities.values())

    def _discover(self, spec: LocalCommandSpec) -> LocalCommandCapability:
        executable = self._which(spec.binary)
        if not executable:
            return LocalCommandCapability(spec, None, None)
        version = _read_executable_version(str(executable), self._version_timeout_seconds)
        if version is None:
            return LocalCommandCapability(spec, None, None)
        return LocalCommandCapability(spec, str(executable), version)


class LocalCommandService:
    def __init__(
        self,
        samples: SampleBytesForAnalysisPort,
        runner: LocalCommandRunnerPort,
        catalog: LocalCommandCatalog,
        *,
        enabled: bool = False,
        max_sample_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self._samples = samples
        self._runner = runner
        self._catalog = catalog
        self._enabled = enabled
        self._max_sample_bytes = max_sample_bytes

    @property
    def filesystem_confined(self) -> bool:
        return bool(getattr(self._runner, "filesystem_confined", False))

    async def run(
        self,
        command_id: LocalCommandId | str,
        sample_sha256: str,
        options: Mapping[str, object] | None = None,
    ) -> LocalCommandResult:
        try:
            normalized_id = LocalCommandId(command_id)
        except (ValueError, TypeError):
            return LocalCommandResult(
                command_id=None,
                status=LocalCommandStatus.POLICY_DENIED,
                sample_sha256=None,
                detail_code="unknown_command",
            )

        if not _SHA256.fullmatch(sample_sha256):
            return _result(normalized_id, LocalCommandStatus.POLICY_DENIED, None, "invalid_sha256")
        capability = self._catalog.get(normalized_id)
        if capability is None:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "command_not_catalogued",
            )
        if not self._enabled:
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                "local_analysis_disabled",
            )
        if not capability.available:
            return _result(
                normalized_id, LocalCommandStatus.UNAVAILABLE, sample_sha256, "binary_unavailable"
            )
        normalized_options, error = normalize_command_options(capability.spec, options or {})
        if error is not None:
            return _result(normalized_id, LocalCommandStatus.POLICY_DENIED, sample_sha256, error)

        try:
            sample_bytes = await self._samples.get_sample_bytes(
                sample_sha256, max_bytes=self._max_sample_bytes
            )
        except SampleBytesUnavailableError:
            return _result(
                normalized_id, LocalCommandStatus.UNAVAILABLE, sample_sha256, "sample_unavailable"
            )
        if len(sample_bytes) > self._max_sample_bytes:
            return _result(
                normalized_id, LocalCommandStatus.POLICY_DENIED, sample_sha256, "sample_too_large"
            )
        if _sha256(sample_bytes) != sample_sha256:
            return _result(
                normalized_id, LocalCommandStatus.FAILED, sample_sha256, "sample_hash_mismatch"
            )

        sample_type = detect_file_type(sample_bytes)
        if sample_type not in capability.spec.accepted_file_types:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "unsupported_file_type",
            )
        return await self._runner.run(
            capability,
            sample_sha256=sample_sha256,
            sample_bytes=sample_bytes,
            options=normalized_options,
        )

    def estimate_budget(self, command_id: LocalCommandId | str) -> LocalCommandBudgetEstimate:
        try:
            normalized_id = LocalCommandId(command_id)
        except (ValueError, TypeError):
            return LocalCommandBudgetEstimate(0)
        capability = self._catalog.get(normalized_id)
        return LocalCommandBudgetEstimate(
            capability.spec.timeout_seconds if capability is not None else 0
        )


class GhidraAnalysisPort(Protocol):
    async def analyze(
        self,
        operation: GhidraOperation,
        sample_sha256: str,
        *,
        address: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> GhidraObservation: ...


def default_local_command_specs() -> tuple[LocalCommandSpec, ...]:
    any_file = frozenset(LocalAnalysisFileType)
    elf = frozenset({LocalAnalysisFileType.ELF})
    native = frozenset({LocalAnalysisFileType.ELF, LocalAnalysisFileType.PE})
    return (
        LocalCommandSpec(
            LocalCommandId.FILE,
            "file",
            (LocalCommandOptionSpec("mime_type", LocalOptionKind.BOOLEAN, False),),
            any_file,
            10,
            16_384,
            5,
            256 * 1024 * 1024,
            1 * 1024 * 1024,
            LocalCommandOutputKind.TEXT,
        ),
        LocalCommandSpec(
            LocalCommandId.STRINGS,
            "strings",
            (
                LocalCommandOptionSpec(
                    "min_length", LocalOptionKind.INTEGER, 4, minimum=1, maximum=256
                ),
                LocalCommandOptionSpec(
                    "max_strings", LocalOptionKind.INTEGER, 1000, minimum=1, maximum=10_000
                ),
            ),
            any_file,
            20,
            256 * 1024,
            10,
            384 * 1024 * 1024,
            1 * 1024 * 1024,
            LocalCommandOutputKind.LINES,
        ),
        LocalCommandSpec(
            LocalCommandId.READELF,
            "readelf",
            (
                LocalCommandOptionSpec(
                    "mode",
                    LocalOptionKind.ENUM,
                    "header",
                    allowed_values=("header", "program_headers", "sections", "dynamic", "symbols"),
                ),
            ),
            elf,
            20,
            512 * 1024,
            10,
            384 * 1024 * 1024,
            1 * 1024 * 1024,
            LocalCommandOutputKind.TEXT,
        ),
        LocalCommandSpec(
            LocalCommandId.OBJDUMP,
            "objdump",
            (
                LocalCommandOptionSpec(
                    "mode",
                    LocalOptionKind.ENUM,
                    "file_header",
                    allowed_values=("file_header", "sections", "private_headers", "disassemble"),
                ),
            ),
            native,
            30,
            1 * 1024 * 1024,
            15,
            512 * 1024 * 1024,
            1 * 1024 * 1024,
            LocalCommandOutputKind.TEXT,
        ),
        LocalCommandSpec(
            LocalCommandId.YARA,
            "yara",
            (
                LocalCommandOptionSpec(
                    "patterns",
                    LocalOptionKind.STRING_LIST,
                    None,
                    max_items=32,
                    max_item_length=128,
                ),
                LocalCommandOptionSpec(
                    "match_mode",
                    LocalOptionKind.ENUM,
                    "any",
                    allowed_values=("any", "all"),
                ),
            ),
            any_file,
            30,
            64 * 1024,
            15,
            512 * 1024 * 1024,
            1 * 1024 * 1024,
            LocalCommandOutputKind.MATCHES,
        ),
    )


def normalize_command_options(
    spec: LocalCommandSpec, options: Mapping[str, object]
) -> tuple[dict[str, object], str | None]:
    if not isinstance(options, Mapping):
        return {}, "options_must_be_object"
    allowed = {option.name: option for option in spec.allowed_options}
    if any(not isinstance(name, str) or name not in allowed for name in options):
        return {}, "option_not_allowed"
    normalized: dict[str, object] = {}
    for name, option_spec in allowed.items():
        if name not in options:
            if option_spec.default is None:
                return {}, "required_option_missing"
            value = option_spec.default
        else:
            value = options[name]
        if option_spec.kind is LocalOptionKind.BOOLEAN:
            if type(value) is not bool:
                return {}, "invalid_boolean_option"
        elif option_spec.kind is LocalOptionKind.INTEGER:
            if type(value) is not int:
                return {}, "invalid_integer_option"
            if option_spec.minimum is not None and value < option_spec.minimum:
                return {}, "integer_option_below_minimum"
            if option_spec.maximum is not None and value > option_spec.maximum:
                return {}, "integer_option_above_maximum"
        elif option_spec.kind is LocalOptionKind.ENUM:
            if not isinstance(value, str) or value not in option_spec.allowed_values:
                return {}, "enum_option_not_allowed"
        elif option_spec.kind is LocalOptionKind.STRING_LIST:
            if not isinstance(value, (list, tuple)):
                return {}, "invalid_string_list_option"
            if not value or (
                option_spec.max_items is not None and len(value) > option_spec.max_items
            ):
                return {}, "string_list_size_out_of_range"
            if any(
                not isinstance(item, str)
                or not item
                or _not_utf8(item)
                or (
                    option_spec.max_item_length is not None
                    and len(item) > option_spec.max_item_length
                )
                for item in value
            ):
                return {}, "string_list_item_invalid"
            value = tuple(value)
        normalized[name] = value
    return normalized, None


def detect_file_type(sample_bytes: bytes) -> LocalAnalysisFileType:
    if sample_bytes.startswith(b"\x7fELF"):
        return LocalAnalysisFileType.ELF
    if len(sample_bytes) >= 0x40 and sample_bytes[:2] == b"MZ":
        pe_offset = int.from_bytes(sample_bytes[0x3C:0x40], "little")
        if (
            pe_offset <= len(sample_bytes) - 4
            and sample_bytes[pe_offset : pe_offset + 4] == b"PE\0\0"
        ):
            return LocalAnalysisFileType.PE
    return LocalAnalysisFileType.OTHER


def _with_configured_limits(
    spec: LocalCommandSpec,
    *,
    timeout_seconds: float | None,
    max_output_bytes: int | None,
    cpu_limit_seconds: int | None,
    memory_limit_bytes: int | None,
    file_size_limit_bytes: int | None,
) -> LocalCommandSpec:
    return LocalCommandSpec(
        id=spec.id,
        binary=spec.binary,
        allowed_options=spec.allowed_options,
        accepted_file_types=spec.accepted_file_types,
        timeout_seconds=timeout_seconds or spec.timeout_seconds,
        max_output_bytes=max_output_bytes or spec.max_output_bytes,
        cpu_limit_seconds=cpu_limit_seconds or spec.cpu_limit_seconds,
        memory_limit_bytes=memory_limit_bytes or spec.memory_limit_bytes,
        file_size_limit_bytes=file_size_limit_bytes or spec.file_size_limit_bytes,
        output_kind=spec.output_kind,
    )


def _read_executable_version(executable: str, timeout_seconds: float) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).decode("utf-8", errors="replace")
    version = " ".join(output.split())[:160]
    return version or None


def _result(
    command_id: LocalCommandId,
    status: LocalCommandStatus,
    sample_sha256: str | None,
    detail_code: str,
) -> LocalCommandResult:
    return LocalCommandResult(
        command_id=command_id,
        status=status,
        sample_sha256=sample_sha256,
        normalized_command=(command_id.value,),
        detail_code=detail_code,
    )


def _sha256(value: bytes) -> str:
    return sha256(value).hexdigest()


def _not_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
