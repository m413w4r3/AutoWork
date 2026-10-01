from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.publication_render import PublicationRender
from cti_app.domain.typst_render import (
    TypstRenderAcquisition,
    TypstRenderAcquisitionOutcome,
    TypstRenderFormat,
    TypstRenderStatus,
    invalid_succeeded_reacquisition_outcome,
    typst_render_acquisition_outcome,
    typst_render_retrying,
)
from cti_app.infrastructure.database.models.publication_render import PublicationRenderRow


class SqlAlchemyPublicationRenderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, render_id: UUID) -> PublicationRender | None:
        row = await self._session.get(PublicationRenderRow, render_id)
        return _publication_render_from_row(row) if row is not None else None

    async def get_by_input_hash(self, input_hash: str) -> PublicationRender | None:
        row = await self._session.scalar(
            select(PublicationRenderRow).where(PublicationRenderRow.input_hash == input_hash)
        )
        return _publication_render_from_row(row) if row is not None else None

    async def acquire_for_render(
        self, proposed: PublicationRender, *, stale_running_before: datetime
    ) -> TypstRenderAcquisition[PublicationRender]:
        """Atomically create, reuse, retry, or take over a render for its input hash."""
        while True:
            inserted_id = await self._session.scalar(
                postgresql_insert(PublicationRenderRow)
                .values(**_publication_render_row_values(proposed))
                .on_conflict_do_nothing(index_elements=[PublicationRenderRow.input_hash])
                .returning(PublicationRenderRow.id)
            )
            if inserted_id is not None:
                return TypstRenderAcquisition(
                    TypstRenderAcquisitionOutcome.ACQUIRED,
                    proposed,
                )

            row = await self._session.scalar(
                select(PublicationRenderRow)
                .where(PublicationRenderRow.input_hash == proposed.input_hash)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                # A concurrent deletion can race the conflict check. Retry the
                # insert so this operation still never exposes an IntegrityError.
                continue
            existing = _publication_render_from_row(row)
            outcome = typst_render_acquisition_outcome(
                existing,
                stale_running_before=stale_running_before,
            )
            if outcome is TypstRenderAcquisitionOutcome.ACQUIRED:
                updated = typst_render_retrying(existing, now=datetime.now(UTC))
                _apply_publication_render(row, updated)
                await self._session.flush()
                return TypstRenderAcquisition(outcome, updated)
            return TypstRenderAcquisition(outcome, existing)

    async def reacquire_invalid_succeeded(
        self,
        proposed: PublicationRender,
        *,
        observed_output_blob_id: UUID,
        observed_output_sha256: str,
    ) -> TypstRenderAcquisition[PublicationRender]:
        """Take ownership of a corrupt cached output with a guarded transition."""
        update_result = await self._session.execute(
            update(PublicationRenderRow)
            .where(
                PublicationRenderRow.id == proposed.id,
                PublicationRenderRow.status == TypstRenderStatus.SUCCEEDED.value,
                PublicationRenderRow.output_blob_id == observed_output_blob_id,
                PublicationRenderRow.output_sha256.is_not_distinct_from(observed_output_sha256),
            )
            .values(
                status=TypstRenderStatus.RUNNING.value,
                error_code=None,
                error_message=None,
                updated_at=datetime.now(UTC),
            )
            .returning(PublicationRenderRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        updated_row = update_result.scalar_one_or_none()
        if updated_row is not None:
            updated = _publication_render_from_row(updated_row)
            return TypstRenderAcquisition(
                TypstRenderAcquisitionOutcome.ACQUIRED,
                updated,
            )

        row = await self._session.scalar(
            select(PublicationRenderRow)
            .where(PublicationRenderRow.id == proposed.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return await self.acquire_for_render(
                proposed,
                stale_running_before=datetime.min.replace(tzinfo=UTC),
            )

        existing = _publication_render_from_row(row)
        outcome = invalid_succeeded_reacquisition_outcome(
            existing,
            observed_output_blob_id=observed_output_blob_id,
            observed_output_sha256=observed_output_sha256,
        )
        if outcome is TypstRenderAcquisitionOutcome.ACQUIRED:
            updated = typst_render_retrying(existing, now=datetime.now(UTC))
            _apply_publication_render(row, updated)
            await self._session.flush()
            return TypstRenderAcquisition(outcome, updated)
        return TypstRenderAcquisition(outcome, existing)

    async def add(self, render: PublicationRender) -> PublicationRender:
        self._session.add(PublicationRenderRow(**_publication_render_row_values(render)))
        await self._session.flush()
        return render

    async def mark_succeeded(
        self,
        render_id: UUID,
        *,
        output_blob_id: UUID,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        row = await self._require_running(render_id)
        updated = PublicationRender(
            **{
                **_publication_render_values(row),
                "source_blob_id": source_blob_id,
                "render_data_blob_id": render_data_blob_id,
                "output_blob_id": output_blob_id,
                "output_sha256": output_sha256,
                "output_byte_size": output_byte_size,
                "status": TypstRenderStatus.SUCCEEDED,
                "error_code": None,
                "error_message": None,
                "updated_at": datetime.now(UTC),
            }
        )
        _apply_publication_render(row, updated)
        await self._session.flush()
        return updated

    async def mark_failed(
        self,
        render_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        row = await self._require_running(render_id)
        updated = PublicationRender(
            **{
                **_publication_render_values(row),
                "source_blob_id": source_blob_id,
                "render_data_blob_id": render_data_blob_id,
                "status": TypstRenderStatus.FAILED,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": datetime.now(UTC),
            }
        )
        _apply_publication_render(row, updated)
        await self._session.flush()
        return updated

    async def _require_running(self, render_id: UUID) -> PublicationRenderRow:
        row = await self._session.get(PublicationRenderRow, render_id)
        if row is None:
            raise LookupError("publication_render_not_found")
        if row.status != TypstRenderStatus.RUNNING.value:
            raise ValueError("publication_render_not_running")
        return row


def _publication_render_values(row: PublicationRenderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "publication_artifact_id": row.publication_artifact_id,
        "renderer": row.renderer,
        "renderer_version": row.renderer_version,
        "template_version": row.template_version,
        "template_sha256": row.template_sha256,
        "compiler": row.compiler,
        "compiler_version": row.compiler_version,
        "font_bundle_version": row.font_bundle_version,
        "render_policy_version": row.render_policy_version,
        "format": TypstRenderFormat(row.format),
        "input_hash": row.input_hash,
        "source_blob_id": row.source_blob_id,
        "render_data_blob_id": row.render_data_blob_id,
        "output_blob_id": row.output_blob_id,
        "output_sha256": row.output_sha256,
        "output_byte_size": row.output_byte_size,
        "status": TypstRenderStatus(row.status),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _publication_render_row_values(render: PublicationRender) -> dict[str, Any]:
    return {
        "id": render.id,
        "publication_artifact_id": render.publication_artifact_id,
        "renderer": render.renderer,
        "renderer_version": render.renderer_version,
        "template_version": render.template_version,
        "template_sha256": render.template_sha256,
        "compiler": render.compiler,
        "compiler_version": render.compiler_version,
        "font_bundle_version": render.font_bundle_version,
        "render_policy_version": render.render_policy_version,
        "format": render.format.value,
        "input_hash": render.input_hash,
        "source_blob_id": render.source_blob_id,
        "render_data_blob_id": render.render_data_blob_id,
        "output_blob_id": render.output_blob_id,
        "output_sha256": render.output_sha256,
        "output_byte_size": render.output_byte_size,
        "status": render.status.value,
        "error_code": render.error_code,
        "error_message": render.error_message,
        "created_at": render.created_at,
        "updated_at": render.updated_at,
    }


def _apply_publication_render(row: PublicationRenderRow, render: PublicationRender) -> None:
    row.source_blob_id = render.source_blob_id
    row.render_data_blob_id = render.render_data_blob_id
    row.output_blob_id = render.output_blob_id
    row.output_sha256 = render.output_sha256
    row.output_byte_size = render.output_byte_size
    row.status = render.status.value
    row.error_code = render.error_code
    row.error_message = render.error_message
    row.updated_at = render.updated_at


def _publication_render_from_row(row: PublicationRenderRow) -> PublicationRender:
    return PublicationRender(**_publication_render_values(row))
