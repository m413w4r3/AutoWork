from datetime import UTC, datetime
from uuid import uuid4

import pytest

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


def test_cycle_limit_exhausts_instead_of_failing() -> None:
    investigation = AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(max_cycles=1),
    )
    investigation.start(now=datetime.now(UTC))
    investigation.finish_cycle(validated_new_members=1, now=datetime.now(UTC))
    assert investigation.status is AnalystInvestigationStatus.EXHAUSTED


def test_input_pack_requires_matching_sha256() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        AnalystInvestigation(
            production_run_id=uuid4(),
            subject_id=uuid4(),
            synthesis_artifact_id=uuid4(),
            budget=LoopBudget(),
            input_pack_blob_id=uuid4(),
        )
