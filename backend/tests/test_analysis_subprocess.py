import errno
import json
import os
import sys

import pytest

from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    get_process_dumpable,
    run_analysis_subprocess,
    set_process_dumpable,
)


@pytest.mark.asyncio
async def test_subprocess_success_nonzero_timeout_and_limit() -> None:
    ok = await run_analysis_subprocess([sys.executable, "-c", "print('ok')"])
    assert ok.status is AnalysisSubprocessStatus.SUCCEEDED
    bad = await run_analysis_subprocess([sys.executable, "-c", "raise SystemExit(3)"])
    assert bad.status is AnalysisSubprocessStatus.NON_ZERO_EXIT
    slow = await run_analysis_subprocess(
        [sys.executable, "-c", "import time; time.sleep(1)"], timeout_seconds=0.01
    )
    assert slow.status is AnalysisSubprocessStatus.TIMED_OUT
    capped = await run_analysis_subprocess(
        [sys.executable, "-c", "print('x'*100)"], output_limit=10
    )
    assert capped.status is AnalysisSubprocessStatus.OUTPUT_LIMIT


@pytest.mark.asyncio
async def test_subprocess_caps_each_stream_while_producing_many_megabytes() -> None:
    script = (
        "import sys; sys.stdout.write('o' * (4 * 1024 * 1024)); "
        "sys.stderr.write('e' * (4 * 1024 * 1024))"
    )
    result = await run_analysis_subprocess(
        [sys.executable, "-c", script], output_limit=17, timeout_seconds=5
    )
    assert result.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
    assert len(result.stdout) <= 17
    assert len(result.stderr) <= 17


@pytest.mark.asyncio
async def test_subprocess_passes_home_and_xdg_directories_but_not_other_variables() -> None:
    # jadx (HotSpot) creates config/cache directories under HOME/XDG_*; the worker root
    # filesystem is read-only, so the caller must be able to redirect them to its tmpfs.
    script = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) for key in "
        "('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'HTTPS_PROXY')}))"
    )

    result = await run_analysis_subprocess(
        [sys.executable, "-c", script],
        timeout_seconds=5,
        output_limit=1024,
        environment={
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/jadx-home",
            "XDG_CONFIG_HOME": "/tmp/jadx-home/config",
            "XDG_CACHE_HOME": "/tmp/jadx-home/cache",
            "XDG_DATA_HOME": "/tmp/jadx-home/data",
            "HTTPS_PROXY": "http://127.0.0.1:9",
        },
    )

    assert result.status is AnalysisSubprocessStatus.SUCCEEDED
    assert json.loads(result.stdout) == {
        "HOME": "/tmp/jadx-home",
        "XDG_CONFIG_HOME": "/tmp/jadx-home/config",
        "XDG_CACHE_HOME": "/tmp/jadx-home/cache",
        "XDG_DATA_HOME": "/tmp/jadx-home/data",
        "HTTPS_PROXY": None,
    }


@pytest.mark.asyncio
async def test_subprocess_applies_dotnet_address_space_and_gc_environment() -> None:
    address_space_limit_bytes = 8 * 1024**3
    script = (
        "import json, os, resource; "
        "print(json.dumps({'address_space': resource.getrlimit(resource.RLIMIT_AS), "
        "'gc_heap': os.environ.get('DOTNET_GCHeapHardLimit'), "
        "'gc_concurrent': os.environ.get('DOTNET_gcConcurrent'), "
        "'dotnet_root': os.environ.get('DOTNET_ROOT'), "
        "'nuget_packages': os.environ.get('NUGET_PACKAGES'), "
        "'unexpected': os.environ.get('LD_PRELOAD')}))"
    )

    result = await run_analysis_subprocess(
        [sys.executable, "-c", script],
        timeout_seconds=5,
        output_limit=1024,
        memory_limit_bytes=address_space_limit_bytes,
        environment={
            "PATH": "/usr/bin:/bin",
            "DOTNET_ROOT": "/usr/share/dotnet",
            "DOTNET_GCHeapHardLimit": "0x10000000",
            "DOTNET_gcConcurrent": "0",
            "NUGET_PACKAGES": "/nonexistent",
            "LD_PRELOAD": "/tmp/ignored.so",
        },
    )

    assert result.status is AnalysisSubprocessStatus.SUCCEEDED
    payload = json.loads(result.stdout)
    assert payload["address_space"] == [address_space_limit_bytes, address_space_limit_bytes]
    assert payload["gc_heap"] == "0x10000000"
    assert payload["gc_concurrent"] == "0"
    assert payload["dotnet_root"] == "/usr/share/dotnet"
    assert payload["nuget_packages"] == "/nonexistent"
    assert payload["unexpected"] is None


@pytest.mark.asyncio
async def test_subprocess_can_deny_network_syscalls() -> None:
    script = "import socket; s = socket.socket(); s.close(); print('opened')"
    result = await run_analysis_subprocess(
        [sys.executable, "-c", script],
        timeout_seconds=5,
        output_limit=1024,
        environment={"PATH": "/usr/bin:/bin"},
        deny_network=True,
    )
    assert result.status is AnalysisSubprocessStatus.NON_ZERO_EXIT
    assert b"PermissionError" in result.stderr


@pytest.mark.asyncio
async def test_subprocess_stops_when_output_tree_exceeds_limits(tmp_path) -> None:
    output = tmp_path / "tool-output"
    output.mkdir()
    script = (
        "import pathlib, sys, time; p=pathlib.Path(sys.argv[1]); "
        "(p/'large.bin').write_bytes(b'x'*4096); time.sleep(2)"
    )
    result = await run_analysis_subprocess(
        [sys.executable, "-c", script, str(output)],
        timeout_seconds=5,
        output_limit=1024,
        directory_limit_path=output,
        directory_max_bytes=1024,
        directory_max_files=10,
    )
    assert result.status is AnalysisSubprocessStatus.DIRECTORY_LIMIT


@pytest.mark.asyncio
async def test_worker_and_child_cannot_read_parent_proc_environment_or_memory() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("PR_SET_DUMPABLE and procfs checks require Linux")
    try:
        fd = os.open("/proc/self/environ", os.O_RDONLY)
        os.close(fd)
        original_dumpable = get_process_dumpable()
    except OSError as exc:
        pytest.skip(f"prctl or /proc is unavailable: {exc}")

    script = """
import errno
import json
import os

fd = os.open("/proc/self/environ", os.O_RDONLY)
os.close(fd)
parent_errors = []
for name in ("environ", "mem"):
    try:
        fd = os.open(f"/proc/{os.getppid()}/{name}", os.O_RDONLY)
        os.close(fd)
        parent_errors.append(None)
    except OSError as exc:
        parent_errors.append(exc.errno)
print(json.dumps({"self_environ_readable": True, "parent_errors": parent_errors}))
"""

    try:
        # Importing the actual Dramatiq module applies the startup hardening.
        from cti_app.workers import analysis_tasks  # noqa: F401

        assert get_process_dumpable() == 0
        result = await run_analysis_subprocess(
            [sys.executable, "-c", script],
            timeout_seconds=10,
            output_limit=2048,
        )
        assert result.status is AnalysisSubprocessStatus.SUCCEEDED, result.stderr
        observed = json.loads(result.stdout)
        assert observed["self_environ_readable"] is True
        assert len(observed["parent_errors"]) == 2
        assert all(error in {errno.EACCES, errno.EPERM} for error in observed["parent_errors"])
    finally:
        set_process_dumpable(original_dumpable)
