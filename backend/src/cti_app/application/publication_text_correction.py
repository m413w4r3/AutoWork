"""Append-only, exact-field edits to canonical V5 publication documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_editorial_enrichment import validate_editorial_enrichment
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.publication_qa import qa_publication_v5
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from cti_app.domain.production_extraction import production_extraction_from_json
from cti_app.domain.production_relevance import relevance_projection_from_json
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_article_v5_body,
    publication_document_from_json,
    publication_document_text_anchors,
    publication_document_v5_from_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import SemanticRole, SemanticTextSpanV1

_PATH_PATTERN = re.compile(
    r"^(?P<root>[A-Za-z_][A-Za-z0-9_]*)(?P<tail>(?:\[[0-9]+\]|\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_PATH_PART_PATTERN = re.compile(r"\[([0-9]+)\]|\.([A-Za-z_][A-Za-z0-9_]*)")
_CORRECTION_METADATA_KEY = "manual_publication_text_correction"
MAX_PUBLICATION_TEXT_CORRECTION_TEXT_LENGTH = 4_194_304
MAX_PUBLICATION_TEXT_CORRECTION_SPANS = 256


class PublicationTextCorrectionErrorCode(StrEnum):
    ARTIFACT_NOT_FOUND = "publication_text_correction_artifact_not_found"
    INVALID_ARTIFACT = "publication_text_correction_artifact_invalid"
    STALE_ARTIFACT = "publication_text_correction_stale_artifact"
    CANONICAL_SHA_MISMATCH = "publication_text_correction_sha_mismatch"
    EXPECTED_TEXT_MISMATCH = "publication_text_correction_expected_text_mismatch"
    FIELD_NOT_TEXT = "publication_text_correction_field_not_text"
    DOCUMENT_INVALID = "publication_text_correction_document_invalid"
    SEMANTIC_SPAN_CONFLICT = "publication_text_correction_semantic_span_conflict"
    REPLACEMENT_SPANS_INVALID = "publication_text_correction_replacement_spans_invalid"
    PUBLICATION_QA_UNAVAILABLE = "publication_text_correction_publication_qa_unavailable"
    PUBLICATION_QA_FAILED = "publication_text_correction_publication_qa_failed"
    NO_CHANGE = "publication_text_correction_no_change"
    IDEMPOTENCY_CONFLICT = "publication_text_correction_idempotency_conflict"


class PublicationTextCorrectionError(ValueError):
    def __init__(self, code: PublicationTextCorrectionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class PublicationTextCorrectionCommand:
    subject_id: UUID
    artifact_id: UUID
    artifact_version: int
    artifact_input_hash: str
    artifact_sha256: str
    field_path: str
    expected_text: str
    replacement_text: str
    actor_id: str
    reason: str
    idempotency_key: str
    replacement_spans: tuple[SemanticTextSpanV1, ...] | None = None


@dataclass(frozen=True, slots=True)
class PublicationTextCorrectionResult:
    artifact: ProductionArtifact
    canonical_sha256: str
    replayed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact": {
                "id": str(self.artifact.id),
                "subject_id": str(self.artifact.subject_id),
                "production_run_id": str(self.artifact.production_run_id),
                "stage": self.artifact.stage.value,
                "version": self.artifact.version,
                "input_hash": self.artifact.input_hash,
                "canonical_sha256": self.canonical_sha256,
                "canonical_blob_id": (
                    str(self.artifact.canonical_blob_id)
                    if self.artifact.canonical_blob_id is not None
                    else None
                ),
                "metadata": self.artifact.metadata,
            },
            "replayed": self.replayed,
        }


class PublicationTextCorrectionService:
    """Validate one exact V5 field edit and append its canonical artifact."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        qa_runner: Callable[
            [Any, ProductionArtifact, PublicationDocumentV5], Awaitable[dict[str, Any]]
        ]
        | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._qa_runner = qa_runner

    async def correct(
        self, command: PublicationTextCorrectionCommand
    ) -> PublicationTextCorrectionResult:
        self._validate_command(command)
        idempotency_key_sha256 = _sha256(command.idempotency_key.encode("utf-8"))
        request_sha256 = _request_sha256(command)

        async with self._uow_factory() as uow:
            requested = await uow.production_artifacts.get(command.artifact_id)
            if requested is None:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.ARTIFACT_NOT_FOUND
                )
            if (
                requested.subject_id != command.subject_id
                or requested.stage is not ProductionArtifactStage.PUBLICATION
            ):
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.INVALID_ARTIFACT
                )

            # Serialize against creation of a newer run for this subject.
            await uow.production_runs.lock_creation_for_subject(command.subject_id)
            # The run row serializes correction/revision writes for this stage.
            current = await uow.production_artifacts.get_current_for_revision(
                requested.production_run_id,
                ProductionArtifactStage.PUBLICATION.value,
            )
            history = tuple(
                await uow.production_artifacts.list_for_run(requested.production_run_id)
            )
            previous_request = next(
                (
                    item
                    for item in history
                    if item.stage is ProductionArtifactStage.PUBLICATION
                    and _correction_metadata(item).get("idempotency_key_sha256")
                    == idempotency_key_sha256
                ),
                None,
            )
            if previous_request is not None:
                metadata = _correction_metadata(previous_request)
                if metadata.get("request_sha256") != request_sha256:
                    raise PublicationTextCorrectionError(
                        PublicationTextCorrectionErrorCode.IDEMPOTENCY_CONFLICT
                    )
                after_sha256 = metadata.get("after_sha256")
                if not isinstance(after_sha256, str):
                    raise PublicationTextCorrectionError(
                        PublicationTextCorrectionErrorCode.INVALID_ARTIFACT
                    )
                return PublicationTextCorrectionResult(
                    previous_request, after_sha256, replayed=True
                )

            latest_version = max(
                (
                    item.version
                    for item in history
                    if item.stage is ProductionArtifactStage.PUBLICATION
                ),
                default=0,
            )
            current_run = await uow.production_runs.get_current_for_subject(command.subject_id)
            is_current_base = current is not None and current.id == requested.id
            is_direct_augmented_base = (
                current is not None
                and current.id != requested.id
                and current.version > requested.version
                and isinstance(current.metadata, Mapping)
                and current.metadata.get("previous_publication_artifact_id") == str(requested.id)
            )
            if (
                current is None
                or latest_version != current.version
                or current_run is None
                or current_run.id != requested.production_run_id
                or requested.version != command.artifact_version
                or requested.input_hash != command.artifact_input_hash
                or requested.status is not ProductionArtifactStatus.VERIFIED
                or requested.canonical_blob_id is None
                or current.status is not ProductionArtifactStatus.VERIFIED
                or current.canonical_blob_id is None
                or not (is_current_base or is_direct_augmented_base)
                or (is_current_base and latest_version != requested.version)
            ):
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.STALE_ARTIFACT
                )

            try:
                before_bytes = await self._artifact_store.read_bytes(requested.canonical_blob_id)
            except ProductionReuseStorageUnavailableError:
                raise
            before_sha256 = _sha256(before_bytes)
            if before_sha256 != command.artifact_sha256:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.CANONICAL_SHA_MISMATCH
                )
            head_sha256 = before_sha256
            if not is_current_base:
                assert current is not None and current.canonical_blob_id is not None
                try:
                    head_bytes = await self._artifact_store.read_bytes(current.canonical_blob_id)
                except ProductionReuseStorageUnavailableError:
                    raise
                head_sha256 = _sha256(head_bytes)
                if _embedded_v5_bytes(head_bytes) != _embedded_v5_bytes(before_bytes):
                    raise PublicationTextCorrectionError(
                        PublicationTextCorrectionErrorCode.STALE_ARTIFACT
                    )
            original_v5_payload, document = _load_canonical_v5(
                before_bytes, subject_id=command.subject_id
            )
            before_v5_sha256 = _sha256(
                ProductionArtifactStore.canonical_json_bytes(original_v5_payload)
            )
            tokens = _parse_field_path(command.field_path)
            target_parent, target_key = _resolve_parent(original_v5_payload, tokens)
            if isinstance(target_parent, dict) and isinstance(target_key, str):
                before_text = target_parent.get(target_key)
            elif isinstance(target_parent, list) and isinstance(target_key, int):
                before_text = target_parent[target_key]
            else:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
                )
            if not isinstance(before_text, str):
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
                )
            if before_text != command.expected_text:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.EXPECTED_TEXT_MISMATCH
                )
            if before_text == command.replacement_text:
                raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.NO_CHANGE)

            anchor = _semantic_anchor_for_path(document, tokens)
            if anchor is None:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
                )
            rich_paragraphs = original_v5_payload.get("rich_text", {}).get("paragraphs")
            matching = [
                item
                for item in rich_paragraphs or ()
                if isinstance(item, dict) and item.get("anchor") == anchor
            ]
            if len(matching) != 1:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                )

            correction_spans = matching[0].get("spans")
            if not isinstance(correction_spans, list):
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                )
            before_spans: list[dict[str, str]] | None = None
            after_spans: list[dict[str, str]] | None = None
            if command.replacement_spans is not None:
                if tokens != ("title",):
                    raise PublicationTextCorrectionError(
                        PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
                    )
                before_spans = _existing_semantic_span_records(
                    correction_spans, expected_text=before_text
                )
                after_spans = _replacement_semantic_span_records(
                    command.replacement_spans,
                    replacement_text=command.replacement_text,
                )
            if isinstance(target_parent, dict) and isinstance(target_key, str):
                target_parent[target_key] = command.replacement_text
            elif isinstance(target_parent, list) and isinstance(target_key, int):
                target_parent[target_key] = command.replacement_text
            else:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
                )
            if after_spans is None:
                _replace_within_one_text_span(
                    correction_spans,
                    before_text=before_text,
                    replacement_text=command.replacement_text,
                )
            else:
                # V5 stores title text in both the root field and the rich-text
                # title anchor. Update both representations in one operation.
                matching[0]["spans"] = after_spans
            before_qa = await self._run_canonical_qa(uow, requested, document)
            try:
                corrected_document = publication_document_v5_from_json(original_v5_payload)
                corrected_payload = serialize_publication_document(corrected_document)
                if corrected_payload != original_v5_payload:
                    raise ValueError("Corrected V5 payload is not canonical")
            except (TypeError, ValueError, KeyError) as exc:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                ) from exc
            after_qa = await self._run_canonical_qa(uow, requested, corrected_document)
            qa_validation = _validate_qa_transition(
                before_qa, after_qa, field_path=command.field_path
            )
            after_bytes = ProductionArtifactStore.canonical_json_bytes(corrected_payload)
            if len(after_bytes) > MAX_ARTIFACT_BYTES:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                )
            after_sha256 = _sha256(after_bytes)
            correction_input = {
                "schema": "publication-text-correction.v1",
                "parent_artifact_id": str(requested.id),
                "parent_version": requested.version,
                "parent_input_hash": requested.input_hash,
                "before_sha256": before_sha256,
                "before_v5_sha256": before_v5_sha256,
                "superseded_head_artifact_id": str(current.id) if current else None,
                "superseded_head_version": current.version if current else None,
                "superseded_head_sha256": head_sha256,
                "target_path": command.field_path,
                "before_text_sha256": _sha256(before_text.encode("utf-8")),
                "after_text_sha256": _sha256(command.replacement_text.encode("utf-8")),
            }
            if before_spans is not None and after_spans is not None:
                correction_input["semantic_spans_before"] = before_spans
                correction_input["semantic_spans_after"] = after_spans
            input_hash = _sha256(ProductionArtifactStore.canonical_json_bytes(correction_input))
            _, canonical_blob_id, _ = await self._artifact_store.store_stage_payloads(
                canonical=corrected_payload
            )
            if canonical_blob_id is None:
                raise RuntimeError("Corrected PUBLICATION body was not persisted")
            correction_metadata = {
                **correction_input,
                "target_anchor": anchor,
                "after_sha256": after_sha256,
                "actor_id": command.actor_id,
                "reason": command.reason,
                "idempotency_key_sha256": idempotency_key_sha256,
                "request_sha256": request_sha256,
                "validation": qa_validation,
            }
            artifact = ProductionArtifact(
                production_run_id=requested.production_run_id,
                subject_id=requested.subject_id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=latest_version + 1,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=canonical_blob_id,
                metadata={
                    **requested.metadata,
                    "previous_publication_artifact_id": str(requested.id),
                    _CORRECTION_METADATA_KEY: correction_metadata,
                },
            )
            try:
                await uow.production_artifacts.append(artifact)
                await uow.commit()
            except Exception as exc:
                if type(exc).__name__ in {"IntegrityError", "VersionConflictError"}:
                    raise PublicationTextCorrectionError(
                        PublicationTextCorrectionErrorCode.STALE_ARTIFACT
                    ) from exc
                raise
            return PublicationTextCorrectionResult(artifact, after_sha256)

    async def _run_canonical_qa(
        self,
        uow: Any,
        source_artifact: ProductionArtifact,
        publication: PublicationDocumentV5,
    ) -> dict[str, Any]:
        if self._qa_runner is not None:
            return await self._qa_runner(uow, source_artifact, publication)

        try:
            snapshot = await uow.production_input_snapshots.get_by_run(
                source_artifact.production_run_id
            )
            input_artifacts = source_artifact.metadata.get("input_artifacts")
            if snapshot is None or not isinstance(input_artifacts, Mapping):
                raise ValueError("Canonical publication QA inputs are unavailable")

            loaded: dict[str, ProductionArtifact] = {}
            for name, stage in (
                ("references_artifact_id", "references"),
                ("extraction_artifact_id", "extraction"),
                ("relevance_projection_artifact_id", "relevance_projection"),
                ("synthesis_artifact_id", "synthesis"),
                ("editorial_enrichment_artifact_id", "editorial_enrichment"),
            ):
                raw_id = input_artifacts.get(name)
                artifact_id = UUID(str(raw_id)) if raw_id is not None else None
                artifact = await uow.production_artifacts.get(artifact_id) if artifact_id else None
                if (
                    artifact is None
                    or artifact.stage.value != stage
                    or artifact.production_run_id != source_artifact.production_run_id
                    or artifact.subject_id != source_artifact.subject_id
                    or artifact.status is not ProductionArtifactStatus.VERIFIED
                    or artifact.canonical_blob_id is None
                ):
                    raise ValueError("Canonical publication QA input artifact is invalid")
                loaded[name] = artifact

            if (
                snapshot.production_run_id != source_artifact.production_run_id
                or snapshot.subject_id != source_artifact.subject_id
            ):
                raise ValueError("Canonical publication QA snapshot lineage is invalid")
            references = production_reference_corpus_from_json(
                await self._artifact_store.read_json(
                    loaded["references_artifact_id"].canonical_blob_id  # type: ignore[arg-type]
                )
            )
            extraction = production_extraction_from_json(
                await self._artifact_store.read_json(
                    loaded["extraction_artifact_id"].canonical_blob_id  # type: ignore[arg-type]
                )
            )
            relevance_projection = relevance_projection_from_json(
                await self._artifact_store.read_json(
                    loaded["relevance_projection_artifact_id"].canonical_blob_id  # type: ignore[arg-type]
                )
            )
            synthesis = production_synthesis_from_json(
                await self._artifact_store.read_json(
                    loaded["synthesis_artifact_id"].canonical_blob_id  # type: ignore[arg-type]
                )
            )
            editorial_enrichment = editorial_enrichment_from_json(
                await self._artifact_store.read_json(
                    loaded["editorial_enrichment_artifact_id"].canonical_blob_id  # type: ignore[arg-type]
                )
            )
            validate_editorial_enrichment(
                editorial_enrichment,
                extraction=extraction,
                synthesis=synthesis,
            )
            return qa_publication_v5(
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                relevance_projection=relevance_projection,
                synthesis=synthesis,
                editorial_enrichment=editorial_enrichment,
                publication=publication,
            )
        except ProductionReuseStorageUnavailableError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.PUBLICATION_QA_UNAVAILABLE
            ) from exc

    @staticmethod
    def _validate_command(command: PublicationTextCorrectionCommand) -> None:
        for value, name in (
            (command.artifact_input_hash, "artifact_input_hash"),
            (command.artifact_sha256, "artifact_sha256"),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if command.artifact_version < 1:
            raise ValueError("artifact_version must be >= 1")
        if not command.actor_id.strip() or not command.reason.strip():
            raise ValueError("Correction actor and reason must not be empty")
        if not command.idempotency_key.strip() or len(command.idempotency_key) > 128:
            raise ValueError("Correction idempotency key must contain 1 to 128 characters")
        if not isinstance(command.expected_text, str) or not isinstance(
            command.replacement_text, str
        ):
            raise TypeError("Publication correction values must be text")
        if command.replacement_spans is not None:
            try:
                is_title_path = _parse_field_path(command.field_path) == ("title",)
            except PublicationTextCorrectionError:
                is_title_path = False
            if not is_title_path:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
                )
            _replacement_semantic_span_records(
                command.replacement_spans,
                replacement_text=command.replacement_text,
            )


def _existing_semantic_span_records(
    spans: list[Any], *, expected_text: str
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str):
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            )
        raw_role = span.get("role")
        if not isinstance(raw_role, str):
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            )
        try:
            role = SemanticRole(raw_role)
        except ValueError as exc:
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            ) from exc
        records.append({"role": role.value, "text": span["text"]})
    if "".join(item["text"] for item in records) != expected_text:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.DOCUMENT_INVALID)
    return records


def _replacement_semantic_span_records(
    spans: tuple[SemanticTextSpanV1, ...], *, replacement_text: str
) -> list[dict[str, str]]:
    if (
        not isinstance(spans, tuple)
        or not spans
        or len(spans) > MAX_PUBLICATION_TEXT_CORRECTION_SPANS
        or len(replacement_text) > MAX_PUBLICATION_TEXT_CORRECTION_TEXT_LENGTH
    ):
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
        )
    records: list[dict[str, str]] = []
    for span in spans:
        if (
            not isinstance(span, SemanticTextSpanV1)
            or not isinstance(span.role, SemanticRole)
            or not isinstance(span.text, str)
            or not span.text
        ):
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
            )
        records.append({"role": span.role.value, "text": span.text})
    if "".join(item["text"] for item in records) != replacement_text:
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
        )
    return records


def _replace_within_one_text_span(
    spans: list[Any], *, before_text: str, replacement_text: str
) -> None:
    span_texts: list[str] = []
    roles: list[SemanticRole] = []
    positions: list[tuple[int, int]] = []
    cursor = 0
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str):
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            )
        raw_role = span.get("role")
        if not isinstance(raw_role, str):
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            )
        try:
            role = SemanticRole(raw_role)
        except (TypeError, ValueError) as exc:
            raise PublicationTextCorrectionError(
                PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
            ) from exc
        value = span["text"]
        span_texts.append(value)
        roles.append(role)
        positions.append((cursor, cursor + len(value)))
        cursor += len(value)

    if "".join(span_texts) != before_text:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.DOCUMENT_INVALID)

    prefix_length = 0
    shared_limit = min(len(before_text), len(replacement_text))
    while (
        prefix_length < shared_limit
        and before_text[prefix_length] == replacement_text[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    while (
        suffix_length < len(before_text) - prefix_length
        and suffix_length < len(replacement_text) - prefix_length
        and before_text[len(before_text) - suffix_length - 1]
        == replacement_text[len(replacement_text) - suffix_length - 1]
    ):
        suffix_length += 1

    old_end = len(before_text) - suffix_length
    new_end = len(replacement_text) - suffix_length
    if prefix_length == old_end:
        candidates = [
            index for index, (start, end) in enumerate(positions) if start <= prefix_length <= end
        ]
    else:
        candidates = [
            index
            for index, (start, end) in enumerate(positions)
            if max(start, prefix_length) < min(end, old_end)
        ]

    if len(candidates) != 1:
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.SEMANTIC_SPAN_CONFLICT
        )
    span_index = candidates[0]
    span_start, span_end = positions[span_index]
    if (
        roles[span_index] is not SemanticRole.TEXT
        or span_start > prefix_length
        or span_end < old_end
    ):
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.SEMANTIC_SPAN_CONFLICT
        )

    local_start = prefix_length - span_start
    local_end = old_end - span_start
    replacement_middle = replacement_text[prefix_length:new_end]
    spans[span_index]["text"] = (
        span_texts[span_index][:local_start]
        + replacement_middle
        + span_texts[span_index][local_end:]
    )
    if [SemanticRole(span["role"]) for span in spans] != roles or "".join(
        span["text"] for span in spans
    ) != replacement_text:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.DOCUMENT_INVALID)


def _validate_qa_transition(
    before: Mapping[str, Any], after: Mapping[str, Any], *, field_path: str
) -> dict[str, Any]:
    before_checks = before.get("checks")
    after_checks = after.get("checks")
    before_errors = before.get("errors")
    after_errors = after.get("errors")
    before_warnings = before.get("warnings")
    after_warnings = after.get("warnings")
    if (
        before.get("passed") is not True
        or not isinstance(before_checks, Mapping)
        or not before_checks
        or any(value is not True for value in before_checks.values())
        or before_errors != []
        or not isinstance(after_checks, Mapping)
        or set(before_checks) != set(after_checks)
        or before_checks.get("exact_projection") is not True
        or after_checks.get("exact_projection") is not False
        or any(
            value is not True for name, value in after_checks.items() if name != "exact_projection"
        )
        or after.get("passed") is not False
        or after_errors != ["Publication differs from canonical Assembly projection"]
        or before_warnings != after_warnings
    ):
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.PUBLICATION_QA_FAILED
        )
    return {
        "method": "canonical_publication_qa_v5_with_declared_text_edit",
        "passed": True,
        "field_path": field_path,
        "checks_before": dict(before_checks),
        "checks_after": dict(after_checks),
        "accepted_exception": "exact_projection",
        "warnings": list(after_warnings or ()),
    }


def _embedded_v5_payload(payload: Any) -> dict[str, Any] | None:
    while isinstance(payload, dict):
        schema_version = payload.get("schema_version")
        if schema_version == "5":
            return payload
        if schema_version not in {"6", "7", "8"}:
            return None
        base = payload.get("base_publication")
        if not isinstance(base, dict):
            return None
        payload = base
    return None


def _embedded_v5_bytes(content: bytes) -> bytes | None:
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return None
        envelope = publication_document_from_json(payload)
        v5_payload = _embedded_v5_payload(payload)
        if v5_payload is None or serialize_publication_document(envelope) != payload:
            return None
        if payload.get("schema_version") in {"7", "8"} and (
            publication_document_article_v5_body(envelope) is None
        ):
            return None
        v5_document = publication_document_v5_from_json(v5_payload)
        if (
            serialize_publication_document(v5_document) != v5_payload
            or ProductionArtifactStore.canonical_json_bytes(
                serialize_publication_document(envelope)
            )
            != content
        ):
            return None
        return ProductionArtifactStore.canonical_json_bytes(v5_payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None


def _load_canonical_v5(
    content: bytes, *, subject_id: UUID
) -> tuple[dict[str, Any], PublicationDocumentV5]:
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Publication document must be an object")
        envelope = publication_document_from_json(payload)
        v5_payload = _embedded_v5_payload(payload)
        if v5_payload is None:
            raise ValueError("Publication correction requires a V5 body")
        if payload.get("schema_version") in {"7", "8"} and (
            publication_document_article_v5_body(envelope) is None
        ):
            raise ValueError("Publication correction requires an article V5 body")
        document = publication_document_v5_from_json(v5_payload)
        if document.subject_id != subject_id:
            raise ValueError("Publication document subject identity mismatch")
        canonical_payload = serialize_publication_document(envelope)
        if (
            canonical_payload != payload
            or ProductionArtifactStore.canonical_json_bytes(canonical_payload) != content
        ):
            raise ValueError("Publication document bytes are not canonical")
        if serialize_publication_document(document) != v5_payload:
            raise ValueError("Publication envelope V5 body is not canonical")
        anchors = publication_document_text_anchors(
            document.document, references=document.references
        )
        if {item.anchor: item.text for item in document.semantic_text.paragraphs} != anchors:
            raise ValueError("Publication semantic text is invalid")
        return v5_payload, document
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise PublicationTextCorrectionError(
            PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
        ) from exc


def _parse_field_path(path: str) -> tuple[str | int, ...]:
    if not isinstance(path, str) or len(path) > 512:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
    match = _PATH_PATTERN.fullmatch(path)
    if match is None:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
    tokens: list[str | int] = [match.group("root")]
    tail = match.group("tail")
    consumed = 0
    for part in _PATH_PART_PATTERN.finditer(tail):
        if part.start() != consumed:
            raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
        tokens.append(int(part.group(1)) if part.group(1) is not None else part.group(2))
        consumed = part.end()
    if consumed != len(tail):
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
    return tuple(tokens)


def _resolve_parent(
    payload: dict[str, Any], tokens: tuple[str | int, ...]
) -> tuple[dict[str, Any] | list[Any], str | int]:
    if not tokens:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
    current: Any = payload
    for token in tokens[:-1]:
        if isinstance(token, str) and isinstance(current, dict):
            current = current.get(token)
        elif isinstance(token, int) and isinstance(current, list) and token < len(current):
            current = current[token]
        else:
            raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
        if current is None:
            raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)
    target = tokens[-1]
    if isinstance(target, str) and isinstance(current, dict):
        return current, target
    if isinstance(target, int) and isinstance(current, list) and target < len(current):
        return current, target
    else:
        raise PublicationTextCorrectionError(PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT)


def _semantic_anchor_for_path(
    document: PublicationDocumentV5, tokens: tuple[str | int, ...]
) -> str | None:
    if tokens == ("title",):
        return "title"
    if len(tokens) == 3 and tokens[0] == "lead" and tokens[2] == "text":
        index = tokens[1]
        return (
            f"lead:{index + 1:04d}"
            if isinstance(index, int) and index < len(document.lead)
            else None
        )
    if (
        len(tokens) == 5
        and tokens[0] == "sections"
        and tokens[2] == "paragraphs"
        and tokens[4] == "text"
    ):
        section_index, paragraph_index = tokens[1], tokens[3]
        if (
            isinstance(section_index, int)
            and isinstance(paragraph_index, int)
            and section_index < len(document.sections)
            and paragraph_index < len(document.sections[section_index].paragraphs)
        ):
            return f"section:{section_index}:paragraph:{paragraph_index + 1:04d}"
        return None
    if len(tokens) == 3 and tokens[0] == "timeline" and tokens[2] == "text":
        index = tokens[1]
        return (
            f"timeline:{index + 1:04d}"
            if isinstance(index, int) and index < len(document.timeline)
            else None
        )
    if len(tokens) == 3 and tokens[0] == "references" and tokens[2] == "text":
        index = tokens[1]
        return (
            f"reference:{index + 1:04d}"
            if isinstance(index, int) and index < len(document.references)
            else None
        )

    doc = document.document
    if len(tokens) == 3 and tokens[0] in {"tables", "diagrams", "charts", "figures"}:
        collection, index, field = tokens
        values = getattr(doc, str(collection))
        if not isinstance(index, int) or index >= len(values) or not isinstance(field, str):
            return None
        item = values[index]
        if collection == "tables":
            if field in {"title", "caption"} and (field != "caption" or item.caption is not None):
                return f"table:{item.key}:{field}"
        elif collection in {"diagrams", "charts"}:
            if field in {"title", "caption"} and (field != "caption" or item.caption is not None):
                return f"{collection[:-1]}:{item.key}:{field}"
        elif collection == "figures" and field in {"caption", "provenance"}:
            return f"figure:{item.key}:{field}"
    if (
        len(tokens) == 5
        and tokens[0] == "tables"
        and tokens[2] == "columns"
        and tokens[4] == "label"
    ):
        table_index, column_index = tokens[1], tokens[3]
        if (
            isinstance(table_index, int)
            and isinstance(column_index, int)
            and table_index < len(doc.tables)
            and column_index < len(doc.tables[table_index].columns)
        ):
            return f"table:{doc.tables[table_index].key}:column:{column_index + 1:04d}"
    if len(tokens) == 6 and tokens[0] == "tables" and tokens[2] == "rows" and tokens[4] == "cells":
        table_index, row_index, cell_index = tokens[1], tokens[3], tokens[5]
        if (
            isinstance(table_index, int)
            and isinstance(row_index, int)
            and isinstance(cell_index, int)
            and table_index < len(doc.tables)
            and row_index < len(doc.tables[table_index].rows)
            and cell_index < len(doc.tables[table_index].rows[row_index].cells)
        ):
            return (
                f"table:{doc.tables[table_index].key}:row:{row_index + 1:04d}:"
                f"cell:{cell_index + 1:04d}"
            )
    return None


def _correction_metadata(artifact: ProductionArtifact) -> Mapping[str, Any]:
    value = artifact.metadata.get(_CORRECTION_METADATA_KEY)
    return value if isinstance(value, Mapping) else {}


def _request_sha256(command: PublicationTextCorrectionCommand) -> str:
    payload: dict[str, Any] = {
        "subject_id": str(command.subject_id),
        "artifact_id": str(command.artifact_id),
        "artifact_version": command.artifact_version,
        "artifact_input_hash": command.artifact_input_hash,
        "artifact_sha256": command.artifact_sha256,
        "field_path": command.field_path,
        "expected_text": command.expected_text,
        "replacement_text": command.replacement_text,
        "actor_id": command.actor_id,
        "reason": command.reason,
    }
    if command.replacement_spans is not None:
        payload["replacement_spans"] = [
            {"role": span.role.value, "text": span.text} for span in command.replacement_spans
        ]
    return _sha256(ProductionArtifactStore.canonical_json_bytes(payload))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
