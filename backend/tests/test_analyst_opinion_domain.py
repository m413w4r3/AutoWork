from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    CitationViolationCode,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    normalize_opinion_text,
    opinion_blocks_with_observations,
    verify_opinion_citations,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
)


def _observation(investigation_id) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"captured report").hexdigest(),
        source="static-analysis",
        source_version="1.2",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id="a" * 64,
        limitations=("static result",),
    )


def _opinion(blocks, *, status=AnalystOpinionStatus.DRAFT, reviewer_id=None):
    return AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256="2" * 64,
        blocks=tuple(blocks),
        unresolved_points=(),
        author_id="analyst:author",
        reviewer_id=reviewer_id,
        status=status,
    )


def test_single_block_opinion_hash_is_canonical_and_reproducible() -> None:
    investigation_id = uuid4()
    observation = _observation(investigation_id)
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The sample contains a second-stage loader.",
        observation_refs=(observation.id,),
    )
    block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text="The analyzed sample contains a second-stage loader.",
        finding_refs=(finding.id,),
        evidence_refs=(observation.id,),
        qualification=OpinionClaimQualification.FACT,
        certainty=0.91,
    )
    opinion = _opinion((block,))
    repeated = AnalystOpinionV1(
        id=opinion.id,
        version=opinion.version,
        investigation_id=opinion.investigation_id,
        synthesis_artifact_id=opinion.synthesis_artifact_id,
        synthesis_sha256=opinion.synthesis_sha256,
        base_publication_artifact_id=opinion.base_publication_artifact_id,
        base_publication_sha256=opinion.base_publication_sha256,
        blocks=opinion.blocks,
        unresolved_points=opinion.unresolved_points,
        author_id=opinion.author_id,
        reviewer_id=opinion.reviewer_id,
        status=opinion.status,
        created_at=opinion.created_at,
    )
    assert opinion.content_sha256 == hashlib.sha256(opinion.canonical_bytes()).hexdigest()
    assert opinion.content_sha256 == repeated.content_sha256
    assert len(opinion.renderable_blocks) == 1
    assert verify_opinion_citations(opinion, (finding,), (observation,)) == ()


def test_legacy_opinion_hash_omits_empty_protocol_figures() -> None:
    opinion = AnalystOpinionV1(
        id=UUID(int=1),
        version=1,
        investigation_id=UUID(int=2),
        synthesis_artifact_id=UUID(int=3),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=UUID(int=4),
        base_publication_sha256="2" * 64,
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="The analyzed sample contains a second-stage loader.",
                finding_refs=(UUID(int=5),),
                evidence_refs=(UUID(int=6),),
                qualification=OpinionClaimQualification.FACT,
                certainty=0.91,
            ),
        ),
        unresolved_points=(),
        author_id="analyst:author",
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )

    assert "protocol_figures" not in opinion.content_payload()
    assert opinion.content_sha256 == (
        "2a0b3840fec56e859518abb1b2639f11454dc8d285fc0f288d635d24b62fd6f1"
    )


def test_nonexistent_citations_are_reported_for_factual_blocks() -> None:
    opinion = _opinion(
        (
            OpinionBlock(
                block_type=OpinionBlockType.CLAIM_VERIFICATION,
                text="The article's C1 is confirmed.",
                finding_refs=(uuid4(),),
                evidence_refs=(uuid4(),),
                qualification=OpinionClaimQualification.FACT,
            ),
        )
    )
    violations = verify_opinion_citations(opinion, (), ())
    codes = {violation.code for violation in violations}
    assert CitationViolationCode.FINDING_NOT_FOUND in codes
    assert CitationViolationCode.OBSERVATION_NOT_FOUND in codes
    assert CitationViolationCode.FACT_WITHOUT_SUPPORTED_FINDING in codes


def test_factual_block_requires_supported_finding_with_existing_observation() -> None:
    investigation_id = uuid4()
    observation = _observation(investigation_id)
    refuted = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.REFUTED,
        statement="The article's claimed domain does not appear in the reports.",
        observation_refs=(observation.id,),
    )
    opinion = _opinion(
        (
            OpinionBlock(
                block_type=OpinionBlockType.CLAIM_VERIFICATION,
                text="The attribution is contradicted.",
                finding_refs=(refuted.id,),
                evidence_refs=(observation.id,),
            ),
        )
    )
    assert any(
        issue.code is CitationViolationCode.FACT_WITHOUT_SUPPORTED_FINDING
        for issue in verify_opinion_citations(opinion, (refuted,), (observation,))
    )


def test_empty_blocks_are_not_rendered_and_only_one_populated_block_is_valid() -> None:
    opinion = _opinion(
        (
            OpinionBlock(block_type=OpinionBlockType.ADDITIONAL_SAMPLES, text="  "),
            OpinionBlock(
                block_type=OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
                text="The sample set is small.",
                qualification=OpinionClaimQualification.HYPOTHESIS,
                certainty=0.3,
            ),
        )
    )
    assert len(opinion.blocks) == 2
    assert len(opinion.renderable_blocks) == 1


def test_opinion_display_normalization_never_changes_the_persisted_hashed_text() -> None:
    raw_claim = (
        "Le chargement du projet pouvait amorcer l'exécution.: l'affirmation doit être nuancée."
    )
    raw_reserves = (
        "• Observation mesurée.\n"
        "  Réserves : donnée datée.; aucun lien établi.; mesure non qualifiée.."
    )
    claim = OpinionBlock(block_type=OpinionBlockType.CLAIM_VERIFICATION, text=raw_claim)
    reserves = OpinionBlock(block_type=OpinionBlockType.KEY_FINDINGS, text=raw_reserves)

    # The domain object keeps the exact text that content_sha256 covers.
    assert claim.text == raw_claim
    assert reserves.text == raw_reserves

    shown_claim = normalize_opinion_text(claim.block_type, claim.text)
    shown_reserves = normalize_opinion_text(reserves.block_type, reserves.text)
    assert shown_claim == (
        "Le chargement du projet pouvait amorcer l'exécution. L'affirmation doit être nuancée."
    )
    assert ".." not in shown_reserves
    assert ";." not in shown_reserves
    assert "donnée datée; aucun lien établi; mesure non qualifiée." in shown_reserves


def test_reviewed_opinion_requires_reviewer_identity_and_valid_hash() -> None:
    with pytest.raises(ValueError, match="reviewer identity"):
        _opinion((), status=AnalystOpinionStatus.APPROVED)

    draft = _opinion(())
    with pytest.raises(ValueError, match="content_sha256"):
        AnalystOpinionV1(
            investigation_id=draft.investigation_id,
            synthesis_artifact_id=draft.synthesis_artifact_id,
            synthesis_sha256=draft.synthesis_sha256,
            base_publication_artifact_id=draft.base_publication_artifact_id,
            base_publication_sha256=draft.base_publication_sha256,
            blocks=(),
            author_id=draft.author_id,
            content_sha256="0" * 64,
        )


def test_analyst_observation_projection_is_versioned_and_requires_citations() -> None:
    investigation_id = uuid4()
    observation = _observation(investigation_id)
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.SUPPORTED,
        statement="A preserved certificate lists both infrastructure domains.",
        observation_refs=(observation.id,),
    )
    note = AnalystObservationV1(
        id=uuid4(),
        anchor="section:1:paragraph:0001",
        text="The shared certificate links the two domains, but does not identify an operator.",
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(finding.id,),
        evidence_refs=(observation.id,),
    )
    blocks = opinion_blocks_with_observations(
        (
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="The report identifies two domains.",
                finding_refs=(finding.id,),
                evidence_refs=(observation.id,),
                qualification=OpinionClaimQualification.FACT,
            ),
        ),
        (note,),
    )
    opinion = _opinion(blocks)

    assert opinion.observations == (note,)
    assert len(opinion.renderable_blocks) == 1
    assert opinion.to_payload()["schema"] == "analyst-opinion.v1"
    assert verify_opinion_citations(opinion, (finding,), (observation,)) == ()

    with pytest.raises(ValueError, match="evidence references"):
        AnalystObservationV1(
            id=uuid4(),
            anchor="section:1:paragraph:0001",
            text="A factual observation without a source.",
            state=AnalystObservationState.OBSERVED,
            relation=AnalystObservationRelation.CONFIRMS,
        )
