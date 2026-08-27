from uuid import UUID


class DramatiqJobDispatcher:
    """Redis-backed dispatcher; the database remains the source of job state."""

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        from cti_app.workers.tasks import execute_job

        execute_job.send_with_options(args=(str(job_id),), delay=max(0, delay_ms))


class DramatiqAnalysisJobDispatcher:
    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        from cti_app.workers.analysis_tasks import execute_analysis_job
        execute_analysis_job.send_with_options(args=(str(job_id),), delay=max(0, delay_ms))
