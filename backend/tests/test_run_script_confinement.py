"""End-to-end confinement oracles for ``analysis.run_script`` (M24.1 F2-A).

These tests exercise the real kernel stack on the host: a trusted subreaper
process, the script Landlock domain, the script seccomp filter, the kernel
rlimits and the descendant sweep.  Nothing is stubbed except the production
probe for the explicit refusal test: RUN-01..RUN-09 use real processes and an
independent ``/proc`` oracle for survivors, and RUN-10 injects an unavailable
probe to prove the typed refusal.

When the production probe cannot run on this host (no Landlock, seccomp or
subreaper), the execution tests skip with the printed probe reason; they never
fall back to an unconfined run.  The refusal test still runs and must pass.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
    ConfinedScriptJobSpec,
)
from cti_app.application.confined_scripts import (
    ConfinedScriptLimits,
    ConfinedScriptRequest,
    ConfinedScriptService,
)
from cti_app.application.investigation_tools import ToolInvocationStatus
from cti_app.config import Settings
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
from cti_app.infrastructure.seccomp_network import (
    network_seccomp_filter_program,
    script_seccomp_filter_program,
)
from cti_app.workers import confined_analysis as worker_module
from cti_app.workers.confined_analysis import ScriptSandboxCapability, _run_script
from tests.investigation_c2_fakes import FakeUow
from tests.test_confined_scripts import _request, _script_fixture

_MIB = 1024 * 1024


def _require_production_confinement() -> None:
    probe = probe_confined_script_execution(sys.executable)
    if not probe.ok:
        reason = f"production script confinement unavailable: {probe.detail}"
        print(f"SKIP {reason}", file=sys.stderr)
        pytest.skip(reason)


def _pid_alive(pid: int) -> bool:
    """A re-parented zombie is dead even though ``kill(pid, 0)`` succeeds."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    return raw[raw.rfind(")") + 2] != "Z"


def _marker_pids(marker: str) -> list[int]:
    """Independent oracle: pids whose command line carries a unique marker."""
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


@dataclass(slots=True)
class _Execution:
    result: AnalysisSubprocessResult
    root: Path
    work: Path
    input_path: Path
    script_path: Path


async def _execute_script(
    tmp_path: Path,
    source: str,
    *,
    timeout_seconds: float = 5.0,
    memory_limit_bytes: int = 256 * _MIB,
    cpu_limit_seconds: int = 10,
    file_size_limit_bytes: int = _MIB,
    process_limit: int = 8,
    deny_network: bool = True,
    landlock: bool = True,
    input_bytes: bytes = b"canonical-input",
) -> _Execution:
    root = tmp_path / "exec"
    root.mkdir(mode=0o700)
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
    result = await run_analysis_subprocess(
        [sys.executable, "-I", "-u", str(script_path)],
        timeout_seconds=timeout_seconds,
        output_limit=64 * 1024,
        environment=environment,
        memory_limit_bytes=memory_limit_bytes,
        deny_network=deny_network,
        cpu_limit_seconds=cpu_limit_seconds,
        file_size_limit_bytes=file_size_limit_bytes,
        process_limit=process_limit,
        cwd=work,
        supervision=supervision,
    )
    return _Execution(result, root, work, input_path, script_path)


@pytest.mark.asyncio
async def test_run_01_simple_script_returns_the_exact_result(tmp_path: Path) -> None:
    _require_production_confinement()
    execution = await _execute_script(
        tmp_path,
        "import os, sys\n"
        "print('run-01-ok')\n"
        "print(open(os.environ['AUTOWORK_SCRIPT_INPUT'], 'rb').read().decode())\n"
        "out = os.path.join(os.environ['AUTOWORK_SCRIPT_SCRATCH'], 'out.txt')\n"
        "open(out, 'w').write('scratch')\n"
        "import subprocess\n"
        "subprocess.run([sys.executable, '-c', 'pass'], stdout=subprocess.DEVNULL)\n"
        "print('devnull-ok')\n",
    )
    result = execution.result
    assert result.status is AnalysisSubprocessStatus.SUCCEEDED, result.stderr
    assert result.exit_code == 0
    assert result.stdout == b"run-01-ok\ncanonical-input\ndevnull-ok\n"
    assert result.sweep_complete is True
    assert result.survivors == ()
    assert result.execution_id is not None
    assert len(result.execution_id) == 32
    assert (execution.work / "out.txt").read_text(encoding="utf-8") == "scratch"


@pytest.mark.asyncio
async def test_run_02_children_are_killed_at_expiry(tmp_path: Path) -> None:
    _require_production_confinement()
    marker = f"CTI-RUN02-{uuid4().hex}"
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(300)  # {marker}'"
        "])\n"
        "time.sleep(300)\n"
    )
    started = time.monotonic()
    execution = await _execute_script(tmp_path, script, timeout_seconds=1.5)
    elapsed = time.monotonic() - started
    assert execution.result.status is AnalysisSubprocessStatus.TIMED_OUT
    assert elapsed < 6, f"timeout did not stop the tree (elapsed {elapsed:.1f}s)"
    assert execution.result.sweep_complete is True
    assert execution.result.survivors == ()
    assert _marker_pids(marker) == []


@pytest.mark.asyncio
async def test_run_03a_setsid_is_denied_by_the_script_filter(tmp_path: Path) -> None:
    _require_production_confinement()
    execution = await _execute_script(
        tmp_path,
        "import errno, os\n"
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
    result = execution.result
    assert result.status is AnalysisSubprocessStatus.SUCCEEDED, result.stderr
    assert b"setsid_errno=1" in result.stdout
    assert b"child_status=0" in result.stdout
    assert result.sweep_complete is True
    assert result.survivors == ()


@pytest.mark.asyncio
async def test_run_03b_setsid_double_fork_survivors_are_swept_at_exit(
    tmp_path: Path,
) -> None:
    # Supervisor-only profile (no seccomp/Landlock), as for a bubblewrap mount
    # namespace: ``setsid`` really runs, so only the subreaper sweep can prove
    # the detached daemon is gone.
    marker = f"CTI-RUN03B-{uuid4().hex}"
    script = (
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
        "print('main-exit')\n"
    )
    execution = await _execute_script(
        tmp_path, script, deny_network=False, landlock=False, timeout_seconds=5.0
    )
    result = execution.result
    assert result.status is AnalysisSubprocessStatus.SUCCEEDED, result.stderr
    assert result.sweep_complete is True
    assert result.survivors == ()
    assert result.descendants_killed
    assert _marker_pids(marker) == []


@pytest.mark.asyncio
async def test_run_03c_setsid_chain_is_killed_on_timeout(tmp_path: Path) -> None:
    marker = f"CTI-RUN03C-{uuid4().hex}"
    script = (
        "import os, subprocess, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(300)  # {marker}'"
        "])\n"
        "    time.sleep(300)\n"
        "time.sleep(300)\n"
    )
    started = time.monotonic()
    execution = await _execute_script(
        tmp_path, script, deny_network=False, landlock=False, timeout_seconds=1.5
    )
    elapsed = time.monotonic() - started
    assert execution.result.status is AnalysisSubprocessStatus.TIMED_OUT
    assert elapsed < 6, f"timeout did not stop the setsid chain (elapsed {elapsed:.1f}s)"
    assert execution.result.sweep_complete is True
    assert execution.result.survivors == ()
    assert _marker_pids(marker) == []


@pytest.mark.asyncio
async def test_run_04_network_is_denied_for_the_script(tmp_path: Path) -> None:
    _require_production_confinement()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.3)
    try:
        port = listener.getsockname()[1]
        execution = await _execute_script(
            tmp_path,
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
            "except OSError as exc:\n"
            "    print('network_errno=%d' % exc.errno)\n"
            "else:\n"
            "    print('network=allowed')\n",
        )
        assert execution.result.status is AnalysisSubprocessStatus.SUCCEEDED
        assert b"network_errno=1" in execution.result.stdout
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_run_05_filesystem_escapes_are_denied(tmp_path: Path) -> None:
    _require_production_confinement()
    host_escape = Path("/tmp") / f"cti-run05-{uuid4().hex}"
    execution = await _execute_script(
        tmp_path,
        "import os\n"
        "scratch = os.environ['AUTOWORK_SCRIPT_SCRATCH']\n"
        f"host_escape = {str(host_escape)!r}\n"
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
    result = execution.result
    assert result.status is AnalysisSubprocessStatus.SUCCEEDED, result.stderr
    line = next(
        (item for item in result.stdout.decode().splitlines() if item.startswith("ESCAPES=")),
        None,
    )
    assert line is not None
    observed = dict(ast.literal_eval(line.split("=", 1)[1]))
    assert set(observed) == {"passwd", "proc1", "host_tmp", "dotdot", "symlink"}
    for name, code in observed.items():
        assert code in {errno.EACCES, errno.EPERM, errno.ENOENT}, f"{name} -> {code}"
    assert not host_escape.exists()
    assert not (execution.root / "escape.txt").exists()


@pytest.mark.asyncio
async def test_run_06_memory_overrun_stops_under_a_kernel_limit(tmp_path: Path) -> None:
    _require_production_confinement()
    execution = await _execute_script(
        tmp_path,
        "try:\n"
        "    bytearray(512 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('memory=blocked')\n"
        "    raise SystemExit(7)\n"
        "print('memory=allowed')\n",
        memory_limit_bytes=128 * _MIB,
    )
    result = execution.result
    assert result.status is AnalysisSubprocessStatus.NON_ZERO_EXIT
    assert result.exit_code == 7
    assert b"memory=blocked" in result.stdout
    assert result.sweep_complete is True
    assert result.survivors == ()
    assert result.usage is not None
    assert result.usage.peak_rss_bytes is not None


@pytest.mark.asyncio
async def test_run_07_wall_timeout_stops_a_sleeping_script(tmp_path: Path) -> None:
    _require_production_confinement()
    started = time.monotonic()
    execution = await _execute_script(
        tmp_path,
        "import time\nprint('sleeping', flush=True)\ntime.sleep(60)\n",
        timeout_seconds=1.5,
    )
    elapsed = time.monotonic() - started
    assert execution.result.status is AnalysisSubprocessStatus.TIMED_OUT
    assert elapsed < 6, f"wall timeout took {elapsed:.1f}s"
    assert execution.result.sweep_complete is True
    assert execution.result.survivors == ()


class _FakeBlobs:
    """Minimal blob catalog for a direct worker-handler execution."""

    def __init__(self) -> None:
        self.payloads: dict[Any, bytes] = {}
        self.ingested: list[tuple[str, str, int]] = []

    async def read(self, blob_id: Any, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("payload exceeds the test read limit")
        return payload

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        del mime_type
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.ingested.append((logical_bucket, digest, len(content)))
        return SimpleNamespace(
            id=uuid4(),
            descriptor=SimpleNamespace(
                sha256=digest, size=len(content), logical_bucket=logical_bucket
            ),
        )


async def _run_worker_script(
    source: str,
    input_bytes: bytes = b"canonical-input",
    *,
    max_processes: int = 8,
    wall_seconds: float = 60.0,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], _FakeBlobs]:
    blobs = _FakeBlobs()
    source_bytes = source.encode("utf-8")
    script_blob_id = uuid4()
    blobs.payloads[script_blob_id] = source_bytes
    spec = ConfinedScriptJobSpec(
        script_blob_id=script_blob_id,
        script_sha256=hashlib.sha256(source_bytes).hexdigest(),
        interpreter="python3",
        wall_seconds=wall_seconds,
        cpu_limit_seconds=30,
        memory_limit_bytes=256 * _MIB,
        file_size_limit_bytes=_MIB,
        scratch_max_files=32,
        scratch_max_bytes=4 * _MIB,
        stdout_max_bytes=64 * 1024,
        stderr_max_bytes=64 * 1024,
        max_processes=max_processes,
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=hashlib.sha256(input_bytes).hexdigest(),
        operation_key="confined-analysis:" + "a" * 64,
        tool=ConfinedAnalysisTool.SCRIPT,
        timeout_seconds=wall_seconds + 15.0,
        max_output_bytes=1_000_000,
        artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
        association_id=uuid4(),
        sample_id=uuid4(),
        script=spec,
    )
    payload = await _run_script(
        parameters,
        input_bytes,
        blobs=blobs,  # type: ignore[arg-type]
        settings=settings or Settings(_env_file=None, analysis_worker_enabled=True),
    )
    return payload, blobs


@pytest.mark.asyncio
async def test_run_08_result_and_diagnostics_keep_their_provenance() -> None:
    _require_production_confinement()
    marker = f"CTI-RUN08-{uuid4().hex}"
    source = (
        "import subprocess, sys\n"
        "print('run-08')\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(300)  # {marker}'"
        "])\n"
    )
    input_bytes = b"canonical-input-run-08"
    payload, blobs = await _run_worker_script(source, input_bytes)
    assert payload["status"] == "SUCCEEDED", payload
    assert payload["exit_code"] == 0
    assert payload["script_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert payload["input_sha256"] == hashlib.sha256(input_bytes).hexdigest()
    assert payload["script_size_bytes"] == len(source.encode())
    assert payload["interpreter"] == "python3"
    assert isinstance(payload["duration_seconds"], float)
    assert payload["duration_seconds"] >= 0
    assert isinstance(payload["limits"], dict) and payload["limits"]
    assert isinstance(payload["execution_id"], str) and len(payload["execution_id"]) == 32
    assert payload["sweep_complete"] is True
    assert payload["survivors"] == []
    assert payload["descendants_killed"]
    assert payload["filesystem_confinement"] in {"landlock", "bwrap_namespaces"}
    assert payload["outputs_blob_id"] is not None
    assert payload["outputs_sha256"] in {digest for _bucket, digest, _size in blobs.ingested}
    assert _marker_pids(marker) == []


@pytest.mark.asyncio
async def test_run_09_executions_are_uniquely_identified_and_bound_to_their_input() -> None:
    _require_production_confinement()
    first_payload, _ = await _run_worker_script("print('run-09-first')\n", b"input-first")
    second_payload, _ = await _run_worker_script("print('run-09-second')\n", b"input-second")
    assert first_payload["status"] == second_payload["status"] == "SUCCEEDED"
    assert first_payload["execution_id"] != second_payload["execution_id"]
    assert first_payload["script_sha256"] != second_payload["script_sha256"]
    assert first_payload["input_sha256"] == hashlib.sha256(b"input-first").hexdigest()
    assert second_payload["input_sha256"] == hashlib.sha256(b"input-second").hexdigest()


@pytest.mark.asyncio
async def test_run_09b_stale_results_are_never_reattributed() -> None:
    _require_production_confinement()
    fixture = await _script_fixture()
    script = "print('stale-result-check')\n"
    request = _request(fixture, script)
    sample_payload = b"harmless confined script input"

    class _FixedPort:
        available = True
        available_reason = None
        isolation_profile = fixture.harness.port.isolation_profile
        family_statuses = fixture.harness.port.family_statuses
        attestation = fixture.harness.port.attestation

        def __init__(self, result_payload: dict[str, Any]) -> None:
            self.result_payload = result_payload
            self.calls = 0

        async def execute(self, engine_request: Any) -> ConfinedAnalysisResult:
            self.calls += 1
            return ConfinedAnalysisResult(
                status=ToolInvocationStatus.SUCCEEDED,
                operation_key="confined-analysis:" + "b" * 64,
                artifact_id=engine_request.artifact_id,
                input_sha256=engine_request.sha256,
                payload={"result": self.result_payload},
            )

    wrong_port = _FixedPort(
        {
            "status": "SUCCEEDED",
            "script_sha256": "0" * 64,
            "input_sha256": hashlib.sha256(sample_payload).hexdigest(),
            "interpreter": "python3",
            "execution_id": "0" * 32,
        }
    )
    wrong_service = ConfinedScriptService(
        execution=wrong_port,  # type: ignore[arg-type]
        uow_factory=lambda: FakeUow(fixture.database),  # type: ignore[arg-type]
        archive=fixture.artifact_store,  # type: ignore[arg-type]
        settings=fixture.settings,
    )
    wrong_result = await wrong_service.run(request)
    assert wrong_result.status is ToolInvocationStatus.FAILED
    assert wrong_result.reason == "script_result_identity_mismatch"
    assert wrong_port.calls == 1

    execution_id = uuid4().hex
    right_port = _FixedPort(
        {
            "status": "SUCCEEDED",
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "input_sha256": hashlib.sha256(sample_payload).hexdigest(),
            "interpreter": "python3",
            "execution_id": execution_id,
            "filesystem_confinement": "landlock",
            "sweep_complete": True,
            "survivors": [],
            "descendants_killed": [4242],
            "exit_code": 0,
        }
    )
    right_service = ConfinedScriptService(
        execution=right_port,  # type: ignore[arg-type]
        uow_factory=lambda: FakeUow(fixture.database),  # type: ignore[arg-type]
        archive=fixture.artifact_store,  # type: ignore[arg-type]
        settings=fixture.settings,
    )
    right_result = await right_service.run(request)
    assert right_result.status is ToolInvocationStatus.SUCCEEDED
    assert right_result.execution_id == execution_id
    assert right_result.filesystem_confinement == "landlock"
    assert right_result.descendants_killed == (4242,)
    assert right_result.sweep_complete is True


@pytest.mark.asyncio
async def test_run_10_unavailable_confinement_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_path = Path("/tmp") / f"cti-run10-{uuid4().hex}"
    source = f"open({str(marker_path)!r}, 'w').write('executed')\nprint('must-not-run')\n"
    monkeypatch.setattr(
        worker_module,
        "probe_script_sandbox",
        lambda: ScriptSandboxCapability(None, "probe_injected_unavailable"),
    )
    payload, _ = await _run_worker_script(source)
    assert payload["status"] == "UNAVAILABLE"
    assert payload["available_reason"] == "script_confinement_unavailable"
    assert "must-not-run" not in payload.get("stdout", "")
    assert not marker_path.exists()

    class _FamilyUnavailable:
        available = True
        available_reason = None
        isolation_profile = "test"
        attestation = None
        family_statuses: ClassVar[dict[str, ConfinedAnalysisFamilyStatus]] = {
            "script": ConfinedAnalysisFamilyStatus(
                "script", False, None, "script_confinement_unavailable"
            )
        }

        async def execute(self, _request: Any) -> Any:
            raise AssertionError("an unavailable confinement must never execute a script")

    service = ConfinedScriptService(
        execution=_FamilyUnavailable(),  # type: ignore[arg-type]
        uow_factory=lambda: pytest.fail("a refused script must not resolve canonical input"),
        archive=None,
        settings=Settings(_env_file=None),
    )
    refused = await service.run(
        ConfinedScriptRequest(
            investigation_id=uuid4(),
            script=b"print('must-not-run')\n",
            limits=ConfinedScriptLimits(),
            sample_sha256="a" * 64,
        )
    )
    assert refused.status is ToolInvocationStatus.UNAVAILABLE
    assert refused.reason == "confinement_unavailable"
    assert refused.executed is False


def test_script_filter_denies_process_chain_syscalls_but_not_the_network_one() -> None:
    def syscalls(program: bytes) -> set[int]:
        values: set[int] = set()
        for offset in range(0, len(program), 8):
            code, _jt, _jf, k = (
                int.from_bytes(program[offset : offset + 2], "little"),
                program[offset + 2],
                program[offset + 3],
                int.from_bytes(program[offset + 4 : offset + 8], "little"),
            )
            if code == 0x15:  # BPF_JMP|BPF_JEQ|BPF_K
                values.add(k)
        return values

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        expected = {62, 109, 112, 234, 272, 297, 308, 424, 435}
        socket_number = 41
    elif machine in {"aarch64", "arm64"}:
        expected = {129, 154, 157, 131, 97, 240, 268, 424, 435}
        socket_number = 198
    else:
        pytest.skip(f"no syscall contract for {machine}")
    script_filter = syscalls(script_seccomp_filter_program())
    network_filter = syscalls(network_seccomp_filter_program())
    assert socket_number in network_filter
    assert expected <= script_filter
    assert not expected & network_filter


def test_production_probe_attests_the_full_confinement_stack() -> None:
    probe = probe_confined_script_execution(sys.executable)
    if not probe.ok:
        reason = f"production script confinement unavailable: {probe.detail}"
        print(f"SKIP {reason}", file=sys.stderr)
        pytest.skip(reason)
    assert probe.detail == "landlock_seccomp_subreaper_verified"
