import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from cti_app.domain.blobs import BlobDescriptor, validate_logical_bucket
from cti_app.domain.errors import BlobIntegrityError

CHUNK_SIZE = 1024 * 1024


def spool_and_describe(
    source: BinaryIO, *, temp_directory: Path, logical_bucket: str, mime_type: str
) -> tuple[Path, BlobDescriptor]:
    validate_logical_bucket(logical_bucket)
    temp_directory.mkdir(parents=True, exist_ok=True)
    descriptor_fd, descriptor_path = tempfile.mkstemp(prefix="blob-", dir=temp_directory)
    path = Path(descriptor_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor_fd, "wb") as destination:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
                destination.write(chunk)
        descriptor = BlobDescriptor(
            sha256=digest.hexdigest(),
            size=size,
            mime_type=mime_type,
            logical_bucket=logical_bucket,
        )
        return path, descriptor
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_file(path: Path, descriptor: BlobDescriptor) -> None:
    digest, size = sha256_file(path)
    if digest != descriptor.sha256 or size != descriptor.size:
        raise BlobIntegrityError(
            f"Stored content does not match descriptor for {descriptor.sha256}"
        )
