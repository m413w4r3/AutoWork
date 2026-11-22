"""Confined execution of agent-authored analysis scripts (M24-05 H3).

A script is untrusted data. It runs only in the attested analysis worker, with
one canonical sample/capture input mounted read-only, a writable quota-bound
scratch directory, no network, a scrubbed environment, and a closed
``python3``-only interpreter. The result is an observation of one measured
execution, never an investigation finding.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.confined_analysis import (
    CONFINED_SCRIPT_CPU_SECONDS_RANGE,
    CONFINED_SCRIPT_FILE_SIZE_BYTES_RANGE,
    CONFINED_SCRIPT_INTERPRETERS,
    CONFINED_SCRIPT_JOB_GRACE_SECONDS,
    CONFINED_SCRIPT_MAX_PROCESSES_RANGE,
    CONFINED_SCRIPT_MEMORY_BYTES_RANGE,
    CONFINED_SCRIPT_SCRATCH_MAX_BYTES_RANGE,
    CONFINED_SCRIPT_SCRATCH_MAX_FILES_RANGE,
    CONFINED_SCRIPT_SOURCE_BUCKET,
    CONFINED_SCRIPT_SOURCE_MIME,
    CONFINED_SCRIPT_STREAM_MAX_BYTES_RANGE,
    CONFINED_SCRIPT_WALL_SECONDS_RANGE,
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
    ConfinedScriptSpec,
)
from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    ToolInvocationStatus,
)

CONFINED_SCRIPT_TOOL = "analysis.run_script"
CONFINED_SCRIPT_CAPABILITY = "analysis.exec"
CONFINED_SCRIPT_SCHEMA_VERSION = "1"
_NETWORK_CAPTURE_BUCKET = "network-captures-quarantine"
_VT_SANDBOX_PCAP_SOURCE_VERSION = "vt-sandbox-pcap.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SCRIPT_PREVIEW_CHARS = 16_000
_WORKER_STATUS_MAP: Mapping[str, ToolInvocationStatus] = {
    "SUCCEEDED": ToolInvocationStatus.SUCCEEDED,
    "NON_ZERO_EXIT": ToolInvocationStatus.FAILED,
    "TIMED_OUT": ToolInvocationStatus.PARTIAL,
    "OUTPUT_LIMIT": ToolInvocationStatus.PARTIAL,
    "DIRECTORY_LIMIT": ToolInvocationStatus.PARTIAL,
    "PROCESS_LIMIT": ToolInvocationStatus.PARTIAL,
    "UNAVAILABLE": ToolInvocationStatus.UNAVAILABLE,
    "POLICY_DENIED": ToolInvocationStatus.POLICY_DENIED,
    "FAILED": ToolInvocationStatus.FAILED,
}
_CONFINEMENT_REASONS = frozenset(
    {
        "analysis_worker_isolation_unattested",
        "script_confinement_unavailable",
        "script_sandbox_failed",
    }
)
_LIMIT_ATTESTATION_NAMES: Mapping[str, str] = {
    "wall_seconds": "analysis_script_wall_timeout_seconds",
    "cpu_seconds": "analysis_script_cpu_seconds",
    "memory_bytes": "analysis_script_memory_bytes",
    "file_size_bytes": "analysis_script_file_size_bytes",
    "scratch_max_files": "analysis_script_scratch_max_files",
    "scratch_max_bytes": "analysis_script_scratch_max_bytes",
    "stdout_max_bytes": "analysis_script_stdout_max_bytes",
    "stderr_max_bytes": "analysis_script_stderr_max_bytes",
    "max_processes": "analysis_script_max_processes",
}
_SETTING_MAXIMA: Mapping[str, str] = _LIMIT_ATTESTATION_NAMES


class ConfinedScriptInterpreter(StrEnum):
    PYTHON3 = "python3"


class ScriptArtifactArchive(Protocol):
    """Content-addressed archive of the script source (canonical blob path)."""

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID: ...


@dataclass(frozen=True, slots=True)
class ConfinedScriptLimits:
    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 256 * 1024 * 1024
    file_size_bytes: int = 16 * 1024 * 1024
    scratch_max_files: int = 512
    scratch_max_bytes: int = 64 * 1024 * 1024
    stdout_max_bytes: int = 1024 * 1024
    stderr_max_bytes: int = 1024 * 1024
    max_processes: int = 64

    def __post_init__(self) -> None:
        _bounded_float(self.wall_seconds, *CONFINED_SCRIPT_WALL_SECONDS_RANGE)
        _bounded_int(self.cpu_seconds, *CONFINED_SCRIPT_CPU_SECONDS_RANGE)
        _bounded_int(self.memory_bytes, *CONFINED_SCRIPT_MEMORY_BYTES_RANGE)
        _bounded_int(self.file_size_bytes, *CONFINED_SCRIPT_FILE_SIZE_BYTES_RANGE)
        _bounded_int(self.scratch_max_files, *CONFINED_SCRIPT_SCRATCH_MAX_FILES_RANGE)
        _bounded_int(self.scratch_max_bytes, *CONFINED_SCRIPT_SCRATCH_MAX_BYTES_RANGE)
        _bounded_int(self.stdout_max_bytes, *CONFINED_SCRIPT_STREAM_MAX_BYTES_RANGE)
        _bounded_int(self.stderr_max_bytes, *CONFINED_SCRIPT_STREAM_MAX_BYTES_RANGE)
        _bounded_int(self.max_processes, *CONFINED_SCRIPT_MAX_PROCESSES_RANGE)

    @classmethod
    def defaults(cls, settings: Settings) -> ConfinedScriptLimits:
        """Safe defaults, never above the worker-attested settings maxima."""
        return cls(
            wall_seconds=min(60.0, settings.analysis_script_wall_timeout_seconds),
            cpu_seconds=min(30, settings.analysis_script_cpu_seconds),
            memory_bytes=min(256 * 1024 * 1024, settings.analysis_script_memory_bytes),
            file_size_bytes=min(16 * 1024 * 1024, settings.analysis_script_file_size_bytes),
            scratch_max_files=min(512, settings.analysis_script_scratch_max_files),
            scratch_max_bytes=min(64 * 1024 * 1024, settings.analysis_script_scratch_max_bytes),
            stdout_max_bytes=min(1024 * 1024, settings.analysis_script_stdout_max_bytes),
            stderr_max_bytes=min(1024 * 1024, settings.analysis_script_stderr_max_bytes),
            max_processes=min(64, settings.analysis_script_max_processes),
        )

    def as_mapping(self) -> Mapping[str, int | float]:
        return {
            "wall_seconds": self.wall_seconds,
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "file_size_bytes": self.file_size_bytes,
            "scratch_max_files": self.scratch_max_files,
            "scratch_max_bytes": self.scratch_max_bytes,
            "stdout_max_bytes": self.stdout_max_bytes,
            "stderr_max_bytes": self.stderr_max_bytes,
            "max_processes": self.max_processes,
        }


@dataclass(frozen=True, slots=True)
class ConfinedScriptRequest:
    investigation_id: UUID
    script: bytes
    interpreter: ConfinedScriptInterpreter | str = ConfinedScriptInterpreter.PYTHON3
    limits: ConfinedScriptLimits | None = None
    sample_sha256: str | None = None
    capture_artifact_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID):
            raise ValueError("script investigation identity must be a UUID")
        if not isinstance(self.script, bytes) or not self.script:
            raise ValueError("script source must be non-empty bytes")
        if not isinstance(self.interpreter, ConfinedScriptInterpreter):
            try:
                object.__setattr__(self, "interpreter", ConfinedScriptInterpreter(self.interpreter))
            except (TypeError, ValueError) as exc:
                raise ValueError("script interpreter is not allowlisted") from exc
        if self.interpreter_value() not in CONFINED_SCRIPT_INTERPRETERS:
            raise ValueError("script interpreter is not allowlisted")
        if self.limits is not None and not isinstance(self.limits, ConfinedScriptLimits):
            raise ValueError("script limits must use the closed limits contract")
        has_sample = self.sample_sha256 is not None
        has_capture = self.capture_artifact_id is not None
        if has_sample == has_capture:
            raise ValueError("script execution requires exactly one canonical input")
        if has_sample and (
            not isinstance(self.sample_sha256, str) or _SHA256.fullmatch(self.sample_sha256) is None
        ):
            raise ValueError("sample SHA-256 is invalid")
        if has_capture and not isinstance(self.capture_artifact_id, UUID):
            raise ValueError("capture artifact identity must be a UUID")

    def interpreter_value(self) -> str:
        interpreter = self.interpreter
        return (
            interpreter.value if isinstance(interpreter, ConfinedScriptInterpreter) else interpreter
        )


@dataclass(frozen=True, slots=True)
class ConfinedScriptResult:
    status: ToolInvocationStatus
    reason: str | None = None
    operation_key: str | None = None
    executed: bool = False
    input_kind: str | None = None
    input_sha256: str | None = None
    input_size_bytes: int | None = None
    script_sha256: str | None = None
    script_blob_id: UUID | None = None
    script_size_bytes: int | None = None
    interpreter: str = ConfinedScriptInterpreter.PYTHON3.value
    interpreter_version: str | None = None
    sandbox: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    resource_usage: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    outputs_blob_id: UUID | None = None
    outputs_sha256: str | None = None
    outputs_size_bytes: int | None = None
    outputs_truncated: bool = False
    output_files: tuple[Mapping[str, Any], ...] = ()
    worker_profile: str | None = None


def script_capability_available(execution: ConfinedAnalysisExecutionPort | None) -> bool:
    if execution is None or not execution.available:
        return False
    family = execution.family_statuses.get("script")
    return bool(family is not None and family.available)


def script_unavailable_reason(execution: ConfinedAnalysisExecutionPort | None) -> str:
    if execution is None:
        return "analysis_worker_disabled_by_configuration"
    if not execution.available:
        return "confinement_unavailable"
    family = execution.family_statuses.get("script")
    reason = family.available_reason if family is not None else None
    if reason in _CONFINEMENT_REASONS:
        return "confinement_unavailable"
    return reason or "analysis_script_unavailable"


class ConfinedScriptService:
    """Archive one script source, dispatch it to the attested worker, map the proof."""

    def __init__(
        self,
        *,
        execution: ConfinedAnalysisExecutionPort | None,
        uow_factory: Callable[[], Any],
        archive: ScriptArtifactArchive | None,
        settings: Settings,
    ) -> None:
        self._execution = execution
        self._uow_factory = uow_factory
        self._archive = archive
        self._settings = settings

    async def run(self, request: ConfinedScriptRequest) -> ConfinedScriptResult:
        limits = request.limits or ConfinedScriptLimits.defaults(self._settings)
        violation = _limit_violation(
            limits,
            {name: getattr(self._settings, name) for name in _SETTING_MAXIMA.values()},
        )
        if violation is not None:
            return _failed(
                ToolInvocationStatus.POLICY_DENIED,
                "script_limit_exceeded",
                limits=limits,
            )
        if len(request.script) > self._settings.analysis_script_max_source_bytes:
            return _failed(
                ToolInvocationStatus.POLICY_DENIED,
                "script_source_too_large",
                limits=limits,
            )
        if self._execution is None:
            return _failed(
                ToolInvocationStatus.UNAVAILABLE,
                "analysis_worker_disabled_by_configuration",
                limits=limits,
            )
        if not self._execution.available:
            return _failed(
                ToolInvocationStatus.UNAVAILABLE, "confinement_unavailable", limits=limits
            )
        family = self._execution.family_statuses.get("script")
        if family is None or not family.available:
            return _failed(
                ToolInvocationStatus.UNAVAILABLE,
                script_unavailable_reason(self._execution),
                limits=limits,
            )
        attested = _limit_violation(limits, self._execution.attestation.resource_limits)
        if attested is not None:
            return _failed(
                ToolInvocationStatus.POLICY_DENIED,
                "analysis_script_limits_unattested"
                if attested == "analysis_script_limits_unattested"
                else "analysis_script_limit_exceeded",
                limits=limits,
            )
        attested_source_max = self._execution.attestation.resource_limits.get(
            "analysis_script_max_source_bytes"
        )
        if not isinstance(attested_source_max, int):
            return _failed(
                ToolInvocationStatus.POLICY_DENIED,
                "analysis_script_limits_unattested",
                limits=limits,
            )
        if len(request.script) > attested_source_max:
            return _failed(
                ToolInvocationStatus.POLICY_DENIED,
                "script_source_too_large",
                limits=limits,
            )
        if self._archive is None:
            return _failed(
                ToolInvocationStatus.UNAVAILABLE, "script_archive_unavailable", limits=limits
            )
        identity = await self._resolve_input(request)
        if isinstance(identity, str):
            return _failed(ToolInvocationStatus.POLICY_DENIED, identity, limits=limits)
        input_kind, input_sha256, input_size, association_id, sample_id, artifact_id = identity
        try:
            script_blob_id = await self._archive.put_bytes(
                request.script,
                bucket=CONFINED_SCRIPT_SOURCE_BUCKET,
                mime_type=CONFINED_SCRIPT_SOURCE_MIME,
            )
        except Exception:
            return _failed(
                ToolInvocationStatus.UNAVAILABLE, "script_archive_unavailable", limits=limits
            )
        script_sha256 = hashlib.sha256(request.script).hexdigest()
        spec = ConfinedScriptSpec(
            script_blob_id=script_blob_id,
            script_sha256=script_sha256,
            interpreter=request.interpreter_value(),
            wall_seconds=limits.wall_seconds,
            cpu_limit_seconds=limits.cpu_seconds,
            memory_limit_bytes=limits.memory_bytes,
            file_size_limit_bytes=limits.file_size_bytes,
            scratch_max_files=limits.scratch_max_files,
            scratch_max_bytes=limits.scratch_max_bytes,
            stdout_max_bytes=limits.stdout_max_bytes,
            stderr_max_bytes=limits.stderr_max_bytes,
            max_processes=limits.max_processes,
        )
        engine_request = ConfinedAnalysisRequest(
            investigation_id=request.investigation_id,
            artifact_id=artifact_id,
            sha256=input_sha256,
            tool=ConfinedAnalysisTool.SCRIPT,
            artifact_kind=(
                ConfinedAnalysisArtifactKind.SAMPLE
                if input_kind == "sample"
                else ConfinedAnalysisArtifactKind.CAPTURE
            ),
            association_id=association_id,
            sample_id=sample_id,
            timeout_seconds=limits.wall_seconds + CONFINED_SCRIPT_JOB_GRACE_SECONDS,
            max_output_bytes=self._settings.analysis_worker_result_max_bytes,
            script=spec,
        )
        result = await self._execution.execute(engine_request)
        base: dict[str, Any] = {
            "operation_key": result.operation_key,
            "input_kind": input_kind,
            "input_sha256": input_sha256,
            "input_size_bytes": input_size,
            "script_sha256": script_sha256,
            "script_blob_id": script_blob_id,
            "script_size_bytes": len(request.script),
            "interpreter": request.interpreter_value(),
            "limits": limits.as_mapping(),
        }
        if result.status is ToolInvocationStatus.UNAVAILABLE:
            reason = result.available_reason or "analysis_worker_unavailable"
            if reason in _CONFINEMENT_REASONS:
                reason = "confinement_unavailable"
            return ConfinedScriptResult(
                status=ToolInvocationStatus.UNAVAILABLE, reason=reason, **base
            )
        if result.status is ToolInvocationStatus.FAILED:
            return ConfinedScriptResult(
                status=ToolInvocationStatus.FAILED,
                reason=result.available_reason or "analysis_worker_job_failed",
                **base,
            )
        if result.status is ToolInvocationStatus.POLICY_DENIED:
            return ConfinedScriptResult(
                status=ToolInvocationStatus.POLICY_DENIED,
                reason=result.available_reason or "analysis_policy_denied",
                **base,
            )
        payload = result.payload
        result_data = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(result_data, Mapping):
            return ConfinedScriptResult(
                status=ToolInvocationStatus.FAILED,
                reason="script_result_invalid",
                **base,
            )
        if (
            result_data.get("script_sha256") != script_sha256
            or result_data.get("input_sha256") != input_sha256
            or result_data.get("interpreter") != request.interpreter_value()
        ):
            return ConfinedScriptResult(
                status=ToolInvocationStatus.FAILED,
                reason="script_result_identity_mismatch",
                **base,
            )
        worker_status = str(result_data.get("status", "FAILED")).upper()
        status = _WORKER_STATUS_MAP.get(worker_status, ToolInvocationStatus.FAILED)
        worker_reason = _string_or_none(result_data.get("detail_code")) or _string_or_none(
            result_data.get("available_reason")
        )
        if status is ToolInvocationStatus.UNAVAILABLE and worker_reason in _CONFINEMENT_REASONS:
            worker_reason = "confinement_unavailable"
        outputs_blob_id = _uuid_or_none(result_data.get("outputs_blob_id"))
        output_files = result_data.get("output_files")
        return ConfinedScriptResult(
            status=status,
            reason=worker_reason,
            executed=status is not ToolInvocationStatus.UNAVAILABLE,
            interpreter_version=_string_or_none(result_data.get("interpreter_version")),
            sandbox=_string_or_none(result_data.get("sandbox")),
            exit_code=_int_or_none(result_data.get("exit_code")),
            duration_seconds=_float_or_none(result_data.get("duration_seconds")),
            stdout=_string(result_data.get("stdout")),
            stderr=_string(result_data.get("stderr")),
            stdout_truncated=result_data.get("stdout_truncated") is True,
            stderr_truncated=result_data.get("stderr_truncated") is True,
            stdout_bytes=_int_or_none(result_data.get("stdout_bytes")) or 0,
            stderr_bytes=_int_or_none(result_data.get("stderr_bytes")) or 0,
            resource_usage=_mapping(result_data.get("resource_usage")),
            outputs_blob_id=outputs_blob_id,
            outputs_sha256=_string_or_none(result_data.get("outputs_sha256")),
            outputs_size_bytes=_int_or_none(result_data.get("outputs_size_bytes")),
            outputs_truncated=result_data.get("outputs_truncated") is True,
            output_files=tuple(item for item in output_files if isinstance(item, Mapping))
            if isinstance(output_files, list)
            else (),
            worker_profile=_string_or_none(_mapping(result.payload).get("worker_profile")),
            **base,
        )

    async def _resolve_input(
        self, request: ConfinedScriptRequest
    ) -> tuple[str, str, int, UUID | None, UUID | None, UUID] | str:
        try:
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(request.investigation_id)
                if investigation is None:
                    return "script_input_not_associated"
                if request.sample_sha256 is not None:
                    associations = await uow.sample_associations.list_for_investigation(
                        request.investigation_id
                    )
                    association = next(
                        (
                            item
                            for item in associations
                            if item.sample_sha256 == request.sample_sha256
                        ),
                        None,
                    )
                    if association is None:
                        return "script_input_not_associated"
                    blob = await uow.blobs.get_by_address(
                        SAMPLES_QUARANTINE_BUCKET, request.sample_sha256
                    )
                    if blob is None:
                        return "sample_bytes_unavailable"
                    sample = await uow.samples.get_by_subject_and_blob(
                        investigation.subject_id, blob.id
                    )
                    if (
                        sample is None
                        or sample.blob_id != blob.id
                        or sample.state
                        not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                        or blob.descriptor.sha256 != request.sample_sha256
                    ):
                        return "sample_not_quarantined"
                    return (
                        "sample",
                        blob.descriptor.sha256,
                        blob.descriptor.size,
                        association.id,
                        sample.id,
                        blob.id,
                    )
                assert request.capture_artifact_id is not None
                blob = await uow.blobs.get(request.capture_artifact_id)
                if blob is None:
                    return "script_input_not_associated"
                # ADR-0011 H3: exactly one *canonical* capture of this
                # investigation, never an arbitrary blob the caller happens to
                # know the UUID of.  This mirrors the worker's own pre-dispatch
                # check (``workers/confined_analysis.py`` capture branch) so an
                # unassociated or non-capture blob is refused before a job is
                # queued.
                if blob.descriptor.logical_bucket != _NETWORK_CAPTURE_BUCKET:
                    return "script_input_not_associated"
                observations = await uow.evidence_observations.list_for_investigation(
                    request.investigation_id
                )
                if not any(
                    observation.blob_id == blob.id
                    and observation.sha256 == blob.descriptor.sha256
                    and observation.object_type == "network_capture"
                    and observation.object_id == blob.descriptor.sha256
                    and (
                        observation.source == "network.quarantine"
                        or (
                            observation.source == "VirusTotal"
                            and observation.source_version == _VT_SANDBOX_PCAP_SOURCE_VERSION
                        )
                    )
                    for observation in observations
                ):
                    return "script_input_not_associated"
                return (
                    "capture",
                    blob.descriptor.sha256,
                    blob.descriptor.size,
                    None,
                    None,
                    blob.id,
                )
        except Exception:
            return "script_input_unavailable"


def _limit_violation(limits: ConfinedScriptLimits, maxima: Mapping[str, int | float]) -> str | None:
    for request_name, maximum_name in _SETTING_MAXIMA.items():
        maximum = maxima.get(maximum_name)
        if maximum is None:
            return "analysis_script_limits_unattested"
        if float(getattr(limits, request_name)) > float(maximum):
            return "script_limit_exceeded"
    return None


def build_run_script_tool_definition(settings: Settings) -> ToolDefinition:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "title": "Run one confined agent-authored analysis script",
        "properties": {
            "script": {
                "type": "string",
                "minLength": 1,
                "maxLength": settings.analysis_script_max_source_bytes,
                "description": (
                    "Texte source Python. Le script est une donnée non fiable, "
                    "archivée par SHA-256 avant exécution."
                ),
            },
            "interpreter": {
                "type": "string",
                "enum": ["python3"],
                "description": "Interpréteur de l'allowlist fermée (python3 uniquement).",
            },
            "sample_sha256": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
                "pattern": _SHA256.pattern,
                "description": "Entrée canonique unique: échantillon de quarantaine associé.",
            },
            "capture_artifact_id": {
                "type": "string",
                "minLength": 36,
                "maxLength": 36,
                "pattern": _UUID.pattern,
                "description": (
                    "Entrée canonique unique: artefact PCAP/capture déjà associé à l'enquête."
                ),
            },
            "limits": {
                "type": "object",
                "description": (
                    "Bornes demandées; toujours inférieures ou égales aux maxima "
                    "attestés du worker."
                ),
                "properties": {
                    "wall_seconds": {
                        "type": "number",
                        "minimum": 60,
                        "maximum": 300,
                    },
                    "cpu_seconds": {
                        "type": "integer",
                        "minimum": 30,
                        "maximum": 120,
                    },
                    "memory_bytes": {
                        "type": "integer",
                        "minimum": 256 * 1024 * 1024,
                        "maximum": 1024 * 1024 * 1024,
                    },
                    "file_size_bytes": {
                        "type": "integer",
                        "minimum": 1024 * 1024,
                        "maximum": 16 * 1024 * 1024,
                    },
                    "scratch_max_files": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 512,
                    },
                    "scratch_max_bytes": {
                        "type": "integer",
                        "minimum": 1024 * 1024,
                        "maximum": 64 * 1024 * 1024,
                    },
                    "stdout_max_bytes": {
                        "type": "integer",
                        "minimum": 1024,
                        "maximum": 1024 * 1024,
                    },
                    "stderr_max_bytes": {
                        "type": "integer",
                        "minimum": 1024,
                        "maximum": 1024 * 1024,
                    },
                    "max_processes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 128,
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["script", "interpreter"],
        "additionalProperties": False,
    }
    return ToolDefinition(
        name=CONFINED_SCRIPT_TOOL,
        schema_version=CONFINED_SCRIPT_SCHEMA_VERSION,
        input_schema=schema,
        effect=ToolEffect.LOCAL_COMPUTE,
        cost_model={BudgetDimension.TOOL_SECONDS: _reserved_seconds(settings)},
        confidentiality=ToolConfidentiality.RESTRICTED,
        limitations=(
            "Le script est une donnée non fiable exécutée uniquement dans le worker "
            "d'analyse attesté; le réseau est refusé par seccomp et l'entrée canonique "
            "est montée en lecture seule.",
            "Aucun argument libre ne parvient à l'interpréteur: python3 et un unique "
            "fichier source sont construits par le worker.",
            "La sortie mesure une exécution; c'est une observation, jamais un finding.",
        ),
        capability_key=CONFINED_SCRIPT_CAPABILITY,
    )


def _reserved_seconds(settings: Settings) -> int:
    wall = min(
        settings.analysis_script_wall_timeout_seconds,
        max(60.0, settings.analysis_worker_job_timeout_seconds - CONFINED_SCRIPT_JOB_GRACE_SECONDS),
    )
    return max(1, math.ceil(wall))


class RunScriptToolAdapter:
    """Broker-facing typed adapter; records one measured execution observation."""

    def __init__(
        self,
        *,
        service: ConfinedScriptService,
        recorder: EvidenceRecorder,
        settings: Settings,
        reserved_seconds: int,
    ) -> None:
        self.service = service
        self.recorder = recorder
        self.settings = settings
        self.reserved_seconds = reserved_seconds

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        script = arguments.get("script")
        interpreter = arguments.get("interpreter")
        if not isinstance(script, str) or not script:
            return self._denied(started, "script_source_required")
        if interpreter != ConfinedScriptInterpreter.PYTHON3.value:
            return self._denied(started, "script_interpreter_not_allowed")
        try:
            script_bytes = script.encode("utf-8")
        except UnicodeEncodeError:
            return self._denied(started, "script_source_invalid")
        sample_sha256 = arguments.get("sample_sha256")
        capture_artifact_id = arguments.get("capture_artifact_id")
        if (sample_sha256 is None) == (capture_artifact_id is None):
            return self._denied(started, "script_input_selector_invalid")
        try:
            capture_uuid = (
                UUID(str(capture_artifact_id)) if capture_artifact_id is not None else None
            )
        except (TypeError, ValueError):
            return self._denied(started, "script_input_selector_invalid")
        limits, limit_error = _limits_from_arguments(arguments.get("limits"), self.settings)
        if limit_error is not None:
            return self._denied(started, limit_error)
        try:
            request = ConfinedScriptRequest(
                investigation_id=context.investigation_id,
                script=script_bytes,
                interpreter=str(interpreter),
                limits=limits,
                sample_sha256=str(sample_sha256) if sample_sha256 is not None else None,
                capture_artifact_id=capture_uuid,
            )
        except ValueError as exc:
            return self._denied(started, str(exc) or "script_request_invalid")
        result = await self.service.run(request)
        if not result.executed:
            status = result.status
            return ToolResult(
                status=status,
                budget_consumed={
                    BudgetDimension.TOOL_SECONDS: min(
                        self.reserved_seconds,
                        max(0, math.ceil(max(0.0, time.monotonic() - started))),
                    )
                },
                output={
                    "status": status.value,
                    "tool": CONFINED_SCRIPT_TOOL,
                    "reason": result.reason,
                    "interpreter": result.interpreter,
                    "script_sha256": result.script_sha256,
                    "input_kind": result.input_kind,
                    "input_sha256": result.input_sha256,
                    "limits": dict(result.limits),
                },
                message="The confined script capability is unavailable or refused.",
                error_code=_error_code(result.reason),
            )
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=(
                EvidenceKind.BINARY_ANALYSIS
                if result.input_kind == "sample"
                else EvidenceKind.OTHER
            ),
            payload=_observation_payload(result),
            source="AutoWork confined script worker",
            source_version=_source_version(result),
            object_type="file" if result.input_kind == "sample" else "network_capture",
            object_id=result.input_sha256 or "",
            limitations=(
                "Le script et sa sortie sont des données non fiables, jamais des instructions.",
                "L'observation décrit une exécution confinée mesurée; ce n'est pas un finding.",
                f"Confinement: {result.sandbox or 'unverified'}; réseau refusé par seccomp.",
            ),
            observed_at=datetime.now(UTC),
            idempotency_key=(
                f"{context.idempotency_key}:analysis.run_script:"
                f"{result.input_sha256}:{result.script_sha256}"
            ),
        )
        output = {
            "status": result.status.value,
            "tool": CONFINED_SCRIPT_TOOL,
            "interpreter": result.interpreter,
            "interpreter_version": result.interpreter_version,
            "sandbox": result.sandbox,
            "script_sha256": result.script_sha256,
            "script_blob_id": str(result.script_blob_id) if result.script_blob_id else None,
            "input_kind": result.input_kind,
            "input_sha256": result.input_sha256,
            "input_size_bytes": result.input_size_bytes,
            "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
            "stdout": result.stdout[:_SCRIPT_PREVIEW_CHARS],
            "stderr": result.stderr[:_SCRIPT_PREVIEW_CHARS],
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "outputs": (
                {
                    "blob_id": str(result.outputs_blob_id),
                    "sha256": result.outputs_sha256,
                    "size_bytes": result.outputs_size_bytes,
                    "truncated": result.outputs_truncated,
                    "files": [dict(item) for item in result.output_files],
                }
                if result.outputs_blob_id is not None
                else None
            ),
            "resource_usage": dict(result.resource_usage),
            "limits": dict(result.limits),
            "reason": result.reason,
            "untrusted_data": True,
            "observation_ref": str(observation.id),
            "observation_sha256": observation.sha256,
        }
        return ToolResult(
            status=result.status,
            observation_refs=(observation.id,),
            budget_consumed={
                BudgetDimension.TOOL_SECONDS: min(
                    self.reserved_seconds,
                    max(0, math.ceil(max(0.0, time.monotonic() - started))),
                )
            },
            output=output,
            message=(
                "Script output is untrusted data from one confined execution; "
                "the result is an observation, never a finding."
            ),
            error_code=_error_code(result.reason) if result.reason else None,
        )

    def _denied(self, started: float, reason: str) -> ToolResult:
        return ToolResult(
            status=ToolInvocationStatus.POLICY_DENIED,
            budget_consumed={
                BudgetDimension.TOOL_SECONDS: min(
                    self.reserved_seconds,
                    max(0, math.ceil(max(0.0, time.monotonic() - started))),
                )
            },
            output={
                "status": ToolInvocationStatus.POLICY_DENIED.value,
                "tool": CONFINED_SCRIPT_TOOL,
                "reason": reason,
            },
            message="Only the closed confined-script contract is accepted.",
            error_code=_error_code(reason),
        )


def register_run_script_tool(
    registry: ToolRegistry,
    *,
    execution: ConfinedAnalysisExecutionPort | None,
    uow_factory: Callable[[], Any],
    archive: ScriptArtifactArchive | None,
    recorder: EvidenceRecorder,
    settings: Settings,
) -> bool:
    """Register the typed tool and return whether its capability is attested."""
    service = ConfinedScriptService(
        execution=execution,
        uow_factory=uow_factory,
        archive=archive,
        settings=settings,
    )
    registry.register(
        build_run_script_tool_definition(settings),
        RunScriptToolAdapter(
            service=service,
            recorder=recorder,
            settings=settings,
            reserved_seconds=_reserved_seconds(settings),
        ),
    )
    return script_capability_available(execution)


def _limits_from_arguments(
    value: Any, settings: Settings
) -> tuple[ConfinedScriptLimits | None, str | None]:
    if value is None:
        return ConfinedScriptLimits.defaults(settings), None
    if not isinstance(value, Mapping):
        return None, "script_limits_invalid"
    allowed = set(_SETTING_MAXIMA)
    if set(value) - allowed:
        return None, "script_limits_invalid"
    defaults = ConfinedScriptLimits.defaults(settings)
    merged: dict[str, Any] = {}
    for name in allowed:
        raw = value.get(name, getattr(defaults, name))
        if name == "wall_seconds":
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                return None, "script_limits_invalid"
            merged[name] = float(raw)
        else:
            if type(raw) is not int:
                return None, "script_limits_invalid"
            merged[name] = raw
    try:
        return ConfinedScriptLimits(**merged), None
    except ValueError:
        return None, "script_limits_invalid"


def _observation_payload(result: ConfinedScriptResult) -> Mapping[str, Any]:
    params = {
        "input_sha256": result.input_sha256,
        "script_sha256": result.script_sha256,
        "interpreter": result.interpreter,
        "limits": dict(result.limits),
    }
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": "confined-script-execution.v1",
        "method": "confined_script",
        "tool": CONFINED_SCRIPT_TOOL,
        "tool_version": _source_version(result),
        "operation_key": result.operation_key,
        "params": params,
        "params_sha256": hashlib.sha256(encoded).hexdigest(),
        "sandbox": result.sandbox,
        "worker_profile": result.worker_profile or "unknown",
        "input_kind": result.input_kind,
        "input_sha256": result.input_sha256,
        "input_size_bytes": result.input_size_bytes,
        "script_blob_id": str(result.script_blob_id) if result.script_blob_id else None,
        "script_sha256": result.script_sha256,
        "script_size_bytes": result.script_size_bytes,
        "interpreter": result.interpreter,
        "interpreter_version": result.interpreter_version,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "resource_usage": dict(result.resource_usage),
        "limits": dict(result.limits),
        "stdout": result.stdout[:_SCRIPT_PREVIEW_CHARS],
        "stderr": result.stderr[:_SCRIPT_PREVIEW_CHARS],
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "outputs": {
            "blob_id": str(result.outputs_blob_id) if result.outputs_blob_id else None,
            "sha256": result.outputs_sha256,
            "size_bytes": result.outputs_size_bytes,
            "truncated": result.outputs_truncated,
            "files": [dict(item) for item in result.output_files],
        },
        "untrusted_data": True,
    }


def _source_version(result: ConfinedScriptResult) -> str:
    version = f"{result.interpreter} {result.interpreter_version or 'unverified'}"
    return sanitize_evidence_text(version, max_chars=128)


def _error_code(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized = "_".join(reason.strip().lower().split())
    if (
        1 <= len(normalized) <= 96
        and normalized[0] in "abcdefghijklmnopqrstuvwxyz"
        and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for character in normalized)
    ):
        return normalized
    return None


def _failed(
    status: ToolInvocationStatus,
    reason: str,
    *,
    limits: ConfinedScriptLimits,
) -> ConfinedScriptResult:
    return ConfinedScriptResult(
        status=status,
        reason=reason,
        interpreter=ConfinedScriptInterpreter.PYTHON3.value,
        limits=limits.as_mapping(),
    )


def _bounded_int(value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("bounded script limit is outside its closed range")


def _bounded_float(value: float, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError("bounded script limit is outside its closed range")


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _uuid_or_none(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "CONFINED_SCRIPT_CAPABILITY",
    "CONFINED_SCRIPT_TOOL",
    "ConfinedScriptInterpreter",
    "ConfinedScriptLimits",
    "ConfinedScriptRequest",
    "ConfinedScriptResult",
    "ConfinedScriptService",
    "RunScriptToolAdapter",
    "build_run_script_tool_definition",
    "register_run_script_tool",
    "script_capability_available",
    "script_unavailable_reason",
]
