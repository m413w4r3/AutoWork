from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.domain.investigation_state import StopReason
from cti_app.domain.production import AnalystInvestigation, AnalystInvestigationStatus, LoopBudget


def test_failed_is_reserved_for_technical_failure() -> None:
    investigation = AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(),
    )
    investigation.start(now=datetime.now(UTC))
    investigation.fail_technical(now=datetime.now(UTC))
    assert investigation.status is AnalystInvestigationStatus.FAILED


def test_zero_new_members_do_not_exhaust_an_investigation() -> None:
    investigation = AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(max_cycles=1),
    )
    investigation.start(now=datetime.now(UTC))
    investigation.finish_cycle(validated_new_members=0, now=datetime.now(UTC))
    assert investigation.status is AnalystInvestigationStatus.RUNNING
    assert investigation.finished_at is None


def test_turn_budget_is_reported_without_forcing_a_stage_or_status_transition() -> None:
    investigation = AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(max_cycles=1),
    )
    investigation.start(now=datetime.now(UTC))
    stage = investigation.current_stage

    assert investigation.record_turn(progress=True, now=datetime.now(UTC)) is True
    assert investigation.status is AnalystInvestigationStatus.RUNNING
    assert investigation.current_stage is stage

    investigation.conclude(StopReason.AGENT_FINISHED, now=datetime.now(UTC))
    assert investigation.status is AnalystInvestigationStatus.COMPLETED


def test_input_pack_requires_matching_sha256() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        AnalystInvestigation(
            production_run_id=uuid4(),
            subject_id=uuid4(),
            synthesis_artifact_id=uuid4(),
            budget=LoopBudget(),
            input_pack_blob_id=uuid4(),
        )
