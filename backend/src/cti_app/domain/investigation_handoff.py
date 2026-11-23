"""Strict, immutable ``InvestigationHandoffV1`` contract (ADR-0011 D5).

A handoff is the transferable state of one investigation task between two
workshop parties.  ADR-0011 D1 makes the persisted handoff row the sole
authority for its content; this module owns the canonical JSON shape, its
SHA-256 and the exclusions the mandate places on the payload.

The contract references canonical objects, never copies them: work items cite
typed references, the ``refs`` section carries the identities and hashes of
observations, findings, human decisions and Bridge consultations, and ``files``
carries workspace revision metadata only.  A handoff is never evidence, and
Bridge is never an actor: it reaches a handoff only through a persisted
``advisor.consult`` invocation cited in ``refs.consultations``.

Exclusions are enforced here, in the domain validator rather than in a prompt:
operational credentials and secret-like values, signed URLs, raw transcripts,
malware bytes or their encodings, absolute host paths and unbounded logs.  The
canonical payload is capped at ``HANDOFF_MAX_CONTENT_BYTES`` (512 KiB, D5).
Canonicalisation reuses :func:`cti_app.domain.investigation.canonical_json_bytes`,
which is byte-identical to the ``AnalystInputPackV1`` helper.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cti_app.domain.errors import DomainError
from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_task import TaskStatus

INVESTIGATION_HANDOFF_SCHEMA_VERSION = "investigation-handoff.v1"
HANDOFF_MAX_CONTENT_BYTES = 512 * 1024
HANDOFF_MAX_TEXT_LENGTH = 16_384

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
# WORKSPACE-AGENT §6 identity: per-file revisions only, never content.
_REVISION_ID_PATTERN = r"^rev-[0-9a-f]{32}$"
_TEXT_TOO_LARGE_BYTES = 64 * 1024


class HandoffContractError(DomainError):
    """Base refusal for a payload that does not satisfy the V1 contract."""

    code = "handoff_invalid"


class HandoffContentRefused(HandoffContractError):
    """The payload carries material ADR-0011 D5 excludes from a handoff."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class HandoffActorKind(StrEnum):
    """Actors a handoff may name; ``bridge`` is deliberately absent."""

    HUMAN = "human"
    OPERATOR = "operator"
    SYSTEM = "system"


class HandoffRefKind(StrEnum):
    """Typed references a work item may cite.

    ``INSTRUCTION`` is additive (F3-D D8): a transfer cites the source
    session's unconsumed instructions so the destination inherits what was not
    delivered, without copying the queue row.  It is a coordination reference,
    not evidence, and carries no freshness family.
    """

    OBSERVATION = "observation"
    FINDING = "finding"
    DECISION = "decision"
    CONSULTATION = "consultation"
    FILE = "file"
    INSTRUCTION = "instruction"


class HandoffFreshnessKind(StrEnum):
    """Reference families recomputed by ``GET /handoffs/{id}/freshness``."""

    OBSERVATION = "observation"
    FINDING = "finding"
    DECISION = "decision"
    CONSULTATION = "consultation"
    FILE = "file"


class HandoffFreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def _uuid_text(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return value


def _reject_duplicate_ids(values: Any, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label} reference {value}")
        seen.add(value)


def _relative_file_path(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("file_relpath must not be empty")
    if normalized.startswith(("/", "\\")):
        raise ValueError("file_relpath must be relative")
    if "\\" in normalized or "\x00" in normalized:
        raise ValueError("file_relpath must use forward slashes")
    segments = normalized.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("file_relpath must not contain empty, '.' or '..' segments")
    return normalized


def _forbidden_key(value: str) -> str | None:
    return _FORBIDDEN_KEY_NORMALIZED.get(re.sub(r"[-_]", "", value.casefold()))


class HandoffActor(_WireModel):
    """One named actor; ``SYSTEM`` is only valid for ``created_by``."""

    kind: HandoffActorKind
    actor_id: str = Field(min_length=1, max_length=255)

    @field_validator("actor_id")
    @classmethod
    def _strip_actor_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("actor_id must not be empty")
        return normalized


class HandoffTaskSnapshot(_WireModel):
    """The task facts a recipient verifies before starting work.

    ``status_version`` and the assignment are the two values
    :func:`~cti_app.application.investigation_handoffs.assert_startable` checks:
    the mandate requires a changed status or assignment to refuse session start
    with ``handoff_task_stale`` (D5).  The assignment is therefore recorded next
    to the D5-listed status fields.
    """

    status: TaskStatus
    status_version: int = Field(ge=1)
    question: str = Field(max_length=HANDOFF_MAX_TEXT_LENGTH)
    objective: str = Field(max_length=HANDOFF_MAX_TEXT_LENGTH)
    expected_outputs: tuple[str, ...] = ()
    assigned_actor_kind: HandoffActorKind | None = None
    assigned_actor_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _validate_assignment(self) -> HandoffTaskSnapshot:
        if (self.assigned_actor_kind is None) != (self.assigned_actor_id is None):
            raise ValueError("task assignment requires both an actor kind and an actor id")
        if self.assigned_actor_kind is HandoffActorKind.SYSTEM:
            raise ValueError("a task is never assigned to the system actor")
        return self


class HandoffPlanRef(_WireModel):
    plan_version_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("plan_version_id")
    @classmethod
    def _validate_plan_version_id(cls, value: str) -> str:
        return _uuid_text(value, "plan_version_id")


class HandoffRef(_WireModel):
    """One typed reference attached to a work item."""

    kind: HandoffRefKind
    id: str = Field(min_length=1, max_length=128)


class HandoffWorkItem(_WireModel):
    text: str = Field(min_length=1, max_length=HANDOFF_MAX_TEXT_LENGTH)
    refs: tuple[HandoffRef, ...] = ()


class HandoffWork(_WireModel):
    """Hypotheses, attempts, results and limits, each with typed references."""

    hypotheses: tuple[HandoffWorkItem, ...] = ()
    attempts: tuple[HandoffWorkItem, ...] = ()
    established_results: tuple[HandoffWorkItem, ...] = ()
    partial_results: tuple[HandoffWorkItem, ...] = ()
    negative_results: tuple[HandoffWorkItem, ...] = ()
    limits: tuple[HandoffWorkItem, ...] = ()


class HandoffObservationRef(_WireModel):
    id: str
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _uuid_text(value, "observation id")


class HandoffFindingRef(_WireModel):
    version_id: str
    version: int = Field(ge=1)

    @field_validator("version_id")
    @classmethod
    def _validate_version_id(cls, value: str) -> str:
        return _uuid_text(value, "finding version_id")


class HandoffDecisionRef(_WireModel):
    analyst_decision_id: str

    @field_validator("analyst_decision_id")
    @classmethod
    def _validate_decision_id(cls, value: str) -> str:
        return _uuid_text(value, "analyst_decision_id")


class HandoffConsultationRef(_WireModel):
    invocation_id: str
    consultation_type: str = Field(min_length=1, max_length=64)
    output_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("invocation_id")
    @classmethod
    def _validate_invocation_id(cls, value: str) -> str:
        return _uuid_text(value, "consultation invocation_id")


class HandoffRefs(_WireModel):
    observations: tuple[HandoffObservationRef, ...] = ()
    findings: tuple[HandoffFindingRef, ...] = ()
    decisions: tuple[HandoffDecisionRef, ...] = ()
    consultations: tuple[HandoffConsultationRef, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicates(self) -> HandoffRefs:
        _reject_duplicate_ids((item.id for item in self.observations), "observation")
        _reject_duplicate_ids((item.version_id for item in self.findings), "finding")
        _reject_duplicate_ids((item.analyst_decision_id for item in self.decisions), "decision")
        _reject_duplicate_ids((item.invocation_id for item in self.consultations), "consultation")
        return self


class HandoffFileRef(_WireModel):
    """Metadata-only reference to one workspace file revision."""

    workspace_revision_id: str = Field(pattern=_REVISION_ID_PATTERN)
    file_relpath: str = Field(min_length=1, max_length=2_048)
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: datetime

    @field_validator("file_relpath")
    @classmethod
    def _validate_relpath(cls, value: str) -> str:
        return _relative_file_path(value)

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value)


class HandoffFreshnessRef(_WireModel):
    """A recorded reference value the freshness endpoint recomputes."""

    kind: HandoffFreshnessKind
    id: str = Field(min_length=1, max_length=128)
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_recorded_value(self) -> HandoffFreshnessRef:
        if (self.sha256 is None) == (self.version is None):
            raise ValueError("a freshness ref records exactly one of sha256 or version")
        if self.kind is HandoffFreshnessKind.FINDING:
            if self.version is None:
                raise ValueError("a finding freshness ref records a version")
            _uuid_text(self.id, "finding freshness id")
        elif self.kind is HandoffFreshnessKind.FILE:
            if re.fullmatch(_REVISION_ID_PATTERN, self.id) is None:
                raise ValueError("a file freshness ref records a workspace revision id")
        else:
            _uuid_text(self.id, f"{self.kind.value} freshness id")
        return self


class InvestigationHandoffV1(_WireModel):
    """The complete, canonical handoff payload stored in ``content``."""

    schema_version: Literal["investigation-handoff.v1"] = "investigation-handoff.v1"
    handoff_id: str
    investigation_id: str
    task_id: str
    version: int = Field(ge=1)
    created_at: datetime
    from_actor: HandoffActor
    to_actor: HandoffActor
    task: HandoffTaskSnapshot
    plan: HandoffPlanRef | None = None
    work: HandoffWork = HandoffWork()
    refs: HandoffRefs = HandoffRefs()
    files: tuple[HandoffFileRef, ...] = ()
    next_action: str = Field(min_length=1, max_length=HANDOFF_MAX_TEXT_LENGTH)
    freshness_refs: tuple[HandoffFreshnessRef, ...] = ()

    @field_validator("handoff_id", "investigation_id", "task_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _uuid_text(value, "handoff identity")

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("next_action")
    @classmethod
    def _strip_next_action(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("next_action must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_contract(self) -> InvestigationHandoffV1:
        for actor in (self.from_actor, self.to_actor):
            if actor.kind is HandoffActorKind.SYSTEM:
                raise ValueError("handoff parties are human or operator, never system")
        _reject_duplicate_ids((item.workspace_revision_id for item in self.files), "file revision")
        self._validate_freshness_coverage()
        payload = self.model_dump(mode="json")
        assert_handoff_content_allowed(payload)
        encoded = canonical_json_bytes(payload)
        if len(encoded) > HANDOFF_MAX_CONTENT_BYTES:
            raise ValueError(f"handoff content exceeds {HANDOFF_MAX_CONTENT_BYTES} canonical bytes")
        return self

    def _validate_freshness_coverage(self) -> None:
        """Every carried reference has exactly one freshness ref, and vice versa.

        A decision freshness ref records a digest the service derives from the
        canonical ``analyst_decisions`` row; the refs section only carries its
        identity, so only the identity is cross-checked here.
        """
        expected: dict[tuple[HandoffFreshnessKind, str], int | str | None] = {}
        for observation in self.refs.observations:
            expected[(HandoffFreshnessKind.OBSERVATION, observation.id)] = observation.sha256
        for finding in self.refs.findings:
            expected[(HandoffFreshnessKind.FINDING, finding.version_id)] = finding.version
        for decision in self.refs.decisions:
            expected[(HandoffFreshnessKind.DECISION, decision.analyst_decision_id)] = None
        for consultation in self.refs.consultations:
            expected[(HandoffFreshnessKind.CONSULTATION, consultation.invocation_id)] = (
                consultation.output_sha256
            )
        for file in self.files:
            expected[(HandoffFreshnessKind.FILE, file.workspace_revision_id)] = file.file_sha256

        observed: dict[tuple[HandoffFreshnessKind, str], HandoffFreshnessRef] = {}
        for ref in self.freshness_refs:
            key = (ref.kind, ref.id)
            if key in observed:
                raise ValueError(f"duplicate freshness ref {ref.kind.value}:{ref.id}")
            observed[key] = ref
        missing = sorted(
            f"{kind.value}:{identifier}"
            for (kind, identifier) in expected
            if (kind, identifier) not in observed
        )
        if missing:
            raise ValueError(f"freshness refs must cover every reference: {missing}")
        unknown = sorted(
            f"{kind.value}:{identifier}"
            for (kind, identifier) in observed
            if (kind, identifier) not in expected
        )
        if unknown:
            raise ValueError(f"freshness refs cite references the handoff omits: {unknown}")
        for key, recorded in expected.items():
            if recorded is None:
                continue
            ref = observed[key]
            actual: int | str | None = ref.version if ref.version is not None else ref.sha256
            if actual != recorded:
                raise ValueError(
                    f"freshness ref {key[0].value}:{key[1]} disagrees with the cited reference"
                )

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: Any) -> InvestigationHandoffV1:
        return cls.model_validate(payload)


# ---------------------------------------------------------------------------
# D5 exclusions: enforced on the canonical payload, never by a prompt.
# ---------------------------------------------------------------------------

_FORBIDDEN_KEY_NORMALIZED = {
    re.sub(r"[-_]", "", name.casefold()): name
    for name in (
        "api_key",
        "apikey",
        "access_key",
        "access_token",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "client_key",
        "consumer_key",
        "consumer_secret",
        "cookie",
        "credentials",
        "id_token",
        "oauth_token",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "set_cookie",
        "signature",
        "signed_url",
        "api_secret",
        # Raw transcripts, dumps and logs are coordination artefacts, not
        # handoff content: the handoff cites persisted objects instead.
        "transcript",
        "transcripts",
        "raw_transcript",
        "raw_transcripts",
        "raw_log",
        "raw_logs",
        "log",
        "logs",
        "stdout",
        "stderr",
        "console_output",
        "dump",
        "hex_dump",
        # Sample/malware bytes and their encodings.
        "bytes",
        "raw_bytes",
        "sample_bytes",
        "payload_bytes",
        "base64",
        "data_url",
        "data_urls",
    )
}
_SIGNED_URL_MARKERS = (
    "x-amz-signature=",
    "x-goog-signature=",
    "x-amz-credential=",
    "x-amz-security-token=",
    "awsaccesskeyid=",
    "signature=",
    "sig=",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    # ``name=value`` credential assignments in prose.
    re.compile(
        r"\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|session[_-]?token"
        r"|client[_-]?secret|password|passwd|passphrase)\s*[=:]\s*['\"]?[A-Za-z0-9._~+/-]{6,}",
        re.IGNORECASE,
    ),
)
_HOST_PATH_PATTERN = re.compile(
    r"(?<![\w.-])/(?:home|Users|root|var|tmp|opt|etc|srv|mnt|media|private)/"
)
_WINDOWS_HOST_PATH_PATTERN = re.compile(
    r"[A-Za-z]:\\(?:Users|Windows|ProgramData)\\", re.IGNORECASE
)
_TRANSCRIPT_USER = re.compile(r'"role"\s*:\s*"user"', re.IGNORECASE)
_TRANSCRIPT_ASSISTANT = re.compile(r'"role"\s*:\s*"assistant"', re.IGNORECASE)
_HEX_BLOB = re.compile(r"[0-9a-fA-F]+")
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=\r\n]+")
_BINARY_MAGICS = (
    b"MZ",
    b"\x7fELF",
    b"PK\x03\x04",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)


def assert_handoff_content_allowed(payload: Any) -> None:
    """Refuse credentials, signed URLs, transcripts, bytes, host paths, logs.

    Raises :class:`HandoffContentRefused` with a stable code.  The check runs on
    the canonical payload, so a value cannot slip through a nested object.
    """
    _scan_value(payload, "content")


def _scan_value(value: Any, path: str) -> None:
    if isinstance(value, BaseModel):
        _scan_value(value.model_dump(mode="json"), path)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise HandoffContentRefused(
                    "handoff_invalid", f"{path}: JSON object keys must be strings"
                )
            forbidden = _forbidden_key(key)
            if forbidden is not None:
                raise HandoffContentRefused(
                    "handoff_secret_or_raw_material_forbidden",
                    f"{path}.{key}: forbidden field name ({forbidden})",
                )
            _scan_value(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_value(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        _scan_string(value, path)


def _scan_string(value: str, path: str) -> None:
    if len(value.encode("utf-8")) > _TEXT_TOO_LARGE_BYTES:
        raise HandoffContentRefused(
            "handoff_unbounded_log_forbidden",
            f"{path}: free text exceeds the bounded handoff log size",
        )
    lowered = value.casefold()
    if lowered.startswith("data:") and "base64," in lowered[:128]:
        raise HandoffContentRefused(
            "handoff_binary_content_forbidden", f"{path}: data URL is not handoff content"
        )
    if "?" in lowered or "://" in lowered:
        if any(marker in lowered for marker in _SIGNED_URL_MARKERS):
            raise HandoffContentRefused(
                "handoff_signed_url_forbidden", f"{path}: signed URL material"
            )
    if "://" in value:
        authority = value.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority:
            raise HandoffContentRefused(
                "handoff_secret_material_forbidden",
                f"{path}: credential-bearing URL",
            )
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            raise HandoffContentRefused(
                "handoff_secret_material_forbidden", f"{path}: credential-like value"
            )
    if _TRANSCRIPT_USER.search(value) and _TRANSCRIPT_ASSISTANT.search(value):
        raise HandoffContentRefused(
            "handoff_transcript_forbidden", f"{path}: raw conversation transcript"
        )
    if "://" not in value and _HOST_PATH_PATTERN.search(value):
        raise HandoffContentRefused("handoff_host_path_forbidden", f"{path}: absolute host path")
    if "://" not in value and _WINDOWS_HOST_PATH_PATTERN.search(value):
        raise HandoffContentRefused("handoff_host_path_forbidden", f"{path}: absolute host path")
    _scan_binary_encoding(value, path)


def _scan_binary_encoding(value: str, path: str) -> None:
    if len(value) < 2_048:
        return
    if len(value) % 2 == 0 and _HEX_BLOB.fullmatch(value):
        raise HandoffContentRefused(
            "handoff_binary_content_forbidden", f"{path}: hexadecimal byte blob"
        )
    if not _BASE64_BLOB.fullmatch(value):
        return
    compact = re.sub(r"\s+", "", value)
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
    except (binascii.Error, ValueError):
        return
    if any(decoded.startswith(magic) for magic in _BINARY_MAGICS):
        raise HandoffContentRefused(
            "handoff_binary_content_forbidden", f"{path}: encoded sample bytes"
        )
    printable = sum(byte in b"\n\r\t" or 32 <= byte < 127 for byte in decoded[:512])
    if decoded and printable / len(decoded[:512]) < 0.7:
        raise HandoffContentRefused(
            "handoff_binary_content_forbidden", f"{path}: encoded binary payload"
        )
