from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field

from cti_app.application.jobs import JobExecutionContext, JobHandler, JobParameters, JobRegistry
from cti_app.domain.investigation import ToolInvocationStatus

CONFINED_ANALYSIS_JOB_KIND = "sample.confined_analysis.v1"
CONFINED_ANALYSIS_RESULT_SCHEMA = "confined-analysis-result.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConfinedAnalysisTool(StrEnum):
    SAMPLE_ANALYSIS = "sample_analysis"
    STATIC = "static"
    CAPA = "capa"
    SMDA = "smda"
    TSHARK = "tshark"
    CAPINFOS = "capinfos"
    FLOSS = "floss"
    MWCP = "mwcp"
    YARA = "yara"


class ConfinedAnalysisArtifactKind(StrEnum):
    SAMPLE = "sample"
    CAPTURE = "capture"
    CORPUS_SAMPLE = "corpus_sample"


@dataclass(frozen=True, slots=True)
class ConfinedAnalysisFamilyStatus:
    family: str
    available: bool
    version: str | None = None
    available_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConfinedAnalysisWorkerAttestation:
    reachable: bool
    confined: bool
    isolation_profile: str | None
    versions: Mapping[str, str | None]
    families: Mapping[str, ConfinedAnalysisFamilyStatus]
    dependencies_ready: bool = True
    available_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConfinedAnalysisRequest:
    investigation_id: UUID
    artifact_id: UUID
    sha256: str
    tool: ConfinedAnalysisTool
    timeout_seconds: float
    max_output_bytes: int
    artifact_kind: ConfinedAnalysisArtifactKind = ConfinedAnalysisArtifactKind.SAMPLE
    association_id: UUID | None = None
    sample_id: UUID | None = None
    arguments: tuple[str, ...] = ()
    analysis_parameters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("analysis budgets must be positive")
        if self.artifact_kind is ConfinedAnalysisArtifactKind.SAMPLE and (
            self.association_id is None or self.sample_id is None
        ):
            raise ValueError("sample analysis requires canonical sample and association identities")
        if self.artifact_kind is ConfinedAnalysisArtifactKind.CAPTURE and self.tool not in {
            ConfinedAnalysisTool.TSHARK,
            ConfinedAnalysisTool.CAPINFOS,
        }:
            raise ValueError("capture artifacts support only tshark and capinfos")
        if len(self.arguments) > 128 or any(
            not isinstance(item, str) or len(item) > 1024 or "\x00" in item
            for item in self.arguments
        ):
            raise ValueError("analysis command arguments exceed the bounded contract")
        allowed_parameters = {
            ConfinedAnalysisTool.FLOSS: {"max_strings"},
            ConfinedAnalysisTool.MWCP: {"parser_id", "parser_version"},
            ConfinedAnalysisTool.YARA: {"rule_source"},
        }.get(self.tool, set())
        if set(self.analysis_parameters) - allowed_parameters:
            raise ValueError("analysis parameters are not supported for this tool")
        if self.artifact_kind is ConfinedAnalysisArtifactKind.CORPUS_SAMPLE and self.tool not in {
            ConfinedAnalysisTool.FLOSS,
            ConfinedAnalysisTool.MWCP,
            ConfinedAnalysisTool.YARA,
        }:
            raise ValueError("corpus sample artifacts support only static extraction tools")
        if self.tool is ConfinedAnalysisTool.FLOSS:
            value = self.analysis_parameters.get("max_strings", "128")
            if not value.isdigit() or not 1 <= int(value) <= 4096:
                raise ValueError("FLOSS string limit is invalid")
        if self.tool is ConfinedAnalysisTool.MWCP:
            if not all(
                self.analysis_parameters.get(key) for key in ("parser_id", "parser_version")
            ):
                raise ValueError("MWCP execution requires a versioned allowlisted parser")
        if self.tool is ConfinedAnalysisTool.YARA:
            rule_source = self.analysis_parameters.get("rule_source")
            if not isinstance(rule_source, str) or not rule_source.strip():
                raise ValueError("YARA execution requires a rule source")
            if len(rule_source.encode("utf-8")) > 256_000:
                raise ValueError("YARA source exceeds the bounded contract")


@dataclass(frozen=True, slots=True)
class ConfinedAnalysisResult:
    status: ToolInvocationStatus
    operation_key: str
    artifact_id: UUID
    input_sha256: str
    result_blob_id: UUID | None = None
    result_sha256: str | None = None
    payload: Mapping[str, Any] | None = None
    available_reason: str | None = None


class ConfinedAnalysisExecutionPort(Protocol):
    """Generic bounded execution boundary for a canonical artifact in analysis-worker."""

    @property
    def available(self) -> bool: ...

    @property
    def available_reason(self) -> str | None: ...

    @property
    def family_statuses(self) -> Mapping[str, ConfinedAnalysisFamilyStatus]: ...

    @property
    def isolation_profile(self) -> str | None: ...

    @property
    def attestation(self) -> ConfinedAnalysisWorkerAttestation: ...

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult: ...


class ConfinedAnalysisJobParameters(JobParameters):
    # Durable job rows are JSON. This contract deliberately accepts their JSON
    # UUID/enumeration forms when the worker validates the persisted payload.
    model_config = ConfigDict(extra="forbid", strict=False)

    investigation_id: UUID
    artifact_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(pattern=r"^confined-analysis:[0-9a-f]{64}$", max_length=96)
    tool: ConfinedAnalysisTool
    timeout_seconds: float = Field(gt=0, le=600)
    max_output_bytes: int = Field(gt=0, le=20_000_000)
    artifact_kind: ConfinedAnalysisArtifactKind = ConfinedAnalysisArtifactKind.SAMPLE
    association_id: UUID | None = None
    sample_id: UUID | None = None
    arguments: tuple[str, ...] = Field(default=(), max_length=128)
    analysis_parameters: dict[str, str] = Field(default_factory=dict, max_length=4)


def create_confined_analysis_job_registry(handler: JobHandler | None = None) -> JobRegistry:
    registry = JobRegistry()

    async def no_worker_handler(_parameters: JobParameters, _context: JobExecutionContext) -> str:
        raise RuntimeError("confined analysis jobs can only run in analysis-worker")

    registry.register(
        CONFINED_ANALYSIS_JOB_KIND,
        ConfinedAnalysisJobParameters,
        handler or no_worker_handler,
    )
    return registry


def deterministic_confined_analysis_key(
    request: ConfinedAnalysisRequest, *, worker_versions: Mapping[str, str | None]
) -> str:
    payload = {
        "schema": "confined-analysis-operation.v1",
        "investigation_id": str(request.investigation_id),
        "association_id": str(request.association_id),
        "sample_id": str(request.sample_id),
        "artifact_kind": request.artifact_kind.value,
        "artifact_id": str(request.artifact_id),
        "sha256": request.sha256,
        "tool": request.tool.value,
        "arguments": request.arguments,
        "analysis_parameters": dict(sorted(request.analysis_parameters.items())),
        "versions": dict(sorted(worker_versions.items())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"confined-analysis:{hashlib.sha256(encoded).hexdigest()}"
