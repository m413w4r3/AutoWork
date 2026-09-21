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
from tests.selection_support import (
    make_selection_fixture,
    make_selection_fixture_with_subjects,
)


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


@pytest.mark.asyncio
async def test_one_key_covers_one_batch_and_rejects_subset_or_superset() -> None:
    factory, edition, snapshot, identity_ids = make_selection_fixture_with_subjects(count=2)
    first, second = identity_ids
    service = SelectionService(factory)

    await service.decide_many(
        edition.id,
        [_command(first, SelectionAction.SELECT, key="batch-key")],
        snapshot_version=snapshot.version,
    )
    assert len(factory.decisions) == 1

    # Superset under the spent key: the extra decision must not be applied.
    with pytest.raises(SelectionIdempotencyConflictError):
        await service.decide_many(
            edition.id,
            [
                _command(first, SelectionAction.SELECT, key="batch-key"),
                _command(second, SelectionAction.IGNORE, key="batch-key"),
            ],
            snapshot_version=snapshot.version,
        )
    assert len(factory.decisions) == len(factory.subjects) == len(factory.origins) == 1

    await service.decide_many(
        edition.id,
        [
            _command(
                first, SelectionAction.SELECT, key="pair-key", expected=factory.decisions[0].id
            ),
            _command(second, SelectionAction.IGNORE, key="pair-key"),
        ],
        snapshot_version=snapshot.version,
    )
    assert len(factory.decisions) == 2

    # Subset under the spent pair key is just as incompatible.
    with pytest.raises(SelectionIdempotencyConflictError):
        await service.decide_many(
            edition.id,
            [_command(second, SelectionAction.IGNORE, key="pair-key")],
            snapshot_version=snapshot.version,
        )
    assert len(factory.decisions) == 2


@pytest.mark.asyncio
async def test_absent_expectation_is_stale_once_a_decision_exists() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    service = SelectionService(factory)

    # The operator read an undecided board, so the command carries no
    # expectation; a concurrent IGNORE landed before the lock was taken.
    await service.decide_many(
        edition.id,
        [_command(discovery_subject_id, SelectionAction.IGNORE, key="concurrent-ignore")],
        snapshot_version=snapshot.version,
    )
    with pytest.raises(SelectionDecisionStaleError):
        await service.decide_many(
            edition.id,
            [_command(discovery_subject_id, SelectionAction.SELECT, key="stale-select")],
            snapshot_version=snapshot.version,
        )
    assert not factory.subjects and not factory.origins
    assert len(factory.decisions) == 1


@pytest.mark.asyncio
async def test_exact_retry_of_an_ignore_then_select_batch_is_replayed() -> None:
    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    service = SelectionService(factory)
    await service.decide_many(
        edition.id,
        [_command(discovery_subject_id, SelectionAction.IGNORE, key="ignore-key")],
        snapshot_version=snapshot.version,
    )
    ignored = factory.decisions[0]
    select = _command(
        discovery_subject_id, SelectionAction.SELECT, key="select-key", expected=ignored.id
    )
    await service.decide_many(edition.id, [select], snapshot_version=snapshot.version)
    # The retry repeats the command verbatim: the newest decision is now the
    # SELECT itself, so the replay must be resolved before the stale check.
    board = await service.decide_many(edition.id, [select], snapshot_version=snapshot.version)

    assert board.items[0].effective_state == "selected"
    assert [decision.action for decision in factory.decisions] == [
        SelectionAction.IGNORE,
        SelectionAction.SELECT,
    ]
    assert len(factory.subjects) == len(factory.origins) == 1


@pytest.mark.asyncio
async def test_selection_survives_workspace_materialization_failure() -> None:
    class FailingWorkspace:
        def __init__(self) -> None:
            self.calls = 0

        async def materialize(self, *args: object, **kwargs: object) -> object:
            self.calls += 1
            raise OSError("workspace unavailable")

    factory, edition, snapshot, discovery_subject_id = make_selection_fixture()
    workspace = FailingWorkspace()
    service = SelectionService(factory, materializer=workspace)
    command = _command(discovery_subject_id, SelectionAction.SELECT, key="workspace-key")

    board = await service.decide_many(edition.id, [command], snapshot_version=snapshot.version)

    # The filesystem projection runs after the commit and is not canonical.
    assert workspace.calls == 1
    assert board.items[0].effective_state == "selected"
    assert len(factory.subjects) == len(factory.origins) == len(factory.decisions) == 1
    assert factory.provenance_events[0].event_type == "subject.created_from_selection"

    retried = await service.decide_many(edition.id, [command], snapshot_version=snapshot.version)
    assert retried.items[0].effective_state == "selected"
    assert len(factory.subjects) == len(factory.origins) == len(factory.decisions) == 1
