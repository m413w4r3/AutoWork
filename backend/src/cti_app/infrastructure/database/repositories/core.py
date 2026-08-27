"""RF-P3/R09: repositories for Blob, Subject, SourceDocument, Sample, Provenance —
foundational entities every other bounded context references. Owns the only
row/domain mappers for these rows."""

from collections.abc import Iterable, Sequence
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.entities import (
    ProvenanceEvent,
    Sample,
    SampleHashSource,
    SampleOrigin,
    SampleState,
    SourceDocument,
    Subject,
)
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.analysis import SampleFeatureSetV1, SampleFormat
from cti_app.domain.goodware import GoodwareBaseline, GoodwareFeature, GoodwareSource
from cti_app.domain.reference_corpus import ReferenceLabelSource, ReferenceMember, ReferenceMemberDispute
from cti_app.domain.capabilities import Capability, CapabilitySet, CapabilitySetStatus
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalFileView,
    VirusTotalObservation,
    VirusTotalOperation,
)
from cti_app.infrastructure.database.models.briefs import BriefEvidencePackRow
from cti_app.infrastructure.database.models.collection import (
    DerivedArtifactRow,
    SourceCollectionRow,
)
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    GoodwareBaselineRow,
    GoodwareBaselineSourceRow,
    GoodwareFeatureRow,
    InvestigationGoodwareBaselineRow,
    ReferenceMemberDisputeRow,
    ReferenceMemberRow,
    CapabilitySetRow,
    ProvenanceEventRow,
    SampleRow,
    SampleFeatureIndexRow,
    SampleFeatureSetRow,
    SourceDocumentRow,
    SubjectRow,
    VirusTotalFileViewRow,
    VirusTotalObservationRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationTurnRow,
    ModelRunRow,
)


class SqlAlchemyBlobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, blob: BlobRecord) -> None:
        descriptor = blob.descriptor
        self._session.add(
            BlobRow(
                id=blob.id,
                sha256=descriptor.sha256,
                size=descriptor.size,
                mime_type=descriptor.mime_type,
                logical_bucket=descriptor.logical_bucket,
                object_key=descriptor.object_key,
                created_at=blob.created_at,
            )
        )
        await self._session.flush()

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        row = await self._session.get(BlobRow, blob_id)
        return _blob_from_row(row) if row else None

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        row = await self._session.scalar(
            select(BlobRow).where(
                BlobRow.logical_bucket == logical_bucket,
                BlobRow.sha256 == sha256,
            )
        )
        return _blob_from_row(row) if row else None

    async def count_references(self, blob_id: UUID) -> int:
        document_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceDocumentRow)
            .where(SourceDocumentRow.blob_id == blob_id)
        )
        decoded_document_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceDocumentRow)
            .where(SourceDocumentRow.decoded_blob_id == blob_id)
        )
        sample_count = await self._session.scalar(
            select(func.count()).select_from(SampleRow).where(SampleRow.blob_id == blob_id)
        )
        model_output_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelRunRow)
            .where(ModelRunRow.output_references.contains([f"blob://{blob_id}"]))
        )
        artifact_count = await self._session.scalar(
            select(func.count())
            .select_from(DerivedArtifactRow)
            .where(DerivedArtifactRow.text_blob_id == blob_id)
        )
        decoded_source_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceCollectionRow)
            .where(SourceCollectionRow.decoded_blob_id == blob_id)
        )
        brief_pack_count = await self._session.scalar(
            select(func.count())
            .select_from(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.blob_id == blob_id)
        )
        conversation_input_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.input_blob_reference == f"blob://{blob_id}")
        )
        conversation_output_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.output_blob_reference == f"blob://{blob_id}")
        )
        virustotal_observation_count = await self._session.scalar(
            select(func.count())
            .select_from(VirusTotalObservationRow)
            .where(VirusTotalObservationRow.blob_id == blob_id)
        )
        feature_set_count = await self._session.scalar(select(func.count()).select_from(SampleFeatureSetRow).where(SampleFeatureSetRow.blob_id == blob_id))
        feature_payload_count = await self._session.scalar(select(func.count()).select_from(SampleFeatureSetRow).where(SampleFeatureSetRow.feature_blob_id == blob_id))
        goodware_source_count = await self._session.scalar(select(func.count()).select_from(GoodwareBaselineSourceRow).where(GoodwareBaselineSourceRow.blob_id == blob_id))
        capability_set_count = await self._session.scalar(select(func.count()).select_from(CapabilitySetRow).where(CapabilitySetRow.blob_id == blob_id))
        return (
            int(document_count or 0)
            + int(decoded_document_count or 0)
            + int(sample_count or 0)
            + int(model_output_count or 0)
            + int(artifact_count or 0)
            + int(decoded_source_count or 0)
            + int(brief_pack_count or 0)
            + int(conversation_input_count or 0)
            + int(conversation_output_count or 0)
            + int(virustotal_observation_count or 0)
            + int(feature_set_count or 0)
            + int(feature_payload_count or 0)
            + int(goodware_source_count or 0)
            + int(capability_set_count or 0)
        )


class SqlAlchemyCapabilitySetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, sample_id: UUID, tool_version: str, ruleset_sha256: str, parameters_sha256: str) -> CapabilitySet | None:
        row = await self._session.scalar(select(CapabilitySetRow).where(CapabilitySetRow.sample_id == sample_id, CapabilitySetRow.tool_version == tool_version, CapabilitySetRow.ruleset_sha256 == ruleset_sha256, CapabilitySetRow.parameters_sha256 == parameters_sha256))
        if row is None:
            return None
        return _capability_set_from_row(row)

    async def add_if_absent(self, capability_set: CapabilitySet, blob_id: UUID) -> bool:
        from sqlalchemy.dialects.postgresql import insert
        statement = insert(CapabilitySetRow).values(id=uuid4(), sample_id=capability_set.sample_id, blob_id=blob_id, tool_name=capability_set.tool_name, tool_version=capability_set.tool_version, ruleset_sha256=capability_set.ruleset_sha256, parameters_sha256=capability_set.parameters_sha256, status=capability_set.status.value, capabilities=[{"rule_id": item.rule_id, "name": item.name, "namespace": item.namespace, "attack": list(item.attack), "mbc": list(item.mbc), "function_addresses": list(item.function_addresses)} for item in capability_set.capabilities], errors=list(capability_set.errors)).on_conflict_do_nothing(constraint="uq_capability_sets_replay")
        result = await self._session.execute(statement)
        await self._session.flush()
        return bool(result.rowcount)

    async def index(self, capability_set: CapabilitySet) -> None:
        row = await self._session.scalar(select(CapabilitySetRow).where(CapabilitySetRow.sample_id == capability_set.sample_id, CapabilitySetRow.tool_version == capability_set.tool_version, CapabilitySetRow.ruleset_sha256 == capability_set.ruleset_sha256, CapabilitySetRow.parameters_sha256 == capability_set.parameters_sha256))
        if row is None:
            raise RuntimeError("capability set is missing")
        for capability in capability_set.capabilities:
            self._session.add(SampleFeatureIndexRow(id=uuid4(), sample_id=capability_set.sample_id, capability_set_id=row.id, feature_kind="capability", normalized_value=capability.rule_id.lower(), occurrence_count=1))
        await self._session.flush()


def _capability_set_from_row(row: CapabilitySetRow) -> CapabilitySet:
    return CapabilitySet(sample_id=row.sample_id, tool_name=row.tool_name, tool_version=row.tool_version, ruleset_sha256=row.ruleset_sha256, parameters_sha256=row.parameters_sha256, status=CapabilitySetStatus(row.status), capabilities=tuple(Capability(**item) for item in row.capabilities), errors=tuple(row.errors))


class SqlAlchemyGoodwareBaselineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_source_set_sha256(self, source_set_sha256: str) -> GoodwareBaseline | None:
        row = await self._session.scalar(select(GoodwareBaselineRow).where(GoodwareBaselineRow.source_set_sha256 == source_set_sha256))
        if row is None:
            return None
        sources = await self._session.scalars(select(GoodwareBaselineSourceRow).where(GoodwareBaselineSourceRow.baseline_id == row.id).order_by(GoodwareBaselineSourceRow.filename))
        return GoodwareBaseline(id=row.id, source_set_sha256=row.source_set_sha256, records_sha256=row.records_sha256, record_count=row.record_count, occurrence_sum=row.occurrence_sum, pattern_version=row.pattern_version, sources=tuple(GoodwareSource(filename=s.filename, feature_kind=s.feature_kind, sha256=s.sha256, size=s.size, blob_id=s.blob_id) for s in sources))

    async def add(self, baseline: GoodwareBaseline) -> None:
        from datetime import UTC, datetime
        self._session.add(GoodwareBaselineRow(id=baseline.id, source_set_sha256=baseline.source_set_sha256, records_sha256=baseline.records_sha256, record_count=baseline.record_count, occurrence_sum=baseline.occurrence_sum, pattern_version=baseline.pattern_version, created_at=datetime.now(UTC)))
        await self._session.flush()

    async def add_sources(self, baseline_id: UUID, sources: Sequence[GoodwareSource]) -> None:
        for source in sources:
            self._session.add(GoodwareBaselineSourceRow(id=uuid4(), baseline_id=baseline_id, filename=source.filename, feature_kind=source.feature_kind, sha256=source.sha256, size=source.size, blob_id=source.blob_id))
        await self._session.flush()

    async def add_features(self, baseline_id: UUID, features: Iterable[GoodwareFeature]) -> None:
        for feature in features:
            self._session.add(GoodwareFeatureRow(id=uuid4(), baseline_id=baseline_id, feature_kind=feature.feature_kind, normalized_value=feature.normalized_value, occurrence_count=feature.occurrence_count))
        await self._session.flush()


class SqlAlchemyInvestigationGoodwareBaselineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, investigation_id: UUID) -> UUID | None:
        row = await self._session.get(InvestigationGoodwareBaselineRow, investigation_id)
        return row.baseline_id if row else None

    async def add(self, investigation_id: UUID, baseline_id: UUID) -> None:
        self._session.add(InvestigationGoodwareBaselineRow(investigation_id=investigation_id, baseline_id=baseline_id))
        await self._session.flush()


class SqlAlchemyReferenceMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, member: ReferenceMember) -> ReferenceMember:
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(ReferenceMemberRow).values(
            id=member.id, sample_id=member.sample_id, sample_sha256=member.sample_sha256,
            family_label=member.family_label, origin_investigation_id=member.origin_investigation_id,
            promoted_at=member.promoted_at, actor_id=member.actor_id, label_source=member.label_source.value,
        ).on_conflict_do_nothing(constraint="uq_reference_members_sample_label")
        await self._session.execute(stmt)
        await self._session.flush()
        row = await self._session.scalar(select(ReferenceMemberRow).where(ReferenceMemberRow.sample_id == member.sample_id, ReferenceMemberRow.family_label == member.family_label))
        return _reference_member_from_row(row)  # type: ignore[arg-type]

    async def get(self, member_id: UUID) -> ReferenceMember | None:
        row = await self._session.get(ReferenceMemberRow, member_id)
        return _reference_member_from_row(row) if row else None

    async def list(self) -> Sequence[ReferenceMember]:
        rows = await self._session.scalars(select(ReferenceMemberRow).order_by(ReferenceMemberRow.promoted_at, ReferenceMemberRow.id))
        return [_reference_member_from_row(row) for row in rows]

    async def append_dispute(self, dispute: ReferenceMemberDispute) -> None:
        self._session.add(ReferenceMemberDisputeRow(member_id=dispute.member_id, reason=dispute.reason, actor_id=dispute.actor_id, created_at=dispute.created_at))
        await self._session.flush()

    async def get_dispute(self, member_id: UUID) -> ReferenceMemberDispute | None:
        row = await self._session.scalar(select(ReferenceMemberDisputeRow).where(ReferenceMemberDisputeRow.member_id == member_id).order_by(ReferenceMemberDisputeRow.created_at))
        return _reference_dispute_from_row(row) if row else None

    async def list_disputes(self, member_id: UUID) -> Sequence[ReferenceMemberDispute]:
        rows = await self._session.scalars(select(ReferenceMemberDisputeRow).where(ReferenceMemberDisputeRow.member_id == member_id).order_by(ReferenceMemberDisputeRow.created_at))
        return [_reference_dispute_from_row(row) for row in rows]

    async def count_eligible_malware_samples(self) -> int:
        count = await self._session.scalar(select(func.count(func.distinct(ReferenceMemberRow.sample_id))).where(ReferenceMemberRow.family_label.not_in(("benign", "unlabeled")), ~select(ReferenceMemberDisputeRow.member_id).where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id).exists()))
        return int(count or 0)

    async def list_feature_members(self, feature_kind: str, normalized_value: str) -> Sequence[tuple[UUID, str]]:
        dispute = select(ReferenceMemberDisputeRow.member_id).where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
        result = await self._session.execute(select(ReferenceMemberRow.sample_id, ReferenceMemberRow.family_label).join(SampleFeatureIndexRow, SampleFeatureIndexRow.sample_id == ReferenceMemberRow.sample_id).where(SampleFeatureIndexRow.feature_kind == feature_kind, SampleFeatureIndexRow.normalized_value == normalized_value.lower(), ReferenceMemberRow.family_label.not_in(("benign", "unlabeled")), ~dispute.exists()).distinct())
        return list(result.all())

    async def count_benign_feature_occurrences(self, feature_kind: str, normalized_value: str) -> int:
        dispute = select(ReferenceMemberDisputeRow.member_id).where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
        count = await self._session.scalar(select(func.count(func.distinct(ReferenceMemberRow.sample_id))).select_from(ReferenceMemberRow).join(SampleFeatureIndexRow, SampleFeatureIndexRow.sample_id == ReferenceMemberRow.sample_id).where(SampleFeatureIndexRow.feature_kind == feature_kind, SampleFeatureIndexRow.normalized_value == normalized_value.lower(), ReferenceMemberRow.family_label == "benign", ~dispute.exists()))
        return int(count or 0)


def _reference_member_from_row(row: ReferenceMemberRow) -> ReferenceMember:
    return ReferenceMember(id=row.id, sample_id=row.sample_id, sample_sha256=row.sample_sha256, family_label=row.family_label, origin_investigation_id=row.origin_investigation_id, promoted_at=row.promoted_at, actor_id=row.actor_id, label_source=ReferenceLabelSource(row.label_source))


def _reference_dispute_from_row(row: ReferenceMemberDisputeRow) -> ReferenceMemberDispute:
    return ReferenceMemberDispute(member_id=row.member_id, reason=row.reason, actor_id=row.actor_id, created_at=row.created_at)


class SqlAlchemySampleFeatureSetRepository:
    def __init__(self, session: AsyncSession) -> None: self._session = session
    async def get(self, sample_id: UUID, extractor_version: str, parameters_sha256: str) -> SampleFeatureSetV1 | None:
        row = await self._session.scalar(select(SampleFeatureSetRow).where(SampleFeatureSetRow.sample_id == sample_id, SampleFeatureSetRow.extractor_version == extractor_version, SampleFeatureSetRow.parameters_sha256 == parameters_sha256))
        return _feature_from_payload(row.payload) if row else None
    async def add_if_absent(self, feature_set: SampleFeatureSetV1, feature_blob_id: UUID) -> bool:
        if await self.get(feature_set.sample_id, feature_set.extractor_version, feature_set.parameters_sha256): return False
        self._session.add(SampleFeatureSetRow(id=uuid4(), sample_id=feature_set.sample_id, blob_id=feature_set.blob_id, feature_blob_id=feature_blob_id, extractor_version=feature_set.extractor_version, parameters_sha256=feature_set.parameters_sha256, payload=feature_set.as_json(), created_at=__import__('datetime').datetime.now(__import__('datetime').UTC)))
        await self._session.flush(); return True
    async def index(self, feature_set: SampleFeatureSetV1) -> None:
        row = await self._session.scalar(select(SampleFeatureSetRow).where(SampleFeatureSetRow.sample_id == feature_set.sample_id, SampleFeatureSetRow.extractor_version == feature_set.extractor_version, SampleFeatureSetRow.parameters_sha256 == feature_set.parameters_sha256))
        if row is None: raise RuntimeError("feature set is missing")
        values = [("string", item["value"], item["occurrence_count"]) for item in feature_set.strings] + [("import", item, 1) for item in feature_set.imports] + [("export", item, 1) for item in feature_set.exports] + [("section", item["name"], 1) for item in feature_set.sections] + [("imphash", feature_set.imphash, 1)] * bool(feature_set.imphash) + [("opcode_fragment16", item, 1) for item in feature_set.opcode_fragment16]
        for kind, value, count in values:
            if not await self._session.scalar(select(SampleFeatureIndexRow.id).where(SampleFeatureIndexRow.feature_set_id == row.id, SampleFeatureIndexRow.feature_kind == kind, SampleFeatureIndexRow.normalized_value == value)):
                self._session.add(SampleFeatureIndexRow(id=uuid4(), sample_id=feature_set.sample_id, feature_set_id=row.id, feature_kind=kind, normalized_value=value.lower(), occurrence_count=count))
        await self._session.flush()


def _feature_from_payload(data: dict) -> SampleFeatureSetV1:
    from uuid import UUID
    return SampleFeatureSetV1(**{**data, "sample_id": UUID(data["sample_id"]), "blob_id": UUID(data["blob_id"]), "format": SampleFormat(data["format"]), "tlp": TLP(data["tlp"]), **{key: tuple(data[key]) for key in ("strings", "sections", "imports", "exports", "resources", "opcode_fragment16", "partial_errors")}})

    async def delete(self, blob_id: UUID) -> None:
        row = await self._session.get(BlobRow, blob_id)
        if row is not None:
            await self._session.delete(row)


class SqlAlchemySubjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, subject: Subject) -> None:
        self._session.add(
            SubjectRow(
                id=subject.id,
                external_id=subject.external_id,
                slug=subject.slug,
                tlp=subject.tlp.value,
                created_at=subject.created_at,
            )
        )
        await self._session.flush()

    async def get(self, subject_id: UUID) -> Subject | None:
        row = await self._session.get(SubjectRow, subject_id)
        if row is None:
            return None
        return Subject(
            id=row.id,
            external_id=row.external_id,
            slug=row.slug,
            tlp=TLP(row.tlp),
            created_at=row.created_at,
        )


class SqlAlchemySourceDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: SourceDocument) -> None:
        self._session.add(_source_document_to_row(document))
        await self._session.flush()

    async def get(self, document_id: UUID) -> SourceDocument | None:
        row = await self._session.get(SourceDocumentRow, document_id)
        return _source_document_from_row(row) if row else None

    async def save(self, document: SourceDocument) -> None:
        row = await self._session.get(SourceDocumentRow, document.id)
        if row is None:
            raise EntityNotFoundError(f"Source document {document.id} does not exist")
        values = _source_document_to_row(document)
        for column in (
            "original_name",
            "origin",
            "logical_filename",
            "source_collection_id",
            "source_candidate_id",
            "decoded_blob_id",
            "title",
            "publisher",
            "published_at",
            "final_url",
            "declared_mime_type",
            "detected_mime_type",
            "encoded_sha256",
            "decoded_sha256",
            "encoded_size",
            "decoded_size",
        ):
            setattr(row, column, getattr(values, column))
        await self._session.flush()

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceDocument]:
        rows = await self._session.scalars(
            select(SourceDocumentRow)
            .where(SourceDocumentRow.subject_id == subject_id)
            .order_by(SourceDocumentRow.created_at, SourceDocumentRow.id)
        )
        return [_source_document_from_row(row) for row in rows]


class SqlAlchemySampleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, sample: Sample) -> None:
        self._session.add(_sample_to_row(sample))
        await self._session.flush()

    async def get(self, sample_id: UUID) -> Sample | None:
        row = await self._session.get(SampleRow, sample_id)
        return _sample_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Sample]:
        rows = await self._session.scalars(
            select(SampleRow)
            .where(SampleRow.subject_id == subject_id)
            .order_by(SampleRow.created_at, SampleRow.id)
        )
        return [_sample_from_row(row) for row in rows]

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None:
        row = await self._session.scalar(
            select(SampleRow).where(
                SampleRow.subject_id == subject_id, SampleRow.blob_id == blob_id
            )
        )
        return _sample_from_row(row) if row else None


class SqlAlchemyProvenanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: ProvenanceEvent) -> None:
        self._session.add(
            ProvenanceEventRow(
                id=event.id,
                subject_id=event.subject_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload=event.payload,
                tlp=event.tlp.value,
                actor_id=event.actor_id,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> Sequence[ProvenanceEvent]:
        rows = await self._session.scalars(
            select(ProvenanceEventRow)
            .where(
                ProvenanceEventRow.aggregate_type == aggregate_type,
                ProvenanceEventRow.aggregate_id == aggregate_id,
            )
            .order_by(ProvenanceEventRow.occurred_at, ProvenanceEventRow.id)
        )
        return [_provenance_from_row(row) for row in rows]


class SqlAlchemyVirusTotalObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, observation: VirusTotalObservation) -> None:
        self._session.add(
            VirusTotalObservationRow(
                id=observation.id,
                subject_id=observation.subject_id,
                operation=observation.operation.value,
                capability=observation.capability.value,
                source_identifier=observation.source_identifier,
                safe_parameters=observation.safe_parameters,
                http_status=observation.http_status,
                blob_id=observation.blob_id,
                raw_sha256=observation.raw_sha256,
                raw_size=observation.raw_size,
                observed_at=observation.observed_at,
                input_cursor=observation.input_cursor,
                output_cursor=observation.output_cursor,
                observed_count=observation.observed_count,
                exhaustive=observation.exhaustive,
                page_order=observation.page_order,
                normalization_contract_version=observation.normalization_contract_version,
                execution_id=observation.execution_id,
            )
        )
        await self._session.flush()

    async def find_file_report_checkpoint(
        self, checkpoint_id: str, file_hash: str
    ) -> VirusTotalObservation | None:
        row = await self._session.scalar(
            select(VirusTotalObservationRow).where(
                VirusTotalObservationRow.operation == "file_report",
                VirusTotalObservationRow.source_identifier == file_hash,
                VirusTotalObservationRow.safe_parameters["checkpoint_id"].as_string()
                == checkpoint_id,
            )
        )
        return _observation_from_row(row) if row else None


def _observation_from_row(row: VirusTotalObservationRow) -> VirusTotalObservation:
    return VirusTotalObservation(
        id=row.id,
        subject_id=row.subject_id,
        operation=VirusTotalOperation(row.operation),
        capability=VirusTotalCapability(row.capability),
        source_identifier=row.source_identifier,
        safe_parameters=row.safe_parameters,
        http_status=row.http_status,
        blob_id=row.blob_id,
        raw_sha256=row.raw_sha256,
        raw_size=row.raw_size,
        observed_at=row.observed_at,
        input_cursor=row.input_cursor,
        output_cursor=row.output_cursor,
        observed_count=row.observed_count,
        exhaustive=row.exhaustive,
        page_order=row.page_order,
        normalization_contract_version=row.normalization_contract_version,
        execution_id=row.execution_id,
    )


class SqlAlchemyVirusTotalFileViewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, view: VirusTotalFileView) -> bool:
        if await self._session.scalar(
            select(VirusTotalFileViewRow.id).where(
                VirusTotalFileViewRow.observation_id == view.observation_id
            )
        ):
            return False
        self._session.add(
            VirusTotalFileViewRow(
                id=view.id,
                observation_id=view.observation_id,
                vt_file_id=view.vt_file_id,
                file_type=view.file_type,
                lookup_hash=view.lookup_hash,
                meaningful_name=view.meaningful_name,
                type_description=view.type_description,
                size=view.size,
                last_analysis_stats=view.last_analysis_stats,
                first_submission_date=view.first_submission_date,
                last_submission_date=view.last_submission_date,
                last_modification_date=view.last_modification_date,
                tags=list(view.tags),
                vhash=view.vhash,
                imphash=view.imphash,
                ssdeep=view.ssdeep,
                tlsh=view.tlsh,
                main_icon_dhash=view.main_icon_dhash,
                rich_header_hash=view.rich_header_hash,
            )
        )
        await self._session.flush()
        return True


def _blob_from_row(row: BlobRow) -> BlobRecord:
    return BlobRecord(
        id=row.id,
        descriptor=BlobDescriptor(
            sha256=row.sha256,
            size=row.size,
            mime_type=row.mime_type,
            logical_bucket=row.logical_bucket,
        ),
        created_at=row.created_at,
    )


def _source_document_to_row(document: SourceDocument) -> SourceDocumentRow:
    return SourceDocumentRow(
        id=document.id,
        subject_id=document.subject_id,
        blob_id=document.blob_id,
        original_name=document.original_name,
        origin=document.origin,
        acquired_at=document.acquired_at,
        license_restriction=document.license_restriction,
        tlp=document.tlp.value,
        do_not_submit=document.do_not_submit,
        external_llm_allowed=document.external_llm_allowed,
        logical_filename=document.logical_filename,
        source_collection_id=document.source_collection_id,
        source_candidate_id=document.source_candidate_id,
        decoded_blob_id=document.decoded_blob_id,
        title=document.title,
        publisher=document.publisher,
        published_at=document.published_at,
        final_url=document.final_url,
        declared_mime_type=document.declared_mime_type,
        detected_mime_type=document.detected_mime_type,
        encoded_sha256=document.encoded_sha256,
        decoded_sha256=document.decoded_sha256,
        encoded_size=document.encoded_size,
        decoded_size=document.decoded_size,
        created_at=document.created_at,
    )


def _source_document_from_row(row: SourceDocumentRow) -> SourceDocument:
    return SourceDocument(
        id=row.id,
        subject_id=row.subject_id,
        blob_id=row.blob_id,
        original_name=row.original_name,
        origin=row.origin,
        acquired_at=row.acquired_at,
        license_restriction=row.license_restriction,
        tlp=TLP(row.tlp),
        do_not_submit=row.do_not_submit,
        external_llm_allowed=row.external_llm_allowed,
        logical_filename=row.logical_filename,
        source_collection_id=row.source_collection_id,
        source_candidate_id=row.source_candidate_id,
        decoded_blob_id=row.decoded_blob_id,
        title=row.title,
        publisher=row.publisher,
        published_at=row.published_at,
        final_url=row.final_url,
        declared_mime_type=row.declared_mime_type,
        detected_mime_type=row.detected_mime_type,
        encoded_sha256=row.encoded_sha256,
        decoded_sha256=row.decoded_sha256,
        encoded_size=row.encoded_size,
        decoded_size=row.decoded_size,
        created_at=row.created_at,
    )


def _sample_to_row(sample: Sample) -> SampleRow:
    return SampleRow(
        id=sample.id,
        subject_id=sample.subject_id,
        blob_id=sample.blob_id,
        original_name=sample.original_name,
        origin=sample.origin,
        acquired_at=sample.acquired_at,
        license_restriction=sample.license_restriction,
        tlp=sample.tlp.value,
        do_not_submit=sample.do_not_submit,
        external_llm_allowed=sample.external_llm_allowed,
        origin_kind=sample.origin_kind.value,
        state=sample.state.value,
        source_service=sample.source_service,
        source_object_id=sample.source_object_id,
        expected_hash=sample.expected_hash,
        validation_actor=sample.validation_actor,
        validation_date=sample.validation_date,
        validation_reason=sample.validation_reason,
        imphash=sample.imphash,
        ssdeep=sample.ssdeep,
        tlsh=sample.tlsh,
        rich_header_hash=sample.rich_header_hash,
        vhash=sample.vhash,
        main_icon_dhash=sample.main_icon_dhash,
        imphash_source=sample.imphash_source.value if sample.imphash_source else None,
        ssdeep_source=sample.ssdeep_source.value if sample.ssdeep_source else None,
        tlsh_source=sample.tlsh_source.value if sample.tlsh_source else None,
        rich_header_hash_source=sample.rich_header_hash_source.value
        if sample.rich_header_hash_source
        else None,
        vhash_source=sample.vhash_source.value if sample.vhash_source else None,
        main_icon_dhash_source=sample.main_icon_dhash_source.value
        if sample.main_icon_dhash_source
        else None,
        created_at=sample.created_at,
    )


def _sample_from_row(row: SampleRow) -> Sample:
    return Sample(
        id=row.id,
        subject_id=row.subject_id,
        blob_id=row.blob_id,
        original_name=row.original_name,
        origin=row.origin,
        acquired_at=row.acquired_at,
        license_restriction=row.license_restriction,
        tlp=TLP(row.tlp),
        do_not_submit=row.do_not_submit,
        external_llm_allowed=row.external_llm_allowed,
        origin_kind=SampleOrigin(row.origin_kind),
        state=SampleState(row.state),
        source_service=row.source_service,
        source_object_id=row.source_object_id,
        expected_hash=row.expected_hash,
        validation_actor=row.validation_actor,
        validation_date=row.validation_date,
        validation_reason=row.validation_reason,
        imphash=row.imphash,
        ssdeep=row.ssdeep,
        tlsh=row.tlsh,
        rich_header_hash=row.rich_header_hash,
        vhash=row.vhash,
        main_icon_dhash=row.main_icon_dhash,
        imphash_source=SampleHashSource(row.imphash_source) if row.imphash_source else None,
        ssdeep_source=SampleHashSource(row.ssdeep_source) if row.ssdeep_source else None,
        tlsh_source=SampleHashSource(row.tlsh_source) if row.tlsh_source else None,
        rich_header_hash_source=SampleHashSource(row.rich_header_hash_source)
        if row.rich_header_hash_source
        else None,
        vhash_source=SampleHashSource(row.vhash_source) if row.vhash_source else None,
        main_icon_dhash_source=SampleHashSource(row.main_icon_dhash_source)
        if row.main_icon_dhash_source
        else None,
        created_at=row.created_at,
    )


def _provenance_from_row(row: ProvenanceEventRow) -> ProvenanceEvent:
    return ProvenanceEvent(
        id=row.id,
        subject_id=row.subject_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=row.payload,
        tlp=TLP(row.tlp),
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
    )
