from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_relevance import (
    build_relevance_projection,
)
from cti_app.application.production_synthesis import (
    MAX_SYNTHESIS_UNCERTAINTIES,
    SynthesisEvidencePackV1,
    build_synthesis_evidence_pack,
    build_synthesis_timeline,
    build_synthesis_uncertainties,
    canonical_extraction_hash,
    extraction_evidence_elements,
    synthesis_evidence_pack_hash,
    synthesis_input_hash,
)
from cti_app.application.publication_builder import (
    _project_publication_iocs,
    _PublicationNarrativeProjection,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
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
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceDecisionProvenance,
    RelevanceProjectionV1,
    RelevanceReasonCode,
)
from cti_app.domain.production_synthesis import (
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
)
from cti_app.domain.publication import ArtifactType


def _snapshot(
    subject_id=None,
    *,
    actor_or_campaign: str = "MOIS",
    title: str = "Iranian Bitcoin OP_RETURN activity",
) -> ProductionInputSnapshot:
    ids = [uuid4() for _ in range(7)]
    return ProductionInputSnapshot(
        production_run_id=ids[0],
        edition_id=ids[1],
        subject_id=subject_id or uuid4(),
        subject_version=1,
        subject_title=title,
        subject_tlp=TLP.CLEAR,
        selection_decision_id=ids[2],
        origin_discovery_subject_id=ids[3],
        canonical_discovery_subject_id=ids[4],
        discovery_snapshot_id=ids[5],
        discovery_snapshot_version=1,
        member_candidate_ids=(ids[6],),
        discovery_summary="A frozen summary of the Bitcoin OP_RETURN case.",
        actor_or_campaign=actor_or_campaign,
        period_start=date(2024, 1, 1),
        period_end=date(2025, 12, 31),
        publication_language="fr",
        research_date=date(2026, 10, 2),
    )


def _fact(source_id, value: str, *, context: str = "") -> ExtractionFactV1:
    return ExtractionFactV1(
        category="actors",
        value=value,
        attack_id=None,
        context=context,
        evidence_quote=value,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def _event(
    source_id,
    text: str,
    *,
    date_text: str | None = None,
    event_date: date | None = None,
) -> ExtractionEventV1:
    return ExtractionEventV1(
        event_date=event_date,
        date_text=date_text,
        text=text,
        context="Source chronology.",
        evidence_quote=text,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def _indicator(
    source_id,
    value: str,
    artifact_type: ArtifactType,
    *,
    context: str,
) -> ExtractionIndicatorV1:
    return ExtractionIndicatorV1(
        value=value,
        artifact_type=artifact_type,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context=context,
        evidence_quote=f"Observed value: {value}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def _source(
    source_id,
    *,
    tier=ProductionReferenceTier.CORE,
    editorial_role=ProductionEditorialRole.PRIMARY,
    role=SourceRole.PRIMARY,
    facts=(),
    events=(),
    indicators=(),
    rules=(),
    uncertainties=(),
) -> ProductionSourceExtractionV1:
    return ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url=f"https://example.test/{source_id}",
        content_sha256=(str(source_id.int).zfill(64))[-64:],
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=role,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=tuple(facts),
        events=tuple(events),
        indicators=tuple(indicators),
        rules=tuple(rules),
        uncertainties=tuple(uncertainties),
        editorial_role=editorial_role,
    )


def _extraction(snapshot: ProductionInputSnapshot, sources) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=tuple(sources),
        omitted_sources=(),
        warnings=(),
    )


def _counterfactual_projection(
    projection: RelevanceProjectionV1,
    ref: ExtractionEvidenceRefV1,
    classification: RelevanceClassification,
) -> RelevanceProjectionV1:
    items = tuple(
        replace(item, classification=classification) if item.evidence_ref == ref else item
        for item in projection.classifications
    )
    return replace(projection, classifications=items)


def test_projection_rejects_unknown_evidence_refs_and_round_trips() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    extraction = _extraction(
        snapshot,
        (_source(source_id, facts=(_fact(source_id, "MOIS activity"),)),),
    )
    projection = build_relevance_projection(snapshot, extraction)
    first = projection.classifications[0]
    unknown_ref = ExtractionEvidenceRefV1(uuid4(), EvidenceKind.FACT, "f" * 64)
    invalid_first = replace(
        first,
        supporting_evidence_refs=(first.evidence_ref, unknown_ref),
    )

    with pytest.raises(ValueError, match="unknown extraction evidence"):
        replace(
            projection,
            classifications=(invalid_first, *projection.classifications[1:]),
        )

    from cti_app.domain.production_relevance import (
        relevance_projection_from_json,
        relevance_projection_to_json,
    )

    restored = relevance_projection_from_json(relevance_projection_to_json(projection))
    assert restored == projection
    assert restored.projection_hash == projection.projection_hash


def test_projection_hash_changes_when_classification_changes() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    extraction = _extraction(
        snapshot,
        (_source(source_id, facts=(_fact(source_id, "MOIS activity"),)),),
    )
    projection = build_relevance_projection(snapshot, extraction)
    changed = _counterfactual_projection(
        projection,
        projection.classifications[0].evidence_ref,
        RelevanceClassification.INDETERMINATE,
    )

    assert projection.input_hash == changed.input_hash
    assert projection.projection_hash != changed.projection_hash


def test_projection_classifies_rules_and_uncertainties_with_extraction_refs() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    body = "rule MOIS_sample { condition: true }"
    rule = ExtractionRuleV1(
        rule_type=DetectionRuleType.YARA,
        name="MOIS sample",
        body=body,
        sha256=hashlib.sha256(body.encode()).hexdigest(),
        context="Rule for the MOIS sample.",
        evidence_quote="The report publishes a YARA rule for the MOIS sample.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    extraction = _extraction(
        snapshot,
        (_source(source_id, rules=(rule,), uncertainties=("Attribution remains unclear.",)),),
    )

    projection = build_relevance_projection(snapshot, extraction)

    assert {item.evidence_ref.kind for item in projection.classifications} == {
        EvidenceKind.RULE,
        EvidenceKind.UNCERTAINTY,
    }
    assert len(projection.classifications) == 2


def test_same_capture_keeps_extraction_rows_and_changes_subject_projection() -> None:
    first_snapshot = _snapshot(actor_or_campaign="MuddyWater")
    shared_source_id = uuid4()
    shared_source = _source(
        shared_source_id,
        facts=(_fact(shared_source_id, "DPRK actors operated infrastructure"),),
    )
    first_extraction = _extraction(first_snapshot, (shared_source,))
    second_snapshot = _snapshot(actor_or_campaign="DPRK")
    second_extraction = replace(
        first_extraction,
        subject_id=second_snapshot.subject_id,
        production_input_hash=second_snapshot.input_hash,
    )

    first_projection = build_relevance_projection(first_snapshot, first_extraction)
    second_projection = build_relevance_projection(second_snapshot, second_extraction)

    assert first_extraction.sources == second_extraction.sources
    assert (
        first_projection.classifications[0].classification is RelevanceClassification.OUT_OF_SCOPE
    )
    assert second_projection.classifications[0].classification is RelevanceClassification.DIRECT
    assert first_projection.projection_hash != second_projection.projection_hash


def test_timeline_filters_other_actors_and_preserves_approximate_date_text() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    primary = _source(
        source_id,
        events=(
            _event(
                source_id,
                "MOIS operators used the OP_RETURN marker",
                date_text="mid-2024",
                event_date=date(2024, 6, 15),
            ),
            _event(
                source_id,
                "MOIS operators used the OP-RETURN marker.",
                date_text="mid-2024",
                event_date=date(2024, 6, 15),
            ),
            _event(source_id, "DPRK actors ran a separate intrusion set"),
            _event(source_id, "Necurs activity affected unrelated victims"),
        ),
    )
    extraction = _extraction(snapshot, (primary,))
    projection = build_relevance_projection(snapshot, extraction)

    timeline = build_synthesis_timeline(extraction, projection=projection)

    assert len(timeline) == 1
    assert timeline[0].date_text == "mid-2024"
    assert timeline[0].event_date is None
    assert "DPRK" not in str(timeline)
    assert "Necurs" not in str(timeline)
    assert len(timeline[0].evidence_refs) == 2


def test_unrelated_muddywater_hashes_generic_filename_and_footer_email_are_not_iocs() -> None:
    snapshot = _snapshot(actor_or_campaign="MOIS")
    primary_id, support_id = uuid4(), uuid4()
    primary = _source(primary_id, facts=(_fact(primary_id, "MOIS OP_RETURN activity"),))
    support = _source(
        support_id,
        tier=ProductionReferenceTier.SUPPORTING,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        role=SourceRole.INDEPENDENT,
        indicators=(
            _indicator(
                support_id,
                "a" * 64,
                ArtifactType.HASH,
                context="MuddyWater malware sample hash for ArenaC2",
            ),
            _indicator(
                support_id,
                "main.py",
                ArtifactType.FILENAME,
                context="Generic file name mentioned by the article",
            ),
            _indicator(
                support_id,
                "analyst@example.test",
                ArtifactType.EMAIL,
                context="Footer contact for editorial questions",
            ),
        ),
    )
    extraction = _extraction(snapshot, (primary, support))
    projection = build_relevance_projection(snapshot, extraction)
    decisions = {item.evidence_ref: item for item in projection.classifications}
    indicator_entries = tuple(
        (ref, payload)
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.source_document_id == support_id and ref.kind is EvidenceKind.INDICATOR
    )

    hash_ref = next(ref for ref, payload in indicator_entries if payload["value"] == "a" * 64)
    assert decisions[hash_ref].classification is RelevanceClassification.OUT_OF_SCOPE
    generic_decision = next(
        item
        for item in projection.classifications
        if item.reason_code is RelevanceReasonCode.GENERIC_FILENAME
    )
    contact_decision = next(
        item
        for item in projection.classifications
        if item.reason_code is RelevanceReasonCode.FOOTER_CONTACT
    )
    assert generic_decision.classification is RelevanceClassification.CONTEXT
    assert contact_decision.classification is RelevanceClassification.CONTEXT

    narrative = _PublicationNarrativeProjection((), (), (), (), frozenset())
    published = _project_publication_iocs(
        extraction=extraction,
        narrative=narrative,
        relevance_projection=projection,
    )
    assert published.indicators == ()


def test_core_confirmed_iocs_are_direct_by_default_and_published() -> None:
    snapshot = _snapshot(actor_or_campaign="MOIS")
    core_id, support_id = uuid4(), uuid4()
    core = _source(
        core_id,
        indicators=(
            # Neutral wording: the CORE source is about this subject and presents
            # the value as a confirmed IOC.
            _indicator(core_id, "c2.example.test", ArtifactType.DOMAIN, context="Command server"),
            # Another actor named without any subject anchor stays out of scope.
            _indicator(
                core_id,
                "b" * 64,
                ArtifactType.HASH,
                context="MuddyWater sample hash for ArenaC2",
            ),
        ),
    )
    support = _source(
        support_id,
        tier=ProductionReferenceTier.SUPPORTING,
        editorial_role=ProductionEditorialRole.CONTEXT,
        role=SourceRole.INDEPENDENT,
        indicators=(
            _indicator(
                support_id, "other.example.test", ArtifactType.DOMAIN, context="Command server"
            ),
        ),
    )
    extraction = _extraction(snapshot, (core, support))
    projection = build_relevance_projection(snapshot, extraction)
    by_value = {
        payload["value"]: projection.classification_for(ref)
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.INDICATOR
    }

    domain = by_value["c2.example.test"]
    assert domain.classification is RelevanceClassification.DIRECT
    assert domain.reason_code is RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION
    assert by_value["b" * 64].classification is RelevanceClassification.OUT_OF_SCOPE
    # A complementary source without a demonstrated subject relation stays unpublished.
    assert by_value["other.example.test"].classification is RelevanceClassification.INDETERMINATE

    narrative = _PublicationNarrativeProjection((), (), (), (), frozenset())
    published = _project_publication_iocs(
        extraction=extraction, narrative=narrative, relevance_projection=projection
    )
    assert [
        item.normalized_value for group in published.indicators for item in group.indicators
    ] == ["c2.example.test"]


def test_indeterminate_stays_in_projection_and_is_omitted_from_synthesis() -> None:
    snapshot = _snapshot()
    primary_id, support_id = uuid4(), uuid4()
    primary = _source(primary_id, facts=(_fact(primary_id, "MOIS operated the campaign"),))
    support = _source(
        support_id,
        tier=ProductionReferenceTier.SUPPORTING,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        role=SourceRole.INDEPENDENT,
        events=(_event(support_id, "An operation affected several networks"),),
        indicators=(
            _indicator(
                support_id,
                "b" * 64,
                ArtifactType.HASH,
                context="Malware sample hash used by the actor",
            ),
        ),
    )
    extraction = _extraction(snapshot, (primary, support))
    projection = build_relevance_projection(snapshot, extraction)
    uncertain = next(
        item
        for item in projection.classifications
        if item.evidence_ref.source_document_id == support_id
    )

    assert uncertain.classification is RelevanceClassification.INDETERMINATE
    assert uncertain.provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
    assert any(
        item.classification is RelevanceClassification.INDETERMINATE
        and item.evidence_ref.kind is EvidenceKind.INDICATOR
        for item in projection.classifications
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
    timeline = build_synthesis_timeline(extraction, projection=projection)
    assert all(
        ref.source_document_id != support_id for entry in timeline for ref in entry.evidence_refs
    )
    assert all("several networks" not in str(item) for item in pack.narrative_evidence)
    narrative = _PublicationNarrativeProjection((), (), (), (), frozenset())
    published = _project_publication_iocs(
        extraction=extraction,
        narrative=narrative,
        relevance_projection=projection,
    )
    assert published.indicators == ()


def test_uncertainties_deduplicate_and_rank_impact_before_the_cap() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    uncertainties = (
        "Operational question 00 remains open.",
        "Attribution of the MOIS role remains unclear.",
        "attribution of the MOIS role remains unclear!",
        *(f"Operational question {index:02} remains open." for index in range(1, 12)),
    )
    extraction = _extraction(
        snapshot,
        (_source(source_id, uncertainties=uncertainties),),
    )
    projection = build_relevance_projection(snapshot, extraction)

    selected = build_synthesis_uncertainties(extraction, projection=projection)

    assert len(selected) == MAX_SYNTHESIS_UNCERTAINTIES
    assert selected[0].text.casefold().rstrip(".!?") == (
        "attribution of the mois role remains unclear"
    )
    assert (
        sum(
            item.text.casefold().rstrip(".!?") == "attribution of the mois role remains unclear"
            for item in selected
        )
        == 1
    )


def test_projection_hash_invalidates_synthesis_and_enrichment_input_hashes() -> None:
    snapshot = _snapshot()
    source_id = uuid4()
    extraction = _extraction(
        snapshot,
        (_source(source_id, facts=(_fact(source_id, "MOIS activity"),)),),
    )
    projection = build_relevance_projection(snapshot, extraction)
    changed = _counterfactual_projection(
        projection,
        projection.classifications[0].evidence_ref,
        RelevanceClassification.INDETERMINATE,
    )
    original_pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
    changed_pack = build_synthesis_evidence_pack(snapshot, extraction, changed)
    original_hash = synthesis_input_hash(snapshot, extraction, original_pack, "c" * 64)
    changed_hash = synthesis_input_hash(snapshot, extraction, changed_pack, "c" * 64)

    synthesis = ProductionSynthesisV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    original_enrichment_pack = build_editorial_enrichment_evidence_pack(
        snapshot, extraction, synthesis, projection
    )
    changed_enrichment_pack = build_editorial_enrichment_evidence_pack(
        snapshot, extraction, synthesis, changed
    )
    original_enrichment_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(original_enrichment_pack),
        access_policy_hash="d" * 64,
        projection_hash=projection.projection_hash,
    )
    changed_enrichment_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(changed_enrichment_pack),
        access_policy_hash="d" * 64,
        projection_hash=changed.projection_hash,
    )

    assert synthesis_evidence_pack_hash(original_pack) != synthesis_evidence_pack_hash(changed_pack)
    assert original_hash != changed_hash
    assert original_enrichment_hash != changed_enrichment_hash
    assert isinstance(original_pack, SynthesisEvidencePackV1)
