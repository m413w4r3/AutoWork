from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ConfigDict, Field

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.extraction import (
    PARSER_NAME,
    PARSER_VERSION,
    EvidenceExtractionService,
    extract_indicators,
    parse_document,
)
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    DownloadTooLargeError,
    SafeHttpCollector,
)
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.collection import (
    AttemptOutcome,
    Claim,
    CollectionAttempt,
    CollectionState,
    DerivedArtifact,
    DetectedMimeType,
    Indicator,
    SourceCollection,
)
from cti_app.domain.discovery import SourceCandidate, SourceRole
from cti_app.domain.editorial import EditorialGroupStatus, HumanDecision, HumanDecisionType
from cti_app.domain.entities import ProvenanceEvent, SourceDocument


class CollectionNotAllowedError(ValueError):
    pass


class CollectionItemNotFoundError(LookupError):
    pass


class SubjectCollectionParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    subject_id: UUID
    requested_by: str = Field(min_length=1, max_length=255)


class SubjectCollectionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        collector: SafeHttpCollector,
        blob_store: BlobStore,
        extraction: EvidenceExtractionService,
    ) -> None:
        self._uow_factory = uow_factory
        self._collector = collector
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self._extraction = extraction

    async def initialize(self, subject_id: UUID) -> list[SourceCollection]:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_by_subject(subject_id)
            if (
                group is None
                or group.status is not EditorialGroupStatus.SELECTED
                or group.subject_id != subject_id
            ):
                raise CollectionNotAllowedError(
                    "Only sources attached to a selected subject can be collected"
                )
            batches = {
                batch.id: batch
                for batch in await uow.discovery_batches.list_for_edition(group.edition_id)
            }
            seen_urls: set[str] = set()
            for reference in group.candidate_references:
                batch = batches.get(reference.batch_id)
                candidate = (
                    next(
                        (item for item in batch.candidates if item.id == reference.candidate_id),
                        None,
                    )
                    if batch
                    else None
                )
                if candidate is None:
                    continue
                for source in candidate.sources:
                    if source.canonical_url in seen_urls:
                        continue
                    seen_urls.add(source.canonical_url)
                    await uow.source_collections.add_if_absent(
                        _new_collection(
                            group.id,
                            group.edition_id,
                            subject_id,
                            reference.batch_id,
                            source,
                        )
                    )
            await uow.commit()
        return await self.list_sources(subject_id)

    async def list_sources(self, subject_id: UUID) -> list[SourceCollection]:
        async with self._uow_factory() as uow:
            return list(await uow.source_collections.list_for_subject(subject_id))

    async def subject_exists(self, subject_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            return await uow.subjects.get(subject_id) is not None

    @property
    def configuration_id(self) -> str:
        return self._collector.policy.snapshot_id()

    async def list_evidence(self, subject_id: UUID) -> tuple[list[Claim], list[Indicator]]:
        async with self._uow_factory() as uow:
            return (
                list(await uow.claims.list_for_subject(subject_id)),
                list(await uow.indicators.list_for_subject(subject_id)),
            )

    async def attempts(self, collection_id: UUID) -> list[CollectionAttempt]:
        async with self._uow_factory() as uow:
            if await uow.source_collections.get(collection_id) is None:
                raise CollectionItemNotFoundError(str(collection_id))
            return list(await uow.collection_attempts.list_for_collection(collection_id))

    async def decisions(self, edition_id: UUID) -> list[HumanDecision]:
        async with self._uow_factory() as uow:
            return list(await uow.human_decisions.list_for_edition(edition_id))

    async def get_claim(self, claim_id: UUID) -> Claim:
        async with self._uow_factory() as uow:
            claim = await uow.claims.get(claim_id)
            if claim is None:
                raise CollectionItemNotFoundError(str(claim_id))
            return claim

    async def extracted_text(self, artifact_id: UUID) -> str:
        async with self._uow_factory() as uow:
            artifact = await uow.derived_artifacts.get(artifact_id)
            if artifact is None:
                raise CollectionItemNotFoundError(str(artifact_id))
            blob = await uow.blobs.get(artifact.text_blob_id)
            if blob is None:
                raise CollectionItemNotFoundError(str(artifact.text_blob_id))
        content = await self._blob_store.read(blob.descriptor, max_bytes=50 * 1024 * 1024)
        return content.decode("utf-8")

    async def collect_subject(
        self, subject_id: UUID, job_id: UUID, context: JobExecutionContext
    ) -> str:
        sources = await self.initialize(subject_id)
        retryable = False
        for index, source in enumerate(sources, start=1):
            await context.report_progress(index - 1, len(sources), f"Collecte de la source {index}")
            if source.state in {
                CollectionState.COMPLETED,
                CollectionState.BLOCKED,
                CollectionState.FAILED_TERMINAL,
            }:
                continue
            state = await self.collect_one(source.id, job_id)
            retryable = retryable or state is CollectionState.FAILED_RETRYABLE
            await context.heartbeat()
        await context.report_progress(len(sources), len(sources), "Collecte terminée")
        if retryable:
            raise JobHandlerError(
                "source_collection_transient",
                "Certaines sources sont temporairement indisponibles.",
                transient=True,
            )
        return f"subject-collection://{subject_id}"

    async def collect_one(self, collection_id: UUID, job_id: UUID) -> CollectionState:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            if collection.state is CollectionState.COMPLETED:
                return collection.state
            if collection.state in {CollectionState.ARCHIVED, CollectionState.EXTRACTED}:
                pass
            else:
                collection.start()
                await uow.source_collections.save(collection)
                await uow.commit()

        if collection.state is CollectionState.FETCHING:
            started_at = datetime.now(UTC)
            try:
                response = await self._collector.fetch(collection.requested_url)
            except CollectionError as exc:
                return await self._record_failure(collection.id, job_id, started_at, exc)
            await self._archive(collection.id, job_id, started_at, response)

        await self._extract(collection.id)
        async with self._uow_factory() as uow:
            completed = await uow.source_collections.get(collection.id)
            if completed is None:
                raise CollectionItemNotFoundError(str(collection.id))
            return completed.state

    async def decide_claim(
        self,
        claim_id: UUID,
        decision_type: HumanDecisionType,
        *,
        actor_id: str,
        correlation_id: str,
        corrected_value: str | None = None,
    ) -> HumanDecision:
        allowed = {
            HumanDecisionType.CLAIM_VALIDATE,
            HumanDecisionType.CLAIM_CORRECT,
            HumanDecisionType.CLAIM_REJECT,
        }
        if decision_type not in allowed:
            raise ValueError("Invalid claim decision")
        if decision_type is HumanDecisionType.CLAIM_CORRECT and not (
            corrected_value and corrected_value.strip()
        ):
            raise ValueError("A claim correction requires a corrected value")
        async with self._uow_factory() as uow:
            claim = await uow.claims.get(claim_id)
            if claim is None:
                raise CollectionItemNotFoundError(str(claim_id))
            decision = HumanDecision(
                edition_id=claim.edition_id,
                decision_type=decision_type,
                group_ids=(claim.group_id,),
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "claim_id": str(claim.id),
                    "original_value": claim.value,
                    "corrected_value": corrected_value,
                },
            )
            await uow.human_decisions.append(decision)
            await uow.commit()
            return decision

    async def decide_indicator(
        self,
        indicator_id: UUID,
        decision_type: HumanDecisionType,
        *,
        actor_id: str,
        correlation_id: str,
        corrected_value: str | None = None,
    ) -> HumanDecision:
        allowed = {
            HumanDecisionType.INDICATOR_VALIDATE,
            HumanDecisionType.INDICATOR_CORRECT,
            HumanDecisionType.INDICATOR_REJECT,
        }
        if decision_type not in allowed:
            raise ValueError("Invalid indicator decision")
        if decision_type is HumanDecisionType.INDICATOR_CORRECT and not (
            corrected_value and corrected_value.strip()
        ):
            raise ValueError("An indicator correction requires a corrected value")
        async with self._uow_factory() as uow:
            indicator = await uow.indicators.get(indicator_id)
            if indicator is None:
                raise CollectionItemNotFoundError(str(indicator_id))
            decision = HumanDecision(
                edition_id=indicator.edition_id,
                decision_type=decision_type,
                group_ids=(indicator.group_id,),
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "indicator_id": str(indicator.id),
                    "original_value": indicator.normalized_value,
                    "corrected_value": corrected_value,
                },
            )
            await uow.human_decisions.append(decision)
            await uow.commit()
            return decision

    async def decide_relationship(
        self,
        collection_id: UUID,
        role: SourceRole,
        *,
        actor_id: str,
        correlation_id: str,
    ) -> SourceCollection:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            previous_role = collection.proposed_role
            collection.correct_relationship(role, actor_id=actor_id)
            await uow.source_collections.save(collection)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=collection.edition_id,
                    decision_type=(
                        HumanDecisionType.SOURCE_RELATIONSHIP_VALIDATE
                        if previous_role is role
                        else HumanDecisionType.SOURCE_RELATIONSHIP_CORRECT
                    ),
                    group_ids=(collection.group_id,),
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={
                        "source_collection_id": str(collection.id),
                        "previous_role": previous_role.value,
                        "role": role.value,
                    },
                )
            )
            await uow.commit()
            return collection

    async def _archive(
        self,
        collection_id: UUID,
        job_id: UUID,
        started_at: datetime,
        response: CollectedResponse,
    ) -> None:
        blob = await self._catalog.ingest(
            BytesIO(response.body),
            logical_bucket="source-raw",
            mime_type=response.detected_content_type.value,
        )
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            batch = await uow.discovery_batches.get(collection.batch_id)
            source = batch.source(collection.source_candidate_id) if batch else None
            if subject is None or source is None:
                raise CollectionNotAllowedError("Collection source lost its canonical context")
            document = SourceDocument(
                subject_id=collection.subject_id,
                blob_id=blob.id,
                original_name=_original_name(response.final_url),
                origin=response.final_url,
                acquired_at=response.acquired_at,
                license_restriction=None,
                tlp=source.tlp,
                do_not_submit=False,
                external_llm_allowed=source.external_llm_allowed,
            )
            attempt = _successful_attempt(
                collection,
                job_id,
                started_at,
                response,
                self._collector.policy.snapshot_id(),
            )
            await uow.source_documents.add(document)
            await uow.collection_attempts.append(attempt)
            collection.archive(attempt_id=attempt.id, source_document_id=document.id)
            await uow.source_collections.save(collection)
            await uow.provenance.append(
                ProvenanceEvent(
                    subject_id=collection.subject_id,
                    aggregate_type="source_collection",
                    aggregate_id=collection.id,
                    event_type="source.archived",
                    payload={
                        "attempt_id": str(attempt.id),
                        "source_document_id": str(document.id),
                        "sha256": response.sha256,
                        "job_id": str(job_id),
                    },
                    tlp=subject.tlp,
                    actor_id="system:collector",
                )
            )
            await uow.commit()

    async def _extract(self, collection_id: UUID) -> None:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            if collection.state is CollectionState.COMPLETED:
                return
            if collection.state is CollectionState.EXTRACTED:
                collection.complete()
                await uow.source_collections.save(collection)
                await uow.commit()
                return
            if (
                collection.state is not CollectionState.ARCHIVED
                or not collection.source_document_id
            ):
                return
            document = await uow.source_documents.get(collection.source_document_id)
            blob = await uow.blobs.get(document.blob_id) if document else None
            if document is None or blob is None:
                raise CollectionItemNotFoundError("Archived source content is missing")
        raw = await self._blob_store.read(
            blob.descriptor, max_bytes=self._collector.policy.max_expanded_bytes
        )
        parsed = parse_document(raw, _mime_from_value(blob.descriptor.mime_type))
        text_blob = await self._catalog.ingest(
            BytesIO(parsed.text.encode()),
            logical_bucket="source-text",
            mime_type="text/plain; charset=utf-8",
        )
        artifact = DerivedArtifact(
            source_document_id=document.id,
            text_blob_id=text_blob.id,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            text_length=len(parsed.text),
            publication_metadata=parsed.metadata,
        )
        indicators = extract_indicators(
            parsed.text,
            subject_id=collection.subject_id,
            edition_id=collection.edition_id,
            group_id=collection.group_id,
            source_document_id=document.id,
            artifact_id=artifact.id,
        )
        claims = await self._extraction.extract_claims(
            parsed.text,
            subject_id=collection.subject_id,
            edition_id=collection.edition_id,
            group_id=collection.group_id,
            source_document_id=document.id,
            artifact_id=artifact.id,
            external_llm_allowed=document.external_llm_allowed,
        )
        async with self._uow_factory() as uow:
            current = await _require_collection(uow, collection.id)
            if current.state is CollectionState.ARCHIVED:
                await uow.derived_artifacts.append(artifact)
                await uow.indicators.append_many(indicators)
                await uow.claims.append_many(claims)
                current.extracted(artifact.id)
                current.complete()
                await uow.source_collections.save(current)
                await uow.commit()

    async def _record_failure(
        self,
        collection_id: UUID,
        job_id: UUID,
        started_at: datetime,
        error: CollectionError,
    ) -> CollectionState:
        completed_at = datetime.now(UTC)
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            attempt = CollectionAttempt(
                collection_id=collection.id,
                job_id=job_id,
                configuration_id=self._collector.policy.snapshot_id(),
                requested_url=collection.requested_url,
                final_url=error.final_url,
                redirect_chain=error.redirect_chain,
                attempted_at=started_at,
                completed_at=completed_at,
                http_status=error.http_status,
                declared_content_type=(
                    error.headers.get("content-type", "").split(";", 1)[0] or None
                ),
                detected_content_type=None,
                size=error.size,
                sha256=None,
                allowed_headers=error.headers,
                outcome=error.outcome,
                failure_reason=str(error),
            )
            state = (
                CollectionState.FAILED_RETRYABLE
                if error.retryable
                else CollectionState.BLOCKED
                if error.outcome is AttemptOutcome.BLOCKED
                else CollectionState.FAILED_TERMINAL
                if isinstance(error, DownloadTooLargeError)
                else CollectionState.UNAVAILABLE
            )
            await uow.collection_attempts.append(attempt)
            collection.fail(state, attempt_id=attempt.id, reason=str(error))
            await uow.source_collections.save(collection)
            await uow.commit()
            return state


def register_collection_jobs(registry: JobRegistry, service: SubjectCollectionService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, SubjectCollectionParameters):
            raise TypeError("Invalid source collection parameters")
        return await service.collect_subject(parameters.subject_id, context.job_id, context)

    registry.register("source.collect", SubjectCollectionParameters, handler)


def collection_idempotency_key(subject_id: UUID, configuration_id: str, round_number: int) -> str:
    return f"source.collect:{subject_id}:{configuration_id}:{round_number}"


def _new_collection(
    group_id: UUID,
    edition_id: UUID,
    subject_id: UUID,
    batch_id: UUID,
    source: SourceCandidate,
) -> SourceCollection:
    return SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        batch_id=batch_id,
        source_candidate_id=source.id,
        requested_url=source.canonical_url,
        proposed_role=source.role,
    )


def _successful_attempt(
    collection: SourceCollection,
    job_id: UUID,
    started_at: datetime,
    response: CollectedResponse,
    configuration_id: str,
) -> CollectionAttempt:
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=job_id,
        configuration_id=configuration_id,
        requested_url=response.requested_url,
        final_url=response.final_url,
        redirect_chain=response.redirect_chain,
        attempted_at=started_at,
        completed_at=datetime.now(UTC),
        http_status=response.status,
        declared_content_type=response.declared_content_type,
        detected_content_type=response.detected_content_type.value,
        size=len(response.body),
        sha256=response.sha256,
        allowed_headers=response.headers,
        outcome=AttemptOutcome.SUCCEEDED,
        failure_reason=None,
    )


async def _require_collection(uow: UnitOfWork, collection_id: UUID) -> SourceCollection:
    collection = await uow.source_collections.get_for_update(collection_id)
    if collection is None:
        raise CollectionItemNotFoundError(str(collection_id))
    return collection


def _original_name(url: str) -> str:
    name = PurePosixPath(url.split("?", 1)[0]).name
    return name[:500] if name else "source"


def _mime_from_value(value: str) -> DetectedMimeType:
    return DetectedMimeType(value)
