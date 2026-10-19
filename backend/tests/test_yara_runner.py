from __future__ import annotations

import builtins
import hashlib
import importlib.util
import signal
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import cti_app.infrastructure.yara_runner as yara_runner_module
from cti_app.application.rule_workbench import YaraSampleInput
from cti_app.domain.rule_workbench import YaraEngineResult, YaraExecutionStatus
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner, YaraRunnerLimits


def _sample(data: bytes) -> YaraSampleInput:
    return YaraSampleInput(sample_sha256=hashlib.sha256(data).hexdigest(), data=data)


def test_isolated_worker_compiles_and_matches_synthetic_fixture() -> None:
    runner = IsolatedYaraRunner()
    source = """rule local_fixture {
    strings:
        $needle = "Qz9Vn5Hs" ascii
    condition:
        $needle
}
"""
    match = _sample(b"harmless synthetic fixture Qz9Vn5Hs")
    no_match = _sample(b"harmless synthetic fixture with no marker")

    compiled = runner.compile(source)
    result = runner.match(source, (match, no_match))

    assert compiled.status is YaraExecutionStatus.OK
    assert compiled.engine_version == "4.5.0"
    assert result.status is YaraExecutionStatus.OK
    assert result.matches_sha256 == (match.sample_sha256,)


def test_available_runs_and_caches_a_match_probe_through_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text("# synthetic worker marker\n", encoding="utf-8")
    runner = IsolatedYaraRunner()
    monkeypatch.setattr(runner, "_worker_path", worker)
    calls: list[tuple[str, str, tuple[YaraSampleInput, ...]]] = []

    def run_worker(
        operation: str, source: str, samples: tuple[YaraSampleInput, ...]
    ) -> YaraEngineResult:
        calls.append((operation, source, samples))
        return YaraEngineResult(
            status=YaraExecutionStatus.OK,
            engine_version="4.5.0",
            matches_sha256=tuple(sample.sample_sha256 for sample in samples),
            message="",
        )

    monkeypatch.setattr(runner, "_run_worker", run_worker)

    assert runner.available is True
    assert runner.available is True
    assert len(calls) == 1
    operation, source, samples = calls[0]
    assert operation == "match"
    assert "condition:" in source
    assert len(samples) == 1
    assert samples[0].data == b"autowork-yara-availability-probe"


def test_compile_error_is_typed_with_source_line() -> None:
    result = IsolatedYaraRunner().compile("rule broken {\n strings:\n condition:\n}")

    assert result.status is YaraExecutionStatus.COMPILE_ERROR
    assert result.line is not None
    assert len(result.message) <= 1000


def test_limits_return_resource_exceeded_without_starting_engine() -> None:
    runner = IsolatedYaraRunner(YaraRunnerLimits(maximum_source_bytes=8))

    result = runner.compile("rule this is too long")

    assert result.status is YaraExecutionStatus.RESOURCE_EXCEEDED


def test_strings_per_rule_limit_is_positive_and_bounded() -> None:
    with pytest.raises(ValueError, match="positive"):
        YaraRunnerLimits(maximum_strings_per_rule=0)
    with pytest.raises(ValueError, match="<= 10000"):
        YaraRunnerLimits(maximum_strings_per_rule=10_001)


def test_unavailable_engine_is_a_local_typed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = tmp_path / "missing_engine_worker.py"
    worker.write_text(
        "import json; print(json.dumps({'status':'engine_unavailable',"
        "'engine_version':'unavailable','matches_sha256':[],'message':'simulated'}))",
        encoding="utf-8",
    )
    runner = IsolatedYaraRunner()
    monkeypatch.setattr(runner, "_worker_path", worker)

    result = runner.compile("rule local_fixture { condition: true }")

    assert result.status is YaraExecutionStatus.ENGINE_UNAVAILABLE
    assert result.engine_version == "unavailable"


def test_costly_regex_is_stopped_by_worker_limits() -> None:
    runner = IsolatedYaraRunner(
        YaraRunnerLimits(
            timeout_seconds=3.0,
            cpu_seconds=1,
            memory_bytes=512 * 1024 * 1024,
            maximum_sample_bytes=16 * 1024 * 1024,
            maximum_total_sample_bytes=16 * 1024 * 1024,
        )
    )
    source = r"""rule bounded_regex {
    strings:
        $expensive = /(a|aa)+b/
    condition:
        $expensive
}
"""
    synthetic = _sample(b"a" * (8 * 1024 * 1024) + b"!")

    result = runner.match(source, (synthetic,))

    assert result.status in {
        YaraExecutionStatus.TIMEOUT,
        YaraExecutionStatus.RESOURCE_EXCEEDED,
    }


def _load_worker_module():
    worker_path = Path(__file__).parents[1] / "src/cti_app/infrastructure/yara_runner_worker.py"
    spec = importlib.util.spec_from_file_location("yara_runner_worker_test", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_socket = socket.socket
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    try:
        spec.loader.exec_module(module)
    finally:
        socket.socket = original_socket
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
    return module


@pytest.mark.skipif(sys.platform != "linux", reason="the YARA seccomp barrier is Linux-only")
def test_worker_installs_network_barrier_before_importing_yara_with_eperm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load_worker_module()
    source_path = tmp_path / "rule.yar"
    source_path.write_text("rule harmless { condition: true }", encoding="ascii")
    events: list[tuple[str, dict[str, object]]] = []
    socket_errors: list[int | None] = []
    real_import = builtins.__import__

    class FakeYara:
        __version__ = "test"
        SyntaxError = type("SyntaxError", (Exception,), {})
        TimeoutError = type("TimeoutError", (Exception,), {})

        @staticmethod
        def set_config(**_kwargs: object) -> None:
            return None

        @staticmethod
        def compile(*_args: object, **_kwargs: object) -> object:
            return object()

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yara":
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError as error:
                socket_errors.append(error.errno)
            else:
                socket_errors.append(None)
            return FakeYara
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(worker, "_set_limits", lambda *_args: None)
    monkeypatch.setattr(worker, "_emit", lambda status, **kwargs: events.append((status, kwargs)))
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return_code = executor.submit(
            worker.main,
            [
                "worker",
                "compile",
                str(source_path),
                "134217728",
                "5",
                "65536",
                "2",
                "10000",
            ],
        ).result(timeout=5)

    assert return_code == 0
    assert socket_errors == [1]  # EPERM from the installed seccomp filter.
    assert events[0][0] == "ok"


def test_worker_fails_closed_when_network_barrier_installation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load_worker_module()
    source_path = tmp_path / "rule.yar"
    source_path.write_text("rule harmless { condition: true }", encoding="ascii")
    events: list[tuple[str, dict[str, object]]] = []
    imported_yara = False
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        nonlocal imported_yara
        if name == "yara":
            imported_yara = True
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(worker, "_set_limits", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "install_network_seccomp_filter",
        lambda: (_ for _ in ()).throw(OSError("simulated seccomp failure")),
    )
    monkeypatch.setattr(worker, "_emit", lambda status, **kwargs: events.append((status, kwargs)))
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    worker.main(["worker", "compile", str(source_path), "134217728", "5", "65536", "2", "10000"])

    assert events[0][0] == "engine_unavailable"
    assert imported_yara is False


def test_worker_timeout_kills_the_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    popen_options: list[dict[str, object]] = []

    class TimedOutWorker:
        pid = 43210

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("yara worker", timeout)
            return -signal.SIGKILL

    def fake_popen(*_args, **kwargs):
        popen_options.append(kwargs)
        return TimedOutWorker()

    monkeypatch.setattr(yara_runner_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(yara_runner_module.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    runner = IsolatedYaraRunner(YaraRunnerLimits(timeout_seconds=0.05))

    result = runner.compile("rule harmless { condition: true }")

    assert result.status is YaraExecutionStatus.TIMEOUT
    assert popen_options[0]["start_new_session"] is True
    assert calls == [(43210, signal.SIGKILL)]
