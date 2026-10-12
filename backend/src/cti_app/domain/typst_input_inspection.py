from enum import StrEnum


class RenderInputInspectionErrorCode(StrEnum):
    RENDER_NOT_FOUND = "render_not_found"
    FILE_NOT_IN_BUNDLE = "file_not_in_bundle"
    INPUT_SNAPSHOT_UNAVAILABLE = "input_snapshot_unavailable"
    INPUT_HASH_MISMATCH = "input_hash_mismatch"
    INVALID_SELECTOR = "invalid_selector"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"


class RenderInputInspectionAvailability(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class RenderInputDiagnosticCategory(StrEnum):
    CANONICAL_DATA = "canonical_data"
    TYPST_SOURCE = "typst_source"
    COMPILER = "compiler"
    LAYOUT_DEPENDENT = "layout_dependent"
