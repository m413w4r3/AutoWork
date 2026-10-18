from __future__ import annotations

from uuid import UUID

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.local_analysis import LocalCommandStatus
from cti_app.domain.network_protocol import NetworkExtractionLimitsV1
from cti_app.infrastructure.tshark_adapter import (
    TSHARK_FIELDS,
    TsharkAdapter,
    TsharkInspection,
    TsharkPacketFields,
    _parse_tshark_rows,
)

_CAPINFOS_ARGUMENTS = ("-a", "-c", "-e", "-t", "-u")
_MAX_COMMAND_SECONDS = 25
_MAX_COMMAND_OUTPUT_BYTES = 64 * 1024


class ConfinedTsharkAdapter(TsharkAdapter):
    """Run bounded TShark/capinfos commands through the attested analysis worker."""

    def __init__(
        self,
        execution: ConfinedAnalysisExecutionPort,
        *,
        limits: NetworkExtractionLimitsV1 | None = None,
        timeout_seconds: float = _MAX_COMMAND_SECONDS,
        max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
    ) -> None:
        self.execution = execution
        self.limits = limits or NetworkExtractionLimitsV1()
        self.timeout_seconds = min(timeout_seconds, _MAX_COMMAND_SECONDS)
        self.max_output_bytes = min(max_output_bytes, _MAX_COMMAND_OUTPUT_BYTES)
        self._ja3_available = False

    @property
    def available(self) -> bool:
        return self.execution.available and all(
            self.execution.family_statuses.get(name) is not None
            and self.execution.family_statuses[name].available
            for name in ("tshark", "capinfos")
        )

    @property
    def available_reason(self) -> str | None:
        if self.available:
            return None
        for name in ("tshark", "capinfos"):
            family = self.execution.family_statuses.get(name)
            if family is not None and not family.available:
                return family.available_reason or f"analysis_worker_{name}_unavailable"
        return self.execution.available_reason or "analysis_worker_tshark_unavailable"

    @property
    def execution_mode(self) -> str:
        return "analysis-worker"

    def versions(self) -> tuple[str | None, str | None]:
        families = self.execution.family_statuses
        tshark = families.get("tshark")
        capinfos = families.get("capinfos")
        return (
            tshark.version if tshark is not None else None,
            capinfos.version if capinfos is not None else None,
        )

    async def inspect_artifact(
        self,
        *,
        artifact_id: UUID,
        investigation_id: UUID,
        capture_sha256: str,
    ) -> TsharkInspection:
        """Inspect the already-resolved canonical capture blob by immutable identity."""
        tshark_version, capinfos_version = self.versions()
        if not self.available:
            return self._empty(
                LocalCommandStatus.UNAVAILABLE,
                self.available_reason or "analysis_worker_tshark_unavailable",
                tshark_version,
                capinfos_version,
            )

        tshark_arguments: list[str] = [
            "-T",
            "fields",
            "-c",
            str(min(self.limits.max_packets, 10_000)),
        ]
        for field in TSHARK_FIELDS:
            tshark_arguments.extend(("-e", field))
        tshark_result = await self.execution.execute(
            ConfinedAnalysisRequest(
                investigation_id=investigation_id,
                artifact_id=artifact_id,
                sha256=capture_sha256,
                tool=ConfinedAnalysisTool.TSHARK,
                artifact_kind=ConfinedAnalysisArtifactKind.CAPTURE,
                timeout_seconds=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                arguments=tuple(tshark_arguments),
            )
        )
        capinfos_result = await self.execution.execute(
            ConfinedAnalysisRequest(
                investigation_id=investigation_id,
                artifact_id=artifact_id,
                sha256=capture_sha256,
                tool=ConfinedAnalysisTool.CAPINFOS,
                artifact_kind=ConfinedAnalysisArtifactKind.CAPTURE,
                timeout_seconds=min(self.timeout_seconds, 10),
                max_output_bytes=min(self.max_output_bytes, 8 * 1024),
                arguments=_CAPINFOS_ARGUMENTS,
            )
        )
        if tshark_result.status is not ToolInvocationStatus.SUCCEEDED:
            return self._empty(
                _local_status(tshark_result.status),
                tshark_result.available_reason or tshark_result.status.value,
                tshark_version,
                capinfos_version,
            )
        if capinfos_result.status is not ToolInvocationStatus.SUCCEEDED:
            return self._empty(
                _local_status(capinfos_result.status),
                capinfos_result.available_reason or capinfos_result.status.value,
                tshark_version,
                capinfos_version,
            )

        tshark_output, tshark_truncated = _command_output(tshark_result.payload)
        capinfos_output, capinfos_truncated = _command_output(capinfos_result.payload)
        if tshark_output is None or capinfos_output is None:
            return self._empty(
                LocalCommandStatus.FAILED,
                "analysis_worker_result_invalid",
                tshark_version,
                capinfos_version,
            )
        packets = self._parse_rows(tshark_output)
        truncated = tshark_truncated or capinfos_truncated
        status = (
            LocalCommandStatus.OUTPUT_TRUNCATED
            if truncated
            else (LocalCommandStatus.SUCCEEDED if packets else LocalCommandStatus.EMPTY)
        )
        return TsharkInspection(
            status=status,
            detail_code="output_limit" if truncated else None,
            tshark_version=tshark_version,
            capinfos_version=capinfos_version,
            packets=packets,
            capinfos_summary=capinfos_output,
            output_truncated=truncated,
            ja3_available=False,
            limitations=(
                "TShark désactive la résolution de noms et limite la dissection aux "
                "protocoles autorisés; les extensions Lua ne sont pas acceptées.",
            ),
        )

    async def inspect(self, capture_bytes: bytes, sha256: str) -> TsharkInspection:
        del capture_bytes, sha256
        return self._empty(
            LocalCommandStatus.UNAVAILABLE,
            "canonical_capture_identity_required",
            *self.versions(),
        )

    @staticmethod
    def _parse_rows(output: str) -> tuple[TsharkPacketFields, ...]:
        return _parse_tshark_rows(output, include_ja3=False)

    @staticmethod
    def _empty(
        status: LocalCommandStatus,
        reason: str,
        tshark_version: str | None,
        capinfos_version: str | None,
    ) -> TsharkInspection:
        return TsharkInspection(
            status=status,
            detail_code=reason,
            tshark_version=tshark_version,
            capinfos_version=capinfos_version,
            packets=(),
            capinfos_summary="",
            output_truncated=status is LocalCommandStatus.OUTPUT_TRUNCATED,
            ja3_available=False,
            limitations=(reason,),
        )


def _command_output(payload: object) -> tuple[str | None, bool]:
    if not isinstance(payload, dict):
        return None, False
    command = payload.get("result")
    if not isinstance(command, dict):
        return None, False
    output = command.get("stdout")
    if not isinstance(output, str):
        return None, False
    status = command.get("status")
    if status not in {"SUCCEEDED", "OUTPUT_LIMIT"}:
        return None, False
    truncated = bool(command.get("truncated")) or status == "OUTPUT_LIMIT"
    return output, truncated


def _local_status(status: ToolInvocationStatus) -> LocalCommandStatus:
    if status is ToolInvocationStatus.UNAVAILABLE:
        return LocalCommandStatus.UNAVAILABLE
    if status is ToolInvocationStatus.POLICY_DENIED:
        return LocalCommandStatus.POLICY_DENIED
    if status is ToolInvocationStatus.PARTIAL:
        return LocalCommandStatus.OUTPUT_TRUNCATED
    return LocalCommandStatus.FAILED
