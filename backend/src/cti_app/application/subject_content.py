"""Read-side facade for the content, indicators, and assets of a subject."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_normalization import (
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.application.production_parsers import (
    DisplayPolicy,
    IndicatorStatus,
    technical_extraction_from_json,
)
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SourceDocument
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication import BriefDocumentV1 as CurrentPublicationDocument


class ArtifactPayloadReader(Protocol):
    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...

    async def read_text(self, blob_id: UUID) -> str: ...


@dataclass(frozen=True, slots=True)
class SubjectContentView:
    subject_id: UUID
    run_id: UUID
    pipeline_generation: int
    artifact_id: UUID
    artifact_version: int
    artifact_input_hash: str
    status: ProductionArtifactStatus
    schema_version: str
    canonical_content: dict[str, Any]
    rendered_content: str | None


@dataclass(frozen=True, slots=True)
class SubjectIndicatorView:
    id: str
    artifact_type: ArtifactType
    display_value: str
    normalized_value: str
    indicator_status: IndicatorStatus
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubjectAssetView:
    id: UUID
    original_name: str
    mime_type: str | None
    sha256: str | None
    size: int | None
    origin: str
    provenance: dict[str, str] | None
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool


@dataclass(frozen=True, slots=True)
class SubjectAssetsView:
    sources: tuple[SubjectAssetView, ...]
    samples: tuple[SubjectAssetView, ...]


class SubjectContentService:
    """Resolve only the current read-side data needed by Subject screens."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, artifact_store: ArtifactPayloadReader
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def content(self, subject_id: UUID) -> SubjectContentView | None:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if run is None:
                return None
            artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.BRIEF.value
            )
            if artifact is None or artifact.canonical_blob_id is None:
                return None

            canonical = await self._artifact_store.read_json(artifact.canonical_blob_id)
            document = CurrentPublicationDocument.from_json(canonical)
            rendered = (
                await self._artifact_store.read_text(artifact.rendered_blob_id)
                if artifact.rendered_blob_id is not None
                else None
            )
            return SubjectContentView(
                subject_id=subject_id,
                run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                artifact_id=artifact.id,
                artifact_version=artifact.version,
                artifact_input_hash=artifact.input_hash,
                status=artifact.status,
                schema_version=document.schema_version,
                canonical_content=canonical,
                rendered_content=rendered,
            )

    async def indicators(self, subject_id: UUID) -> list[SubjectIndicatorView]:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if run is None:
                return []
            artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            if artifact is None or artifact.canonical_blob_id is None:
                return []
            extraction = technical_extraction_from_json(
                await self._artifact_store.read_json(artifact.canonical_blob_id)
            )

        indicators: list[SubjectIndicatorView] = []
        for item in extraction.items:
            if (
                item.artifact_type is None
                or item.indicator_status is not IndicatorStatus.CONFIRMED_IOC
                or item.display_policy not in {DisplayPolicy.IOC_SECTION, DisplayPolicy.BOTH}
            ):
                continue
            try:
                normalized = item.normalized_value or normalize_indicator_value(
                    item.value, item.artifact_type
                )
                display = display_indicator_value(
                    item.value, item.artifact_type, defanged=True
                )
            except ValueError:
                continue
            indicators.append(
                SubjectIndicatorView(
                    id=item.local_id,
                    artifact_type=item.artifact_type,
                    display_value=display,
                    normalized_value=normalized,
                    indicator_status=item.indicator_status,
                    source_ids=item.source_ids,
                )
            )
        return indicators

    async def assets(self, subject_id: UUID) -> SubjectAssetsView | None:
        async with self._uow_factory() as uow:
            if await uow.subjects.get(subject_id) is None:
                return None
            sources = await uow.source_documents.list_for_subject(subject_id)
            samples = await uow.samples.list_for_subject(subject_id)

        return SubjectAssetsView(
            sources=tuple(self._source_asset(source) for source in sources),
            samples=tuple(self._sample_asset(sample) for sample in samples),
        )

    @staticmethod
    def _source_asset(source: SourceDocument) -> SubjectAssetView:
        provenance = {
            key: str(value)
            for key, value in {
                "source_collection_id": source.source_collection_id,
                "source_candidate_id": source.source_candidate_id,
            }.items()
            if value is not None
        }
        return SubjectAssetView(
            id=source.id,
            original_name=source.original_name,
            mime_type=source.detected_mime_type or source.declared_mime_type,
            sha256=source.decoded_sha256 or source.encoded_sha256,
            size=(
                source.decoded_size
                if source.decoded_size is not None
                else source.encoded_size
            ),
            origin=source.origin,
            provenance=provenance or None,
            tlp=source.tlp,
            do_not_submit=source.do_not_submit,
            external_llm_allowed=source.external_llm_allowed,
        )

    @staticmethod
    def _sample_asset(sample: Sample) -> SubjectAssetView:
        provenance = {
            key: str(value)
            for key, value in {
                "origin_kind": sample.origin_kind.value,
                "source_service": sample.source_service,
                "source_object_id": sample.source_object_id,
            }.items()
            if value is not None
        }
        return SubjectAssetView(
            id=sample.id,
            original_name=sample.original_name,
            mime_type=None,
            sha256=(
                sample.expected_hash
                if sample.expected_hash is not None and len(sample.expected_hash) == 64
                else None
            ),
            size=None,
            origin=sample.origin,
            provenance=provenance or None,
            tlp=sample.tlp,
            do_not_submit=sample.do_not_submit,
            external_llm_allowed=sample.external_llm_allowed,
        )
