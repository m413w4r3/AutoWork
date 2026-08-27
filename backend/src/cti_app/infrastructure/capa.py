from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    run_analysis_subprocess,
)


class SubprocessRunner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit: int,
        environment: dict[str, str] | None = None,
        memory_limit_bytes: int,
    ) -> AnalysisSubprocessResult: ...


class CapaMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: StrictStr
    namespace: StrictStr = ""
    attack: list[StrictStr] = Field(default_factory=list)
    mbc: list[StrictStr] = Field(default_factory=list)


class CapaMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    locations: list[Any] = Field(default_factory=list)


class CapaRule(BaseModel):
    model_config = ConfigDict(extra="ignore")
    meta: CapaMeta
    matches: list[CapaMatch] = Field(default_factory=list)


class CapaOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rules: dict[str, CapaRule]


def ruleset_manifest(rules_path: Path) -> str | None:
    if not rules_path.is_dir():
        return None
    files = sorted(path for path in rules_path.rglob("*.yml") if path.is_file())
    if not files:
        return None
    lines = []
    for path in files:
        relative = path.relative_to(rules_path).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{relative}\t{digest}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def parse_capa_output(payload: bytes) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    try:
        parsed = CapaOutput.model_validate_json(payload)
    except Exception as exc:
        return (), (f"invalid capa JSON: {type(exc).__name__}",)
    capabilities = []
    for rule_id, rule in parsed.rules.items():
        addresses = []
        for match in rule.matches:
            addresses.extend(str(location) for location in match.locations)
        capabilities.append(
            {
                "rule_id": rule_id,
                "name": rule.meta.name,
                "namespace": rule.meta.namespace,
                "attack": tuple(sorted(set(rule.meta.attack))),
                "mbc": tuple(sorted(set(rule.meta.mbc))),
                "function_addresses": tuple(sorted(set(addresses))),
            }
        )
    capabilities.sort(key=lambda item: (item["rule_id"], item["name"], item["namespace"]))
    return tuple(capabilities), ()


class CapaRunner:
    def __init__(self, runner: SubprocessRunner = run_analysis_subprocess) -> None:
        self._runner = runner

    async def run(
        self,
        *,
        sample: bytes,
        rules_path: Path,
        timeout_seconds: float,
        output_limit: int,
        memory_limit_bytes: int,
    ) -> tuple[str, AnalysisSubprocessResult]:
        with tempfile.NamedTemporaryFile(prefix="cti-capa-", delete=False) as handle:
            handle.write(sample)
            sample_path = handle.name
        try:
            result = await self._runner(
                ["capa", "-r", str(rules_path), "--json", sample_path],
                timeout_seconds=timeout_seconds, output_limit=output_limit,
                memory_limit_bytes=memory_limit_bytes,
                environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
            )
            return "9.4.0", result
        finally:
            try:
                os.unlink(sample_path)
            except FileNotFoundError:
                pass
