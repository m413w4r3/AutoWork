import sys

import pytest

from cti_app.infrastructure.analysis_subprocess import AnalysisSubprocessStatus, run_analysis_subprocess


@pytest.mark.asyncio
async def test_subprocess_success_nonzero_timeout_and_limit() -> None:
    ok = await run_analysis_subprocess([sys.executable, "-c", "print('ok')"])
    assert ok.status is AnalysisSubprocessStatus.SUCCEEDED
    bad = await run_analysis_subprocess([sys.executable, "-c", "raise SystemExit(3)"])
    assert bad.status is AnalysisSubprocessStatus.NON_ZERO_EXIT
    slow = await run_analysis_subprocess([sys.executable, "-c", "import time; time.sleep(1)"], timeout_seconds=.01)
    assert slow.status is AnalysisSubprocessStatus.TIMED_OUT
    capped = await run_analysis_subprocess([sys.executable, "-c", "print('x'*100)"], output_limit=10)
    assert capped.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
