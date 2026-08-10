from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from cti_app.domain.blobs import BlobDescriptor

MaterializationMethod = Literal["hardlink", "copy", "existing"]


class BlobStore(Protocol):
    async def put(
        self, source: BinaryIO, *, logical_bucket: str, mime_type: str
    ) -> BlobDescriptor: ...

    async def exists(self, descriptor: BlobDescriptor) -> bool: ...

    async def materialize(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> MaterializationMethod: ...

    async def delete(self, descriptor: BlobDescriptor) -> None: ...
