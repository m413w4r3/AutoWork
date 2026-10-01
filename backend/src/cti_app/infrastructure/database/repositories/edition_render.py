from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.edition_render import (
    EditionRender,
    EditionRenderAcquisition,
    EditionRenderAcquisitionOutcome,
    EditionRenderFormat,
    EditionRenderStatus,
    edition_render_acquisition_outcome,
    edition_render_retrying,
    invalid_succeeded_edition_render_reacquisition_outcome,
)
from cti_app.infrastructure.database.models.edition_render import EditionRenderRow


class SqlAlchemyEditionRenderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, render_id: UUID) -> EditionRender | None:
        row = await self._session.get(EditionRenderRow, render_id)
        return _edition_render_from_row(row) if row is not None else None

    async def get_by_input_hash(self, input_hash: str) -> EditionRender | None:
        row = await self._session.scalar(
            select(EditionRenderRow).where(EditionRenderRow.input_hash == input_hash)
        )
        return _edition_render_from_row(row) if row is not None else None

    async def get_latest_for_release(self, edition_release_id: UUID) -> EditionRender | None:
        row = await self._session.scalar(
            select(EditionRenderRow)
            .where(EditionRenderRow.edition_release_id == edition_release_id)
            .order_by(EditionRenderRow.created_at.desc(), EditionRenderRow.id.desc())
            .limit(1)
        )
        return _edition_render_from_row(row) if row is not None else None

    async def get_latest_succeeded_for_release(
        self, edition_release_id: UUID
    ) -> EditionRender | None:
        row = await self._session.scalar(
            select(EditionRenderRow)
            .where(
                EditionRenderRow.edition_release_id == edition_release_id,
                EditionRenderRow.status == EditionRenderStatus.SUCCEEDED.value,
            )
            .order_by(EditionRenderRow.created_at.desc(), EditionRenderRow.id.desc())
            .limit(1)
        )
        return _edition_render_from_row(row) if row is not None else None

    async def acquire_for_render(
        self, proposed: EditionRender, *, stale_running_before: datetime
    ) -> EditionRenderAcquisition:
        """Atomically create, reuse, retry, or take over a render for its input hash."""
        while True:
            inserted_id = await self._session.scalar(
                postgresql_insert(EditionRenderRow)
                .values(**_edition_render_row_values(proposed))
                .on_conflict_do_nothing(index_elements=[EditionRenderRow.input_hash])
                .returning(EditionRenderRow.id)
            )
            if inserted_id is not None:
                return EditionRenderAcquisition(
                    EditionRenderAcquisitionOutcome.ACQUIRED,
                    proposed,
                )

            row = await self._session.scalar(
                select(EditionRenderRow)
                .where(EditionRenderRow.input_hash == proposed.input_hash)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                # A concurrent deletion can race the conflict check. Retry the
                # insert so this operation still never exposes an IntegrityError.
                continue
            existing = _edition_render_from_row(row)
            outcome = edition_render_acquisition_outcome(
                existing,
                stale_running_before=stale_running_before,
            )
            if outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
                updated = edition_render_retrying(existing, now=datetime.now(UTC))
                _apply_edition_render(row, updated)
                await self._session.flush()
                return EditionRenderAcquisition(outcome, updated)
            return EditionRenderAcquisition(outcome, existing)

    async def reacquire_invalid_succeeded(
        self,
        proposed: EditionRender,
        *,
        observed_output_blob_id: UUID,
        observed_output_sha256: str,
    ) -> EditionRenderAcquisition:
        """Take ownership of a corrupt cached output with a guarded transition."""
        update_result = await self._session.execute(
            update(EditionRenderRow)
            .where(
                EditionRenderRow.id == proposed.id,
                EditionRenderRow.status == EditionRenderStatus.SUCCEEDED.value,
                EditionRenderRow.output_blob_id == observed_output_blob_id,
                EditionRenderRow.output_sha256.is_not_distinct_from(observed_output_sha256),
            )
            .values(
                status=EditionRenderStatus.RUNNING.value,
                error_code=None,
                error_message=None,
                updated_at=datetime.now(UTC),
            )
            .returning(EditionRenderRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        updated_row = update_result.scalar_one_or_none()
        if updated_row is not None:
            updated = _edition_render_from_row(updated_row)
            return EditionRenderAcquisition(
                EditionRenderAcquisitionOutcome.ACQUIRED,
                updated,
            )

        row = await self._session.scalar(
            select(EditionRenderRow)
            .where(EditionRenderRow.id == proposed.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return await self.acquire_for_render(
                proposed,
                stale_running_before=datetime.min.replace(tzinfo=UTC),
            )

        existing = _edition_render_from_row(row)
        outcome = invalid_succeeded_edition_render_reacquisition_outcome(
            existing,
            observed_output_blob_id=observed_output_blob_id,
            observed_output_sha256=observed_output_sha256,
        )
        if outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
            updated = edition_render_retrying(existing, now=datetime.now(UTC))
            _apply_edition_render(row, updated)
            await self._session.flush()
            return EditionRenderAcquisition(outcome, updated)
        return EditionRenderAcquisition(outcome, existing)

    async def mark_succeeded(
        self,
        render_id: UUID,
        *,
        output_blob_id: UUID,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> EditionRender:
        row = await self._require_running(render_id)
        updated = EditionRender(
            **{
                **_edition_render_values(row),
                "source_blob_id": source_blob_id,
                "render_data_blob_id": render_data_blob_id,
                "output_blob_id": output_blob_id,
                "output_sha256": output_sha256,
                "output_byte_size": output_byte_size,
                "status": EditionRenderStatus.SUCCEEDED,
                "error_code": None,
                "error_message": None,
                "updated_at": datetime.now(UTC),
            }
        )
        _apply_edition_render(row, updated)
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
    ) -> EditionRender:
        row = await self._require_running(render_id)
        updated = EditionRender(
            **{
                **_edition_render_values(row),
                "source_blob_id": source_blob_id,
                "render_data_blob_id": render_data_blob_id,
                "status": EditionRenderStatus.FAILED,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": datetime.now(UTC),
            }
        )
        _apply_edition_render(row, updated)
        await self._session.flush()
        return updated

    async def _require_running(self, render_id: UUID) -> EditionRenderRow:
        row = await self._session.get(EditionRenderRow, render_id)
        if row is None:
            raise LookupError("edition_render_not_found")
        if row.status != EditionRenderStatus.RUNNING.value:
            raise ValueError("edition_render_not_running")
        return row


def _edition_render_values(row: EditionRenderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "edition_release_id": row.edition_release_id,
        "renderer": row.renderer,
        "renderer_version": row.renderer_version,
        "template_version": row.template_version,
        "template_sha256": row.template_sha256,
        "compiler": row.compiler,
        "compiler_version": row.compiler_version,
        "font_bundle_version": row.font_bundle_version,
        "render_policy_version": row.render_policy_version,
        "format": EditionRenderFormat(row.format),
        "input_hash": row.input_hash,
        "source_blob_id": row.source_blob_id,
        "render_data_blob_id": row.render_data_blob_id,
        "output_blob_id": row.output_blob_id,
        "output_sha256": row.output_sha256,
        "output_byte_size": row.output_byte_size,
        "status": EditionRenderStatus(row.status),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _edition_render_row_values(render: EditionRender) -> dict[str, Any]:
    return {
        "id": render.id,
        "edition_release_id": render.edition_release_id,
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


def _apply_edition_render(row: EditionRenderRow, render: EditionRender) -> None:
    row.source_blob_id = render.source_blob_id
    row.render_data_blob_id = render.render_data_blob_id
    row.output_blob_id = render.output_blob_id
    row.output_sha256 = render.output_sha256
    row.output_byte_size = render.output_byte_size
    row.status = render.status.value
    row.error_code = render.error_code
    row.error_message = render.error_message
    row.updated_at = render.updated_at


def _edition_render_from_row(row: EditionRenderRow) -> EditionRender:
    return EditionRender(**_edition_render_values(row))
