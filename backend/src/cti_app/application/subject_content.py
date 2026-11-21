"""Read-side facade for the content, indicators, and assets of a subject."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.edition_classification import (
    explicit_publication_kind,
    is_legacy_historical_brief,
)
from cti_app.application.editorial_legacy_read_only import (
    publication_kind_is_ambiguous,
    publication_read_only_reason,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.application.production_extraction import legacy_technical_extraction_from_payload
from cti_app.application.production_normalization import (
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.application.production_parsers import DisplayPolicy, IndicatorStatus
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SourceDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_routing import ProductionRoutingDecisionV1
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    PublicationDocumentV6,
    PublicationDocumentV7,
    PublicationDocumentV8,
    parse_publication_document,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind


class ArtifactPayloadReader(Protocol):
    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...


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
    document_version: str
    publication_kind: PublicationKind | None
    read_only_reason: str | None
    canonical_content: dict[str, Any]
    analyst_opinion: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CurrentSubjectPublication:
    run: ProductionRun
    artifact: ProductionArtifact
    routing_decision: ProductionRoutingDecisionV1 | None


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


def resolve_publication_kind(
    routing_decision: ProductionRoutingDecisionV1 | None,
    publication_content: Mapping[str, Any] | CanonicalPublicationDocument | None,
) -> tuple[PublicationKind | None, str | None]:
    """Resolve the explicit route, falling back only for legacy V4-V6 documents."""
    routing_kind = routing_decision.publication_kind if routing_decision else None
    routing_hash = routing_decision.decision_hash if routing_decision else None
    if publication_content is None:
        return routing_kind, routing_hash

    try:
        document = (
            parse_publication_document(publication_content)
            if isinstance(publication_content, Mapping)
            else publication_content
        )
    except (TypeError, ValueError):
        return None, routing_hash

    envelope_kind = explicit_publication_kind(document)
    envelope_hash = getattr(document, "routing_decision_hash", None)
    if not isinstance(envelope_hash, str):
        envelope_hash = None
    if routing_kind is not None and envelope_kind is not None and routing_kind is not envelope_kind:
        return None, routing_hash
    if routing_hash is not None and envelope_hash is not None and routing_hash != envelope_hash:
        return None, routing_hash
    if routing_kind is not None:
        return routing_kind, routing_hash
    if envelope_kind is not None:
        return envelope_kind, envelope_hash
    if document.schema_version in {"4", "5", "6"}:
        legacy_kind = (
            PublicationKind.BRIEF
            if is_legacy_historical_brief(document)
            else PublicationKind.ARTICLE
        )
        return legacy_kind, routing_hash
    return None, routing_hash


def analyst_opinion_view(document: PublicationDocumentV6) -> dict[str, Any] | None:
    """Additive read projection of the approved opinion carried by a V6 wrapper."""
    if document.analyst_opinion is None or document.rendered_opinion is None:
        return None
    provenance = document.provenance
    return {
        "status": document.analyst_opinion.status.value,
        "blocks": [
            {
                "type": block.block_type.value,
                "heading": block.heading,
                "text": block.text,
                "qualification": block.qualification.value,
                "certainty": block.certainty,
                "finding_refs": [str(item) for item in block.finding_refs],
                "evidence_refs": [str(item) for item in block.evidence_refs],
            }
            for block in document.rendered_opinion.blocks
        ],
        "unresolved_points": list(document.rendered_opinion.unresolved_points),
        "provenance": {
            "base_publication_artifact_id": str(provenance.base_publication_artifact_id),
            "base_publication_sha256": provenance.base_publication_sha256,
            "investigation_id": str(provenance.investigation_id),
            "opinion_id": str(provenance.opinion_id),
            "opinion_version": provenance.opinion_version,
            "opinion_content_sha256": provenance.opinion_content_sha256,
            "reviewer_id": provenance.reviewer_id,
            "approver_id": provenance.approver_id,
        },
    }


class SubjectContentService:
    """Resolve only the current read-side data needed by Subject screens."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, artifact_store: ArtifactPayloadReader
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def content(self, subject_id: UUID) -> SubjectContentView | None:
        publication = await self.current_publication(subject_id)
        if publication is None:
            return None

        artifact = publication.artifact
        assert artifact.canonical_blob_id is not None
        canonical = await self._artifact_store.read_json(artifact.canonical_blob_id)
        document = parse_publication_document(canonical)
        publication_kind, _routing_hash = resolve_publication_kind(
            publication.routing_decision, document
        )
        read_only_reason = publication_read_only_reason(
            canonical,
            publication_kind,
            ambiguous_kind=publication_kind_is_ambiguous(publication.routing_decision, canonical),
        )
        document_version = document.schema_version
        analyst_opinion: dict[str, Any] | None = None
        if isinstance(document, PublicationDocumentV8):
            v7 = document.base_publication
            v6 = v7.base_publication
            analyst_opinion = analyst_opinion_view(v6)
            document = v6.base_publication
            canonical = serialize_publication_document(document)
        elif isinstance(document, PublicationDocumentV7):
            v6 = document.base_publication
            analyst_opinion = analyst_opinion_view(v6)
            document = v6.base_publication
            canonical = serialize_publication_document(document)
        elif isinstance(document, PublicationDocumentV6):
            analyst_opinion = analyst_opinion_view(document)
            document = document.base_publication
            canonical = serialize_publication_document(document)
        return SubjectContentView(
            subject_id=subject_id,
            run_id=publication.run.id,
            pipeline_generation=publication.run.pipeline_generation,
            artifact_id=artifact.id,
            artifact_version=artifact.version,
            artifact_input_hash=artifact.input_hash,
            status=artifact.status,
            schema_version=document.schema_version,
            document_version=document_version,
            publication_kind=publication_kind,
            read_only_reason=(read_only_reason.value if read_only_reason is not None else None),
            canonical_content=canonical,
            analyst_opinion=analyst_opinion,
        )

    async def current_publication(self, subject_id: UUID) -> CurrentSubjectPublication | None:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None:
                return None
            artifact = await current_publication_artifact(uow.production_artifacts, run.id)
            if artifact is None or artifact.canonical_blob_id is None:
                return None
            routing_decision = await get_adopted_production_routing_decision(uow=uow, run=run)
            return CurrentSubjectPublication(
                run=run,
                artifact=artifact,
                routing_decision=routing_decision,
            )

    async def indicators(self, subject_id: UUID) -> list[SubjectIndicatorView]:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None:
                return []
            artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            if artifact is None or artifact.canonical_blob_id is None:
                return []
            extraction = legacy_technical_extraction_from_payload(
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
                display = display_indicator_value(item.value, item.artifact_type, defanged=True)
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
            size=(source.decoded_size if source.decoded_size is not None else source.encoded_size),
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
