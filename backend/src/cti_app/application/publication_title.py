"""Title consistency checks for synthesis-backed publication previews."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_v5_sha256,
)

_CORRECTION_KEY = "manual_publication_text_correction"
_ACCEPTED_CORRECTION_METHODS = {
    "canonical_publication_qa_v5_with_declared_text_edit",
    "canonical_publication_qa_v5_with_declared_text_edit_chained",
}
_CHAINED_CORRECTION_METHOD = "canonical_publication_qa_v5_with_declared_text_edit_chained"


def publication_title_matches_synthesis(
    base_artifact: ProductionArtifact | None,
    base_document: PublicationDocumentV5,
    synthesis: ProductionSynthesisV1 | None,
) -> bool:
    """Allow a title mismatch only when accepted correction metadata proves the exact edit.

    The publication correction contract stores SHA-256 commitments for the title text,
    with semantic spans containing the raw texts when replacement spans were supplied.
    Compare both commitments to the current synthesis and canonical V5 body; malformed,
    unrelated, or unaccepted provenance keeps the historical mismatch rejection.
    """
    if synthesis is None or synthesis.title == base_document.title:
        return True
    if (
        base_artifact is None
        or base_artifact.stage is not ProductionArtifactStage.PUBLICATION
        or base_artifact.status is not ProductionArtifactStatus.VERIFIED
        or base_artifact.canonical_blob_id is None
        or base_artifact.subject_id != base_document.subject_id
        or synthesis.subject_id != base_document.subject_id
    ):
        return False

    correction = base_artifact.metadata.get(_CORRECTION_KEY)
    if not isinstance(correction, Mapping) or correction.get("schema") != (
        "publication-text-correction.v1"
    ):
        return False
    validation = correction.get("validation")
    if not isinstance(validation, Mapping) or not _accepted_correction_validation(validation):
        return False
    if correction.get("target_path", correction.get("field_path")) != "title":
        return False
    if validation.get("field_path") != "title":
        return False

    parent_id = correction.get("parent_artifact_id")
    parent_version = correction.get("parent_version")
    try:
        parsed_parent_id = UUID(parent_id) if isinstance(parent_id, str) else None
    except ValueError:
        return False
    if (
        parsed_parent_id is None
        or str(parsed_parent_id) != parent_id
        or isinstance(parent_version, bool)
        or not isinstance(parent_version, int)
        or parent_version < 1
        or parent_version >= base_artifact.version
    ):
        return False

    method = validation.get("method")
    if method == _CHAINED_CORRECTION_METHOD:
        if correction.get("parent_correction_artifact_id") != parent_id or not _is_sha256(
            correction.get("origin_v5_sha256")
        ):
            return False
    elif correction.get("parent_correction_artifact_id") is not None:
        return False

    before_hash = _text_sha256(synthesis.title)
    after_hash = _text_sha256(base_document.title)
    if (
        correction.get("before_text_sha256") != before_hash
        or correction.get("after_text_sha256") != after_hash
        or correction.get("after_sha256") != publication_document_v5_sha256(base_document)
    ):
        return False

    for text_key, span_key, expected in (
        ("before_text", "semantic_spans_before", synthesis.title),
        ("replacement_text", "semantic_spans_after", base_document.title),
    ):
        recorded_text = correction.get(text_key)
        if recorded_text is not None and recorded_text != expected:
            return False
        recorded_spans = correction.get(span_key)
        if recorded_spans is not None and _span_text(recorded_spans) != expected:
            return False
    return True


def _accepted_correction_validation(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    method = value.get("method")
    checks_before = value.get("checks_before")
    checks_after = value.get("checks_after")
    warnings = value.get("warnings")
    if (
        not isinstance(method, str)
        or method not in _ACCEPTED_CORRECTION_METHODS
        or value.get("passed") is not True
        or value.get("accepted_exception") != "exact_projection"
        or not isinstance(checks_before, Mapping)
        or not checks_before
        or not isinstance(checks_after, Mapping)
        or set(checks_before) != set(checks_after)
        or not isinstance(warnings, list)
    ):
        return False
    after_is_projection_drift = _is_projection_drift_profile(checks_after)
    if method == _CHAINED_CORRECTION_METHOD:
        before_is_valid = (
            _is_projection_drift_profile(checks_before) and checks_before == checks_after
        )
    else:
        before_is_valid = checks_before.get("exact_projection") is True and all(
            item is True for item in checks_before.values()
        )
    return before_is_valid and after_is_projection_drift


def _is_projection_drift_profile(checks: Mapping[str, Any]) -> bool:
    return (
        bool(checks)
        and checks.get("exact_projection") is False
        and all(value is True for name, value in checks.items() if name != "exact_projection")
    )


def _span_text(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
            return None
        parts.append(item["text"])
    return "".join(parts)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
