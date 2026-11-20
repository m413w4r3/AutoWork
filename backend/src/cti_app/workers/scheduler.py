import signal
import threading

from cti_app.config import get_settings
from cti_app.logging import configure_logging
from cti_app.workers.tasks import (
    dispatch_editorial_proposal_impacts,
    recover_abandoned_jobs,
    sweep_expired_editorial_drafts,
)


def main() -> None:
    """Periodically enqueue passive job recovery; no job state lives here."""
    settings = get_settings()
    configure_logging(settings.log_level)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped.is_set():
        recover_abandoned_jobs.send()
        dispatch_editorial_proposal_impacts.send()
        sweep_expired_editorial_drafts.send()
        stopped.wait(settings.job_recovery_interval_seconds)


if __name__ == "__main__":
    main()
