"""Portable export and import of verified publication production state.

Blob ingestion necessarily precedes the SQL transaction that references the
four new immutable payloads. If that transaction fails, the content-addressed
catalog may retain unreferenced, safely deduplicated blobs; operators can
reclaim them with the existing ``delete_unreferenced`` operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.production_batch_repointing import _repoint_batch_item
from cti_app.application.production_editorial_enrichment import (
    validate_editorial_enrichment,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    production_reference_corpus_from_json,
)
from cti_app.application.production_relevance import (
    build_relevance_projection,
    persist_relevance_projection_in_uow,
)
from cti_app.application.production_repairs import repair_projection_decision_ids
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.subject_production import (
    _lock_open_edition,
    capture_production_input_snapshot,
)
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentV1,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    production_synthesis_from_json,
)

PRODUCTION_STATE_FORMAT = "autowork.production-state"
PRODUCTION_STATE_SCHEMA_VERSION = 5
PRODUCTION_STATE_SUPPORTED_SCHEMA_VERSIONS = frozenset({5})
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
    "production_input_snapshot_missing",
}
_HASH = r"^[0-9a-f]{64}$"


class ProductionStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"Unsupported production state error code: {code}")
        self.code = code
        self.message = message
        super().__init__(message)


class ProductionStateOriginV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_title: str
    subject_id: UUID
    production_run_id: UUID
    research_date: date
    discovery_snapshot_id: UUID
    discovery_snapshot_version: int = Field(ge=1)


class ProductionStateReferencesV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateExtractionV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateSynthesisV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateEditorialEnrichmentV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_hash: str = Field(pattern=_HASH)
    canonical_content: dict[str, Any]


class ProductionStateArtifactsV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: ProductionStateReferencesV5
    extraction: ProductionStateExtractionV5
    synthesis: ProductionStateSynthesisV5
    editorial_enrichment: ProductionStateEditorialEnrichmentV5


class ProductionStateRepairDecision(BaseModel):
    """One human arbitration that shaped the exported effective extraction."""

    model_config = ConfigDict(extra="forbid")

    repair_key: str = Field(pattern=_HASH)
    decision_id: str | None = None
    issue_kind: str = Field(min_length=1)
    action: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    decided_at: datetime
    reason: str | None = None

    @field_validator("decided_at")
    @classmethod
    def decided_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        return value


class ProductionStateRepair(BaseModel):
    """Audit trail of the repair projection that produced the extraction."""

    model_config = ConfigDict(extra="forbid")

    projection_version: str = Field(min_length=1)
    base_extraction_artifact_id: str
    actor_id: str | None = None
    included_repair_keys: tuple[str, ...] = ()
    excluded_repair_keys: tuple[str, ...] = ()
    unresolved_repair_keys: tuple[str, ...] = ()
    decisions: tuple[ProductionStateRepairDecision, ...] = ()
    materialization: dict[str, Any] | None = None


class ProductionStateSnapshotV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["autowork.production-state"]
    schema_version: Literal[5]
    exported_at: datetime
    origin: ProductionStateOriginV5
    artifacts: ProductionStateArtifactsV5
    # Absent when the run carries no repair projection at all.
    repair: ProductionStateRepair | None = None
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
    status: Literal["needs_review", "running"]
    current_stage: Literal["assembly"]
    imported_stages: tuple[
        Literal["references"],
        Literal["extraction"],
        Literal["synthesis"],
        Literal["editorial_enrichment"],
    ]
    schema_version: Literal[5]
    content_sha256: str = Field(pattern=_HASH)


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_production_state_checksum(
    snapshot_without_checksum: (ProductionStateSnapshotV5 | Mapping[str, Any]),
) -> str:
    if isinstance(snapshot_without_checksum, BaseModel):
        model: ProductionStateSnapshotV5 | None = snapshot_without_checksum
    else:
        # Import always hashes the validated model, so a raw payload has to be
        # normalised the same way: a hand-edited file that dropped an optional
        # field would otherwise get a checksum import then rejects.
        try:
            model = ProductionStateSnapshotV5.model_validate(
                {**snapshot_without_checksum, "content_sha256": "0" * 64}
            )
        except ValidationError:
            model = None
    if model is not None:
        payload = model.model_dump(mode="json", exclude={"content_sha256"})
    else:
        payload = dict(cast(Mapping[str, Any], snapshot_without_checksum))
        payload.pop("content_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _invalid(message: str) -> ProductionStateError:
    return ProductionStateError(code="production_state_invalid", message=message)


def _json_size(payload: dict[str, Any]) -> int:
    try:
        return len(_canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise _invalid("Production state contains non-JSON content") from exc


def _validate_canonical_artifacts_v5(
    snapshot: ProductionStateSnapshotV5,
) -> tuple[
    ProductionReferenceCorpusV1,
    ProductionExtractionV1,
    ProductionSynthesisV1,
    EditorialEnrichmentV1,
]:
    try:
        references = production_reference_corpus_from_json(
            snapshot.artifacts.references.canonical_content
        )
        extraction = production_extraction_from_json(
            snapshot.artifacts.extraction.canonical_content
        )
        synthesis = production_synthesis_from_json(snapshot.artifacts.synthesis.canonical_content)
        enrichment = editorial_enrichment_from_json(
            snapshot.artifacts.editorial_enrichment.canonical_content
        )

        origin_subject_id = snapshot.origin.subject_id
        if any(
            subject_id != origin_subject_id
            for subject_id in (
                references.subject_id,
                extraction.subject_id,
                synthesis.subject_id,
                enrichment.subject_id,
            )
        ):
            raise ValueError("Canonical artifacts do not match the snapshot origin subject")
        if references.subject_id != extraction.subject_id:
            raise ValueError("REFERENCES and EXTRACTION subjects differ")
        if extraction.subject_id != synthesis.subject_id:
            raise ValueError("EXTRACTION and SYNTHESIS subjects differ")

        if references_corpus_hash(references) != extraction.references_corpus_hash:
            raise ValueError("EXTRACTION references corpus hash does not match REFERENCES")

        if canonical_extraction_hash(extraction) != synthesis.extraction_hash:
            raise ValueError("SYNTHESIS extraction hash does not match EXTRACTION")
        if extraction.production_input_hash != synthesis.production_input_hash:
            raise ValueError("EXTRACTION and SYNTHESIS production input hashes differ")
        if (
            len(
                {
                    references.production_input_hash,
                    extraction.production_input_hash,
                    synthesis.production_input_hash,
                    enrichment.production_input_hash,
                }
            )
            != 1
        ):
            raise ValueError("Canonical artifacts have different production input hashes")

        validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid("Production state canonical artifacts or lineage are invalid") from exc
    return references, extraction, synthesis, enrichment


def _validate_snapshot(payload: dict[str, Any]) -> ProductionStateSnapshotV5:
    if payload.get("format") != PRODUCTION_STATE_FORMAT:
        raise ProductionStateError(
            code="production_state_invalid_format", message="Unsupported production state format"
        )
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in PRODUCTION_STATE_SUPPORTED_SCHEMA_VERSIONS
    ):
        raise ProductionStateError(
            code="production_state_version_unsupported",
            message="Unsupported production state schema version",
        )
    try:
        snapshot = ProductionStateSnapshotV5.model_validate(payload)
    except ValidationError as exc:
        raise _invalid("Invalid production state") from exc

    references = snapshot.artifacts.references.canonical_content
    extraction = snapshot.artifacts.extraction.canonical_content
    synthesis = snapshot.artifacts.synthesis.canonical_content
    enrichment = snapshot.artifacts.editorial_enrichment.canonical_content
    if (
        _json_size(references) > MAX_ARTIFACT_BYTES
        or _json_size(extraction) > MAX_ARTIFACT_BYTES
        or _json_size(synthesis) > MAX_ARTIFACT_BYTES
        or _json_size(enrichment) > MAX_ARTIFACT_BYTES
        or _json_size(payload) > MAX_PRODUCTION_STATE_BYTES
    ):
        raise ProductionStateError(
            code="production_state_too_large", message="Production state exceeds its size limit"
        )
    if compute_production_state_checksum(snapshot) != snapshot.content_sha256:
        raise ProductionStateError(
            code="production_state_checksum_mismatch", message="Production state checksum mismatch"
        )
    _validate_canonical_artifacts_v5(snapshot)
    return snapshot


def _validate_import_lineage(
    snapshot: ProductionStateSnapshotV5,
    subject_id: UUID,
    edition_id: UUID,
    input_snapshot: Any,
) -> tuple[
    ProductionReferenceCorpusV1,
    ProductionExtractionV1,
    ProductionSynthesisV1,
    EditorialEnrichmentV1,
]:
    if snapshot.origin.subject_id != subject_id:
        raise _invalid("Production state origin subject does not match the target subject")
    if input_snapshot.subject_id != subject_id or input_snapshot.edition_id != edition_id:
        raise _invalid("Local production snapshot does not match the import target")
    if (
        snapshot.origin.subject_title != input_snapshot.subject_title
        or snapshot.origin.research_date != input_snapshot.research_date
        or snapshot.origin.discovery_snapshot_id != input_snapshot.discovery_snapshot_id
        or snapshot.origin.discovery_snapshot_version != input_snapshot.discovery_snapshot_version
    ):
        raise _invalid("Production state origin does not match the local production snapshot")

    canonical = _validate_canonical_artifacts_v5(snapshot)
    expected_input_hash = input_snapshot.input_hash
    if canonical[0].production_input_hash != expected_input_hash:
        raise _invalid(
            "Production artifact input hash does not match the local production snapshot"
        )
    return canonical


def _snapshot_metadata(snapshot: ProductionStateSnapshotV5, now: datetime) -> dict[str, Any]:
    return {
        "snapshot_import": {
            "format": PRODUCTION_STATE_FORMAT,
            "schema_version": snapshot.schema_version,
            "exported_at": snapshot.exported_at.isoformat(),
            "content_sha256": snapshot.content_sha256,
            "origin": snapshot.origin.model_dump(mode="json"),
        },
        "generated_at": now.isoformat(),
    }


def _snapshot_repair(snapshot: ProductionStateSnapshotV5) -> ProductionStateRepair | None:
    return getattr(snapshot, "repair", None)


def _exported_repair_block(
    metadata: Mapping[str, Any] | None,
    decisions: Mapping[str, Any],
    materialization: Mapping[str, Any] | None = None,
) -> ProductionStateRepair | None:
    """Describe the repair projection that produced the exported extraction."""
    marker = metadata.get("repair_projection") if isinstance(metadata, Mapping) else None
    if not isinstance(marker, Mapping):
        # Imported states deliberately do not forge local projection markers:
        # their original audit is still portable and must survive a second
        # export unchanged.
        imported = metadata.get("imported_repair_audit") if isinstance(metadata, Mapping) else None
        if isinstance(imported, Mapping):
            try:
                return ProductionStateRepair.model_validate(imported)
            except ValidationError:
                return None
        return None
    base_id = marker.get("base_extraction_artifact_id")
    if not isinstance(base_id, str):
        return None

    def _keys(name: str) -> tuple[str, ...]:
        value = marker.get(name)
        return (
            tuple(sorted(item for item in value if isinstance(item, str)))
            if isinstance(value, list)
            else ()
        )

    exported_decisions = tuple(
        ProductionStateRepairDecision(
            repair_key=str(decision.repair_key),
            decision_id=str(decision.id),
            issue_kind=str(getattr(decision.issue_kind, "value", decision.issue_kind)),
            action=str(getattr(decision.action, "value", decision.action)),
            actor_id=str(decision.actor_id),
            decided_at=decision.created_at,
            reason=decision.reason,
        )
        for _decision_id, decision in sorted(decisions.items())
    )
    actor_id = marker.get("actor_id")
    marker_materialization = marker.get("repair_materialization")
    merged_materialization: dict[str, Any] | None = (
        dict(cast(Mapping[str, Any], marker_materialization))
        if isinstance(marker_materialization, Mapping)
        else None
    )
    if materialization is not None:
        merged_materialization = {
            **(merged_materialization or {}),
            **dict(materialization),
        }
    return ProductionStateRepair(
        projection_version=str(marker.get("version") or "1"),
        base_extraction_artifact_id=base_id,
        actor_id=actor_id if isinstance(actor_id, str) else None,
        included_repair_keys=_keys("included_repair_keys"),
        excluded_repair_keys=_keys("excluded_repair_keys"),
        unresolved_repair_keys=_keys("unresolved_repair_keys"),
        decisions=exported_decisions,
        materialization=merged_materialization,
    )


async def _repair_decisions_for_projection(
    uow: Any, run: Any, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Load exactly the decisions a projection marker already names."""
    marker = metadata.get("repair_projection") if isinstance(metadata, Mapping) else None
    if not isinstance(marker, Mapping):
        return {}
    # The marker names what the projection really did with each decision, so
    # the export carries the applied ones and the unbuildable ones alike.
    wanted = repair_projection_decision_ids(marker)
    if not wanted:
        return {}
    repository = getattr(uow, "production_repair_decisions", None)
    if repository is None:
        return {}
    lister = getattr(repository, "list_for_edition", None)
    if not callable(lister):
        return {}
    history = await lister(run.edition_id, run.subject_id)
    return {str(decision.id): decision for decision in history if str(decision.id) in wanted}


class ProductionStateService:
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def export_state(self, *, subject_id: UUID) -> ProductionStateSnapshotV5:
        """Export the latest terminal run for a subject from its input snapshot."""
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None:
                raise ProductionStateError(
                    code="production_state_not_found", message="No production run found"
                )

        return await self.export_run_state(run.id)

    async def export_run_state(self, run_id: UUID) -> ProductionStateSnapshotV5:
        """Export exactly ``run_id`` without resolving another current run."""
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get(run_id)
            if run is None:
                raise ProductionStateError(
                    code="production_state_not_found", message="No production run found"
                )
            if run.status in (ProductionRunStatus.QUEUED, ProductionRunStatus.RUNNING):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )
            input_snapshot = await uow.production_input_snapshots.get_by_run(run.id)
            if input_snapshot is None:
                raise ProductionStateError(
                    code="production_input_snapshot_missing",
                    message="Production input snapshot is missing",
                )
            refs = await uow.production_artifacts.get_current(run.id, "references")
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            synthesis = await uow.production_artifacts.get_current(run.id, "synthesis")
            enrichment = await uow.production_artifacts.get_current(run.id, "editorial_enrichment")
            publication = await uow.production_artifacts.get_current(run.id, "publication")
            # The current extraction already IS the effective projection; the
            # decisions travel with it so an import stays auditable, not only
            # byte-identical.
            repair_decisions = await _repair_decisions_for_projection(
                uow, run, getattr(extraction, "metadata", None)
            )

        if refs is None or extraction is None or synthesis is None or enrichment is None:
            raise ProductionStateError(
                code="production_state_incomplete", message="Production artifacts are incomplete"
            )
        artifacts = (refs, extraction, synthesis, enrichment)
        if any(artifact.status is not ProductionArtifactStatus.VERIFIED for artifact in artifacts):
            raise ProductionStateError(
                code="production_state_unverified", message="Production artifacts are not verified"
            )
        if any(artifact.canonical_blob_id is None for artifact in artifacts):
            raise ProductionStateError(
                code="production_state_incomplete", message="Production artifact content is missing"
            )
        repair_materialization: dict[str, Any] = {}
        for artifact in (*artifacts, publication):
            if artifact is None:
                continue
            candidate = artifact.metadata.get("repair_materialization")
            if isinstance(candidate, Mapping):
                repair_materialization.update(dict(candidate))
        try:
            refs_content = await self._artifact_store.read_json(cast(UUID, refs.canonical_blob_id))
            extraction_content = await self._artifact_store.read_json(
                cast(UUID, extraction.canonical_blob_id)
            )
            synthesis_content = await self._artifact_store.read_json(
                cast(UUID, synthesis.canonical_blob_id)
            )
            enrichment_content = await self._artifact_store.read_json(
                cast(UUID, enrichment.canonical_blob_id)
            )
        except (EntityNotFoundError, KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise _invalid("Production artifact content is invalid") from exc

        origin = ProductionStateOriginV5(
            subject_title=input_snapshot.subject_title,
            subject_id=input_snapshot.subject_id,
            production_run_id=run.id,
            research_date=input_snapshot.research_date,
            discovery_snapshot_id=input_snapshot.discovery_snapshot_id,
            discovery_snapshot_version=input_snapshot.discovery_snapshot_version,
        )
        snapshot = ProductionStateSnapshotV5(
            format=PRODUCTION_STATE_FORMAT,
            schema_version=PRODUCTION_STATE_SCHEMA_VERSION,
            exported_at=datetime.now(UTC),
            origin=origin,
            repair=_exported_repair_block(
                extraction.metadata,
                repair_decisions,
                repair_materialization or None,
            ),
            artifacts=ProductionStateArtifactsV5(
                references=ProductionStateReferencesV5(
                    input_hash=refs.input_hash, canonical_content=refs_content
                ),
                extraction=ProductionStateExtractionV5(
                    input_hash=extraction.input_hash, canonical_content=extraction_content
                ),
                synthesis=ProductionStateSynthesisV5(
                    input_hash=synthesis.input_hash, canonical_content=synthesis_content
                ),
                editorial_enrichment=ProductionStateEditorialEnrichmentV5(
                    input_hash=enrichment.input_hash, canonical_content=enrichment_content
                ),
            ),
            content_sha256="0" * 64,
        )
        parsed = _validate_canonical_artifacts_v5(snapshot)
        if parsed[0].production_input_hash != input_snapshot.input_hash:
            raise _invalid("Production artifact input hash does not match its local snapshot")
        checksum = compute_production_state_checksum(snapshot)
        exported = snapshot.model_copy(update={"content_sha256": checksum})
        return _validate_snapshot(exported.model_dump(mode="json"))

    async def import_state(
        self,
        *,
        subject_id: UUID,
        edition_id: UUID,
        payload: dict[str, Any],
    ) -> ProductionStateImportResult:
        snapshot = _validate_snapshot(payload)
        if snapshot.origin.subject_id != subject_id:
            raise _invalid("Production state origin subject does not match the target subject")
        now = datetime.now(UTC)
        run_id = uuid4()

        async with self._uow_factory() as uow:
            await _lock_open_edition(uow, edition_id)
            current = await uow.production_runs.get_current_for_subject(subject_id)
            if current and current.status in (
                ProductionRunStatus.QUEUED,
                ProductionRunStatus.RUNNING,
            ):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )
            local_input_snapshot = await capture_production_input_snapshot(
                uow,
                production_run_id=run_id,
                subject_id=subject_id,
                edition_id=edition_id,
                research_date=snapshot.origin.research_date,
                captured_at=now,
            )
        parsed_references, parsed_extraction, parsed_synthesis, parsed_enrichment = (
            _validate_import_lineage(snapshot, subject_id, edition_id, local_input_snapshot)
        )

        refs_content = snapshot.artifacts.references.canonical_content
        extraction_content = snapshot.artifacts.extraction.canonical_content
        synthesis_content = snapshot.artifacts.synthesis.canonical_content
        enrichment_content = snapshot.artifacts.editorial_enrichment.canonical_content
        _, refs_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=refs_content
        )
        _, extraction_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=extraction_content
        )
        _, synthesis_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=synthesis_content
        )
        _, enrichment_canonical, _ = await self._artifact_store.store_stage_payloads(
            canonical=enrichment_content
        )
        metadata_base = _snapshot_metadata(snapshot, now)
        # An exported V5 state predates the relevance projection. It is rebuilt
        # deterministically from the imported extraction (no model call) so the
        # operator-attested synthesis and enrichment stay directly assemblable.
        relevance_projection = build_relevance_projection(local_input_snapshot, parsed_extraction)
        refs_meta = {
            **metadata_base,
            "source_count": len(parsed_references.sources),
            "warnings": list(parsed_references.warnings),
        }
        repair_block = _snapshot_repair(snapshot)
        extraction_meta = {
            **metadata_base,
            "element_counts": {
                "sources": len(parsed_extraction.sources),
                "omitted_sources": len(parsed_extraction.omitted_sources),
            },
            "warnings": list(parsed_extraction.warnings),
            # Deliberately NOT "repair_projection": the exported base artifact
            # and decision rows do not exist here, so a projection marker would
            # dangle. This keeps the audit trail without forging local identity.
            **(
                {"imported_repair_audit": repair_block.model_dump(mode="json")}
                if repair_block is not None
                else {}
            ),
            **(
                {"repair_materialization": dict(repair_block.materialization)}
                if repair_block is not None and repair_block.materialization is not None
                else {}
            ),
        }
        synthesis_words = [parsed_synthesis.title]
        synthesis_words.extend(paragraph.text for paragraph in parsed_synthesis.lead)
        synthesis_words.extend(
            paragraph.text
            for section in parsed_synthesis.sections
            for paragraph in section.paragraphs
        )
        synthesis_meta = {
            **metadata_base,
            "section_count": len(parsed_synthesis.sections),
            "word_count": sum(len(part.split()) for part in synthesis_words),
            "warnings_count": len(parsed_synthesis.warnings),
            "diagnostics": {},
            "relevance_projection_hash": relevance_projection.projection_hash,
        }
        enrichment_meta = {
            **metadata_base,
            "schema_version": parsed_enrichment.schema_version,
            "policy_version": parsed_enrichment.enrichment_policy_version,
            "table_count": len(parsed_enrichment.tables),
            "diagram_count": len(parsed_enrichment.diagrams),
            "source_figure_count": len(parsed_enrichment.source_figures),
            "warnings_count": len(parsed_enrichment.warnings),
            "extraction_hash": parsed_enrichment.extraction_hash,
            "synthesis_hash": parsed_enrichment.synthesis_hash,
            "relevance_projection_hash": relevance_projection.projection_hash,
        }
        if repair_block := _snapshot_repair(snapshot):
            audit_metadata = {"imported_repair_audit": repair_block.model_dump(mode="json")}
            refs_meta.update(audit_metadata)
            extraction_meta.update(audit_metadata)
            synthesis_meta.update(audit_metadata)
            enrichment_meta.update(audit_metadata)
            if repair_block.materialization is not None:
                materialization_metadata = {
                    "repair_materialization": dict(repair_block.materialization)
                }
                extraction_meta.update(materialization_metadata)
                synthesis_meta.update(materialization_metadata)
                enrichment_meta.update(materialization_metadata)

        async with self._uow_factory() as uow:
            await _lock_open_edition(uow, edition_id)
            await uow.production_runs.lock_creation_for_subject(subject_id)
            current = await uow.production_runs.get_current_for_subject(subject_id)
            if current and current.status in (
                ProductionRunStatus.QUEUED,
                ProductionRunStatus.RUNNING,
            ):
                raise ProductionStateError(
                    code="production_state_active_run", message="Production run is active"
                )
            confirmed_input_snapshot = await capture_production_input_snapshot(
                uow,
                production_run_id=run_id,
                subject_id=subject_id,
                edition_id=edition_id,
                research_date=snapshot.origin.research_date,
                captured_at=now,
            )
            _validate_import_lineage(snapshot, subject_id, edition_id, confirmed_input_snapshot)
            if confirmed_input_snapshot.input_hash != local_input_snapshot.input_hash:
                raise _invalid("Local production input changed while importing production state")
            # L'item de lot d'édition pointe vers le run remplacé. Sans
            # repointage, la revue de publication continue d'afficher l'ancien
            # run en échec et l'état importé reste invisible.
            replaced_run_id = current.id if current is not None else None
            next_run_number = await uow.production_runs.allocate_next_run_number(subject_id)
            run = ProductionRun(
                subject_id=subject_id,
                edition_id=edition_id,
                id=run_id,
                status=ProductionRunStatus.NEEDS_REVIEW,
                current_stage=ProductionStage.ASSEMBLY,
                run_number=next_run_number,
                research_date=snapshot.origin.research_date,
                error_code=IMPORTED_RUN_ERROR_CODE,
                error_message=(
                    "État importé : références, extraction, synthèse et enrichissement éditorial "
                    "restaurés, périmètre des preuves reconstruit de façon déterministe ; "
                    "l'assemblage doit être rejoué."
                ),
                started_at=now,
                finished_at=now,
                created_at=now,
                updated_at=now,
                version=1,
            )
            await uow.production_runs.add(run)
            # La décision de publication reste attachée au run remplacé : un
            # état corrigé à la main doit être revu, pas hérité.
            await _repoint_batch_item(uow, replaced_run_id, run.id)
            await uow.production_input_snapshots.add(confirmed_input_snapshot)
            refs = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash=snapshot.artifacts.references.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=refs_canonical,
                metadata=refs_meta,
            )
            await uow.production_artifacts.append(refs)
            extraction = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=snapshot.artifacts.extraction.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=extraction_canonical,
                metadata=extraction_meta,
            )
            await uow.production_artifacts.append(extraction)
            synthesis = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=1,
                input_hash=snapshot.artifacts.synthesis.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=synthesis_canonical,
                metadata=synthesis_meta,
            )
            await uow.production_artifacts.append(synthesis)
            enrichment = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                version=1,
                input_hash=snapshot.artifacts.editorial_enrichment.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=enrichment_canonical,
                metadata=enrichment_meta,
            )
            await uow.production_artifacts.append(enrichment)
            await persist_relevance_projection_in_uow(
                uow,
                run,
                confirmed_input_snapshot,
                relevance_projection,
                self._artifact_store,
                mark_downstream_stale=False,
            )
            await uow.commit()
        return ProductionStateImportResult(
            run_id=run.id,
            status="needs_review",
            current_stage="assembly",
            imported_stages=("references", "extraction", "synthesis", "editorial_enrichment"),
            schema_version=snapshot.schema_version,
            content_sha256=snapshot.content_sha256,
        )
