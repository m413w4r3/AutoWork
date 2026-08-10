from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ConfigDict, Field

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.extraction import (
    PARSER_NAME,
    PARSER_VERSION,
    DocumentParsingError,
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
    JobCancelledError,
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
    CollectionPolicySnapshot,
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
    collection_id: UUID | None = None
    requested_by: str = Field(min_length=1, max_length=255)


class SubjectCollectionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        collector: SafeHttpCollector,
        blob_store: BlobStore,
        extraction: EvidenceExtractionService,
        *,
        fetch_lease_seconds: float = 120.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._collector = collector
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self._extraction = extraction
        self._fetch_lease = timedelta(
            seconds=max(fetch_lease_seconds, self._collector.policy.timeout_seconds + 5)
        )
        self._policy_snapshot = self._collector.policy.snapshot(self._extraction.policy_values())

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
        return self._policy_snapshot.id

    @property
    def policy_snapshot(self) -> CollectionPolicySnapshot:
        return self._policy_snapshot

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

    async def prepare_retry(self, collection_id: UUID) -> SourceCollection:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            attempts = await uow.collection_attempts.list_for_collection(collection_id)
            previous_policy = attempts[-1].policy_snapshot_id if attempts else None
            collection.prepare_explicit_retry(
                policy_changed=previous_policy is not None
                and previous_policy != self._policy_snapshot.id
            )
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            await uow.source_collections.save(collection)
            await uow.commit()
            return collection

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
        self,
        subject_id: UUID,
        job_id: UUID,
        context: JobExecutionContext,
        *,
        collection_id: UUID | None = None,
    ) -> str:
        sources = await self.initialize(subject_id)
        if collection_id is not None:
            sources = [item for item in sources if item.id == collection_id]
            if not sources:
                raise CollectionItemNotFoundError(str(collection_id))
        retryable = False
        terminal_failure = False
        for index, source in enumerate(sources, start=1):
            await context.check_cancelled()
            await context.report_progress(index - 1, len(sources), f"Collecte de la source {index}")
            if source.state in {
                CollectionState.COMPLETED,
                CollectionState.BLOCKED,
                CollectionState.FAILED_TERMINAL,
            }:
                continue
            state = await self.collect_one(source.id, job_id, context=context)
            retryable = retryable or state in {
                CollectionState.FAILED_RETRYABLE,
                CollectionState.FETCHING,
            }
            terminal_failure = terminal_failure or state is CollectionState.FAILED_TERMINAL
            await context.heartbeat()
        await context.report_progress(len(sources), len(sources), "Collecte terminée")
        if retryable:
            raise JobHandlerError(
                "source_collection_transient",
                "Certaines sources sont temporairement indisponibles.",
                transient=True,
            )
        if terminal_failure:
            raise JobHandlerError(
                "source_collection_terminal",
                "Une source a dépassé une limite sûre ou ne peut pas être extraite.",
                transient=False,
            )
        return f"subject-collection://{subject_id}"

    async def collect_one(
        self,
        collection_id: UUID,
        job_id: UUID,
        *,
        context: JobExecutionContext | None = None,
    ) -> CollectionState:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            if collection.state is CollectionState.COMPLETED:
                return collection.state
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            if collection.state in {CollectionState.ARCHIVED, CollectionState.EXTRACTED}:
                pass
            else:
                now = datetime.now(UTC)
                if collection.state is CollectionState.FETCHING:
                    if (
                        collection.fetch_lease_expires_at
                        and collection.fetch_lease_expires_at > now
                    ):
                        return collection.state
                    await uow.collection_attempts.append(
                        _interrupted_attempt(
                            collection,
                            now,
                            fallback_job_id=job_id,
                            fallback_policy_snapshot_id=self._policy_snapshot.id,
                        )
                    )
                claimed = collection.claim_fetch(
                    job_id,
                    lease_duration=self._fetch_lease,
                    policy_snapshot_id=self._policy_snapshot.id,
                    now=now,
                )
                if not claimed:
                    return collection.state
                await uow.source_collections.save(collection)
                await uow.commit()

        if collection.state is CollectionState.FETCHING:
            started_at = collection.fetch_started_at or datetime.now(UTC)
            try:
                response = await self._collector.fetch(
                    collection.requested_url,
                    cancellation_check=context.check_cancelled if context else None,
                )
            except JobCancelledError:
                await self._record_interruption(
                    collection.id,
                    job_id,
                    "Collection cancelled before archival",
                )
                raise
            except CollectionError as exc:
                return await self._record_failure(collection.id, job_id, started_at, exc)
            if not await self._renew_fetch_lease(collection.id, job_id):
                return CollectionState.FETCHING
            if context is not None:
                try:
                    await context.check_cancelled()
                except JobCancelledError:
                    await self._record_interruption(
                        collection.id,
                        job_id,
                        "Collection cancelled before archival",
                    )
                    raise
            await self._archive(collection.id, job_id, started_at, response)

        await self._extract(collection.id, context=context)
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
        raw_blob = await self._catalog.ingest(
            BytesIO(response.encoded_body),
            logical_bucket="source-raw",
            mime_type="application/octet-stream",
        )
        decoded_blob = await self._catalog.ingest(
            BytesIO(response.decoded_body),
            logical_bucket="source-decoded",
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
                blob_id=raw_blob.id,
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
                self._policy_snapshot.id,
            )
            await uow.source_documents.add(document)
            await uow.collection_attempts.append(attempt)
            collection.archive(
                job_id=job_id,
                attempt_id=attempt.id,
                source_document_id=document.id,
                decoded_blob_id=decoded_blob.id,
            )
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
                        "encoded_sha256": response.encoded_sha256,
                        "decoded_sha256": response.decoded_sha256,
                        "content_encoding": response.content_encoding,
                        "job_id": str(job_id),
                    },
                    tlp=subject.tlp,
                    actor_id="system:collector",
                )
            )
            await uow.commit()

    async def _renew_fetch_lease(self, collection_id: UUID, job_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            renewed = collection.renew_fetch_lease(
                job_id,
                lease_duration=self._fetch_lease,
            )
            if renewed:
                await uow.source_collections.save(collection)
                await uow.commit()
            return renewed

    async def _extract(
        self,
        collection_id: UUID,
        *,
        context: JobExecutionContext | None = None,
    ) -> None:
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
                or not collection.decoded_blob_id
            ):
                return
            document = await uow.source_documents.get(collection.source_document_id)
            blob = await uow.blobs.get(collection.decoded_blob_id) if document else None
            if document is None or blob is None:
                raise CollectionItemNotFoundError("Archived source content is missing")
        if context is not None:
            await context.check_cancelled()
        raw = await self._blob_store.read(
            blob.descriptor, max_bytes=self._collector.policy.max_expanded_bytes
        )
        try:
            parsed = parse_document(
                raw,
                _mime_from_value(blob.descriptor.mime_type),
                self._extraction.pdf_policy,
            )
        except DocumentParsingError as exc:
            await self._record_processing_failure(
                collection.id,
                f"{exc.code}: {exc}",
            )
            return
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
        outcome = await self._extraction.extract_claims(
            parsed.text,
            subject_id=collection.subject_id,
            edition_id=collection.edition_id,
            group_id=collection.group_id,
            source_document_id=document.id,
            artifact_id=artifact.id,
            external_llm_allowed=document.external_llm_allowed,
            cancellation_check=context.check_cancelled if context else None,
        )
        if context is not None:
            await context.check_cancelled()
        async with self._uow_factory() as uow:
            current = await _require_collection(uow, collection.id)
            if current.state is CollectionState.ARCHIVED:
                await uow.derived_artifacts.append(artifact)
                await uow.indicators.append_many(indicators)
                await uow.claims.append_many(outcome.claims)
                await uow.rejected_model_proposals.append_many(outcome.rejected_proposals)
                current.extracted(artifact.id)
                current.complete()
                await uow.source_collections.save(current)
                await uow.commit()

    async def _record_processing_failure(self, collection_id: UUID, reason: str) -> None:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            if subject is None:
                raise CollectionItemNotFoundError(str(collection.subject_id))
            collection.fail_processing(reason=reason)
            await uow.source_collections.save(collection)
            await uow.provenance.append(
                ProvenanceEvent(
                    subject_id=collection.subject_id,
                    aggregate_type="source_collection",
                    aggregate_id=collection.id,
                    event_type="source.extraction_failed",
                    payload={"reason": reason},
                    tlp=subject.tlp,
                    actor_id="system:extractor",
                )
            )
            await uow.commit()

    async def _record_interruption(
        self,
        collection_id: UUID,
        job_id: UUID,
        reason: str,
    ) -> None:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            if collection.state is not CollectionState.FETCHING:
                return
            now = datetime.now(UTC)
            attempt = _interrupted_attempt(
                collection,
                now,
                fallback_job_id=job_id,
                fallback_policy_snapshot_id=self._policy_snapshot.id,
                reason=reason,
            )
            await uow.collection_attempts.append(attempt)
            collection.fail(
                CollectionState.FAILED_RETRYABLE,
                attempt_id=attempt.id,
                reason=reason,
            )
            await uow.source_collections.save(collection)
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
                policy_snapshot_id=self._policy_snapshot.id,
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
                encoded_size=error.encoded_size,
                encoded_sha256=None,
                decoded_size=None,
                decoded_sha256=None,
                content_encoding=None,
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
        return await service.collect_subject(
            parameters.subject_id,
            context.job_id,
            context,
            collection_id=parameters.collection_id,
        )

    registry.register("source.collect", SubjectCollectionParameters, handler)


def collection_idempotency_key(
    subject_id: UUID,
    configuration_id: str,
    round_number: int,
    *,
    collection_id: UUID | None = None,
) -> str:
    target = str(collection_id) if collection_id else "all"
    return f"source.collect:{subject_id}:{target}:{configuration_id}:{round_number}"


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
    policy_snapshot_id: str,
) -> CollectionAttempt:
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=job_id,
        policy_snapshot_id=policy_snapshot_id,
        requested_url=response.requested_url,
        final_url=response.final_url,
        redirect_chain=response.redirect_chain,
        attempted_at=started_at,
        completed_at=datetime.now(UTC),
        http_status=response.status,
        declared_content_type=response.declared_content_type,
        detected_content_type=response.detected_content_type.value,
        encoded_size=response.encoded_size,
        encoded_sha256=response.encoded_sha256,
        decoded_size=response.decoded_size,
        decoded_sha256=response.decoded_sha256,
        content_encoding=response.content_encoding,
        allowed_headers=response.headers,
        outcome=AttemptOutcome.SUCCEEDED,
        failure_reason=None,
    )


def _interrupted_attempt(
    collection: SourceCollection,
    completed_at: datetime,
    *,
    fallback_job_id: UUID,
    fallback_policy_snapshot_id: str,
    reason: str = "Previous fetch lease expired before archival",
) -> CollectionAttempt:
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=collection.fetch_job_id or fallback_job_id,
        policy_snapshot_id=(collection.fetch_policy_snapshot_id or fallback_policy_snapshot_id),
        requested_url=collection.requested_url,
        final_url=None,
        redirect_chain=(),
        attempted_at=collection.fetch_started_at or completed_at,
        completed_at=completed_at,
        http_status=None,
        declared_content_type=None,
        detected_content_type=None,
        encoded_size=None,
        encoded_sha256=None,
        decoded_size=None,
        decoded_sha256=None,
        content_encoding=None,
        allowed_headers={},
        outcome=AttemptOutcome.INTERRUPTED,
        failure_reason=reason,
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
