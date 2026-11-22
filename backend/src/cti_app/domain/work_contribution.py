"""Strict contracts for workspace contribution imports (M24-07, ADR-0011 D6).

A contribution is the *provenance* row of one workspace file revision, never a
second evidence store.  Three categories are closed:

* ``work_contribution`` — notes, scripts, reports, hypotheses.  It carries the
  exact blob and SHA but no canonical reference, so it can never be cited as
  evidence; a human hypothesis stays unverified.
* ``observation`` — one archived technical datum imported through
  ``EvidenceRecorder.record_existing_blob``.  ``(object_type, method)`` maps
  through the closed table below to an existing :class:`EvidenceKind`; provider
  kinds (VT, Shodan, claim ingestion) can never be produced here.
* ``finding`` — one version created through ``FindingService.propose`` with
  observations of the same investigation; an import never becomes
  ``supported`` on its own.

Because lot 07 may not widen the canonical evidence schema, the exact
``method``/``tool``/``tool_version``/``parameters``/origin/date of an imported
observation travel in one bounded, prefixed limitation line next to the raw
file blob (the same idiom as ``rule_corpus_import``).

The file-type guard reuses ``detect_file_format_magic``: executable images and
archives (PE/MZ, ELF, Mach-O, APK/ZIP and gzip) never enter contribution blob
storage.  Sample bytes therefore never flow through this module either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.errors import DomainError
from cti_app.domain.investigation import (
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    freeze_json,
    thaw_json,
)
from cti_app.domain.rule_workbench import detect_file_format_magic

WORK_CONTRIBUTION_SCHEMA_VERSION = "work-contribution.v1"
WORKSPACE_IMPORT_SOURCE = "workspace_import"
WORKSPACE_IMPORT_SOURCE_VERSION = WORK_CONTRIBUTION_SCHEMA_VERSION
DEFAULT_MAX_CONTRIBUTION_BYTES = 16 * 1024 * 1024

OBSERVATION_METADATA_PREFIX = "work_contribution_metadata:"
MAX_OBSERVATION_METADATA_CHARS = 1_900
MAX_OBSERVATION_LIMITATIONS = 8
MAX_OBSERVATION_LIMITATION_CHARS = 500
MAX_OBSERVATION_PARAMETERS_BYTES = 512
MAX_OBJECT_ID_CHARS = 512
MAX_TOOL_CHARS = 128
MAX_TOOL_VERSION_CHARS = 64
MAX_RELPATH_CHARS = 4_096

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# Formats refused anywhere in a contribution blob.  ``dos_mz`` covers a PE
# image whose header is truncated or packed, ``zip`` covers APK and any
# archive whose unpacking could become a zip bomb; ``gzip`` is the same class.
FORBIDDEN_CONTRIBUTION_FORMATS = frozenset(
    {
        "dos_mz",
        "pe",
        "pe_cli",
        "elf",
        "mach_o_32",
        "mach_o_64",
        "mach_o_fat",
        "zip",
        "gzip",
    }
)


class ContributionError(DomainError):
    """Base refusal for a contribution operation, carrying a stable code."""


class ContributionValidationError(ContributionError):
    """A declaration or persisted row violates the ADR-0011 D6 contract."""

    def __init__(
        self,
        code: WorkContributionValidationCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class WorkContributionCategory(StrEnum):
    WORK_CONTRIBUTION = "work_contribution"
    OBSERVATION = "observation"
    FINDING = "finding"


class WorkContributionImportStatus(StrEnum):
    IMPORTED = "imported"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class WorkContributionOrigin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    HOST = "host"


class WorkContributionObjectType(StrEnum):
    SAMPLE = "sample"
    FILE = "file"
    HOST = "host"
    IP = "ip"
    DOMAIN = "domain"
    URL = "url"


class WorkContributionMethod(StrEnum):
    HASH = "hash"
    STATIC_ANALYSIS = "static_analysis"
    STRING_EXTRACTION = "string_extraction"
    YARA_SCAN = "yara_scan"
    DYNAMIC_BEHAVIOUR = "dynamic_behaviour"
    NETWORK_CAPTURE_ANALYSIS = "network_capture_analysis"
    DNS_QUERY = "dns_query"
    HTTP_REQUEST = "http_request"
    NEGATIVE_CHECK = "negative_check"


class WorkContributionValidationCode(StrEnum):
    """Closed refusal vocabulary persisted in ``validation_error_code``."""

    INVALID_DECLARATION = "invalid_declaration"
    INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
    INVALID_FILE_RELPATH = "invalid_file_relpath"
    OBJECT_METHOD_NOT_ALLOWED = "object_method_not_allowed"
    OBSERVATION_METADATA_TOO_LARGE = "observation_metadata_too_large"
    FORBIDDEN_FILE_TYPE = "forbidden_file_type"
    CONTRIBUTION_TOO_LARGE = "contribution_too_large"
    EMPTY_CONTENT = "empty_content"
    INTERNAL_WORKSPACE_PATH = "internal_workspace_path"
    WORKSPACE_REVISION_CONTENT_MISMATCH = "workspace_revision_content_mismatch"
    OBSERVATION_REF_NOT_FOUND = "observation_ref_not_found"
    OBSERVATION_REF_INVESTIGATION_MISMATCH = "observation_ref_investigation_mismatch"
    ADVISORY_OBSERVATION_NOT_EVIDENCE = "advisory_observation_not_evidence"
    FINDING_CLAIM_REF_NOT_FOUND = "finding_claim_ref_not_found"
    FINDING_INVALID = "finding_invalid"
    SAMPLE_HASH_INVALID = "sample_hash_invalid"
    TASK_NOT_IN_INVESTIGATION = "task_not_in_investigation"
    LEASE_FENCE_REQUIRED = "lease_fence_required"
    LEASE_FENCE_STALE = "lease_fence_stale"
    LEASE_EXPIRED = "lease_expired"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _error(code: WorkContributionValidationCode, message: str) -> ContributionValidationError:
    return ContributionValidationError(code, message)


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must use {enum_type.__name__}")


def _require_sha256(value: str, field_name: str) -> str:
    if not _SHA256.fullmatch(value):
        raise _error(
            WorkContributionValidationCode.INVALID_DECLARATION,
            f"{field_name} must be lowercase SHA-256",
        )
    return value


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _error(
            WorkContributionValidationCode.INVALID_DECLARATION,
            f"{field_name} must be timezone-aware",
        )


def _bounded_text(value: str, field_name: str, *, max_chars: int) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > max_chars or "\x00" in stripped:
        raise _error(
            WorkContributionValidationCode.INVALID_DECLARATION,
            f"{field_name} must be non-empty, NUL-free and at most {max_chars} characters",
        )
    return stripped


def is_relative_workspace_path(value: str) -> bool:
    """Return whether a POSIX relative path cannot escape the workspace."""
    if not value or value.startswith("/") or "\x00" in value or value.endswith("/"):
        return False
    return all(segment not in {"", ".", ".."} for segment in value.split("/"))


def assert_contribution_blob_allowed(content: bytes) -> None:
    """Refuse executable images and archives before they reach blob storage.

    The explicit ``MZ`` test runs first because ``detect_file_format_magic``
    only classifies a DOS header of at least ``0x40`` bytes; a truncated MZ
    prefix must not fall through to the text branch.
    """
    if content.startswith(b"MZ"):
        raise _error(
            WorkContributionValidationCode.FORBIDDEN_FILE_TYPE,
            "executable images are not valid contribution blobs",
        )
    detected = detect_file_format_magic(content)
    if detected in FORBIDDEN_CONTRIBUTION_FORMATS:
        raise _error(
            WorkContributionValidationCode.FORBIDDEN_FILE_TYPE,
            f"file format {detected!r} is not a valid contribution blob",
        )


def is_internal_workspace_path(path: str) -> bool:
    """Return whether a path is an internal projection or materialized sample.

    ``.autowork/*`` files are rebuildable projections and ``base_samples/*``
    are read-only materialized samples; neither is a contribution.  Recording
    them as ``skipped`` keeps the workspace observation auditable without
    importing bytes.
    """
    return path == ".autowork" or path.startswith(".autowork/") or path.startswith("base_samples/")


def _build_kind_map() -> Mapping[
    tuple[WorkContributionObjectType, WorkContributionMethod], EvidenceKind
]:
    """The closed ``(object_type, method)`` table of ADR-0011 D6."""
    mapping: dict[tuple[WorkContributionObjectType, WorkContributionMethod], EvidenceKind] = {
        (WorkContributionObjectType.SAMPLE, WorkContributionMethod.HASH): EvidenceKind.OTHER,
        (WorkContributionObjectType.FILE, WorkContributionMethod.HASH): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.STATIC_ANALYSIS,
        ): EvidenceKind.BINARY_ANALYSIS,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.STATIC_ANALYSIS,
        ): EvidenceKind.BINARY_ANALYSIS,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.STRING_EXTRACTION,
        ): EvidenceKind.BINARY_STRING,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.STRING_EXTRACTION,
        ): EvidenceKind.BINARY_STRING,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.YARA_SCAN,
        ): EvidenceKind.YARA_MATCH,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.YARA_SCAN,
        ): EvidenceKind.YARA_MATCH,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.HOST,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
    }
    network_objects = (
        WorkContributionObjectType.HOST,
        WorkContributionObjectType.IP,
        WorkContributionObjectType.DOMAIN,
        WorkContributionObjectType.URL,
    )
    for object_type in network_objects:
        mapping[(object_type, WorkContributionMethod.NETWORK_CAPTURE_ANALYSIS)] = EvidenceKind.OTHER
        mapping[(object_type, WorkContributionMethod.DNS_QUERY)] = EvidenceKind.OTHER
        mapping[(object_type, WorkContributionMethod.HTTP_REQUEST)] = EvidenceKind.OTHER
    for object_type in WorkContributionObjectType:
        mapping[(object_type, WorkContributionMethod.NEGATIVE_CHECK)] = EvidenceKind.NEGATIVE_RESULT
    return MappingProxyType(mapping)


_CONTRIBUTION_KINDS = _build_kind_map()
WORK_CONTRIBUTION_KIND_MAP = _CONTRIBUTION_KINDS


def evidence_kind_for(
    object_type: WorkContributionObjectType, method: WorkContributionMethod
) -> EvidenceKind:
    """Return the closed ``(object_type, method)`` evidence kind or refuse."""
    _require_enum(object_type, WorkContributionObjectType, "object_type")
    _require_enum(method, WorkContributionMethod, "method")
    try:
        return _CONTRIBUTION_KINDS[(object_type, method)]
    except KeyError:
        raise _error(
            WorkContributionValidationCode.OBJECT_METHOD_NOT_ALLOWED,
            f"({object_type.value}, {method.value}) is not a workspace import pair",
        ) from None


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationDeclarationV1:
    """The exact technical datum an imported observation asserts."""

    object_type: WorkContributionObjectType
    method: WorkContributionMethod
    object_id: str
    tool: str
    tool_version: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        evidence_kind_for(self.object_type, self.method)
        object_id = _bounded_text(self.object_id, "object_id", max_chars=MAX_OBJECT_ID_CHARS)
        if self.object_type in {WorkContributionObjectType.SAMPLE, WorkContributionObjectType.FILE}:
            _require_sha256(object_id, "object_id")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "tool", _bounded_text(self.tool, "tool", max_chars=MAX_TOOL_CHARS))
        object.__setattr__(
            self,
            "tool_version",
            _bounded_text(self.tool_version, "tool_version", max_chars=MAX_TOOL_VERSION_CHARS),
        )
        frozen_parameters = freeze_json(self.parameters)
        encoded = json.dumps(
            thaw_json(frozen_parameters),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_OBSERVATION_PARAMETERS_BYTES:
            raise _error(
                WorkContributionValidationCode.OBSERVATION_METADATA_TOO_LARGE,
                "observation parameters exceed their bounded metadata budget",
            )
        object.__setattr__(self, "parameters", frozen_parameters)
        limits = tuple(self.limitations)
        if len(limits) > MAX_OBSERVATION_LIMITATIONS or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > MAX_OBSERVATION_LIMITATION_CHARS
            for item in limits
        ):
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "observation limitations are bounded non-empty strings",
            )
        object.__setattr__(self, "limitations", limits)
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")

    @property
    def evidence_kind(self) -> EvidenceKind:
        return evidence_kind_for(self.object_type, self.method)


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingDeclarationV1:
    """A finding proposed from observations of the same investigation."""

    kind: FindingKind
    assessment: FindingAssessment
    statement: str
    observation_refs: tuple[UUID, ...] = ()
    claim_refs: tuple[str, ...] = ()
    novelty: NoveltyClass | None = None
    scope: str = ""
    limitations: tuple[str, ...] = ()
    proposed_sample_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, FindingKind, "kind")
        _require_enum(self.assessment, FindingAssessment, "assessment")
        if self.novelty is not None:
            _require_enum(self.novelty, NoveltyClass, "novelty")
        statement = self.statement.strip()
        if not statement:
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "finding statement must not be empty",
            )
        object.__setattr__(self, "statement", statement)
        for observation_ref in self.observation_refs:
            if not isinstance(observation_ref, UUID):
                raise TypeError("observation_refs must contain UUID values")
        for claim_ref in self.claim_refs:
            if not isinstance(claim_ref, str) or not claim_ref.strip():
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "claim_refs must be non-empty strings",
                )
        object.__setattr__(self, "observation_refs", tuple(dict.fromkeys(self.observation_refs)))
        object.__setattr__(self, "claim_refs", tuple(dict.fromkeys(self.claim_refs)))
        object.__setattr__(self, "limitations", tuple(dict.fromkeys(self.limitations)))
        if self.proposed_sample_sha256 is not None:
            _require_sha256(self.proposed_sample_sha256, "proposed_sample_sha256")
        if self.kind is FindingKind.NEW_SAMPLE and self.proposed_sample_sha256 is None:
            raise _error(
                WorkContributionValidationCode.SAMPLE_HASH_INVALID,
                "a new-sample finding must name the proposed sample SHA-256",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContributionDeclarationV1:
    """The three-way category shape, validated before any I/O."""

    category: WorkContributionCategory = WorkContributionCategory.WORK_CONTRIBUTION
    observation: ObservationDeclarationV1 | None = None
    finding: FindingDeclarationV1 | None = None

    def __post_init__(self) -> None:
        _require_enum(self.category, WorkContributionCategory, "category")
        if self.category is WorkContributionCategory.WORK_CONTRIBUTION:
            if self.observation is not None or self.finding is not None:
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "a work contribution carries no observation or finding declaration",
                )
            return
        if self.category is WorkContributionCategory.OBSERVATION:
            if self.observation is None or self.finding is not None:
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "an observation import requires exactly one observation declaration",
                )
            return
        if self.finding is None or self.observation is not None:
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "a finding import requires exactly one finding declaration",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkContributionV1:
    """One immutable, append-only contribution row (ADR-0011 D2/D6)."""

    investigation_id: UUID
    workspace_id: str
    workspace_revision_id: str
    file_relpath: str
    file_sha256: str
    observed_at: datetime
    origin: WorkContributionOrigin
    idempotency_key: str
    category: WorkContributionCategory = WorkContributionCategory.WORK_CONTRIBUTION
    schema_version: str = WORK_CONTRIBUTION_SCHEMA_VERSION
    task_id: UUID | None = None
    origin_actor_id: str | None = None
    lease_fence: int | None = None
    content_blob_id: UUID | None = None
    content_sha256: str | None = None
    import_status: WorkContributionImportStatus = WorkContributionImportStatus.IMPORTED
    observation_id: UUID | None = None
    finding_id: UUID | None = None
    sample_association_id: UUID | None = None
    validation_error_code: WorkContributionValidationCode | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID) or not isinstance(self.id, UUID):
            raise TypeError("contribution and investigation ids must be UUIDs")
        _require_enum(self.category, WorkContributionCategory, "category")
        _require_enum(self.import_status, WorkContributionImportStatus, "import_status")
        _require_enum(self.origin, WorkContributionOrigin, "origin")
        if self.schema_version != WORK_CONTRIBUTION_SCHEMA_VERSION:
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                f"schema_version must be {WORK_CONTRIBUTION_SCHEMA_VERSION}",
            )
        if not self.workspace_id.strip() or not self.workspace_revision_id.strip():
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "workspace and revision identities must not be empty",
            )
        if not is_relative_workspace_path(self.file_relpath):
            raise _error(
                WorkContributionValidationCode.INVALID_FILE_RELPATH,
                "file_relpath must be a relative workspace path without '..'",
            )
        if len(self.file_relpath) > MAX_RELPATH_CHARS:
            raise _error(
                WorkContributionValidationCode.INVALID_FILE_RELPATH,
                "file_relpath exceeds its bounded length",
            )
        _require_sha256(self.file_sha256, "file_sha256")
        if self.content_sha256 is not None:
            _require_sha256(self.content_sha256, "content_sha256")
            if self.content_sha256 != self.file_sha256:
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "content_sha256 must equal file_sha256",
                )
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.created_at, "created_at")
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(self.idempotency_key):
            raise _error(
                WorkContributionValidationCode.INVALID_IDEMPOTENCY_KEY,
                "idempotency_key must match [A-Za-z0-9._:-]{8,128}",
            )
        if self.origin_actor_id is not None and not self.origin_actor_id.strip():
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "origin_actor_id must be a non-empty explicit identity or null",
            )
        if self.lease_fence is not None:
            if (
                isinstance(self.lease_fence, bool)
                or not isinstance(self.lease_fence, int)
                or self.lease_fence < 1
            ):
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "lease_fence must be a positive integer or null",
                )
        if self.task_id is None and self.origin is WorkContributionOrigin.AGENT:
            raise _error(
                WorkContributionValidationCode.LEASE_FENCE_REQUIRED,
                "an agent contribution requires a task link and a current lease fence",
            )
        if self.origin is WorkContributionOrigin.AGENT and self.lease_fence is None:
            raise _error(
                WorkContributionValidationCode.LEASE_FENCE_REQUIRED,
                "an agent contribution requires a current lease fence",
            )
        if (
            self.origin in {WorkContributionOrigin.HUMAN, WorkContributionOrigin.HOST}
            and self.lease_fence is not None
        ):
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "human and host contributions never claim lease authority",
            )
        imported = self.import_status is WorkContributionImportStatus.IMPORTED
        if imported != (self.validation_error_code is None):
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "only non-imported rows carry a validation_error_code",
            )
        if imported and (self.content_blob_id is None or self.content_sha256 is None):
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "an imported contribution archives its exact blob and content digest",
            )
        self._validate_category_refs()

    def _validate_category_refs(self) -> None:
        refs = (self.observation_id, self.finding_id, self.sample_association_id)
        for ref in refs:
            if ref is not None and not isinstance(ref, UUID):
                raise TypeError("canonical contribution references must be UUIDs or null")
        imported = self.import_status is WorkContributionImportStatus.IMPORTED
        if not imported:
            if any(ref is not None for ref in refs):
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "a non-imported contribution carries no canonical reference",
                )
            return
        if (
            self.sample_association_id is not None
            and self.category is not WorkContributionCategory.FINDING
        ):
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "only an imported finding may propose a sample association",
            )
        if self.category is WorkContributionCategory.WORK_CONTRIBUTION:
            if any(ref is not None for ref in refs):
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "a work contribution is never citable and carries no canonical reference",
                )
            return
        if self.category is WorkContributionCategory.OBSERVATION:
            if (
                self.observation_id is None
                or self.finding_id is not None
                or self.sample_association_id is not None
            ):
                raise _error(
                    WorkContributionValidationCode.INVALID_DECLARATION,
                    "an imported observation references exactly its evidence observation",
                )
            return
        if self.finding_id is None or self.observation_id is not None:
            raise _error(
                WorkContributionValidationCode.INVALID_DECLARATION,
                "an imported finding references exactly its finding version",
            )

    @property
    def citable_as_evidence(self) -> bool:
        """Whether this row produced an ``evidence_observation`` reference.

        Only an imported observation is citable as an evidence observation.
        An imported finding is canonical finding content (cited as a finding),
        and a work contribution is never evidence at all.
        """
        return (
            self.category is WorkContributionCategory.OBSERVATION
            and self.import_status is WorkContributionImportStatus.IMPORTED
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "workspace_id": self.workspace_id,
            "workspace_revision_id": self.workspace_revision_id,
            "file_relpath": self.file_relpath,
            "file_sha256": self.file_sha256,
            "category": self.category.value,
            "schema_version": self.schema_version,
            "import_status": self.import_status.value,
            "origin": self.origin.value,
            "origin_actor_id": self.origin_actor_id,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "lease_fence": self.lease_fence,
            "content_blob_id": (
                str(self.content_blob_id) if self.content_blob_id is not None else None
            ),
            "content_sha256": self.content_sha256,
            "observation_id": (
                str(self.observation_id) if self.observation_id is not None else None
            ),
            "finding_id": str(self.finding_id) if self.finding_id is not None else None,
            "sample_association_id": (
                str(self.sample_association_id) if self.sample_association_id is not None else None
            ),
            "validation_error_code": (
                self.validation_error_code.value if self.validation_error_code is not None else None
            ),
            "citable_as_evidence": self.citable_as_evidence,
            "idempotency_key": self.idempotency_key,
            "observed_at": self.observed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


def observation_metadata_line(
    *,
    declaration: ObservationDeclarationV1,
    file_sha256: str,
    workspace_revision_id: str,
    file_origin: str,
    observed_at: datetime,
) -> str:
    """Render the bounded, recoverable metadata limitation of an observation."""
    payload = {
        "schema": WORK_CONTRIBUTION_SCHEMA_VERSION,
        "category": WorkContributionCategory.OBSERVATION.value,
        "object_type": declaration.object_type.value,
        "method": declaration.method.value,
        "object_id": declaration.object_id,
        "tool": declaration.tool,
        "tool_version": declaration.tool_version,
        "parameters": declaration.parameters,
        "file_sha256": file_sha256,
        "workspace_revision_id": workspace_revision_id,
        "file_origin": file_origin,
        "observed_at": observed_at.isoformat(),
    }
    line = OBSERVATION_METADATA_PREFIX + json.dumps(
        thaw_json(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(line) > MAX_OBSERVATION_METADATA_CHARS:
        raise _error(
            WorkContributionValidationCode.OBSERVATION_METADATA_TOO_LARGE,
            "observation metadata exceeds its bounded limitation budget",
        )
    return line


__all__ = [
    "DEFAULT_MAX_CONTRIBUTION_BYTES",
    "FORBIDDEN_CONTRIBUTION_FORMATS",
    "IDEMPOTENCY_KEY_PATTERN",
    "MAX_OBSERVATION_METADATA_CHARS",
    "OBSERVATION_METADATA_PREFIX",
    "WORKSPACE_IMPORT_SOURCE",
    "WORKSPACE_IMPORT_SOURCE_VERSION",
    "WORK_CONTRIBUTION_KIND_MAP",
    "WORK_CONTRIBUTION_SCHEMA_VERSION",
    "ContributionDeclarationV1",
    "ContributionError",
    "ContributionValidationError",
    "FindingDeclarationV1",
    "ObservationDeclarationV1",
    "WorkContributionCategory",
    "WorkContributionImportStatus",
    "WorkContributionMethod",
    "WorkContributionObjectType",
    "WorkContributionOrigin",
    "WorkContributionV1",
    "WorkContributionValidationCode",
    "assert_contribution_blob_allowed",
    "evidence_kind_for",
    "is_internal_workspace_path",
    "is_relative_workspace_path",
    "observation_metadata_line",
]
