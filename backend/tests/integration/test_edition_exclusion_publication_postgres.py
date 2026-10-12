"""PostgreSQL proof that an authentic editorial exclusion can be published.

Real repositories, real unit of work and the real Review / Composition /
Publication services.  No Review projection is built by hand: the Review is
read back from the persisted composition, exactly as the console reads it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from cti_app.application.edition_composition import (
    CompositionIdempotencyConflict,
    CompositionReferenceError,
    CompositionVersionConflict,
    EditionCompositionService,
)
from cti_app.application.edition_publication import (
    EditionPublicationService,
    PublicationAcceptanceError,
)
from cti_app.application.edition_review import EditionReviewService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionStage
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision
from cti_app.infrastructure.database.models.publication_review import (
    PublicationReviewDecisionRow,
)
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs

pytestmark = pytest.mark.integration

EXCLUSION_REASON = "Hors périmètre du bulletin de la période."


async def _decide_exclusion_of_b(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> tuple[Any, Any, dict[str, Subject], PublicationReviewDecision]:
    """A/B/C included at composition v1, then B excluded through Review.decide."""
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    review = await EditionReviewService(uow_factory).get(edition.id)
    item_b = next(item for item in review.items if item.subject_id == subjects["B"].id)
    assert item_b.document_artifact_id is not None
    decision = await EditionReviewService(uow_factory).decide(
        edition.id,
        subjects["B"].id,
        decision=PublicationDecision.EXCLUDE,
        production_run_id=item_b.run_id,
        pipeline_generation=item_b.pipeline_generation,
        document_artifact_id=item_b.document_artifact_id,
        document_artifact_version=item_b.document_artifact_version,
        document_input_hash=item_b.document_input_hash,
        actor_id="analyst",
        reason=EXCLUSION_REASON,
    )
    return edition, artifact_store, subjects, decision


async def _assert_abc_publication_with_excluded_b(
    uow_factory: UnitOfWorkFactory,
    artifact_store: Any,
    edition: Any,
    subjects: dict[str, Subject],
    decision: PublicationReviewDecision,
) -> None:
    review_service = EditionReviewService(uow_factory)
    composition_service = EditionCompositionService(uow_factory)
    composition = await composition_service.get(edition.id)
    assert composition.composition is not None
    dispositions = {
        entry.subject_id: entry.disposition for entry in composition.composition.entries
    }
    assert dispositions == {
        subjects["A"].id: CompositionDisposition.INCLUDED,
        subjects["B"].id: CompositionDisposition.EXCLUDED,
        subjects["C"].id: CompositionDisposition.INCLUDED,
    }

    # Review reads the real persisted composition.
    review = await review_service.get(edition.id)
    assert review.composition_version == composition.composition.version
    assert review.can_accept is True, [d.code for d in review.diagnostics]
    assert {item.subject_id for item in review.items if item.included} == {
        subjects["A"].id,
        subjects["C"].id,
    }
    assert len(review.exclusions) == 1
    exclusion = review.exclusions[0]
    assert exclusion.subject_id == subjects["B"].id
    assert exclusion.decision_id == decision.id
    assert exclusion.applicable is True
    assert exclusion.actor_id == "analyst"
    assert exclusion.reason == EXCLUSION_REASON

    # Composition validation agrees with Review.
    assert (await composition_service.validate(edition.id)).reasons == ()

    accepted = await EditionPublicationService(uow_factory, artifact_store).accept(
        edition.id,
        actor_id="analyst",
        expected_composition_version=composition.composition.version,
        correlation_id=f"accept-{edition.id}",
    )
    manifest = accepted.manifest
    assert manifest.schema_version == "2"
    assert {entry.subject_id for entry in manifest.entries} == {
        subjects["A"].id,
        subjects["C"].id,
    }
    assert [exclusion.subject_id for exclusion in manifest.exclusions] == [subjects["B"].id]
    assert manifest.exclusions[0].review_decision_id == decision.id
    async with uow_factory() as uow:
        persisted_manifest = await uow.publication_manifests.get(manifest.id)
        persisted_entries = await uow.publication_manifest_entries.list_for_manifest(manifest.id)
        persisted_exclusions = await uow.publication_manifest_exclusions.list_for_manifest(
            manifest.id
        )
    assert persisted_manifest is not None
    assert persisted_manifest.content_sha256 == manifest.content_sha256
    assert {entry.subject_id for entry in persisted_entries} == {
        subjects["A"].id,
        subjects["C"].id,
    }
    assert [(item.subject_id, item.review_decision_id) for item in persisted_exclusions] == [
        (subjects["B"].id, decision.id)
    ]

    # The persisted decision, not only the returned object, carries the audit.
    async with uow_factory() as uow:
        stored = await uow._session.scalar(
            select(PublicationReviewDecisionRow).where(
                PublicationReviewDecisionRow.id == decision.id
            )
        )
        assert stored is not None
        assert stored.subject_id == subjects["B"].id
        assert stored.decision == "exclude"
        assert stored.actor_id == "analyst"
        assert stored.reason == EXCLUSION_REASON


@pytest.mark.asyncio
async def test_two_step_exclusion_without_identity_pin_is_rejected(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """An exclusion decision for a document cannot authorize an unpinned entry."""
    edition, _artifact_store, subjects, decision = await _decide_exclusion_of_b(
        uow_factory, tmp_path
    )
    with pytest.raises(CompositionReferenceError) as error:
        await EditionCompositionService(uow_factory).exclude(
            edition.id,
            subjects["B"].id,
            decision.id,
            expected_version=1,
            idempotency_key=f"exclude-{uuid4()}",
            actor_id="analyst",
            correlation_id=f"exclude-{edition.id}",
        )
    assert error.value.code == "review_decision_identity_mismatch"


@pytest.mark.asyncio
async def test_exclude_subject_atomically_publishes_a_c_and_exclusion_b(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    composition, decision_id = await EditionCompositionService(uow_factory).exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=1,
        idempotency_key="exclude-b-atomic",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    assert composition.version == 2

    async with uow_factory() as uow:
        decision = next(
            item
            for item in await uow.publication_review_decisions.list_for_edition(edition.id)
            if item.id == decision_id
        )

    await _assert_abc_publication_with_excluded_b(
        uow_factory, artifact_store, edition, subjects, decision
    )


@pytest.mark.asyncio
async def test_exclude_selected_b_without_a_run_is_publishable(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "C"),
        composition_order=("A", "C"),
    )
    composition, decision_id = await EditionCompositionService(uow_factory).exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=1,
        idempotency_key="exclude-never-produced-b",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    assert composition.version == 2

    async with uow_factory() as uow:
        runs = await uow.production_runs.list_for_subject(subjects["B"].id)
        decision = next(
            item
            for item in await uow.publication_review_decisions.list_for_edition(edition.id)
            if item.id == decision_id
        )
    assert runs == []
    assert decision.production_run_id is None
    assert decision.pipeline_generation is None
    assert decision.document_artifact_id is None

    await _assert_abc_publication_with_excluded_b(
        uow_factory, artifact_store, edition, subjects, decision
    )


@pytest.mark.asyncio
async def test_deferred_subject_blocks_publication_as_incomplete(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    composition = await EditionCompositionService(uow_factory).defer(
        edition.id,
        subjects["B"].id,
        expected_version=1,
        idempotency_key="defer-b",
        actor_id="analyst",
        correlation_id=f"defer-{edition.id}",
    )
    with pytest.raises(PublicationAcceptanceError) as error:
        await EditionPublicationService(uow_factory, artifact_store).accept(
            edition.id,
            actor_id="analyst",
            expected_composition_version=composition.version,
            correlation_id=f"accept-{edition.id}",
        )
    assert error.value.code == "composition_incomplete"
    review = await EditionReviewService(uow_factory).get(edition.id)
    assert review.exclusions == ()
    assert any(item.code.value == "subject_deferred" for item in review.diagnostics)


@pytest.mark.asyncio
async def test_exclusion_is_blocked_after_run_retry_until_a_new_decision(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    composition_service = EditionCompositionService(uow_factory)
    initial = await composition_service.get(edition.id)
    assert initial.composition is not None
    run_id = next(
        entry.production_run_id
        for entry in initial.composition.entries
        if entry.subject_id == subjects["B"].id
    )
    assert run_id is not None
    composition, old_decision_id = await composition_service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=initial.composition.version,
        idempotency_key="exclude-b-before-retry",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )

    retry_service = SubjectProductionService(uow_factory)
    retried = await retry_service.retry_from_stage(run_id, ProductionStage.SOURCES)
    assert retried.run.pipeline_generation == 2
    stale_review = await EditionReviewService(uow_factory).get(edition.id)
    assert len(stale_review.exclusions) == 1
    assert stale_review.exclusions[0].decision_id == old_decision_id
    assert stale_review.exclusions[0].applicable is False
    assert any(
        diagnostic.code.value == "exclusion_decision_missing"
        and diagnostic.subject_id == subjects["B"].id
        for diagnostic in stale_review.diagnostics
    )
    with pytest.raises(PublicationAcceptanceError) as error:
        await EditionPublicationService(uow_factory, artifact_store).accept(
            edition.id,
            actor_id="analyst",
            expected_composition_version=composition.version,
            correlation_id=f"accept-{edition.id}",
        )
    assert error.value.code == "excluded_item_has_no_decision"

    await retry_service.mark_needs_review(run_id, "test_retry_finished", "Retry needs review")
    next_composition, new_decision_id = await composition_service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason="Nouvelle génération examinée, toujours hors périmètre.",
        expected_version=composition.version,
        idempotency_key="exclude-b-after-retry",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}-again",
    )
    assert new_decision_id != old_decision_id
    current_review = await EditionReviewService(uow_factory).get(edition.id)
    assert current_review.exclusions[0].applicable is True
    accepted = await EditionPublicationService(uow_factory, artifact_store).accept(
        edition.id,
        actor_id="analyst",
        expected_composition_version=next_composition.version,
        correlation_id=f"accept-{edition.id}-again",
    )
    assert accepted.manifest.exclusions[0].review_decision_id == new_decision_id


@pytest.mark.asyncio
async def test_stale_expected_version_does_not_append_an_exclusion_decision(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, _artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    service = EditionCompositionService(uow_factory)
    await service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=1,
        idempotency_key="exclude-b-version-winner",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    async with uow_factory() as uow:
        before = len(await uow.publication_review_decisions.list_for_edition(edition.id))
    with pytest.raises(CompositionVersionConflict):
        await service.exclude_subject(
            edition.id,
            subjects["C"].id,
            reason="Action périmée.",
            expected_version=1,
            idempotency_key="exclude-c-version-loser",
            actor_id="analyst",
            correlation_id=f"exclude-{edition.id}-stale",
        )
    async with uow_factory() as uow:
        after = len(await uow.publication_review_decisions.list_for_edition(edition.id))
    assert after == before


@pytest.mark.asyncio
async def test_exclusion_idempotency_reuses_one_decision_and_rejects_changed_payload(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, _artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    service = EditionCompositionService(uow_factory)
    first_composition, first_decision_id = await service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=1,
        idempotency_key="exclude-b-idempotent",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    replay_composition, replay_decision_id = await service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=1,
        idempotency_key="exclude-b-idempotent",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    assert replay_composition.version == first_composition.version
    assert replay_decision_id == first_decision_id
    with pytest.raises(CompositionIdempotencyConflict):
        await service.exclude_subject(
            edition.id,
            subjects["B"].id,
            reason="Un autre motif.",
            expected_version=1,
            idempotency_key="exclude-b-idempotent",
            actor_id="analyst",
            correlation_id=f"exclude-{edition.id}",
        )
    async with uow_factory() as uow:
        decisions = await uow.publication_review_decisions.list_for_edition(edition.id)
    assert [decision.id for decision in decisions] == [first_decision_id]


@pytest.mark.asyncio
async def test_reinclusion_keeps_exclusion_history_and_does_not_change_frozen_manifest(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, _batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    composition_service = EditionCompositionService(uow_factory)
    initial = await composition_service.get(edition.id)
    assert initial.composition is not None
    original_b = next(
        entry for entry in initial.composition.entries if entry.subject_id == subjects["B"].id
    )
    excluded, decision_id = await composition_service.exclude_subject(
        edition.id,
        subjects["B"].id,
        reason=EXCLUSION_REASON,
        expected_version=initial.composition.version,
        idempotency_key="exclude-b-before-reinclude",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    accepted = await EditionPublicationService(uow_factory, artifact_store).accept(
        edition.id,
        actor_id="analyst",
        expected_composition_version=excluded.version,
        correlation_id=f"accept-{edition.id}",
    )
    frozen_hash = accepted.manifest.content_sha256

    reincluded = await composition_service.include(
        edition.id,
        replace(original_b, position=3, review_decision_id=None),
        expected_version=excluded.version,
        idempotency_key="reinclude-b",
        actor_id="analyst",
        correlation_id=f"reinclude-{edition.id}",
    )
    assert any(
        entry.subject_id == subjects["B"].id
        and entry.disposition is CompositionDisposition.INCLUDED
        for entry in reincluded.entries
    )
    async with uow_factory() as uow:
        history = await uow.publication_review_decisions.list_for_edition(edition.id)
        persisted_manifest = await uow.publication_manifests.get(accepted.manifest.id)
    assert any(decision.id == decision_id for decision in history)
    assert persisted_manifest is not None
    assert persisted_manifest.content_sha256 == frozen_hash


@pytest.mark.asyncio
async def test_excluded_b_pinned_to_its_decided_document_is_accepted_and_published(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """The exclusion entry keeps the exact document identity the decision names."""
    edition, artifact_store, subjects, decision = await _decide_exclusion_of_b(
        uow_factory, tmp_path
    )
    composition_service = EditionCompositionService(uow_factory)
    current = (await composition_service.get(edition.id)).composition
    assert current is not None
    entries = []
    for _position, entry in enumerate(current.entries, start=1):
        if entry.subject_id == subjects["B"].id:
            entry = replace(
                entry,
                disposition=CompositionDisposition.EXCLUDED,
                position=None,
                review_decision_id=decision.id,
            )
        else:
            entry = replace(entry, position=1 if entry.subject_id == subjects["A"].id else 2)
        entries.append(entry)
    await composition_service.replace_entries(
        edition.id,
        tuple(entries),
        expected_version=current.version,
        idempotency_key=f"exclude-{uuid4()}",
        actor_id="analyst",
        correlation_id=f"exclude-{edition.id}",
    )
    await _assert_abc_publication_with_excluded_b(
        uow_factory, artifact_store, edition, subjects, decision
    )
