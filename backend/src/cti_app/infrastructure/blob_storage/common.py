import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Protocol

from cti_app.domain.blobs import BlobDescriptor, validate_logical_bucket
from cti_app.domain.errors import BlobIntegrityError

CHUNK_SIZE = 1024 * 1024


class ReadableBinaryStream(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


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


def read_verified_range(
    source: ReadableBinaryStream, descriptor: BlobDescriptor, *, offset: int, length: int
) -> bytes:
    """Read one range while verifying the complete blob with bounded memory."""
    if type(offset) is not int or offset < 0 or type(length) is not int or length < 0:
        raise ValueError("Blob range offset and length must be non-negative integers")
    range_end = offset + length
    if range_end > descriptor.size:
        raise ValueError("Blob range extends beyond its descriptor size")

    digest = hashlib.sha256()
    result = bytearray(length)
    position = 0
    copied = 0
    while chunk := source.read(CHUNK_SIZE):
        digest.update(chunk)
        chunk_end = position + len(chunk)
        overlap_start = max(position, offset)
        overlap_end = min(chunk_end, range_end)
        if overlap_start < overlap_end:
            chunk_start = overlap_start - position
            result_start = overlap_start - offset
            overlap_length = overlap_end - overlap_start
            result[result_start : result_start + overlap_length] = chunk[
                chunk_start : chunk_start + overlap_length
            ]
            copied += overlap_length
        position = chunk_end

    if position != descriptor.size or digest.hexdigest() != descriptor.sha256:
        raise BlobIntegrityError(
            f"Stored content does not match descriptor for {descriptor.sha256}"
        )
    if copied != length:
        raise BlobIntegrityError("Stored content did not contain the requested byte range")
    return bytes(result)


def verify_file(path: Path, descriptor: BlobDescriptor) -> None:
    digest, size = sha256_file(path)
    if digest != descriptor.sha256 or size != descriptor.size:
        raise BlobIntegrityError(
            f"Stored content does not match descriptor for {descriptor.sha256}"
        )
