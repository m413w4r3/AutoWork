"""One-request external effect admission and permit lifecycle.

The coordinator owns the authorization cut. An injected claim store must
commit ``consume`` before it returns ``ConsumeCommitted``; no database lock is
held while the injected transport runs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
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
    application_request_sha256,
    effect_key,
    occurrence_sha256,
    validate_persistable_reference,
    validate_prepared_request,
)


@dataclass(frozen=True, slots=True)
class FrameworkNoSendProof:
    """A bounded proof that this coordinator invocation made no handoff."""

    kind: str
    reason_code: str


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


ConsumeResult = ConsumeCommitted | ExistingClaim | ConsumeRefused


@dataclass(frozen=True, slots=True)
class ConsumePolicyContext:
    authority: ExternalEffectAuthority
    prepared: PreparedExternalRequest
    effect_key: str
    database_session: Any
    investigation_subject_id: UUID


class ExternalEffectPolicyGuard(Protocol):
    """OSINT guard; policy/revocation writers lock policy before envelope."""

    async def lock_for_consume(
        self, database_session: Any, authority: ExternalEffectAuthority
    ) -> bool:
        """Lock/reload the persisted event and verify active status, version, and subject
        pin before the caller acquires envelope FOR UPDATE. Missing events deny.
        Revocation writers take this policy-event lock before the same envelope lock.
        """

    async def authorize(self, context: ConsumePolicyContext) -> bool: ...


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
    ) -> bool: ...

    async def definitely_not_submitted(
        self, claim_id: UUID, *, proof: _CoordinatorNoHandoffProof, reason_code: str
    ) -> bool: ...

    async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool: ...

    async def record_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        decision: ReuseDecision,
    ) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class TransportResponse:
    result_ref: str | None = None
    evidence_refs: tuple[EvidenceReference, ...] = ()

    def __post_init__(self) -> None:
        if self.result_ref is not None:
            validate_persistable_reference(self.result_ref, "result_ref")
        if any(not isinstance(item, EvidenceReference) for item in self.evidence_refs):
            raise ExternalEffectValidationError("transport evidence refs must be typed references")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _CoordinatorNoHandoffProof:
    """Opaque proof minted only while the coordinator's handoff count is zero."""

    claim_id: UUID
    handoff_count: int
    nonce: UUID
    _issuer: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("no-handoff proofs are issued by ExternalEffectCoordinator")

    @classmethod
    def _issue(cls, *, claim_id: UUID, issuer: object) -> _CoordinatorNoHandoffProof:
        proof = object.__new__(cls)
        for name, value in {
            "claim_id": claim_id,
            "handoff_count": 0,
            "nonce": uuid4(),
            "_issuer": issuer,
        }.items():
            object.__setattr__(proof, name, value)
        return proof


class RequestHandoff(Protocol):
    """The only callback a transport may use to invoke its physical sender."""

    async def __call__(
        self, operation: Callable[[], Awaitable[TransportResponse]]
    ) -> TransportResponse: ...


class PermitAwareTransport(Protocol):
    async def send(
        self,
        request: FrozenWireRequest,
        *,
        permit: ConsumedEffectPermit,
        handoff: RequestHandoff,
    ) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class Completed:
    claim_id: UUID
    result_ref: str | None
    evidence_refs: tuple[EvidenceReference, ...]


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


@dataclass(frozen=True, slots=True)
class DefinitelyNotSubmitted:
    claim_id: UUID
    framework_proof: _CoordinatorNoHandoffProof | None = None
    state: EffectClaimState = EffectClaimState.DEFINITIVELY_NOT_SUBMITTED


@dataclass(frozen=True, slots=True)
class Ambiguous:
    claim_id: UUID | None
    state: EffectClaimState = EffectClaimState.NEEDS_REVIEW


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
        """Burn before handoff; locks are released before the transport call."""

        nonce = getattr(permit, "nonce", None)
        if not isinstance(nonce, UUID):
            return False
        if getattr(permit, "_issuer", None) is not self._issuer:
            return False
        expired = time.monotonic() >= permit.expires_at_monotonic
        matches = (
            permit.investigation_id == authority.investigation_id
            and permit.invocation_id == authority.invocation_id
            and permit.effect_key == key
            and permit.family == prepared.family
            and permit.provider == prepared.provider
            and permit.provider_scope == prepared.provider_scope
            and permit.request_digest == prepared.application_request_sha256
            and permit.envelope_version == authority.expected_envelope_version
            and permit.approval_decision_id == authority.approval_decision_id
            and not expired
            and application_request_sha256(prepared.application_request) == permit.request_digest
        )
        if not matches:
            if expired:
                with self._lock:
                    if self._issued.get(nonce) is permit:
                        del self._issued[nonce]
            return False
        with self._lock:
            issued = self._issued.get(nonce)
            if issued is not permit:
                return False
            del self._issued[nonce]
        return True


class ExternalEffectCoordinator:
    def __init__(
        self,
        claim_store: ExternalEffectClaimStore,
        *,
        reuse_policies: Mapping[str, ExternalEffectReusePolicy] | None = None,
    ) -> None:
        self._claim_store = claim_store
        self._reuse_policies = dict(reuse_policies or {})
        self._permits = _PermitRegistry()
        self._proof_issuer = object()

    async def send_one(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        transport: PermitAwareTransport,
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
                raise ExternalEffectValidationError("OSINT_POLICY authority lacks its policy event")
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
            consume = await self._claim_store.consume(
                authority,
                prepared,
                effect_key=key,
                occurrence_digest=occurrence_digest,
            )
        except Exception:
            # No permit exists and transport has not been entered. A commit
            # acknowledgement failure is safe from egress and a replay will
            # observe the durable claim if the database committed it.
            return Refused(
                reason="consume_unavailable",
                framework_no_send_proof=FrameworkNoSendProof("PRE_CONSUME", "consume_unavailable"),
            )

        if isinstance(consume, ConsumeRefused):
            return Refused(
                reason=consume.reason,
                framework_no_send_proof=FrameworkNoSendProof("PRE_CONSUME", consume.reason),
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

        # Permit creation occurs only after the store reports a committed claim.
        permit = self._permits.issue(consume.claim_id, authority, prepared, key)
        if not self._permits.burn_and_validate(permit, authority, prepared, key):
            return await self._seal_zero_handoff(
                consume.claim_id, handoff_count=0, reason_code="permit_validation_failed"
            )

        handoff_counter = _CoordinatorHandoffCounter(
            claim_id=consume.claim_id,
            issuer=self._proof_issuer,
            expected_request_digest=prepared.application_request_sha256,
            request=prepared.application_request,
        )
        try:
            response = await transport.send(
                prepared.application_request,
                permit=permit,
                handoff=handoff_counter.invoke,
            )
        except asyncio.CancelledError:
            if handoff_counter.count == 0:
                return await self._seal_zero_handoff(
                    consume.claim_id,
                    handoff_count=handoff_counter.count,
                    reason_code="cancelled_before_handoff",
                )
            await self._seal_needs_review(consume.claim_id, "cancelled_after_handoff")
            return Ambiguous(consume.claim_id)
        except Exception:
            if handoff_counter.count == 0:
                return await self._seal_zero_handoff(
                    consume.claim_id,
                    handoff_count=handoff_counter.count,
                    reason_code="transport_failed_before_handoff",
                )
            await self._seal_needs_review(consume.claim_id, "transport_outcome_unknown")
            return Ambiguous(consume.claim_id)

        if handoff_counter.count == 0:
            return await self._seal_zero_handoff(
                consume.claim_id,
                handoff_count=handoff_counter.count,
                reason_code="transport_returned_without_handoff",
            )
        if not isinstance(response, TransportResponse):
            await self._seal_needs_review(consume.claim_id, "invalid_transport_response")
            return Ambiguous(consume.claim_id)
        try:
            completed = await self._claim_store.complete(
                consume.claim_id,
                result_ref=response.result_ref,
                evidence_refs=response.evidence_refs,
            )
        except Exception:
            completed = False
        if not completed:
            await self._seal_needs_review(consume.claim_id, "outcome_persist_failed")
            return Ambiguous(consume.claim_id)
        return Completed(consume.claim_id, response.result_ref, response.evidence_refs)

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
            policy = self._reuse_policies.get(prepared.family)
            if policy is None:
                return Refused(
                    "effect_occurrence_required",
                    FrameworkNoSendProof("EXISTING_CLAIM", "effect_occurrence_required"),
                )
            try:
                decision = await policy.authorize_reuse(claim, authority, prepared)
            except Exception:
                decision = None
            if decision is None:
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
        return Ambiguous(claim.id)

    async def _seal_needs_review(self, claim_id: UUID, reason_code: str) -> None:
        try:
            await self._claim_store.needs_review(claim_id, reason_code=reason_code)
        except Exception:
            # The durable CONSUMED row remains a no-resend barrier if sealing
            # itself is unavailable.
            return

    async def _seal_zero_handoff(
        self, claim_id: UUID, *, handoff_count: int, reason_code: str
    ) -> DefinitelyNotSubmitted | Ambiguous:
        if handoff_count != 0:
            await self._seal_needs_review(claim_id, "invalid_zero_handoff_proof")
            return Ambiguous(claim_id)
        proof = _CoordinatorNoHandoffProof._issue(claim_id=claim_id, issuer=self._proof_issuer)
        try:
            changed = await self._claim_store.definitely_not_submitted(
                claim_id, proof=proof, reason_code=reason_code
            )
        except Exception:
            changed = False
        if changed:
            return DefinitelyNotSubmitted(claim_id, framework_proof=proof)
        await self._seal_needs_review(claim_id, "zero_handoff_seal_failed")
        return Ambiguous(claim_id)


class _CoordinatorHandoffCounter:
    """Coordinator-owned one-request gate passed to the injected transport."""

    def __init__(
        self,
        *,
        claim_id: UUID,
        issuer: object,
        expected_request_digest: str,
        request: FrozenWireRequest,
    ) -> None:
        self.claim_id = claim_id
        self.issuer = issuer
        self.expected_request_digest = expected_request_digest
        self.request = request
        self.count = 0
        self._lock = threading.Lock()

    async def invoke(
        self, operation: Callable[[], Awaitable[TransportResponse]]
    ) -> TransportResponse:
        with self._lock:
            if self.count != 0:
                raise RuntimeError("one physical handoff is permitted")
            if application_request_sha256(self.request) != self.expected_request_digest:
                raise ExternalEffectValidationError("wire request changed before handoff")
            self.count = 1
        return await operation()


def _safe_reason_code(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        return "framework_pre_handoff_failure"
    if not all(char.isalnum() or char in "_-" for char in value):
        return "framework_pre_handoff_failure"
    return value


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
    "PermitAwareTransport",
    "Refused",
    "RequestHandoff",
    "ReuseDecision",
    "TransportResponse",
]
