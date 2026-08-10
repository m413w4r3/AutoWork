from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.domain.jobs import InvalidJobTransitionError, Job, JobStatus


def make_job() -> Job:
    return Job(
        kind="demo.deterministic",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="demo-1",
        correlation_id="test",
        input_parameters={"steps": 2},
    )


def assert_status(job: Job, expected: JobStatus) -> None:
    assert job.status is expected


def test_job_happy_path_transitions_and_progress() -> None:
    job = make_job()
    started = datetime(2026, 8, 7, 10, tzinfo=UTC)

    job.start(started)
    job.report_progress(1, 2, "Étape 1", started + timedelta(seconds=1))
    job.succeed("demo://result", started + timedelta(seconds=2))

    assert_status(job, JobStatus.SUCCEEDED)
    assert job.attempt == 1
    assert (job.progress_current, job.progress_total) == (1, 2)
    assert job.output_reference == "demo://result"
    assert job.finished_at == started + timedelta(seconds=2)


def test_invalid_transition_and_progress_are_rejected() -> None:
    job = make_job()

    with pytest.raises(InvalidJobTransitionError):
        job.succeed(None)
    job.start()
    with pytest.raises(ValueError, match="Invalid job progress"):
        job.report_progress(3, 2)


def test_running_job_cancellation_is_cooperative() -> None:
    job = make_job()
    job.start()

    job.request_cancellation()

    assert_status(job, JobStatus.RUNNING)
    assert job.cancellation_requested is True
    job.mark_cancelled()
    assert_status(job, JobStatus.CANCELLED)


def test_waiting_human_is_an_explicit_non_terminal_state() -> None:
    job = make_job()
    job.start()
    job.wait_for_human("Décision analyste requise")

    assert_status(job, JobStatus.WAITING_HUMAN)
    assert job.is_terminal is False
    job.retry_manually()
    assert_status(job, JobStatus.QUEUED)
