from __future__ import annotations

import hashlib
from pathlib import Path

from cti_app.application.extraction import parse_document
from cti_app.application.production_extraction import (
    archived_source_chunks,
    build_source_evidence_document,
    effective_evidence_sha256,
    normalize_effective_evidence_text,
    source_checkpoint_identity,
    source_prompt_version,
)
from cti_app.application.production_source_evidence import (
    scope_source_evidence_document,
    source_evidence_document_from_html,
)
from cti_app.domain.collection import DetectedMimeType
from cti_app.domain.production import ExtractionProfile
from cti_app.domain.production_references import ProductionReferenceKind

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "extraction_effective_hash"
_CAPTURES = (
    "anthropic_74edd474.html",
    "anthropic_e82a4067.html",
    "anthropic_17e79a15.html",
    "anthropic_1aa8d48b.html",
)


def test_reduced_real_capture_states_have_one_effective_evidence_hash() -> None:
    raw_captures = [(_FIXTURE_ROOT / name).read_bytes() for name in _CAPTURES]
    documents = [
        build_source_evidence_document(content, mime_type="text/html") for content in raw_captures
    ]
    effective_texts = [
        normalize_effective_evidence_text(document.parsed_text) for document in documents
    ]
    effective_hashes = [effective_evidence_sha256(text) for text in effective_texts]

    assert len(set(raw_captures)) == 4
    assert len(set(effective_texts)) == 1
    assert len(effective_texts[0]) == 17_914
    assert (
        effective_hashes == ["c2422851d5dd490e66f362e1dc4ba8eb7493f1dbcda9615f081fde193fef812e"] * 4
    )
    assert "self.__next_f.push" not in effective_texts[0]
    assert "main-current" not in effective_texts[0]


def test_checkpoint_identity_uses_effective_text_hash_in_raw_hash_column() -> None:
    raw = (_FIXTURE_ROOT / _CAPTURES[0]).read_bytes()
    document = build_source_evidence_document(raw, mime_type="text/html")
    effective_hash = effective_evidence_sha256(document.parsed_text)
    identity = source_checkpoint_identity(
        effective_evidence_sha256=effective_hash,
        profile=ExtractionProfile.FULL,
        prompt_version=source_prompt_version(ExtractionProfile.FULL),
    )

    assert identity["source_content_sha256"] == effective_hash
    assert effective_hash != hashlib.sha256(raw).hexdigest()


def test_effective_text_normalization_only_canonicalizes_unicode_and_newlines() -> None:
    assert normalize_effective_evidence_text("\r\nCafe\u0301\r\n") == "Café"


def test_case_scopes_create_case_specific_cache_identities() -> None:
    html = """
    <article>
      <p>Shared report preamble.</p>
      <h2>GTG-30004: first case</h2><p>FirstCaseRAT activity.</p>
      <h2>GTG-30005: second case</h2><p>SecondCaseRAT activity.</p>
    </article>
    """
    parsed = parse_document(html.encode(), DetectedMimeType.HTML)
    document = source_evidence_document_from_html(parsed.text, html)

    def scope(case_id: str):
        return scope_source_evidence_document(
            document,
            subject_title=f"Subject {case_id}",
            actor_or_campaign="",
            canonical_url="https://example.test/report",
            source_kind=ProductionReferenceKind.PUBLICATION,
            mime_type="text/html",
        )

    first = scope("GTG-30004")
    same_case = scope("GTG-30004")
    second = scope("GTG-30005")
    first_hash = effective_evidence_sha256(first.parsed_text)
    same_case_hash = effective_evidence_sha256(same_case.parsed_text)
    second_hash = effective_evidence_sha256(second.parsed_text)

    assert first.scope is not None and second.scope is not None
    assert first_hash == same_case_hash
    assert first_hash != second_hash

    def checkpoint_identity(digest: str):
        return source_checkpoint_identity(
            effective_evidence_sha256=digest,
            profile=ExtractionProfile.FULL,
            prompt_version=source_prompt_version(ExtractionProfile.FULL),
        )

    assert checkpoint_identity(first_hash) == checkpoint_identity(same_case_hash)
    assert checkpoint_identity(first_hash) != checkpoint_identity(second_hash)
    assert first.scope.case_id == "GTG-30004"
    assert second.scope.case_id == "GTG-30005"


def test_real_report_chunk_plan_drops_from_eleven_chunks_to_one_per_case() -> None:
    # Measured from the decoded report HTML in the audit cache. The test keeps
    # only reduced source fixtures; chunk planning depends on these exact text
    # lengths and does not require a model call or the 1.2 MB HTML capture.
    full_report_chars = 258_860
    scoped_report_chars = {
        "GTG-30004": 8_361,
        "GTG-30005": 8_992,
        "GTG-30006": 16_556,
        "GTG-34007": 10_795,
    }

    assert len(archived_source_chunks("x" * full_report_chars)) == 11
    assert {
        case_id: len(archived_source_chunks("x" * size))
        for case_id, size in scoped_report_chars.items()
    } == {case_id: 1 for case_id in scoped_report_chars}

    reduced = build_source_evidence_document(
        (_FIXTURE_ROOT / _CAPTURES[0]).read_bytes(), mime_type="text/html"
    )
    assert len(archived_source_chunks(reduced.parsed_text)) == 1
    for case_id in scoped_report_chars:
        scoped = scope_source_evidence_document(
            reduced,
            subject_title=f"Threat activity {case_id}",
            actor_or_campaign="",
            canonical_url="https://www.anthropic.com/threat-intelligence-report-september-2026",
            source_kind=ProductionReferenceKind.PUBLICATION,
            mime_type="text/html",
        )
        assert scoped.scope is not None
        assert len(archived_source_chunks(scoped.parsed_text)) == 1
