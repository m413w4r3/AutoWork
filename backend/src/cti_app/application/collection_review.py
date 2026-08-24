from __future__ import annotations

from uuid import UUID

from cti_app.application.blob_storage import BlobStore
from cti_app.application.collection_errors import CollectionItemNotFoundError
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.collection import Claim, Indicator, SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editorial import HumanDecision, HumanDecisionType


class CollectionReviewService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        blob_store: BlobStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store

    async def list_evidence(self, subject_id: UUID) -> tuple[list[Claim], list[Indicator]]:
        async with self._uow_factory() as uow:
            return (
                list(await uow.claims.list_for_subject(subject_id)),
                list(await uow.indicators.list_for_subject(subject_id)),
            )

    async def decisions(self, edition_id: UUID) -> list[HumanDecision]:
        async with self._uow_factory() as uow:
            return list(await uow.human_decisions.list_for_edition(edition_id))

    async def get_claim(self, claim_id: UUID) -> Claim:
        async with self._uow_factory() as uow:
            claim = await uow.claims.get(claim_id)
            if claim is None:
                raise CollectionItemNotFoundError(str(claim_id))
            return claim

    async def extracted_text(self, artifact_id: UUID) -> str:
        async with self._uow_factory() as uow:
            artifact = await uow.derived_artifacts.get(artifact_id)
            if artifact is None:
                raise CollectionItemNotFoundError(str(artifact_id))
            blob = await uow.blobs.get(artifact.text_blob_id)
            if blob is None:
                raise CollectionItemNotFoundError(str(artifact.text_blob_id))
        content = await self._blob_store.read(blob.descriptor, max_bytes=50 * 1024 * 1024)
        return content.decode("utf-8")

    async def decide_claim(
        self,
        claim_id: UUID,
        decision_type: HumanDecisionType,
        *,
        actor_id: str,
        correlation_id: str,
        corrected_value: str | None = None,
    ) -> HumanDecision:
        allowed = {
            HumanDecisionType.CLAIM_VALIDATE,
            HumanDecisionType.CLAIM_CORRECT,
            HumanDecisionType.CLAIM_REJECT,
        }
        if decision_type not in allowed:
            raise ValueError("Invalid claim decision")
        if decision_type is HumanDecisionType.CLAIM_CORRECT and not (
            corrected_value and corrected_value.strip()
        ):
            raise ValueError("A claim correction requires a corrected value")
        async with self._uow_factory() as uow:
            claim = await uow.claims.get(claim_id)
            if claim is None:
                raise CollectionItemNotFoundError(str(claim_id))
            decision = HumanDecision(
                edition_id=claim.edition_id,
                decision_type=decision_type,
                group_ids=(claim.group_id,),
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "claim_id": str(claim.id),
                    "original_value": claim.value,
                    "corrected_value": corrected_value,
                },
            )
            await uow.human_decisions.append(decision)
            await uow.commit()
            return decision

    async def decide_indicator(
        self,
        indicator_id: UUID,
        decision_type: HumanDecisionType,
        *,
        actor_id: str,
        correlation_id: str,
        corrected_value: str | None = None,
    ) -> HumanDecision:
        allowed = {
            HumanDecisionType.INDICATOR_VALIDATE,
            HumanDecisionType.INDICATOR_CORRECT,
            HumanDecisionType.INDICATOR_REJECT,
        }
        if decision_type not in allowed:
            raise ValueError("Invalid indicator decision")
        if decision_type is HumanDecisionType.INDICATOR_CORRECT and not (
            corrected_value and corrected_value.strip()
        ):
            raise ValueError("An indicator correction requires a corrected value")
        async with self._uow_factory() as uow:
            indicator = await uow.indicators.get(indicator_id)
            if indicator is None:
                raise CollectionItemNotFoundError(str(indicator_id))
            decision = HumanDecision(
                edition_id=indicator.edition_id,
                decision_type=decision_type,
                group_ids=(indicator.group_id,),
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "indicator_id": str(indicator.id),
                    "original_value": indicator.normalized_value,
                    "corrected_value": corrected_value,
                },
            )
            await uow.human_decisions.append(decision)
            await uow.commit()
            return decision

    async def decide_relationship(
        self,
        collection_id: UUID,
        role: SourceRole,
        *,
        actor_id: str,
        correlation_id: str,
    ) -> SourceCollection:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            previous_role = collection.proposed_role
            collection.correct_relationship(role, actor_id=actor_id)
            await uow.source_collections.save(collection)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=collection.edition_id,
                    decision_type=(
                        HumanDecisionType.SOURCE_RELATIONSHIP_VALIDATE
                        if previous_role is role
                        else HumanDecisionType.SOURCE_RELATIONSHIP_CORRECT
                    ),
                    group_ids=(collection.group_id,),
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={
                        "source_collection_id": str(collection.id),
                        "previous_role": previous_role.value,
                        "role": role.value,
                    },
                )
            )
            await uow.commit()
            return collection
