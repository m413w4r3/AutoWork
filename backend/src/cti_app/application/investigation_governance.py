"""G2 envelope decisions and append-only sample qualification decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid5

from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings, get_settings
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
    QualificationActorKind,
    SampleAssociation,
    SampleQualification,
    TLPLevel,
)
from cti_app.domain.osint_open import OsintCapabilities
from cti_app.domain.production import AnalystInvestigation

_DECISION_NAMESPACE = UUID("4a4a20ac-2826-4c12-af6c-a72681034bc5")
_QUALIFICATION_STATES = frozenset(SampleQualification)
_G3_STATES = frozenset({SampleQualification.CONFIRMED, SampleQualification.REJECTED})


class InvestigationGovernanceError(RuntimeError):
    """Base for typed G2/G3 refusals."""


class EnvelopeNotFoundError(InvestigationGovernanceError):
    pass


class EnvelopeVersionConflictError(InvestigationGovernanceError):
    pass


class EnvelopeDecisionConflictError(InvestigationGovernanceError):
    pass


class EnvelopeRevokedError(InvestigationGovernanceError):
    """A revoked envelope cannot be reapproved from its retained audit data."""


class SampleAssociationNotFoundError(InvestigationGovernanceError):
    pass


class SampleQualificationPermissionError(InvestigationGovernanceError):
    pass


class SampleQualificationEvidenceError(InvestigationGovernanceError):
    pass


@dataclass(frozen=True, slots=True)
class InvestigationEnvelopeProposal:
    """Unapproved G2 proposal; it is deliberately not persisted as an envelope."""

    investigation_id: UUID
    allowed_providers: tuple[str, ...]
    budget: InvestigationBudget
    acquire_hits_allowed: bool
    disclosure: DisclosureBounds

    @property
    def approved(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class G2AuthorizationBounds:
    allowed_providers: tuple[str, ...]
    budget_limits: Mapping[BudgetDimension, int]
    acquire_hits_allowed: bool
    disclosure: DisclosureBounds


@dataclass(frozen=True, slots=True)
class G2EnvelopeAuthorizationState:
    revoked: bool
    authorization_ceiling: G2AuthorizationBounds | None


def g2_envelope_authorization_state(
    envelope: InvestigationEnvelope | None,
    decisions: Sequence[AnalystDecision],
) -> G2EnvelopeAuthorizationState:
    if envelope is None:
        return G2EnvelopeAuthorizationState(revoked=False, authorization_ceiling=None)
    if envelope.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
        # The approval UUID is a policy event UUID, never an AnalystDecision UUID.
        return G2EnvelopeAuthorizationState(
            revoked=False,
            authorization_ceiling=_authorization_bounds_for_envelope(envelope),
        )
    revocation = next(
        (
            item
            for item in decisions
            if item.id == envelope.approval_decision_id
            and item.decision_type is AnalystDecisionType.PIVOT_REJECT
            and item.target_type is AnalystDecisionTargetType.PIVOT
            and item.target_id == envelope.investigation_id
        ),
        None,
    )
    if revocation is None:
        return G2EnvelopeAuthorizationState(
            revoked=False,
            authorization_ceiling=_authorization_bounds_for_envelope(envelope),
        )
    ceiling = _authorization_bounds_from_payload(revocation.details.get("authorization_ceiling"))
    return G2EnvelopeAuthorizationState(revoked=True, authorization_ceiling=ceiling)


@dataclass(frozen=True, slots=True)
class InvestigationActor:
    actor_id: str
    kind: QualificationActorKind

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("An identified actor is required")
        if not isinstance(self.kind, QualificationActorKind):
            raise TypeError("Actor kind must use QualificationActorKind")


class InvestigationEnvelopeService:
    """Persist approved G2 envelopes with decision audit and repository CAS."""

    def __init__(self, uow_factory: UnitOfWorkFactory, *, settings: Settings | None = None) -> None:
        self._uow_factory = uow_factory
        self._settings = settings or get_settings()

    def propose_default(
        self,
        investigation_id: UUID,
        *,
        budget: InvestigationBudget | None = None,
    ) -> InvestigationEnvelopeProposal:
        """Return a local-only proposal without manufacturing an approval decision."""
        return InvestigationEnvelopeProposal(
            investigation_id=investigation_id,
            allowed_providers=(),
            budget=_with_global_budget_defaults(
                budget
                if budget is not None
                else InvestigationBudget(
                    limits={
                        BudgetDimension.VT_UNITS: 0,
                        BudgetDimension.DOWNLOADS: 0,
                    }
                ),
                self._settings,
            ),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=None,
                do_not_submit=True,
                external_llm_allowed=False,
            ),
        )

    async def create_policy_inherited_envelope(
        self,
        uow: Any,
        *,
        investigation_id: UUID,
        policy_id: UUID,
        policy_version: int,
        policy_event_id: UUID,
        actor_id: str,
        capabilities: OsintCapabilities,
        disclosure: DisclosureBounds,
    ) -> InvestigationEnvelope:
        """Persist policy authority without creating a human G2 decision."""
        if not actor_id.strip():
            raise ValueError("Policy inheritance requires the identified activating actor")
        current = await uow.investigation_envelopes.get(investigation_id)
        if current is not None:
            return cast(InvestigationEnvelope, current)
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise EnvelopeNotFoundError("Investigation does not exist")
        budget = InvestigationBudget(limits=capabilities.budget_limits)
        envelope = InvestigationEnvelope(
            investigation_id=investigation_id,
            allowed_providers=capabilities.allowed_providers,
            budget=budget,
            acquire_hits_allowed=capabilities.acquire_hits_allowed,
            disclosure=disclosure,
            approval_decision_id=policy_event_id,
            approval_actor_id=actor_id.strip(),
            authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
            inherited_policy_id=policy_id,
            inherited_policy_version=policy_version,
            inherited_policy_event_id=policy_event_id,
            approved_at=datetime.now(UTC),
            version=1,
        )
        try:
            await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
        except VersionConflictError as exc:
            existing = await uow.investigation_envelopes.get(investigation_id)
            if existing is not None:
                return cast(InvestigationEnvelope, existing)
            raise EnvelopeVersionConflictError(str(exc)) from exc
        if _synchronize_loop_budget_downloads(
            investigation, capabilities.budget_limits.get(BudgetDimension.DOWNLOADS, 0)
        ):
            await uow.analyst_investigations.save(investigation)
        return envelope

    async def approve(
        self,
        proposal: InvestigationEnvelopeProposal,
        *,
        actor_id: str,
        reason: str,
        correlation_id: str,
        expected_version: int | None = None,
    ) -> InvestigationEnvelope:
        if not actor_id.strip() or not reason.strip() or not correlation_id.strip():
            raise ValueError("G2 approval requires actor, reason and correlation")
        budget = _with_global_budget_defaults(proposal.budget, self._settings)
        _require_acquisition_budget(proposal.acquire_hits_allowed, budget)
        payload = {
            "action": "approve",
            "investigation_id": str(proposal.investigation_id),
            "allowed_providers": _normalize_providers(proposal.allowed_providers),
            "budget": _budget_identity(budget),
            "acquire_hits_allowed": proposal.acquire_hits_allowed,
            "disclosure": _disclosure_identity(proposal.disclosure),
            "actor_id": actor_id.strip(),
            "reason": reason.strip(),
        }
        decision = _decision(
            payload=payload,
            investigation_id=proposal.investigation_id,
            decision_type=AnalystDecisionType.PIVOT_APPROVE,
            target_type=AnalystDecisionTargetType.PIVOT,
            target_id=proposal.investigation_id,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )
        async with self._uow_factory() as uow:
            replay = await _decision_by_correlation(
                uow, proposal.investigation_id, correlation_id, decision
            )
            current = await uow.investigation_envelopes.get(proposal.investigation_id)
            if replay:
                if current is None:
                    raise EnvelopeDecisionConflictError(
                        "G2 approval decision exists without its approved envelope"
                    )
                return current
            if expected_version is not None:
                raise EnvelopeVersionConflictError("A first G2 envelope has no prior version")
            if current is not None:
                raise EnvelopeVersionConflictError(
                    "An approved envelope already exists; use extend"
                )
            investigation = await uow.analyst_investigations.get(proposal.investigation_id)
            if investigation is None:
                raise EnvelopeNotFoundError("Investigation does not exist")
            envelope = InvestigationEnvelope(
                investigation_id=proposal.investigation_id,
                allowed_providers=_normalize_providers(proposal.allowed_providers),
                budget=_copy_budget(budget),
                acquire_hits_allowed=proposal.acquire_hits_allowed,
                disclosure=proposal.disclosure,
                approval_decision_id=decision.id,
                approval_actor_id=actor_id.strip(),
                authority_kind=EnvelopeAuthorityKind.HUMAN_G2,
                approved_at=datetime.now(UTC),
                version=1,
            )
            await uow.analyst_decisions.append(decision)
            try:
                await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
            except VersionConflictError as exc:
                raise EnvelopeVersionConflictError(str(exc)) from exc
            if _synchronize_loop_budget_downloads(
                investigation, budget.limits.get(BudgetDimension.DOWNLOADS, 0)
            ):
                await uow.analyst_investigations.save(investigation)
            await uow.commit()
            return envelope

    async def extend(
        self,
        investigation_id: UUID,
        *,
        expected_version: int,
        allowed_providers: tuple[str, ...],
        budget: InvestigationBudget,
        acquire_hits_allowed: bool,
        disclosure: DisclosureBounds,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> InvestigationEnvelope:
        if not actor_id.strip() or not reason.strip() or not correlation_id.strip():
            raise ValueError("G2 extension requires actor, reason and correlation")
        normalized_providers = _normalize_providers(allowed_providers)
        async with self._uow_factory() as uow:
            current = await uow.investigation_envelopes.get(investigation_id)
            if current is None:
                raise EnvelopeNotFoundError("No approved G2 envelope exists")
            decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation_id))
            authorization = g2_envelope_authorization_state(current, decisions)
            limits = dict(current.budget.limits)
            limits.update(budget.limits)
            requested_budget = InvestigationBudget(limits=limits)
            _require_acquisition_budget(acquire_hits_allowed, requested_budget)
            payload = {
                "action": "extend",
                "investigation_id": str(investigation_id),
                "expected_version": expected_version,
                "allowed_providers": normalized_providers,
                "budget": _budget_identity(requested_budget),
                "acquire_hits_allowed": acquire_hits_allowed,
                "disclosure": _disclosure_identity(disclosure),
                "actor_id": actor_id.strip(),
                "reason": reason.strip(),
            }
            decision = _decision(
                payload=payload,
                investigation_id=investigation_id,
                decision_type=AnalystDecisionType.PIVOT_APPROVE,
                target_type=AnalystDecisionTargetType.PIVOT,
                target_id=investigation_id,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            replay = await _decision_by_correlation(uow, investigation_id, correlation_id, decision)
            if replay:
                return current
            if current.version != expected_version:
                raise EnvelopeVersionConflictError("Investigation envelope version changed")
            if authorization.revoked:
                ceiling = authorization.authorization_ceiling
                if ceiling is None:
                    raise EnvelopeRevokedError(
                        "The revoked envelope has no retained authorization bounds; "
                        "create a new investigation."
                    )
                _require_revoked_reextension(
                    current_budget=current.budget,
                    providers=normalized_providers,
                    requested_budget=requested_budget,
                    acquire_hits_allowed=acquire_hits_allowed,
                    disclosure=disclosure,
                    ceiling=ceiling,
                )
            else:
                if not set(current.allowed_providers).issubset(normalized_providers):
                    raise ValueError("An envelope extension cannot remove an allowed provider")
                if current.acquire_hits_allowed and not acquire_hits_allowed:
                    raise ValueError("An envelope extension cannot revoke acquisition rights")
                _require_budget_extension(current.budget, requested_budget)
                _require_disclosure_within_bounds(disclosure, current.disclosure)
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise EnvelopeNotFoundError("Investigation does not exist")
            extended = InvestigationEnvelope(
                investigation_id=investigation_id,
                allowed_providers=normalized_providers,
                budget=_copy_budget(
                    current.budget,
                    limits=requested_budget.limits,
                ),
                acquire_hits_allowed=acquire_hits_allowed,
                disclosure=disclosure,
                approval_decision_id=decision.id,
                approval_actor_id=actor_id.strip(),
                authority_kind=EnvelopeAuthorityKind.HUMAN_G2,
                approved_at=datetime.now(UTC),
                version=current.version + 1,
            )
            await uow.analyst_decisions.append(decision)
            if current.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
                policy_repository = getattr(uow, "osint_open_policies", None)
                if policy_repository is not None:
                    from cti_app.domain.osint_open import OsintInheritanceStatus

                    await policy_repository.set_inheritance_status_by_investigation(
                        investigation_id, OsintInheritanceStatus.SUPERSEDED
                    )
            try:
                await uow.investigation_envelopes.save_if_version(
                    extended, expected_version=expected_version
                )
            except VersionConflictError as exc:
                raise EnvelopeVersionConflictError(str(exc)) from exc
            if _synchronize_loop_budget_downloads(
                investigation, requested_budget.limits.get(BudgetDimension.DOWNLOADS, 0)
            ):
                await uow.analyst_investigations.save(investigation)
            await uow.commit()
            return extended

    async def reject(
        self,
        investigation_id: UUID,
        *,
        actor_id: str,
        reason: str,
        correlation_id: str,
        expected_version: int | None = None,
    ) -> AnalystDecision:
        if not actor_id.strip() or not reason.strip() or not correlation_id.strip():
            raise ValueError("G2 rejection requires actor, reason and correlation")
        payload = {
            "action": "reject",
            "investigation_id": str(investigation_id),
            "expected_version": expected_version,
            "actor_id": actor_id.strip(),
            "reason": reason.strip(),
        }
        decision = _decision(
            payload=payload,
            investigation_id=investigation_id,
            decision_type=AnalystDecisionType.PIVOT_REJECT,
            target_type=AnalystDecisionTargetType.PIVOT,
            target_id=investigation_id,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )
        async with self._uow_factory() as uow:
            replay = await _decision_by_correlation(uow, investigation_id, correlation_id, decision)
            if replay is not None:
                return replay
            current = await uow.investigation_envelopes.get(investigation_id)
            current_version = current.version if current is not None else None
            if current_version != expected_version:
                raise EnvelopeVersionConflictError("Investigation envelope version changed")
            if current is not None:
                raise EnvelopeVersionConflictError(
                    "The domain has no revocation state for an approved envelope; a separate "
                    "G2 revocation model is required."
                )
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise EnvelopeNotFoundError("Investigation does not exist")
            await uow.analyst_decisions.append(decision)
            await uow.commit()
            return decision

    async def revoke(
        self,
        investigation_id: UUID,
        *,
        expected_version: int,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> InvestigationEnvelope:
        """Append an audited G2 rejection and replace rights with a zero-external envelope."""
        if not actor_id.strip() or not reason.strip() or not correlation_id.strip():
            raise ValueError("G2 revocation requires actor, reason and correlation")
        payload = {
            "action": "revoke",
            "investigation_id": str(investigation_id),
            "expected_version": expected_version,
            "actor_id": actor_id.strip(),
            "reason": reason.strip(),
        }
        decision = _decision(
            payload=payload,
            investigation_id=investigation_id,
            decision_type=AnalystDecisionType.PIVOT_REJECT,
            target_type=AnalystDecisionTargetType.PIVOT,
            target_id=investigation_id,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )
        async with self._uow_factory() as uow:
            replay = await _decision_by_correlation(uow, investigation_id, correlation_id, decision)
            current = await uow.investigation_envelopes.get(investigation_id)
            if replay is not None:
                if current is None:
                    raise EnvelopeDecisionConflictError(
                        "G2 revocation decision exists without its envelope"
                    )
                return current
            if current is None:
                raise EnvelopeNotFoundError("No approved G2 envelope exists")
            if current.version != expected_version:
                raise EnvelopeVersionConflictError("Investigation envelope version changed")
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise EnvelopeNotFoundError("Investigation does not exist")
            if _synchronize_loop_budget_downloads(
                investigation, current.budget.limits.get(BudgetDimension.DOWNLOADS, 0)
            ):
                await uow.analyst_investigations.save(investigation)

            external_dimensions = {
                BudgetDimension.VT_UNITS,
                BudgetDimension.SHODAN_CREDITS,
                BudgetDimension.DOWNLOADS,
            }
            limits = dict(current.budget.limits)
            limits.update({dimension: 0 for dimension in external_dimensions})
            budget = InvestigationBudget(limits=limits)
            for reservation in current.budget.reservations:
                budget.restore_reservation(reservation)

            revoked = InvestigationEnvelope(
                investigation_id=investigation_id,
                allowed_providers=(),
                budget=budget,
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(
                    tlp=current.disclosure.tlp,
                    do_not_submit=current.disclosure.do_not_submit,
                    external_llm_allowed=False,
                ),
                approval_decision_id=decision.id,
                approval_actor_id=actor_id.strip(),
                authority_kind=EnvelopeAuthorityKind.HUMAN_G2,
                approved_at=datetime.now(UTC),
                version=current.version + 1,
            )
            decision = replace(
                decision,
                details={"authorization_ceiling": _authorization_bounds_payload(current)},
            )
            await uow.analyst_decisions.append(decision)
            if current.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
                policy_repository = getattr(uow, "osint_open_policies", None)
                if policy_repository is not None:
                    from cti_app.domain.osint_open import OsintInheritanceStatus

                    await policy_repository.set_inheritance_status_by_investigation(
                        investigation_id, OsintInheritanceStatus.SUPERSEDED
                    )
            try:
                await uow.investigation_envelopes.save_if_version(
                    revoked, expected_version=expected_version
                )
            except VersionConflictError as exc:
                raise EnvelopeVersionConflictError(str(exc)) from exc
            await uow.commit()
            return revoked

    async def current(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        async with self._uow_factory() as uow:
            return await uow.investigation_envelopes.get(investigation_id)

    async def authorization_state(self, investigation_id: UUID) -> G2EnvelopeAuthorizationState:
        async with self._uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(investigation_id)
            decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation_id))
            return g2_envelope_authorization_state(envelope, decisions)


class SampleQualificationService:
    """Append qualification history and require human G3 for trusted membership."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def qualify(
        self,
        association_id: UUID,
        new_state: SampleQualification,
        actor: InvestigationActor,
        reason: str,
        evidence_refs: tuple[UUID, ...],
        *,
        correlation_id: str | None = None,
        expected_version: int | None = None,
    ) -> SampleAssociation:
        if not isinstance(new_state, SampleQualification) or new_state not in _QUALIFICATION_STATES:
            raise TypeError("Qualification state must use SampleQualification")
        if not isinstance(actor, InvestigationActor):
            raise TypeError("Qualification actor must be an InvestigationActor")
        if not reason.strip():
            raise ValueError("Qualification requires a reason")
        if new_state in _G3_STATES and actor.kind is not QualificationActorKind.HUMAN:
            raise SampleQualificationPermissionError(
                "Only an identified human may validate or reject cluster membership"
            )
        refs = tuple(dict.fromkeys(evidence_refs))
        async with self._uow_factory() as uow:
            association = await uow.sample_associations.get(association_id)
            if association is None:
                raise SampleAssociationNotFoundError(str(association_id))
            if (
                expected_version is not None
                and association.version != expected_version
                and association.qualification is not new_state
            ):
                raise VersionConflictError("Sample association version changed")
            for reference in refs:
                observation = await uow.evidence_observations.get(reference)
                if (
                    observation is None
                    or observation.investigation_id != association.investigation_id
                ):
                    raise SampleQualificationEvidenceError(
                        "Qualification evidence must exist in the same investigation"
                    )
            if association.qualification is new_state:
                return association

            recorded_reason = reason.strip()
            if refs:
                recorded_reason += " Evidence refs: " + ", ".join(str(ref) for ref in refs)
            effective_correlation = correlation_id or _qualification_correlation(
                association=association,
                new_state=new_state,
                actor=actor,
                reason=recorded_reason,
                evidence_refs=refs,
            )
            updated = association.change_qualification(
                new_state,
                actor_kind=actor.kind,
                actor_id=actor.actor_id,
                reason=recorded_reason,
            )
            if new_state in _G3_STATES:
                decision = _decision(
                    payload={
                        "action": "qualify",
                        "association_id": str(association.id),
                        "from": association.qualification.value,
                        "to": new_state.value,
                        "actor_id": actor.actor_id,
                        "reason": recorded_reason,
                        "evidence_refs": [str(ref) for ref in refs],
                    },
                    investigation_id=association.investigation_id,
                    decision_type=(
                        AnalystDecisionType.MEMBER_VALIDATE
                        if new_state is SampleQualification.CONFIRMED
                        else AnalystDecisionType.MEMBER_REJECT
                    ),
                    target_type=AnalystDecisionTargetType.MEMBER,
                    target_id=association.id,
                    actor_id=actor.actor_id,
                    reason=recorded_reason,
                    correlation_id=effective_correlation,
                )
                if await _decision_by_correlation(
                    uow, association.investigation_id, effective_correlation, decision
                ):
                    latest = await uow.sample_associations.get(association_id)
                    if latest is None:
                        raise SampleAssociationNotFoundError(str(association_id))
                    return latest
                await uow.analyst_decisions.append(decision)
            try:
                updated = await uow.sample_associations.append_version(
                    updated, expected_version=association.version
                )
            except VersionConflictError:
                raise
            await uow.commit()
            return updated


def _decision(
    *,
    payload: Mapping[str, object],
    investigation_id: UUID,
    decision_type: AnalystDecisionType,
    target_type: AnalystDecisionTargetType,
    target_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: str,
) -> AnalystDecision:
    identity = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    decision_id = uuid5(_DECISION_NAMESPACE, f"{correlation_id}:{identity}")
    return AnalystDecision(
        id=decision_id,
        investigation_id=investigation_id,
        decision_type=decision_type,
        target_type=target_type,
        target_id=target_id,
        actor_id=actor_id.strip(),
        reason=reason.strip(),
        correlation_id=correlation_id.strip(),
    )


def _authorization_bounds_for_envelope(
    envelope: InvestigationEnvelope,
) -> G2AuthorizationBounds:
    return G2AuthorizationBounds(
        allowed_providers=tuple(envelope.allowed_providers),
        budget_limits=dict(envelope.budget.limits),
        acquire_hits_allowed=envelope.acquire_hits_allowed,
        disclosure=envelope.disclosure,
    )


def _authorization_bounds_payload(envelope: InvestigationEnvelope) -> dict[str, object]:
    return {
        "allowed_providers": list(envelope.allowed_providers),
        "budget_limits": {
            dimension.value: amount for dimension, amount in envelope.budget.limits.items()
        },
        "acquire_hits_allowed": envelope.acquire_hits_allowed,
        "disclosure": _disclosure_identity(envelope.disclosure),
    }


def _authorization_bounds_from_payload(value: object) -> G2AuthorizationBounds | None:
    if not isinstance(value, Mapping):
        return None
    providers = value.get("allowed_providers")
    limits = value.get("budget_limits")
    acquisition = value.get("acquire_hits_allowed")
    disclosure_payload = value.get("disclosure")
    if (
        not isinstance(providers, (list, tuple))
        or not all(isinstance(item, str) for item in providers)
        or not isinstance(limits, Mapping)
        or not isinstance(acquisition, bool)
        or not isinstance(disclosure_payload, Mapping)
    ):
        return None
    try:
        normalized_providers = _normalize_providers(tuple(providers))
        budget_limits: dict[BudgetDimension, int] = {}
        for raw_dimension, amount in limits.items():
            if (
                not isinstance(raw_dimension, str)
                or isinstance(amount, bool)
                or not isinstance(amount, int)
            ):
                return None
            budget_limits[BudgetDimension(raw_dimension)] = amount
        raw_tlp = disclosure_payload.get("tlp")
        tlp = TLPLevel(raw_tlp) if isinstance(raw_tlp, str) else None
        do_not_submit = disclosure_payload.get("do_not_submit")
        external_llm_allowed = disclosure_payload.get("external_llm_allowed")
        if not isinstance(do_not_submit, bool) or not isinstance(external_llm_allowed, bool):
            return None
        budget = InvestigationBudget(limits=budget_limits)
    except (TypeError, ValueError):
        return None
    return G2AuthorizationBounds(
        allowed_providers=normalized_providers,
        budget_limits=dict(budget.limits),
        acquire_hits_allowed=acquisition,
        disclosure=DisclosureBounds(
            tlp=tlp,
            do_not_submit=do_not_submit,
            external_llm_allowed=external_llm_allowed,
        ),
    )


def _require_revoked_reextension(
    *,
    current_budget: InvestigationBudget,
    providers: tuple[str, ...],
    requested_budget: InvestigationBudget,
    acquire_hits_allowed: bool,
    disclosure: DisclosureBounds,
    ceiling: G2AuthorizationBounds,
) -> None:
    if not set(providers).issubset(ceiling.allowed_providers):
        raise ValueError("A re-extension cannot exceed the prior G2 provider authorization")
    if acquire_hits_allowed and not ceiling.acquire_hits_allowed:
        raise ValueError("A re-extension cannot restore unapproved acquisition rights")
    _require_disclosure_within_bounds(disclosure, ceiling.disclosure)
    for dimension in BudgetDimension:
        limit = requested_budget.limits.get(dimension)
        ceiling_limit = ceiling.budget_limits.get(dimension)
        if ceiling_limit is not None and (limit is None or limit > ceiling_limit):
            raise ValueError(f"A re-extension cannot exceed the prior G2 {dimension.value} budget")
        used = current_budget.consumed[dimension] + current_budget.reserved[dimension]
        if limit is not None and limit < used:
            raise ValueError(
                f"A re-extension cannot reduce {dimension.value} below its consumed "
                "or reserved usage"
            )


def _require_disclosure_within_bounds(
    disclosure: DisclosureBounds,
    ceiling: DisclosureBounds,
) -> None:
    if ceiling.do_not_submit and not disclosure.do_not_submit:
        raise ValueError("An envelope extension cannot remove do_not_submit")
    if not ceiling.external_llm_allowed and disclosure.external_llm_allowed:
        raise ValueError("An envelope extension cannot enable external LLM access")
    if _tlp_rank(disclosure.tlp) < _tlp_rank(ceiling.tlp):
        raise ValueError("An envelope extension cannot widen the approved TLP")
    if (disclosure.do_not_submit or disclosure.tlp is TLPLevel.RED) and (
        disclosure.external_llm_allowed
    ):
        raise ValueError("Disclosure policy forbids external LLM access")


def _tlp_rank(level: TLPLevel | None) -> int:
    if level is None:
        return 0
    return {
        TLPLevel.WHITE: 1,
        TLPLevel.CLEAR: 1,
        TLPLevel.GREEN: 2,
        TLPLevel.AMBER: 3,
        TLPLevel.RED: 4,
    }[level]


async def _decision_by_correlation(
    uow: Any,
    investigation_id: UUID,
    correlation_id: str,
    expected: AnalystDecision,
) -> AnalystDecision | None:
    decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
    matches = [item for item in decisions if item.correlation_id == correlation_id]
    if not matches:
        return None
    if len(matches) != 1 or not _same_decision(matches[0], expected):
        raise EnvelopeDecisionConflictError(
            "Correlation id is already associated with a different analyst decision"
        )
    return cast(AnalystDecision, matches[0])


def _same_decision(actual: AnalystDecision, expected: AnalystDecision) -> bool:
    """Compare idempotent decision content while ignoring its first-write timestamp."""
    return (
        actual.id == expected.id
        and actual.investigation_id == expected.investigation_id
        and actual.decision_type is expected.decision_type
        and actual.target_type is expected.target_type
        and actual.target_id == expected.target_id
        and actual.actor_id == expected.actor_id
        and actual.reason == expected.reason
        and actual.correlation_id == expected.correlation_id
    )


def _normalize_providers(providers: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_normalize_provider(item) for item in providers)
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("Allowed provider names must be non-empty and unique")
    return normalized


def _with_global_budget_defaults(
    budget: InvestigationBudget, settings: Settings
) -> InvestigationBudget:
    limits = dict(budget.limits)
    limits.setdefault(BudgetDimension.TOOL_SECONDS, settings.investigation_max_tool_seconds)
    limits.setdefault(BudgetDimension.CONTEXT_BYTES, settings.investigation_max_context_bytes)
    limits.setdefault(
        BudgetDimension.CONTEXT_TOKENS,
        max(1, limits[BudgetDimension.CONTEXT_BYTES] // 4),
    )
    return _copy_budget(budget, limits=limits)


def _synchronize_loop_budget_downloads(investigation: AnalystInvestigation, downloads: int) -> bool:
    """Raise legacy acquisition caps with G2 DOWNLOADS without lowering any cap."""
    changed = False
    for name in ("max_hits_acquired", "max_new_samples", "max_vt_read_units"):
        current = getattr(investigation.budget, name)
        if current < downloads:
            setattr(investigation.budget, name, downloads)
            changed = True
    if changed:
        investigation._bump()
    return changed


def _copy_budget(
    budget: InvestigationBudget,
    *,
    limits: Mapping[BudgetDimension, int] | None = None,
) -> InvestigationBudget:
    copied = InvestigationBudget(limits=dict(budget.limits if limits is None else limits))
    for reservation in budget.reservations:
        # This copies recorded usage, not a new admission. Revocation can lower
        # an external limit below an outstanding reservation; preserve the
        # canonical reservation and its status without revalidating capacity.
        copied.restore_reservation(reservation)
    return copied


def _budget_identity(budget: InvestigationBudget) -> dict[str, object]:
    return {
        "limits": {key.value: value for key, value in budget.limits.items()},
        "reservations": [
            {
                "operation_key": item.operation_key,
                "amounts": {key.value: value for key, value in item.amounts.items()},
                "status": item.status.value,
                "committed_amounts": {
                    key.value: value for key, value in item.committed_amounts.items()
                },
            }
            for item in budget.reservations
        ],
    }


def _disclosure_identity(disclosure: DisclosureBounds) -> dict[str, object]:
    return {
        "tlp": disclosure.tlp.value if disclosure.tlp is not None else None,
        "do_not_submit": disclosure.do_not_submit,
        "external_llm_allowed": disclosure.external_llm_allowed,
    }


def _require_budget_extension(current: InvestigationBudget, extended: InvestigationBudget) -> None:
    for dimension in BudgetDimension:
        previous = current.limits.get(dimension)
        proposed = extended.limits.get(dimension)
        if previous is None and proposed is not None:
            raise ValueError(
                f"An envelope extension cannot cap previously unlimited {dimension.value}"
            )
        if previous is not None and proposed is not None and proposed < previous:
            raise ValueError(f"An envelope extension cannot reduce {dimension.value}")


def _require_acquisition_budget(allowed: bool, budget: InvestigationBudget) -> None:
    if not allowed:
        return
    for dimension in (BudgetDimension.DOWNLOADS, BudgetDimension.VT_UNITS):
        if dimension not in budget.limits:
            raise ValueError(
                f"Hit acquisition requires an explicit finite {dimension.value} budget"
            )


def _normalize_provider(provider: str) -> str:
    value = provider.strip().casefold()
    return "vt" if value in {"virustotal", "virus_total"} else value


def _qualification_correlation(
    *,
    association: SampleAssociation,
    new_state: SampleQualification,
    actor: InvestigationActor,
    reason: str,
    evidence_refs: tuple[UUID, ...],
) -> str:
    payload = json.dumps(
        {
            "association_id": str(association.id),
            "version": association.version,
            "state": new_state.value,
            "actor_id": actor.actor_id,
            "reason": reason,
            "evidence_refs": [str(ref) for ref in evidence_refs],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"sample-qualification:{association.id}:{association.version}:{digest}"


__all__ = [
    "EnvelopeDecisionConflictError",
    "EnvelopeNotFoundError",
    "EnvelopeRevokedError",
    "EnvelopeVersionConflictError",
    "G2AuthorizationBounds",
    "G2EnvelopeAuthorizationState",
    "InvestigationActor",
    "InvestigationEnvelopeProposal",
    "InvestigationEnvelopeService",
    "InvestigationGovernanceError",
    "SampleAssociationNotFoundError",
    "SampleQualificationEvidenceError",
    "SampleQualificationPermissionError",
    "SampleQualificationService",
    "g2_envelope_authorization_state",
]
