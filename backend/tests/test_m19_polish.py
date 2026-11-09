from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.brief_publication_builder import wrap_publication_document_v7
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.typst_render_execution import TypstRenderExecutor
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstDraftPreviewDocument,
    TypstMediaRef,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
    _refang_known_iocs,
    _remap_semantic_spans,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1, OpinionBlock, OpinionBlockType
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.media_assets import MediaAssetKind, MediaAssetManifest, media_asset_id
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    ArticleFormatV1,
    IOCArticleCategoriesV1,
    PublicationDocumentV8,
    build_publication_date_events,
    build_publication_date_events_v3,
    extract_article_overview,
    parse_publication_document,
    publication_document_v5_sha256,
    select_publication_timeline_key_events_v2,
    select_publication_timeline_key_events_v3,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.test_m19_projection import _publication, _ref, _source

_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000001")
_SECOND_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")


def test_refang_matches_partial_known_ioc_tokens_and_preserves_semantic_spans() -> None:
    known_iocs = (
        (ArtifactType.URL, "www.xionamiconm.com:8050/ws/", "www.xionamiconm.com:8050/ws/"),
        (ArtifactType.IP, "153.75.247.94", "153.75.247.94"),
        (ArtifactType.EMAIL, "analyst@example.test", "analyst@example.test"),
        (ArtifactType.URL, "http://Case.example/Path?Query=A", "http://Case.example/Path?Query=A"),
    )
    source = (
        "Known endpoints: www.xionamiconm[.]com:8050/ws/ and "
        "153.75.247[.]94:80; analyst[at]example[.]test; "
        "hxxp[://]case.example/Path?Query=A. Unknown unknown[.]example and "
        "hxxp[://]case.example/path?Query=A remain defanged."
    )
    projected, _offsets = _refang_known_iocs(source, known_iocs)

    assert projected == (
        "Known endpoints: www.xionamiconm.com:8050/ws/ and "
        "153.75.247.94:80; analyst@example.test; "
        "http://case.example/Path?Query=A. Unknown unknown[.]example and "
        "hxxp[://]case.example/path?Query=A remain defanged."
    )
    spans = [
        {"style": "semantic-plain", "text": "Known endpoints: "},
        {"style": "semantic-ioc", "text": "www.xionamiconm[.]com:8050/ws/"},
        {"style": "semantic-plain", "text": source.split("www.xionamiconm[.]com:8050/ws/", 1)[1]},
    ]
    remapped = _remap_semantic_spans(source, spans, projected)
    assert "".join(span["text"] for span in remapped or ()) == projected
    assert "www.xionamiconm.com:8050/ws/" in (remapped or [])[1]["text"]
    domain_projected, _domain_offsets = _refang_known_iocs(
        "Domain endpoint: telemetry[.]example:444/path.",
        ((ArtifactType.DOMAIN, "telemetry.example", "telemetry.example"),),
    )
    assert domain_projected == "Domain endpoint: telemetry.example:444/path."
    file_projected, _file_offsets = _refang_known_iocs(
        "sample[.]dll C:\\temp\\sample[.]dll",
        (
            (ArtifactType.FILENAME, "sample.dll", "sample.dll"),
            (ArtifactType.FILEPATH, "C:\\temp\\sample.dll", "C:\\temp\\sample.dll"),
        ),
    )
    assert file_projected == "sample[.]dll C:\\temp\\sample[.]dll"


class _FakeMediaStore:
    def __init__(self, kind: MediaAssetKind, content: bytes) -> None:
        self.content = content
        digest = hashlib.sha256(content).hexdigest()
        self.manifest = MediaAssetManifest(
            asset_id=media_asset_id(digest, "image/svg+xml"),
            kind=kind,
            blob_id=UUID("10000000-0000-0000-0000-000000000001"),
            sha256=digest,
            mime_type="image/svg+xml",
            byte_size=len(content),
            source="m19-polish-test",
            policy_version="test-v1",
            compiler_name="test-compiler",
            compiler_version="1",
        )

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        return self.manifest if asset_id == self.manifest.asset_id else None

    async def read(self, asset_id: UUID) -> bytes:
        assert asset_id == self.manifest.asset_id
        return self.content


@pytest.mark.parametrize("kind", (MediaAssetKind.DIAGRAM_SVG, MediaAssetKind.CHART_SVG))
@pytest.mark.asyncio
async def test_svg_refang_changes_only_known_text_nodes_and_keeps_blob_bytes_canonical(
    kind: MediaAssetKind,
) -> None:
    original = (
        b'<svg xmlns="http://www.w3.org/2000/svg" data-ioc="153.75.247[.]94:80" '
        b'data-flag="a > b">'
        b'<text data-ioc="153.75.247[.]94:80">IP '
        b"<title>153.75.247[.]94:80</title> "
        b"<tspan>153.75.247[.]94:80</tspan> "
        b"www.xionamiconm[.]com:8050/ws/ unknown[.]example</text>"
        b"<title>153.75.247[.]94:80</title></svg>"
    )
    store = _FakeMediaStore(kind, original)
    asset_id = store.manifest.asset_id
    source_bytes = b"#publication"
    render_data = b"{}"
    source = TypstRenderSource(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        render_data_bytes=render_data,
        render_data_sha256=hashlib.sha256(render_data).hexdigest(),
        media_refs=(
            TypstMediaRef(
                asset_id=asset_id,
                expected_kind=kind,
                expected_mime_type="image/svg+xml",
                expected_sha256=None,
                expected_byte_size=None,
                media_path=f"media/{asset_id}.svg",
            ),
        ),
        media_refang_iocs=(
            (ArtifactType.IP, "153.75.247.94", "153.75.247.94"),
            (
                ArtifactType.URL,
                "www.xionamiconm.com:8050/ws/",
                "www.xionamiconm.com:8050/ws/",
            ),
        ),
    )
    executor = TypstRenderExecutor(media_asset_store=store, compiler=object())  # type: ignore[arg-type]

    resolved = await executor.resolve_media(source)

    assert hashlib.sha256(store.content).hexdigest() == store.manifest.sha256
    assert store.content == original
    assert resolved[asset_id] == (
        b'<svg xmlns="http://www.w3.org/2000/svg" data-ioc="153.75.247[.]94:80" '
        b'data-flag="a > b">'
        b'<text data-ioc="153.75.247[.]94:80">IP '
        b"<title>153.75.247[.]94:80</title> "
        b"<tspan>153.75.247.94:80</tspan> "
        b"www.xionamiconm.com:8050/ws/ unknown[.]example</text>"
        b"<title>153.75.247[.]94:80</title></svg>"
    )


def test_publication_and_edition_renderers_pass_projected_iocs_to_media_resolution() -> None:
    known_ip = "153.75.247.94"
    publication = _publication(indicators=(_indicator_group_ip(known_ip),))
    wrapped = wrap_publication_document_v7(
        publication,
        base_publication_artifact_id=UUID("20000000-0000-0000-0000-000000000001"),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="test-v1",
    )
    bundle = TypstTemplateBundle(
        template_version="test-v1",
        sha256="b" * 64,
        files=(TemplateFile("RENDERER/publication.typ", b"#publication"),),
    )
    publication_source = TypstRenderer().render(
        wrapped,
        bundle,
        document_label="Test publication",
    )
    edition_bundle = TypstTemplateBundle(
        template_version="edition-test-v1",
        sha256="c" * 64,
        files=(TemplateFile("RENDERER/edition.typ", b"#edition"),),
    )
    edition = EditionDocumentV2(
        edition={
            "id": "30000000-0000-0000-0000-000000000001",
            "country": "Iran",
            "country_code": "IR",
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "tlp": "TLP:GREEN",
            "languages": ["fr"],
            "version": 1,
        },
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=wrapped.subject_id,
                document=wrapped,
            ),
        ),
    )
    edition_source = EditionTypstRenderer().render(
        edition,
        edition_bundle,
        document_label="Test edition",
    )

    expected = ((ArtifactType.IP, known_ip, known_ip),)
    assert publication_source.media_refang_iocs == expected
    assert edition_source.media_refang_iocs == expected
    assert project_publication_to_typst_model(wrapped).media_refang_iocs == expected


def test_opinion_projection_keeps_known_defanged_iocs_unchanged() -> None:
    base_publication = _publication(
        lead_text="The report identifies 153.75.247[.]94.",
        indicators=(_indicator_group_ip("153.75.247.94"),),
    )
    opinion = AnalystOpinionV1(
        investigation_id=UUID("40000000-0000-0000-0000-000000000001"),
        synthesis_artifact_id=UUID("40000000-0000-0000-0000-000000000002"),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=UUID("40000000-0000-0000-0000-000000000003"),
        base_publication_sha256=publication_document_v5_sha256(base_publication),
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="The reviewer recorded 153.75.247[.]94.",
            ),
        ),
        author_id="m19-polish-test",
    )
    preview = TypstDraftPreviewDocument(base_publication, opinion)

    model = project_publication_to_typst_model(preview)

    synthesis = next(
        section for section in model.content_sections if section["type"] == "synthesis"
    )
    opinion_section = next(
        section for section in model.content_sections if section["type"] == "analyst_opinion"
    )
    assert synthesis["blocks"][0]["text"] == "The report identifies 153.75.247[.]94."
    assert opinion_section["blocks"][0]["text"] == "The reviewer recorded 153.75.247[.]94."
    assert model.media_refang_iocs == ()


def _indicator_group_ip(value: str):
    from cti_app.domain.publication import PublicationIndicatorGroupV1, PublicationIndicatorV1

    return PublicationIndicatorGroupV1(
        artifact_type=ArtifactType.IP,
        indicators=(
            PublicationIndicatorV1(
                value=value,
                normalized_value=value,
                artifact_type=ArtifactType.IP,
                source_document_ids=(_SOURCE_ID,),
            ),
        ),
    )


def test_v3_timeline_skips_cti_dates_technical_context_and_applies_two_date_gate() -> None:
    primary = replace(
        _source(
            _SOURCE_ID,
            url="https://primary.example/report",
            title="Primary report",
            publisher="Primary Press",
            published_at=date(2026, 10, 14),
        ),
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    technical_context = replace(
        _source(
            UUID("00000000-0000-0000-0000-000000000003"),
            url="https://technical.example/report",
            title="Sandbox report",
            publisher="Sandbox",
            published_at=date(2026, 10, 15),
        ),
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    corroboration = replace(
        _source(
            _SECOND_SOURCE_ID,
            url="https://corroboration.example/report",
            title="Rapport d\u2019analyse complémentaire " * 5,
            publisher="Analyst Desk",
            published_at=date(2026, 10, 16),
        ),
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )
    cti_timeline = (
        PublicationTimelineEntryV1(
            date(2026, 10, 14),
            None,
            "dmpdump publie une analyse de la campagne.",
            (_ref(_SOURCE_ID, "d"),),
        ),
    )

    events = select_publication_timeline_key_events_v3(
        cti_timeline,
        (primary, technical_context, corroboration),
    )

    assert [event.event_date for event in events] == [
        date(2026, 10, 14),
        date(2026, 10, 16),
    ]
    assert events[0].text == "dmpdump publie une analyse de la campagne."
    assert all("Sandbox" not in event.text for event in events)
    assert events[1].text.startswith("Publication par Analyst Desk : « Rapport d\u2019analyse")
    assert events[1].text.endswith("… »")
    assert len(events[1].text.split("«", 1)[1].split("»", 1)[0]) <= 90
    assert all(
        ref.kind is PublicationEvidenceKind.SOURCE_METADATA for ref in events[1].evidence_refs
    )
    assert select_publication_timeline_key_events_v3((), (primary, technical_context)) == ()

    legacy_independent = replace(
        corroboration,
        editorial_role=None,
    )
    legacy_independent_event = build_publication_date_events_v3((legacy_independent,))[0]
    assert legacy_independent_event.text.startswith(
        "Publication par Analyst Desk : « Rapport d\u2019analyse"
    )
    assert len(legacy_independent_event.text.split("«", 1)[1].split("»", 1)[0]) <= 90
    url_title_source = replace(
        corroboration,
        title="hxxps[://]corroboration.example/report",
        published_at=date(2026, 10, 17),
    )
    url_free_events = select_publication_timeline_key_events_v3(
        (),
        (primary, url_title_source),
    )
    assert len(url_free_events) == 2
    assert url_free_events[1].text == "Publication par Analyst Desk"
    assert "http" not in url_free_events[1].text.casefold()

    old_placeholder = build_publication_date_events((primary,))[0]
    persisted_placeholder = PublicationTimelineEntryV1(
        old_placeholder.event_date,
        None,
        old_placeholder.text,
        old_placeholder.evidence_refs,
    )
    regenerated = select_publication_timeline_key_events_v3(
        (persisted_placeholder,),
        (primary, corroboration),
    )
    assert [event.event_date for event in regenerated] == [
        date(2026, 10, 14),
        date(2026, 10, 16),
    ]
    assert regenerated[0].text == "Publication par Primary Press : « Primary report »"
    assert all(event.text != "Publication de la source." for event in regenerated)


def test_v8_replay_accepts_legacy_v2_and_new_v3_timeline_selection() -> None:
    primary = replace(
        _source(
            _SOURCE_ID,
            url="https://primary.example/report",
            title="Primary report",
            publisher="Primary Press",
            published_at=date(2026, 10, 11),
        ),
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    corroboration = replace(
        _source(
            _SECOND_SOURCE_ID,
            url="https://corroboration.example/report",
            title="Independent report",
            publisher="Analyst Desk",
            published_at=date(2026, 10, 12),
        ),
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )
    timeline = (
        PublicationTimelineEntryV1(
            date(2026, 10, 10),
            None,
            "A dated CTI event.",
            (_ref(_SOURCE_ID, "e"),),
        ),
    )
    publication = _publication(sources=(primary, corroboration), timeline=timeline)
    base_v7 = wrap_publication_document_v7(
        publication,
        base_publication_artifact_id=UUID("20000000-0000-0000-0000-000000000002"),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="f" * 64,
        routing_policy_version="test-v1",
    )

    for selected in (
        select_publication_timeline_key_events_v2(timeline, (primary, corroboration)),
        select_publication_timeline_key_events_v3(timeline, (primary, corroboration)),
    ):
        document = PublicationDocumentV8(
            base_publication=base_v7,
            article_format=ArticleFormatV1(
                overview=extract_article_overview(publication.document.sections),
                timeline_key_events=selected,
                ioc_categories=IOCArticleCategoriesV1(ips=(), urls=(), domains=(), files=()),
            ),
        )
        payload = serialize_publication_document(document)
        replayed = parse_publication_document(payload)
        assert serialize_publication_document(replayed) == payload


def test_persisted_brief_replays_its_stored_legacy_timeline_text() -> None:
    first_source = _source(
        _SOURCE_ID,
        url="https://primary.example/report",
        title="Primary report",
        publisher="Primary Press",
        published_at=date(2026, 10, 14),
    )
    second_source = replace(
        _source(
            _SECOND_SOURCE_ID,
            url="https://corroboration.example/report",
            title="Independent report",
            publisher="Analyst Desk",
            published_at=date(2026, 10, 15),
        ),
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )
    legacy_timeline = (
        PublicationTimelineEntryV1(
            date(2026, 10, 14),
            None,
            "Publication de la source.",
            (_ref(_SOURCE_ID, "a"),),
        ),
        PublicationTimelineEntryV1(
            date(2026, 10, 15),
            None,
            "Publication de la source.",
            (_ref(_SECOND_SOURCE_ID, "b"),),
        ),
    )
    publication = _publication(sources=(first_source, second_source), timeline=legacy_timeline)
    brief = wrap_publication_document_v7(
        publication,
        base_publication_artifact_id=UUID("20000000-0000-0000-0000-000000000003"),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="9" * 64,
        routing_policy_version="test-v1",
    )

    model = project_publication_to_typst_model(brief, include_brief_chronology=True)
    chronology = next(
        section for section in model.content_sections if section["type"] == "chronology"
    )

    assert [event["text"] for event in chronology["timeline"]] == [
        "Publication de la source.",
        "Publication de la source.",
    ]


def test_brief_timeline_policy_replays_legacy_and_projects_v4_for_both_renderers() -> None:
    primary = replace(
        _source(
            _SOURCE_ID,
            url="https://primary.example/report",
            title="Primary report",
            publisher="Primary Press",
            published_at=date(2026, 10, 14),
        ),
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    technical_context = replace(
        _source(
            UUID("00000000-0000-0000-0000-000000000003"),
            url="https://technical.example/report",
            title="Sandbox report",
            publisher="Sandbox",
            published_at=date(2026, 10, 15),
        ),
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    corroboration = replace(
        _source(
            _SECOND_SOURCE_ID,
            url="https://corroboration.example/report",
            title="Corroboration report",
            publisher="Analyst Desk",
            published_at=date(2026, 10, 16),
        ),
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )
    brief = wrap_publication_document_v7(
        _publication(sources=(primary, technical_context, corroboration)),
        base_publication_artifact_id=UUID("20000000-0000-0000-0000-000000000004"),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="8" * 64,
        routing_policy_version="test-v1",
    )
    policy_v4 = "brief-assembly-v4-m19-publication-events"

    def timeline(model: Any) -> list[dict[str, object]]:
        section = next(item for item in model.content_sections if item["type"] == "chronology")
        return section["timeline"]

    legacy_model = project_publication_to_typst_model(brief, include_brief_chronology=True)
    v4_model = project_publication_to_typst_model(
        brief,
        include_brief_chronology=True,
        brief_timeline_policy_version=policy_v4,
    )
    assert [row["display_date"] for row in timeline(legacy_model)] == [
        "14 octobre 2026",
        "15 octobre 2026",
        "16 octobre 2026",
    ]
    assert [row["display_date"] for row in timeline(v4_model)] == [
        "14 octobre 2026",
        "16 octobre 2026",
    ]

    publication_bundle = TypstTemplateBundle(
        template_version="publication-test-v1",
        sha256="d" * 64,
        files=(TemplateFile("RENDERER/publication.typ", b"#publication"),),
    )
    publication_source = TypstRenderer().render(
        brief,
        publication_bundle,
        document_label="Brief",
        brief_timeline_policy_version=policy_v4,
    )
    publication_data = json.loads(publication_source.render_data_bytes)
    publication_chronology = next(
        section
        for section in publication_data["content_sections"]
        if section["type"] == "chronology"
    )
    assert [row["display_date"] for row in publication_chronology["timeline"]] == [
        "14 octobre 2026",
        "16 octobre 2026",
    ]

    edition = EditionDocumentV2(
        edition={
            "id": "30000000-0000-0000-0000-000000000002",
            "country": "Iran",
            "country_code": "IR",
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "tlp": "TLP:GREEN",
            "languages": ["fr"],
            "version": 1,
        },
        publications=(
            EditionPublicationV2(position=1, subject_id=brief.subject_id, document=brief),
        ),
    )
    edition_bundle = TypstTemplateBundle(
        template_version="edition-test-v1",
        sha256="e" * 64,
        files=(TemplateFile("RENDERER/edition.typ", b"#edition"),),
    )
    edition_source = EditionTypstRenderer().render(
        edition,
        edition_bundle,
        document_label="Edition",
        artifact_identities={str(brief.subject_id): {"brief_assembly_policy_version": policy_v4}},
    )
    edition_data = json.loads(edition_source.render_data_bytes)
    edition_chronology = next(
        section
        for section in edition_data["publications"][0]["content_sections"]
        if section["type"] == "chronology"
    )
    assert [row["display_date"] for row in edition_chronology["timeline"]] == [
        "14 octobre 2026",
        "16 octobre 2026",
    ]
