from __future__ import annotations

from datetime import datetime
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import utc_now
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.reference_corpus import (
    ReferenceCorpusAssessment,
    ReferenceLabelSource,
    ReferenceMember,
    ReferenceMemberDispute,
    assess_reference_feature,
)


class ReferenceCorpusService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def promote(
        self,
        *,
        sample_id: UUID,
        family_label: str,
        actor_id: str,
        label_source: ReferenceLabelSource,
        origin_investigation_id: UUID | None = None,
        promoted_at: datetime | None = None,
    ) -> ReferenceMember:
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None:
                raise EntityNotFoundError(f"Sample {sample_id} does not exist")
            blob = await uow.blobs.get(sample.blob_id)
            if blob is None:
                raise EntityNotFoundError(f"Blob {sample.blob_id} does not exist")
            sample_sha256 = blob.descriptor.sha256
        member = ReferenceMember(
            sample_id=sample_id,
            sample_sha256=sample_sha256,
            family_label=family_label,
            actor_id=actor_id,
            label_source=label_source,
            origin_investigation_id=origin_investigation_id,
            promoted_at=promoted_at or utc_now(),
        )
        async with self._uow_factory() as uow:
            result = await uow.reference_members.append(member)
            await uow.commit()
            return result

    async def dispute(
        self,
        *,
        member_id: UUID,
        reason: str,
        actor_id: str,
        created_at: datetime | None = None,
    ) -> None:
        dispute = ReferenceMemberDispute(
            member_id=member_id,
            reason=reason,
            actor_id=actor_id,
            created_at=created_at or utc_now(),
        )
        async with self._uow_factory() as uow:
            if await uow.reference_members.get(member_id) is None:
                raise EntityNotFoundError(f"Reference member {member_id} does not exist")
            await uow.reference_members.append_dispute(dispute)
            await uow.commit()

    async def assess(
        self,
        *,
        feature_kind: str,
        normalized_value: str,
        min_family_samples: int = 5,
    ) -> ReferenceCorpusAssessment:
        async with self._uow_factory() as uow:
            members = await uow.reference_members.list_feature_members(
                feature_kind, normalized_value
            )
            benign = await uow.reference_members.count_benign_feature_occurrences(
                feature_kind, normalized_value
            )
            family_sizes = await uow.reference_members.count_eligible_malware_samples_by_family()
            return assess_reference_feature(
                feature_kind=feature_kind,
                normalized_value=normalized_value,
                malware_members=members,
                benign_sample_occurrences=benign,
                total_eligible_samples_by_family=family_sizes,
                min_family_samples=min_family_samples,
            )
