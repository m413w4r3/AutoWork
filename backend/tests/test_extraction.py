from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from cti_app.application.extraction import (
    ChunkingPolicy,
    DocumentParsingError,
    EvidenceExtractionService,
    PdfParsingPolicy,
    QwenEvidenceOutput,
    extract_indicators,
    parse_document,
    segment_text,
)
from cti_app.application.model_gateway import StructuredExtractionModel
from cti_app.domain.collection import ClaimKind, DetectedMimeType, Indicator, IndicatorKind

FIXTURES = Path(__file__).parent / "fixtures"


class StructuredFixtureModel:
    def __init__(self, output: QwenEvidenceOutput) -> None:
        self.output = output
        self.requests: list[object] = []

    async def extract(self, request: object, output_schema: object) -> object:
        del output_schema
        self.requests.append(request)
        return SimpleNamespace(structured_output=self.output)


def test_multilingual_html_is_cleaned_without_script_and_keeps_metadata() -> None:
    parsed = parse_document(
        (FIXTURES / "source_multilingual.html").read_bytes(),
        DetectedMimeType.HTML,
    )

    assert "Campagne ExampleRAT" in parsed.text
    assert "English summary" in parsed.text
    assert "execute malware" not in parsed.text
    assert parsed.metadata["author"] == "Équipe de recherche"
    assert parsed.metadata["article:published_time"] == "2026-07-12T10:00:00Z"


def test_multilingual_pdf_fixture_is_extracted_without_execution() -> None:
    parsed = parse_document(
        (FIXTURES / "source_multilingual.pdf").read_bytes(),
        DetectedMimeType.PDF,
    )

    assert "Bonjour rapport CTI" in parsed.text
    assert "English evidence summary" in parsed.text


def test_defanged_indicators_keep_original_and_normalize_value() -> None:
    parsed = parse_document(
        (FIXTURES / "source_multilingual.html").read_bytes(),
        DetectedMimeType.HTML,
    )
    indicators = extract_indicators(
        parsed.text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
    )
    values = {(item.kind, item.original_value, item.normalized_value) for item in indicators}

    assert (IndicatorKind.DOMAIN, "evil[.]example", "evil.example") in values
    assert (IndicatorKind.IP, "203[.]0[.]113[.]9", "203.0.113.9") in values
    assert (
        IndicatorKind.URL,
        "hxxps[:]//evil[.]example/path",
        "https://evil.example/path",
    ) in values
    assert any(item.kind is IndicatorKind.CVE for item in indicators)
    assert any(item.kind is IndicatorKind.ATTACK_ID for item in indicators)


def _indicators(text: str) -> tuple[Indicator, ...]:
    return extract_indicators(
        text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
    )


def test_text_and_json_documents_preserve_ioc_text() -> None:
    text = "IPv6 2001:db8::42"
    assert parse_document(text.encode(), DetectedMimeType.TEXT).text == text

    raw_json = '{"ioc":"2001:db8::42","hash":"' + "a" * 128 + '"}'
    parsed = parse_document(raw_json.encode(), DetectedMimeType.JSON)
    assert parsed.text == raw_json
    assert any(item.normalized_value == "2001:db8::42" for item in _indicators(parsed.text))


def test_ipv6_validation_and_sha512() -> None:
    sha512 = "a" * 128
    indicators = _indicators(f"valid 2001:db8::1 loopback ::1 invalid 2001:db8:zzzz::1 {sha512}")

    assert any(
        item.kind is IndicatorKind.IP and item.normalized_value == "2001:db8::1"
        for item in indicators
    )
    assert any(
        item.kind is IndicatorKind.IP and item.normalized_value == "::1" for item in indicators
    )
    assert not any("zzzz" in item.original_value for item in indicators)
    assert any(
        item.kind is IndicatorKind.HASH and item.normalized_value == sha512 for item in indicators
    )


def test_shared_normalization_accepts_defanged_forms_and_rejects_bad_url_port() -> None:
    indicators = _indicators(
        "evil{.}example user[@]evil[.]example user[at]evil[.]example "
        "hxxps[:]//EVIL[.]example/path 2001[:]db8::1 http://example.test:bad/path"
    )
    values = {(item.kind, item.original_value, item.normalized_value) for item in indicators}

    assert (IndicatorKind.DOMAIN, "evil{.}example", "evil.example") in values
    assert (IndicatorKind.EMAIL, "user[@]evil[.]example", "user@evil.example") in values
    assert (IndicatorKind.EMAIL, "user[at]evil[.]example", "user@evil.example") in values
    assert (
        IndicatorKind.URL,
        "hxxps[:]//EVIL[.]example/path",
        "https://evil.example/path",
    ) in values
    assert (IndicatorKind.IP, "2001[:]db8::1", "2001:db8::1") in values
    assert not any("example.test:bad" in item.original_value for item in indicators)


def test_hash_families_and_spans_are_not_deduplicated() -> None:
    md5 = "a" * 32
    sha1 = "b" * 40
    sha256 = "c" * 64
    text = f"{md5} {sha1} {sha256} {sha1}"
    indicators = _indicators(text)

    assert {len(item.original_value) for item in indicators} == {32, 40, 64}
    repeated = [item for item in indicators if item.normalized_value == sha1]
    assert len(repeated) == 2
    assert repeated[0].span != repeated[1].span


def test_url_does_not_emit_overlapping_domain() -> None:
    indicators = _indicators("https://example.test/path")
    assert [item.kind for item in indicators] == [IndicatorKind.URL]


def test_html_links_keep_href_and_anchor_text_but_skip_script_and_style() -> None:
    parsed = parse_document(
        b'<a href="/report">Read <strong>report</strong></a>'
        b'<script><a href="/bad">bad</a></script>'
        b'<style>.hidden { content: "bad" }</style>',
        DetectedMimeType.HTML,
    )

    assert [(link.href, link.anchor_text) for link in parsed.links] == [("/report", "Read report")]
    assert "bad" not in parsed.text


def test_synthetic_olalampo_like_fixture_extracts_all_network_artifacts() -> None:
    text = (
        "alpha.example beta.example gamma.test delta.org "
        "192.0.2.1 192.0.2.2 198.51.100.1 203.0.113.9 "
        "1111111111111111111111111111111111111111 "
        "2222222222222222222222222222222222222222 "
        "3333333333333333333333333333333333333333"
    )
    indicators = _indicators(text)

    assert {item.normalized_value for item in indicators if item.kind is IndicatorKind.DOMAIN} >= {
        "alpha.example",
        "beta.example",
        "gamma.test",
        "delta.org",
    }
    assert {item.normalized_value for item in indicators if item.kind is IndicatorKind.IP} >= {
        "192.0.2.1",
        "192.0.2.2",
        "198.51.100.1",
        "203.0.113.9",
    }
    assert len([item for item in indicators if item.kind is IndicatorKind.HASH]) >= 3


async def test_hallucinated_literal_ioc_claim_is_rejected() -> None:
    parsed = parse_document(
        (FIXTURES / "source_multilingual.html").read_bytes(),
        DetectedMimeType.HTML,
    )
    output = QwenEvidenceOutput.model_validate(
        {
            "facts": [
                {
                    "kind": "ioc",
                    "value": "198.51.100.99",
                    "exact_quote": "Campagne ExampleRAT",
                    "confidence": "high",
                    "uncertainty": None,
                }
            ]
        }
    )
    service = EvidenceExtractionService(
        cast(StructuredExtractionModel, StructuredFixtureModel(output))
    )

    outcome = await service.extract_claims(
        parsed.text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
        external_llm_allowed=False,
    )

    assert outcome.claims == ()
    assert "ioc claim is absent" in outcome.rejected_proposals[0].reason


async def test_claim_span_highlights_exact_source_passage() -> None:
    parsed = parse_document(
        (FIXTURES / "source_multilingual.html").read_bytes(),
        DetectedMimeType.HTML,
    )
    quote = "ExampleRAT le 12 juillet 2026"
    output = QwenEvidenceOutput.model_validate(
        {
            "facts": [
                {
                    "kind": "date",
                    "value": "12 juillet 2026",
                    "exact_quote": quote,
                    "confidence": "high",
                    "uncertainty": None,
                }
            ]
        }
    )
    service = EvidenceExtractionService(
        cast(StructuredExtractionModel, StructuredFixtureModel(output))
    )

    outcome = await service.extract_claims(
        parsed.text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
        external_llm_allowed=False,
    )

    assert outcome.claims[0].kind is ClaimKind.DATE
    assert outcome.claims[0].span.passage(parsed.text) == quote


def _pdf_bytes(*, pages: int = 1, encrypted: bool = False, metadata: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if metadata is not None:
        writer.add_metadata({"/Title": metadata})
    if encrypted:
        writer.encrypt("test-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_page_limit_is_explicit() -> None:
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(
            _pdf_bytes(pages=2),
            DetectedMimeType.PDF,
            PdfParsingPolicy(max_pages=1),
        )
    assert failure.value.code == "pdf_too_many_pages"


def test_pdf_document_size_limit_is_explicit() -> None:
    content = _pdf_bytes()
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(
            content,
            DetectedMimeType.PDF,
            PdfParsingPolicy(max_document_bytes=len(content) - 1),
        )
    assert failure.value.code == "pdf_too_large"


def test_pdf_text_limit_is_explicit() -> None:
    content = (FIXTURES / "source_multilingual.pdf").read_bytes()
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(
            content,
            DetectedMimeType.PDF,
            PdfParsingPolicy(max_text_chars=5),
        )
    assert failure.value.code == "pdf_text_too_large"


def test_encrypted_pdf_is_explicit() -> None:
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(_pdf_bytes(encrypted=True), DetectedMimeType.PDF)
    assert failure.value.code == "pdf_encrypted"


def test_malformed_pdf_is_explicit() -> None:
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(b"%PDF-1.7 malformed", DetectedMimeType.PDF)
    assert failure.value.code == "pdf_malformed"


def test_pdf_timeout_is_explicit() -> None:
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(
            _pdf_bytes(),
            DetectedMimeType.PDF,
            PdfParsingPolicy(timeout_seconds=0.000001),
        )
    assert failure.value.code == "pdf_timeout"


def test_pdf_metadata_limit_is_explicit() -> None:
    with pytest.raises(DocumentParsingError) as failure:
        parse_document(
            _pdf_bytes(metadata="a" * 100),
            DetectedMimeType.PDF,
            PdfParsingPolicy(max_metadata_length=10),
        )
    assert failure.value.code == "pdf_metadata_too_large"


def test_segmentation_is_deterministic_and_bounded() -> None:
    policy = ChunkingPolicy(max_chars=20, overlap_chars=5)
    first = segment_text("0123456789" * 6, policy)
    second = segment_text("0123456789" * 6, policy)

    assert first == second
    assert all(len(chunk.text) <= 20 for chunk in first)
    assert first[1].start_offset == 15


async def test_invalid_model_proposal_does_not_remove_valid_claim() -> None:
    output = QwenEvidenceOutput.model_validate(
        {
            "actors": [
                {
                    "kind": "name",
                    "value": "ExampleRAT",
                    "exact_quote": "ExampleRAT",
                    "confidence": "high",
                },
                {
                    "kind": "ttp",
                    "value": "T1059",
                    "exact_quote": "ExampleRAT",
                    "confidence": "low",
                },
            ]
        }
    )
    service = EvidenceExtractionService(
        cast(StructuredExtractionModel, StructuredFixtureModel(output))
    )

    outcome = await service.extract_claims(
        "ExampleRAT uses PowerShell.",
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
        external_llm_allowed=False,
    )

    assert [item.value for item in outcome.claims] == ["ExampleRAT"]
    assert "cannot emit" in outcome.rejected_proposals[0].reason


async def test_overlap_claim_is_deduplicated_without_losing_chunk_provenance() -> None:
    quote = "ExampleRAT"
    output = QwenEvidenceOutput.model_validate(
        {
            "actors": [
                {
                    "kind": "name",
                    "value": quote,
                    "exact_quote": quote,
                    "confidence": "high",
                }
            ]
        }
    )
    model = StructuredFixtureModel(output)
    service = EvidenceExtractionService(
        cast(StructuredExtractionModel, model),
        chunking_policy=ChunkingPolicy(max_chars=40, overlap_chars=20),
    )
    text = "x" * 25 + quote + "y" * 25

    outcome = await service.extract_claims(
        text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
        external_llm_allowed=False,
    )

    assert len(outcome.claims) == 1
    assert len(outcome.claims[0].extraction_payload["overlap_provenance"]) == 1
