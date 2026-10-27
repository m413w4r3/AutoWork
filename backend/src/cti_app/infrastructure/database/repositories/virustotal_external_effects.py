"""VT family bindings and consume-time OSINT_OPEN policy guard."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.external_effects import (
    ConsumePolicyContext,
    ExternalEffectClaim,
    ReuseDecision,
    external_effect_source_do_not_submit_blocks,
)
from cti_app.application.virustotal import VT_DOWNLOAD_CREDENTIAL_PARAMETER_NAMES
from cti_app.domain.external_effects import (
    EffectClaimState,
    EvidenceReference,
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
from cti_app.infrastructure.database.models.core import BlobRow, SampleRow, SourceDocumentRow
from cti_app.infrastructure.database.models.external_effects import ExternalEffectClaimRow
from cti_app.infrastructure.database.models.investigation import (
    EvidenceObservationRow,
    SampleAssociationRow,
)
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
    "vt.behaviour_pcap": ("vt.behaviour_pcap", "1"),
    "vt.download_sample": ("vt.download_sample", "1"),
}
_VT_RAW_RESPONSE_KIND = "vt.raw_response"
_VT_SOURCE_REFS_KEY = "_vt_external_effect_source_refs"
_VT_FILE_OBJECT_TYPES = {"sample", "file", "sample_observation"}
_VT_HASH_READ_FAMILIES = {
    "vt.file_report",
    "vt.behaviour",
    "vt.behaviour_pcap",
    "vt.relationships",
}
# Bridge/advisor dossiers ignore the source do_not_submit veto, as the claim
# store does at consume (user decision 2026-10-27); a source that forbids
# external LLMs, TLP RED and licences still bind.
_BRIDGE_FAMILY_PREFIXES = ("bridge.", "advisor.")


def _source_flags_block(family: str, *, do_not_submit: bool, external_llm_allowed: bool) -> bool:
    if family.startswith(_BRIDGE_FAMILY_PREFIXES):
        return not external_llm_allowed
    return external_effect_source_do_not_submit_blocks(family, do_not_submit)


def _metadata_source_refs(parameters: Mapping[str, object]) -> tuple[EvidenceReference, ...]:
    raw = parameters.get(_VT_SOURCE_REFS_KEY)
    if not isinstance(raw, (tuple, list)):
        return ()
    references: list[EvidenceReference] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return ()
        try:
            references.append(
                EvidenceReference(
                    kind=str(item["kind"]),
                    id=str(item["id"]),
                    version=str(item["version"]),
                    sha256=str(item["sha256"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(references)


def _source_refs_for_family(
    family: str, parameters: Mapping[str, object]
) -> tuple[EvidenceReference, ...]:
    metadata = _metadata_source_refs(parameters)
    hash_value: object = None
    if family in {"vt.file_report", "vt.behaviour", "vt.behaviour_pcap"}:
        hash_value = parameters.get("file_hash")
    elif family == "vt.relationships" and parameters.get("object_type") == "file":
        hash_value = parameters.get("object_id")
    elif family == "vt.download_sample":
        hash_value = parameters.get("sha256")
    if hash_value is not None:
        if not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hash_value):
            return ()
        expected = (
            EvidenceReference(
                kind="sample_hash",
                id=hash_value,
                version="sample-hash.v1",
                sha256=hash_value,
            ),
        )
        if not metadata or metadata == expected:
            return expected
        if (
            sum(reference.kind == "sample_hash" for reference in metadata) == 1
            and expected[0] in metadata
            and all(
                reference.kind in {"sample_hash", "evidence_observation"} for reference in metadata
            )
        ):
            return metadata
        return ()
    return metadata


def _source_resolver_for_family(
    family: str,
) -> Callable[[Mapping[str, object]], tuple[EvidenceReference, ...]]:
    def resolve(parameters: Mapping[str, object]) -> tuple[EvidenceReference, ...]:
        return _source_refs_for_family(family, parameters)

    return resolve


def virustotal_external_effect_family_bindings() -> dict[str, ExternalEffectFamilyBinding]:
    return {
        family: ExternalEffectFamilyBinding(
            provider="vt",
            provider_scopes=frozenset({"default"}),
            normalizer_versions=frozenset(
                {"vt-download.v1" if family == "vt.download_sample" else "vt-read.v1"}
            ),
            invocation_tool_binding=tool_binding,
            credential_parameter_names=(
                VT_DOWNLOAD_CREDENTIAL_PARAMETER_NAMES
                if family == "vt.download_sample"
                else frozenset({"apikey"})
            ),
            source_reference_resolver=_source_resolver_for_family(family),
            requires_source_refs=True,
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
    """Check VT source disclosure and lock pinned OSINT policy before the envelope."""

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
        if (
            pinned.disclosure_tlp.casefold() == "red"
            or pinned.disclosure_do_not_submit
            or not pinned.disclosure_external_llm_allowed
            or not await self.authorize_source_disclosure(context)
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

    async def authorize_source_disclosure(self, context: ConsumePolicyContext) -> bool:
        return await self._source_external_disclosure_allowed(context)

    async def _source_external_disclosure_allowed(self, context: ConsumePolicyContext) -> bool:
        session = context.database_session
        if not isinstance(session, AsyncSession):
            return False
        sample_hashes = {
            reference.id
            for reference in context.prepared.evidence_refs
            if reference.kind == "sample_hash"
        }
        observation_hashes: set[str] = set()
        sample_ids: set[UUID] = set()
        document_ids: set[UUID] = set()
        observation_refs = {
            reference.id: reference
            for reference in context.prepared.evidence_refs
            if reference.kind == "evidence_observation"
        }
        for reference in context.prepared.evidence_refs:
            if reference.kind in {"sample", "sample_association_sample"}:
                try:
                    sample_ids.add(UUID(reference.id))
                except ValueError:
                    return False
            elif reference.kind in {"source_document", "document"}:
                try:
                    document_ids.add(UUID(reference.id))
                except ValueError:
                    return False
        if observation_refs:
            try:
                requested_observation_ids = {UUID(item) for item in observation_refs}
            except ValueError:
                return False
            observations = list(
                await session.scalars(
                    select(EvidenceObservationRow)
                    .where(
                        EvidenceObservationRow.investigation_id
                        == context.authority.investigation_id,
                        EvidenceObservationRow.id.in_(sorted(requested_observation_ids, key=str)),
                    )
                    .with_for_update()
                )
            )
            if {item.id for item in observations} != requested_observation_ids:
                return False
            for observation in observations:
                reference = observation_refs[str(observation.id)]
                if (
                    reference.version != observation.source_version
                    or reference.sha256 != observation.sha256
                ):
                    return False
                object_type = observation.object_type.casefold()
                if object_type in _VT_FILE_OBJECT_TYPES or (
                    object_type == "sha256"
                    and (
                        context.prepared.family in _VT_HASH_READ_FAMILIES
                        or context.prepared.family.startswith(_BRIDGE_FAMILY_PREFIXES)
                    )
                ):
                    if not re.fullmatch(r"[0-9a-f]{64}", observation.object_id):
                        return False
                    observation_hashes.add(observation.object_id)
                elif observation.object_type.casefold() in {"source_document", "document"}:
                    try:
                        document_ids.add(UUID(observation.object_id))
                    except ValueError:
                        return False

        source_hashes = sample_hashes | observation_hashes
        if (
            context.prepared.family in _VT_HASH_READ_FAMILIES
            and sample_hashes
            and not observation_hashes.issubset(sample_hashes)
        ):
            return False
        if source_hashes:
            associations = list(
                await session.scalars(
                    select(SampleAssociationRow)
                    .where(
                        SampleAssociationRow.investigation_id == context.authority.investigation_id,
                        SampleAssociationRow.sample_sha256.in_(sorted(source_hashes)),
                    )
                    .with_for_update()
                )
            )
            associated_hashes = {item.sample_sha256 for item in associations}
            if sample_hashes - (associated_hashes | observation_hashes):
                return False
            if observation_hashes - (associated_hashes | sample_hashes):
                return False
            sample_ids.update(item.sample_id for item in associations if item.sample_id is not None)
        if sample_ids:
            samples = list(
                await session.scalars(
                    select(SampleRow)
                    .where(
                        SampleRow.id.in_(sorted(sample_ids, key=str)),
                        SampleRow.subject_id == context.investigation_subject_id,
                    )
                    .with_for_update()
                )
            )
            if {item.id for item in samples} != sample_ids or any(
                item.tlp.casefold() == "red"
                or _source_flags_block(
                    context.prepared.family,
                    do_not_submit=item.do_not_submit,
                    external_llm_allowed=item.external_llm_allowed,
                )
                or bool((item.license_restriction or "").strip())
                for item in samples
            ):
                return False
        if source_hashes:
            sample_hash_terms = [BlobRow.sha256.in_(sorted(source_hashes))]
            if context.prepared.family in _VT_HASH_READ_FAMILIES:
                sample_hash_terms.extend(
                    (
                        SampleRow.expected_hash.in_(sorted(source_hashes)),
                        SampleRow.source_object_id.in_(sorted(source_hashes)),
                    )
                )
            hash_samples = list(
                await session.scalars(
                    select(SampleRow)
                    .join(BlobRow, BlobRow.id == SampleRow.blob_id)
                    .where(
                        SampleRow.subject_id == context.investigation_subject_id,
                        or_(*sample_hash_terms),
                    )
                    .with_for_update()
                )
            )
            if any(
                item.tlp.casefold() == "red"
                or _source_flags_block(
                    context.prepared.family,
                    do_not_submit=item.do_not_submit,
                    external_llm_allowed=item.external_llm_allowed,
                )
                or bool((item.license_restriction or "").strip())
                for item in hash_samples
            ):
                return False
        if document_ids:
            documents = list(
                await session.scalars(
                    select(SourceDocumentRow)
                    .where(
                        SourceDocumentRow.id.in_(sorted(document_ids, key=str)),
                        SourceDocumentRow.subject_id == context.investigation_subject_id,
                    )
                    .with_for_update()
                )
            )
            if {item.id for item in documents} != document_ids or any(
                item.tlp.casefold() == "red"
                or _source_flags_block(
                    context.prepared.family,
                    do_not_submit=item.do_not_submit,
                    external_llm_allowed=item.external_llm_allowed,
                )
                or bool((item.license_restriction or "").strip())
                for item in documents
            ):
                return False
        return True


__all__ = [
    "VT_EXTERNAL_EFFECT_FAMILIES",
    "SqlAlchemyVirusTotalPolicyGuard",
    "VirusTotalReadReusePolicy",
    "virustotal_external_effect_family_bindings",
]
