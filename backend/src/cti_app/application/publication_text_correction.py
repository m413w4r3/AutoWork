"""Append-only, exact-field edits to canonical V5 publication documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_text_anchors,
    publication_document_v5_from_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import SemanticRole

_PATH_PATTERN = re.compile(
    r"^(?P<root>[A-Za-z_][A-Za-z0-9_]*)(?P<tail>(?:\[[0-9]+\]|\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_PATH_PART_PATTERN = re.compile(r"\[([0-9]+)\]|\.([A-Za-z_][A-Za-z0-9_]*)")
_CORRECTION_METADATA_KEY = "manual_publication_text_correction"


class PublicationTextCorrectionErrorCode(StrEnum):
    ARTIFACT_NOT_FOUND = "publication_text_correction_artifact_not_found"
    INVALID_ARTIFACT = "publication_text_correction_artifact_invalid"
    STALE_ARTIFACT = "publication_text_correction_stale_artifact"
    CANONICAL_SHA_MISMATCH = "publication_text_correction_sha_mismatch"
    EXPECTED_TEXT_MISMATCH = "publication_text_correction_expected_text_mismatch"
    FIELD_NOT_TEXT = "publication_text_correction_field_not_text"
    DOCUMENT_INVALID = "publication_text_correction_document_invalid"
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
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

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
            if (
                current is None
                or current.id != requested.id
                or current_run is None
                or current_run.id != requested.production_run_id
                or latest_version != requested.version
                or requested.version != command.artifact_version
                or requested.input_hash != command.artifact_input_hash
                or requested.status is not ProductionArtifactStatus.VERIFIED
                or requested.canonical_blob_id is None
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
            original_payload, document = _load_canonical_v5(
                before_bytes, subject_id=command.subject_id
            )
            tokens = _parse_field_path(command.field_path)
            target_parent, target_key = _resolve_parent(original_payload, tokens)
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
            rich_paragraphs = original_payload.get("rich_text", {}).get("paragraphs")
            matching = [
                item
                for item in rich_paragraphs or ()
                if isinstance(item, dict) and item.get("anchor") == anchor
            ]
            if len(matching) != 1:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                )

            if isinstance(target_parent, dict) and isinstance(target_key, str):
                target_parent[target_key] = command.replacement_text
            elif isinstance(target_parent, list) and isinstance(target_key, int):
                target_parent[target_key] = command.replacement_text
            else:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
                )
            # Rich text is a projection of the paragraph's exact text. Keep its
            # stable anchor and replace only that paragraph's semantic spans.
            matching[0]["spans"] = [
                {"role": SemanticRole.TEXT.value, "text": command.replacement_text}
            ]
            try:
                corrected_document = publication_document_v5_from_json(original_payload)
                corrected_payload = serialize_publication_document(corrected_document)
            except (TypeError, ValueError, KeyError) as exc:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
                ) from exc
            if corrected_payload != original_payload:
                raise PublicationTextCorrectionError(
                    PublicationTextCorrectionErrorCode.DOCUMENT_INVALID
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
                "target_path": command.field_path,
                "before_text_sha256": _sha256(before_text.encode("utf-8")),
                "after_text_sha256": _sha256(command.replacement_text.encode("utf-8")),
            }
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
                "validation": "publication_document_v5_round_trip",
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


def _load_canonical_v5(
    content: bytes, *, subject_id: UUID
) -> tuple[dict[str, Any], PublicationDocumentV5]:
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict) or payload.get("schema_version") != "5":
            raise ValueError("Publication correction requires a V5 document")
        document = publication_document_v5_from_json(payload)
        if document.subject_id != subject_id:
            raise ValueError("Publication document subject identity mismatch")
        canonical = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(document)
        )
        if canonical != content:
            raise ValueError("Publication document bytes are not canonical")
        anchors = publication_document_text_anchors(
            document.document, references=document.references
        )
        if {item.anchor: item.text for item in document.semantic_text.paragraphs} != anchors:
            raise ValueError("Publication semantic text is invalid")
        return payload, document
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
    payload = {
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
    return _sha256(ProductionArtifactStore.canonical_json_bytes(payload))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
