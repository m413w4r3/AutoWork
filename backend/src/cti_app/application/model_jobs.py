from uuid import UUID

from pydantic import Field, field_validator

from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import (
    BackgroundResponsePendingError,
    ModelGateway,
    ModelGatewayError,
)
from cti_app.domain.model_runs import ModelRunStatus


class ModelBackgroundPollParameters(JobParameters):
    model_run_id: UUID
    poll_number: int = Field(default=1, ge=1, le=100)

    @field_validator("model_run_id", mode="before")
    @classmethod
    def parse_model_run_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value


def register_model_jobs(registry: JobRegistry, gateway: ModelGateway) -> None:
    async def poll_background_response(
        parameters: JobParameters, context: JobExecutionContext
    ) -> str | None:
        if not isinstance(parameters, ModelBackgroundPollParameters):
            raise TypeError("Invalid model background polling parameters")
        await context.heartbeat()
        try:
            execution = await gateway.resume(parameters.model_run_id)
        except BackgroundResponsePendingError as exc:
            raise JobHandlerError(
                "model_response_pending",
                "La réponse du modèle est toujours en cours.",
                transient=True,
            ) from exc
        except ModelGatewayError as exc:
            raise JobHandlerError(
                str(getattr(exc, "code", "model_response_failed")),
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
            ) from exc
        if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
            raise JobHandlerError(
                execution.run.error_code or "model_response_needs_review",
                execution.run.error_message or "La réponse du modèle requiert une réconciliation.",
                transient=False,
            )
        return execution.run.output_references[0]

    registry.register(
        "model.openai.background.poll",
        ModelBackgroundPollParameters,
        poll_background_response,
    )
