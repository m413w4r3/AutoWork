from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from cti_app.application.discovery_identity import normalize
from cti_app.application.editions import EditionNotFoundError
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import EditionImmutableError, EditionStatus
from cti_app.domain.entities import Subject


class SubjectNotFoundError(LookupError):
    pass


class SubjectConcurrencyError(RuntimeError):
    pass


def _subject_slug(title: str, subject_id: UUID) -> str:
    base = "-".join(normalize(title).split())[:100].strip("-") or "subject"
    return f"{base}-{subject_id.hex[:8]}"


class SubjectService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def materialize_in_uow(
        self, uow: UnitOfWork, *, edition_id: UUID, title: str
    ) -> Subject:
        edition = await uow.editions.get_for_update(edition_id)
        if edition is None:
            raise EditionNotFoundError(str(edition_id))
        if edition.state is EditionStatus.ARCHIVED:
            raise EditionImmutableError("Archived editions cannot be modified")

        subject_id = uuid4()
        subject = Subject(
            edition_id=edition.id,
            title=title,
            slug=_subject_slug(title, subject_id),
            tlp=edition.tlp,
            id=subject_id,
        )
        await uow.subjects.add(subject)
        return subject

    async def materialize(self, edition_id: UUID, title: str) -> Subject:
        async with self._uow_factory() as uow:
            subject = await self.materialize_in_uow(uow, edition_id=edition_id, title=title)
            await uow.commit()
            return subject

    async def get(self, subject_id: UUID) -> Subject:
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise SubjectNotFoundError(str(subject_id))
            return subject

    async def list_for_edition(self, edition_id: UUID) -> Sequence[Subject]:
        async with self._uow_factory() as uow:
            if await uow.editions.get(edition_id) is None:
                raise EditionNotFoundError(str(edition_id))
            return list(await uow.subjects.list_for_edition(edition_id))

    async def update_metadata(
        self,
        subject_id: UUID,
        *,
        expected_version: int,
        title: str,
        tlp: TLP,
    ) -> Subject:
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise SubjectNotFoundError(str(subject_id))
            if subject.version != expected_version:
                raise SubjectConcurrencyError("Subject was modified by another request")

            edition = await uow.editions.get_for_update(subject.edition_id)
            if edition is None:
                raise EditionNotFoundError(str(subject.edition_id))
            if edition.state is EditionStatus.ARCHIVED:
                raise EditionImmutableError("Archived editions cannot be modified")

            subject.update_metadata(title=title, tlp=tlp)
            if not await uow.subjects.update(subject, expected_version=expected_version):
                raise SubjectConcurrencyError("Subject was modified by another request")
            await uow.commit()
            return subject
