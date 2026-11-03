"""One-request external effect admission and permit lifecycle.

The coordinator owns the authorization cut. An injected claim store must
commit ``consume`` before it returns ``ConsumeCommitted``; no database lock is
held while the injected transport runs.

Security boundary: family, adapter, and tool code may use public or conventional
single-underscore APIs, and may forge, copy, or replay values. This module does
not claim to contain arbitrary in-process introspection through frames, closure
inspection, ``gc``, or ``ctypes``.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, SupportsIndex, cast
from uuid import UUID, uuid4

from cti_app.domain.external_effects import (
    ConsumedEffectPermit,
    EffectClaimState,
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectValidationError,
    FrozenWireRequest,
    PreparedExternalRequest,
    _wire_request_sha256,
    effect_key,
    occurrence_sha256,
    validate_persistable_reference,
    validate_prepared_request,
)

_BRIDGE_FAMILY_PREFIXES = ("bridge.", "advisor.")


def external_effect_source_do_not_submit_blocks(family: str, do_not_submit: bool) -> bool:
    """Return whether a source flag blocks this external effect family.

    VT and Bridge/advisor effects ignore this metadata flag. Other external
    effect families retain the source ``do_not_submit`` veto.
    """
    return do_not_submit and not family.startswith(("vt.", *_BRIDGE_FAMILY_PREFIXES))


def external_effect_source_restriction_reason(
    *,
    source_kind: str,
    source: Any,
    subject_id: UUID,
    family: str,
    is_model_submission: bool,
) -> str | None:
    """Return the disclosure-policy refusal for a previously resolved source row."""
    source_subject_id = getattr(source, "subject_id", None)
    tlp = getattr(source, "tlp", None)
    if (
        source_subject_id != subject_id
        or (isinstance(tlp, str) and tlp.casefold() == "red")
        or external_effect_source_do_not_submit_blocks(
            family, bool(getattr(source, "do_not_submit", False))
        )
    ):
        return (
            "sample_source_restricted" if source_kind == "sample" else "source_document_restricted"
        )
    license_restriction = getattr(source, "license_restriction", None)
    if source_kind == "sample":
        if bool((license_restriction or "").strip()):
            return "sample_source_restricted"
        if is_model_submission and not bool(getattr(source, "external_llm_allowed", False)):
            return "sample_source_restricted"
    elif license_restriction:
        return "source_document_restricted"
    elif is_model_submission and not bool(getattr(source, "external_llm_allowed", False)):
        return "source_document_external_llm_not_allowed"
    return None


VIRUSTOTAL_OBSERVATION_SOURCE = "VirusTotal"
_VT_SHORT_HASH_FAMILIES = frozenset({"vt.file_report", "vt.relationships"})
_VT_SHA256_OBSERVATION_FAMILIES = frozenset(
    {"vt.file_report", "vt.behaviour", "vt.behaviour_pcap", "vt.relationships"}
)
_BASE_FILE_OBJECT_TYPES = frozenset({"sample", "file", "sample_observation"})
_HASH_TYPE_ALIASES = frozenset({"md5", "sha1", "sha256"})


class ExternalObservationHashPolicy(StrEnum):
    """Application-owned classification for hashes cited by external effects."""

    SHA256 = "sha256"
    SHORT_OR_SHA256 = "short_or_sha256"
    VT_PROVIDER_REPORT = "vt_provider_report"
    REJECT = "reject"


def external_observation_hash_policy(
    *, family: str, source: object, object_type: object
) -> ExternalObservationHashPolicy | None:
    """Classify whether and how an observation hash may ground an external effect.

    This is application policy. The database repository only uses the result to
    validate a source row and resolve any referenced sample.
    """
    normalized_type = object_type.casefold() if isinstance(object_type, str) else ""
    is_bridge_family = family.startswith(_BRIDGE_FAMILY_PREFIXES)
    if is_bridge_family and normalized_type in {"md5", "sha1"}:
        return ExternalObservationHashPolicy.REJECT
    if is_bridge_family and normalized_type == "file" and object_type != "file":
        return ExternalObservationHashPolicy.REJECT
    if is_bridge_family and normalized_type == "file" and source == VIRUSTOTAL_OBSERVATION_SOURCE:
        return ExternalObservationHashPolicy.VT_PROVIDER_REPORT
    if family in _VT_SHORT_HASH_FAMILIES:
        allowed_types = _BASE_FILE_OBJECT_TYPES | _HASH_TYPE_ALIASES
    elif family in _VT_SHA256_OBSERVATION_FAMILIES or is_bridge_family:
        allowed_types = _BASE_FILE_OBJECT_TYPES | {"sha256"}
    else:
        allowed_types = _BASE_FILE_OBJECT_TYPES
    if normalized_type not in allowed_types:
        return None
    if family in _VT_SHORT_HASH_FAMILIES:
        return ExternalObservationHashPolicy.SHORT_OR_SHA256
    return ExternalObservationHashPolicy.SHA256


def external_effect_vt_file_hash_read(family: str) -> bool:
    """Whether this effect family reads a VirusTotal file hash as its target."""
    return family in _VT_SHA256_OBSERVATION_FAMILIES


def validate_external_observation_hash(
    value: object, policy: ExternalObservationHashPolicy
) -> str | None:
    """Validate an observed file hash under an application policy.

    Existing VT and SHA-256 paths retain their historical case-folding. Newly
    admitted Bridge provider hashes must already be lowercase ASCII hex.
    """
    if not isinstance(value, str):
        return None
    if policy is ExternalObservationHashPolicy.REJECT:
        return None
    if policy is ExternalObservationHashPolicy.VT_PROVIDER_REPORT:
        if not value.isascii():
            return None
        if len(value) == 64:
            normalized_sha256 = value.casefold()
            return normalized_sha256 if re.fullmatch(r"[0-9a-f]{64}", normalized_sha256) else None
        if len(value) in {32, 40} and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40})", value):
            return value
        return None
    normalized = value.casefold()
    pattern = (
        r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})"
        if policy is ExternalObservationHashPolicy.SHORT_OR_SHA256
        else r"[0-9a-f]{64}"
    )
    return normalized if re.fullmatch(pattern, normalized) else None


def bridge_provider_report_requires_blob(
    *,
    family: str,
    source: object,
    object_type: object,
    object_id: object,
    observation_kind: object,
) -> bool:
    """Whether a Bridge VirusTotal file report needs its archived raw blob read."""
    return (
        family.startswith(_BRIDGE_FAMILY_PREFIXES)
        and source == VIRUSTOTAL_OBSERVATION_SOURCE
        and object_type == "file"
        and observation_kind == "vt_file_report"
        and isinstance(object_id, str)
        and len(object_id) in {32, 40, 64}
    )


def external_effect_may_resolve_bridge_vt_report(family: str) -> bool:
    """Whether this external-effect family can cite a Bridge VT file report."""
    return family.startswith(_BRIDGE_FAMILY_PREFIXES)


@dataclass(frozen=True, slots=True)
class BridgeVTFileReportHashes:
    requested_hash: str
    md5: str
    sha1: str
    sha256: str

    @property
    def identifiers(self) -> frozenset[str]:
        return frozenset({self.requested_hash, self.md5, self.sha1, self.sha256})


def resolve_bridge_vt_file_report_hashes(
    *,
    family: str,
    source: object,
    object_type: object,
    observation_kind: object = "vt_file_report",
    object_id: object,
    report_content: bytes | None,
) -> BridgeVTFileReportHashes | None:
    """Resolve a Bridge VT file hash only through its verified raw report."""
    if not bridge_provider_report_requires_blob(
        family=family,
        source=source,
        object_type=object_type,
        object_id=object_id,
        observation_kind=observation_kind,
    ):
        return None
    if not isinstance(object_id, str) or not object_id.isascii():
        return None
    requested_hash = object_id.casefold() if len(object_id) == 64 else object_id
    if not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", requested_hash):
        return None
    if len(object_id) in {32, 40} and requested_hash != object_id:
        return None
    if not isinstance(report_content, bytes):
        return None
    try:
        payload = json.loads(report_content, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("type") != "file":
        return None
    attributes = data.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    md5 = attributes.get("md5")
    sha1 = attributes.get("sha1")
    sha256 = attributes.get("sha256")
    if not (
        isinstance(md5, str)
        and md5.isascii()
        and re.fullmatch(r"[0-9a-f]{32}", md5)
        and isinstance(sha1, str)
        and sha1.isascii()
        and re.fullmatch(r"[0-9a-f]{40}", sha1)
    ):
        return None

    data_id = data.get("id")
    data_id_is_sha256 = (
        isinstance(data_id, str)
        and data_id.isascii()
        and re.fullmatch(r"[0-9a-f]{64}", data_id.casefold()) is not None
    )
    if (
        isinstance(sha256, str)
        and sha256.isascii()
        and re.fullmatch(r"[0-9a-f]{64}", sha256.casefold())
    ):
        resolved_sha256 = sha256.casefold()
    else:
        return None

    # VT file reports identify the file by its canonical SHA-256 in data.id.
    # The MD5/SHA-1 used to request the report must only match its attributes.
    if not data_id_is_sha256 or not isinstance(data_id, str):
        return None
    if data_id.casefold() != resolved_sha256:
        return None
    if (isinstance(sha256, str) and sha256.casefold() != resolved_sha256) or (
        (len(requested_hash) == 32 and md5 != requested_hash)
        or (len(requested_hash) == 40 and sha1 != requested_hash)
        or (len(requested_hash) == 64 and requested_hash != resolved_sha256)
    ):
        return None
    return BridgeVTFileReportHashes(
        requested_hash=requested_hash,
        md5=md5,
        sha1=sha1,
        sha256=resolved_sha256,
    )


def resolve_bridge_vt_file_report_sha256(
    *,
    family: str,
    source: object,
    object_type: object,
    object_id: object,
    report_content: bytes | None,
) -> str | None:
    """Resolve the canonical SHA-256 from a verified Bridge VT file report."""
    resolved = resolve_bridge_vt_file_report_hashes(
        family=family,
        source=source,
        object_type=object_type,
        object_id=object_id,
        report_content=report_content,
    )
    return resolved.sha256 if resolved is not None else None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def bridge_refusal_action_for_object_type(object_type: object) -> str:
    """Return a safe action hint for a Bridge refusal, without exposing the hash."""
    normalized_type = object_type.casefold() if isinstance(object_type, str) else ""
    if normalized_type in _BASE_FILE_OBJECT_TYPES | _HASH_TYPE_ALIASES:
        return "drop_ref_or_cite_sha256"
    return "drop_ref"


def matching_bridge_provider_report_hashes(
    *, source: Any, report_hashes: set[str], blob_sha256: str | None = None
) -> tuple[str, ...]:
    """Map a restricted sample back to report hashes using its stored lineage."""
    matches = {
        report_hash
        for report_hash in report_hashes
        if getattr(source, "expected_hash", None) == report_hash
        or getattr(source, "source_object_id", None) == report_hash
        or blob_sha256 == report_hash
    }
    return tuple(sorted(matches))


def bridge_provider_report_samples_conflict(
    *,
    report_hashes: set[str],
    report_sha256: str,
    samples: Iterable[tuple[Any, str]],
) -> bool:
    """Whether samples linked to one provider report disagree on their real digest."""
    # A cross-digest conflict requires a shared MD5/SHA1 alias; compare the actual SHA-256s below.
    short_report_hashes = {
        report_hash for report_hash in report_hashes if len(report_hash) in {32, 40}
    }
    matched_blob_hashes = {
        blob_sha256
        for sample, blob_sha256 in samples
        if matching_bridge_provider_report_hashes(source=sample, report_hashes=short_report_hashes)
    }
    return len(matched_blob_hashes) > 1 or any(
        blob_sha256 != report_sha256 for blob_sha256 in matched_blob_hashes
    )


def bridge_may_cite_provider_report_hash_without_sample(*, family: str, observation: Any) -> bool:
    """Whether a Bridge dossier may cite a provider report about a not-yet-acquired hash.

    A VirusTotal report is provider metadata, not bytes of an acquired sample, so a fresh
    investigation holding only reports can still consult Bridge. The hash stays subject to
    the restrictions of any sample the subject already holds under it; observations derived
    from local analysis keep requiring an associated, resolvable sample. The exact file-report
    kind and object type plus the source label the VirusTotal recorders emit establish provenance;
    a tool name an operator could imitate does not.
    """
    if not family.startswith(_BRIDGE_FAMILY_PREFIXES):
        return False
    return (
        getattr(observation, "source", None) == VIRUSTOTAL_OBSERVATION_SOURCE
        and getattr(observation, "object_type", None) == "file"
        and getattr(observation, "kind", None) == "vt_file_report"
    )


def osint_vt_search_may_cite_unassociated_observation_hash(
    *, authority_kind: ExternalEffectAuthorityKind, provider: str, family: str
) -> bool:
    """Allow OSINT_OPEN passive searches to cite same-investigation file observations.

    Callers must still verify the active policy, reference provenance, and source
    restrictions. This exception concerns evidence references only; the search
    request sends its query, not sample bytes.
    """
    return (
        authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
        and provider == "vt"
        and family == "vt.intelligence_search"
    )


@dataclass(frozen=True, slots=True)
class FrameworkNoSendProof:
    """A bounded proof that this coordinator invocation made no handoff."""

    kind: str
    reason_code: str


class ExternalEffectFailureStage(StrEnum):
    """Safe transport failure stages that may be persisted without exception text."""

    RAW_ARCHIVE_WRITE_FAILED = "raw_archive_write_failed"
    ARCHIVE_REFERENCE_INVALID = "archive_reference_invalid"


class SafeExternalEffectFailure(RuntimeError):
    """A transport failure with a bounded, non-sensitive diagnostic stage."""

    def __init__(self, failure_stage: ExternalEffectFailureStage) -> None:
        self.failure_stage = failure_stage
        super().__init__(failure_stage.value)


@dataclass(frozen=True, slots=True)
class ExternalEffectClaim:
    id: UUID
    investigation_id: UUID
    invocation_id: UUID | None
    effect_key: str
    family: str
    provider: str
    provider_scope: str
    canonical_request_sha256: str
    application_request_sha256: str
    envelope_version: int
    approval_decision_id: UUID
    state: EffectClaimState
    evidence_refs: tuple[EvidenceReference, ...] = ()
    result_ref: str | None = None
    bridge_turn_id: UUID | None = None
    model_run_id: UUID | None = None
    outcome_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConsumeCommitted:
    """The claim insert committed; only this result can mint a permit."""

    claim_id: UUID


@dataclass(frozen=True, slots=True)
class ExistingClaim:
    claim: ExternalEffectClaim


@dataclass(frozen=True, slots=True)
class ConsumeRefused:
    reason: str
    detail: str | None = None


ConsumeResult = ConsumeCommitted | ExistingClaim | ConsumeRefused


@dataclass(frozen=True, slots=True)
class ConsumePolicyContext:
    authority: ExternalEffectAuthority
    prepared: PreparedExternalRequest
    effect_key: str
    database_session: Any
    investigation_subject_id: UUID


class ExternalEffectPolicyGuard(Protocol):
    """Source and OSINT guard; policy/revocation writers lock before the envelope."""

    async def lock_for_consume(
        self, database_session: Any, authority: ExternalEffectAuthority
    ) -> bool:
        """Lock/reload the persisted event and verify active status, version, and subject
        pin before the caller acquires envelope FOR UPDATE. Missing events deny.
        Revocation writers take this policy-event lock before the same envelope lock.
        """

    async def authorize(self, context: ConsumePolicyContext) -> bool: ...

    async def authorize_source_disclosure(self, context: ConsumePolicyContext) -> bool:
        """Check source disclosure restrictions without requiring an OSINT policy pin."""
        ...


@dataclass(frozen=True, slots=True)
class ReuseDecision:
    source_invocation_id: UUID | None
    observation_refs: tuple[EvidenceReference, ...]


class ExternalEffectReusePolicy(Protocol):
    """A family declares whether a completed effect is reusable."""

    async def authorize_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> ReuseDecision | None: ...


class ExternalEffectClaimStore(Protocol):
    """Durable consume and compare-and-set port used by the coordinator."""

    async def consume(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        *,
        effect_key: str,
        occurrence_digest: str,
    ) -> ConsumeResult: ...

    async def complete(
        self,
        claim_id: UUID,
        *,
        result_ref: str | None,
        evidence_refs: tuple[EvidenceReference, ...],
        outcome_code: str | None = None,
    ) -> bool: ...

    async def enter_handoff(
        self,
        claim_id: UUID,
        *,
        permit_nonce: UUID,
        request_digest: str,
        expires_at_monotonic: float,
    ) -> bool: ...

    async def definitely_not_submitted(
        self, claim_id: UUID, *, proof: _CoordinatorNoHandoffProof, reason_code: str
    ) -> bool: ...

    def _bind_no_handoff_proof_verifier(self, verifier: _NoHandoffProofVerifier) -> None: ...

    async def needs_review(
        self,
        claim_id: UUID,
        *,
        reason_code: str,
        mark_invocation: bool = True,
    ) -> bool: ...

    async def record_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        decision: ReuseDecision,
    ) -> UUID | None: ...

    async def release_refused_reuse_reservation(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class TransportResponse:
    result_ref: str | None = None
    evidence_refs: tuple[EvidenceReference, ...] = ()
    outcome_code: str | None = None
    payload: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.result_ref is not None:
            validate_persistable_reference(self.result_ref, "result_ref")
        if any(not isinstance(item, EvidenceReference) for item in self.evidence_refs):
            raise ExternalEffectValidationError("transport evidence refs must be typed references")
        if self.outcome_code is not None and not _is_safe_outcome_code(self.outcome_code):
            raise ExternalEffectValidationError("transport outcome code is invalid")
        if self.payload is not None and not isinstance(self.payload, bytes):
            raise ExternalEffectValidationError("transport payload must be bytes")


class NoSubmissionProofKind(StrEnum):
    """Coordinator-verified proof phase delivered only to the claim store."""

    BEFORE_HANDOFF = "before_handoff"


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _CoordinatorNoHandoffProof:
    """Opaque, one-use proof bound to one coordinator's private authority."""

    claim_id: UUID
    nonce: UUID
    kind: NoSubmissionProofKind

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("no-handoff proofs are issued by ExternalEffectCoordinator")


class _NoHandoffProofVerifier:
    """Store-side proof consumer; it has no proof-minting operation."""

    __slots__ = ("__consume", "__store")

    def __init__(self) -> None:
        raise TypeError("no-handoff verifiers are coordinator-bound")

    def bind_store(self, store: object) -> None:
        try:
            object.__getattribute__(self, "_NoHandoffProofVerifier__store")
        except AttributeError:
            object.__setattr__(self, "_NoHandoffProofVerifier__store", store)
            return
        raise ValueError("no-handoff verifier is already bound to a store")

    def consume(self, store: object, proof: object, claim_id: UUID) -> NoSubmissionProofKind | None:
        try:
            bound_store = object.__getattribute__(self, "_NoHandoffProofVerifier__store")
            consume = object.__getattribute__(self, "_NoHandoffProofVerifier__consume")
        except AttributeError:
            return None
        if bound_store is not store:
            return None
        return cast(NoSubmissionProofKind | None, consume(proof, claim_id))


class PermitHandoffGate(Protocol):
    """Own the final permit check, durable handoff, and single physical call."""

    async def submit(
        self,
        permit: ConsumedEffectPermit,
        prepared: PreparedExternalRequest,
        transport: PermitAwareTransport,
    ) -> TransportResponse: ...


class PermitAwareTransport(Protocol):
    async def submit_once(
        self, request: FrozenWireRequest, capability: PhysicalSendCapability
    ) -> TransportResponse:
        """Send once through the capability immediately before the raw client.

        Implementations keep their raw client private, configure retries off,
        and invoke the capability immediately before their single raw-client
        call. This is the transport's only physical-send API.
        """


class HandoffGateRefused(RuntimeError):
    """The one-use gate could not authorize physical handoff."""


class PhysicalSendCapability:
    """Opaque token for one coordinator-registered physical send authorization."""

    __slots__ = ()

    def __init__(self) -> None:
        raise TypeError("physical send capabilities are coordinator-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("physical send capabilities cannot be subclassed")

    def __call__(self, request: FrozenWireRequest) -> None:
        raise HandoffGateRefused("physical send capability is not registered")

    def __copy__(self) -> PhysicalSendCapability:
        return object.__new__(type(self))

    def __deepcopy__(self, _memo: dict[int, Any]) -> PhysicalSendCapability:
        return object.__new__(type(self))

    def __reduce_ex__(self, _protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError("physical send capabilities cannot be serialized")


def _install_physical_send_capability_dispatch() -> tuple[
    Callable[[Callable[[FrozenWireRequest], None]], PhysicalSendCapability],
    Callable[[PhysicalSendCapability], None],
]:
    registry_lock = threading.Lock()
    authorizers: dict[PhysicalSendCapability, Callable[[FrozenWireRequest], None]] = {}

    def dispatch(capability: PhysicalSendCapability, request: FrozenWireRequest) -> None:
        with registry_lock:
            registered = authorizers.get(capability)
        if registered is None:
            raise HandoffGateRefused("physical send capability is not registered")
        registered(request)

    def issue(authorize: Callable[[FrozenWireRequest], None]) -> PhysicalSendCapability:
        capability = object.__new__(PhysicalSendCapability)
        with registry_lock:
            authorizers[capability] = authorize
        return capability

    def revoke(capability: PhysicalSendCapability) -> None:
        with registry_lock:
            authorizers.pop(capability, None)

    type.__setattr__(PhysicalSendCapability, "__call__", dispatch)
    return issue, revoke


_issue_physical_send_capability, _revoke_physical_send_capability = (
    _install_physical_send_capability_dispatch()
)
del _install_physical_send_capability_dispatch


def _make_capability_init_binder(
    issue: Callable[[Callable[[FrozenWireRequest], None]], PhysicalSendCapability],
    revoke: Callable[[PhysicalSendCapability], None],
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def bind(initializer: Callable[..., None]) -> Callable[..., None]:
        def wrapped(
            self: Any,
            claim_store: ExternalEffectClaimStore,
            *,
            reuse_policies: Mapping[str, ExternalEffectReusePolicy] | None = None,
            permit_ttl_seconds: float = 5.0,
        ) -> None:
            initializer(
                self,
                claim_store,
                reuse_policies=reuse_policies,
                permit_ttl_seconds=permit_ttl_seconds,
                capability_issuer=issue,
                capability_revoker=revoke,
            )

        return wrapped

    return bind


_bind_capability_init = _make_capability_init_binder(
    _issue_physical_send_capability, _revoke_physical_send_capability
)
del _make_capability_init_binder
del _issue_physical_send_capability
del _revoke_physical_send_capability


@dataclass(frozen=True, slots=True)
class Completed:
    claim_id: UUID
    result_ref: str | None
    evidence_refs: tuple[EvidenceReference, ...]
    outcome_code: str | None = None
    payload: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Reused:
    claim_id: UUID
    use_id: UUID
    source_invocation_id: UUID | None
    observation_refs: tuple[EvidenceReference, ...]
    budget_delta: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class Refused:
    reason: str
    framework_no_send_proof: FrameworkNoSendProof
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DefinitelyNotSubmitted:
    claim_id: UUID
    state: EffectClaimState = EffectClaimState.DEFINITIVELY_NOT_SUBMITTED


class _PreHandoffGateRefused(HandoffGateRefused):
    """A gate refusal proven to occur before its durable handoff marker."""


@dataclass(frozen=True, slots=True)
class Ambiguous:
    claim_id: UUID | None
    state: EffectClaimState = EffectClaimState.NEEDS_REVIEW
    retryable: bool = True
    failure_stage: str | None = None


ExternalEffectOutcome = Completed | Reused | Refused | DefinitelyNotSubmitted | Ambiguous


class _PermitRegistry:
    """Process-local, identity-based one-use permit registry."""

    def __init__(self, *, ttl_seconds: float = 5.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("permit lifetime must be positive")
        self._issuer = object()
        self._ttl_seconds = ttl_seconds
        self._issued: dict[UUID, ConsumedEffectPermit] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        claim_id: UUID,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
    ) -> ConsumedEffectPermit:
        nonce = uuid4()
        permit = ConsumedEffectPermit._issue(
            issuer=self._issuer,
            claim_id=claim_id,
            authority=authority,
            prepared=prepared,
            effect_key=key,
            nonce=nonce,
            expires_at_monotonic=time.monotonic() + self._ttl_seconds,
        )
        with self._lock:
            self._issued[nonce] = permit
        return permit

    def burn_and_validate(
        self,
        permit: ConsumedEffectPermit,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
    ) -> bool:
        """Atomically validate and burn immediately before the physical send."""

        nonce = getattr(permit, "nonce", None)
        if not isinstance(nonce, UUID):
            return False
        with self._lock:
            issued = self._issued.get(nonce)
            if issued is not permit:
                return False
            if not self._matches(permit, authority, prepared, key):
                if time.monotonic() >= permit.expires_at_monotonic:
                    del self._issued[nonce]
                return False
            del self._issued[nonce]
        return True

    def is_valid(
        self,
        permit: ConsumedEffectPermit,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
    ) -> bool:
        nonce = getattr(permit, "nonce", None)
        if not isinstance(nonce, UUID):
            return False
        with self._lock:
            return self._issued.get(nonce) is permit and self._matches(
                permit, authority, prepared, key
            )

    def _matches(
        self,
        permit: ConsumedEffectPermit,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
    ) -> bool:
        return (
            getattr(permit, "_issuer", None) is self._issuer
            and time.monotonic() < permit.expires_at_monotonic
            and permit.investigation_id == authority.investigation_id
            and permit.invocation_id == authority.invocation_id
            and permit.effect_key == key
            and permit.family == prepared.family
            and permit.provider == prepared.provider
            and permit.provider_scope == prepared.provider_scope
            and permit.request_digest == prepared.physical_request_sha256
            and permit.envelope_version == authority.expected_envelope_version
            and permit.approval_decision_id == authority.approval_decision_id
            and _wire_request_sha256(prepared.application_request) == permit.request_digest
        )


class ExternalEffectCoordinator:
    @_bind_capability_init
    def __init__(
        self,
        claim_store: ExternalEffectClaimStore,
        *,
        reuse_policies: Mapping[str, ExternalEffectReusePolicy] | None = None,
        permit_ttl_seconds: float = 5.0,
        capability_issuer: Callable[
            [Callable[[FrozenWireRequest], None]],
            PhysicalSendCapability,
        ],
        capability_revoker: Callable[[PhysicalSendCapability], None],
    ) -> None:
        self._claim_store = claim_store
        self._reuse_policies = dict(reuse_policies or {})
        permit_registry = _PermitRegistry(ttl_seconds=permit_ttl_seconds)

        issued_proofs: dict[
            UUID, tuple[UUID, _CoordinatorNoHandoffProof, NoSubmissionProofKind]
        ] = {}
        proof_lock = threading.Lock()

        def issue_no_handoff_proof(
            claim_id: UUID, kind: NoSubmissionProofKind
        ) -> _CoordinatorNoHandoffProof:
            nonce = uuid4()
            proof = object.__new__(_CoordinatorNoHandoffProof)
            object.__setattr__(proof, "claim_id", claim_id)
            object.__setattr__(proof, "nonce", nonce)
            object.__setattr__(proof, "kind", kind)
            with proof_lock:
                issued_proofs[nonce] = (claim_id, proof, kind)
            return proof

        def consume_no_handoff_proof(proof: object, claim_id: UUID) -> NoSubmissionProofKind | None:
            if not isinstance(proof, _CoordinatorNoHandoffProof):
                return None
            nonce = getattr(proof, "nonce", None)
            bound_claim_id = getattr(proof, "claim_id", None)
            if (
                not isinstance(nonce, UUID)
                or not isinstance(bound_claim_id, UUID)
                or bound_claim_id != claim_id
            ):
                return None
            with proof_lock:
                registered = issued_proofs.get(nonce)
                if registered is None or registered[0] != claim_id or registered[1] is not proof:
                    return None
                del issued_proofs[nonce]
                return registered[2]

        verifier = object.__new__(_NoHandoffProofVerifier)
        object.__setattr__(verifier, "_NoHandoffProofVerifier__consume", consume_no_handoff_proof)
        claim_store._bind_no_handoff_proof_verifier(verifier)

        async def submit_through_gate(
            permit: ConsumedEffectPermit,
            authority: ExternalEffectAuthority,
            prepared: PreparedExternalRequest,
            key: str,
            transport: PermitAwareTransport,
            handoff_state: dict[str, Any],
        ) -> TransportResponse:
            if not permit_registry.is_valid(permit, authority, prepared, key):
                raise _PreHandoffGateRefused("permit expired, invalid, or already used")
            request = prepared.application_request
            try:
                validate_prepared_request(prepared)
            except ExternalEffectValidationError as exc:
                raise _PreHandoffGateRefused("prepared request changed before handoff") from exc
            if _wire_request_sha256(request) != permit.request_digest:
                raise _PreHandoffGateRefused("physical request digest mismatch")
            try:
                admitted = await claim_store.enter_handoff(
                    permit.claim_id,
                    permit_nonce=permit.nonce,
                    request_digest=prepared.application_request_sha256,
                    expires_at_monotonic=permit.expires_at_monotonic,
                )
            except Exception as exc:
                raise HandoffGateRefused("durable handoff gate unavailable") from exc
            if not admitted:
                raise _PreHandoffGateRefused("claim is sealed or handoff already entered")
            handoff_state["entered"] = True
            if time.monotonic() >= permit.expires_at_monotonic:
                raise HandoffGateRefused("permit expired before physical operation invocation")

            capability_attempted = False
            capability_used = False
            capability_active = True
            capability_lock = threading.Lock()

            def physical_send_capability(
                sent_request: FrozenWireRequest,
            ) -> None:
                nonlocal capability_attempted, capability_used
                with capability_lock:
                    if not capability_active:
                        raise HandoffGateRefused("physical send capability is no longer active")
                    if capability_attempted:
                        raise HandoffGateRefused("physical send capability was already used")
                    capability_attempted = True
                    if sent_request is not request:
                        raise HandoffGateRefused("physical request substitution refused")
                    actual_digest = _wire_request_sha256(sent_request)
                    try:
                        validate_prepared_request(prepared)
                    except ExternalEffectValidationError as exc:
                        raise HandoffGateRefused(
                            "prepared request changed before physical send"
                        ) from exc
                    permit_valid = permit_registry.burn_and_validate(
                        permit, authority, prepared, key
                    )
                    if actual_digest != permit.request_digest or not permit_valid:
                        raise HandoffGateRefused("physical send capability expired or invalid")
                    if time.monotonic() >= permit.expires_at_monotonic:
                        raise HandoffGateRefused("physical send capability expired or invalid")
                    capability_used = True

            capability = capability_issuer(physical_send_capability)
            capability_was_used = False
            try:
                response = await transport.submit_once(request, capability)
            finally:
                with capability_lock:
                    capability_active = False
                    capability_was_used = capability_used
                capability_revoker(capability)
            if not capability_was_used:
                # The durable handoff marker is already present and the transport
                # ran, so reject its response without claiming zero egress.
                raise HandoffGateRefused("transport returned without using its send capability")
            return response

        async def seal_zero_handoff(
            claim_id: UUID,
            *,
            reason_code: str,
            kind: NoSubmissionProofKind,
            mark_invocation_on_ambiguity: bool,
        ) -> DefinitelyNotSubmitted | Ambiguous:
            proof = issue_no_handoff_proof(claim_id, kind)
            try:
                changed = await claim_store.definitely_not_submitted(
                    claim_id, proof=proof, reason_code=reason_code
                )
            except Exception:
                changed = False
            if changed:
                return DefinitelyNotSubmitted(claim_id)
            await self._seal_needs_review(
                claim_id,
                "zero_handoff_seal_failed",
                mark_invocation=mark_invocation_on_ambiguity,
            )
            return Ambiguous(claim_id)

        async def send_one(
            authority: ExternalEffectAuthority,
            prepared: PreparedExternalRequest,
            transport: PermitAwareTransport,
            *,
            mark_invocation_on_ambiguity: bool = True,
            record_passive_failure_stage: bool = False,
        ) -> ExternalEffectOutcome:
            try:
                validate_prepared_request(prepared)
                if authority.bridge_turn_id is None and authority.model_run_id is not None:
                    raise ExternalEffectValidationError("model_run_id requires bridge_turn_id")
                if authority.bridge_turn_id is not None and authority.model_run_id is None:
                    raise ExternalEffectValidationError("bridge_turn_id requires model_run_id")
                if (
                    authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
                    and authority.policy_event_id is None
                ):
                    raise ExternalEffectValidationError(
                        "OSINT_POLICY authority lacks its policy event"
                    )
                key = effect_key(authority.investigation_id, prepared)
                occurrence_digest = occurrence_sha256(prepared.occurrence_id)
            except (ExternalEffectValidationError, TypeError, ValueError):
                return Refused(
                    reason="invalid_prepared_effect",
                    framework_no_send_proof=FrameworkNoSendProof(
                        "PREPARATION", "invalid_prepared_effect"
                    ),
                )

            try:
                consume = await claim_store.consume(
                    authority,
                    prepared,
                    effect_key=key,
                    occurrence_digest=occurrence_digest,
                )
            except Exception:
                # A commit acknowledgement failure is safe from egress; replay
                # observes any claim that committed despite the error.
                return Refused(
                    reason="consume_unavailable",
                    framework_no_send_proof=FrameworkNoSendProof(
                        "PRE_CONSUME", "consume_unavailable"
                    ),
                )

            if isinstance(consume, ConsumeRefused):
                refusal_reason = consume.reason
                if consume.detail is not None:
                    refusal_reason = f"{refusal_reason}:{consume.detail}"
                return Refused(
                    reason=refusal_reason,
                    framework_no_send_proof=FrameworkNoSendProof("PRE_CONSUME", consume.reason),
                    detail=consume.detail,
                )
            if isinstance(consume, ExistingClaim):
                return await self._handle_existing(consume.claim, authority, prepared)
            if not isinstance(consume, ConsumeCommitted):
                return Refused(
                    reason="invalid_consume_result",
                    framework_no_send_proof=FrameworkNoSendProof(
                        "PRE_CONSUME", "invalid_consume_result"
                    ),
                )

            permit = permit_registry.issue(consume.claim_id, authority, prepared, key)
            if not permit_registry.is_valid(permit, authority, prepared, key):
                return await seal_zero_handoff(
                    consume.claim_id,
                    reason_code="permit_validation_failed",
                    kind=NoSubmissionProofKind.BEFORE_HANDOFF,
                    mark_invocation_on_ambiguity=mark_invocation_on_ambiguity,
                )

            handoff_state: dict[str, Any] = {"entered": False}
            try:
                response = await submit_through_gate(
                    permit, authority, prepared, key, transport, handoff_state
                )
            except asyncio.CancelledError:
                await self._seal_needs_review(consume.claim_id, "cancelled_during_transport")
                return Ambiguous(
                    consume.claim_id,
                    retryable=False,
                    failure_stage=(
                        "cancelled_during_transport" if record_passive_failure_stage else None
                    ),
                )
            except _PreHandoffGateRefused:
                if handoff_state["entered"]:
                    failure_stage = (
                        "handoff_gate_failed"
                        if record_passive_failure_stage
                        else "transport_outcome_unknown"
                    )
                    await self._seal_needs_review(
                        consume.claim_id,
                        failure_stage,
                        mark_invocation=mark_invocation_on_ambiguity,
                    )
                    return Ambiguous(
                        consume.claim_id,
                        failure_stage=(failure_stage if record_passive_failure_stage else None),
                    )
                return await seal_zero_handoff(
                    consume.claim_id,
                    reason_code="pre_handoff_gate_refused",
                    kind=NoSubmissionProofKind.BEFORE_HANDOFF,
                    mark_invocation_on_ambiguity=mark_invocation_on_ambiguity,
                )
            except Exception as exc:
                failure_stage = (
                    _passive_read_failure_stage(exc)
                    if record_passive_failure_stage
                    else "transport_outcome_unknown"
                )
                await self._seal_needs_review(
                    consume.claim_id,
                    failure_stage,
                    mark_invocation=mark_invocation_on_ambiguity,
                )
                return Ambiguous(
                    consume.claim_id,
                    failure_stage=(failure_stage if record_passive_failure_stage else None),
                )

            if not isinstance(response, TransportResponse):
                failure_stage = "invalid_transport_response"
                await self._seal_needs_review(
                    consume.claim_id,
                    failure_stage,
                    mark_invocation=mark_invocation_on_ambiguity,
                )
                return Ambiguous(
                    consume.claim_id,
                    failure_stage=(failure_stage if record_passive_failure_stage else None),
                )
            try:
                completed = await claim_store.complete(
                    consume.claim_id,
                    result_ref=response.result_ref,
                    evidence_refs=response.evidence_refs,
                    outcome_code=response.outcome_code,
                )
            except Exception:
                completed = False
            if not completed:
                failure_stage = (
                    "claim_completion_failed"
                    if record_passive_failure_stage
                    else "outcome_persist_failed"
                )
                await self._seal_needs_review(
                    consume.claim_id,
                    failure_stage,
                    mark_invocation=mark_invocation_on_ambiguity,
                )
                return Ambiguous(
                    consume.claim_id,
                    failure_stage=(failure_stage if record_passive_failure_stage else None),
                )
            return Completed(
                consume.claim_id,
                response.result_ref,
                response.evidence_refs,
                response.outcome_code,
                response.payload,
            )

        # The issuer stays in this closure; no coordinator attribute exposes it.
        self.send_one: Callable[
            [ExternalEffectAuthority, PreparedExternalRequest, PermitAwareTransport],
            Awaitable[ExternalEffectOutcome],
        ] = send_one

        async def send_passive_read(
            authority: ExternalEffectAuthority,
            prepared: PreparedExternalRequest,
            transport: PermitAwareTransport,
            *,
            defer_invocation_needs_review: bool,
        ) -> ExternalEffectOutcome:
            if not _is_retryable_vt_passive_read(prepared):
                return Refused(
                    "passive_read_retry_not_allowed",
                    FrameworkNoSendProof("PREPARATION", "passive_read_retry_not_allowed"),
                )
            return await send_one(
                authority,
                prepared,
                transport,
                mark_invocation_on_ambiguity=not defer_invocation_needs_review,
                record_passive_failure_stage=True,
            )

        self.send_passive_read = send_passive_read

    async def latest_advisor_claim_for_invocation(
        self, investigation_id: UUID, invocation_id: UUID
    ) -> ExternalEffectClaim | None:
        """Read the latest durable advisor claim without granting send authority."""
        lookup = getattr(self._claim_store, "latest_advisor_claim_for_invocation", None)
        if not callable(lookup):
            return None
        return cast(ExternalEffectClaim | None, await lookup(investigation_id, invocation_id))

    async def advisor_claims_for_invocation(
        self, investigation_id: UUID, invocation_id: UUID
    ) -> tuple[ExternalEffectClaim, ...]:
        """Read every durable Bridge advisor claim for one invocation."""
        lookup = getattr(self._claim_store, "advisor_claims_for_invocation", None)
        if not callable(lookup):
            return ()
        return cast(tuple[ExternalEffectClaim, ...], await lookup(investigation_id, invocation_id))

    async def latest_claim_for_invocation(
        self, investigation_id: UUID, invocation_id: UUID
    ) -> ExternalEffectClaim | None:
        """Read an invocation's durable effect claim without granting send authority."""
        lookup = getattr(self._claim_store, "latest_claim_for_invocation", None)
        if not callable(lookup):
            return None
        return cast(ExternalEffectClaim | None, await lookup(investigation_id, invocation_id))

    async def claims_for_invocation(
        self, investigation_id: UUID, invocation_id: UUID
    ) -> tuple[ExternalEffectClaim, ...]:
        """Read every durable effect claim for one invocation."""
        lookup = getattr(self._claim_store, "claims_for_invocation", None)
        if not callable(lookup):
            return ()
        return cast(tuple[ExternalEffectClaim, ...], await lookup(investigation_id, invocation_id))

    async def _handle_existing(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> ExternalEffectOutcome:
        if (
            claim.investigation_id != authority.investigation_id
            or claim.effect_key != effect_key(authority.investigation_id, prepared)
            or claim.family != prepared.family
            or claim.provider != prepared.provider
            or claim.provider_scope != prepared.provider_scope
            or claim.canonical_request_sha256 != prepared.canonical_request_sha256
            or claim.application_request_sha256 != prepared.application_request_sha256
        ):
            return Refused(
                "effect_binding_conflict",
                FrameworkNoSendProof("EXISTING_CLAIM", "effect_binding_conflict"),
            )
        if claim.state is EffectClaimState.COMPLETED:
            if claim.outcome_code is not None:
                if (
                    claim.invocation_id == authority.invocation_id
                    and claim.bridge_turn_id == authority.bridge_turn_id
                    and claim.model_run_id == authority.model_run_id
                ):
                    return Completed(
                        claim.id,
                        claim.result_ref,
                        claim.evidence_refs,
                        claim.outcome_code,
                    )
                await self._release_refused_reuse_reservation(claim, authority, prepared)
                return Refused(
                    "effect_occurrence_required",
                    FrameworkNoSendProof("EXISTING_CLAIM", "effect_occurrence_required"),
                )
            policy = self._reuse_policies.get(prepared.family)
            if policy is None:
                await self._release_refused_reuse_reservation(claim, authority, prepared)
                return Refused(
                    "effect_occurrence_required",
                    FrameworkNoSendProof("EXISTING_CLAIM", "effect_occurrence_required"),
                )
            try:
                decision = await policy.authorize_reuse(claim, authority, prepared)
            except Exception:
                decision = None
            if decision is None:
                await self._release_refused_reuse_reservation(claim, authority, prepared)
                return Refused(
                    "effect_occurrence_required",
                    FrameworkNoSendProof("REUSE_POLICY", "effect_occurrence_required"),
                )
            try:
                use_id = await self._claim_store.record_reuse(claim, authority, prepared, decision)
            except Exception:
                use_id = None
            if use_id is None:
                return Refused(
                    "reuse_record_failed",
                    FrameworkNoSendProof("REUSE_RECORD", "reuse_record_failed"),
                )
            return Reused(
                claim_id=claim.id,
                use_id=use_id,
                source_invocation_id=decision.source_invocation_id,
                observation_refs=decision.observation_refs,
                budget_delta={dimension: 0 for dimension in authority.reservation_amounts},
            )
        if claim.state is EffectClaimState.DEFINITIVELY_NOT_SUBMITTED:
            return DefinitelyNotSubmitted(claim.id)
        if claim.state is EffectClaimState.CONSUMED:
            await self._seal_needs_review(claim.id, "replayed_consumed_claim")
        return Ambiguous(claim.id)

    async def _release_refused_reuse_reservation(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> None:
        try:
            await self._claim_store.release_refused_reuse_reservation(claim, authority, prepared)
        except Exception:
            # A release failure cannot authorize another send; the typed refusal
            # remains the correct result and recovery can reconcile the reservation.
            return

    async def _seal_needs_review(
        self,
        claim_id: UUID,
        reason_code: str,
        *,
        mark_invocation: bool = True,
    ) -> None:
        try:
            if mark_invocation:
                await self._claim_store.needs_review(claim_id, reason_code=reason_code)
            else:
                await self._claim_store.needs_review(
                    claim_id, reason_code=reason_code, mark_invocation=False
                )
        except Exception:
            # The durable CONSUMED row remains a no-resend barrier if sealing
            # itself is unavailable.
            return


# Drop the temporary binder after the coordinator constructor closes over the
# private registry functions.
del _bind_capability_init


def _is_safe_outcome_code(value: str) -> bool:
    return isinstance(value, str) and value in {"bridge_rate_limited", "bridge_rejected"}


def _passive_read_failure_stage(error: Exception) -> str:
    if isinstance(error, SafeExternalEffectFailure):
        return error.failure_stage.value
    exception_name = type(error).__name__
    if (
        not exception_name
        or not exception_name.isascii()
        or not exception_name[0].isalpha()
        or not all(character.isalnum() or character == "_" for character in exception_name)
    ):
        exception_name = "Unknown"
    return f"transport_exception_{exception_name[:40]}"


_RETRYABLE_VT_READ_FAMILIES = frozenset(
    {
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.intelligence_search",
        "vt.behaviour",
        "vt.behaviour_pcap",
    }
)


def _is_retryable_vt_passive_read(prepared: PreparedExternalRequest) -> bool:
    return (
        prepared.provider == "vt"
        and prepared.family in _RETRYABLE_VT_READ_FAMILIES
        and prepared.application_request.method.upper() == b"GET"
    )


__all__ = [
    "Ambiguous",
    "Completed",
    "ConsumeCommitted",
    "ConsumePolicyContext",
    "ConsumeRefused",
    "ConsumeResult",
    "DefinitelyNotSubmitted",
    "ExistingClaim",
    "ExternalEffectClaim",
    "ExternalEffectClaimStore",
    "ExternalEffectCoordinator",
    "ExternalEffectOutcome",
    "ExternalEffectPolicyGuard",
    "ExternalEffectReusePolicy",
    "FrameworkNoSendProof",
    "HandoffGateRefused",
    "NoSubmissionProofKind",
    "PermitAwareTransport",
    "PermitHandoffGate",
    "PhysicalSendCapability",
    "Refused",
    "ReuseDecision",
    "TransportResponse",
]
