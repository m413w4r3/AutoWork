from __future__ import annotations

from datetime import datetime
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import utc_now
from cti_app.domain.reference_corpus import (
    ReferenceCorpusAssessment,
    ReferenceLabelSource,
    ReferenceMember,
    assess_reference_feature,
)


class ReferenceCorpusService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def promote(
        self,
        *,
        sample_id: UUID,
        sample_sha256: str,
        family_label: str,
        actor_id: str,
        label_source: ReferenceLabelSource,
        origin_investigation_id: UUID | None = None,
        promoted_at: datetime | None = None,
    ) -> ReferenceMember:
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
            corpus_size = await uow.reference_members.count_eligible_malware_samples()
            return assess_reference_feature(
                feature_kind=feature_kind,
                normalized_value=normalized_value,
                malware_members=members,
                benign_sample_occurrences=benign,
                malware_corpus_size=corpus_size,
                min_family_samples=min_family_samples,
            )
