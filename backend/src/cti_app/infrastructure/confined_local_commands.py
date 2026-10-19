from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.application.local_analysis import (
    LocalCommandBudgetEstimate,
    LocalCommandCatalog,
    _command_range_end,
    _validate_local_command_range_options,
    normalize_command_options,
)
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.local_analysis import (
    LocalCommandId,
    LocalCommandResult,
    LocalCommandStatus,
)
from cti_app.infrastructure.local_command_runner import _render_yara_rule

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConfinedLocalCommands:
    """Execute the typed local command catalogue through the attested analysis worker."""

    def __init__(
        self,
        *,
        execution: ConfinedAnalysisExecutionPort,
        catalog: LocalCommandCatalog,
        uow_factory: Callable[[], Any],
        settings: Settings,
        enabled: bool,
        max_sample_bytes: int,
    ) -> None:
        self.execution = execution
        self.catalog = catalog
        self._uow_factory = uow_factory
        self._settings = settings
        self._enabled = enabled
        self._max_sample_bytes = max_sample_bytes

    @property
    def filesystem_confined(self) -> bool:
        attestation = getattr(self.execution, "attestation", None)
        return bool(self.execution.available and getattr(attestation, "confined", True))

    def estimate_budget(self, command_id: LocalCommandId | str) -> LocalCommandBudgetEstimate:
        try:
            normalized_id = LocalCommandId(command_id)
        except (TypeError, ValueError):
            return LocalCommandBudgetEstimate(0)
        capability = self.catalog.get(normalized_id)
        return LocalCommandBudgetEstimate(
            capability.spec.timeout_seconds if capability is not None else 0
        )

    async def run(
        self,
        command_id: LocalCommandId | str,
        sample_sha256: str,
        options: Mapping[str, object] | None = None,
    ) -> LocalCommandResult:
        del options
        try:
            normalized_id = LocalCommandId(command_id)
        except (TypeError, ValueError):
            return _result(None, LocalCommandStatus.POLICY_DENIED, None, "unknown_command")
        return _result(
            normalized_id,
            LocalCommandStatus.UNAVAILABLE,
            sample_sha256 if _SHA256.fullmatch(sample_sha256) else None,
            "canonical_investigation_identity_required",
        )

    async def run_for_investigation(
        self,
        investigation_id: UUID,
        command_id: LocalCommandId | str,
        sample_sha256: str,
        options: Mapping[str, object] | None = None,
    ) -> LocalCommandResult:
        started_at = datetime.now(UTC)
        monotonic_start = time.monotonic()
        try:
            normalized_id = LocalCommandId(command_id)
        except (TypeError, ValueError):
            return _result(None, LocalCommandStatus.POLICY_DENIED, None, "unknown_command")
        if not isinstance(sample_sha256, str) or _SHA256.fullmatch(sample_sha256) is None:
            return _result(normalized_id, LocalCommandStatus.POLICY_DENIED, None, "invalid_sha256")
        capability = self.catalog.get(normalized_id)
        if capability is None:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "command_not_catalogued",
            )
        raw_options = dict(options or {}) if isinstance(options, Mapping) else options
        if not isinstance(raw_options, Mapping):
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "options_must_be_object",
            )

        normalized_options, option_error = normalize_command_options(capability.spec, raw_options)
        if option_error is not None:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                option_error,
            )
        range_error = _validate_local_command_range_options(
            normalized_id,
            normalized_options,
            max_range_bytes=self._settings.analysis_local_command_max_range_bytes,
        )
        if range_error is not None:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                range_error,
            )
        offset = normalized_options.get("offset")
        length = normalized_options.get("length")
        bounded_range = (offset, length) if type(offset) is int and type(length) is int else None
        if not self._enabled:
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                "local_analysis_disabled",
            )

        family_name = "yara" if normalized_id is LocalCommandId.YARA else "local_command"
        family = self.execution.family_statuses.get(family_name)
        if not self.execution.available:
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                self.execution.available_reason or "analysis_worker_unavailable",
            )
        if family is None or not family.available:
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                family.available_reason or "analysis_family_not_attested"
                if family is not None
                else "analysis_family_not_attested",
            )

        identity = await self._canonical_sample(investigation_id, sample_sha256)
        if isinstance(identity, str):
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE
                if identity == "sample_bytes_unavailable"
                else LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                identity,
            )
        association_id, sample_id, blob_id, blob_size = identity
        if blob_size > self._max_sample_bytes:
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "sample_too_large",
            )
        range_end = _command_range_end(normalized_options)
        if (
            normalized_id in {LocalCommandId.XXD, LocalCommandId.SHA256SUM}
            and range_end is not None
            and range_end > blob_size
        ):
            return _result(
                normalized_id,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                "range_out_of_bounds",
            )

        analysis_parameters = {
            key: _option_string(value)
            for key, value in normalized_options.items()
            if key not in {"offset", "length"} and value is not None
        }
        arguments: tuple[str, ...] = (normalized_id.value,)
        tool = ConfinedAnalysisTool.LOCAL_COMMAND
        if normalized_id is LocalCommandId.YARA:
            tool = ConfinedAnalysisTool.YARA
            arguments = ()
            analysis_parameters = {"rule_source": _render_yara_rule(normalized_options)}
        elif bounded_range is not None:
            analysis_parameters["offset"] = str(bounded_range[0])
            analysis_parameters["length"] = str(bounded_range[1])

        request = ConfinedAnalysisRequest(
            investigation_id=investigation_id,
            association_id=association_id,
            sample_id=sample_id,
            artifact_id=blob_id,
            sha256=sample_sha256,
            artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
            tool=tool,
            arguments=arguments,
            analysis_parameters=analysis_parameters,
            timeout_seconds=min(
                capability.spec.timeout_seconds,
                self._settings.analysis_worker_job_timeout_seconds,
            ),
            max_output_bytes=min(
                capability.spec.max_output_bytes,
                self._settings.local_analysis_max_output_bytes,
                self._settings.analysis_worker_result_max_bytes,
            ),
        )
        result = await self.execution.execute(request)
        duration = max(0.0, time.monotonic() - monotonic_start)
        if result.status is not ToolInvocationStatus.SUCCEEDED or not isinstance(
            result.payload, Mapping
        ):
            return _result(
                normalized_id,
                LocalCommandStatus.UNAVAILABLE
                if result.status is ToolInvocationStatus.UNAVAILABLE
                else LocalCommandStatus.FAILED,
                sample_sha256,
                result.available_reason or "analysis_worker_result_unavailable",
                started_at=started_at,
                duration_seconds=duration,
            )
        result_data = result.payload.get("result")
        if not isinstance(result_data, Mapping):
            return _result(
                normalized_id,
                LocalCommandStatus.FAILED,
                sample_sha256,
                "analysis_worker_result_invalid",
                started_at=started_at,
                duration_seconds=duration,
            )

        if normalized_id is LocalCommandId.YARA:
            output = "local_analysis_f1" if result_data.get("matches") is True else ""
            command_argv: tuple[str, ...] = ("yara", "<rules>", "<sample>")
        elif normalized_id is LocalCommandId.MONODIS:
            operation = str(result_data.get("operation", "unknown"))
            output = (
                f"operation={operation} offset={result_data.get('offset', 0)} "
                f"length={result_data.get('length', 0)} "
                f"observed_bytes={result_data.get('total_bytes_observed', 0)} "
                f"complete={str(result_data.get('total_bytes_complete') is True).lower()}"
            )
            if isinstance(result_data.get("method"), str):
                output += (
                    f" method={result_data['method']} "
                    f"rid={result_data.get('method_rid', 'unknown')}"
                )
            output += "\n" + str(result_data.get("stdout", ""))
            command_argv = tuple(
                value
                for value in result_data.get("normalized_command", ("monodis", "<sample>"))
                if isinstance(value, str)
            )
        else:
            output = str(result_data.get("stdout", ""))
            if not output:
                output = str(result_data.get("stderr", ""))
            command_argv = tuple(
                value
                for value in result_data.get(
                    "normalized_command", (normalized_id.value, "<sample>")
                )
                if isinstance(value, str)
            )
        worker_status = str(result_data.get("status", "FAILED")).upper()
        truncated = result_data.get("truncated") is True or result.payload.get("truncated") is True
        status = {
            "SUCCEEDED": LocalCommandStatus.SUCCEEDED,
            "EMPTY": LocalCommandStatus.EMPTY,
            "TIMED_OUT": LocalCommandStatus.TIMEOUT,
            "TIMEOUT": LocalCommandStatus.TIMEOUT,
            "OUTPUT_LIMIT": LocalCommandStatus.OUTPUT_TRUNCATED,
            "OUTPUT_TRUNCATED": LocalCommandStatus.OUTPUT_TRUNCATED,
            "PARTIAL": LocalCommandStatus.OUTPUT_TRUNCATED,
            "POLICY_DENIED": LocalCommandStatus.POLICY_DENIED,
            "UNAVAILABLE": LocalCommandStatus.UNAVAILABLE,
        }.get(worker_status, LocalCommandStatus.FAILED)
        if truncated and status is LocalCommandStatus.SUCCEEDED:
            status = LocalCommandStatus.OUTPUT_TRUNCATED
        if status is LocalCommandStatus.SUCCEEDED and not output:
            status = LocalCommandStatus.EMPTY
        version = result_data.get("version")
        if not isinstance(version, str):
            version_key = {
                LocalCommandId.FILE: "local_file",
                LocalCommandId.STRINGS: "local_strings",
                LocalCommandId.READELF: "local_readelf",
                LocalCommandId.OBJDUMP: "local_objdump",
                LocalCommandId.YARA: "yara",
                LocalCommandId.MONODIS: "local_monodis",
            }[normalized_id]
            version = self.execution.attestation.versions.get(version_key)
        return LocalCommandResult(
            command_id=normalized_id,
            status=status,
            sample_sha256=sample_sha256,
            output=output,
            normalized_command=command_argv,
            executable_version=version if isinstance(version, str) else None,
            exit_code=_optional_int(result_data.get("return_code")),
            duration_seconds=duration,
            started_at=started_at,
            detail_code=(
                str(result_data.get("available_reason"))
                if result_data.get("available_reason") is not None
                else None
            ),
        )

    async def _canonical_sample(
        self, investigation_id: UUID, sample_sha256: str
    ) -> tuple[UUID, UUID, UUID, int] | str:
        try:
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(investigation_id)
                associations = await uow.sample_associations.list_for_investigation(
                    investigation_id
                )
                association = next(
                    (item for item in associations if item.sample_sha256 == sample_sha256), None
                )
                if investigation is None or association is None:
                    return "sample_not_associated"
                blob = await uow.blobs.get_by_address(SAMPLES_QUARANTINE_BUCKET, sample_sha256)
                if blob is None:
                    return "sample_bytes_unavailable"
                sample = await uow.samples.get_by_subject_and_blob(
                    investigation.subject_id, blob.id
                )
                if (
                    sample is None
                    or sample.blob_id != blob.id
                    or sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                    or blob.descriptor.sha256 != sample_sha256
                ):
                    return "sample_not_quarantined"
                return association.id, sample.id, blob.id, blob.descriptor.size
        except Exception:
            return "sample_identity_unavailable"


def _option_string(value: object) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _result(
    command_id: LocalCommandId | None,
    status: LocalCommandStatus,
    sample_sha256: str | None,
    detail_code: str,
    *,
    started_at: datetime | None = None,
    duration_seconds: float = 0,
) -> LocalCommandResult:
    return LocalCommandResult(
        command_id=command_id,
        status=status,
        sample_sha256=sample_sha256,
        normalized_command=(command_id.value,) if command_id is not None else (),
        detail_code=detail_code,
        started_at=started_at,
        duration_seconds=duration_seconds,
    )
