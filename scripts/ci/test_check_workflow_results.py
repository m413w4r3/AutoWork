#!/usr/bin/env python3
"""Fast, database-free tests for the workflow result guard."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_workflow_results import (
    JUnitCase,
    compare_results,
    parse_collected_nodeids,
    parse_junit,
)


class WorkflowResultsGuardTests(unittest.TestCase):
    def test_parse_collected_nodeids_ignores_summary_lines(self) -> None:
        output = """tests/workflow_emulator/test_a.py::test_a
tests/workflow_emulator/test_b.py::Case::test_b[one]
2 tests collected in 0.01s
"""
        self.assertEqual(
            parse_collected_nodeids(output),
            [
                "tests/workflow_emulator/test_a.py::test_a",
                "tests/workflow_emulator/test_b.py::Case::test_b[one]",
            ],
        )

    def test_all_expected_nodes_must_be_passed_and_below_floor_fails(self) -> None:
        expected = [
            "tests/workflow_emulator/test_a.py::test_passes",
            "tests/workflow_emulator/test_a.py::test_skips",
            "tests/workflow_emulator/test_a.py::test_missing",
        ]
        cases = [
            JUnitCase("tests.workflow_emulator.test_a", "test_passes", "passed"),
            JUnitCase("tests.workflow_emulator.test_a", "test_skips", "skipped"),
        ]
        problems = compare_results(expected, cases, minimum=4)
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("PROFILE UNDERFLOOR" in problem for problem in problems))
        self.assertTrue(
            any("IGNORÉ/ÉCHOUÉ" in problem and "test_skips" in problem for problem in problems)
        )
        self.assertTrue(
            any("ABSENT" in problem and "test_missing" in problem for problem in problems)
        )

    def test_failed_junit_case_is_named(self) -> None:
        nodeid = "tests/workflow_emulator/test_a.py::Case::test_fails[bad]"
        case = JUnitCase("tests.workflow_emulator.test_a.Case", "test_fails[bad]", "failed")
        self.assertEqual(
            compare_results([nodeid], [case], minimum=1),
            [f"ÉCHOUÉ: {nodeid} — statut JUnit failed."],
        )

    def test_xfailed_is_not_accepted_as_passed(self) -> None:
        nodeid = "tests/workflow_emulator/test_a.py::test_xfails"
        case = JUnitCase("tests.workflow_emulator.test_a", "test_xfails", "xfailed")
        self.assertIn("IGNORÉ/ÉCHOUÉ", compare_results([nodeid], [case], minimum=1)[0])

    def test_parse_junit_marks_xfail_and_skip_as_non_passing(self) -> None:
        xml = """<testsuite tests="2" skipped="2">
          <testcase classname="tests.workflow_emulator.test_a" name="test_xfails">
            <skipped type="pytest.xfail" message="expected failure" />
          </testcase>
          <testcase classname="tests.workflow_emulator.test_a" name="test_skips">
            <skipped type="pytest.skip" message="fixture skip" />
          </testcase>
        </testsuite>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "junit.xml"
            path.write_text(xml, encoding="utf-8")
            cases = parse_junit(path)
        self.assertEqual([case.outcome for case in cases], ["xfailed", "skipped"])


if __name__ == "__main__":
    unittest.main()
