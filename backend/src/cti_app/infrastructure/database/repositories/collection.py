from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    AttemptOutcome,
    Claim,
    ClaimKind,
    CollectionAttempt,
    CollectionPolicySnapshot,
    CollectionState,
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    RejectedModelProposal,
    SourceCollection,
    SourceOriginKind,
    SourceSpan,
)
from cti_app.domain.discovery import (
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.infrastructure.database.models import (
    ClaimRow,
    CollectionAttemptRow,
    CollectionPolicySnapshotRow,
    DerivedArtifactRow,
    IndicatorRow,
    RejectedModelProposalRow,
    SourceCollectionRow,
)


class SqlAlchemySourceCollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, collection: SourceCollection) -> bool:
        statement = (
            insert(SourceCollectionRow)
            .values(**_source_collection_values(collection))
            .on_conflict_do_nothing(
                index_elements=[
                    SourceCollectionRow.subject_id,
                    SourceCollectionRow.canonical_url,
                ]
            )
            .returning(SourceCollectionRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.get(SourceCollectionRow, collection_id)
        return _source_collection_from_row(row) if row else None

    async def get_for_update(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.id == collection_id)
            .with_for_update()
        )
        return _source_collection_from_row(row) if row else None

    async def get_by_canonical_url(
        self, subject_id: UUID, canonical_url: str
    ) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow).where(
                SourceCollectionRow.subject_id == subject_id,
                SourceCollectionRow.canonical_url == canonical_url,
            )
        )
        return _source_collection_from_row(row) if row else None

    async def get_by_candidate(
        self, subject_id: UUID, source_candidate_id: UUID
    ) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow).where(
                SourceCollectionRow.subject_id == subject_id,
                SourceCollectionRow.source_candidate_id == source_candidate_id,
            )
        )
        return _source_collection_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceCollection]:
        rows = await self._session.scalars(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.subject_id == subject_id)
            .order_by(SourceCollectionRow.created_at, SourceCollectionRow.id)
        )
        return [_source_collection_from_row(row) for row in rows]

    async def save(self, collection: SourceCollection) -> None:
        row = await self._session.get(SourceCollectionRow, collection.id)
        if row is None:
            raise LookupError(f"Source collection {collection.id} does not exist")
        for field_name, value in _source_collection_values(collection).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyCollectionAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, attempt: CollectionAttempt) -> None:
        self._session.add(CollectionAttemptRow(**_collection_attempt_values(attempt)))
        await self._session.flush()

    async def list_for_collection(self, collection_id: UUID) -> Sequence[CollectionAttempt]:
        rows = await self._session.scalars(
            select(CollectionAttemptRow)
            .where(CollectionAttemptRow.collection_id == collection_id)
            .order_by(CollectionAttemptRow.attempted_at, CollectionAttemptRow.id)
        )
        return [_collection_attempt_from_row(row) for row in rows]


class SqlAlchemyCollectionPolicySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, snapshot: CollectionPolicySnapshot) -> bool:
        statement = (
            insert(CollectionPolicySnapshotRow)
            .values(**_policy_snapshot_values(snapshot))
            .on_conflict_do_nothing(index_elements=[CollectionPolicySnapshotRow.id])
            .returning(CollectionPolicySnapshotRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, snapshot_id: str) -> CollectionPolicySnapshot | None:
        row = await self._session.get(CollectionPolicySnapshotRow, snapshot_id)
        return _policy_snapshot_from_row(row) if row else None


class SqlAlchemyDerivedArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, artifact: DerivedArtifact) -> None:
        self._session.add(DerivedArtifactRow(**_derived_artifact_values(artifact)))
        await self._session.flush()

    async def get(self, artifact_id: UUID) -> DerivedArtifact | None:
        row = await self._session.get(DerivedArtifactRow, artifact_id)
        return _derived_artifact_from_row(row) if row else None


class SqlAlchemyClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, claims: Sequence[Claim]) -> None:
        self._session.add_all([ClaimRow(**_claim_values(claim)) for claim in claims])
        await self._session.flush()

    async def get(self, claim_id: UUID) -> Claim | None:
        row = await self._session.get(ClaimRow, claim_id)
        return _claim_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Claim]:
        rows = await self._session.scalars(
            select(ClaimRow)
            .where(ClaimRow.subject_id == subject_id)
            .order_by(ClaimRow.created_at, ClaimRow.id)
        )
        return [_claim_from_row(row) for row in rows]


class SqlAlchemyIndicatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, indicators: Sequence[Indicator]) -> None:
        self._session.add_all(
            [IndicatorRow(**_indicator_values(indicator)) for indicator in indicators]
        )
        await self._session.flush()

    async def get(self, indicator_id: UUID) -> Indicator | None:
        row = await self._session.get(IndicatorRow, indicator_id)
        return _indicator_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Indicator]:
        rows = await self._session.scalars(
            select(IndicatorRow)
            .where(IndicatorRow.subject_id == subject_id)
            .order_by(IndicatorRow.created_at, IndicatorRow.id)
        )
        return [_indicator_from_row(row) for row in rows]


class SqlAlchemyRejectedModelProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, proposals: Sequence[RejectedModelProposal]) -> None:
        self._session.add_all(
            [RejectedModelProposalRow(**_rejected_proposal_values(item)) for item in proposals]
        )
        await self._session.flush()


def _source_collection_values(collection: SourceCollection) -> dict[str, object]:
    return {
        "id": collection.id,
        "subject_id": collection.subject_id,
        "edition_id": collection.edition_id,
        "group_id": collection.group_id,
        "batch_id": collection.batch_id,
        "source_candidate_id": collection.source_candidate_id,
        "origin_kind": collection.origin_kind.value,
        "requested_url": collection.requested_url,
        "canonical_url": collection.canonical_url,
        "title": collection.title,
        "publisher": collection.publisher,
        "published_at": collection.published_at,
        "source_tlp": collection.source_tlp.value,
        "sensitivity": collection.sensitivity,
        "external_llm_allowed": collection.external_llm_allowed,
        "do_not_submit": collection.do_not_submit,
        "proposed_role": collection.proposed_role.value,
        "relationship_status": collection.relationship_status.value,
        "relationship_evidence": collection.relationship_evidence,
        "state": collection.state.value,
        "source_document_id": collection.source_document_id,
        "decoded_blob_id": collection.decoded_blob_id,
        "latest_attempt_id": collection.latest_attempt_id,
        "derived_artifact_id": collection.derived_artifact_id,
        "fetch_job_id": collection.fetch_job_id,
        "fetch_policy_snapshot_id": collection.fetch_policy_snapshot_id,
        "fetch_started_at": collection.fetch_started_at,
        "fetch_lease_expires_at": collection.fetch_lease_expires_at,
        "error_reason": collection.error_reason,
        "attempt_count": collection.attempt_count,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
    }


def _source_collection_from_row(row: SourceCollectionRow) -> SourceCollection:
    return SourceCollection(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        batch_id=row.batch_id,
        source_candidate_id=row.source_candidate_id,
        origin_kind=SourceOriginKind(row.origin_kind),
        requested_url=row.requested_url,
        canonical_url=row.canonical_url,
        title=row.title,
        publisher=row.publisher,
        published_at=row.published_at,
        source_tlp=TLP(row.source_tlp),
        sensitivity=row.sensitivity,
        external_llm_allowed=row.external_llm_allowed,
        do_not_submit=row.do_not_submit,
        proposed_role=SourceRole(row.proposed_role),
        relationship_status=SourceRelationshipStatus(row.relationship_status),
        relationship_evidence=row.relationship_evidence,
        state=CollectionState(row.state),
        source_document_id=row.source_document_id,
        decoded_blob_id=row.decoded_blob_id,
        latest_attempt_id=row.latest_attempt_id,
        derived_artifact_id=row.derived_artifact_id,
        fetch_job_id=row.fetch_job_id,
        fetch_policy_snapshot_id=row.fetch_policy_snapshot_id,
        fetch_started_at=row.fetch_started_at,
        fetch_lease_expires_at=row.fetch_lease_expires_at,
        error_reason=row.error_reason,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _collection_attempt_values(attempt: CollectionAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "collection_id": attempt.collection_id,
        "job_id": attempt.job_id,
        "configuration_id": attempt.policy_snapshot_id,
        "policy_snapshot_id": attempt.policy_snapshot_id,
        "requested_url": attempt.requested_url,
        "final_url": attempt.final_url,
        "redirect_chain": list(attempt.redirect_chain),
        "attempted_at": attempt.attempted_at,
        "completed_at": attempt.completed_at,
        "http_status": attempt.http_status,
        "declared_content_type": attempt.declared_content_type,
        "detected_content_type": attempt.detected_content_type,
        "size": attempt.encoded_size,
        "sha256": attempt.encoded_sha256,
        "encoded_size": attempt.encoded_size,
        "encoded_sha256": attempt.encoded_sha256,
        "decoded_size": attempt.decoded_size,
        "decoded_sha256": attempt.decoded_sha256,
        "content_encoding": attempt.content_encoding,
        "allowed_headers": attempt.allowed_headers,
        "outcome": attempt.outcome.value,
        "failure_reason": attempt.failure_reason,
    }


def _collection_attempt_from_row(row: CollectionAttemptRow) -> CollectionAttempt:
    return CollectionAttempt(
        id=row.id,
        collection_id=row.collection_id,
        job_id=row.job_id,
        policy_snapshot_id=row.policy_snapshot_id,
        requested_url=row.requested_url,
        final_url=row.final_url,
        redirect_chain=tuple(row.redirect_chain),
        attempted_at=row.attempted_at,
        completed_at=row.completed_at,
        http_status=row.http_status,
        declared_content_type=row.declared_content_type,
        detected_content_type=row.detected_content_type,
        encoded_size=row.encoded_size,
        encoded_sha256=row.encoded_sha256,
        decoded_size=row.decoded_size,
        decoded_sha256=row.decoded_sha256,
        content_encoding=row.content_encoding,
        allowed_headers=row.allowed_headers,
        outcome=AttemptOutcome(row.outcome),
        failure_reason=row.failure_reason,
    )


def _derived_artifact_values(artifact: DerivedArtifact) -> dict[str, object]:
    return {
        "id": artifact.id,
        "source_document_id": artifact.source_document_id,
        "text_blob_id": artifact.text_blob_id,
        "parser_name": artifact.parser_name,
        "parser_version": artifact.parser_version,
        "text_length": artifact.text_length,
        "publication_metadata": artifact.publication_metadata,
        "created_at": artifact.created_at,
    }


def _derived_artifact_from_row(row: DerivedArtifactRow) -> DerivedArtifact:
    return DerivedArtifact(
        id=row.id,
        source_document_id=row.source_document_id,
        text_blob_id=row.text_blob_id,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        text_length=row.text_length,
        publication_metadata=row.publication_metadata,
        created_at=row.created_at,
    )


def _claim_values(claim: Claim) -> dict[str, object]:
    return {
        "id": claim.id,
        "subject_id": claim.subject_id,
        "edition_id": claim.edition_id,
        "group_id": claim.group_id,
        "source_document_id": claim.source_document_id,
        "derived_artifact_id": claim.derived_artifact_id,
        "kind": claim.kind.value,
        "value": claim.value,
        "span_start": claim.span.start,
        "span_end": claim.span.end,
        "extraction_method": claim.extraction_method,
        "extraction_payload": claim.extraction_payload,
        "chunk_id": claim.chunk_id,
        "local_span_start": claim.local_span.start if claim.local_span else None,
        "local_span_end": claim.local_span.end if claim.local_span else None,
        "model_run_id": claim.model_run_id,
        "created_at": claim.created_at,
    }


def _claim_from_row(row: ClaimRow) -> Claim:
    return Claim(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=ClaimKind(row.kind),
        value=row.value,
        span=SourceSpan(row.span_start, row.span_end),
        extraction_method=row.extraction_method,
        extraction_payload=row.extraction_payload,
        chunk_id=row.chunk_id,
        local_span=(
            SourceSpan(row.local_span_start, row.local_span_end)
            if row.local_span_start is not None and row.local_span_end is not None
            else None
        ),
        model_run_id=row.model_run_id,
        created_at=row.created_at,
    )


def _indicator_values(indicator: Indicator) -> dict[str, object]:
    return {
        "id": indicator.id,
        "subject_id": indicator.subject_id,
        "edition_id": indicator.edition_id,
        "group_id": indicator.group_id,
        "source_document_id": indicator.source_document_id,
        "derived_artifact_id": indicator.derived_artifact_id,
        "kind": indicator.kind.value,
        "original_value": indicator.original_value,
        "normalized_value": indicator.normalized_value,
        "span_start": indicator.span.start,
        "span_end": indicator.span.end,
        "created_at": indicator.created_at,
    }


def _indicator_from_row(row: IndicatorRow) -> Indicator:
    return Indicator(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=IndicatorKind(row.kind),
        original_value=row.original_value,
        normalized_value=row.normalized_value,
        span=SourceSpan(row.span_start, row.span_end),
        created_at=row.created_at,
    )


def _policy_snapshot_values(snapshot: CollectionPolicySnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "max_redirects": snapshot.max_redirects,
        "timeout_seconds": snapshot.timeout_seconds,
        "max_download_bytes": snapshot.max_download_bytes,
        "max_expanded_bytes": snapshot.max_expanded_bytes,
        "max_decompression_ratio": snapshot.max_decompression_ratio,
        "user_agent": snapshot.user_agent,
        "allowed_domains": list(snapshot.allowed_domains),
        "blocked_domains": list(snapshot.blocked_domains),
        "collector_version": snapshot.collector_version,
        "extraction_limits": snapshot.extraction_limits,
        "created_at": snapshot.created_at,
    }


def _policy_snapshot_from_row(row: CollectionPolicySnapshotRow) -> CollectionPolicySnapshot:
    return CollectionPolicySnapshot(
        id=row.id,
        max_redirects=row.max_redirects,
        timeout_seconds=row.timeout_seconds,
        max_download_bytes=row.max_download_bytes,
        max_expanded_bytes=row.max_expanded_bytes,
        max_decompression_ratio=row.max_decompression_ratio,
        user_agent=row.user_agent,
        allowed_domains=tuple(row.allowed_domains),
        blocked_domains=tuple(row.blocked_domains),
        collector_version=row.collector_version,
        extraction_limits=row.extraction_limits,
        created_at=row.created_at,
    )


def _rejected_proposal_values(proposal: RejectedModelProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "source_document_id": proposal.source_document_id,
        "derived_artifact_id": proposal.derived_artifact_id,
        "chunk_id": proposal.chunk_id,
        "category": proposal.category,
        "requested_kind": proposal.requested_kind,
        "reason": proposal.reason,
        "proposal_hash": proposal.proposal_hash,
        "model_run_id": proposal.model_run_id,
        "created_at": proposal.created_at,
    }
