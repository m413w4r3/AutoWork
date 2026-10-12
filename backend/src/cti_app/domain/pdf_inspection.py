from enum import StrEnum


class PdfInspectionErrorCode(StrEnum):
    ABSENT = "absent"
    HASH_INVALID = "hash_invalid"
    OUTPUT_CORRUPT = "output_corrupt"
    LIMIT_EXCEEDED = "limit_exceeded"
    PREVIEW_CACHE_EXPIRED = "preview_cache_expired"
    PAGE_OUT_OF_BOUNDS = "page_out_of_bounds"


class PdfInspectionAvailability(StrEnum):
    READY = "ready"
    CORRUPT = "corrupt"
    LIMIT_EXCEEDED = "limit_exceeded"


class PdfInspectionProfile(StrEnum):
    PREVIEW = "preview"
    THUMBNAIL = "thumbnail"


class PdfDiagnosticClass(StrEnum):
    DETERMINISTIC = "deterministic"
    HEURISTIC = "heuristic"


class PdfRasterStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PdfMappingStatus(StrEnum):
    NOT_AVAILABLE = "not_available"
