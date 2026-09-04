from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.discovery.manual_source_edits import (
    MANUAL_SOURCE_EDIT_VERSION,
    ManualSourceEditService,
    _build_manual_edit_batch,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)


def _candidate() -> CandidateTopic:
    return CandidateTopic(
        title="Candidate",
        summary="Summary.",
        novelty="Novel.",
        technical_potential=1,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def test_build_manual_edit_batch_uses_manual_source_edit_version() -> None:
    batch, _digest = _build_manual_edit_batch(
        edition_id=uuid4(),
        subject_id=uuid4(),
        incomplete_source_id=uuid4(),
        url="https://example.com/report",
        candidate=_candidate(),
    )

    assert MANUAL_SOURCE_EDIT_VERSION == "manual-url-attach-v1"
    assert batch.parser_version == "manual-url-attach-v1"


class _BatchRepository:
    def __init__(self, batches: list[DiscoveryBatch]) -> None:
        self.batches = batches

    async def get_by_request_hash(
        self, edition_id: object, request_hash: str
    ) -> DiscoveryBatch | None:
        return next(
            (
                batch
                for batch in self.batches
                if batch.edition_id == edition_id and batch.request_hash == request_hash
            ),
            None,
        )

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        if any(item.id == batch.id for item in self.batches):
            return False
        self.batches.append(batch)
        return True

    async def list_for_edition(self, edition_id: object) -> list[DiscoveryBatch]:
        return [batch for batch in self.batches if batch.edition_id == edition_id]


class _GroupRepository:
    def __init__(self, groups: list[EditorialGroup]) -> None:
        self.groups = groups

    async def get_by_subject(self, subject_id: object) -> EditorialGroup | None:
        return next((group for group in self.groups if group.subject_id == subject_id), None)

    async def save(self, group: EditorialGroup) -> None:
        del group


class _Uow:
    def __init__(
        self, batches: _BatchRepository, groups: _GroupRepository
    ) -> None:
        self.discovery_batches = batches
        self.editorial_groups = groups

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def commit(self) -> None:
        return None


class _Archive:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_manual_research_output(
        self,
        run_id: object,
        content: bytes,
        *,
        evidence_pack_hash: str,
        actor_id: str,
        operation: str,
    ) -> None:
        self.calls.append(
            {
                "run_id": run_id,
                "content": content,
                "evidence_pack_hash": evidence_pack_hash,
                "actor_id": actor_id,
                "operation": operation,
            }
        )


class _Cumulative:
    def __init__(self, snapshot: DiscoverySnapshot) -> None:
        self.snapshot = snapshot
        self.intake: DiscoveryIntake | None = None
        self.planner: object | None = None

    async def active_snapshot(self, edition_id: object) -> DiscoverySnapshot | None:
        if self.snapshot.edition_id != edition_id:
            return None
        return self.snapshot

    async def ingest_batch(
        self, batch: DiscoveryBatch, *, input_mode: DiscoveryInputMode, actor_id: str
    ) -> tuple[DiscoveryIntake, bool]:
        digest = hashlib.sha256(str(batch.id).encode()).hexdigest()
        self.intake = DiscoveryIntake(
            edition_id=batch.edition_id,
            sequence=1,
            input_mode=input_mode,
            raw_report_hash=digest,
            parsed_report_hash=digest,
            intake_hash=digest,
            research_model_run_id=batch.discovery_model_run_id,
            source_mode=DiscoverySourceMode.MANUAL_IMPORT,
            complementary_axis=batch.complementary_axis,
            batch_id=batch.id,
            created_by=actor_id,
        )
        return self.intake, False

    async def reconcile_intake(
        self,
        intake_id: object,
        *,
        expected_parent_snapshot_id: object,
        actor_id: str,
        planner_override: object,
    ) -> DiscoverySnapshot:
        del intake_id, expected_parent_snapshot_id, actor_id
        self.planner = planner_override
        return self.snapshot


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


def _score() -> EditorialScore:
    return EditorialScore(
        impact=3,
        novelty=3,
        technical_depth=3,
        hunting_potential=3,
        actionability=3,
        source_quality=3,
        justifications={},
    )


def _source(url: str, *, title: str = "Blocked report") -> SourceCandidate:
    return SourceCandidate(
        url=url,
        title=title,
        publisher="Research vendor",
        role=SourceRole.PRIMARY,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def _topic_candidate(
    source: SourceCandidate | None = None, *, title: str = "Candidate"
) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary="Summary.",
        novelty="Novel.",
        technical_potential=1,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[source] if source is not None else [],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


@pytest.mark.asyncio
async def test_replacement_archives_new_candidate_and_repoints_only_target() -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    other_subject_id = uuid4()
    old = _source("https://blocked.example/report")
    other_old = _source("https://blocked.example/report", title="Other report")
    candidate = _topic_candidate(old)
    other_candidate = _topic_candidate(other_old, title="Other candidate")
    old_batch = DiscoveryBatch(
        edition_id=edition_id,
        request_hash="b" * 64,
        complementary_axis="research",
        queries=(),
        citations=(),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test",
        candidates=[candidate, other_candidate],
        source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        source_coverage_complete=False,
        source_coverage_incomplete_reason="test",
    )
    group = EditorialGroup(
        edition_id=edition_id,
        title="Candidate",
        candidate_references=(CandidateReference(old_batch.id, candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=_score(),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(subject_id)
    other_group = EditorialGroup(
        edition_id=edition_id,
        title="Other candidate",
        candidate_references=(CandidateReference(old_batch.id, other_candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=_score(),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    other_group.select(other_subject_id)
    snapshot = DiscoverySnapshot(
        edition_id=edition_id,
        version=1,
        parent_snapshot_id=None,
        intake_id=uuid4(),
        merge_run_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.HEURISTIC,
        subjects=(
            DiscoverySubject(
                subject_id=subject_id,
                candidate=candidate,
                member_references=(DiscoveryMemberReference(old_batch.id, candidate.id),),
                created_at=datetime.now(UTC),
            ),
            DiscoverySubject(
                subject_id=other_subject_id,
                candidate=other_candidate,
                member_references=(
                    DiscoveryMemberReference(old_batch.id, other_candidate.id),
                ),
                created_at=datetime.now(UTC),
            ),
        ),
        snapshot_hash="a" * 64,
        is_active=True,
    )
    batches = _BatchRepository([old_batch])
    uow = _Uow(batches, _GroupRepository([group, other_group]))
    archive = _Archive()
    cumulative = _Cumulative(snapshot)
    service = ManualSourceEditService(_Factory(uow), archive, cumulative)  # type: ignore[arg-type]

    result = await service.attach_replacement_source_url(
        edition_id,
        subject_id,
        old.canonical_url,
        "https://mirror.example/report",
        actor_id="analyst-1",
    )

    assert result.updated_subject_ids == (subject_id,)
    assert result.promoted_source.canonical_url == "https://mirror.example/report"
    assert "url_replaced_manually" in result.promoted_source.parsing_warnings
    manual_batch = next(batch for batch in batches.batches if batch is not old_batch)
    manual_candidate = manual_batch.candidates[0]
    assert [source.canonical_url for source in manual_candidate.sources] == [
        "https://mirror.example/report"
    ]
    assert manual_candidate.local_ref == "manual-url-replace"
    assert old.canonical_url not in {source.canonical_url for source in manual_candidate.sources}
    assert group.candidate_references == (
        CandidateReference(manual_batch.id, manual_candidate.id),
    )
    assert other_group.candidate_references == (
        CandidateReference(old_batch.id, other_candidate.id),
    )
    assert other_candidate.sources[0].canonical_url == old.canonical_url
    assert archive.calls[0]["operation"] == "replace"
    assert old.canonical_url.encode() in archive.calls[0]["content"]  # type: ignore[operator]
