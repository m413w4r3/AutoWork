import hashlib
import json
from dataclasses import replace
from datetime import date
from inspect import Parameter, signature
from uuid import UUID

import pytest

import cti_app.application.publication_builder as publication_builder
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_builder import (
    _project_publication_iocs,
    _project_synthesis_publication,
    _validate_publication_v3_lineage,
    _validate_synthesis_evidence_refs,
    build_publication_document_v3,
    compute_assembly_input_hash,
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
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
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
    production_synthesis_to_json,
)
from cti_app.domain.publication import (
    PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
    ArtifactType,
    PublicationDocumentV3,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
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


def test_assembly_input_hash_uses_exact_canonical_functional_payload() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    payload = {
        "snapshot_input_hash": snapshot.input_hash,
        "references_hash": references_corpus_hash(references),
        "extraction_hash": canonical_extraction_hash(extraction),
        "synthesis_hash": hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis))
        ).hexdigest(),
        "publication_document_schema_version": PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
        "assembly_policy_version": publication_builder.ASSEMBLY_POLICY_VERSION,
    }

    expected_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(payload)
    ).hexdigest()
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        == expected_hash
    )
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        == expected_hash
    )

    reconstructed_snapshot = replace(snapshot)
    reconstructed_references = replace(references)
    reconstructed_extraction = replace(extraction)
    reconstructed_synthesis = replace(synthesis)
    assert (
        compute_assembly_input_hash(
            snapshot=reconstructed_snapshot,
            references=reconstructed_references,
            extraction=reconstructed_extraction,
            synthesis=reconstructed_synthesis,
        )
        == expected_hash
    )


def test_assembly_input_hash_changes_with_each_functional_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    original = compute_assembly_input_hash(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )

    changed_snapshot = replace(
        snapshot,
        subject_title="Changed subject",
        input_hash="",
        reuse_basis_hash="",
    )
    assert changed_snapshot.input_hash != snapshot.input_hash
    assert (
        compute_assembly_input_hash(
            snapshot=changed_snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        != original
    )

    changed_reference = replace(references.sources[0], title="Changed report")
    changed_references = replace(references, sources=(changed_reference,))
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=changed_references,
            extraction=extraction,
            synthesis=synthesis,
        )
        != original
    )

    changed_extraction = replace(extraction, warnings=("Changed extraction",))
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=changed_extraction,
            synthesis=synthesis,
        )
        != original
    )

    changed_synthesis = replace(synthesis, title="Changed synthesis")
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=changed_synthesis,
        )
        != original
    )

    monkeypatch.setattr(
        publication_builder,
        "PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION",
        "changed-schema",
    )
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        != original
    )

    monkeypatch.setattr(publication_builder, "ASSEMBLY_POLICY_VERSION", "2")
    assert (
        compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        != original
    )


def test_assembly_input_hash_api_excludes_runtime_and_renderer_inputs() -> None:
    parameters = signature(compute_assembly_input_hash).parameters
    assert tuple(parameters) == ("snapshot", "references", "extraction", "synthesis")
    assert all(parameter.kind is Parameter.KEYWORD_ONLY for parameter in parameters.values())


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


def test_publication_ioc_projection_normalizes_deduplicates_and_merges_provenance() -> None:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    source_a = extraction.sources[0]
    source_b_id = UUID(int=5)

    def indicator(
        value: str,
        artifact_type: ArtifactType,
        status: ExtractionIndicatorStatus,
        source_document_id: UUID,
    ) -> ExtractionIndicatorV1:
        return ExtractionIndicatorV1(
            value=value,
            artifact_type=artifact_type,
            indicator_status=status,
            context="",
            evidence_quote=f"The source identifies {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_document_id,),
        )

    source_a = replace(
        source_a,
        indicators=(
            indicator(
                "Example[.]COM.",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "Analyst[at]Example[.]COM",
                ArtifactType.EMAIL,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "A" * 64,
                ArtifactType.HASH,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "2001:DB8::1",
                ArtifactType.IP,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "HXXPS://Example[.]COM/Path",
                ArtifactType.URL,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_a.source_document_id,
            ),
            indicator(
                "contextual.example",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONTEXTUAL,
                source_a.source_document_id,
            ),
            *(
                indicator(
                    "rule artifact",
                    artifact_type,
                    ExtractionIndicatorStatus.CONFIRMED_IOC,
                    source_a.source_document_id,
                )
                for artifact_type in (
                    ArtifactType.YARA_RULE,
                    ArtifactType.SIGMA_RULE,
                    ArtifactType.SURICATA_RULE,
                )
            ),
        ),
    )
    source_b = replace(
        source_a,
        source_document_id=source_b_id,
        canonical_url="https://example.com/second",
        facts=(),
        events=(),
        indicators=(
            indicator(
                "example.com",
                ArtifactType.DOMAIN,
                ExtractionIndicatorStatus.CONFIRMED_IOC,
                source_b_id,
            ),
        ),
        uncertainties=(),
    )
    extraction = replace(extraction, sources=(source_a, source_b))
    synthesis = replace(
        synthesis,
        uncertainties=(
            SynthesisUncertaintyV1("Uncertain finding.", (source_a.source_document_id,)),
        ),
    )
    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)

    projection = _project_publication_iocs(extraction=extraction, narrative=narrative)

    assert projection.indicators == (
        PublicationIndicatorGroupV1(
            ArtifactType.DOMAIN,
            (
                PublicationIndicatorV1(
                    "Example[.]COM.",
                    "example.com",
                    ArtifactType.DOMAIN,
                    (source_a.source_document_id, source_b_id),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.EMAIL,
            (
                PublicationIndicatorV1(
                    "Analyst[at]Example[.]COM",
                    "Analyst@example.com",
                    ArtifactType.EMAIL,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.HASH,
            (
                PublicationIndicatorV1(
                    "A" * 64,
                    "a" * 64,
                    ArtifactType.HASH,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.IP,
            (
                PublicationIndicatorV1(
                    "2001:DB8::1",
                    "2001:db8::1",
                    ArtifactType.IP,
                    (source_a.source_document_id,),
                ),
            ),
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.URL,
            (
                PublicationIndicatorV1(
                    "HXXPS://Example[.]COM/Path",
                    "https://example.com/Path",
                    ArtifactType.URL,
                    (source_a.source_document_id,),
                ),
            ),
        ),
    )
    assert projection.used_source_document_ids == frozenset(
        {source_a.source_document_id, source_b_id}
    )

    reordered_extraction = replace(
        extraction,
        sources=(
            replace(source_a, indicators=tuple(reversed(source_a.indicators))),
            replace(source_b, indicators=tuple(reversed(source_b.indicators))),
        ),
    )
    assert (
        _project_publication_iocs(
            extraction=reordered_extraction,
            narrative=narrative,
        )
        == projection
    )


def test_publication_v3_builder_is_exact_deterministic_and_resolves_used_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source_a = extraction.sources[0]
    source_a_id = source_a.source_document_id
    source_b_id = UUID(int=20)
    unused_source_id = UUID(int=30)

    def indicator(source_document_id: UUID) -> ExtractionIndicatorV1:
        return ExtractionIndicatorV1(
            value="shared.example",
            artifact_type=ArtifactType.DOMAIN,
            indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
            context="",
            evidence_quote="The report identifies shared.example.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_document_id,),
        )

    source_a = replace(source_a, indicators=(indicator(source_a_id),))
    source_b = replace(
        source_a,
        source_document_id=source_b_id,
        canonical_url="https://example.com/second-report",
        content_sha256="d" * 64,
        role=SourceRole.INDEPENDENT,
        facts=(
            replace(
                source_a.facts[0],
                value="Another actor",
                source_document_ids=(source_b_id,),
            ),
        ),
        indicators=(indicator(source_b_id),),
    )
    reference_a = references.sources[0]
    reference_b = replace(
        reference_a,
        canonical_url=source_b.canonical_url,
        role=SourceRole.INDEPENDENT,
        title=None,
        publisher=None,
        published_at=date(2025, 1, 12),
        source_collection_id=UUID(int=10),
        source_document_id=source_b_id,
        content_sha256="d" * 64,
    )
    unused_reference = replace(
        reference_a,
        canonical_url="https://example.com/unused-report",
        source_collection_id=UUID(int=11),
        source_document_id=unused_source_id,
        content_sha256="e" * 64,
    )
    references = replace(
        references,
        sources=(reference_a, reference_b, unused_reference),
    )
    extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(references),
        sources=(source_a, source_b),
    )
    evidence = extraction_evidence_refs_v1(extraction)
    fact_a = next(
        ref
        for ref in evidence
        if ref.source_document_id == source_a_id and ref.kind is EvidenceKind.FACT
    )
    fact_b = next(
        ref
        for ref in evidence
        if ref.source_document_id == source_b_id and ref.kind is EvidenceKind.FACT
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        title="Exact canonical title",
        lead=(
            SynthesisParagraphV1("First lead.", (fact_a,)),
            SynthesisParagraphV1("Second lead.", (fact_b,)),
        ),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Overview",
                (SynthesisParagraphV1("Overview text.", (fact_a, fact_b)),),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Technical details",
                (SynthesisParagraphV1("Technical text.", (fact_b,)),),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(date(2025, 1, 8), "8 January", "Dated event.", (fact_a,)),
            SynthesisTimelineEntryV1(None, None, "Undated event.", (fact_b,)),
        ),
        uncertainties=(
            SynthesisUncertaintyV1("Attribution remains uncertain.", (source_b_id, source_a_id)),
        ),
    )

    def forbidden_adapter(*_args: object, **_kwargs: object) -> None:
        pytest.fail("V3 builder invoked a legacy model or renderer adapter")

    monkeypatch.setattr(publication_builder, "SemanticAnnotator", forbidden_adapter)
    monkeypatch.setattr(publication_builder, "collect_indicators", forbidden_adapter)
    monkeypatch.setattr(publication_builder, "apply_french_spacing", forbidden_adapter)

    document = build_publication_document_v3(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )
    ref_a = PublicationEvidenceRefV1(
        fact_a.source_document_id,
        PublicationEvidenceKind(fact_a.kind.value),
        fact_a.evidence_key,
    )
    ref_b = PublicationEvidenceRefV1(
        fact_b.source_document_id,
        PublicationEvidenceKind(fact_b.kind.value),
        fact_b.evidence_key,
    )
    expected = PublicationDocumentV3(
        schema_version="3",
        subject_id=snapshot.subject_id,
        publication_language="fr",
        title="Exact canonical title",
        lead=(
            PublicationParagraphV1("First lead.", (ref_a,)),
            PublicationParagraphV1("Second lead.", (ref_b,)),
        ),
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.OVERVIEW,
                "Overview",
                (PublicationParagraphV1("Overview text.", (ref_a, ref_b)),),
            ),
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                "Technical details",
                (PublicationParagraphV1("Technical text.", (ref_b,)),),
            ),
        ),
        timeline=(
            PublicationTimelineEntryV1(date(2025, 1, 8), "8 January", "Dated event.", (ref_a,)),
            PublicationTimelineEntryV1(None, None, "Undated event.", (ref_b,)),
        ),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.DOMAIN,
                (
                    PublicationIndicatorV1(
                        "shared.example",
                        "shared.example",
                        ArtifactType.DOMAIN,
                        (source_a_id, source_b_id),
                    ),
                ),
            ),
        ),
        sources=(
            PublicationSourceV1(
                source_document_id=source_a_id,
                canonical_url=reference_a.canonical_url,
                title=reference_a.title,
                publisher=reference_a.publisher,
                published_at=reference_a.published_at,
                tier=reference_a.tier,
                kind=reference_a.kind,
                role=reference_a.role,
            ),
            PublicationSourceV1(
                source_document_id=source_b_id,
                canonical_url=reference_b.canonical_url,
                title=reference_b.title,
                publisher=reference_b.publisher,
                published_at=reference_b.published_at,
                tier=reference_b.tier,
                kind=reference_b.kind,
                role=reference_b.role,
            ),
        ),
        uncertainties=(
            PublicationUncertaintyV1("Attribution remains uncertain.", (source_a_id, source_b_id)),
        ),
    )
    assert document == expected

    canonical_json = json.dumps(
        document.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    repeated = build_publication_document_v3(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )
    assert (
        json.dumps(repeated.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == canonical_json
    )

    missing_references = replace(
        references,
        sources=tuple(
            source for source in references.sources if source.source_document_id != source_b_id
        ),
    )
    missing_extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(missing_references),
    )
    missing_synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(missing_extraction),
    )
    with pytest.raises(ValueError, match="absent from the canonical reference corpus"):
        build_publication_document_v3(
            snapshot=snapshot,
            references=missing_references,
            extraction=missing_extraction,
            synthesis=missing_synthesis,
        )
