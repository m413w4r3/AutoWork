import os
import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from cti_app.application.blob_storage import MaterializationMethod
from cti_app.domain.blobs import BlobDescriptor
from cti_app.infrastructure.blob_storage.common import (
    spool_and_describe,
    verify_file,
)


class FilesystemBlobStore:
    """Filesystem adapter reserved for isolated tests."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._temp = self._root / ".tmp"
        self._temp.mkdir(parents=True, exist_ok=True)

    async def put(self, source: BinaryIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        return self._put_sync(source, logical_bucket=logical_bucket, mime_type=mime_type)

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return self._exists_sync(descriptor)

    async def materialize(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod:
        return self._materialize_sync(descriptor, destination)

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self._path_for(descriptor).unlink(missing_ok=True)

    def _put_sync(self, source: BinaryIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        temporary, descriptor = spool_and_describe(
            source,
            temp_directory=self._temp,
            logical_bucket=logical_bucket,
            mime_type=mime_type,
        )
        destination = self._path_for(descriptor)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if destination.exists():
                verify_file(destination, descriptor)
                return descriptor
            os.replace(temporary, destination)
            return descriptor
        finally:
            temporary.unlink(missing_ok=True)

    def _exists_sync(self, descriptor: BlobDescriptor) -> bool:
        path = self._path_for(descriptor)
        if not path.exists():
            return False
        verify_file(path, descriptor)
        return True

    def _materialize_sync(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod:
        source = self._path_for(descriptor)
        verify_file(source, descriptor)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("Refusing to replace a symbolic link")
        if destination.exists():
            verify_file(destination, descriptor)
            return "existing"
        try:
            os.link(source, destination)
            return "hardlink"
        except OSError:
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                verify_file(temporary, descriptor)
                os.replace(temporary, destination)
                return "copy"
            finally:
                temporary.unlink(missing_ok=True)

    def _path_for(self, descriptor: BlobDescriptor) -> Path:
        path = self._root / descriptor.object_key
        resolved_parent = path.parent.resolve()
        if not resolved_parent.is_relative_to(self._root):
            raise ValueError("Blob path escaped its configured root")
        return path
