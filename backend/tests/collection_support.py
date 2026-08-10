from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from typing import cast
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.briefs import BriefDraft, BriefEvidencePack
from cti_app.domain.collection import (
    Claim,
    CollectionAttempt,
    CollectionPolicySnapshot,
    DerivedArtifact,
    Indicator,
    RejectedModelProposal,
    SourceCollection,
)
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import EditorialGroup, HumanDecision
from cti_app.domain.entities import ProvenanceEvent, SourceDocument, Subject
from tests.discovery_support import InMemoryDiscoveryBatchRepository
from tests.editorial_support import (
    InMemoryEditorialGroupRepository,
    InMemoryHumanDecisionRepository,
    InMemorySubjectRepository,
)


class InMemoryBlobRepository:
    def __init__(self, blobs: dict[UUID, BlobRecord]) -> None:
        self._blobs = blobs

    async def add(self, blob: BlobRecord) -> None:
        self._blobs[blob.id] = blob

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self._blobs.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return next(
            (
                item
                for item in self._blobs.values()
                if item.descriptor.logical_bucket == logical_bucket
                and item.descriptor.sha256 == sha256
            ),
            None,
        )

    async def count_references(self, blob_id: UUID) -> int:
        del blob_id
        return 0

    async def delete(self, blob_id: UUID) -> None:
        self._blobs.pop(blob_id, None)


class InMemorySourceDocumentRepository:
    def __init__(self, documents: dict[UUID, SourceDocument]) -> None:
        self._documents = documents

    async def add(self, document: SourceDocument) -> None:
        self._documents[document.id] = deepcopy(document)

    async def get(self, document_id: UUID) -> SourceDocument | None:
        value = self._documents.get(document_id)
        return deepcopy(value) if value else None

    async def list_for_subject(self, subject_id: UUID) -> list[SourceDocument]:
        return [
            deepcopy(item) for item in self._documents.values() if item.subject_id == subject_id
        ]


class InMemorySourceCollectionRepository:
    def __init__(self, collections: dict[UUID, SourceCollection]) -> None:
        self._collections = collections

    async def add_if_absent(self, collection: SourceCollection) -> bool:
        if any(
            item.subject_id == collection.subject_id
            and item.source_candidate_id == collection.source_candidate_id
            for item in self._collections.values()
        ):
            return False
        self._collections[collection.id] = deepcopy(collection)
        return True

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        value = self._collections.get(collection_id)
        return deepcopy(value) if value else None

    async def get_for_update(self, collection_id: UUID) -> SourceCollection | None:
        return await self.get(collection_id)

    async def get_by_candidate(
        self, subject_id: UUID, source_candidate_id: UUID
    ) -> SourceCollection | None:
        value = next(
            (
                item
                for item in self._collections.values()
                if item.subject_id == subject_id and item.source_candidate_id == source_candidate_id
            ),
            None,
        )
        return deepcopy(value) if value else None

    async def list_for_subject(self, subject_id: UUID) -> list[SourceCollection]:
        return [
            deepcopy(item) for item in self._collections.values() if item.subject_id == subject_id
        ]

    async def save(self, collection: SourceCollection) -> None:
        self._collections[collection.id] = deepcopy(collection)


class InMemoryAttemptRepository:
    def __init__(self, attempts: list[CollectionAttempt]) -> None:
        self._attempts = attempts

    async def append(self, attempt: CollectionAttempt) -> None:
        self._attempts.append(deepcopy(attempt))

    async def list_for_collection(self, collection_id: UUID) -> list[CollectionAttempt]:
        return [item for item in self._attempts if item.collection_id == collection_id]


class InMemoryPolicySnapshotRepository:
    def __init__(self, snapshots: dict[str, CollectionPolicySnapshot]) -> None:
        self._snapshots = snapshots

    async def add_if_absent(self, snapshot: CollectionPolicySnapshot) -> bool:
        if snapshot.id in self._snapshots:
            return False
        self._snapshots[snapshot.id] = deepcopy(snapshot)
        return True

    async def get(self, snapshot_id: str) -> CollectionPolicySnapshot | None:
        return self._snapshots.get(snapshot_id)


class InMemoryArtifactRepository:
    def __init__(self, artifacts: dict[UUID, DerivedArtifact]) -> None:
        self._artifacts = artifacts

    async def append(self, artifact: DerivedArtifact) -> None:
        self._artifacts[artifact.id] = artifact

    async def get(self, artifact_id: UUID) -> DerivedArtifact | None:
        return self._artifacts.get(artifact_id)


class InMemoryClaimRepository:
    def __init__(self, claims: dict[UUID, Claim]) -> None:
        self._claims = claims

    async def append_many(self, claims: list[Claim] | tuple[Claim, ...]) -> None:
        self._claims.update({item.id: item for item in claims})

    async def get(self, claim_id: UUID) -> Claim | None:
        return self._claims.get(claim_id)

    async def list_for_subject(self, subject_id: UUID) -> list[Claim]:
        return [item for item in self._claims.values() if item.subject_id == subject_id]


class InMemoryIndicatorRepository:
    def __init__(self, indicators: dict[UUID, Indicator]) -> None:
        self._indicators = indicators

    async def append_many(self, indicators: list[Indicator] | tuple[Indicator, ...]) -> None:
        self._indicators.update({item.id: item for item in indicators})

    async def get(self, indicator_id: UUID) -> Indicator | None:
        return self._indicators.get(indicator_id)

    async def list_for_subject(self, subject_id: UUID) -> list[Indicator]:
        return [item for item in self._indicators.values() if item.subject_id == subject_id]


class InMemoryRejectedProposalRepository:
    def __init__(self, proposals: list[RejectedModelProposal]) -> None:
        self._proposals = proposals

    async def append_many(
        self, proposals: list[RejectedModelProposal] | tuple[RejectedModelProposal, ...]
    ) -> None:
        self._proposals.extend(deepcopy(list(proposals)))


class InMemoryBriefPackRepository:
    def __init__(self, packs: dict[UUID, BriefEvidencePack]) -> None:
        self._packs = packs

    async def append(self, pack: BriefEvidencePack) -> None:
        self._packs[pack.id] = deepcopy(pack)

    async def get(self, pack_id: UUID) -> BriefEvidencePack | None:
        return deepcopy(self._packs.get(pack_id))

    async def get_current(self, subject_id: UUID) -> BriefEvidencePack | None:
        values = [item for item in self._packs.values() if item.subject_id == subject_id]
        return deepcopy(max(values, key=lambda item: item.version)) if values else None

    async def get_by_hash(self, subject_id: UUID, content_hash: str) -> BriefEvidencePack | None:
        value = next(
            (
                item
                for item in self._packs.values()
                if item.subject_id == subject_id and item.content_hash == content_hash
            ),
            None,
        )
        return deepcopy(value)

    async def list_for_subject(self, subject_id: UUID) -> list[BriefEvidencePack]:
        return sorted(
            [deepcopy(item) for item in self._packs.values() if item.subject_id == subject_id],
            key=lambda item: item.version,
        )


class InMemoryBriefDraftRepository:
    def __init__(self, drafts: dict[UUID, BriefDraft]) -> None:
        self._drafts = drafts

    async def append(self, draft: BriefDraft) -> None:
        self._drafts[draft.id] = deepcopy(draft)

    async def get(self, draft_id: UUID) -> BriefDraft | None:
        return deepcopy(self._drafts.get(draft_id))

    async def get_current(self, subject_id: UUID) -> BriefDraft | None:
        values = [item for item in self._drafts.values() if item.subject_id == subject_id]
        return deepcopy(max(values, key=lambda item: item.version)) if values else None

    async def list_for_subject(self, subject_id: UUID) -> list[BriefDraft]:
        return sorted(
            [deepcopy(item) for item in self._drafts.values() if item.subject_id == subject_id],
            key=lambda item: item.version,
        )


class InMemoryProvenanceRepository:
    def __init__(self, events: list[ProvenanceEvent]) -> None:
        self._events = events

    async def append(self, event: ProvenanceEvent) -> None:
        self._events.append(event)

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> list[ProvenanceEvent]:
        return [
            item
            for item in self._events
            if item.aggregate_type == aggregate_type and item.aggregate_id == aggregate_id
        ]


class InMemoryCollectionUnitOfWork:
    def __init__(self, factory: InMemoryCollectionUnitOfWorkFactory) -> None:
        self.blobs = InMemoryBlobRepository(factory.blobs)
        self.subjects = InMemorySubjectRepository(factory.subjects)
        self.source_documents = InMemorySourceDocumentRepository(factory.documents)
        self.provenance = InMemoryProvenanceRepository(factory.provenance)
        self.discovery_batches = InMemoryDiscoveryBatchRepository(factory.batches)
        self.editorial_groups = InMemoryEditorialGroupRepository(factory.groups, factory.editions)
        self.human_decisions = InMemoryHumanDecisionRepository(factory.decisions)
        self.source_collections = InMemorySourceCollectionRepository(factory.collections)
        self.collection_attempts = InMemoryAttemptRepository(factory.attempts)
        self.collection_policy_snapshots = InMemoryPolicySnapshotRepository(factory.snapshots)
        self.derived_artifacts = InMemoryArtifactRepository(factory.artifacts)
        self.claims = InMemoryClaimRepository(factory.claims)
        self.indicators = InMemoryIndicatorRepository(factory.indicators)
        self.rejected_model_proposals = InMemoryRejectedProposalRepository(
            factory.rejected_proposals
        )
        self.brief_evidence_packs = InMemoryBriefPackRepository(factory.brief_packs)
        self.brief_drafts = InMemoryBriefDraftRepository(factory.brief_drafts)

    async def __aenter__(self) -> InMemoryCollectionUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class InMemoryCollectionUnitOfWorkFactory:
    def __init__(self) -> None:
        self.blobs: dict[UUID, BlobRecord] = {}
        self.subjects: dict[UUID, Subject] = {}
        self.documents: dict[UUID, SourceDocument] = {}
        self.provenance: list[ProvenanceEvent] = []
        self.batches: dict[UUID, DiscoveryBatch] = {}
        self.groups: dict[UUID, EditorialGroup] = {}
        self.editions: dict[UUID, Edition] = {}
        self.decisions: list[HumanDecision] = []
        self.collections: dict[UUID, SourceCollection] = {}
        self.attempts: list[CollectionAttempt] = []
        self.snapshots: dict[str, CollectionPolicySnapshot] = {}
        self.artifacts: dict[UUID, DerivedArtifact] = {}
        self.claims: dict[UUID, Claim] = {}
        self.indicators: dict[UUID, Indicator] = {}
        self.rejected_proposals: list[RejectedModelProposal] = []
        self.brief_packs: dict[UUID, BriefEvidencePack] = {}
        self.brief_drafts: dict[UUID, BriefDraft] = {}

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, InMemoryCollectionUnitOfWork(self))
