"""M24.1 R2B-3 oracles: a refused anchor timeout never leaks a sandbox child.

``_popen_with_anchor`` starts the child from a dedicated thread so bwrap's
``PR_SET_PDEATHSIG`` follows a thread that lives as long as the run.  If the
anchor's ``Popen`` takes longer than ``SPAWN_ANCHOR_TIMEOUT_SECONDS``, the
caller refuses the launch with an ``OSError`` — but the late child must not be
released unmanaged: it is killed (whole process group) and reaped before it can
survive as an unsupervised sandbox.

The oracle injects a slow ``Popen`` and a short timeout; the spawned process is
a local ``python -c`` sleep.  No provider, network or bwrap is involved.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from sidecars.workspace_agent.sessions import process

from tests.workspace_agent.sessions_support import proc_alive, wait_until


def test_anchor_timeout_kills_and_reaps_a_child_that_starts_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def late_popen(argv: object, **kwargs: object) -> subprocess.Popen[bytes]:
        time.sleep(0.4)  # longer than the injected timeout, bounded for the test
        child = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started.append(child)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", late_popen)
    monkeypatch.setattr(process, "SPAWN_ANCHOR_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(OSError, match="did not start"):
        process._popen_with_anchor(("unused",))

    wait_until(lambda: started, timeout=10)
    child = started[0]
    try:
        # The anchor owns the late child: it signals the group and reaps it.
        wait_until(lambda: child.poll() is not None, timeout=10)
        assert child.returncode is not None and child.returncode < 0
        assert not proc_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        process.close_pipes(child)


def test_anchor_still_returns_a_child_that_starts_in_time() -> None:
    child = process._popen_with_anchor(
        (sys.executable, "-c", "import time; time.sleep(0.2)"),
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.poll() is None
        assert child.wait(timeout=10) == 0
    finally:
        process.close_pipes(child)
