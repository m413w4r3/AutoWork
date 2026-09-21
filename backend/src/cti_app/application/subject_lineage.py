from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cti_app.application.collection_errors import CollectionNotAllowedError
from cti_app.application.persistence import (
    DiscoveryCandidateRepository,
    DiscoverySnapshotRepository,
    DiscoverySubjectIdentityRepository,
    SubjectDiscoveryOriginRepository,
    SubjectRepository,
)
from cti_app.domain.discovery import DiscoveryCandidate
from cti_app.domain.discovery_cumulative import DiscoverySnapshot, DiscoverySubject
from cti_app.domain.entities import Subject
from cti_app.domain.selection import SubjectDiscoveryOrigin


class SubjectDiscoveryLineageUnitOfWork(Protocol):
    """Narrow port shared by Collection and Production to resolve a lineage.

    Both the generic and the Production unit of work satisfy it structurally,
    so neither capability has to depend on the other's full contract.
    """

    subjects: SubjectRepository
    subject_discovery_origins: SubjectDiscoveryOriginRepository
    discovery_subject_identities: DiscoverySubjectIdentityRepository
    discovery_snapshots: DiscoverySnapshotRepository
    discovery_candidates: DiscoveryCandidateRepository


@dataclass(frozen=True, slots=True)
class SubjectDiscoveryLineage:
    subject: Subject
    origin: SubjectDiscoveryOrigin
    snapshot: DiscoverySnapshot
    origin_discovery_subject_id: UUID
    canonical_discovery_subject_id: UUID
    discovery_subject: DiscoverySubject
    members: tuple[DiscoveryCandidate, ...]


async def resolve_subject_discovery_lineage(
    uow: SubjectDiscoveryLineageUnitOfWork,
    subject_id: UUID,
    edition_id: UUID | None = None,
) -> SubjectDiscoveryLineage:
    subject = await uow.subjects.get(subject_id)
    if subject is None:
        raise CollectionNotAllowedError(f"Subject {subject_id} does not exist")
    if edition_id is not None and subject.edition_id != edition_id:
        raise CollectionNotAllowedError("Subject does not belong to the requested edition")

    origin = await uow.subject_discovery_origins.get_by_subject(subject_id)
    if origin is None or origin.subject_id != subject_id or origin.edition_id != subject.edition_id:
        raise CollectionNotAllowedError("Subject discovery origin is missing")

    canonical_id = await uow.discovery_subject_identities.resolve_canonical_subject(
        origin.discovery_subject_id
    )
    snapshot = await uow.discovery_snapshots.get_active(subject.edition_id)
    if snapshot is None:
        raise CollectionNotAllowedError("Active discovery snapshot is missing")

    matches = tuple(item for item in snapshot.subjects if item.subject_id == canonical_id)
    if len(matches) != 1:
        raise CollectionNotAllowedError("Canonical discovery subject is not unique in the snapshot")
    discovery_subject = matches[0]

    candidates = {
        candidate.id: candidate
        for candidate in await uow.discovery_candidates.list_for_edition(
            subject.edition_id, include_replaced=False
        )
    }
    members: list[DiscoveryCandidate] = []
    for reference in discovery_subject.member_references:
        candidate = candidates.get(reference.candidate_id)
        if candidate is None:
            raise CollectionNotAllowedError(
                f"Discovery candidate {reference.candidate_id} is missing from the edition"
            )
        members.append(candidate)

    return SubjectDiscoveryLineage(
        subject=subject,
        origin=origin,
        snapshot=snapshot,
        origin_discovery_subject_id=origin.discovery_subject_id,
        canonical_discovery_subject_id=canonical_id,
        discovery_subject=discovery_subject,
        members=tuple(members),
    )
