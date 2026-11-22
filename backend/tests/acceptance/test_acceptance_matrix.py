"""Integrity proof for the A01-F10 acceptance matrix (M24 lot 10).

This module never executes the scenario tests themselves: the real runs are
recorded in ``docs/m24/acceptance-matrix.md``.  It proves by programmatic
collection that every node id named in :mod:`tests.acceptance.matrix` exists in
this repository, that A01-F10 appears exactly once each, and that no scenario
is empty.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.acceptance.matrix import PROOF_LEVELS, SCENARIO_IDS, SCENARIOS

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_IDS = tuple(
    f"{letter}{index:02d}"
    for letter, count in (("A", 7), ("B", 10), ("C", 6), ("D", 10), ("E", 6), ("F", 10))
    for index in range(1, count + 1)
)


def _collect(node_ids: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    source_root = str(BACKEND_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *node_ids,
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def _collected_node_ids(stdout: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    )


def _is_collected(node_id: str, collected: set[str]) -> bool:
    if node_id in collected:
        return True
    # A parametrized oracle is named once in the matrix and collected once per
    # parameter set, e.g. ``...::test_x[outcome]``.
    return any(item.startswith(f"{node_id}[") for item in collected)


def test_a01_to_f10() -> None:
    assert SCENARIO_IDS == EXPECTED_IDS
    assert len(SCENARIOS) == 49
    seen: set[str] = set()
    for scenario in SCENARIOS:
        assert scenario.scenario_id not in seen
        seen.add(scenario.scenario_id)
        assert scenario.summary.strip()
        assert scenario.owner_lot
        assert scenario.level in PROOF_LEVELS
        assert scenario.tests, f"{scenario.scenario_id} has no oracle"
        assert len(set(scenario.tests)) == len(scenario.tests)
    assert seen == set(EXPECTED_IDS)

    node_ids = tuple(dict.fromkeys(node for scenario in SCENARIOS for node in scenario.tests))
    missing_files = [
        node_id for node_id in node_ids if not (BACKEND_ROOT / node_id.split("::", 1)[0]).is_file()
    ]
    assert not missing_files, f"matrix references missing files: {missing_files}"

    completed = _collect(node_ids)
    collected = set(_collected_node_ids(completed.stdout))
    if completed.returncode != 0:
        # One unknown node id makes pytest hide the otherwise collected ids;
        # re-collect one by one only on that failure path to name each gap.
        collected = set()
        for node_id in node_ids:
            per_node = _collect((node_id,))
            if per_node.returncode == 0:
                collected.update(_collected_node_ids(per_node.stdout))

    missing = [
        f"{scenario.scenario_id}:{node_id}"
        for scenario in SCENARIOS
        for node_id in scenario.tests
        if not _is_collected(node_id, collected)
    ]
    assert not missing, f"matrix oracles are not collectible: {missing}"
