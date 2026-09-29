"""Permanent boundary gate for the AW-013 canonical production path."""

from __future__ import annotations

import inspect
from pathlib import Path

from cti_app.application.production_workflow import ProductionWorkflowOrchestrator

_APPLICATION = Path(__file__).resolve().parents[1] / "src" / "cti_app" / "application"
_CANONICAL_FILES = (
    "pandoc_rendering.py",
    "production_stages.py",
    "publication_builder.py",
    "publication_assembly.py",
    "publication_qa.py",
    "publication_renderer.py",
)
_FORBIDDEN = (
    "ReferenceReport",
    "TechnicalExtraction",
    "load_reference_projection",
    "report_source_labels",
    "project_legacy_synthesis_markdown",
    "build_reference_numbering",
    "apply_numbering",
    "[S1]",
)


def test_canonical_assembly_qa_and_orchestrator_have_no_legacy_dependency() -> None:
    sources = [(_APPLICATION / name).read_text() for name in _CANONICAL_FILES]
    sources.append(inspect.getsource(ProductionWorkflowOrchestrator._execute_assembly_stage))
    for source in sources:
        for forbidden in _FORBIDDEN:
            assert forbidden not in source
