from datetime import date

import pytest

from cti_app.application.editions import EditionConcurrencyError, EditionService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import EditionStatus
from tests.edition_support import InMemoryEditionUnitOfWorkFactory


async def test_optimistic_concurrency_and_complete_audit() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    service = EditionService(factory)
    edition = await service.create(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=2,
        target_briefs=6,
        previous_edition_id=None,
        source_profile="iran-default",
        actor_id="dev-analyst",
        correlation_id="test-correlation",
    )
    stale_version = edition.version
    updated = await service.update(
        edition.id,
        expected_version=stale_version,
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=3,
        target_briefs=7,
        previous_edition_id=None,
        source_profile="iran-default",
        actor_id="dev-analyst",
        correlation_id="update-correlation",
    )

    with pytest.raises(EditionConcurrencyError):
        await service.transition(
            edition.id,
            target=EditionStatus.DISCOVERY,
            expected_version=stale_version,
            actor_id="dev-analyst",
            correlation_id="stale",
        )

    transitioned = await service.transition(
        edition.id,
        target=EditionStatus.DISCOVERY,
        expected_version=updated.version,
        actor_id="dev-analyst",
        correlation_id="transition-correlation",
    )
    audit = await service.audit(edition.id)

    assert transitioned.status is EditionStatus.DISCOVERY
    assert [event.action for event in audit] == [
        "edition.created",
        "edition.updated",
        "edition.transitioned",
    ]
    assert all(event.actor_id == "dev-analyst" for event in audit)
