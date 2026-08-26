"""Portable export and import of verified brief production state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.production_parsers import (
    ParseResult,
    ReferenceReport,
    TechnicalExtraction,
    reference_report_from_json,
    technical_extraction_from_json,
    validate_synthesis,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

PRODUCTION_STATE_FORMAT = "autowork.production-state"
PRODUCTION_STATE_SCHEMA_VERSION = 1
MAX_PRODUCTION_STATE_BYTES = 16 * 1024 * 1024
IMPORTED_RUN_ERROR_CODE = "imported_production_state"

_ERROR_CODES = {
    "production_state_not_found",
    "production_state_active_run",
    "production_state_incomplete",
    "production_state_unverified",
    "production_state_invalid_format",
    "production_state_version_unsupported",
    "production_state_invalid",
    "production_state_checksum_mismatch",
    "production_state_too_large",
}
_HASH = r"^[0-9a-f]{64}$"


class ProductionStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"Unsupported production state error code: {code}")
        self.code = code
        self.message = message
        super().__init__(message)


class ProductionStateOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_title: str
    editorial_type: Literal["brief"]
    profile: Literal["brief_auto"]
    research_date: date | None


class ProductionStateReferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateSynthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    rendered_content: str = Field(min_length=1)


class ProductionStateArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: ProductionStateReferences
    extraction: ProductionStateExtraction
    synthesis: ProductionStateSynthesis


class ProductionStateSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["autowork.production-state"]
    schema_version: Literal[1]
    exported_at: datetime
    origin: ProductionStateOrigin
    artifacts: ProductionStateArtifacts
    content_sha256: str = Field(pattern=_HASH)

    @field_validator("exported_at")
    @classmethod
    def exported_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("exported_at must be timezone-aware")
        return value


class ProductionStateImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["needs_review"]
    current_stage: Literal["assembly"]
    imported_stages: tuple[Literal["references"], Literal["extraction"], Literal["synthesis"]]
    schema_version: Literal[1]
    content_sha256: str = Field(pattern=_HASH)


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_production_state_checksum(
    snapshot_without_checksum: ProductionStateSnapshotV1 | Mapping[str, Any],
) -> str:
    if isinstance(snapshot_without_checksum, BaseModel):
        payload = snapshot_without_checksum.model_dump(mode="json", exclude={"content_sha256"})
    else:
        payload = dict(snapshot_without_checksum)
        payload.pop("content_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _invalid(message: str) -> ProductionStateError:
    return ProductionStateError(code="production_state_invalid", message=message)


def _json_size(payload: dict[str, Any]) -> int:
    try:
        return len(_canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise _invalid("Production state contains non-JSON content") from exc


def _validate_parsers(
    snapshot: ProductionStateSnapshotV1,
) -> tuple[ReferenceReport, TechnicalExtraction]:
    try:
        report = reference_report_from_json(snapshot.artifacts.references.canonical_content)
        extraction = technical_extraction_from_json(snapshot.artifacts.extraction.canonical_content)
        synthesis_result: ParseResult[str] = validate_synthesis(
            snapshot.artifacts.synthesis.rendered_content, report, extraction
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid("Production state artifact content is invalid") from exc
    if not synthesis_result.usable:
        raise _invalid("Production state synthesis is invalid")
    return report, extraction


def _validate_snapshot(payload: dict[str, Any]) -> ProductionStateSnapshotV1:
    if payload.get("format") != PRODUCTION_STATE_FORMAT:
        raise ProductionStateError(
            code="production_state_invalid_format", message="Unsupported production state format"
        )
    if payload.get("schema_version") != PRODUCTION_STATE_SCHEMA_VERSION:
        raise ProductionStateError(
            code="production_state_version_unsupported",
            message="Unsupported production state schema version",
        )
    try:
        snapshot = ProductionStateSnapshotV1.model_validate(payload)
    except ValidationError as exc:
        raise _invalid("Invalid production state") from exc

    references = snapshot.artifacts.references.canonical_content
    extraction = snapshot.artifacts.extraction.canonical_content
    synthesis = snapshot.artifacts.synthesis.rendered_content
    if (
        _json_size(references) > MAX_ARTIFACT_BYTES
        or _json_size(extraction) > MAX_ARTIFACT_BYTES
        or len(synthesis.encode("utf-8")) > MAX_ARTIFACT_BYTES
        or _json_size(payload) > MAX_PRODUCTION_STATE_BYTES
    ):
        raise ProductionStateError(
            code="production_state_too_large", message="Production state exceeds its size limit"
        )
    if compute_production_state_checksum(snapshot) != snapshot.content_sha256:
        raise ProductionStateError(
            code="production_state_checksum_mismatch", message="Production state checksum mismatch"
        )
    _validate_parsers(snapshot)
    return snapshot


def _snapshot_metadata(snapshot: ProductionStateSnapshotV1, now: datetime) -> dict[str, Any]:
    return {
        "snapshot_import": {
            "format": PRODUCTION_STATE_FORMAT,
            "schema_version": PRODUCTION_STATE_SCHEMA_VERSION,
            "exported_at": snapshot.exported_at.isoformat(),
            "content_sha256": snapshot.content_sha256,
        },
        "generated_at": now.isoformat(),
    }


def _portable_extraction_content(content: dict[str, Any]) -> dict[str, Any]:
    """Keep canonical extraction data, excluding model-run provenance."""
    portable = dict(content)
    items = portable.get("items")
    if isinstance(items, list):
        portable["items"] = [
            {key: value for key, value in item.items() if key != "model_run_ids"}
            if isinstance(item, dict)
            else item
            for item in items
        ]
    return portable


class ProductionStateService:
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def export_state(
        self, *, subject_id: UUID, subject_title: str
    ) -> ProductionStateSnapshotV1:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if run is None:
                raise ProductionStateError(
                    code="production_state_not_found", message="No production run found"
                )
            if run.status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )
            artifacts = {
                artifact.stage: artifact
                for artifact in await uow.production_artifacts.list_for_run(run.id)
            }

        required = (
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.SYNTHESIS,
        )
        if any(stage not in artifacts for stage in required):
            raise ProductionStateError(
                code="production_state_incomplete", message="Production artifacts are incomplete"
            )
        refs = artifacts[ProductionArtifactStage.REFERENCES]
        extraction = artifacts[ProductionArtifactStage.EXTRACTION]
        synthesis = artifacts[ProductionArtifactStage.SYNTHESIS]
        if any(
            artifact.status is not ProductionArtifactStatus.VERIFIED
            for artifact in (refs, extraction, synthesis)
        ):
            raise ProductionStateError(
                code="production_state_unverified", message="Production artifacts are not verified"
            )
        if (
            refs.canonical_blob_id is None
            or extraction.canonical_blob_id is None
            or synthesis.rendered_blob_id is None
        ):
            raise ProductionStateError(
                code="production_state_incomplete", message="Production artifact content is missing"
            )
        try:
            refs_content = await self._artifact_store.read_json(refs.canonical_blob_id)
            extraction_content = _portable_extraction_content(
                await self._artifact_store.read_json(extraction.canonical_blob_id)
            )
            synthesis_content = await self._artifact_store.read_text(synthesis.rendered_blob_id)
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise _invalid("Production artifact content is invalid") from exc

        origin = ProductionStateOrigin(
            subject_title=subject_title,
            editorial_type="brief",
            profile="brief_auto",
            research_date=run.research_date,
        )
        snapshot = ProductionStateSnapshotV1(
            format=PRODUCTION_STATE_FORMAT,
            schema_version=PRODUCTION_STATE_SCHEMA_VERSION,
            exported_at=datetime.now(UTC),
            origin=origin,
            artifacts=ProductionStateArtifacts(
                references=ProductionStateReferences(
                    input_hash=refs.input_hash, canonical_content=refs_content
                ),
                extraction=ProductionStateExtraction(
                    input_hash=extraction.input_hash, canonical_content=extraction_content
                ),
                synthesis=ProductionStateSynthesis(
                    input_hash=synthesis.input_hash, rendered_content=synthesis_content
                ),
            ),
            content_sha256="0" * 64,
        )
        _validate_parsers(snapshot)
        checksum = compute_production_state_checksum(snapshot)
        return snapshot.model_copy(update={"content_sha256": checksum})

    async def import_state(
        self, *, subject_id: UUID, edition_id: UUID, payload: dict[str, Any]
    ) -> ProductionStateImportResult:
        snapshot = _validate_snapshot(payload)
        now = datetime.now(UTC)

        async with self._uow_factory() as uow:
            current = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if current and current.status in (
                SubjectProductionStatus.QUEUED,
                SubjectProductionStatus.RUNNING,
            ):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )

        refs_content = snapshot.artifacts.references.canonical_content
        extraction_content = _portable_extraction_content(
            snapshot.artifacts.extraction.canonical_content
        )
        synthesis_content = snapshot.artifacts.synthesis.rendered_content
        _, refs_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=refs_content
        )
        _, extraction_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=extraction_content
        )
        _, _, synthesis_rendered = await self._artifact_store.store_stage_payloads(
            rendered=synthesis_content
        )
        metadata_base = _snapshot_metadata(snapshot, now)
        refs_meta = {
            **metadata_base,
            "event_count": len(refs_content.get("events", [])),
            "source_count": len(refs_content.get("sources", [])),
            "parser_version": refs_content.get("parser_version"),
            "warnings": [],
        }
        extraction_meta = {
            **metadata_base,
            "element_counts": {
                k: len(v) for k, v in extraction_content.items() if isinstance(v, list)
            },
            "parser_version": extraction_content.get("parser_version"),
            "warnings": [],
        }
        synthesis_meta = {
            **metadata_base,
            "word_count": len(synthesis_content.split()),
            "reference_count": synthesis_content.count("[S"),
            "diagnostics": {},
        }

        async with self._uow_factory() as uow:
            current = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if current and current.status in (
                SubjectProductionStatus.QUEUED,
                SubjectProductionStatus.RUNNING,
            ):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )
            runs = await uow.subject_production_runs.list_for_edition(edition_id)
            run = SubjectProductionRun(
                subject_id=subject_id,
                edition_id=edition_id,
                profile=ProductionProfile.BRIEF_AUTO,
                status=SubjectProductionStatus.NEEDS_REVIEW,
                current_stage=SubjectProductionStage.ASSEMBLY,
                run_number=len([item for item in runs if item.subject_id == subject_id]) + 1,
                research_date=snapshot.origin.research_date,
                error_code=IMPORTED_RUN_ERROR_CODE,
                error_message=(
                    "État importé : références, extraction et synthèse restaurées ; "
                    "assemblage non rejoué."
                ),
                started_at=now,
                finished_at=now,
                created_at=now,
                updated_at=now,
                version=1,
            )
            await uow.subject_production_runs.add(run)
            await uow.production_artifacts.append(
                ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subject_id,
                    stage=ProductionArtifactStage.REFERENCES,
                    version=1,
                    input_hash=snapshot.artifacts.references.input_hash,
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=refs_canonical,
                    metadata=refs_meta,
                )
            )
            await uow.production_artifacts.append(
                ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subject_id,
                    stage=ProductionArtifactStage.EXTRACTION,
                    version=1,
                    input_hash=snapshot.artifacts.extraction.input_hash,
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=extraction_canonical,
                    metadata=extraction_meta,
                )
            )
            await uow.production_artifacts.append(
                ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subject_id,
                    stage=ProductionArtifactStage.SYNTHESIS,
                    version=1,
                    input_hash=snapshot.artifacts.synthesis.input_hash,
                    status=ProductionArtifactStatus.VERIFIED,
                    rendered_blob_id=synthesis_rendered,
                    metadata=synthesis_meta,
                )
            )
            await uow.commit()
        return ProductionStateImportResult(
            run_id=run.id,
            status="needs_review",
            current_stage="assembly",
            imported_stages=("references", "extraction", "synthesis"),
            schema_version=1,
            content_sha256=snapshot.content_sha256,
        )
