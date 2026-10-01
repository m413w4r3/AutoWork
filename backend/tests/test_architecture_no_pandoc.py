"""Prevent the removed publication renderer from returning to product code."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GUARD = Path(__file__).resolve()
_PRODUCT_ROOTS = (
    _ROOT / "backend" / "src",
    _ROOT / "scripts",
    _ROOT / "infra",
    _ROOT / ".github",
)
_PRODUCT_FILES = (_ROOT / "Makefile", _ROOT / "backend" / "pyproject.toml")
_SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".lock",
    ".Dockerfile",
    ".ini",
    ".cfg",
}
_FORBIDDEN_REFERENCES = (
    "pandoc_rendering",
    "pandoc_export",
    "docx_postprocessing",
    "render_publication_pandoc",
    "render_edition_pandoc",
    "export_markdown_docx",
    "export_publication_docx",
    "DEFAULT_REFERENCE_DOC",
    "edition_template_values",
    "backend/assets/pandoc",
    "generate_pandoc_reference_doc.py",
)
_FORBIDDEN_IMPORT_MODULES = (
    "cti_app.application.pandoc_rendering",
    "cti_app.application.pandoc_export",
    "cti_app.application.docx_postprocessing",
)
_REMOVED_PATHS = (
    _ROOT / "backend/src/cti_app/application/pandoc_rendering.py",
    _ROOT / "backend/src/cti_app/application/pandoc_export.py",
    _ROOT / "backend/src/cti_app/application/docx_postprocessing.py",
    _ROOT / "backend/assets/pandoc",
    _ROOT / "scripts/generate_pandoc_reference_doc.py",
)
_ALLOWED_ABSENCE_CHECKS = {
    _ROOT / "infra/backend.Dockerfile": ("RUN typst --version && ! command -v pandoc",),
    _ROOT / ".github/workflows/ci.yml": (
        "- name: backend image does not contain pandoc",
        "run: docker run --rm --entrypoint sh autowork-backend-smoke -c '! command -v pandoc'",
    ),
}


def _product_source_files() -> tuple[Path, ...]:
    files = {
        path
        for root in _PRODUCT_ROOTS
        for path in root.rglob("*")
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES and path != _GUARD
    }
    files.update(path for path in _PRODUCT_FILES if path.is_file())
    return tuple(sorted(files))


def _imported_modules(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return tuple(modules)


def test_product_code_has_no_reference_to_the_removed_renderer() -> None:
    for path in _product_source_files():
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(_ROOT)
        for check in _ALLOWED_ABSENCE_CHECKS.get(path, ()):
            assert source.count(check) == 1, f"{relative_path} lost its image absence check"
            source = source.replace(check, "")
        assert "pandoc" not in source.lower(), f"{relative_path} refers to the removed binary"
        matches = [name for name in _FORBIDDEN_REFERENCES if name in source]
        assert matches == [], f"{relative_path} references removed renderer names: {matches}"

        if path.suffix == ".py":
            modules = _imported_modules(source)
            forbidden_imports = [
                module
                for module in modules
                if any(
                    module == forbidden or module.startswith(f"{forbidden}.")
                    for forbidden in _FORBIDDEN_IMPORT_MODULES
                )
            ]
            assert forbidden_imports == [], (
                f"{relative_path} imports removed renderer modules: {forbidden_imports}"
            )


def test_removed_renderer_files_and_assets_are_absent() -> None:
    remaining = [path.relative_to(_ROOT) for path in _REMOVED_PATHS if path.exists()]
    assert remaining == []
