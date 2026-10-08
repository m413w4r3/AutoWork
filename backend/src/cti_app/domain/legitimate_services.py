"""Versioned policy for generic, legitimate service hosts in published IOC sets."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit

from cti_app.domain.publication import ArtifactType

LEGITIMATE_SERVICES_POLICY_VERSION = "legitimate-services-v1"
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class LegitimateServicePolicyError(ValueError):
    """The checked-in legitimate-service policy is malformed or unsupported."""


class LegitimateServiceCategory(StrEnum):
    MESSAGING = "messaging"
    MESSAGING_API = "messaging_api"
    FILE_SHARING = "file_sharing"
    CLOUD_MESSAGING = "cloud_messaging"
    CLOUD_IDENTITY = "cloud_identity"
    GENERIC_OBJECT_HOSTING = "generic_object_hosting"
    CODE_HOSTING = "code_hosting"
    CLOUD_STORAGE = "cloud_storage"
    CLOUD_PRODUCTIVITY = "cloud_productivity"
    PASTE_SERVICE = "paste_service"
    FILE_TRANSFER = "file_transfer"


@dataclass(frozen=True, slots=True)
class LegitimateServiceV1:
    hostname: str
    category: LegitimateServiceCategory
    reason: str


@dataclass(frozen=True, slots=True)
class LegitimateServiceRegistry:
    entries: tuple[LegitimateServiceV1, ...]
    _lookup: Mapping[str, LegitimateServiceV1] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_lookup",
            MappingProxyType({entry.hostname: entry for entry in self.entries}),
        )

    def match_indicator(
        self, artifact_type: ArtifactType, value: str
    ) -> LegitimateServiceV1 | None:
        """Match only a bare exact hostname or a URL to its empty/root path."""
        if artifact_type is ArtifactType.DOMAIN:
            return self._lookup.get(value.strip().rstrip(".").casefold())
        if artifact_type is not ArtifactType.URL:
            return None

        try:
            parts = urlsplit(value.strip())
            hostname = parts.hostname
            # Accessing port performs urllib's validation of malformed ports.
            _ = parts.port
        except ValueError:
            return None
        if (
            not parts.scheme
            or not parts.netloc
            or hostname is None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
        ):
            return None
        return self._lookup.get(hostname.rstrip(".").casefold())

    def __iter__(self) -> Iterator[LegitimateServiceV1]:
        return iter(self.entries)


def _is_exact_hostname(value: str) -> bool:
    if not value or len(value) > 253 or value != value.lower() or value.endswith("."):
        return False
    labels = value.split(".")
    return len(labels) > 1 and all(_HOST_LABEL.fullmatch(label) for label in labels)


def validate_legitimate_services_document(document: object) -> LegitimateServiceRegistry:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "entries"}
        or document.get("schema_version") != LEGITIMATE_SERVICES_POLICY_VERSION
    ):
        raise LegitimateServicePolicyError("unsupported legitimate-services policy schema")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise LegitimateServicePolicyError("legitimate-services entries must be a list")

    entries: list[LegitimateServiceV1] = []
    seen: set[str] = set()
    for value in raw_entries:
        if not isinstance(value, dict) or set(value) != {"hostname", "category", "reason"}:
            raise LegitimateServicePolicyError("invalid legitimate-services entry")
        hostname = value["hostname"]
        category_value = value["category"]
        reason = value["reason"]
        if not isinstance(hostname, str) or not _is_exact_hostname(hostname):
            raise LegitimateServicePolicyError(
                "legitimate-services entries must be exact lowercase hostnames"
            )
        if hostname in seen:
            raise LegitimateServicePolicyError("duplicate legitimate-services hostname")
        if not isinstance(category_value, str):
            raise LegitimateServicePolicyError("invalid legitimate-services category")
        try:
            category = LegitimateServiceCategory(category_value)
        except ValueError as exc:
            raise LegitimateServicePolicyError("unknown legitimate-services category") from exc
        if not isinstance(reason, str) or not reason.strip():
            raise LegitimateServicePolicyError("legitimate-services entry needs a reason")
        seen.add(hostname)
        entries.append(LegitimateServiceV1(hostname, category, reason.strip()))
    return LegitimateServiceRegistry(tuple(entries))
