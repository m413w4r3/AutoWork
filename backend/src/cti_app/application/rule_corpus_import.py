"""Local, audited operator imports for the YARA rule-testing corpus."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import Any
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_persistence import EvidenceObservationRepository
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production import AnalystInvestigationStatus
from cti_app.domain.rule_workbench import (
    RuleCorpusInventoryItem,
    RuleSampleProvenance,
    RuleTestCorpusRole,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_SOURCE = "operator.rule-corpus"
_IMPORT_VERSION = "manual-rule-corpus-import.v1"
_IMPORT_OBJECT_TYPE = "manual_rule_corpus_import"
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


class ManualRuleCorpusImportRejected(ValueError):
    def __init__(self, reason: RuleCorpusImportRejection) -> None:
        self.reason = reason
        super().__init__(reason.value)


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
    idempotent_replay: bool = False

    @property
    def logical_bucket(self) -> str:
        return SAMPLES_QUARANTINE_BUCKET


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
            role = RuleTestCorpusRole(intended_role)
        except (TypeError, ValueError) as exc:
            raise ManualRuleCorpusImportRejected(RuleCorpusImportRejection.INVALID_ROLE) from exc
        if role is RuleTestCorpusRole.HOLDOUT:
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
            prior_observation = await self._find_existing_import(
                uow.evidence_observations, investigation_id, object_id
            )
            if prior_observation is not None:
                record = await uow.blobs.get(prior_observation.blob_id)
                if record is None or record.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET:
                    raise ValueError("Existing rule-corpus import has no quarantined byte blob")
                prior = parse_manual_rule_corpus_metadata(prior_observation)
                if prior is None:
                    raise ValueError("Existing rule-corpus import metadata is unreadable")
                imported = _import_from_observation(
                    prior_observation,
                    size_bytes=record.descriptor.size,
                    metadata=prior,
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
        )
        _audit_import(imported)
        return imported

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
            role = RuleTestCorpusRole(metadata["intended_role"])
            provenance = RuleSampleProvenance(metadata["provenance"])
            if role is RuleTestCorpusRole.HOLDOUT:
                return None
            if not isinstance(metadata.get("size_bytes"), int):
                return None
            if not all(
                isinstance(metadata.get(key), str)
                for key in ("reason", "source_description", "operator_id")
            ):
                return None
            return {**metadata, "intended_role": role, "provenance": provenance}
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
        idempotent_replay=idempotent_replay,
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
