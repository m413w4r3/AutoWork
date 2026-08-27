from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.code_features import CodeNgram, GoodwareVerdict, PackingSignals
from cti_app.domain.invariants import (
    AnalystManualProvenance,
    CandidateInvariant,
    CapabilityProvenance,
    CodeFeatureProvenance,
    FeatureMeasurements,
    InvariantCategory,
    InvariantProvenance,
    InvariantRejection,
    InvariantRejectionCause,
    InvariantStatus,
    InvariantTransition,
    InvariantType,
    ReportClaimProvenance,
    ResolvedFeature,
    SampleFeatureProvenance,
    ToolOutputProvenance,
    canonical_provenance,
    m2_feature_kind,
)
from cti_app.infrastructure.database.models.collection import ClaimRow
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    CapabilitySetRow,
    CodeFeatureSetRow,
    ReferenceMemberDisputeRow,
    ReferenceMemberRow,
    SampleFeatureIndexRow,
    SampleFeatureSetRow,
    SampleRow,
)
from cti_app.infrastructure.database.models.invariants import (
    CandidateInvariantProvenanceRow,
    CandidateInvariantRow,
    CandidateInvariantTransitionRow,
    InvariantRejectionRow,
)


class SqlAlchemyInvariantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_proposal(self, proposal_key: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:proposal_key, 0))"),
            {"proposal_key": proposal_key},
        )

    async def get_proposal_outcome(
        self, proposal_key: str
    ) -> tuple[CandidateInvariant | None, InvariantRejection | None]:
        invariant = await self.get_invariant_by_proposal_key(proposal_key)
        rejection = await self.get_rejection_by_proposal_key(proposal_key)
        if invariant is not None and rejection is not None:
            raise RuntimeError("proposal key has both an invariant and a rejection")
        return invariant, rejection

    async def resolve_provenance(
        self,
        *,
        provenance: InvariantProvenance,
        invariant_type: InvariantType,
        pattern: str,
    ) -> ResolvedFeature | None:
        if isinstance(provenance, SampleFeatureProvenance):
            return await self._resolve_sample_feature(provenance, invariant_type, pattern)
        if isinstance(provenance, CodeFeatureProvenance):
            return await self._resolve_code_feature(provenance, invariant_type, pattern)
        if isinstance(provenance, CapabilityProvenance):
            return await self._resolve_capability(provenance, invariant_type, pattern)
        if isinstance(provenance, ToolOutputProvenance):
            return await self._resolve_tool_output(provenance, invariant_type, pattern)
        if isinstance(provenance, ReportClaimProvenance):
            return await self._resolve_report_claim(provenance, invariant_type, pattern)
        if isinstance(provenance, AnalystManualProvenance):
            descriptor = m2_feature_kind(invariant_type, pattern)
            return ResolvedFeature(
                source_id=f"manual:{provenance.actor_id}:{provenance.occurred_at.isoformat()}",
                sample_sha256=None,
                feature_kind=descriptor[0] if descriptor else None,
                normalized_value=descriptor[1] if descriptor else None,
            )
        return None

    async def measure_feature(
        self,
        *,
        feature_kind: str,
        normalized_value: str,
        snapshot_sample_ids: Sequence[UUID],
    ) -> FeatureMeasurements:
        sample_ids = tuple(dict.fromkeys(snapshot_sample_ids))
        eligible = await self._eligible_samples_by_family()
        if feature_kind in {
            "imphash",
            "ssdeep",
            "tlsh",
            "rich_header_hash",
            "vhash",
            "main_icon_dhash",
        }:
            members, benign, support = await self._measure_sample_hash(
                feature_kind, normalized_value, sample_ids
            )
        elif feature_kind in {
            "string",
            "import",
            "export",
            "section",
            "opcode_fragment16",
            "code_ngram",
            "capability",
        }:
            members, benign, support = await self._measure_indexed_feature(
                feature_kind, normalized_value, sample_ids
            )
        else:
            return FeatureMeasurements(eligible_samples_by_family=eligible)
        return FeatureMeasurements(
            reference_members=members,
            eligible_samples_by_family=eligible,
            benign_prevalence=benign,
            positive_support=support,
        )

    async def add_invariant(self, invariant: CandidateInvariant) -> CandidateInvariant:
        statement = (
            insert(CandidateInvariantRow)
            .values(**_candidate_values(invariant))
            .on_conflict_do_nothing(constraint="uq_candidate_invariants_proposal_key")
            .returning(CandidateInvariantRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        await self._session.flush()
        if inserted_id is not None:
            self._session.add_all(
                [
                    CandidateInvariantProvenanceRow(
                        id=uuid4(),
                        invariant_id=invariant.id,
                        kind=provenance.kind,
                        sample_sha256=_provenance_sample_sha256(provenance),
                        payload=canonical_provenance(provenance),
                        created_at=invariant.created_at,
                    )
                    for provenance in invariant.provenances
                ]
            )
            await self._session.flush()
        row = await self._session.scalar(
            select(CandidateInvariantRow).where(
                CandidateInvariantRow.proposal_key == invariant.proposal_key
            )
        )
        if row is None:
            raise RuntimeError("invariant insert conflict without row")
        return await self._candidate_from_row(row)

    async def get_invariant(self, invariant_id: UUID) -> CandidateInvariant | None:
        row = await self._session.get(CandidateInvariantRow, invariant_id)
        return await self._candidate_from_row(row) if row is not None else None

    async def get_invariant_by_proposal_key(
        self, proposal_key: str
    ) -> CandidateInvariant | None:
        row = await self._session.scalar(
            select(CandidateInvariantRow).where(
                CandidateInvariantRow.proposal_key == proposal_key
            )
        )
        return await self._candidate_from_row(row) if row is not None else None

    async def get_rejection_by_proposal_key(
        self, proposal_key: str
    ) -> InvariantRejection | None:
        row = await self._session.scalar(
            select(InvariantRejectionRow).where(
                InvariantRejectionRow.proposal_key == proposal_key
            )
        )
        return _rejection_from_row(row) if row is not None else None

    async def list_invariants(
        self,
        *,
        investigation_id: UUID | None = None,
        status: InvariantStatus | None = None,
        invariant_type: InvariantType | None = None,
        category: InvariantCategory | None = None,
    ) -> Sequence[CandidateInvariant]:
        statement = select(CandidateInvariantRow)
        if investigation_id is not None:
            statement = statement.where(CandidateInvariantRow.investigation_id == investigation_id)
        if status is not None:
            statement = statement.where(CandidateInvariantRow.status == status.value)
        if invariant_type is not None:
            statement = statement.where(CandidateInvariantRow.type == invariant_type.value)
        if category is not None:
            statement = statement.where(CandidateInvariantRow.category == category.value)
        rows = await self._session.scalars(
            statement.order_by(CandidateInvariantRow.created_at, CandidateInvariantRow.id)
        )
        return [await self._candidate_from_row(row) for row in rows]

    async def add_rejection(self, rejection: InvariantRejection) -> InvariantRejection:
        statement = (
            insert(InvariantRejectionRow)
            .values(**_rejection_values(rejection))
            .on_conflict_do_nothing(constraint="uq_invariant_rejections_proposal_key")
            .returning(InvariantRejectionRow.id)
        )
        await self._session.scalar(statement)
        await self._session.flush()
        row = await self._session.scalar(
            select(InvariantRejectionRow).where(
                InvariantRejectionRow.proposal_key == rejection.proposal_key
            )
        )
        if row is None:
            raise RuntimeError("rejection insert conflict without row")
        return _rejection_from_row(row)

    async def transition(
        self,
        *,
        invariant_id: UUID,
        to_status: InvariantStatus,
        actor_id: str,
        occurred_at: datetime,
        reason: str,
    ) -> CandidateInvariant:
        row = await self._session.scalar(
            select(CandidateInvariantRow)
            .where(CandidateInvariantRow.id == invariant_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError(f"Invariant {invariant_id} does not exist")
        transition = InvariantTransition(
            invariant_id=invariant_id,
            from_status=InvariantStatus(row.status),
            to_status=to_status,
            actor_id=actor_id,
            occurred_at=occurred_at,
            reason=reason,
        )
        row.status = transition.to_status.value
        self._session.add(
            CandidateInvariantTransitionRow(
                id=transition.id,
                invariant_id=transition.invariant_id,
                from_status=transition.from_status.value,
                to_status=transition.to_status.value,
                actor_id=transition.actor_id,
                occurred_at=transition.occurred_at,
                reason=transition.reason,
            )
        )
        await self._session.flush()
        return await self._candidate_from_row(row)

    async def list_transitions(self, invariant_id: UUID) -> Sequence[InvariantTransition]:
        rows = await self._session.scalars(
            select(CandidateInvariantTransitionRow)
            .where(CandidateInvariantTransitionRow.invariant_id == invariant_id)
            .order_by(
                CandidateInvariantTransitionRow.occurred_at,
                CandidateInvariantTransitionRow.id,
            )
        )
        return [_transition_from_row(row) for row in rows]

    async def list_rejections(
        self,
        *,
        investigation_id: UUID | None = None,
        cycle_number: int | None = None,
        cause: InvariantRejectionCause | None = None,
    ) -> Sequence[InvariantRejection]:
        statement = select(InvariantRejectionRow)
        if investigation_id is not None:
            statement = statement.where(InvariantRejectionRow.investigation_id == investigation_id)
        if cycle_number is not None:
            statement = statement.where(InvariantRejectionRow.cycle_number == cycle_number)
        if cause is not None:
            statement = statement.where(InvariantRejectionRow.cause == cause.value)
        rows = await self._session.scalars(
            statement.order_by(InvariantRejectionRow.occurred_at, InvariantRejectionRow.id)
        )
        return [_rejection_from_row(row) for row in rows]

    async def rejection_statistics(
        self, *, investigation_id: UUID | None = None, cycle_number: int | None = None
    ) -> Mapping[str, int]:
        statement = select(InvariantRejectionRow.cause, func.count()).group_by(
            InvariantRejectionRow.cause
        )
        if investigation_id is not None:
            statement = statement.where(InvariantRejectionRow.investigation_id == investigation_id)
        if cycle_number is not None:
            statement = statement.where(InvariantRejectionRow.cycle_number == cycle_number)
        result = await self._session.execute(statement)
        return {cause: int(count) for cause, count in result.all()}

    async def _resolve_sample_feature(
        self, provenance: SampleFeatureProvenance, invariant_type: InvariantType, pattern: str
    ) -> ResolvedFeature | None:
        sample_id = await self._sample_id_for_sha(provenance.sample_sha256)
        feature_id = _uuid_or_none(provenance.feature_id)
        if sample_id is None or feature_id is None:
            return None
        row = await self._session.scalar(
            select(SampleFeatureIndexRow).where(
                SampleFeatureIndexRow.id == feature_id,
                SampleFeatureIndexRow.sample_id == sample_id,
            )
        )
        if row is None:
            row = await self._session.scalar(
                select(SampleFeatureIndexRow).where(
                    SampleFeatureIndexRow.feature_set_id == feature_id,
                    SampleFeatureIndexRow.sample_id == sample_id,
                )
            )
        if row is None or row.feature_set_id is None:
            return None
        descriptor = m2_feature_kind(invariant_type, pattern)
        if descriptor is None or (row.feature_kind, row.normalized_value) != descriptor:
            return None
        feature_set = await self._session.get(SampleFeatureSetRow, row.feature_set_id)
        if feature_set is None or not _sample_offsets_match(
            feature_set.payload, row.feature_kind, row.normalized_value, provenance.offsets
        ):
            return None
        return ResolvedFeature(
            source_id=str(row.id),
            sample_sha256=provenance.sample_sha256,
            feature_kind=row.feature_kind,
            normalized_value=row.normalized_value,
        )

    async def _resolve_code_feature(
        self, provenance: CodeFeatureProvenance, invariant_type: InvariantType, pattern: str
    ) -> ResolvedFeature | None:
        if invariant_type is not InvariantType.CODE_NGRAM:
            return None
        sample_id = await self._sample_id_for_sha(provenance.sample_sha256)
        address = _address_or_none(provenance.function_address)
        if sample_id is None or address is None:
            return None
        rows = await self._session.scalars(
            select(CodeFeatureSetRow).where(
                CodeFeatureSetRow.sample_id == sample_id,
                CodeFeatureSetRow.tool_version == provenance.disassembler_version,
                CodeFeatureSetRow.status == "SUCCEEDED",
            )
        )
        wanted = pattern.strip().lower()
        for row in rows:
            for item in row.payload.get("ngrams", []):
                if (
                    str(item.get("pattern", "")).strip().lower() == wanted
                    and int(item.get("function_offset", -1)) == address
                    and int(item.get("start_offset", -1)) == provenance.offset
                ):
                    return ResolvedFeature(
                        source_id=str(row.id),
                        sample_sha256=provenance.sample_sha256,
                        feature_kind="code_ngram",
                        normalized_value=wanted,
                        code_ngram=_code_ngram_from_payload(item),
                        packing=_packing_from_payload(row.payload.get("packing")),
                    )
        return None

    async def _resolve_capability(
        self, provenance: CapabilityProvenance, invariant_type: InvariantType, pattern: str
    ) -> ResolvedFeature | None:
        if invariant_type is not InvariantType.CAPABILITY:
            return None
        sample_id = await self._sample_id_for_sha(provenance.sample_sha256)
        if sample_id is None:
            return None
        rows = await self._session.scalars(
            select(CapabilitySetRow).where(
                CapabilitySetRow.sample_id == sample_id,
                CapabilitySetRow.status == "SUCCEEDED",
            )
        )
        wanted = pattern.strip().lower()
        for row in rows:
            for item in row.capabilities:
                if (
                    str(item.get("rule_id", "")).lower() == provenance.capability_id.lower()
                    and str(item.get("rule_id", "")).lower() == wanted
                    and set(item.get("function_addresses", [])) == set(provenance.addresses)
                ):
                    return ResolvedFeature(
                        source_id=str(row.id),
                        sample_sha256=provenance.sample_sha256,
                        feature_kind="capability",
                        normalized_value=wanted,
                    )
        return None

    async def _resolve_tool_output(
        self, provenance: ToolOutputProvenance, invariant_type: InvariantType, pattern: str
    ) -> ResolvedFeature | None:
        source_id = _uuid_or_none(provenance.internal_id)
        sample_id = await self._sample_id_for_sha(provenance.sample_sha256)
        if source_id is None or sample_id is None:
            return None
        if invariant_type is InvariantType.CODE_NGRAM:
            row = await self._session.scalar(
                select(CodeFeatureSetRow).where(
                    CodeFeatureSetRow.id == source_id,
                    CodeFeatureSetRow.sample_id == sample_id,
                    CodeFeatureSetRow.tool_version == provenance.version,
                    CodeFeatureSetRow.status == "SUCCEEDED",
                )
            )
            if row is None:
                return None
            wanted = pattern.strip().lower()
            for item in row.payload.get("ngrams", []):
                if str(item.get("pattern", "")).strip().lower() == wanted:
                    return ResolvedFeature(
                        source_id=str(row.id),
                        sample_sha256=provenance.sample_sha256,
                        feature_kind="code_ngram",
                        normalized_value=wanted,
                        code_ngram=_code_ngram_from_payload(item),
                        packing=_packing_from_payload(row.payload.get("packing")),
                    )
        if invariant_type is InvariantType.CAPABILITY:
            row = await self._session.scalar(
                select(CapabilitySetRow).where(
                    CapabilitySetRow.id == source_id,
                    CapabilitySetRow.sample_id == sample_id,
                    CapabilitySetRow.tool_name == provenance.tool,
                    CapabilitySetRow.tool_version == provenance.version,
                    CapabilitySetRow.status == "SUCCEEDED",
                )
            )
            if row is None:
                return None
            wanted = pattern.strip().lower()
            if any(str(item.get("rule_id", "")).lower() == wanted for item in row.capabilities):
                return ResolvedFeature(
                    source_id=str(row.id),
                    sample_sha256=provenance.sample_sha256,
                    feature_kind="capability",
                    normalized_value=wanted,
                )
        descriptor = m2_feature_kind(invariant_type, pattern)
        if descriptor is None:
            return None
        row = await self._session.scalar(
            select(SampleFeatureSetRow).where(
                SampleFeatureSetRow.id == source_id,
                SampleFeatureSetRow.sample_id == sample_id,
                SampleFeatureSetRow.extractor_version == provenance.version,
            )
        )
        if row is None:
            return None
        index = await self._session.scalar(
            select(SampleFeatureIndexRow).where(
                SampleFeatureIndexRow.feature_set_id == row.id,
                SampleFeatureIndexRow.feature_kind == descriptor[0],
                SampleFeatureIndexRow.normalized_value == descriptor[1],
            )
        )
        if index is None:
            return None
        return ResolvedFeature(
            source_id=str(index.id),
            sample_sha256=provenance.sample_sha256,
            feature_kind=index.feature_kind,
            normalized_value=index.normalized_value,
        )

    async def _resolve_report_claim(
        self, provenance: ReportClaimProvenance, invariant_type: InvariantType, pattern: str
    ) -> ResolvedFeature | None:
        claim_id = _uuid_or_none(provenance.claim_id)
        source_document = _uuid_or_none(provenance.source_document)
        if claim_id is None or source_document is None:
            return None
        row = await self._session.scalar(
            select(ClaimRow).where(
                ClaimRow.id == claim_id,
                ClaimRow.source_document_id == source_document,
            )
        )
        if row is None:
            return None
        descriptor = m2_feature_kind(invariant_type, pattern)
        return ResolvedFeature(
            source_id=str(row.id),
            sample_sha256=None,
            feature_kind=descriptor[0] if descriptor else None,
            normalized_value=descriptor[1] if descriptor else None,
        )

    async def _sample_id_for_sha(self, sample_sha256: str) -> UUID | None:
        return await self._session.scalar(
            select(SampleRow.id)
            .join(BlobRow, BlobRow.id == SampleRow.blob_id)
            .where(BlobRow.sha256 == sample_sha256)
        )

    async def _eligible_samples_by_family(self) -> Mapping[str, int]:
        dispute = select(ReferenceMemberDisputeRow.member_id).where(
            ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id
        )
        result = await self._session.execute(
            select(
                ReferenceMemberRow.family_label,
                func.count(func.distinct(ReferenceMemberRow.sample_id)),
            )
            .where(
                ReferenceMemberRow.family_label.not_in(("benign", "unlabeled")),
                ~dispute.exists(),
            )
            .group_by(ReferenceMemberRow.family_label)
        )
        return {family: int(count) for family, count in result.all()}

    async def _measure_indexed_feature(
        self, feature_kind: str, normalized_value: str, sample_ids: Sequence[UUID]
    ) -> tuple[tuple[tuple[UUID, str], ...], int, int]:
        members_result = await self._session.execute(
            select(ReferenceMemberRow.sample_id, ReferenceMemberRow.family_label)
            .join(
                SampleFeatureIndexRow,
                SampleFeatureIndexRow.sample_id == ReferenceMemberRow.sample_id,
            )
            .where(
                SampleFeatureIndexRow.feature_kind == feature_kind,
                SampleFeatureIndexRow.normalized_value == normalized_value.lower(),
                ReferenceMemberRow.family_label.not_in(("benign", "unlabeled")),
                ~select(ReferenceMemberDisputeRow.member_id)
                .where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
                .exists(),
            )
            .distinct()
        )
        members = tuple(members_result.all())
        benign = await self._session.scalar(
            select(func.count(func.distinct(ReferenceMemberRow.sample_id)))
            .select_from(ReferenceMemberRow)
            .join(
                SampleFeatureIndexRow,
                SampleFeatureIndexRow.sample_id == ReferenceMemberRow.sample_id,
            )
            .where(
                SampleFeatureIndexRow.feature_kind == feature_kind,
                SampleFeatureIndexRow.normalized_value == normalized_value.lower(),
                ReferenceMemberRow.family_label == "benign",
                ~select(ReferenceMemberDisputeRow.member_id)
                .where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
                .exists(),
            )
        )
        support = await self._session.scalar(
            select(func.count(func.distinct(SampleFeatureIndexRow.sample_id))).where(
                SampleFeatureIndexRow.feature_kind == feature_kind,
                SampleFeatureIndexRow.normalized_value == normalized_value.lower(),
                SampleFeatureIndexRow.sample_id.in_(tuple(sample_ids)),
            )
        ) if sample_ids else 0
        return members, int(benign or 0), int(support or 0)

    async def _measure_sample_hash(
        self, feature_kind: str, normalized_value: str, sample_ids: Sequence[UUID]
    ) -> tuple[tuple[tuple[UUID, str], ...], int, int]:
        column = getattr(SampleRow, feature_kind)
        members_result = await self._session.execute(
            select(ReferenceMemberRow.sample_id, ReferenceMemberRow.family_label)
            .join(SampleRow, SampleRow.id == ReferenceMemberRow.sample_id)
            .where(
                column == normalized_value,
                ReferenceMemberRow.family_label.not_in(("benign", "unlabeled")),
                ~select(ReferenceMemberDisputeRow.member_id)
                .where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
                .exists(),
            )
            .distinct()
        )
        members = tuple(members_result.all())
        benign = await self._session.scalar(
            select(func.count(func.distinct(ReferenceMemberRow.sample_id)))
            .select_from(ReferenceMemberRow)
            .join(SampleRow, SampleRow.id == ReferenceMemberRow.sample_id)
            .where(
                column == normalized_value,
                ReferenceMemberRow.family_label == "benign",
                ~select(ReferenceMemberDisputeRow.member_id)
                .where(ReferenceMemberDisputeRow.member_id == ReferenceMemberRow.id)
                .exists(),
            )
        )
        support = await self._session.scalar(
            select(func.count(func.distinct(SampleRow.id))).where(
                column == normalized_value, SampleRow.id.in_(tuple(sample_ids))
            )
        ) if sample_ids else 0
        return members, int(benign or 0), int(support or 0)

    async def _candidate_from_row(
        self, row: CandidateInvariantRow
    ) -> CandidateInvariant:
        provenance_rows = await self._session.scalars(
            select(CandidateInvariantProvenanceRow)
            .where(CandidateInvariantProvenanceRow.invariant_id == row.id)
            .order_by(
                CandidateInvariantProvenanceRow.created_at,
                CandidateInvariantProvenanceRow.id,
            )
        )
        return CandidateInvariant(
            id=row.id,
            investigation_id=row.investigation_id,
            type=InvariantType(row.type),
            category=InvariantCategory(row.category),
            pattern=row.pattern,
            proposal_key=row.proposal_key,
            provenances=tuple(_provenance_from_payload(item.payload) for item in provenance_rows),
            status=InvariantStatus(row.status),
            banality=row.banality_verdict,
            banality_occurrence_count=row.banality_occurrence_count,
            goodware_baseline_id=row.goodware_baseline_id,
            corpus_verdict=row.corpus_verdict,
            corpus_malware_sample_count=row.corpus_malware_sample_count,
            family_labels=tuple(row.family_labels),
            benign_prevalence=row.benign_prevalence,
            positive_support=row.positive_support,
            positive_sample_confirmed=row.positive_sample_confirmed,
            masked_pattern=row.masked_pattern,
            byte_count=row.byte_count,
            fixed_byte_count=row.fixed_byte_count,
            masked_byte_count=row.masked_byte_count,
            longest_fixed_run=row.longest_fixed_run,
            likely_packed=row.likely_packed,
            created_at=row.created_at,
        )


def _candidate_values(invariant: CandidateInvariant) -> dict[str, object]:
    return {
        "id": invariant.id,
        "investigation_id": invariant.investigation_id,
        "type": invariant.type.value,
        "category": invariant.category.value,
        "pattern": invariant.pattern,
        "proposal_key": invariant.proposal_key,
        "status": invariant.status.value,
        "banality_verdict": invariant.banality.value,
        "banality_occurrence_count": invariant.banality_occurrence_count,
        "goodware_baseline_id": invariant.goodware_baseline_id,
        "corpus_verdict": invariant.corpus_verdict.value,
        "corpus_malware_sample_count": invariant.corpus_malware_sample_count,
        "family_labels": list(invariant.family_labels),
        "benign_prevalence": invariant.benign_prevalence,
        "positive_support": invariant.positive_support,
        "positive_sample_confirmed": invariant.positive_sample_confirmed,
        "masked_pattern": invariant.masked_pattern,
        "byte_count": invariant.byte_count,
        "fixed_byte_count": invariant.fixed_byte_count,
        "masked_byte_count": invariant.masked_byte_count,
        "longest_fixed_run": invariant.longest_fixed_run,
        "likely_packed": invariant.likely_packed,
        "created_at": invariant.created_at,
    }


def _rejection_values(rejection: InvariantRejection) -> dict[str, object]:
    return {
        "id": rejection.id,
        "investigation_id": rejection.investigation_id,
        "cycle_number": rejection.cycle_number,
        "cause": rejection.cause.value,
        "type": rejection.type,
        "category": rejection.category,
        "pattern": rejection.pattern,
        "proposal_key": rejection.proposal_key,
        "reason": rejection.reason,
        "occurred_at": rejection.occurred_at,
    }


def _rejection_from_row(row: InvariantRejectionRow) -> InvariantRejection:
    return InvariantRejection(
        id=row.id,
        investigation_id=row.investigation_id,
        cycle_number=row.cycle_number,
        cause=InvariantRejectionCause(row.cause),
        type=row.type,
        category=row.category,
        pattern=row.pattern,
        proposal_key=row.proposal_key,
        reason=row.reason,
        occurred_at=row.occurred_at,
    )


def _transition_from_row(row: CandidateInvariantTransitionRow) -> InvariantTransition:
    return InvariantTransition(
        id=row.id,
        invariant_id=row.invariant_id,
        from_status=InvariantStatus(row.from_status),
        to_status=InvariantStatus(row.to_status),
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        reason=row.reason,
    )


def _provenance_sample_sha256(provenance: InvariantProvenance) -> str | None:
    return getattr(provenance, "sample_sha256", None)


def _uuid_or_none(value: str | UUID) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (ValueError, TypeError):
        return None


def _address_or_none(value: int | str) -> int | None:
    if isinstance(value, int):
        return value if value >= 0 else None
    try:
        return int(value, 0)
    except (ValueError, TypeError):
        return None


def _code_ngram_from_payload(item: Mapping[str, Any]) -> CodeNgram:
    return CodeNgram(
        **{
            **dict(item),
            "mnemonics": tuple(item["mnemonics"]),
            "goodware_verdict": GoodwareVerdict(item["goodware_verdict"]),
            "corpus_family_sample_counts": tuple(
                (str(pair[0]), int(pair[1])) for pair in item["corpus_family_sample_counts"]
            ),
        }
    )


def _packing_from_payload(value: Any) -> PackingSignals | None:
    if not isinstance(value, dict):
        return None
    return PackingSignals(
        max_executable_section_entropy=value.get("max_executable_section_entropy"),
        executable_bytes=int(value["executable_bytes"]),
        recovered_function_count=int(value["recovered_function_count"]),
        executable_bytes_per_function=value.get("executable_bytes_per_function"),
        known_packer_marker_hits=tuple(value.get("known_packer_marker_hits", ())),
    )


def _sample_offsets_match(
    payload: Mapping[str, Any],
    feature_kind: str,
    normalized_value: str,
    offsets: Sequence[int],
) -> bool:
    keys = {
        "string": "strings",
        "import": "imports",
        "export": "exports",
        "section": "sections",
        "opcode_fragment16": "opcode_fragment16",
    }
    values = payload.get(keys.get(feature_kind, ""), ())
    if not isinstance(values, (list, tuple)):
        return False
    for item in values:
        if isinstance(item, dict):
            raw_value = item.get("value", item.get("name"))
            raw_offsets = item.get("offsets", item.get("offset"))
        else:
            raw_value, raw_offsets = item, None
        if str(raw_value).lower() != normalized_value:
            continue
        if raw_offsets is None:
            return True
        if isinstance(raw_offsets, int):
            raw_offsets = (raw_offsets,)
        if isinstance(raw_offsets, (list, tuple)):
            return set(int(offset) for offset in raw_offsets) == set(offsets)
    return False


def _provenance_from_payload(payload: Mapping[str, Any]) -> InvariantProvenance:
    kind = payload["kind"]
    if kind == "sample_feature":
        return SampleFeatureProvenance(
            sample_sha256=payload["sample_sha256"],
            feature_id=payload["feature_id"],
            offsets=tuple(payload["offsets"]),
        )
    if kind == "code_feature":
        return CodeFeatureProvenance(
            sample_sha256=payload["sample_sha256"],
            function_address=payload["function_address"],
            offset=payload["offset"],
            disassembler_version=payload["disassembler_version"],
        )
    if kind == "tool_output":
        return ToolOutputProvenance(
            sample_sha256=payload["sample_sha256"],
            tool=payload["tool"],
            version=payload["version"],
            internal_id=payload["internal_id"],
        )
    if kind == "capability":
        return CapabilityProvenance(
            sample_sha256=payload["sample_sha256"],
            capability_id=payload["capability_id"],
            addresses=tuple(payload["addresses"]),
        )
    if kind == "report_claim":
        return ReportClaimProvenance(
            claim_id=payload["claim_id"], source_document=payload["source_document"]
        )
    if kind == "analyst_manual":
        return AnalystManualProvenance(
            actor_id=payload["actor_id"],
            occurred_at=datetime.fromisoformat(payload["occurred_at"]),
            motif=payload["motif"],
        )
    raise ValueError("invalid invariant provenance kind")
