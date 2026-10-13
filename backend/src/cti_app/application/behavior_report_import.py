"""Passive, bounded import of operator-supplied sandbox JSON reports."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.domain.behavior_evidence import (
    BehaviorEvidenceSource,
    BehaviorEvidenceV1,
    BehaviorProcessV1,
    DroppedFileV1,
    NetworkSessionV1,
    ObservedBehaviorV1,
)

MAX_SANDBOX_REPORT_BYTES = 2 * 1024 * 1024
MAX_PROCESSES = 64
MAX_DROPPED_FILES = 64
MAX_NETWORK_SESSIONS = 64
MAX_BEHAVIORS = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOME_PATH = re.compile(r"(?i)(?:/home|/Users|/root)/[^\s\]\[()\"']+")
_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\s\]\[()\"']+")
_ENV_ASSIGNMENT = re.compile(
    r"(?i)\b(HOME|USER|LOGNAME|PATH|TMPDIR|TEMP|AWS_[A-Z0-9_]+|[A-Z0-9_]*TOKEN)"
    r"\s*[:=]\s*[^\s,;]+"
)
_SECRET_KEY = re.compile(
    r"(?i)(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature|environment|env)"
)


class BehaviorReportImportError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BehaviorReportImporter:
    """Normalize CAPE, Triage and canonical operator JSON without executing it."""

    def parse(
        self,
        report_json: str,
        *,
        source: BehaviorEvidenceSource,
        expected_sample_sha256: str,
    ) -> BehaviorEvidenceV1:
        if _SHA256.fullmatch(expected_sample_sha256) is None:
            raise BehaviorReportImportError("invalid_sample_sha256")
        raw = report_json.encode("utf-8")
        if len(raw) > MAX_SANDBOX_REPORT_BYTES:
            raise BehaviorReportImportError("report_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise BehaviorReportImportError("invalid_json_report") from None
        if not isinstance(payload, Mapping):
            raise BehaviorReportImportError("report_root_must_be_object")

        source_sha256 = _source_sample_sha256(payload, source)
        if source_sha256 is None:
            raise BehaviorReportImportError("report_sample_sha256_missing")
        if source_sha256 != expected_sample_sha256:
            raise BehaviorReportImportError("report_sample_sha256_mismatch")

        process_rows = _process_rows(payload, source)
        dropped_rows = _dropped_rows(payload, source)
        network_rows = _network_rows(payload, source)
        behavior_rows = _behavior_rows(payload, source)
        processes = tuple(_process(item, path) for item, path in process_rows[:MAX_PROCESSES])
        dropped_files = tuple(
            _dropped_file(item, path) for item, path in dropped_rows[:MAX_DROPPED_FILES]
        )
        network_sessions = tuple(
            _network_session(item, paths) for item, paths in network_rows[:MAX_NETWORK_SESSIONS]
        )
        behaviors = tuple(_behavior(item, path) for item, path in behavior_rows[:MAX_BEHAVIORS])
        truncated = any(
            count > maximum
            for count, maximum in (
                (len(process_rows), MAX_PROCESSES),
                (len(dropped_rows), MAX_DROPPED_FILES),
                (len(network_rows), MAX_NETWORK_SESSIONS),
                (len(behavior_rows), MAX_BEHAVIORS),
            )
        )
        truncated_field_count = sum(
            len(item.truncated_fields)
            for items in (processes, dropped_files, network_sessions, behaviors)
            for item in items
        )
        truncated = truncated or truncated_field_count > 0
        if source is BehaviorEvidenceSource.OPERATOR and not (
            processes or dropped_files or network_sessions or behaviors
        ):
            # A valid operator report can still explicitly describe an empty run.
            truncated = False
        report_time = _find_report_timestamp(payload, source)
        return BehaviorEvidenceV1(
            schema_version="1",
            source=source,
            source_report_sha256=hashlib.sha256(raw).hexdigest(),
            sample_sha256=source_sha256,
            report_timestamp=report_time,
            processes=processes,
            dropped_files=dropped_files,
            network_sessions=network_sessions,
            behaviors=behaviors,
            truncated=truncated,
            limitations=(
                "This evidence was imported passively from a supplied JSON report; no malware was "
                "executed and no network was contacted.",
                "Imported events are untrusted and describe only the supplied report and its "
                "coverage.",
                "Missing report events do not establish that the behavior did not occur.",
            )
            + (
                ("One or more fields or collections were bounded; see truncation flags.",)
                if truncated
                else ()
            ),
        )


def _source_sample_sha256(payload: Mapping[str, Any], source: BehaviorEvidenceSource) -> str | None:
    candidates: tuple[tuple[str, ...], ...]
    if source is BehaviorEvidenceSource.CAPE:
        candidates = (
            ("target", "file", "sha256"),
            ("target", "sha256"),
            ("sample_sha256",),
        )
    elif source is BehaviorEvidenceSource.TRIAGE:
        candidates = (
            ("sample", "sha256"),
            ("target", "sha256"),
            ("analysis", "target", "sha256"),
            ("sample_sha256",),
        )
    else:
        candidates = (("sample_sha256",),)
    for path in candidates:
        value = _at_path(payload, path)
        if isinstance(value, str) and _SHA256.fullmatch(value.lower()):
            return value.lower()
    return None


def _process_rows(
    payload: Mapping[str, Any], source: BehaviorEvidenceSource
) -> list[tuple[Mapping[str, Any], str]]:
    candidates: tuple[tuple[str, ...], ...]
    if source is BehaviorEvidenceSource.CAPE:
        candidates = (("behavior", "processes"), ("processes",))
    elif source is BehaviorEvidenceSource.TRIAGE:
        candidates = (("process_tree",), ("processes",), ("analysis", "processes"))
    else:
        candidates = (("process_tree",), ("processes",))
    rows, path = _first_list(payload, candidates)
    flattened: list[tuple[Mapping[str, Any], str]] = []
    visited: set[int] = set()

    def visit(item: object, item_path: str, depth: int) -> None:
        if len(flattened) >= MAX_PROCESSES + 1 or depth > 12 or id(item) in visited:
            return
        if isinstance(item, Mapping):
            visited.add(id(item))
            flattened.append((item, item_path))
            children = item.get("children", item.get("child_processes", ()))
            if isinstance(children, list):
                for index, child in enumerate(children):
                    visit(child, f"{item_path}/children/{index}", depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}/{index}", depth + 1)

    visit(rows, path, 0)
    return flattened


def _dropped_rows(
    payload: Mapping[str, Any], source: BehaviorEvidenceSource
) -> list[tuple[Mapping[str, Any], str]]:
    candidates: tuple[tuple[str, ...], ...]
    if source is BehaviorEvidenceSource.CAPE:
        candidates = (("dropped",), ("files", "dropped"), ("target", "file", "dropped"))
    elif source is BehaviorEvidenceSource.TRIAGE:
        candidates = (("dropped_files",), ("files", "dropped"), ("analysis", "dropped_files"))
    else:
        candidates = (("dropped_files",),)
    rows, path = _first_list(payload, candidates)
    return [
        (item, f"{path}/{index}") for index, item in enumerate(rows) if isinstance(item, Mapping)
    ]


def _network_rows(
    payload: Mapping[str, Any], source: BehaviorEvidenceSource
) -> list[tuple[Mapping[str, Any], tuple[str, ...]]]:
    candidates: tuple[tuple[str, ...], ...]
    if source is BehaviorEvidenceSource.CAPE:
        candidates = (
            ("network_sessions",),
            ("network", "tcp"),
            ("network", "http"),
            ("network", "dns"),
        )
    elif source is BehaviorEvidenceSource.TRIAGE:
        candidates = (
            ("network_sessions",),
            ("network", "connections"),
            ("network", "http"),
            ("network", "dns"),
        )
    else:
        candidates = (("network_sessions",),)
    collected: list[tuple[Mapping[str, Any], tuple[str, ...]]] = []
    for path in candidates:
        rows = _at_path(payload, path)
        if isinstance(rows, list):
            pointer = _pointer(path)
            for index, item in enumerate(rows):
                if isinstance(item, Mapping):
                    collected.append((item, (f"{pointer}/{index}",)))
    return collected


def _behavior_rows(
    payload: Mapping[str, Any], source: BehaviorEvidenceSource
) -> list[tuple[Mapping[str, Any], str]]:
    candidates: tuple[tuple[str, ...], ...]
    if source is BehaviorEvidenceSource.CAPE:
        candidates = (("behavior", "events"), ("signatures",), ("behaviors",))
    elif source is BehaviorEvidenceSource.TRIAGE:
        candidates = (("behaviors",), ("signatures",), ("analysis", "behaviors"))
    else:
        candidates = (("behaviors",),)
    rows, path = _first_list(payload, candidates)
    return [
        (item, f"{path}/{index}") for index, item in enumerate(rows) if isinstance(item, Mapping)
    ]


def _process(item: Mapping[str, Any], path: str) -> BehaviorProcessV1:
    pid = _first_value(item, "pid", "process_id", "id")
    parent = _first_value(item, "ppid", "parent_pid", "parent_process_id", "parent_id")
    process_id, process_id_truncated = _safe_text_with_truncation(pid, 128)
    parent_process_id, parent_process_id_truncated = _safe_text_with_truncation(parent, 128)
    name, name_truncated = _safe_text_with_truncation(
        _first_value(item, "process_name", "name", "image"), 256
    )
    command_line, command_line_truncated = _safe_text_with_truncation(
        _first_value(item, "command_line", "cmdline", "command"), 512
    )
    return BehaviorProcessV1(
        process_id=process_id or "unknown",
        parent_process_id=parent_process_id,
        name=name,
        command_line=command_line,
        started_at=_timestamp(_first_value(item, "first_seen", "started_at", "timestamp", "time")),
        evidence_path=path,
        truncated_fields=tuple(
            field
            for field, was_truncated in (
                ("process_id", process_id_truncated),
                ("parent_process_id", parent_process_id_truncated),
                ("name", name_truncated),
                ("command_line", command_line_truncated),
            )
            if was_truncated
        ),
    )


def _dropped_file(item: Mapping[str, Any], path: str) -> DroppedFileV1:
    digest = _first_value(item, "sha256", "sha_256", "hash")
    if isinstance(digest, Mapping):
        digest = digest.get("sha256")
    digest = (
        digest.lower() if isinstance(digest, str) and _SHA256.fullmatch(digest.lower()) else None
    )
    size = _first_value(item, "size", "filesize", "file_size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    file_path, path_truncated = _safe_text_with_truncation(
        _first_value(item, "path", "filepath", "filename", "name"), 512
    )
    return DroppedFileV1(
        path=file_path,
        sha256=digest,
        size=size,
        evidence_path=path,
        truncated_fields=("path",) if path_truncated else (),
    )


def _network_session(item: Mapping[str, Any], paths: tuple[str, ...]) -> NetworkSessionV1:
    source = item.get("src", item.get("source", {}))
    destination = item.get("dst", item.get("destination", {}))
    source = source if isinstance(source, Mapping) else {}
    destination = destination if isinstance(destination, Mapping) else {}
    source_ip = _normalize_ip(_first_value(item, "src_ip", "source_ip") or source.get("ip"))
    destination_ip = _normalize_ip(
        _first_value(item, "dst_ip", "destination_ip", "remote_ip") or destination.get("ip")
    )
    host_value = _first_value(item, "host", "domain", "hostname", "server_name", "dns")
    uri_value = _first_value(item, "uri", "url", "request", "http_url")
    host, uri, host_uri_truncated_fields = _host_and_uri(host_value, uri_value)
    protocol, protocol_truncated = _safe_text_with_truncation(
        _first_value(item, "protocol", "proto", "transport"), 32
    )
    truncated_fields = list(host_uri_truncated_fields)
    if protocol_truncated:
        truncated_fields.append("protocol")
    return NetworkSessionV1(
        protocol=protocol,
        source_ip=source_ip,
        source_port=_port(_first_value(item, "src_port", "source_port") or source.get("port")),
        destination_ip=destination_ip,
        destination_port=_port(
            _first_value(item, "dst_port", "destination_port", "remote_port")
            or destination.get("port")
        ),
        host=host,
        uri=uri,
        started_at=_timestamp(_first_value(item, "first_seen", "started_at", "timestamp", "time")),
        ended_at=_timestamp(_first_value(item, "last_seen", "ended_at", "end_time")),
        evidence_paths=paths,
        truncated_fields=tuple(truncated_fields),
    )


def _behavior(item: Mapping[str, Any], path: str) -> ObservedBehaviorV1:
    category, category_truncated = _safe_text_with_truncation(
        _first_value(item, "category", "name", "signature"), 128
    )
    description, description_truncated = _safe_text_with_truncation(
        _first_value(item, "description", "message", "behavior", "summary"), 1_000
    )
    process_id, process_id_truncated = _safe_text_with_truncation(
        _first_value(item, "pid", "process_id"), 128
    )
    return ObservedBehaviorV1(
        category=category or "observed",
        description=description or "",
        timestamp=_timestamp(_first_value(item, "timestamp", "time", "first_seen")),
        process_id=process_id,
        evidence_path=path,
        truncated_fields=tuple(
            field
            for field, was_truncated in (
                ("category", category_truncated),
                ("description", description_truncated),
                ("process_id", process_id_truncated),
            )
            if was_truncated
        ),
    )


def _find_report_timestamp(
    payload: Mapping[str, Any], source: BehaviorEvidenceSource
) -> datetime | None:
    candidates = (
        (("info", "started"), ("info", "timestamp"), ("started_at",), ("created_at",))
        if source is BehaviorEvidenceSource.CAPE
        else (("analysis", "created"), ("created_at",), ("submitted_at",), ("timestamp",))
    )
    for path in candidates:
        parsed = _timestamp(_at_path(payload, path))
        if parsed is not None:
            return parsed
    return None


def _host_and_uri(
    host_value: object, uri_value: object
) -> tuple[str | None, str | None, tuple[str, ...]]:
    host = _normalize_host(host_value) if isinstance(host_value, str) else None
    uri: str | None = None
    truncated_fields: list[str] = []
    if isinstance(uri_value, str) and uri_value.strip():
        raw_candidate = uri_value.strip()
        candidate = raw_candidate[:4_000]
        if len(raw_candidate) > len(candidate):
            truncated_fields.append("uri")
        if "://" in candidate:
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                parsed = None
            if parsed is not None:
                host = host or _normalize_host(parsed.hostname or "")
                if parsed.scheme.casefold() in {"http", "https", "ftp"} and parsed.hostname:
                    normalized_host = _normalize_host(parsed.hostname)
                    if normalized_host is not None:
                        netloc = (
                            f"[{normalized_host}]" if ":" in normalized_host else normalized_host
                        )
                        uri = urlunsplit(
                            (parsed.scheme.casefold(), netloc, parsed.path or "/", "", "")
                        )
                        if len(uri) > 1_000 and "uri" not in truncated_fields:
                            truncated_fields.append("uri")
        else:
            uri, uri_truncated = _safe_text_with_truncation(candidate, 1_000)
            if uri_truncated and "uri" not in truncated_fields:
                truncated_fields.append("uri")
    return host, uri, tuple(truncated_fields)


def _normalize_ip(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _normalize_host(value: str) -> str | None:
    value = value.strip().rstrip(".")
    if not value or len(value) > 253:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            normalized = value.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return None
        if any(
            not part or len(part) > 63 or re.fullmatch(r"[a-z0-9-]+", part) is None
            for part in normalized.split(".")
        ):
            return None
        return normalized


def _safe_text(value: object, maximum: int) -> str | None:
    return _safe_text_with_truncation(value, maximum)[0]


def _safe_text_with_truncation(value: object, maximum: int) -> tuple[str | None, bool]:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None, False
    truncated = len(str(value)) > maximum
    text = sanitize_evidence_text(value, max_chars=maximum)
    text = _HOME_PATH.sub("[home-path-redacted]", text)
    text = _WINDOWS_HOME.sub("[home-path-redacted]", text)
    text = _ENV_ASSIGNMENT.sub(r"\1=[redacted]", text)
    return text or None, truncated


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _port(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and 0 <= value <= 65535 else None


def _safe_scalar(value: object) -> str | None:
    return _safe_text(value, 128)


def _first_value(item: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _at_path(item: Mapping[str, Any], path: tuple[str, ...]) -> object:
    current: object = item
    for component in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(component)
    return current


def _first_list(
    payload: Mapping[str, Any], candidates: tuple[tuple[str, ...], ...]
) -> tuple[list[Any], str]:
    for path in candidates:
        value = _at_path(payload, path)
        if isinstance(value, list):
            return value, _pointer(path)
    return [], _pointer(candidates[0]) if candidates else "/"


def _pointer(path: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)
