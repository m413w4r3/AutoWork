"""Drift guard for the M23 editorial acceptance traceability table.

``docs/design/atelier-editorial/ACCEPTANCE.md`` claims one exact test node per
acceptance scenario. This test parses those references and fails when a file or
a test function/title disappears, so the table cannot rot silently.

This is a *reference* guard, not behavioural evidence: it never replaces the
chained PostgreSQL/Typst oracle in
``tests/integration/editorial/test_acceptance_chain_postgres.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_ACCEPTANCE_DOC = (
    Path(__file__).resolve().parents[2] / "docs" / "design" / "atelier-editorial" / "ACCEPTANCE.md"
)
_EXPECTED_IDS = tuple(f"E{number:02d}" for number in range(1, 23))
_PATH_TOKEN = re.compile(r"^(backend|frontend)/[A-Za-z0-9_./-]+\.(py|ts|tsx)$")
_BACKTICK = re.compile(r"`([^`]+)`")


def _table_rows() -> dict[str, list[str]]:
    text = _ACCEPTANCE_DOC.read_text(encoding="utf-8")
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("| E"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        scenario = cells[0]
        if re.fullmatch(r"E\d{2}", scenario) is None:
            continue
        assert scenario not in rows, f"duplicate acceptance row {scenario}"
        rows[scenario] = cells
    return rows


def _resolve(repo_root: Path, relative: str) -> Path:
    path = repo_root / relative
    assert path.is_file(), f"ACCEPTANCE.md references a missing file: {relative}"
    return path


def test_acceptance_table_covers_every_scenario_and_references_exist() -> None:
    repo_root = _ACCEPTANCE_DOC.parents[3]
    rows = _table_rows()
    assert tuple(rows) == _EXPECTED_IDS, (
        "ACCEPTANCE.md must list E01..E22 exactly once",
        tuple(rows),
    )

    checked_nodes = 0
    checked_titles = 0
    for scenario, cells in rows.items():
        references = [
            token
            for cell in cells
            for token in _BACKTICK.findall(cell)
            if token.startswith(("backend/", "frontend/"))
        ]
        assert references, f"{scenario} must cite at least one test or test file"
        for reference in references:
            if "::" in reference:
                relative, node = reference.split("::", 1)
                path = _resolve(repo_root, relative)
                if relative.endswith(".py"):
                    function = node.split("[", 1)[0]
                    pattern = re.compile(
                        rf"^(?:async\s+)?def\s+{re.escape(function)}\s*\(",
                        re.MULTILINE,
                    )
                    assert pattern.search(path.read_text(encoding="utf-8")), (
                        f"{scenario} cites a missing test function: {reference}"
                    )
                    checked_nodes += 1
                else:
                    assert node in path.read_text(encoding="utf-8"), (
                        f"{scenario} cites a missing frontend test title: {reference}"
                    )
                    checked_titles += 1
                continue
            assert _PATH_TOKEN.fullmatch(reference), (
                f"{scenario} has an unsupported test reference: {reference}"
            )
            _resolve(repo_root, reference)

    assert checked_nodes >= 30
    assert checked_titles >= 15


def test_acceptance_doc_states_the_unproven_limits() -> None:
    """The honest-limits section is part of the contract, not decoration."""
    text = _ACCEPTANCE_DOC.read_text(encoding="utf-8")
    for required in (
        "Real Bridge transport",
        "stubbed API",
        "Visual parity of the PDF",
        "BRIEF overview merge",
        "EditorialWorkspaceBarChart",
    ):
        assert required in text, required
