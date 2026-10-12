"""Privacy-safe, append-only JSONL trace for an emulated workflow."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

TraceVia = Literal["http", "service", "fixture"]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    scenario: str
    step: str
    action: str
    via: TraceVia
    status: str
    identities: dict[str, str]
    assertions: tuple[str, ...]


def short_identity(value: object) -> str:
    text = str(value)
    return text[:8]


class TraceWriter:
    def __init__(self, scenario: str, tmp_path: Path) -> None:
        trace_dir = os.getenv("WORKFLOW_TRACE_DIR")
        root = Path(trace_dir) if trace_dir else tmp_path
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{scenario}.jsonl"

    def append(self, event: TraceEvent) -> None:
        payload = json.dumps(asdict(event), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.write("\n")
