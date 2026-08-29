from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class CodeFeatureStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_OUTPUT = "INVALID_OUTPUT"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeInstruction:
    offset: int
    bytes: bytes
    mnemonic: str
    escaped_bytes: tuple[Any, ...]

    def __post_init__(self) -> None:
        if self.offset < 0 or not self.bytes or not self.mnemonic.strip():
            raise ValueError("invalid code instruction")
        if len(self.escaped_bytes) != len(self.bytes):
            raise ValueError("escaped_bytes must have one token per byte")


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeFunction:
    offset: int
    instructions: tuple[CodeInstruction, ...]

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("function offset must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True, init=False)
class CodeNgram:
    pattern: str
    instruction_count: int
    byte_count: int
    fixed_byte_count: int
    masked_byte_count: int
    longest_fixed_run: int
    function_offset: int
    start_offset: int
    mnemonics: tuple[str, ...]
    occurrence_count: int = 1

    def __init__(
        self,
        *,
        pattern: str,
        instruction_count: int,
        byte_count: int,
        fixed_byte_count: int,
        masked_byte_count: int,
        longest_fixed_run: int,
        function_offset: int,
        start_offset: int,
        mnemonics: tuple[str, ...],
        occurrence_count: int = 1,
        **_legacy_fields: object,
    ) -> None:
        del _legacy_fields
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "instruction_count", instruction_count)
        object.__setattr__(self, "byte_count", byte_count)
        object.__setattr__(self, "fixed_byte_count", fixed_byte_count)
        object.__setattr__(self, "masked_byte_count", masked_byte_count)
        object.__setattr__(self, "longest_fixed_run", longest_fixed_run)
        object.__setattr__(self, "function_offset", function_offset)
        object.__setattr__(self, "start_offset", start_offset)
        object.__setattr__(self, "mnemonics", mnemonics)
        object.__setattr__(self, "occurrence_count", occurrence_count)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.instruction_count < 1 or self.byte_count < 1:
            raise ValueError("invalid ngram size")
        if self.fixed_byte_count + self.masked_byte_count != self.byte_count:
            raise ValueError("fixed and masked byte counts must add up")
        if self.occurrence_count < 1:
            raise ValueError("occurrence_count must be positive")


CODE_NGRAM_STRUCTURAL_FIELDS = (
    "pattern",
    "instruction_count",
    "byte_count",
    "fixed_byte_count",
    "masked_byte_count",
    "longest_fixed_run",
    "function_offset",
    "start_offset",
    "mnemonics",
    "occurrence_count",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PackingSignals:
    max_executable_section_entropy: float | None
    executable_bytes: int
    recovered_function_count: int
    executable_bytes_per_function: float | None
    known_packer_marker_hits: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeFeatureSet:
    sample_id: UUID
    blob_id: UUID
    tool_version: str
    escaper_compatibility_version: str
    intel_pic_hash_escape_version: str
    parameters_sha256: str
    architecture: str
    status: CodeFeatureStatus
    ngrams: tuple[CodeNgram, ...]
    packing: PackingSignals
    id: UUID = field(default_factory=uuid4)
    feature_blob_id: UUID | None = None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_version.strip() or not self.parameters_sha256.strip():
            raise ValueError("tool version and parameters hash are required")

    def as_json(self) -> dict[str, Any]:
        return {
            "sample_id": str(self.sample_id),
            "blob_id": str(self.blob_id),
            "tool_version": self.tool_version,
            "escaper_compatibility_version": self.escaper_compatibility_version,
            "intel_pic_hash_escape_version": self.intel_pic_hash_escape_version,
            "parameters_sha256": self.parameters_sha256,
            "architecture": self.architecture,
            "status": self.status.value,
            "ngrams": [
                {
                    **{name: getattr(ngram, name) for name in CODE_NGRAM_STRUCTURAL_FIELDS},
                    "mnemonics": list(ngram.mnemonics),
                }
                for ngram in self.ngrams
            ],
            "packing": {
                name: getattr(self.packing, name) for name in self.packing.__dataclass_fields__
            },
            "errors": list(self.errors),
        }


def validate_ngram_sizes(sizes: Iterable[int]) -> tuple[int, ...]:
    normalized = tuple(sizes)
    if any(size < 2 for size in normalized):
        raise ValueError("code ngram sizes must be at least 2")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("code ngram sizes must be unique and sorted")
    if not normalized:
        raise ValueError("at least one code ngram size is required")
    return normalized


def _escaped_is_masked(token: Any) -> bool:
    if token is None or token is False:
        return True
    if isinstance(token, str):
        return token.strip() in {"?", "??", "*", "masked", "MASKED"}
    return isinstance(token, int) and not 0 <= token <= 255


def _escaped_byte(token: Any) -> str:
    if _escaped_is_masked(token):
        return "??"
    if isinstance(token, str):
        value = token.strip().removeprefix("0x")
        if len(value) == 2:
            int(value, 16)
            return value.lower()
    if isinstance(token, bytes) and len(token) == 1:
        return f"{token[0]:02x}"
    if isinstance(token, int) and 0 <= token <= 255:
        return f"{token:02x}"
    raise ValueError("invalid escaped byte token")


def escaped_pattern(tokens: Iterable[Any]) -> str:
    """Render SMDA's already-classified escaped bytes canonically."""
    return " ".join(_escaped_byte(token) for token in tokens)


def _fixed_run(tokens: Sequence[Any]) -> int:
    longest = current = 0
    for token in tokens:
        if _escaped_is_masked(token):
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _instruction_runs(function: CodeFunction) -> tuple[tuple[CodeInstruction, ...], ...]:
    ordered = sorted(function.instructions, key=lambda instruction: instruction.offset)
    runs: list[list[CodeInstruction]] = []
    for instruction in ordered:
        if not runs or instruction.offset != runs[-1][-1].offset + len(runs[-1][-1].bytes):
            runs.append([])
        runs[-1].append(instruction)
    return tuple(tuple(run) for run in runs)


def build_code_ngrams(
    functions: Iterable[CodeFunction],
    sizes: Iterable[int],
    *,
    max_per_sample: int,
) -> tuple[CodeNgram, ...]:
    sizes = validate_ngram_sizes(sizes)
    if max_per_sample < 1:
        raise ValueError("max_per_sample must be positive")
    output: dict[str, CodeNgram] = {}
    for function in sorted(functions, key=lambda item: item.offset):
        for run in _instruction_runs(function):
            for start in range(len(run)):
                for size in sizes:
                    selected = run[start : start + size]
                    if len(selected) != size:
                        continue
                    tokens = tuple(
                        token for instruction in selected for token in instruction.escaped_bytes
                    )
                    pattern = escaped_pattern(tokens)
                    fixed = sum(not _escaped_is_masked(token) for token in tokens)
                    current = output.get(pattern)
                    if current is not None:
                        output[pattern] = replace(
                            current, occurrence_count=current.occurrence_count + 1
                        )
                    elif len(output) < max_per_sample:
                        output[pattern] = CodeNgram(
                            pattern=pattern,
                            instruction_count=size,
                            byte_count=len(tokens),
                            fixed_byte_count=fixed,
                            masked_byte_count=len(tokens) - fixed,
                            longest_fixed_run=_fixed_run(tokens),
                            function_offset=function.offset,
                            start_offset=selected[0].offset,
                            mnemonics=tuple(instruction.mnemonic for instruction in selected),
                        )
    return tuple(
        sorted(
            output.values(),
            key=lambda item: (item.function_offset, item.start_offset, item.pattern),
        )
    )


def opcode_fragment16_lookup_value(ngram: CodeNgram) -> str | None:
    if ngram.masked_byte_count or not 8 <= ngram.byte_count <= 16:
        return None
    return ngram.pattern.replace(" ", "")


def mapping_to_tuple(data: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((str(key), int(value)) for key, value in data.items()))
