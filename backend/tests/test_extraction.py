from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from cti_app.application.extraction import (
    EvidenceExtractionService,
    QwenEvidenceOutput,
    extract_indicators,
    parse_document,
)
from cti_app.application.model_gateway import StructuredExtractionModel
from cti_app.domain.collection import ClaimKind, DetectedMimeType, IndicatorKind

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

    with pytest.raises(ValueError, match="ioc claim is absent"):
        await service.extract_claims(
            parsed.text,
            subject_id=uuid4(),
            edition_id=uuid4(),
            group_id=uuid4(),
            source_document_id=uuid4(),
            artifact_id=uuid4(),
            external_llm_allowed=False,
        )


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

    claims = await service.extract_claims(
        parsed.text,
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        source_document_id=uuid4(),
        artifact_id=uuid4(),
        external_llm_allowed=False,
    )

    assert claims[0].kind is ClaimKind.DATE
    assert claims[0].span.passage(parsed.text) == quote
