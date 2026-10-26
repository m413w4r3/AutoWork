"""PostgreSQL claim store for serialized external-effect consumption."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.external_effects import (
    ConsumeCommitted,
    ConsumePolicyContext,
    ConsumeRefused,
    ConsumeResult,
    ExistingClaim,
    ExternalEffectClaim,
    ExternalEffectPolicyGuard,
    NoSubmissionProofKind,
    ReuseDecision,
    _CoordinatorNoHandoffProof,
    _NoHandoffProofVerifier,
)
from cti_app.domain.external_effects import (
    EffectClaimState,
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectUseKind,
    PreparedExternalRequest,
    canonical_json_bytes,
    occurrence_sha256,
    validate_persistable_reference,
    validate_prepared_request,
)
from cti_app.domain.external_effects import (
    effect_key as compute_effect_key,
)
from cti_app.domain.investigation import BudgetReservationStatus
from cti_app.infrastructure.database.models.core import SampleRow, SourceDocumentRow
from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectClaimRow,
    ExternalEffectUseRow,
)
from cti_app.infrastructure.database.models.investigation import (
    InvestigationBudgetReservationAmountRow,
    InvestigationBudgetReservationRow,
    InvestigationEnvelopeRow,
    InvestigationWorkingStateRow,
    SampleAssociationRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.models.production import (
    AnalystInvestigationRow,
    SampleAcquisitionAttemptRow,
)
from cti_app.infrastructure.database.models.retrohunt import RetrohuntReservationRow
from cti_app.infrastructure.database.repositories.investigation import (
    _budget_from_payload,
    _budget_to_payload,
)


class _ConsumeDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


SourceReferenceResolver = Callable[[Mapping[str, Any]], tuple[EvidenceReference, ...]]


@dataclass(frozen=True, slots=True)
class ExternalEffectFamilyBinding:
    """Server-owned provider, model, and source contract for one external family.

    A source resolver derives the expected evidence references from persisted,
    normalized invocation inputs; callers cannot use empty references to bypass
    a source restriction.
    """

    provider: str
    provider_scopes: frozenset[str]
    normalizer_versions: frozenset[str]
    invocation_tool_binding: tuple[str, str] | None
    model_submission: bool = False
    requires_model_lineage: bool = False
    model_run_provider: str | None = None
    requires_source_refs: bool = False
    source_reference_resolver: SourceReferenceResolver | None = None

    def __post_init__(self) -> None:
        if self.requires_source_refs and self.source_reference_resolver is None:
            raise ValueError("source-dependent families require a trusted source resolver")
        if self.requires_model_lineage and self.model_run_provider is None:
            raise ValueError("model-lineage families require a persisted model provider binding")


def _is_model_submission(
    prepared: PreparedExternalRequest, family_binding: ExternalEffectFamilyBinding
) -> bool:
    return (
        prepared.provider.casefold() in _MODEL_SUBMISSION_PROVIDERS
        or family_binding.model_submission
        or prepared.family.startswith(_MODEL_LINEAGE_PREFIXES)
    )


def _requires_model_lineage(family: str, family_binding: ExternalEffectFamilyBinding) -> bool:
    return family_binding.requires_model_lineage or family.startswith(_MODEL_LINEAGE_PREFIXES)


def _retrohunt_request_matches(
    attempt: RetrohuntReservationRow,
    prepared: PreparedExternalRequest,
    authority: ExternalEffectAuthority,
) -> bool:
    if authority.operation_key.strip() == "":
        return False
    expected_request = {
        "schema": "vt-retrohunt-operation.v1",
        "rule_source_sha256": attempt.rule_source_sha256,
        "corpus_id": attempt.corpus_id,
        "corpus_sha256": attempt.corpus_sha256,
        "parameters": attempt.parameters,
    }
    request = dict(prepared.canonical_request)
    if request.get("version_id") != str(attempt.rule_version_id):
        return False
    request.pop("version_id", None)
    if request != expected_request:
        return False
    expected_operation_key = _sha256(canonical_json_bytes(expected_request))
    return attempt.operation_key == authority.operation_key == expected_operation_key


_MODEL_SUBMISSION_PROVIDERS = frozenset(
    {
        "openai",
        "gemini",
        "qwen",
        "bridge",
        "chatgpt_bridge",
        "openai_chatgpt_bridge",
    }
)
_MODEL_LINEAGE_PREFIXES = ("bridge.", "advisor.")
_RETROHUNT_PREFIXES = ("retrohunt.",)
_ACQUISITION_PREFIXES = ("acquisition.", "vt.download")


class SqlAlchemyExternalEffectClaimStore:
    """Owns each consume/CAS transaction and returns only after commit.

    Normalizer and invocation bindings are server-owned composition data. An
    absent binding fails closed. ``sample_acquisition_attempts`` is currently
    a terminal ledger, so acquisition requests are refused until the later
    adapter lot adds its pre-send dispatch-attempt lineage.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        policy_guard: ExternalEffectPolicyGuard | None = None,
        family_bindings: Mapping[str, ExternalEffectFamilyBinding] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._policy_guard = policy_guard
        self._family_bindings = dict(family_bindings or {})
        self._no_handoff_proof_verifier: _NoHandoffProofVerifier | None = None

    def _bind_no_handoff_proof_verifier(self, verifier: _NoHandoffProofVerifier) -> None:
        if type(verifier) is not _NoHandoffProofVerifier:
            raise TypeError("claim store requires a coordinator-bound no-handoff verifier")
        if self._no_handoff_proof_verifier is not None:
            raise ValueError("claim store already has a no-handoff proof verifier")
        verifier.bind_store(self)
        self._no_handoff_proof_verifier = verifier

    def _consume_no_handoff_proof(
        self, proof: _CoordinatorNoHandoffProof, claim_id: UUID
    ) -> NoSubmissionProofKind | None:
        verifier = self._no_handoff_proof_verifier
        return verifier.consume(self, proof, claim_id) if verifier is not None else None

    async def consume(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        *,
        effect_key: str,
        occurrence_digest: str,
    ) -> ConsumeResult:
        if (
            authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
            and self._policy_guard is None
        ):
            return ConsumeRefused("osint_policy_guard_unavailable")
        if prepared.identity_version != "effect-key.v1":
            return ConsumeRefused("unsupported_identity_version")
        try:
            validate_prepared_request(prepared)
            if compute_effect_key(authority.investigation_id, prepared) != effect_key:
                return ConsumeRefused("effect_key_mismatch")
            if occurrence_sha256(prepared.occurrence_id) != occurrence_digest:
                return ConsumeRefused("occurrence_digest_mismatch")
        except (TypeError, ValueError):
            return ConsumeRefused("invalid_prepared_effect")
        family_binding = self._family_bindings.get(prepared.family)
        if family_binding is None:
            return ConsumeRefused("family_not_registered")
        if (
            family_binding.provider != prepared.provider
            or prepared.provider_scope not in family_binding.provider_scopes
        ):
            return ConsumeRefused("provider_scope_not_registered")
        if prepared.normalizer_version not in family_binding.normalizer_versions:
            return ConsumeRefused("unsupported_normalizer_version")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    if authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY:
                        assert self._policy_guard is not None
                        try:
                            policy_locked = await self._policy_guard.lock_for_consume(
                                session, authority
                            )
                        except Exception as exc:
                            raise _ConsumeDenied("osint_policy_lock_failed") from exc
                        if not policy_locked:
                            raise _ConsumeDenied("osint_policy_denied")
                    (
                        investigation,
                        envelope,
                        invocation,
                        reservation,
                        _amount_rows,
                    ) = await self._lock_authority(
                        session, authority, prepared, effect_key, allow_any_reservation_state=True
                    )
                    existing = await session.scalar(
                        select(ExternalEffectClaimRow)
                        .where(
                            ExternalEffectClaimRow.investigation_id == authority.investigation_id,
                            ExternalEffectClaimRow.effect_key == effect_key,
                        )
                        .with_for_update()
                    )
                    await self._check_authority(
                        session,
                        authority,
                        prepared,
                        effect_key,
                        investigation,
                        envelope,
                        invocation,
                        family_binding,
                        allow_nonrunning_invocation=existing is not None,
                    )
                    self._validate_family_source_refs(family_binding, invocation, prepared)
                    if existing is not None:
                        if (
                            reservation.status == "released"
                            and existing.state != EffectClaimState.COMPLETED.value
                        ):
                            raise _ConsumeDenied("reservation_already_released")
                        result: ConsumeResult = ExistingClaim(_claim_to_domain(existing))
                    else:
                        if reservation.status != "reserved":
                            raise _ConsumeDenied("reservation_not_reserved")
                        now = datetime.now(UTC)
                        row_id = uuid4()
                        statement = (
                            insert(ExternalEffectClaimRow)
                            .values(
                                id=row_id,
                                investigation_id=authority.investigation_id,
                                invocation_id=authority.invocation_id,
                                bridge_turn_id=authority.bridge_turn_id,
                                model_run_id=authority.model_run_id,
                                acquisition_attempt_id=authority.acquisition_attempt_id,
                                retrohunt_reservation_id=authority.retrohunt_reservation_id,
                                provider=prepared.provider,
                                provider_scope=prepared.provider_scope,
                                family=prepared.family,
                                identity_version=prepared.identity_version,
                                normalizer_version=prepared.normalizer_version,
                                effect_key=effect_key,
                                canonical_request_sha256=prepared.canonical_request_sha256,
                                application_request_sha256=prepared.application_request_sha256,
                                occurrence_sha256=occurrence_digest,
                                occurrence_id=prepared.occurrence_id,
                                request_ref=prepared.request_ref,
                                canonical_request=_thaw_json(prepared.canonical_request),
                                evidence_refs=[ref.to_json() for ref in prepared.evidence_refs],
                                operation_key=authority.operation_key,
                                reservation_amounts=dict(authority.reservation_amounts),
                                envelope_version=authority.expected_envelope_version,
                                approval_decision_id=authority.approval_decision_id,
                                invocation_arguments_sha256=authority.invocation_arguments_sha256,
                                state=EffectClaimState.CONSUMED.value,
                                version=1,
                                created_at=now,
                                consumed_at=now,
                                updated_at=now,
                            )
                            .on_conflict_do_nothing(
                                index_elements=[
                                    ExternalEffectClaimRow.investigation_id,
                                    ExternalEffectClaimRow.effect_key,
                                ]
                            )
                            .returning(ExternalEffectClaimRow.id)
                        )
                        inserted_id = (await session.execute(statement)).scalar_one_or_none()
                        if inserted_id is None:
                            existing = await session.scalar(
                                select(ExternalEffectClaimRow)
                                .where(
                                    ExternalEffectClaimRow.investigation_id
                                    == authority.investigation_id,
                                    ExternalEffectClaimRow.effect_key == effect_key,
                                )
                                .with_for_update()
                            )
                            if existing is None:
                                raise RuntimeError(
                                    "unique effect claim conflict row was not visible"
                                )
                            result = ExistingClaim(_claim_to_domain(existing))
                        else:
                            await self._lock_and_consume_family_attempt(
                                session, authority, prepared, investigation, family_binding
                            )
                            await self._check_source_restrictions(
                                session,
                                authority,
                                prepared,
                                investigation.subject_id,
                                family_binding,
                            )
                            await self._lock_and_start_model_attempt(
                                session,
                                authority,
                                prepared,
                                investigation.subject_id,
                                family_binding,
                            )
                            result = ConsumeCommitted(row_id)
            return result
        except _ConsumeDenied as denied:
            return ConsumeRefused(denied.reason)

    async def enter_handoff(
        self,
        claim_id: UUID,
        *,
        permit_nonce: UUID,
        request_digest: str,
        expires_at_monotonic: float,
    ) -> bool:
        if (
            not isinstance(permit_nonce, UUID)
            or not re.fullmatch(r"[0-9a-f]{64}", request_digest)
            or not math.isfinite(expires_at_monotonic)
        ):
            return False
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ExternalEffectClaimRow)
                    .where(ExternalEffectClaimRow.id == claim_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    row is None
                    or row.state != EffectClaimState.CONSUMED.value
                    or row.handoff_permit_nonce is not None
                    or row.application_request_sha256 != request_digest
                    or time.monotonic() >= expires_at_monotonic
                ):
                    return False
                row.handoff_permit_nonce = permit_nonce
                row.version += 1
                row.updated_at = datetime.now(UTC)
                return True

    async def complete(
        self,
        claim_id: UUID,
        *,
        result_ref: str | None,
        evidence_refs: tuple[EvidenceReference, ...],
        outcome_code: str | None = None,
    ) -> bool:
        if outcome_code is not None and (
            not isinstance(outcome_code, str)
            or outcome_code not in {"bridge_rate_limited", "bridge_rejected"}
        ):
            return False
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._claim_for_transition(session, claim_id)
                if row is None:
                    return False
                await self._lock_transition_parents(session, row)
                now = datetime.now(UTC)
                persisted_refs = list(row.evidence_refs or [])
                persisted_refs.extend(ref.to_json() for ref in evidence_refs)
                if result_ref is not None:
                    validate_persistable_reference(result_ref, "result_ref")
                    persisted_refs.append({"kind": "result_ref", "reference": result_ref})
                changed_id = await session.scalar(
                    update(ExternalEffectClaimRow)
                    .where(
                        ExternalEffectClaimRow.id == claim_id,
                        ExternalEffectClaimRow.state == EffectClaimState.CONSUMED.value,
                        ExternalEffectClaimRow.handoff_permit_nonce.is_not(None),
                        ExternalEffectClaimRow.version == row.version,
                    )
                    .values(
                        state=EffectClaimState.COMPLETED.value,
                        version=row.version + 1,
                        evidence_refs=persisted_refs,
                        updated_at=now,
                        completed_at=now,
                        last_error_code=outcome_code,
                    )
                    .returning(ExternalEffectClaimRow.id)
                )
                if changed_id is None:
                    return False
                if row.invocation_id is not None:
                    observation_refs = list(ref.to_json() for ref in evidence_refs)
                    if result_ref is not None:
                        observation_refs.append({"kind": "result_ref", "reference": result_ref})
                    await session.execute(
                        insert(ExternalEffectUseRow)
                        .values(
                            id=uuid4(),
                            claim_id=claim_id,
                            invocation_id=row.invocation_id,
                            use_kind=ExternalEffectUseKind.SENT.value,
                            observation_refs=observation_refs,
                            budget_delta={},
                            created_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                ExternalEffectUseRow.claim_id,
                                ExternalEffectUseRow.invocation_id,
                            ]
                        )
                    )
                if row.model_run_id is not None:
                    await session.execute(
                        update(ModelRunRow)
                        .where(
                            ModelRunRow.id == row.model_run_id,
                            ModelRunRow.submission_state == "submission_in_progress",
                        )
                        .values(submission_state="result_obtained", updated_at=now)
                    )
                return True

    async def definitely_not_submitted(
        self,
        claim_id: UUID,
        *,
        proof: _CoordinatorNoHandoffProof,
        reason_code: str,
    ) -> bool:
        proof_kind = self._consume_no_handoff_proof(proof, claim_id)
        if proof_kind is None:
            return False
        expected_handoff_nonce_is_null = proof_kind is NoSubmissionProofKind.BEFORE_HANDOFF
        reason = _safe_code(reason_code)
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._claim_for_transition(session, claim_id)
                if row is None:
                    return False
                await self._lock_transition_parents(session, row)
                now = datetime.now(UTC)
                changed_id = await session.scalar(
                    update(ExternalEffectClaimRow)
                    .where(
                        ExternalEffectClaimRow.id == claim_id,
                        ExternalEffectClaimRow.state == EffectClaimState.CONSUMED.value,
                        (
                            ExternalEffectClaimRow.handoff_permit_nonce.is_(None)
                            if expected_handoff_nonce_is_null
                            else ExternalEffectClaimRow.handoff_permit_nonce.is_not(None)
                        ),
                        ExternalEffectClaimRow.version == row.version,
                    )
                    .values(
                        state=EffectClaimState.DEFINITIVELY_NOT_SUBMITTED.value,
                        version=row.version + 1,
                        updated_at=now,
                        last_error_code=reason,
                    )
                    .returning(ExternalEffectClaimRow.id)
                )
                if changed_id is None:
                    return False
                if row.invocation_id is not None:
                    await session.execute(
                        update(ToolInvocationRow)
                        .where(
                            ToolInvocationRow.id == row.invocation_id,
                            ToolInvocationRow.status == "running",
                        )
                        .values(
                            status="failed", version=ToolInvocationRow.version + 1, updated_at=now
                        )
                    )
                if (
                    row.model_run_id is not None
                    and proof_kind is NoSubmissionProofKind.TRANSPORT_NO_BYTES_LEFT
                ):
                    await session.execute(
                        update(ModelRunRow)
                        .where(
                            ModelRunRow.id == row.model_run_id,
                            ModelRunRow.submission_state == "submission_in_progress",
                        )
                        .values(submission_state="not_submitted", updated_at=now)
                    )
                return True

    async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool:
        reason = _safe_code(reason_code)
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._claim_for_transition(session, claim_id)
                if row is None:
                    return False
                await self._lock_transition_parents(session, row)
                now = datetime.now(UTC)
                changed_id = await session.scalar(
                    update(ExternalEffectClaimRow)
                    .where(
                        ExternalEffectClaimRow.id == claim_id,
                        ExternalEffectClaimRow.state == EffectClaimState.CONSUMED.value,
                        ExternalEffectClaimRow.version == row.version,
                    )
                    .values(
                        state=EffectClaimState.NEEDS_REVIEW.value,
                        version=row.version + 1,
                        updated_at=now,
                        last_error_code=reason,
                    )
                    .returning(ExternalEffectClaimRow.id)
                )
                if changed_id is None:
                    return False
                if row.invocation_id is not None:
                    await session.execute(
                        update(ToolInvocationRow)
                        .where(
                            ToolInvocationRow.id == row.invocation_id,
                            ToolInvocationRow.status == "running",
                        )
                        .values(
                            status="needs_review",
                            version=ToolInvocationRow.version + 1,
                            updated_at=now,
                        )
                    )
                if row.retrohunt_reservation_id is not None:
                    await session.execute(
                        update(RetrohuntReservationRow)
                        .where(
                            RetrohuntReservationRow.id == row.retrohunt_reservation_id,
                            RetrohuntReservationRow.state == "submitting",
                        )
                        .values(
                            state="reconciliation_required",
                            version=RetrohuntReservationRow.version + 1,
                            updated_at=now,
                        )
                    )
                if row.bridge_turn_id is not None and row.model_run_id is not None:
                    await session.execute(
                        update(ModelConversationTurnRow)
                        .where(
                            ModelConversationTurnRow.id == row.bridge_turn_id,
                            ModelConversationTurnRow.status == "running",
                        )
                        .values(status="needs_review", finished_at=now, error_code=reason)
                    )
                    await session.execute(
                        update(ModelRunRow)
                        .where(ModelRunRow.id == row.model_run_id)
                        .values(
                            status="needs_review",
                            submission_state="external_state_unknown",
                            error_code=reason,
                            updated_at=now,
                        )
                    )
                return True

    async def record_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        decision: ReuseDecision,
    ) -> UUID | None:
        if (
            authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
            and self._policy_guard is None
        ):
            return None
        if authority.invocation_id is None:
            return None
        family_binding = self._family_bindings.get(prepared.family)
        if (
            family_binding is None
            or family_binding.provider != prepared.provider
            or prepared.provider_scope not in family_binding.provider_scopes
            or prepared.normalizer_version not in family_binding.normalizer_versions
            or _requires_model_lineage(prepared.family, family_binding)
        ):
            return None
        if (
            authority.acquisition_attempt_id is not None
            or authority.retrohunt_reservation_id is not None
        ):
            return None
        if any(not isinstance(ref, EvidenceReference) for ref in decision.observation_refs):
            return None
        if claim.invocation_id is not None and decision.source_invocation_id != claim.invocation_id:
            return None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    if authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY:
                        assert self._policy_guard is not None
                        if not await self._policy_guard.lock_for_consume(session, authority):
                            return None
                    (
                        investigation,
                        envelope,
                        invocation,
                        reservation,
                        _amount_rows,
                    ) = await self._lock_authority(
                        session,
                        authority,
                        prepared,
                        claim.effect_key,
                        allow_any_reservation_state=True,
                    )
                    await self._check_authority(
                        session,
                        authority,
                        prepared,
                        claim.effect_key,
                        investigation,
                        envelope,
                        invocation,
                        family_binding,
                    )
                    self._validate_family_source_refs(family_binding, invocation, prepared)
                    current = await session.scalar(
                        select(ExternalEffectClaimRow)
                        .where(ExternalEffectClaimRow.id == claim.id)
                        .with_for_update()
                    )
                    if current is None or current.state != EffectClaimState.COMPLETED.value:
                        return None
                    if (
                        current.effect_key != claim.effect_key
                        or current.application_request_sha256 != prepared.application_request_sha256
                        or current.canonical_request_sha256 != prepared.canonical_request_sha256
                    ):
                        return None
                    await self._check_source_restrictions(
                        session,
                        authority,
                        prepared,
                        investigation.subject_id,
                        family_binding,
                    )
                    if reservation.status == "released":
                        existing_use = await session.scalar(
                            select(ExternalEffectUseRow).where(
                                ExternalEffectUseRow.claim_id == claim.id,
                                ExternalEffectUseRow.invocation_id == authority.invocation_id,
                                ExternalEffectUseRow.use_kind == ExternalEffectUseKind.REUSED.value,
                            )
                        )
                        return existing_use.id if existing_use is not None else None
                    if reservation.status != "reserved":
                        return None
                    now = datetime.now(UTC)
                    use_id = uuid4()
                    budget_delta = {dimension: 0 for dimension in authority.reservation_amounts}
                    inserted = await session.scalar(
                        insert(ExternalEffectUseRow)
                        .values(
                            id=use_id,
                            claim_id=claim.id,
                            invocation_id=authority.invocation_id,
                            use_kind=ExternalEffectUseKind.REUSED.value,
                            observation_refs=[ref.to_json() for ref in decision.observation_refs],
                            budget_delta=budget_delta,
                            created_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                ExternalEffectUseRow.claim_id,
                                ExternalEffectUseRow.invocation_id,
                            ]
                        )
                        .returning(ExternalEffectUseRow.id)
                    )
                    if inserted is None:
                        existing_use = await session.scalar(
                            select(ExternalEffectUseRow).where(
                                ExternalEffectUseRow.claim_id == claim.id,
                                ExternalEffectUseRow.invocation_id == authority.invocation_id,
                            )
                        )
                        if (
                            existing_use is None
                            or existing_use.use_kind != ExternalEffectUseKind.REUSED.value
                        ):
                            return None
                        use_id = existing_use.id
                    reservation.status = "released"
                    return use_id
        except _ConsumeDenied:
            return None

    async def release_refused_reuse_reservation(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> bool:
        """Release only a different invocation's still-reserved replay budget."""

        if (
            claim.state is not EffectClaimState.COMPLETED
            or claim.investigation_id != authority.investigation_id
            or authority.invocation_id is None
            or authority.invocation_id == claim.invocation_id
        ):
            return False
        if (
            authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
            and self._policy_guard is None
        ):
            return False
        family_binding = self._family_bindings.get(prepared.family)
        if (
            family_binding is None
            or family_binding.provider != prepared.provider
            or prepared.provider_scope not in family_binding.provider_scopes
            or prepared.normalizer_version not in family_binding.normalizer_versions
        ):
            return False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    if authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY:
                        assert self._policy_guard is not None
                        try:
                            policy_locked = await self._policy_guard.lock_for_consume(
                                session, authority
                            )
                        except Exception as exc:
                            raise _ConsumeDenied("osint_policy_lock_failed") from exc
                        if not policy_locked:
                            raise _ConsumeDenied("osint_policy_denied")
                    (
                        investigation,
                        envelope,
                        invocation,
                        reservation,
                        _amount_rows,
                    ) = await self._lock_authority(
                        session,
                        authority,
                        prepared,
                        claim.effect_key,
                        allow_any_reservation_state=True,
                    )
                    await self._check_authority(
                        session,
                        authority,
                        prepared,
                        claim.effect_key,
                        investigation,
                        envelope,
                        invocation,
                        family_binding,
                        allow_nonrunning_invocation=True,
                    )
                    self._validate_family_source_refs(family_binding, invocation, prepared)
                    existing = await session.scalar(
                        select(ExternalEffectClaimRow)
                        .where(
                            ExternalEffectClaimRow.investigation_id == authority.investigation_id,
                            ExternalEffectClaimRow.effect_key == claim.effect_key,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if (
                        existing is None
                        or existing.id != claim.id
                        or existing.state != EffectClaimState.COMPLETED.value
                        or existing.invocation_id != claim.invocation_id
                        or existing.application_request_sha256
                        != prepared.application_request_sha256
                        or existing.canonical_request_sha256 != prepared.canonical_request_sha256
                        or reservation.status != "reserved"
                    ):
                        return False
                    try:
                        budget = _budget_from_payload(envelope.budget_snapshot)
                    except (KeyError, TypeError, ValueError):
                        return False
                    budget_reservation = next(
                        (
                            item
                            for item in budget.reservations
                            if item.operation_key == authority.operation_key
                        ),
                        None,
                    )
                    if (
                        budget_reservation is None
                        or budget_reservation.status is not BudgetReservationStatus.RESERVED
                    ):
                        return False
                    budget.release(authority.operation_key)
                    envelope.budget_snapshot = _budget_to_payload(budget)
                    envelope.version += 1
                    envelope.updated_at = datetime.now(UTC)
                    reservation.status = "released"
                    return True
        except _ConsumeDenied:
            return False

    async def _lock_authority(
        self,
        session: AsyncSession,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
        *,
        allow_any_reservation_state: bool = False,
    ) -> tuple[
        AnalystInvestigationRow,
        InvestigationEnvelopeRow,
        ToolInvocationRow | None,
        InvestigationBudgetReservationRow,
        list[Any],
    ]:
        # OSINT callers already hold the pinned-policy lock. Revocation writers
        # must take that same policy lock before this envelope lock, then follow
        # the persisted-stop, invocation, reservation, claim, and family order.
        envelope = await session.scalar(
            select(InvestigationEnvelopeRow)
            .where(InvestigationEnvelopeRow.investigation_id == authority.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if envelope is None:
            raise _ConsumeDenied("investigation_envelope_missing")
        investigation = await session.scalar(
            select(AnalystInvestigationRow).where(
                AnalystInvestigationRow.id == authority.investigation_id
            )
        )
        if investigation is None:
            raise _ConsumeDenied("investigation_missing")
        working_state = await session.scalar(
            select(InvestigationWorkingStateRow)
            .where(InvestigationWorkingStateRow.investigation_id == authority.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if working_state is not None and working_state.stop_reason is not None:
            raise _ConsumeDenied("investigation_stopped")
        if investigation.status != "running":
            raise _ConsumeDenied("investigation_not_running")

        invocation: ToolInvocationRow | None = None
        if authority.invocation_id is not None:
            invocation = await session.scalar(
                select(ToolInvocationRow)
                .where(ToolInvocationRow.id == authority.invocation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if invocation is None:
                raise _ConsumeDenied("invocation_missing")

        reservation = await session.scalar(
            select(InvestigationBudgetReservationRow)
            .where(
                InvestigationBudgetReservationRow.investigation_id == authority.investigation_id,
                InvestigationBudgetReservationRow.operation_key == authority.operation_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        allowed_reservation_states = (
            {"reserved", "released", "committed"} if allow_any_reservation_state else {"reserved"}
        )
        if reservation is None or reservation.status not in allowed_reservation_states:
            raise _ConsumeDenied("reservation_not_reserved")
        amount_rows = list(
            await session.scalars(
                select(InvestigationBudgetReservationAmountRow)
                .where(
                    InvestigationBudgetReservationAmountRow.investigation_id
                    == authority.investigation_id,
                    InvestigationBudgetReservationAmountRow.operation_key
                    == authority.operation_key,
                )
                .order_by(InvestigationBudgetReservationAmountRow.dimension)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        actual_amounts = {
            row.dimension: int(row.reserved_amount)
            for row in amount_rows
            if row.reserved_amount is not None
        }
        if not authority.reservation_amounts or actual_amounts != dict(
            authority.reservation_amounts
        ):
            raise _ConsumeDenied("reservation_amounts_mismatch")
        return investigation, envelope, invocation, reservation, amount_rows

    async def _check_authority(
        self,
        session: AsyncSession,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        key: str,
        investigation: AnalystInvestigationRow,
        envelope: InvestigationEnvelopeRow,
        invocation: ToolInvocationRow | None,
        family_binding: ExternalEffectFamilyBinding,
        *,
        allow_nonrunning_invocation: bool = False,
    ) -> None:
        if envelope.version != authority.expected_envelope_version:
            raise _ConsumeDenied("envelope_version_changed")
        if envelope.approval_decision_id != authority.approval_decision_id:
            raise _ConsumeDenied("approval_changed")
        if prepared.provider not in envelope.allowed_providers:
            raise _ConsumeDenied("provider_not_allowed")
        if envelope.disclosure_tlp == "red":
            raise _ConsumeDenied("tlp_red")
        if envelope.disclosure_do_not_submit:
            raise _ConsumeDenied("do_not_submit")
        is_model_submission = _is_model_submission(prepared, family_binding)
        if is_model_submission and not envelope.disclosure_external_llm_allowed:
            raise _ConsumeDenied("external_llm_not_allowed")
        if (
            prepared.family.startswith(("acquisition.", "vt.download"))
            and not envelope.acquire_hits_allowed
        ):
            raise _ConsumeDenied("acquisition_not_allowed")
        if (
            authority.pinned_subject_id is not None
            and investigation.subject_id != authority.pinned_subject_id
        ):
            raise _ConsumeDenied("pinned_subject_mismatch")

        if authority.authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY:
            if self._policy_guard is None:
                raise _ConsumeDenied("osint_policy_guard_unavailable")
            try:
                allowed = await self._policy_guard.authorize(
                    ConsumePolicyContext(
                        authority=authority,
                        prepared=prepared,
                        effect_key=key,
                        database_session=session,
                        investigation_subject_id=investigation.subject_id,
                    )
                )
            except Exception as exc:
                raise _ConsumeDenied("osint_policy_guard_error") from exc
            if not allowed:
                raise _ConsumeDenied("osint_policy_denied")

        if invocation is None:
            if not any(
                (
                    authority.bridge_turn_id,
                    authority.acquisition_attempt_id,
                    authority.retrohunt_reservation_id,
                )
            ):
                raise _ConsumeDenied("authority_source_missing")
        else:
            if invocation.investigation_id != authority.investigation_id:
                raise _ConsumeDenied("invocation_investigation_mismatch")
            if invocation.operation_key != authority.operation_key:
                raise _ConsumeDenied("invocation_operation_key_mismatch")
            if invocation.status != "running" and not allow_nonrunning_invocation:
                raise _ConsumeDenied("invocation_not_running")
            if authority.invocation_arguments_sha256 is None:
                raise _ConsumeDenied("invocation_arguments_digest_missing")
            try:
                actual_digest = _sha256(canonical_json_bytes(invocation.normalized_parameters))
            except (TypeError, ValueError):
                raise _ConsumeDenied("invocation_arguments_not_canonical") from None
            if actual_digest != authority.invocation_arguments_sha256:
                raise _ConsumeDenied("invocation_arguments_digest_mismatch")
            binding = family_binding.invocation_tool_binding
            if binding is None:
                raise _ConsumeDenied("invocation_family_binding_missing")
            if (invocation.tool_name, invocation.schema_version) != binding:
                raise _ConsumeDenied("invocation_operation_schema_mismatch")

    def _validate_family_source_refs(
        self,
        family_binding: ExternalEffectFamilyBinding,
        invocation: ToolInvocationRow | None,
        prepared: PreparedExternalRequest,
    ) -> None:
        if not family_binding.requires_source_refs:
            return
        if invocation is None or family_binding.source_reference_resolver is None:
            raise _ConsumeDenied("trusted_source_inputs_required")
        try:
            expected = family_binding.source_reference_resolver(invocation.normalized_parameters)
        except Exception as exc:
            raise _ConsumeDenied("trusted_source_reference_resolution_failed") from exc
        if not expected:
            raise _ConsumeDenied("source_references_required")
        try:
            normalized_expected = tuple(
                EvidenceReference(item.kind, item.id, item.version, item.sha256)
                for item in expected
            )
            normalized_actual = tuple(
                EvidenceReference(item.kind, item.id, item.version, item.sha256)
                for item in prepared.evidence_refs
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise _ConsumeDenied("malformed_source_references") from exc
        for reference in (*normalized_expected, *normalized_actual):
            if reference.kind == "sample_hash":
                if not re.fullmatch(r"[0-9a-f]{64}", reference.id):
                    raise _ConsumeDenied("malformed_source_reference")
            elif reference.kind in {
                "sample",
                "sample_association_sample",
                "sample_association",
                "source_document",
                "document",
            }:
                try:
                    UUID(reference.id)
                except ValueError as exc:
                    raise _ConsumeDenied("malformed_source_reference") from exc
            else:
                raise _ConsumeDenied("unrecognized_source_reference_kind")

        def sort_key(item: EvidenceReference) -> tuple[str, str, str, str]:
            return item.kind, item.id, item.version, item.sha256

        if not normalized_actual or sorted(normalized_actual, key=sort_key) != sorted(
            normalized_expected, key=sort_key
        ):
            raise _ConsumeDenied("source_references_mismatch")

    async def _lock_and_consume_family_attempt(
        self,
        session: AsyncSession,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        investigation: AnalystInvestigationRow,
        family_binding: ExternalEffectFamilyBinding,
    ) -> None:
        is_retrohunt = prepared.family.startswith(_RETROHUNT_PREFIXES)
        is_acquisition = prepared.family.startswith(_ACQUISITION_PREFIXES)
        requires_model_lineage = _requires_model_lineage(prepared.family, family_binding)
        if (
            authority.acquisition_attempt_id is not None
            and authority.retrohunt_reservation_id is not None
        ):
            raise _ConsumeDenied("multiple_family_attempts")
        if authority.acquisition_attempt_id is not None and not is_acquisition:
            raise _ConsumeDenied("acquisition_attempt_on_unrelated_family")
        if authority.retrohunt_reservation_id is not None and not is_retrohunt:
            raise _ConsumeDenied("retrohunt_attempt_on_unrelated_family")
        if (authority.bridge_turn_id is not None or authority.model_run_id is not None) and not (
            requires_model_lineage
        ):
            raise _ConsumeDenied("model_attempt_on_unrelated_family")
        if requires_model_lineage and (
            authority.bridge_turn_id is None or authority.model_run_id is None
        ):
            raise _ConsumeDenied("bridge_turn_run_pair_required")
        if authority.acquisition_attempt_id is not None:
            attempt = await session.scalar(
                select(SampleAcquisitionAttemptRow)
                .where(SampleAcquisitionAttemptRow.id == authority.acquisition_attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if attempt is None or attempt.investigation_id != authority.investigation_id:
                raise _ConsumeDenied("acquisition_attempt_mismatch")
            # Existing ledger rows are terminal outcomes written after a pull;
            # they cannot authorize a new pre-send request.
            raise _ConsumeDenied("acquisition_dispatch_attempt_required")
        if is_acquisition:
            raise _ConsumeDenied("acquisition_dispatch_attempt_required")
        if authority.acquisition_attempt_id is not None:
            raise _ConsumeDenied("acquisition_dispatch_attempt_required")

        if is_retrohunt and authority.retrohunt_reservation_id is None:
            raise _ConsumeDenied("retrohunt_attempt_required")
        if authority.retrohunt_reservation_id is not None:
            attempt = await session.scalar(
                select(RetrohuntReservationRow)
                .where(RetrohuntReservationRow.id == authority.retrohunt_reservation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                attempt is None
                or attempt.investigation_id != authority.investigation_id
                or attempt.state != "reserved"
                or attempt.subject_id != investigation.subject_id
                or (
                    authority.pinned_subject_id is not None
                    and attempt.subject_id != authority.pinned_subject_id
                )
                or not _retrohunt_request_matches(attempt, prepared, authority)
            ):
                raise _ConsumeDenied("retrohunt_attempt_not_reserved")
            now = datetime.now(UTC)
            attempt.state = "submitting"
            attempt.version += 1
            attempt.updated_at = now

    async def _check_source_restrictions(
        self,
        session: AsyncSession,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        subject_id: UUID,
        family_binding: ExternalEffectFamilyBinding,
    ) -> None:
        sample_ids: set[UUID] = set()
        referenced_sample_ids: set[UUID] = set()
        association_ids: set[UUID] = set()
        document_ids: set[UUID] = set()
        sample_hashes: set[str] = set()
        recognized_kinds = {
            "sample",
            "sample_association_sample",
            "sample_association",
            "sample_hash",
            "source_document",
            "document",
        }
        for reference in prepared.evidence_refs:
            if reference.kind not in recognized_kinds:
                continue
            if reference.kind == "sample_hash":
                if not re.fullmatch(r"[0-9a-f]{64}", reference.id):
                    raise _ConsumeDenied("malformed_sample_hash_reference")
                sample_hashes.add(reference.id)
                continue
            try:
                ref_id = UUID(reference.id)
            except ValueError as exc:
                raise _ConsumeDenied("malformed_source_reference") from exc
            if reference.kind in {"sample", "sample_association_sample"}:
                sample_ids.add(ref_id)
                referenced_sample_ids.add(ref_id)
            elif reference.kind == "sample_association":
                association_ids.add(ref_id)
            elif reference.kind in {"source_document", "document"}:
                document_ids.add(ref_id)

        associations = list(
            await session.scalars(
                select(SampleAssociationRow)
                .where(
                    or_(
                        SampleAssociationRow.id.in_(sorted(association_ids, key=str))
                        if association_ids
                        else SampleAssociationRow.id.is_(None),
                        (
                            (SampleAssociationRow.investigation_id == authority.investigation_id)
                            & SampleAssociationRow.sample_id.in_(sorted(sample_ids, key=str))
                        )
                        if sample_ids
                        else SampleAssociationRow.sample_id.is_(None),
                        SampleAssociationRow.sample_sha256.in_(sorted(sample_hashes))
                        if sample_hashes
                        else SampleAssociationRow.sample_sha256.is_(None),
                    )
                )
                .order_by(SampleAssociationRow.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for association in associations:
            if association.investigation_id != authority.investigation_id:
                raise _ConsumeDenied("sample_association_investigation_mismatch")
            if association.sample_id is not None:
                sample_ids.add(association.sample_id)
        if (
            association_ids
            and {item.id for item in associations}.intersection(association_ids) != association_ids
        ):
            raise _ConsumeDenied("sample_association_missing")
        found_hashes = {item.sample_sha256 for item in associations}
        if sample_hashes and not sample_hashes.issubset(found_hashes):
            raise _ConsumeDenied("sample_hash_not_associated_with_investigation")
        if referenced_sample_ids and not referenced_sample_ids.issubset(
            {item.sample_id for item in associations if item.sample_id is not None}
        ):
            raise _ConsumeDenied("sample_not_associated_with_investigation")

        samples = list(
            await session.scalars(
                select(SampleRow)
                .where(
                    SampleRow.id.in_(sorted(sample_ids, key=str))
                    if sample_ids
                    else SampleRow.id.is_(None)
                )
                .order_by(SampleRow.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for sample in samples:
            if sample.subject_id != subject_id or sample.tlp == "red" or sample.do_not_submit:
                raise _ConsumeDenied("sample_source_restricted")
            if _is_model_submission(prepared, family_binding) and not sample.external_llm_allowed:
                raise _ConsumeDenied("sample_external_llm_not_allowed")
        if sample_ids and {sample.id for sample in samples} != sample_ids:
            raise _ConsumeDenied("sample_source_missing")

        documents = list(
            await session.scalars(
                select(SourceDocumentRow)
                .where(
                    SourceDocumentRow.id.in_(sorted(document_ids, key=str))
                    if document_ids
                    else SourceDocumentRow.id.is_(None)
                )
                .order_by(SourceDocumentRow.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for document in documents:
            if (
                document.subject_id != subject_id
                or document.tlp == "red"
                or document.do_not_submit
                or bool(document.license_restriction)
            ):
                raise _ConsumeDenied("source_document_restricted")
            if _is_model_submission(prepared, family_binding) and not document.external_llm_allowed:
                raise _ConsumeDenied("source_document_external_llm_not_allowed")
        if document_ids and {document.id for document in documents} != document_ids:
            raise _ConsumeDenied("source_document_missing")

    async def _lock_and_start_model_attempt(
        self,
        session: AsyncSession,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        investigation_subject_id: UUID,
        family_binding: ExternalEffectFamilyBinding,
    ) -> None:
        requires_model_lineage = _requires_model_lineage(prepared.family, family_binding)
        if not requires_model_lineage:
            if authority.bridge_turn_id is not None or authority.model_run_id is not None:
                raise _ConsumeDenied("model_attempt_on_unrelated_family")
            return
        if authority.bridge_turn_id is None and authority.model_run_id is None:
            raise _ConsumeDenied("bridge_turn_run_pair_required")
        if authority.bridge_turn_id is None or authority.model_run_id is None:
            raise _ConsumeDenied("bridge_turn_run_pair_required")
        turn = await session.scalar(
            select(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.id == authority.bridge_turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if turn is None or turn.model_run_id != authority.model_run_id or turn.status != "running":
            raise _ConsumeDenied("bridge_turn_not_eligible")
        conversation = await session.scalar(
            select(ModelConversationRow)
            .where(ModelConversationRow.id == turn.conversation_id)
            .execution_options(populate_existing=True)
        )
        if (
            conversation is None
            or conversation.subject_id != investigation_subject_id
            or (
                authority.pinned_subject_id is not None
                and conversation.subject_id != authority.pinned_subject_id
            )
            or conversation.provider != (family_binding.model_run_provider or prepared.provider)
        ):
            raise _ConsumeDenied("model_conversation_binding_mismatch")
        run = await session.scalar(
            select(ModelRunRow)
            .where(ModelRunRow.id == authority.model_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        expected_provider = family_binding.model_run_provider or prepared.provider
        model_input_sha256 = prepared.canonical_request.get("model_input_sha256")
        authorized_input_sha256 = prepared.canonical_request.get("authorized_input_hash")
        if (
            run is None
            or run.status != "running"
            or run.submission_state != "not_submitted"
            or run.provider != expected_provider
            or not isinstance(model_input_sha256, str)
            or not isinstance(authorized_input_sha256, str)
            or turn.input_sha256 != model_input_sha256
            or run.authorized_input_hash != authorized_input_sha256
        ):
            raise _ConsumeDenied("model_run_not_eligible")
        run.submission_state = "submission_in_progress"
        run.submission_attempt += 1
        run.updated_at = datetime.now(UTC)

    async def _claim_for_transition(
        self, session: AsyncSession, claim_id: UUID
    ) -> ExternalEffectClaimRow | None:
        return cast(
            ExternalEffectClaimRow | None,
            await session.scalar(
                select(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.id == claim_id)
                .execution_options(populate_existing=True)
            ),
        )

    async def _lock_transition_parents(
        self, session: AsyncSession, row: ExternalEffectClaimRow
    ) -> None:
        await session.scalar(
            select(InvestigationEnvelopeRow)
            .where(InvestigationEnvelopeRow.investigation_id == row.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        await session.scalar(
            select(InvestigationWorkingStateRow)
            .where(InvestigationWorkingStateRow.investigation_id == row.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.invocation_id is not None:
            await session.scalar(
                select(ToolInvocationRow)
                .where(ToolInvocationRow.id == row.invocation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        await session.scalar(
            select(InvestigationBudgetReservationRow)
            .where(
                InvestigationBudgetReservationRow.investigation_id == row.investigation_id,
                InvestigationBudgetReservationRow.operation_key == row.operation_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        await session.scalars(
            select(InvestigationBudgetReservationAmountRow)
            .where(
                InvestigationBudgetReservationAmountRow.investigation_id == row.investigation_id,
                InvestigationBudgetReservationAmountRow.operation_key == row.operation_key,
            )
            .order_by(InvestigationBudgetReservationAmountRow.dimension)
            .with_for_update()
        )
        # Claim is the next row in the universal order.
        await session.scalar(
            select(ExternalEffectClaimRow)
            .where(ExternalEffectClaimRow.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.retrohunt_reservation_id is not None:
            await session.scalar(
                select(RetrohuntReservationRow)
                .where(RetrohuntReservationRow.id == row.retrohunt_reservation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        elif row.acquisition_attempt_id is not None:
            await session.scalar(
                select(SampleAcquisitionAttemptRow)
                .where(SampleAcquisitionAttemptRow.id == row.acquisition_attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row.bridge_turn_id is not None:
            await session.scalar(
                select(ModelConversationTurnRow)
                .where(ModelConversationTurnRow.id == row.bridge_turn_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row.model_run_id is not None:
            await session.scalar(
                select(ModelRunRow)
                .where(ModelRunRow.id == row.model_run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )


def _claim_to_domain(row: ExternalEffectClaimRow) -> ExternalEffectClaim:
    refs: list[EvidenceReference] = []
    result_ref: str | None = None
    for item in row.evidence_refs or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "result_ref":
            candidate = item.get("reference")
            if isinstance(candidate, str):
                result_ref = candidate
            continue
        try:
            refs.append(
                EvidenceReference(
                    kind=item["kind"], id=item["id"], version=item["version"], sha256=item["sha256"]
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ExternalEffectClaim(
        id=row.id,
        investigation_id=row.investigation_id,
        invocation_id=row.invocation_id,
        effect_key=row.effect_key,
        family=row.family,
        provider=row.provider,
        provider_scope=row.provider_scope,
        canonical_request_sha256=row.canonical_request_sha256,
        application_request_sha256=row.application_request_sha256,
        envelope_version=row.envelope_version,
        approval_decision_id=row.approval_decision_id,
        state=EffectClaimState(row.state),
        evidence_refs=tuple(refs),
        result_ref=result_ref,
        bridge_turn_id=row.bridge_turn_id,
        model_run_id=row.model_run_id,
        outcome_code=(
            row.last_error_code
            if row.state == EffectClaimState.COMPLETED.value
            and row.last_error_code in {"bridge_rate_limited", "bridge_rejected"}
            else None
        ),
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    if isinstance(value, list):
        return [_thaw_json(child) for child in value]
    return value


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _safe_code(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        return "external_effect_error"
    if not all(char.isalnum() or char in "_-" for char in value):
        return "external_effect_error"
    return value


__all__ = ["SqlAlchemyExternalEffectClaimStore"]
