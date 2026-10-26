"""VT family bindings and consume-time OSINT_OPEN policy guard."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.external_effects import (
    ConsumePolicyContext,
    ExternalEffectClaim,
    ReuseDecision,
)
from cti_app.domain.external_effects import (
    EffectClaimState,
    ExternalEffectAuthority,
    PreparedExternalRequest,
)
from cti_app.domain.osint_open import (
    OsintInheritanceStatus,
    OsintPolicyEventType,
    OsintPolicyStatus,
    OsintPolicyVersion,
    OsintScopeKind,
)
from cti_app.infrastructure.database.models.external_effects import ExternalEffectClaimRow
from cti_app.infrastructure.database.models.osint_open_policy import (
    OsintOpenInheritanceRow,
    OsintOpenPolicyEventRow,
    OsintOpenPolicyRow,
)
from cti_app.infrastructure.database.repositories.external_effects import (
    ExternalEffectFamilyBinding,
)

VT_EXTERNAL_EFFECT_FAMILIES = {
    "vt.file_report": ("vt.file_report", "1"),
    "vt.object_report": ("vt.object_report", "1"),
    "vt.relationships": ("vt.relationships", "1"),
    "vt.intelligence_search": ("vt.intelligence_search", "1"),
    "vt.behaviour": ("vt.behaviour", "1"),
}
_VT_RAW_RESPONSE_KIND = "vt.raw_response"


def virustotal_external_effect_family_bindings() -> dict[str, ExternalEffectFamilyBinding]:
    return {
        family: ExternalEffectFamilyBinding(
            provider="vt",
            provider_scopes=frozenset({"default"}),
            normalizer_versions=frozenset({"vt-read.v1"}),
            invocation_tool_binding=tool_binding,
            credential_parameter_names=frozenset({"apikey"}),
        )
        for family, tool_binding in VT_EXTERNAL_EFFECT_FAMILIES.items()
    }


class VirusTotalReadReusePolicy:
    """Allow only completed VT claims with a durable, verifiable raw response."""

    async def authorize_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> ReuseDecision | None:
        del authority, prepared
        references = tuple(
            reference
            for reference in claim.evidence_refs
            if reference.kind == _VT_RAW_RESPONSE_KIND
        )
        if (
            claim.state is not EffectClaimState.COMPLETED
            or claim.result_ref is None
            or len(references) != 1
            or references[0].id != claim.result_ref
        ):
            return None
        return ReuseDecision(
            source_invocation_id=claim.invocation_id,
            observation_refs=references,
        )


class SqlAlchemyVirusTotalPolicyGuard:
    """Lock a pinned OSINT policy before the coordinator locks its envelope."""

    async def lock_for_consume(
        self, database_session: AsyncSession, authority: ExternalEffectAuthority
    ) -> bool:
        policy_event_id = authority.policy_event_id
        if (
            policy_event_id is None
            or authority.pinned_subject_id is None
            or authority.policy_version is None
            or not authority.policy_version.isdecimal()
        ):
            return False
        event = await database_session.scalar(
            select(OsintOpenPolicyEventRow)
            .where(OsintOpenPolicyEventRow.id == policy_event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            event is None
            or event.policy_version != int(authority.policy_version)
            or event.event_type
            not in {
                OsintPolicyEventType.ACTIVATED.value,
                OsintPolicyEventType.EXTENDED.value,
            }
        ):
            return False

        pinned = await database_session.scalar(
            select(OsintOpenPolicyRow)
            .where(
                OsintOpenPolicyRow.policy_id == event.policy_id,
                OsintOpenPolicyRow.version == event.policy_version,
            )
            .execution_options(populate_existing=True)
        )
        if pinned is None:
            return False
        current = await database_session.scalar(
            select(OsintOpenPolicyRow)
            .where(OsintOpenPolicyRow.policy_id == event.policy_id)
            .order_by(OsintOpenPolicyRow.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        inheritance = await database_session.scalar(
            select(OsintOpenInheritanceRow)
            .where(OsintOpenInheritanceRow.investigation_id == authority.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (
            current is not None
            and current.status == OsintPolicyStatus.ACTIVE.value
            and pinned.status == OsintPolicyStatus.ACTIVE.value
            and inheritance is not None
            and inheritance.status == OsintInheritanceStatus.ACTIVE.value
            and inheritance.policy_id == event.policy_id
            and inheritance.policy_version == event.policy_version
            and inheritance.policy_event_id == event.id
            and inheritance.subject_id == authority.pinned_subject_id
        )

    async def authorize(self, context: ConsumePolicyContext) -> bool:
        authority = context.authority
        if (
            authority.policy_event_id is None
            or authority.pinned_subject_id is None
            or authority.policy_version is None
            or not authority.policy_version.isdecimal()
            or authority.pinned_subject_id != context.investigation_subject_id
        ):
            return False
        session = context.database_session
        if not isinstance(session, AsyncSession):
            return False
        event = await session.scalar(
            select(OsintOpenPolicyEventRow).where(
                OsintOpenPolicyEventRow.id == authority.policy_event_id
            )
        )
        if (
            event is None
            or event.policy_version != int(authority.policy_version)
            or event.event_type
            not in {
                OsintPolicyEventType.ACTIVATED.value,
                OsintPolicyEventType.EXTENDED.value,
            }
        ):
            return False
        pinned = await session.scalar(
            select(OsintOpenPolicyRow).where(
                OsintOpenPolicyRow.policy_id == event.policy_id,
                OsintOpenPolicyRow.version == event.policy_version,
            )
        )
        current = await session.scalar(
            select(OsintOpenPolicyRow)
            .where(OsintOpenPolicyRow.policy_id == event.policy_id)
            .order_by(OsintOpenPolicyRow.version.desc())
            .limit(1)
        )
        inheritance = await session.scalar(
            select(OsintOpenInheritanceRow).where(
                OsintOpenInheritanceRow.investigation_id == authority.investigation_id
            )
        )
        if (
            pinned is None
            or pinned.status != OsintPolicyStatus.ACTIVE.value
            or current is None
            or current.status != OsintPolicyStatus.ACTIVE.value
            or inheritance is None
            or inheritance.status != OsintInheritanceStatus.ACTIVE.value
            or inheritance.policy_event_id != event.id
            or inheritance.policy_id != event.policy_id
            or inheritance.policy_version != event.policy_version
            or inheritance.subject_id != authority.pinned_subject_id
        ):
            return False
        try:
            scope_kind = OsintScopeKind(pinned.scope_kind)
            subject_matches = (
                scope_kind is OsintScopeKind.INSTANCE
                or authority.pinned_subject_id
                in OsintPolicyVersion.scope_subject_ids(pinned.scope_ref)
            )
        except (TypeError, ValueError):
            return False
        if not subject_matches or context.prepared.provider not in pinned.allowed_providers:
            return False
        claim_count = await session.scalar(
            select(func.count(ExternalEffectClaimRow.id)).where(
                ExternalEffectClaimRow.investigation_id == authority.investigation_id
            )
        )
        existing_claim = await session.scalar(
            select(ExternalEffectClaimRow.id).where(
                ExternalEffectClaimRow.investigation_id == authority.investigation_id,
                ExternalEffectClaimRow.effect_key == context.effect_key,
            )
        )
        if (
            existing_claim is None
            and int(claim_count or 0) >= pinned.max_effect_claims_per_investigation
        ):
            return False
        vt_budget = int(pinned.budget_limits.get("vt_units", 0))
        reserved_vt = int(context.authority.reservation_amounts.get("vt_units", 0))
        return reserved_vt <= vt_budget


__all__ = [
    "VT_EXTERNAL_EFFECT_FAMILIES",
    "SqlAlchemyVirusTotalPolicyGuard",
    "VirusTotalReadReusePolicy",
    "virustotal_external_effect_family_bindings",
]
