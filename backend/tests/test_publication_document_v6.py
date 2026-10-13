from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    augment_publication_with_opinion,
)
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6BaseMismatchError,
    publication_document_from_json,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from tests.test_publication_builder_v4 import _build_v4, _canonical_inputs, _enrichment_with


def _base_v5() -> PublicationDocumentV5:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    return build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )


def _approved_opinion(
    base: PublicationDocumentV5,
) -> tuple[AnalystOpinionV1, OpinionCitationContext]:
    investigation_id = UUID(int=901)
    finding_id = UUID(int=902)
    observation_id = UUID(int=903)
    artifact_id = UUID(int=904)
    base_sha256 = publication_document_v5_sha256(base)
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=UUID(int=905),
        sha256="a" * 64,
        source="fixture",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="claim",
        object_id="claim-1",
        id=observation_id,
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.SUPPORTED,
        statement="The cited claim is supported.",
        observation_refs=(observation_id,),
        id=finding_id,
    )
    opinion = AnalystOpinionV1(
        id=UUID(int=906),
        version=2,
        investigation_id=investigation_id,
        synthesis_artifact_id=UUID(int=907),
        synthesis_sha256="b" * 64,
        base_publication_artifact_id=artifact_id,
        base_publication_sha256=base_sha256,
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="Le constat supplémentaire est corroboré.",
                finding_refs=(finding_id,),
                evidence_refs=(observation_id,),
                qualification=OpinionClaimQualification.FACT,
                certainty=0.9,
            ),
        ),
        author_id="analyst-1",
        reviewer_id="reviewer-1",
        status=AnalystOpinionStatus.APPROVED,
    )
    context = OpinionCitationContext(
        base_publication_artifact_id=artifact_id,
        base_publication_sha256=base_sha256,
        investigation_id=investigation_id,
        approver_id="approver-1",
        findings=(finding,),
        observations=(observation,),
        tlp_violation=False,
        do_not_submit=False,
    )
    return opinion, context


def test_v6_round_trip_keeps_exact_v5_wrapper_and_opinion_provenance() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    base_bytes = serialize_publication_document(base)

    v6 = augment_publication_with_opinion(base, opinion, context)
    payload = serialize_publication_document(v6)
    parsed = publication_document_from_json(payload)

    assert isinstance(parsed, PublicationDocumentV6)
    assert parsed.base_publication == base
    assert parsed.provenance.base_publication_artifact_id == context.base_publication_artifact_id
    assert parsed.provenance.base_publication_sha256 == context.base_publication_sha256
    assert parsed.provenance.opinion_id == opinion.id
    assert parsed.provenance.opinion_version == opinion.version
    assert parsed.provenance.reviewer_id == opinion.reviewer_id
    assert parsed.provenance.approver_id == context.approver_id
    assert parsed.rendered_opinion is not None
    assert parsed.rendered_opinion.blocks[0].qualification is OpinionClaimQualification.FACT
    assert serialize_publication_document(base) == base_bytes
    assert payload["schema_version"] == "6"
    assert payload["base_publication"]["schema_version"] == PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION


def test_v6_rejects_a_base_hash_that_does_not_match_the_embedded_v5() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    v6 = augment_publication_with_opinion(base, opinion, context)
    with pytest.raises(PublicationDocumentV6BaseMismatchError):
        replace(
            v6,
            provenance=replace(v6.provenance, base_publication_sha256="f" * 64),
        )


def test_v6_rejects_a_v4_base_and_v4_v5_schema_versions_remain_readable() -> None:
    base = _base_v5()
    payload = serialize_publication_document(base)
    assert payload["schema_version"] == PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION
    assert isinstance(publication_document_from_json(payload), PublicationDocumentV5)

    v4_payload = serialize_publication_document(_build_v4())
    assert v4_payload["schema_version"] == PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION
    assert isinstance(publication_document_from_json(v4_payload), PublicationDocumentV4)
