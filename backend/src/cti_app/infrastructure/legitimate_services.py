"""Load the checked-in legitimate-service publication policy."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from cti_app.domain.legitimate_services import (
    LegitimateServicePolicyError,
    LegitimateServiceRegistry,
    validate_legitimate_services_document,
)

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "legitimate_services_v1.json"


def _load_from_path(path: Path) -> LegitimateServiceRegistry:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegitimateServicePolicyError("invalid legitimate-services policy") from exc
    return validate_legitimate_services_document(document)


@lru_cache(maxsize=1)
def _load_default() -> LegitimateServiceRegistry:
    return _load_from_path(_DEFAULT_POLICY_PATH)


def load_legitimate_services(path: Path | None = None) -> LegitimateServiceRegistry:
    if path is None:
        return _load_default()
    return _load_from_path(path)
