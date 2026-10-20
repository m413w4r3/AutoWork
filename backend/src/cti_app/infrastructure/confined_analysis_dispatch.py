from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.confined_analysis import (
    CONFINED_ANALYSIS_JOB_KIND,
    CONFINED_ANALYSIS_RESULT_SCHEMA,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
    ConfinedAnalysisWorkerAttestation,
    create_confined_analysis_job_registry,
    deterministic_confined_analysis_key,
)
from cti_app.application.dotnet_analysis import ILSPYCMD_PACKAGE_SHA512, ILSPYCMD_VERSION
from cti_app.application.jobs import DuplicateJobError, JobService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.jobs import JobStatus
from cti_app.infrastructure.jobs import DramatiqAnalysisJobDispatcher

_PROBE_KEY_PREFIX = "autowork:analysis-worker-probe:"
_PROBE_RESULT_MAX_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_REFERENCE = re.compile(
    r"^confined-analysis-result://(?P<blob>[0-9a-f-]{36})/(?P<sha>[0-9a-f]{64})$"
)


class DramatiqConfinedAnalysisExecutionPort(ConfinedAnalysisExecutionPort):
    """Submit durable analysis jobs and resolve their canonical MinIO result artefacts."""

    def __init__(
        self,
        *,
        settings: Settings,
        uow_factory: UnitOfWorkFactory,
        blob_catalog: BlobCatalogService,
        attestation: ConfinedAnalysisWorkerAttestation,
        dispatcher: Any | None = None,
        job_service: JobService | None = None,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._blobs = blob_catalog
        self._attestation = attestation
        self._dispatcher = dispatcher or DramatiqAnalysisJobDispatcher()
        self._job_service = job_service or JobService(
            uow_factory, create_confined_analysis_job_registry()
        )
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def available(self) -> bool:
        return (
            self._attestation.reachable
            and self._attestation.confined
            and self._attestation.dependencies_ready
        )

    @property
    def available_reason(self) -> str | None:
        if self.available:
            return None
        return self._attestation.available_reason or "analysis_worker_unresponsive"

    @property
    def family_statuses(self) -> Mapping[str, ConfinedAnalysisFamilyStatus]:
        return self._attestation.families

    @property
    def isolation_profile(self) -> str | None:
        return self._attestation.isolation_profile

    @property
    def attestation(self) -> ConfinedAnalysisWorkerAttestation:
        return self._attestation

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        operation_key = deterministic_confined_analysis_key(
            request, worker_versions=self._attestation.versions
        )
        family = (
            "dotnet"
            if request.tool is ConfinedAnalysisTool.DOTNET_ILSPY
            else "suricata"
            if request.tool
            in {
                ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
                ConfinedAnalysisTool.SURICATA_REPLAY,
            }
            else request.tool.value
        )
        if request.tool.value == "mwcp":
            parser_id = request.analysis_parameters.get("parser_id", "")
            family = f"mwcp_parser:{parser_id}"
        family_status = self._attestation.families.get(family)
        if family_status is None or not family_status.available:
            reason = (
                family_status.available_reason
                if family_status is not None
                else "mwcp_parser_not_attested"
                if request.tool.value == "mwcp"
                else "analysis_family_not_attested"
            )
            return self._unavailable(request, operation_key, reason)
        if not self.available:
            return self._unavailable(request, operation_key, self.available_reason)
        if request.timeout_seconds > self._settings.analysis_worker_job_timeout_seconds:
            return ConfinedAnalysisResult(
                ToolInvocationStatus.POLICY_DENIED,
                operation_key,
                request.artifact_id,
                request.sha256,
                available_reason="analysis_budget_exceeded",
            )
        if request.max_output_bytes > self._settings.analysis_worker_result_max_bytes:
            return ConfinedAnalysisResult(
                ToolInvocationStatus.POLICY_DENIED,
                operation_key,
                request.artifact_id,
                request.sha256,
                available_reason="analysis_output_budget_exceeded",
            )

        parameters = ConfinedAnalysisJobParameters(
            investigation_id=request.investigation_id,
            association_id=request.association_id,
            sample_id=request.sample_id,
            artifact_id=request.artifact_id,
            artifact_sha256=request.sha256,
            artifact_kind=request.artifact_kind,
            operation_key=operation_key,
            tool=request.tool,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            arguments=request.arguments,
            analysis_parameters=dict(request.analysis_parameters),
        )
        try:
            job = await self._job_service.submit(
                kind=CONFINED_ANALYSIS_JOB_KIND,
                aggregate_type="sample",
                aggregate_id=request.sample_id or request.artifact_id,
                idempotency_key=operation_key,
                correlation_id=str(request.investigation_id),
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=1,
                actor_id="system:confined-analysis",
            )
        except DuplicateJobError as exc:
            job = await self._job_service.get(exc.existing_job_id)
        except Exception:
            return self._unavailable(request, operation_key, "analysis_job_submission_failed")
        else:
            try:
                await self._dispatcher.dispatch(job.id)
            except Exception:
                # The durable row is retained. An ambiguous broker handoff is never re-submitted.
                return self._unavailable(request, operation_key, "analysis_worker_dispatch_failed")

        deadline = self._monotonic() + request.timeout_seconds
        while True:
            try:
                current = await self._job_service.get(job.id)
            except Exception:
                return self._unavailable(request, operation_key, "analysis_job_state_unavailable")
            if current.status is JobStatus.SUCCEEDED:
                return await self._read_result(request, operation_key, current.output_reference)
            if current.status in {
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.WAITING_HUMAN,
            }:
                return ConfinedAnalysisResult(
                    ToolInvocationStatus.FAILED,
                    operation_key,
                    request.artifact_id,
                    request.sha256,
                    available_reason="analysis_worker_job_failed",
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._unavailable(request, operation_key, "analysis_worker_job_timeout")
            await self._sleep(min(self._settings.analysis_worker_poll_interval_seconds, remaining))

    async def _read_result(
        self,
        request: ConfinedAnalysisRequest,
        operation_key: str,
        reference: str | None,
    ) -> ConfinedAnalysisResult:
        match = _OUTPUT_REFERENCE.fullmatch(reference or "")
        if match is None:
            return ConfinedAnalysisResult(
                ToolInvocationStatus.FAILED,
                operation_key,
                request.artifact_id,
                request.sha256,
                available_reason="analysis_result_reference_invalid",
            )
        result_blob_id = UUID(match.group("blob"))
        result_sha256 = match.group("sha")
        try:
            payload_bytes = await self._blobs.read(
                result_blob_id,
                max_bytes=self._settings.analysis_worker_result_max_bytes,
            )
            if hashlib.sha256(payload_bytes).hexdigest() != result_sha256:
                raise ValueError("result_hash_mismatch")
            result_payload = json.loads(payload_bytes)
        except Exception:
            return ConfinedAnalysisResult(
                ToolInvocationStatus.FAILED,
                operation_key,
                request.artifact_id,
                request.sha256,
                result_blob_id=result_blob_id,
                result_sha256=result_sha256,
                available_reason="analysis_result_integrity_failed",
            )
        if (
            not isinstance(result_payload, dict)
            or result_payload.get("schema") != CONFINED_ANALYSIS_RESULT_SCHEMA
            or result_payload.get("operation_key") != operation_key
            or result_payload.get("artifact_id") != str(request.artifact_id)
            or result_payload.get("artifact_kind") != request.artifact_kind.value
            or result_payload.get("artifact_sha256") != request.sha256
            or result_payload.get("tool") != request.tool.value
        ):
            return ConfinedAnalysisResult(
                ToolInvocationStatus.FAILED,
                operation_key,
                request.artifact_id,
                request.sha256,
                result_blob_id=result_blob_id,
                result_sha256=result_sha256,
                available_reason="analysis_result_identity_mismatch",
            )
        return ConfinedAnalysisResult(
            ToolInvocationStatus.SUCCEEDED,
            operation_key,
            request.artifact_id,
            request.sha256,
            result_blob_id=result_blob_id,
            result_sha256=result_sha256,
            payload=result_payload,
        )

    @staticmethod
    def _unavailable(
        request: ConfinedAnalysisRequest, operation_key: str, reason: str | None
    ) -> ConfinedAnalysisResult:
        return ConfinedAnalysisResult(
            ToolInvocationStatus.UNAVAILABLE,
            operation_key,
            request.artifact_id,
            request.sha256,
            available_reason=reason or "analysis_worker_unavailable",
        )


def build_confined_analysis_execution_port(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory,
    blob_catalog: BlobCatalogService,
    attestation: ConfinedAnalysisWorkerAttestation | None = None,
    dispatcher: Any | None = None,
) -> DramatiqConfinedAnalysisExecutionPort:
    if not settings.analysis_worker_enabled:
        attestation = ConfinedAnalysisWorkerAttestation(
            reachable=False,
            confined=False,
            isolation_profile=None,
            versions={},
            families=_unavailable_families("analysis_worker_disabled_by_configuration"),
            dependencies_ready=False,
            available_reason="analysis_worker_disabled_by_configuration",
        )
    elif attestation is None:
        attestation = probe_analysis_worker(settings)
    return DramatiqConfinedAnalysisExecutionPort(
        settings=settings,
        uow_factory=uow_factory,
        blob_catalog=blob_catalog,
        attestation=attestation,
        dispatcher=dispatcher,
    )


def probe_analysis_worker(settings: Settings) -> ConfinedAnalysisWorkerAttestation:
    """Ask the analysis queue for a bounded, versioned runtime attestation."""
    probe_id = uuid4().hex
    key = f"{_PROBE_KEY_PREFIX}{probe_id}"
    client: Any | None = None
    try:
        from cti_app.workers.analysis_tasks import probe_analysis_worker as probe_actor
        from cti_app.workers.broker import broker

        client = broker.client
        probe_actor.send(probe_id)
        deadline = time.monotonic() + settings.analysis_worker_probe_timeout_seconds
        while time.monotonic() < deadline:
            response = client.get(key)
            if response is not None:
                raw = response if isinstance(response, bytes) else str(response).encode()
                if len(raw) > _PROBE_RESULT_MAX_BYTES:
                    return _unavailable_attestation("analysis_worker_probe_response_too_large")
                return _attestation_from_response(raw, settings)
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except Exception:
        return _unavailable_attestation("analysis_worker_probe_unreachable")
    finally:
        try:
            if client is not None:
                client.delete(key)
        except Exception:
            pass
    return _unavailable_attestation("analysis_worker_unresponsive")


def _attestation_from_response(raw: bytes, settings: Settings) -> ConfinedAnalysisWorkerAttestation:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _unavailable_attestation("analysis_worker_probe_response_invalid")
    if not isinstance(payload, dict):
        return _unavailable_attestation("analysis_worker_probe_response_invalid")
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        return _unavailable_attestation("analysis_worker_probe_response_invalid")
    worker_confined = bool(
        payload.get("uid") == 10001
        and payload.get("no_new_privileges") is True
        and payload.get("effective_capabilities") == "0000000000000000"
        and payload.get("root_read_only") is True
        and payload.get("isolation_profile") == settings.analysis_worker_expected_isolation_profile
    )
    dependencies_ready = (
        payload.get("postgres_access") is True and payload.get("minio_access") is True
    )
    raw_supported = payload.get("supported_families")
    supported = (
        frozenset(item for item in raw_supported if isinstance(item, str) and len(item) <= 64)
        if isinstance(raw_supported, list)
        else frozenset()
    )
    families = _family_statuses(
        versions,
        worker_confined=worker_confined,
        dependencies_ready=dependencies_ready,
        supported_families=supported,
        settings=settings,
    )
    return ConfinedAnalysisWorkerAttestation(
        reachable=True,
        confined=worker_confined,
        isolation_profile=_safe_version(payload.get("isolation_profile")),
        versions={
            key: _safe_version(value)
            for key, value in versions.items()
            if key
            in {
                "static",
                "capa",
                "smda",
                "capa_ruleset_revision",
                "capa_ruleset_sha256",
                "tshark",
                "capinfos",
                "suricata",
                "suricata_ruleset_revision",
                "suricata_ruleset_sha256",
                "floss",
                "mwcp",
                "yara",
                "local_file",
                "local_strings",
                "local_readelf",
                "local_objdump",
                "local_xxd",
                "local_sha256sum",
                "ilspycmd",
                "ilspycmd_package_sha512",
            }
            or key.startswith("mwcp_parser:")
        },
        families=families,
        dependencies_ready=dependencies_ready,
        available_reason=(
            None
            if worker_confined and dependencies_ready
            else "analysis_worker_isolation_unattested"
            if not worker_confined
            else "analysis_worker_dependencies_unavailable"
        ),
    )


def _family_statuses(
    versions: Mapping[str, Any],
    *,
    worker_confined: bool,
    dependencies_ready: bool,
    supported_families: frozenset[str],
    settings: Settings,
) -> dict[str, ConfinedAnalysisFamilyStatus]:
    static_version = versions.get("static")
    capa_version = versions.get("capa")
    ruleset_revision = versions.get("capa_ruleset_revision")
    ruleset_sha256 = versions.get("capa_ruleset_sha256")
    smda_version = versions.get("smda")
    tshark_version = versions.get("tshark")
    capinfos_version = versions.get("capinfos")
    suricata_version = versions.get("suricata")
    yara_version = versions.get("yara")

    def status(
        family: str, version: Any, expected: str | None, *, reason: str | None = None
    ) -> ConfinedAnalysisFamilyStatus:
        safe_version = _safe_version(version)
        if not worker_confined:
            return ConfinedAnalysisFamilyStatus(
                family,
                False,
                safe_version,
                "analysis_worker_isolation_unattested",
            )
        if not dependencies_ready:
            return ConfinedAnalysisFamilyStatus(
                family, False, safe_version, "analysis_worker_dependencies_unavailable"
            )
        if reason is not None:
            return ConfinedAnalysisFamilyStatus(family, False, None, reason)
        if family not in supported_families:
            return ConfinedAnalysisFamilyStatus(
                family, False, safe_version, "analysis_family_not_supported_by_worker"
            )
        if safe_version is None:
            return ConfinedAnalysisFamilyStatus(family, False, None, "analysis_tool_unavailable")
        if expected is not None and safe_version != expected:
            return ConfinedAnalysisFamilyStatus(
                family, False, safe_version, "analysis_tool_version_unattested"
            )
        return ConfinedAnalysisFamilyStatus(family, True, safe_version)

    static = status("static", static_version, "autowork-static-v1")
    capa = status("capa", capa_version, settings.analysis_capa_expected_version)
    if capa.available and ruleset_revision != settings.analysis_capa_ruleset_revision:
        capa = ConfinedAnalysisFamilyStatus(
            "capa", False, str(capa_version), "analysis_capa_ruleset_unattested"
        )
    if capa.available and (
        not isinstance(ruleset_sha256, str) or _SHA256.fullmatch(ruleset_sha256) is None
    ):
        capa = ConfinedAnalysisFamilyStatus(
            "capa", False, str(capa_version), "analysis_capa_ruleset_unattested"
        )
    smda = status("smda", smda_version, settings.analysis_smda_expected_version)
    tshark = status("tshark", tshark_version, settings.analysis_tshark_expected_version)
    capinfos = status("capinfos", capinfos_version, settings.analysis_capinfos_expected_version)
    suricata = status("suricata", suricata_version, settings.analysis_suricata_expected_version)
    if suricata.available and (
        versions.get("suricata_ruleset_revision") != settings.analysis_suricata_ruleset_revision
        or versions.get("suricata_ruleset_sha256") != settings.analysis_suricata_ruleset_sha256
    ):
        suricata = ConfinedAnalysisFamilyStatus(
            "suricata", False, suricata.version, "analysis_suricata_ruleset_unattested"
        )
    floss = status("floss", versions.get("floss"), "3.1.1")
    mwcp = status("mwcp", versions.get("mwcp"), "3.16.1")
    dotnet = status("dotnet", versions.get("ilspycmd"), ILSPYCMD_VERSION)
    if dotnet.available and versions.get("ilspycmd_package_sha512") != ILSPYCMD_PACKAGE_SHA512:
        dotnet = ConfinedAnalysisFamilyStatus(
            "dotnet", False, dotnet.version, "ilspycmd_package_hash_unattested"
        )
    if (
        dotnet.available
        and versions.get("local_monodis") != settings.analysis_local_monodis_expected_version
    ):
        dotnet = ConfinedAnalysisFamilyStatus(
            "dotnet", False, dotnet.version, "monodis_package_version_unattested"
        )
    mwcp_parser_families: dict[str, ConfinedAnalysisFamilyStatus] = {}
    for parser_id, configured_version in settings.mwcp_parser_versions.items():
        family_name = f"mwcp_parser:{parser_id}"
        attested_version = _safe_version(versions.get(family_name))
        if not mwcp.available:
            parser_family = ConfinedAnalysisFamilyStatus(
                family_name, False, attested_version, mwcp.available_reason
            )
        elif attested_version is None:
            parser_family = ConfinedAnalysisFamilyStatus(
                family_name, False, None, "mwcp_parser_unavailable_or_unregistered"
            )
        elif attested_version != configured_version:
            parser_family = ConfinedAnalysisFamilyStatus(
                family_name, False, attested_version, "mwcp_parser_version_unattested"
            )
        else:
            parser_family = ConfinedAnalysisFamilyStatus(family_name, True, attested_version)
        mwcp_parser_families[family_name] = parser_family
    if mwcp.available and not mwcp_parser_families:
        mwcp = ConfinedAnalysisFamilyStatus("mwcp", True, mwcp.version, "no_authorized_parser")
    elif mwcp.available and not any(item.available for item in mwcp_parser_families.values()):
        mwcp = ConfinedAnalysisFamilyStatus(
            "mwcp", True, mwcp.version, "allowlisted_parser_unavailable"
        )
    yara = status("yara", yara_version, "4.5.0")
    local_command_versions = (
        ("local_file", settings.analysis_local_file_expected_version),
        ("local_strings", settings.analysis_local_binutils_expected_version),
        ("local_readelf", settings.analysis_local_binutils_expected_version),
        ("local_objdump", settings.analysis_local_binutils_expected_version),
        ("local_xxd", settings.analysis_local_xxd_expected_version),
        ("local_sha256sum", settings.analysis_local_coreutils_expected_version),
    )
    local_command_reason: str | None = None
    if not worker_confined:
        local_command_reason = "analysis_worker_isolation_unattested"
    elif not dependencies_ready:
        local_command_reason = "analysis_worker_dependencies_unavailable"
    elif "local_command" not in supported_families:
        local_command_reason = "analysis_family_not_supported_by_worker"
    else:
        for binary_family, expected_version in local_command_versions:
            actual_version = _safe_version(versions.get(binary_family))
            if actual_version is None:
                local_command_reason = "analysis_tool_unavailable"
                break
            if actual_version != expected_version:
                local_command_reason = "analysis_tool_version_unattested"
                break
    local_command = ConfinedAnalysisFamilyStatus(
        "local_command",
        local_command_reason is None,
        "local-command-catalog-v1" if local_command_reason is None else None,
        local_command_reason,
    )
    sample = ConfinedAnalysisFamilyStatus(
        "sample_analysis",
        "sample_analysis" in supported_families
        and static.available
        and capa.available
        and smda.available,
        "static+capa+smda"
        if "sample_analysis" in supported_families
        and static.available
        and capa.available
        and smda.available
        else None,
        None
        if "sample_analysis" in supported_families
        and static.available
        and capa.available
        and smda.available
        else "analysis_family_not_supported_by_worker"
        if "sample_analysis" not in supported_families
        else next(item.available_reason for item in (static, capa, smda) if not item.available),
    )
    return {
        item.family: item
        for item in (
            static,
            capa,
            smda,
            tshark,
            capinfos,
            suricata,
            floss,
            mwcp,
            *mwcp_parser_families.values(),
            yara,
            local_command,
            sample,
            dotnet,
        )
    }


def _unavailable_families(reason: str) -> dict[str, ConfinedAnalysisFamilyStatus]:
    names = (
        "static",
        "capa",
        "smda",
        "tshark",
        "capinfos",
        "suricata",
        "floss",
        "mwcp",
        "yara",
        "local_command",
        "sample_analysis",
        "dotnet",
    )
    return {
        name: ConfinedAnalysisFamilyStatus(name, False, available_reason=reason) for name in names
    }


def _safe_version(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    if re.fullmatch(r"[A-Za-z0-9.+_-]+", value) is None:
        return None
    return value


def _unavailable_attestation(reason: str) -> ConfinedAnalysisWorkerAttestation:
    return ConfinedAnalysisWorkerAttestation(
        reachable=False,
        confined=False,
        isolation_profile=None,
        versions={},
        families=_unavailable_families(reason),
        dependencies_ready=False,
        available_reason=reason,
    )
