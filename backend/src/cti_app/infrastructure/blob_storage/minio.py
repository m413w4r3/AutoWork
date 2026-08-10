import asyncio
import os
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from cti_app.application.blob_storage import MaterializationMethod
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.errors import BlobIntegrityError
from cti_app.infrastructure.blob_storage.common import (
    spool_and_describe,
    verify_file,
)


class MinioBlobStore:
    """Development adapter using one physical MinIO bucket and logical prefixes."""

    def __init__(
        self,
        client: Minio,
        *,
        physical_bucket: str,
        temp_directory: Path | None = None,
    ) -> None:
        self._client = client
        self._physical_bucket = physical_bucket
        self._temp = temp_directory or Path(tempfile.gettempdir()) / "cti-app-blobs"

    async def put(self, source: BinaryIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        return await asyncio.to_thread(
            self._put_sync,
            source,
            logical_bucket=logical_bucket,
            mime_type=mime_type,
        )

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return await asyncio.to_thread(self._exists_sync, descriptor)

    async def materialize(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod:
        return await asyncio.to_thread(self._materialize_sync, descriptor, destination)

    async def delete(self, descriptor: BlobDescriptor) -> None:
        await asyncio.to_thread(
            self._client.remove_object, self._physical_bucket, descriptor.object_key
        )

    def _put_sync(self, source: BinaryIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        temporary, descriptor = spool_and_describe(
            source,
            temp_directory=self._temp,
            logical_bucket=logical_bucket,
            mime_type=mime_type,
        )
        try:
            if self._exists_sync(descriptor):
                return descriptor
            self._client.fput_object(
                self._physical_bucket,
                descriptor.object_key,
                str(temporary),
                content_type=descriptor.mime_type,
                metadata={"sha256": descriptor.sha256},
            )
            return descriptor
        finally:
            temporary.unlink(missing_ok=True)

    def _exists_sync(self, descriptor: BlobDescriptor) -> bool:
        try:
            stat = self._client.stat_object(self._physical_bucket, descriptor.object_key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return False
            raise
        if stat.size != descriptor.size:
            raise BlobIntegrityError(
                f"Object size does not match descriptor for {descriptor.sha256}"
            )
        stored_sha256 = (stat.metadata or {}).get("x-amz-meta-sha256")
        if stored_sha256 != descriptor.sha256:
            raise BlobIntegrityError(
                f"Object digest metadata does not match descriptor for {descriptor.sha256}"
            )
        return True

    def _materialize_sync(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("Refusing to replace a symbolic link")
        if destination.exists():
            verify_file(destination, descriptor)
            return "existing"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            self._client.fget_object(self._physical_bucket, descriptor.object_key, str(temporary))
            verify_file(temporary, descriptor)
            os.replace(temporary, destination)
            return "copy"
        finally:
            temporary.unlink(missing_ok=True)
