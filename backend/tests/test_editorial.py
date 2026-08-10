from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial import (
    AmbiguousGroupingResult,
    EditorialActionError,
    EditorialGroupingService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecisionType,
)
from cti_app.domain.entities import Subject
from tests.editorial_support import InMemoryEditorialUnitOfWorkFactory


def _edition(*, month: int = 7) -> Edition:
    return Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, month, 1),
        period_end=date(2026, month, calendar.monthrange(2026, month)[1]),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_major_articles=2,
        target_briefs=4,
        previous_edition_id=None,
        source_profile="iran-default",
    )


def _candidate(title: str, url: str, *, iocs: tuple[str, ...] = ()) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary="Publication technique décrivant une campagne et ses indicateurs.",
        novelty="Nouveau rapport technique",
        technical_potential=4,
        event_date=date(2026, 7, 10),
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
        iocs=iocs,
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


def _batch(edition_id: UUID, candidates: list[CandidateTopic]) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=uuid4().hex + uuid4().hex,
        complementary_axis="initial",
        queries=("query",),
        citations=({"label": "citation", "url": "https://vendor.example/report"},),
        candidates=candidates,
        discovery_model_run_id=uuid4(),
        structuring_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def _score() -> EditorialScore:
    return EditorialScore(2, 2, 2, 2, 2, 2, {"impact": "test"})


class RecordingMaterializer:
    def __init__(self) -> None:
        self.subjects: list[Subject] = []

    async def materialize(
        self,
        subject: Subject,
        source_documents: object,
        samples: object,
        blobs: object,
        workspace_root: Path,
    ) -> object:
        self.subjects.append(subject)
        return workspace_root / subject.slug


class RecordingStructuredModel:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def extract(self, request: object, output_schema: object) -> object:
        self.calls.append(request)
        return SimpleNamespace(
            structured_output=AmbiguousGroupingResult(
                decision="separate",
                confidence=GroupingConfidence.LOW,
                justification="Similarité insuffisante après comparaison structurée.",
            )
        )


async def test_deterministic_grouping_merges_same_canonical_url_without_attribution() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    batch = _batch(
        edition.id,
        [
            _candidate(
                "Campagne MuddyWater détaillée",
                "https://vendor.example/report?utm_source=test",
            ),
            _candidate("Nouveaux IOC de la campagne", "https://vendor.example/report"),
        ],
    )
    uow.batches[batch.id] = batch

    groups = await EditorialGroupingService(uow, None).synchronize(edition.id)

    assert len(groups) == 1
    assert len(groups[0].candidate_references) == 2
    assert groups[0].source_relationship_status is SourceRelationshipStatus.PROVISIONAL
    assert groups[0].needs_source_verification is True
    assert not hasattr(groups[0], "attribution_level")


async def test_previous_month_match_is_presented_as_update_not_filtered() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    previous = _edition(month=6)
    current = _edition(month=7)
    uow.editions.update({previous.id: previous, current.id: current})
    previous_batch = _batch(
        previous.id,
        [_candidate("Campagne MuddyWater", "https://vendor.example/june", iocs=("1.2.3.4",))],
    )
    current_batch = _batch(
        current.id,
        [
            _candidate(
                "Mise à jour campagne MuddyWater",
                "https://another.example/july",
                iocs=("1.2.3.4",),
            )
        ],
    )
    uow.batches.update({previous_batch.id: previous_batch, current_batch.id: current_batch})
    historical = EditorialGroup(
        edition_id=previous.id,
        title="Campagne MuddyWater",
        candidate_references=(
            CandidateReference(previous_batch.id, previous_batch.candidates[0].id),
        ),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=_score(),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="Sélection du mois précédent",
    )
    historical.select(EditorialType.MAJOR, uuid4())
    uow.groups[historical.id] = historical

    groups = await EditorialGroupingService(uow, None).synchronize(current.id)

    assert len(groups) == 1
    assert groups[0].outcome is GroupingOutcome.UPDATE_PREVIOUS
    assert groups[0].potential_historical_group_id == historical.id


async def test_structured_model_is_used_only_for_ambiguous_grouping() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    batch = _batch(
        edition.id,
        [
            _candidate("Campagne MuddyWater", "https://a.example/report"),
            _candidate("Rapport MuddyWater complémentaire", "https://b.example/report"),
        ],
    )
    uow.batches[batch.id] = batch
    model = RecordingStructuredModel()

    groups = await EditorialGroupingService(uow, model).synchronize(edition.id)  # type: ignore[arg-type]

    assert len(model.calls) == 1
    assert len(groups) == 2
    assert groups[1].outcome is GroupingOutcome.NEW_SUBJECT
    assert "attribution" not in groups[1].grouping_justification.casefold()


async def test_complementary_batch_enriches_group_without_duplicate_reference() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    initial = _batch(
        edition.id,
        [_candidate("Campagne MuddyWater", "https://a.example/report")],
    )
    uow.batches[initial.id] = initial
    service = EditorialGroupingService(uow, None)
    await service.synchronize(edition.id)
    complement = _batch(
        edition.id,
        [_candidate("Nouveaux IOC MuddyWater", "https://a.example/report")],
    )
    uow.batches[complement.id] = complement

    await service.synchronize(edition.id)
    groups = await service.synchronize(edition.id)

    assert len(groups) == 1
    assert len(groups[0].candidate_references) == 2
    assert len(set(groups[0].candidate_references)) == 2


async def test_match_against_selected_current_subject_remains_visible_for_review() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    initial = _batch(
        edition.id,
        [_candidate("Campagne MuddyWater", "https://a.example/report", iocs=("1.2.3.4",))],
    )
    uow.batches[initial.id] = initial
    service = EditorialGroupingService(uow, None)
    first = (await service.synchronize(edition.id))[0]
    await service.select(
        edition.id,
        first.id,
        EditorialType.MAJOR,
        actor_id="dev-analyst",
        correlation_id="first-selection",
    )
    complement = _batch(
        edition.id,
        [
            _candidate(
                "Mise à jour de la campagne MuddyWater",
                "https://b.example/report",
                iocs=("1.2.3.4",),
            )
        ],
    )
    uow.batches[complement.id] = complement

    groups = await service.synchronize(edition.id)

    assert len(groups) == 2
    proposed = next(group for group in groups if group.status.value == "proposed")
    assert proposed.outcome is GroupingOutcome.AMBIGUOUS_REVIEW
    assert proposed.potential_historical_group_id == first.id


async def test_merge_split_and_selection_append_new_human_decisions() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    batch = _batch(
        edition.id,
        [
            _candidate("Publication A", "https://a.example/report"),
            _candidate("Publication B", "https://b.example/report"),
        ],
    )
    uow.batches[batch.id] = batch
    service = EditorialGroupingService(uow, None)
    initial = await service.synchronize(edition.id)

    merged = await service.merge(
        edition.id,
        (initial[0].id, initial[1].id),
        actor_id="dev-analyst",
        correlation_id="merge",
    )
    split = await service.split(
        edition.id,
        merged.id,
        (merged.candidate_references[-1].candidate_id,),
        actor_id="dev-analyst",
        correlation_id="split",
    )
    materializer = RecordingMaterializer()
    selection = EditorialGroupingService(uow, None, materializer=materializer)
    selected = await selection.select(
        edition.id,
        split.id,
        EditorialType.BRIEF,
        actor_id="dev-analyst",
        correlation_id="select",
    )

    assert [item.decision_type for item in uow.decisions] == [
        HumanDecisionType.MERGE,
        HumanDecisionType.SPLIT,
        HumanDecisionType.SELECT,
    ]
    assert selected.subject_id in uow.subjects
    assert selected.editorial_type is EditorialType.BRIEF
    assert len(materializer.subjects) == 1
    assert uow.decisions[-1].payload["automatic"] is False


async def test_split_rejects_entire_request_when_one_candidate_is_foreign() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    batch = _batch(
        edition.id,
        [
            _candidate("Publication A", "https://a.example/report"),
            _candidate("Publication B", "https://b.example/report"),
        ],
    )
    uow.batches[batch.id] = batch
    service = EditorialGroupingService(uow, None)
    initial = await service.synchronize(edition.id)
    merged = await service.merge(
        edition.id,
        (initial[0].id, initial[1].id),
        actor_id="dev-analyst",
        correlation_id="merge-before-invalid-split",
    )
    original_references = merged.candidate_references
    group_count = len(uow.groups)
    decision_count = len(uow.decisions)

    with pytest.raises(
        EditorialActionError,
        match="Every requested split candidate must belong to the group",
    ):
        await service.split(
            edition.id,
            merged.id,
            (original_references[0].candidate_id, uuid4()),
            actor_id="dev-analyst",
            correlation_id="invalid-split",
        )

    persisted = uow.groups[merged.id]
    assert persisted.candidate_references == original_references
    assert len(uow.groups) == group_count
    assert len(uow.decisions) == decision_count
