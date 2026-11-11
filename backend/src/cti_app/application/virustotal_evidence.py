"""Canonical payload checks used to ground VirusTotal evidence references."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cti_app.domain.investigation import canonical_json_bytes

MAX_VT_CANONICAL_EVIDENCE_BYTES = 4 * 1024 * 1024


def canonical_vt_evidence_payload(
    raw: bytes, expected_sha256: str
) -> dict[str, Any] | None:
    if (
        not isinstance(raw, bytes)
        or len(raw) > MAX_VT_CANONICAL_EVIDENCE_BYTES
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
            return None
    except Exception:
        return None
    return payload


def nested_vt_evidence_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key
            yield from nested_vt_evidence_strings(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from nested_vt_evidence_strings(nested)


def vt_hash_payload_contains(payload: object, normalized_sha256: str) -> bool:
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(normalized_sha256) + r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    return any(
        pattern.search(value) is not None for value in nested_vt_evidence_strings(payload)
    )
