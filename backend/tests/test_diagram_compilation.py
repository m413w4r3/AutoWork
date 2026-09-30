from __future__ import annotations

import ast
import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cti_app.application import diagram_compilation
from cti_app.application.diagram_compilation import (
    CompiledDiagram,
    DiagramCompilationError,
    DiagramCompilerOutputInvalidError,
    DiagramCompilerOutputTooLargeError,
    DiagramCompilerProcessError,
    DiagramCompilerTimeoutError,
    DiagramCompilerUnavailableError,
    DiagramCompilerVersionError,
    UnsupportedDiagramStructureError,
)


def _compiled_diagram(**overrides: object) -> CompiledDiagram:
    source_bytes = b"source"
    media_bytes = b"media"
    values: dict[str, object] = {
        "diagram_key": "diagram-1",
        "source_format": "d2",
        "source_bytes": source_bytes,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "media_type": "image/svg+xml",
        "media_bytes": media_bytes,
        "media_sha256": hashlib.sha256(media_bytes).hexdigest(),
        "compiler": "d2",
        "compiler_version": "0.9.0",
        "compiler_policy_version": "diagram-d2-svg-v1",
    }
    values.update(overrides)
    return CompiledDiagram(**values)  # type: ignore[arg-type]


def test_compiled_diagram_accepts_matching_byte_hashes_and_is_immutable() -> None:
    compiled = _compiled_diagram()

    assert compiled.source_sha256 == hashlib.sha256(compiled.source_bytes).hexdigest()
    assert compiled.media_sha256 == hashlib.sha256(compiled.media_bytes).hexdigest()
    with pytest.raises(FrozenInstanceError):
        compiled.compiler = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "digest"),
    (
        ("source_sha256", "0" * 63),
        ("source_sha256", "A" * 64),
        ("media_sha256", "g" * 64),
    ),
)
def test_compiled_diagram_rejects_noncanonical_hashes(field_name: str, digest: str) -> None:
    with pytest.raises(ValueError, match="lowercase hexadecimal"):
        _compiled_diagram(**{field_name: digest})


@pytest.mark.parametrize(
    ("field_name", "digest"),
    (("source_sha256", "0" * 64), ("media_sha256", "f" * 64)),
)
def test_compiled_diagram_rejects_hashes_that_do_not_match_bytes(
    field_name: str, digest: str
) -> None:
    with pytest.raises(ValueError, match="does not match"):
        _compiled_diagram(**{field_name: digest})


def test_compiled_diagram_rejects_empty_identity_fields() -> None:
    with pytest.raises(ValueError, match="diagram_key must be non-empty"):
        _compiled_diagram(diagram_key=" ")


def test_compilation_errors_have_distinct_stable_codes_without_infrastructure_imports() -> None:
    error_types = (
        DiagramCompilerUnavailableError,
        DiagramCompilerVersionError,
        DiagramCompilerTimeoutError,
        DiagramCompilerProcessError,
        DiagramCompilerOutputTooLargeError,
        DiagramCompilerOutputInvalidError,
        UnsupportedDiagramStructureError,
    )
    expected_codes = (
        "diagram_compiler_unavailable",
        "diagram_compiler_version_mismatch",
        "diagram_compiler_timeout",
        "diagram_compiler_process_failure",
        "diagram_compiler_output_too_large",
        "diagram_compiler_output_invalid",
        "unsupported_diagram_structure",
    )

    assert all(issubclass(error_type, DiagramCompilationError) for error_type in error_types)
    assert tuple(error_type.code for error_type in error_types) == expected_codes
    assert all(error_type.__module__ == diagram_compilation.__name__ for error_type in error_types)

    source_path = Path(diagram_compilation.__file__)
    module_ast = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = []
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
    assert all(not name.startswith("cti_app.infrastructure") for name in imported_modules)
