from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial import EditorialGroupingService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import CandidateTopic, IocPresence, SourceCandidate, SourceRole
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import EditorialGroupStatus
from tests.editorial_support import InMemoryEditorialUnitOfWorkFactory


def _edition() -> Edition:
    return Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_articles=6,
        previous_edition_id=None,
        source_profile="iran-default",
    )


def _candidate(title: str, url: str) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary="Publication technique décrivant une campagne.",
        novelty="Nouveau rapport technique",
        technical_potential=4,
        event_date=date(2026, 8, 10),
        uncertainties=("Relations de sources non vérifiées",),
        relevance_reasons=("Artefacts techniques",),
        actors=("MuddyWater",),
        campaigns=("Example Campaign",),
        malware=("ExampleRAT",),
        cves=(),
        victims=("administration",),
        sectors=("gouvernement",),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        iocs=(),
        sources=[
            SourceCandidate(
                url=url,
                title=title,
                publisher="Vendor Research",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def _snapshot(
    edition_id: UUID,
    subjects: list[tuple[UUID, CandidateTopic, tuple[DiscoveryMemberReference, ...]]],
    *,
    version: int = 1,
) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        id=uuid4(),
        edition_id=edition_id,
        version=version,
        parent_snapshot_id=None,
        intake_id=uuid4(),
        merge_run_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.CHATGPT,
        subjects=tuple(
            DiscoverySubject(
                subject_id=subject_id,
                candidate=candidate,
                member_references=references,
                created_at=datetime.now(UTC),
            )
            for subject_id, candidate, references in subjects
        ),
        snapshot_hash="a" * 64,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_editorial_synchronization_requires_an_active_snapshot() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition

    groups = await EditorialGroupingService(uow).synchronize(edition.id)

    assert groups == []


@pytest.mark.asyncio
async def test_active_snapshot_creates_one_group_per_durable_subject() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    first = _candidate("Campaign A", "https://example.test/a")
    second = _candidate("Campaign B", "https://example.test/b")
    first_id, second_id = uuid4(), uuid4()
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [
            (
                first_id,
                first,
                (DiscoveryMemberReference(uuid4(), first.id),),
            ),
            (
                second_id,
                second,
                (DiscoveryMemberReference(uuid4(), second.id),),
            ),
        ],
    )

    groups = await EditorialGroupingService(uow).synchronize(edition.id)

    assert {group.discovery_subject_id for group in groups} == {first_id, second_id}
    assert all(len(group.candidate_references) == 1 for group in groups)
    assert all(group.status is EditorialGroupStatus.PROPOSED for group in groups)


@pytest.mark.asyncio
async def test_ioc_signal_auto_selects_article_even_above_target_and_audits_once() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    edition.target_articles = 0
    uow.editions[edition.id] = edition
    candidate = _candidate("IOC campaign", "https://example.test/ioc")
    candidate.iocs = ("203.0.113.10",)
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [(uuid4(), candidate, (DiscoveryMemberReference(uuid4(), candidate.id),))],
    )
    service = EditorialGroupingService(uow)

    first = await service.synchronize(edition.id)
    second = await service.synchronize(edition.id)

    assert first[0].status is EditorialGroupStatus.SELECTED
    assert second[0].id == first[0].id
    assert second[0].subject_id == first[0].subject_id
    assert len(uow.subjects) == 1
    assert len(uow.decisions) == 1
    decision = uow.decisions[0]
    assert decision.actor_id == "system:editorial-auto-selection"
    assert decision.payload["automatic"] is True
    assert decision.payload["rule"] == "ioc_signal_v1"
    assert decision.payload["policy_version"] == 1


@pytest.mark.asyncio
async def test_source_ioc_signal_auto_selects_article() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    candidate = _candidate("Source IOC campaign", "https://example.test/source-ioc")
    candidate.sources[0].ioc_presence = IocPresence.VISIBLE
    candidate.sources[0].ioc_visible_count = 1
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [(uuid4(), candidate, (DiscoveryMemberReference(uuid4(), candidate.id),))],
    )

    groups = await EditorialGroupingService(uow).synchronize(edition.id)

    assert groups[0].status is EditorialGroupStatus.SELECTED


@pytest.mark.asyncio
async def test_snapshot_enrichment_keeps_selected_editorial_group_and_subject() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    subject_id = uuid4()
    first = _candidate("Canonical title", "https://example.test/a")
    first_ref = DiscoveryMemberReference(uuid4(), first.id)
    uow.snapshots[edition.id] = _snapshot(edition.id, [(subject_id, first, (first_ref,))])
    service = EditorialGroupingService(uow)
    group = (await service.synchronize(edition.id))[0]
    selected = await service.select(
        edition.id,
        group.id,
        actor_id="analyst",
        correlation_id="test",
    )
    editorial_subject_id = selected.subject_id

    update = _candidate("Renamed incoming title", "https://example.test/b")
    second_ref = DiscoveryMemberReference(uuid4(), update.id)
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [(subject_id, first, (first_ref, second_ref))],
        version=2,
    )
    groups = await service.synchronize(edition.id)

    assert len(groups) == 1
    assert groups[0].status is EditorialGroupStatus.SELECTED
    assert groups[0].subject_id == editorial_subject_id
    assert groups[0].discovery_subject_id == subject_id
    assert len(groups[0].candidate_references) == 2


@pytest.mark.asyncio
async def test_repeated_snapshot_synchronization_is_idempotent() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    candidate = _candidate("Campaign A", "https://example.test/a")
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [
            (
                uuid4(),
                candidate,
                (DiscoveryMemberReference(uuid4(), candidate.id),),
            )
        ],
    )
    service = EditorialGroupingService(uow)

    first = await service.synchronize(edition.id)
    second = await service.synchronize(edition.id)

    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id


@pytest.mark.asyncio
async def test_selection_survives_workspace_projection_failure() -> None:
    class FailingWorkspace:
        async def materialize(self, *args: object) -> None:
            del args
            raise OSError("workspace unavailable")

    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    candidate = _candidate("Workspace failure", "https://example.test/failure")
    candidate.iocs = ("203.0.113.10",)
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [(uuid4(), candidate, (DiscoveryMemberReference(uuid4(), candidate.id),))],
    )

    service = EditorialGroupingService(uow, materializer=FailingWorkspace())
    groups = await service.synchronize(edition.id)

    assert groups[0].status is EditorialGroupStatus.SELECTED
    assert groups[0].subject_id in uow.subjects
    assert len(uow.decisions) == 1
