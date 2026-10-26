"""Pure identity and authority contracts for one physical external request.

This module deliberately has no database, HTTP, settings, or provider imports.
Credential-bearing wire bytes are carried only by ``FrozenWireRequest`` and
are excluded from representations and persistence helpers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit
from uuid import UUID

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_DEFAULT_OCCURRENCE_JSON = b'{"kind":"default","version":"effect-occurrence.v1"}'


class ExternalEffectValidationError(ValueError):
    """A prepared effect is malformed or does not match its declared digest."""


class EffectClaimState(StrEnum):
    CONSUMED = "CONSUMED"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    DEFINITIVELY_NOT_SUBMITTED = "DEFINITIVELY_NOT_SUBMITTED"


class ExternalEffectAuthorityKind(StrEnum):
    HUMAN_G2 = "HUMAN_G2"
    OSINT_POLICY = "OSINT_POLICY"


class ExternalEffectUseKind(StrEnum):
    SENT = "SENT"
    REUSED = "REUSED"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A stable reference to versioned evidence, never the evidence bytes."""

    kind: str
    id: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        for name in ("kind", "id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ExternalEffectValidationError(f"evidence reference {name} is required")
        _require_sha256(self.sha256, "evidence reference sha256")
        _validate_safe_reference(self.id, "evidence reference id", allow_non_reference=True)
        _assert_safe_persisted_json(self.to_json(), "evidence_reference")

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "sha256": self.sha256, "version": self.version}


@dataclass(frozen=True, slots=True)
class FrozenWireRequest:
    """Exact in-memory request bytes; repr never exposes URLs, headers, or bodies."""

    method: bytes = field(repr=False)
    target: bytes = field(repr=False)
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.method, bytes) or not self.method:
            raise ExternalEffectValidationError("wire method must be non-empty bytes")
        if not isinstance(self.target, bytes) or not self.target:
            raise ExternalEffectValidationError("wire target must be non-empty bytes")
        if not isinstance(self.body, bytes):
            raise ExternalEffectValidationError("wire body must be bytes")
        if not isinstance(self.headers, tuple):
            raise ExternalEffectValidationError("wire headers must be an ordered tuple")
        for header in self.headers:
            if (
                not isinstance(header, tuple)
                or len(header) != 2
                or not isinstance(header[0], bytes)
                or not isinstance(header[1], bytes)
            ):
                raise ExternalEffectValidationError("wire headers must be byte name/value pairs")


@dataclass(frozen=True, slots=True)
class PreparedExternalRequest:
    family: str
    provider: str
    provider_scope: str
    normalizer_version: str
    canonical_request: Mapping[str, Any]
    request_ref: str | None
    canonical_request_sha256: str
    application_request_sha256: str
    occurrence_id: UUID | None
    evidence_refs: tuple[EvidenceReference, ...]
    application_request: FrozenWireRequest = field(repr=False, compare=False)
    identity_version: str = "effect-key.v1"

    def __post_init__(self) -> None:
        for name in ("family", "provider", "provider_scope", "normalizer_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ExternalEffectValidationError(f"{name} is required")
        _validate_safe_reference(self.provider_scope, "provider_scope", allow_non_reference=True)
        if self.identity_version != "effect-key.v1":
            raise ExternalEffectValidationError("unsupported identity_version")
        if self.request_ref is not None:
            _validate_safe_reference(self.request_ref, "request_ref")
        if not isinstance(self.application_request, FrozenWireRequest):
            raise ExternalEffectValidationError("application_request must be FrozenWireRequest")
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(item, EvidenceReference) for item in self.evidence_refs
        ):
            raise ExternalEffectValidationError("evidence_refs must be EvidenceReference values")
        frozen = _freeze_json(self.canonical_request)
        if not isinstance(frozen, Mapping):
            raise ExternalEffectValidationError("canonical_request must be a JSON object")
        _assert_safe_persisted_json(frozen)
        object.__setattr__(self, "canonical_request", frozen)
        _require_sha256(self.canonical_request_sha256, "canonical_request_sha256")
        _require_sha256(self.application_request_sha256, "application_request_sha256")
        if canonical_request_sha256(frozen) != self.canonical_request_sha256:
            raise ExternalEffectValidationError("canonical_request_sha256 does not match request")
        if application_request_sha256(self.application_request) != self.application_request_sha256:
            raise ExternalEffectValidationError(
                "application_request_sha256 does not match wire bytes"
            )

    @classmethod
    def build(
        cls,
        *,
        family: str,
        provider: str,
        provider_scope: str,
        normalizer_version: str,
        canonical_request: Mapping[str, Any],
        application_request: FrozenWireRequest,
        request_ref: str | None = None,
        occurrence_id: UUID | None = None,
        evidence_refs: tuple[EvidenceReference, ...] = (),
        identity_version: str = "effect-key.v1",
    ) -> PreparedExternalRequest:
        return cls(
            family=family,
            provider=provider,
            provider_scope=provider_scope,
            normalizer_version=normalizer_version,
            canonical_request=canonical_request,
            request_ref=request_ref,
            canonical_request_sha256=canonical_request_sha256(canonical_request),
            application_request_sha256=application_request_sha256(application_request),
            occurrence_id=occurrence_id,
            evidence_refs=evidence_refs,
            application_request=application_request,
            identity_version=identity_version,
        )


@dataclass(frozen=True, slots=True)
class ExternalEffectAuthority:
    investigation_id: UUID
    invocation_id: UUID | None
    operation_key: str
    reservation_amounts: Mapping[str, int]
    expected_envelope_version: int
    approval_decision_id: UUID
    invocation_arguments_sha256: str | None
    bridge_turn_id: UUID | None = None
    model_run_id: UUID | None = None
    acquisition_attempt_id: UUID | None = None
    retrohunt_reservation_id: UUID | None = None
    authority_kind: ExternalEffectAuthorityKind = ExternalEffectAuthorityKind.HUMAN_G2
    policy_event_id: UUID | None = None
    policy_version: str | None = None
    pinned_subject_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID):
            raise ExternalEffectValidationError("investigation_id must be UUID")
        for name in (
            "invocation_id",
            "bridge_turn_id",
            "model_run_id",
            "acquisition_attempt_id",
            "retrohunt_reservation_id",
            "policy_event_id",
            "pinned_subject_id",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise ExternalEffectValidationError(f"{name} must be UUID or None")
        if not isinstance(self.approval_decision_id, UUID):
            raise ExternalEffectValidationError("approval_decision_id must be UUID")
        if not isinstance(self.authority_kind, ExternalEffectAuthorityKind):
            raise ExternalEffectValidationError("authority_kind must be a supported enum")
        if not isinstance(self.operation_key, str) or not self.operation_key.strip():
            raise ExternalEffectValidationError("operation_key is required")
        if self.expected_envelope_version < 1:
            raise ExternalEffectValidationError("expected_envelope_version must be positive")
        if self.invocation_id is not None:
            _require_sha256(self.invocation_arguments_sha256, "invocation_arguments_sha256")
        elif self.invocation_arguments_sha256 is not None:
            _require_sha256(self.invocation_arguments_sha256, "invocation_arguments_sha256")
        if self.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY:
            if (
                self.policy_event_id is None
                or not self.policy_version
                or self.pinned_subject_id is None
            ):
                raise ExternalEffectValidationError(
                    "OSINT_POLICY authority requires policy bindings"
                )
        elif any(value is not None for value in (self.policy_event_id, self.policy_version)):
            raise ExternalEffectValidationError("policy bindings require OSINT_POLICY authority")
        copied: dict[str, int] = {}
        if not isinstance(self.reservation_amounts, Mapping):
            raise ExternalEffectValidationError("reservation_amounts must be a mapping")
        for dimension, amount in self.reservation_amounts.items():
            if not isinstance(dimension, str) or not dimension.strip():
                raise ExternalEffectValidationError("budget dimension must be non-empty text")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ExternalEffectValidationError(
                    "reservation amounts must be non-negative integers"
                )
            copied[dimension] = amount
        object.__setattr__(
            self, "reservation_amounts", MappingProxyType(dict(sorted(copied.items())))
        )


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ConsumedEffectPermit:
    """Opaque capability minted only by the coordinator's local issuer."""

    claim_id: UUID
    investigation_id: UUID
    invocation_id: UUID | None
    effect_key: str
    family: str
    provider: str
    provider_scope: str
    request_digest: str
    envelope_version: int
    approval_decision_id: UUID
    nonce: UUID
    expires_at_monotonic: float
    _issuer: object

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("ConsumedEffectPermit values are issued by ExternalEffectCoordinator")

    @classmethod
    def _issue(
        cls,
        *,
        issuer: object,
        claim_id: UUID,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        effect_key: str,
        nonce: UUID,
        expires_at_monotonic: float,
    ) -> ConsumedEffectPermit:
        permit = object.__new__(cls)
        for name, value in {
            "claim_id": claim_id,
            "investigation_id": authority.investigation_id,
            "invocation_id": authority.invocation_id,
            "effect_key": effect_key,
            "family": prepared.family,
            "provider": prepared.provider,
            "provider_scope": prepared.provider_scope,
            "request_digest": prepared.application_request_sha256,
            "envelope_version": authority.expected_envelope_version,
            "approval_decision_id": authority.approval_decision_id,
            "nonce": nonce,
            "expires_at_monotonic": expires_at_monotonic,
            "_issuer": issuer,
        }.items():
            object.__setattr__(permit, name, value)
        return permit


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize the supported safe JSON subset using deterministic JCS ordering."""

    normalized = _normalize_json(value)
    return _write_jcs(normalized).encode("utf-8")


def canonical_request_sha256(canonical_request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(canonical_request)).hexdigest()


def application_request_sha256(request: FrozenWireRequest) -> str:
    """Hash the JCS/base64url wire image specified by the S3 contract."""

    headers_value = [[_b64url(name), _b64url(value)] for name, value in request.headers]
    image = {
        "body_b64": _b64url(request.body),
        "format": "autowork.application-request.v1",
        "headers_b64": _b64url(canonical_json_bytes(headers_value)),
        "method_b64": _b64url(request.method),
        "target_b64": _b64url(request.target),
    }
    return hashlib.sha256(canonical_json_bytes(image)).hexdigest()


def occurrence_sha256(occurrence_id: UUID | None) -> str:
    if occurrence_id is None:
        raw = _DEFAULT_OCCURRENCE_JSON
    else:
        raw = canonical_json_bytes(
            {
                "kind": "explicit",
                "occurrence_id": str(occurrence_id),
                "version": "effect-occurrence.v1",
            }
        )
    return hashlib.sha256(raw).hexdigest()


def effect_key(investigation_id: UUID, prepared: PreparedExternalRequest) -> str:
    if not isinstance(investigation_id, UUID):
        raise ExternalEffectValidationError("investigation_id must be UUID")
    payload = {
        "canonical_request_sha256": prepared.canonical_request_sha256,
        "family": prepared.family,
        "identity_version": prepared.identity_version,
        "investigation_id": str(investigation_id),
        "normalizer_version": prepared.normalizer_version,
        "occurrence_sha256": occurrence_sha256(prepared.occurrence_id),
        "provider": prepared.provider,
        "provider_scope": prepared.provider_scope,
    }
    return hashlib.sha256(b"autowork.effect-key.v1\0" + canonical_json_bytes(payload)).hexdigest()


def validate_prepared_request(prepared: PreparedExternalRequest) -> None:
    """Recheck the digests immediately before consume and immediately before handoff."""

    if canonical_request_sha256(prepared.canonical_request) != prepared.canonical_request_sha256:
        raise ExternalEffectValidationError("canonical request changed after preparation")
    if (
        application_request_sha256(prepared.application_request)
        != prepared.application_request_sha256
    ):
        raise ExternalEffectValidationError("application request changed after preparation")
    _assert_safe_persisted_json(prepared.canonical_request)
    if prepared.request_ref is not None:
        _validate_safe_reference(prepared.request_ref, "request_ref")
    for reference in prepared.evidence_refs:
        _assert_safe_persisted_json(reference.to_json())


def validate_persistable_reference(value: str, label: str = "reference") -> None:
    """Reject credential-bearing URLs and signed URLs before persistence."""

    _validate_safe_reference(value, label, allow_non_reference=True)


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            _validate_unicode(value)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ExternalEffectValidationError("JSON integers must be safe integers")
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ExternalEffectValidationError("NaN and Infinity are invalid JSON numbers")
        raise ExternalEffectValidationError("decimal quantities must be normalized strings")
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ExternalEffectValidationError("JSON object keys must be strings")
            _validate_unicode(key)
            output[key] = _normalize_json(child)
        return output
    if isinstance(value, (list, tuple)):
        return [_normalize_json(child) for child in value]
    raise ExternalEffectValidationError(f"unsupported JSON value: {type(value).__name__}")


def _write_jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_write_jcs(child) for child in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: item.encode("utf-16-be"))
        return "{" + ",".join(_write_jcs(key) + ":" + _write_jcs(value[key]) for key in keys) + "}"
    raise ExternalEffectValidationError(f"unsupported normalized JSON: {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    normalized = _normalize_json(value)
    return _freeze_normalized(normalized)


def _freeze_normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_normalized(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_normalized(child) for child in value)
    return value


def _assert_safe_persisted_json(value: Any, path: str = "canonical_request") -> None:
    sensitive_key_parts = (
        "authorization",
        "api_key",
        "apikey",
        "access_key",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "secret",
        "cookie",
        "set-cookie",
        "credential",
        "private_key",
        "signed_url",
        "signature",
    )
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = key.casefold().replace("-", "_")
            if any(part.replace("-", "_") in lowered for part in sensitive_key_parts):
                raise ExternalEffectValidationError(
                    f"secret-bearing field is not persistable: {path}.{key}"
                )
            _assert_safe_persisted_json(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_persisted_json(child, f"{path}[{index}]")
    elif isinstance(value, str):
        _validate_safe_reference(value, path, allow_non_reference=True)


def _validate_safe_reference(value: str, label: str, *, allow_non_reference: bool = False) -> None:
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in ("x-amz-signature=", "awsaccesskeyid=", "signature=", "sig=")
    ):
        raise ExternalEffectValidationError(f"signed URL material is not persistable: {label}")
    if "://" in value:
        if "@" in value.split("?", 1)[0].split("//", 1)[-1]:
            raise ExternalEffectValidationError(
                f"credential-bearing URL is not persistable: {label}"
            )
    if "://" in value or "?" in value or "#" in value:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ExternalEffectValidationError(f"invalid URL is not persistable: {label}") from exc
        sensitive_parameter_names = {
            "access_token",
            "access_key",
            "access_key_id",
            "apikey",
            "auth",
            "authorization",
            "bearer",
            "bearer_token",
            "client_secret",
            "client_assertion",
            "credential",
            "credentials",
            "id_token",
            "jwt",
            "jwt_token",
            "key",
            "password",
            "passwd",
            "refresh_token",
            "secret",
            "session",
            "session_id",
            "session_token",
            "sig",
            "signature",
            "token",
            "oauth_token",
            "oauth_signature",
            "private_key",
            "private_key_id",
            "client_key",
            "client_secret_key",
            "secret_key",
            "api_secret",
            "consumer_key",
            "consumer_secret",
            "code_verifier",
            "passphrase",
            "x_amz_credential",
            "x_amz_security_token",
            "x_api_key",
        }
        normalized_sensitive = {
            re.sub(r"[^a-z0-9]", "", name) for name in sensitive_parameter_names
        }
        for component in (parsed.query, parsed.fragment):
            for name, _ in parse_qsl(component, keep_blank_values=True):
                normalized_name = re.sub(r"[^a-z0-9]", "", unquote_plus(name).casefold())
                if (
                    normalized_name in normalized_sensitive
                    or "token" in normalized_name
                    or "secret" in normalized_name
                    or normalized_name.startswith("xamz")
                ):
                    raise ExternalEffectValidationError(
                        f"credential-bearing URL is not persistable: {label}"
                    )
    if not allow_non_reference and not value.strip():
        raise ExternalEffectValidationError(f"{label} must not be empty")


def _validate_unicode(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExternalEffectValidationError("invalid Unicode is not valid JCS input") from exc


def _require_sha256(value: str | None, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ExternalEffectValidationError(f"{label} must be lowercase SHA-256 hex")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


__all__ = [
    "ConsumedEffectPermit",
    "EffectClaimState",
    "EvidenceReference",
    "ExternalEffectAuthority",
    "ExternalEffectAuthorityKind",
    "ExternalEffectUseKind",
    "ExternalEffectValidationError",
    "FrozenWireRequest",
    "PreparedExternalRequest",
    "application_request_sha256",
    "canonical_json_bytes",
    "canonical_request_sha256",
    "effect_key",
    "occurrence_sha256",
    "validate_persistable_reference",
    "validate_prepared_request",
]
