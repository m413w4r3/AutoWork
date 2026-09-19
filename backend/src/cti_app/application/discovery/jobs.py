from __future__ import annotations

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    ReprocessDiscoveryReportParameters,
)
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.discovery_report_parser import ReportParsingError
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import ModelGatewayError
from cti_app.logging import get_correlation_id

DISCOVERY_JOB_KIND = "discover_edition"
REPROCESS_DISCOVERY_REPORT_JOB_KIND = "reprocess_discovery_report"


def register_discovery_jobs(registry: JobRegistry, service: DiscoveryService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, DiscoverEditionParameters):
            raise TypeError("Invalid discovery parameters")
        try:
            batch = await service.discover_edition(parameters, context)
        except (ModelGatewayError, ReportParsingError) as exc:
            details = None
            if isinstance(exc, ReportParsingError):
                details = {
                    "phase": "local_parsing",
                    "research_model_run_id": (
                        str(exc.research_model_run_id)
                        if exc.research_model_run_id is not None
                        else None
                    ),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": exc.research_model_run_id is not None,
                }
            else:
                details = {
                    "correlation_id": get_correlation_id(),
                }
            error_code = str(getattr(exc, "code", "research_failed"))
            if error_code == "bridge_unreachable":
                error_code = "bridge_unavailable"
            raise JobHandlerError(
                error_code,
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(
        DISCOVERY_JOB_KIND,
        DiscoverEditionParameters,
        handler,
        resume_after_worker_loss=True,
    )

    async def reprocess_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, ReprocessDiscoveryReportParameters):
            raise TypeError("Invalid discovery report reprocessing parameters")
        try:
            batch = await service.reprocess_archived_report(parameters, context)
        except (ModelGatewayError, ReportParsingError) as exc:
            details = None
            if isinstance(exc, ReportParsingError):
                details = {
                    "phase": "local_parsing",
                    "research_model_run_id": (
                        str(exc.research_model_run_id)
                        if exc.research_model_run_id is not None
                        else None
                    ),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": exc.research_model_run_id is not None,
                }
            else:
                details = {"correlation_id": get_correlation_id()}
            error_code = str(getattr(exc, "code", "research_failed"))
            raise JobHandlerError(
                error_code,
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(
        REPROCESS_DISCOVERY_REPORT_JOB_KIND,
        ReprocessDiscoveryReportParameters,
        reprocess_handler,
        resume_after_worker_loss=True,
    )
