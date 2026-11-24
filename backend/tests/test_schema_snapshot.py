from __future__ import annotations

from tests.support.schema_snapshot import diff_snapshots


def test_diff_snapshots_is_empty_for_equal_json_values() -> None:
    snapshot = {"tables": [{"name": "items", "columns": ["id", "name"]}]}

    equal_snapshot = {"tables": [{"columns": ["id", "name"], "name": "items"}]}

    assert diff_snapshots(snapshot, equal_snapshot) == []


def test_diff_snapshots_reports_nested_and_missing_values_in_order() -> None:
    left = {"columns": [{"name": "id", "nullable": False}], "tables": ["items"]}
    right = {"columns": [{"name": "id", "nullable": True}], "indexes": [], "tables": []}

    assert diff_snapshots(left, right) == [
        "$.columns[0].nullable: left=false; right=true",
        "$.indexes: missing on left; right=[]",
        '$.tables[0]: left="items"; missing on right',
    ]
