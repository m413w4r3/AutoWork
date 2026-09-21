from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.editorial import LegacyEditorialProjectionService
from cti_app.domain.discovery import DiscoveryCandidate
from cti_app.domain.discovery_cumulative import DiscoveryMemberReference, DiscoverySnapshot
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import EditorialGroupStatus
from cti_app.domain.entities import Subject
from cti_app.domain.selection import SelectionAction, SelectionDecision, SubjectDiscoveryOrigin
from tests.selection_support import InMemorySelectionUnitOfWorkFactory, make_selection_fixture


class _Subjects:
    def __init__(self, state: dict) -> None:
        self.state = state

    async def add(self, subject: Subject) -> None:
        self.state[subject.id] = subject

    async def get(self, subject_id):
        return self.state.get(subject_id)

    async def list_for_edition(self, edition_id):
        return [subject for subject in self.state.values() if subject.edition_id == edition_id]


class _Identities:
    def __init__(self, state: dict) -> None:
        self.state = state

    async def resolve_canonical_subject(self, subject_id):
        current = subject_id
        while self.state[current].merged_into_id is not None:
            current = self.state[current].merged_into_id
        return current


class _Groups:
    def __init__(self, state: dict) -> None:
        self.state = state

    async def add(self, group) -> None:
        self.state[group.id] = group

    async def list_for_edition(self, edition_id):
        return [group for group in self.state.values() if group.edition_id == edition_id]

    async def save(self, group) -> None:
        self.state[group.id] = group


class _HumanDecisions:
    def __init__(self, state: list) -> None:
        self.state = state

    async def append(self, decision) -> None:
        self.state.append(decision)

    async def list_for_edition(self, edition_id):
        return [decision for decision in self.state if decision.edition_id == edition_id]


class _Factory:
    def __init__(self) -> None:
        self.base = InMemorySelectionUnitOfWorkFactory()
        self.human_decisions: list = []

    def __call__(self):
        uow = self.base()
        uow.subjects = _Subjects(self.base.subjects)
        uow.discovery_subject_identities = _Identities(self.base.identities)
        uow.editorial_groups = _Groups(self.base.__dict__.setdefault("groups", {}))
        uow.human_decisions = _HumanDecisions(self.human_decisions)
        return uow


def _selected_factory(
    *, with_ioc: bool = False
) -> tuple[_Factory, Edition, DiscoverySnapshot, Subject]:
    factory = _Factory()
    base, edition, snapshot, identity_id = make_selection_fixture(with_ioc=with_ioc)
    factory.base = base
    subject = Subject(
        edition_id=edition.id,
        title="Analyst-owned subject title",
        slug="analyst-owned-subject-title",
        tlp=edition.tlp,
    )
    base.subjects[subject.id] = subject
    decision = SelectionDecision(
        edition_id=edition.id,
        discovery_subject_id=identity_id,
        snapshot_id=snapshot.id,
        snapshot_version=snapshot.version,
        action=SelectionAction.SELECT,
        subject_id=subject.id,
        actor_id="analyst",
        correlation_id="selection-test",
        idempotency_key="selection-test-1",
    )
    base.decisions.append(decision)
    base.origins.append(
        SubjectDiscoveryOrigin(
            subject_id=subject.id,
            edition_id=edition.id,
            discovery_subject_id=identity_id,
            selection_decision_id=decision.id,
            selected_snapshot_id=snapshot.id,
            selected_snapshot_version=snapshot.version,
        )
    )
    return factory, edition, snapshot, subject


@pytest.mark.asyncio
async def test_selected_origin_projects_a_selected_group_without_human_decision() -> None:
    factory, edition, snapshot, subject = _selected_factory()

    groups = await LegacyEditorialProjectionService(factory).synchronize(edition.id)

    assert len(groups) == 1
    assert groups[0].status is EditorialGroupStatus.SELECTED
    assert groups[0].subject_id == subject.id
    assert groups[0].title == subject.title
    assert groups[0].title != snapshot.subjects[0].candidate.title
    assert factory.human_decisions == []


@pytest.mark.asyncio
async def test_undecided_and_ignored_identities_do_not_project() -> None:
    factory = _Factory()
    base, edition, _snapshot, identity_id = make_selection_fixture()
    factory.base = base
    base.decisions.append(
        SelectionDecision(
            edition_id=edition.id,
            discovery_subject_id=identity_id,
            snapshot_id=next(iter(base.snapshots)),
            snapshot_version=1,
            action=SelectionAction.IGNORE,
            subject_id=None,
            actor_id="analyst",
            correlation_id="ignore-test",
            idempotency_key="ignore-test-1",
        )
    )

    groups = await LegacyEditorialProjectionService(factory).synchronize(edition.id)

    assert groups == []
    assert base.subjects == {}


@pytest.mark.asyncio
async def test_new_snapshot_membership_refreshes_candidate_references() -> None:
    factory, edition, snapshot, _subject = _selected_factory()
    service = LegacyEditorialProjectionService(factory)
    first = (await service.synchronize(edition.id))[0]
    topic = deepcopy(snapshot.subjects[0].candidate)
    topic.id = uuid4()
    second_candidate = DiscoveryCandidate.from_candidate_topic(
        topic,
        discovery_run_id=uuid4(),
        discovery_batch_id=uuid4(),
        position=1,
    )
    factory.base.candidates.append(second_candidate)
    member = replace(
        snapshot.subjects[0],
        member_references=(
            *snapshot.subjects[0].member_references,
            DiscoveryMemberReference(topic.id),
        ),
    )
    factory.base.snapshots[snapshot.id] = replace(
        snapshot,
        version=2,
        subjects=(member,),
        snapshot_hash="1" * 64,
    )

    groups = await service.synchronize(edition.id)

    assert groups[0].id == first.id
    assert len(groups[0].candidate_references) == 2


@pytest.mark.asyncio
async def test_ioc_bearing_undecided_identity_creates_no_subject_or_group() -> None:
    factory = _Factory()
    base, edition, _snapshot, _identity_id = make_selection_fixture(with_ioc=True)
    factory.base = base

    groups = await LegacyEditorialProjectionService(factory).synchronize(edition.id)

    assert groups == []
    assert base.subjects == {}
    assert base.decisions == []
