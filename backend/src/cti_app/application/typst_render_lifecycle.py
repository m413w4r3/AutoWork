"""Persistent, idempotent lifecycle shared by every Typst render service.

Publication and edition renders differ only in what they render and which
repository records them. Atomic acquisition, concurrent waiting, cached-output
validation, blob persistence and failure recording are identical, so they live
here once and are driven by a small per-service ``TypstRenderErrors`` table.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
    FontBundleSnapshot,
    TypstCompilationError,
    TypstCompiler,
)
from cti_app.application.typst_render_execution import (
    ExecutedTypstRender,
    TypstRenderExecutionError,
    TypstRenderExecutionErrorTypes,
    TypstRenderExecutor,
    translate_execution_error,
)
from cti_app.application.typst_render_output import (
    TypstRenderOutputError,
    read_verified_render_pdf,
)
from cti_app.application.typst_rendering import (
    TypstRenderSource,
    TypstTemplateBundle,
)
from cti_app.domain.typst_render import (
    TypstRenderAcquisition,
    TypstRenderAcquisitionOutcome,
    TypstRenderRecord,
    TypstRenderStatus,
    typst_render_retrying,
)


class TypstRenderError(Exception):
    """Base for stable render failures; ``code`` is persisted on the render row."""

    code = "typst_render_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class TypstRenderRepository[RecordT: TypstRenderRecord](Protocol):
    """The persistence port every render record repository implements."""

    async def get_by_input_hash(self, input_hash: str) -> RecordT | None: ...

    async def acquire_for_render(
        self, proposed: RecordT, *, stale_running_before: datetime
    ) -> TypstRenderAcquisition[RecordT]: ...

    async def reacquire_invalid_succeeded(
        self,
        proposed: RecordT,
        *,
        observed_output_blob_id: UUID,
        observed_output_sha256: str,
    ) -> TypstRenderAcquisition[RecordT]: ...

    async def mark_succeeded(
        self,
        render_id: UUID,
        *,
        output_blob_id: UUID,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> RecordT: ...

    async def mark_failed(
        self,
        render_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> RecordT: ...


@dataclass(frozen=True, slots=True)
class TypstRenderErrors:
    """Service-specific stable errors raised by the shared lifecycle."""

    execution: TypstRenderExecutionErrorTypes[TypstRenderError]
    in_progress: type[TypstRenderError]
    unexpected: type[TypstRenderError]
    label: str


@dataclass(frozen=True, slots=True)
class TypstRenderBuckets:
    source: str
    render_data: str
    output: str


class TypstRenderRunner[RecordT: TypstRenderRecord]:
    """Acquire one render atomically, execute Typst once, and record the outcome."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], AbstractAsyncContextManager[Any]],
        repository: Callable[[Any], TypstRenderRepository[RecordT]],
        artifact_store: ProductionArtifactStore,
        media_asset_store: MediaAssetStore,
        compiler: TypstCompiler,
        buckets: TypstRenderBuckets,
        errors: TypstRenderErrors,
        wait_poll_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 60.0,
        running_lease_seconds: float = 300.0,
    ) -> None:
        if wait_poll_interval_seconds <= 0:
            raise ValueError("wait_poll_interval_seconds must be positive")
        if wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        if running_lease_seconds <= 0:
            raise ValueError("running_lease_seconds must be positive")
        self._uow_factory = uow_factory
        self._repository = repository
        self._artifact_store = artifact_store
        self._executor = TypstRenderExecutor(media_asset_store=media_asset_store, compiler=compiler)
        self._buckets = buckets
        self._errors = errors
        self._wait_poll_interval_seconds = wait_poll_interval_seconds
        self._wait_timeout_seconds = wait_timeout_seconds
        self._running_lease_seconds = running_lease_seconds

    async def run(
        self,
        proposed: RecordT,
        *,
        template_bundle: TypstTemplateBundle,
        font_bundle: FontBundleSnapshot,
        build_source: Callable[[], TypstRenderSource],
    ) -> RecordT:
        """Return the render for ``proposed.input_hash``, compiling it at most once.

        ``build_source`` projects the canonical document into Typst inputs. It runs
        only for the owning caller and must raise the service's own stable errors.
        """
        acquired = await self._acquire(proposed)
        if acquired.status is TypstRenderStatus.SUCCEEDED:
            return acquired
        render_id = acquired.id

        source_blob_id: UUID | None = None
        render_data_blob_id: UUID | None = None

        async def persist_render_inputs(render_source: TypstRenderSource) -> None:
            nonlocal source_blob_id, render_data_blob_id
            try:
                source_blob_id = await self._artifact_store.put_bytes(
                    render_source.source_bytes,
                    bucket=self._buckets.source,
                    mime_type="text/plain; charset=utf-8",
                )
                render_data_blob_id = await self._artifact_store.put_bytes(
                    render_source.render_data_bytes,
                    bucket=self._buckets.render_data,
                    mime_type="application/json",
                )
            except Exception as exc:
                raise self._errors.execution.storage_failed(
                    "Unable to persist Typst source or render data"
                ) from exc

        try:
            render_source = build_source()
            executed = await self._execute(
                render_source, template_bundle, font_bundle, persist_render_inputs
            )
            compiled = executed.compiled_document
            try:
                output_blob_id = await self._artifact_store.put_bytes(
                    compiled.content,
                    bucket=self._buckets.output,
                    mime_type="application/pdf",
                )
            except Exception as exc:
                raise self._errors.execution.storage_failed(
                    "Unable to persist the compiled PDF"
                ) from exc
            try:
                async with self._uow_factory() as uow:
                    succeeded = await self._repository(uow).mark_succeeded(
                        render_id,
                        output_blob_id=output_blob_id,
                        output_sha256=compiled.sha256,
                        output_byte_size=compiled.byte_size,
                        source_blob_id=source_blob_id,
                        render_data_blob_id=render_data_blob_id,
                    )
                    await uow.commit()
            except Exception as exc:
                # BlobCatalogService commits every catalog row before put_bytes
                # returns. If a reference still disappears before this commit,
                # keep the database integrity failure behind the service API.
                raise self._errors.execution.storage_failed(
                    "Unable to persist the completed render"
                ) from exc
            return succeeded
        except Exception as error:
            failure = self._stable_error(error)
            try:
                async with self._uow_factory() as uow:
                    await self._repository(uow).mark_failed(
                        render_id,
                        error_code=failure.code,
                        error_message=str(failure)[:500],
                        source_blob_id=source_blob_id,
                        render_data_blob_id=render_data_blob_id,
                    )
                    await uow.commit()
            except Exception as mark_failed_error:
                failure.add_note(f"Could not persist render failure: {mark_failed_error}")
            if failure is error:
                raise
            raise failure from error

    async def _execute(
        self,
        render_source: TypstRenderSource,
        template_bundle: TypstTemplateBundle,
        font_bundle: FontBundleSnapshot,
        persist_render_inputs: Callable[[TypstRenderSource], Awaitable[None]],
    ) -> ExecutedTypstRender:
        try:
            resolved_media = await self._executor.resolve_media(render_source)
            return await self._executor.execute(
                render_source=render_source,
                template_bundle=template_bundle,
                font_bundle=font_bundle,
                resolved_media=resolved_media,
                on_workspace_ready=lambda: persist_render_inputs(render_source),
            )
        except TypstRenderExecutionError as exc:
            raise translate_execution_error(exc, self._errors.execution) from exc

    async def _acquire(self, proposed: RecordT) -> RecordT:
        """Return the render this caller owns (RUNNING) or a valid SUCCEEDED one."""
        wait_deadline: float | None = None
        acquisition: TypstRenderAcquisition[RecordT] | None = None
        while True:
            if acquisition is None:
                async with self._uow_factory() as uow:
                    acquisition = await self._repository(uow).acquire_for_render(
                        proposed,
                        stale_running_before=(
                            datetime.now(UTC) - timedelta(seconds=self._running_lease_seconds)
                        ),
                    )
                    await uow.commit()

            if acquisition.outcome is TypstRenderAcquisitionOutcome.ACQUIRED:
                return acquisition.render

            if acquisition.outcome is TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED:
                succeeded = acquisition.render
                if await self._cached_output_is_valid(succeeded):
                    return succeeded
                assert succeeded.output_blob_id is not None
                assert succeeded.output_sha256 is not None
                async with self._uow_factory() as uow:
                    acquisition = await self._repository(uow).reacquire_invalid_succeeded(
                        typst_render_retrying(succeeded, now=datetime.now(UTC)),
                        observed_output_blob_id=succeeded.output_blob_id,
                        observed_output_sha256=succeeded.output_sha256,
                    )
                    await uow.commit()
                continue

            if wait_deadline is None:
                wait_deadline = monotonic() + self._wait_timeout_seconds
            completed = await self._wait_for_render(
                proposed.input_hash, wait_deadline=wait_deadline
            )
            if completed is None or completed.status is TypstRenderStatus.FAILED:
                acquisition = None
            else:
                acquisition = TypstRenderAcquisition(
                    TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED, completed
                )

    async def _cached_output_is_valid(self, render: RecordT) -> bool:
        if render.status is not TypstRenderStatus.SUCCEEDED:
            return False
        try:
            await read_verified_render_pdf(self._artifact_store, render)
        except TypstRenderOutputError:
            return False
        return True

    async def _wait_for_render(self, input_hash: str, *, wait_deadline: float) -> RecordT | None:
        while True:
            remaining = wait_deadline - monotonic()
            if remaining <= 0:
                raise self._errors.in_progress(
                    f"{self._errors.label} render {input_hash} is still in progress"
                )
            await asyncio.sleep(min(self._wait_poll_interval_seconds, remaining))
            async with self._uow_factory() as uow:
                render = await self._repository(uow).get_by_input_hash(input_hash)
                await uow.commit()
            if render is None or render.status is TypstRenderStatus.FAILED:
                return None
            if render.status is TypstRenderStatus.SUCCEEDED:
                return render
            if render.updated_at < datetime.now(UTC) - timedelta(
                seconds=self._running_lease_seconds
            ):
                return None

    def _stable_error(self, error: Exception) -> TypstRenderError | TypstCompilationError:
        if isinstance(error, (TypstRenderError, TypstCompilationError)):
            return error
        return self._errors.unexpected(f"Unable to render the {self._errors.label.lower()}")


__all__ = [
    "TypstRenderBuckets",
    "TypstRenderError",
    "TypstRenderErrors",
    "TypstRenderRepository",
    "TypstRenderRunner",
]
