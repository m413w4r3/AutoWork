from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.confined_analysis import (
    CONFINED_ANALYSIS_RESULT_SCHEMA,
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisTool,
)
from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.rule_workbench import YaraSampleInput
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.code_features import CodeFeatureSet, CodeFeatureStatus, build_code_ngrams
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.rule_workbench import YaraExecutionStatus
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)
from cti_app.infrastructure.capa import CapaRunner, parse_capa_output, ruleset_manifest
from cti_app.infrastructure.floss_adapter import _parse_floss_payload
from cti_app.infrastructure.smda import SmdaAdapter
from cti_app.infrastructure.static_analysis import StaticFeatureExtractor, build_packing_signals
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner, YaraRunnerLimits

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"(?:TShark|Capinfos) \(Wireshark\) ([0-9]+\.[0-9]+\.[0-9]+)")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_FOLLOW_STREAM = re.compile(r"^(?:follow,tcp|follow,udp),raw,[0-9]{1,8}$")
_ANALYSIS_RESULT_BUCKET = "confined-analysis-results"
_NETWORK_CAPTURE_BUCKET = "network-captures-quarantine"
_NETWORK_CAPTURE_DISSECTORS = "frame,eth,ip,ipv6,tcp,udp,dns,http,tls,data"
_NETWORK_CAPTURE_TSHARK_FIELDS = frozenset(
    {
        "frame.number",
        "frame.time_epoch",
        "frame.len",
        "_ws.col.Protocol",
        "frame.protocols",
        "ip.src",
        "ip.dst",
        "ipv6.src",
        "ipv6.dst",
        "tcp.srcport",
        "tcp.dstport",
        "udp.srcport",
        "udp.dstport",
        "tcp.stream",
        "udp.stream",
        "dns.qry.name",
        "dns.flags.response",
        "http.host",
        "http.request.method",
        "http.request.uri",
        "tls.handshake.extensions_server_name",
    }
)


class ConfinedAnalysisArtifactMismatch(RuntimeError):
    code = "analysis_artifact_identity_mismatch"


async def execute_confined_analysis_job(
    parameters: ConfinedAnalysisJobParameters,
    *,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
) -> str:
    """Verify canonical input identity, run one closed operation, and persist its proof."""
    async with asyncio.timeout(parameters.timeout_seconds):
        sample, descriptor = await _resolve_input(
            parameters,
            blobs=blobs,
            uow_factory=uow_factory,
            maximum_bytes=settings.analysis_max_sample_bytes,
        )
        payload = await blobs.read(
            parameters.artifact_id, max_bytes=settings.analysis_max_sample_bytes
        )
        if hashlib.sha256(payload).hexdigest() != parameters.artifact_sha256:
            raise ConfinedAnalysisArtifactMismatch("analysis_artifact_hash_mismatch")
        corpus_tool = parameters.artifact_kind is ConfinedAnalysisArtifactKind.CORPUS_SAMPLE and (
            parameters.tool
            in {ConfinedAnalysisTool.FLOSS, ConfinedAnalysisTool.MWCP, ConfinedAnalysisTool.YARA}
        )
        if (
            sample is None
            and not corpus_tool
            and parameters.tool
            not in {
                ConfinedAnalysisTool.TSHARK,
                ConfinedAnalysisTool.CAPINFOS,
            }
        ):
            raise ConfinedAnalysisArtifactMismatch("analysis_tool_requires_sample_artifact")

        result_data = await _execute_tool(
            parameters,
            sample=sample,
            payload=payload,
            blob_id=parameters.artifact_id,
            settings=settings,
        )
        result_payload = {
            "schema": CONFINED_ANALYSIS_RESULT_SCHEMA,
            "operation_key": parameters.operation_key,
            "investigation_id": str(parameters.investigation_id),
            "artifact_kind": parameters.artifact_kind.value,
            "association_id": str(parameters.association_id) if parameters.association_id else None,
            "sample_id": str(parameters.sample_id) if parameters.sample_id else None,
            "artifact_id": str(parameters.artifact_id),
            "artifact_sha256": parameters.artifact_sha256,
            "tool": parameters.tool.value,
            "worker_profile": os.environ.get("ANALYSIS_WORKER_ISOLATION_PROFILE", "unknown"),
            "input_size": descriptor.size,
            "result": result_data,
        }
        output = json.dumps(
            result_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(output) > parameters.max_output_bytes:
            result_payload["result"] = _reduce_result(result_data)
            result_payload["truncated"] = True
            output = json.dumps(
                result_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        if len(output) > parameters.max_output_bytes:
            raise ValueError("analysis_result_exceeds_output_budget")
        result_blob = await blobs.ingest(
            BytesIO(output),
            logical_bucket=_ANALYSIS_RESULT_BUCKET,
            mime_type="application/json",
        )
        result_sha256 = hashlib.sha256(output).hexdigest()
        if result_blob.descriptor.sha256 != result_sha256:
            raise RuntimeError("analysis_result_blob_hash_mismatch")
        return f"confined-analysis-result://{result_blob.id}/{result_sha256}"


async def _resolve_input(
    parameters: ConfinedAnalysisJobParameters,
    *,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
    maximum_bytes: int,
) -> tuple[Any | None, Any]:
    async with uow_factory() as uow:
        blob = await uow.blobs.get(parameters.artifact_id)
        investigation = await uow.analyst_investigations.get(parameters.investigation_id)
        sample = (
            await uow.samples.get(parameters.sample_id)
            if parameters.artifact_kind is ConfinedAnalysisArtifactKind.SAMPLE
            and parameters.sample_id is not None
            else None
        )
        association = (
            await uow.sample_associations.get(parameters.association_id)
            if parameters.artifact_kind is ConfinedAnalysisArtifactKind.SAMPLE
            and parameters.association_id is not None
            else None
        )
        capture_observations = (
            tuple(
                await uow.evidence_observations.list_for_investigation(parameters.investigation_id)
            )
            if parameters.artifact_kind is ConfinedAnalysisArtifactKind.CAPTURE
            else ()
        )
    if investigation is None or blob is None:
        raise EntityNotFoundError("Canonical analysis input does not exist")
    descriptor = blob.descriptor
    if descriptor.sha256 != parameters.artifact_sha256 or descriptor.size > maximum_bytes:
        raise ConfinedAnalysisArtifactMismatch("analysis_artifact_identity_mismatch")
    if parameters.artifact_kind is ConfinedAnalysisArtifactKind.CORPUS_SAMPLE and (
        descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
    ):
        raise ConfinedAnalysisArtifactMismatch("analysis_artifact_identity_mismatch")
    if parameters.artifact_kind is ConfinedAnalysisArtifactKind.SAMPLE and (
        sample is None
        or association is None
        or sample.blob_id != parameters.artifact_id
        or sample.state.value not in {"quarantined", "review_candidate"}
        or association.investigation_id != parameters.investigation_id
        or association.sample_sha256 != descriptor.sha256
    ):
        raise ConfinedAnalysisArtifactMismatch("analysis_artifact_identity_mismatch")
    if parameters.artifact_kind is ConfinedAnalysisArtifactKind.CAPTURE and (
        descriptor.logical_bucket != _NETWORK_CAPTURE_BUCKET
        or not any(
            item.blob_id == parameters.artifact_id
            and item.sha256 == descriptor.sha256
            and item.source == "network.quarantine"
            and item.object_type == "network_capture"
            for item in capture_observations
        )
    ):
        raise ConfinedAnalysisArtifactMismatch("analysis_capture_not_associated")
    return sample, descriptor


async def _execute_tool(
    parameters: ConfinedAnalysisJobParameters,
    *,
    sample: Any,
    payload: bytes,
    blob_id: UUID,
    settings: Settings,
) -> dict[str, Any]:
    tool = parameters.tool
    if (
        tool
        in {
            ConfinedAnalysisTool.SAMPLE_ANALYSIS,
            ConfinedAnalysisTool.STATIC,
            ConfinedAnalysisTool.CAPA,
            ConfinedAnalysisTool.SMDA,
        }
        and sample is None
    ):
        raise ConfinedAnalysisArtifactMismatch("analysis_tool_requires_sample_artifact")
    if tool is ConfinedAnalysisTool.SAMPLE_ANALYSIS:
        static_result = await _run_static(parameters, sample, payload, blob_id, settings)
        capa_result = await _run_capa(parameters, payload, settings)
        smda_result = await _run_smda(parameters, payload, blob_id, settings)
        floss_result = await _run_floss(parameters, payload, settings)
        return {
            "status": _combined_status(static_result, capa_result, smda_result, floss_result),
            "families": {
                "static": static_result,
                "capa": capa_result,
                "smda": smda_result,
                "floss": floss_result,
            },
        }
    if tool is ConfinedAnalysisTool.STATIC:
        return await _run_static(parameters, sample, payload, blob_id, settings)
    if tool is ConfinedAnalysisTool.CAPA:
        return await _run_capa(parameters, payload, settings)
    if tool is ConfinedAnalysisTool.SMDA:
        return await _run_smda(parameters, payload, blob_id, settings)
    if tool is ConfinedAnalysisTool.FLOSS:
        return await _run_floss(parameters, payload, settings)
    if tool is ConfinedAnalysisTool.MWCP:
        return await _run_mwcp(parameters, payload, settings)
    if tool is ConfinedAnalysisTool.YARA:
        return await _run_yara(parameters, payload, settings)
    if tool is ConfinedAnalysisTool.TSHARK:
        return await _run_readonly_command(
            "tshark", parameters.arguments, payload, parameters, settings
        )
    if tool is ConfinedAnalysisTool.CAPINFOS:
        return await _run_readonly_command(
            "capinfos", parameters.arguments, payload, parameters, settings
        )
    return {"status": "UNAVAILABLE", "available_reason": "analysis_tool_unavailable"}


async def _run_static(
    parameters: ConfinedAnalysisJobParameters,
    sample: Any | None,
    payload: bytes,
    blob_id: UUID,
    settings: Settings,
) -> dict[str, Any]:
    if sample is None or parameters.sample_id is None:
        return {
            "status": "POLICY_DENIED",
            "available_reason": "analysis_tool_requires_sample_artifact",
        }
    try:
        parameter_values = {
            "extractor": "static-v1",
            "string_min_length": settings.analysis_string_min_length,
            "max_strings": settings.analysis_max_strings,
            "max_sample_bytes": settings.analysis_max_sample_bytes,
        }
        parameters_sha256 = hashlib.sha256(
            json.dumps(parameter_values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        features = StaticFeatureExtractor().extract(
            sample_id=parameters.sample_id,
            blob_id=blob_id,
            payload=payload,
            parameters_sha256=parameters_sha256,
            tlp=sample.tlp,
            do_not_submit=sample.do_not_submit,
            external_llm_allowed=sample.external_llm_allowed,
            max_strings=settings.analysis_max_strings,
            min_string_length=settings.analysis_string_min_length,
        )
        feature_data = features.as_json()
        strings = [
            {
                "offset": item.get("offset"),
                "encoding": item.get("encoding", "unknown"),
                "value": sanitize_evidence_text(item.get("value", ""), max_chars=512),
                "occurrence_count": max(1, int(item.get("occurrence_count", 1))),
            }
            for item in feature_data.get("strings", ())[:128]
            if isinstance(item, Mapping) and item.get("value")
        ]
        return {
            "status": "SUCCEEDED",
            "version": features.extractor_version,
            "format": features.format.value,
            "size": features.size,
            "sha256": features.sha256,
            "sections": len(features.sections),
            "imports": len(features.imports),
            "exports": len(features.exports),
            "strings_observed": len(feature_data.get("strings", ())),
            "strings": strings,
            "partial_errors": list(features.partial_errors[:16]),
        }
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


async def _run_capa(
    parameters: ConfinedAnalysisJobParameters, payload: bytes, settings: Settings
) -> dict[str, Any]:
    rules_hash = ruleset_manifest(settings.analysis_capa_rules_path)
    if rules_hash is None:
        return {"status": "UNAVAILABLE", "available_reason": "capa_ruleset_unavailable"}
    try:
        version, result = await CapaRunner().run(
            sample=payload,
            rules_path=settings.analysis_capa_rules_path,
            timeout_seconds=min(parameters.timeout_seconds, settings.analysis_capa_timeout_seconds),
            output_limit=min(parameters.max_output_bytes, settings.analysis_capa_max_output_bytes),
            memory_limit_bytes=settings.analysis_capa_max_memory_bytes,
        )
        if result.status is not AnalysisSubprocessStatus.SUCCEEDED:
            return {
                "status": result.status.value,
                "version": version,
                "ruleset_sha256": rules_hash,
            }
        capabilities, errors = parse_capa_output(result.stdout)
        return {
            "status": "SUCCEEDED" if not errors else "PARTIAL",
            "version": version,
            "ruleset_sha256": rules_hash,
            "capabilities_observed": len(capabilities),
            "capabilities": [
                sanitize_evidence_text(item.get("rule_id", ""), max_chars=256)
                for item in capabilities[:256]
            ],
            "errors": list(errors[:16]),
        }
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


async def _run_smda(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    blob_id: UUID,
    settings: Settings,
) -> dict[str, Any]:
    try:
        result = await SmdaAdapter().extract(
            payload,
            timeout_seconds=min(parameters.timeout_seconds, settings.smda_timeout_seconds),
            output_limit=min(parameters.max_output_bytes, settings.smda_max_output_bytes),
            memory_limit_bytes=settings.smda_max_memory_bytes,
        )
        if result.extraction is None:
            return {"status": result.status, "error": result.error}
        if parameters.sample_id is None:
            return {
                "status": "POLICY_DENIED",
                "available_reason": "smda_requires_sample_identity",
            }
        extraction = result.extraction
        max_per_sample = min(100_000, max(1, parameters.max_output_bytes // 4096))
        feature_parameters = {
            "analysis": "analysis-worker-smda-code-features.v1",
            "code_ngram_sizes": [4, 6, 8],
            "code_ngram_max_per_sample": max_per_sample,
        }
        feature_parameters_sha256 = hashlib.sha256(
            json.dumps(feature_parameters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        ngrams = build_code_ngrams(
            extraction.functions,
            (4, 6, 8),
            max_per_sample=max_per_sample,
        )
        code_features = CodeFeatureSet(
            sample_id=parameters.sample_id,
            blob_id=blob_id,
            tool_version=extraction.smda_version,
            escaper_compatibility_version=extraction.escaper_compatibility_version,
            intel_pic_hash_escape_version=extraction.intel_pic_hash_escape_version,
            parameters_sha256=feature_parameters_sha256,
            architecture=extraction.architecture,
            status=CodeFeatureStatus.SUCCEEDED,
            ngrams=ngrams,
            packing=build_packing_signals(payload, len(extraction.functions)),
        ).as_json()
        code_features_bytes = json.dumps(
            code_features, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return {
            "status": result.status,
            "version": extraction.smda_version,
            "architecture": extraction.architecture,
            "functions_observed": len(extraction.functions),
            "escaper_compatibility_version": extraction.escaper_compatibility_version,
            "intel_pic_hash_escape_version": extraction.intel_pic_hash_escape_version,
            "code_features": code_features,
            "code_features_sha256": hashlib.sha256(code_features_bytes).hexdigest(),
        }
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


async def _run_floss(
    parameters: ConfinedAnalysisJobParameters, payload: bytes, settings: Settings
) -> dict[str, Any]:
    executable = shutil.which("floss")
    if executable is None:
        return {"status": "UNAVAILABLE", "available_reason": "floss_binary_unavailable"}
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cti-floss-", dir="/tmp", delete=False) as handle:
            path = handle.name
            handle.write(payload)
        os.chmod(path, 0o400)
        result = await run_analysis_subprocess(
            [executable, "--json", "--quiet", "--", path],
            timeout_seconds=min(parameters.timeout_seconds, 120),
            output_limit=parameters.max_output_bytes,
            memory_limit_bytes=min(
                max(settings.analysis_capa_max_memory_bytes, settings.smda_max_memory_bytes),
                1536 * 1024 * 1024,
            ),
            environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
        output_hash = hashlib.sha256(result.stdout).hexdigest()
        if result.status is not AnalysisSubprocessStatus.SUCCEEDED:
            return {
                "status": result.status.value,
                "version": _distribution_version("flare-floss"),
                "output_sha256": output_hash,
                "output_bytes": len(result.stdout),
                "exit_code": result.exit_code,
                "truncated": result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT,
            }
        decoded = json.loads(result.stdout)
        strings, observed = _parse_floss_payload(
            decoded,
            max_strings=min(int(parameters.analysis_parameters.get("max_strings", "128")), 4096),
        )
        version = None
        if isinstance(decoded, Mapping) and isinstance(decoded.get("metadata"), Mapping):
            version = decoded["metadata"].get("version")
        return {
            "status": "SUCCEEDED",
            "version": version or _distribution_version("flare-floss"),
            "output_sha256": output_hash,
            "output_bytes": len(result.stdout),
            "exit_code": result.exit_code,
            "strings_observed": observed,
            "strings": [
                {
                    "value": sanitize_evidence_text(item.value, max_chars=512),
                    "kind": item.kind.value,
                    "encoding": item.encoding,
                    "offset": item.offset,
                    "address": item.address,
                    "function": item.function,
                    "context": sanitize_evidence_text(item.context or "", max_chars=1000) or None,
                    "evidence_path": item.evidence_path,
                }
                for item in strings
            ],
            "truncated": observed > len(strings),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"status": "INVALID_OUTPUT", "error_type": type(exc).__name__}
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


async def _run_mwcp(
    parameters: ConfinedAnalysisJobParameters, payload: bytes, settings: Settings
) -> dict[str, Any]:
    parser_id = parameters.analysis_parameters.get("parser_id", "")
    parser_version = parameters.analysis_parameters.get("parser_version", "")
    allowed_version = settings.mwcp_parser_versions.get(parser_id)
    mwcp_version = _distribution_version("mwcp")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", parser_id)
        or not allowed_version
        or allowed_version != parser_version
    ):
        return {"status": "POLICY_DENIED", "available_reason": "parser_not_allowlisted"}
    executable = shutil.which("mwcp")
    if executable is None:
        return {"status": "UNAVAILABLE", "available_reason": "mwcp_binary_unavailable"}
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cti-mwcp-", dir="/tmp", delete=False) as handle:
            path = handle.name
            handle.write(payload)
        os.chmod(path, 0o400)
        command = [executable]
        parser_directory = os.environ.get("ANALYSIS_MWCP_PARSER_DIR")
        if parser_directory:
            command.extend(("--parser-dir", parser_directory))
        command.extend(
            [
                "parse",
                "--no-recursive",
                "--no-output-files",
                "--no-data",
                "--no-logs",
                "-f",
                "json",
                parser_id,
                path,
            ]
        )
        result = await run_analysis_subprocess(
            command,
            timeout_seconds=min(parameters.timeout_seconds, 120),
            output_limit=parameters.max_output_bytes,
            memory_limit_bytes=min(
                max(settings.analysis_capa_max_memory_bytes, settings.smda_max_memory_bytes),
                1536 * 1024 * 1024,
            ),
            environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
        output_hash = hashlib.sha256(result.stdout).hexdigest()
        if result.status is not AnalysisSubprocessStatus.SUCCEEDED:
            return {
                "status": result.status.value,
                "parser_id": parser_id,
                "parser_version": parser_version,
                "output_sha256": output_hash,
                "output_bytes": len(result.stdout),
                "exit_code": result.exit_code,
                "truncated": result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT,
            }
        decoded = json.loads(result.stdout)
        report = decoded[0] if isinstance(decoded, list) and len(decoded) == 1 else decoded
        if not isinstance(report, Mapping) or report.get("parser") != parser_id:
            return {"status": "INVALID_OUTPUT", "available_reason": "parser_identity_mismatch"}
        if report.get("mwcp_version") != mwcp_version:
            return {"status": "POLICY_DENIED", "available_reason": "parser_version_unattested"}
        return {
            "status": "SUCCEEDED",
            "parser_id": parser_id,
            "parser_version": parser_version,
            "mwcp_version": mwcp_version,
            "output_sha256": output_hash,
            "output_bytes": len(result.stdout),
            "exit_code": result.exit_code,
            "report": report,
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"status": "INVALID_OUTPUT", "error_type": type(exc).__name__}
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


async def _run_yara(
    parameters: ConfinedAnalysisJobParameters, payload: bytes, settings: Settings
) -> dict[str, Any]:
    source = parameters.analysis_parameters.get("rule_source", "")
    limits = YaraRunnerLimits(
        timeout_seconds=min(parameters.timeout_seconds, 30),
        cpu_seconds=max(1, math.ceil(min(parameters.timeout_seconds, 30))),
        memory_bytes=settings.analysis_capa_max_memory_bytes,
        maximum_output_bytes=min(parameters.max_output_bytes, 64 * 1024),
        maximum_source_bytes=256_000,
        maximum_samples=1,
        maximum_sample_bytes=settings.analysis_max_sample_bytes,
        maximum_total_sample_bytes=settings.analysis_max_sample_bytes,
    )
    try:
        sample = YaraSampleInput(sample_sha256=parameters.artifact_sha256, data=payload)
        result = IsolatedYaraRunner(limits).match(source, (sample,))
        return {
            "status": result.status.value,
            "version": result.engine_version,
            "matches": parameters.artifact_sha256 in result.matches_sha256,
            "output_sha256": hashlib.sha256(
                json.dumps(
                    {"status": result.status.value, "matches": result.matches_sha256},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "message": sanitize_evidence_text(result.message, max_chars=1000),
            "line": result.line,
        }
    except Exception as exc:
        return {
            "status": YaraExecutionStatus.ENGINE_CRASHED.value,
            "error_type": type(exc).__name__,
        }


async def _run_readonly_command(
    executable: str,
    arguments: Sequence[str],
    payload: bytes,
    parameters: ConfinedAnalysisJobParameters,
    settings: Settings,
) -> dict[str, Any]:
    if shutil.which(executable) is None:
        return {"status": "UNAVAILABLE", "available_reason": "analysis_tool_unavailable"}
    try:
        validated = _validate_command_arguments(executable, arguments)
        with tempfile.NamedTemporaryFile(prefix="cti-analysis-", dir="/tmp", delete=False) as file:
            sample_path = file.name
            file.write(payload)
        os.chmod(sample_path, 0o400)
        try:
            if executable == "capinfos":
                command = [executable, *validated, str(sample_path)]
            else:
                command = [
                    executable,
                    "-n",
                    "--only-protocols",
                    _NETWORK_CAPTURE_DISSECTORS,
                    "-r",
                    str(sample_path),
                    *validated,
                ]
            output = await run_analysis_subprocess(
                command,
                timeout_seconds=parameters.timeout_seconds,
                output_limit=parameters.max_output_bytes,
                memory_limit_bytes=settings.analysis_capa_max_memory_bytes,
                environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
            )
        finally:
            os.unlink(sample_path)
        return {
            "status": output.status.value,
            "return_code": output.exit_code,
            "stdout": sanitize_evidence_text(
                output.stdout.decode("utf-8", "replace"), max_chars=16_000
            ),
            "stderr": sanitize_evidence_text(
                output.stderr.decode("utf-8", "replace"), max_chars=2_000
            ),
            "truncated": output.status is AnalysisSubprocessStatus.OUTPUT_LIMIT,
        }
    except (TypeError, ValueError):
        return {"status": "POLICY_DENIED", "available_reason": "analysis_command_not_allowlisted"}
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


def _validate_command_arguments(executable: str, arguments: Sequence[str]) -> tuple[str, ...]:
    items = tuple(arguments)
    if len(items) > 128 or any(not item or len(item) > 1024 or "\x00" in item for item in items):
        raise ValueError("invalid argument bounds")
    if executable == "capinfos":
        allowed = {"-a", "-c", "-e", "-E", "-I", "-M", "-t", "-u", "-x", "-y"}
        if any(item not in allowed for item in items):
            raise ValueError("capinfos option is not allowlisted")
        return items

    simple = {"-2", "-n", "-q"}
    values = {"-Y", "-T", "-e", "-c", "-z"}
    index = 0
    while index < len(items):
        option = items[index]
        if option in simple:
            index += 1
            continue
        if option not in values or index + 1 >= len(items):
            raise ValueError("tshark option is not allowlisted")
        value = items[index + 1]
        if option == "-T" and value not in {"fields", "json", "ek"}:
            raise ValueError("tshark output format is not allowlisted")
        if option == "-e" and _FIELD_NAME.fullmatch(value) is None:
            raise ValueError("tshark field is invalid")
        if option == "-e" and value not in _NETWORK_CAPTURE_TSHARK_FIELDS:
            raise ValueError("tshark field is not allowlisted")
        if option == "-c" and (not value.isdigit() or int(value) > 100_000):
            raise ValueError("tshark packet limit is invalid")
        if (
            option == "-z"
            and value not in {"conv,tcp", "conv,udp", "expert,warn"}
            and not _FOLLOW_STREAM.fullmatch(value)
        ):
            raise ValueError("tshark statistic is not allowlisted")
        if option == "-Y" and len(value) > 1024:
            raise ValueError("tshark display filter is too long")
        index += 2
    return items


def _combined_status(*results: Mapping[str, Any]) -> str:
    statuses = [str(item.get("status", "FAILED")).upper() for item in results]
    if statuses and all(item == "SUCCEEDED" for item in statuses):
        return "SUCCEEDED"
    if any(item == "SUCCEEDED" for item in statuses):
        return "PARTIAL"
    return "FAILED"


def _reduce_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if "families" in result and isinstance(result["families"], Mapping):
        return {
            "status": result.get("status", "PARTIAL"),
            "families": {
                name: _reduce_family_result(family)
                for name, family in result["families"].items()
                if isinstance(family, Mapping)
            },
        }
    if isinstance(result.get("code_features"), Mapping):
        return _reduce_family_result(result)
    return {
        key: value
        for key, value in result.items()
        if key
        in {
            "status",
            "version",
            "format",
            "size",
            "sha256",
            "ruleset_sha256",
            "architecture",
            "functions_observed",
            "capabilities_observed",
            "strings_observed",
            "code_features_sha256",
            "code_features_status",
            "code_ngram_count",
            "error_type",
            "available_reason",
            "return_code",
            "truncated",
        }
    }


def _reduce_family_result(family: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "status",
        "version",
        "format",
        "size",
        "sha256",
        "ruleset_sha256",
        "architecture",
        "functions_observed",
        "capabilities_observed",
        "strings_observed",
        "code_features_sha256",
        "error_type",
        "available_reason",
    }
    reduced = {key: value for key, value in family.items() if key in keys}
    code_features = family.get("code_features")
    if isinstance(code_features, Mapping):
        ngrams = code_features.get("ngrams", ())
        reduced["code_features"] = {
            key: code_features[key]
            for key in (
                "sample_id",
                "blob_id",
                "tool_version",
                "escaper_compatibility_version",
                "intel_pic_hash_escape_version",
                "parameters_sha256",
                "architecture",
                "status",
                "packing",
                "errors",
            )
            if key in code_features
        }
        reduced["code_features"]["ngram_count"] = len(ngrams) if isinstance(ngrams, list) else 0
        reduced["code_features_status"] = code_features.get("status")
        reduced["code_ngram_count"] = reduced["code_features"]["ngram_count"]
    return reduced


def build_analysis_worker_probe_payload(settings: Settings) -> dict[str, Any]:
    capa_rules = settings.analysis_capa_rules_path
    versions = {
        "static": "autowork-static-v1"
        if _distribution_version("pefile") and _distribution_version("pyelftools")
        else None,
        "capa": _distribution_version("flare-capa"),
        "smda": _distribution_version("smda"),
        "capa_ruleset_revision": _read_marker(capa_rules / "AUTOWORK_RULESET_REVISION"),
        "capa_ruleset_sha256": _read_marker(capa_rules / "AUTOWORK_RULESET_SHA256"),
        "tshark": _binary_version("tshark", "--version"),
        "capinfos": _binary_version("capinfos", "-v"),
        "floss": _distribution_version("flare-floss"),
        "mwcp": _distribution_version("mwcp"),
        "yara": _distribution_version("yara-python"),
    }
    registered_mwcp_parsers = _registered_mwcp_parsers()
    for parser_id, parser_version in sorted(settings.mwcp_parser_versions.items()):
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", parser_id)
            and parser_id in registered_mwcp_parsers
            and re.fullmatch(r"[A-Za-z0-9.+_-]{1,128}", parser_version)
        ):
            versions[f"mwcp_parser:{parser_id}"] = parser_version
    status = _proc_status()
    return {
        "uid": os.getuid(),
        "no_new_privileges": status.get("NoNewPrivs") == "1",
        "effective_capabilities": status.get("CapEff"),
        "root_read_only": _root_mount_is_read_only(),
        "isolation_profile": os.environ.get("ANALYSIS_WORKER_ISOLATION_PROFILE"),
        "supported_families": [
            "static",
            "capa",
            "smda",
            "sample_analysis",
            "tshark",
            "capinfos",
            "floss",
            "mwcp",
            "yara",
        ],
        "versions": versions,
    }


@functools.cache
def _registered_mwcp_parsers() -> frozenset[str]:
    executable = shutil.which("mwcp")
    if executable is None:
        return frozenset()
    command = [executable]
    parser_directory = os.environ.get("ANALYSIS_MWCP_PARSER_DIR")
    if parser_directory:
        command.extend(("--parser-dir", parser_directory))
    command.extend(("list", "--json"))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 64 * 1024:
        return frozenset()
    try:
        entries = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return frozenset()
    return frozenset(
        row[0]
        for row in entries
        if isinstance(row, list)
        and row
        and isinstance(row[0], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", row[0])
    )


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


@functools.cache
def _binary_version(name: str, argument: str) -> str | None:
    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, argument],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(result.stdout + result.stderr)
    return match.group(1) if result.returncode == 0 and match else None


def _read_marker(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if value else None


def _proc_status() -> dict[str, str]:
    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    return {
        key: value.strip()
        for line in lines
        if ":" in line
        for key, value in (line.split(":", 1),)
        if key in {"NoNewPrivs", "CapEff"}
    }


def _root_mount_is_read_only() -> bool:
    try:
        for line in Path("/proc/mounts").read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[1] == "/":
                return "ro" in fields[3].split(",")
    except OSError:
        return False
    return False
