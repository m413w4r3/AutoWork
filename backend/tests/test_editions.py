from datetime import date

import pytest

from cti_app.application.editions import (
    DuplicateEditionError,
    EditionConcurrencyError,
    EditionService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionImmutableError, EditionStatus
from cti_app.domain.errors import TlpDowngradeError
from tests.edition_support import InMemoryEditionUnitOfWorkFactory


async def _create_edition(
    service: EditionService,
    *,
    country: str = "Iran",
    country_code: str = "IR",
    period_start: date = date(2026, 7, 1),
    period_end: date = date(2026, 7, 31),
    tlp: TLP = TLP.AMBER,
) -> Edition:
    return await service.create(
        country=country,
        country_code=country_code,
        period_start=period_start,
        period_end=period_end,
        tlp=tlp,
        languages=("fr", "en", "fa"),
        actor_id="dev-analyst",
        correlation_id="create-correlation",
    )


async def test_logical_key_is_unique_on_create_and_update() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    service = EditionService(factory)
    edition = await _create_edition(service)

    with pytest.raises(DuplicateEditionError):
        await _create_edition(service, country="Iran duplicate")

    other = await _create_edition(
        service,
        country="Iraq",
        country_code="IQ",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
    )
    with pytest.raises(DuplicateEditionError):
        await service.update(
            other.id,
            expected_version=other.version,
            country=edition.country,
            country_code=edition.country_code,
            period_start=edition.period_start,
            period_end=edition.period_end,
            tlp=TLP.AMBER,
            languages=("fr", "en", "fa"),
            actor_id="dev-analyst",
            correlation_id="duplicate-update",
        )


async def test_update_checks_version_and_preserves_tlp_invariant() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    service = EditionService(factory)
    edition = await _create_edition(service)

    updated = await service.update(
        edition.id,
        expected_version=edition.version,
        country="Iran updated",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.RED,
        languages=("en",),
        actor_id="dev-analyst",
        correlation_id="update-correlation",
    )
    assert updated.version == edition.version + 1
    assert updated.tlp is TLP.RED

    with pytest.raises(EditionConcurrencyError):
        await service.update(
            edition.id,
            expected_version=edition.version,
            country="Iran stale",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.RED,
            languages=("en",),
            actor_id="dev-analyst",
            correlation_id="stale-update",
        )

    with pytest.raises(TlpDowngradeError):
        await service.update(
            edition.id,
            expected_version=updated.version,
            country="Iran updated",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("en",),
            actor_id="dev-analyst",
            correlation_id="tlp-downgrade",
        )


async def test_archive_is_optimistic_audited_and_final() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    service = EditionService(factory)
    edition = await _create_edition(service)

    archived = await service.archive(
        edition.id,
        expected_version=edition.version,
        actor_id="dev-analyst",
        correlation_id="archive-correlation",
    )
    assert archived.state is EditionStatus.ARCHIVED
    assert archived.version == edition.version + 1

    with pytest.raises(EditionConcurrencyError):
        await service.archive(
            edition.id,
            expected_version=edition.version,
            actor_id="dev-analyst",
        )

    with pytest.raises(EditionImmutableError):
        await service.archive(
            edition.id,
            expected_version=archived.version,
            actor_id="dev-analyst",
        )

    with pytest.raises(EditionImmutableError):
        await service.update(
            edition.id,
            expected_version=archived.version,
            country="Iran changed",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("en",),
            actor_id="dev-analyst",
            correlation_id="after-archive",
        )

    audit = await service.audit(edition.id)
    assert [event.action for event in audit] == ["edition.created", "edition.archived"]
    assert audit[-1].actor_id == "dev-analyst"
    assert audit[-1].correlation_id == "archive-correlation"
    assert audit[-1].before is not None
    assert audit[-1].before["state"] == EditionStatus.OPEN.value
    assert audit[-1].after["state"] == EditionStatus.ARCHIVED.value
