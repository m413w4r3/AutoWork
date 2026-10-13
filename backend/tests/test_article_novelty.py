from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.article_novelty import (
    ArticleNoveltyService,
    NoveltyCandidate,
    NoveltyCandidateKind,
    NoveltyReasonCode,
)
from cti_app.application.investigation_contracts import ArticleClaimV1, ArticleContextV1
from cti_app.application.investigation_persistence import InMemoryEvidenceObservationRepository
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    NoveltyClass,
)


def _snapshot(
    *,
    documented_samples: tuple[str, ...] = (),
    known_chain: tuple[dict[str, object], ...] = (),
    claims: tuple[ArticleClaimV1, ...] = (),
) -> ArticleSnapshot:
    return ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="A loader campaign communicates with a command server.",
            claims=claims,
            documented_samples=documented_samples,
            known_chain=known_chain,
            open_questions=(),
        ),
    )


def _observation(investigation_id, marker: str) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(marker.encode()).hexdigest(),
        source="local.analysis",
        source_version="1",
        observed_at=datetime(2026, 10, 13, tzinfo=UTC),
        object_type="file",
        object_id="e" * 64,
    )


class _SnapshotPort:
    def __init__(self, snapshot: ArticleSnapshot) -> None:
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id):
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(self, investigation_id, claim_ids=()):
        claims = self.snapshot.article.claims
        return claims if not claim_ids else tuple(item for item in claims if item.id in claim_ids)


class _InvestigationPort:
    def __init__(self, investigation_id, snapshot: ArticleSnapshot) -> None:
        self.investigation_id = investigation_id
        self.snapshot = snapshot

    async def get(self, investigation_id):
        if investigation_id != self.investigation_id:
            return None
        return SimpleNamespace(
            id=investigation_id,
            synthesis_artifact_id=self.snapshot.synthesis_artifact_id,
        )


def _service(observations, investigation_id, snapshot):
    return ArticleNoveltyService(
        observations=observations,
        investigations=_InvestigationPort(investigation_id, snapshot),
        article_snapshots=_SnapshotPort(snapshot),
    )


@pytest.mark.asyncio
async def test_known_sample_hash_is_never_new_to_article() -> None:
    investigation_id = uuid4()
    sample_hash = "0123456789abcdef" * 2
    snapshot = _snapshot(documented_samples=(sample_hash,))
    observations = InMemoryEvidenceObservationRepository()
    observation = _observation(investigation_id, "known hash evidence")
    await observations.append_if_absent(observation)

    result = await _service(observations, investigation_id, snapshot).classify(
        investigation_id=investigation_id,
        candidate=NoveltyCandidate(
            investigation_id=investigation_id,
            kind=NoveltyCandidateKind.SAMPLE,
            value=sample_hash.upper(),
            object_type="file",
            object_id=sample_hash,
            observation_refs=(observation.id,),
        ),
    )

    assert result.novelty is NoveltyClass.NOT_CONFIRMED
    assert result.reason.code is NoveltyReasonCode.ALREADY_DOCUMENTED
    assert result.reason.matched_article_identifiers == (f"sample:md5:{sample_hash}",)
    assert "absence mondiale" in result.scope


@pytest.mark.asyncio
async def test_hashes_are_compared_only_within_their_family_and_absence_is_scoped() -> None:
    investigation_id = uuid4()
    article_md5 = "a" * 32
    candidate_sha256 = "b" * 64
    snapshot = _snapshot(documented_samples=(article_md5,))
    observations = InMemoryEvidenceObservationRepository()
    observation = _observation(investigation_id, "new sha256 candidate")
    await observations.append_if_absent(observation)

    service = _service(observations, investigation_id, snapshot)
    result = await service.classify(
        investigation_id=investigation_id,
        candidate=NoveltyCandidate(
            investigation_id=investigation_id,
            kind=NoveltyCandidateKind.SAMPLE,
            value=candidate_sha256,
            object_type="file",
            object_id=candidate_sha256,
            observation_refs=(observation.id,),
        ),
    )
    unresolved = await service.classify(
        investigation_id=investigation_id,
        candidate=NoveltyCandidate(
            investigation_id=investigation_id,
            kind=NoveltyCandidateKind.IOC,
            value="new.example.invalid",
            object_type="domain",
            object_id="new.example.invalid",
        ),
    )

    assert result.novelty is NoveltyClass.NEW_TO_ARTICLE
    assert result.reason.code is NoveltyReasonCode.NEW_ARTICLE_ITEM
    assert "absence mondiale" in result.limitations[0]
    assert unresolved.novelty is NoveltyClass.NOT_CONFIRMED
    assert unresolved.reason.code is NoveltyReasonCode.EVIDENCE_REQUIRED


@pytest.mark.asyncio
async def test_claim_correction_requires_snapshot_claim_and_same_investigation_evidence() -> None:
    investigation_id = uuid4()
    claim = ArticleClaimV1(
        id="C1",
        statement="The article says the sample contacts 192.0.2.10.",
        source_refs=("source:1",),
    )
    snapshot = _snapshot(claims=(claim,))
    observations = InMemoryEvidenceObservationRepository()
    observation = _observation(investigation_id, "contradictory evidence")
    await observations.append_if_absent(observation)
    service = _service(observations, investigation_id, snapshot)

    correction = await service.classify(
        investigation_id=investigation_id,
        candidate=NoveltyCandidate(
            investigation_id=investigation_id,
            kind=NoveltyCandidateKind.CLAIM,
            value="The observed endpoint differs from the article claim.",
            object_type="claim",
            object_id=claim.id,
            claim_id=claim.id,
            claim_assessment=FindingAssessment.REFUTED,
            observation_refs=(observation.id,),
        ),
    )

    assert correction.novelty is NoveltyClass.CLAIM_CORRECTION
    assert correction.reason.code is NoveltyReasonCode.CLAIM_CORRECTION
    assert f"claim:{claim.id}" in correction.compared_ids

    no_proof = await service.classify(
        investigation_id=investigation_id,
        candidate=NoveltyCandidate(
            investigation_id=investigation_id,
            kind=NoveltyCandidateKind.CLAIM,
            value="Bridge marked this claim refuted.",
            object_type="claim",
            object_id=claim.id,
            claim_id=claim.id,
            claim_assessment=FindingAssessment.REFUTED,
        ),
    )
    assert no_proof.novelty is NoveltyClass.NOT_CONFIRMED
    assert no_proof.reason.code is NoveltyReasonCode.EVIDENCE_REQUIRED

    with pytest.raises(ValueError, match="not in the selected article snapshot"):
        await service.classify(
            investigation_id=investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=investigation_id,
                kind=NoveltyCandidateKind.CLAIM,
                value="Unknown claim.",
                object_type="claim",
                object_id="C-unknown",
                claim_id="C-unknown",
                claim_assessment=FindingAssessment.REFUTED,
                observation_refs=(observation.id,),
            ),
        )


@pytest.mark.asyncio
async def test_novelty_checks_investigation_id_on_candidate_and_evidence() -> None:
    investigation_id = uuid4()
    snapshot = _snapshot()
    observations = InMemoryEvidenceObservationRepository()
    foreign_observation = _observation(uuid4(), "foreign investigation")
    await observations.append_if_absent(foreign_observation)
    service = _service(observations, investigation_id, snapshot)

    with pytest.raises(ValueError, match="another investigation"):
        await service.classify(
            investigation_id=investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=investigation_id,
                kind=NoveltyCandidateKind.BEHAVIOUR,
                value="creates a scheduled task",
                object_type="behaviour",
                object_id="behaviour-1",
                observation_refs=(foreign_observation.id,),
            ),
        )
    with pytest.raises(ValueError, match="another investigation"):
        await service.classify(
            investigation_id=investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=uuid4(),
                kind=NoveltyCandidateKind.BEHAVIOUR,
                value="creates a scheduled task",
                object_type="behaviour",
                object_id="behaviour-1",
            ),
        )
