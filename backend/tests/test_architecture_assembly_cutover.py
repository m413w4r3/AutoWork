"""Permanent boundary gate for the AW-014 canonical production path."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from cti_app.application.production_workflow import ProductionWorkflowOrchestrator

_APPLICATION = Path(__file__).resolve().parents[1] / "src" / "cti_app" / "application"
_DOMAIN = _APPLICATION.parent / "domain"
_CANONICAL_FILES = (
    "production_stages.py",
    "production_synthesis.py",
    "publication_builder.py",
    "publication_assembly.py",
    "publication_qa.py",
)
_LEGACY_FORBIDDEN = (
    "ReferenceReport",
    "TechnicalExtraction",
    "load_reference_projection",
    "report_source_labels",
    "[S1]",
)
_RENDERER_FORBIDDEN = (
    "PandocRenderer",
    "publication_renderer",
    "render_publication_pandoc",
    "export_markdown_docx",
    "pandoc",
    "Pandoc",
    "DOCX",
    "OOXML",
    "reference-doc",
)


def test_canonical_assembly_qa_and_orchestrator_have_no_legacy_dependency() -> None:
    sources = {name: (_APPLICATION / name).read_text() for name in _CANONICAL_FILES}
    sources["_execute_assembly_stage"] = inspect.getsource(
        ProductionWorkflowOrchestrator._execute_assembly_stage
    )
    for _name, source in sources.items():
        for forbidden in _RENDERER_FORBIDDEN:
            assert forbidden not in source
        for forbidden in _LEGACY_FORBIDDEN:
            assert forbidden not in source

    assert not (_APPLICATION / "publication_renderer.py").exists()
    assert not (_APPLICATION / ("production" + "_legacy_assembly.py")).exists()


def test_pandoc_renderer_class_is_absent_from_active_backend_python() -> None:
    active_python = Path(__file__).resolve().parents[1] / "src" / "cti_app"
    occurrences = [
        path.relative_to(active_python)
        for path in active_python.rglob("*.py")
        if "PandocRenderer" in path.read_text()
    ]
    assert occurrences == []


def test_editorial_enrichment_contract_has_no_renderer_or_compiler_dependency() -> None:
    for path in (
        _APPLICATION / "production_editorial_enrichment.py",
        _DOMAIN / "production_editorial_enrichment.py",
    ):
        tree = ast.parse(path.read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        imported.extend(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        )
        for forbidden in ("pandoc", "typst", "d2", "mermaid", "graphviz", "tikz"):
            assert all(forbidden not in name.lower() for name in imported)
