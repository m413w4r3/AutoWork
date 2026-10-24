"""Fail-closed checks for secret material in PDF render-data."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from cti_app.application.typst_compilation import TypstCompilationError
from cti_app.config import get_settings

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HASH = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
_DOCUMENTED_HASH_CONTEXT = re.compile(r"\b(?:hash|sha-?256|sha-?1|md5|empreinte|digest)\b", re.I)
_DOCUMENTED_IP_CONTEXT = re.compile(r"\b(?:ip|ipv4|ipv6|ip address|adresse ip)\b", re.I)
_DOCUMENTED_DOMAIN_CONTEXT = re.compile(
    r"\b(?:domain|domain name|hostname|domaine|nom de domaine)\b", re.I
)
_SECRET_REDACTION = "[clé masquée]"
_OPERATIONAL_SETTING_FIELDS = frozenset(
    {
        "operator_api_token",
        "operator_client_api_token",
        "postgres_dsn",
        "redis_url",
        "s3_access_key",
        "s3_secret_key",
    }
)
_OPERATIONAL_FIELD_NAME = re.compile(
    r"^(?:operator(?:_[a-z0-9]+)*_token|"
    r"(?:vt|virustotal|shodan)(?:_api)?_(?:key|token)|"
    r"(?:openai_bridge|qwen|webai)_api_key|"
    r"(?:postgres|database|redis|s3|minio|bridge|service)_(?:dsn|url|username|password|pass|secret|access_key|secret_key)|"
    r"(?:pre)?signed_url)$",
    re.I,
)
_OPERATIONAL_ENV_NAME = re.compile(
    r"^(?:OPERATOR_[A-Z0-9_]*TOKEN|(?:VT|VIRUSTOTAL|SHODAN)_API_(?:KEY|TOKEN)|"
    r"(?:OPENAI_BRIDGE|QWEN|WEBAI)_API_KEY|"
    r"(?:DATABASE|POSTGRES|REDIS|S3|MINIO|BRIDGE|SERVICE)_(?:DSN|URL|USER(?:NAME)?|PASSWORD|PASS|SECRET(?:_KEY)?|ACCESS_KEY)|"
    r"(?:PRE)?SIGNED_URL)$",
    re.I,
)
_OPERATIONAL_ASSIGNMENT = re.compile(
    r"(?i)(?P<label>\b(?:OPERATOR_[A-Z0-9_]*TOKEN|"
    r"(?:VT|VIRUSTOTAL|SHODAN)_API_(?:KEY|TOKEN)|"
    r"(?:OPENAI_BRIDGE|QWEN|WEBAI)_API_KEY|"
    r"(?:DATABASE|POSTGRES|REDIS|S3|MINIO|BRIDGE|SERVICE)_(?:DSN|URL|USER(?:NAME)?|PASSWORD|PASS|SECRET(?:_KEY)?|ACCESS_KEY)|"
    r"(?:PRE)?SIGNED_URL)\b\s*[:=]\s*)"
    r"(?P<value>\[clé masquée\]|\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SERVICE_DSN = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|redis|rediss|mysql|mariadb|mongodb(?:\+srv)?)://[^\s,;<>\"']+"
)
_URL = re.compile(r"(?i)\bhttps?://[^\s,;<>\"']+")
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "sig",
        "signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "googleaccessid",
    }
)
_IOC_VALUE_FIELDS = frozenset(
    {"value", "value_spans", "normalized_value", "values", "hashes", "ips", "domains"}
)
_DIGEST_FIELD_NAMES = frozenset({"md5", "sha1", "sha256"})
_HOST_PATH_SETTING_SUFFIXES = ("_path", "_root", "_directory", "_dir")


@dataclass(frozen=True, slots=True)
class PublicationSecretFinding:
    location: str
    secret_type: str


class PublicationSecretDetectedError(TypstCompilationError):
    """A safe, stable render refusal that never includes the matched value."""

    code = "publication_secret_detected"

    def __init__(self, finding: PublicationSecretFinding) -> None:
        self.location = finding.location
        self.secret_type = finding.secret_type
        super().__init__(
            f"publication_secret_detected at {finding.location} ({finding.secret_type})"
        )


def guard_publication_render_data(
    render_data: Mapping[str, Any], *, settings: Any | None = None
) -> None:
    """Reject AutoWork operational secrets while allowing published CTI material."""
    operational_values = _operational_values(settings)
    for text, path, field_name, _ioc_type in _rendered_strings(render_data):
        secret_type = _operational_secret_type(text, field_name, operational_values)
        if secret_type is not None:
            raise PublicationSecretDetectedError(
                PublicationSecretFinding(
                    location=_redact_known_values(path, operational_values), secret_type=secret_type
                )
            )


def redact_operational_secrets(value: Any, *, settings: Any | None = None) -> tuple[Any, int]:
    """Redact AutoWork operational secrets before an advisor payload reaches Bridge."""
    operational_values = _operational_values(settings)
    count = 0

    def walk(item: Any, *, field_name: str = "") -> Any:
        nonlocal count
        if isinstance(item, Mapping):
            cleaned: dict[Any, Any] = {}
            for key, child in item.items():
                if _is_operational_field_name(str(key)) and child not in (
                    None,
                    False,
                    True,
                    _SECRET_REDACTION,
                ):
                    cleaned[key] = _SECRET_REDACTION
                    count += 1
                else:
                    cleaned[key] = walk(child, field_name=str(key))
            return cleaned
        if isinstance(item, list):
            return [walk(child, field_name=field_name) for child in item]
        if isinstance(item, tuple):
            return tuple(walk(child, field_name=field_name) for child in item)
        if isinstance(item, Path):
            item = str(item)
        if not isinstance(item, str):
            return item
        redacted_text, replacements = _redact_operational_text(item, operational_values)
        count += replacements
        if not replacements and _is_operational_field_name(field_name) and item.strip():
            return _SECRET_REDACTION
        return redacted_text

    return walk(value), count


def contains_operational_secrets(value: Any, *, settings: Any | None = None) -> bool:
    cleaned, count = redact_operational_secrets(value, settings=settings)
    return count > 0 or cleaned != value


def _operational_values(settings: Any | None) -> tuple[tuple[str, str, bool], ...]:
    configured = settings if settings is not None else get_settings()
    values: dict[tuple[str, str, bool], None] = {}
    model_fields = getattr(type(configured), "model_fields", {})
    field_names = (
        model_fields.keys()
        if isinstance(model_fields, Mapping) and model_fields
        else vars(configured)
    )
    for name in field_names:
        value = getattr(configured, name, None)
        if value is None:
            continue
        secret_value = getattr(value, "get_secret_value", None)
        if callable(secret_value):
            _add_operational_value(values, secret_value(), "service_credential")
        elif isinstance(value, Path):
            _add_operational_value(values, str(value), "host_path", case_insensitive=True)
            try:
                _add_operational_value(
                    values, str(value.expanduser().resolve()), "host_path", case_insensitive=True
                )
            except (OSError, RuntimeError):
                pass
        elif isinstance(value, str):
            normalized_name = _normalized_field_name(str(name))
            if normalized_name in _OPERATIONAL_SETTING_FIELDS or _is_operational_field_name(
                normalized_name
            ):
                _add_operational_value(values, value, _operational_field_type(normalized_name))
            elif normalized_name.endswith(_HOST_PATH_SETTING_SUFFIXES):
                _add_operational_value(values, value, "host_path", case_insensitive=True)
            elif _is_sensitive_url(value):
                _add_operational_value(values, value, "signed_url")

    for name, value in os.environ.items():
        if _OPERATIONAL_ENV_NAME.fullmatch(name) and value:
            _add_operational_value(values, value, _operational_field_type(name))
        elif name.casefold() in {"home", "pwd", "tmpdir", "temp", "tmp"} and value:
            _add_operational_value(values, value, "host_path", case_insensitive=True)

    for host_path in (Path.cwd(), Path.home()):
        _add_operational_value(values, str(host_path), "host_path", case_insensitive=True)
    return tuple(
        (value, secret_type, case_insensitive)
        for value, secret_type, case_insensitive in sorted(
            values, key=lambda item: len(item[0]), reverse=True
        )
    )


def _add_operational_value(
    values: dict[tuple[str, str, bool], None],
    value: Any,
    secret_type: str,
    *,
    case_insensitive: bool = False,
) -> None:
    if not isinstance(value, str) or not value or value == _SECRET_REDACTION:
        return
    values.setdefault((value, secret_type, case_insensitive), None)


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[\s.-]+", "_", value.casefold()).strip("_")


def _is_operational_field_name(value: str) -> bool:
    return _OPERATIONAL_FIELD_NAME.fullmatch(_normalized_field_name(value)) is not None


def is_operational_secret_field_name(value: str) -> bool:
    """Expose the operational-secret label predicate to content projections."""
    return _is_operational_field_name(value)


def _operational_field_type(value: str) -> str:
    name = _normalized_field_name(value)
    if "dsn" in name or name.endswith("_url"):
        return "service_connection_string"
    if name.endswith(("_api_key", "_access_key", "_secret_key", "_token")):
        return "service_credential"
    if "password" in name or name.endswith("_pass"):
        return "service_password"
    if "signed_url" in name:
        return "signed_url"
    return "operational_secret"


def _is_sensitive_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        query_names = {
            name.casefold() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        return (
            parsed.username is not None
            or parsed.password is not None
            or bool(query_names & _SENSITIVE_QUERY_NAMES)
        )
    except ValueError:
        return False


def _operational_secret_type(
    text: str, field_name: str, operational_values: tuple[tuple[str, str, bool], ...]
) -> str | None:
    if _is_operational_field_name(field_name) and text.strip() not in {"", _SECRET_REDACTION}:
        return _operational_field_type(field_name)
    for value, secret_type, case_insensitive in operational_values:
        found = value.casefold() in text.casefold() if case_insensitive else value in text
        if found:
            return secret_type
    assignment = _OPERATIONAL_ASSIGNMENT.search(text)
    if assignment is not None and assignment.group("value").strip("\"'`") != _SECRET_REDACTION:
        return _operational_field_type(assignment.group("label"))
    if _SERVICE_DSN.search(text):
        return "service_connection_string"
    for match in _URL.finditer(text):
        if _is_sensitive_url(match.group()):
            parsed = urlsplit(match.group())
            return (
                "service_connection_string" if parsed.username or parsed.password else "signed_url"
            )
    return None


def _redact_operational_text(
    text: str, operational_values: tuple[tuple[str, str, bool], ...]
) -> tuple[str, int]:
    count = 0

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal count
        if match.group("value").strip("\"'`") == _SECRET_REDACTION:
            return match.group()
        count += 1
        return f"{match.group('label')}{_SECRET_REDACTION}"

    cleaned = _OPERATIONAL_ASSIGNMENT.sub(replace_assignment, text)
    cleaned, dsn_count = _SERVICE_DSN.subn(_SECRET_REDACTION, cleaned)
    count += dsn_count

    def replace_sensitive_url(match: re.Match[str]) -> str:
        nonlocal count
        url = match.group()
        if not _is_sensitive_url(url):
            return url
        count += 1
        return _SECRET_REDACTION

    cleaned = _URL.sub(replace_sensitive_url, cleaned)
    for value, _secret_type, case_insensitive in operational_values:
        if value == _SECRET_REDACTION:
            continue
        flags = re.IGNORECASE if case_insensitive else 0
        cleaned, replacements = re.subn(re.escape(value), _SECRET_REDACTION, cleaned, flags=flags)
        count += replacements
    return cleaned, count


def _redact_known_values(text: str, operational_values: tuple[tuple[str, str, bool], ...]) -> str:
    cleaned, _count = _redact_operational_text(text, operational_values)
    return cleaned


def _rendered_strings(
    value: Any,
    *,
    path: str = "render-data",
    field_name: str = "",
    ioc_type: str | None = None,
) -> Iterator[tuple[str, str, str, str | None]]:
    if isinstance(value, Mapping):
        declared_type = _recognized_ioc_type(value.get("artifact_type", value.get("type")))
        table_cell_types = _table_cell_ioc_types(value)
        for key, child in value.items():
            name = str(key)
            normalized_name = name.casefold()
            if normalized_name == "rows" and isinstance(child, (list, tuple)):
                cell_index = 0
                for row_index, row in enumerate(child):
                    if not isinstance(row, (list, tuple)):
                        yield from _rendered_strings(
                            row,
                            path=f"{path}.{name}[{row_index}]",
                            field_name=name,
                            ioc_type=ioc_type,
                        )
                        continue
                    for column_index, cell in enumerate(row):
                        cell_type = (
                            table_cell_types[cell_index]
                            if cell_index < len(table_cell_types)
                            else ioc_type
                        )
                        yield from _rendered_strings(
                            cell,
                            path=f"{path}.{name}[{row_index}][{column_index}]",
                            field_name="value" if cell_type is not None else name,
                            ioc_type=cell_type,
                        )
                        cell_index += 1
                continue
            if normalized_name == "semantic_cells" and isinstance(child, (list, tuple)):
                for cell_index, cell in enumerate(child):
                    cell_type = (
                        table_cell_types[cell_index]
                        if cell_index < len(table_cell_types)
                        else ioc_type
                    )
                    yield from _rendered_strings(
                        cell,
                        path=f"{path}.{name}[{cell_index}]",
                        field_name="value" if cell_type is not None else name,
                        ioc_type=cell_type,
                    )
                continue
            if normalized_name in _DIGEST_FIELD_NAMES or normalized_name.endswith(
                ("_sha256", "_sha1", "_md5", "sha256_spans", "sha1_spans", "md5_spans")
            ):
                digest_value = child.get("text") if isinstance(child, Mapping) else child
                if isinstance(digest_value, str) and _HASH.fullmatch(digest_value.strip()):
                    continue
            if name.casefold().endswith("sha256s") and isinstance(child, (list, tuple)):
                if all(isinstance(item, str) and _HASH.fullmatch(item.strip()) for item in child):
                    continue
            child_type = declared_type or ioc_type
            if name.casefold() in {"hashes", "ips", "domains"}:
                child_type = {"hashes": "hash", "ips": "ip", "domains": "domain"}[name.casefold()]
            child_path = f"{path}.{name}"
            yield from _rendered_strings(
                child,
                path=child_path,
                field_name=name,
                ioc_type=child_type if name.casefold() in _IOC_VALUE_FIELDS else ioc_type,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _rendered_strings(
                child,
                path=f"{path}[{index}]",
                field_name=field_name,
                ioc_type=ioc_type,
            )
        return
    if isinstance(value, str):
        if not _UUID.fullmatch(value.strip()):
            yield value, path, field_name, ioc_type


def _recognized_ioc_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold().replace("-", "").replace("_", "")
    aliases = {
        "hash": "hash",
        "md5": "md5",
        "sha1": "sha1",
        "sha256": "sha256",
        "ip": "ip",
        "ipv4": "ipv4",
        "ipv6": "ipv6",
        "domain": "domain",
    }
    return aliases.get(normalized)


def _ioc_type_from_label(value: str) -> str | None:
    normalized = value.strip().casefold()
    declared = _recognized_ioc_type(normalized)
    if declared is not None:
        return declared
    if _DOCUMENTED_HASH_CONTEXT.search(normalized):
        return "hash"
    if _DOCUMENTED_IP_CONTEXT.search(normalized):
        return "ip"
    if _DOCUMENTED_DOMAIN_CONTEXT.search(normalized):
        return "domain"
    return None


def _table_cell_ioc_types(table: Mapping[str, Any]) -> tuple[str | None, ...]:
    rows = table.get("rows")
    columns = table.get("columns")
    if not isinstance(rows, (list, tuple)):
        return ()
    column_types = (
        tuple(
            _ioc_type_from_label(column) if isinstance(column, str) else None for column in columns
        )
        if isinstance(columns, (list, tuple))
        else ()
    )
    cell_types: list[str | None] = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        row_type = next(
            (
                label_type
                for cell in row
                if isinstance(cell, str)
                if (label_type := _ioc_type_from_label(cell)) is not None
            ),
            None,
        )
        for column_index, _cell in enumerate(row):
            column_type = column_types[column_index] if column_index < len(column_types) else None
            cell_types.append(column_type or row_type)
    return tuple(cell_types)


def _valid_ioc_value(value: str, ioc_type: str | None) -> bool:
    candidate = value.strip()
    if ioc_type in {"hash", "md5", "sha1", "sha256"}:
        return bool(_HASH.fullmatch(candidate))
    if ioc_type in {"ip", "ipv4", "ipv6"}:
        candidate = candidate.replace("[.]", ".").replace("(.)", ".")
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return False
        return ioc_type == "ip" or address.version == (4 if ioc_type == "ipv4" else 6)
    if ioc_type == "domain":
        candidate = candidate.replace("[.]", ".").replace("(.)", ".").rstrip(".")
        return bool(_DOMAIN.fullmatch(candidate))
    return False


__all__ = [
    "PublicationSecretDetectedError",
    "PublicationSecretFinding",
    "contains_operational_secrets",
    "guard_publication_render_data",
    "redact_operational_secrets",
]
