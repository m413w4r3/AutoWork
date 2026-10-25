"""Application orchestration for durable OSINT_OPEN policy versions and start pins."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from cti_app.application.identity import is_human_actor_id
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    InvestigationBudget,
    TLPLevel,
)
from cti_app.domain.osint_open import (
    OsintCapabilities,
    OsintEffectivePolicy,
    OsintInheritance,
    OsintInheritanceStatus,
    OsintPolicyEvent,
    OsintPolicyEventType,
    OsintPolicyMode,
    OsintPolicyStatus,
    OsintPolicyVersion,
    OsintScopeKind,
    intersect_disclosure,
)

_POLICY_NAMESPACE = UUID("77f9548c-e69e-4f25-9840-4c3f97a9eb5a")
_SECRET_SHAPED = re.compile(
    r"(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]+=*"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|"
    r"authorization|credential|cookie|signature)\s*[:=]\s*\S+)"
    r"|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{16})"
    r"|https?://[^\s?#]+\?[^\s#]*(?:x-amz-[^=]+|signature|sig|token|access_token)=",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_KNOWN_PROVIDERS = frozenset({"vt", "shodan", "bridge"})


class OsintPolicyError(ValueError):
    """A policy request is invalid or cannot be applied safely."""


class OsintPolicyConflictError(OsintPolicyError):
    """A policy version or idempotency key conflicts with durable state."""


class OsintPolicyNotFoundError(OsintPolicyError):
    """A referenced policy or investigation does not exist."""


@dataclass(frozen=True, slots=True)
class OsintOpenPolicyService:
    uow_factory: UnitOfWorkFactory
    settings: Settings

    async def current(
        self, scope_kind: OsintScopeKind, scope_ref: str
    ) -> OsintPolicyVersion | None:
        normalized_ref = _normalize_scope_ref(scope_kind, scope_ref)
        async with self.uow_factory() as uow:
            repository = uow.osint_open_policies
            return cast(
                OsintPolicyVersion | None,
                await repository.latest_for_scope(scope_kind, normalized_ref),
            )

    async def activate(
        self,
        *,
        scope_kind: OsintScopeKind,
        scope_ref: str,
        capabilities: OsintCapabilities,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> OsintPolicyVersion:
        _validate_audit_fields(actor_id, reason, correlation_id)
        _validate_actor(actor_id)
        normalized_ref = _normalize_scope_ref(scope_kind, scope_ref)
        self._validate_capabilities(capabilities)
        async with self.uow_factory() as uow:
            repository = uow.osint_open_policies
            await repository.lock_scope_index()
            await repository.lock_scope(scope_kind, normalized_ref)
            replay = await self._replay(
                repository,
                correlation_id,
                self._payload_hash(
                    action="activate",
                    scope_kind=scope_kind,
                    scope_ref=normalized_ref,
                    capabilities=capabilities,
                    actor_id=actor_id,
                    reason=reason,
                    correlation_id=correlation_id,
                    activation_correlation_id=correlation_id,
                    previous_version=None,
                ),
            )
            if replay is not None:
                return replay
            previous = await repository.latest_for_scope(
                scope_kind, normalized_ref, for_update=True
            )
            if previous is not None and previous.status is OsintPolicyStatus.ACTIVE:
                raise OsintPolicyConflictError("An active policy already exists for this scope")
            if scope_kind is OsintScopeKind.SUBJECT_SET:
                for subject_id in OsintPolicyVersion.scope_subject_ids(normalized_ref):
                    if await uow.subjects.get(subject_id) is None:
                        raise OsintPolicyNotFoundError("A policy subject does not exist")
            version = (previous.version + 1) if previous is not None else 1
            policy = self._new_version(
                scope_kind=scope_kind,
                scope_ref=normalized_ref,
                version=version,
                previous_version=previous.version if previous is not None else None,
                status=OsintPolicyStatus.ACTIVE,
                capabilities=capabilities,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
                activation_correlation_id=correlation_id,
                event_type=OsintPolicyEventType.ACTIVATED,
            )
            event = self._event_for(policy, OsintPolicyEventType.ACTIVATED)
            await repository.add_version(policy, event)
            await uow.commit()
            return policy

    async def extend(
        self,
        *,
        policy_id: UUID,
        expected_version: int,
        capabilities: OsintCapabilities,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> OsintPolicyVersion:
        _validate_audit_fields(actor_id, reason, correlation_id)
        _validate_actor(actor_id)
        self._validate_capabilities(capabilities)
        async with self.uow_factory() as uow:
            repository = uow.osint_open_policies
            await repository.lock_scope_index()
            current = await repository.get_version(policy_id, expected_version, for_update=True)
            if current is None:
                raise OsintPolicyNotFoundError("Policy version does not exist")
            await repository.lock_scope(current.scope_kind, current.scope_ref)
            payload_hash = self._payload_hash(
                action="extend",
                scope_kind=current.scope_kind,
                scope_ref=current.scope_ref,
                capabilities=capabilities,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
                activation_correlation_id=current.activation_correlation_id,
                previous_version=expected_version,
            )
            replay = await self._replay(repository, correlation_id, payload_hash)
            if replay is not None:
                return replay
            latest = await repository.latest_for_scope(
                current.scope_kind, current.scope_ref, for_update=True
            )
            if latest is None or latest.version != expected_version:
                raise OsintPolicyConflictError("Policy version changed")
            if latest.status is not OsintPolicyStatus.ACTIVE:
                raise OsintPolicyConflictError("A revoked policy cannot be extended")
            try:
                capabilities.require_growth_from(latest.capabilities)
            except ValueError as exc:
                raise OsintPolicyError(str(exc)) from exc
            policy = self._new_version(
                scope_kind=current.scope_kind,
                scope_ref=current.scope_ref,
                version=expected_version + 1,
                previous_version=expected_version,
                status=OsintPolicyStatus.ACTIVE,
                capabilities=capabilities,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
                activation_correlation_id=current.activation_correlation_id,
                event_type=OsintPolicyEventType.EXTENDED,
                policy_id=policy_id,
            )
            await repository.add_version(
                policy, self._event_for(policy, OsintPolicyEventType.EXTENDED)
            )
            await uow.commit()
            return policy

    async def revoke(
        self,
        *,
        policy_id: UUID,
        expected_version: int,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> OsintPolicyVersion:
        _validate_audit_fields(actor_id, reason, correlation_id)
        _validate_actor(actor_id)
        async with self.uow_factory() as uow:
            repository = uow.osint_open_policies
            await repository.lock_scope_index()
            current = await repository.get_version(policy_id, expected_version, for_update=True)
            if current is None:
                raise OsintPolicyNotFoundError("Policy version does not exist")
            await repository.lock_scope(current.scope_kind, current.scope_ref)
            payload_hash = self._payload_hash(
                action="revoke",
                scope_kind=current.scope_kind,
                scope_ref=current.scope_ref,
                capabilities=None,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
                activation_correlation_id=current.activation_correlation_id,
                previous_version=expected_version,
            )
            replay = await self._replay(repository, correlation_id, payload_hash)
            if replay is not None:
                return replay
            latest = await repository.latest_for_scope(
                current.scope_kind, current.scope_ref, for_update=True
            )
            if latest is None or latest.version != expected_version:
                raise OsintPolicyConflictError("Policy version changed")
            if latest.status is not OsintPolicyStatus.ACTIVE:
                raise OsintPolicyConflictError("Policy is already revoked")
            revoked_capabilities = OsintCapabilities(
                allowed_providers=(),
                budget_limits={
                    **latest.capabilities.budget_limits,
                    BudgetDimension.VT_UNITS: 0,
                    BudgetDimension.SHODAN_CREDITS: 0,
                    BudgetDimension.DOWNLOADS: 0,
                },
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(
                    tlp=latest.capabilities.disclosure.tlp,
                    do_not_submit=latest.capabilities.disclosure.do_not_submit,
                    external_llm_allowed=False,
                ),
                max_effect_claims_per_investigation=latest.capabilities.max_effect_claims_per_investigation,
            )
            policy = self._new_version(
                scope_kind=current.scope_kind,
                scope_ref=current.scope_ref,
                version=expected_version + 1,
                previous_version=expected_version,
                status=OsintPolicyStatus.REVOKED,
                capabilities=revoked_capabilities,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
                activation_correlation_id=current.activation_correlation_id,
                event_type=OsintPolicyEventType.REVOKED,
                policy_id=policy_id,
            )
            await repository.add_version(
                policy, self._event_for(policy, OsintPolicyEventType.REVOKED)
            )
            await self._propagate_revocation(uow, policy)
            await uow.commit()
            return policy

    async def ensure_inherited_envelope(
        self,
        uow: Any,
        *,
        investigation: object,
        snapshot: object,
        at_start: bool,
    ) -> object | None:
        """Pin once, only for a new investigation start with no prior execution state."""
        if not at_start:
            return None
        investigation_id = getattr(investigation, "id", None)
        subject_id = getattr(investigation, "subject_id", None)
        if not isinstance(investigation_id, UUID) or not isinstance(subject_id, UUID):
            raise OsintPolicyError("Investigation identity is incomplete")
        repository = uow.osint_open_policies
        await repository.lock_scope_index()
        candidates = await repository.latest_for_subject(subject_id)
        await repository.lock_investigation_start(investigation_id)
        pin = await repository.get_inheritance(investigation_id, for_update=True)
        envelope = await uow.investigation_envelopes.get(investigation_id)
        if pin is not None:
            if pin.subject_id != subject_id:
                raise OsintPolicyConflictError(
                    "Pinned subject does not match investigation subject"
                )
            if envelope is None:
                raise OsintPolicyConflictError("A durable policy pin exists without its envelope")
            return cast(object, envelope)
        if envelope is not None:
            return cast(object, envelope)
        if await uow.investigation_states.get(investigation_id) is not None:
            return None
        if await repository.invocation_count(investigation_id) != 0:
            return None
        if getattr(snapshot, "subject_id", subject_id) != subject_id:
            raise OsintPolicyConflictError("Source snapshot subject does not match investigation")
        matching_subject_sets = tuple(
            item for item in candidates if item.scope_kind is OsintScopeKind.SUBJECT_SET
        )
        if len(matching_subject_sets) > 1:
            raise OsintPolicyConflictError("Multiple subject policies match this investigation")
        if matching_subject_sets:
            policy = matching_subject_sets[0]
        else:
            global_policies = tuple(
                item for item in candidates if item.scope_kind is OsintScopeKind.INSTANCE
            )
            policy = global_policies[0] if global_policies else None
        if policy is None or policy.status is not OsintPolicyStatus.ACTIVE:
            return None
        disclosure = intersect_disclosure(
            getattr(snapshot, "tlp", None),
            getattr(snapshot, "do_not_submit", True),
            getattr(snapshot, "external_llm_allowed", False),
            policy.capabilities.disclosure,
        )
        envelope_service = InvestigationEnvelopeService(self.uow_factory, settings=self.settings)
        inherited_envelope = await envelope_service.create_policy_inherited_envelope(
            uow,
            investigation_id=investigation_id,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_event_id=policy.event_id,
            actor_id=policy.actor_id,
            capabilities=policy.capabilities,
            disclosure=disclosure,
        )
        now = datetime.now(UTC)
        pin = OsintInheritance(
            investigation_id=investigation_id,
            subject_id=subject_id,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_event_id=policy.event_id,
            correlation_id=(
                f"osint-inherit:{policy.policy_id}:{policy.version}:{investigation_id}"
            ),
            activation_correlation_id=policy.activation_correlation_id,
            status=OsintInheritanceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        try:
            await repository.add_inheritance(pin)
        except Exception as exc:
            # The transaction is rolled back by the UoW; the primary-key pin is the
            # concurrency oracle, so callers retry the entire start request.
            raise OsintPolicyConflictError("Concurrent policy inheritance; retry start") from exc
        return inherited_envelope

    async def effective_for(
        self, uow: Any, investigation_id: UUID, *, for_update: bool = False
    ) -> OsintEffectivePolicy | None:
        repository = uow.osint_open_policies
        candidate = await repository.get_inheritance(investigation_id)
        if candidate is None:
            return None
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None or investigation.subject_id != candidate.subject_id:
            raise OsintPolicyConflictError("Pinned subject no longer matches investigation")
        policy = await repository.get_version(
            candidate.policy_id,
            candidate.policy_version,
        )
        if policy is None:
            raise OsintPolicyConflictError("Pinned policy version is missing")
        # Revocation locks the current policy row before inheritance rows. S3
        # consume must use this same order: policy status, pin, investigation,
        # envelope, then the effect-claim unique key.
        latest = await repository.latest_for_scope(
            policy.scope_kind,
            policy.scope_ref,
            for_update=for_update,
        )
        inheritance = (
            await repository.get_inheritance(investigation_id, for_update=True)
            if for_update
            else candidate
        )
        if inheritance is None or (
            inheritance.policy_id,
            inheritance.policy_version,
            inheritance.policy_event_id,
            inheritance.subject_id,
        ) != (
            candidate.policy_id,
            candidate.policy_version,
            candidate.policy_event_id,
            candidate.subject_id,
        ):
            raise OsintPolicyConflictError("Policy pin changed while resolving authorization")
        event = await repository.event_by_id(inheritance.policy_event_id)
        if (
            event is None
            or event.policy_id != inheritance.policy_id
            or event.policy_version != inheritance.policy_version
        ):
            raise OsintPolicyConflictError("Pinned policy event lineage is incomplete")
        if latest is None:
            raise OsintPolicyConflictError("Pinned policy has no current version")
        return OsintEffectivePolicy(
            inheritance=inheritance,
            policy=policy,
            current_policy_status=latest.status,
        )

    async def effective_for_investigation(
        self, investigation_id: UUID
    ) -> OsintEffectivePolicy | None:
        """Resolve immutable pin status in a short read transaction for effect gates."""
        async with self.uow_factory() as uow:
            return await self.effective_for(uow, investigation_id)

    def default_capabilities(
        self, allowed_providers: tuple[str, ...] | None = None
    ) -> OsintCapabilities:
        settings = self.settings
        providers = ("vt", "bridge") if allowed_providers is None else allowed_providers
        budget = {
            BudgetDimension.VT_UNITS: settings.osint_open_default_vt_units,
            BudgetDimension.DOWNLOADS: settings.osint_open_default_downloads,
            BudgetDimension.BRIDGE_TURNS: settings.osint_open_default_bridge_turns,
            BudgetDimension.TOOL_SECONDS: settings.osint_open_default_tool_seconds,
            BudgetDimension.CONTEXT_BYTES: settings.osint_open_default_context_bytes,
            BudgetDimension.CONTEXT_TOKENS: settings.osint_open_default_context_tokens,
        }
        if settings.osint_open_default_shodan_credits is not None:
            budget[BudgetDimension.SHODAN_CREDITS] = settings.osint_open_default_shodan_credits
            if allowed_providers is None:
                providers = (*providers, "shodan")
        capabilities = OsintCapabilities(
            allowed_providers=providers,
            budget_limits=budget,
            acquire_hits_allowed=settings.osint_open_default_acquire_hits_allowed,
            disclosure=DisclosureBounds(
                tlp=TLPLevel(settings.osint_open_default_tlp),
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            max_effect_claims_per_investigation=settings.osint_open_default_max_effect_claims,
        )
        self._validate_capabilities(capabilities)
        return capabilities

    def _validate_capabilities(self, capabilities: OsintCapabilities) -> None:
        if not isinstance(capabilities, OsintCapabilities):
            raise OsintPolicyError("Policy capabilities are required")
        unknown = set(capabilities.allowed_providers).difference(_KNOWN_PROVIDERS)
        if unknown:
            raise OsintPolicyError("Unsupported policy provider")
        maxima = {
            BudgetDimension.VT_UNITS: self.settings.osint_open_max_vt_units,
            BudgetDimension.DOWNLOADS: self.settings.osint_open_max_downloads,
            BudgetDimension.BRIDGE_TURNS: self.settings.osint_open_max_bridge_turns,
            BudgetDimension.TOOL_SECONDS: self.settings.osint_open_max_tool_seconds,
            BudgetDimension.CONTEXT_BYTES: self.settings.osint_open_max_context_bytes,
            BudgetDimension.CONTEXT_TOKENS: self.settings.osint_open_max_context_tokens,
            BudgetDimension.SHODAN_CREDITS: self.settings.osint_open_max_shodan_credits,
        }
        for dimension, amount in capabilities.budget_limits.items():
            if amount > maxima[dimension]:
                raise OsintPolicyError(f"{dimension.value} exceeds the configured maximum")
        if (
            capabilities.max_effect_claims_per_investigation
            > self.settings.osint_open_max_effect_claims
        ):
            raise OsintPolicyError("Effect-claim limit exceeds the configured maximum")

    async def _propagate_revocation(self, uow: Any, policy: OsintPolicyVersion) -> None:
        repository = uow.osint_open_policies
        inherited = await repository.active_inheritances_for_policy(policy.policy_id)
        for pin in inherited:
            investigation = await uow.analyst_investigations.get(pin.investigation_id)
            envelope = await uow.investigation_envelopes.get(pin.investigation_id)
            if investigation is None or investigation.subject_id != pin.subject_id:
                raise OsintPolicyConflictError("Pinned subject no longer matches investigation")
            if envelope is None:
                raise OsintPolicyConflictError("Pinned envelope is missing")
            from cti_app.domain.investigation import EnvelopeAuthorityKind

            if envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY:
                await repository.set_inheritance_status(pin, OsintInheritanceStatus.SUPERSEDED)
                continue
            if (
                envelope.inherited_policy_id != pin.policy_id
                or envelope.inherited_policy_version != pin.policy_version
                or envelope.inherited_policy_event_id != pin.policy_event_id
            ):
                raise OsintPolicyConflictError("Envelope policy lineage differs from its pin")
            external_dimensions = {
                BudgetDimension.VT_UNITS,
                BudgetDimension.SHODAN_CREDITS,
                BudgetDimension.DOWNLOADS,
            }
            limits = dict(envelope.budget.limits)
            limits.update({dimension: 0 for dimension in external_dimensions})
            budget = InvestigationBudget(limits=limits)
            for reservation in envelope.budget.reservations:
                budget.restore_reservation(reservation)
            updated = type(envelope)(
                investigation_id=envelope.investigation_id,
                allowed_providers=(),
                budget=budget,
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(
                    tlp=envelope.disclosure.tlp,
                    do_not_submit=envelope.disclosure.do_not_submit,
                    external_llm_allowed=False,
                ),
                approval_decision_id=envelope.approval_decision_id,
                approval_actor_id=envelope.approval_actor_id,
                authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
                inherited_policy_id=envelope.inherited_policy_id,
                inherited_policy_version=envelope.inherited_policy_version,
                inherited_policy_event_id=envelope.inherited_policy_event_id,
                approved_at=envelope.approved_at,
                version=envelope.version + 1,
            )
            await uow.investigation_envelopes.save_if_version(
                updated, expected_version=envelope.version
            )
            await repository.set_inheritance_status(pin, OsintInheritanceStatus.REVOKED)

    async def _replay(
        self, repository: Any, correlation_id: str, payload_hash: str
    ) -> OsintPolicyVersion | None:
        existing = await repository.by_correlation(correlation_id)
        if existing is None:
            return None
        policy, event = existing
        if event.payload_sha256 != payload_hash:
            raise OsintPolicyConflictError("Correlation was reused with different policy content")
        return cast(OsintPolicyVersion, policy)

    @staticmethod
    def _payload_hash(
        *,
        action: str,
        scope_kind: OsintScopeKind,
        scope_ref: str,
        capabilities: OsintCapabilities | None,
        actor_id: str,
        reason: str,
        correlation_id: str,
        activation_correlation_id: str,
        previous_version: int | None,
    ) -> str:
        payload = {
            "action": action,
            "scope_kind": scope_kind.value,
            "scope_ref": scope_ref,
            "capabilities": _capabilities_payload(capabilities) if capabilities else None,
            "actor_id": actor_id.strip(),
            "reason": reason.strip(),
            "correlation_id": correlation_id.strip(),
            "activation_correlation_id": activation_correlation_id.strip(),
            "previous_version": previous_version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_version(
        *,
        scope_kind: OsintScopeKind,
        scope_ref: str,
        version: int,
        previous_version: int | None,
        status: OsintPolicyStatus,
        capabilities: OsintCapabilities,
        actor_id: str,
        reason: str,
        correlation_id: str,
        activation_correlation_id: str,
        event_type: OsintPolicyEventType,
        policy_id: UUID | None = None,
    ) -> OsintPolicyVersion:
        stable_id = policy_id or uuid5(_POLICY_NAMESPACE, f"{scope_kind.value}:{scope_ref}")
        event_id = uuid4()
        return OsintPolicyVersion(
            policy_id=stable_id,
            version=version,
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            mode=OsintPolicyMode.OSINT_OPEN,
            status=status,
            capabilities=capabilities,
            actor_id=actor_id.strip(),
            reason=reason.strip(),
            correlation_id=correlation_id.strip(),
            activation_correlation_id=activation_correlation_id.strip(),
            event_id=event_id,
            created_at=datetime.now(UTC),
            previous_version=previous_version,
        )

    @staticmethod
    def _event_for(
        policy: OsintPolicyVersion, event_type: OsintPolicyEventType
    ) -> OsintPolicyEvent:
        action = {
            OsintPolicyEventType.ACTIVATED: "activate",
            OsintPolicyEventType.EXTENDED: "extend",
            OsintPolicyEventType.REVOKED: "revoke",
        }[event_type]
        return OsintPolicyEvent(
            id=policy.event_id,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            event_type=event_type,
            actor_id=policy.actor_id,
            reason=policy.reason,
            correlation_id=policy.correlation_id,
            payload_sha256=OsintOpenPolicyService._payload_hash(
                action=action,
                scope_kind=policy.scope_kind,
                scope_ref=policy.scope_ref,
                capabilities=policy.capabilities
                if event_type is not OsintPolicyEventType.REVOKED
                else None,
                actor_id=policy.actor_id,
                reason=policy.reason,
                correlation_id=policy.correlation_id,
                activation_correlation_id=policy.activation_correlation_id,
                previous_version=policy.previous_version,
            ),
            previous_version=policy.previous_version,
            created_at=policy.created_at,
        )


def _normalize_scope_ref(scope_kind: OsintScopeKind, scope_ref: str) -> str:
    if scope_kind is OsintScopeKind.INSTANCE:
        if scope_ref != "instance":
            raise OsintPolicyError("INSTANCE is global and must use scope_ref 'instance'")
        return scope_ref
    try:
        ids = OsintPolicyVersion.scope_subject_ids(scope_ref)
    except ValueError as exc:
        raise OsintPolicyError("Invalid SUBJECT_SET scope") from exc
    return OsintPolicyVersion.subject_set_ref(tuple(ids))


def _validate_actor(actor_id: str) -> None:
    if not is_human_actor_id(actor_id):
        raise OsintPolicyError("A human operator identity is required")


def _validate_audit_fields(actor_id: str, reason: str, correlation_id: str) -> None:
    fields = (
        ("actor", actor_id, 255),
        ("reason", reason, 2000),
        ("correlation", correlation_id, 255),
    )
    for label, value, max_length in fields:
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise OsintPolicyError(f"Invalid {label} field")
        if _SECRET_SHAPED.search(value) or _CONTROL.search(value):
            raise OsintPolicyError(f"Unsafe {label} field")


def _capabilities_payload(capabilities: OsintCapabilities) -> dict[str, object]:
    return {
        "allowed_providers": list(capabilities.allowed_providers),
        "budget_limits": {
            dimension.value: amount
            for dimension, amount in sorted(
                capabilities.budget_limits.items(), key=lambda item: item[0].value
            )
        },
        "acquire_hits_allowed": capabilities.acquire_hits_allowed,
        "disclosure": {
            "tlp": capabilities.disclosure.tlp.value if capabilities.disclosure.tlp else None,
            "do_not_submit": capabilities.disclosure.do_not_submit,
            "external_llm_allowed": capabilities.disclosure.external_llm_allowed,
        },
        "max_effect_claims_per_investigation": capabilities.max_effect_claims_per_investigation,
    }


__all__ = [
    "OsintOpenPolicyService",
    "OsintPolicyConflictError",
    "OsintPolicyError",
    "OsintPolicyNotFoundError",
]
