import json
import sys

import pytest

from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
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
