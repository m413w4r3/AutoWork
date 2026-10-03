"""Permanent architecture gates for the canonical AW-012 Synthesis path."""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SYNTHESIS = BACKEND / "src/cti_app/application/production_synthesis.py"
WORKFLOW = BACKEND / "src/cti_app/application/production_workflow.py"
FRONTEND = BACKEND.parent / "frontend"
BASELINE = BACKEND / "migrations/versions/0001_baseline.py"

LEGACY_SYNTHESIS_INPUTS = (
    "ReferenceReport",
    "load_reference_projection",
    "reference_report_to_json",
    "TechnicalExtraction",
    "load_legacy_technical_extraction",
    "synthesis_projection_payload",
    "source_tiers_by_url",
    "report_source_labels",
    "FORMAT_REPAIR",
    "format_repair",
    "ModelConversationService",
    "add_turn(",
)


def _canonical_workflow_branch() -> str:
    module = ast.parse(WORKFLOW.read_text())
    orchestrator = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionWorkflowOrchestrator"
    )
    method = next(
        node
        for node in orchestrator.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_synthesis_stage"
    )
    return ast.get_source_segment(WORKFLOW.read_text(), method) or ""


def _assert_symbol_absent(path: Path, source: str, symbol: str) -> None:
    assert symbol not in source, f"{path}: forbidden symbol {symbol}"


def _current_artifact_reads(method_name: str) -> set[str]:
    module = ast.parse(WORKFLOW.read_text())
    orchestrator = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionWorkflowOrchestrator"
    )
    method = next(
        node
        for node in orchestrator.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name
    )
    stages: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "get_current"
            and isinstance(function.value, ast.Attribute)
            and function.value.attr == "production_artifacts"
        ):
            continue
        assert len(node.args) == 2
        stage = node.args[1]
        if isinstance(stage, ast.Constant) and isinstance(stage.value, str):
            stages.add(stage.value)
            continue
        if (
            isinstance(stage, ast.Attribute)
            and stage.attr == "value"
            and isinstance(stage.value, ast.Attribute)
        ):
            stages.add(stage.value.attr.lower())
            continue
        raise AssertionError(f"{method_name} reads an unrecognized artifact stage")
    return stages


def test_canonical_synthesis_has_no_legacy_evidence_or_conversation_path() -> None:
    synthesis_source = SYNTHESIS.read_text()
    workflow_branch = _canonical_workflow_branch()

    for forbidden in LEGACY_SYNTHESIS_INPUTS:
        _assert_symbol_absent(SYNTHESIS, synthesis_source, forbidden)
        _assert_symbol_absent(WORKFLOW, workflow_branch, forbidden)

    assert not re.search(r"web_search\s*=\s*True\b", synthesis_source + workflow_branch)


def test_canonical_synthesis_uses_gateway_text_blocks_without_source_fetch() -> None:
    source = SYNTHESIS.read_text()

    assert "class SynthesisProposalV1" in source
    assert "parse_synthesis_proposal_wire" in source
    assert "model_gateway.draft(request)" in source
    assert "model_gateway.draft(request, SynthesisProposalV1)" not in source

    for forbidden in (
        "parse_document(",
        "decoded_blob_id",
        "fetch_source(",
        "httpx.",
        "requests.",
    ):
        assert forbidden not in source


def test_synthesis_and_enrichment_read_only_the_projection_artifact_in_addition() -> None:
    assert _current_artifact_reads("_execute_synthesis_stage") == {
        "extraction",
        "relevance_projection",
    }
    assert _current_artifact_reads("_execute_editorial_enrichment_stage") == {
        "extraction",
        "relevance_projection",
        "synthesis",
    }


def test_backend_runtime_and_schema_have_no_synthesis_conversation_identity() -> None:
    backend_src = BACKEND / "src"
    frontend_src = FRONTEND / "src"
    assert backend_src.is_dir(), f"missing runtime source directory: {backend_src}"
    assert frontend_src.is_dir(), f"missing runtime source directory: {frontend_src}"

    source_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
    for root in (backend_src, frontend_src):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in source_suffixes:
                _assert_symbol_absent(path, path.read_text(), "synthesis_conversation_id")

    _assert_symbol_absent(BASELINE, BASELINE.read_text(), "synthesis_conversation_id")
