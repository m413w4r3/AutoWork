"""Local, audited operator imports for the YARA rule-testing corpus."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO
from typing import Any, cast
from uuid import UUID, uuid5

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_persistence import EvidenceObservationRepository
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleOrigin, SampleState
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    QualificationActorKind,
    SampleAssociation,
    SampleQualification,
)
from cti_app.domain.investigation import SampleOrigin as AssociationSampleOrigin
from cti_app.domain.production import AnalystInvestigation, AnalystInvestigationStatus
from cti_app.domain.rule_workbench import (
    RuleCorpusInventoryItem,
    RuleSampleProvenance,
    RuleTestCorpusRole,
    detect_file_format_magic,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_SOURCE = "operator.rule-corpus"
_IMPORT_VERSION = "manual-rule-corpus-import.v1"
_IMPORT_OBJECT_TYPE = "manual_rule_corpus_import"
_HOLDOUT_DESIGNATION_EVENT = "rule_corpus_holdout_designated"
_HOLDOUT_ROLES = frozenset(
    {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
)
_OPERATOR_IMPORT_ASSOCIATION_NAMESPACE = UUID("b7bcc71a-5fb4-4e18-93d6-a82b8aa813dc")
_METADATA_PREFIX = "manual_rule_corpus_metadata:"
_HARD_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_TERMINAL_STATUSES = frozenset(
    {
        AnalystInvestigationStatus.COMPLETED,
        AnalystInvestigationStatus.EXHAUSTED,
        AnalystInvestigationStatus.FAILED,
        AnalystInvestigationStatus.CANCELLED,
    }
)
_logger = logging.getLogger(__name__)


class RuleCorpusImportRejection(StrEnum):
    EMPTY_CONTENT = "empty_content"
    SIZE_LIMIT = "size_limit"
    SHA256_MISMATCH = "sha256_mismatch"
    INVALID_ROLE = "invalid_role"
    INVALID_PROVENANCE = "invalid_provenance"
    INVALID_METADATA = "invalid_metadata"
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    INVESTIGATION_TERMINAL = "investigation_terminal"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"
    ROLE_CONFLICT = "sample_sha256_role_conflict"
    HOLDOUT_AFTER_RULE = "holdout_must_be_designated_before_rule"
    HOLDOUT_OVERLAP = "holdout_sha256_overlaps_development"
    HOLDOUT_NOT_DESIGNATED = "holdout_designation_required"
    SAMPLE_LOOKUP_INVALID = "sample_lookup_requires_exactly_one_identity"
    SAMPLE_NOT_FOUND = "quarantined_sample_not_found"
    SAMPLE_AMBIGUOUS = "quarantined_sample_identity_ambiguous"
    SAMPLE_NOT_QUARANTINED = "sample_not_in_quarantine"


class ManualRuleCorpusImportRejected(ValueError):
    def __init__(self, reason: RuleCorpusImportRejection) -> None:
        self.reason = reason
        super().__init__(reason.value)


class RuleCorpusImportInvestigationNotFound(LookupError):
    """The requested investigation does not exist."""


@dataclass(frozen=True, slots=True)
class ImportedRuleCorpusFile:
    investigation_id: UUID
    observation_id: UUID
    blob_id: UUID
    sha256: str
    size_bytes: int
    intended_role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    reason: str
    source_description: str
    operator_id: str
    sample_id: UUID
    association_id: UUID
    format_magic: str = "unknown"
    designated_at: datetime | None = None
    idempotent_replay: bool = False

    @property
    def logical_bucket(self) -> str:
        return SAMPLES_QUARANTINE_BUCKET


@dataclass(frozen=True, slots=True)
class HoldoutDesignation:
    investigation_id: UUID
    sample_sha256: str
    intended_role: RuleTestCorpusRole
    designated_at: datetime
    operator_id: str
    reason: str
    idempotent_replay: bool = False


class ManualRuleCorpusImportService:
    """Store untrusted bytes in quarantine and journal the operator's classification."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_catalog: BlobCatalogService,
        artifact_store: ProductionArtifactStore,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_catalog = blob_catalog
        self._artifact_store = artifact_store
        self._settings = settings

    @property
    def max_upload_bytes(self) -> int:
        configured = getattr(self._settings, "rule_corpus_import_max_bytes", _HARD_MAX_UPLOAD_BYTES)
        if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
            raise ValueError("rule_corpus_import_max_bytes must be a positive integer")
        return min(configured, _HARD_MAX_UPLOAD_BYTES)

    async def list_imports(self, investigation_id: UUID) -> tuple[RuleCorpusInventoryItem, ...]:
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise RuleCorpusImportInvestigationNotFound(str(investigation_id))
        return await list_manual_rule_corpus_imports(self._uow_factory, investigation_id)

    async def designate_holdout(
        self,
        *,
        investigation_id: UUID,
        sample_sha256: str,
        intended_role: RuleTestCorpusRole | str,
        operator_id: str,
        reason: str,
    ) -> HoldoutDesignation:
        if not isinstance(sample_sha256, str) or _SHA256.fullmatch(sample_sha256) is None:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SHA256_MISMATCH)
        sample_sha256 = sample_sha256.casefold()
        try:
            role = RuleTestCorpusRole(intended_role)
        except (TypeError, ValueError) as exc:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE) from exc
        if role not in _HOLDOUT_ROLES:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE)
        clean_operator_id = sanitize_evidence_text(operator_id, max_chars=256).strip()
        clean_reason = sanitize_evidence_text(reason, max_chars=1_500).strip()
        if not clean_operator_id or not clean_reason:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_METADATA)

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND
                )
            if investigation.status in _TERMINAL_STATUSES:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_TERMINAL
                )
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            prior_roles = _corpus_roles_for_sha(observations, sample_sha256)
            if prior_roles and prior_roles != {role}:
                raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
            prior = await self._holdout_designation_for_sha(
                uow, investigation_id, sample_sha256, observations=observations
            )
            if prior is not None:
                if prior.intended_role is not role:
                    raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
                return HoldoutDesignation(
                    investigation_id=prior.investigation_id,
                    sample_sha256=prior.sample_sha256,
                    intended_role=prior.intended_role,
                    designated_at=prior.designated_at,
                    operator_id=prior.operator_id,
                    reason=prior.reason,
                    idempotent_replay=True,
                )

            designated_at = datetime.now(UTC)
            subject = await uow.subjects.get(investigation.subject_id)
            if subject is None:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND
                )
            event = ProvenanceEvent(
                aggregate_type="investigation",
                aggregate_id=investigation_id,
                subject_id=investigation.subject_id,
                event_type=_HOLDOUT_DESIGNATION_EVENT,
                payload={
                    "sample_sha256": sample_sha256,
                    "intended_role": role.value,
                    "designated_at": designated_at.isoformat(),
                    "operator_id": clean_operator_id,
                    "reason": clean_reason,
                },
                tlp=subject.tlp,
                actor_id=clean_operator_id,
                occurred_at=designated_at,
            )
            await uow.provenance.append(event)
            await uow.commit()
        _logger.info(
            "operator_rule_corpus_holdout_designated",
            extra={
                "event": "operator.rule_corpus_holdout_designated",
                "investigation_id": str(investigation_id),
                "operator_id": clean_operator_id,
                "sample_sha256": sample_sha256,
                "intended_role": role.value,
                "designated_at": designated_at.isoformat(),
            },
        )
        return HoldoutDesignation(
            investigation_id=investigation_id,
            sample_sha256=sample_sha256,
            intended_role=role,
            designated_at=designated_at,
            operator_id=clean_operator_id,
            reason=clean_reason,
        )

    @staticmethod
    async def _holdout_designation_for_sha(
        uow: Any,
        investigation_id: UUID,
        sample_sha256: str,
        *,
        observations: Sequence[EvidenceObservation] | None = None,
    ) -> HoldoutDesignation | None:
        found: list[HoldoutDesignation] = []
        for event in await uow.provenance.list_for_aggregate("investigation", investigation_id):
            payload = event.payload
            if (
                event.event_type != _HOLDOUT_DESIGNATION_EVENT
                or payload.get("sample_sha256") != sample_sha256
            ):
                continue
            try:
                role = RuleTestCorpusRole(payload["intended_role"])
                designated_at = event.occurred_at
                operator_id = event.actor_id or payload["operator_id"]
                reason = payload["reason"]
            except (KeyError, TypeError, ValueError):
                continue
            if (
                role not in _HOLDOUT_ROLES
                or not isinstance(operator_id, str)
                or not operator_id.strip()
                or not isinstance(reason, str)
                or not reason.strip()
                or designated_at.tzinfo is None
                or designated_at.utcoffset() is None
            ):
                continue
            found.append(
                HoldoutDesignation(
                    investigation_id=investigation_id,
                    sample_sha256=sample_sha256,
                    intended_role=role,
                    designated_at=designated_at.astimezone(UTC),
                    operator_id=operator_id,
                    reason=reason,
                )
            )

        for observation in observations or ():
            if observation.sha256 != sample_sha256:
                continue
            metadata = parse_manual_rule_corpus_metadata(observation)
            if metadata is None or metadata["intended_role"] not in _HOLDOUT_ROLES:
                continue
            designated_at = metadata["designated_at"]
            found.append(
                HoldoutDesignation(
                    investigation_id=investigation_id,
                    sample_sha256=sample_sha256,
                    intended_role=metadata["intended_role"],
                    designated_at=designated_at,
                    operator_id=metadata["operator_id"],
                    reason=metadata["reason"],
                )
            )
        return min(found, key=lambda item: item.designated_at) if found else None

    async def import_bytes(
        self,
        *,
        investigation_id: UUID,
        content: bytes,
        intended_role: RuleTestCorpusRole | str,
        provenance: RuleSampleProvenance | str,
        operator_id: str,
        reason: str,
        source_description: str = "",
        declared_sha256: str | None = None,
    ) -> ImportedRuleCorpusFile:
        if not isinstance(content, bytes):
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_METADATA)
        if not content:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.EMPTY_CONTENT)
        if len(content) > self.max_upload_bytes:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SIZE_LIMIT)
        try:
            raw_role = str(intended_role)
            legacy_roles = {
                "confirmed_positive": RuleTestCorpusRole.HUMAN_CONFIRMED,
                "out_of_cluster_candidate": RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED,
            }
            role = legacy_roles.get(raw_role) or RuleTestCorpusRole(raw_role)
        except (TypeError, ValueError) as exc:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE) from exc
        if role is RuleTestCorpusRole.BYTES_MISSING:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE)
        try:
            source_provenance = RuleSampleProvenance(provenance)
        except (TypeError, ValueError) as exc:
            raise ManualRuleCorpusImportRejected(
                RuleCorpusImportRejection.INVALID_PROVENANCE
            ) from exc

        clean_operator_id = sanitize_evidence_text(operator_id, max_chars=256).strip()
        clean_reason = sanitize_evidence_text(reason, max_chars=1_500).strip()
        clean_source_description = sanitize_evidence_text(
            source_description, max_chars=1_000
        ).strip()
        if not clean_operator_id or not clean_reason:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_METADATA)

        actual_sha256 = hashlib.sha256(content).hexdigest()
        format_magic = detect_file_format_magic(content)
        designated_at: datetime | None = None
        if declared_sha256 is not None and (
            not isinstance(declared_sha256, str)
            or not _SHA256.fullmatch(declared_sha256)
            or declared_sha256 != actual_sha256
        ):
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SHA256_MISMATCH)

        object_id = f"{actual_sha256}:{role.value}"
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND
                )
            if investigation.status in _TERMINAL_STATUSES:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_TERMINAL
                )
            associations = await uow.sample_associations.list_for_investigation(investigation_id)
            existing_association = next(
                (item for item in associations if item.sample_sha256 == actual_sha256), None
            )
            if role is RuleTestCorpusRole.HUMAN_CONFIRMED and not _human_confirmed(
                existing_association
            ):
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.HUMAN_CONFIRMATION_REQUIRED
                )
            prior_observation = await self._find_existing_import(
                uow.evidence_observations, investigation_id, object_id
            )
            all_import_observations = await uow.evidence_observations.list_for_investigation(
                investigation_id
            )
            prior_roles = _corpus_roles_for_sha(all_import_observations, actual_sha256)
            designation = await self._holdout_designation_for_sha(
                uow, investigation_id, actual_sha256, observations=all_import_observations
            )
            if role in _HOLDOUT_ROLES:
                if designation is None:
                    raise ManualRuleCorpusImportRejected(
                        RuleCorpusImportRejection.HOLDOUT_NOT_DESIGNATED
                    )
                if designation.intended_role is not role:
                    raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
                designated_at = designation.designated_at
            elif designation is not None:
                raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
            if prior_observation is None and prior_roles and prior_roles != {role}:
                if role in _HOLDOUT_ROLES or prior_roles & _HOLDOUT_ROLES:
                    raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
                raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.ROLE_CONFLICT)
            if prior_observation is not None:
                record = await uow.blobs.get(prior_observation.blob_id)
                if record is None or record.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET:
                    raise ValueError("Existing rule-corpus import has no quarantined byte blob")
                prior = parse_manual_rule_corpus_metadata(prior_observation)
                if prior is None:
                    raise ValueError("Existing rule-corpus import metadata is unreadable")
                sample, association = await self._ensure_imported_sample(
                    uow,
                    investigation=investigation,
                    blob=record,
                    sha256=actual_sha256,
                    metadata=prior,
                    object_id=object_id,
                )
                await uow.commit()
                imported = _import_from_observation(
                    prior_observation,
                    size_bytes=record.descriptor.size,
                    metadata=prior,
                    sample_id=sample.id,
                    association_id=association.id,
                    idempotent_replay=True,
                )
                _audit_import(imported)
                return imported

        blob = await self._blob_catalog.ingest(
            BytesIO(content),
            logical_bucket=SAMPLES_QUARANTINE_BUCKET,
            mime_type="application/octet-stream",
        )
        if (
            blob.descriptor.sha256 != actual_sha256
            or blob.descriptor.size != len(content)
            or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
        ):
            raise ValueError("Quarantine blob catalog returned inconsistent byte metadata")

        metadata: dict[str, Any] = {
            "intended_role": role.value,
            "provenance": source_provenance.value,
            "reason": clean_reason,
            "source_description": clean_source_description,
            "operator_id": clean_operator_id,
            "size_bytes": len(content),
            "format_magic": format_magic,
            "designated_at": designated_at.isoformat() if designated_at else None,
        }
        limitations = (
            "Fichier non fiable conservé uniquement en quarantaine; jamais exécuté.",
            "Import local uniquement; aucun transfert vers un tiers, quel que soit le TLP.",
            _METADATA_PREFIX + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        )

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND
                )
            if investigation.status in _TERMINAL_STATUSES:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_TERMINAL
                )
            recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
            observation = await recorder.record_existing_blob(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob.id,
                sha256=actual_sha256,
                source=_IMPORT_SOURCE,
                source_version=_IMPORT_VERSION,
                object_type=_IMPORT_OBJECT_TYPE,
                object_id=object_id,
                limitations=limitations,
                idempotency_key=f"rule-corpus-import:{investigation_id}:{actual_sha256}:{role.value}",
            )
            sample, association = await self._ensure_imported_sample(
                uow,
                investigation=investigation,
                blob=blob,
                sha256=actual_sha256,
                metadata=metadata,
                object_id=object_id,
            )
            await uow.commit()

        imported = ImportedRuleCorpusFile(
            investigation_id=investigation_id,
            observation_id=observation.id,
            blob_id=blob.id,
            sha256=actual_sha256,
            size_bytes=len(content),
            intended_role=role,
            provenance=source_provenance,
            reason=clean_reason,
            source_description=clean_source_description,
            operator_id=clean_operator_id,
            sample_id=sample.id,
            association_id=association.id,
            format_magic=format_magic,
            designated_at=designated_at,
        )
        _audit_import(imported)
        return imported

    async def assign_existing_sample(
        self,
        *,
        investigation_id: UUID,
        intended_role: RuleTestCorpusRole | str,
        provenance: RuleSampleProvenance | str,
        operator_id: str,
        reason: str,
        sample_id: UUID | None = None,
        sample_sha256: str | None = None,
        source_description: str = "",
    ) -> ImportedRuleCorpusFile:
        if (sample_id is None) == (sample_sha256 is None):
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SAMPLE_LOOKUP_INVALID)
        if sample_sha256 is not None and (
            not isinstance(sample_sha256, str) or _SHA256.fullmatch(sample_sha256) is None
        ):
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SHA256_MISMATCH)
        try:
            raw_role = str(intended_role)
            role = {
                "confirmed_positive": RuleTestCorpusRole.HUMAN_CONFIRMED,
                "out_of_cluster_candidate": RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED,
            }.get(raw_role) or RuleTestCorpusRole(raw_role)
            source_provenance = RuleSampleProvenance(provenance)
        except (TypeError, ValueError) as exc:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE) from exc
        if role is RuleTestCorpusRole.BYTES_MISSING:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE)

        clean_operator_id = sanitize_evidence_text(operator_id, max_chars=256).strip()
        clean_reason = sanitize_evidence_text(reason, max_chars=1_500).strip()
        clean_source_description = sanitize_evidence_text(
            source_description, max_chars=1_000
        ).strip()
        if not clean_operator_id or not clean_reason:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_METADATA)

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND
                )
            if investigation.status in _TERMINAL_STATUSES:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.INVESTIGATION_TERMINAL
                )
            sample = await self._existing_quarantined_sample(
                uow,
                investigation,
                sample_id=sample_id,
                sample_sha256=sample_sha256,
            )
            blob = await uow.blobs.get(sample.blob_id)
            if (
                blob is None
                or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                or blob.descriptor.sha256 is None
                or (
                    sample_sha256 is not None and blob.descriptor.sha256 != sample_sha256.casefold()
                )
                or (
                    sample.expected_hash is not None
                    and _SHA256.fullmatch(sample.expected_hash.casefold()) is not None
                    and sample.expected_hash.casefold() != blob.descriptor.sha256
                )
            ):
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.SAMPLE_NOT_QUARANTINED
                )
            actual_sha256 = blob.descriptor.sha256
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            prior_roles = _corpus_roles_for_sha(observations, actual_sha256)
            designation = await self._holdout_designation_for_sha(
                uow, investigation_id, actual_sha256, observations=observations
            )
            if role in _HOLDOUT_ROLES:
                if designation is None:
                    raise ManualRuleCorpusImportRejected(
                        RuleCorpusImportRejection.HOLDOUT_NOT_DESIGNATED
                    )
                if designation.intended_role is not role:
                    raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
            elif designation is not None:
                raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
            if prior_roles and prior_roles != {role}:
                if role in _HOLDOUT_ROLES or prior_roles & _HOLDOUT_ROLES:
                    raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.HOLDOUT_OVERLAP)
                raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.ROLE_CONFLICT)

            associations = await uow.sample_associations.list_for_investigation(investigation_id)
            existing_association = next(
                (item for item in associations if item.sample_sha256 == actual_sha256), None
            )
            if role is RuleTestCorpusRole.HUMAN_CONFIRMED and not _human_confirmed(
                existing_association
            ):
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.HUMAN_CONFIRMATION_REQUIRED
                )
            object_id = f"{actual_sha256}:{role.value}"
            prior_observation = await self._find_existing_import(
                uow.evidence_observations, investigation_id, object_id
            )
            if prior_observation is not None:
                prior = parse_manual_rule_corpus_metadata(prior_observation)
                if prior is None or prior["intended_role"] is not role:
                    raise ValueError("Existing rule-corpus role metadata is unreadable")
                association = await self._ensure_existing_sample_association(
                    uow, investigation=investigation, sample=sample, sha256=actual_sha256
                )
                await uow.commit()
                return _import_from_observation(
                    prior_observation,
                    size_bytes=blob.descriptor.size,
                    metadata=prior,
                    sample_id=sample.id,
                    association_id=association.id,
                    idempotent_replay=True,
                )

            designated_at = (
                designation.designated_at
                if role in _HOLDOUT_ROLES and designation is not None
                else None
            )
            metadata: dict[str, Any] = {
                "intended_role": role.value,
                "provenance": source_provenance.value,
                "reason": clean_reason,
                "source_description": clean_source_description,
                "operator_id": clean_operator_id,
                "size_bytes": blob.descriptor.size,
                "format_magic": "quarantined_uninspected",
                "designated_at": designated_at.isoformat() if designated_at else None,
                "classification_method": "existing_quarantined_sample",
            }
            limitations = (
                "Rôle opérateur appliqué à un blob déjà en quarantaine; octets non exposés.",
                "Échantillon non fiable; jamais exécuté par cette opération.",
                _METADATA_PREFIX + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            )
            recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
            observation = await recorder.record_existing_blob(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob.id,
                sha256=actual_sha256,
                source=_IMPORT_SOURCE,
                source_version=_IMPORT_VERSION,
                object_type=_IMPORT_OBJECT_TYPE,
                object_id=object_id,
                limitations=limitations,
                idempotency_key=f"rule-corpus-role:{investigation_id}:{actual_sha256}:{role.value}",
            )
            association = await self._ensure_existing_sample_association(
                uow, investigation=investigation, sample=sample, sha256=actual_sha256
            )
            await uow.provenance.append(
                ProvenanceEvent(
                    aggregate_type="sample",
                    aggregate_id=sample.id,
                    subject_id=sample.subject_id,
                    event_type="sample_corpus_role_assigned_by_operator",
                    payload={
                        "investigation_id": str(investigation_id),
                        "sample_sha256": actual_sha256,
                        "intended_role": role.value,
                        "provenance": source_provenance.value,
                        "operator_id": clean_operator_id,
                        "reason": clean_reason,
                    },
                    tlp=sample.tlp,
                    actor_id=clean_operator_id,
                )
            )
            await uow.commit()

        assigned = ImportedRuleCorpusFile(
            investigation_id=investigation_id,
            observation_id=observation.id,
            blob_id=blob.id,
            sha256=actual_sha256,
            size_bytes=blob.descriptor.size,
            intended_role=role,
            provenance=source_provenance,
            reason=clean_reason,
            source_description=clean_source_description,
            operator_id=clean_operator_id,
            sample_id=sample.id,
            association_id=association.id,
            format_magic="quarantined_uninspected",
            designated_at=designated_at,
        )
        _logger.info(
            "operator_rule_corpus_role_assigned",
            extra={
                "event": "operator.rule_corpus_role_assigned",
                "investigation_id": str(investigation_id),
                "operator_id": clean_operator_id,
                "sha256": actual_sha256,
                "intended_role": role.value,
                "sample_id": str(sample.id),
            },
        )
        return assigned

    @staticmethod
    async def _existing_quarantined_sample(
        uow: Any,
        investigation: AnalystInvestigation,
        *,
        sample_id: UUID | None,
        sample_sha256: str | None,
    ) -> Sample:
        if sample_id is not None:
            sample = await uow.samples.get(sample_id)
            candidates = (sample,) if sample is not None else ()
        else:
            candidates = await uow.samples.list_for_subject(investigation.subject_id)
        matches: list[Sample] = []
        non_quarantined_match = False
        for sample in candidates:
            if sample is None or sample.subject_id != investigation.subject_id:
                continue
            blob = await uow.blobs.get(sample.blob_id)
            if (
                blob is None
                or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                or (
                    sample_sha256 is not None and blob.descriptor.sha256 != sample_sha256.casefold()
                )
            ):
                continue
            if getattr(sample.state, "value", sample.state) not in {
                SampleState.QUARANTINED.value,
                SampleState.REVIEW_CANDIDATE.value,
            }:
                non_quarantined_match = True
                continue
            matches.append(sample)
        if not matches:
            if non_quarantined_match:
                raise ManualRuleCorpusImportRejected(
                    RuleCorpusImportRejection.SAMPLE_NOT_QUARANTINED
                )
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SAMPLE_NOT_FOUND)
        if len(matches) != 1:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.SAMPLE_AMBIGUOUS)
        return matches[0]

    @staticmethod
    async def _ensure_existing_sample_association(
        uow: Any,
        *,
        investigation: AnalystInvestigation,
        sample: Sample,
        sha256: str,
    ) -> SampleAssociation:
        association = cast(
            SampleAssociation | None,
            next(
                (
                    item
                    for item in await uow.sample_associations.list_for_investigation(
                        investigation.id
                    )
                    if item.sample_sha256 == sha256
                ),
                None,
            ),
        )
        if association is not None:
            return association
        association = SampleAssociation(
            id=uuid5(_OPERATOR_IMPORT_ASSOCIATION_NAMESPACE, f"{investigation.id}:{sha256}"),
            investigation_id=investigation.id,
            sample_sha256=sha256,
            origin=AssociationSampleOrigin.MANUAL,
        )
        return cast(
            SampleAssociation,
            await uow.sample_associations.append_version(association, expected_version=0),
        )

    @staticmethod
    async def _ensure_imported_sample(
        uow: Any,
        *,
        investigation: AnalystInvestigation,
        blob: Any,
        sha256: str,
        metadata: dict[str, Any],
        object_id: str,
    ) -> tuple[Sample, SampleAssociation]:
        if (
            blob.descriptor.sha256 != sha256
            or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
        ):
            raise ValueError("Operator-imported bytes are not the verified quarantine blob")
        subject = await uow.subjects.get(investigation.subject_id)
        if subject is None:
            raise ValueError("The imported investigation subject does not exist")

        sample = await uow.samples.get_by_subject_and_blob(subject.id, blob.id)
        if sample is None:
            sample = Sample(
                subject_id=subject.id,
                blob_id=blob.id,
                original_name=sha256,
                origin="operator_import",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=subject.tlp,
                do_not_submit=True,
                external_llm_allowed=False,
                origin_kind=SampleOrigin.MANUAL,
                state=SampleState.QUARANTINED,
                source_service="operator_import",
                source_object_id=sha256,
                expected_hash=sha256,
            )
            await uow.samples.add(sample)
        elif sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}:
            raise ValueError("An operator import cannot reuse a sample outside quarantine")

        association = next(
            (
                item
                for item in await uow.sample_associations.list_for_investigation(investigation.id)
                if item.sample_sha256 == sha256
            ),
            None,
        )
        if association is None:
            association = SampleAssociation(
                id=uuid5(
                    _OPERATOR_IMPORT_ASSOCIATION_NAMESPACE,
                    f"{investigation.id}:{sha256}",
                ),
                investigation_id=investigation.id,
                sample_sha256=sha256,
                origin=AssociationSampleOrigin.MANUAL,
            )
            association = await uow.sample_associations.append_version(
                association, expected_version=0
            )

        existing_events = await uow.provenance.list_for_aggregate("sample", sample.id)
        if not any(
            event.event_type == "sample_imported_by_operator"
            and event.payload.get("investigation_id") == str(investigation.id)
            and event.payload.get("import_source") == "rule_corpus"
            and event.payload.get("import_object_id") == object_id
            for event in existing_events
        ):
            await uow.provenance.append(
                ProvenanceEvent(
                    aggregate_type="sample",
                    aggregate_id=sample.id,
                    subject_id=sample.subject_id,
                    event_type="sample_imported_by_operator",
                    payload={
                        "provenance": "operator_import",
                        "import_source": "rule_corpus",
                        "investigation_id": str(investigation.id),
                        "import_object_id": object_id,
                        "operator_id": metadata["operator_id"],
                        "intended_role": metadata["intended_role"],
                        "declared_corpus_provenance": metadata["provenance"],
                    },
                    tlp=sample.tlp,
                    actor_id=metadata["operator_id"],
                )
            )
        return sample, association

    @staticmethod
    async def _find_existing_import(
        repository: EvidenceObservationRepository,
        investigation_id: UUID,
        object_id: str,
    ) -> EvidenceObservation | None:
        observations = await repository.list_for_investigation(investigation_id)
        return next(
            (
                observation
                for observation in observations
                if observation.kind is EvidenceKind.OTHER
                and observation.source == _IMPORT_SOURCE
                and observation.source_version == _IMPORT_VERSION
                and observation.object_type == _IMPORT_OBJECT_TYPE
                and observation.object_id == object_id
            ),
            None,
        )


def _corpus_roles_for_sha(
    observations: Sequence[EvidenceObservation], sample_sha256: str
) -> set[RuleTestCorpusRole]:
    return {
        metadata["intended_role"]
        for observation in observations
        if observation.sha256 == sample_sha256
        and (metadata := parse_manual_rule_corpus_metadata(observation)) is not None
    }


def parse_manual_rule_corpus_metadata(
    observation: EvidenceObservation,
) -> dict[str, Any] | None:
    if (
        observation.kind is not EvidenceKind.OTHER
        or observation.source != _IMPORT_SOURCE
        or observation.source_version != _IMPORT_VERSION
        or observation.object_type != _IMPORT_OBJECT_TYPE
    ):
        return None
    for limitation in observation.limitations:
        if not limitation.startswith(_METADATA_PREFIX):
            continue
        try:
            metadata = json.loads(limitation[len(_METADATA_PREFIX) :])
            raw_role = metadata["intended_role"]
            if raw_role == "confirmed_positive":
                role = RuleTestCorpusRole.HUMAN_CONFIRMED
            elif raw_role == "out_of_cluster_candidate":
                role = RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED
            else:
                role = RuleTestCorpusRole(raw_role)
            provenance = RuleSampleProvenance(metadata["provenance"])
            if role is RuleTestCorpusRole.BYTES_MISSING:
                return None
            if not isinstance(metadata.get("size_bytes"), int):
                return None
            if not all(
                isinstance(metadata.get(key), str)
                for key in ("reason", "source_description", "operator_id")
            ):
                return None
            format_magic = metadata.get("format_magic", "legacy_untyped")
            if not isinstance(format_magic, str) or not format_magic.strip():
                return None
            designated_at = metadata.get("designated_at")
            if role in {
                RuleTestCorpusRole.HOLDOUT_POSITIVE,
                RuleTestCorpusRole.HOLDOUT_NEGATIVE,
            }:
                if not isinstance(designated_at, str):
                    return None
                designated_at = datetime.fromisoformat(designated_at)
                if designated_at.tzinfo is None or designated_at.utcoffset() is None:
                    return None
            elif designated_at is not None:
                return None
            return {
                **metadata,
                "format_magic": format_magic,
                "intended_role": role,
                "provenance": provenance,
                "designated_at": designated_at,
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def manual_rule_corpus_inventory_item(
    observation: EvidenceObservation, *, size_bytes: int
) -> RuleCorpusInventoryItem | None:
    metadata = parse_manual_rule_corpus_metadata(observation)
    if metadata is None or _SHA256.fullmatch(observation.sha256) is None:
        return None
    if metadata["size_bytes"] != size_bytes:
        return None
    return RuleCorpusInventoryItem(
        sample_sha256=observation.sha256,
        role=metadata["intended_role"],
        provenance=metadata["provenance"],
        size_bytes=size_bytes,
        source_description=metadata["source_description"],
        format_magic=metadata["format_magic"],
        designated_at=metadata["designated_at"],
        observation_id=observation.id,
        operator_id=metadata["operator_id"],
    )


async def list_manual_rule_corpus_imports(
    uow_factory: UnitOfWorkFactory, investigation_id: UUID
) -> tuple[RuleCorpusInventoryItem, ...]:
    async with uow_factory() as uow:
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        result: list[RuleCorpusInventoryItem] = []
        for observation in observations:
            if (
                observation.source != _IMPORT_SOURCE
                or observation.object_type != _IMPORT_OBJECT_TYPE
            ):
                continue
            blob = await uow.blobs.get(observation.blob_id)
            if blob is None or blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET:
                continue
            item = manual_rule_corpus_inventory_item(observation, size_bytes=blob.descriptor.size)
            if item is not None:
                result.append(item)
    return tuple(sorted(result, key=lambda item: (item.role.value, item.sample_sha256)))


def _import_from_observation(
    observation: EvidenceObservation,
    *,
    size_bytes: int,
    metadata: dict[str, Any],
    sample_id: UUID,
    association_id: UUID,
    idempotent_replay: bool,
) -> ImportedRuleCorpusFile:
    if metadata["size_bytes"] != size_bytes:
        raise ValueError("Existing rule-corpus import size metadata is inconsistent")
    return ImportedRuleCorpusFile(
        investigation_id=observation.investigation_id,
        observation_id=observation.id,
        blob_id=observation.blob_id,
        sha256=observation.sha256,
        size_bytes=size_bytes,
        intended_role=metadata["intended_role"],
        provenance=metadata["provenance"],
        reason=metadata["reason"],
        source_description=metadata["source_description"],
        operator_id=metadata["operator_id"],
        sample_id=sample_id,
        association_id=association_id,
        format_magic=metadata.get("format_magic", "legacy_untyped"),
        designated_at=metadata.get("designated_at"),
        idempotent_replay=idempotent_replay,
    )


def _human_confirmed(association: SampleAssociation | None) -> bool:
    return bool(
        association is not None
        and association.qualification is SampleQualification.CONFIRMED
        and any(
            change.to_qualification is SampleQualification.CONFIRMED
            and change.actor_kind is QualificationActorKind.HUMAN
            for change in association.changes
        )
    )


def _audit_import(imported: ImportedRuleCorpusFile) -> None:
    _logger.info(
        "operator_rule_corpus_imported",
        extra={
            "event": "operator.rule_corpus_imported",
            "investigation_id": str(imported.investigation_id),
            "operator_id": imported.operator_id,
            "sha256": imported.sha256,
            "intended_role": imported.intended_role.value,
            "provenance": imported.provenance.value,
            "size_bytes": imported.size_bytes,
            "idempotent_replay": imported.idempotent_replay,
        },
    )
