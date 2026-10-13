from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BehaviorEvidenceSource(StrEnum):
    CAPE = "cape"
    TRIAGE = "triage"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class BehaviorProcessV1:
    process_id: str
    parent_process_id: str | None
    name: str | None
    command_line: str | None
    started_at: datetime | None
    evidence_path: str


@dataclass(frozen=True, slots=True)
class DroppedFileV1:
    path: str | None
    sha256: str | None
    size: int | None
    evidence_path: str

    def __post_init__(self) -> None:
        if self.sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("Dropped file SHA-256 must be lowercase hexadecimal")
        if self.size is not None and self.size < 0:
            raise ValueError("Dropped file size must be non-negative")


@dataclass(frozen=True, slots=True)
class NetworkSessionV1:
    protocol: str | None
    source_ip: str | None
    source_port: int | None
    destination_ip: str | None
    destination_port: int | None
    host: str | None
    uri: str | None
    started_at: datetime | None
    ended_at: datetime | None
    evidence_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedBehaviorV1:
    category: str
    description: str
    timestamp: datetime | None
    process_id: str | None
    evidence_path: str


@dataclass(frozen=True, slots=True)
class BehaviorEvidenceV1:
    schema_version: str
    source: BehaviorEvidenceSource
    source_report_sha256: str
    sample_sha256: str
    report_timestamp: datetime | None
    processes: tuple[BehaviorProcessV1, ...]
    dropped_files: tuple[DroppedFileV1, ...]
    network_sessions: tuple[NetworkSessionV1, ...]
    behaviors: tuple[ObservedBehaviorV1, ...]
    truncated: bool = False
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("Unsupported behavior evidence schema version")
        for value in (self.source_report_sha256, self.sample_sha256):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("Behavior evidence hashes must be lowercase SHA-256")
        object.__setattr__(self, "processes", tuple(self.processes))
        object.__setattr__(self, "dropped_files", tuple(self.dropped_files))
        object.__setattr__(self, "network_sessions", tuple(self.network_sessions))
        object.__setattr__(self, "behaviors", tuple(self.behaviors))
        object.__setattr__(self, "limitations", tuple(self.limitations))
