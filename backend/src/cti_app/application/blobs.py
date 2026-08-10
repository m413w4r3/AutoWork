from typing import BinaryIO
from uuid import UUID

from cti_app.application.blob_storage import BlobStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.errors import (
    BlobIntegrityError,
    BlobStillReferencedError,
    EntityNotFoundError,
)


class BlobCatalogService:
    """Coordinates content storage with canonical PostgreSQL metadata."""

    def __init__(self, store: BlobStore, uow_factory: UnitOfWorkFactory) -> None:
        self._store = store
        self._uow_factory = uow_factory

    async def ingest(self, source: BinaryIO, *, logical_bucket: str, mime_type: str) -> BlobRecord:
        descriptor = await self._store.put(
            source, logical_bucket=logical_bucket, mime_type=mime_type
        )
        async with self._uow_factory() as uow:
            existing = await uow.blobs.get_by_address(logical_bucket, descriptor.sha256)
            if existing is not None:
                self._ensure_same_metadata(existing.descriptor, descriptor)
                return existing
            blob = BlobRecord(descriptor=descriptor)
            await uow.blobs.add(blob)
            await uow.commit()
            return blob

    async def delete_unreferenced(self, blob_id: UUID) -> None:
        async with self._uow_factory() as uow:
            blob = await uow.blobs.get(blob_id)
            if blob is None:
                raise EntityNotFoundError(f"Blob {blob_id} does not exist")
            reference_count = await uow.blobs.count_references(blob_id)
            if reference_count:
                raise BlobStillReferencedError(
                    f"Blob {blob_id} still has {reference_count} canonical reference(s)"
                )
            await uow.blobs.delete(blob_id)
            await uow.commit()
        await self._store.delete(blob.descriptor)

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        async with self._uow_factory() as uow:
            blob = await uow.blobs.get(blob_id)
            if blob is None:
                raise EntityNotFoundError(f"Blob {blob_id} does not exist")
        return await self._store.read(blob.descriptor, max_bytes=max_bytes)

    @staticmethod
    def _ensure_same_metadata(current: BlobDescriptor, requested: BlobDescriptor) -> None:
        if current.size != requested.size or current.mime_type != requested.mime_type:
            raise BlobIntegrityError(
                "Existing content address has conflicting size or MIME metadata"
            )
