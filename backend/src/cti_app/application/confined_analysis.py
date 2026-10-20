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
from cti_app.domain.local_analysis import (
    LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES,
    LOCAL_COMMAND_MAX_RANGE_BYTES,
    LOCAL_COMMAND_MAX_XXD_RANGE_BYTES,
)

CONFINED_ANALYSIS_JOB_KIND = "sample.confined_analysis.v1"
CONFINED_ANALYSIS_RESULT_SCHEMA = "confined-analysis-result.v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConfinedAnalysisTool(StrEnum):
    SAMPLE_ANALYSIS = "sample_analysis"
    STATIC = "static"
    CAPA = "capa"
    SMDA = "smda"
    TSHARK = "tshark"
    CAPINFOS = "capinfos"
    LOCAL_COMMAND = "local_command"
    FLOSS = "floss"
    MWCP = "mwcp"
    YARA = "yara"
    SURICATA_VALIDATE_RULES = "suricata_validate_rules"
    SURICATA_REPLAY = "suricata_replay"
    DOTNET_ILSPY = "dotnet_ilspy"


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
            ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
            ConfinedAnalysisTool.SURICATA_REPLAY,
        }:
            raise ValueError("capture artifacts support only offline network tools")
        if len(self.arguments) > 128 or any(
            not isinstance(item, str) or len(item) > 1024 or "\x00" in item
            for item in self.arguments
        ):
            raise ValueError("analysis command arguments exceed the bounded contract")
        allowed_parameters = {
            ConfinedAnalysisTool.LOCAL_COMMAND: {
                "mime_type",
                "min_length",
                "max_strings",
                "mode",
                "operation",
                "method_name",
                "method_rid",
                "offset",
                "length",
            },
            ConfinedAnalysisTool.FLOSS: {"max_strings"},
            ConfinedAnalysisTool.MWCP: {"parser_id", "parser_version"},
            ConfinedAnalysisTool.YARA: {"rule_source"},
            ConfinedAnalysisTool.SURICATA_VALIDATE_RULES: {
                "candidate_id",
                "candidate_version",
                "rule_source",
            },
            ConfinedAnalysisTool.SURICATA_REPLAY: {
                "candidate_id",
                "candidate_version",
                "rule_source",
            },
            ConfinedAnalysisTool.DOTNET_ILSPY: {
                "operation",
                "method_rid",
                "offset",
                "limit",
                "max_chars",
            },
        }.get(self.tool, set())
        if set(self.analysis_parameters) - allowed_parameters:
            raise ValueError("analysis parameters are not supported for this tool")
        if self.tool is ConfinedAnalysisTool.DOTNET_ILSPY:
            parameters = self.analysis_parameters
            if self.arguments:
                raise ValueError("dotnet analysis does not accept command arguments")
            if parameters.get("operation") not in {
                "assembly",
                "types",
                "methods",
                "decompile",
                "references",
                "strings",
                "resources",
            }:
                raise ValueError("dotnet operation is invalid")
            if "offset" in parameters and not _bounded_decimal(parameters["offset"], 0, 10**7):
                raise ValueError("dotnet output offset is invalid")
            if "limit" in parameters and not _bounded_decimal(parameters["limit"], 1, 200):
                raise ValueError("dotnet result limit is invalid")
            if "max_chars" in parameters and not _bounded_decimal(
                parameters["max_chars"], 1, 16 * 1024
            ):
                raise ValueError("dotnet character limit is invalid")
            has_rid = "method_rid" in parameters
            if has_rid and not _bounded_decimal(parameters["method_rid"], 1, 0xFFFFFF):
                raise ValueError("dotnet method RID is invalid")
            if (parameters["operation"] == "decompile") != has_rid:
                raise ValueError("dotnet method RID is required only for decompile")
        if self.tool is ConfinedAnalysisTool.LOCAL_COMMAND:
            if len(self.arguments) != 1 or self.arguments[0] not in {
                "file",
                "strings",
                "xxd",
                "sha256sum",
                "readelf",
                "objdump",
                "monodis",
            }:
                raise ValueError("local command identifier is not allowlisted")
            command_id = self.arguments[0]
            parameters = self.analysis_parameters
            if command_id == "file":
                if set(parameters) != {"mime_type"} or parameters["mime_type"] not in {
                    "true",
                    "false",
                }:
                    raise ValueError("file command parameters are invalid")
            elif command_id == "strings":
                if set(parameters) != {"min_length", "max_strings"}:
                    raise ValueError("strings command parameters are invalid")
                if not _bounded_decimal(parameters["min_length"], 1, 256):
                    raise ValueError("strings minimum length is invalid")
                if not _bounded_decimal(parameters["max_strings"], 1, 10_000):
                    raise ValueError("strings output limit is invalid")
            elif command_id == "readelf":
                if set(parameters) != {"mode"} or parameters["mode"] not in {
                    "header",
                    "program_headers",
                    "sections",
                    "dynamic",
                    "symbols",
                }:
                    raise ValueError("readelf command parameters are invalid")
            elif command_id == "xxd":
                if set(parameters) != {"offset", "length"}:
                    raise ValueError("xxd requires a bounded byte range")
                if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                    raise ValueError("xxd range offset is invalid")
                if not _bounded_decimal(parameters["length"], 1, LOCAL_COMMAND_MAX_XXD_RANGE_BYTES):
                    raise ValueError("xxd range length is invalid")
                if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                    raise ValueError("xxd range exceeds the address limit")
            elif command_id == "sha256sum":
                if parameters and set(parameters) != {"offset", "length"}:
                    raise ValueError("sha256sum range options must be paired")
                if parameters:
                    if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                        raise ValueError("sha256sum range offset is invalid")
                    if not _bounded_decimal(parameters["length"], 1, LOCAL_COMMAND_MAX_RANGE_BYTES):
                        raise ValueError("sha256sum range length is invalid")
                    if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                        raise ValueError("sha256sum range exceeds the address limit")
            elif command_id == "monodis":
                if parameters.get("operation") not in {
                    "userstrings",
                    "typedef",
                    "methods",
                    "cil",
                }:
                    raise ValueError("monodis operation is invalid")
                allowed_monodis = {"operation", "method_name", "method_rid", "offset", "length"}
                if set(parameters) - allowed_monodis:
                    raise ValueError("monodis command parameters are invalid")
                has_name = "method_name" in parameters
                has_rid = "method_rid" in parameters
                method_name = parameters.get("method_name")
                if has_name and (
                    not isinstance(method_name, str)
                    or re.fullmatch(r"[A-Za-z_$<>][A-Za-z0-9_.$<>+`-]{0,127}", method_name) is None
                ):
                    raise ValueError("monodis method name is invalid")
                if has_rid and not _bounded_decimal(parameters["method_rid"], 1, 0xFFFFFF):
                    raise ValueError("monodis method RID is invalid")
                if has_name and has_rid:
                    raise ValueError("monodis method selector must be unique")
                has_offset = "offset" in parameters
                has_length = "length" in parameters
                if has_offset != has_length:
                    raise ValueError("monodis output range options must be paired")
                if parameters["operation"] == "cil":
                    if has_name == has_rid:
                        raise ValueError("monodis CIL requires a method name or RID")
                elif has_name or has_rid:
                    raise ValueError("monodis selectors are allowed only for CIL")
                if has_offset:
                    if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                        raise ValueError("monodis output offset is invalid")
                    if not _bounded_decimal(
                        parameters["length"], 1, LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES
                    ):
                        raise ValueError("monodis output range length is invalid")
                    if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                        raise ValueError("monodis output range exceeds the address limit")
            else:
                if parameters.get("mode") not in {
                    "file_header",
                    "sections",
                    "private_headers",
                    "disassemble",
                }:
                    raise ValueError("objdump command mode is invalid")
                if parameters["mode"] == "disassemble":
                    if set(parameters) != {"mode", "offset", "length"}:
                        raise ValueError("objdump disassembly requires a bounded range")
                    if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                        raise ValueError("objdump range offset is invalid")
                    if not _bounded_decimal(parameters["length"], 1, LOCAL_COMMAND_MAX_RANGE_BYTES):
                        raise ValueError("objdump range length is invalid")
                    if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                        raise ValueError("objdump range exceeds the address limit")
                elif set(parameters) != {"mode"}:
                    raise ValueError("objdump command parameters are invalid")
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
        if self.tool in {
            ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
            ConfinedAnalysisTool.SURICATA_REPLAY,
        }:
            if self.artifact_kind is not ConfinedAnalysisArtifactKind.CAPTURE:
                raise ValueError("Suricata tools require a quarantined capture artifact")
            if self.arguments:
                raise ValueError("Suricata tools do not accept command arguments")
            present = set(self.analysis_parameters)
            candidate_fields = {"candidate_id", "candidate_version", "rule_source"}
            if present and present != candidate_fields:
                raise ValueError("Suricata candidates require an id, version, and rule source")
            if present:
                candidate_id = self.analysis_parameters["candidate_id"]
                candidate_version = self.analysis_parameters["candidate_version"]
                rule_source = self.analysis_parameters["rule_source"]
                if (
                    not isinstance(candidate_id, str)
                    or not isinstance(candidate_version, str)
                    or not isinstance(rule_source, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", candidate_id)
                    or not candidate_version.isascii()
                    or not candidate_version.isdecimal()
                    or not 1 <= int(candidate_version) <= 100_000
                ):
                    raise ValueError("Suricata candidate identity is invalid")
                if not rule_source.strip() or len(rule_source.encode("utf-8")) > 65_536:
                    raise ValueError("Suricata candidate source exceeds its bounded contract")


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


def confined_analysis_operation_cache_identity(
    execution: ConfinedAnalysisExecutionPort | None,
    *,
    result_schema_version: str = CONFINED_ANALYSIS_RESULT_SCHEMA,
) -> Mapping[str, Any]:
    """Version a tool-result reuse key with its result contract and worker proof."""
    attestation = execution.attestation if execution is not None else None
    worker_attestation = None
    if attestation is not None:
        worker_attestation = {
            "reachable": attestation.reachable,
            "confined": attestation.confined,
            "dependencies_ready": attestation.dependencies_ready,
            "isolation_profile": attestation.isolation_profile,
            "versions": dict(sorted(attestation.versions.items())),
            "families": {
                name: {
                    "available": status.available,
                    "version": status.version,
                }
                for name, status in sorted(attestation.families.items())
            },
        }
    return {
        "result_schema_version": result_schema_version,
        "worker_attestation": worker_attestation,
    }


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
        "schema": "confined-analysis-operation.v2",
        "result_schema_version": CONFINED_ANALYSIS_RESULT_SCHEMA,
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


def _bounded_decimal(value: str, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and minimum <= int(value) <= maximum
    )
