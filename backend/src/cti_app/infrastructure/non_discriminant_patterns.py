from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from cti_app.domain.goodware import (
    GoodwareBaselineError,
    NonDiscriminantPatternRegistry,
    validate_non_discriminant_pattern_document,
)

_DEFAULT_PATTERN_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "non_discriminant_patterns_v1.json"
)


def _load_from_path(path: Path) -> NonDiscriminantPatternRegistry:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodwareBaselineError("invalid non-discriminant pattern registry") from exc
    return validate_non_discriminant_pattern_document(document)


@lru_cache(maxsize=1)
def _load_default() -> NonDiscriminantPatternRegistry:
    return _load_from_path(_DEFAULT_PATTERN_PATH)


def load_non_discriminant_patterns(
    path: Path | None = None,
) -> NonDiscriminantPatternRegistry:
    if path is None:
        return _load_default()
    return _load_from_path(path)
