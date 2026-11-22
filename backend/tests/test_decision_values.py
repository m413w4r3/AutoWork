"""The analyst decision enums and their database CHECK value sets are one contract.

Migration 0020 (M24-04A1) extends ``analyst_decisions`` with the plan decision
values and target.  Keeping the Python enums and the SQL value strings aligned
here prevents a value accepted by the domain from being rejected by PostgreSQL,
or a dropped value from silently widening the database.
"""

from __future__ import annotations

import re

from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.infrastructure.database.models.production import (
    ANALYST_DECISION_TARGET_VALUES_SQL,
    ANALYST_DECISION_VALUES_SQL,
)


def _values(sql: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sql))


def test_enums_and_sql_values_aligned() -> None:
    decision_types = _values(ANALYST_DECISION_VALUES_SQL)
    target_types = _values(ANALYST_DECISION_TARGET_VALUES_SQL)

    assert decision_types == {item.value for item in AnalystDecisionType}
    assert target_types == {item.value for item in AnalystDecisionTargetType}

    # 0020 adds exactly the plan decision surface; 0021 will add the task one.
    assert {"plan_accept", "plan_reject"} <= decision_types
    assert "plan_version" in target_types
    assert len(decision_types) == 12
    assert len(target_types) == 9

    # Existing values are preserved: M24 is additive for legacy decisions.
    assert {
        "member_validate",
        "member_reject",
        "feature_validate",
        "feature_reject",
        "pivot_approve",
        "pivot_reject",
        "note_approve",
        "note_changes_requested",
        "detection_approve",
        "detection_reject",
    } <= decision_types
    assert {
        "member",
        "feature",
        "tool",
        "invariant",
        "pivot",
        "corpus",
        "detection",
        "note",
    } <= target_types
