from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.domain.investigation import (
    AgendaItem,
    InvestigationAgenda,
    InvestigationExecutionMode,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.infrastructure.database.repositories.investigation import _working_state_from_row


@pytest.mark.parametrize(
    ("missing_keys", "expected_key_count"),
    (
        (("deduplicated_findings",), 18),
        (("deduplicated_findings", "execution_mode"), 17),
    ),
)
def test_working_state_from_row_reads_legacy_payload_defaults_without_mutating_it(
    missing_keys: tuple[str, ...], expected_key_count: int
) -> None:
    agenda = InvestigationAgenda(
        (
            AgendaItem("First question?", 0.5, 1),
            AgendaItem("Second question?", 0.4, 2),
        )
    )
    state = InvestigationWorkingState(investigation_id=uuid4(), agenda=agenda)
    payload = state.to_payload()
    legacy_payload = {key: value for key, value in payload.items() if key not in missing_keys}
    row = SimpleNamespace(
        state=legacy_payload,
        investigation_id=state.investigation_id,
        version=state.version,
        turn_index=state.turn_index,
        stop_reason=None,
    )

    loaded_state = _working_state_from_row(row)

    assert len(legacy_payload) == expected_key_count
    assert loaded_state.deduplicated_findings == 0
    assert loaded_state.execution_mode is InvestigationExecutionMode.BRIDGE_LED
    assert row.state == legacy_payload
    assert all(key not in row.state for key in missing_keys)


def test_working_state_from_row_still_rejects_indexed_column_divergence() -> None:
    agenda = InvestigationAgenda(
        (
            AgendaItem("First question?", 0.5, 1),
            AgendaItem("Second question?", 0.4, 2),
        )
    )
    state = InvestigationWorkingState(investigation_id=uuid4(), agenda=agenda)
    payload = state.to_payload()
    row = SimpleNamespace(
        state=payload,
        investigation_id=state.investigation_id,
        version=state.version + 1,
        turn_index=state.turn_index,
        stop_reason=None,
    )

    with pytest.raises(
        ValueError, match="Persisted investigation state disagrees with its indexed columns"
    ):
        _working_state_from_row(row)
