from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from cti_app.application.local_analysis import LocalCommandCatalog, LocalCommandService
from cti_app.domain.configuration_extraction import (
    ConfigurationExtractionV1,
    ConfigurationKeyValue,
    ConfigurationValueType,
    NetworkIndicator,
    NetworkIndicatorKind,
)
from cti_app.domain.extraction import ExtractionProvenance, ExtractionStatus
from cti_app.domain.local_analysis import LocalCommandId, LocalCommandStatus

_PARAMETERS = {"analysis": "mwcp.json.v1", "output_format": "json"}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature|environment|env)",
    re.I,
)
_NETWORK_KEY = re.compile(r"(?:ip|host|domain|url|uri|c2|server|endpoint|callback)", re.I)
_MAX_VALUES = 256
_MAX_VALUE_CHARS = 4_000


class MwcpAdapter:
    """DC3-MWCP JSON adapter; only explicitly supplied parser IDs are callable."""

    def __init__(
        self,
        command_service: LocalCommandService,
        command_catalog: LocalCommandCatalog,
        *,
        parser_versions: Mapping[str, str],
        enabled: bool,
    ) -> None:
        self._service = command_service
        self._catalog = command_catalog
        self._parser_versions = dict(parser_versions)
        self._enabled = enabled

    @property
    def allowed_parsers(self) -> Mapping[str, str]:
        return dict(self._parser_versions)

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        if not self._enabled:
            return "mwcp_disabled"
        if not self._parser_versions:
            return "parser_allowlist_empty"
        if not self._service.filesystem_confined:
            return "filesystem_confinement_required"
        capability = self._catalog.get(LocalCommandId.MWCP)
        if capability is None or not capability.available:
            return "binary_unavailable"
        return None

    async def extract(self, sample_sha256: str, *, parser_id: str) -> ConfigurationExtractionV1:
        parser_version = self._parser_versions.get(parser_id)
        if parser_version is None:
            return _empty_result(
                sample_sha256,
                parser_id="unauthorized",
                parser_version="unavailable",
                status=ExtractionStatus.POLICY_DENIED,
                detail_code="parser_not_allowlisted",
                raw_output_sha256=None,
            )
        result = await self._service.run(
            LocalCommandId.MWCP, sample_sha256, {"parser_id": parser_id}
        )
        raw_prefix = result.raw_output_prefix
        if raw_prefix is None:
            raw_prefix = result.output.encode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_prefix.decode("utf-8")) if raw_prefix else {}
            values, indicators, normalization_truncated = _normalize_configuration(payload)
            parse_failed = False
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            values, indicators, normalization_truncated, parse_failed = (
                (),
                (),
                False,
                bool(raw_prefix),
            )

        raw_sha256 = result.raw_output_sha256 or hashlib.sha256(raw_prefix).hexdigest()
        complete = result.raw_output_complete or (
            result.raw_output_sha256 is None
            and result.status in {LocalCommandStatus.SUCCEEDED, LocalCommandStatus.EMPTY}
        )
        truncated = (
            result.status
            in {
                LocalCommandStatus.OUTPUT_TRUNCATED,
                LocalCommandStatus.TIMEOUT,
            }
            or not complete
            or parse_failed
            or normalization_truncated
        )
        status = _status(result.status, empty=not values and not indicators)
        if parse_failed:
            status = (
                ExtractionStatus.PARTIAL
                if result.status is LocalCommandStatus.OUTPUT_TRUNCATED
                or result.status is LocalCommandStatus.TIMEOUT
                else ExtractionStatus.FAILED
            )
        elif (
            result.status is LocalCommandStatus.OUTPUT_TRUNCATED
            and status is ExtractionStatus.SUCCEEDED
        ):
            status = ExtractionStatus.PARTIAL

        parameters_sha256 = hashlib.sha256(
            json.dumps(
                {**_PARAMETERS, "parser_id": parser_id}, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        return ConfigurationExtractionV1(
            schema_version="1",
            sample_sha256=sample_sha256,
            parser_id=parser_id,
            parser_version=parser_version,
            status=status,
            values=values,
            network_indicators=indicators,
            provenance=ExtractionProvenance(
                tool_id="dc3-mwcp",
                tool_version=result.executable_version or "unavailable",
                parameters_sha256=parameters_sha256,
                raw_output_sha256=raw_sha256,
                raw_output_bytes=result.raw_output_bytes or len(raw_prefix),
                raw_output_complete=complete,
                output_truncated=truncated,
                exit_code=result.exit_code,
            ),
            truncated=truncated,
            detail_code=result.detail_code or ("invalid_mwcp_json" if parse_failed else None),
        )


def _normalize_configuration(
    payload: object,
) -> tuple[tuple[ConfigurationKeyValue, ...], tuple[NetworkIndicator, ...], bool]:
    if not isinstance(payload, Mapping):
        raise ValueError("MWCP JSON root must be an object")
    config: object = payload.get(
        "configuration", payload.get("config", payload.get("report", payload))
    )
    if isinstance(config, Mapping) and isinstance(config.get("configuration"), Mapping):
        config = config["configuration"]
    values: list[ConfigurationKeyValue] = []
    indicators: list[NetworkIndicator] = []
    truncated = False
    visited_nodes = 0

    def visit(
        value: object,
        path: str,
        depth: int,
        path_truncated: bool = False,
        key_truncated: bool = False,
    ) -> None:
        nonlocal truncated, visited_nodes
        visited_nodes += 1
        if visited_nodes > 4096 or len(values) >= _MAX_VALUES or depth > 12:
            truncated = True
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                raw_key_text = str(key)
                key_text = raw_key_text[:128]
                key_truncated = len(raw_key_text) > len(key_text)
                if _SECRET_KEY.search(key_text):
                    continue
                child_path = f"{path}/{_escape_pointer(key_text)}"
                visit(item, child_path, depth + 1, path_truncated or key_truncated, key_truncated)
            return
        if isinstance(value, list | tuple):
            if len(value) > 64:
                truncated = True
            for index, item in enumerate(value[:64]):
                visit(item, f"{path}/{index}", depth + 1, path_truncated, key_truncated)
            return
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and not __import__("math").isfinite(value):
                return
            truncated_fields: list[str] = []
            if key_truncated:
                truncated_fields.append("key")
            if path_truncated:
                truncated_fields.append("evidence_path")
            if isinstance(value, str):
                raw_value = value
                value = raw_value[:_MAX_VALUE_CHARS]
                value_truncated = len(raw_value) > len(value)
                if value_truncated:
                    truncated_fields.append("value")
                value_type = ConfigurationValueType.STRING
                if _NETWORK_KEY.search(path):
                    indicator = _network_indicator(
                        path,
                        value,
                        truncated_fields=(
                            (("evidence_path",) if path_truncated else ())
                            + (("value",) if value_truncated else ())
                        ),
                    )
                    if indicator is not None:
                        indicators.append(indicator)
                    elif len(raw_value) > 2_048:
                        truncated_fields.append("network_indicator")
            elif isinstance(value, bool):
                value_type = ConfigurationValueType.BOOLEAN
            elif isinstance(value, int):
                value_type = ConfigurationValueType.INTEGER
            elif isinstance(value, float):
                value_type = ConfigurationValueType.NUMBER
            else:
                value_type = ConfigurationValueType.NULL
            values.append(
                ConfigurationKeyValue(
                    key=path.rsplit("/", maxsplit=1)[-1],
                    value_type=value_type,
                    value=value,
                    evidence_path=path or "/",
                    truncated_fields=tuple(truncated_fields),
                )
            )
            if truncated_fields:
                truncated = True

    visit(config, "/configuration", 0)
    return tuple(values), tuple(indicators), truncated


def _network_indicator(
    path: str, value: str, *, truncated_fields: tuple[str, ...] = ()
) -> NetworkIndicator | None:
    candidate = value.strip().strip("[](){}\"'")
    if not candidate or len(candidate) > 2_048:
        return None
    try:
        normalized_ip = str(ipaddress.ip_address(candidate))
    except ValueError:
        normalized_ip = None
    if normalized_ip is not None:
        return NetworkIndicator(
            NetworkIndicatorKind.IP_ADDRESS,
            candidate,
            normalized_ip,
            path or "/",
            truncated_fields,
        )

    if "://" in candidate:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        if parsed.scheme.casefold() not in {"http", "https", "ftp"} or not parsed.hostname:
            return None
        host = _normalize_host(parsed.hostname)
        if host is None:
            return None
        netloc = f"[{host}]" if ":" in host else host
        normalized = urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", "", ""))
        return NetworkIndicator(
            NetworkIndicatorKind.URL, candidate, normalized, path or "/", truncated_fields
        )

    host = _normalize_host(candidate)
    if host is not None and ("." in host or host == "localhost"):
        return NetworkIndicator(
            NetworkIndicatorKind.DOMAIN, candidate, host, path or "/", truncated_fields
        )
    return None


def _normalize_host(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        try:
            normalized = host.rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return None
        if (
            len(normalized) > 253
            or not normalized
            or any(
                not part or len(part) > 63 or re.fullmatch(r"[a-z0-9-]+", part) is None
                for part in normalized.split(".")
            )
        ):
            return None
        return normalized


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _empty_result(
    sample_sha256: str,
    *,
    parser_id: str,
    parser_version: str,
    status: ExtractionStatus,
    detail_code: str,
    raw_output_sha256: str | None,
) -> ConfigurationExtractionV1:
    return ConfigurationExtractionV1(
        schema_version="1",
        sample_sha256=sample_sha256,
        parser_id=parser_id,
        parser_version=parser_version,
        status=status,
        values=(),
        network_indicators=(),
        provenance=ExtractionProvenance(
            tool_id="dc3-mwcp",
            tool_version="unavailable",
            parameters_sha256=hashlib.sha256(b"{}").hexdigest(),
            raw_output_sha256=raw_output_sha256,
            raw_output_bytes=0,
            raw_output_complete=False,
            output_truncated=False,
        ),
        detail_code=detail_code,
    )


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
