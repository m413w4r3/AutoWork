"""Canonical payload checks used to ground VirusTotal evidence references."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cti_app.domain.investigation import canonical_json_bytes

MAX_VT_CANONICAL_EVIDENCE_BYTES = 4 * 1024 * 1024
_VT_FILE_HASH_PATTERN = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
_VT_HASH_OBJECT_TYPES = {"sample", "file", "sample_observation", "sha256", "md5", "sha1"}
VT_RELATIONSHIP_HASH_GROUNDING_FAMILIES = frozenset(
    {
        "vt.file_report",
        "vt.behaviour",
        "vt.behaviour_pcap",
        "vt.relationships",
        "vt.download_sample",
    }
)


def canonical_vt_evidence_payload(raw: bytes, expected_sha256: str) -> dict[str, Any] | None:
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
    return any(pattern.search(value) is not None for value in nested_vt_evidence_strings(payload))


def vt_observation_anchors_hash(
    *,
    observation_kind: object,
    observation_source: object,
    object_type: object,
    object_id: object,
    requested_hash: str,
    canonical_payload_bytes: bytes | None = None,
    canonical_payload_sha256: str | None = None,
) -> bool:
    """Whether a current observation directly or canonically anchors a VT hash.

    Direct hash objects retain their established hash aliases. A related child
    hash is accepted only from a SHA-256, canonical VirusTotal file relationship
    payload whose bytes match the observation's recorded SHA-256.
    """
    normalized_hash = requested_hash.casefold() if isinstance(requested_hash, str) else ""
    normalized_object_id = object_id.casefold() if isinstance(object_id, str) else ""
    normalized_object_type = object_type.casefold() if isinstance(object_type, str) else ""
    if not _VT_FILE_HASH_PATTERN.fullmatch(normalized_hash):
        return False
    if normalized_object_type in _VT_HASH_OBJECT_TYPES and normalized_object_id == normalized_hash:
        return True
    if (
        len(normalized_hash) != 64
        or not isinstance(observation_kind, str)
        or observation_kind.casefold() != "vt_relationship"
        or not isinstance(observation_source, str)
        or observation_source.casefold() != "virustotal"
        or normalized_object_type != "file"
        or canonical_payload_bytes is None
        or canonical_payload_sha256 is None
    ):
        return False
    payload = canonical_vt_evidence_payload(canonical_payload_bytes, canonical_payload_sha256)
    return payload is not None and vt_hash_payload_contains(payload, normalized_hash)


def requested_vt_file_hash(family: str, canonical_request: object) -> str | None:
    """Extract the normalized target hash from a canonical VT request."""
    if not isinstance(canonical_request, Mapping):
        return None
    path = canonical_request.get("path")
    if isinstance(path, str):
        path_parts = path.split("/")
        try:
            files_index = path_parts.index("files")
        except ValueError:
            files_index = -1
        if files_index >= 0 and files_index + 1 < len(path_parts):
            candidate = path_parts[files_index + 1]
            if re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", candidate):
                return candidate
        if path.rstrip("/").endswith("/file/report"):
            query = canonical_request.get("query")
            if isinstance(query, (list, tuple)):
                resources = [
                    item[1]
                    for item in query
                    if isinstance(item, (list, tuple))
                    and len(item) == 2
                    and item[0] == "resource"
                    and isinstance(item[1], str)
                ]
                if len(resources) == 1 and re.fullmatch(
                    r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", resources[0]
                ):
                    return resources[0]
    if family == "vt.download_sample":
        sha256 = canonical_request.get("sha256")
        if isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256):
            return sha256
    return None
