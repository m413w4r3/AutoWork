from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cti_app.domain.extraction import ExtractionProvenance, ExtractionStatus


class ExtractedStringType(StrEnum):
    STATIC = "static"
    STACK = "stack"
    TIGHT = "tight"
    DECODED = "decoded"


@dataclass(frozen=True, slots=True)
class ExtractedString:
    value: str
    kind: ExtractedStringType
    encoding: str | None = None
    offset: int | None = None
    address: str | None = None
    function: str | None = None
    context: str | None = None
    evidence_path: str | None = None

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Extracted string value must not be empty")
        if self.offset is not None and self.offset < 0:
            raise ValueError("String offset must be non-negative")
        if self.address is not None and not self.address.strip():
            raise ValueError("String address must not be empty")


@dataclass(frozen=True, slots=True)
class StringExtractionV1:
    schema_version: str
    sample_sha256: str
    status: ExtractionStatus
    strings: tuple[ExtractedString, ...]
    provenance: ExtractionProvenance
    strings_observed: int
    truncated: bool = False
    detail_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("Unsupported string extraction schema version")
        if re.fullmatch(r"[0-9a-f]{64}", self.sample_sha256) is None:
            raise ValueError("sample_sha256 must be lowercase SHA-256")
        if self.strings_observed < len(self.strings):
            raise ValueError("Observed string count cannot be less than recorded strings")
        object.__setattr__(self, "strings", tuple(self.strings))
