"""Application port and result contract for compiling canonical diagrams."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from cti_app.domain.production_editorial_enrichment import DiagramSpecV1

DIAGRAM_COMPILATION_POLICY_VERSION = "diagram-d2-svg-v3-relation-semantics"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _validate_bytes_hash(raw: bytes, digest: str, field_name: str) -> None:
    if not isinstance(raw, bytes):
        raise ValueError(f"{field_name} must be bytes")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{field_name} SHA-256 must be 64 lowercase hexadecimal characters")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{field_name} SHA-256 does not match its bytes")


@dataclass(frozen=True, slots=True)
class CompiledDiagram:
    diagram_key: str
    source_format: str
    source_bytes: bytes
    source_sha256: str
    media_type: str
    media_bytes: bytes
    media_sha256: str
    compiler: str
    compiler_version: str
    compiler_policy_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "diagram_key",
            "source_format",
            "media_type",
            "compiler",
            "compiler_version",
            "compiler_policy_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
        _validate_bytes_hash(self.source_bytes, self.source_sha256, "source")
        _validate_bytes_hash(self.media_bytes, self.media_sha256, "media")


class DiagramCompiler(Protocol):
    async def compile(self, diagram: DiagramSpecV1) -> CompiledDiagram: ...


class DiagramCompilationError(Exception):
    """Base class for stable, typed application compilation failures."""

    code = "diagram_compilation_error"


class DiagramCompilerUnavailableError(DiagramCompilationError):
    code = "diagram_compiler_unavailable"


class DiagramCompilerVersionError(DiagramCompilationError):
    code = "diagram_compiler_version_mismatch"


class DiagramCompilerTimeoutError(DiagramCompilationError):
    code = "diagram_compiler_timeout"


class DiagramCompilerProcessError(DiagramCompilationError):
    code = "diagram_compiler_process_failure"


class DiagramCompilerOutputTooLargeError(DiagramCompilationError):
    code = "diagram_compiler_output_too_large"


class DiagramCompilerOutputInvalidError(DiagramCompilationError):
    code = "diagram_compiler_output_invalid"


class UnsupportedDiagramStructureError(DiagramCompilationError):
    code = "unsupported_diagram_structure"
