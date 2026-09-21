from datetime import date, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.editions import EditionNotFoundError
from cti_app.application.subjects import (
    SubjectConcurrencyError,
    SubjectNotFoundError,
    SubjectService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionImmutableError, EditionStatus
from cti_app.domain.entities import SLUG_PATTERN, Subject
from cti_app.domain.errors import DomainError, TlpDowngradeError
from tests.subject_support import InMemorySubjectUnitOfWorkFactory


def _edition(*, state: EditionStatus = EditionStatus.OPEN, tlp: TLP = TLP.GREEN) -> Edition:
    return Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        tlp=tlp,
        languages=("fr",),
        state=state,
    )


def _subject(edition_id: UUID, **overrides: object) -> Subject:
    values: dict[str, object] = {
        "edition_id": edition_id,
        "title": " A Subject ",
        "slug": "a-subject-12345678",
        "tlp": TLP.GREEN,
    }
    values.update(overrides)
    return Subject(**values)  # type: ignore[arg-type]


def test_subject_normalizes_title_and_validates_required_fields() -> None:
    subject = _subject(uuid4())
    assert subject.title == "A Subject"
    assert not hasattr(subject, "external_id")
    assert "status" not in subject.__dataclass_fields__
    assert SLUG_PATTERN.fullmatch(subject.slug)

    with pytest.raises(DomainError):
        _subject(uuid4(), title="  ")
    with pytest.raises(DomainError):
        _subject(uuid4(), slug="Not a slug")
    with pytest.raises(DomainError):
        _subject(uuid4(), version=0)


def test_subject_edition_and_slug_are_immutable() -> None:
    edition_id = uuid4()
    subject = _subject(edition_id)
    with pytest.raises(AttributeError):
        subject.edition_id = uuid4()
    with pytest.raises(AttributeError):
        subject.slug = "changed"


def test_subject_tlp_changes_preserve_downgrade_invariant() -> None:
    subject = _subject(uuid4(), tlp=TLP.GREEN)
    original_updated_at = subject.updated_at
    subject.update_metadata(title=subject.title, tlp=TLP.AMBER)
    assert subject.tlp is TLP.AMBER
    assert subject.version == 2
    assert subject.updated_at > original_updated_at

    with pytest.raises(TlpDowngradeError):
        subject.update_metadata(title=subject.title, tlp=TLP.CLEAR)
    assert subject.version == 2


@pytest.mark.asyncio
async def test_materialize_inherits_tlp_and_uses_subject_id_in_slug() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    edition = _edition(tlp=TLP.AMBER)
    factory.state[edition.id] = edition
    service = SubjectService(factory)

    subject = await service.materialize(edition_id=edition.id, title="  Fancy Malware! ")

    assert subject.edition_id == edition.id
    assert subject.title == "Fancy Malware!"
    assert subject.tlp is TLP.AMBER
    assert subject.slug == f"fancy-malware-{subject.id.hex[:8]}"
    assert subject.id != edition.id


@pytest.mark.asyncio
async def test_materialize_accepts_restrictive_initial_tlp_but_rejects_downgrade() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    edition = _edition(tlp=TLP.GREEN)
    factory.state[edition.id] = edition
    service = SubjectService(factory)

    subject = await service.materialize(
        edition_id=edition.id,
        title="Restricted subject",
    )
    assert subject.tlp is TLP.GREEN

    async with factory() as uow:
        restricted = await service.materialize_in_uow(
            uow,
            edition_id=edition.id,
            title="Amber subject",
            initial_tlp=TLP.AMBER,
        )
        assert restricted.tlp is TLP.AMBER
        with pytest.raises(TlpDowngradeError):
            await service.materialize_in_uow(
                uow,
                edition_id=edition.id,
                title="Clear subject",
                initial_tlp=TLP.CLEAR,
            )


@pytest.mark.asyncio
async def test_materialize_rejects_missing_and_archived_editions() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    service = SubjectService(factory)
    with pytest.raises(EditionNotFoundError):
        await service.materialize(edition_id=uuid4(), title="Subject")

    edition = _edition(state=EditionStatus.ARCHIVED)
    factory.state[edition.id] = edition
    with pytest.raises(EditionImmutableError):
        await service.materialize(edition_id=edition.id, title="Subject")


@pytest.mark.asyncio
async def test_get_and_list_verify_edition_but_allow_archived_reads() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    edition = _edition(state=EditionStatus.ARCHIVED)
    factory.state[edition.id] = edition
    first = _subject(edition.id, title="First Subject", slug="first-subject-12345678")
    second = _subject(edition.id, title="Second Subject", slug="second-subject-12345678")
    factory.subject_state.update({first.id: first, second.id: second})
    service = SubjectService(factory)

    assert await service.get(first.id) == first
    assert {item.id for item in await service.list_for_edition(edition.id)} == {
        first.id,
        second.id,
    }
    with pytest.raises(SubjectNotFoundError):
        await service.get(uuid4())
    with pytest.raises(EditionNotFoundError):
        await service.list_for_edition(uuid4())


@pytest.mark.asyncio
async def test_update_metadata_is_optimistic_and_rejects_archived_editions() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    edition = _edition(tlp=TLP.GREEN)
    factory.state[edition.id] = edition
    subject = _subject(edition.id)
    factory.subject_state[subject.id] = subject
    service = SubjectService(factory)

    original_updated_at = subject.updated_at
    original_slug = subject.slug
    updated = await service.update_metadata(
        subject.id,
        expected_version=1,
        title="  Updated Subject ",
        tlp=TLP.AMBER,
        actor_id="analyst:1",
    )
    assert updated.title == "Updated Subject"
    assert updated.slug == original_slug
    assert updated.tlp is TLP.AMBER
    assert updated.version == 2
    assert updated.updated_at > original_updated_at

    # La modification est journalisée par le mécanisme de provenance existant.
    assert [
        (event.event_type, event.actor_id, event.payload["before"], event.payload["after"])
        for event in factory.provenance_events
    ] == [
        (
            "subject.metadata_updated",
            "analyst:1",
            {"title": "A Subject", "tlp": "GREEN"},
            {"title": "Updated Subject", "tlp": "AMBER"},
        )
    ]

    with pytest.raises(SubjectConcurrencyError):
        await service.update_metadata(
            subject.id,
            expected_version=1,
            title="Stale",
            tlp=TLP.AMBER,
            actor_id="analyst:1",
        )

    with pytest.raises(TlpDowngradeError):
        await service.update_metadata(
            subject.id,
            expected_version=2,
            title="Downgraded",
            tlp=TLP.CLEAR,
            actor_id="analyst:1",
        )

    edition.state = EditionStatus.ARCHIVED
    factory.state[edition.id] = edition
    with pytest.raises(EditionImmutableError):
        await service.update_metadata(
            subject.id,
            expected_version=2,
            title="Blocked",
            tlp=TLP.AMBER,
            actor_id="analyst:1",
        )


def test_subject_requires_aware_timestamps() -> None:
    naive = datetime.now().replace(tzinfo=None)
    with pytest.raises(DomainError):
        _subject(uuid4(), created_at=naive)
    with pytest.raises(DomainError):
        _subject(uuid4(), updated_at=naive)
