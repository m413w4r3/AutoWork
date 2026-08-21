from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.infrastructure.database.repositories._shared import (
    coerce_optional_uuid,
    coerce_uuid,
    isoformat_or_none,
    parse_date_or_none,
    parse_datetime_or_none,
)


def test_coerce_uuid_accepts_a_string() -> None:
    value = uuid4()

    assert coerce_uuid(str(value)) == value


def test_coerce_uuid_passes_a_uuid_through_unchanged() -> None:
    value = uuid4()

    assert coerce_uuid(value) == value


def test_coerce_optional_uuid_passes_none_through() -> None:
    assert coerce_optional_uuid(None) is None


def test_coerce_optional_uuid_coerces_a_string() -> None:
    value = uuid4()

    assert coerce_optional_uuid(str(value)) == value


def test_isoformat_or_none_passes_none_through() -> None:
    assert isoformat_or_none(None) is None


def test_isoformat_or_none_serializes_a_datetime() -> None:
    value = datetime(2026, 8, 21, 12, 30, tzinfo=UTC)

    assert isoformat_or_none(value) == value.isoformat()


def test_isoformat_or_none_serializes_a_date() -> None:
    value = date(2026, 8, 21)

    assert isoformat_or_none(value) == value.isoformat()


def test_parse_datetime_or_none_passes_none_through() -> None:
    assert parse_datetime_or_none(None) is None


def test_parse_datetime_or_none_round_trips_an_iso_string() -> None:
    value = datetime(2026, 8, 21, 12, 30, tzinfo=UTC)

    assert parse_datetime_or_none(value.isoformat()) == value


def test_parse_date_or_none_passes_none_through() -> None:
    assert parse_date_or_none(None) is None


def test_parse_date_or_none_round_trips_an_iso_string() -> None:
    value = date(2026, 8, 21)

    assert parse_date_or_none(value.isoformat()) == value


def test_coerce_uuid_rejects_a_malformed_value() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        coerce_uuid("not-a-uuid")


def test_coerce_uuid_type_is_uuid() -> None:
    assert isinstance(coerce_uuid(str(uuid4())), UUID)
