from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.domain.selection import SelectionAction, SelectionDecision, SubjectDiscoveryOrigin


def _decision(**overrides: object) -> SelectionDecision:
    values: dict[str, object] = {
        "edition_id": uuid4(),
        "discovery_subject_id": uuid4(),
        "snapshot_id": uuid4(),
        "snapshot_version": 1,
        "action": SelectionAction.SELECT,
        "subject_id": uuid4(),
        "actor_id": "analyst-1",
        "correlation_id": "corr-1",
        "idempotency_key": "selection-1",
        "occurred_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SelectionDecision(**values)  # type: ignore[arg-type]


def _origin(**overrides: object) -> SubjectDiscoveryOrigin:
    values: dict[str, object] = {
        "subject_id": uuid4(),
        "edition_id": uuid4(),
        "discovery_subject_id": uuid4(),
        "selection_decision_id": uuid4(),
        "selected_snapshot_id": uuid4(),
        "selected_snapshot_version": 1,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SubjectDiscoveryOrigin(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["actor_id", "correlation_id", "idempotency_key"])
def test_selection_decision_rejects_blank_identity_fields(field: str) -> None:
    with pytest.raises(ValueError):
        _decision(**{field: "  "})


def test_selection_decision_rejects_invalid_snapshot_and_action_pairs() -> None:
    with pytest.raises(ValueError):
        _decision(snapshot_version=0)
    with pytest.raises(ValueError):
        _decision(occurred_at=datetime.now())
    with pytest.raises(ValueError):
        _decision(subject_id=None)
    with pytest.raises(ValueError):
        _decision(action=SelectionAction.IGNORE, subject_id=uuid4())


def test_selection_decision_and_origin_are_immutable() -> None:
    decision = _decision()
    origin = _origin()
    with pytest.raises(FrozenInstanceError):
        decision.actor_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        origin.subject_id = uuid4()  # type: ignore[misc]


def test_subject_discovery_origin_requires_positive_version_and_aware_time() -> None:
    with pytest.raises(ValueError):
        _origin(selected_snapshot_version=0)
    with pytest.raises(ValueError):
        _origin(created_at=datetime.now())
