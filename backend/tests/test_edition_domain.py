from datetime import date

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus, InvalidEditionTransitionError


def make_edition() -> Edition:
    return Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=2,
        target_briefs=6,
        source_profile="iran-default",
    )


def test_month_period_and_language_validation() -> None:
    with pytest.raises(ValueError, match="complete calendar month"):
        Edition(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 2),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr",),
            target_major_articles=2,
            target_briefs=6,
            source_profile="iran-default",
        )


def test_state_machine_exposes_only_valid_actions() -> None:
    edition = make_edition()
    assert edition.allowed_transitions == (EditionStatus.DISCOVERY, EditionStatus.ARCHIVED)

    edition.transition(EditionStatus.DISCOVERY)

    assert edition.status is EditionStatus.DISCOVERY
    assert edition.progress_percent == 15
    with pytest.raises(InvalidEditionTransitionError):
        edition.transition(EditionStatus.PUBLISHED)
