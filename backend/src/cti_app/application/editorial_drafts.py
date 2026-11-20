"""Retention-limited server storage for noncanonical editorial text."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_workspace.registry import WorkspaceOccurrenceResolution
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_draft import EditorialDraft, draft_expiry
from cti_app.domain.editorial_workspace import EditorialWorkspaceError

MAX_DRAFT_BYTES = 4 * 1024 * 1024
_DRAFT_BUCKET = "editorial-drafts"
_DRAFT_MIME = "text/plain; charset=utf-8"
_logger = logging.getLogger(__name__)


class EditorialDraftApplicationError(ValueError):
    def __init__(self, code: str, *, expires_at: datetime | None = None) -> None:
        self.code = code
        self.expires_at = expires_at
        super().__init__(code)


class EditorialDraftWorkspace(Protocol):
    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution: ...

    async def resolve_occurrence_in_uow(
        self, uow: UnitOfWork, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution: ...


@dataclass(frozen=True, slots=True)
class EditorialDraftContent:
    draft: EditorialDraft
    text: str
    is_stale: bool


class EditorialDraftService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialDraftWorkspace,
        blob_store: BlobStore,
        retention_days: int | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._blob_store = blob_store
        self._retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get(self, *, subject_id: UUID, occurrence_id: UUID) -> EditorialDraftContent:
        initial_resolution = await self._resolve(subject_id, occurrence_id)
        now = self._now()
        async with self._uow_factory() as uow:
            drafts = uow.editorial_drafts
            await self._lock_run(uow, initial_resolution.record.production_run_id)
            await drafts.lock_for_key(subject_id, occurrence_id)
            try:
                resolution = await self._workspace.resolve_occurrence_in_uow(
                    uow, subject_id, occurrence_id
                )
            except EditorialWorkspaceError:
                raise EditorialDraftApplicationError("draft_not_found") from None
            if resolution.record.production_run_id != initial_resolution.record.production_run_id:
                raise EditorialDraftApplicationError("draft_base_stale")
            draft = await drafts.get_for_update(subject_id, occurrence_id)
            if draft is None:
                raise EditorialDraftApplicationError("draft_not_found")
            if _is_expired(draft, now):
                expired_at = draft.expires_at
                blob = None
            else:
                expired_at = None
                blob = await uow.blobs.get(draft.text_blob_ref)

        if expired_at is not None:
            raise EditorialDraftApplicationError("draft_expired", expires_at=expired_at)
        if draft is None or blob is None:
            raise EditorialDraftApplicationError("draft_storage_unavailable")
        text = await self._read_verified_text(blob, draft.text_sha256)
        return EditorialDraftContent(
            draft=draft,
            text=text,
            is_stale=not _matches_current_base(draft, resolution),
        )

    async def save(
        self,
        *,
        subject_id: UUID,
        occurrence_id: UUID,
        actor_id: UUID,
        snapshot_id: UUID,
        text: str,
        expected_version: int,
    ) -> EditorialDraftContent:
        retention_days = self._valid_retention_days()
        if expected_version < 0:
            raise EditorialDraftApplicationError("draft_if_match_invalid")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise EditorialDraftApplicationError("draft_request_invalid") from None
        if len(encoded) > MAX_DRAFT_BYTES:
            raise EditorialDraftApplicationError("draft_text_too_large")
        resolution = await self._resolve(subject_id, occurrence_id)
        if (
            resolution.binding_state != "valid"
            or resolution.entry is None
            or resolution.artifact_id is None
            or resolution.artifact_version is None
            or resolution.canonical_sha256 is None
            or snapshot_id != resolution.record.snapshot_id
        ):
            raise EditorialDraftApplicationError("draft_base_stale")

        try:
            descriptor = await self._upload_and_verify(encoded)
        except EditorialDraftApplicationError as exc:
            if exc.__cause__ is not None:
                _log_save_failure(
                    exc.__cause__,
                    subject_id=subject_id,
                    occurrence_id=occurrence_id,
                    snapshot_id=snapshot_id,
                )
            raise
        now = self._now()
        expired_at: datetime | None = None
        draft: EditorialDraft | None = None
        try:
            async with self._uow_factory() as uow:
                repository = uow.editorial_drafts
                await self._lock_run(uow, resolution.record.production_run_id)
                await repository.lock_for_key(subject_id, occurrence_id)
                try:
                    current_resolution = await self._workspace.resolve_occurrence_in_uow(
                        uow, subject_id, occurrence_id
                    )
                except EditorialWorkspaceError:
                    raise EditorialDraftApplicationError("draft_base_stale") from None
                if (
                    current_resolution.record.production_run_id
                    != resolution.record.production_run_id
                    or current_resolution.record.snapshot_id != snapshot_id
                    or current_resolution.binding_state != "valid"
                    or current_resolution.artifact_id is None
                    or current_resolution.artifact_version is None
                    or current_resolution.canonical_sha256 is None
                ):
                    raise EditorialDraftApplicationError("draft_base_stale")
                artifact_id = current_resolution.artifact_id
                artifact_version = current_resolution.artifact_version
                canonical_sha256 = current_resolution.canonical_sha256
                resolution = current_resolution
                current = await repository.get_for_update(subject_id, occurrence_id)
                if current is not None and _is_expired(current, now):
                    expired_at = current.expires_at
                    await repository.delete(current.id)
                    if expected_version != 0:
                        await uow.commit()
                    else:
                        current = None
                if current is None or not _is_expired(current, now):
                    if current is not None and not _matches_current_base(current, resolution):
                        raise EditorialDraftApplicationError("draft_base_stale")
                    current_version = current.version if current is not None else 0
                    if expected_version != current_version:
                        raise EditorialDraftApplicationError("draft_version_conflict")

                    await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
                    canonical_blob = await uow.blobs.add_if_absent(
                        BlobRecord(descriptor=descriptor)
                    )
                    if not _same_content(canonical_blob.descriptor, descriptor):
                        raise EditorialDraftApplicationError("draft_storage_unavailable")
                    draft = EditorialDraft(
                        id=current.id if current is not None else uuid4(),
                        subject_id=subject_id,
                        production_run_id=resolution.record.production_run_id,
                        occurrence_id=occurrence_id,
                        base_snapshot_id=resolution.record.snapshot_id,
                        base_artifact_id=artifact_id,
                        base_artifact_version=artifact_version,
                        base_canonical_sha256=canonical_sha256,
                        actor_id=actor_id,
                        text_blob_ref=canonical_blob.id,
                        text_sha256=descriptor.sha256,
                        version=current_version + 1,
                        created_at=current.created_at if current is not None else now,
                        updated_at=now,
                        expires_at=draft_expiry(now, retention_days),
                    )
                    await repository.save(draft)
                    await uow.commit()
        except EditorialDraftApplicationError:
            raise
        except Exception as exc:
            _log_save_failure(
                exc,
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                snapshot_id=snapshot_id,
            )
            raise EditorialDraftApplicationError("draft_storage_unavailable") from exc

        if expired_at is not None and draft is None:
            raise EditorialDraftApplicationError("draft_expired", expires_at=expired_at)
        if draft is None:
            raise EditorialDraftApplicationError("draft_storage_unavailable")
        return EditorialDraftContent(draft=draft, text=text, is_stale=False)

    async def delete(
        self,
        *,
        subject_id: UUID,
        occurrence_id: UUID,
        expected_version: int,
    ) -> None:
        await self._resolve(subject_id, occurrence_id)
        async with self._uow_factory() as uow:
            repository = uow.editorial_drafts
            await repository.lock_for_key(subject_id, occurrence_id)
            current = await repository.get_for_update(subject_id, occurrence_id)
            if current is None:
                return
            if expected_version != current.version:
                raise EditorialDraftApplicationError("draft_version_conflict")
            await repository.delete(current.id)
            await uow.commit()

    async def purge(self, subject_id: UUID, occurrence_id: UUID) -> None:
        async with self._uow_factory() as uow:
            repository = uow.editorial_drafts
            await repository.lock_for_key(subject_id, occurrence_id)
            deleted = await repository.delete_for_occurrence(subject_id, occurrence_id)
            # Purge removes the SQL reference; shared blob bytes remain for offline GC.
            if deleted:
                await uow.commit()

    async def purge_expired(self, *, limit: int = 500) -> int:
        return await purge_expired_editorial_drafts(self._uow_factory, now=self._now(), limit=limit)

    async def _lock_run(self, uow: UnitOfWork, production_run_id: UUID) -> None:
        # Match the canonical writers' order: the adoption advisory lock and
        # run row lock fence the snapshot resolution against an adoption.
        await uow.editorial_proposals.lock_adoption_for_run(production_run_id)
        run = await uow.production_runs.get_for_update(production_run_id)
        if run is None:
            raise EditorialDraftApplicationError("draft_base_stale")

    async def _resolve(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        try:
            resolution = await self._workspace.resolve_occurrence(subject_id, occurrence_id)
        except EditorialWorkspaceError:
            raise EditorialDraftApplicationError("draft_not_found") from None
        if resolution.record.subject_id != subject_id:
            raise EditorialDraftApplicationError("draft_not_found")
        return resolution

    def _valid_retention_days(self) -> int:
        value = self._retention_days
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EditorialDraftApplicationError("draft_retention_unconfigured")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _upload_and_verify(self, encoded: bytes) -> BlobDescriptor:
        descriptor: BlobDescriptor | None = None
        try:
            descriptor = await self._blob_store.put(
                BytesIO(encoded), logical_bucket=_DRAFT_BUCKET, mime_type=_DRAFT_MIME
            )
            observed = await self._blob_store.read(descriptor, max_bytes=max(descriptor.size, 1))
            digest = hashlib.sha256(observed).hexdigest()
            if (
                observed != encoded
                or len(observed) != descriptor.size
                or digest != descriptor.sha256
            ):
                raise EditorialDraftApplicationError("draft_storage_unavailable")
            return descriptor
        except EditorialDraftApplicationError:
            raise
        except Exception as exc:
            raise EditorialDraftApplicationError("draft_storage_unavailable") from exc

    async def _read_verified_text(self, blob: BlobRecord, expected_sha256: str) -> str:
        descriptor = blob.descriptor
        if descriptor.sha256 != expected_sha256 or descriptor.size > MAX_DRAFT_BYTES:
            raise EditorialDraftApplicationError("draft_storage_unavailable")
        try:
            raw = await self._blob_store.read(descriptor, max_bytes=max(descriptor.size, 1))
            if len(raw) != descriptor.size or hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise EditorialDraftApplicationError("draft_storage_unavailable")
            return raw.decode("utf-8")
        except EditorialDraftApplicationError:
            raise
        except Exception:
            raise EditorialDraftApplicationError("draft_storage_unavailable") from None


async def purge_expired_editorial_drafts(
    uow_factory: UnitOfWorkFactory, *, now: datetime, limit: int = 500
) -> int:
    """Delete one locked batch of expired metadata; blob objects stay for offline GC."""
    if limit < 1:
        raise ValueError("limit must be positive")
    async with uow_factory() as uow:
        deleted_count = await uow.editorial_drafts.purge_expired(now, limit=limit)
        if deleted_count:
            await uow.commit()
        return deleted_count


def _is_expired(draft: EditorialDraft, now: datetime) -> bool:
    expires_at = draft.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def _matches_current_base(draft: EditorialDraft, resolution: WorkspaceOccurrenceResolution) -> bool:
    record = resolution.record
    return (
        draft.production_run_id == record.production_run_id
        and draft.base_snapshot_id == record.snapshot_id
        and draft.base_artifact_id == resolution.artifact_id
        and draft.base_artifact_version == resolution.artifact_version
        and draft.base_canonical_sha256 == resolution.canonical_sha256
        and resolution.binding_state == "valid"
    )


def _same_content(left: BlobDescriptor, right: BlobDescriptor) -> bool:
    """Match the bytes; the catalog may return a canonical descriptor from another bucket."""
    return left.sha256 == right.sha256 and left.size == right.size


def _log_save_failure(
    exc: BaseException,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    snapshot_id: UUID,
) -> None:
    _logger.error(
        "editorial_draft_save_error exception_class=%s subject_id=%s "
        "occurrence_id=%s snapshot_id=%s",
        type(exc).__name__,
        subject_id,
        occurrence_id,
        snapshot_id,
    )
