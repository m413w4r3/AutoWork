from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID


class CapabilitySetStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_OUTPUT = "INVALID_OUTPUT"


@dataclass(frozen=True, slots=True, kw_only=True)
class Capability:
    rule_id: str
    name: str
    namespace: str
    attack: tuple[str, ...]
    mbc: tuple[str, ...]
    function_addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilitySet:
    sample_id: UUID
    tool_name: str
    tool_version: str
    ruleset_sha256: str
    parameters_sha256: str
    status: CapabilitySetStatus
    capabilities: tuple[Capability, ...]
    errors: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "sample_id": str(self.sample_id),
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "ruleset_sha256": self.ruleset_sha256,
            "parameters_sha256": self.parameters_sha256,
            "status": self.status.value,
            "capabilities": [
                {
                    "rule_id": item.rule_id,
                    "name": item.name,
                    "namespace": item.namespace,
                    "attack": list(item.attack),
                    "mbc": list(item.mbc),
                    "function_addresses": list(item.function_addresses),
                }
                for item in self.capabilities
            ],
            "errors": list(self.errors),
        }
