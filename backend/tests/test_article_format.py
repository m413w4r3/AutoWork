from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_normalization import normalize_publication_indicator_value
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
    select_article_timeline_key_events,
)
from cti_app.application.publication_qa import qa_publication_v7
from cti_app.application.semantic_annotation import SemanticAnnotator
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
)
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    ArticleFormatV1,
    ArticleOverviewV1,
    IOCArticleCategoriesV1,
    OverviewFieldV1,
    PublicationDocumentV5,
    PublicationDocumentV7,
    PublicationDocumentV8,
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    PublicationIOCValueV1,
    TimelineKeyEventV1,
    extract_article_overview,
    parse_publication_document,
    publication_document_text_anchors,
    publication_document_v5_to_json,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import SemanticTextV1
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with
from tests.test_publication_document_v7 import _v6


def _publication_ref(source_id: UUID, evidence_key: str) -> PublicationEvidenceRefV1:
    return PublicationEvidenceRefV1(
        source_document_id=source_id,
        kind=PublicationEvidenceKind.FACT,
        evidence_key=evidence_key,
    )


def _overview_section(ref: PublicationEvidenceRefV1) -> PublicationSectionV1:
    return PublicationSectionV1(
        kind=PublicationSectionKind.OVERVIEW,
        heading="  VUE   D\u2019ENSEMBLE  ",
        paragraphs=(
            PublicationParagraphV1("Victimologie : Secteur public ciblé.", (ref,)),
            PublicationParagraphV1("Arsenal : Chargeur et outil distant.", (ref,)),
            PublicationParagraphV1("Objectif principal : Voler des données.", (ref,)),
        ),
    )


def _article_with_overview() -> tuple[PublicationDocumentV5, object, object, object]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_refs = extraction_evidence_refs_v1(extraction)
    assert evidence_refs
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Vue d\u2019ensemble",
                (
                    SynthesisParagraphV1(
                        "Victimologie : Secteur public ciblé.", (evidence_refs[0],)
                    ),
                    SynthesisParagraphV1(
                        "Arsenal : Chargeur et outil distant.", (evidence_refs[0],)
                    ),
                    SynthesisParagraphV1(
                        "Objectif principal : Voler des données.", (evidence_refs[1],)
                    ),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Activité observée",
                (
                    SynthesisParagraphV1(
                        "Le rapport décrit une activité réseau.", (evidence_refs[0],)
                    ),
                ),
            ),
        ),
    )
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    return publication, snapshot, references, (extraction, synthesis, enrichment)


def _routed_v8(
    base: PublicationDocumentV5,
    article_format: ArticleFormatV1,
    kind: PublicationKind = PublicationKind.ARTICLE,
) -> PublicationDocumentV8:
    return PublicationDocumentV8(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=kind,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=article_format,
    )


def test_overview_extraction_is_labelled_evidenced_and_tolerates_editorial_spacing() -> None:
    source_id = UUID(int=1)
    ref = _publication_ref(source_id, "1" * 64)

    overview = extract_article_overview((_overview_section(ref),))

    assert overview == ArticleOverviewV1(
        victimology=OverviewFieldV1("Secteur public ciblé.", (ref,)),
        arsenal=OverviewFieldV1("Chargeur et outil distant.", (ref,)),
        main_objective=OverviewFieldV1("Voler des données.", (ref,)),
    )


def test_overview_extraction_returns_null_fields_for_missing_or_incomplete_sections() -> None:
    source_id = UUID(int=2)
    ref = _publication_ref(source_id, "2" * 64)
    partial = PublicationSectionV1(
        PublicationSectionKind.OVERVIEW,
        "Vue d'ensemble",
        (PublicationParagraphV1("Victimologie : Hôpitaux.", (ref,)),),
    )
    malformed = PublicationSectionV1(
        PublicationSectionKind.OVERVIEW,
        "Vue d'ensemble",
        (
            PublicationParagraphV1("Victimologie : Hôpitaux.", (ref,)),
            PublicationParagraphV1("Arsenal : Outil.", (ref,)),
            PublicationParagraphV1("Objectif principal sans séparateur.", (ref,)),
        ),
    )

    assert extract_article_overview(()) is None
    assert extract_article_overview((partial,)) is None
    overview = extract_article_overview((malformed,))
    assert overview is not None
    assert overview.victimology == OverviewFieldV1("Hôpitaux.", (ref,))
    assert overview.arsenal == OverviewFieldV1("Outil.", (ref,))
    assert overview.main_objective is None
    assert extract_article_overview((_overview_section(ref), partial)) is not None


def test_timeline_selection_deduplicates_caps_and_prefers_multi_source_events() -> None:
    refs_by_count = {
        day: tuple(
            _publication_ref(UUID(int=day * 10 + source), f"{day:02x}" * 32)
            for source in range(count)
        )
        for day, count in {
            1: 1,
            2: 1,
            3: 2,
            4: 5,
            5: 4,
            6: 4,
            7: 3,
            8: 1,
            9: 1,
            10: 1,
        }.items()
    }
    entries = [
        PublicationTimelineEntryV1(
            date(2026, 3, day),
            f"date {day}",
            f"Event {day:02d}.",
            refs_by_count[day],
        )
        for day in range(1, 11)
    ]
    entries.extend(
        (
            PublicationTimelineEntryV1(
                date(2026, 3, 2),
                "relative date ignored",
                "EVENT   02.",
                (_publication_ref(UUID(int=999), "f" * 64),),
            ),
            PublicationTimelineEntryV1(
                None,
                "early March",
                "Undated event.",
                (_publication_ref(UUID(int=998), "e" * 64),),
            ),
        )
    )

    selected = select_article_timeline_key_events(tuple(entries))

    assert len(selected) == 8
    assert [event.event_date.day for event in selected] == [1, 2, 3, 4, 5, 6, 7, 10]
    assert len(selected[1].evidence_refs) == 2
    assert select_article_timeline_key_events(tuple(entries[:1])) == ()
    assert select_article_timeline_key_events(tuple(entries[-1:])) == ()


def test_typst_article_timeline_uses_exact_iso_dates_and_french_display_dates() -> None:
    _base, snapshot, references, inputs = _article_with_overview()
    extraction, synthesis, _enrichment = inputs
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = replace(
        synthesis,
        timeline=(
            SynthesisTimelineEntryV1(
                date(2026, 4, 1), "1 April 2026", "The implant was identified.", (evidence_ref,)
            ),
            SynthesisTimelineEntryV1(
                date(2026, 3, 12),
                "12 March 2026",
                "The first sample appeared.",
                (evidence_ref,),
            ),
        ),
    )
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    article = _routed_v8(base, build_article_format_v1(base, extraction))

    model = project_publication_to_typst_model(article)

    assert model.article_format is not None
    assert model.article_format["timeline_key_events"] == [
        {
            "date_iso": "2026-03-12",
            "display_date_fr": "12 mars 2026",
            "text": "The first sample appeared.",
        },
        {
            "date_iso": "2026-04-01",
            "display_date_fr": "1er avril 2026",
            "text": "The implant was identified.",
        },
    ]


def test_timeline_article_format_requires_exact_date_order_and_evidence() -> None:
    source_id = UUID(int=4)
    ref = _publication_ref(source_id, "4" * 64)
    categories = IOCArticleCategoriesV1((), (), (), ())

    with pytest.raises(ValueError, match="evidence_refs"):
        OverviewFieldV1("A sourced overview field.", ())
    with pytest.raises(ValueError, match="evidence_refs"):
        TimelineKeyEventV1(date(2026, 4, 1), "Event.", ())
    with pytest.raises(ValueError, match="date"):
        TimelineKeyEventV1("2026-04-01", "Event.", (ref,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="date order"):
        ArticleFormatV1(
            overview=None,
            timeline_key_events=(
                TimelineKeyEventV1(date(2026, 4, 2), "Later.", (ref,)),
                TimelineKeyEventV1(date(2026, 4, 1), "Earlier.", (ref,)),
            ),
            ioc_categories=categories,
        )
    with pytest.raises(ValueError, match="deterministic order"):
        IOCArticleCategoriesV1(
            (),
            (),
            (
                PublicationIOCValueV1("z.example", "z.example", (source_id,), (ref,)),
                PublicationIOCValueV1("a.example", "a.example", (source_id,), (ref,)),
            ),
            (),
        )


def test_v8_round_trip_wraps_exact_v7_and_brief_has_null_article_format() -> None:
    base, _snapshot, _references, inputs = _article_with_overview()
    extraction = inputs[0]
    article_format = build_article_format_v1(base, extraction)
    article = _routed_v8(base, article_format)
    v7_payload = article.base_publication._to_json()
    v5_payload = publication_document_v5_to_json(base)
    v8_payload = serialize_publication_document(article)
    v4_payload = serialize_publication_document(base.document)

    assert v8_payload["base_publication"] == v7_payload
    assert v7_payload["base_publication"]["base_publication"] == v5_payload
    frozen_payload_pairs = (
        (v7_payload, v8_payload["base_publication"]),
        (v7_payload["base_publication"], v8_payload["base_publication"]["base_publication"]),
        (
            v5_payload,
            v8_payload["base_publication"]["base_publication"]["base_publication"],
        ),
    )
    assert all(
        hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(before)).digest()
        == hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(after)).digest()
        for before, after in frozen_payload_pairs
    )
    assert serialize_publication_document(parse_publication_document(v4_payload)) == v4_payload
    assert parse_publication_document(v8_payload) == article
    assert serialize_publication_document(article)["schema_version"] == "8"
    evidence_ref = base.document.lead[0].evidence_refs[0]
    noncanonical_format = replace(
        article_format,
        timeline_key_events=(
            TimelineKeyEventV1(date(2026, 4, 1), "Event one.", (evidence_ref,)),
            TimelineKeyEventV1(date(2026, 4, 2), "Event two.", (evidence_ref,)),
        ),
    )
    with pytest.raises(ValueError, match="deterministic canonical selection"):
        _routed_v8(base, noncanonical_format)

    brief = _routed_v8(base, None, PublicationKind.BRIEF)
    assert serialize_publication_document(brief)["article_format"] is None
    assert parse_publication_document(serialize_publication_document(brief)) == brief
    with pytest.raises(ValueError, match="requires ArticleFormatV1"):
        _routed_v8(base, None)

    invalid_date_payload = json.loads(json.dumps(v8_payload))
    invalid_date_payload["article_format"]["timeline_key_events"] = [
        {"event_date": "20260401", "text": "Bad date", "evidence_refs": []}
    ]
    with pytest.raises(ValueError, match="canonical ISO date"):
        parse_publication_document(invalid_date_payload)


def test_builder_groups_iocs_into_reader_categories_without_emails_or_statuses() -> None:
    base = _article_with_overview()[0]
    extraction = _article_with_overview()[3][0]
    source = extraction.sources[0]
    values = (
        ("198.51.100[.]7", ArtifactType.IP, ExtractionIndicatorStatus.CONFIRMED_IOC),
        (
            "hxxps://cdn[.]example.test/path",
            ArtifactType.URL,
            ExtractionIndicatorStatus.CONFIRMED_IOC,
        ),
        ("c2[.]example.test", ArtifactType.DOMAIN, ExtractionIndicatorStatus.CONFIRMED_IOC),
        ("A" * 64, ArtifactType.HASH, ExtractionIndicatorStatus.CONFIRMED_IOC),
        ("analyst@example.test", ArtifactType.EMAIL, ExtractionIndicatorStatus.CONFIRMED_IOC),
        (
            "context[.]example.test",
            ArtifactType.DOMAIN,
            ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL,
        ),
        (
            "C2[.]EXAMPLE.TEST",
            ArtifactType.DOMAIN,
            ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL,
        ),
        ("payload.exe", ArtifactType.FILENAME, ExtractionIndicatorStatus.CONFIRMED_IOC),
        ("/tmp/payload.exe", ArtifactType.FILEPATH, ExtractionIndicatorStatus.CONFIRMED_IOC),
    )
    extraction_indicators = tuple(
        ExtractionIndicatorV1(
            value=value,
            artifact_type=artifact_type,
            indicator_status=status,
            context="Observed in the source.",
            evidence_quote=f"The report includes {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        )
        for value, artifact_type, status in values
    )
    extraction = replace(
        extraction,
        sources=(replace(source, indicators=extraction_indicators),),
    )
    publication_groups: list[PublicationIndicatorGroupV1] = []
    original_indicators: list[PublicationIndicatorV1] = []
    details: list[PublicationIOCDetailV1] = []
    for value, artifact_type, status in values:
        normalized = normalize_publication_indicator_value(value, artifact_type)
        if status is ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL:
            if value.casefold().startswith("c2"):
                details.append(
                    PublicationIOCDetailV1(
                        artifact_type=artifact_type,
                        value=value,
                        normalized_value=normalized,
                        source_role=source.role,
                        context="The same domain is also listed in a contextual row.",
                        evidence_quote=f"The report includes {value}.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source.source_document_id,),
                        disposition=PublicationIOCDisposition.RETAINED,
                        disposition_reason="retained_publication_ioc",
                    )
                )
                continue
            indicator = PublicationIndicatorV1(
                value=normalized,
                normalized_value=normalized,
                artifact_type=artifact_type,
                source_document_ids=(source.source_document_id,),
            )
            original_indicators.append(indicator)
        elif artifact_type in {
            ArtifactType.IP,
            ArtifactType.URL,
            ArtifactType.DOMAIN,
            ArtifactType.HASH,
            ArtifactType.EMAIL,
        }:
            indicator = PublicationIndicatorV1(
                value=normalized,
                normalized_value=normalized,
                artifact_type=artifact_type,
                source_document_ids=(source.source_document_id,),
            )
            publication_groups.append(PublicationIndicatorGroupV1(artifact_type, (indicator,)))
        else:
            details.append(
                PublicationIOCDetailV1(
                    artifact_type=artifact_type,
                    value=value,
                    normalized_value=normalized,
                    source_role=source.role,
                    context="File observed in the report.",
                    evidence_quote=f"The report includes {value}.",
                    evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    source_document_ids=(source.source_document_id,),
                    disposition=PublicationIOCDisposition.EXCLUDED,
                    disposition_reason="unsupported_artifact_type",
                )
            )
    publication = replace(
        base,
        document=replace(base.document, indicators=tuple(publication_groups)),
        original_indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.DOMAIN,
                tuple(
                    item
                    for item in original_indicators
                    if item.artifact_type is ArtifactType.DOMAIN
                ),
            ),
        ),
        ioc_details=tuple(details),
    )

    article_format = build_article_format_v1(publication, extraction)

    assert len(article_format.ioc_categories.ips) == 1
    assert len(article_format.ioc_categories.urls) == 1
    assert len(article_format.ioc_categories.domains) == 2
    assert len(article_format.ioc_categories.files) == 3
    c2_value = next(
        item
        for item in article_format.ioc_categories.domains
        if item.normalized_value == "c2.example.test"
    )
    assert len(c2_value.evidence_refs) == 2
    assert all(
        item.evidence_refs
        for category in (
            article_format.ioc_categories.ips,
            article_format.ioc_categories.urls,
            article_format.ioc_categories.domains,
            article_format.ioc_categories.files,
        )
        for item in category
    )
    all_reader_values = {
        item.normalized_value
        for category in (
            article_format.ioc_categories.ips,
            article_format.ioc_categories.urls,
            article_format.ioc_categories.domains,
            article_format.ioc_categories.files,
        )
        for item in category
    }
    assert "analyst@example.test" not in all_reader_values


def test_typst_v33_projection_removes_overview_duplication_and_legacy_sections() -> None:
    base, _snapshot, _references, (extraction, _synthesis, _enrichment) = _article_with_overview()
    article_format = build_article_format_v1(base, extraction)
    article = _routed_v8(base, article_format)

    model = project_publication_to_typst_model(article)
    section_types = [section["type"] for section in model.content_sections]
    synthesis = next(
        section for section in model.content_sections if section["type"] == "synthesis"
    )
    body_text = " ".join(block.get("text", "") for block in synthesis["blocks"])

    assert model.article_format is not None
    assert model.article_format["overview"]["victimology"]["text"] == "Secteur public ciblé."
    assert "Victimologie : Secteur public ciblé." not in body_text
    assert "Arsenal : Chargeur et outil distant." not in body_text
    assert section_types == ["references", "synthesis"]
    assert not {"chronology", "technical_annex", "provenance"}.intersection(section_types)
    assert all(
        {"status", "status_label", "indicator_status", "provenance"}.isdisjoint(item)
        for category in model.article_format["ioc_categories"].values()
        for item in category
    )


def test_legacy_overview_section_is_removed_only_when_overview_is_projected() -> None:
    base, *_ = _article_with_overview()
    evidence_ref = base.sections[0].paragraphs[0].evidence_refs[0]

    def project_with_overview(paragraph_texts: tuple[str, str, str]):
        overview_section = PublicationSectionV1(
            PublicationSectionKind.OVERVIEW,
            "Vue d'ensemble",
            tuple(PublicationParagraphV1(text, (evidence_ref,)) for text in paragraph_texts),
        )
        legacy_document = replace(
            base.document,
            sections=(overview_section, *base.sections[1:]),
        )
        semantic_text = SemanticTextV1(
            schema_version=base.semantic_text.schema_version,
            policy_version=base.semantic_text.policy_version,
            paragraphs=SemanticAnnotator().annotate_paragraphs(
                tuple(
                    publication_document_text_anchors(
                        legacy_document,
                        references=base.references,
                    ).items()
                ),
                entities=(),
            ),
        )
        legacy_base = replace(
            base,
            document=legacy_document,
            semantic_text=semantic_text,
        )
        v7 = PublicationDocumentV7(
            base_publication=_v6(legacy_base),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="b" * 64,
            routing_policy_version="routing-policy-v1",
        )
        return project_publication_to_typst_model(v7)

    unlabelled_model = project_with_overview(
        ("Contexte narratif libre.", "Activité observée.", "Impact décrit.")
    )
    unlabelled_synthesis = next(
        section for section in unlabelled_model.content_sections if section["type"] == "synthesis"
    )
    unlabelled_text = " ".join(block.get("text", "") for block in unlabelled_synthesis["blocks"])
    assert "Vue d'ensemble" in unlabelled_text
    assert unlabelled_model.article_format is not None
    assert unlabelled_model.article_format["overview"] is None

    labelled_model = project_with_overview(
        ("Victimologie : Hôpitaux.", "Activité observée.", "Impact décrit.")
    )
    labelled_synthesis = next(
        section for section in labelled_model.content_sections if section["type"] == "synthesis"
    )
    labelled_text = " ".join(block.get("text", "") for block in labelled_synthesis["blocks"])
    assert "Vue d'ensemble" not in labelled_text
    assert labelled_model.article_format is not None
    assert labelled_model.article_format["overview"]["victimology"]["text"] == "Hôpitaux."


@pytest.mark.asyncio
async def test_article_format_v8_qa_contract_is_checked() -> None:
    publication, snapshot, references, inputs = _article_with_overview()
    extraction, synthesis, enrichment = inputs
    article = _routed_v8(publication, build_article_format_v1(publication, extraction))
    relevance_projection = build_relevance_projection(snapshot, extraction)

    result = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance_projection,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=article,
    )

    assert result["checks"]["article_format_present"] is True
    assert result["checks"]["reader_ioc_projection"] is True


def test_builder_matches_defanged_publication_value_to_canonical_evidence() -> None:
    base = _article_with_overview()[0]
    extraction = _article_with_overview()[3][0]
    source = extraction.sources[0]
    canonical = "afterworld.store"
    extraction = replace(
        extraction,
        sources=(
            replace(
                source,
                indicators=(
                    ExtractionIndicatorV1(
                        value=canonical,
                        artifact_type=ArtifactType.DOMAIN,
                        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
                        context="Observed in the source.",
                        evidence_quote=f"The report includes {canonical}.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source.source_document_id,),
                    ),
                ),
            ),
        ),
    )
    indicator = PublicationIndicatorV1(
        value="afterworld[.]store",
        normalized_value="afterworld[.]store",
        artifact_type=ArtifactType.DOMAIN,
        source_document_ids=(source.source_document_id,),
    )
    document = replace(
        base.document,
        indicators=(PublicationIndicatorGroupV1(ArtifactType.DOMAIN, (indicator,)),),
    )
    article_format = build_article_format_v1(replace(base, document=document), extraction)

    assert [item.normalized_value for item in article_format.ioc_categories.domains] == [canonical]
    assert article_format.ioc_categories.domains[0].evidence_refs


def test_builder_keeps_range_ioc_with_document_level_evidence_instead_of_failing() -> None:
    base = _article_with_overview()[0]
    extraction = _article_with_overview()[3][0]
    source = extraction.sources[0]
    extraction = replace(
        extraction,
        sources=(
            replace(
                source,
                indicators=(
                    ExtractionIndicatorV1(
                        value="198.51.100.7",
                        artifact_type=ArtifactType.IP,
                        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
                        context="Observed in the source.",
                        evidence_quote="The report includes 198.51.100.7.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source.source_document_id,),
                    ),
                ),
            ),
        ),
    )
    indicator = PublicationIndicatorV1(
        value="198.51.100.0/24",
        normalized_value="198.51.100.0/24",
        artifact_type=ArtifactType.IP,
        source_document_ids=(source.source_document_id,),
    )
    document = replace(
        base.document,
        indicators=(PublicationIndicatorGroupV1(ArtifactType.IP, (indicator,)),),
    )
    article_format = build_article_format_v1(replace(base, document=document), extraction)

    assert [item.value for item in article_format.ioc_categories.ips] == ["198.51.100.0/24"]
    assert article_format.ioc_categories.ips[0].evidence_refs
