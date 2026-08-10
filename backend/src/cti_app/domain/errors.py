class DomainError(ValueError):
    """Base class for rejected domain operations."""


class TlpDowngradeError(DomainError):
    """Raised when data would become less restricted."""


class BlobIntegrityError(DomainError):
    """Raised when content-addressed metadata does not match stored bytes."""


class BlobStillReferencedError(DomainError):
    """Raised when a referenced blob is selected for physical deletion."""


class EntityNotFoundError(DomainError):
    """Raised when a requested canonical entity does not exist."""
