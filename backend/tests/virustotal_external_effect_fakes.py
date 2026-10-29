from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.external_effects import (
    ConsumeCommitted,
    ExistingClaim,
    ExternalEffectClaim,
    ExternalEffectCoordinator,
    NoSubmissionProofKind,
    ReuseDecision,
    _NoHandoffProofVerifier,
)
from cti_app.application.virustotal import (
    current_virustotal_external_effect_authority,
    virustotal_external_effect_scope,
)
from cti_app.application.virustotal_persistence import (
    ArchivedVirusTotalResponse,
    VirusTotalObservationService,
)
from cti_app.domain.external_effects import (
    EffectClaimState,
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    PreparedExternalRequest,
)
from cti_app.infrastructure.database.repositories.virustotal_external_effects import (
    VT_EXTERNAL_EFFECT_FAMILIES,
    VirusTotalReadReusePolicy,
)


def make_test_authority(*, invocation_id: UUID | None = None) -> ExternalEffectAuthority:
    return ExternalEffectAuthority(
        investigation_id=UUID("91bb9e76-8d46-4d09-bfde-034c39a26b87"),
        invocation_id=invocation_id or UUID("a7f9e0bd-6e03-4bfb-a1d3-86ec4b4e4486"),
        operation_key="test-vt-read-operation",
        reservation_amounts={"vt_units": 1_000},
        expected_envelope_version=1,
        approval_decision_id=UUID("0d86dbde-1838-4b45-8e37-aef123cd9dc8"),
        invocation_arguments_sha256="a" * 64,
        authority_kind=ExternalEffectAuthorityKind.HUMAN_G2,
    )


@contextmanager
def authorized_vt_read(*, invocation_id: UUID | None = None) -> Iterator[ExternalEffectAuthority]:
    authority = make_test_authority(invocation_id=invocation_id)
    with virustotal_external_effect_scope(authority, passive_read_retries=1):
        yield authority


@contextmanager
def authorized_vt_download() -> Iterator[ExternalEffectAuthority]:
    authority = replace(
        make_test_authority(),
        operation_key="test-vt-download-operation",
        reservation_amounts={"downloads": 1, "vt_units": 1},
        acquisition_dispatch_attempt_id=uuid4(),
    )
    with virustotal_external_effect_scope(authority):
        yield authority


class InMemoryExternalEffectClaimStore:
    """Minimal durable-store double that still exercises the real coordinator gate."""

    def __init__(self) -> None:
        self.claims: dict[str, ExternalEffectClaim] = {}
        self.consumed_operation_keys: list[str] = []
        self.gate_nonces: set[UUID] = set()
        self.reuse_count = 0
        self.invocation_review_marks: list[bool] = []
        self._proof_verifier: _NoHandoffProofVerifier | None = None

    def _bind_no_handoff_proof_verifier(self, verifier: _NoHandoffProofVerifier) -> None:
        if type(verifier) is not _NoHandoffProofVerifier or self._proof_verifier is not None:
            raise TypeError("claim store requires one coordinator-bound verifier")
        verifier.bind_store(self)
        self._proof_verifier = verifier

    async def consume(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        *,
        effect_key: str,
        occurrence_digest: str,
    ) -> ConsumeCommitted | ExistingClaim:
        del occurrence_digest
        self.consumed_operation_keys.append(authority.operation_key)
        existing = self.claims.get(effect_key)
        if existing is not None:
            return ExistingClaim(existing)
        claim_id = uuid4()
        self.claims[effect_key] = ExternalEffectClaim(
            id=claim_id,
            investigation_id=authority.investigation_id,
            invocation_id=authority.invocation_id,
            effect_key=effect_key,
            family=prepared.family,
            provider=prepared.provider,
            provider_scope=prepared.provider_scope,
            canonical_request_sha256=prepared.canonical_request_sha256,
            application_request_sha256=prepared.application_request_sha256,
            envelope_version=authority.expected_envelope_version,
            approval_decision_id=authority.approval_decision_id,
            state=EffectClaimState.CONSUMED,
        )
        return ConsumeCommitted(claim_id)

    async def complete(
        self,
        claim_id: UUID,
        *,
        result_ref: str | None,
        evidence_refs: tuple[EvidenceReference, ...],
        outcome_code: str | None = None,
    ) -> bool:
        key, claim = self._find(claim_id)
        self.claims[key] = replace(
            claim,
            state=EffectClaimState.COMPLETED,
            result_ref=result_ref,
            evidence_refs=evidence_refs,
            outcome_code=outcome_code,
        )
        return True

    async def enter_handoff(
        self,
        claim_id: UUID,
        *,
        permit_nonce: UUID,
        request_digest: str,
        expires_at_monotonic: float,
    ) -> bool:
        del request_digest, expires_at_monotonic
        _, claim = self._find(claim_id)
        if claim.state is not EffectClaimState.CONSUMED or permit_nonce in self.gate_nonces:
            return False
        self.gate_nonces.add(permit_nonce)
        return True

    async def definitely_not_submitted(
        self, claim_id: UUID, *, proof: object, reason_code: str
    ) -> bool:
        del reason_code
        kind = (
            self._proof_verifier.consume(self, proof, claim_id)
            if self._proof_verifier is not None
            else None
        )
        if kind not in {
            NoSubmissionProofKind.BEFORE_HANDOFF,
            NoSubmissionProofKind.TRANSPORT_NO_BYTES_LEFT,
        }:
            return False
        key, claim = self._find(claim_id)
        self.claims[key] = replace(claim, state=EffectClaimState.DEFINITIVELY_NOT_SUBMITTED)
        return True

    async def needs_review(
        self,
        claim_id: UUID,
        *,
        reason_code: str,
        mark_invocation: bool = True,
    ) -> bool:
        del reason_code
        self.invocation_review_marks.append(mark_invocation)
        key, claim = self._find(claim_id)
        self.claims[key] = replace(claim, state=EffectClaimState.NEEDS_REVIEW)
        return True

    async def record_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        decision: ReuseDecision,
    ) -> UUID:
        del claim, authority, prepared, decision
        self.reuse_count += 1
        return uuid4()

    async def release_refused_reuse_reservation(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> bool:
        del claim, authority, prepared
        return True

    def _find(self, claim_id: UUID) -> tuple[str, ExternalEffectClaim]:
        for key, claim in self.claims.items():
            if claim.id == claim_id:
                return key, claim
        raise KeyError(claim_id)


class InMemoryVirusTotalArchive(VirusTotalObservationService):
    """A SHA-verifying archive double; it persists no transport credentials."""

    def __init__(self) -> None:
        self.items: dict[str, ArchivedVirusTotalResponse] = {}

    async def archive_external_response(
        self,
        content: bytes,
        *,
        status_code: int,
        content_type_class: str,
        location_host: str | None,
        retry_after: float | None,
        issuer: str,
        oversized: bool,
    ) -> EvidenceReference:
        reference = EvidenceReference(
            kind="vt.raw_response",
            id=str(uuid4()),
            version="test-vt-http-response.v1",
            sha256=hashlib.sha256(content).hexdigest(),
        )
        self.items[reference.id] = ArchivedVirusTotalResponse(
            body=content,
            status_code=status_code,
            content_type_class=content_type_class,
            reference=reference,
            location_host=location_host,
            retry_after=retry_after,
            issuer=issuer,
            oversized=oversized,
        )
        return reference

    async def read_external_response(
        self, reference: EvidenceReference, *, max_bytes: int
    ) -> ArchivedVirusTotalResponse:
        result = self.items[reference.id]
        if (
            result.reference != reference
            or len(result.body) > max_bytes
            or hashlib.sha256(result.body).hexdigest() != reference.sha256
        ):
            raise ValueError("archive reference is missing or unverifiable")
        return result


def configure_test_vt_adapter(
    adapter: Any,
) -> tuple[InMemoryExternalEffectClaimStore, InMemoryVirusTotalArchive]:
    store = InMemoryExternalEffectClaimStore()
    archive = InMemoryVirusTotalArchive()
    if (
        getattr(adapter, "_offline_probe_mode", False)
        and current_virustotal_external_effect_authority() is None
    ):
        return store, archive
    coordinator = ExternalEffectCoordinator(
        store,
        reuse_policies={
            family: VirusTotalReadReusePolicy() for family in VT_EXTERNAL_EFFECT_FAMILIES
        },
    )
    configure = adapter.configure_external_effects
    configure(coordinator, archive)
    adapter._test_external_effect_store = store
    adapter._test_vt_response_archive = archive
    return store, archive
