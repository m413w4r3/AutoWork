from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cti_app.domain.extraction import ExtractionProvenance, ExtractionStatus


class ConfigurationValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


class NetworkIndicatorKind(StrEnum):
    IP_ADDRESS = "ip_address"
    DOMAIN = "domain"
    URL = "url"


type JsonScalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ConfigurationKeyValue:
    key: str
    value_type: ConfigurationValueType
    value: JsonScalar
    evidence_path: str
    truncated_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key or not self.evidence_path.startswith("/"):
            raise ValueError("Configuration key and JSON evidence path are required")


@dataclass(frozen=True, slots=True)
class NetworkIndicator:
    kind: NetworkIndicatorKind
    value: str
    normalized_value: str
    evidence_path: str
    truncated_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigurationExtractionV1:
    schema_version: str
    sample_sha256: str
    parser_id: str
    parser_version: str
    status: ExtractionStatus
    values: tuple[ConfigurationKeyValue, ...]
    network_indicators: tuple[NetworkIndicator, ...]
    provenance: ExtractionProvenance
    truncated: bool = False
    detail_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("Unsupported configuration extraction schema version")
        if re.fullmatch(r"[0-9a-f]{64}", self.sample_sha256) is None:
            raise ValueError("sample_sha256 must be lowercase SHA-256")
        if not self.parser_id.strip() or not self.parser_version.strip():
            raise ValueError("An allowlisted parser identity and version are required")
        object.__setattr__(self, "values", tuple(self.values))
        object.__setattr__(self, "network_indicators", tuple(self.network_indicators))
