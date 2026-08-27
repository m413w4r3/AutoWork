from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.goodware import Banality, BanalityScorer, BanalityThresholds
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
    InvariantType,
    ReportClaimProvenance,
    ResolvedFeature,
    SampleFeatureProvenance,
    ToolOutputProvenance,
    canonical_pattern,
    likely_packed,
    m2_feature_kind,
    make_proposal_key,
)
from cti_app.domain.reference_corpus import (
    ReferenceCorpusAssessment,
    ReferenceCorpusVerdict,
    assess_reference_feature,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class InvariantProposalResult:
    invariant: CandidateInvariant | None
    rejection: InvariantRejection | None

    @property
    def accepted(self) -> bool:
        return self.invariant is not None


class InvariantRegistryService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        settings: Settings | None = None,
        banality_scorer: BanalityScorer | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._settings = settings or Settings()
        self._banality_scorer = banality_scorer or BanalityScorer(
            BanalityThresholds(
                suspicious_count=self._settings.goodware_suspicious_count,
                banal_count=self._settings.goodware_banal_count,
            )
        )

    async def propose(
        self,
        *,
        investigation_id: UUID,
        sample_ids: Sequence[UUID],
        type: InvariantType | str,
        category: InvariantCategory | str,
        pattern: str,
        provenances: Sequence[InvariantProvenance] | None = None,
        provenance: InvariantProvenance | None = None,
        cycle_number: int | None = None,
        positive_sample_confirmed: bool = False,
        occurred_at: datetime | None = None,
    ) -> InvariantProposalResult:
        try:
            invariant_type = InvariantType(type)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid invariant type") from exc
        if provenances is None:
            provenances = (provenance,) if provenance is not None else ()
        elif provenance is not None:
            raise ValueError("provide provenances or provenance, not both")
        try:
            provenance_items = tuple(provenances)
        except TypeError:
            provenance_items = ()
        canonical = canonical_pattern(pattern)
        try:
            proposal_key = make_proposal_key(
                investigation_id=investigation_id,
                invariant_type=invariant_type,
                pattern=canonical,
                provenances=provenance_items,
            )
        except ValueError:
            proposal_key = _invalid_provenance_key(
                investigation_id=investigation_id,
                invariant_type=invariant_type,
                pattern=canonical,
                provenances=provenance_items,
            )
        moment = occurred_at or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

        async with self._uow_factory() as uow:
            await uow.invariants.lock_proposal(proposal_key)
            existing_invariant, existing_rejection = await uow.invariants.get_proposal_outcome(
                proposal_key
            )
            if existing_invariant is not None:
                return InvariantProposalResult(invariant=existing_invariant, rejection=None)
            if existing_rejection is not None:
                return InvariantProposalResult(invariant=None, rejection=existing_rejection)

            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ValueError(f"Investigation {investigation_id} does not exist")
            effective_cycle = (
                cycle_number if cycle_number is not None else investigation.cycle_number
            )

            provenance_cause = _validate_provenances(provenance_items)
            if provenance_cause is not None:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.PROVENANCE_INVALID,
                    reason=provenance_cause,
                    occurred_at=moment,
                )

            try:
                invariant_category = InvariantCategory(category)
            except (TypeError, ValueError):
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=str(category)[:32],
                    pattern=canonical,
                    cause=InvariantRejectionCause.INVALID_CATEGORY,
                    reason="category is not a P09 category",
                    occurred_at=moment,
                )

            category_cause = _category_rejection_cause(invariant_category)
            if category_cause is not None:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=category_cause,
                    reason=f"category {invariant_category.value} is not selective",
                    occurred_at=moment,
                )

            if not canonical:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.EMPTY_PATTERN,
                    reason="pattern is empty",
                    occurred_at=moment,
                )
            if len(canonical) > self._settings.invariant_max_pattern_chars:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.PATTERN_TOO_LONG,
                    reason="pattern exceeds the configured P09 limit",
                    occurred_at=moment,
                )

            resolved_features: list[tuple[InvariantProvenance, ResolvedFeature]] = []
            for item in provenance_items:
                try:
                    resolved = await uow.invariants.resolve_provenance(
                        provenance=item,
                        invariant_type=invariant_type,
                        pattern=canonical,
                    )
                except (KeyError, TypeError, ValueError):
                    resolved = None
                if resolved is None:
                    return await self._reject(
                        uow=uow,
                        investigation_id=investigation_id,
                        cycle_number=effective_cycle,
                        proposal_key=proposal_key,
                        invariant_type=invariant_type,
                        category=invariant_category,
                        pattern=canonical,
                        cause=InvariantRejectionCause.PROVENANCE_INVALID,
                        reason="provenance does not resolve to a persisted M2 output",
                        occurred_at=moment,
                    )
                if resolved is not None:
                    resolved_features.append((item, resolved))

            descriptor = _resolved_descriptor(
                invariant_type, canonical, [resolved for _, resolved in resolved_features]
            )
            if descriptor is _INVALID_DESCRIPTOR:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.PROVENANCE_INVALID,
                    reason="resolved feature descriptor does not match the proposed type/pattern",
                    occurred_at=moment,
                )
            if not resolved_features and not provenance_items:
                descriptor = None
            ngram = next(
                (
                    feature.code_ngram
                    for item, feature in resolved_features
                    if _is_technical_provenance(item) and feature.code_ngram is not None
                ),
                None,
            )
            packing = next(
                (
                    feature.packing
                    for item, feature in resolved_features
                    if (
                        _is_technical_provenance(item)
                        and feature.code_ngram is not None
                        and feature.packing is not None
                    )
                ),
                None,
            )
            if invariant_type is InvariantType.CODE_NGRAM and (
                ngram is None or packing is None
            ):
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.PROVENANCE_INVALID,
                    reason=(
                        "code_ngram requires a resolvable persisted M2 origin with counters "
                        "and packing signals"
                    ),
                    occurred_at=moment,
                )
            measurements = (
                await uow.invariants.measure_feature(
                    feature_kind=descriptor[0],
                    normalized_value=descriptor[1],
                    snapshot_sample_ids=sample_ids,
                )
                if descriptor is not None
                else FeatureMeasurements()
            )
            baseline_id, occurrence_count = await self._goodware_measurement(
                uow, investigation_id, descriptor
            )
            measured_occurrence_count = (
                occurrence_count if occurrence_count is not None and occurrence_count > 0 else None
            )
            banality = self._banality_scorer.score(
                measured_occurrence_count
            )
            if banality is Banality.BANAL:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.BANAL,
                    reason="goodware baseline classifies the proposal as banal",
                    occurred_at=moment,
                )

            assessment = _reference_assessment(measurements, descriptor)
            if assessment.verdict is ReferenceCorpusVerdict.MULTI_FAMILY:
                return await self._reject(
                    uow=uow,
                    investigation_id=investigation_id,
                    cycle_number=effective_cycle,
                    proposal_key=proposal_key,
                    invariant_type=invariant_type,
                    category=invariant_category,
                    pattern=canonical,
                    cause=InvariantRejectionCause.MULTI_FAMILY,
                    reason="feature is present in multiple malware families",
                    occurred_at=moment,
                )

            if ngram is not None:
                ratio = ngram.masked_byte_count / ngram.byte_count
                if ratio > self._settings.code_ngram_max_mask_ratio:
                    return await self._reject(
                        uow=uow,
                        investigation_id=investigation_id,
                        cycle_number=effective_cycle,
                        proposal_key=proposal_key,
                        invariant_type=invariant_type,
                        category=invariant_category,
                        pattern=canonical,
                        cause=InvariantRejectionCause.CODE_NGRAM_MASK_RATIO,
                        reason="code ngram masked-byte ratio exceeds the configured limit",
                        occurred_at=moment,
                    )
                if ngram.longest_fixed_run < self._settings.code_ngram_min_contiguous_fixed_bytes:
                    return await self._reject(
                        uow=uow,
                        investigation_id=investigation_id,
                        cycle_number=effective_cycle,
                        proposal_key=proposal_key,
                        invariant_type=invariant_type,
                        category=invariant_category,
                        pattern=canonical,
                        cause=InvariantRejectionCause.CODE_NGRAM_CONTIGUOUS_FIXED_RUN,
                        reason="code ngram contiguous fixed run is below the configured limit",
                        occurred_at=moment,
                    )

            packed = likely_packed(
                packing,
                max_executable_section_entropy_gte=(
                    self._settings.likely_packed_max_executable_section_entropy_gte
                ),
                executable_bytes_per_function_gte=(
                    self._settings.likely_packed_executable_bytes_per_function_gte
                ),
                known_packer_marker_hit=self._settings.likely_packed_known_packer_marker_hit,
            )
            confirmed = bool(
                any(isinstance(item, ReportClaimProvenance) for item in provenance_items)
                and positive_sample_confirmed
                and measurements.positive_support is not None
                and measurements.positive_support > 0
            )
            candidate = CandidateInvariant(
                investigation_id=investigation_id,
                type=invariant_type,
                category=invariant_category,
                pattern=canonical,
                proposal_key=proposal_key,
                provenances=provenance_items,
                banality=banality,
                banality_occurrence_count=measured_occurrence_count,
                goodware_baseline_id=baseline_id,
                corpus_verdict=assessment.verdict,
                corpus_malware_sample_count=(
                    assessment.malware_sample_count if descriptor is not None else None
                ),
                family_labels=tuple(sorted(assessment.family_sample_counts)),
                benign_prevalence=measurements.benign_prevalence,
                positive_support=measurements.positive_support,
                positive_sample_confirmed=confirmed,
                masked_pattern=ngram.pattern if ngram is not None else None,
                byte_count=ngram.byte_count if ngram is not None else None,
                fixed_byte_count=ngram.fixed_byte_count if ngram is not None else None,
                masked_byte_count=ngram.masked_byte_count if ngram is not None else None,
                longest_fixed_run=ngram.longest_fixed_run if ngram is not None else None,
                likely_packed=packed,
                created_at=moment,
            )
            stored = await uow.invariants.add_invariant(candidate)
            await uow.commit()
            return InvariantProposalResult(invariant=stored, rejection=None)

    async def propose_manual(
        self,
        *,
        investigation_id: UUID,
        sample_ids: Sequence[UUID],
        type: InvariantType | str,
        category: InvariantCategory | str,
        motif: str,
        pattern: str,
        actor_id: str,
        occurred_at: datetime,
        positive_sample_confirmed: bool = False,
        cycle_number: int | None = None,
    ) -> InvariantProposalResult:
        provenance = AnalystManualProvenance(
            actor_id=actor_id,
            occurred_at=occurred_at,
            motif=motif,
        )
        return await self.propose(
            investigation_id=investigation_id,
            sample_ids=sample_ids,
            type=type,
            category=category,
            pattern=pattern,
            provenances=(provenance,),
            cycle_number=cycle_number,
            positive_sample_confirmed=positive_sample_confirmed,
            occurred_at=occurred_at,
        )

    async def transition(
        self,
        *,
        invariant_id: UUID,
        to_status: InvariantStatus,
        actor_id: str,
        occurred_at: datetime,
        reason: str,
    ) -> CandidateInvariant:
        async with self._uow_factory() as uow:
            result = await uow.invariants.transition(
                invariant_id=invariant_id,
                to_status=to_status,
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason=reason,
            )
            await uow.commit()
            return result

    async def rejection_statistics(
        self, *, investigation_id: UUID | None = None, cycle_number: int | None = None
    ) -> dict[str, int]:
        async with self._uow_factory() as uow:
            return dict(
                await uow.invariants.rejection_statistics(
                    investigation_id=investigation_id, cycle_number=cycle_number
                )
            )

    async def _goodware_measurement(
        self, uow: object, investigation_id: UUID, descriptor: tuple[str, str] | None
    ) -> tuple[UUID | None, int | None]:
        baseline_id = await uow.investigation_goodware_baselines.get(investigation_id)  # type: ignore[attr-defined]
        if baseline_id is None or descriptor is None:
            return baseline_id, None
        occurrence = await uow.goodware_baselines.get_feature_occurrence(  # type: ignore[attr-defined]
            baseline_id, descriptor[0], descriptor[1]
        )
        return baseline_id, occurrence

    async def _reject(
        self,
        *,
        uow: object,
        investigation_id: UUID,
        cycle_number: int | None,
        proposal_key: str,
        invariant_type: InvariantType,
        category: InvariantCategory | str,
        pattern: str,
        cause: InvariantRejectionCause,
        reason: str,
        occurred_at: datetime,
    ) -> InvariantProposalResult:
        rejection = InvariantRejection(
            investigation_id=investigation_id,
            cycle_number=cycle_number,
            cause=cause,
            type=invariant_type.value,
            category=_rejection_category_text(category),
            pattern=pattern,
            proposal_key=proposal_key,
            reason=reason,
            occurred_at=occurred_at,
        )
        stored = await uow.invariants.add_rejection(rejection)  # type: ignore[attr-defined]
        await uow.commit()  # type: ignore[attr-defined]
        return InvariantProposalResult(invariant=None, rejection=stored)


_PROVENANCE_TYPES = (
    SampleFeatureProvenance,
    CodeFeatureProvenance,
    ToolOutputProvenance,
    CapabilityProvenance,
    ReportClaimProvenance,
    AnalystManualProvenance,
)
_INVALID_DESCRIPTOR = object()


def _validate_provenances(provenances: Sequence[object]) -> str | None:
    if not provenances:
        return "at least one provenance is required"
    canonical_values: list[str] = []
    for provenance in provenances:
        if not isinstance(provenance, _PROVENANCE_TYPES):
            return "provenance is missing or is not a P09 provenance type"
        try:
            canonical_values.append(
                json.dumps(
                    provenance.as_canonical_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AttributeError, TypeError, ValueError):
            return "provenance is incomplete or invalid"
    if len(canonical_values) != len(set(canonical_values)):
        return "duplicate provenance members are not allowed"
    return None


def _is_technical_provenance(provenance: object) -> bool:
    return isinstance(
        provenance,
        (
            SampleFeatureProvenance,
            CodeFeatureProvenance,
            ToolOutputProvenance,
            CapabilityProvenance,
        ),
    )


def _invalid_provenance_key(
    *,
    investigation_id: UUID,
    invariant_type: InvariantType,
    pattern: str,
    provenances: Sequence[object],
) -> str:
    canonical_values = []
    for provenance in provenances:
        try:
            value = provenance.as_canonical_dict()  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            value = {
                "invalid_type": f"{type(provenance).__module__}.{type(provenance).__qualname__}"
            }
        canonical_values.append(value)
    canonical_values.sort(
        key=lambda value: json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    )
    payload = {
        "investigation_id": str(investigation_id),
        "type": invariant_type.value,
        "pattern": pattern,
        "provenances": canonical_values,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _rejection_category_text(category: InvariantCategory | str) -> str:
    value = category.value if isinstance(category, InvariantCategory) else str(category)
    return value.strip()[:32] or "invalid_category"


def _category_rejection_cause(
    category: InvariantCategory,
) -> InvariantRejectionCause | None:
    return {
        InvariantCategory.LIBRARY_NOISE: InvariantRejectionCause.LIBRARY_NOISE,
        InvariantCategory.PACKER_ARTIFACT: InvariantRejectionCause.PACKER_ARTIFACT,
        InvariantCategory.COMPILER_ARTIFACT: InvariantRejectionCause.COMPILER_ARTIFACT,
        InvariantCategory.GENERIC_WINAPI: InvariantRejectionCause.GENERIC_WINAPI,
    }.get(category)


def _resolved_descriptor(
    invariant_type: InvariantType,
    pattern: str,
    resolved_features: Sequence[ResolvedFeature],
) -> tuple[str, str] | object | None:
    expected = m2_feature_kind(invariant_type, pattern)
    descriptors: list[tuple[str, str]] = []
    for resolved in resolved_features:
        if (resolved.feature_kind is None) != (resolved.normalized_value is None):
            return _INVALID_DESCRIPTOR
        if resolved.feature_kind is not None and resolved.normalized_value is not None:
            descriptor = (resolved.feature_kind, resolved.normalized_value)
            if expected is None or descriptor != expected:
                return _INVALID_DESCRIPTOR
            descriptors.append(descriptor)
    if descriptors and any(descriptor != descriptors[0] for descriptor in descriptors[1:]):
        return _INVALID_DESCRIPTOR
    return descriptors[0] if descriptors else expected


def _reference_assessment(
    measurements: FeatureMeasurements, descriptor: tuple[str, str] | None
) -> ReferenceCorpusAssessment:
    if descriptor is None:
        return ReferenceCorpusAssessment(
            verdict=ReferenceCorpusVerdict.UNKNOWN,
            feature_kind="unknown",
            normalized_value="unknown",
            malware_sample_count=0,
            family_sample_counts={},
            benign_sample_occurrences=0,
        )
    family_counts: dict[str, set[UUID]] = {}
    for sample_id, family in measurements.reference_members:
        family_counts.setdefault(family, set()).add(sample_id)
    if len(family_counts) >= 2:
        return ReferenceCorpusAssessment(
            verdict=ReferenceCorpusVerdict.MULTI_FAMILY,
            feature_kind=descriptor[0],
            normalized_value=descriptor[1],
            malware_sample_count=sum(len(samples) for samples in family_counts.values()),
            family_sample_counts={
                family: len(samples) for family, samples in family_counts.items()
            },
            benign_sample_occurrences=measurements.benign_prevalence or 0,
        )
    if measurements.benign_prevalence is None:
        return ReferenceCorpusAssessment(
            verdict=ReferenceCorpusVerdict.UNKNOWN,
            feature_kind=descriptor[0],
            normalized_value=descriptor[1],
            malware_sample_count=sum(len(samples) for samples in family_counts.values()),
            family_sample_counts={
                family: len(samples) for family, samples in family_counts.items()
            },
            benign_sample_occurrences=0,
        )
    return assess_reference_feature(
        feature_kind=descriptor[0],
        normalized_value=descriptor[1],
        malware_members=measurements.reference_members,
        benign_sample_occurrences=measurements.benign_prevalence,
        total_eligible_samples_by_family=measurements.eligible_samples_by_family,
    )
