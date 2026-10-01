from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.publication_render import (
    PublicationRender,
    PublicationRenderFormat,
    PublicationRenderStatus,
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

    async def add(self, render: PublicationRender) -> PublicationRender:
        self._session.add(
            PublicationRenderRow(
                id=render.id,
                publication_artifact_id=render.publication_artifact_id,
                renderer=render.renderer,
                renderer_version=render.renderer_version,
                template_version=render.template_version,
                template_sha256=render.template_sha256,
                compiler=render.compiler,
                compiler_version=render.compiler_version,
                format=render.format.value,
                input_hash=render.input_hash,
                source_blob_id=render.source_blob_id,
                render_data_blob_id=render.render_data_blob_id,
                output_blob_id=render.output_blob_id,
                output_sha256=render.output_sha256,
                output_byte_size=render.output_byte_size,
                status=render.status.value,
                error_code=render.error_code,
                error_message=render.error_message,
                created_at=render.created_at,
                updated_at=render.updated_at,
            )
        )
        await self._session.flush()
        return render

    async def mark_retrying(self, render_id: UUID) -> PublicationRender:
        row = await self._session.get(PublicationRenderRow, render_id)
        if row is None:
            raise LookupError("publication_render_not_found")
        updated = PublicationRender(
            **{
                **_publication_render_values(row),
                "status": PublicationRenderStatus.RUNNING,
                "error_code": None,
                "error_message": None,
                "updated_at": datetime.now(UTC),
            }
        )
        _apply_publication_render(row, updated)
        await self._session.flush()
        return updated

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
                "status": PublicationRenderStatus.SUCCEEDED,
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
                "status": PublicationRenderStatus.FAILED,
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
        if row.status != PublicationRenderStatus.RUNNING.value:
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
        "format": PublicationRenderFormat(row.format),
        "input_hash": row.input_hash,
        "source_blob_id": row.source_blob_id,
        "render_data_blob_id": row.render_data_blob_id,
        "output_blob_id": row.output_blob_id,
        "output_sha256": row.output_sha256,
        "output_byte_size": row.output_byte_size,
        "status": PublicationRenderStatus(row.status),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
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
