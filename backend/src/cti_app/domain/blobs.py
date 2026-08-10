import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cti_app.domain.errors import BlobIntegrityError

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOGICAL_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_logical_bucket(value: str) -> None:
    if not LOGICAL_BUCKET_PATTERN.fullmatch(value):
        raise BlobIntegrityError("Logical bucket contains unsupported characters")


@dataclass(frozen=True, slots=True)
class BlobDescriptor:
    sha256: str
    size: int
    mime_type: str
    logical_bucket: str

    def __post_init__(self) -> None:
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise BlobIntegrityError("SHA-256 must contain exactly 64 lowercase hexadecimal chars")
        if self.size < 0:
            raise BlobIntegrityError("Blob size cannot be negative")
        if not self.mime_type.strip():
            raise BlobIntegrityError("Blob MIME type cannot be empty")
        validate_logical_bucket(self.logical_bucket)

    @property
    def object_key(self) -> str:
        return f"{self.logical_bucket}/{self.sha256[:2]}/{self.sha256}"


@dataclass(frozen=True, slots=True, kw_only=True)
class BlobRecord:
    descriptor: BlobDescriptor
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
