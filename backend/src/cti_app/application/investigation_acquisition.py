"""Investigation-scoped sample discovery and VT acquisition under G2."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid5

from cti_app.application.article_novelty import (
    ArticleNoveltyService,
    NoveltyCandidate,
    NoveltyCandidateKind,
    NoveltyClassification,
)
from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.sample_acquisition import (
    SAMPLES_QUARANTINE_BUCKET,
    SampleAcquisitionBudgetExceededError,
    SampleAcquisitionCapabilityDisabledError,
    SampleAcquisitionDownloadError,
    SampleAcquisitionPolicy,
    SampleAcquisitionPolicyDeniedError,
    VirusTotalSampleAcquisitionService,
)
from cti_app.application.virustotal import (
    VirusTotalError,
    file_hash_family,
    normalize_file_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample
from cti_app.domain.external_effects import EvidenceReference, ExternalEffectAuthority
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    EvidenceKind,
    InvestigationEnvelope,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.production import SampleAcquisitionOutcome, SampleAcquisitionReason

_ASSOCIATION_NAMESPACE = UUID("a3bcb4cf-7403-43a6-ad9b-8da838b282b1")
_NETWORK_CAPTURE_QUARANTINE_BUCKET = "network-captures-quarantine"
_ACQUISITION_COST = {
    BudgetDimension.VT_UNITS: 1,
    BudgetDimension.DOWNLOADS: 1,
}
_HIT_ORIGINS = frozenset({SampleOrigin.VT_PIVOT, SampleOrigin.RELATIONSHIP, SampleOrigin.BEHAVIOR})
_SUGGESTED_ALTERNATIVES = ("vt.file_report", "vt.relationships", "vt.intelligence_search")


def sample_association_id(investigation_id: UUID, sample_sha256: str) -> UUID:
    """Return the stable association identity shared by all sample hit sources."""
    return uuid5(_ASSOCIATION_NAMESPACE, f"{investigation_id}:{_require_sha256(sample_sha256)}")


class HitAcquisitionError(RuntimeError):
    """Base for typed hit-acquisition refusals."""


class HitNotFoundError(HitAcquisitionError):
    pass


class HitEvidenceError(HitAcquisitionError):
    pass


@dataclass(frozen=True, slots=True)
class HitAcquisitionResult:
    status: ToolInvocationStatus
    association: SampleAssociation
    sample_id: UUID | None = None
    reused_existing_sample: bool = False
    observation_refs: tuple[UUID, ...] = ()
    novelty: NoveltyClassification | None = None
    request_permission: bool = False
    suggested_alternatives: tuple[str, ...] = ()
    message: str = ""
    budget_consumed: dict[BudgetDimension, int] | None = None
    error_code: str | None = None
    reason: str | None = None
    confirmed_non_execution: bool = False
    cached: bool = False
    exception_class: str | None = None


@dataclass(frozen=True, slots=True)
class QuarantinedSampleRef:
    investigation_id: UUID
    association_id: UUID
    sample_id: UUID
    blob_id: UUID
    sha256: str
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool


class HitAcquisitionService:
    """Keep hit discovery separate from acquisition and member qualification."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        acquisition: VirusTotalSampleAcquisitionService | None,
        recorder: EvidenceRecorder,
        novelty: ArticleNoveltyService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._acquisition = acquisition
        self._recorder = recorder
        self._novelty = novelty

    async def register_hit(
        self,
        investigation_id: UUID,
        sample_sha256: str,
        origin: SampleOrigin,
        evidence_refs: tuple[UUID, ...] = (),
    ) -> SampleAssociation:
        normalized = _require_sha256(sample_sha256)
        if not isinstance(origin, SampleOrigin) or origin not in _HIT_ORIGINS:
            raise TypeError("Hit origin must be vt_pivot, relationship or behavior")
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise HitNotFoundError("Investigation does not exist")
            await _verify_evidence_refs(uow, investigation_id, evidence_refs)
            associations = await uow.sample_associations.list_for_investigation(investigation_id)
            existing = next(
                (item for item in associations if item.sample_sha256 == normalized), None
            )
            if existing is not None:
                return existing
            association = SampleAssociation(
                id=sample_association_id(investigation_id, normalized),
                investigation_id=investigation_id,
                sample_sha256=normalized,
                origin=origin,
            )
            stored = await uow.sample_associations.append_version(association, expected_version=0)
            await uow.commit()
            return stored

    async def acquire_hit(
        self,
        association_id: UUID,
        *,
        purpose: str,
        evidence_refs: tuple[UUID, ...] = (),
        operation_key: str | None = None,
        envelope: InvestigationEnvelope | None = None,
        external_effect_authority: ExternalEffectAuthority | None = None,
        external_effect_evidence_refs: tuple[EvidenceReference, ...] = (),
    ) -> HitAcquisitionResult:
        if not purpose.strip():
            raise ValueError("Acquisition purpose must not be empty")
        async with self._uow_factory() as uow:
            association = await uow.sample_associations.get(association_id)
            if association is None:
                raise HitNotFoundError("Sample association does not exist")
            investigation = await uow.analyst_investigations.get(association.investigation_id)
            if investigation is None:
                raise HitNotFoundError("Investigation does not exist")
            await _verify_evidence_refs(uow, association.investigation_id, evidence_refs)
            current = await uow.investigation_envelopes.get(association.investigation_id)
            subject_samples = await uow.samples.list_for_subject(investigation.subject_id)
            quarantine_blob = await uow.blobs.get_by_address(
                SAMPLES_QUARANTINE_BUCKET, association.sample_sha256
            )
            quarantine_blob_present = (
                quarantine_blob is not None
                and quarantine_blob.descriptor.sha256 == association.sample_sha256
                and quarantine_blob.descriptor.logical_bucket == SAMPLES_QUARANTINE_BUCKET
            )
            prior = await uow.sample_acquisition_attempts.find_successful(
                association.investigation_id, association.sample_sha256
            )
            latest_error = await uow.sample_acquisition_attempts.find_latest_error(
                association.investigation_id, association.sample_sha256
            )
            prior_sample = None
            if prior is not None and prior.sample_id is not None:
                prior_sample = await uow.samples.get(prior.sample_id)
            subject_sample_candidates: list[Sample] = []
            for subject_sample in subject_samples:
                candidate_blob = await uow.blobs.get(subject_sample.blob_id)
                if subject_sample.expected_hash == association.sample_sha256 or (
                    candidate_blob is not None
                    and candidate_blob.descriptor.sha256 == association.sample_sha256
                ):
                    subject_sample_candidates.append(subject_sample)

        if current is None:
            return _needs_review(association, "No approved G2 envelope exists.")
        if envelope is not None and envelope.version != current.version:
            return _needs_review(
                association, "The approved G2 envelope changed during this tool call."
            )
        active_envelope = envelope or current
        if active_envelope.investigation_id != association.investigation_id:
            raise ValueError("Envelope and sample association belong to different investigations")

        if prior is not None:
            if prior.sample_id is None or prior_sample is None:
                return _needs_review(
                    association,
                    "A prior success ledger entry has no Sample; restore and reconcile its "
                    "quarantine record before retrying.",
                )
            compatibility_message = _subject_sample_policy_conflict(prior_sample, active_envelope)
            verified = (
                compatibility_message is None
                and self._acquisition is not None
                and await self._acquisition.verify_quarantined_sample_bytes(
                    sample_id=prior_sample.id,
                    subject_id=investigation.subject_id,
                    requested_sha256=association.sample_sha256,
                )
            )
            if compatibility_message is not None:
                return _needs_review(association, compatibility_message)
            if not verified:
                return _needs_review(
                    association,
                    "The prior quarantined Sample needs SHA-256 verification: restore the blob "
                    "if missing, then verify its bytes and Sample metadata before retrying.",
                )
            refs = await self._record_acquired_sample(
                association=association,
                sample_id=prior_sample.id,
                reused=True,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.SUCCEEDED,
                association=association,
                sample_id=prior_sample.id,
                reused_existing_sample=True,
                observation_refs=refs,
                novelty=await self._classify_with_evidence_refs(association, refs),
                message=(
                    "This investigation already acquired this hash; its verified quarantine "
                    "Sample was reused without another download."
                ),
                budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
            )

        if subject_sample_candidates:
            if len(subject_sample_candidates) != 1:
                return _needs_review(
                    association,
                    "Multiple same-subject Samples claim this hash; resolve the duplicate "
                    "quarantined records and verify which original source is authoritative.",
                )
            subject_sample = subject_sample_candidates[0]
            compatibility_message = _subject_sample_policy_conflict(subject_sample, active_envelope)
            if compatibility_message is not None:
                return _needs_review(association, compatibility_message)
            verified = (
                self._acquisition is not None
                and await self._acquisition.verify_quarantined_sample_bytes(
                    sample_id=subject_sample.id,
                    subject_id=investigation.subject_id,
                    requested_sha256=association.sample_sha256,
                )
            )
            if not verified:
                return _needs_review(
                    association,
                    "Same-subject quarantine candidate needs SHA-256 verification: restore the "
                    + "blob if missing, then verify its bytes and Sample metadata before retrying.",
                )
            refs = await self._record_acquired_sample(
                association=association,
                sample_id=subject_sample.id,
                reused=True,
                reused_from_subject_sample=True,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.SUCCEEDED,
                association=association,
                sample_id=subject_sample.id,
                reused_existing_sample=True,
                observation_refs=refs,
                novelty=await self._classify_with_evidence_refs(association, refs),
                message=(
                    "Reused the verified quarantine Sample already owned by this subject; "
                    "no VirusTotal download was made."
                ),
                budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
            )

        acquisition_tlp = _sample_tlp(active_envelope)
        if acquisition_tlp is None:
            return HitAcquisitionResult(
                status=ToolInvocationStatus.POLICY_DENIED,
                association=association,
                message="TLP bounds prohibit VirusTotal acquisition.",
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                budget_consumed={},
            )
        if (
            "vt" not in active_envelope.allowed_providers
            or not active_envelope.acquire_hits_allowed
        ):
            return _needs_review(
                association,
                "The approved G2 envelope does not authorize VirusTotal hit acquisition.",
            )
        if self._acquisition is None:
            return HitAcquisitionResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                association=association,
                message="VirusTotal file download capability is not configured.",
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                budget_consumed={},
            )

        cached_refusal = (
            self._acquisition.cached_refusal(latest_error.error_code, association.sample_sha256)
            if latest_error is not None
            else None
        )
        if cached_refusal is not None:
            return HitAcquisitionResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                association=association,
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                message="VirusTotal refused this hash on the current authorized route.",
                budget_consumed={},
                error_code=self._acquisition.refusal_error_code(cached_refusal),
                reason=self._acquisition.refusal_reason(cached_refusal),
                confirmed_non_execution=True,
                cached=True,
            )

        if quarantine_blob_present:
            return _needs_review(
                association,
                "Quarantined bytes exist without a linked Sample for this subject; verify "
                "subject ownership and reconcile their provenance before requesting acquisition.",
            )

        novelty = await self._classify_without_evidence(association)

        key = operation_key or f"vt.download_sample:{association.investigation_id}:{association.id}"
        budget = active_envelope.budget
        existing_reservation = next(
            (item for item in budget.reservations if item.operation_key == key), None
        )
        if existing_reservation is not None and existing_reservation.status.value == "committed":
            return _needs_review(
                association,
                "A previous attempt reserved and committed this operation; "
                "its outcome needs review.",
            )
        if envelope is None and existing_reservation is not None:
            return _needs_review(
                association,
                "A previous direct acquisition reserved this operation; its outcome needs review.",
            )
        try:
            budget.reserve(key, _ACQUISITION_COST)
        except BudgetExceeded as exc:
            return _needs_review(
                association,
                f"The approved {exc.dimension.value} budget is exhausted.",
            )
        managed_by_dispatcher = envelope is not None and existing_reservation is not None
        if envelope is None:
            try:
                active_envelope = await self._save_envelope_budget(
                    active_envelope,
                    expected_version=active_envelope.version,
                )
            except Exception:
                budget.release(key)
                raise

        try:
            result = await self._acquisition.acquire(
                investigation_id=association.investigation_id,
                subject_id=investigation.subject_id,
                requested_hash=association.sample_sha256,
                reason=SampleAcquisitionReason.HIT_REVIEW,
                association_id=association.id,
                operation_key=key,
                invocation_id=(
                    external_effect_authority.invocation_id
                    if external_effect_authority is not None
                    else None
                ),
                external_effect_authority=external_effect_authority,
                external_effect_evidence_refs=external_effect_evidence_refs,
                policy=SampleAcquisitionPolicy(
                    tlp=acquisition_tlp,
                    do_not_submit=active_envelope.disclosure.do_not_submit,
                    external_llm_allowed=active_envelope.disclosure.external_llm_allowed,
                ),
            )
        except SampleAcquisitionBudgetExceededError as exc:
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                association=association,
                novelty=novelty,
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                message=(
                    "Investigation-local acquisition budget is exhausted; no download was made."
                ),
                budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
                error_code=exc.code,
                reason="investigation_budget_exhausted",
                confirmed_non_execution=True,
                exception_class=type(exc).__name__,
            )
        except SampleAcquisitionDownloadError as exc:
            consumed = {
                BudgetDimension.VT_UNITS: int(exc.provider_called),
                BudgetDimension.DOWNLOADS: 0,
            }
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
                consumed_budget=consumed,
            )
            definitive = not exc.ambiguous
            return HitAcquisitionResult(
                status=(
                    ToolInvocationStatus.UNAVAILABLE
                    if definitive
                    else ToolInvocationStatus.NEEDS_REVIEW
                ),
                association=association,
                novelty=novelty,
                request_permission=False,
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                message=(
                    "VirusTotal file download is unavailable for this request."
                    if definitive
                    else "VirusTotal download may have delivered partial or unverifiable bytes; "
                    "operator reconciliation is required."
                ),
                budget_consumed=consumed if definitive else {},
                error_code=exc.code,
                reason=(
                    exc.refusal_reason or _download_failure_reason(exc.code)
                    if definitive
                    else "provider_download_outcome_ambiguous"
                ),
                confirmed_non_execution=definitive,
                exception_class=type(exc).__name__,
            )
        except SampleAcquisitionPolicyDeniedError as exc:
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.POLICY_DENIED,
                association=association,
                novelty=novelty,
                message=str(exc),
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
            )
        except SampleAcquisitionCapabilityDisabledError as exc:
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                association=association,
                novelty=novelty,
                message=str(exc),
                suggested_alternatives=_SUGGESTED_ALTERNATIVES,
                budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
            )
        except (VirusTotalError, RuntimeError) as exc:
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.FAILED,
                association=association,
                novelty=novelty,
                message=f"Sample acquisition failed ({type(exc).__name__}).",
                budget_consumed={BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 0},
            )

        if result.outcome is not SampleAcquisitionOutcome.SUCCESS or result.sample_id is None:
            active_envelope = await self._finish_operation_budget(
                active_envelope,
                key,
                managed_by_dispatcher,
                succeeded=False,
                persist=envelope is None,
            )
            return HitAcquisitionResult(
                status=ToolInvocationStatus.FAILED,
                association=association,
                novelty=novelty,
                message=f"Sample acquisition returned {result.outcome.value} without a sample.",
                budget_consumed={BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 0},
            )

        refs = await self._record_acquired_sample(
            association=association,
            sample_id=result.sample_id,
            reused=result.reused_existing_sample,
        )
        active_envelope = await self._finish_operation_budget(
            active_envelope,
            key,
            managed_by_dispatcher,
            succeeded=True,
            persist=envelope is None,
        )
        novelty_after_acquisition = await self._classify_with_evidence_refs(association, refs)
        return HitAcquisitionResult(
            status=ToolInvocationStatus.SUCCEEDED,
            association=association,
            sample_id=result.sample_id,
            reused_existing_sample=result.reused_existing_sample,
            observation_refs=refs,
            novelty=novelty_after_acquisition or novelty,
            message="The sample was acquired to quarantine and remains a candidate.",
            budget_consumed=_ACQUISITION_COST.copy(),
        )

    async def require_association(
        self, investigation_id: UUID, association_id: UUID
    ) -> SampleAssociation:
        async with self._uow_factory() as uow:
            association = await uow.sample_associations.get(association_id)
            if association is None or association.investigation_id != investigation_id:
                raise HitNotFoundError("Sample association does not belong to this investigation")
            return association

    async def validate_evidence_refs(
        self, investigation_id: UUID, evidence_refs: tuple[UUID, ...]
    ) -> None:
        async with self._uow_factory() as uow:
            await _verify_evidence_refs(uow, investigation_id, evidence_refs)

    async def resolve_quarantined_sample(self, association_id: UUID) -> QuarantinedSampleRef:
        async with self._uow_factory() as uow:
            association = await uow.sample_associations.get(association_id)
            if association is None:
                raise HitNotFoundError("Sample association does not exist")
            investigation = await uow.analyst_investigations.get(association.investigation_id)
            if investigation is None:
                raise HitNotFoundError("Investigation does not exist")
            imported_samples = await uow.samples.list_for_subject(investigation.subject_id)
            sample = next(
                (
                    item
                    for item in imported_samples
                    if item.expected_hash == association.sample_sha256
                    and item.origin == "operator_import"
                    and item.source_service == "operator_import"
                ),
                None,
            )
            if sample is None:
                attempt = await uow.sample_acquisition_attempts.find_successful(
                    association.investigation_id, association.sample_sha256
                )
                sample = (
                    await uow.samples.get(attempt.sample_id)
                    if attempt is not None and attempt.sample_id is not None
                    else None
                )
            blob = await uow.blobs.get(sample.blob_id) if sample is not None else None
            if blob is None or blob.descriptor.sha256 != association.sample_sha256:
                blob = await uow.blobs.get_by_address(
                    SAMPLES_QUARANTINE_BUCKET, association.sample_sha256
                )
                if blob is not None:
                    sample = await uow.samples.get_by_subject_and_blob(
                        investigation.subject_id, blob.id
                    )
            if sample is None:
                raise HitNotFoundError("No quarantined sample is linked to this investigation")
            if getattr(sample.state, "value", sample.state) not in {
                "quarantined",
                "review_candidate",
            }:
                raise HitAcquisitionError("Analysis is limited to quarantined samples")
            if (
                blob is None
                or blob.descriptor.sha256 != association.sample_sha256
                or blob.descriptor.logical_bucket
                not in {
                    SAMPLES_QUARANTINE_BUCKET,
                    *(
                        (_NETWORK_CAPTURE_QUARANTINE_BUCKET,)
                        if sample.origin == "operator_import"
                        and sample.source_service == "operator_import"
                        else ()
                    ),
                }
            ):
                raise HitAcquisitionError("Quarantine blob hash does not match the association")
            return QuarantinedSampleRef(
                investigation_id=association.investigation_id,
                association_id=association.id,
                sample_id=sample.id,
                blob_id=sample.blob_id,
                sha256=blob.descriptor.sha256,
                tlp=sample.tlp,
                do_not_submit=sample.do_not_submit,
                external_llm_allowed=sample.external_llm_allowed,
            )

    async def _classify_without_evidence(
        self, association: SampleAssociation
    ) -> NoveltyClassification | None:
        if self._novelty is None:
            return None
        return await self._novelty.classify(
            investigation_id=association.investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=association.investigation_id,
                kind=NoveltyCandidateKind.SAMPLE,
                value=association.sample_sha256,
                object_type="sample",
                object_id=association.sample_sha256,
            ),
        )

    async def _classify_with_evidence_refs(
        self, association: SampleAssociation, refs: tuple[UUID, ...]
    ) -> NoveltyClassification | None:
        if self._novelty is None:
            return None
        return await self._novelty.classify(
            investigation_id=association.investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=association.investigation_id,
                kind=NoveltyCandidateKind.SAMPLE,
                value=association.sample_sha256,
                object_type="sample",
                object_id=association.sample_sha256,
                observation_refs=refs,
            ),
        )

    async def _finish_operation_budget(
        self,
        envelope: InvestigationEnvelope,
        operation_key: str,
        managed_by_dispatcher: bool,
        *,
        succeeded: bool,
        persist: bool,
        consumed_budget: dict[BudgetDimension, int] | None = None,
    ) -> InvestigationEnvelope:
        _finish_budget(
            envelope.budget,
            operation_key,
            managed_by_dispatcher,
            succeeded=succeeded,
            consumed_budget=consumed_budget,
        )
        if persist and not managed_by_dispatcher:
            return await self._save_envelope_budget(
                envelope,
                expected_version=envelope.version,
            )
        return envelope

    async def _record_acquired_sample(
        self,
        *,
        association: SampleAssociation,
        sample_id: UUID,
        reused: bool,
        reused_from_subject_sample: bool = False,
    ) -> tuple[UUID, ...]:
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None:
                raise HitNotFoundError("Acquired sample does not exist")
            blob = await uow.blobs.get(sample.blob_id)
            if (
                blob is None
                or blob.descriptor.sha256 != association.sample_sha256
                or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
            ):
                raise HitAcquisitionError(
                    "Quarantined blob hash does not match the requested SHA-256"
                )
            await uow.sample_associations.link_sample(
                association.id,
                sample.id,
                expected_sha256=association.sample_sha256,
            )
            if reused_from_subject_sample:
                events = await uow.provenance.list_for_aggregate("sample", sample.id)
                already_recorded = any(
                    event.event_type == "reused_from_subject_sample"
                    and event.payload.get("investigation_id") == str(association.investigation_id)
                    and event.payload.get("association_id") == str(association.id)
                    for event in events
                )
                if not already_recorded:
                    await uow.provenance.append(
                        ProvenanceEvent(
                            aggregate_type="sample",
                            aggregate_id=sample.id,
                            subject_id=sample.subject_id,
                            event_type="reused_from_subject_sample",
                            payload={
                                "provenance": "reused_from_subject_sample",
                                "investigation_id": str(association.investigation_id),
                                "association_id": str(association.id),
                                "sample_id": str(sample.id),
                                "sample_origin": sample.origin,
                                "sample_origin_kind": sample.origin_kind.value,
                                "source_service": sample.source_service,
                                "source_object_id": sample.source_object_id,
                                "sha256": association.sample_sha256,
                                "new_download": False,
                            },
                            tlp=sample.tlp,
                        )
                    )
            await uow.commit()
        is_subject_reuse = reused_from_subject_sample
        observation = await self._recorder.record_existing_blob(
            investigation_id=association.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=sample.blob_id,
            sha256=blob.descriptor.sha256,
            source=(
                "investigation.subject_sample_reuse"
                if is_subject_reuse
                else "virustotal.sample_download"
            ),
            source_version=(
                "sample-subject-reuse.v1" if is_subject_reuse else "sample-acquisition.v1"
            ),
            object_type="sample",
            object_id=association.sample_sha256,
            limitations=(
                "The sample is quarantined and untrusted.",
                (
                    "The Sample was already quarantined for this subject; no VirusTotal "
                    "download occurred for this investigation."
                    if is_subject_reuse
                    else "Acquisition and static analysis do not validate cluster membership."
                ),
            ),
            idempotency_key=(
                f"sample-acquisition:{association.investigation_id}:"
                f"{association.sample_sha256}:"
                f"{'subject-reuse' if is_subject_reuse else 'reused' if reused else 'acquired'}"
            ),
        )
        return (observation.id,)

    async def _save_envelope_budget(
        self, envelope: InvestigationEnvelope, *, expected_version: int
    ) -> InvestigationEnvelope:
        updated = replace(envelope, budget=envelope.budget, version=envelope.version + 1)
        async with self._uow_factory() as uow:
            current = await uow.investigation_envelopes.get(envelope.investigation_id)
            if current is None or current.version != expected_version:
                raise RuntimeError("Investigation envelope version changed while reserving budget")
            saved = await uow.investigation_envelopes.save_if_version(
                updated, expected_version=expected_version
            )
            await uow.commit()
            return saved


async def _verify_evidence_refs(uow: Any, investigation_id: UUID, refs: tuple[UUID, ...]) -> None:
    for reference in dict.fromkeys(refs):
        observation = await uow.evidence_observations.get(reference)
        if observation is None or observation.investigation_id != investigation_id:
            raise HitEvidenceError("Hit evidence must exist in the same investigation")


def _require_sha256(value: str) -> str:
    normalized = normalize_file_hash(value)
    if file_hash_family(normalized) != "sha256":
        raise ValueError("Investigation sample associations require an explicit SHA-256")
    return normalized


def _sample_tlp(envelope: InvestigationEnvelope) -> TLP | None:
    level = envelope.disclosure.tlp
    if level in {TLPLevel.WHITE, TLPLevel.CLEAR}:
        return TLP.CLEAR
    if level is TLPLevel.GREEN:
        return TLP.GREEN
    return None


def _subject_sample_policy_conflict(sample: Sample, envelope: InvestigationEnvelope) -> str | None:
    target_level = envelope.disclosure.tlp
    if target_level is None:
        return (
            "The investigation has no approved TLP bound; set a disclosure classification "
            "before reuse."
        )
    target_tlp = {
        TLPLevel.WHITE: TLP.CLEAR,
        TLPLevel.CLEAR: TLP.CLEAR,
        TLPLevel.GREEN: TLP.GREEN,
        TLPLevel.AMBER: TLP.AMBER,
        TLPLevel.RED: TLP.RED,
    }[target_level]
    rank = {
        TLP.CLEAR: 0,
        TLP.GREEN: 1,
        TLP.AMBER: 2,
        TLP.AMBER_STRICT: 3,
        TLP.RED: 4,
    }
    if rank[sample.tlp] > rank[target_tlp]:
        return (
            f"The quarantined Sample is classified {sample.tlp.value}, above this investigation's "
            f"{target_level.value} disclosure bound; align the approved TLP before reuse."
        )
    if not envelope.disclosure.external_llm_allowed and sample.external_llm_allowed:
        return (
            "The Sample's source policy is incompatible: it allows external LLM access while "
            "this investigation "
            "prohibits it; reconcile the disclosure bounds before reuse."
        )
    return None


def _needs_review(association: SampleAssociation, message: str) -> HitAcquisitionResult:
    return HitAcquisitionResult(
        status=ToolInvocationStatus.NEEDS_REVIEW,
        association=association,
        request_permission=True,
        suggested_alternatives=_SUGGESTED_ALTERNATIVES,
        message=message,
        budget_consumed={},
    )


def _finish_budget(
    budget: Any,
    operation_key: str,
    managed_by_dispatcher: bool,
    *,
    succeeded: bool,
    consumed_budget: dict[BudgetDimension, int] | None = None,
) -> None:
    if managed_by_dispatcher:
        return
    consumed = (
        _ACQUISITION_COST
        if succeeded
        else consumed_budget or {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 0}
    )
    budget.commit(operation_key, consumed)


def _download_failure_reason(error_code: str) -> str:
    if error_code == "virustotal_download_route_not_found":
        return "provider_download_route_not_found"
    if error_code == "virustotal_download_authorization_refused":
        return "provider_download_authorization_refused"
    return "provider_download_failed_before_bytes"


__all__ = [
    "HitAcquisitionError",
    "HitAcquisitionResult",
    "HitAcquisitionService",
    "HitEvidenceError",
    "HitNotFoundError",
    "QuarantinedSampleRef",
]
