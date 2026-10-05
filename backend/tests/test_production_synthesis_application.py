"""Deterministic inputs and canonical orchestration for production synthesis."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

import cti_app.application.production_synthesis as synthesis_module
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseResult
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
)
from cti_app.application.production_synthesis import (
    MAX_SYNTHESIS_UNCERTAINTIES,
    MAX_TECHNICAL_EVIDENCE_V1,
    SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION,
    SYNTHESIS_MODEL_POLICY_VERSION,
    SYNTHESIS_ROUTING_POLICY_VERSION,
    ProductionSynthesisService,
    SynthesisClaimProposalV1,
    SynthesisEvidencePackV1,
    SynthesisExecutionStatus,
    SynthesisProposalControlError,
    SynthesisProposalErrorCode,
    SynthesisProposalV1,
    SynthesisSectionProposalV1,
    SynthesisStageErrorCode,
    _date_supported_by_payload,
    build_synthesis_access_policy,
    build_synthesis_delta,
    build_synthesis_evidence_pack,
    build_synthesis_model_request,
    build_synthesis_timeline,
    build_synthesis_uncertainties,
    canonical_extraction_hash,
    draft_synthesis_proposal,
    extraction_evidence_elements,
    parse_synthesis_proposal_wire,
    render_synthesis_markdown,
    synthesis_access_policy_hash,
    synthesis_evidence_pack_hash,
    synthesis_input_hash,
    synthesis_invocation_hash,
    synthesis_model_run_id,
    validate_synthesis_proposal,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelUsage
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ExtractionRuleV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
    resolve_timeline_date_text,
)
from cti_app.domain.publication import ArtifactType


def make_snapshot(subject_id: UUID) -> ProductionInputSnapshot:
    ids = [uuid4() for _ in range(7)]
    return ProductionInputSnapshot(
        production_run_id=ids[0],
        edition_id=ids[1],
        subject_id=subject_id,
        subject_version=1,
        subject_title="Frozen subject title",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=ids[2],
        origin_discovery_subject_id=ids[3],
        canonical_discovery_subject_id=ids[4],
        discovery_snapshot_id=ids[5],
        discovery_snapshot_version=1,
        member_candidate_ids=(ids[6],),
        discovery_summary="A discovery summary.",
        actor_or_campaign="Example actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        publication_language="fr",
        research_date=date(2026, 3, 1),
    )


def make_document(
    subject_id: UUID,
    source_id: UUID,
    *,
    tlp: TLP = TLP.CLEAR,
    external_llm_allowed: bool = True,
    do_not_submit: bool = False,
) -> SourceDocument:
    return SourceDocument(
        id=source_id,
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="source.pdf",
        origin="test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=tlp,
        external_llm_allowed=external_llm_allowed,
        do_not_submit=do_not_submit,
    )


class MemorySourceDocuments:
    def __init__(self, documents: tuple[SourceDocument, ...]) -> None:
        self.documents = {document.id: document for document in documents}
        self.requested: list[UUID] = []

    async def get(self, document_id: UUID) -> SourceDocument | None:
        self.requested.append(document_id)
        return self.documents.get(document_id)


class MemorySourceCollections:
    def __init__(self, collections: tuple[SourceCollection, ...] = ()) -> None:
        self.collections = {collection.id: collection for collection in collections}

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        return self.collections.get(collection_id)


def make_fact(source_id: UUID, value: str, *, context: str = "") -> ExtractionFactV1:
    return ExtractionFactV1(
        category="malware",
        value=value,
        attack_id=None,
        context=context,
        evidence_quote=f"The report identifies {value}.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_event(
    source_id: UUID,
    text: str,
    event_date: date | None,
    *,
    date_text: str | None = None,
) -> ExtractionEventV1:
    return ExtractionEventV1(
        event_date=event_date,
        date_text=date_text,
        text=text,
        context="Campaign chronology.",
        evidence_quote=f"The report states: {text}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_indicator(source_id: UUID, value: str) -> ExtractionIndicatorV1:
    return ExtractionIndicatorV1(
        value=value,
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Published as command infrastructure.",
        evidence_quote=f"Domain: {value}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_source(
    source_id: UUID,
    *,
    tier: ProductionReferenceTier = ProductionReferenceTier.CORE,
    profile: ExtractionProfile | None = None,
    role: SourceRole = SourceRole.PRIMARY,
    editorial_role: ProductionEditorialRole | None = None,
    kind: ProductionReferenceKind = ProductionReferenceKind.PUBLICATION,
    facts: tuple[ExtractionFactV1, ...] = (),
    events: tuple[ExtractionEventV1, ...] = (),
    indicators: tuple[ExtractionIndicatorV1, ...] = (),
    rules: tuple[ExtractionRuleV1, ...] = (),
    uncertainties: tuple[str, ...] = (),
    url_suffix: str = "core",
) -> ProductionSourceExtractionV1:
    return ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url=f"https://example.com/{url_suffix}",
        content_sha256=hashlib.sha256(url_suffix.encode()).hexdigest(),
        tier=tier,
        kind=kind,
        role=role,
        editorial_role=editorial_role,
        profile=profile
        or (
            ExtractionProfile.FULL
            if tier is ProductionReferenceTier.CORE
            else ExtractionProfile.IOC_RULES
        ),
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=facts,
        events=events,
        indicators=indicators,
        rules=rules,
        uncertainties=uncertainties,
    )


def make_extraction(
    subject_id: UUID, sources: tuple[ProductionSourceExtractionV1, ...]
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=sources,
        omitted_sources=(),
        warnings=(),
    )


def make_proposal(
    text: str,
    handle: str,
    *,
    section_kind: str = "overview",
    heading: str = "Overview",
) -> dict[str, object]:
    return {
        "lead": [{"text": text, "evidence_handles": [handle]}],
        "sections": [
            {
                "kind": section_kind,
                "heading": heading,
                "claims": [{"text": text, "evidence_handles": [handle]}],
            }
        ],
    }


def assert_proposal_error(call: Callable[[], object], expected_code: str) -> None:
    with pytest.raises(SynthesisProposalControlError) as error:
        call()
    assert error.value.code == expected_code


def test_refs_hash_and_prompt_handles_are_stable_across_source_load_order():
    subject_id = uuid4()
    core_id, technical_id = uuid4(), uuid4()
    core = make_source(
        core_id,
        facts=(make_fact(core_id, "FooRAT"), make_fact(core_id, "BarRAT")),
        url_suffix="a-core",
    )
    technical = make_source(
        technical_id,
        tier=ProductionReferenceTier.TECHNICAL,
        facts=(make_fact(technical_id, "Technical-only claim"),),
        indicators=(make_indicator(technical_id, "infra.example"),),
        url_suffix="z-technical",
    )
    first = make_extraction(subject_id, (core, technical))
    second = make_extraction(subject_id, (technical, core))

    assert canonical_extraction_hash(first) == canonical_extraction_hash(second)
    assert extraction_evidence_refs_v1(first) == extraction_evidence_refs_v1(second)
    first_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), first)
    second_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), second)
    assert first_pack.narrative_evidence == second_pack.narrative_evidence
    assert first_pack.technical_evidence == second_pack.technical_evidence
    assert tuple(record["handle"] for record in first_pack.narrative_evidence) == tuple(
        record["handle"] for record in second_pack.narrative_evidence
    )
    assert first_pack.policy_version == SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION
    assert all("source_document_id" not in record for record in first_pack.narrative_evidence)
    assert "technical-only claim" not in str(first_pack.narrative_evidence)

    ref = first_pack.resolve_handle(first_pack.narrative_evidence[0]["handle"])
    assert ref.source_document_id == core_id
    try:
        first_pack.resolve_handle("E999")
    except ValueError as exc:
        assert str(exc) == "synthesis_unknown_evidence"
    else:
        raise AssertionError("unknown handles must not be approximated")


def test_core_full_facts_and_events_follow_core_narrative_evidence():
    subject_id = uuid4()
    core_id, supporting_id = uuid4(), uuid4()
    core = make_source(
        core_id,
        facts=(make_fact(core_id, "CoreReport"),),
        events=(make_event(core_id, "The core incident began.", date(2026, 1, 3)),),
        url_suffix="core-primary",
    )
    supporting = make_source(
        supporting_id,
        tier=ProductionReferenceTier.CORE,
        profile=ExtractionProfile.FULL,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        facts=(make_fact(supporting_id, "IndependentResearch"),),
        events=(
            make_event(
                supporting_id,
                "The independent analysis confirmed activity.",
                date(2026, 1, 4),
            ),
        ),
        url_suffix="core-independent",
    )
    extraction = make_extraction(subject_id, (supporting, core))

    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    records = pack.narrative_evidence
    core_positions = [
        index for index, record in enumerate(records) if record["source_role"] == "primary"
    ]
    supporting_positions = [
        index for index, record in enumerate(records) if record["source_role"] == "independent"
    ]

    assert {record["kind"] for record in records if record["source_role"] == "independent"} == {
        "fact",
        "event",
    }
    assert max(core_positions) < min(supporting_positions)
    assert all(
        record["editorial_role"] == ProductionEditorialRole.CORROBORATION.value
        for record in records
        if record["source_role"] == "independent"
    )
    assert str(supporting_id) not in str(records)


def test_changed_fact_payload_changes_its_evidence_key():
    subject_id, source_id = uuid4(), uuid4()
    original_fact = make_fact(source_id, "FooRAT")
    changed_fact = replace(original_fact, value="BarRAT")
    original = make_extraction(
        subject_id,
        (make_source(source_id, facts=(original_fact,)),),
    )
    changed = make_extraction(
        subject_id,
        (make_source(source_id, facts=(changed_fact,)),),
    )

    original_ref = next(
        ref for ref in extraction_evidence_refs_v1(original) if ref.kind is EvidenceKind.FACT
    )
    changed_ref = next(
        ref for ref in extraction_evidence_refs_v1(changed) if ref.kind is EvidenceKind.FACT
    )
    assert original_ref.source_document_id == changed_ref.source_document_id
    assert original_ref.evidence_key != changed_ref.evidence_key


def test_date_grounding_preserves_approximate_precision_and_checks_quotes():
    approximate_event = {
        "event_date": date(2024, 6, 15),
        "date_text": "mid-2024",
    }
    assert not _date_supported_by_payload(approximate_event, "date:2024-06-15")

    quoted_fact = {
        "category": "timeline",
        "value": "Deployment was reported.",
        "context": "",
        "evidence_quote": "The report dates deployment to 2025-04-06.",
    }
    assert _date_supported_by_payload(quoted_fact, "date:2025-04-06")


def test_technical_pack_is_contextual_body_free_deterministic_and_bounded():
    subject_id, source_id = uuid4(), uuid4()
    indicators = tuple(
        make_indicator(source_id, f"host-{index:03d}.example") for index in range(140)
    )
    body = "rule body must remain in extraction only"
    rule = ExtractionRuleV1(
        rule_type=DetectionRuleType.YARA,
        name="Example rule",
        body=body,
        sha256=hashlib.sha256(body.encode()).hexdigest(),
        context="Detects the published sample.",
        evidence_quote="A YARA rule is provided.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    source = make_source(
        source_id,
        tier=ProductionReferenceTier.SUPPORTING,
        facts=(make_fact(source_id, "Supporting campaign claim"),),
        indicators=indicators,
        rules=(rule,),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    reordered = make_extraction(
        subject_id,
        (replace(source, indicators=tuple(reversed(indicators))),),
    )
    reordered_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), reordered)

    assert pack.narrative_evidence == ()
    assert len(pack.technical_evidence) == MAX_TECHNICAL_EVIDENCE_V1
    assert pack.technical_evidence == reordered_pack.technical_evidence
    assert all("body" not in record for record in pack.technical_evidence)
    assert body not in str(pack.technical_evidence)
    rule_only_source = replace(source, facts=(), indicators=())
    rule_only = make_extraction(subject_id, (rule_only_source,))
    rule_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), rule_only)
    rule_record = rule_pack.technical_evidence[0]
    assert set(rule_record) == {
        "handle",
        "kind",
        "source_role",
        "editorial_role",
        "source_label",
        "type",
        "name",
        "sha256",
        "context",
        "evidence",
    }
    assert body not in str(rule_pack.technical_evidence)


def test_timeline_deduplicates_events_and_sorts_dated_before_undated():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    shared_date = date(2026, 7, 2)
    shared_text = "The campaign began."
    first = make_source(
        first_id,
        events=(
            make_event(first_id, "Undated activity", None),
            make_event(first_id, shared_text, shared_date, date_text="2 July 2026"),
            make_event(first_id, "Later activity", date(2026, 7, 4)),
        ),
        url_suffix="a-event",
    )
    second = make_source(
        second_id,
        tier=ProductionReferenceTier.SUPPORTING,
        events=(make_event(second_id, shared_text, shared_date, date_text="2 July 2026"),),
        url_suffix="b-event",
    )
    timeline = build_synthesis_timeline(make_extraction(subject_id, (second, first)))

    assert [entry.event_date for entry in timeline] == [
        date(2026, 7, 2),
        date(2026, 7, 4),
        None,
    ]
    assert timeline[0].text == shared_text
    assert len(timeline[0].evidence_refs) == 2
    assert timeline == build_synthesis_timeline(make_extraction(subject_id, (first, second)))


def test_timeline_resolves_english_and_french_dates_and_handles_relative_wording():
    subject_id, source_id = uuid4(), uuid4()
    date_texts = (
        "2026-09-17",
        "mid-2023",
        "mid-2025",
        "late December 2023",
        "early 2025",
        "February 2025",
        "février 2025",
        "2013",
        "late 2024",
        "Q2 2026",
        "2019",
        "Also that year",
        "fin 2024",
        "début 2025",
    )
    source = make_source(
        source_id,
        events=(
            *(
                make_event(source_id, f"English bulletin event {index}", None, date_text=text)
                for index, text in enumerate(date_texts)
            ),
            make_event(source_id, "Event without a date", None),
        ),
        url_suffix="timeline-language",
    )
    extraction = make_extraction(subject_id, (source,))

    timeline = build_synthesis_timeline(extraction)
    assert len(timeline) == len(date_texts) + 1
    resolved = [
        entry
        for entry in timeline
        if entry.date_text is not None and entry.date_text != "Also that year"
    ]
    resolved_dates = [resolve_timeline_date_text(entry.date_text) for entry in resolved]
    assert all(resolved_date is not None for resolved_date in resolved_dates)
    assert resolved_dates == sorted(
        resolved_date for resolved_date in resolved_dates if resolved_date is not None
    )
    assert timeline[-2].date_text is None
    assert timeline[-1].date_text == "Also that year"
    assert {entry.date_text for entry in timeline if entry.date_text is not None} == set(date_texts)
    assert timeline == build_synthesis_timeline(
        make_extraction(
            subject_id,
            (replace(source, events=tuple(reversed(source.events))),),
        )
    )

    warnings: list[str] = []
    filtered_timeline = build_synthesis_timeline(extraction, warnings=warnings)
    assert len(filtered_timeline) == len(date_texts)
    assert "Also that year" not in {entry.date_text for entry in filtered_timeline}
    assert filtered_timeline[-1].date_text is None
    assert warnings == ["Dropped 1 timeline event with unresolvable date wording."]


def test_timeline_deduplicates_normalized_text_and_preserves_a_stable_display_value():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    event_date = date(2025, 2, 2)
    first = make_source(
        first_id,
        events=(
            make_event(
                first_id,
                "Operation began!",
                event_date,
                date_text="February 2, 2025",
            ),
        ),
        url_suffix="timeline-normalized-a",
    )
    second = make_source(
        second_id,
        events=(
            make_event(
                second_id,
                "operation began.",
                event_date,
                date_text="2 February 2025",
            ),
        ),
        url_suffix="timeline-normalized-b",
    )

    timeline = build_synthesis_timeline(make_extraction(subject_id, (first, second)))
    reversed_timeline = build_synthesis_timeline(make_extraction(subject_id, (second, first)))

    assert timeline == reversed_timeline
    assert len(timeline) == 1
    assert timeline[0].event_date == event_date
    assert timeline[0].date_text == "2 February 2025"
    assert len(timeline[0].evidence_refs) == 2


def test_uncertainties_union_provenance_and_delta_compares_exact_refs():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    shared = "Attribution remains uncertain."
    previous = make_extraction(
        subject_id,
        (
            make_source(
                first_id,
                facts=(make_fact(first_id, "OldRAT"),),
                uncertainties=(shared, "Scope is unknown."),
                url_suffix="a-uncertainty",
            ),
            make_source(
                second_id,
                tier=ProductionReferenceTier.SUPPORTING,
                uncertainties=(shared,),
                url_suffix="b-uncertainty",
            ),
        ),
    )
    current = make_extraction(
        subject_id,
        (
            make_source(
                first_id,
                facts=(make_fact(first_id, "NewRAT"),),
                uncertainties=(shared, "Scope is unknown."),
                url_suffix="a-uncertainty",
            ),
            make_source(
                second_id,
                tier=ProductionReferenceTier.SUPPORTING,
                uncertainties=(shared,),
                url_suffix="b-uncertainty",
            ),
        ),
    )

    uncertainties = build_synthesis_uncertainties(previous)
    shared_uncertainty = next(item for item in uncertainties if item.text == shared)
    assert shared_uncertainty.source_document_ids == (first_id,)
    assert tuple(item.text for item in uncertainties) == (
        "Attribution remains uncertain.",
        "Scope is unknown.",
    )

    delta = build_synthesis_delta(previous, current)
    previous_refs = set(extraction_evidence_refs_v1(previous))
    current_refs = set(extraction_evidence_refs_v1(current))
    assert set(delta.added_evidence) == current_refs - previous_refs
    assert set(delta.removed_evidence) == previous_refs - current_refs
    assert set(delta.unchanged_evidence) == previous_refs & current_refs


def test_ioc_rules_uncertainty_is_excluded_from_synthesis_and_enrichment_packs():
    subject_id = uuid4()
    core_id, supporting_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    support_uncertainty = "The supporting source does not verify attribution."
    extraction = replace(
        make_extraction(
            subject_id,
            (
                make_source(
                    core_id,
                    facts=(make_fact(core_id, "CoreReport"),),
                    uncertainties=("The campaign operator is not identified.",),
                    url_suffix="core-uncertainty",
                ),
                make_source(
                    supporting_id,
                    tier=ProductionReferenceTier.SUPPORTING,
                    uncertainties=(support_uncertainty,),
                    url_suffix="supporting-uncertainty",
                ),
            ),
        ),
        production_input_hash=snapshot.input_hash,
    )
    synthesis_pack = build_synthesis_evidence_pack(snapshot, extraction)
    core_uncertainty = next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.UNCERTAINTY
        and ref.source_document_id == core_id
        and payload.get("text") == "The campaign operator is not identified."
    )
    supporting_uncertainty = next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.UNCERTAINTY
        and ref.source_document_id == supporting_id
        and payload.get("text") == support_uncertainty
    )

    assert support_uncertainty in extraction.sources[1].uncertainties
    assert synthesis_pack.handle_for(core_uncertainty) is not None
    assert synthesis_pack.handle_for(supporting_uncertainty) is None
    uncertainties = build_synthesis_uncertainties(extraction)
    assert {source_id for item in uncertainties for source_id in item.source_document_ids} == {
        core_id
    }

    synthesis = _canonical_synthesis(snapshot, extraction)
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1(support_uncertainty, (supporting_uncertainty,)),),
    )
    enrichment_pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    assert supporting_uncertainty not in enrichment_pack._handle_to_ref.values()
    assert supporting_uncertainty not in enrichment_pack._reserve_handle_to_ref.values()
    assert support_uncertainty not in str(enrichment_pack.narrative_evidence)
    assert support_uncertainty not in str(enrichment_pack.current_synthesis)
    assert support_uncertainty not in str(enrichment_pack.reserve_evidence)
    assert not enrichment_pack.current_synthesis["lead"]


def test_uncertainties_filter_known_noise_and_merge_punctuation_variants():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    first = make_source(
        first_id,
        uncertainties=(
            "Attribution remains uncertain!",
            "No complete YARA, Sigma, Suricata, or Snort detection rule is visible "
            "in the archived capture.",
            'The archived capture ends with the incomplete text "Address-po" ...',
            "...the supplied artifact schema has no blockchain-address type...",
            "La capture tronquée se termine sur un texte incomplet.",
        ),
        url_suffix="uncertainty-noise-a",
    )
    second = make_source(
        second_id,
        uncertainties=("attribution-remains uncertain.",),
        url_suffix="uncertainty-noise-b",
    )

    uncertainties = build_synthesis_uncertainties(make_extraction(subject_id, (first, second)))

    assert len(uncertainties) == 1
    assert uncertainties[0].text == "Attribution remains uncertain!"
    assert uncertainties[0].source_document_ids == tuple(sorted((first_id, second_id), key=str))


def test_uncertainties_are_capped_in_stable_normalized_order():
    subject_id, source_id = uuid4(), uuid4()
    values = tuple(f"Analytic issue {index:02d} remains unresolved" for index in range(12))
    source = make_source(source_id, uncertainties=values, url_suffix="uncertainty-cap")

    uncertainties = build_synthesis_uncertainties(make_extraction(subject_id, (source,)))

    assert len(uncertainties) == MAX_SYNTHESIS_UNCERTAINTIES
    assert tuple(item.text for item in uncertainties) == values[:MAX_SYNTHESIS_UNCERTAINTIES]


def test_proposal_resolves_exact_handles_to_canonical_evidence_refs():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    lead, sections = validate_synthesis_proposal(
        make_proposal("FooRAT was identified in the report.", handle), pack, extraction
    )

    assert lead[0].text == "FooRAT was identified in the report."
    assert lead[0].evidence_refs == (pack.resolve_handle(handle),)
    assert sections[0].kind is SynthesisSectionKind.OVERVIEW
    assert sections[0].heading == ""
    assert not hasattr(lead[0], "evidence_handles")


def test_one_paragraph_can_cite_multiple_supporting_handles():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (
            make_source(
                source_id,
                facts=(make_fact(source_id, "FooRAT"), make_fact(source_id, "BarRAT")),
            ),
        ),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handles_by_value = {
        str(record["value"]): str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.FACT.value
    }
    proposal = {
        "lead": [
            {
                "text": "FooRAT and BarRAT are both named in the report.",
                "evidence_handles": [handles_by_value["FooRAT"], handles_by_value["BarRAT"]],
            }
        ],
        "sections": [],
    }

    lead, sections = validate_synthesis_proposal(proposal, pack, extraction)

    assert sections == ()
    assert set(lead[0].evidence_refs) == {
        pack.resolve_handle(handles_by_value["FooRAT"]),
        pack.resolve_handle(handles_by_value["BarRAT"]),
    }


def test_proposal_rejects_unknown_handle_and_malformed_schema():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT was identified.", "E999"), pack, extraction
        ),
        "synthesis_unknown_evidence",
    )
    malformed = make_proposal("FooRAT was identified.", str(pack.narrative_evidence[0]["handle"]))
    malformed["subject_id"] = str(subject_id)
    assert_proposal_error(
        lambda: validate_synthesis_proposal(malformed, pack, extraction),
        "synthesis_output_invalid",
    )
    empty_refs = make_proposal("FooRAT was identified.", "unused")
    empty_refs["lead"] = [{"text": "FooRAT was identified.", "evidence_handles": []}]
    assert_proposal_error(
        lambda: validate_synthesis_proposal(empty_refs, pack, extraction),
        "synthesis_output_invalid",
    )


@pytest.mark.parametrize(
    "text",
    (
        "# Campaign overview",
        "| Source | Value |\n| --- | --- |",
        "A claim [S1] was reported.",
        "<p>A claim was reported.</p>",
        "---\ntitle: Campaign\n---",
    ),
)
def test_proposal_rejects_markdown_html_and_source_markers(text: str):
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(make_proposal(text, handle), pack, extraction),
        "synthesis_output_invalid",
    )


def test_proposal_drops_model_section_heading_without_rejecting_claims():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    _, sections = validate_synthesis_proposal(
        make_proposal("FooRAT was identified.", handle, heading="# Overview"),
        pack,
        extraction,
    )
    assert sections[0].heading == ""


def test_proposal_rejects_technical_literals_missing_from_extraction():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT exploits CVE-2026-9999.", handle), pack, extraction
        ),
        "synthesis_unknown_technical_value",
    )


def test_technical_literal_scanner_accepts_extracted_value_classes():
    subject_id, source_id = uuid4(), uuid4()
    extracted_values = (
        "CVE-2026-1234",
        "192.0.2.7",
        "2001:db8::1",
        "evil.example",
        "https://evil.example/path",
        "a" * 64,
        "analyst@evil.example",
        "T1059.001",
    )
    text = "The report lists " + ", ".join(extracted_values) + "."
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, " ".join(extracted_values)),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    lead, _ = validate_synthesis_proposal(make_proposal(text, handle), pack, extraction)

    assert lead[0].text == text


def test_proposal_requires_technical_literal_to_be_covered_by_claim_refs():
    subject_id, source_id = uuid4(), uuid4()
    source = make_source(
        source_id,
        facts=(make_fact(source_id, "FooRAT"),),
        indicators=(make_indicator(source_id, "evil.example"),),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    fact_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.FACT.value
    )
    proposal = make_proposal(
        "The domain evil.example supports the operation.",
        fact_handle,
        section_kind="technical",
        heading="Technical findings",
    )

    assert_proposal_error(
        lambda: validate_synthesis_proposal(proposal, pack, extraction),
        "synthesis_unknown_technical_value",
    )


def test_proposal_rejects_technical_only_evidence_for_campaign_narrative():
    subject_id, source_id = uuid4(), uuid4()
    source = make_source(
        source_id,
        tier=ProductionReferenceTier.TECHNICAL,
        indicators=(make_indicator(source_id, "evil.example"),),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.technical_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("The campaign used evil.example.", handle, section_kind="campaign"),
            pack,
            extraction,
        ),
        "synthesis_output_invalid",
    )


def test_proposal_accepts_event_and_fact_supported_dates_and_rejects_unsupported_dates():
    subject_id, source_id = uuid4(), uuid4()
    event = make_event(source_id, "The campaign began.", date(2026, 7, 2))
    textual_date_event = make_event(
        source_id, "Another operation was reported.", None, date_text="4 July 2026"
    )
    dated_fact = make_fact(source_id, "FooRAT was reported on 2 July 2026")
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(dated_fact,), events=(event, textual_date_event)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    event_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.EVENT.value and record["event_date"] == "2026-07-02"
    )
    fact_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.FACT.value
    )
    textual_date_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.EVENT.value and record["date_text"] == "4 July 2026"
    )
    event_result = validate_synthesis_proposal(
        make_proposal("The campaign began on 2026-07-02.", event_handle), pack, extraction
    )
    fact_result = validate_synthesis_proposal(
        make_proposal("FooRAT was reported on 2 July 2026.", fact_handle), pack, extraction
    )
    textual_date_result = validate_synthesis_proposal(
        make_proposal("Another operation was reported on 4 July 2026.", textual_date_handle),
        pack,
        extraction,
    )
    assert event_result[0][0].evidence_refs == (pack.resolve_handle(event_handle),)
    assert fact_result[0][0].evidence_refs == (pack.resolve_handle(fact_handle),)
    assert textual_date_result[0][0].evidence_refs == (pack.resolve_handle(textual_date_handle),)
    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("The campaign began on 2026-07-03.", event_handle), pack, extraction
        ),
        "synthesis_unknown_date",
    )


def test_proposal_rejects_removed_revision_evidence():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])
    removed_ref = pack.resolve_handle(handle)

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT was identified.", handle),
            pack,
            extraction,
            removed_evidence=(removed_ref,),
        ),
        "synthesis_unknown_evidence",
    )


def test_proposal_schema_objects_are_strict_and_validate_section_kinds():
    claim = SynthesisClaimProposalV1(text="FooRAT was reported.", evidence_handles=("E001",))
    section = SynthesisSectionProposalV1(
        kind=SynthesisSectionKind.OVERVIEW, heading="Overview", claims=(claim,)
    )
    assert section.claims == (claim,)
    with pytest.raises(ValueError):
        SynthesisClaimProposalV1(text="Claim", evidence_handles=())
    with pytest.raises(ValueError):
        SynthesisSectionProposalV1(kind="unknown", heading="Heading", claims=(claim,))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_access_policy_folds_subject_and_exact_source_metadata_conservatively():
    subject_id = uuid4()
    snapshot = make_snapshot(subject_id)
    source_ids = (uuid4(), uuid4(), uuid4())
    extraction = make_extraction(
        subject_id,
        tuple(
            make_source(source_id, url_suffix=f"source-{index}")
            for index, source_id in enumerate(source_ids)
        ),
    )
    docs = (
        make_document(subject_id, source_ids[0], tlp=TLP.GREEN),
        make_document(
            subject_id,
            source_ids[1],
            tlp=TLP.RED,
            external_llm_allowed=False,
            do_not_submit=True,
        ),
        make_document(subject_id, source_ids[2], tlp=TLP.AMBER),
    )
    repo = MemorySourceDocuments(tuple(reversed(docs)))

    policy = await build_synthesis_access_policy(snapshot, extraction, repo)
    repeated = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments(docs)
    )

    assert policy.effective_tlp is TLP.RED
    assert policy.external_llm_allowed is False
    assert policy.do_not_submit is True
    assert tuple(item.source_document_id for item in policy.sources) == tuple(
        sorted(source_ids, key=str)
    )
    assert repo.requested == sorted(source_ids, key=str)
    assert synthesis_access_policy_hash(policy) == synthesis_access_policy_hash(repeated)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "wrong_subject", "missing_field"])
async def test_access_policy_fails_closed_for_unavailable_or_invalid_exact_document(failure: str):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(subject_id, (make_source(source_id),))
    if failure == "missing":
        document = None
    elif failure == "wrong_subject":
        document = make_document(uuid4(), source_id)
    else:
        document = SimpleNamespace(
            id=source_id,
            subject_id=subject_id,
            tlp=TLP.CLEAR,
            external_llm_allowed=True,
        )

    class Repository:
        async def get(self, requested_id: UUID) -> object | None:
            assert requested_id == source_id
            return document

    with pytest.raises(ValueError, match="synthesis_access_policy_unavailable"):
        await build_synthesis_access_policy(snapshot, extraction, Repository())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_synthesis_hash_request_and_model_identity_are_functional_and_stateless():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    evidence_pack = build_synthesis_evidence_pack(snapshot, extraction)
    document = make_document(
        subject_id,
        source_id,
        tlp=TLP.AMBER,
        external_llm_allowed=True,
    )
    policy = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments((document,))
    )
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=subject_id,
        edition_id=snapshot.edition_id,
        pipeline_generation=4,
    )
    request = build_synthesis_model_request(
        run, snapshot, extraction, evidence_pack, policy, SynthesisMode.FRESH
    )

    assert request.web_search is False
    assert request.conversation is None
    assert request.background is False
    assert request.external_llm_allowed is True
    assert request.sensitivity == TLP.AMBER.value
    assert request.routing_hint is ModelRoutingHint.PREMIUM_SYNTHESIS
    assert request.evidence_pack_hash == synthesis_evidence_pack_hash(evidence_pack)
    assert all(str(source_id) not in request.text for source_id in (source_id, document.blob_id))
    assert all(record["handle"] in request.text for record in evidence_pack.narrative_evidence)
    assert "@@EVIDENCE E001@@" in request.text
    assert "@@CLAIM L001@@" in request.text
    assert "Do not return JSON" in request.text
    assert request.run_id == synthesis_model_run_id(
        run,
        synthesis_invocation_hash(
            snapshot,
            extraction,
            evidence_pack,
            synthesis_access_policy_hash(policy),
        ),
        SynthesisMode.FRESH,
    )
    assert request.run_id == synthesis_model_run_id(
        run, request.metadata["synthesis_invocation_hash"], SynthesisMode.FRESH
    )
    assert request.metadata["synthesis_input_hash"] != request.metadata["synthesis_invocation_hash"]
    changed_generation = replace(run, pipeline_generation=5)
    assert request.run_id != synthesis_model_run_id(
        changed_generation, request.metadata["synthesis_invocation_hash"], SynthesisMode.FRESH
    )


@pytest.mark.asyncio
async def test_synthesis_input_hash_changes_with_policy_but_ignores_run_ids_and_timestamps(
    monkeypatch: pytest.MonkeyPatch,
):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    permissive = await build_synthesis_access_policy(
        snapshot,
        extraction,
        MemorySourceDocuments((make_document(subject_id, source_id),)),
    )
    restricted = await build_synthesis_access_policy(
        snapshot,
        extraction,
        MemorySourceDocuments((make_document(subject_id, source_id, external_llm_allowed=False),)),
    )
    original_hash = synthesis_input_hash(
        snapshot, extraction, pack, synthesis_access_policy_hash(permissive)
    )
    changed_hash = synthesis_input_hash(
        snapshot, extraction, pack, synthesis_access_policy_hash(restricted)
    )
    other_run_snapshot = replace(
        snapshot,
        production_run_id=uuid4(),
        id=uuid4(),
        captured_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    other_run_pack = build_synthesis_evidence_pack(other_run_snapshot, extraction)

    assert original_hash != changed_hash
    assert original_hash == synthesis_input_hash(
        other_run_snapshot,
        extraction,
        other_run_pack,
        synthesis_access_policy_hash(permissive),
    )
    with monkeypatch.context() as changed_policy:
        changed_policy.setattr(
            "cti_app.application.production_synthesis.SYNTHESIS_TIMELINE_POLICY_VERSION",
            "synthesis-timeline-v5-test-policy",
        )
        assert original_hash != synthesis_input_hash(
            snapshot, extraction, pack, synthesis_access_policy_hash(permissive)
        )


@pytest.mark.asyncio
async def test_model_gateway_receives_synthesis_as_plain_text_without_schema():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id,
        (
            make_source(
                source_id,
                facts=(make_fact(source_id, "FooRAT"),),
                uncertainties=("Scope remains unknown.", "Attribution remains uncertain."),
            ),
        ),
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    policy = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments((make_document(subject_id, source_id),))
    )
    run = ProductionRun(
        id=snapshot.production_run_id, subject_id=subject_id, edition_id=snapshot.edition_id
    )
    request = build_synthesis_model_request(
        run, snapshot, extraction, pack, policy, SynthesisMode.FRESH
    )

    class Gateway:
        request_seen: object | None = None
        schema_seen: object | None = None

        async def draft(self, model_request: object, output_schema: object | None = None) -> str:
            self.request_seen = model_request
            self.schema_seen = output_schema
            return "drafted"

    gateway = Gateway()
    result = await draft_synthesis_proposal(gateway, request)  # type: ignore[arg-type]
    assert result == "drafted"
    assert gateway.request_seen is request
    assert gateway.schema_seen is None
    assert "Do not return JSON" in request.text
    assert "dense, coherent CTI prose" in request.text
    for marker in (
        "campaign, victimology",
        "infection/execution chain",
        "persistence",
        "C2 protocol",
        "concrete observable",
        "confidence level",
        "HANDLE_A, HANDLE_B",
        "@@DIAGNOSTICS@@",
        "PROJECTED / RANKED ANALYTICAL UNCERTAINTIES",
        "Attribution remains uncertain.",
        "Scope remains unknown.",
    ):
        assert marker.casefold() in request.text.casefold()
    assert request.text.index("Attribution remains uncertain.") < request.text.index(
        "Scope remains unknown."
    )
    assert "HEADING: a short" not in request.text
    assert "Return JSON" not in request.text


# --- canonical synthesis application service --------------------------------


EXTRACTION_ARTIFACT_HASH = "d" * 64


class _MemoryArtifactStore:
    """Only canonical JSON is readable; reading a source body fails the test."""

    def __init__(self, payloads: dict[UUID, dict[str, object]]) -> None:
        self.payloads = dict(payloads)
        self.json_reads: list[UUID] = []
        self.body_reads: list[UUID] = []

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        self.json_reads.append(blob_id)
        return self.payloads[blob_id]

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        self.body_reads.append(blob_id)
        raise AssertionError("Synthesis must never read an archived source body")


class _Uow:
    def __init__(
        self,
        documents: MemorySourceDocuments,
        artifacts: _MemoryArtifacts | None = None,
        invalidations: _MemoryInvalidations | None = None,
        collections: tuple[SourceCollection, ...] = (),
    ) -> None:
        self.source_documents = documents
        self.source_collections = MemorySourceCollections(collections)
        self.production_artifacts = artifacts or _MemoryArtifacts()
        self.production_reuse_invalidations = invalidations or _MemoryInvalidations()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _MemoryArtifacts:
    """Minimal artifact repository exposing only the reuse/revision reads."""

    def __init__(self, artifacts: tuple[ProductionArtifact, ...] = ()) -> None:
        self.artifacts = list(artifacts)

    async def list_current_for_edition(
        self, edition_id: UUID, stage: str
    ) -> list[ProductionArtifact]:
        del edition_id
        return [
            artifact
            for artifact in self.artifacts
            if artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [artifact for artifact in self.artifacts if artifact.production_run_id == run_id]


class _MemoryInvalidations:
    def __init__(self, items: tuple[object, ...] = ()) -> None:
        self.items = list(items)

    async def list_for_subject(self, edition_id: UUID, subject_id: UUID) -> list[object]:
        del edition_id, subject_id
        return list(self.items)


class _RecordingGateway:
    """A small archived-output ModelGateway double; provider submissions are recorded."""

    def __init__(self, responder: Callable[[ModelRequest], ModelExecution] | Exception) -> None:
        self._responder = responder
        self.calls: list[tuple[ModelRequest, object | None]] = []
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}
        self.diagnostics: list[dict[str, object]] = []
        self._normalized_count = 0

    async def draft(
        self, request: ModelRequest, output_schema: object | None = None
    ) -> ModelExecution:
        self.calls.append((request, output_schema))
        if isinstance(self._responder, Exception):
            raise self._responder
        execution = self._responder(request)
        if execution.run.raw_output_reference and execution.output_text is not None:
            self.outputs[execution.run.raw_output_reference] = execution.output_text.encode("utf-8")
        self.runs[execution.run.id] = execution.run
        return execution

    async def get_run(self, run_id: UUID) -> ModelRun | None:
        return self.runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        content = self.outputs[reference]
        if len(content) > max_bytes:
            raise ValueError("output too large")
        return content

    async def archive_output(self, content: bytes, *, mime_type: str) -> str:
        del mime_type
        self._normalized_count += 1
        reference = f"model-normalized://{self._normalized_count}"
        self.outputs[reference] = content
        return reference

    async def record_output_diagnostics(self, run_id: UUID, **values: object) -> None:
        self.diagnostics.append({"run_id": run_id, **values})
        run = self.runs[run_id]
        run.normalized_output_reference = values["normalized_reference"]  # type: ignore[assignment]
        run.normalized_output_sha256 = values["normalized_sha256"]  # type: ignore[assignment]
        run.parser_stage = values["parser_stage"]  # type: ignore[assignment]
        run.normalization_version = values["normalization_version"]  # type: ignore[assignment]
        run.transformations = values["transformations"]  # type: ignore[assignment]
        run.validation_errors = values["validation_errors"]  # type: ignore[assignment]


class _RecordingSynthesisWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def store_synthesis_result(self, **kwargs: object) -> ProductionArtifact:
        self.calls.append(kwargs)
        return ProductionArtifact(
            production_run_id=kwargs["run_id"],  # type: ignore[arg-type]
            subject_id=kwargs["subject_id"],  # type: ignore[arg-type]
            stage=ProductionArtifactStage.SYNTHESIS,
            version=len(self.calls),
            input_hash=kwargs["input_hash"],  # type: ignore[arg-type]
            canonical_blob_id=uuid4(),
            model_run_id=kwargs["model_run_id"],  # type: ignore[arg-type]
        )


class _ReuseStub:
    def __init__(self, result: ProductionArtifactReuseResult | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def find_or_reuse(self, **kwargs: object) -> ProductionArtifactReuseResult | None:
        self.calls.append(kwargs)
        return self.result


class _PreSubmissionFailure(ModelGatewayError):
    """A proven pre-submission failure: the caller retries the same ModelRun."""

    code = "bridge_unreachable"
    retryable = True


def _proposal_wire(proposal: SynthesisProposalV1) -> str:
    lines = ["@@LEAD@@"]
    for index, claim in enumerate(proposal.lead, start=1):
        lines.extend(
            (
                f"@@CLAIM L{index:03d}@@",
                f"EVIDENCE: {', '.join(claim.evidence_handles)}",
                f"TEXT: {claim.text}",
            )
        )
    for section_index, section in enumerate(proposal.sections, start=1):
        lines.extend(
            (
                f"@@SECTION {section.kind.value} S{section_index:03d}@@",
                f"HEADING: {section.heading}",
            )
        )
        for claim_index, claim in enumerate(section.claims, start=1):
            lines.extend(
                (
                    f"@@CLAIM S{section_index:03d}C{claim_index:03d}@@",
                    f"EVIDENCE: {', '.join(claim.evidence_handles)}",
                    f"TEXT: {claim.text}",
                )
            )
        lines.append("@@END SECTION@@")
    return "\n".join(lines)


def _succeeded(
    request: ModelRequest, proposal: BaseModel | None, *, text: str | None = None
) -> ModelExecution:
    if text is None:
        text = (
            _proposal_wire(proposal)
            if isinstance(proposal, SynthesisProposalV1)
            else "This response has no synthesis blocks."
        )
    run = ModelRun(
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.DRAFTING,
        requested_model="gpt-5",
        prompt_template_id=request.prompt_template_id,
        prompt_template_version=request.prompt_template_version,
        authorized_input_hash=hashlib.sha256(request.text.encode()).hexdigest(),
        evidence_pack_hash=request.evidence_pack_hash,
        parameters=dict(request.parameters),
        # The durable ModelRun identity is the one the request already carries.
        id=request.run_id or uuid4(),
    )
    raw_bytes = text.encode("utf-8")
    raw_reference = f"model-output://{run.id}"
    run.raw_output_reference = raw_reference
    run.raw_output_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    run.raw_output_chars = len(text)
    run.succeed(
        actual_model_version="gpt-5",
        duration_ms=3,
        usage=ModelUsage(total_tokens=7),
        output_references=(raw_reference,),
        response_id=None,
    )
    return ModelExecution(run=run, output_text=text, structured_output=proposal)


def _matched_extraction(
    snapshot: ProductionInputSnapshot, *sources: ProductionSourceExtractionV1
) -> ProductionExtractionV1:
    return replace(
        make_extraction(snapshot.subject_id, tuple(sources)),
        production_input_hash=snapshot.input_hash,
    )


def _service_world(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    documents: tuple[SourceDocument, ...],
    gateway: _RecordingGateway,
    *,
    reuse: _ReuseStub | None = None,
    extraction_blob_id: UUID | None = None,
    extra_payloads: Mapping[UUID, dict[str, object]] | None = None,
    artifacts: tuple[ProductionArtifact, ...] = (),
    collections: tuple[SourceCollection, ...] = (),
) -> SimpleNamespace:
    blob_id = extraction_blob_id or uuid4()
    payloads = dict(extra_payloads or {})
    payloads[blob_id] = production_extraction_to_json(extraction)
    store = _MemoryArtifactStore(payloads)
    writer = _RecordingSynthesisWriter()
    document_repository = MemorySourceDocuments(documents)
    artifact_repository = _MemoryArtifacts(artifacts)
    service = ProductionSynthesisService(
        uow_factory=lambda: _Uow(  # type: ignore[arg-type]
            document_repository, artifact_repository, collections=collections
        ),
        artifact_store=store,  # type: ignore[arg-type]
        model_gateway=gateway,  # type: ignore[arg-type]
        synthesis_service=writer,  # type: ignore[arg-type]
        artifact_reuse=reuse,  # type: ignore[arg-type]
    )
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        pipeline_generation=3,
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=snapshot.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=EXTRACTION_ARTIFACT_HASH,
        canonical_blob_id=blob_id,
    )
    return SimpleNamespace(
        service=service,
        store=store,
        writer=writer,
        docs=document_repository,
        artifacts=artifact_repository,
        run=run,
        artifact=artifact,
    )


def _fresh_setup() -> SimpleNamespace:
    """One CORE source, one event and one fact, already extracted canonically."""
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    source = make_source(
        source_id,
        facts=(make_fact(source_id, "FooRAT"),),
        events=(make_event(source_id, "The campaign began.", date(2026, 7, 2)),),
        uncertainties=("Attribution remains uncertain.",),
    )
    extraction = _matched_extraction(snapshot, source)
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    handle = str(pack.narrative_evidence[0]["handle"])
    proposal = SynthesisProposalV1.model_validate(
        make_proposal("FooRAT was identified in the report.", handle)
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, proposal))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)
    world.snapshot = snapshot
    world.extraction = extraction
    world.source_id = source_id
    world.pack = pack
    world.handle = handle
    world.proposal = proposal
    world.gateway = gateway
    return world


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing_blob", "unreadable_blob", "invalid_payload", "wrong_stage", "not_verified"],
)
async def test_service_requires_readable_verified_canonical_extraction(failure: str):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    extraction = _matched_extraction(snapshot, make_source(source_id))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)
    artifact = world.artifact
    if failure == "missing_blob":
        artifact = replace(artifact, canonical_blob_id=None)
    elif failure == "unreadable_blob":
        artifact = replace(artifact, canonical_blob_id=uuid4())
    elif failure == "invalid_payload":
        world.store.payloads[artifact.canonical_blob_id] = {"schema_version": 1}
    elif failure == "wrong_stage":
        artifact = replace(artifact, stage=ProductionArtifactStage.REFERENCES)
    else:
        artifact = replace(artifact, status=ProductionArtifactStatus.NEEDS_REVIEW)

    result = await world.service.execute(world.run, snapshot, artifact)

    assert result.status is SynthesisExecutionStatus.BLOCKED
    assert result.error_code == SynthesisStageErrorCode.INPUTS_MISSING.value
    assert result.model_calls == 0
    assert gateway.calls == []
    assert world.writer.calls == []
    assert world.docs.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["extraction_subject", "extraction_input_hash", "artifact_run", "run_snapshot"],
)
async def test_service_stops_on_lineage_mismatch_before_drafting(failure: str):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    if failure == "extraction_subject":
        extraction = _matched_extraction(snapshot, make_source(source_id))
        extraction = replace(extraction, subject_id=uuid4())
    elif failure == "extraction_input_hash":
        extraction = replace(
            _matched_extraction(snapshot, make_source(source_id)),
            production_input_hash="a" * 64,
        )
    else:
        extraction = _matched_extraction(snapshot, make_source(source_id))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)
    run = world.run
    artifact = world.artifact
    if failure == "artifact_run":
        artifact = replace(artifact, production_run_id=uuid4())
    elif failure == "run_snapshot":
        run = replace(run, id=uuid4())

    result = await world.service.execute(run, snapshot, artifact)

    assert result.status is SynthesisExecutionStatus.BLOCKED
    assert result.error_code == SynthesisStageErrorCode.INPUTS_MISMATCH.value
    assert result.model_calls == 0
    assert gateway.calls == []
    assert world.writer.calls == []
    assert world.docs.requested == []


@pytest.mark.asyncio
async def test_fresh_synthesis_submits_once_and_never_reads_source_bodies():
    world = _fresh_setup()

    result = await world.service.execute(world.run, world.snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.mode is SynthesisMode.FRESH
    assert result.model_calls == 1
    assert result.error_code is None
    assert result.extraction_hash == canonical_extraction_hash(world.extraction)
    assert len(world.gateway.calls) == 1
    request, schema = world.gateway.calls[0]
    assert schema is None
    assert request.web_search is False
    assert request.conversation is None
    assert request.allow_failed_resubmit is True
    assert request.run_id == result.model_run_id
    assert result.input_hash == request.metadata["synthesis_input_hash"]

    # Only the canonical extraction JSON is read; no archived source body is.
    assert world.store.body_reads == []
    assert world.store.json_reads == [world.artifact.canonical_blob_id]
    assert world.docs.requested == [world.source_id]
    assert str(world.source_id) not in request.text
    assert "source.pdf" not in request.text

    assert len(world.writer.calls) == 1
    stored = world.writer.calls[0]
    synthesis = stored["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    assert stored["run_id"] == world.run.id
    assert stored["subject_id"] == world.snapshot.subject_id
    assert stored["input_hash"] == result.input_hash
    # The canonical extraction is the deserialized artifact blob, never a raw input.
    assert stored["extraction"] == world.extraction
    assert result.model_run_id is not None
    stored_run = world.gateway.runs[result.model_run_id]
    assert stored_run.raw_output_reference is not None
    archived = world.gateway.outputs[stored_run.raw_output_reference]
    assert stored["raw_result"] == archived.decode("utf-8")
    assert stored["model_run_id"] == result.model_run_id
    assert stored["mode"] is SynthesisMode.FRESH
    assert stored["model_policy_version"] == SYNTHESIS_MODEL_POLICY_VERSION
    assert stored["routing_policy_version"] == SYNTHESIS_ROUTING_POLICY_VERSION

    # Title, timeline and uncertainties are deterministic, never model output.
    assert synthesis.title == world.snapshot.subject_title
    assert synthesis.publication_language == world.snapshot.publication_language == "fr"
    assert synthesis.production_input_hash == world.snapshot.input_hash
    assert synthesis.extraction_hash == result.extraction_hash
    assert [entry.event_date for entry in synthesis.timeline] == [date(2026, 7, 2)]
    assert [item.text for item in synthesis.uncertainties] == ["Attribution remains uncertain."]
    assert synthesis.lead[0].text == "FooRAT was identified in the report."
    assert synthesis.lead[0].evidence_refs == (world.pack.resolve_handle(world.handle),)
    assert [section.kind for section in synthesis.sections] == [SynthesisSectionKind.OVERVIEW]

    # The stage result stays bounded: counters only, never the narrative body.
    assert result.details["section_count"] == 1
    assert result.details["timeline_entry_count"] == 1
    assert "FooRAT was identified in the report." not in str(result.details)
    assert result.artifact_id is not None


@pytest.mark.asyncio
async def test_do_not_submit_source_policy_stops_before_drafting():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (
            make_document(
                subject_id,
                source_id,
                tlp=TLP.RED,
                external_llm_allowed=False,
                do_not_submit=True,
            ),
        ),
        gateway,
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.POLICY_BLOCKED.value
    assert result.model_calls == 0
    assert result.extraction_hash == canonical_extraction_hash(extraction)
    assert result.details["do_not_submit"] is True
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_external_model_block_from_gateway_is_needs_review():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    gateway = _RecordingGateway(ExternalModelBlockedError("external route blocked"))
    world = _service_world(
        snapshot,
        extraction,
        (make_document(subject_id, source_id),),
        gateway,
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.POLICY_BLOCKED.value
    assert result.model_calls == 1
    assert len(gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_collection_access_policy_blocks_synthesis_before_gateway() -> None:
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=snapshot.edition_id,
        requested_url="https://restricted.example/report",
        proposed_role=SourceRole.PRIMARY,
        external_llm_allowed=False,
    )
    document = replace(make_document(subject_id, source_id), source_collection_id=collection.id)
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (document,),
        gateway,
        collections=(collection,),
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.POLICY_BLOCKED.value
    assert result.model_calls == 0
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_unintelligible_synthesis_text_needs_review_without_empty_success():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    raw = "This response has no recognizable synthesis blocks."
    gateway = _RecordingGateway(lambda request: _succeeded(request, None, text=raw))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisProposalErrorCode.OUTPUT_INVALID.value
    assert result.details["parse_error"] == "synthesis_unintelligible_response"
    assert result.model_calls == 1
    assert len(gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_dirty_synthesis_wire_keeps_valid_blocks_and_archives_raw_response():
    world = _fresh_setup()
    raw = f"""```text
@@LEAD@@
@@CLAIM L001@@
EVIDENCE: {world.handle}
TEXT: Le rapport nomme "FooRAT" :chatgpt-content-reference{{index="0"}} dans cette activité.
@@CLAIM BROKEN@@
EVIDENCE: {world.handle}
@@CLAIM L002@@
Evidence {world.handle}
Text: L'analyse cite aussi "FooRAT" comme élément du dossier.
@@SECTION overview S001@@
HEADING: Constat principal
@@CLAIM C001@@
EVIDENCE: {world.handle}
TEXT: Le texte associe "FooRAT" à cette campagne.
@@END SECTION@@
```"""
    parsed = parse_synthesis_proposal_wire(raw)

    assert parsed.proposal is not None
    assert len(parsed.proposal.lead) == 2
    assert len(parsed.proposal.sections) == 1
    assert [(item.block_id, item.reason_code) for item in parsed.rejections] == [
        ("BROKEN", "synthesis_claim_missing_text")
    ]
    assert [(item.block_id, item.warning_code) for item in parsed.warnings] == [
        ("S001", "synthesis_section_heading_dropped")
    ]
    assert parsed.transformations == ("bridge_ui_markers_removed",)

    world.gateway._responder = lambda request: _succeeded(request, None, text=raw)
    result = await world.service.execute(world.run, world.snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.model_calls == 1
    assert result.details["parse_rejections"] == [
        {"block_id": "BROKEN", "reason_code": "synthesis_claim_missing_text"}
    ]
    assert result.model_run_id is not None
    model_run = world.gateway.runs[result.model_run_id]
    assert model_run.raw_output_reference is not None
    assert world.gateway.outputs[model_run.raw_output_reference] == raw.encode("utf-8")
    stored = world.writer.calls[0]
    assert stored["raw_result"] == raw
    synthesis = stored["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    assert [paragraph.text for paragraph in synthesis.lead] == [
        'Le rapport nomme "FooRAT"  dans cette activité.',
        'L\'analyse cite aussi "FooRAT" comme élément du dossier.',
    ]
    assert len(synthesis.sections) == 1
    assert synthesis.sections[0].heading == ""
    assert synthesis.sections[0].paragraphs[0].evidence_refs == (
        world.pack.resolve_handle(world.handle),
    )
    assert ":chatgpt-content-reference" not in str(production_synthesis_to_json(synthesis))
    assert model_run.normalized_output_reference is not None
    normalized = SynthesisProposalV1.model_validate_json(
        world.gateway.outputs[model_run.normalized_output_reference]
    )
    assert len(normalized.lead) == 2
    assert normalized.lead[0].evidence_handles == (world.handle,)


def test_wire_parser_accepts_headingless_sections_and_drops_present_heading_with_warning():
    world = _fresh_setup()

    def wire(heading: str) -> str:
        optional_heading = f"HEADING: {heading}\n" if heading else ""
        return (
            "@@LEAD@@\n"
            "@@CLAIM L001@@\n"
            f"EVIDENCE: {world.handle}\n"
            "TEXT: FooRAT was identified in the report.\n"
            "@@SECTION technical S001@@\n"
            f"{optional_heading}"
            "@@CLAIM C001@@\n"
            f"EVIDENCE: {world.handle}\n"
            "TEXT: FooRAT was identified in the technical section.\n"
            "@@END SECTION@@"
        )

    headingless = parse_synthesis_proposal_wire(wire(""))
    headed = parse_synthesis_proposal_wire(wire("Internal heading"))

    assert headingless.proposal is not None
    assert headingless.proposal.sections[0].heading == ""
    assert headingless.warnings == ()
    assert headed.proposal is not None
    assert headed.proposal.sections[0].heading == ""
    assert [(item.block_id, item.warning_code) for item in headed.warnings] == [
        ("S001", "synthesis_section_heading_dropped")
    ]


@pytest.mark.asyncio
async def test_synthesis_diagnostics_are_persisted_separately_from_body():
    world = _fresh_setup()
    raw = f"""@@LEAD@@
@@CLAIM L001@@
EVIDENCE: {world.handle}
TEXT: FooRAT was identified in the report.
@@SECTION overview S001@@
HEADING: Drafted title that must be dropped
@@CLAIM BROKEN@@
EVIDENCE: {world.handle}
@@CLAIM C001@@
EVIDENCE: {world.handle}
TEXT: FooRAT was also examined technically.
@@END SECTION@@
@@DIAGNOSTICS@@
MISSING COVERAGE: the delivery chain is absent from the supplied extraction
@@END DIAGNOSTICS@@"""
    world.gateway._responder = lambda request: _succeeded(request, None, text=raw)

    result = await world.service.execute(world.run, world.snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.details["diagnostics"]["missing_coverage"] == [
        "the delivery chain is absent from the supplied extraction"
    ]
    assert result.details["diagnostics"]["rejected_blocks"] == [
        {"block_id": "BROKEN", "reason_code": "synthesis_claim_missing_text"}
    ]
    assert result.details["diagnostics"]["parse_warnings"] == [
        {"block_id": "S001", "warning_code": "synthesis_section_heading_dropped"}
    ]
    synthesis = world.writer.calls[0]["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    body = render_synthesis_markdown(synthesis, world.extraction)
    assert "Drafted title that must be dropped" not in body
    assert "delivery chain is absent" not in body
    assert "synthesis_section_heading_dropped" not in body
    assert "Attribution remains uncertain." not in body
    assert world.writer.calls[0]["diagnostics"] == result.details["diagnostics"]


@pytest.mark.asyncio
async def test_explicit_empty_synthesis_marker_is_a_valid_empty_proposal():
    world = _fresh_setup()
    raw = "```text\n@@EMPTY@@\n```"
    world.gateway._responder = lambda request: _succeeded(request, None, text=raw)

    result = await world.service.execute(world.run, world.snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.error_code is None
    assert result.model_calls == 1
    synthesis = world.writer.calls[0]["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    assert synthesis.lead == ()
    assert synthesis.sections == ()
    assert result.details["paragraph_count"] == 0


@pytest.mark.asyncio
async def test_parser_version_change_reparses_verified_raw_without_draft_call(monkeypatch):
    first_world = _fresh_setup()
    first = await first_world.service.execute(
        first_world.run, first_world.snapshot, first_world.artifact
    )
    first_synthesis = first_world.writer.calls[0]["synthesis"]
    assert isinstance(first_synthesis, ProductionSynthesisV1)

    monkeypatch.setattr(
        synthesis_module, "SYNTHESIS_WIRE_PARSER_VERSION", "synthesis-text-parser-v2-test"
    )
    resumed_world = _service_world(
        first_world.snapshot,
        first_world.extraction,
        (make_document(first_world.snapshot.subject_id, first_world.source_id),),
        first_world.gateway,
    )
    old_synthesis_blob = uuid4()
    resumed_world.store.payloads[old_synthesis_blob] = production_synthesis_to_json(first_synthesis)
    resumed_world.artifacts.artifacts.extend(
        (
            ProductionArtifact(
                production_run_id=resumed_world.run.id,
                subject_id=resumed_world.run.subject_id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=1,
                input_hash=first.input_hash,
                canonical_blob_id=old_synthesis_blob,
                model_run_id=first.model_run_id,
            ),
            ProductionArtifact(
                production_run_id=resumed_world.run.id,
                subject_id=resumed_world.run.subject_id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=EXTRACTION_ARTIFACT_HASH,
                canonical_blob_id=resumed_world.artifact.canonical_blob_id,
            ),
        )
    )
    resumed = await resumed_world.service.execute(
        resumed_world.run, first_world.snapshot, resumed_world.artifact
    )

    assert first.status is SynthesisExecutionStatus.SUCCEEDED
    assert resumed.status is SynthesisExecutionStatus.SUCCEEDED
    assert resumed.model_calls == 0
    assert first.input_hash != resumed.input_hash
    assert first.model_run_id == resumed.model_run_id
    assert len(first_world.gateway.calls) == 1
    assert resumed.details["parse_identity"] != first.details["parse_identity"]
    resumed_synthesis = resumed_world.writer.calls[0]["synthesis"]
    assert resumed_synthesis == first_synthesis


@pytest.mark.asyncio
async def test_prompt_version_change_requires_a_new_model_call(monkeypatch):
    first_world = _fresh_setup()
    first = await first_world.service.execute(
        first_world.run, first_world.snapshot, first_world.artifact
    )

    monkeypatch.setattr(synthesis_module, "SYNTHESIS_PROMPT_VERSION", "synthesis-draft-v3-test")
    next_world = _service_world(
        first_world.snapshot,
        first_world.extraction,
        (make_document(first_world.snapshot.subject_id, first_world.source_id),),
        first_world.gateway,
    )
    next_result = await next_world.service.execute(
        next_world.run, first_world.snapshot, next_world.artifact
    )

    assert first.status is SynthesisExecutionStatus.SUCCEEDED
    assert next_result.status is SynthesisExecutionStatus.SUCCEEDED
    assert next_result.model_calls == 1
    assert next_result.input_hash != first.input_hash
    assert next_result.model_run_id != first.model_run_id
    assert len(first_world.gateway.calls) == 2
    assert first_world.gateway.calls[1][0].prompt_template_version == "synthesis-draft-v3-test"


@pytest.mark.asyncio
async def test_contract_version_change_requires_a_new_model_call(monkeypatch):
    first_world = _fresh_setup()
    first = await first_world.service.execute(
        first_world.run, first_world.snapshot, first_world.artifact
    )

    monkeypatch.setattr(
        synthesis_module,
        "SYNTHESIS_PROPOSAL_CONTRACT_VERSION",
        "synthesis-text-blocks-v2-test",
    )
    next_world = _service_world(
        first_world.snapshot,
        first_world.extraction,
        (make_document(first_world.snapshot.subject_id, first_world.source_id),),
        first_world.gateway,
    )
    next_result = await next_world.service.execute(
        next_world.run, first_world.snapshot, next_world.artifact
    )

    assert first.status is SynthesisExecutionStatus.SUCCEEDED
    assert next_result.status is SynthesisExecutionStatus.SUCCEEDED
    assert next_result.model_calls == 1
    assert next_result.input_hash != first.input_hash
    assert next_result.model_run_id != first.model_run_id
    assert len(first_world.gateway.calls) == 2
    assert "Contract version: synthesis-text-blocks-v2-test" in first_world.gateway.calls[1][0].text


@pytest.mark.asyncio
async def test_restart_resumes_same_archived_response_and_result():
    first_world = _fresh_setup()
    first = await first_world.service.execute(
        first_world.run, first_world.snapshot, first_world.artifact
    )
    first_synthesis = first_world.writer.calls[0]["synthesis"]
    assert isinstance(first_synthesis, ProductionSynthesisV1)
    resumed_world = _service_world(
        first_world.snapshot,
        first_world.extraction,
        (make_document(first_world.snapshot.subject_id, first_world.source_id),),
        first_world.gateway,
    )

    resumed = await resumed_world.service.execute(
        resumed_world.run, first_world.snapshot, resumed_world.artifact
    )

    assert resumed.status is SynthesisExecutionStatus.SUCCEEDED
    assert resumed.model_calls == 0
    assert resumed.input_hash == first.input_hash
    assert resumed.model_run_id == first.model_run_id
    assert resumed.details["parse_identity"] == first.details["parse_identity"]
    assert len(first_world.gateway.calls) == 1
    assert resumed_world.writer.calls[0]["raw_result"] == first_world.writer.calls[0]["raw_result"]
    assert resumed_world.writer.calls[0]["synthesis"] == first_synthesis


@pytest.mark.asyncio
async def test_unknown_evidence_handle_reaches_needs_review_without_repair_call():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    proposal = SynthesisProposalV1.model_validate(make_proposal("FooRAT was identified.", "E999"))
    gateway = _RecordingGateway(lambda request: _succeeded(request, proposal))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisProposalErrorCode.UNKNOWN_EVIDENCE.value
    assert result.error_code == "synthesis_unknown_evidence"
    assert result.model_calls == 1
    assert len(gateway.calls) == 1
    assert result.model_run_id == gateway.calls[0][0].run_id
    assert result.input_hash == gateway.calls[0][0].metadata["synthesis_input_hash"]
    assert result.extraction_hash == canonical_extraction_hash(extraction)
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_unavailable_source_access_policy_blocks_before_drafting():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    # No archived document exists for the exact extraction source.
    world = _service_world(snapshot, extraction, (), gateway)

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.BLOCKED
    assert result.error_code == SynthesisStageErrorCode.ACCESS_POLICY_UNAVAILABLE.value
    assert result.model_calls == 0
    assert world.docs.requested == [source_id]
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_retryable_pre_submission_failure_propagates_without_persisting():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    gateway = _RecordingGateway(_PreSubmissionFailure("the bridge was never reached"))
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)

    with pytest.raises(_PreSubmissionFailure):
        await world.service.execute(world.run, snapshot, world.artifact)

    assert len(gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["raised", "returned"])
async def test_ambiguous_submission_is_never_resubmitted(outcome: str):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    reconciliation_run_id = uuid4()
    reconciliation_details = {"bridge_reason": "active_signal_stalled"}
    if outcome == "raised":
        gateway: _RecordingGateway = _RecordingGateway(
            ModelSubmissionReconciliationRequiredError(
                details=reconciliation_details, model_run_id=reconciliation_run_id
            )
        )
    else:

        def responder(request: ModelRequest) -> ModelExecution:
            run = ModelRun(
                provider=ModelProvider.OPENAI,
                model_role=ModelRole.DRAFTING,
                requested_model="gpt-5",
                prompt_template_id=request.prompt_template_id,
                prompt_template_version=request.prompt_template_version,
                authorized_input_hash=hashlib.sha256(request.text.encode()).hexdigest(),
                evidence_pack_hash=request.evidence_pack_hash,
                parameters={},
                id=reconciliation_run_id,
            )
            run.require_review(
                PRODUCTION_RECONCILIATION_ERROR_CODE,
                "submission state is unknown",
                details=reconciliation_details,
            )
            return ModelExecution(run=run)

        gateway = _RecordingGateway(responder)
    world = _service_world(snapshot, extraction, (make_document(subject_id, source_id),), gateway)

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
    assert result.error_code == SynthesisStageErrorCode.RECONCILIATION_REQUIRED.value
    assert result.model_run_id == reconciliation_run_id
    assert result.model_calls == 1
    assert result.details["error_code"] == PRODUCTION_RECONCILIATION_ERROR_CODE
    assert result.details["model_run_id"] == str(reconciliation_run_id)
    assert result.details["bridge_reason"] == "active_signal_stalled"
    assert len(gateway.calls) == 1
    assert world.writer.calls == []


def _canonical_synthesis(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    *,
    title: str | None = None,
) -> ProductionSynthesisV1:
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    ref = pack.resolve_handle(str(pack.narrative_evidence[0]["handle"]))
    return ProductionSynthesisV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=title or snapshot.subject_title,
        lead=(SynthesisParagraphV1(text="FooRAT was identified.", evidence_refs=(ref,)),),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )


async def _expected_input_hash(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    documents: tuple[SourceDocument, ...],
) -> str:
    """The complete functional synthesis hash the exact-reuse probe must carry."""
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    policy = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments(documents)
    )
    return synthesis_input_hash(snapshot, extraction, pack, synthesis_access_policy_hash(policy))


def _pack_ref(evidence_pack: SynthesisEvidencePackV1, *, value: str) -> ExtractionEvidenceRefV1:
    """Resolve the canonical evidence ref of one projected evidence value."""
    records = (*evidence_pack.narrative_evidence, *evidence_pack.technical_evidence)
    for record in records:
        if record.get("value") == value:
            return evidence_pack.resolve_handle(str(record["handle"]))
    raise AssertionError(f"No projected evidence carries the value {value!r}")


def _pack_handle(evidence_pack: SynthesisEvidencePackV1, *, value: str) -> str:
    """The temporary prompt handle of one projected evidence value."""
    handle = evidence_pack.handle_for(_pack_ref(evidence_pack, value=value))
    assert handle is not None
    return handle


def _prior_synthesis(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    *,
    production_input_hash: str,
    paragraphs: tuple[SynthesisParagraphV1, ...],
) -> ProductionSynthesisV1:
    return ProductionSynthesisV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=paragraphs,
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )


def _prior_artifacts(
    *,
    subject_id: UUID,
    previous_run_id: UUID,
    synthesis_blob_id: UUID,
    extraction_blob_id: UUID,
    artifact_input_hash: str = "c" * 64,
    created_at: datetime | None = None,
) -> tuple[ProductionArtifact, ...]:
    """One prior run's canonical synthesis and its associated extraction."""
    kwargs = {} if created_at is None else {"created_at": created_at}
    return (
        ProductionArtifact(
            production_run_id=previous_run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=artifact_input_hash,
            canonical_blob_id=synthesis_blob_id,
            model_run_id=uuid4(),
            **kwargs,
        ),
        ProductionArtifact(
            production_run_id=previous_run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash="e" * 64,
            canonical_blob_id=extraction_blob_id,
            **kwargs,
        ),
    )


@pytest.mark.asyncio
async def test_exact_canonical_reuse_avoids_the_model_call():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    document = make_document(subject_id, source_id)
    expected_input_hash = await _expected_input_hash(snapshot, extraction, (document,))
    canonical_blob_id = uuid4()
    previous = _canonical_synthesis(snapshot, extraction)
    reuse_artifact = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=expected_input_hash,
        canonical_blob_id=canonical_blob_id,
        model_run_id=uuid4(),
        reused_from_artifact_id=uuid4(),
    )
    reuse = _ReuseStub(ProductionArtifactReuseResult(artifact=reuse_artifact, reused=True))
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (document,),
        gateway,
        reuse=reuse,
        extra_payloads={canonical_blob_id: production_synthesis_to_json(previous)},
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.REUSED
    assert result.mode is SynthesisMode.REUSE_EXACT
    assert result.model_calls == 0
    assert result.artifact_id == reuse_artifact.id
    assert result.model_run_id == reuse_artifact.model_run_id
    assert result.input_hash == expected_input_hash
    assert result.extraction_hash == canonical_extraction_hash(extraction)
    assert reuse.calls[0]["stage"] is ProductionArtifactStage.SYNTHESIS
    assert reuse.calls[0]["input_hash"] == result.input_hash
    assert result.details["reused"] is True
    assert result.details["reused_from_artifact_id"] == str(reuse_artifact.reused_from_artifact_id)
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_reuse_candidate_with_wrong_lineage_needs_review_without_drafting():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    document = make_document(subject_id, source_id)
    expected_input_hash = await _expected_input_hash(snapshot, extraction, (document,))
    canonical_blob_id = uuid4()
    stale = _canonical_synthesis(snapshot, extraction, title="An unfrozen editorial title")
    reuse_artifact = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=expected_input_hash,
        canonical_blob_id=canonical_blob_id,
        model_run_id=uuid4(),
    )
    reuse = _ReuseStub(ProductionArtifactReuseResult(artifact=reuse_artifact, reused=False))
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (document,),
        gateway,
        reuse=reuse,
        extra_payloads={canonical_blob_id: production_synthesis_to_json(stale)},
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.REUSE_INVALID.value
    assert result.model_calls == 0
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_same_run_exact_reuse_uses_the_current_artifact_without_cloning():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    document = make_document(subject_id, source_id)
    expected_input_hash = await _expected_input_hash(snapshot, extraction, (document,))
    canonical_blob_id = uuid4()
    previous = _canonical_synthesis(snapshot, extraction)
    current = ProductionArtifact(
        production_run_id=snapshot.production_run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=expected_input_hash,
        canonical_blob_id=canonical_blob_id,
        model_run_id=uuid4(),
    )
    reuse = _ReuseStub(ProductionArtifactReuseResult(artifact=current, reused=False))
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (document,),
        gateway,
        reuse=reuse,
        extra_payloads={canonical_blob_id: production_synthesis_to_json(previous)},
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.REUSED
    assert result.mode is SynthesisMode.REUSE_EXACT
    assert result.model_calls == 0
    assert result.artifact_id == current.id
    assert result.model_run_id == current.model_run_id
    assert result.input_hash == expected_input_hash
    assert result.details["reused"] is False
    assert result.details["reused_from_artifact_id"] is None
    assert reuse.calls[0]["input_hash"] == expected_input_hash
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_rendered_only_legacy_candidate_is_refused_as_canonical_reuse():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    document = make_document(subject_id, source_id)
    expected_input_hash = await _expected_input_hash(snapshot, extraction, (document,))
    rendered_blob_id = uuid4()
    legacy = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=expected_input_hash,
        rendered_blob_id=rendered_blob_id,
        model_run_id=uuid4(),
    )
    reuse = _ReuseStub(ProductionArtifactReuseResult(artifact=legacy, reused=False))
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(snapshot, extraction, (document,), gateway, reuse=reuse)

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.REUSE_INVALID.value
    assert result.model_calls == 0
    assert gateway.calls == []
    assert world.writer.calls == []
    assert rendered_blob_id not in world.store.json_reads


@pytest.mark.asyncio
async def test_revision_ignores_legacy_and_malformed_candidates_and_drafts_fresh():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    source = make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    extraction = _matched_extraction(snapshot, source)
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    handle = str(pack.narrative_evidence[0]["handle"])
    proposal = SynthesisProposalV1.model_validate(
        make_proposal("FooRAT was identified in the report.", handle)
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, proposal))
    previous_run_id = uuid4()
    legacy = ProductionArtifact(
        production_run_id=previous_run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash="c" * 64,
        rendered_blob_id=uuid4(),
        model_run_id=uuid4(),
    )
    malformed_blob_id = uuid4()
    malformed = ProductionArtifact(
        production_run_id=previous_run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="d" * 64,
        canonical_blob_id=malformed_blob_id,
        model_run_id=uuid4(),
    )
    world = _service_world(
        snapshot,
        extraction,
        (make_document(subject_id, source_id),),
        gateway,
        artifacts=(legacy, malformed),
        extra_payloads={malformed_blob_id: {"not": "a synthesis"}},
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.mode is SynthesisMode.FRESH
    assert result.model_calls == 1
    assert len(gateway.calls) == 1
    assert world.writer.calls[0]["mode"] is SynthesisMode.FRESH
    assert world.store.json_reads == [world.artifact.canonical_blob_id, malformed_blob_id]


@pytest.mark.asyncio
async def test_revision_sends_previous_document_and_exact_delta_without_leaking_ids():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    value_a, value_b, value_c, value_d = "FooRAT", "BarRAT", "CVE-2026-1234", "QuxLoader"
    previous_extraction = make_extraction(
        subject_id,
        (
            make_source(
                source_id,
                facts=(
                    make_fact(source_id, value_a),
                    make_fact(source_id, value_b),
                    make_fact(source_id, value_c),
                ),
            ),
        ),
    )
    current_extraction = _matched_extraction(
        snapshot,
        make_source(
            source_id,
            facts=(
                make_fact(source_id, value_a),
                make_fact(source_id, value_c),
                make_fact(source_id, value_d),
            ),
        ),
    )
    previous_pack = build_synthesis_evidence_pack(snapshot, previous_extraction)
    current_pack = build_synthesis_evidence_pack(snapshot, current_extraction)
    previous_synthesis = _prior_synthesis(
        snapshot,
        previous_extraction,
        production_input_hash="f" * 64,
        paragraphs=(
            SynthesisParagraphV1(
                text="FooRAT was observed.",
                evidence_refs=(
                    _pack_ref(previous_pack, value=value_a),
                    _pack_ref(previous_pack, value=value_c),
                ),
            ),
            SynthesisParagraphV1(
                text="BarRAT was also observed.",
                evidence_refs=(_pack_ref(previous_pack, value=value_b),),
            ),
        ),
    )
    synthesis_blob_id, extraction_blob_id = uuid4(), uuid4()
    previous_run_id = uuid4()
    reuse = _ReuseStub(None)
    gateway = _RecordingGateway(
        lambda request: _succeeded(
            request,
            SynthesisProposalV1.model_validate(
                make_proposal("FooRAT was observed.", _pack_handle(current_pack, value=value_a))
            ),
        )
    )
    world = _service_world(
        snapshot,
        current_extraction,
        (make_document(subject_id, source_id),),
        gateway,
        reuse=reuse,
        artifacts=_prior_artifacts(
            subject_id=subject_id,
            previous_run_id=previous_run_id,
            synthesis_blob_id=synthesis_blob_id,
            extraction_blob_id=extraction_blob_id,
        ),
        extra_payloads={
            synthesis_blob_id: production_synthesis_to_json(previous_synthesis),
            extraction_blob_id: production_extraction_to_json(previous_extraction),
        },
    )
    expected_input_hash = await _expected_input_hash(
        snapshot, current_extraction, (make_document(subject_id, source_id),)
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    assert result.mode is SynthesisMode.REVISE_PREVIOUS
    assert result.model_calls == 1
    assert result.details["previous_synthesis_artifact_id"] == str(world.artifacts.artifacts[0].id)
    assert result.details["added_evidence_count"] == 1
    assert result.details["removed_evidence_count"] == 1
    assert result.details["unchanged_evidence_count"] == 2
    assert reuse.calls[0]["stage"] is ProductionArtifactStage.SYNTHESIS
    assert reuse.calls[0]["input_hash"] == expected_input_hash

    request, _schema = gateway.calls[0]
    assert request.metadata["synthesis_mode"] == SynthesisMode.REVISE_PREVIOUS.value
    assert request.metadata["synthesis_input_hash"] == expected_input_hash
    assert request.run_id == synthesis_model_run_id(
        world.run,
        request.metadata["synthesis_invocation_hash"],
        SynthesisMode.REVISE_PREVIOUS,
    )
    assert "@@PREVIOUS SYNTHESIS: NON-AUTHORITATIVE@@" in request.text
    assert "FooRAT was observed." in request.text
    assert "BarRAT was also observed." in request.text
    assert "CURRENT HANDLES:" in request.text
    assert "UNSUPPORTED EVIDENCE COUNT: 1" in request.text
    assert "ADDED COUNT: 1" in request.text
    assert "UNCHANGED COUNT: 2" in request.text
    assert "REMOVED COUNT: 1" in request.text
    assert "REMOVED KINDS: fact" in request.text
    assert current_pack.handle_for(_pack_ref(current_pack, value=value_d)) in request.text
    assert current_pack.handle_for(_pack_ref(current_pack, value=value_a)) in request.text
    assert str(source_id) not in request.text
    assert str(previous_run_id) not in request.text

    stored = world.writer.calls[0]
    assert stored["mode"] is SynthesisMode.REVISE_PREVIOUS
    synthesis = stored["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    removed_ref = _pack_ref(previous_pack, value=value_b)
    cited_refs = {ref for paragraph in synthesis.lead for ref in paragraph.evidence_refs}
    cited_refs.update(
        ref
        for section in synthesis.sections
        for paragraph in section.paragraphs
        for ref in paragraph.evidence_refs
    )
    assert removed_ref not in cited_refs
    assert [paragraph.text for paragraph in synthesis.lead] == ["FooRAT was observed."]
    assert all("BarRAT" not in paragraph.text for paragraph in synthesis.lead)


@pytest.mark.asyncio
async def test_revision_output_replaying_a_removed_evidence_handle_is_rejected():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    previous_extraction = make_extraction(
        subject_id,
        (
            make_source(
                source_id,
                facts=(make_fact(source_id, "FooRAT"), make_fact(source_id, "BarRAT")),
            ),
        ),
    )
    current_extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    previous_pack = build_synthesis_evidence_pack(snapshot, previous_extraction)
    current_pack = build_synthesis_evidence_pack(snapshot, current_extraction)
    previous_handles = {str(record["handle"]) for record in previous_pack.narrative_evidence}
    current_handles = {str(record["handle"]) for record in current_pack.narrative_evidence}
    removed_handle = sorted(previous_handles - current_handles)
    assert len(removed_handle) == 1
    previous_synthesis = _prior_synthesis(
        snapshot,
        previous_extraction,
        production_input_hash="f" * 64,
        paragraphs=(
            SynthesisParagraphV1(
                text="BarRAT was also observed.",
                evidence_refs=(_pack_ref(previous_pack, value="BarRAT"),),
            ),
        ),
    )
    synthesis_blob_id, extraction_blob_id = uuid4(), uuid4()
    gateway = _RecordingGateway(
        lambda request: _succeeded(
            request,
            SynthesisProposalV1.model_validate(
                make_proposal("BarRAT was also observed.", removed_handle[0])
            ),
        )
    )
    world = _service_world(
        snapshot,
        current_extraction,
        (make_document(subject_id, source_id),),
        gateway,
        reuse=_ReuseStub(None),
        artifacts=_prior_artifacts(
            subject_id=subject_id,
            previous_run_id=uuid4(),
            synthesis_blob_id=synthesis_blob_id,
            extraction_blob_id=extraction_blob_id,
        ),
        extra_payloads={
            synthesis_blob_id: production_synthesis_to_json(previous_synthesis),
            extraction_blob_id: production_extraction_to_json(previous_extraction),
        },
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.mode is SynthesisMode.REVISE_PREVIOUS
    assert result.error_code == SynthesisProposalErrorCode.UNKNOWN_EVIDENCE.value
    assert result.error_code == "synthesis_unknown_evidence"
    assert result.model_calls == 1
    assert len(gateway.calls) == 1
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_language_mismatched_prior_cannot_be_accepted_as_exact_reuse():
    subject_id, source_id = uuid4(), uuid4()
    previous_snapshot = make_snapshot(subject_id)
    snapshot = replace(
        previous_snapshot, publication_language="en", input_hash="", reuse_basis_hash=""
    )
    assert snapshot.input_hash != previous_snapshot.input_hash
    document = make_document(subject_id, source_id)
    extraction = _matched_extraction(
        snapshot, make_source(source_id, facts=(make_fact(source_id, "FooRAT"),))
    )
    expected_input_hash = await _expected_input_hash(snapshot, extraction, (document,))
    canonical_blob_id = uuid4()
    previous = ProductionSynthesisV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(
            SynthesisParagraphV1(
                text="FooRAT was identified.",
                evidence_refs=(
                    _pack_ref(build_synthesis_evidence_pack(snapshot, extraction), value="FooRAT"),
                ),
            ),
        ),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    assert previous.publication_language == "fr"
    candidate = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=expected_input_hash,
        canonical_blob_id=canonical_blob_id,
        model_run_id=uuid4(),
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, None))
    world = _service_world(
        snapshot,
        extraction,
        (document,),
        gateway,
        reuse=_ReuseStub(ProductionArtifactReuseResult(artifact=candidate, reused=False)),
        extra_payloads={canonical_blob_id: production_synthesis_to_json(previous)},
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.NEEDS_REVIEW
    assert result.error_code == SynthesisStageErrorCode.REUSE_INVALID.value
    assert result.model_calls == 0
    assert gateway.calls == []
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_revision_retry_reuses_the_same_model_run_identity():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    previous_extraction = make_extraction(
        subject_id, (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),)
    )
    current_extraction = _matched_extraction(
        snapshot,
        make_source(
            source_id, facts=(make_fact(source_id, "FooRAT"), make_fact(source_id, "QuxLoader"))
        ),
    )
    previous_synthesis = _prior_synthesis(
        snapshot,
        previous_extraction,
        production_input_hash="f" * 64,
        paragraphs=(
            SynthesisParagraphV1(
                text="FooRAT was observed.",
                evidence_refs=(
                    _pack_ref(
                        build_synthesis_evidence_pack(snapshot, previous_extraction),
                        value="FooRAT",
                    ),
                ),
            ),
        ),
    )
    synthesis_blob_id, extraction_blob_id = uuid4(), uuid4()
    gateway = _RecordingGateway(_PreSubmissionFailure("the bridge was never reached"))
    world = _service_world(
        snapshot,
        current_extraction,
        (make_document(subject_id, source_id),),
        gateway,
        reuse=_ReuseStub(None),
        artifacts=_prior_artifacts(
            subject_id=subject_id,
            previous_run_id=uuid4(),
            synthesis_blob_id=synthesis_blob_id,
            extraction_blob_id=extraction_blob_id,
        ),
        extra_payloads={
            synthesis_blob_id: production_synthesis_to_json(previous_synthesis),
            extraction_blob_id: production_extraction_to_json(previous_extraction),
        },
    )

    for _attempt in range(2):
        with pytest.raises(_PreSubmissionFailure):
            await world.service.execute(world.run, snapshot, world.artifact)

    assert len(gateway.calls) == 2
    first_request = gateway.calls[0][0]
    second_request = gateway.calls[1][0]
    assert first_request.metadata["synthesis_mode"] == SynthesisMode.REVISE_PREVIOUS.value
    assert first_request.run_id == second_request.run_id
    assert first_request.run_id == synthesis_model_run_id(
        world.run,
        first_request.metadata["synthesis_invocation_hash"],
        SynthesisMode.REVISE_PREVIOUS,
    )
    assert world.writer.calls == []


@pytest.mark.asyncio
async def test_revision_never_merges_previous_paragraphs_into_the_new_document():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    previous_extraction = make_extraction(
        subject_id,
        (
            make_source(
                source_id,
                facts=(make_fact(source_id, "FooRAT"), make_fact(source_id, "BarRAT")),
            ),
        ),
    )
    current_extraction = _matched_extraction(
        snapshot,
        make_source(
            source_id, facts=(make_fact(source_id, "FooRAT"), make_fact(source_id, "QuxLoader"))
        ),
    )
    previous_pack = build_synthesis_evidence_pack(snapshot, previous_extraction)
    current_pack = build_synthesis_evidence_pack(snapshot, current_extraction)
    previous_synthesis = _prior_synthesis(
        snapshot,
        previous_extraction,
        production_input_hash="f" * 64,
        paragraphs=(
            SynthesisParagraphV1(
                text="FooRAT was observed.",
                evidence_refs=(_pack_ref(previous_pack, value="FooRAT"),),
            ),
            SynthesisParagraphV1(
                text="BarRAT was also observed.",
                evidence_refs=(_pack_ref(previous_pack, value="BarRAT"),),
            ),
        ),
    )
    synthesis_blob_id, extraction_blob_id = uuid4(), uuid4()
    proposal = SynthesisProposalV1.model_validate(
        make_proposal("QuxLoader was observed.", _pack_handle(current_pack, value="QuxLoader"))
    )
    gateway = _RecordingGateway(lambda request: _succeeded(request, proposal))
    world = _service_world(
        snapshot,
        current_extraction,
        (make_document(subject_id, source_id),),
        gateway,
        reuse=_ReuseStub(None),
        artifacts=_prior_artifacts(
            subject_id=subject_id,
            previous_run_id=uuid4(),
            synthesis_blob_id=synthesis_blob_id,
            extraction_blob_id=extraction_blob_id,
        ),
        extra_payloads={
            synthesis_blob_id: production_synthesis_to_json(previous_synthesis),
            extraction_blob_id: production_extraction_to_json(previous_extraction),
        },
    )

    result = await world.service.execute(world.run, snapshot, world.artifact)

    assert result.status is SynthesisExecutionStatus.SUCCEEDED
    synthesis = world.writer.calls[0]["synthesis"]
    assert isinstance(synthesis, ProductionSynthesisV1)
    texts = [paragraph.text for paragraph in synthesis.lead]
    texts.extend(
        paragraph.text for section in synthesis.sections for paragraph in section.paragraphs
    )
    assert texts == ["QuxLoader was observed.", "QuxLoader was observed."]
    assert "FooRAT was observed." not in texts
    assert "BarRAT was also observed." not in texts


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("second half of 2024", date(2024, 7, 1)),
        ("S2 2024", date(2024, 7, 1)),
        ("H1 2025", date(2025, 1, 1)),
        ("premier semestre 2025", date(2025, 1, 1)),
        ("summer 2025", date(2025, 6, 1)),
        ("été 2025", date(2025, 6, 1)),
        ("spring 2024", date(2024, 3, 1)),
        ("the same year", None),
    ],
)
def test_timeline_resolves_half_year_and_season_wording(
    wording: str, expected: date | None
) -> None:
    assert resolve_timeline_date_text(wording) == expected
