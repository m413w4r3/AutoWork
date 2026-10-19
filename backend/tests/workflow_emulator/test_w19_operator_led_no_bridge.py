from __future__ import annotations

import pytest

from cti_app.domain.investigation import InvestigationExecutionMode
from cti_app.domain.investigation_state import StopReason
from tests.test_investigation_service import _Fixture


@pytest.mark.asyncio
async def test_operator_led_resume_and_run_never_submit_a_bridge_turn() -> None:
    fixture = _Fixture(responses=[])
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    assert state.execution_mode is InvestigationExecutionMode.OPERATOR_LED

    interrupted = await fixture.service.interrupt(
        state.investigation_id, expected_version=state.version
    )
    assert interrupted.stop_reason is StopReason.USER_INTERRUPT

    await fixture.service.resume(
        state.investigation_id,
        expected_version=interrupted.version,
        owner_job_terminal=True,
    )
    await fixture.service.run(state.investigation_id)

    assert fixture.bridge_transport.submission_count == 0
