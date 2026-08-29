from uuid import uuid4

import pytest

from cti_app.application.capabilities import CAPA_ANALYSIS_JOB_KIND, CapabilitiesService
from cti_app.application.static_analysis import (
    STATIC_ANALYSIS_JOB_KIND,
    StaticAnalysisService,
    create_analysis_job_registry,
)
from cti_app.domain.capabilities import CapabilitySet, CapabilitySetStatus
from cti_app.infrastructure.jobs import DramatiqAnalysisJobDispatcher, DramatiqJobDispatcher
from cti_app.workers.analysis_tasks import execute_analysis_job


def test_analysis_worker_contract() -> None:
    assert STATIC_ANALYSIS_JOB_KIND == "sample.static_analysis.v1"
    assert execute_analysis_job.queue_name == "analysis"
    assert DramatiqAnalysisJobDispatcher is not DramatiqJobDispatcher


class _FakeCapabilitiesService(CapabilitiesService):
    def __init__(self, result: CapabilitySet) -> None:
        self.result = result
        self.calls = []

    async def analyze(self, sample_id):
        self.calls.append(sample_id)
        return self.result


class _ProgressContext:
    async def report_progress(self, current, total, message):
        self.progress = (current, total, message)


@pytest.mark.asyncio
async def test_analysis_registry_contains_and_invokes_capa_job() -> None:
    sample_id = uuid4()
    result = CapabilitySet(
        sample_id=sample_id,
        tool_name="capa",
        tool_version="9.4.0",
        ruleset_sha256="a" * 64,
        parameters_sha256="b" * 64,
        status=CapabilitySetStatus.SUCCEEDED,
        capabilities=(),
        errors=(),
    )
    capa = _FakeCapabilitiesService(result)
    registry = create_analysis_job_registry(object.__new__(StaticAnalysisService), capa)

    assert registry.validate(STATIC_ANALYSIS_JOB_KIND, {"sample_id": sample_id})
    parameters = registry.validate(CAPA_ANALYSIS_JOB_KIND, {"sample_id": sample_id})
    context = _ProgressContext()
    value = await registry.handler(CAPA_ANALYSIS_JOB_KIND)(
        parameters,
        context,  # type: ignore[arg-type]
    )

    assert capa.calls == [sample_id]
    assert value.endswith(f"/{result.ruleset_sha256}")
