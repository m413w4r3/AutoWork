from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.investigation_contracts import ArticleClaimV1
from cti_app.application.investigation_findings import (
    ClaimVerdict,
    FindingDowngradeReason,
    FindingService,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
)


class _SnapshotPort:
    def __init__(self, claims: tuple[ArticleClaimV1, ...]) -> None:
        self.claims = claims

    async def get_claims(self, investigation_id, claim_ids=()):
        if not claim_ids:
            return self.claims
        wanted = set(claim_ids)
        return tuple(item for item in self.claims if item.id in wanted)

    async def resolve(self, synthesis_artifact_id):
        raise AssertionError("FindingService reads claims by investigation identity")


def _observation(investigation_id, marker: str) -> EvidenceObservation:
    digest = hashlib.sha256(marker.encode()).hexdigest()
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.SHODAN_OBSERVATION,
        blob_id=uuid4(),
        sha256=digest,
        source="shodan.host",
        source_version="api-v1",
        observed_at=datetime(2026, 10, 13, tzinfo=UTC),
        object_type="ip",
        object_id="203.0.113.7",
        limitations=("Passive observation does not establish attribution.",),
    )


@pytest.mark.asyncio
async def test_claim_knowledge_keeps_favorable_and_contradictory_d3_evidence() -> None:
    investigation_id = uuid4()
    claim = ArticleClaimV1(
        id="C1",
        statement="The article attributes 203.0.113.7 to the campaign.",
        source_refs=("article:source-1",),
    )
    confirmed_claim = ArticleClaimV1(
        id="C2",
        statement="The article describes a loader stage.",
        source_refs=("article:source-2",),
    )
    observations = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    service = FindingService(
        findings=findings,
        observations=observations,
        article_snapshots=_SnapshotPort((claim, confirmed_claim)),
    )
    favorable = _observation(investigation_id, "favorable")
    contradictory = _observation(investigation_id, "contradictory")
    ignored = _observation(investigation_id, "not-linked-to-claim")
    for observation in (favorable, contradictory, ignored):
        await observations.append_if_absent(observation)

    supported = await service.propose(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.SUPPORTED,
        statement="The observation is consistent with the article attribution.",
        observation_refs=(favorable.id,),
        claim_refs=(claim.id,),
        novelty=NoveltyClass.NOT_CONFIRMED,
    )
    refuted = await service.propose(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.REFUTED,
        statement="A second source contradicts the article attribution.",
        observation_refs=(contradictory.id,),
        claim_refs=(claim.id,),
        limitations=("The two observations may cover different time windows.",),
    )
    confirmation = await service.propose(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.SUPPORTED,
        statement="The loader stage is supported by the local analysis.",
        observation_refs=(favorable.id,),
        claim_refs=(confirmed_claim.id,),
    )

    knowledge = await service.knowledge_of_claim(investigation_id, claim.id)

    assert knowledge.verdict is ClaimVerdict.QUALIFIED
    assert knowledge.claim_statement == claim.statement
    assert {item.id for item in knowledge.favorable_observations} == {favorable.id}
    assert {item.id for item in knowledge.contradictory_observations} == {contradictory.id}
    assert {item.id for item in knowledge.ignored_observations} == {ignored.id}
    assert knowledge.absence_of_evidence is False
    assert {item.id for item in knowledge.current_findings} == {supported.id, refuted.id}
    confirmed = await service.knowledge_of_claim(investigation_id, confirmed_claim.id)
    assert confirmation.assessment is FindingAssessment.SUPPORTED
    assert confirmed.verdict is ClaimVerdict.CONFIRMED
    assert {item.id for item in confirmed.favorable_observations} == {favorable.id}

    revised = await service.revise(
        refuted.id,
        assessment=FindingAssessment.QUALIFIED,
        statement="The second source narrows the attribution period.",
    )
    after_revision = await service.knowledge_of_claim(investigation_id, claim.id)
    assert revised.version == 2
    assert revised.supersedes == refuted.id
    assert len(after_revision.finding_versions) == 3
    assert any(item.id == refuted.id for item in after_revision.finding_versions)
    assert after_revision.verdict is ClaimVerdict.QUALIFIED


@pytest.mark.asyncio
async def test_bridge_supported_without_observation_is_downgraded_and_claim_refs_are_checked() -> (
    None
):
    investigation_id = uuid4()
    claim = ArticleClaimV1(id="C1", statement="The loader contacts one endpoint.", source_refs=())
    service = FindingService(
        findings=InMemoryFindingRepository(),
        observations=InMemoryEvidenceObservationRepository(),
        article_snapshots=_SnapshotPort((claim,)),
    )

    finding = await service.propose(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.SUPPORTED,
        statement="Bridge says the claim is confirmed.",
        claim_refs=(claim.id,),
    )
    knowledge = await service.knowledge_of_claim(investigation_id, claim.id)

    assert finding.assessment is FindingAssessment.INCONCLUSIVE
    assert FindingDowngradeReason.SUPPORTED_WITHOUT_OBSERVATION.value in finding.limitations
    assert knowledge.verdict is ClaimVerdict.NOT_ESTABLISHED
    assert knowledge.absence_of_evidence is True
    with pytest.raises(ValueError, match="not in the article snapshot"):
        await service.propose(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.SUPPORTED,
            statement="Unknown claim reference.",
            claim_refs=("C-unknown",),
        )


@pytest.mark.asyncio
async def test_finding_rejects_missing_or_cross_investigation_observations() -> None:
    investigation_id = uuid4()
    claim = ArticleClaimV1(id="C1", statement="A source statement.", source_refs=())
    observations = InMemoryEvidenceObservationRepository()
    service = FindingService(
        findings=InMemoryFindingRepository(),
        observations=observations,
        article_snapshots=_SnapshotPort((claim,)),
    )
    foreign = _observation(uuid4(), "foreign")
    await observations.append_if_absent(foreign)

    with pytest.raises(ValueError, match="another investigation"):
        await service.propose(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.SUPPORTED,
            statement="Cross-enquiry evidence is not a citation here.",
            observation_refs=(foreign.id,),
            claim_refs=(claim.id,),
        )
    with pytest.raises(ValueError, match="does not exist"):
        await service.propose(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.SUPPORTED,
            statement="Missing evidence is rejected.",
            observation_refs=(uuid4(),),
            claim_refs=(claim.id,),
        )
