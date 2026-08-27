"""Controlled acquisition of VirusTotal file bytes into a quarantined Sample.

Validation order, enforced strictly before any network call: capability ->
hash -> canonical object (the normalized hash itself) -> policy -> identity
-> idempotence -> budget.  Budget is consumed and the investigation's new
optimistic version is persisted and committed *before* the VirusTotal
download starts; once that call has started, consumed budget is never
refunded regardless of outcome (403, 404, timeout, truncated stream, wrong
hash, oversized download).

This module performs no upload, rescan, or submission, and defines no
endpoint or Dramatiq actor: it is a pure application service, wired by a
later batch.  `hit_review` is a fully valid `reason` here even though the
API only exposes it starting at batch 14.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.identity import IdentityProvider
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalPort,
    file_hash_family,
    normalize_file_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleOrigin, SampleState
from cti_app.domain.production import (
    LoopBudgetCategory,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
)
from cti_app.domain.virustotal import VirusTotalCapability

SAMPLES_QUARANTINE_BUCKET = "samples-quarantine"
# Small downloads never touch disk; large ones spool to a temp file instead
# of ever holding the whole payload in memory.
SAMPLE_ACQUISITION_SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024

_DISCLOSURE_ALLOWED_TLP = frozenset({TLP.CLEAR, TLP.GREEN})


class SampleAcquisitionError(RuntimeError):
    code = "sample_acquisition_error"


class SampleAcquisitionCapabilityDisabledError(SampleAcquisitionError):
    code = "sample_acquisition_capability_disabled"


class SampleAcquisitionPolicyDeniedError(SampleAcquisitionError):
    code = "sample_acquisition_policy_denied"


class SampleAcquisitionInvestigationNotFoundError(SampleAcquisitionError):
    code = "sample_acquisition_investigation_not_found"


@dataclass(frozen=True, slots=True)
class SampleAcquisitionPolicy:
    """The investigation's persisted disclosure policy, exactly as handed off.

    Distinct from the resulting Sample's own fields: a successfully
    acquired live sample is always locked down (``do_not_submit=True``)
    regardless of this policy, which only governs whether VirusTotal may be
    asked for the bytes at all.  ``external_llm_allowed`` is inherited onto
    the Sample but never itself authorizes a VirusTotal disclosure.
    """

    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool


@dataclass(frozen=True, slots=True)
class SampleAcquisitionResult:
    outcome: SampleAcquisitionOutcome
    sample_id: UUID | None
    reused_existing_sample: bool
    error_code: str | None = None


class VirusTotalSampleAcquisitionService:
    """Pulls exactly one file's bytes from VirusTotal into a quarantined Sample."""

    def __init__(
        self,
        port: VirusTotalPort,
        capabilities: VirusTotalCapabilities,
        catalog: BlobCatalogService,
        uow_factory: UnitOfWorkFactory,
        identity: IdentityProvider,
    ) -> None:
        self._port = port
        self._capabilities = capabilities
        self._catalog = catalog
        self._uow_factory = uow_factory
        self._identity = identity

    async def acquire(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        requested_hash: str,
        reason: SampleAcquisitionReason,
        policy: SampleAcquisitionPolicy,
    ) -> SampleAcquisitionResult:
        normalized: str | None = None
        hash_family: str | None = None
        budget_committed = False
        actor_id: str | None = None
        try:
            # 1. capability
            if not self._capabilities.is_enabled(VirusTotalCapability.FILE_DOWNLOAD):
                raise SampleAcquisitionCapabilityDisabledError(
                    "La capability VirusTotal file_download est désactivée."
                )
            # 2. hash
            normalized = normalize_file_hash(requested_hash)
            hash_family = file_hash_family(normalized)
            # 3. canonical object: the normalized hash *is* the canonical
            # seed/graine acquired — nothing further to derive here.
            # 4. policy
            if policy.do_not_submit or policy.tlp not in _DISCLOSURE_ALLOWED_TLP:
                raise SampleAcquisitionPolicyDeniedError(
                    "La politique de diffusion interdit l'acquisition VirusTotal."
                )
            # 5. identity
            identity = await self._identity.current()
            actor_id = identity.actor_id

            # 6. idempotence — a prior success never touches budget or network.
            async with self._uow_factory() as uow:
                existing = await uow.sample_acquisition_attempts.find_successful(
                    investigation_id, normalized
                )
            if existing is not None:
                return SampleAcquisitionResult(
                    outcome=SampleAcquisitionOutcome.SUCCESS,
                    sample_id=existing.sample_id,
                    reused_existing_sample=True,
                )

            # 7. budget — consumed and committed before any network call starts.
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(investigation_id)
                if investigation is None:
                    raise SampleAcquisitionInvestigationNotFoundError(
                        "L'investigation demandée est introuvable."
                    )
                investigation.consume_budget(LoopBudgetCategory.VT_READ_UNITS)
                await uow.analyst_investigations.save(investigation)
                if reason is SampleAcquisitionReason.HIT_REVIEW:
                    investigation.consume_budget(LoopBudgetCategory.HITS_ACQUIRED)
                    await uow.analyst_investigations.save(investigation)
                await uow.commit()
            budget_committed = True

            return await self._download_and_store(
                investigation_id=investigation_id,
                subject_id=subject_id,
                normalized_hash=normalized,
                hash_family=hash_family,
                reason=reason,
                policy=policy,
                actor_id=actor_id,
            )
        except (VirusTotalError, SampleAcquisitionError) as exc:
            error_code = getattr(exc, "code", "sample_acquisition_error")
            await self._record_failure(
                investigation_id=investigation_id,
                subject_id=subject_id,
                policy=policy,
                requested_hash=normalized,
                hash_family=hash_family,
                reason=reason,
                error_code=error_code,
                actor_id=actor_id,
                record_ledger=budget_committed,
            )
            raise

    async def _download_and_store(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        normalized_hash: str,
        hash_family: str,
        reason: SampleAcquisitionReason,
        policy: SampleAcquisitionPolicy,
        actor_id: str,
    ) -> SampleAcquisitionResult:
        spool = tempfile.SpooledTemporaryFile(
            max_size=SAMPLE_ACQUISITION_SPOOL_MAX_MEMORY_BYTES, mode="w+b"
        )
        try:
            # Hash verification against the requested family happens inside
            # the port implementation, while the flow is still streaming.
            await self._port.file_download(normalized_hash, sink=spool)
            spool.seek(0)
            blob = await self._catalog.ingest(
                spool,
                logical_bucket=SAMPLES_QUARANTINE_BUCKET,
                mime_type="application/octet-stream",
            )
        finally:
            spool.close()

        origin_kind = (
            SampleOrigin.VT_HUNT_HIT
            if reason is SampleAcquisitionReason.HIT_REVIEW
            else SampleOrigin.VT_SEED
        )
        state = (
            SampleState.REVIEW_CANDIDATE
            if reason is SampleAcquisitionReason.HIT_REVIEW
            else SampleState.QUARANTINED
        )

        async with self._uow_factory() as uow:
            # One blob never produces more than one Sample per subject: the
            # DB-level unique constraint on (subject_id, blob_id) backs this
            # lookup, not just this pre-check.
            existing_sample = await uow.samples.get_by_subject_and_blob(subject_id, blob.id)
            if existing_sample is not None:
                await uow.provenance.append(
                    ProvenanceEvent(
                        aggregate_type="sample",
                        aggregate_id=existing_sample.id,
                        subject_id=subject_id,
                        event_type="sample_hash_family_observed",
                        payload={
                            "source_service": "virustotal",
                            "hash_family": hash_family,
                            "reason": reason.value,
                            "investigation_id": str(investigation_id),
                        },
                        tlp=existing_sample.tlp,
                        actor_id=actor_id,
                    )
                )
                await uow.sample_acquisition_attempts.append(
                    SampleAcquisitionAttempt(
                        investigation_id=investigation_id,
                        requested_hash=normalized_hash,
                        hash_family=hash_family,
                        reason=reason,
                        outcome=SampleAcquisitionOutcome.SUCCESS,
                        sample_id=existing_sample.id,
                    )
                )
                await uow.commit()
                return SampleAcquisitionResult(
                    outcome=SampleAcquisitionOutcome.SUCCESS,
                    sample_id=existing_sample.id,
                    reused_existing_sample=True,
                )

            sample = Sample(
                subject_id=subject_id,
                blob_id=blob.id,
                original_name=blob.descriptor.sha256,
                origin="virustotal_file_download",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=policy.tlp,
                do_not_submit=True,
                external_llm_allowed=policy.external_llm_allowed,
                origin_kind=origin_kind,
                state=state,
                source_service="virustotal",
                source_object_id=normalized_hash,
                expected_hash=normalized_hash,
            )
            await uow.samples.add(sample)
            await uow.provenance.append(
                ProvenanceEvent(
                    aggregate_type="sample",
                    aggregate_id=sample.id,
                    subject_id=subject_id,
                    event_type="sample_acquired_from_virustotal",
                    payload={
                        "source_service": "virustotal",
                        "hash_family": hash_family,
                        "reason": reason.value,
                        "investigation_id": str(investigation_id),
                    },
                    tlp=policy.tlp,
                    actor_id=actor_id,
                )
            )
            await uow.sample_acquisition_attempts.append(
                SampleAcquisitionAttempt(
                    investigation_id=investigation_id,
                    requested_hash=normalized_hash,
                    hash_family=hash_family,
                    reason=reason,
                    outcome=SampleAcquisitionOutcome.SUCCESS,
                    sample_id=sample.id,
                )
            )
            await uow.commit()
            return SampleAcquisitionResult(
                outcome=SampleAcquisitionOutcome.SUCCESS,
                sample_id=sample.id,
                reused_existing_sample=False,
            )

    async def _record_failure(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        policy: SampleAcquisitionPolicy,
        requested_hash: str | None,
        hash_family: str | None,
        reason: SampleAcquisitionReason,
        error_code: str,
        actor_id: str | None,
        record_ledger: bool,
    ) -> None:
        """Audit any failure on the investigation — never on a Sample that never existed.

        The payload carries only the error code, hash family, and reason:
        never the requested hash's originating URL, never a signed URL,
        never a secret.
        """
        async with self._uow_factory() as uow:
            await uow.provenance.append(
                ProvenanceEvent(
                    aggregate_type="analyst_investigation",
                    aggregate_id=investigation_id,
                    subject_id=subject_id,
                    event_type="sample_acquisition_failed",
                    payload={
                        "source_service": "virustotal",
                        "reason": reason.value,
                        "hash_family": hash_family,
                        "error_code": error_code,
                    },
                    tlp=policy.tlp,
                    actor_id=actor_id,
                )
            )
            if record_ledger and requested_hash is not None and hash_family is not None:
                await uow.sample_acquisition_attempts.append(
                    SampleAcquisitionAttempt(
                        investigation_id=investigation_id,
                        requested_hash=requested_hash,
                        hash_family=hash_family,
                        reason=reason,
                        outcome=SampleAcquisitionOutcome.ERROR,
                        error_code=error_code[:64],
                    )
                )
            await uow.commit()
