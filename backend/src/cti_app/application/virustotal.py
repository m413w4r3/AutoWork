from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class VirusTotalError(RuntimeError):
    code = "virustotal_error"
    retryable = False
    status_code: int | None = None
    retry_after: float | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class VirusTotalConfigurationError(VirusTotalError):
    code = "virustotal_transport_not_configured"


class VirusTotalCapabilityDisabledError(VirusTotalError):
    code = "virustotal_capability_disabled"


class VirusTotalRelationNotAllowedError(VirusTotalError):
    code = "virustotal_relation_not_allowed"


class VirusTotalInvalidInputError(VirusTotalError):
    code = "virustotal_invalid_input"


class VirusTotalPayloadError(VirusTotalError):
    code = "virustotal_payload_invalid"


class VirusTotalJsonError(VirusTotalError):
    code = "virustotal_json_invalid"


class VirusTotalResponseTooLargeError(VirusTotalError):
    code = "virustotal_response_too_large"


class VirusTotalUnexpectedRedirectError(VirusTotalError):
    code = "virustotal_unexpected_redirect"


class VirusTotalHttpError(VirusTotalError):
    pass


class VirusTotalTransportError(VirusTotalError):
    pass


@dataclass(frozen=True, slots=True)
class VirusTotalCapabilities:
    file_report: bool = False
    file_relationships: bool = False
    intelligence_search: bool = False
    file_download: bool = False
    submissions: bool = False
    behaviour_pcap: bool = False
    retrohunt: bool = False


class FileRelationship(StrEnum):
    CONTACTED_DOMAINS = "contacted_domains"
    CONTACTED_IPS = "contacted_ips"
    CONTACTED_URLS = "contacted_urls"
    DROPPED_FILES = "dropped_files"
    EXECUTION_PARENTS = "execution_parents"
    ITW_URLS = "itw_urls"
    EMBEDDED_URLS = "embedded_urls"
    SIMILAR_FILES = "similar_files"
    BUNDLED_FILES = "bundled_files"


@dataclass(frozen=True, slots=True)
class VirusTotalFile:
    id: str
    type: str
    lookup_value: str
    meaningful_name: str | None = None
    type_description: str | None = None
    size: int | None = None
    last_analysis_stats: dict[str, int] | None = None
    first_submission_date: int | None = None
    last_submission_date: int | None = None
    last_modification_date: int | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VirusTotalFileReport:
    file: VirusTotalFile
    raw_json: bytes


@dataclass(frozen=True, slots=True)
class VirusTotalPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    observed_count: int
    stopped_due_to_limit: bool
    exhaustive: bool
    raw_json_pages: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class VirusTotalSearchResult:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    observed_count: int
    stopped_due_to_limit: bool
    exhaustive: bool
    raw_json_pages: tuple[bytes, ...]


class VirusTotalPort(Protocol):
    async def file_report(self, file_hash: str) -> VirusTotalFileReport: ...

    async def file_relationship(
        self,
        file_hash: str,
        relation: FileRelationship,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage: ...

    async def intelligence_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalSearchResult: ...


_HASH_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)


def normalize_file_hash(value: str) -> str:
    candidate = value.strip().lower()
    if not _HASH_RE.fullmatch(candidate):
        raise VirusTotalInvalidInputError("Le hash fichier VirusTotal est invalide.")
    return candidate


def validate_search_query(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise VirusTotalInvalidInputError("La requête Intelligence Search est vide.")
    return candidate
