from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
from types import TracebackType
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router
from cti_app.application.edition_composition import (
    CompositionIdempotencyConflict,
    CompositionRead,
    CompositionReferenceError,
    CompositionReferenceSnapshot,
    CompositionReferenceState,
    CompositionValidationAxes,
    CompositionValidationCode,
    CompositionVersionConflict,
    EditionCompositionService,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.classification import TLP
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    CompositionInvariantError,
    CompositionPreparationStatus,
    EditionComposition,
    EditionCompositionEntry,
)
from cti_app.domain.editions import Edition
from cti_app.domain.production import ProductionArtifactStatus, ProductionRunStatus


def _edition() -> Edition:
    return Edition(
        country="France",
        country_code="FR",
        period_start=date(2099, 1, 1),
        period_end=date(2099, 1, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
    )


def _included_entry(subject_id: UUID, *, position: int = 1) -> EditionCompositionEntry:
    return EditionCompositionEntry(
        subject_id=subject_id,
        disposition=CompositionDisposition.INCLUDED,
        position=position,
        production_run_id=uuid4(),
        pipeline_generation=2,
        artifact_id=uuid4(),
        artifact_version=1,
        artifact_input_hash="a" * 64,
    )


def _valid_reference() -> CompositionReferenceState:
    return CompositionReferenceState(
        subject_exists=True,
        run_exists=True,
        run_belongs_to_edition=True,
        run_subject_matches=True,
        run_pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
        artifact_exists=True,
        artifact_run_matches=True,
        artifact_subject_matches=True,
        artifact_stage="publication",
        artifact_status=ProductionArtifactStatus.VERIFIED,
        artifact_version=1,
        artifact_input_hash="a" * 64,
        latest_artifact_version=1,
    )


class _CompositionRepository:
    def __init__(self, factory: _CompositionFactory) -> None:
        self.factory = factory
        self.validate_calls = 0

    async def get_for_edition(self, edition_id: UUID) -> EditionComposition | None:
        return deepcopy(self.factory.compositions.get(edition_id))

    async def get_for_edition_for_update(self, edition_id: UUID) -> EditionComposition | None:
        return await self.get_for_edition(edition_id)

    async def save(self, composition: EditionComposition) -> None:
        self.factory.compositions[composition.edition_id] = deepcopy(composition)

    async def replace_entries(
        self, composition_id: UUID, entries: tuple[EditionCompositionEntry, ...]
    ) -> None:
        return None

    async def list_subject_ids(self, edition_id: UUID) -> tuple[UUID, ...]:
        return tuple(self.factory.subject_ids.get(edition_id, ()))

    async def validate_references(
        self,
        edition_id: UUID,
        entries: tuple[EditionCompositionEntry, ...],
        *,
        lock: bool,
    ) -> CompositionReferenceSnapshot:
        self.validate_calls += 1
        return CompositionReferenceSnapshot(
            subject_ids=frozenset(self.factory.subject_ids.get(edition_id, ())),
            entries={
                entry.subject_id: self.factory.references.get(
                    entry.subject_id, CompositionReferenceState(subject_exists=False)
                )
                for entry in entries
            },
        )


class _EventRepository:
    def __init__(self, factory: _CompositionFactory) -> None:
        self.factory = factory

    async def get_by_idempotency_key(self, composition_id: UUID, idempotency_key: str):
        return deepcopy(self.factory.events.get((composition_id, idempotency_key)))

    async def append(self, event) -> None:
        self.factory.events[(event.composition_id, event.idempotency_key)] = deepcopy(event)


class _Uow:
    def __init__(self, factory: _CompositionFactory) -> None:
        self.factory = factory
        self.editions = self
        self.edition_compositions = _CompositionRepository(factory)
        self.edition_composition_events = _EventRepository(factory)

    async def get(self, edition_id: UUID) -> Edition | None:
        return deepcopy(self.factory.editions.get(edition_id))

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return await self.get(edition_id)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None


class _CompositionFactory:
    def __init__(self, edition: Edition) -> None:
        self.editions = {edition.id: edition}
        self.compositions: dict[UUID, EditionComposition] = {}
        self.events = {}
        self.subject_ids: dict[UUID, set[UUID]] = {edition.id: set()}
        self.references: dict[UUID, CompositionReferenceState] = {}
        self.production_mutations = 0

    def __call__(self) -> _Uow:
        return _Uow(self)


def test_domain_uses_typed_dispositions_and_enforces_entry_invariants() -> None:
    subject_id = uuid4()
    deferred = EditionCompositionEntry(
        subject_id=subject_id, disposition=CompositionDisposition.DEFERRED
    )
    assert deferred.preparation_status is CompositionPreparationStatus.UNASSESSED

    with pytest.raises(CompositionInvariantError, match="complete artifact pin"):
        EditionCompositionEntry(
            subject_id=subject_id,
            disposition=CompositionDisposition.INCLUDED,
            position=1,
        )
    with pytest.raises(CompositionInvariantError, match="cannot carry artifacts"):
        EditionCompositionEntry(
            subject_id=subject_id,
            disposition=CompositionDisposition.DEFERRED,
            production_run_id=uuid4(),
            pipeline_generation=1,
            artifact_id=uuid4(),
            artifact_version=1,
            artifact_input_hash="a" * 64,
        )
    with pytest.raises(CompositionInvariantError, match="require a review decision"):
        EditionCompositionEntry(subject_id=subject_id, disposition=CompositionDisposition.EXCLUDED)


@pytest.mark.asyncio
async def test_commands_are_versioned_idempotent_and_reject_stale_operators() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    first_subject, second_subject, third_subject = uuid4(), uuid4(), uuid4()
    factory.subject_ids[edition.id].update({first_subject, second_subject, third_subject})
    factory.references.update(
        {
            subject_id: CompositionReferenceState(subject_exists=True)
            for subject_id in factory.subject_ids[edition.id]
        }
    )
    service = EditionCompositionService(factory)
    payload = (
        EditionCompositionEntry(
            subject_id=first_subject, disposition=CompositionDisposition.DEFERRED
        ),
    )
    created = await service.replace_entries(
        edition.id,
        payload,
        expected_version=0,
        idempotency_key="replace-1",
        actor_id="analyst-a",
        correlation_id="corr-a",
    )
    replay = await service.replace_entries(
        edition.id,
        (
            EditionCompositionEntry(
                subject_id=first_subject, disposition=CompositionDisposition.DEFERRED
            ),
        ),
        expected_version=0,
        idempotency_key="replace-1",
        actor_id="analyst-b",
        correlation_id="corr-b",
    )
    assert replay == created
    assert replay.version == 1

    with pytest.raises(CompositionIdempotencyConflict):
        await service.replace_entries(
            edition.id,
            (
                EditionCompositionEntry(
                    subject_id=second_subject, disposition=CompositionDisposition.DEFERRED
                ),
            ),
            expected_version=0,
            idempotency_key="replace-1",
            actor_id="analyst-a",
            correlation_id="corr-c",
        )

    await service.defer(
        edition.id,
        second_subject,
        expected_version=1,
        idempotency_key="operator-a",
        actor_id="analyst-a",
        correlation_id="corr-d",
    )
    with pytest.raises(CompositionVersionConflict) as conflict:
        await service.defer(
            edition.id,
            third_subject,
            expected_version=1,
            idempotency_key="operator-b",
            actor_id="analyst-b",
            correlation_id="corr-e",
        )
    assert conflict.value.expected_version == 1
    assert conflict.value.actual_version == 2


@pytest.mark.asyncio
async def test_include_rejects_an_artifact_from_another_edition() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    factory.references[subject_id] = replace(_valid_reference(), run_belongs_to_edition=False)
    service = EditionCompositionService(factory)

    with pytest.raises(CompositionReferenceError) as error:
        await service.include(
            edition.id,
            _included_entry(subject_id),
            expected_version=0,
            idempotency_key="wrong-edition",
            actor_id="analyst",
            correlation_id="corr",
        )
    assert error.value.reason == "production_run_not_in_edition"


@pytest.mark.asyncio
async def test_include_rejects_a_review_decision_for_another_artifact_pin() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    factory.references[subject_id] = _valid_reference()
    service = EditionCompositionService(factory)
    entry = replace(_included_entry(subject_id), review_decision_id=uuid4())

    with pytest.raises(CompositionReferenceError) as error:
        await service.include(
            edition.id,
            entry,
            expected_version=0,
            idempotency_key="stale-review-decision",
            actor_id="analyst",
            correlation_id="corr-stale-review",
        )

    assert error.value.reason == "review_decision_identity_mismatch"


@pytest.mark.asyncio
async def test_exclude_requires_a_matching_exclusion_decision() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    decision_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    factory.references[subject_id] = replace(
        CompositionReferenceState(subject_exists=True),
        decision_matches_identity=True,
        decision_is_valid_exclusion=True,
    )
    service = EditionCompositionService(factory)
    result = await service.exclude(
        edition.id,
        subject_id,
        decision_id,
        expected_version=0,
        idempotency_key="exclude-1",
        actor_id="analyst",
        correlation_id="corr-exclude",
    )
    assert result.entries[0].disposition is CompositionDisposition.EXCLUDED
    assert result.entries[0].review_decision_id == decision_id


@pytest.mark.asyncio
async def test_duplicate_subjects_are_rejected_and_reorder_preserves_production_pins() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    first_subject, second_subject = uuid4(), uuid4()
    factory.subject_ids[edition.id].update({first_subject, second_subject})
    factory.references.update(
        {subject_id: _valid_reference() for subject_id in (first_subject, second_subject)}
    )
    service = EditionCompositionService(factory)
    first = _included_entry(first_subject, position=1)
    second = replace(
        _included_entry(second_subject, position=2),
        production_run_id=first.production_run_id,
    )
    # Both subjects have independently verified pin snapshots.
    factory.references[first_subject] = _valid_reference()
    factory.references[second_subject] = _valid_reference()

    with pytest.raises(CompositionInvariantError, match="each subject may appear only once"):
        await service.replace_entries(
            edition.id,
            (first, first),
            expected_version=0,
            idempotency_key="duplicate",
            actor_id="analyst",
            correlation_id="corr-1",
        )

    created = await service.replace_entries(
        edition.id,
        (first, second),
        expected_version=0,
        idempotency_key="includes",
        actor_id="analyst",
        correlation_id="corr-2",
    )
    reordered = await service.reorder(
        edition.id,
        (second_subject, first_subject),
        expected_version=1,
        idempotency_key="reorder",
        actor_id="analyst",
        correlation_id="corr-3",
    )
    by_subject = {entry.subject_id: entry for entry in reordered.entries}
    assert by_subject[first_subject].position == 2
    assert by_subject[second_subject].position == 1
    assert by_subject[first_subject].artifact_id == next(
        entry.artifact_id for entry in created.entries if entry.subject_id == first_subject
    )
    assert by_subject[first_subject].production_run_id == next(
        entry.production_run_id for entry in created.entries if entry.subject_id == first_subject
    )
    assert factory.production_mutations == 0


@pytest.mark.asyncio
async def test_validation_reports_a_newer_artifact_without_rewriting_the_pin() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    entry = _included_entry(subject_id)
    factory.references[subject_id] = replace(
        _valid_reference(), latest_artifact_run_id=entry.production_run_id
    )
    service = EditionCompositionService(factory)
    created = await service.replace_entries(
        edition.id,
        (entry,),
        expected_version=0,
        idempotency_key="pin-v1",
        actor_id="analyst",
        correlation_id="corr-1",
    )
    factory.references[subject_id] = replace(
        _valid_reference(),
        latest_artifact_run_id=entry.production_run_id,
        latest_artifact_version=2,
    )

    result = await service.validate(edition.id)
    assert CompositionValidationCode.SUPERSEDED_ARTIFACT_AVAILABLE in {
        reason.code for reason in result.reasons
    }
    assert result.axes.discovery_update_available is None
    assert result.composition is not None
    assert result.composition.preparation_status is CompositionPreparationStatus.BLOCKED
    assert result.composition.entries[0].preparation_status is CompositionPreparationStatus.BLOCKED
    assert next(iter(created.entries)).artifact_version == 1
    assert next(iter(factory.compositions[edition.id].entries)).artifact_version == 1


@pytest.mark.asyncio
async def test_a_newer_run_does_not_replace_the_explicitly_pinned_run() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    entry = _included_entry(subject_id)
    factory.references[subject_id] = replace(
        _valid_reference(), latest_artifact_run_id=entry.production_run_id
    )
    service = EditionCompositionService(factory)
    await service.replace_entries(
        edition.id,
        (entry,),
        expected_version=0,
        idempotency_key="pin-run-1",
        actor_id="analyst",
        correlation_id="corr-1",
    )
    factory.references[subject_id] = replace(
        _valid_reference(), latest_artifact_run_id=uuid4(), latest_artifact_version=1
    )

    result = await service.validate(edition.id)
    assert CompositionValidationCode.SUPERSEDED_ARTIFACT_AVAILABLE not in {
        reason.code for reason in result.reasons
    }
    assert result.axes.production_validity is True
    assert result.axes.composition_eligibility is True


@pytest.mark.asyncio
async def test_get_put_composition_routes_expose_validation_and_idempotent_commands() -> None:
    edition_id = uuid4()
    subject_id = uuid4()

    class _ApiCompositionService:
        received: tuple[UUID, tuple[EditionCompositionEntry, ...], dict[str, object]] | None = None

        async def get(self, requested_edition_id: UUID) -> CompositionRead:
            return CompositionRead(
                edition_id=requested_edition_id,
                composition=None,
                reasons=(),
                axes=CompositionValidationAxes(
                    production_validity=None,
                    discovery_update_available=None,
                    composition_eligibility=False,
                ),
            )

        async def replace_entries(
            self,
            requested_edition_id: UUID,
            entries: tuple[EditionCompositionEntry, ...],
            **kwargs: object,
        ) -> EditionComposition:
            self.received = (requested_edition_id, entries, kwargs)
            return EditionComposition(
                edition_id=requested_edition_id,
                version=1,
                entries=entries,
                actor_id=str(kwargs["actor_id"]),
            )

    application = FastAPI()
    application.include_router(router)
    service = _ApiCompositionService()
    application.state.edition_composition_service = service
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        read = await client.get(f"/api/editions/{edition_id}/composition")
        written = await client.put(
            f"/api/editions/{edition_id}/composition",
            headers={"Idempotency-Key": "api-composition-1"},
            json={
                "expected_version": 0,
                "entries": [{"subject_id": str(subject_id), "disposition": "deferred"}],
            },
        )

    assert read.status_code == 200
    assert read.json()["axes"]["discovery_update_available"] is None
    assert read.json()["composition"] is None
    assert written.status_code == 200
    assert written.json()["entries"][0]["disposition"] == "deferred"
    assert service.received is not None
    assert service.received[0] == edition_id
    assert service.received[2]["expected_version"] == 0
    assert service.received[2]["idempotency_key"] == "api-composition-1"


@pytest.mark.asyncio
async def test_discovery_update_is_orthogonal_to_production_and_composition_eligibility() -> None:
    edition = _edition()
    factory = _CompositionFactory(edition)
    subject_id = uuid4()
    factory.subject_ids[edition.id].add(subject_id)
    factory.references[subject_id] = replace(_valid_reference(), discovery_update_available=True)
    service = EditionCompositionService(factory)

    await service.replace_entries(
        edition.id,
        (_included_entry(subject_id),),
        expected_version=0,
        idempotency_key="discovery-update",
        actor_id="analyst",
        correlation_id="corr-discovery-update",
    )
    read = await service.get(edition.id)

    assert read.axes.production_validity is True
    assert read.axes.discovery_update_available is True
    assert read.axes.composition_eligibility is True
    assert factory.production_mutations == 0
