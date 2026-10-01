"""Resolve the repository and deployment paths for shared Typst inputs."""

from __future__ import annotations

import os
from pathlib import Path


def typst_bundle_paths() -> tuple[Path, Path, Path]:
    source_path = Path(__file__).resolve()
    repository_root = source_path.parents[4]
    if not (repository_root / "chpTypst").is_dir():
        repository_root = source_path.parents[3]
    chp_typst_root = repository_root / "chpTypst"
    font_bundle_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(chp_typst_root)))
    default_font_lock_path = Path("/usr/local/share/autowork/typst-fonts.lock")
    if not default_font_lock_path.is_file():
        default_font_lock_path = repository_root / "infra" / "typst-fonts.lock"
    typst_fonts_lock_path = Path(
        os.environ.get("TYPST_FONTS_LOCK_FILE", str(default_font_lock_path))
    )
    return chp_typst_root, font_bundle_root, typst_fonts_lock_path


__all__ = ["typst_bundle_paths"]
