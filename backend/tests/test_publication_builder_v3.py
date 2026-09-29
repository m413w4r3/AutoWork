from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_builder import (
    _project_synthesis_publication,
    _validate_publication_v3_lineage,
    _validate_synthesis_evidence_refs,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
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
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
)


def _canonical_inputs() -> tuple[
    ProductionInputSnapshot,
    ProductionReferenceCorpusV1,
    ProductionExtractionV1,
    ProductionSynthesisV1,
]:
    subject_id = UUID(int=1)
    source_document_id = UUID(int=2)
    content_hash = "c" * 64
    snapshot = ProductionInputSnapshot(
        production_run_id=UUID(int=3),
        edition_id=UUID(int=4),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Example subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=UUID(int=5),
        origin_discovery_subject_id=UUID(int=6),
        canonical_discovery_subject_id=UUID(int=7),
        discovery_snapshot_id=UUID(int=8),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Example discovery summary",
        actor_or_campaign="Example actor",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        publication_language="en",
        research_date=date(2025, 2, 1),
    )
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://example.com/report",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Example report",
                publisher="Example publisher",
                published_at=None,
                source_collection_id=UUID(int=9),
                source_document_id=source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=content_hash,
                relevance_reason="Relevant report",
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    fact = ExtractionFactV1(
        category="actors",
        value="Example actor",
        attack_id=None,
        context="",
        evidence_quote="The source identifies the actor.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_document_id,),
    )
    source = ProductionSourceExtractionV1(
        source_document_id=source_document_id,
        canonical_url="https://example.com/report",
        content_sha256=content_hash,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(
            fact,
            replace(
                fact,
                category="infrastructure",
                value="example.net",
                evidence_quote="The source identifies example.net.",
            ),
        ),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_corpus_hash(references),
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )
    evidence = extraction_evidence_refs_v1(extraction)[0]
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="en",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Example synthesis",
        lead=(SynthesisParagraphV1("Example finding.", (evidence,)),),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    return snapshot, references, extraction, synthesis


def test_publication_v3_validators_accept_matching_canonical_inputs() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()

    _validate_publication_v3_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )
    _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)


@pytest.mark.parametrize("artifact", ("snapshot", "references", "extraction", "synthesis"))
def test_publication_v3_lineage_rejects_each_subject_mismatch(artifact: str) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    mismatched_subject_id = UUID(int=99)
    if artifact == "snapshot":
        snapshot = replace(
            snapshot,
            subject_id=mismatched_subject_id,
            input_hash="",
            reuse_basis_hash="",
        )
    elif artifact == "references":
        references = replace(references, subject_id=mismatched_subject_id)
    elif artifact == "extraction":
        extraction = replace(extraction, subject_id=mismatched_subject_id)
    else:
        synthesis = replace(synthesis, subject_id=mismatched_subject_id)

    with pytest.raises(ValueError, match="same subject identity"):
        _validate_publication_v3_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v3_lineage_rejects_extraction_snapshot_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    extraction = replace(extraction, production_input_hash="0" * 64)

    with pytest.raises(ValueError, match="production input snapshot"):
        _validate_publication_v3_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v3_lineage_rejects_synthesis_snapshot_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    synthesis = replace(synthesis, production_input_hash="0" * 64)

    with pytest.raises(ValueError, match="production input snapshot"):
        _validate_publication_v3_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v3_lineage_rejects_references_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    extraction = replace(extraction, references_corpus_hash="0" * 64)

    with pytest.raises(ValueError, match="canonical references corpus"):
        _validate_publication_v3_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


def test_publication_v3_lineage_rejects_extraction_hash_mismatch() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    synthesis = replace(synthesis, extraction_hash="0" * 64)

    with pytest.raises(ValueError, match="canonical extraction"):
        _validate_publication_v3_lineage(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )


@pytest.mark.parametrize("invalid_part", ("source", "key", "kind"))
def test_synthesis_evidence_validation_rejects_noncurrent_identity(
    invalid_part: str,
) -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    current_ref = extraction_evidence_refs_v1(extraction)[0]
    if invalid_part == "source":
        invalid_ref = replace(current_ref, source_document_id=UUID(int=99))
    elif invalid_part == "key":
        invalid_ref = replace(current_ref, evidence_key="0" * 64)
    else:
        invalid_ref = replace(current_ref, kind=EvidenceKind.EVENT)
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("Invalid citation.", (invalid_ref,)),),
    )

    with pytest.raises(ValueError, match="absent from the current extraction"):
        _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)


def test_synthesis_publication_projection_preserves_narrative_and_order() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)
    assert len(evidence) == 2
    source_document_id = evidence[0].source_document_id
    synthesis = replace(
        synthesis,
        lead=(
            SynthesisParagraphV1("Lead first.", (evidence[1],)),
            SynthesisParagraphV1("Lead second.", (evidence[0],)),
        ),
        sections=(
            SynthesisSectionV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="Overview heading",
                paragraphs=(SynthesisParagraphV1("Overview text.", (evidence[0],)),),
            ),
            SynthesisSectionV1(
                kind=SynthesisSectionKind.TECHNICAL,
                heading="Technical heading",
                paragraphs=(
                    SynthesisParagraphV1("Technical first.", (evidence[1],)),
                    SynthesisParagraphV1("Technical second.", (evidence[0],)),
                ),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2025, 1, 8),
                date_text="8 January",
                text="Dated event text.",
                evidence_refs=(evidence[1],),
            ),
            SynthesisTimelineEntryV1(
                event_date=None,
                date_text=None,
                text="Undated event text.",
                evidence_refs=(evidence[0],),
            ),
        ),
        uncertainties=(
            SynthesisUncertaintyV1("Uncertainty one.", (source_document_id,)),
            SynthesisUncertaintyV1("Uncertainty two.", (source_document_id,)),
        ),
    )

    projection = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)

    def publication_ref(ref: ExtractionEvidenceRefV1) -> PublicationEvidenceRefV1:
        return PublicationEvidenceRefV1(
            source_document_id=ref.source_document_id,
            kind=PublicationEvidenceKind(ref.kind.value),
            evidence_key=ref.evidence_key,
        )

    assert projection.lead == (
        PublicationParagraphV1("Lead first.", (publication_ref(evidence[1]),)),
        PublicationParagraphV1("Lead second.", (publication_ref(evidence[0]),)),
    )
    assert projection.sections == (
        PublicationSectionV1(
            PublicationSectionKind.OVERVIEW,
            "Overview heading",
            (PublicationParagraphV1("Overview text.", (publication_ref(evidence[0]),)),),
        ),
        PublicationSectionV1(
            PublicationSectionKind.TECHNICAL,
            "Technical heading",
            (
                PublicationParagraphV1("Technical first.", (publication_ref(evidence[1]),)),
                PublicationParagraphV1("Technical second.", (publication_ref(evidence[0]),)),
            ),
        ),
    )
    assert projection.timeline == (
        PublicationTimelineEntryV1(
            date(2025, 1, 8),
            "8 January",
            "Dated event text.",
            (publication_ref(evidence[1]),),
        ),
        PublicationTimelineEntryV1(
            None,
            None,
            "Undated event text.",
            (publication_ref(evidence[0]),),
        ),
    )
    assert projection.uncertainties == tuple(
        PublicationUncertaintyV1(item.text, item.source_document_ids)
        for item in synthesis.uncertainties
    )
    assert projection.used_source_document_ids == frozenset({source_document_id})

    reversed_synthesis = replace(
        synthesis,
        lead=tuple(reversed(synthesis.lead)),
        sections=tuple(reversed(synthesis.sections)),
    )
    reversed_projection = _project_synthesis_publication(
        extraction=extraction,
        synthesis=reversed_synthesis,
    )
    assert reversed_projection.lead == tuple(reversed(projection.lead))
    assert reversed_projection.sections == tuple(reversed(projection.sections))


def test_synthesis_publication_projection_validates_evidence_before_conversion() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    current_ref = extraction_evidence_refs_v1(extraction)[0]
    invalid_ref = replace(current_ref, evidence_key="0" * 64)
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("Invalid citation.", (invalid_ref,)),),
    )

    with pytest.raises(ValueError, match="absent from the current extraction"):
        _project_synthesis_publication(extraction=extraction, synthesis=synthesis)
