from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SOURCE_METADATA_POLICY_VERSION,
    _editorial_enrichment_identity_payload,
    build_editorial_enrichment_evidence_pack,
)
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisTimelineEntryV1,
    source_metadata_evidence_ref_v1,
)


def _brief_inputs() -> tuple[
    ProductionInputSnapshot,
    ProductionExtractionV1,
    ProductionSynthesisV1,
]:
    ids = [uuid4() for _ in range(8)]
    source_id = ids[7]
    snapshot = ProductionInputSnapshot(
        production_run_id=ids[0],
        edition_id=ids[1],
        subject_id=ids[2],
        subject_version=1,
        subject_title="Anonymous subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=ids[3],
        origin_discovery_subject_id=ids[4],
        canonical_discovery_subject_id=ids[5],
        discovery_snapshot_id=ids[6],
        discovery_snapshot_version=1,
        member_candidate_ids=(uuid4(),),
        discovery_summary="Anonymous summary",
        actor_or_campaign="Anonymous actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        publication_language="fr",
        research_date=date(2026, 3, 1),
    )
    fact = ExtractionFactV1(
        category="actors",
        value="Example actor",
        attack_id=None,
        context="An anonymized source verified statement.",
        evidence_quote="An anonymized source verified statement.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    source = ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url="https://example.test/brief-source",
        content_sha256="a" * 64,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(fact,),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )
    metadata_ref = source_metadata_evidence_ref_v1(
        source_document_id=source_id,
        canonical_url=source.canonical_url,
        title="Anonymous publication",
        publisher="Example publisher",
        published_at=date(2026, 1, 15),
    )
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="[Anonymous group] Brief",
        lead=(),
        sections=(),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2026, 1, 15),
                date_text=None,
                text="Publication metadata fallback.",
                evidence_refs=(metadata_ref,),
            ),
        ),
        uncertainties=(),
        warnings=(),
    )
    return snapshot, extraction, synthesis


def test_brief_metadata_timeline_is_not_promoted_to_enrichment_evidence() -> None:
    snapshot, extraction, synthesis = _brief_inputs()
    projection = build_relevance_projection(snapshot, extraction)

    pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis, projection)

    assert len(pack.narrative_evidence) == 1
    assert pack.current_synthesis["timeline"][0]["evidence_handles"] == []
    assert all(ref.kind is not EvidenceKind.SOURCE_METADATA for ref in pack._handle_to_ref.values())
    assert pack.policy_version == EDITORIAL_ENRICHMENT_EVIDENCE_PACK_SOURCE_METADATA_POLICY_VERSION
    assert EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION == (
        "editorial-enrichment-evidence-pack-v8-relation-support"
    )

    article_like_synthesis = replace(synthesis, timeline=())
    article_like_pack = build_editorial_enrichment_evidence_pack(
        snapshot, extraction, article_like_synthesis, projection
    )
    assert article_like_pack.policy_version == EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION

    common_identity = {
        "extraction": extraction,
        "synthesis": article_like_synthesis,
        "evidence_pack_hash": "c" * 64,
        "access_policy_hash": "d" * 64,
    }
    article_identity_payload = _editorial_enrichment_identity_payload(**common_identity)
    brief_identity_payload = _editorial_enrichment_identity_payload(
        **{**common_identity, "synthesis": synthesis}
    )
    assert (
        article_identity_payload["evidence_pack_policy_version"]
        == EDITORIAL_ENRICHMENT_EVIDENCE_PACK_POLICY_VERSION
    )
    assert brief_identity_payload["evidence_pack_policy_version"] == pack.policy_version


def test_unmatched_extraction_ref_is_still_rejected() -> None:
    snapshot, extraction, synthesis = _brief_inputs()
    projection = build_relevance_projection(snapshot, extraction)
    unknown_ref = ExtractionEvidenceRefV1(
        source_document_id=extraction.sources[0].source_document_id,
        kind=EvidenceKind.FACT,
        evidence_key="0" * 64,
    )
    synthesis_with_unknown_ref = ProductionSynthesisV1(
        schema_version=synthesis.schema_version,
        subject_id=synthesis.subject_id,
        production_input_hash=synthesis.production_input_hash,
        extraction_hash=synthesis.extraction_hash,
        publication_language=synthesis.publication_language,
        synthesis_policy_version=synthesis.synthesis_policy_version,
        title=synthesis.title,
        lead=synthesis.lead,
        sections=synthesis.sections,
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2026, 1, 15),
                date_text=None,
                text="An anonymized extraction event.",
                evidence_refs=(unknown_ref,),
            ),
        ),
        uncertainties=synthesis.uncertainties,
        warnings=synthesis.warnings,
    )

    with pytest.raises(ValueError, match="Projection has no classification"):
        build_editorial_enrichment_evidence_pack(
            snapshot, extraction, synthesis_with_unknown_ref, projection
        )
