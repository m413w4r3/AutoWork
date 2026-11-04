from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from m14_create_investigation import make_g2_proposal
from m14_export_sources import _write_verified
from m14_seed_subject import (
    load_initial_hashes,
    source_document_references,
    synthesis_hash_coverage,
    validate_source_files,
)


def test_source_export_writes_digest_named_file_and_accepts_verified_replay(
    tmp_path,
) -> None:
    content = b"source bytes"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / digest

    _write_verified(target, content, digest)
    _write_verified(target, content, digest)

    assert target.read_bytes() == content


def test_source_export_refuses_digest_mismatch(tmp_path) -> None:
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _write_verified(tmp_path / ("0" * 64), b"bad", "0" * 64)


def test_initial_corpus_hash_file_requires_fifteen_unique_sha256_values(
    tmp_path,
) -> None:
    values = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(15)]
    path = tmp_path / "hashes.txt"
    path.write_text("\n".join(values))

    assert load_initial_hashes(path) == tuple(values)
    path.write_text("\n".join(values[:-1]))
    with pytest.raises(ValueError, match="15 unique SHA-256"):
        load_initial_hashes(path)


def test_synthesis_hash_coverage_checks_only_synthesis_canonical_content() -> None:
    wanted = ("a" * 64, "b" * 64)
    state = {
        "artifacts": {
            "synthesis": {"canonical_content": {"cited": wanted[0]}},
            "extraction": {"canonical_content": {"cited": wanted[1]}},
        }
    }

    assert synthesis_hash_coverage(state, wanted) == (wanted[0],)


def test_state_source_document_reference_scan_covers_singular_and_plural_fields() -> (
    None
):
    payload = {
        "source_document_id": "one",
        "nested": [{"source_document_ids": ["two", "three"]}],
    }

    assert source_document_references(payload) == {"one", "two", "three"}


def test_seed_preflight_verifies_source_file_digest_and_size(tmp_path) -> None:
    content = b"article bytes"
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / digest).write_bytes(content)
    manifest = {
        "documents": [{"id": "source-id", "sha256": digest, "size": len(content)}]
    }

    validate_source_files(manifest=manifest, source_dir=tmp_path)
    manifest["documents"][0]["size"] += 1
    with pytest.raises(ValueError, match="size and SHA-256"):
        validate_source_files(manifest=manifest, source_dir=tmp_path)


def test_g2_proposal_is_unapproved_and_respects_red_tlp() -> None:
    proposal = make_g2_proposal(
        investigation_id=uuid4(),
        tlp="RED",
        do_not_submit=False,
        external_llm_allowed=True,
    )

    assert proposal["approved"] is False
    assert proposal["allowed_providers"] == []
    assert proposal["budget_limits"]["bridge_turns"] == 0
    assert proposal["disclosure"]["external_llm_allowed"] is False


def test_g2_proposal_contains_general_vt_and_bridge_caps() -> None:
    proposal = make_g2_proposal(
        investigation_id=uuid4(),
        tlp="GREEN",
        do_not_submit=True,
        external_llm_allowed=True,
    )

    assert proposal["allowed_providers"] == ["vt", "bridge"]
    assert proposal["budget_limits"] == {
        "bridge_turns": 200,
        "vt_units": 10_000,
        "downloads": 1_000,
    }
    assert proposal["disclosure"]["do_not_submit"] is True
    assert proposal["approved"] is False
