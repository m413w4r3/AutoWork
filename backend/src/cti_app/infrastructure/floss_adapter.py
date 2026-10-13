from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from cti_app.application.local_analysis import LocalCommandCatalog, LocalCommandService
from cti_app.domain.extraction import ExtractionProvenance, ExtractionStatus
from cti_app.domain.local_analysis import LocalCommandId, LocalCommandStatus
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
    """FLOSS adapter backed by the catalogue and confined local command service."""

    def __init__(
        self,
        command_service: LocalCommandService,
        command_catalog: LocalCommandCatalog,
        *,
        enabled: bool,
    ) -> None:
        self._service = command_service
        self._catalog = command_catalog
        self._enabled = enabled

    @property
    def available(self) -> bool:
        capability = self._catalog.get(LocalCommandId.FLOSS)
        return self._enabled and capability is not None and capability.available

    @property
    def version(self) -> str | None:
        capability = self._catalog.get(LocalCommandId.FLOSS)
        return capability.version if self.available and capability is not None else None

    async def extract(self, sample_sha256: str, *, max_strings: int) -> StringExtractionV1:
        result = await self._service.run(
            LocalCommandId.FLOSS, sample_sha256, {"max_strings": max_strings}
        )
        raw_prefix = result.raw_output_prefix
        if raw_prefix is None:
            raw_prefix = result.output.encode("utf-8", errors="replace")
        strings: tuple[ExtractedString, ...] = ()
        strings_observed = 0
        parse_failed = False
        try:
            payload = json.loads(raw_prefix.decode("utf-8")) if raw_prefix else {}
            strings, strings_observed = _parse_floss_payload(payload, max_strings=max_strings)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            parse_failed = bool(raw_prefix)

        raw_sha256 = result.raw_output_sha256 or hashlib.sha256(raw_prefix).hexdigest()
        complete = result.raw_output_complete or (
            result.raw_output_sha256 is None
            and result.status in {LocalCommandStatus.SUCCEEDED, LocalCommandStatus.EMPTY}
        )
        output_truncated = (
            result.status
            in {
                LocalCommandStatus.OUTPUT_TRUNCATED,
                LocalCommandStatus.TIMEOUT,
            }
            or not complete
        )
        extraction_truncated = output_truncated or strings_observed > len(strings)
        status = _status(result.status, empty=not strings)
        if parse_failed:
            status = ExtractionStatus.PARTIAL if output_truncated else ExtractionStatus.FAILED
        elif extraction_truncated and status is ExtractionStatus.SUCCEEDED:
            status = ExtractionStatus.PARTIAL

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
                tool_version=result.executable_version or self.version or "unavailable",
                parameters_sha256=parameters_sha256,
                raw_output_sha256=raw_sha256,
                raw_output_bytes=result.raw_output_bytes or len(raw_prefix),
                raw_output_complete=complete,
                output_truncated=output_truncated,
                exit_code=result.exit_code,
            ),
            strings_observed=max(strings_observed, len(strings)),
            truncated=extraction_truncated or parse_failed,
            detail_code=result.detail_code or ("invalid_floss_json" if parse_failed else None),
        )


def _parse_floss_payload(
    payload: object, *, max_strings: int
) -> tuple[tuple[ExtractedString, ...], int]:
    if not isinstance(payload, Mapping):
        raise ValueError("FLOSS JSON root must be an object")
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
            result.append(
                ExtractedString(
                    value=value[:_MAX_ITEM_CHARS],
                    kind=item_kind,
                    encoding=_optional_text(item.get("encoding"), 128),
                    offset=_optional_offset(item.get("offset")),
                    address=_optional_address(item.get("address")),
                    function=_optional_text(item.get("function"), 256),
                    context=_optional_text(item.get("context"), 1_000),
                    evidence_path=path,
                )
            )
    return tuple(result), observed


def _optional_text(value: object, max_chars: int) -> str | None:
    if isinstance(value, str) and value.strip():
        return value[:max_chars]
    return None


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
