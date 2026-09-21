from __future__ import annotations

from uuid import uuid4

import pytest

from cti_app.application.selection import (
    SelectionDecisionCommand,
    SelectionDecisionStaleError,
    SelectionEditionArchivedError,
    SelectionIdempotencyConflictError,
    SelectionInvalidCommandError,
    SelectionService,
    SelectionSnapshotStaleError,
    SelectionSubjectAlreadyMaterializedError,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import EditionStatus
from cti_app.domain.selection import SelectionAction
from tests.selection_support import make_selection_fixture


def _command(subject_id, action, *, key=None, expected=None):
    return SelectionDecisionCommand(
        discovery_subject_id=subject_id,
        action=action,
        expected_decision_id=expected,
        actor_id="analyst:1",
        correlation_id="test-correlation",
        idempotency_key=key or str(uuid4()),
    )


@pytest.mark.asyncio
async def test_select_creates_subject_origin_and_provenance_with_restrictive_tlp() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture(
        tlp=TLP.GREEN, candidate_tlp=TLP.AMBER
    )
    service = SelectionService(factory)

    board = await service.board(edition.id)
    assert board.items[0].effective_state == "undecided"
    result = await service.decide_many(
        edition.id,
        [_command(discovery_subject_id, SelectionAction.SELECT, key="select-1")],
        snapshot_version=snapshot.version,
    )

    assert result.items[0].effective_state == "selected"
    assert len(factory.subjects) == len(factory.origins) == len(factory.decisions) == 1
    assert next(iter(factory.subjects.values())).tlp is TLP.AMBER
    event = factory.provenance_events[0]
    assert event.event_type == "subject.created_from_selection"
    assert event.payload["snapshot_version"] == snapshot.version
    assert event.payload["candidate_ids"]


@pytest.mark.asyncio
async def test_exact_retry_is_idempotent_and_select_ignore_is_terminal() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    service = SelectionService(factory)
    command = _command(discovery_subject_id, SelectionAction.SELECT, key="same-key")
    await service.decide_many(edition.id, [command], snapshot_version=snapshot.version)
    decision = factory.decisions[0]
    await service.decide_many(
        edition.id,
        [
            _command(
                discovery_subject_id,
                SelectionAction.SELECT,
                key="same-key",
                expected=decision.id,
            )
        ],
        snapshot_version=snapshot.version,
    )

    assert len(factory.subjects) == 1
    assert len(factory.decisions) == 1
    with pytest.raises(SelectionSubjectAlreadyMaterializedError):
        await service.decide_many(
            edition.id,
            [_command(discovery_subject_id, SelectionAction.IGNORE, key="ignore")],
            snapshot_version=snapshot.version,
        )


@pytest.mark.asyncio
async def test_ignore_then_select_is_valid_and_ioc_recommendation_is_advisory() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture(with_ioc=True)
    service = SelectionService(factory)
    board = await service.board(edition.id)
    assert board.items[0].recommendation.recommended
    assert board.items[0].recommendation.reason == "ioc_signal"
    assert not factory.subjects and not factory.decisions and not factory.origins

    await service.decide_many(
        edition.id,
        [_command(discovery_subject_id, SelectionAction.IGNORE, key="ignore")],
        snapshot_version=snapshot.version,
    )
    ignored = factory.decisions[-1]
    await service.decide_many(
        edition.id,
        [_command(discovery_subject_id, SelectionAction.SELECT, key="select", expected=ignored.id)],
        snapshot_version=snapshot.version,
    )
    assert len(factory.subjects) == len(factory.origins) == 1
    assert len(factory.decisions) == 2


@pytest.mark.asyncio
async def test_stale_and_duplicate_batches_fail_before_mutation() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    service = SelectionService(factory)
    command = _command(discovery_subject_id, SelectionAction.SELECT, key="batch")
    with pytest.raises(SelectionSnapshotStaleError):
        await service.decide_many(edition.id, [command], snapshot_version=99)
    with pytest.raises(SelectionInvalidCommandError):
        await service.decide_many(
            edition.id,
            [command, command],
            snapshot_version=snapshot.version,
        )
    assert not factory.subjects and not factory.decisions


@pytest.mark.asyncio
async def test_archived_board_is_readable_but_mutation_is_rejected() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    edition.state = EditionStatus.ARCHIVED
    service = SelectionService(factory)
    assert (await service.board(edition.id)).items
    with pytest.raises(SelectionEditionArchivedError):
        await service.decide_many(
            edition.id,
            [_command(discovery_subject_id, SelectionAction.IGNORE)],
            snapshot_version=snapshot.version,
        )


@pytest.mark.asyncio
async def test_stale_decision_and_incompatible_key_are_deterministic() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    service = SelectionService(factory)
    first = _command(discovery_subject_id, SelectionAction.IGNORE, key="key")
    await service.decide_many(edition.id, [first], snapshot_version=snapshot.version)
    with pytest.raises(SelectionDecisionStaleError):
        await service.decide_many(
            edition.id,
            [_command(discovery_subject_id, SelectionAction.SELECT, key="other", expected=uuid4())],
            snapshot_version=snapshot.version,
        )
    with pytest.raises(SelectionIdempotencyConflictError):
        await service.decide_many(
            edition.id,
            [
                _command(
                    discovery_subject_id,
                    SelectionAction.SELECT,
                    key="key",
                    expected=factory.decisions[0].id,
                )
            ],
            snapshot_version=snapshot.version,
        )
