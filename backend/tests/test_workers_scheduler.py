"""B1 wiring oracle: the scheduler actually enqueues the task-lease sweep.

The sweep existed only as an application service before this lot; an actor
that is never sent is not a production wiring.  ``run_once`` is the loop body
extracted for this test so the assertion does not depend on signal/timing.
"""

from __future__ import annotations

import pytest

from cti_app.workers import scheduler
from cti_app.workers.tasks import sweep_expired_investigation_leases


class _Sender:
    def __init__(self) -> None:
        self.sent = 0

    def send(self) -> None:
        self.sent += 1


def _install_senders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Sender]:
    senders = {
        name: _Sender()
        for name in (
            "recover_abandoned_jobs",
            "dispatch_editorial_proposal_impacts",
            "sweep_expired_editorial_drafts",
            "sweep_expired_investigation_leases",
        )
    }
    for name, sender in senders.items():
        monkeypatch.setattr(scheduler, name, sender)
    return senders


def test_run_once_enqueues_the_expired_lease_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    senders = _install_senders(monkeypatch)

    scheduler.run_once(task_sweep_due=True)

    assert all(sender.sent == 1 for sender in senders.values())


def test_run_once_honours_the_configured_sweep_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    senders = _install_senders(monkeypatch)

    scheduler.run_once(task_sweep_due=False)

    assert senders["sweep_expired_investigation_leases"].sent == 0
    assert senders["recover_abandoned_jobs"].sent == 1


def test_expired_lease_sweep_is_a_dramatiq_actor() -> None:
    # A plain function here would never be picked up by the worker.
    assert callable(getattr(sweep_expired_investigation_leases, "send", None))
    assert (
        getattr(sweep_expired_investigation_leases, "actor_name", None)
        == "sweep_expired_investigation_leases"
    )
