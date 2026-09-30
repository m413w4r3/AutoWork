"""Regression coverage for the semantic publication pipeline."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.docx_postprocessing import (
    TEMPLATE_PART_PATTERN,
    edition_template_values,
)
from cti_app.application.pandoc_export import (
    DEFAULT_REFERENCE_DOC,
    export_markdown_docx,
    export_publication_docx,
)
from cti_app.application.pandoc_rendering import (
    PAGE_BREAK_MARKDOWN,
    WORD_STYLE_MAP,
    _render_v3_citations,
    render_edition_pandoc,
)
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
from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.publication import (
    PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
    ArtifactType,
    PublicationDocumentV3,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    RichSpanKind,
)

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


def _publication_source(source_id: UUID, url: str, title: str = "Example") -> PublicationSourceV1:
    return PublicationSourceV1(
        source_document_id=source_id,
        canonical_url=url,
        title=title,
        publisher="Example",
        published_at=None,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
    )


def _multi_source_document() -> PublicationDocumentV3:
    first_id, second_id = UUID(int=1), UUID(int=2)
    refs = (
        PublicationEvidenceRefV1(first_id, PublicationEvidenceKind.FACT, "a" * 64),
        PublicationEvidenceRefV1(second_id, PublicationEvidenceKind.FACT, "b" * 64),
    )
    return PublicationDocumentV3(
        schema_version=PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
        subject_id=UUID(int=10),
        publication_language="fr",
        title="Citation test",
        lead=(PublicationParagraphV1("Information vérifiée", refs),),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(
            _publication_source(first_id, "https://example.test/1"),
            _publication_source(second_id, "https://example.test/2"),
        ),
        uncertainties=(),
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
    assert next(span for span in spans if span.kind is RichSpanKind.CITATION).source_ids == ("S1",)


@pytest.mark.parametrize(
    ("source_ids", "expected"),
    [
        ((1,), " ^[https://example.test/1]"),
        ((1, 2), " ^[https://example.test/1 ; https://example.test/2]"),
        ((1, 1, 2), " ^[https://example.test/1 ; https://example.test/2]"),
    ],
)
def test_pandoc_renderer_renders_one_footnote_per_citation(
    source_ids: tuple[int, ...], expected: str
) -> None:
    rendered = _render_v3_citations(
        tuple(
            PublicationEvidenceRefV1(UUID(int=source_id), PublicationEvidenceKind.FACT, "a" * 64)
            for source_id in source_ids
        ),
        {
            str(UUID(int=1)): "https://example.test/1",
            str(UUID(int=2)): "https://example.test/2",
        },
    )

    assert rendered == expected
    assert "^[https://example.test/1]^[https://example.test/2]" not in rendered


def test_synthesis_validator_allows_ioc_section_values_in_body() -> None:
    accepted = validate_synthesis(
        "Le domaine cloudlanecdn[.]com sert au C2 [S1].", _report(), _extraction()
    )
    assert accepted.usable, accepted.errors
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


def _edition_document(count: int) -> EditionDocumentV2:
    return EditionDocumentV2(
        edition={"period_start": "2026-07-01", "country": "Iran"},
        publications=tuple(
            EditionPublicationV2(
                position=position,
                subject_id=UUID(int=position),
                document=_publication(f"Publication {position}"),
            )
            for position in range(1, count + 1)
        ),
    )


def _publication(title: str) -> PublicationDocumentV3:
    source_id = UUID(int=1)
    evidence = PublicationEvidenceRefV1(source_id, PublicationEvidenceKind.FACT, "a" * 64)
    return PublicationDocumentV3(
        schema_version=PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
        subject_id=UUID(int=1),
        publication_language="fr",
        title=title,
        lead=(PublicationParagraphV1("Contenu", (evidence,)),),
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.OVERVIEW,
                "Synthèse",
                (PublicationParagraphV1("Synthèse du sujet", (evidence,)),),
            ),
        ),
        timeline=(),
        indicators=(),
        sources=(_publication_source(source_id, "https://example.test/article"),),
        uncertainties=(),
    )


@pytest.mark.parametrize(("publications", "breaks"), ((1, 0), (2, 1), (3, 2)))
def test_edition_markdown_separates_publications_with_one_page_break(
    publications: int, breaks: int
) -> None:
    markdown = render_edition_pandoc(_edition_document(publications))

    assert markdown.count(PAGE_BREAK_MARKDOWN) == breaks
    assert not markdown.startswith(PAGE_BREAK_MARKDOWN)
    assert not markdown.rstrip().endswith(PAGE_BREAK_MARKDOWN)


def test_reference_doc_contains_every_mapped_style() -> None:
    with zipfile.ZipFile(DEFAULT_REFERENCE_DOC) as archive:
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
    document = _publication("Cavern")
    output = export_publication_docx(document, tmp_path / "publication.docx")
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        styles = archive.read("word/styles.xml")
        assert b"Titre partie bulletin" in styles


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is not installed")
def test_real_pandoc_export_renders_multi_source_citation_as_word_footnote(
    tmp_path: Path,
) -> None:
    output = export_publication_docx(_multi_source_document(), tmp_path / "multi-source.docx")
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    with zipfile.ZipFile(output) as archive:
        document_root = ET.fromstring(archive.read("word/document.xml"))
        footnotes_root = ET.fromstring(archive.read("word/footnotes.xml"))

    references = document_root.findall(f".//{{{namespace}}}footnoteReference")
    assert len(references) == 1
    assert "[https://" not in "".join(document_root.itertext())

    cited = [
        "".join(footnote.itertext())
        for footnote in footnotes_root.findall(f"{{{namespace}}}footnote")
        if "https://example.test" in "".join(footnote.itertext())
    ]
    assert len(cited) == 1
    assert "https://example.test/1" in cited[0]
    assert "https://example.test/2" in cited[0]
    assert cited[0].index("example.test/1") < cited[0].index("example.test/2")


def _header_and_footer_text(archive: zipfile.ZipFile) -> str:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    return "".join(
        "".join(ET.fromstring(archive.read(name)).itertext())
        for name in sorted(archive.namelist())
        if TEMPLATE_PART_PATTERN.match(name)
    ).replace(f"{{{namespace}}}", "")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is not installed")
@pytest.mark.parametrize(("publications", "breaks"), ((1, 0), (2, 1), (3, 2)))
def test_real_pandoc_export_writes_one_word_page_break_between_publications(
    tmp_path: Path, publications: int, breaks: int
) -> None:
    edition = _edition_document(publications)
    output = export_markdown_docx(
        render_edition_pandoc(edition),
        tmp_path / f"edition-{publications}.docx",
        template_values=edition_template_values(edition.edition),
    )
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    with zipfile.ZipFile(output) as archive:
        document_root = ET.fromstring(archive.read("word/document.xml"))

    page_breaks = [
        node
        for node in document_root.iter(f"{{{namespace}}}br")
        if node.attrib.get(f"{{{namespace}}}type") == "page"
    ]
    assert len(page_breaks) == breaks


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is not installed")
@pytest.mark.parametrize(
    ("period_start", "expected"),
    (("2026-07-01", "juillet 2026"), ("2026-08-01", "août 2026")),
)
def test_real_pandoc_export_stamps_the_edition_month_into_the_template(
    tmp_path: Path, period_start: str, expected: str
) -> None:
    edition = EditionDocumentV2(
        edition={"period_start": period_start, "country": "Iran"},
        publications=_edition_document(1).publications,
    )
    output = export_markdown_docx(
        render_edition_pandoc(edition),
        tmp_path / f"edition-{period_start}.docx",
        template_values=edition_template_values(edition.edition),
    )

    with zipfile.ZipFile(output) as archive:
        text = _header_and_footer_text(archive)

    assert expected in text
    assert "Iran" in text
    # The historical template metadata must not survive the export.
    assert "Juillet 2024" not in text
    assert "Bulletin n°32" not in text
    assert "XXX" not in text
    assert "{{" not in text
