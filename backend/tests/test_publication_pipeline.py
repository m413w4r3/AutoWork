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
    _render_rich,
    render_edition_pandoc,
    render_publication_pandoc,
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
from cti_app.application.publication_builder import (
    build_publication_document,
)
from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.publication import (
    ArtifactType,
    PublicationDocumentV2,
    PublicationSource,
    RichSpan,
    RichSpanKind,
    RichText,
    TimelineEntry,
    publication_document_from_json,
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


def _multi_source_document() -> PublicationDocumentV2:
    return PublicationDocumentV2(
        schema_version="2",
        title="Citation test",
        timeline=(
            TimelineEntry(
                date=None,
                content=(
                    RichSpan(RichSpanKind.TEXT, "Information vérifiée"),
                    RichSpan(RichSpanKind.CITATION, "", ("S1", "S2")),
                ),
                source_ids=("S1", "S2"),
            ),
        ),
        synthesis=(),
        indicators=(),
        sources=(
            PublicationSource("S1", "https://example.test/1"),
            PublicationSource("S2", "https://example.test/2"),
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
        (("S1",), "^[https://example.test/1]"),
        (("S1", "S2"), "^[https://example.test/1 ; https://example.test/2]"),
        (("S1", "S1", "S2"), "^[https://example.test/1 ; https://example.test/2]"),
        (("S1", "UNKNOWN", "S2"), "^[https://example.test/1 ; https://example.test/2]"),
        (("UNKNOWN",), ""),
    ],
)
def test_pandoc_renderer_renders_one_footnote_per_citation(
    source_ids: tuple[str, ...], expected: str
) -> None:
    rendered = _render_rich(
        (RichSpan(RichSpanKind.CITATION, "", source_ids),),
        {
            "S1": "https://example.test/1",
            "S2": "https://example.test/2",
        },
    )

    assert rendered == expected
    assert "^[https://example.test/1]^[https://example.test/2]" not in rendered


def test_builder_never_emits_two_adjacent_footnotes_for_one_event() -> None:
    report = _report()
    second = ParsedSource(
        local_id="S2",
        title="Cavern follow-up",
        url="https://research.example/followup",
        canonical_url="https://research.example/followup",
        publisher="Research",
        published_at=date(2026, 7, 7),
        role=SourceRole.INDEPENDENT,
    )
    report = ReferenceReport(
        sources=(*report.sources, second),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 7, 6),
                source_ids=("S1", "S2"),
                text="Publication de l'analyse Cavern [S1].",
            ),
        ),
        editorial_title=report.editorial_title,
    )
    document = build_publication_document(
        subject_title="Cavern",
        report=report,
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )

    citations = [
        span for span in document.timeline[0].content if span.kind is RichSpanKind.CITATION
    ]
    assert len(citations) == 1
    assert citations[0].source_ids == ("S1", "S2")

    paragraph = render_publication_pandoc(document).split("Synthèse")[0]
    assert paragraph.count("^[") == 1
    assert (
        "^[https://research.example/cavern\\_report ; https://research.example/followup]"
        in paragraph
    )


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


def test_current_builder_writes_only_v2_and_uses_the_central_reader() -> None:
    document = build_publication_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )

    assert isinstance(document, PublicationDocumentV2)
    assert document.to_json()["schema_version"] == "2"
    assert publication_document_from_json(document.to_json()) == document
    assert render_publication_pandoc(document)


def test_cavern_document_round_trip_and_pandoc_golden() -> None:
    document = build_publication_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text=(
            "Cavern Manticore déploie HOLLOWGRAPH par DLL side-loading au moyen de "
            "WinDirStat, un binaire légitime [S1]."
        ),
    )
    markdown = render_publication_pandoc(document)
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


def test_publication_document_title_uses_editorial_title_exactly() -> None:
    """Q1's editorial_title must reach the published title verbatim."""
    document = build_publication_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )
    assert document.title == _report().editorial_title


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


def _publication(title: str, *, analyst_note: RichText | None = None) -> PublicationDocumentV2:
    return PublicationDocumentV2(
        schema_version="2",
        title=title,
        timeline=(
            TimelineEntry(
                date=None,
                content=(RichSpan(RichSpanKind.TEXT, "Contenu"),),
                source_ids=(),
            ),
        ),
        synthesis=((RichSpan(RichSpanKind.TEXT, "Synthèse du sujet"),),),
        indicators=(),
        sources=(),
        uncertainties=(),
        analyst_note=analyst_note,
    )


@pytest.mark.parametrize(("publications", "breaks"), ((1, 0), (2, 1), (3, 2)))
def test_edition_markdown_separates_publications_with_one_page_break(
    publications: int, breaks: int
) -> None:
    markdown = render_edition_pandoc(_edition_document(publications))

    assert markdown.count(PAGE_BREAK_MARKDOWN) == breaks
    assert not markdown.startswith(PAGE_BREAK_MARKDOWN)
    assert not markdown.rstrip().endswith(PAGE_BREAK_MARKDOWN)


def test_publication_without_analyst_note_renders_no_note_block() -> None:
    markdown = render_publication_pandoc(_publication("Alpha"))

    assert "Note de l'analyste" not in markdown
    assert str(WORD_STYLE_MAP["analyst_note"]) not in markdown


def test_publication_renders_an_explicit_analyst_note_with_editorial_styles() -> None:
    note: RichText = (
        RichSpan(RichSpanKind.TEXT, "Le lien avec "),
        RichSpan(RichSpanKind.ACTOR, "Cavern Manticore"),
        RichSpan(RichSpanKind.TEXT, " reste probable."),
    )

    markdown = render_publication_pandoc(_publication("Alpha", analyst_note=note))

    title_style = WORD_STYLE_MAP["analyst_note"]
    assert f'::: {{custom-style="{title_style}"}}\nNote de l\'analyste' in markdown
    assert (
        f'::: {{custom-style="{WORD_STYLE_MAP["analyst_note_body"]}"}}\n'
        "Le lien avec **Cavern Manticore** reste probable."
    ) in markdown


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
    document = build_publication_document(
        subject_title="Cavern",
        report=_report(),
        extraction=_extraction(),
        synthesis_text="Cavern Manticore utilise WinDirStat [S1].",
    )
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
