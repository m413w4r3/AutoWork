from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.model_runs import (
    ModelOutputRejection,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
    ModelUsage,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelOutputRejectionRow,
    ModelRunRow,
)


class SqlAlchemyModelRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: ModelRun) -> None:
        self._session.add(ModelRunRow(**_model_run_values(run)))
        await self._session.flush()

    async def get(self, run_id: UUID) -> ModelRun | None:
        row = await self._session.get(ModelRunRow, run_id)
        return _model_run_from_row(row) if row else None

    async def get_for_update(self, run_id: UUID) -> ModelRun | None:
        row = await self._session.scalar(
            select(ModelRunRow).where(ModelRunRow.id == run_id).with_for_update()
        )
        return _model_run_from_row(row) if row else None

    async def find_successful_q2_checkpoint(self, checkpoint_key: str) -> ModelRun | None:
        row = await self._session.scalar(
            select(ModelRunRow)
            .where(
                ModelRunRow.status == ModelRunStatus.SUCCEEDED.value,
                ModelRunRow.parameters.contains({"q2_checkpoint_keys": [checkpoint_key]}),
            )
            .order_by(ModelRunRow.updated_at.desc(), ModelRunRow.id.desc())
            .limit(1)
        )
        return _model_run_from_row(row) if row else None

    async def save(self, run: ModelRun) -> None:
        row = await self._session.get(ModelRunRow, run.id)
        if row is None:
            raise LookupError(f"Model run {run.id} does not exist")
        for field_name, value in _model_run_values(run).items():
            setattr(row, field_name, value)


class SqlAlchemyModelOutputRejectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, rejection: ModelOutputRejection) -> None:
        self._session.add(
            ModelOutputRejectionRow(
                id=rejection.id,
                model_run_id=rejection.model_run_id,
                path=list(rejection.path),
                error_type=rejection.error_type,
                value_sha256=rejection.value_sha256,
                raw_output_reference=rejection.raw_output_reference,
                created_at=rejection.created_at,
            )
        )

    async def list_for_run(self, run_id: UUID) -> list[ModelOutputRejection]:
        rows = (
            await self._session.scalars(
                select(ModelOutputRejectionRow)
                .where(ModelOutputRejectionRow.model_run_id == run_id)
                .order_by(ModelOutputRejectionRow.created_at)
            )
        ).all()
        return [
            ModelOutputRejection(
                id=row.id,
                model_run_id=row.model_run_id,
                path=tuple(row.path),
                error_type=row.error_type,
                value_sha256=row.value_sha256,
                raw_output_reference=row.raw_output_reference,
                created_at=row.created_at,
            )
            for row in rows
        ]
        await self._session.flush()


def _model_run_values(run: ModelRun) -> dict[str, object]:
    return {
        "id": run.id,
        "provider": run.provider.value,
        "model_role": run.model_role.value,
        "requested_model": run.requested_model,
        "actual_model_version": run.actual_model_version,
        "prompt_template_id": run.prompt_template_id,
        "prompt_template_version": run.prompt_template_version,
        "authorized_input_hash": run.authorized_input_hash,
        "evidence_pack_hash": run.evidence_pack_hash,
        "parameters": run.parameters,
        "duration_ms": run.duration_ms,
        "usage": run.usage.snapshot() if run.usage else None,
        "status": run.status.value,
        "submission_state": run.submission_state.value,
        "submission_attempt": run.submission_attempt,
        "response_id": run.response_id,
        "output_references": list(run.output_references),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "error_details": run.error_details,
        "raw_output_reference": run.raw_output_reference,
        "raw_output_sha256": run.raw_output_sha256,
        "raw_output_chars": run.raw_output_chars,
        "normalized_output_reference": run.normalized_output_reference,
        "normalized_output_sha256": run.normalized_output_sha256,
        "parser_stage": run.parser_stage,
        "serializer_version": run.serializer_version,
        "normalization_version": run.normalization_version,
        "json_error_line": run.json_error_line,
        "json_error_column": run.json_error_column,
        "validation_errors": list(run.validation_errors),
        "transformations": list(run.transformations),
        "citation_count": run.citation_count,
        "extracted_url_count": run.extracted_url_count,
        "visible_citations": list(run.visible_citations),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }


def _model_run_from_row(row: ModelRunRow) -> ModelRun:
    usage = row.usage
    return ModelRun(
        id=row.id,
        provider=ModelProvider(row.provider),
        model_role=ModelRole(row.model_role),
        requested_model=row.requested_model,
        actual_model_version=row.actual_model_version,
        prompt_template_id=row.prompt_template_id,
        prompt_template_version=row.prompt_template_version,
        authorized_input_hash=row.authorized_input_hash,
        evidence_pack_hash=row.evidence_pack_hash,
        parameters=row.parameters,
        duration_ms=row.duration_ms,
        usage=(
            ModelUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                estimated=bool(usage.get("estimated", False)),
            )
            if usage
            else None
        ),
        status=ModelRunStatus(row.status),
        submission_state=ModelSubmissionState(row.submission_state),
        submission_attempt=row.submission_attempt,
        response_id=row.response_id,
        output_references=tuple(row.output_references),
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        raw_output_reference=row.raw_output_reference,
        raw_output_sha256=row.raw_output_sha256,
        raw_output_chars=row.raw_output_chars,
        normalized_output_reference=row.normalized_output_reference,
        normalized_output_sha256=row.normalized_output_sha256,
        parser_stage=row.parser_stage,
        serializer_version=row.serializer_version,
        normalization_version=row.normalization_version,
        json_error_line=row.json_error_line,
        json_error_column=row.json_error_column,
        validation_errors=tuple(row.validation_errors),
        transformations=tuple(row.transformations),
        citation_count=row.citation_count,
        extracted_url_count=row.extracted_url_count,
        visible_citations=tuple(row.visible_citations),
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )
