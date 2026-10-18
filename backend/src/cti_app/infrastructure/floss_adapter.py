from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from uuid import UUID

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.application.local_analysis import LocalCommandCatalog, LocalCommandService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.extraction import ExtractionProvenance, ExtractionStatus
from cti_app.domain.local_analysis import LocalCommandStatus
from cti_app.domain.string_extraction import (
    ExtractedString,
    ExtractedStringType,
    StringExtractionV1,
)

_PARAMETERS = {"analysis": "floss.json.v1", "output_format": "json"}
_MAX_ITEM_CHARS = 8_000
_TYPE_NAMES = {
    "static": ExtractedStringType.STATIC,
    "stack": ExtractedStringType.STACK,
    "tight": ExtractedStringType.TIGHT,
    "decoded": ExtractedStringType.DECODED,
}


class FlossAdapter:
    """FLOSS result adapter; sample bytes are read only by analysis-worker."""

    def __init__(
        self,
        command_service: LocalCommandService | None = None,
        command_catalog: LocalCommandCatalog | None = None,
        *,
        enabled: bool,
        confined_analysis: ConfinedAnalysisExecutionPort | None = None,
        uow_factory: UnitOfWorkFactory | None = None,
        timeout_seconds: float = 120,
        max_output_bytes: int = 1_000_000,
        max_sample_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self._service = command_service
        self._catalog = command_catalog
        self._enabled = enabled
        self._confined_analysis = confined_analysis
        self._uow_factory = uow_factory
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._max_sample_bytes = max_sample_bytes

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        if not self._enabled:
            return "floss_disabled"
        if self._confined_analysis is None or self._uow_factory is None:
            return "analysis_worker_unavailable"
        if not self._confined_analysis.available:
            return self._confined_analysis.available_reason or "analysis_worker_unavailable"
        family = self._confined_analysis.family_statuses.get("floss")
        return (
            None
            if family is not None and family.available
            else (family.available_reason if family is not None else "floss_unattested")
        )

    @property
    def version(self) -> str | None:
        if not self.available or self._confined_analysis is None:
            return None
        family = self._confined_analysis.family_statuses.get("floss")
        return family.version if family is not None else None

    async def extract(
        self,
        sample_sha256: str,
        *,
        max_strings: int,
        investigation_id: UUID | None = None,
    ) -> StringExtractionV1:
        if investigation_id is None or not self.available:
            return self._unavailable(sample_sha256, max_strings)
        assert self._confined_analysis is not None and self._uow_factory is not None
        async with self._uow_factory() as uow:
            blob = await uow.blobs.get_by_address(SAMPLES_QUARANTINE_BUCKET, sample_sha256)
        if blob is None or blob.descriptor.sha256 != sample_sha256:
            return self._unavailable(sample_sha256, max_strings, reason="sample_bytes_unavailable")
        if blob.descriptor.size > self._max_sample_bytes:
            return self._unavailable(
                sample_sha256,
                max_strings,
                reason="sample_too_large",
                status=ExtractionStatus.POLICY_DENIED,
            )
        response = await self._confined_analysis.execute(
            ConfinedAnalysisRequest(
                investigation_id=investigation_id,
                artifact_id=blob.id,
                sha256=sample_sha256,
                artifact_kind=ConfinedAnalysisArtifactKind.CORPUS_SAMPLE,
                tool=ConfinedAnalysisTool.FLOSS,
                timeout_seconds=self._timeout_seconds,
                max_output_bytes=self._max_output_bytes,
                analysis_parameters={"max_strings": str(max_strings)},
            )
        )
        worker_result = (response.payload or {}).get("result", {})
        if not isinstance(worker_result, Mapping):
            worker_result = {}
        strings = tuple(
            ExtractedString(
                value=str(item.get("value", "")),
                kind=ExtractedStringType(str(item.get("kind", "static"))),
                encoding=item.get("encoding") if isinstance(item.get("encoding"), str) else None,
                offset=item.get("offset") if isinstance(item.get("offset"), int) else None,
                address=item.get("address") if isinstance(item.get("address"), str) else None,
                function=item.get("function") if isinstance(item.get("function"), str) else None,
                context=item.get("context") if isinstance(item.get("context"), str) else None,
                evidence_path=(
                    item.get("evidence_path")
                    if isinstance(item.get("evidence_path"), str)
                    else None
                ),
            )
            for item in worker_result.get("strings", ())
            if isinstance(item, Mapping) and isinstance(item.get("value"), str) and item["value"]
        )
        result_status = str(worker_result.get("status", "FAILED")).upper()
        status = (
            ExtractionStatus.TIMEOUT
            if result_status in {"TIMEOUT", "TIMED_OUT"}
            else ExtractionStatus.EMPTY
            if response.status.value == "succeeded" and result_status == "SUCCEEDED" and not strings
            else ExtractionStatus.SUCCEEDED
            if response.status.value == "succeeded" and result_status == "SUCCEEDED"
            else ExtractionStatus.PARTIAL
            if result_status in {"PARTIAL", "OUTPUT_LIMIT"}
            else ExtractionStatus.FAILED
        )
        parameters_sha256 = hashlib.sha256(
            json.dumps(
                {**_PARAMETERS, "max_strings": max_strings}, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        return StringExtractionV1(
            schema_version="1",
            sample_sha256=sample_sha256,
            status=status,
            strings=strings,
            provenance=ExtractionProvenance(
                tool_id="floss",
                tool_version=str(worker_result.get("version") or self.version or "unknown"),
                parameters_sha256=parameters_sha256,
                raw_output_sha256=(
                    worker_result.get("output_sha256")
                    if isinstance(worker_result.get("output_sha256"), str)
                    else None
                ),
                raw_output_bytes=int(worker_result.get("output_bytes", 0)),
                raw_output_complete=(
                    isinstance(worker_result.get("output_sha256"), str)
                    and not bool(worker_result.get("truncated"))
                ),
                output_truncated=bool(worker_result.get("truncated")),
                exit_code=worker_result.get("exit_code")
                if isinstance(worker_result.get("exit_code"), int)
                else None,
            ),
            strings_observed=max(int(worker_result.get("strings_observed", 0)), len(strings)),
            truncated=bool(worker_result.get("truncated")),
            detail_code=response.available_reason,
        )

    async def extract_confined(
        self, sample_sha256: str, *, max_strings: int, investigation_id: UUID
    ) -> StringExtractionV1:
        return await self.extract(
            sample_sha256, max_strings=max_strings, investigation_id=investigation_id
        )

    def _unavailable(
        self,
        sample_sha256: str,
        max_strings: int,
        *,
        reason: str = "analysis_worker_unavailable",
        status: ExtractionStatus = ExtractionStatus.UNAVAILABLE,
    ) -> StringExtractionV1:
        parameters_sha256 = hashlib.sha256(
            json.dumps(
                {**_PARAMETERS, "max_strings": max_strings}, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        return StringExtractionV1(
            schema_version="1",
            sample_sha256=sample_sha256,
            status=status,
            strings=(),
            provenance=ExtractionProvenance(
                tool_id="floss",
                tool_version=self.version or "unavailable",
                parameters_sha256=parameters_sha256,
                raw_output_sha256=None,
                raw_output_bytes=0,
                raw_output_complete=False,
                output_truncated=False,
            ),
            strings_observed=0,
            detail_code=reason,
        )


def _parse_floss_payload(
    payload: object, *, max_strings: int
) -> tuple[tuple[ExtractedString, ...], int]:
    if not isinstance(payload, Mapping):
        raise ValueError("FLOSS JSON root must be an object")
    strings_root = payload.get("strings")
    if isinstance(strings_root, Mapping):
        payload = {**payload, **strings_root}
    result: list[ExtractedString] = []
    sections = (
        ("strings", ExtractedStringType.STATIC),
        ("static_strings", ExtractedStringType.STATIC),
        ("stack_strings", ExtractedStringType.STACK),
        ("tight_strings", ExtractedStringType.TIGHT),
        ("decoded_strings", ExtractedStringType.DECODED),
    )
    seen_paths: set[str] = set()
    observed = 0
    for section, section_kind in sections:
        records = payload.get(section, ())
        if not isinstance(records, list):
            continue
        for index, item in enumerate(records):
            if not isinstance(item, Mapping):
                continue
            value = item.get("string", item.get("value", item.get("text")))
            if not isinstance(value, str) or not value:
                continue
            observed += 1
            if len(result) >= max_strings:
                continue
            item_kind = _TYPE_NAMES.get(str(item.get("type", "")).casefold(), section_kind)
            path = f"/{section}/{index}"
            if path in seen_paths:
                continue
            seen_paths.add(path)
            truncated_fields: list[str] = []
            bounded_value = value[:_MAX_ITEM_CHARS]
            if len(value) > len(bounded_value):
                truncated_fields.append("value")
            encoding, encoding_truncated = _optional_text(item.get("encoding"), 128)
            function, function_truncated = _optional_text(item.get("function"), 256)
            context, context_truncated = _optional_text(item.get("context"), 1_000)
            if encoding_truncated:
                truncated_fields.append("encoding")
            if function_truncated:
                truncated_fields.append("function")
            if context_truncated:
                truncated_fields.append("context")
            result.append(
                ExtractedString(
                    value=bounded_value,
                    kind=item_kind,
                    encoding=encoding,
                    offset=_optional_offset(item.get("offset")),
                    address=_optional_address(item.get("address")),
                    function=function,
                    context=context,
                    evidence_path=path,
                    truncated_fields=tuple(truncated_fields),
                )
            )
    return tuple(result), observed


def _optional_text(value: object, max_chars: int) -> tuple[str | None, bool]:
    if isinstance(value, str) and value.strip():
        bounded = value[:max_chars]
        return bounded, len(value) > len(bounded)
    return None, False


def _optional_offset(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _optional_address(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return hex(value)
    if isinstance(value, str) and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,16}", value):
        return value
    return None


def _status(status: LocalCommandStatus, *, empty: bool) -> ExtractionStatus:
    if status in {LocalCommandStatus.SUCCEEDED, LocalCommandStatus.EMPTY}:
        return ExtractionStatus.EMPTY if empty else ExtractionStatus.SUCCEEDED
    return {
        LocalCommandStatus.UNAVAILABLE: ExtractionStatus.UNAVAILABLE,
        LocalCommandStatus.POLICY_DENIED: ExtractionStatus.POLICY_DENIED,
        LocalCommandStatus.TIMEOUT: ExtractionStatus.TIMEOUT,
        LocalCommandStatus.OUTPUT_TRUNCATED: ExtractionStatus.PARTIAL,
        LocalCommandStatus.FAILED: ExtractionStatus.FAILED,
    }.get(status, ExtractionStatus.FAILED)
