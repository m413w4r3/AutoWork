from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ExtractionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    FAILED = "failed"
    TIMEOUT = "timeout"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ExtractionProvenance:
    tool_id: str
    tool_version: str
    parameters_sha256: str
    raw_output_sha256: str | None
    raw_output_bytes: int
    raw_output_complete: bool
    output_truncated: bool
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not self.tool_id.strip() or not self.tool_version.strip():
            raise ValueError("Extraction tool identity and version are required")
        if re.fullmatch(r"[0-9a-f]{64}", self.parameters_sha256) is None:
            raise ValueError("parameters_sha256 must be lowercase SHA-256")
        if (
            self.raw_output_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.raw_output_sha256) is None
        ):
            raise ValueError("raw_output_sha256 must be lowercase SHA-256")
        if self.raw_output_bytes < 0:
            raise ValueError("raw_output_bytes must be non-negative")
        if self.raw_output_complete and self.raw_output_sha256 is None:
            raise ValueError("Complete raw output requires a digest")
