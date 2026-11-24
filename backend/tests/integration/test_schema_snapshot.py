from __future__ import annotations

import pytest
from alembic import command

from tests.integration.conftest import _alembic_config
from tests.support.schema_snapshot import capture_schema, diff_snapshots

pytestmark = pytest.mark.integration


def test_0001_schema_snapshot_is_stable(
    temporary_postgres_url: str,
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "0001_baseline")

    first = capture_schema(temporary_postgres_url)
    second = capture_schema(temporary_postgres_url)

    assert diff_snapshots(first, second) == []
