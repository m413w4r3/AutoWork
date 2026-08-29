from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from cti_app.domain.goodware import GoodwareBaselineError

SCHEMA_VERSION = "autowork-goodware-index-v2"
NORMALIZATION_VERSION = "autowork-goodware-normalization-v2"
KEY_VERSION = "autowork-goodware-key-v1"
INDEX_FORMAT_VERSION = "autowork-goodware-index-v2"
SOURCE_FORMAT = "yargen-gzip-json-counter-v1"
NON_DISCRIMINANT_PATTERN_VERSION = "non-discriminant-patterns-v1"
INDEX_FILENAME = "goodware-index.sqlite3"
MANIFEST_FILENAME = "manifest.json"

SUPPORTED_FEATURE_KINDS = (
    "string",
    "opcode_fragment16",
    "imphash",
    "export",
)
_SUPPORTED_FEATURE_KIND_SET = frozenset(SUPPORTED_FEATURE_KINDS)


class GoodwareImportError(GoodwareBaselineError):
    pass


class GoodwareMeasurementError(GoodwareBaselineError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def source_set_sha256(sources: Sequence[Mapping[str, object]]) -> str:
    stable = [
        {
            "filename": source["filename"],
            "feature_kind": source["feature_kind"],
            "sha256": source["sha256"],
            "size": source["size"],
        }
        for source in sources
    ]
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


def goodware_lookup_key(feature_kind: str, normalized_value: str) -> bytes:
    """Return the frozen key for an already-normalized runtime feature."""
    if not isinstance(feature_kind, str) or feature_kind not in _SUPPORTED_FEATURE_KIND_SET:
        raise GoodwareMeasurementError(f"unsupported feature kind: {feature_kind}")
    if not isinstance(normalized_value, str):
        raise GoodwareMeasurementError("normalized feature value must be a string")
    try:
        kind_bytes = feature_kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GoodwareMeasurementError("feature kind must be ASCII") from exc
    return hashlib.sha256(
        KEY_VERSION.encode("ascii") + b"\0" + kind_bytes + b"\0" + normalized_value.encode("utf-8")
    ).digest()


lookup_key = goodware_lookup_key
canonical_lookup_key = goodware_lookup_key


def baseline_fingerprint_sha256(
    source_set_sha256: str,
    *,
    pattern_version: str = NON_DISCRIMINANT_PATTERN_VERSION,
) -> str:
    value = {
        "normalization_version": NORMALIZATION_VERSION,
        "pattern_version": pattern_version,
        "source_set_sha256": source_set_sha256,
    }
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# Compatibility for callers that used the original application-local helpers.
_canonical_json = canonical_json
_source_set_sha256 = source_set_sha256
