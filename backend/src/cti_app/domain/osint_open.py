"""Pure OSINT_OPEN contracts. INSTANCE scope is global; this app has no tenant dimension."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from cti_app.domain.investigation import BudgetDimension, DisclosureBounds, TLPLevel


class OsintPolicyMode(StrEnum):
    RESTRICTED = "restricted"
    OSINT_OPEN = "osint_open"


class OsintScopeKind(StrEnum):
    INSTANCE = "instance"
    SUBJECT_SET = "subject_set"


class OsintPolicyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class OsintInheritanceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class OsintPolicyEventType(StrEnum):
    ACTIVATED = "activated"
    EXTENDED = "extended"
    REVOKED = "revoked"


_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REQUIRED_BUDGETS = frozenset(
    {
        BudgetDimension.VT_UNITS,
        BudgetDimension.DOWNLOADS,
        BudgetDimension.BRIDGE_TURNS,
        BudgetDimension.TOOL_SECONDS,
        BudgetDimension.CONTEXT_BYTES,
        BudgetDimension.CONTEXT_TOKENS,
    }
)


@dataclass(frozen=True, slots=True)
class OsintCapabilities:
    """Non-secret policy ceiling granted to matching investigations."""

    allowed_providers: tuple[str, ...]
    budget_limits: Mapping[BudgetDimension, int]
    acquire_hits_allowed: bool
    disclosure: DisclosureBounds
    max_effect_claims_per_investigation: int

    def __post_init__(self) -> None:
        providers = tuple(sorted({item.strip().casefold() for item in self.allowed_providers}))
        if any(not _PROVIDER_RE.fullmatch(item) for item in providers):
            raise ValueError("Provider names must be lowercase identifiers")
        if not isinstance(self.acquire_hits_allowed, bool):
            raise TypeError("acquire_hits_allowed must be a boolean")
        if not isinstance(self.disclosure, DisclosureBounds):
            raise TypeError("disclosure must use DisclosureBounds")
        if self.disclosure.tlp is None:
            raise ValueError("OSINT_OPEN policy requires an explicit default TLP")
        limits: dict[BudgetDimension, int] = {}
        for dimension, amount in self.budget_limits.items():
            if not isinstance(dimension, BudgetDimension):
                raise TypeError("Budget dimensions must use BudgetDimension")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("Policy budgets must be finite non-negative integers")
            limits[dimension] = amount
        if not _REQUIRED_BUDGETS.issubset(limits):
            raise ValueError("All required OSINT_OPEN budget dimensions must be finite")
        if self.acquire_hits_allowed and (
            "vt" not in providers
            or limits[BudgetDimension.VT_UNITS] < 1
            or limits[BudgetDimension.DOWNLOADS] < 1
        ):
            raise ValueError("Hit acquisition requires positive VT_UNITS and DOWNLOADS limits")
        if (
            isinstance(self.max_effect_claims_per_investigation, bool)
            or not isinstance(self.max_effect_claims_per_investigation, int)
            or self.max_effect_claims_per_investigation < 1
        ):
            raise ValueError("max_effect_claims_per_investigation must be positive")
        if (
            BudgetDimension.VT_UNITS in limits
            and "vt" in providers
            and limits[BudgetDimension.VT_UNITS] < 1
        ):
            raise ValueError("The vt provider requires a positive VT_UNITS limit")
        if "shodan" in providers and limits.get(BudgetDimension.SHODAN_CREDITS, 0) < 1:
            raise ValueError("The shodan provider requires a positive SHODAN_CREDITS limit")
        if self.disclosure.external_llm_allowed and limits[BudgetDimension.BRIDGE_TURNS] < 1:
            raise ValueError("External model disclosure requires a positive BRIDGE_TURNS limit")
        if "bridge" in providers and (
            not self.disclosure.external_llm_allowed or limits[BudgetDimension.BRIDGE_TURNS] < 1
        ):
            raise ValueError(
                "The bridge provider requires external disclosure and a positive limit"
            )
        effects_enabled = (
            bool(providers) or self.acquire_hits_allowed or self.disclosure.external_llm_allowed
        )
        if effects_enabled:
            for dimension in (
                BudgetDimension.TOOL_SECONDS,
                BudgetDimension.CONTEXT_BYTES,
                BudgetDimension.CONTEXT_TOKENS,
            ):
                if limits[dimension] < 1:
                    raise ValueError(f"{dimension.value} must be positive for enabled effects")
        object.__setattr__(self, "allowed_providers", providers)
        object.__setattr__(self, "budget_limits", MappingProxyType(limits))

    def require_growth_from(self, current: OsintCapabilities) -> None:
        if not set(current.allowed_providers).issubset(self.allowed_providers):
            raise ValueError("A policy extension cannot remove an allowed provider")
        for dimension, previous in current.budget_limits.items():
            if self.budget_limits.get(dimension, 0) < previous:
                raise ValueError(f"A policy extension cannot reduce {dimension.value}")
        if current.acquire_hits_allowed and not self.acquire_hits_allowed:
            raise ValueError("A policy extension cannot revoke acquisition rights")
        if current.disclosure.do_not_submit and not self.disclosure.do_not_submit:
            raise ValueError("A policy extension cannot clear do_not_submit")
        if current.disclosure.external_llm_allowed and not self.disclosure.external_llm_allowed:
            raise ValueError("A policy extension cannot revoke external model rights")
        if self.disclosure.tlp is None or current.disclosure.tlp is None:
            raise ValueError("Policy disclosure requires a concrete TLP")
        tlp_order = {
            TLPLevel.WHITE: 0,
            TLPLevel.CLEAR: 1,
            TLPLevel.GREEN: 2,
            TLPLevel.AMBER: 3,
            TLPLevel.RED: 4,
        }
        if tlp_order[self.disclosure.tlp] < tlp_order[current.disclosure.tlp]:
            raise ValueError("A policy extension cannot lower the TLP restriction")
        if current.max_effect_claims_per_investigation > self.max_effect_claims_per_investigation:
            raise ValueError("A policy extension cannot reduce the effect-claim ceiling")


@dataclass(frozen=True, slots=True)
class OsintPolicyVersion:
    policy_id: UUID
    version: int
    scope_kind: OsintScopeKind
    scope_ref: str
    mode: OsintPolicyMode
    status: OsintPolicyStatus
    capabilities: OsintCapabilities
    actor_id: str
    reason: str
    correlation_id: str
    activation_correlation_id: str
    event_id: UUID
    created_at: datetime
    previous_version: int | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Policy version must be positive")
        if self.previous_version is not None and self.previous_version != self.version - 1:
            raise ValueError("Policy previous_version must immediately precede version")
        if not self.scope_ref.strip() or not self.actor_id.strip() or not self.reason.strip():
            raise ValueError("Policy scope, actor and reason are required")
        if not self.correlation_id.strip() or not self.activation_correlation_id.strip():
            raise ValueError("Policy correlations are required")
        if self.scope_kind is OsintScopeKind.INSTANCE and self.scope_ref != "instance":
            raise ValueError("INSTANCE policy scope_ref must be 'instance'")
        if self.scope_kind is OsintScopeKind.SUBJECT_SET:
            subject_ids = self.scope_subject_ids(self.scope_ref)
            if not subject_ids:
                raise ValueError("SUBJECT_SET policy requires at least one subject")
        if self.mode is not OsintPolicyMode.OSINT_OPEN:
            raise ValueError("Persisted OSINT_OPEN policy versions must use OSINT_OPEN mode")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("Policy timestamp must be timezone-aware")
        if self.status is OsintPolicyStatus.REVOKED:
            caps = self.capabilities
            if (
                caps.allowed_providers
                or caps.acquire_hits_allowed
                or caps.disclosure.external_llm_allowed
            ):
                raise ValueError("A revoked policy must have zero external rights")
            if any(
                caps.budget_limits[item] != 0
                for item in (
                    BudgetDimension.VT_UNITS,
                    BudgetDimension.SHODAN_CREDITS,
                    BudgetDimension.DOWNLOADS,
                )
            ):
                raise ValueError("A revoked policy must zero acquisition budgets")

    @staticmethod
    def scope_subject_ids(scope_ref: str) -> frozenset[UUID]:
        values = [part for part in scope_ref.split(",") if part]
        try:
            parsed = tuple(UUID(value) for value in values)
        except ValueError as exc:
            raise ValueError("SUBJECT_SET scope_ref must contain UUIDs") from exc
        if tuple(sorted(set(parsed), key=str)) != parsed:
            raise ValueError("SUBJECT_SET UUIDs must be unique and canonical")
        if any(str(item) != raw for item, raw in zip(parsed, values, strict=True)):
            raise ValueError("SUBJECT_SET UUIDs must use canonical form")
        return frozenset(parsed)

    @staticmethod
    def subject_set_ref(subject_ids: tuple[UUID, ...] | list[UUID]) -> str:
        normalized = tuple(sorted(set(subject_ids), key=str))
        if not normalized:
            raise ValueError("SUBJECT_SET requires at least one subject")
        return ",".join(str(item) for item in normalized)

    def contains_subject(self, subject_id: UUID) -> bool:
        return self.scope_kind is OsintScopeKind.INSTANCE or subject_id in self.scope_subject_ids(
            self.scope_ref
        )


@dataclass(frozen=True, slots=True)
class OsintPolicyEvent:
    id: UUID
    policy_id: UUID
    policy_version: int
    event_type: OsintPolicyEventType
    actor_id: str
    reason: str
    correlation_id: str
    payload_sha256: str
    previous_version: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OsintInheritance:
    investigation_id: UUID
    subject_id: UUID
    policy_id: UUID
    policy_version: int
    policy_event_id: UUID
    correlation_id: str
    activation_correlation_id: str
    status: OsintInheritanceStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OsintEffectivePolicy:
    """Immutable pinned policy plus mutable pin status for downstream effect gates."""

    inheritance: OsintInheritance
    policy: OsintPolicyVersion
    current_policy_status: OsintPolicyStatus

    @property
    def revoked(self) -> bool:
        return (
            self.inheritance.status is OsintInheritanceStatus.REVOKED
            or self.current_policy_status is OsintPolicyStatus.REVOKED
        )

    @property
    def authorized(self) -> bool:
        return (
            not self.revoked
            and self.inheritance.status is OsintInheritanceStatus.ACTIVE
            and self.policy.status is OsintPolicyStatus.ACTIVE
        )


def intersect_disclosure(
    source_tlp: TLPLevel | str | None,
    source_do_not_submit: bool,
    source_external_llm_allowed: bool,
    policy: DisclosureBounds,
) -> DisclosureBounds:
    """Intersect source disclosure with a policy ceiling; malformed TLP fails closed."""
    try:
        raw_source_tlp = source_tlp.value if isinstance(source_tlp, TLPLevel) else source_tlp
        if isinstance(raw_source_tlp, str):
            raw_source_tlp = raw_source_tlp.strip().casefold()
            if raw_source_tlp.startswith("tlp:"):
                raw_source_tlp = raw_source_tlp[4:].strip()
        source_level = TLPLevel(raw_source_tlp) if raw_source_tlp is not None else None
    except (TypeError, ValueError):
        source_level = None
    policy_level = policy.tlp
    if source_level is None or policy_level is None:
        return DisclosureBounds(tlp=TLPLevel.RED, do_not_submit=True, external_llm_allowed=False)
    order = {
        TLPLevel.WHITE: 0,
        TLPLevel.CLEAR: 1,
        TLPLevel.GREEN: 2,
        TLPLevel.AMBER: 3,
        TLPLevel.RED: 4,
    }
    valid_flags = isinstance(source_do_not_submit, bool) and isinstance(
        source_external_llm_allowed, bool
    )
    if not valid_flags:
        return DisclosureBounds(tlp=TLPLevel.RED, do_not_submit=True, external_llm_allowed=False)
    return DisclosureBounds(
        tlp=max((source_level, policy_level), key=order.__getitem__),
        do_not_submit=source_do_not_submit or policy.do_not_submit,
        external_llm_allowed=(
            source_external_llm_allowed
            and policy.external_llm_allowed
            and source_level is not TLPLevel.RED
            and policy_level is not TLPLevel.RED
        ),
    )


__all__ = [
    "OsintCapabilities",
    "OsintEffectivePolicy",
    "OsintInheritance",
    "OsintInheritanceStatus",
    "OsintPolicyEvent",
    "OsintPolicyEventType",
    "OsintPolicyMode",
    "OsintPolicyStatus",
    "OsintPolicyVersion",
    "OsintScopeKind",
    "intersect_disclosure",
]
