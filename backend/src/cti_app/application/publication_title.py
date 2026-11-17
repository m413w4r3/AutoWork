"""Title consistency checks for synthesis-backed publication previews."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
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
_MAX_TITLE_CORRECTION_CHAIN_LENGTH = 16
_TITLE_PROOF_SEAL = object()


@dataclass(frozen=True, slots=True)
class TitleCorrectionProof:
    """Persisted commitments proving how a synthesis title became the current title."""

    base_artifact_id: UUID
    correction_artifact_id: UUID
    correction_artifact_version: int
    before_text_sha256: str
    after_text_sha256: str
    before_text: str | None = None
    replacement_text: str | None = None
    semantic_spans_before: tuple[tuple[str, str], ...] | None = None
    semantic_spans_after: tuple[tuple[str, str], ...] | None = None
    _resolver_seal: object | None = field(default=None, init=False, repr=False, compare=False)


async def resolve_title_correction_proof(
    uow: UnitOfWork,
    base_artifact: ProductionArtifact,
) -> TitleCorrectionProof | None:
    """Resolve an accepted title correction through later accepted corrections.

    Every edge is checked against immutable PUBLICATION artifacts from the same
    production run. The walk is bounded and fails closed on malformed metadata,
    unaccepted QA, broken parent links, or discontinuous canonical SHA commitments.
    """
    if (
        base_artifact.stage is not ProductionArtifactStage.PUBLICATION
        or base_artifact.status is not ProductionArtifactStatus.VERIFIED
        or base_artifact.canonical_blob_id is None
    ):
        return None

    history = tuple(await uow.production_artifacts.list_for_run(base_artifact.production_run_id))
    by_id: dict[UUID, ProductionArtifact] = {}
    for artifact in history:
        if (
            artifact.stage is ProductionArtifactStage.PUBLICATION
            and artifact.production_run_id == base_artifact.production_run_id
            and artifact.subject_id == base_artifact.subject_id
        ):
            if artifact.id in by_id:
                return None
            by_id[artifact.id] = artifact
    head = by_id.get(base_artifact.id)
    if (
        head is None
        or head.version != base_artifact.version
        or head.input_hash != base_artifact.input_hash
        or head.status is not ProductionArtifactStatus.VERIFIED
        or head.canonical_blob_id is None
    ):
        return None

    current = head
    seen: set[UUID] = set()
    title_correction: tuple[ProductionArtifact, Mapping[str, Any]] | None = None
    for _ in range(_MAX_TITLE_CORRECTION_CHAIN_LENGTH):
        if current.id in seen:
            return None
        seen.add(current.id)
        if _CORRECTION_KEY not in current.metadata:
            return _proof_from_title_correction(head, title_correction)
        correction = current.metadata.get(_CORRECTION_KEY)
        if not isinstance(correction, Mapping):
            return None
        validation = correction.get("validation")
        target_path = correction.get("target_path", correction.get("field_path"))
        if (
            correction.get("schema") != "publication-text-correction.v1"
            or not isinstance(validation, Mapping)
            or not _accepted_correction_validation(validation)
            or not isinstance(target_path, str)
            or validation.get("field_path") != target_path
            or not _is_sha256(correction.get("before_text_sha256"))
            or not _is_sha256(correction.get("after_text_sha256"))
            or not _is_sha256(correction.get("after_sha256"))
            or (not _is_sha256(correction.get("before_sha256")))
            or (not _is_sha256(correction.get("before_v5_sha256")))
        ):
            return None

        parent_id = _parse_artifact_id(correction.get("parent_artifact_id"))
        parent_version = correction.get("parent_version")
        if (
            parent_id is None
            or isinstance(parent_version, bool)
            or not isinstance(parent_version, int)
            or parent_version < 1
            or parent_version >= current.version
        ):
            return None
        parent = by_id.get(parent_id)
        if (
            parent is None
            or parent.version != parent_version
            or parent.version >= current.version
            or parent.stage is not ProductionArtifactStage.PUBLICATION
            or parent.status is not ProductionArtifactStatus.VERIFIED
            or parent.canonical_blob_id is None
            or parent.production_run_id != head.production_run_id
            or parent.subject_id != head.subject_id
            or correction.get("parent_input_hash") != parent.input_hash
        ):
            return None

        parent_has_correction = _CORRECTION_KEY in parent.metadata
        parent_correction_value = parent.metadata.get(_CORRECTION_KEY)
        if parent_has_correction and not isinstance(parent_correction_value, Mapping):
            return None
        parent_correction = (
            parent_correction_value if isinstance(parent_correction_value, Mapping) else None
        )
        method = validation.get("method")
        if method == _CHAINED_CORRECTION_METHOD:
            if (
                parent_correction is None
                or not _accepted_correction_metadata(parent_correction)
                or correction.get("parent_correction_artifact_id") != str(parent.id)
                or not _is_sha256(correction.get("origin_v5_sha256"))
            ):
                return None
        elif correction.get("parent_correction_artifact_id") is not None or parent_has_correction:
            return None

        child_before_sha256 = _correction_body_before_sha256(correction)
        parent_after_sha256 = (
            parent_correction.get("after_sha256") if parent_correction is not None else None
        )
        if child_before_sha256 is None or (
            parent_correction is not None
            and (parent_after_sha256 is None or child_before_sha256 != parent_after_sha256)
        ):
            return None

        if target_path == "title" and title_correction is None:
            title_correction = (current, correction)
        current = parent

    # Reaching the cap is valid only if the next artifact is not another correction.
    if _CORRECTION_KEY in current.metadata:
        return None
    return _proof_from_title_correction(head, title_correction)


def publication_title_matches_synthesis(
    base_artifact: ProductionArtifact | None,
    base_document: PublicationDocumentV5,
    synthesis: ProductionSynthesisV1 | None,
    title_proof: TitleCorrectionProof | None = None,
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

    if title_proof is not None:
        return _title_proof_matches_synthesis(
            base_artifact,
            base_document,
            synthesis,
            title_proof,
        )

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


def _title_proof_matches_synthesis(
    base_artifact: ProductionArtifact,
    base_document: PublicationDocumentV5,
    synthesis: ProductionSynthesisV1,
    proof: TitleCorrectionProof,
) -> bool:
    if (
        proof.base_artifact_id != base_artifact.id
        or not _is_sha256(proof.before_text_sha256)
        or not _is_sha256(proof.after_text_sha256)
        or proof.before_text_sha256 != _text_sha256(synthesis.title)
        or proof.after_text_sha256 != _text_sha256(base_document.title)
    ):
        return False

    current_correction = base_artifact.metadata.get(_CORRECTION_KEY)
    current_target_path = (
        current_correction.get("target_path", current_correction.get("field_path"))
        if isinstance(current_correction, Mapping)
        else None
    )
    if (
        not isinstance(current_correction, Mapping)
        or not _accepted_correction_metadata(current_correction)
        or not isinstance(current_target_path, str)
        or current_correction.get("validation", {}).get("field_path") != current_target_path
        or current_correction.get("after_sha256") != publication_document_v5_sha256(base_document)
        or proof._resolver_seal is not _TITLE_PROOF_SEAL
        or proof.correction_artifact_version < 1
        or proof.correction_artifact_version > base_artifact.version
        or (
            current_target_path == "title"
            and (
                proof.correction_artifact_id != base_artifact.id
                or proof.correction_artifact_version != base_artifact.version
            )
        )
        or (
            current_target_path != "title"
            and (
                proof.correction_artifact_id == base_artifact.id
                or proof.correction_artifact_version >= base_artifact.version
            )
        )
    ):
        return False

    if proof.before_text is not None and proof.before_text != synthesis.title:
        return False
    if proof.replacement_text is not None and proof.replacement_text != base_document.title:
        return False
    return (
        _proof_spans_text(proof.semantic_spans_before) == synthesis.title
        if proof.semantic_spans_before is not None
        else True
    ) and (
        _proof_spans_text(proof.semantic_spans_after) == base_document.title
        if proof.semantic_spans_after is not None
        else True
    )


def _proof_from_title_correction(
    base_artifact: ProductionArtifact,
    value: tuple[ProductionArtifact, Mapping[str, Any]] | None,
) -> TitleCorrectionProof | None:
    if value is None:
        return None
    artifact, correction = value
    try:
        before_spans = _recorded_spans(correction, "semantic_spans_before")
        after_spans = _recorded_spans(correction, "semantic_spans_after")
    except ValueError:
        return None
    before_text = correction.get("before_text")
    replacement_text = correction.get("replacement_text")
    if before_text is not None and not isinstance(before_text, str):
        return None
    if replacement_text is not None and not isinstance(replacement_text, str):
        return None
    proof = TitleCorrectionProof(
        base_artifact_id=base_artifact.id,
        correction_artifact_id=artifact.id,
        correction_artifact_version=artifact.version,
        before_text_sha256=correction["before_text_sha256"],
        after_text_sha256=correction["after_text_sha256"],
        before_text=before_text,
        replacement_text=replacement_text,
        semantic_spans_before=(None if before_spans is None else before_spans),
        semantic_spans_after=None if after_spans is None else after_spans,
    )
    object.__setattr__(proof, "_resolver_seal", _TITLE_PROOF_SEAL)
    return proof


def _accepted_correction_metadata(value: Mapping[str, Any]) -> bool:
    validation = value.get("validation")
    return (
        value.get("schema") == "publication-text-correction.v1"
        and isinstance(validation, Mapping)
        and _accepted_correction_validation(validation)
    )


def _correction_body_before_sha256(value: Mapping[str, Any]) -> str | None:
    before = value.get("before_sha256")
    if before is None:
        return None
    return before if _is_sha256(before) else ""


def _parse_artifact_id(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _recorded_spans(correction: Mapping[str, Any], key: str) -> tuple[tuple[str, str], ...] | None:
    if key not in correction:
        return None
    value = correction[key]
    if not isinstance(value, list):
        raise ValueError("Recorded title correction spans are malformed")
    spans: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("role"), str)
            or not isinstance(item.get("text"), str)
        ):
            raise ValueError("Recorded title correction spans are malformed")
        spans.append((item["role"], item["text"]))
    return tuple(spans)


def _proof_spans_text(spans: tuple[tuple[str, str], ...]) -> str:
    return "".join(text for _, text in spans)


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
