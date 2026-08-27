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
