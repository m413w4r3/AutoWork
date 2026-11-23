#!/usr/bin/env bash
# Verify ``analysis.run_script`` confinement inside the production analysis-worker
# container image (M24.1 F2-B).  Operator-run only: CI does not call it and it
# needs the local Docker daemon plus an existing ``cti-bulletin-analysis-worker``
# image.  It never builds, pulls or touches the running stack: every execution is
# a bounded ``docker run --rm``.
#
# The run reuses the exact compose.yaml options of the analysis-worker
# (read-only rootfs, no egress, uid 10001, cap-drop ALL, no-new-privileges,
# pids/memory/cpu/core limits, noexec /tmp).  The repository's ``backend/src``
# is mounted at ``/app/src:ro`` -- the path the image's editable install points
# at -- so the battery executes the current source exactly as a rebuilt image
# would, and a stale local image cannot silently attest older code.
#
# Checks, in order:
#   * ``probe_confined_script_execution`` (Landlock + script seccomp + subreaper
#     + descendant sweep, executed for real);
#   * ``confined_analysis.probe_script_sandbox`` returns a non-null kind;
#   * RUN-02/03a/03b/04/05/06/07 equivalents: detached-child sweep at deadline,
#     setsid denial, setsid double-fork sweep at normal exit, socket denial,
#     filesystem escapes, memory overrun and wall deadline;
#   * a final independent ``/proc`` scan proves no marker process survived.
#
# Usage: scripts/ops/verify_run_script_confinement_container.sh [image]
# Exit: 0 every check passed; 1 a check failed; 2 a precondition is missing.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=${1:-${ANALYSIS_WORKER_IMAGE:-cti-bulletin-analysis-worker:latest}}
SOURCE_TREE="$REPO_ROOT/backend/src"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 2
fi
if [ ! -d "$SOURCE_TREE/cti_app" ]; then
    echo "missing source tree: $SOURCE_TREE/cti_app" >&2
    exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "image not present locally: $IMAGE (build it instead of pulling)" >&2
    exit 2
fi

battery_dir=$(mktemp -d "${TMPDIR:-/tmp}/autowork-f2b-battery.XXXXXX")
chmod 0755 "$battery_dir"
cleanup() {
    rm -rf "$battery_dir"
}
trap cleanup EXIT

cat >"$battery_dir/battery.py" <<'PY'
"""In-container confinement battery for analysis.run_script (M24.1 F2-B)."""

from __future__ import annotations

import ast
import asyncio
import errno
import os
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
    DescendantSupervision,
    LandlockPaths,
    probe_confined_script_execution,
    run_analysis_subprocess,
    runtime_read_directories,
    runtime_read_files,
    runtime_write_files,
)
from cti_app.workers import confined_analysis as worker_module

_MIB = 1024 * 1024
_failures: list[str] = []
_markers: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}" + ("" if condition else f" detail={detail!r}"), flush=True)
    if not condition:
        _failures.append(name)


def marker_pids(marker: str) -> list[int]:
    """Independent oracle: pids whose command line still carries the marker."""
    needle = marker.encode("ascii")
    found: list[int] = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return found
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/cmdline", "rb") as handle:
                    if needle in handle.read(65536):
                        found.append(int(entry.name))
            except OSError:
                continue
    return found


def execute(
    source: str,
    *,
    timeout_seconds: float = 5.0,
    memory_limit_bytes: int = 256 * _MIB,
    process_limit: int = 8,
    deny_network: bool = True,
    landlock: bool = True,
    input_bytes: bytes = b"battery-input",
) -> AnalysisSubprocessResult:
    root = Path(tempfile.mkdtemp(prefix="battery-exec-"))
    root.chmod(0o700)
    input_path = root / "input.bin"
    input_path.write_bytes(input_bytes)
    input_path.chmod(0o400)
    work = root / "work"
    work.mkdir(mode=0o700)
    script_path = root / "script.py"
    script_path.write_text(source, encoding="utf-8")
    script_path.chmod(0o400)
    filesystem = None
    if landlock:
        filesystem = LandlockPaths(
            read_directories=runtime_read_directories(),
            read_files=runtime_read_files(script_path, input_path),
            write_directories=(work,),
            write_files=runtime_write_files(),
        )
    supervision = DescendantSupervision(
        wall_deadline_seconds=timeout_seconds + 5.0,
        process_limit=process_limit,
        filesystem=filesystem,
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "AUTOWORK_SCRIPT_INPUT": str(input_path),
        "AUTOWORK_SCRIPT_SCRATCH": str(work),
    }
    return asyncio.run(
        run_analysis_subprocess(
            [sys.executable, "-I", "-u", str(script_path)],
            timeout_seconds=timeout_seconds,
            output_limit=64 * 1024,
            environment=environment,
            memory_limit_bytes=memory_limit_bytes,
            deny_network=deny_network,
            cpu_limit_seconds=max(1, int(timeout_seconds)),
            file_size_limit_bytes=_MIB,
            process_limit=process_limit,
            cwd=work,
            supervision=supervision,
        )
    )


def main() -> int:
    import cti_app

    print("battery uid=%d python=%s exe=%s" % (os.getuid(), sys.version.split()[0], sys.executable))
    print("cti_app %s" % cti_app.__file__)

    probe = probe_confined_script_execution(sys.executable)
    check("production_probe", probe.ok, probe.detail)
    sandbox = worker_module.probe_script_sandbox()
    check("worker_script_sandbox", sandbox.kind is not None, sandbox)

    result = execute(
        "import os, subprocess, sys\n"
        "print('battery-ok')\n"
        "print(open(os.environ['AUTOWORK_SCRIPT_INPUT'], 'rb').read().decode())\n"
        "open(os.path.join(os.environ['AUTOWORK_SCRIPT_SCRATCH'], 'out.txt'), 'w').write('scratch')\n"
        "subprocess.run([sys.executable, '-c', 'pass'], stdout=subprocess.DEVNULL)\n"
        "print('devnull-ok')\n",
    )
    check(
        "run01_input_scratch_devnull",
        result.status is AnalysisSubprocessStatus.SUCCEEDED
        and result.stdout == b"battery-ok\nbattery-input\ndevnull-ok\n",
        (result.status, result.stdout, result.stderr[-200:]),
    )

    marker = f"CTI-F2B-RUN02-{uuid4().hex}"
    _markers.append(marker)
    started = time.monotonic()
    result = execute(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(300)  # {marker}'"
        "])\n"
        "time.sleep(300)\n",
        timeout_seconds=1.5,
    )
    elapsed = time.monotonic() - started
    check(
        "run02_timeout_sweeps_children",
        result.status is AnalysisSubprocessStatus.TIMED_OUT
        and result.sweep_complete is True
        and not result.survivors
        and elapsed < 8.0
        and marker_pids(marker) == [],
        (
            result.status,
            result.sweep_complete,
            result.survivors,
            round(elapsed, 2),
            marker_pids(marker),
        ),
    )

    result = execute(
        "import os\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    try:\n"
        "        os.setsid()\n"
        "    except OSError as exc:\n"
        "        print('setsid_errno=%d' % exc.errno)\n"
        "        os._exit(0)\n"
        "    os._exit(8)\n"
        "_, status = os.waitpid(pid, 0)\n"
        "print('child_status=%d' % os.waitstatus_to_exitcode(status))\n",
    )
    check(
        "run03a_setsid_denied",
        result.status is AnalysisSubprocessStatus.SUCCEEDED
        and b"setsid_errno=1" in result.stdout
        and b"child_status=0" in result.stdout,
        (result.status, result.stdout, result.stderr[-200:]),
    )

    marker = f"CTI-F2B-RUN03B-{uuid4().hex}"
    _markers.append(marker)
    result = execute(
        "import os, subprocess, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(300)  # {marker}'"
        "])\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "time.sleep(0.3)\n"
        "print('main-exit')\n",
        deny_network=False,
        landlock=False,
    )
    check(
        "run03b_setsid_daemon_swept",
        result.status is AnalysisSubprocessStatus.SUCCEEDED
        and result.sweep_complete is True
        and not result.survivors
        and bool(result.descendants_killed)
        and marker_pids(marker) == [],
        (
            result.status,
            result.sweep_complete,
            result.survivors,
            result.descendants_killed,
            marker_pids(marker),
        ),
    )

    result = execute(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "except OSError as exc:\n"
        "    print('network_errno=%d' % exc.errno)\n"
        "else:\n"
        "    print('network=allowed')\n",
    )
    check(
        "run04_network_denied",
        result.status is AnalysisSubprocessStatus.SUCCEEDED
        and b"network_errno=1" in result.stdout,
        (result.status, result.stdout, result.stderr[-200:]),
    )

    host_escape = f"/tmp/cti-f2b-run05-{uuid4().hex}"
    result = execute(
        "import os\n"
        "scratch = os.environ['AUTOWORK_SCRIPT_SCRATCH']\n"
        f"host_escape = {host_escape!r}\n"
        "os.symlink('/etc/passwd', os.path.join(scratch, 'passwd-link'))\n"
        "results = {}\n"
        "def attempt(name, path, mode='rb'):\n"
        "    try:\n"
        "        with open(path, mode) as handle:\n"
        "            handle.write(b'x') if 'w' in mode else handle.read(16)\n"
        "    except OSError as exc:\n"
        "        results[name] = exc.errno\n"
        "    else:\n"
        "        results[name] = 0\n"
        "attempt('passwd', '/etc/passwd')\n"
        "attempt('proc1', '/proc/1/environ')\n"
        "attempt('host_tmp', host_escape, 'wb')\n"
        "attempt('dotdot', '../escape.txt', 'wb')\n"
        "attempt('symlink', os.path.join(scratch, 'passwd-link'))\n"
        "print('ESCAPES=' + repr(sorted(results.items())))\n",
    )
    line = next(
        (
            item
            for item in result.stdout.decode("utf-8", "replace").splitlines()
            if item.startswith("ESCAPES=")
        ),
        "",
    )
    observed = dict(ast.literal_eval(line.split("=", 1)[1])) if line else {}
    allowed = {errno.EACCES, errno.EPERM, errno.ENOENT}
    check(
        "run05_filesystem_escapes_denied",
        result.status is AnalysisSubprocessStatus.SUCCEEDED
        and set(observed) == {"passwd", "proc1", "host_tmp", "dotdot", "symlink"}
        and all(code in allowed for code in observed.values())
        and not Path(host_escape).exists(),
        (result.status, observed, result.stderr[-200:]),
    )

    result = execute(
        "try:\n"
        "    bytearray(512 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('memory=blocked')\n"
        "    raise SystemExit(7)\n"
        "print('memory=allowed')\n",
        memory_limit_bytes=128 * _MIB,
    )
    check(
        "run06_memory_limit",
        result.status is AnalysisSubprocessStatus.NON_ZERO_EXIT
        and result.exit_code == 7
        and b"memory=blocked" in result.stdout
        and result.sweep_complete is True,
        (result.status, result.exit_code, result.stdout, result.stderr[-200:]),
    )

    started = time.monotonic()
    result = execute(
        "import time\nprint('sleeping', flush=True)\ntime.sleep(60)\n",
        timeout_seconds=1.5,
    )
    elapsed = time.monotonic() - started
    check(
        "run07_wall_timeout",
        result.status is AnalysisSubprocessStatus.TIMED_OUT
        and result.sweep_complete is True
        and not result.survivors
        and elapsed < 8.0,
        (result.status, result.sweep_complete, result.survivors, round(elapsed, 2)),
    )

    survivors = {marker: marker_pids(marker) for marker in _markers}
    check("final_proc_oracle", all(not pids for pids in survivors.values()), survivors)

    if _failures:
        print("FAILED checks: %s" % ", ".join(_failures), flush=True)
        return 1
    print("ALL CHECKS PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

chmod 0644 "$battery_dir/battery.py"

timeout 120 docker run --rm \
    --network none \
    --user 10001:10001 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --pids-limit 128 \
    --memory 2g \
    --cpus 2 \
    --ulimit core=0 \
    -v "$SOURCE_TREE:/app/src:ro" \
    -v "$battery_dir:/battery:ro" \
    -e HOME=/tmp \
    -e XDG_CONFIG_HOME=/tmp/.config \
    -e XDG_CACHE_HOME=/tmp/.cache \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/app/src \
    --entrypoint python \
    "$IMAGE" \
    /battery/battery.py
