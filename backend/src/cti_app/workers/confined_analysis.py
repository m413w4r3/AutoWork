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
import time
import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.confined_analysis import (
    CONFINED_ANALYSIS_RESULT_SCHEMA,
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisTool,
    archive_member_transfer_bucket,
    validate_archive_tool_request,
)
from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.application.local_analysis import detect_file_type
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.rule_workbench import YaraSampleInput, _extract_yara_string_hits
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.config import Settings
from cti_app.domain.capabilities import Capability, CapabilitySet, CapabilitySetStatus
from cti_app.domain.code_features import CodeFeatureSet, CodeFeatureStatus, build_code_ngrams
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.local_analysis import (
    LOCAL_ARCHIVE_MAX_MEMBER_BYTES,
    LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES,
    LOCAL_COMMAND_MAX_RANGE_BYTES,
    LOCAL_COMMAND_MAX_XXD_RANGE_BYTES,
    LocalAnalysisFileType,
)
from cti_app.domain.rule_workbench import YaraExecutionStatus, detect_file_format_magic
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)
from cti_app.infrastructure.capa import CapaRunner, parse_capa_output, ruleset_manifest
from cti_app.infrastructure.confined_suricata import validate_suricata_rule_source
from cti_app.infrastructure.floss_adapter import _parse_floss_payload
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture
from cti_app.infrastructure.smda import SmdaAdapter
from cti_app.infrastructure.static_analysis import StaticFeatureExtractor, build_packing_signals
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner, YaraRunnerLimits
from cti_app.workers.archive_commands import run_7z_archive_command, run_zip_members
from cti_app.workers.dotnet_analysis import probe_ilspycmd, run_dotnet_analysis

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_YARA_COVERAGE_HITS = 256
_MAX_YARA_COVERAGE_OFFSETS = 4_096
_VERSION = re.compile(r"(?:TShark|Capinfos) \(Wireshark\) ([0-9]+\.[0-9]+\.[0-9]+)")
_LOCAL_VERSION = re.compile(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]+(?:\.[0-9]+)+)\b")
_SURICATA_VERSION = re.compile(r"\bSuricata(?:\s+version)?\s+([0-9]+\.[0-9]+\.[0-9]+)", re.I)
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_MONODIS_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9.+:~_-]{1,128}$")
ZIP_MEMBERS_TOOL_VERSION = "autowork-zip-members-v1"
_MONODIS_METHOD_ROW = re.compile(r"^\s*(\d+):.*?\s('[^']+'|[^\s(]+)\s+\(")
_VT_SANDBOX_PCAP_SOURCE_VERSION = "vt-sandbox-pcap.v1"
_FOLLOW_STREAM = re.compile(r"^(?:follow,tcp|follow,udp),raw,[0-9]{1,8}$")
_ANALYSIS_RESULT_BUCKET = "confined-analysis-results"
_NETWORK_CAPTURE_BUCKET = "network-captures-quarantine"
_NETWORK_CAPTURE_DISSECTORS = frozenset(
    {
        "frame",
        "eth",
        "sll",
        "sll2",
        "raw",
        "ip",
        "ipv6",
        "tcp",
        "udp",
        "dns",
        "http",
        "http2",
        "tls",
        "data",
    }
)
_TSHARK_PROFILE_MAX_BYTES = 1_048_576
_TSHARK_DISSECTION_PROFILES: dict[str, tuple[str, ...]] = {}
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
        if parameters.tool is ConfinedAnalysisTool.LOCAL_COMMAND:
            _validate_local_command_arguments(
                parameters.arguments,
                parameters.analysis_parameters,
                max_range_bytes=settings.analysis_local_command_max_range_bytes,
            )
        elif parameters.tool in {
            ConfinedAnalysisTool.ZIP_MEMBERS,
            ConfinedAnalysisTool.ARCHIVE_LIST,
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
            ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER,
        }:
            validate_archive_tool_request(
                parameters.tool, parameters.arguments, parameters.analysis_parameters
            )
        if (
            parameters.tool
            in {
                ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
                ConfinedAnalysisTool.SURICATA_REPLAY,
            }
            and parameters.artifact_kind is not ConfinedAnalysisArtifactKind.CAPTURE
        ):
            raise ConfinedAnalysisArtifactMismatch("suricata_requires_capture_artifact")
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
                ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
                ConfinedAnalysisTool.SURICATA_REPLAY,
            }
        ):
            raise ConfinedAnalysisArtifactMismatch("analysis_tool_requires_sample_artifact")

        if (
            parameters.artifact_kind is ConfinedAnalysisArtifactKind.CORPUS_SAMPLE
            and parameters.tool is ConfinedAnalysisTool.YARA
            and descriptor.logical_bucket == _NETWORK_CAPTURE_BUCKET
        ):
            result_data = {
                "status": "non_measurable",
                "message": "YARA rules cannot measure network-capture artifacts as file samples.",
            }
        else:
            result_data = await _execute_tool(
                parameters,
                sample=sample,
                payload=payload,
                blob_id=parameters.artifact_id,
                blobs=blobs,
                uow_factory=uow_factory,
                settings=settings,
            )
        if parameters.tool is ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER:
            member_bytes = result_data.pop("_member_bytes", None)
            if member_bytes is not None:
                if not isinstance(member_bytes, bytes):
                    raise ValueError("archive member transfer is not bytes")
                member_sha256 = hashlib.sha256(member_bytes).hexdigest()
                if (
                    len(member_bytes) > LOCAL_ARCHIVE_MAX_MEMBER_BYTES
                    or member_sha256 != result_data.get("member_sha256")
                    or len(member_bytes) != result_data.get("member_size")
                ):
                    raise ConfinedAnalysisArtifactMismatch("archive_member_transfer_mismatch")
                transfer_nonce = parameters.analysis_parameters.get("transfer_nonce")
                if transfer_nonce is None:
                    transfer_nonce = hashlib.sha256(parameters.operation_key.encode()).hexdigest()
                transfer_bucket = archive_member_transfer_bucket(transfer_nonce)
                transfer_blob = await blobs.ingest(
                    BytesIO(member_bytes),
                    logical_bucket=transfer_bucket,
                    mime_type="application/octet-stream",
                )
                if (
                    transfer_blob.descriptor.sha256 != member_sha256
                    or transfer_blob.descriptor.size != len(member_bytes)
                    or transfer_blob.descriptor.logical_bucket != transfer_bucket
                ):
                    raise ConfinedAnalysisArtifactMismatch("archive_member_transfer_mismatch")
                result_data["transfer_blob_id"] = str(transfer_blob.id)
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
        and not (
            parameters.tool is ConfinedAnalysisTool.YARA
            and descriptor.logical_bucket == _NETWORK_CAPTURE_BUCKET
        )
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
    if parameters.tool is ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER and (
        sample is None
        or sample.subject_id != investigation.subject_id
        or descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
        or association is None
        or association.investigation_id != parameters.investigation_id
        or association.sample_sha256 != descriptor.sha256
    ):
        raise ConfinedAnalysisArtifactMismatch("analysis_archive_parent_not_quarantined")
    if parameters.artifact_kind is ConfinedAnalysisArtifactKind.CAPTURE and (
        descriptor.logical_bucket != _NETWORK_CAPTURE_BUCKET
        or not any(
            item.blob_id == parameters.artifact_id
            and item.sha256 == descriptor.sha256
            and item.object_type == "network_capture"
            and item.object_id == descriptor.sha256
            and (
                item.source == "network.quarantine"
                or (
                    item.source == "VirusTotal"
                    and item.source_version == _VT_SANDBOX_PCAP_SOURCE_VERSION
                )
            )
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
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
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
        static_result = await _run_static(
            parameters, sample, payload, blob_id, blobs, uow_factory, settings
        )
        capa_result = await _run_capa(parameters, payload, blobs, uow_factory, settings)
        smda_result = await _run_smda(parameters, payload, blob_id, blobs, uow_factory, settings)
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
        return await _run_static(parameters, sample, payload, blob_id, blobs, uow_factory, settings)
    if tool is ConfinedAnalysisTool.CAPA:
        return await _run_capa(parameters, payload, blobs, uow_factory, settings)
    if tool is ConfinedAnalysisTool.SMDA:
        return await _run_smda(parameters, payload, blob_id, blobs, uow_factory, settings)
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
    if tool is ConfinedAnalysisTool.LOCAL_COMMAND:
        return await _run_local_command(parameters, payload, settings)
    if tool is ConfinedAnalysisTool.ZIP_MEMBERS:
        return await run_zip_members(
            payload,
            member_prefix=parameters.analysis_parameters.get("member_prefix"),
            max_members=int(parameters.analysis_parameters.get("max_members", "2000")),
            max_output_bytes=max(1, parameters.max_output_bytes - 4096),
            tool_version=ZIP_MEMBERS_TOOL_VERSION,
        )
    if tool in {
        ConfinedAnalysisTool.ARCHIVE_LIST,
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_HASH,
        ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER,
    }:
        return await run_7z_archive_command(
            tool,
            payload,
            member_prefix=parameters.analysis_parameters.get("member_prefix"),
            max_members=int(parameters.analysis_parameters.get("max_members", "2000")),
            archive_preset=parameters.analysis_parameters.get("archive_preset", "none"),
            max_output_bytes=max(1, parameters.max_output_bytes - 4096),
            timeout_seconds=min(
                parameters.timeout_seconds, settings.local_analysis_timeout_seconds
            ),
            memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
            tool_version=settings.analysis_local_7z_expected_version,
            member_path=parameters.analysis_parameters.get("member_path"),
            expected_sha256=parameters.analysis_parameters.get("expected_sha256"),
        )
    if tool is ConfinedAnalysisTool.DOTNET_ILSPY:
        if sample is None:
            raise ConfinedAnalysisArtifactMismatch("analysis_tool_requires_sample_artifact")
        return await run_dotnet_analysis(
            parameters,
            payload,
            settings,
            monodis_version=_monodis_package_version,
            extract_monodis_method_body=_extract_monodis_method_body,
        )
    if tool in {
        ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
        ConfinedAnalysisTool.SURICATA_REPLAY,
    }:
        return await _run_suricata(parameters, payload, blobs=blobs, settings=settings)
    return {"status": "UNAVAILABLE", "available_reason": "analysis_tool_unavailable"}


async def _run_static(
    parameters: ConfinedAnalysisJobParameters,
    sample: Any | None,
    payload: bytes,
    blob_id: UUID,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
) -> dict[str, Any]:
    if sample is None or parameters.sample_id is None:
        return {
            "status": "POLICY_DENIED",
            "available_reason": "analysis_tool_requires_sample_artifact",
        }
    try:
        parameter_values = {
            "analysis_string_min_length": settings.analysis_string_min_length,
            "analysis_max_strings": settings.analysis_max_strings,
            "analysis_max_sample_bytes": settings.analysis_max_sample_bytes,
        }
        parameters_sha256 = hashlib.sha256(
            json.dumps(parameter_values, sort_keys=True).encode()
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
        await StaticAnalysisService(
            blobs,
            uow_factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
            string_min_length=settings.analysis_string_min_length,
            max_strings=settings.analysis_max_strings,
        ).persist_worker_result(features)
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
            "exports_observed": len(features.exports),
            "imphash": features.imphash,
            "opcode_fragment16": list(features.opcode_fragment16[:128]),
            "exports": list(features.exports[:128]),
            "strings_observed": len(feature_data.get("strings", ())),
            "strings": strings,
            "partial_errors": list(features.partial_errors[:16]),
        }
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


_CAPA_RUNNER_REVISION = "confined-r2"


async def _run_capa(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
) -> dict[str, Any]:
    rules_hash = ruleset_manifest(settings.analysis_capa_rules_path)
    try:
        parameter_values = {
            "analysis_capa_timeout_seconds": settings.analysis_capa_timeout_seconds,
            "analysis_capa_max_output_bytes": settings.analysis_capa_max_output_bytes,
            "analysis_capa_max_memory_bytes": settings.analysis_capa_max_memory_bytes,
            # Capability sets are immutable per key: results recorded by the earlier broken
            # runner (read-only HOME, strict rule metadata) must not shadow a working run.
            "capa_runner_revision": _CAPA_RUNNER_REVISION,
        }
        parameters_sha256 = hashlib.sha256(
            json.dumps(parameter_values, sort_keys=True).encode()
        ).hexdigest()
        sample_id = parameters.sample_id
        if sample_id is None:
            return {
                "status": "POLICY_DENIED",
                "engine_status": "POLICY_DENIED",
                "available_reason": "capa_requires_sample_identity",
            }
        capabilities: tuple[dict[str, Any], ...] = ()
        errors: tuple[str, ...] = ()
        version = "9.4.0"
        engine_status: str
        available_reason: str | None = None
        if zipfile.is_zipfile(BytesIO(payload)):
            status = CapabilitySetStatus.UNAVAILABLE
            engine_status = "UNSUPPORTED_FORMAT"
            available_reason = "capa_unsupported_format_zip"
            errors = (available_reason,)
            rules_hash = rules_hash or ""
        elif rules_hash is None:
            status = CapabilitySetStatus.UNAVAILABLE
            engine_status = "UNAVAILABLE"
            available_reason = "capa_ruleset_unavailable"
            errors = (available_reason,)
            rules_hash = ""
        else:
            version, execution = await CapaRunner().run(
                sample=payload,
                rules_path=settings.analysis_capa_rules_path,
                timeout_seconds=min(
                    parameters.timeout_seconds, settings.analysis_capa_timeout_seconds
                ),
                output_limit=min(
                    parameters.max_output_bytes, settings.analysis_capa_max_output_bytes
                ),
                memory_limit_bytes=settings.analysis_capa_max_memory_bytes,
            )
            if execution.status is not AnalysisSubprocessStatus.SUCCEEDED:
                engine_status = execution.status.value
                stderr_detail = _safe_engine_stderr(execution.stderr)
                available_reason = (
                    f"capa_{execution.status.value.lower()}: {stderr_detail}"
                    if stderr_detail
                    else f"capa_{execution.status.value.lower()}"
                )[:1200]
                status = (
                    CapabilitySetStatus.INVALID_OUTPUT
                    if execution.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                    else CapabilitySetStatus.UNAVAILABLE
                )
                errors = (available_reason,)
            else:
                capabilities, errors = parse_capa_output(execution.stdout)
                status = (
                    CapabilitySetStatus.SUCCEEDED
                    if not errors
                    else CapabilitySetStatus.INVALID_OUTPUT
                )
                engine_status = "SUCCEEDED" if not errors else "INVALID_OUTPUT"
                if errors:
                    available_reason = "capa_invalid_output"
        feature_set = CapabilitySet(
            sample_id=sample_id,
            tool_name="capa",
            tool_version=version,
            ruleset_sha256=rules_hash,
            parameters_sha256=parameters_sha256,
            status=status,
            capabilities=tuple(Capability(**item) for item in capabilities),
            errors=tuple(errors),
        )
        await CapabilitiesService(
            blobs,
            uow_factory,
            rules_path=settings.analysis_capa_rules_path,
            timeout_seconds=settings.analysis_capa_timeout_seconds,
            max_output_bytes=settings.analysis_capa_max_output_bytes,
            max_memory_bytes=settings.analysis_capa_max_memory_bytes,
        ).persist_worker_result(feature_set)
        return {
            "status": (
                "SUCCEEDED"
                if feature_set.status is CapabilitySetStatus.SUCCEEDED
                else "PARTIAL"
                if feature_set.capabilities
                else "UNAVAILABLE"
                if feature_set.status is CapabilitySetStatus.UNAVAILABLE
                else "PARTIAL"
                if feature_set.errors
                else feature_set.status.value
            ),
            "engine_status": engine_status,
            "available_reason": available_reason,
            "version": version,
            "ruleset_sha256": rules_hash,
            "parameters_sha256": parameters_sha256,
            "capabilities_observed": len(feature_set.capabilities),
            "capabilities": [
                sanitize_evidence_text(item.rule_id, max_chars=256)
                for item in feature_set.capabilities[:256]
            ],
            "errors": list(feature_set.errors[:16]),
        }
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__}


def _safe_engine_stderr(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace")
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s\"'<>:]+/?)+", "<path>", text)
    return sanitize_evidence_text(text, max_chars=1024)


async def _run_smda(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    blob_id: UUID,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
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
            if parameters.sample_id is None:
                return {
                    "status": "POLICY_DENIED",
                    "available_reason": "smda_requires_sample_identity",
                }
            try:
                feature_status = CodeFeatureStatus(result.status)
            except ValueError:
                feature_status = CodeFeatureStatus.INVALID_OUTPUT
            feature_parameters = {
                "analysis": "analysis-worker-smda-code-features.v1",
                "code_ngram_sizes": [4, 6, 8],
                "code_ngram_max_per_sample": min(
                    100_000, max(1, parameters.max_output_bytes // 4096)
                ),
            }
            feature_parameters_sha256 = hashlib.sha256(
                json.dumps(feature_parameters, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            feature_set = CodeFeatureSet(
                sample_id=parameters.sample_id,
                blob_id=blob_id,
                tool_version="4.5.0",
                escaper_compatibility_version="4.4.5",
                intel_pic_hash_escape_version="4.3.5",
                parameters_sha256=feature_parameters_sha256,
                architecture="UNKNOWN",
                status=feature_status,
                ngrams=(),
                packing=build_packing_signals(payload, 0),
                errors=(result.error,) if result.error else (),
            )
            stored = await CodeFeatureService(
                blobs, uow_factory, SmdaAdapter()
            ).persist_worker_result(feature_set)
            code_features = stored.as_json()
            code_features_bytes = json.dumps(
                code_features, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
            return {
                "status": result.status,
                "version": stored.tool_version,
                "error": result.error,
                "code_features": code_features,
                "code_features_sha256": hashlib.sha256(code_features_bytes).hexdigest(),
            }
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
        feature_set = CodeFeatureSet(
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
        )
        stored = await CodeFeatureService(blobs, uow_factory, SmdaAdapter()).persist_worker_result(
            feature_set
        )
        code_features = stored.as_json()
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
    if zipfile.is_zipfile(BytesIO(payload)):
        return {
            "status": "UNSUPPORTED_FORMAT",
            "version": _distribution_version("flare-floss"),
            "available_reason": "floss_unsupported_format_zip",
            "truncated": False,
        }
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
            output_limit=min(settings.analysis_floss_max_output_bytes, 16 * 1024 * 1024),
            memory_limit_bytes=min(
                max(settings.analysis_capa_max_memory_bytes, settings.smda_max_memory_bytes),
                1536 * 1024 * 1024,
            ),
            environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
        output_hash = hashlib.sha256(result.stdout).hexdigest()
        if result.status is not AnalysisSubprocessStatus.SUCCEEDED:
            output_limited = result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
            return {
                "status": "PARTIAL" if output_limited else result.status.value,
                "version": _distribution_version("flare-floss"),
                "output_sha256": output_hash,
                "output_bytes": len(result.stdout),
                "exit_code": result.exit_code,
                "truncated": output_limited,
                "available_reason": "floss_output_truncated" if output_limited else None,
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
    timeout = min(parameters.timeout_seconds, settings.analysis_yara_timeout_seconds)
    limits = YaraRunnerLimits(
        timeout_seconds=timeout,
        cpu_seconds=max(1, math.ceil(timeout)),
        memory_bytes=settings.analysis_capa_max_memory_bytes,
        maximum_output_bytes=min(parameters.max_output_bytes, 64 * 1024),
        maximum_source_bytes=settings.model_output_max_bytes,
        maximum_samples=1,
        maximum_sample_bytes=settings.analysis_max_sample_bytes,
        maximum_total_sample_bytes=settings.analysis_max_sample_bytes,
        maximum_strings_per_rule=settings.analysis_yara_max_strings_per_rule,
    )
    try:
        sample = YaraSampleInput(sample_sha256=parameters.artifact_sha256, data=payload)
        result = IsolatedYaraRunner(limits).match(source, (sample,))
        matched = parameters.artifact_sha256 in result.matches_sha256
        string_hits, unavailable_strings = _extract_yara_string_hits(source, payload)
        bounded_hits: list[dict[str, Any]] = []
        unavailable = set(unavailable_strings)
        total_offsets = 0
        truncated = False
        for hit in string_hits:
            remaining = _MAX_YARA_COVERAGE_OFFSETS - total_offsets
            if len(bounded_hits) >= _MAX_YARA_COVERAGE_HITS or remaining <= 0:
                unavailable.add(hit.identifier)
                truncated = True
                continue
            offsets = hit.offsets[:remaining]
            bounded_hits.append({"identifier": hit.identifier, "offsets": list(offsets)})
            total_offsets += len(offsets)
            if len(offsets) != len(hit.offsets):
                unavailable.add(hit.identifier)
                truncated = True
        return {
            "status": result.status.value,
            "version": result.engine_version,
            "matches": matched,
            "format_magic": detect_file_format_magic(payload),
            "string_hits": bounded_hits,
            "unavailable_strings": sorted(unavailable),
            "coverage_details_complete": not unavailable and not truncated,
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
        dissection_profile: tuple[str, ...] = ()
        if executable == "tshark":
            dissection_profile = _TSHARK_DISSECTION_PROFILES.get(executable, ())
            if not dissection_profile:
                dissection_profile = await _tshark_dissection_profile(
                    executable,
                    parameters=parameters,
                    settings=settings,
                )
                _TSHARK_DISSECTION_PROFILES[executable] = dissection_profile
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
                    *dissection_profile,
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


async def _tshark_dissection_profile(
    executable: str,
    *,
    parameters: ConfinedAnalysisJobParameters,
    settings: Settings,
) -> tuple[str, ...]:
    """Disable all protocols and enable only supported network dissectors."""
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    result = await run_analysis_subprocess(
        [executable, "-G", "protocols"],
        timeout_seconds=min(parameters.timeout_seconds, 5),
        output_limit=_TSHARK_PROFILE_MAX_BYTES,
        memory_limit_bytes=settings.analysis_capa_max_memory_bytes,
        environment=environment,
    )
    if (
        result.status is not AnalysisSubprocessStatus.SUCCEEDED
        or result.exit_code != 0
        or len(result.stdout) >= _TSHARK_PROFILE_MAX_BYTES
    ):
        raise RuntimeError("tshark_protocol_profile_unavailable")

    available = _tshark_report_network_protocols(result.stdout)
    if not {"frame", "eth", "ip", "ipv6", "tcp", "udp"} <= available:
        raise RuntimeError("tshark_protocol_profile_invalid")
    enabled = ",".join(sorted(available))
    return ("--disable-protocol", "ALL", "--enable-protocol", enabled)


def _tshark_report_network_protocols(output: bytes) -> frozenset[str]:
    try:
        lines = output.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("tshark_protocol_profile_invalid") from exc
    supported: set[str] = set()
    for line in lines:
        columns = line.split("\t")
        if len(columns) < 3:
            raise RuntimeError("tshark_protocol_profile_invalid")
        name = columns[2].strip()
        if name in _NETWORK_CAPTURE_DISSECTORS:
            supported.add(name)
    return frozenset(supported)


async def _run_local_command(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    settings: Settings,
) -> dict[str, Any]:
    command_id = _validate_local_command_arguments(
        parameters.arguments,
        parameters.analysis_parameters,
        max_range_bytes=settings.analysis_local_command_max_range_bytes,
    )
    file_type = detect_file_type(payload)
    if command_id == "readelf" and file_type is not LocalAnalysisFileType.ELF:
        return {"status": "POLICY_DENIED", "available_reason": "unsupported_file_type"}
    if command_id == "objdump" and file_type not in {
        LocalAnalysisFileType.ELF,
        LocalAnalysisFileType.PE,
    }:
        return {"status": "POLICY_DENIED", "available_reason": "unsupported_file_type"}
    if command_id == "monodis":
        if file_type is not LocalAnalysisFileType.PE:
            return {"status": "POLICY_DENIED", "available_reason": "unsupported_file_type"}
        if len(payload) > settings.analysis_local_monodis_max_sample_bytes:
            return {"status": "POLICY_DENIED", "available_reason": "sample_too_large"}
        return await _run_monodis(parameters, payload, settings)

    values = parameters.analysis_parameters
    if command_id in {"xxd", "sha256sum"} and "offset" in values:
        range_start = int(values["offset"])
        range_length = int(values["length"])
        if range_start + range_length > len(payload):
            return {"status": "POLICY_DENIED", "available_reason": "range_out_of_bounds"}

    executable = shutil.which(command_id)
    if executable is None:
        return {"status": "UNAVAILABLE", "available_reason": "analysis_tool_unavailable"}
    command = [executable]
    values = parameters.analysis_parameters
    if command_id == "file":
        command.append("--mime-type" if values["mime_type"] == "true" else "-b")
    elif command_id == "strings":
        command.extend(("-a", "-n", values["min_length"]))
    elif command_id == "xxd":
        command.extend(("-g", "1", "-s", values["offset"], "-l", values["length"]))
    elif command_id == "sha256sum":
        pass
    elif command_id == "readelf":
        command.append(
            {
                "header": "-h",
                "program_headers": "-l",
                "sections": "-S",
                "dynamic": "-d",
                "symbols": "-s",
            }[values["mode"]]
        )
    elif command_id == "objdump":
        mode = values["mode"]
        command.append(
            {
                "file_header": "-f",
                "sections": "-h",
                "private_headers": "-p",
                "disassemble": "-d",
            }[mode]
        )
        if mode == "disassemble":
            start = int(values["offset"])
            stop = start + int(values["length"])
            command.extend((f"--start-address=0x{start:x}", f"--stop-address=0x{stop:x}"))

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="cti-local-command-", dir="/tmp") as directory:
        sample_path = Path(directory) / "sample.bin"
        command_payload = payload
        if command_id == "sha256sum" and "offset" in values:
            start = int(values["offset"])
            command_payload = payload[start : start + int(values["length"])]
        sample_path.write_bytes(command_payload)
        sample_path.chmod(0o400)
        argv = [*command, str(sample_path)]
        output_limit = max(
            1, min(parameters.max_output_bytes, settings.local_analysis_max_output_bytes) - 4096
        )
        result = await run_analysis_subprocess(
            argv,
            timeout_seconds=min(
                parameters.timeout_seconds,
                settings.local_analysis_timeout_seconds,
            ),
            output_limit=output_limit,
            memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
            environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    stdout = result.stdout.decode("utf-8", "replace").replace(str(sample_path), "<sample>")
    stderr = result.stderr.decode("utf-8", "replace").replace(str(sample_path), "<sample>")
    truncated = result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
    if command_id == "strings" and result.status is AnalysisSubprocessStatus.SUCCEEDED:
        lines = stdout.splitlines()
        maximum = int(values["max_strings"])
        if len(lines) > maximum:
            stdout = "\n".join(lines[:maximum])
            truncated = True
    version_name = {
        "file": "file",
        "strings": "strings",
        "xxd": "xxd",
        "sha256sum": "sha256sum",
        "readelf": "readelf",
        "objdump": "objdump",
    }[command_id]
    return {
        "status": result.status.value,
        "version": _local_binary_version(command_id, "--version"),
        "return_code": result.exit_code,
        "duration_seconds": max(0.0, time.monotonic() - started),
        "stdout": sanitize_evidence_text(stdout, max_chars=output_limit),
        "stderr": sanitize_evidence_text(stderr, max_chars=min(output_limit, 2048)),
        "normalized_command": [
            version_name,
            *command[1:],
            "<sample>",
        ],
        "truncated": truncated,
    }


async def _run_monodis(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    settings: Settings,
) -> dict[str, Any]:
    executable = shutil.which("monodis")
    if executable is None:
        return {"status": "UNAVAILABLE", "available_reason": "monodis_binary_unavailable"}
    package_version = _monodis_package_version()
    if package_version is None:
        return {"status": "UNAVAILABLE", "available_reason": "monodis_package_version_unavailable"}
    if package_version != settings.analysis_local_monodis_expected_version:
        return {
            "status": "UNAVAILABLE",
            "version": package_version,
            "available_reason": "monodis_package_version_unattested",
        }

    values = parameters.analysis_parameters
    operation = values["operation"]
    started = time.monotonic()
    process_truncated = False
    with tempfile.TemporaryDirectory(prefix="cti-monodis-", dir="/tmp") as directory:
        sample_path = Path(directory) / "sample.bin"
        sample_path.write_bytes(payload)
        sample_path.chmod(0o400)
        timeout = min(
            parameters.timeout_seconds,
            settings.analysis_local_monodis_timeout_seconds,
        )
        output_limit = settings.analysis_local_monodis_max_output_bytes
        if operation == "cil":
            method_table = await run_analysis_subprocess(
                [executable, "--method", str(sample_path)],
                timeout_seconds=timeout,
                output_limit=output_limit,
                memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if method_table.status is AnalysisSubprocessStatus.TIMED_OUT:
                return {
                    "status": "TIMEOUT",
                    "version": package_version,
                    "available_reason": "monodis_method_table_timeout",
                }
            if method_table.status is not AnalysisSubprocessStatus.SUCCEEDED:
                return {
                    "status": "PARTIAL"
                    if method_table.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                    else "FAILED",
                    "version": package_version,
                    "available_reason": "monodis_method_table_output_truncated"
                    if method_table.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                    else "monodis_method_table_failed",
                    "truncated": method_table.status is AnalysisSubprocessStatus.OUTPUT_LIMIT,
                    "stderr": _sanitize_local_command_detail(method_table.stderr, sample_path),
                }
            methods = _parse_monodis_method_table(method_table.stdout.decode("utf-8", "replace"))
            selector_name = values.get("method_name")
            selector_rid = values.get("method_rid")
            if selector_name is not None:
                matches = [
                    rid
                    for rid, (type_name, method_name) in methods.items()
                    if selector_name == method_name
                    or selector_name == f"{type_name.lstrip('.')}.{method_name}"
                ]
                if len(matches) != 1:
                    return {
                        "status": "UNAVAILABLE" if not matches else "POLICY_DENIED",
                        "version": package_version,
                        "available_reason": "monodis_method_not_found"
                        if not matches
                        else "monodis_method_name_ambiguous_use_rid",
                    }
                method_rid = matches[0]
            else:
                method_rid = int(selector_rid or "0")
                if method_rid not in methods:
                    return {
                        "status": "UNAVAILABLE",
                        "version": package_version,
                        "available_reason": "monodis_method_rid_not_found",
                    }
            disassembly = await run_analysis_subprocess(
                [executable, "--show-method-tokens", str(sample_path)],
                timeout_seconds=timeout,
                output_limit=output_limit,
                memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            disassembly_text = disassembly.stdout.decode("utf-8", "replace")
            body = _extract_monodis_method_body(disassembly_text, method_rid)
            if body is None:
                truncated = disassembly.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                timed_out = disassembly.status is AnalysisSubprocessStatus.TIMED_OUT
                stderr_detail = _sanitize_local_command_detail(disassembly.stderr, sample_path)
                return {
                    "status": "PARTIAL" if truncated else "TIMEOUT" if timed_out else "FAILED",
                    "version": package_version,
                    "available_reason": "monodis_output_truncated_before_method"
                    if truncated
                    else "monodis_method_disassembly_timeout"
                    if timed_out
                    else "monodis_method_body_missing"
                    + (f": {stderr_detail}" if stderr_detail else ""),
                    "truncated": truncated,
                    "stderr": stderr_detail,
                }
            if disassembly.status in {
                AnalysisSubprocessStatus.FAILED_TO_START,
                AnalysisSubprocessStatus.NON_ZERO_EXIT,
            }:
                stderr_detail = _sanitize_local_command_detail(disassembly.stderr, sample_path)
                return {
                    "status": "FAILED",
                    "version": package_version,
                    "available_reason": "monodis_disassembly_failed"
                    + (f": {stderr_detail}" if stderr_detail else ""),
                    "stderr": stderr_detail,
                    "truncated": True,
                }
            raw_output = body.encode("utf-8")
            method_type, method_name = methods[method_rid]
            selector = f"{method_type.lstrip('.')}.{method_name}"
            command = [executable, "--show-method-tokens", str(sample_path)]
            detail = {
                "operation": "cil",
                "method_rid": method_rid,
                "method": selector,
                "scan_status": disassembly.status.value,
            }
            process_status = AnalysisSubprocessStatus.SUCCEEDED
            stderr = disassembly.stderr
            exit_code = disassembly.exit_code
        else:
            option = {
                "userstrings": "--userstrings",
                "typedef": "--typedef",
                "methods": "--method",
            }[operation]
            command = [executable, option, str(sample_path)]
            execution = await run_analysis_subprocess(
                command,
                timeout_seconds=timeout,
                output_limit=output_limit,
                memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            raw_output = execution.stdout
            process_status = execution.status
            process_truncated = execution.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
            stderr = execution.stderr
            exit_code = execution.exit_code
            detail = {"operation": operation}

    offset = int(values.get("offset", "0"))
    requested_length = (
        int(values["length"]) if "length" in values else LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES
    )
    output_complete = process_status is AnalysisSubprocessStatus.SUCCEEDED and not process_truncated
    if offset > len(raw_output):
        if not output_complete:
            incomplete_status = (
                "PARTIAL"
                if process_truncated
                else "TIMEOUT"
                if process_status is AnalysisSubprocessStatus.TIMED_OUT
                else "FAILED"
            )
            return {
                "status": incomplete_status,
                "version": package_version,
                "available_reason": "monodis_output_offset_beyond_scan_limit",
                "truncated": True,
                "offset": offset,
                "length": 0,
                "total_bytes_observed": len(raw_output),
                "total_bytes_complete": False,
                **detail,
            }
        return {
            "status": "POLICY_DENIED",
            "version": package_version,
            "available_reason": "monodis_output_offset_out_of_bounds",
        }
    output_bytes = raw_output[offset : offset + requested_length]
    truncated = not output_complete or offset + len(output_bytes) < len(raw_output)
    status = (
        "TIMEOUT"
        if process_status is AnalysisSubprocessStatus.TIMED_OUT
        else "PARTIAL"
        if process_truncated
        else "FAILED"
        if process_status is not AnalysisSubprocessStatus.SUCCEEDED
        else "SUCCEEDED"
    )
    safe_output = _sanitize_local_command_detail(output_bytes, None)
    result: dict[str, Any] = {
        "status": status,
        "version": package_version,
        "return_code": exit_code,
        "duration_seconds": max(0.0, time.monotonic() - started),
        "stdout": safe_output,
        "stderr": _sanitize_local_command_detail(stderr, None),
        "normalized_command": ["monodis", *command[1:-1], "<sample>"],
        "truncated": truncated,
        "offset": offset,
        "length": len(output_bytes),
        "total_bytes_observed": len(raw_output),
        "total_bytes_complete": output_complete,
        **detail,
    }
    if process_truncated:
        result["available_reason"] = "monodis_output_truncated"
    elif process_status is AnalysisSubprocessStatus.TIMED_OUT:
        result["available_reason"] = "monodis_timeout"
    elif process_status is not AnalysisSubprocessStatus.SUCCEEDED:
        stderr_detail = result["stderr"]
        result["available_reason"] = (
            f"monodis_{process_status.value.lower()}: {stderr_detail}"
            if stderr_detail
            else f"monodis_{process_status.value.lower()}"
        )[:1200]
    elif truncated:
        result["available_reason"] = "monodis_output_page_truncated"
    return result


def _monodis_package_version() -> str | None:
    executable = shutil.which("dpkg-query")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-W", "-f=${Version}", "mono-utils"],
            check=False,
            capture_output=True,
            timeout=2,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = result.stdout.decode("ascii", "ignore").strip()
    return (
        version if result.returncode == 0 and _MONODIS_PACKAGE_VERSION.fullmatch(version) else None
    )


def _parse_monodis_method_table(output: str) -> dict[int, tuple[str, str]]:
    methods: dict[int, tuple[str, str]] = {}
    current_type = ""
    for line in output.splitlines():
        if line.startswith("########## "):
            current_type = line.removeprefix("########## ").strip()
            continue
        match = _MONODIS_METHOD_ROW.match(line)
        if match is None or not current_type:
            continue
        method_name = match.group(2).strip("'")
        methods[int(match.group(1))] = (current_type, method_name)
    return methods


def _extract_monodis_method_body(output: str, rid: int) -> str | None:
    lines = output.splitlines(keepends=True)
    marker = f"// method line {rid}"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == marker)
    except StopIteration:
        return None
    for end in range(start + 1, len(lines)):
        if lines[end].lstrip().startswith("} // end of method "):
            return "".join(lines[start : end + 1])
        if lines[end].startswith("// method line "):
            return None
    return None


def _sanitize_local_command_detail(value: bytes, sample_path: Path | None) -> str:
    text = value.decode("utf-8", "replace")
    if sample_path is not None:
        text = text.replace(str(sample_path), "<sample>")
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s\"'<>:]+/?)+", "<path>", text)
    return sanitize_evidence_text(text, max_chars=8192)


def _validate_local_command_arguments(
    arguments: Sequence[str],
    parameters: Mapping[str, str],
    *,
    max_range_bytes: int,
) -> str:
    if len(arguments) != 1 or arguments[0] not in {
        "file",
        "strings",
        "xxd",
        "sha256sum",
        "readelf",
        "objdump",
        "monodis",
    }:
        raise ValueError("local command identifier is not allowlisted")
    command_id = arguments[0]
    if command_id == "file":
        if set(parameters) != {"mime_type"} or parameters["mime_type"] not in {"true", "false"}:
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
            if not _bounded_decimal(
                parameters["length"],
                1,
                min(max_range_bytes, LOCAL_COMMAND_MAX_RANGE_BYTES),
            ):
                raise ValueError("sha256sum range length is invalid")
            if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("sha256sum range exceeds the address limit")
    elif command_id == "monodis":
        if set(parameters) - {"operation", "method_name", "method_rid", "offset", "length"}:
            raise ValueError("monodis command parameters are invalid")
        operation = parameters.get("operation")
        if operation not in {"userstrings", "typedef", "methods", "cil"}:
            raise ValueError("monodis operation is invalid")
        has_name = "method_name" in parameters
        has_rid = "method_rid" in parameters
        if (
            has_name
            and re.fullmatch(r"[A-Za-z_$<>][A-Za-z0-9_.$<>+`-]{0,127}", parameters["method_name"])
            is None
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
        if operation == "cil":
            if has_name == has_rid:
                raise ValueError("monodis CIL requires a method name or RID")
        elif has_name or has_rid:
            raise ValueError("monodis selectors are allowed only for CIL")
        if has_offset:
            if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                raise ValueError("monodis output offset is invalid")
            if not _bounded_decimal(
                parameters["length"], 1, min(max_range_bytes, LOCAL_COMMAND_MAX_MONODIS_RANGE_BYTES)
            ):
                raise ValueError("monodis output range length is invalid")
            if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("monodis output range exceeds the address limit")
    else:
        mode = parameters.get("mode")
        if mode not in {"file_header", "sections", "private_headers", "disassemble"}:
            raise ValueError("objdump command mode is invalid")
        if mode == "disassemble":
            if set(parameters) != {"mode", "offset", "length"}:
                raise ValueError("objdump disassembly requires a bounded range")
            if not _bounded_decimal(parameters["offset"], 0, 0xFFFFFFFFFFFFFFFF):
                raise ValueError("objdump range offset is invalid")
            if not _bounded_decimal(parameters["length"], 1, max_range_bytes):
                raise ValueError("objdump range length is invalid")
            if int(parameters["offset"]) + int(parameters["length"]) > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("objdump range exceeds the address limit")
        elif set(parameters) != {"mode"}:
            raise ValueError("objdump command parameters are invalid")
    return command_id


def _bounded_decimal(value: str, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and minimum <= int(value) <= maximum
    )


async def _run_suricata(
    parameters: ConfinedAnalysisJobParameters,
    payload: bytes,
    *,
    blobs: BlobCatalogService,
    settings: Settings,
) -> dict[str, Any]:
    executable = shutil.which("suricata")
    if executable is None:
        return {"status": "unavailable", "available_reason": "suricata_binary_unavailable"}
    version_result = _probe_suricata_version(executable)
    if version_result is None:
        return {"status": "unavailable", "available_reason": "suricata_version_probe_failed"}

    candidate = parameters.analysis_parameters
    if candidate:
        if set(candidate) != {"candidate_id", "candidate_version", "rule_source"}:
            return {"status": "POLICY_DENIED", "available_reason": "suricata_candidate_invalid"}
        try:
            rules_source = validate_suricata_rule_source(
                candidate["rule_source"],
                candidate_id=candidate["candidate_id"],
                candidate_version=candidate["candidate_version"],
                max_bytes=settings.model_output_max_bytes,
            )
        except ValueError as exc:
            return {"status": "POLICY_DENIED", "available_reason": str(exc)}
        rule_sha256 = hashlib.sha256(rules_source.encode("utf-8")).hexdigest()
        candidate_id = candidate["candidate_id"]
        candidate_version = candidate["candidate_version"]
    else:
        candidate_id = None
        candidate_version = None
        rules_path = settings.analysis_suricata_rules_path / "autowork.rules"
        try:
            if rules_path.is_symlink() or not rules_path.is_file():
                raise OSError("ruleset_missing")
            rules_source = rules_path.read_text(encoding="utf-8")
            validate_suricata_rule_source(
                rules_source,
                max_bytes=settings.model_output_max_bytes,
            )
            rule_sha256 = hashlib.sha256(rules_source.encode("utf-8")).hexdigest()
        except (OSError, UnicodeError, ValueError):
            return {
                "status": "unavailable",
                "available_reason": "suricata_ruleset_unavailable_or_invalid",
            }
        if rule_sha256 != settings.analysis_suricata_ruleset_sha256:
            return {"status": "unavailable", "available_reason": "suricata_ruleset_hash_mismatch"}

    operation = (
        "validate_rules"
        if parameters.tool is ConfinedAnalysisTool.SURICATA_VALIDATE_RULES
        else "replay"
    )
    if operation == "replay":
        try:
            parsed_capture = read_capture(payload, max_packets=1)
        except PcapReaderError:
            parsed_capture = None
        if parsed_capture is not None and not parsed_capture.packets:
            empty_result: dict[str, Any] = {
                "status": "SUCCEEDED",
                "mode": operation,
                "version": version_result,
                "rule_sha256": rule_sha256,
                "ruleset_revision": (
                    f"{candidate_id}:{candidate_version}"
                    if candidate_id is not None
                    else settings.analysis_suricata_ruleset_revision
                ),
                "duration_ms": 0,
                "exit_code": 0,
                "stderr": "",
                "empty_capture": True,
                "capture_packet_count": 0,
                "event_counts": {name: 0 for name in ("alert", "flow", "dns", "tls")},
                "engine_stats": {},
                "events": [],
                "invalid_eve_lines": 0,
                "truncated": False,
            }
            if candidate_id is not None:
                empty_result["candidate_id"] = candidate_id
                empty_result["candidate_version"] = candidate_version
            return empty_result
    candidate_rule_blob_id: str | None = None
    if candidate_id is not None:
        rule_record = await blobs.ingest(
            BytesIO(rules_source.encode("utf-8")),
            logical_bucket="confined-analysis-rules",
            mime_type="text/plain; charset=utf-8",
        )
        if rule_record.descriptor.sha256 != rule_sha256:
            raise RuntimeError("suricata_candidate_rule_blob_hash_mismatch")
        candidate_rule_blob_id = str(rule_record.id)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="cti-suricata-", dir="/tmp") as temporary:
        root = Path(temporary)
        output_dir = root / "output"
        output_dir.mkdir(mode=0o700)
        config_path = root / "suricata-minimal.yaml"
        classification_path = root / "classification.config"
        classification_path.write_text(
            "config classification: autowork,AutoWork Offline Alert,3\n", encoding="ascii"
        )
        reference_path = root / "reference.config"
        reference_path.write_text("", encoding="ascii")
        classification_path.chmod(0o400)
        reference_path.chmod(0o400)
        config_path.write_text(
            _minimal_suricata_config(
                classification_file=classification_path,
                reference_file=reference_path,
            ),
            encoding="ascii",
        )
        rule_file = root / "candidate.rules"
        rule_file.write_text(rules_source, encoding="utf-8")
        rule_file.chmod(0o400)
        pcap_path = root / "capture.pcap"
        if operation == "replay":
            pcap_path.write_bytes(payload)
            pcap_path.chmod(0o400)
            command = [
                executable,
                "-r",
                str(pcap_path),
                "-c",
                str(config_path),
                "-S",
                str(rule_file),
                "-l",
                str(output_dir),
                "-k",
                "none",
                "--runmode",
                "single",
            ]
        else:
            command = [
                executable,
                "-T",
                "-c",
                str(config_path),
                "-S",
                str(rule_file),
                "-l",
                str(output_dir),
                "--runmode",
                "single",
            ]
        try:
            result = await run_analysis_subprocess(
                command,
                timeout_seconds=min(
                    parameters.timeout_seconds, settings.analysis_suricata_timeout_seconds
                ),
                output_limit=min(parameters.max_output_bytes, 256 * 1024),
                memory_limit_bytes=settings.analysis_capa_max_memory_bytes,
                environment={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
            )
        except Exception as exc:
            return {
                "status": "FAILED",
                "error_type": type(exc).__name__,
                "version": version_result,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }

        stderr = _sanitize_suricata_stderr(result.stderr)
        response: dict[str, Any] = {
            "status": "SUCCEEDED"
            if result.status is AnalysisSubprocessStatus.SUCCEEDED and result.exit_code == 0
            else "PARTIAL"
            if result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
            else "TIMED_OUT"
            if result.status is AnalysisSubprocessStatus.TIMED_OUT
            else "FAILED",
            "mode": operation,
            "version": version_result,
            "rule_sha256": rule_sha256,
            "ruleset_revision": (
                f"{candidate_id}:{candidate_version}"
                if candidate_id is not None
                else settings.analysis_suricata_ruleset_revision
            ),
            "duration_ms": min(600_000, max(0, round((time.monotonic() - started) * 1000))),
            "exit_code": result.exit_code,
            "stderr": stderr,
        }
        if candidate_id is not None:
            response["candidate_id"] = candidate_id
            response["candidate_version"] = candidate_version
            response["candidate_rule_blob_id"] = candidate_rule_blob_id
        if operation == "replay":
            eve_path = output_dir / "eve.json"
            try:
                eve_content, eve_truncated = _read_bounded_file(
                    eve_path, settings.analysis_suricata_max_eve_bytes
                )
            except OSError:
                return {**response, "status": "FAILED", "available_reason": "suricata_eve_missing"}
            if not eve_content:
                return {**response, "status": "FAILED", "available_reason": "suricata_eve_empty"}
            eve_record = await blobs.ingest(
                BytesIO(eve_content),
                logical_bucket="confined-analysis-eve",
                mime_type="application/x-ndjson",
            )
            eve_sha256 = hashlib.sha256(eve_content).hexdigest()
            if eve_record.descriptor.sha256 != eve_sha256:
                raise RuntimeError("suricata_eve_blob_hash_mismatch")
            summary, event_truncated = _summarize_suricata_eve(
                eve_content, max_events=settings.analysis_suricata_max_events
            )
            response.update(
                {
                    "eve_blob_id": str(eve_record.id),
                    "eve_sha256": eve_sha256,
                    "eve_size_bytes": len(eve_content),
                    "truncated": eve_truncated or event_truncated,
                    **summary,
                }
            )
            if eve_truncated and response["status"] == "SUCCEEDED":
                response["status"] = "PARTIAL"
        return response


def _minimal_suricata_config(
    *, classification_file: Path | None = None, reference_file: Path | None = None
) -> str:
    classification_path = classification_file or Path(
        "/tmp/autowork-suricata-classification.config"
    )
    reference_path = reference_file or Path("/tmp/autowork-suricata-reference.config")
    return (
        """%YAML 1.1
---
vars:
  address-groups:
    HOME_NET: \"[10.0.0.0/8,172.16.0.0/12,192.168.0.0/16]\"
    EXTERNAL_NET: \"!$HOME_NET\"
  port-groups:
    HTTP_PORTS: \"80\"
default-rule-path: /tmp
rule-files: []
classification-file: """
        + str(classification_path)
        + """
reference-config-file: """
        + str(reference_path)
        + """
stats:
  enabled: yes
  interval: 8
outputs:
  - eve-log:
      enabled: yes
      filetype: regular
      filename: eve.json
      types:
        - alert
        - flow
        - dns
        - http
        - tls
        - stats:
            totals: yes
            threads: no
"""
    )


def _probe_suricata_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "-V"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _SURICATA_VERSION.search(result.stdout + result.stderr)
    return match.group(1) if result.returncode == 0 and match else None


def _sanitize_suricata_stderr(stderr: bytes) -> str:
    text = sanitize_evidence_text(stderr.decode("utf-8", "replace"), max_chars=2_000)
    return re.sub(r"/tmp/cti-suricata-[^\s:/]+", "[temporary-path]", text)


def _bounded_jsonl(content: bytes, max_bytes: int) -> tuple[bytes, bool]:
    if len(content) <= max_bytes:
        return content, False
    bounded = content[:max_bytes]
    last_newline = bounded.rfind(b"\n")
    return (bounded[: last_newline + 1], True) if last_newline >= 0 else (b"", True)


def _read_bounded_file(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    with path.open("rb") as source:
        content = source.read(max_bytes + 1)
    return _bounded_jsonl(content, max_bytes)


def _summarize_suricata_eve(content: bytes, *, max_events: int) -> tuple[dict[str, Any], bool]:
    counts = {name: 0 for name in ("alert", "flow", "dns", "tls")}
    events: list[dict[str, Any]] = []
    engine_stats: dict[str, int | float] = {}
    invalid_lines = 0
    for line in content.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid_lines += 1
            continue
        if not isinstance(event, dict):
            invalid_lines += 1
            continue
        event_type = event.get("event_type")
        if event_type in counts:
            counts[event_type] += 1
        if event_type == "stats" and isinstance(event.get("stats"), dict):
            _collect_numeric_stats(event["stats"], engine_stats)
        if len(events) < max_events:
            sanitized = _sanitize_suricata_event(event)
            if sanitized:
                events.append(sanitized)
    return (
        {
            "event_counts": counts,
            "engine_stats": engine_stats,
            "events": events,
            "invalid_eve_lines": invalid_lines,
        },
        sum(counts.values()) > len(events),
    )


def _sanitize_suricata_event(event: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "timestamp",
        "event_type",
        "src_ip",
        "src_port",
        "dest_ip",
        "dest_port",
        "proto",
        "app_proto",
        "flow_id",
    )
    sanitized: dict[str, Any] = {}
    for key in allowed:
        value = event.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            sanitized[key] = sanitize_evidence_text(str(value), max_chars=128)
    for group, keys in (
        ("alert", ("signature", "signature_id", "category", "severity")),
        ("dns", ("rrname", "rrtype", "type")),
        ("tls", ("sni", "version")),
        ("http", ("hostname", "method", "status")),
    ):
        values = event.get(group)
        if isinstance(values, dict):
            picked = {
                key: sanitize_evidence_text(str(values[key]), max_chars=128)
                for key in keys
                if isinstance(values.get(key), (str, int, float))
                and not isinstance(values.get(key), bool)
            }
            if picked:
                sanitized[group] = picked
    return sanitized


def _collect_numeric_stats(value: Any, output: dict[str, int | float], prefix: str = "") -> None:
    if len(output) >= 64 or not isinstance(value, dict):
        return
    for key, item in value.items():
        if len(output) >= 64:
            break
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            _collect_numeric_stats(item, output, name[:128])
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            output[name[:128]] = item


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
            "mode",
            "rule_sha256",
            "ruleset_revision",
            "candidate_id",
            "candidate_version",
            "candidate_rule_blob_id",
            "duration_ms",
            "eve_blob_id",
            "eve_sha256",
            "eve_size_bytes",
            "event_counts",
            "engine_stats",
            "invalid_eve_lines",
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
    ilspy_version, ilspy_package_sha512 = probe_ilspycmd(settings)
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
        "local_file": _local_binary_version("file", "--version"),
        "local_strings": _local_binary_version("strings", "--version"),
        "local_readelf": _local_binary_version("readelf", "--version"),
        "local_objdump": _local_binary_version("objdump", "--version"),
        "local_xxd": _local_binary_version("xxd", "-v"),
        "local_sha256sum": _local_binary_version("sha256sum", "--version"),
        "local_7z": _seven_zip_version(),
        "zip_members": "autowork-zip-members-v1",
        "local_monodis": _monodis_package_version() if shutil.which("monodis") else None,
        "ilspycmd": ilspy_version,
        "ilspycmd_package_sha512": ilspy_package_sha512,
        "suricata": _probe_suricata_version(shutil.which("suricata") or "suricata"),
        "suricata_ruleset_revision": _read_marker(
            settings.analysis_suricata_rules_path / "AUTOWORK_RULESET_REVISION"
        ),
        "suricata_ruleset_sha256": _file_sha256(
            settings.analysis_suricata_rules_path / "autowork.rules",
            max_bytes=settings.model_output_max_bytes,
        ),
        "floss": (
            _distribution_version("flare-floss") if shutil.which("floss") is not None else None
        ),
        "mwcp": _distribution_version("mwcp"),
        "yara": _distribution_version("yara-python"),
    }
    registered_mwcp_parsers = _registered_mwcp_parsers(os.environ.get("ANALYSIS_MWCP_PARSER_DIR"))
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
            "local_command",
            "zip_members",
            "archive_tools",
            "suricata",
            "floss",
            "mwcp",
            "yara",
            "dotnet",
        ],
        "versions": versions,
    }


@functools.cache
def _registered_mwcp_parsers(parser_directory: str | None) -> frozenset[str]:
    executable = shutil.which("mwcp")
    if executable is None:
        return frozenset()
    command = [executable]
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


_P7ZIP_VERSION = re.compile(r"\bp7zip Version ([0-9]+(?:\.[0-9]+)+)\b")


def _seven_zip_version() -> str | None:
    """Report the p7zip package version (the banner may also print an unrelated 7-Zip build)."""
    executable = shutil.which("7z")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "i"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _P7ZIP_VERSION.search((result.stdout + result.stderr)[:4096])
    if result.returncode == 0 and match:
        return match.group(1)
    return _local_binary_version("7z", "i")


@functools.cache
def _local_binary_version(name: str, argument: str) -> str | None:
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
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout + result.stderr)[:4096]
    match = _LOCAL_VERSION.search(output)
    return match.group(1) if result.returncode == 0 and match else None


def _read_marker(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if value else None


def _file_sha256(path: Path, *, max_bytes: int) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


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
