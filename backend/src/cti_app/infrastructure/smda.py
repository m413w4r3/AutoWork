from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cti_app.domain.code_features import CodeFunction, CodeInstruction
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)


class SmdaOutputError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class SmdaExtraction:
    smda_version: str
    escaper_compatibility_version: str
    intel_pic_hash_escape_version: str
    architecture: str
    functions: tuple[CodeFunction, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SmdaAdapterResult:
    status: str
    extraction: SmdaExtraction | None = None
    error: str | None = None


Runner = Callable[..., Awaitable[AnalysisSubprocessResult]]


class SmdaAdapter:
    """Boundary for the JSON-only SMDA wrapper.

    The application receives typed data from this class and never imports the
    optional SMDA dependency.
    """

    def __init__(
        self,
        wrapper_path: Path = Path("tools/smda_extract.py"),
        *,
        runner: Runner = run_analysis_subprocess,
        python_executable: str = sys.executable,
    ) -> None:
        self.wrapper_path = wrapper_path
        self._runner = runner
        self._python_executable = python_executable

    async def extract(
        self,
        sample_path: Path,
        *,
        timeout_seconds: float,
        output_limit: int,
        memory_limit_bytes: int,
    ) -> SmdaAdapterResult:
        try:
            result = await self._runner(
                [self._python_executable, str(self.wrapper_path), str(sample_path)],
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
                memory_limit_bytes=memory_limit_bytes,
            )
        except (OSError, FileNotFoundError) as exc:
            return SmdaAdapterResult(status="UNAVAILABLE", error=type(exc).__name__)
        if result.status is not AnalysisSubprocessStatus.SUCCEEDED:
            status = (
                "INVALID_OUTPUT"
                if result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                else "UNAVAILABLE"
            )
            return SmdaAdapterResult(status=status, error=result.status.value)
        try:
            extraction = parse_smda_output(result.stdout)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return SmdaAdapterResult(status="INVALID_OUTPUT", error=str(exc))
        if extraction.architecture not in {"x86", "x64"}:
            return SmdaAdapterResult(status="UNAVAILABLE", error="unsupported architecture")
        return SmdaAdapterResult(status="SUCCEEDED", extraction=extraction)


def parse_smda_output(raw: bytes | str | Mapping[str, Any]) -> SmdaExtraction:
    if isinstance(raw, Mapping):
        data: Mapping[str, Any] = raw
    else:
        data = json.loads(raw)
    required = (
        "smda_version",
        "escaper_compatibility_version",
        "intel_pic_hash_escape_version",
        "architecture",
        "functions",
    )
    if set(data) != set(required):
        raise SmdaOutputError("SMDA output schema is not minimal")
    architecture = _architecture(data["architecture"])
    functions: list[CodeFunction] = []
    for function_data in _list(data["functions"], "functions"):
        function = _parse_function(function_data)
        functions.append(function)
    return SmdaExtraction(
        smda_version=_string(data["smda_version"], "smda_version"),
        escaper_compatibility_version=_string(
            data["escaper_compatibility_version"], "escaper_compatibility_version"
        ),
        intel_pic_hash_escape_version=_string(
            data["intel_pic_hash_escape_version"], "intel_pic_hash_escape_version"
        ),
        architecture=architecture,
        functions=tuple(sorted(functions, key=lambda item: item.offset)),
    )


def _parse_function(data: Any) -> CodeFunction:
    if not isinstance(data, Mapping) or set(data) != {"offset", "basic_blocks"}:
        raise SmdaOutputError("invalid SMDA function")
    instructions: list[CodeInstruction] = []
    for block in _list(data["basic_blocks"], "basic_blocks"):
        if not isinstance(block, Mapping) or set(block) != {"offset", "instructions"}:
            raise SmdaOutputError("invalid SMDA basic block")
        for instruction in _list(block["instructions"], "instructions"):
            if not isinstance(instruction, Mapping) or set(instruction) != {
                "offset",
                "bytes",
                "mnemonic",
                "escaped_bytes",
            }:
                raise SmdaOutputError("invalid SMDA instruction")
            raw_bytes = _bytes(instruction["bytes"], "bytes")
            escaped = tuple(_list(instruction["escaped_bytes"], "escaped_bytes"))
            instructions.append(
                CodeInstruction(
                    offset=_integer(instruction["offset"], "offset"),
                    bytes=raw_bytes,
                    mnemonic=_string(instruction["mnemonic"], "mnemonic"),
                    escaped_bytes=escaped,
                )
            )
    return CodeFunction(
        offset=_integer(data["offset"], "offset"),
        instructions=tuple(sorted(instructions, key=lambda item: item.offset)),
    )


def _architecture(value: Any) -> str:
    architecture = _string(value, "architecture").lower()
    aliases = {"intel.32bit": "x86", "intel.64bit": "x64", "x86": "x86", "x64": "x64"}
    return aliases.get(architecture, architecture)


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SmdaOutputError(f"{name} must be a list")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SmdaOutputError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SmdaOutputError(f"{name} must be a non-negative integer")
    return value


def _bytes(value: Any, name: str) -> bytes:
    if isinstance(value, str):
        try:
            output = bytes.fromhex(value)
        except ValueError as exc:
            raise SmdaOutputError(f"{name} must be hex") from exc
        if not output:
            raise SmdaOutputError(f"{name} must not be empty")
        return output
    if isinstance(value, list) and all(
        isinstance(item, int) and 0 <= item <= 255 for item in value
    ):
        output = bytes(value)
        if output:
            return output
    raise SmdaOutputError(f"{name} must be non-empty bytes")
