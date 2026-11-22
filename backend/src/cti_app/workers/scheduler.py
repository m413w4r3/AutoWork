import signal
import threading
import time

from cti_app.config import get_settings
from cti_app.logging import configure_logging
from cti_app.workers.tasks import (
    dispatch_editorial_proposal_impacts,
    recover_abandoned_jobs,
    sweep_expired_editorial_drafts,
    sweep_expired_investigation_leases,
)


def run_once(*, task_sweep_due: bool) -> None:
    """Enqueue one round of passive periodic work; no job state lives here."""
    recover_abandoned_jobs.send()
    dispatch_editorial_proposal_impacts.send()
    sweep_expired_editorial_drafts.send()
    if task_sweep_due:
        # M24 task leases (ADR-0011 D3).  The interval is owned by the
        # scheduler; the actor itself is idempotent and never steals a lease.
        sweep_expired_investigation_leases.send()


def main() -> None:
    """Periodically enqueue passive job recovery; no job state lives here."""
    settings = get_settings()
    configure_logging(settings.log_level)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_task_sweep: float | None = None
    while not stopped.is_set():
        now = time.monotonic()
        task_sweep_due = (
            last_task_sweep is None
            or now - last_task_sweep >= settings.m24_task_sweep_interval_seconds
        )
        if task_sweep_due:
            last_task_sweep = now
        run_once(task_sweep_due=task_sweep_due)
        stopped.wait(settings.job_recovery_interval_seconds)


if __name__ == "__main__":
    main()
