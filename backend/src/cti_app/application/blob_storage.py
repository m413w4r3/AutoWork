from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from cti_app.domain.blobs import BlobDescriptor


class BlobStorageUnavailableError(RuntimeError):
    """The canonical blob store is temporarily unreachable."""

    code = "blob_storage_unavailable"
    retryable = True


class BlobReadLimitExceededError(ValueError):
    """A blob is larger than the caller's explicit read limit."""

    code = "blob_read_limit_exceeded"

    def __init__(self, *, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__("Blob exceeds the read limit")


MaterializationMethod = Literal["hardlink", "copy", "existing"]


class BlobStore(Protocol):
    async def put(
        self, source: BinaryIO, *, logical_bucket: str, mime_type: str
    ) -> BlobDescriptor: ...

    async def exists(self, descriptor: BlobDescriptor) -> bool: ...

    async def materialize(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod: ...

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes: ...

    async def delete(self, descriptor: BlobDescriptor) -> None: ...
