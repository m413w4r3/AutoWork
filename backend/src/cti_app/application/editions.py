from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from cti_app.application.persistence import EditionUnitOfWork, EditionUnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus


class EditionNotFoundError(LookupError):
    pass


class DuplicateEditionError(ValueError):
    def __init__(self, existing_edition_id: UUID | None = None) -> None:
        super().__init__("An edition already exists for this country and period")
        self.existing_edition_id = existing_edition_id


class EditionConcurrencyError(RuntimeError):
    pass


class PreviousEditionError(ValueError):
    pass


class EditionTransitionRequiresUseCaseError(ValueError):
    """Raised when the generic transition API would bypass a workflow use case."""

    code = "edition_transition_requires_use_case"

    def __init__(self, source: EditionStatus, target: EditionStatus) -> None:
        super().__init__(
            f"Transition from {source.value} to {target.value} must be performed by its use case"
        )


_USE_CASE_OWNED_TRANSITIONS = {
    (EditionStatus.SELECTION, EditionStatus.PRODUCTION),
    (EditionStatus.PRODUCTION, EditionStatus.REVIEW),
    (EditionStatus.REVIEW, EditionStatus.PRODUCTION),
    (EditionStatus.REVIEW, EditionStatus.ASSEMBLING),
    (EditionStatus.ASSEMBLING, EditionStatus.REVIEW),
    (EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED),
}


@dataclass(frozen=True, slots=True)
class EditionPage:
    items: list[Edition]
    total: int
    page: int
    page_size: int


class EditionService:
    def __init__(self, uow_factory: EditionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def create(
        self,
        *,
        country: str,
        country_code: str,
        period_start: date,
        period_end: date,
        tlp: TLP,
        languages: tuple[str, ...],
        target_articles: int,
        previous_edition_id: UUID | None,
        source_profile: str,
        actor_id: str,
        correlation_id: str,
    ) -> Edition:
        edition = Edition(
            country=country,
            country_code=country_code,
            period_start=period_start,
            period_end=period_end,
            tlp=tlp,
            languages=languages,
            target_articles=target_articles,
            previous_edition_id=previous_edition_id,
            source_profile=source_profile,
        )
        async with self._uow_factory() as uow:
            await self._validate_previous(uow, previous_edition_id, edition.id)
            if not await uow.editions.add_if_absent(edition):
                existing = await uow.editions.get_by_logical_key(
                    edition.country_code, edition.period_start, edition.period_end
                )
                raise DuplicateEditionError(existing.id if existing else None)
            await uow.edition_audit.append(
                EditionAuditEvent(
                    edition_id=edition.id,
                    actor_id=actor_id,
                    action="edition.created",
                    before=None,
                    after=edition.snapshot(),
                    correlation_id=correlation_id,
                )
            )
            await uow.commit()
        return edition

    async def get(self, edition_id: UUID) -> Edition:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionNotFoundError(str(edition_id))
            return edition

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        country_code: str | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        status: EditionStatus | None = None,
    ) -> EditionPage:
        async with self._uow_factory() as uow:
            editions, total = await uow.editions.list(
                offset=(page - 1) * page_size,
                limit=page_size,
                country_code=country_code.upper() if country_code else None,
                period_start=period_start,
                period_end=period_end,
                status=status,
            )
            return EditionPage(list(editions), total, page, page_size)

    async def update(
        self,
        edition_id: UUID,
        *,
        expected_version: int,
        country: str,
        country_code: str,
        period_start: date,
        period_end: date,
        tlp: TLP,
        languages: tuple[str, ...],
        target_articles: int,
        previous_edition_id: UUID | None,
        source_profile: str,
        actor_id: str,
        correlation_id: str,
    ) -> Edition:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionNotFoundError(str(edition_id))
            if edition.version != expected_version:
                raise EditionConcurrencyError("Edition was modified by another request")
            await self._validate_previous(uow, previous_edition_id, edition.id)
            before = edition.snapshot()
            edition.update_metadata(
                country=country,
                country_code=country_code,
                period_start=period_start,
                period_end=period_end,
                tlp=tlp,
                languages=languages,
                target_articles=target_articles,
                previous_edition_id=previous_edition_id,
                source_profile=source_profile,
            )
            await self._ensure_logical_key_available(uow, edition)
            if not await uow.editions.update(edition, expected_version):
                raise EditionConcurrencyError("Edition was modified by another request")
            await uow.edition_audit.append(
                EditionAuditEvent(
                    edition_id=edition.id,
                    actor_id=actor_id,
                    action="edition.updated",
                    before=before,
                    after=edition.snapshot(),
                    correlation_id=correlation_id,
                )
            )
            await uow.commit()
            return edition

    async def transition(
        self,
        edition_id: UUID,
        *,
        target: EditionStatus,
        expected_version: int,
        actor_id: str,
        correlation_id: str,
    ) -> Edition:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionNotFoundError(str(edition_id))
            if edition.version != expected_version:
                raise EditionConcurrencyError("Edition was modified by another request")
            if (edition.status, target) in _USE_CASE_OWNED_TRANSITIONS:
                raise EditionTransitionRequiresUseCaseError(edition.status, target)
            before = edition.snapshot()
            edition.transition(target)
            if not await uow.editions.update(edition, expected_version):
                raise EditionConcurrencyError("Edition was modified by another request")
            await uow.edition_audit.append(
                EditionAuditEvent(
                    edition_id=edition.id,
                    actor_id=actor_id,
                    action="edition.transitioned",
                    before=before,
                    after=edition.snapshot(),
                    correlation_id=correlation_id,
                )
            )
            await uow.commit()
            return edition

    async def audit(self, edition_id: UUID) -> Sequence[EditionAuditEvent]:
        async with self._uow_factory() as uow:
            if await uow.editions.get(edition_id) is None:
                raise EditionNotFoundError(str(edition_id))
            return list(await uow.edition_audit.list_for_edition(edition_id))

    async def delete(self, edition_id: UUID, *, expected_version: int) -> None:
        """Permanently remove an edition and all of its edition-owned records."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionNotFoundError(str(edition_id))
            if edition.version != expected_version:
                raise EditionConcurrencyError("Edition was modified by another request")
            await uow.edition_audit.delete_for_edition(edition_id)
            if not await uow.editions.delete(edition_id, expected_version):
                raise EditionConcurrencyError("Edition was modified by another request")
            await uow.commit()

    @staticmethod
    async def _validate_previous(
        uow: EditionUnitOfWork, previous_edition_id: UUID | None, edition_id: UUID
    ) -> None:
        if previous_edition_id is None:
            return
        if previous_edition_id == edition_id:
            raise PreviousEditionError("An edition cannot reference itself")
        if await uow.editions.get(previous_edition_id) is None:
            raise PreviousEditionError("Previous edition does not exist")

    @staticmethod
    async def _ensure_logical_key_available(uow: EditionUnitOfWork, edition: Edition) -> None:
        existing = await uow.editions.get_by_logical_key(
            edition.country_code, edition.period_start, edition.period_end
        )
        if existing is not None and existing.id != edition.id:
            raise DuplicateEditionError(existing.id)
