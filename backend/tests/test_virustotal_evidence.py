from __future__ import annotations

import hashlib

import pytest

from cti_app.application.virustotal_evidence import (
    MAX_VT_CANONICAL_EVIDENCE_BYTES,
    requested_vt_file_hash,
    vt_observation_anchors_hash,
)
from cti_app.domain.investigation import canonical_json_bytes


def _anchor(
    *,
    requested_hash: str,
    object_id: str,
    payload: bytes | None = None,
    payload_sha256: str | None = None,
    kind: str = "vt_relationship",
    source: str = "VirusTotal",
    object_type: str = "file",
) -> bool:
    return vt_observation_anchors_hash(
        observation_kind=kind,
        observation_source=source,
        object_type=object_type,
        object_id=object_id,
        requested_hash=requested_hash,
        canonical_payload_bytes=payload,
        canonical_payload_sha256=payload_sha256,
    )


def test_vt_observation_anchor_accepts_exact_parent_hash_without_payload() -> None:
    parent_hash = "b" * 64

    assert _anchor(requested_hash=parent_hash, object_id=parent_hash)


def test_vt_relationship_anchor_accepts_hash_in_verified_canonical_payload() -> None:
    child_hash = "c" * 64
    raw = canonical_json_bytes({"relationships": {"dropped_files": [{"id": child_hash}]}})

    assert _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=raw,
        payload_sha256=hashlib.sha256(raw).hexdigest(),
    )


@pytest.mark.parametrize(
    ("kind", "source", "object_type"),
    [
        ("vt_file_report", "VirusTotal", "file"),
        ("vt_relationship", "Other", "file"),
        ("vt_relationship", "VirusTotal", "sample"),
    ],
    ids=["wrong-kind", "wrong-source", "wrong-object-type"],
)
def test_vt_relationship_anchor_rejects_non_relationship_metadata(
    kind: str, source: str, object_type: str
) -> None:
    child_hash = "c" * 64
    raw = canonical_json_bytes({"related": child_hash})

    assert not _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=raw,
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        kind=kind,
        source=source,
        object_type=object_type,
    )


def test_vt_relationship_anchor_rejects_missing_hash_and_invalid_canonical_evidence() -> None:
    child_hash = "c" * 64
    raw = canonical_json_bytes({"related": "d" * 64})

    assert not _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=raw,
        payload_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert not _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=raw,
        payload_sha256="e" * 64,
    )
    assert not _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=raw + b" ",
        payload_sha256=hashlib.sha256(raw + b" ").hexdigest(),
    )
    oversized = b"x" * (MAX_VT_CANONICAL_EVIDENCE_BYTES + 1)
    assert not _anchor(
        requested_hash=child_hash,
        object_id="b" * 64,
        payload=oversized,
        payload_sha256=hashlib.sha256(oversized).hexdigest(),
    )


def test_requested_vt_file_hash_resolves_download_hash_and_file_path() -> None:
    sample_hash = "c" * 64

    assert requested_vt_file_hash("vt.download_sample", {"sha256": sample_hash}) == sample_hash
    assert (
        requested_vt_file_hash("vt.file_report", {"path": f"/api/v3/files/{sample_hash}"})
        == sample_hash
    )
    assert requested_vt_file_hash("vt.download_sample", {"sha256": "not-a-hash"}) is None
