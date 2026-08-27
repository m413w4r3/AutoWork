from cti_app.application.static_analysis import STATIC_ANALYSIS_JOB_KIND
from cti_app.infrastructure.jobs import DramatiqAnalysisJobDispatcher, DramatiqJobDispatcher
from cti_app.workers.analysis_tasks import execute_analysis_job


def test_analysis_worker_contract() -> None:
    assert STATIC_ANALYSIS_JOB_KIND == "sample.static_analysis.v1"
    assert execute_analysis_job.queue_name == "analysis"
    assert DramatiqAnalysisJobDispatcher is not DramatiqJobDispatcher
