#!/usr/bin/env python3
"""Fail closed when a marked workflow scenario is missing or not passed."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JUnitCase:
    classname: str
    name: str
    outcome: str
    nodeid: str | None = None


def parse_collected_nodeids(output: str) -> list[str]:
    """Read quiet pytest collection output, ignoring its summary and warnings."""
    return sorted(
        {
            line.strip()
            for line in output.splitlines()
            if line.strip().startswith("tests/workflow_emulator/") and "::" in line
        }
    )


def parse_junit(path: Path) -> list[JUnitCase]:
    root = ET.parse(path).getroot()
    cases: list[JUnitCase] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        children = {child.tag.rsplit("}", 1)[-1]: child for child in element}
        if "failure" in children or "error" in children:
            outcome = "failed"
        elif "skipped" in children:
            skipped = children["skipped"]
            marker = f"{skipped.get('type', '')} {skipped.get('message', '')}".lower()
            outcome = "xfailed" if "xfail" in marker else "skipped"
        else:
            outcome = "passed"
        cases.append(
            JUnitCase(
                classname=element.get("classname", ""),
                name=element.get("name", ""),
                outcome=outcome,
                nodeid=element.get("nodeid"),
            )
        )
    return cases


def _case_matches(case: JUnitCase, nodeid: str) -> bool:
    if case.nodeid is not None:
        return case.nodeid == nodeid
    path, *parts = nodeid.split("::")
    if not parts:
        return False
    module = path.removesuffix(".py").replace("/", ".")
    test_name = parts[-1]
    class_path = parts[:-1]
    class_name = ".".join(class_path)
    allowed_classnames = {module}
    if class_name:
        allowed_classnames.add(f"{module}.{class_name}")
    allowed_names = {test_name, "::".join(parts)}
    return case.classname in allowed_classnames and case.name in allowed_names


def compare_results(expected_nodeids: list[str], cases: list[JUnitCase], minimum: int) -> list[str]:
    problems: list[str] = []
    if len(expected_nodeids) < minimum:
        problems.append(
            f"PROFILE UNDERFLOOR: {len(expected_nodeids)} scénario(s) collecté(s), "
            f"plancher demandé {minimum}."
        )

    matched_case_indexes: set[int] = set()
    for nodeid in expected_nodeids:
        matches = [(index, case) for index, case in enumerate(cases) if _case_matches(case, nodeid)]
        if not matches:
            problems.append(f"ABSENT: {nodeid} n'apparaît pas dans le rapport JUnit.")
            continue
        if len(matches) > 1:
            problems.append(f"DUPLIQUÉ: {nodeid} apparaît plusieurs fois dans le rapport JUnit.")
            matched_case_indexes.update(index for index, _ in matches)
            continue
        index, case = matches[0]
        matched_case_indexes.add(index)
        if case.outcome != "passed":
            label = "IGNORÉ/ÉCHOUÉ" if case.outcome in {"skipped", "xfailed"} else "ÉCHOUÉ"
            problems.append(f"{label}: {nodeid} — statut JUnit {case.outcome}.")

    for index, case in enumerate(cases):
        if index not in matched_case_indexes:
            problems.append(
                f"INATTENDU: {case.classname}::{case.name} ({case.outcome}) "
                "n'a pas de node id collecté correspondant."
            )
    return problems


def collect_nodeids(backend_dir: Path, uv: str, expression: str) -> list[str]:
    env = os.environ.copy()
    env["WORKFLOW_EMULATOR_REQUIRED"] = "1"
    command = [
        uv,
        "run",
        "--no-sync",
        "pytest",
        "--collect-only",
        "-q",
        "-m",
        expression,
        "tests/workflow_emulator",
    ]
    result = subprocess.run(command, cwd=backend_dir, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
        raise RuntimeError(
            f"La collecte pytest a échoué ({result.returncode}) avec {expression!r}:\n{detail}"
        )
    return parse_collected_nodeids(result.stdout + "\n" + result.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", required=True, type=Path)
    parser.add_argument("--expression", required=True)
    parser.add_argument("--min-count", required=True, type=int)
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    if args.min_count < 1:
        parser.error("--min-count doit être supérieur ou égal à 1")

    repository_root = Path(__file__).resolve().parents[2]
    backend_dir = repository_root / "backend"
    try:
        expected = collect_nodeids(backend_dir, args.uv, args.expression)
    except (OSError, RuntimeError) as exc:
        print(f"ÉCHEC DU GARDE: {exc}", file=sys.stderr)
        return 1
    if not args.junitxml.is_file():
        print(
            f"ÉCHEC DU GARDE: rapport JUnit absent: {args.junitxml}; "
            f"{len(expected)} scénario(s) attendu(s).",
            file=sys.stderr,
        )
        for nodeid in expected:
            print(f"ABSENT: {nodeid} n'a pas de rapport de résultat.", file=sys.stderr)
        return 1
    try:
        cases = parse_junit(args.junitxml)
    except (ET.ParseError, OSError) as exc:
        print(f"ÉCHEC DU GARDE: rapport JUnit illisible: {exc}", file=sys.stderr)
        return 1

    problems = compare_results(expected, cases, args.min_count)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(
        f"Garde workflow OK: {len(expected)} scénario(s) collecté(s), "
        "tous présents et passed dans le JUnit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
