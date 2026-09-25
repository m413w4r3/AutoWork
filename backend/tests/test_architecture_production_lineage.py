"""Architectural gates of AW-009: Production depends on Subject lineage only."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cti_app.infrastructure.database.models import (  # noqa: F401
    collection,
    core,
    production,
)
from cti_app.infrastructure.database.models.base import Base

_BACKEND = Path(__file__).resolve().parents[1]
_REPOSITORY = _BACKEND.parent
_RUNTIME_ROOTS = (_BACKEND / "src", _REPOSITORY / "frontend" / "src")
_SUFFIXES = {".py", ".ts", ".tsx"}

_FORBIDDEN = (
    "LegacyEditorialProjectionService",
    "EditorialGroup",
    "editorial_groups",
    "candidate_references",
    "CandidateReference",
    "brief_auto",
    "major_assisted",
    "SubjectProductionStatus",
    "SubjectProductionStage",
    "subject_production_runs",
)


def _runtime_files() -> list[Path]:
    return [
        path
        for root in _RUNTIME_ROOTS
        if root.is_dir()
        for path in root.rglob("*")
        if path.suffix in _SUFFIXES and "node_modules" not in path.parts
    ]


@pytest.mark.parametrize("token", _FORBIDDEN)
def test_runtime_code_has_no_editorial_group_legacy(token: str) -> None:
    pattern = re.compile(re.escape(token))
    offenders = [
        f"{path.relative_to(_REPOSITORY)}:{number}"
        for path in _runtime_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]

    assert offenders == []


def test_schema_has_no_editorial_group_columns() -> None:
    tables = Base.metadata.tables

    assert "editorial_groups" not in tables
    assert not {"editorial_group_id", "editorial_group_version"} & set(
        tables["production_input_snapshots"].columns.keys()
    )
    for table in ("source_collections", "claims", "indicators"):
        assert "group_id" not in tables[table].columns.keys()


def test_production_frontend_never_reads_the_selection_api() -> None:
    frontend = _REPOSITORY / "frontend" / "src"
    if not frontend.is_dir():
        pytest.skip("frontend sources are not part of this checkout")
    production_surfaces = [
        frontend / "api" / "production.ts",
        frontend / "features" / "edition-workflow" / "ProductionConsole.tsx",
        frontend / "features" / "edition-workflow" / "ProductionBatchSelector.tsx",
        frontend / "features" / "edition-workflow" / "productionBatchSelection.ts",
    ]

    for path in production_surfaces:
        source = path.read_text(encoding="utf-8")
        assert "api/selection" not in source, path
        assert "/selection" not in source, path


def test_single_alembic_baseline() -> None:
    revisions = sorted(
        path.name
        for path in (_BACKEND / "migrations" / "versions").glob("*.py")
        if path.name != "__init__.py"
    )

    assert revisions == ["0001_baseline.py"]


def test_production_reference_modules_do_not_import_malware_reference_corpus() -> None:
    forbidden = {
        "cti_app.domain.reference_corpus",
        "cti_app.application.reference_corpus",
    }
    modules = (
        _BACKEND / "src" / "cti_app" / "domain" / "production_references.py",
        _BACKEND / "src" / "cti_app" / "application" / "production_references.py",
    )

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                imports.update(f"{base}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        assert imports.isdisjoint(forbidden), path


@pytest.mark.parametrize("token", ("references_conversation_id", "repair_source_index"))
def test_runtime_code_has_no_aw010_references_legacy(token: str) -> None:
    """AW-010: REFERENCES is stateless and its corpus replaces the repair index."""
    offenders = [
        f"{path.relative_to(_REPOSITORY)}:{number}"
        for path in _runtime_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if token in line
    ]

    assert offenders == []


def test_schema_stores_the_reference_corpus_only_as_an_artifact() -> None:
    tables = Base.metadata.tables

    assert "references_conversation_id" not in tables["production_runs"].columns.keys()
    assert "synthesis_conversation_id" in tables["production_runs"].columns.keys()
    assert not {
        "production_reference_corpora",
        "production_reference_sources",
        "reference_events",
    } & set(tables)
