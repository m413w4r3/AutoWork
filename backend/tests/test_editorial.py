from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.application.editorial import (
    AmbiguousGroupingResult,
    EditorialActionError,
    EditorialDecisionCommand,
    EditorialDecisionValue,
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
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecisionType,
)
from cti_app.domain.entities import Subject
from tests.editorial_support import InMemoryEditorialUnitOfWorkFactory

IRAN_REPORT = (Path(__file__).parent / "fixtures/chatgpt_iran_2026_08_escaped.md").read_text()


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
        self.source_document_inputs: list[object] = []

    async def materialize(
        self,
        subject: Subject,
        source_documents: object,
        samples: object,
        blobs: object,
        workspace_root: Path,
    ) -> object:
        self.subjects.append(subject)
        self.source_document_inputs.append(source_documents)
        return workspace_root / subject.slug


class RecordingStructuredModel:
    def __init__(self) -> None:
        self.calls: list[object] = []
        # Allow tests to override the return value
        self.return_result: AmbiguousGroupingResult | None = None

    async def extract(self, request: object, output_schema: object) -> object:
        self.calls.append(request)
        result = self.return_result or AmbiguousGroupingResult(
            decision="separate",
            confidence=GroupingConfidence.LOW,
            justification="Similarité insuffisante après comparaison structurée.",
        )
        return SimpleNamespace(structured_output=result)


async def test_same_batch_candidates_never_merge_on_same_canonical_url() -> None:
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

    assert len(groups) == 2
    assert all(len(group.candidate_references) == 1 for group in groups)
    assert all(
        group.source_relationship_status is SourceRelationshipStatus.PROVISIONAL for group in groups
    )
    assert all(group.needs_source_verification for group in groups)


async def test_real_iran_report_creates_five_groups_despite_shared_kaspersky_relay() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition(month=8)
    uow.editions[edition.id] = edition
    parsed = parse_discovery_report(
        IRAN_REPORT,
        visible_citations=[],
        period_start=edition.period_start,
        period_end=edition.period_end,
        tlp=edition.tlp,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    batch = _batch(edition.id, parsed.candidates)
    uow.batches[batch.id] = batch

    groups = await EditorialGroupingService(uow, None).synchronize(edition.id)

    assert len(groups) == 5
    assert all(len(group.candidate_references) == 1 for group in groups)
    assert (
        sum(
            "Kaspersky" in source.publisher
            for candidate in parsed.candidates
            for source in candidate.sources
        )
        == 5
    )


async def test_shared_relay_across_batches_is_not_identity_evidence() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    first = _candidate("Cyber Isnaad Front", "https://summary.example/quarterly")
    first.sources[0].role = SourceRole.RELAY
    second = _candidate("Nimbus Manticore", "https://summary.example/quarterly")
    second.sources[0].role = SourceRole.RELAY
    first_batch = _batch(edition.id, [first])
    second_batch = _batch(edition.id, [second])
    uow.batches.update({first_batch.id: first_batch, second_batch.id: second_batch})

    groups = await EditorialGroupingService(uow, None).synchronize(edition.id)

    assert len(groups) == 2


async def test_shared_primary_url_requires_another_editorial_signal() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    first = _candidate("Cyber Isnaad Front", "https://vendor.example/report")
    second = _candidate("Activité visant des PLC", "https://vendor.example/report")
    for candidate in (first, second):
        candidate.actors = ()
        candidate.campaigns = ()
        candidate.malware = ()
    first_batch = _batch(edition.id, [first])
    second_batch = _batch(edition.id, [second])
    uow.batches.update({first_batch.id: first_batch, second_batch.id: second_batch})

    groups = await EditorialGroupingService(uow, None).synchronize(edition.id)

    assert len(groups) == 2


async def test_shared_primary_url_and_explicit_actor_alias_can_merge_across_batches() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    first = _candidate("Opération de sideloading", "https://vendor.example/report")
    second = _candidate("Analyse technique distincte", "https://vendor.example/report")
    first.actors = ("Nimbus Manticore / UNC1549",)
    second.actors = ("UNC1549",)
    for candidate in (first, second):
        candidate.campaigns = ()
        candidate.malware = ()
    first_batch = _batch(edition.id, [first])
    second_batch = _batch(edition.id, [second])
    uow.batches.update({first_batch.id: first_batch, second_batch.id: second_batch})

    groups = await EditorialGroupingService(uow, None).synchronize(edition.id)

    assert len(groups) == 1
    assert len(groups[0].candidate_references) == 2


async def test_reprocessing_replaces_group_reference_without_duplicate() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    candidate = _candidate("Campagne MuddyWater", "https://vendor.example/report")
    previous = _batch(edition.id, [candidate])
    uow.batches[previous.id] = previous
    service = EditorialGroupingService(uow, None)
    original_group = (await service.synchronize(edition.id))[0]

    replacement = _batch(edition.id, [candidate])
    replacement.discovery_model_run_id = previous.discovery_model_run_id
    replacement.parsing_revision = 2
    replacement.supersedes_batch_id = previous.id
    previous.replaced_by_batch_id = replacement.id
    uow.batches.update({previous.id: previous, replacement.id: replacement})

    groups = await service.synchronize(edition.id)

    assert len(groups) == 1
    assert groups[0].id == original_group.id
    assert groups[0].candidate_references == (CandidateReference(replacement.id, candidate.id),)


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

    assert len(model.calls) == 0
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


async def test_new_publication_enriches_a_selected_subject_in_place() -> None:
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
    selected = await service.select(
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

    # §27.1 : le sujet déjà sélectionné absorbe la nouvelle contribution plutôt
    # que de faire apparaître un second sujet concurrent dans le chemin de fer.
    assert len(groups) == 1
    enriched = groups[0]
    assert enriched.id == first.id
    assert enriched.status is EditorialGroupStatus.SELECTED
    assert enriched.subject_id == selected.subject_id
    assert enriched.editorial_type is EditorialType.MAJOR
    assert len(enriched.candidate_references) == 2
    # Les nouvelles sources doivent être collectées et vérifiées, sans réécrire
    # le livrable déjà validé par l'analyste.
    assert enriched.needs_source_expansion is True
    assert enriched.needs_source_verification is True


async def test_rejected_group_is_not_resurrected_by_a_similar_candidate() -> None:
    """§27.2 : un rejet humain n'est jamais annulé automatiquement."""
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
    await service.decide_many(
        edition.id,
        [
            EditorialDecisionCommand(
                group_id=first.id,
                version=first.version,
                decision=EditorialDecisionValue.IGNORE,
            )
        ],
        actor_id="dev-analyst",
        correlation_id="rejection",
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

    rejected = next(group for group in groups if group.id == first.id)
    assert rejected.status is EditorialGroupStatus.REJECTED
    assert len(rejected.candidate_references) == 1
    # La nouvelle contribution existe, mais sous un sujet distinct à arbitrer.
    others = [group for group in groups if group.id != first.id]
    assert len(others) == 1
    assert others[0].status is EditorialGroupStatus.PROPOSED


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


async def test_bulk_decisions_create_two_ready_subjects_without_collection() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition
    batch = _batch(
        edition.id,
        [
            _candidate("Publication A", "https://a.example/report"),
            _candidate("Publication B", "https://b.example/report"),
            _candidate("Publication C", "https://c.example/report"),
        ],
    )
    uow.batches[batch.id] = batch
    materializer = RecordingMaterializer()
    service = EditorialGroupingService(uow, None, materializer=materializer)
    groups = await service.synchronize(edition.id)

    await service.decide_many(
        edition.id,
        (
            EditorialDecisionCommand(groups[0].id, groups[0].version, EditorialDecisionValue.BRIEF),
            EditorialDecisionCommand(groups[1].id, groups[1].version, EditorialDecisionValue.MAJOR),
            EditorialDecisionCommand(
                groups[2].id, groups[2].version, EditorialDecisionValue.IGNORE
            ),
        ),
        actor_id="dev-analyst",
        correlation_id="bulk-selection",
    )

    board = await service.board(edition.id)
    assert board.selected_briefs == 1
    assert board.selected_major == 1
    assert board.ignored == 1
    assert board.undecided == 0
    assert len(uow.subjects) == 2
    assert len(materializer.subjects) == 2
    assert materializer.source_document_inputs == [(), ()]
    assert sum(item.decision_type is HumanDecisionType.SELECT for item in uow.decisions) == 2
    assert sum(item.decision_type is HumanDecisionType.REJECT for item in uow.decisions) == 1
    # Selection only prepares empty workspaces; source collection has no entry point here.
    assert all(subject.id in uow.subjects for subject in materializer.subjects)


async def test_bulk_decisions_are_atomic_on_version_conflict() -> None:
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
    materializer = RecordingMaterializer()
    service = EditorialGroupingService(uow, None, materializer=materializer)
    groups = await service.synchronize(edition.id)
    original = {group.id: (group.status, group.version, group.subject_id) for group in groups}

    with pytest.raises(EditorialActionError, match="has changed"):
        await service.decide_many(
            edition.id,
            (
                EditorialDecisionCommand(
                    groups[0].id, groups[0].version, EditorialDecisionValue.BRIEF
                ),
                EditorialDecisionCommand(
                    groups[1].id, groups[1].version + 1, EditorialDecisionValue.IGNORE
                ),
            ),
            actor_id="dev-analyst",
            correlation_id="conflict",
        )

    assert {
        group_id: (group.status, group.version, group.subject_id)
        for group_id, group in uow.groups.items()
    } == original
    assert uow.subjects == {}
    assert uow.decisions == []
    assert materializer.subjects == []


async def test_ambiguous_llm_unavailable_never_merges() -> None:
    """Patch 1: LLM merge authority removed.

    When _structured_model is None (LLM unavailable), the outcome must stay
    AMBIGUOUS_REVIEW, and the candidate must NOT be added to any group
    (assert group membership unchanged).
    """
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition

    # Batch 1: Create an initial group
    batch1 = _batch(
        edition.id,
        [_candidate("Campaign alpha", "https://alpha.example/report")],
    )
    uow.batches[batch1.id] = batch1
    service = EditorialGroupingService(uow, None)  # No structured model
    groups = await service.synchronize(edition.id)
    assert len(groups) == 1
    original_group = groups[0]
    original_refs = original_group.candidate_references

    # Batch 2: Complementary batch with a similar but not identical candidate
    # (score should be 0.5-0.8 range -> AMBIGUOUS without LLM)
    batch2 = _batch(
        edition.id,
        [_candidate("Campaign similar", "https://similar.example/report")],  # Close title but different URL
    )
    uow.batches[batch2.id] = batch2

    # Synchronize without LLM (model=None)
    groups = await service.synchronize(edition.id)

    # Verify original group was NOT modified
    persisted_original = uow.groups[original_group.id]
    assert persisted_original.candidate_references == original_refs, (
        "When LLM is unavailable, AMBIGUOUS candidates must not be auto-merged"
    )

    # Verify a new group was created for the ambiguous candidate
    assert len(uow.groups) >= 2, "Ambiguous candidate without LLM must create new group"


async def test_ambiguous_llm_merge_verdict_does_not_structurally_merge() -> None:
    """Patch 1: LLM suggests merge, but structural merge is NOT applied.

    Even when the LLM returns decision='merge', the candidate must NOT be
    added to the existing group. Instead, outcome becomes AMBIGUOUS_REVIEW
    with the LLM's suggestion flagged for human decision.
    """
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    uow.editions[edition.id] = edition

    # Batch 1: Create an initial group
    batch1 = _batch(
        edition.id,
        [_candidate("Campaign alpha", "https://alpha.example/report")],
    )
    uow.batches[batch1.id] = batch1
    service = EditorialGroupingService(uow, None)
    groups = await service.synchronize(edition.id)
    original_group = groups[0]
    original_refs = original_group.candidate_references

    # Batch 2: Ambiguous-score candidate that triggers LLM
    batch2 = _batch(
        edition.id,
        [_candidate("Campaign similar", "https://similar.example/report")],
    )
    uow.batches[batch2.id] = batch2

    # Synchronize WITH a stub LLM that returns decision='merge'
    llm_stub = RecordingStructuredModel()
    # Override the recorded result to return 'merge'
    llm_stub.return_result = AmbiguousGroupingResult(
        decision="merge",
        confidence="high",
        justification="LLM thinks these should merge",
    )
    service = EditorialGroupingService(uow, llm_stub)
    groups = await service.synchronize(edition.id)

    # Verify original group was NOT modified (no structural merge applied)
    persisted_original = uow.groups[original_group.id]
    assert persisted_original.candidate_references == original_refs, (
        "LLM merge verdict must NOT cause structural merge in Patch 1"
    )

    # Verify a new AMBIGUOUS_REVIEW group was created instead
    from cti_app.domain.editorial import GroupingOutcome

    ambiguous_groups = [g for g in uow.groups.values() if g.outcome is GroupingOutcome.AMBIGUOUS_REVIEW]
    assert len(ambiguous_groups) >= 1, "LLM merge suggestion must create AMBIGUOUS_REVIEW group"

    # The AMBIGUOUS_REVIEW group should reference the original group as potential match
    ambiguous_group = ambiguous_groups[0]
    assert ambiguous_group.potential_historical_group_id == original_group.id, (
        "AMBIGUOUS_REVIEW must preserve the suggested group reference"
    )
