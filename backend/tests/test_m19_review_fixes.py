from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date
from pathlib import Path

from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.publication import (
    ArtifactType,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.semantic_annotation import (
    SemanticRole,
    SemanticTextSpanV1,
    section_paragraph_anchor,
    timeline_anchor,
)
from tests.test_m19_projection import (
    _SOURCE_ID,
    _file_detail,
    _publication,
    _ref,
    _source,
)


def _semantic_override(publication, values: dict[str, tuple[SemanticTextSpanV1, ...]]):
    paragraphs = tuple(
        replace(paragraph, spans=values[paragraph.anchor])
        if paragraph.anchor in values
        else paragraph
        for paragraph in publication.semantic_text.paragraphs
    )
    return replace(
        publication,
        semantic_text=replace(publication.semantic_text, paragraphs=paragraphs),
    )


def _role_spans(
    text: str,
    styled: tuple[tuple[str, SemanticRole], ...],
) -> tuple[SemanticTextSpanV1, ...]:
    spans: list[SemanticTextSpanV1] = []
    cursor = 0
    for value, role in styled:
        start = text.index(value, cursor)
        if start > cursor:
            spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor:start]))
        spans.append(SemanticTextSpanV1(role, value))
        cursor = start + len(value)
    if cursor < len(text):
        spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor:]))
    return tuple(spans)


def _reader_model(publication):
    return project_publication_to_typst_model(publication)


def test_revision_assembly_calls_bind_consumed_brief_enrichment_artifact_identity() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    paths = (
        backend_root / "src/cti_app/application/production_synthesis_revision.py",
        backend_root / "src/cti_app/application/production_enrichment_revision.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assemble_publication"
        ]
        assert len(calls) == 1, path
        keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
        assert ast.unparse(keywords["editorial_enrichment_artifact_id"]) == (
            "enrichment_artifact.id"
        ), path
        assert ast.unparse(keywords["editorial_enrichment_artifact_input_hash"]) == (
            "enrichment_artifact.input_hash"
        ), path


def test_refang_does_not_restore_filename_iocs_in_narrative_text() -> None:
    text = "The source names sample[.]dll as the dropped file."
    publication = _publication(
        lead_text=text,
        ioc_details=(_file_detail(ArtifactType.FILENAME, "sample[.]dll"),),
    )

    model = _reader_model(publication)
    synthesis = next(
        section for section in model.content_sections if section["type"] == "synthesis"
    )
    lead = next(block for block in synthesis["blocks"] if block["type"] == "paragraph")

    assert lead["text"] == text


def test_refang_preserves_case_sensitive_url_path_and_query_spellings() -> None:
    first = "hxxps[:]//bad[.]example/Foo?Key=One"
    second = "hxxps[:]//bad[.]example/foo?Key=one"
    text = f"First {first}; second {second}."
    group = PublicationIndicatorGroupV1(
        ArtifactType.URL,
        (
            PublicationIndicatorV1(
                value=first,
                normalized_value="https://bad.example/Foo?Key=One",
                artifact_type=ArtifactType.URL,
                source_document_ids=(_SOURCE_ID,),
            ),
            PublicationIndicatorV1(
                value=second,
                normalized_value="https://bad.example/foo?Key=one",
                artifact_type=ArtifactType.URL,
                source_document_ids=(_SOURCE_ID,),
            ),
        ),
    )
    publication = _publication(lead_text=text, indicators=(group,))

    model = _reader_model(publication)
    synthesis = next(
        section for section in model.content_sections if section["type"] == "synthesis"
    )
    lead = next(block for block in synthesis["blocks"] if block["type"] == "paragraph")

    assert lead["text"] == (
        "First https://bad.example/Foo?Key=One; second https://bad.example/foo?Key=one."
    )


def test_article_overview_and_timeline_keep_semantic_spans_after_projection() -> None:
    reference = _ref(_SOURCE_ID, "a")
    defanged_url = "hxxps[:]//bad[.]example/path"
    sections = (
        PublicationSectionV1(
            PublicationSectionKind.TECHNICAL,
            "Vue d'ensemble",
            (
                PublicationParagraphV1(
                    f"Victimologie: ToolX targets {defanged_url}.", (reference,)
                ),
                PublicationParagraphV1("Arsenal: A collection tool.", (reference,)),
                PublicationParagraphV1("Objectif principal: Data theft.", (reference,)),
            ),
        ),
    )
    event_text = f"ToolX accessed the server at {defanged_url}."
    publication = _publication(
        sources=(
            _source(
                _SOURCE_ID,
                url="https://source.example/report",
                title="Source report",
                publisher="Example",
                published_at=date(2026, 1, 2),
            ),
        ),
        sections=sections,
        timeline=(
            PublicationTimelineEntryV1(date(2026, 1, 4), None, event_text, (reference,)),
            PublicationTimelineEntryV1(
                date(2026, 1, 5), None, "Second event is documented.", (reference,)
            ),
        ),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.URL,
                (
                    PublicationIndicatorV1(
                        value=defanged_url,
                        normalized_value="https://bad.example/path",
                        artifact_type=ArtifactType.URL,
                        source_document_ids=(_SOURCE_ID,),
                    ),
                ),
            ),
        ),
    )
    publication = _semantic_override(
        publication,
        {
            section_paragraph_anchor(0, 1): _role_spans(
                sections[0].paragraphs[0].text,
                (
                    ("ToolX", SemanticRole.TOOL),
                    (defanged_url, SemanticRole.IOC),
                ),
            ),
            timeline_anchor(1): _role_spans(
                event_text,
                (("ToolX", SemanticRole.TOOL), (defanged_url, SemanticRole.IOC)),
            ),
        },
    )

    model = _reader_model(publication)
    assert model.article_format is not None
    overview = model.article_format["overview"]["victimology"]
    event = next(
        item
        for item in model.article_format["timeline_key_events"]
        if item["text"].startswith("ToolX")
    )

    assert overview["text"] == "ToolX targets https://bad.example/path."
    assert "".join(span["text"] for span in overview["semantic_spans"]) == overview["text"]
    assert {span["style"] for span in overview["semantic_spans"]} >= {
        "semantic-tool",
        "semantic-ioc",
    }
    assert event["text"] == "ToolX accessed the server at https://bad.example/path."
    assert "".join(span["text"] for span in event["semantic_spans"]) == event["text"]
    assert {span["style"] for span in event["semantic_spans"]} >= {
        "semantic-tool",
        "semantic-ioc",
    }


def test_reference_note_cleanup_and_truncation_remap_semantic_spans() -> None:
    source = _source(
        _SOURCE_ID,
        url="https://source.example/report",
        title="Source report",
        publisher="Example",
        published_at=None,
    )
    note = (
        "CredDump documented the operator workflow at https://source.example/report "
        "on 14 octobre 2026. " + "Neutral evidence detail " * 20 + "TailTool"
    )
    publication = _publication(
        sources=(source,),
        reference_notes={_SOURCE_ID: note},
    )
    publication = _semantic_override(
        publication,
        {
            "reference:0001": _role_spans(
                note,
                (("CredDump", SemanticRole.TOOL), ("TailTool", SemanticRole.TOOL)),
            )
        },
    )

    model = _reader_model(publication)
    entry = model.content_sections[0]["entries"][0]

    assert len(entry["text"]) <= 260
    assert "https://" not in entry["text"]
    assert "14 octobre 2026" not in entry["text"]
    spans = entry["semantic_spans"]
    assert "".join(span["text"] for span in spans) == entry["text"]
    assert any(span["style"] == "semantic-tool" and "CredDump" in span["text"] for span in spans)
    assert "TailTool" not in "".join(span["text"] for span in spans)
