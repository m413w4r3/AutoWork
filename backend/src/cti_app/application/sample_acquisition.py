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

import hashlib
import json
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, BinaryIO, Protocol, cast
from uuid import UUID

from cti_app.application.blob_storage import BlobReadLimitExceededError, BlobStorageUnavailableError
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.identity import IdentityProvider
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalDownloadHashMismatchError,
    VirusTotalError,
    VirusTotalExternalEffectNeedsReviewError,
    VirusTotalExternalEffectRefusedError,
    VirusTotalHttpError,
    VirusTotalPort,
    file_hash_family,
    normalize_file_hash,
    virustotal_external_effect_scope,
)
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleOrigin, SampleState
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.external_effects import EvidenceReference, ExternalEffectAuthority
from cti_app.domain.production import (
    LoopBudgetCategory,
    SampleAcquisitionAttempt,
    SampleAcquisitionDispatchAttempt,
    SampleAcquisitionDispatchState,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
)
from cti_app.domain.virustotal import VirusTotalCapability

SAMPLES_QUARANTINE_BUCKET = "samples-quarantine"
# Small downloads never touch disk; large ones spool to a temp file instead
# of ever holding the whole payload in memory.
SAMPLE_ACQUISITION_SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024

_DISCLOSURE_ALLOWED_TLP = frozenset({TLP.CLEAR, TLP.GREEN})
_REFUSAL_CACHE_PREFIX = "rc1"


class SampleAcquisitionResponseOrigin(StrEnum):
    """Trusted response boundary supplied by the download adapter."""

    PROVIDER = "provider"
    PROXY = "proxy"
    SIGNED_CDN = "signed_cdn"
    UNKNOWN = "unknown"


class SampleAcquisitionFailureKind(StrEnum):
    """Normalized download failure type; transient outcomes are never cached."""

    PROVIDER_FILE_NOT_FOUND = "provider_file_not_found"
    AUTHORIZATION_REFUSED = "authorization_refused"
    PROXY_ROUTE_NOT_FOUND = "proxy_route_not_found"
    PROXY_AUTHORIZATION_REFUSED = "proxy_authorization_refused"
    RESPONSE_ORIGIN_UNKNOWN = "response_origin_unknown"
    RATE_LIMITED = "rate_limited"
    TIMED_OUT = "timed_out"
    FAILED_BEFORE_BYTES = "failed_before_bytes"
    INTERRUPTED_AFTER_BYTES = "interrupted_after_bytes"
    HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True, slots=True)
class SampleAcquisitionCacheScope:
    """Non-secret adapter identity for one download route/configuration.

    The adapter must provide stable, non-secret account/capability and
    configuration fingerprints, a normalized route, and its transport. Raw
    credentials, signed URLs, and query strings are not valid scope values.
    """

    provider: str
    account_capability_fingerprint: str
    route_fingerprint: str
    transport: str
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("account_capability_fingerprint", self.account_capability_fingerprint),
            ("route_fingerprint", self.route_fingerprint),
            ("transport", self.transport),
            ("configuration_fingerprint", self.configuration_fingerprint),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded fingerprint")
        if "?" in self.route_fingerprint:
            raise ValueError("route_fingerprint must not contain query parameters")


class SampleAcquisitionCacheScopePort(Protocol):
    """Adapter contract consumed structurally by the acquisition service."""

    @property
    def sample_acquisition_cache_scope(self) -> SampleAcquisitionCacheScope: ...


class SampleAcquisitionError(RuntimeError):
    code = "sample_acquisition_error"


class SampleAcquisitionCapabilityDisabledError(SampleAcquisitionError):
    code = "sample_acquisition_capability_disabled"


class SampleAcquisitionPolicyDeniedError(SampleAcquisitionError):
    code = "sample_acquisition_policy_denied"


class SampleAcquisitionInvestigationNotFoundError(SampleAcquisitionError):
    code = "sample_acquisition_investigation_not_found"


class SampleAcquisitionInconsistentLedgerError(SampleAcquisitionError):
    code = "sample_acquisition_success_without_quarantined_bytes"


class SampleAcquisitionDownloadError(SampleAcquisitionError):
    """A download failure classified by whether any bytes may have arrived."""

    def __init__(
        self,
        *,
        code: str,
        ambiguous: bool,
        provider_called: bool,
        refusal_reason: str | None = None,
        failure_kind: SampleAcquisitionFailureKind | None = None,
    ) -> None:
        self.code = code[:64]
        self.ambiguous = ambiguous
        self.provider_called = provider_called
        self.refusal_reason = refusal_reason
        self.failure_kind = failure_kind
        message = (
            "VirusTotal download outcome needs reconciliation."
            if ambiguous
            else "VirusTotal download failed before receiving sample bytes."
        )
        super().__init__(message)


class SampleAcquisitionBudgetExceededError(SampleAcquisitionError):
    """A deterministic local budget refusal before any provider request."""

    code = "investigation_loop_budget_exceeded"


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
        cache_scope_provider: SampleAcquisitionCacheScopePort | None = None,
    ) -> None:
        self._port = port
        self._capabilities = capabilities
        self._catalog = catalog
        self._uow_factory = uow_factory
        self._identity = identity
        self._cache_scope_provider = cache_scope_provider

    def _cache_scope(self) -> SampleAcquisitionCacheScope | None:
        adapter = self._cache_scope_provider or self._port
        return _sample_acquisition_cache_scope(adapter)

    def cached_refusal(
        self, error_code: str | None, requested_hash: str
    ) -> SampleAcquisitionFailureKind | None:
        """Return only a durable refusal matching this adapter's current scope."""
        if error_code is None:
            return None
        scope = self._cache_scope()
        if scope is None:
            return None
        normalized = normalize_file_hash(requested_hash)
        for kind in _CACHEABLE_FAILURE_KINDS:
            if error_code == _refusal_cache_code(scope, normalized, kind):
                return kind
        return None

    @staticmethod
    def refusal_error_code(kind: SampleAcquisitionFailureKind) -> str:
        return _refusal_error_code(kind)

    @staticmethod
    def refusal_reason(kind: SampleAcquisitionFailureKind) -> str:
        return _refusal_reason(kind)

    async def verify_quarantined_sample_bytes(
        self,
        *,
        sample_id: UUID,
        subject_id: UUID,
        requested_sha256: str,
    ) -> bool:
        """Verify a same-subject quarantine Sample against its actual stored bytes."""
        try:
            normalized = normalize_file_hash(requested_sha256)
        except ValueError:
            return False
        if file_hash_family(normalized) != "sha256":
            return False
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None or sample.subject_id != subject_id:
                return False
            blob = await uow.blobs.get(sample.blob_id)
            if (
                blob is None
                or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                or blob.descriptor.sha256 != normalized
                or sample.expected_hash not in {None, normalized}
                or sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
            ):
                return False
            descriptor = blob.descriptor
        try:
            content = await self._catalog.read(sample.blob_id, max_bytes=descriptor.size)
        except (
            BlobReadLimitExceededError,
            BlobStorageUnavailableError,
            EntityNotFoundError,
            OSError,
            ValueError,
        ):
            return False
        return len(content) == descriptor.size and hashlib.sha256(content).hexdigest() == normalized

    async def acquire(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        requested_hash: str,
        reason: SampleAcquisitionReason,
        policy: SampleAcquisitionPolicy,
        association_id: UUID | None = None,
        operation_key: str | None = None,
        invocation_id: UUID | None = None,
        external_effect_authority: ExternalEffectAuthority | None = None,
        external_effect_evidence_refs: tuple[EvidenceReference, ...] = (),
    ) -> SampleAcquisitionResult:
        normalized: str | None = None
        hash_family: str | None = None
        budget_committed = False
        actor_id: str | None = None
        dispatch_attempt_id: UUID | None = None
        dispatch_authority: ExternalEffectAuthority | None = None
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
                    sample = (
                        await uow.samples.get(existing.sample_id)
                        if existing.sample_id is not None
                        else None
                    )
                    blob = await uow.blobs.get(sample.blob_id) if sample is not None else None
                    if (
                        sample is None
                        or blob is None
                        or (
                            blob.descriptor.sha256 != normalized
                            if hash_family == "sha256"
                            else sample.expected_hash != normalized
                        )
                        or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                        or sample.state
                        not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                    ):
                        raise SampleAcquisitionInconsistentLedgerError(
                            "A prior success has no matching quarantined bytes."
                        )
                if existing is not None:
                    return SampleAcquisitionResult(
                        outcome=SampleAcquisitionOutcome.SUCCESS,
                        sample_id=existing.sample_id,
                        reused_existing_sample=True,
                    )

            # 7. Persist the pre-send identity with the investigation-local budget.
            # Replays of a PREPARED identity do not consume that budget twice.
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(investigation_id)
                if investigation is None:
                    raise SampleAcquisitionInvestigationNotFoundError(
                        "L'investigation demandée est introuvable."
                    )
                created_dispatch = True
                if external_effect_authority is not None:
                    if (
                        association_id is None
                        or operation_key is None
                        or invocation_id is None
                        or external_effect_authority.investigation_id != investigation_id
                        or external_effect_authority.invocation_id != invocation_id
                        or external_effect_authority.operation_key != operation_key
                        or external_effect_authority.acquisition_attempt_id is not None
                        or external_effect_authority.acquisition_dispatch_attempt_id is not None
                    ):
                        raise SampleAcquisitionError(
                            "External download authority is missing its dispatch binding."
                        )
                    (
                        dispatch,
                        created_dispatch,
                    ) = await uow.sample_acquisition_attempts.prepare_dispatch(
                        SampleAcquisitionDispatchAttempt(
                            investigation_id=investigation_id,
                            association_id=association_id,
                            invocation_id=invocation_id,
                            operation_key=operation_key,
                            requested_hash=normalized,
                            hash_family=hash_family,
                            reason=reason,
                        )
                    )
                    dispatch_attempt_id = dispatch.id
                    if dispatch.state is not SampleAcquisitionDispatchState.PREPARED:
                        target_code = (
                            "virustotal_download_replay_needs_review"
                            if dispatch.state
                            in {
                                SampleAcquisitionDispatchState.SUBMISSION_IN_PROGRESS,
                                SampleAcquisitionDispatchState.NEEDS_REVIEW,
                            }
                            else "virustotal_download_already_completed"
                        )
                        raise SampleAcquisitionDownloadError(
                            code=target_code,
                            ambiguous=dispatch.state
                            in {
                                SampleAcquisitionDispatchState.SUBMISSION_IN_PROGRESS,
                                SampleAcquisitionDispatchState.NEEDS_REVIEW,
                            },
                            provider_called=False,
                        )
                    dispatch_authority = replace(
                        external_effect_authority,
                        acquisition_dispatch_attempt_id=dispatch.id,
                    )
                if created_dispatch:
                    try:
                        investigation.consume_budget(LoopBudgetCategory.VT_READ_UNITS)
                        await uow.analyst_investigations.save(investigation)
                        if reason is SampleAcquisitionReason.HIT_REVIEW:
                            investigation.consume_budget(LoopBudgetCategory.HITS_ACQUIRED)
                            await uow.analyst_investigations.save(investigation)
                    except ValueError as exc:
                        raise SampleAcquisitionBudgetExceededError(
                            "Investigation-local sample acquisition budget is exhausted."
                        ) from exc
                await uow.commit()
            budget_committed = True
            if dispatch_authority is None:
                return await self._download_and_store(
                    investigation_id=investigation_id,
                    subject_id=subject_id,
                    normalized_hash=normalized,
                    hash_family=hash_family,
                    reason=reason,
                    policy=policy,
                    actor_id=actor_id,
                )
            with virustotal_external_effect_scope(
                dispatch_authority, external_effect_evidence_refs
            ):
                return await self._download_and_store(
                    investigation_id=investigation_id,
                    subject_id=subject_id,
                    normalized_hash=normalized,
                    hash_family=hash_family,
                    reason=reason,
                    policy=policy,
                    actor_id=actor_id,
                    dispatch_attempt_id=dispatch_attempt_id,
                )
        except SampleAcquisitionBudgetExceededError:
            # No provider call was made; do not record this as a provider failure.
            raise
        except (VirusTotalError, SampleAcquisitionError) as exc:
            error_code = getattr(exc, "code", "sample_acquisition_error")
            cache_error_code = None
            if (
                isinstance(exc, SampleAcquisitionDownloadError)
                and normalized is not None
                and exc.failure_kind is not None
                and exc.failure_kind in _CACHEABLE_FAILURE_KINDS
            ):
                scope = self._cache_scope()
                if scope is not None:
                    cache_error_code = _refusal_cache_code(scope, normalized, exc.failure_kind)
            await self._record_failure(
                investigation_id=investigation_id,
                subject_id=subject_id,
                policy=policy,
                requested_hash=normalized,
                hash_family=hash_family,
                reason=reason,
                error_code=error_code,
                cache_error_code=cache_error_code,
                actor_id=actor_id,
                record_ledger=budget_committed,
                dispatch_attempt_id=dispatch_attempt_id,
                operation_key=operation_key,
                dispatch_target=(
                    SampleAcquisitionDispatchState.NEEDS_REVIEW
                    if isinstance(exc, SampleAcquisitionDownloadError) and exc.ambiguous
                    else (
                        SampleAcquisitionDispatchState.DEFINITIVELY_NOT_SUBMITTED
                        if isinstance(exc, SampleAcquisitionDownloadError)
                        and not exc.provider_called
                        else SampleAcquisitionDispatchState.COMPLETED
                    )
                ),
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
        dispatch_attempt_id: UUID | None = None,
    ) -> SampleAcquisitionResult:
        spool = cast(
            BinaryIO,
            tempfile.SpooledTemporaryFile(
                max_size=SAMPLE_ACQUISITION_SPOOL_MAX_MEMORY_BYTES, mode="w+b"
            ),
        )
        try:
            # Hash verification against the requested family happens inside
            # the port implementation, while the flow is still streaming.
            try:
                await self._port.file_download(normalized_hash, sink=spool)
            except Exception as exc:
                bytes_written = max(0, spool.tell())
                preflight_codes = {
                    "virustotal_download_host_not_allowed",
                    "virustotal_unexpected_redirect",
                    "virustotal_download_too_large",
                    "virustotal_capability_disabled",
                    "virustotal_route_unavailable",
                    "virustotal_transport_not_configured",
                    "virustotal_download_query_not_allowlisted",
                }
                if isinstance(exc, VirusTotalExternalEffectNeedsReviewError):
                    code = exc.code
                    ambiguous = True
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.FAILED_BEFORE_BYTES
                elif isinstance(exc, VirusTotalExternalEffectRefusedError):
                    code = exc.code
                    ambiguous = False
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.FAILED_BEFORE_BYTES
                elif isinstance(exc, VirusTotalDownloadHashMismatchError):
                    code = exc.code
                    ambiguous = True
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.HASH_MISMATCH
                elif bytes_written:
                    code = "virustotal_download_interrupted_after_bytes"
                    ambiguous = True
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.INTERRUPTED_AFTER_BYTES
                elif isinstance(exc, VirusTotalHttpError) and exc.status_code == 404:
                    response_origin = _response_origin(exc)
                    if response_origin is SampleAcquisitionResponseOrigin.PROVIDER:
                        code = "virustotal_download_file_not_found"
                        refusal_reason = "provider_file_not_found"
                        failure_kind = SampleAcquisitionFailureKind.PROVIDER_FILE_NOT_FOUND
                    elif response_origin is SampleAcquisitionResponseOrigin.PROXY:
                        code = "virustotal_download_route_not_found"
                        refusal_reason = "proxy_route_not_found"
                        failure_kind = SampleAcquisitionFailureKind.PROXY_ROUTE_NOT_FOUND
                    else:
                        code = "virustotal_download_404_origin_unknown"
                        refusal_reason = "provider_download_404_origin_unknown"
                        failure_kind = SampleAcquisitionFailureKind.RESPONSE_ORIGIN_UNKNOWN
                    ambiguous = False
                elif isinstance(exc, VirusTotalHttpError) and exc.status_code in {401, 403}:
                    response_origin = _response_origin(exc)
                    if response_origin is SampleAcquisitionResponseOrigin.PROVIDER:
                        code = "virustotal_download_authorization_refused"
                        refusal_reason = "provider_download_authorization_refused"
                        failure_kind = SampleAcquisitionFailureKind.AUTHORIZATION_REFUSED
                    elif response_origin is SampleAcquisitionResponseOrigin.PROXY:
                        code = "virustotal_download_proxy_authorization_refused"
                        refusal_reason = "proxy_authorization_refused"
                        failure_kind = SampleAcquisitionFailureKind.PROXY_AUTHORIZATION_REFUSED
                    else:
                        code = "virustotal_download_authorization_origin_unknown"
                        refusal_reason = "provider_download_authorization_origin_unknown"
                        failure_kind = SampleAcquisitionFailureKind.RESPONSE_ORIGIN_UNKNOWN
                    ambiguous = False
                elif isinstance(exc, VirusTotalHttpError) and exc.status_code == 429:
                    code = "virustotal_download_rate_limited"
                    ambiguous = False
                    refusal_reason = "provider_download_rate_limited"
                    failure_kind = SampleAcquisitionFailureKind.RATE_LIMITED
                elif isinstance(exc, TimeoutError) or type(exc).__name__.lower().endswith(
                    "timeout"
                ):
                    code = "virustotal_download_timeout"
                    ambiguous = False
                    refusal_reason = "provider_download_timeout"
                    failure_kind = SampleAcquisitionFailureKind.TIMED_OUT
                elif isinstance(exc, VirusTotalError) and exc.code in preflight_codes:
                    code = exc.code
                    ambiguous = False
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.FAILED_BEFORE_BYTES
                else:
                    code = "virustotal_download_failed_before_bytes"
                    ambiguous = False
                    refusal_reason = None
                    failure_kind = SampleAcquisitionFailureKind.FAILED_BEFORE_BYTES
                provider_called = not (
                    isinstance(exc, VirusTotalError)
                    and exc.status_code is None
                    and (
                        exc.code in preflight_codes
                        or isinstance(exc, VirusTotalExternalEffectRefusedError)
                    )
                )
                raise SampleAcquisitionDownloadError(
                    code=code,
                    ambiguous=ambiguous,
                    provider_called=provider_called,
                    refusal_reason=refusal_reason,
                    failure_kind=failure_kind,
                ) from exc
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
                        dispatch_attempt_id=dispatch_attempt_id,
                    )
                )
                await _complete_dispatch(uow, dispatch_attempt_id)
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
                    dispatch_attempt_id=dispatch_attempt_id,
                )
            )
            await _complete_dispatch(uow, dispatch_attempt_id)
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
        cache_error_code: str | None,
        actor_id: str | None,
        record_ledger: bool,
        dispatch_attempt_id: UUID | None = None,
        operation_key: str | None = None,
        dispatch_target: SampleAcquisitionDispatchState | None = None,
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
                        error_code=(cache_error_code or error_code)[:64],
                        dispatch_attempt_id=dispatch_attempt_id,
                    )
                )
            if dispatch_attempt_id is not None and dispatch_target is not None:
                current_dispatch = (
                    await uow.sample_acquisition_attempts.get_dispatch(
                        investigation_id, operation_key
                    )
                    if operation_key is not None
                    else None
                )
                if (
                    dispatch_target is SampleAcquisitionDispatchState.DEFINITIVELY_NOT_SUBMITTED
                    and current_dispatch is not None
                    and current_dispatch.state
                    is SampleAcquisitionDispatchState.SUBMISSION_IN_PROGRESS
                ):
                    dispatch_target = SampleAcquisitionDispatchState.COMPLETED
                await _finish_dispatch(uow, dispatch_attempt_id, dispatch_target)
            await uow.commit()


async def _complete_dispatch(uow: Any, dispatch_attempt_id: UUID | None) -> None:
    if dispatch_attempt_id is None:
        return
    changed = await uow.sample_acquisition_attempts.transition_dispatch(
        dispatch_attempt_id,
        expected=SampleAcquisitionDispatchState.SUBMISSION_IN_PROGRESS,
        target=SampleAcquisitionDispatchState.COMPLETED,
    )
    if not changed:
        raise SampleAcquisitionError("Acquisition dispatch was not in a sendable state.")


async def _finish_dispatch(
    uow: Any,
    dispatch_attempt_id: UUID,
    target: SampleAcquisitionDispatchState,
) -> None:
    for expected in (
        SampleAcquisitionDispatchState.SUBMISSION_IN_PROGRESS,
        SampleAcquisitionDispatchState.PREPARED,
        target,
    ):
        if await uow.sample_acquisition_attempts.transition_dispatch(
            dispatch_attempt_id, expected=expected, target=target
        ):
            return


_CACHEABLE_FAILURE_KINDS = frozenset(
    {
        SampleAcquisitionFailureKind.PROVIDER_FILE_NOT_FOUND,
        SampleAcquisitionFailureKind.AUTHORIZATION_REFUSED,
    }
)
_CACHE_KIND_MARKERS = {
    SampleAcquisitionFailureKind.PROVIDER_FILE_NOT_FOUND: "n",
    SampleAcquisitionFailureKind.AUTHORIZATION_REFUSED: "a",
}


def _sample_acquisition_cache_scope(
    port: object,
) -> SampleAcquisitionCacheScope | None:
    adapter = cast(SampleAcquisitionCacheScopePort, port)
    scope = getattr(adapter, "sample_acquisition_cache_scope", None)
    return scope if isinstance(scope, SampleAcquisitionCacheScope) else None


def _response_origin(error: Exception) -> SampleAcquisitionResponseOrigin:
    value = getattr(error, "response_origin", None)
    if not isinstance(value, str):
        return SampleAcquisitionResponseOrigin.UNKNOWN
    try:
        return SampleAcquisitionResponseOrigin(value)
    except (TypeError, ValueError):
        return SampleAcquisitionResponseOrigin.UNKNOWN


def _refusal_cache_code(
    scope: SampleAcquisitionCacheScope,
    requested_hash: str,
    failure_kind: SampleAcquisitionFailureKind,
) -> str:
    marker = _CACHE_KIND_MARKERS[failure_kind]
    identity = {
        "version": 1,
        "provider": scope.provider,
        "account_capability_fingerprint": scope.account_capability_fingerprint,
        "route_fingerprint": scope.route_fingerprint,
        "transport": scope.transport,
        "configuration_fingerprint": scope.configuration_fingerprint,
        "requested_hash": requested_hash,
        "failure_kind": failure_kind.value,
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(encoded).hexdigest()[:60]
    # SampleAcquisitionAttempt.error_code is the only existing append-only
    # refusal slot. Keep this opaque marker within its 64-character limit.
    return f"{_REFUSAL_CACHE_PREFIX}{marker}{digest}"


def _refusal_error_code(kind: SampleAcquisitionFailureKind) -> str:
    if kind is SampleAcquisitionFailureKind.PROVIDER_FILE_NOT_FOUND:
        return "virustotal_download_file_not_found"
    if kind is SampleAcquisitionFailureKind.AUTHORIZATION_REFUSED:
        return "virustotal_download_authorization_refused"
    raise ValueError("Only durable provider refusals have a cached error code")


def _refusal_reason(kind: SampleAcquisitionFailureKind) -> str:
    if kind is SampleAcquisitionFailureKind.PROVIDER_FILE_NOT_FOUND:
        return "provider_file_not_found"
    if kind is SampleAcquisitionFailureKind.AUTHORIZATION_REFUSED:
        return "provider_download_authorization_refused"
    raise ValueError("Only durable provider refusals have a cached refusal reason")
