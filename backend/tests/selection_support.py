from __future__ import annotations

from datetime import date
from types import TracebackType
from uuid import UUID, uuid4

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryCandidate,
    DiscoveryCandidateEvidence,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import ProvenanceEvent, Subject
from cti_app.domain.selection import (
    SelectionDecision,
    SelectionIdempotencyRecord,
    SubjectDiscoveryOrigin,
)


class _EditionRepository:
    def __init__(self, state: dict[UUID, Edition]) -> None:
        self.state = state

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.state.get(edition_id)

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return self.state.get(edition_id)


class _SubjectRepository:
    def __init__(self, state: dict[UUID, Subject]) -> None:
        self.state = state

    async def add(self, subject: Subject) -> None:
        self.state[subject.id] = subject

    async def list_for_edition(self, edition_id: UUID) -> list[Subject]:
        return [subject for subject in self.state.values() if subject.edition_id == edition_id]


class _DecisionRepository:
    def __init__(self, state: list[SelectionDecision]) -> None:
        self.state = state

    async def append(self, decision: SelectionDecision) -> None:
        self.state.append(decision)

    async def list_for_edition(self, edition_id: UUID) -> list[SelectionDecision]:
        return [decision for decision in self.state if decision.edition_id == edition_id]


class _IdempotencyRepository:
    def __init__(self, state: list[SelectionIdempotencyRecord]) -> None:
        self.state = state

    async def get_for_update(
        self, edition_id: UUID, idempotency_key: str
    ) -> SelectionIdempotencyRecord | None:
        return next(
            (
                record
                for record in self.state
                if record.edition_id == edition_id and record.idempotency_key == idempotency_key
            ),
            None,
        )

    async def add(self, record: SelectionIdempotencyRecord) -> None:
        self.state.append(record)


class _OriginRepository:
    def __init__(self, state: list[SubjectDiscoveryOrigin]) -> None:
        self.state = state

    async def add(self, origin: SubjectDiscoveryOrigin) -> None:
        self.state.append(origin)

    async def list_for_edition(self, edition_id: UUID) -> list[SubjectDiscoveryOrigin]:
        return [origin for origin in self.state if origin.edition_id == edition_id]


class _IdentityRepository:
    def __init__(self, state: dict[UUID, DiscoverySubjectIdentity]) -> None:
        self.state = state

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoverySubjectIdentity]:
        return [identity for identity in self.state.values() if identity.edition_id == edition_id]

    async def get_for_update(self, subject_id: UUID) -> DiscoverySubjectIdentity | None:
        return self.state.get(subject_id)


class _CandidateRepository:
    def __init__(self, state: list[DiscoveryCandidate]) -> None:
        self.state = state

    async def list_for_edition(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryCandidate]:
        return list(self.state)


class _SnapshotRepository:
    def __init__(self, state: dict[UUID, DiscoverySnapshot]) -> None:
        self.state = state

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None:
        return next(
            (
                snapshot
                for snapshot in self.state.values()
                if snapshot.edition_id == edition_id and snapshot.is_active
            ),
            None,
        )

    async def get_active_for_update(self, edition_id: UUID) -> DiscoverySnapshot | None:
        return await self.get_active(edition_id)

    async def get(self, snapshot_id: UUID) -> DiscoverySnapshot | None:
        return self.state.get(snapshot_id)


class _MergeRepository:
    def __init__(self, state: list[DiscoveryMergeRun]) -> None:
        self.state = state

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryMergeRun]:
        return [run for run in self.state if run.edition_id == edition_id]


class _ProvenanceRepository:
    def __init__(self, state: list[ProvenanceEvent]) -> None:
        self.state = state

    async def append(self, event: ProvenanceEvent) -> None:
        self.state.append(event)


class InMemorySelectionUnitOfWork:
    def __init__(self, factory: InMemorySelectionUnitOfWorkFactory) -> None:
        self.editions = _EditionRepository(factory.editions)
        self.subjects = _SubjectRepository(factory.subjects)
        self.selection_decisions = _DecisionRepository(factory.decisions)
        self.selection_idempotency = _IdempotencyRepository(factory.idempotency_records)
        self.subject_discovery_origins = _OriginRepository(factory.origins)
        self.discovery_subject_identities = _IdentityRepository(factory.identities)
        self.discovery_candidates = _CandidateRepository(factory.candidates)
        self.discovery_snapshots = _SnapshotRepository(factory.snapshots)
        self.discovery_merge_runs = _MergeRepository(factory.merge_runs)
        self.provenance = _ProvenanceRepository(factory.provenance_events)
        self._factory = factory

    async def __aenter__(self) -> InMemorySelectionUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self._factory.commit_count += 1

    async def rollback(self) -> None:
        return None


class InMemorySelectionUnitOfWorkFactory:
    def __init__(self) -> None:
        self.editions: dict[UUID, Edition] = {}
        self.subjects: dict[UUID, Subject] = {}
        self.decisions: list[SelectionDecision] = []
        self.idempotency_records: list[SelectionIdempotencyRecord] = []
        self.origins: list[SubjectDiscoveryOrigin] = []
        self.identities: dict[UUID, DiscoverySubjectIdentity] = {}
        self.candidates: list[DiscoveryCandidate] = []
        self.snapshots: dict[UUID, DiscoverySnapshot] = {}
        self.merge_runs: list[DiscoveryMergeRun] = []
        self.provenance_events: list[ProvenanceEvent] = []
        self.commit_count = 0

    def __call__(self) -> InMemorySelectionUnitOfWork:
        return InMemorySelectionUnitOfWork(self)


def make_selection_fixture(
    *, tlp: TLP = TLP.GREEN, candidate_tlp: TLP = TLP.GREEN, with_ioc: bool = False
) -> tuple[InMemorySelectionUnitOfWorkFactory, Edition, DiscoverySnapshot, UUID]:
    factory, edition, snapshot, identity_ids = make_selection_fixture_with_subjects(
        tlp=tlp, candidate_tlp=candidate_tlp, with_ioc=with_ioc
    )
    return factory, edition, snapshot, identity_ids[0]


def make_selection_fixture_with_subjects(
    *,
    count: int = 1,
    tlp: TLP = TLP.GREEN,
    candidate_tlp: TLP = TLP.GREEN,
    with_ioc: bool = False,
) -> tuple[InMemorySelectionUnitOfWorkFactory, Edition, DiscoverySnapshot, tuple[UUID, ...]]:
    factory = InMemorySelectionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        tlp=tlp,
        languages=("fr",),
    )
    merge_run_id = uuid4()
    discovery_subjects: list[DiscoverySubject] = []
    identity_ids: list[UUID] = []
    for position in range(count):
        candidate = DiscoveryCandidate(
            discovery_run_id=uuid4(),
            discovery_batch_id=uuid4(),
            position=position,
            title=f"Canonical topic {position}",
            summary="A discovery summary",
            novelty="A novel finding",
            technical_potential=3,
            technical_potential_reason="Useful for hunting",
            event_date=None,
            actor_or_campaign="Actor",
            context_only=False,
            tlp=candidate_tlp,
            sensitivity="normal",
            external_llm_allowed=True,
            evidence=DiscoveryCandidateEvidence(iocs=("1.2.3.4",) if with_ioc else ()),
        )
        identity = DiscoverySubjectIdentity(
            edition_id=edition.id,
            origin_key=f"topic-{position}",
            created_by_merge_run_id=merge_run_id,
            id=uuid4(),
        )
        topic = CandidateTopic(
            title=candidate.title,
            summary=candidate.summary,
            novelty=candidate.novelty,
            technical_potential=candidate.technical_potential,
            uncertainties=("Needs verification",),
            relevance_reasons=("Relevant",),
            actors=("Actor",),
            campaigns=(),
            malware=(),
            cves=(),
            victims=(),
            sectors=(),
            countries=("FR",),
            likely_artifacts=("mutex",),
            sources=[],
            tlp=candidate_tlp,
            sensitivity="normal",
            external_llm_allowed=True,
            iocs=("1.2.3.4",) if with_ioc else (),
            actor_or_campaign="Actor",
            technical_potential_reason=candidate.technical_potential_reason,
            id=identity.id,
        )
        discovery_subjects.append(
            DiscoverySubject(
                subject_id=identity.id,
                candidate=topic,
                member_references=(DiscoveryMemberReference(candidate.id),),
                created_at=candidate.created_at,
            )
        )
        identity_ids.append(identity.id)
        factory.identities[identity.id] = identity
        factory.candidates.append(candidate)

    snapshot = DiscoverySnapshot(
        edition_id=edition.id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=merge_run_id,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        subjects=tuple(discovery_subjects),
        snapshot_hash="0" * 64,
        is_active=True,
    )
    factory.editions[edition.id] = edition
    factory.snapshots[snapshot.id] = snapshot
    factory.merge_runs.append(
        DiscoveryMergeRun(
            edition_id=edition.id,
            parent_snapshot_id=None,
            intake_id=None,
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            prompt_version="1",
            policy_version="1",
            blocking_version="1",
            merge_input_hash="1" * 64,
            handle_map={},
            included_subject_ids=tuple(identity_ids),
            excluded_subject_count=0,
            validation_status=MergeValidationStatus.VALID,
            id=merge_run_id,
        )
    )
    return factory, edition, snapshot, tuple(identity_ids)
