"""A run that loses its publication owes a rebuild, and says so.

An IOC arbitration with a semantic impact stales SYNTHESIS and PUBLICATION and
asks the caller for a retry.  Before this, the run kept claiming READY: the
review read model joins only non-STALE artifacts, so the article surfaced with
no document, no error, and a "Réessayer" pointed at ASSEMBLY -- whose SYNTHESIS
prerequisite the same repair had just invalidated.  Every gesture the desk
offered ended in ``retry_prerequisite_missing``.

The three rules the tests below pin down:

* the transition rides the stale's transaction (no READY run without a current
  publication is ever observable);
* the retry aims at the first stage whose artifact is missing, not at the run's
  last stage;
* the rebuild debt is read off the artifacts, so an article carrying no repair
  issue at all is still visible to the Repair Desk.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_review import EditionReviewReadItem
from cti_app.application.production_repairs import _require_publication_rebuild
from cti_app.application.production_resume import resolve_retry_stage
from cti_app.domain.production import (
    PUBLICATION_REBUILD_REQUIRED_ERROR_CODE,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_ID = UUID("22222222-2222-4222-8222-222222222222")

_REFERENCES = ProductionArtifactStage.REFERENCES.value
_EXTRACTION = ProductionArtifactStage.EXTRACTION.value
_SYNTHESIS = ProductionArtifactStage.SYNTHESIS.value
_PUBLICATION = ProductionArtifactStage.PUBLICATION.value


def _run(status: SubjectProductionStatus = SubjectProductionStatus.READY) -> SubjectProductionRun:
    now = datetime.now(UTC)
    return SubjectProductionRun(
        subject_id=SUBJECT_ID,
        edition_id=EDITION_ID,
        status=status,
        current_stage=SubjectProductionStage.ASSEMBLY,
        run_number=1,
        research_date=date(2026, 9, 1),
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
        version=1,
    )


class _RunsRepository:
    def __init__(self) -> None:
        self.saved: list[SubjectProductionRun] = []

    async def save(self, run: SubjectProductionRun) -> None:
        self.saved.append(run)


class _Uow:
    def __init__(self) -> None:
        self.subject_production_runs = _RunsRepository()


def _row(
    *,
    run_status: SubjectProductionStatus = SubjectProductionStatus.READY,
    live_stages: frozenset[str] | None,
    document: bool,
) -> EditionReviewReadItem:
    """A review row, with the publication either current or gone."""
    return EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_ID,
        title="Article",
        run_id=uuid4(),
        pipeline_generation=0,
        run_status=run_status,
        document_artifact_id=uuid4() if document else None,
        document_artifact_version=1 if document else None,
        document_input_hash=("a" * 64) if document else None,
        document_artifact_status=(ProductionArtifactStatus.VERIFIED if document else None),
        error_code=None,
        error_message=None,
        effective_decision=None,
        live_artifact_stages=live_stages,
    )


# --------------------------------------------------------------------------
# 1. The transition itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staling_the_publication_takes_a_ready_run_out_of_ready() -> None:
    run = _run()
    uow = _Uow()

    changed = await _require_publication_rebuild(
        uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
    )

    assert changed is True
    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert run.error_code == PUBLICATION_REBUILD_REQUIRED_ERROR_CODE
    assert run.error_message
    assert run.error_details == {"retry_stage": "synthesis"}
    # Persisted through the caller's UoW: the write rides the stale's
    # transaction rather than following it.
    assert uow.subject_production_runs.saved == [run]


@pytest.mark.asyncio
async def test_a_run_that_already_failed_keeps_its_own_diagnosis() -> None:
    """The rebuild debt is less informative than the failure that caused it."""
    run = _run(status=SubjectProductionStatus.RUNNING)
    run.mark_failed(code="synthesis_unusable", message="Le modèle n'a rien rendu d'exploitable.")
    uow = _Uow()

    changed = await _require_publication_rebuild(
        uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
    )

    assert changed is False
    assert run.status is SubjectProductionStatus.FAILED
    assert run.error_code == "synthesis_unusable"
    assert uow.subject_production_runs.saved == []


@pytest.mark.asyncio
async def test_a_cancelled_run_is_never_moved() -> None:
    """Cancellation owns its own resume gesture; a rebuild must not hijack it."""
    run = _run(status=SubjectProductionStatus.RUNNING)
    run.mark_cancelled()
    uow = _Uow()

    assert (
        await _require_publication_rebuild(
            uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
        )
        is False
    )
    assert run.status is SubjectProductionStatus.CANCELLED


# --------------------------------------------------------------------------
# 2. The stage a retry must start from
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        # The IOC repair case: synthesis and publication staled together.
        (
            {_REFERENCES, _EXTRACTION},
            SubjectProductionStage.SYNTHESIS,
        ),
        # A references reconciliation stales everything downstream of it.
        ({_REFERENCES}, SubjectProductionStage.EXTRACTION),
        # Nothing survived at all.
        (set(), SubjectProductionStage.REFERENCES),
        # Publication alone was staled, ready for a deterministic reassembly.
        (
            {_REFERENCES, _EXTRACTION, _SYNTHESIS},
            SubjectProductionStage.ASSEMBLY,
        ),
    ],
)
def test_retry_stage_is_the_first_missing_artifact(
    live: set[str], expected: SubjectProductionStage
) -> None:
    assert (
        resolve_retry_stage(live, current_stage=SubjectProductionStage.ASSEMBLY) is expected
    )


def test_a_complete_run_replays_its_last_stage() -> None:
    """Every artifact is current: there is no gap to aim at."""
    live = {_REFERENCES, _EXTRACTION, _SYNTHESIS, _PUBLICATION}

    assert (
        resolve_retry_stage(live, current_stage=SubjectProductionStage.ASSEMBLY)
        is SubjectProductionStage.ASSEMBLY
    )


def test_retry_stage_never_points_at_a_stage_whose_prerequisite_is_missing() -> None:
    """The exact bug: ASSEMBLY offered while SYNTHESIS was staled.

    ``_retry_from_stage_in_uow`` requires the current SYNTHESIS artifact before
    it will run ASSEMBLY, so proposing ASSEMBLY here is what produced the 409.
    """
    live = {_REFERENCES, _EXTRACTION}

    stage = resolve_retry_stage(live, current_stage=SubjectProductionStage.ASSEMBLY)

    assert stage is not SubjectProductionStage.ASSEMBLY


# --------------------------------------------------------------------------
# 3. The rebuild debt, read off the artifacts
# --------------------------------------------------------------------------


def test_a_ready_run_without_a_publication_owes_a_rebuild() -> None:
    row = _row(live_stages=frozenset({_REFERENCES, _EXTRACTION}), document=False)

    assert row.rebuild_required is True
    assert row.rebuild_stage is SubjectProductionStage.SYNTHESIS


def test_a_ready_run_with_its_publication_owes_nothing() -> None:
    row = _row(
        live_stages=frozenset({_REFERENCES, _EXTRACTION, _SYNTHESIS, _PUBLICATION}),
        document=True,
    )

    assert row.rebuild_required is False


@pytest.mark.parametrize(
    "run_status",
    [SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING],
)
def test_a_run_in_flight_owes_no_rebuild(run_status: SubjectProductionStatus) -> None:
    """It has not produced its outputs yet; the pipeline owns that, not the desk."""
    row = _row(run_status=run_status, live_stages=frozenset(), document=False)

    assert row.rebuild_required is False


def test_the_debt_does_not_depend_on_any_repair_decision() -> None:
    """An article with no repair issue can still be missing its deliverable.

    A reference reconciliation, a state import or a manual stale all destroy
    the publication without arbitrating anything.  Deriving the debt from the
    decisions is what hid three of the four broken articles from the desk.
    """
    row = _row(live_stages=frozenset({_REFERENCES}), document=False)

    assert row.rebuild_required is True
    assert row.rebuild_stage is SubjectProductionStage.EXTRACTION


def test_an_unsupplied_artifact_inventory_names_no_stage() -> None:
    """``None`` is "unknown", not "nothing current".

    Naming a stage from an unknown inventory would send the analyst to the
    wrong gesture, which is the whole failure being removed here.
    """
    row = _row(live_stages=None, document=True)

    assert row.rebuild_stage is None


# --------------------------------------------------------------------------
# 4. The permanent invariant
# --------------------------------------------------------------------------


def _rows_from_state(
    state: list[tuple[SubjectProductionStatus, set[str]]],
) -> list[EditionReviewReadItem]:
    return [
        _row(
            run_status=status,
            live_stages=frozenset(live),
            document=_PUBLICATION in live,
        )
        for status, live in state
    ]


def test_ready_requires_a_verified_publication() -> None:
    """The invariant that would have caught this bug on day one.

    A READY run means "assembly complete and QA passed".  If the review can see
    a READY run with no current publication artifact, some writer staled the
    deliverable without transitioning the run -- exactly the defect above.
    """
    complete = {_REFERENCES, _EXTRACTION, _SYNTHESIS, _PUBLICATION}
    rows = _rows_from_state(
        [
            (SubjectProductionStatus.READY, complete),
            # The repaired article now leaves READY instead of staying there.
            (SubjectProductionStatus.NEEDS_REVIEW, {_REFERENCES, _EXTRACTION}),
            (SubjectProductionStatus.RUNNING, set()),
        ]
    )

    offenders = [
        row
        for row in rows
        if row.run_status is SubjectProductionStatus.READY and row.rebuild_required
    ]

    assert offenders == []


def test_the_invariant_actually_catches_the_regression() -> None:
    """Guard the guard: the assertion above must fail on the broken state.

    This is the shape the four production runs were found in -- READY, with
    every downstream artifact staled.
    """
    rows = _rows_from_state([(SubjectProductionStatus.READY, {_REFERENCES, _EXTRACTION})])

    offenders = [
        row
        for row in rows
        if row.run_status is SubjectProductionStatus.READY and row.rebuild_required
    ]

    assert len(offenders) == 1
    assert offenders[0].rebuild_stage is SubjectProductionStage.SYNTHESIS


# --------------------------------------------------------------------------
# 5. The IOC scenarios the audit called out
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ioc_added_after_publication_leaves_a_named_debt() -> None:
    """Generation N publishes, an IOC arbitration invalidates it, N+1 is owed."""
    run = _run()
    uow = _Uow()

    await _require_publication_rebuild(
        uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
    )
    row = _row(
        run_status=run.status,
        live_stages=frozenset({_REFERENCES, _EXTRACTION}),
        document=False,
    )

    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert row.rebuild_required is True
    # The gesture the desk offers is the one the retry service will accept.
    assert row.rebuild_stage is SubjectProductionStage.SYNTHESIS


@pytest.mark.asyncio
async def test_an_article_with_no_ioc_is_never_disturbed() -> None:
    """No repair, no stale, no transition: the article stays publishable."""
    run = _run()
    uow = _Uow()
    row = _row(
        live_stages=frozenset({_REFERENCES, _EXTRACTION, _SYNTHESIS, _PUBLICATION}),
        document=True,
    )

    assert row.rebuild_required is False
    assert run.status is SubjectProductionStatus.READY
    assert uow.subject_production_runs.saved == []


@pytest.mark.asyncio
async def test_the_transition_is_idempotent_across_repeated_repairs() -> None:
    """A second arbitration on an already-indebted run adds no second diagnosis."""
    run = _run()
    uow = _Uow()

    first = await _require_publication_rebuild(
        uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
    )
    version_after_first = run.version
    second = await _require_publication_rebuild(
        uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
    )

    assert (first, second) == (True, False)
    assert run.version == version_after_first
    assert uow.subject_production_runs.saved == [run]


@pytest.mark.asyncio
async def test_a_repair_service_without_a_run_port_does_not_crash() -> None:
    """Lightweight callers predating the run port still drive the repair paths."""
    run = _run()

    assert (
        await _require_publication_rebuild(
            SimpleNamespace(), run, retry_stage=SubjectProductionStage.SYNTHESIS.value
        )
        is True
    )
    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
