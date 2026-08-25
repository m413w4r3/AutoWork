"""Regression coverage for the semantic publication pipeline."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import pytest

from cti_app.application.pandoc_export import export_brief_docx
from cti_app.application.pandoc_rendering import WORD_STYLE_MAP, render_brief_pandoc
from cti_app.application.production_normalization import (
    canonical_indicator_key,
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
    technical_extraction_from_json,
    validate_synthesis,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.publication_builder import build_brief_document
from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.domain.discovery import SourceRole
from cti_app.domain.publication import ArtifactType, BriefDocumentV1, RichSpanKind

ROOT = Path(__file__).parents[2]
HASH = "37e123bd" + "a" * 52 + "4066"


def _item(
    identifier: str,
    value: str,
    semantic_type: SemanticType,
    artifact_type: ArtifactType | None = None,
    *,
    status: IndicatorStatus = IndicatorStatus.CONTEXTUAL,
    policy: DisplayPolicy = DisplayPolicy.BODY_ONLY,
) -> ExtractionItem:
    return ExtractionItem(
        local_id=identifier,
        category="other_technical",
        value=value,
        context="contexte",
        artifact_type=artifact_type,
        attack_id=None,
        reference_ids=("R1",),
        source_ids=("S1",),
        supported=True,
        semantic_type=semantic_type,
        indicator_status=status,
        provenance=IndicatorProvenance.SOURCE,
        display_policy=policy,
        normalized_value=(
            normalize_indicator_value(value, artifact_type) if artifact_type else None
        ),
    )


def _extraction() -> TechnicalExtraction:
    return TechnicalExtraction(
        items=(
            _item("A1", "Cavern Manticore", SemanticType.ACTOR),
            _item("A2", "OilRig / APT34", SemanticType.ACTOR),
            _item("M1", "HOLLOWGRAPH", SemanticType.MALWARE),
            _item("M2", "Cavern Agent", SemanticType.MALWARE),
            _item("O1", "WinDirStat", SemanticType.TOOL),
            _item("P1", "Microsoft Graph", SemanticType.PRODUCT),
            _item("T1", "DLL side-loading", SemanticType.TECHNIQUE),
            _item("T2", "AES-256-GCM", SemanticType.TECHNIQUE),
            _item(
                "I1",
                "cloudlanecdn[.]com",
                SemanticType.INDICATOR,
                ArtifactType.DOMAIN,
                status=IndicatorStatus.CONFIRMED_IOC,
                policy=DisplayPolicy.IOC_SECTION,
            ),
            _item(
                "I2",
                "216.126.237.197",
                SemanticType.INDICATOR,
                ArtifactType.IP,
                status=IndicatorStatus.CONFIRMED_IOC,
                policy=DisplayPolicy.IOC_SECTION,
            ),
            _item(
                "I3",
                HASH,
                SemanticType.INDICATOR,
                ArtifactType.HASH,
                status=IndicatorStatus.CONFIRMED_IOC,
                policy=DisplayPolicy.IOC_SECTION,
            ),
            _item(
                "I4",
                "2001:4998:44:3507::8000",
                SemanticType.INDICATOR,
                ArtifactType.IP,
                status=IndicatorStatus.EXCLUDED,
                policy=DisplayPolicy.HIDDEN,
            ),
            _item("F1", "uxtheme.dll", SemanticType.FILE, ArtifactType.FILENAME),
            _item("C1", "CVE-2026-1234", SemanticType.OTHER, ArtifactType.CVE),
        )
    )


def _report() -> ReferenceReport:
    source = ParsedSource(
        local_id="S1",
        title="Cavern research",
        url="https://research.example/cavern_report",
        canonical_url="https://research.example/cavern_report",
        publisher="Research",
        published_at=date(2026, 7, 6),
        role=SourceRole.PRIMARY,
    )
    return ReferenceReport(
        sources=(source,),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 7, 6),
                source_ids=("S1",),
                text="Publication de l'analyse Cavern.",
            ),
        ),
        editorial_title="[Cavern Manticore] Un framework C2 modulaire lié à l'Iran",
    )


def test_indicator_normalization_and_collection_are_explicit() -> None:
    assert canonical_indicator_key("Example[.]COM", ArtifactType.DOMAIN) == "example.com"
    assert normalize_indicator_value("2001:0db8::1", ArtifactType.IP) == "2001:db8::1"
    assert normalize_indicator_value("ABCDEF", ArtifactType.HASH) == "abcdef"
    assert display_indicator_value("example.com", ArtifactType.DOMAIN, defanged=True) == (
        "example[.]com"
    )
    assert [item.value for item in collect_indicators(_extraction())] == [
        "cloudlanecdn[.]com",
        "216.126.237.197",
        HASH,
    ]


def test_v1_extraction_is_readable_but_never_promoted_to_ioc() -> None:
    extraction = technical_extraction_from_json(
        {
            "items": [
                {
                    "id": "N1",
                    "category": "network_artifacts",
                    "value": "example.com",
                    "type": "domain",
                    "supported": True,
                }
            ]
        }
    )
    assert extraction.items[0].artifact_type is ArtifactType.DOMAIN
    assert extraction.items[0].indicator_status is IndicatorStatus.CONTEXTUAL
    assert collect_indicators(extraction) == []


def test_semantic_annotation_prioritizes_entities_and_citations() -> None:
    text = "Cavern Manticore utilise WinDirStat pour du DLL side-loading [S1]."
    spans = SemanticAnnotator(EnglishTermDetector(("side-loading", "loader"))).annotate(
        text, _extraction()
    )
    kinds = {span.text: span.kind for span in spans if span.text}
    assert kinds["Cavern Manticore"] is RichSpanKind.ACTOR
    assert kinds["WinDirStat"] is RichSpanKind.TOOL
    assert kinds["DLL side-loading"] is RichSpanKind.TECHNICAL
    assert next(span for span in spans if span.kind is RichSpanKind.CITATION).source_ids == (
        "S1",
    )


def test_synthesis_validator_blocks_inventory_only_ioc_but_accepts_both() -> None:
    rejected = validate_synthesis(
        "Le domaine cloudlanecdn[.]com sert au C2 [S1].", _report(), _extraction()
    )
    assert "ioc_repeated_in_body" in rejected.errors
    both = TechnicalExtraction(
        items=(
            _item(
                "I1",
                "cloudlanecdn[.]com",
                SemanticType.INDICATOR,
                ArtifactType.DOMAIN,
                status=IndicatorStatus.CONFIRMED_IOC,
                policy=DisplayPolicy.BOTH,
            ),
        )
    )
    assert validate_synthesis(
        "Le domaine cloudlanecdn[.]com sert au C2 [S1].", _report(), both
    ).usable
    assert validate_synthesis("Un fichier version.1 est décrit [S1].", _report(), both).usable
    spans = SemanticAnnotator().annotate("Le domaine cloudlanecdn.com répond.", both)
    ioc = next(span for span in spans if span.kind is RichSpanKind.IOC)
    assert ioc.text == "cloudlanecdn[.]com"


def test_cavern_document_round_trip_and_pandoc_golden() -> None:
    document = build_brief_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text=(
            "Cavern Manticore déploie HOLLOWGRAPH par DLL side-loading au moyen de "
            "WinDirStat, un binaire légitime [S1]."
        ),
    )
    assert BriefDocumentV1.from_json(document.to_json()) == document
    markdown = render_brief_pandoc(document)
    assert "# " not in markdown and "[S1]" not in markdown and "`" not in markdown
    assert "**Cavern Manticore**" in markdown
    assert "**HOLLOWGRAPH**" in markdown
    assert '[WinDirStat]{custom-style="Veille - Outil Char"}' in markdown
    assert '[DLL side-loading]{custom-style="Veille - Elément technique Char"}' in markdown
    assert "^[https://research.example/cavern\\_report]" in markdown
    assert "cloudlanecdn.com" in markdown
    assert "2001:4998:44:3507::8000" not in markdown
    assert "uxtheme.dll" not in markdown
    assert "CVE-2026-1234" not in markdown


def test_brief_document_title_uses_editorial_title_exactly() -> None:
    """Q1's editorial_title must reach the published title verbatim."""
    document = build_brief_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )
    assert document.title == _report().editorial_title


def test_reference_doc_contains_every_mapped_style() -> None:
    reference = ROOT / "backend/assets/pandoc/reference-doc-v1.docx"
    with zipfile.ZipFile(reference) as archive:
        root = ET.fromstring(archive.read("word/styles.xml"))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    names = {
        node.attrib[f"{namespace}val"]
        for node in root.iter(f"{namespace}name")
        if f"{namespace}val" in node.attrib
    }
    assert {style for style in WORD_STYLE_MAP.values() if style is not None} <= names


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is not installed")
def test_real_pandoc_export_produces_an_openable_docx(tmp_path: Path) -> None:
    document = build_brief_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )
    output = export_brief_docx(document, tmp_path / "brief.docx")
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        styles = archive.read("word/styles.xml")
        assert b"Titre partie bulletin" in styles
