"""Read canonical investigation, rule, sample, and observation data for annex export."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.evidence_recorder import MAX_EVIDENCE_JSON_BYTES
from cti_app.application.investigation_annex_export import (
    AnnexAsset,
    InvestigationAnnexSnapshot,
)
from cti_app.application.investigation_audit_export import AuditSourceSnapshot
from cti_app.application.investigation_governance import g2_envelope_authorization_state
from cti_app.application.media_assets import MAX_MEDIA_ASSET_READ_BYTES
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import TLPLevel
from cti_app.infrastructure.database.investigation_audit_reader import (
    load_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    ProvenanceEventRow,
    SampleRow,
    SubjectRow,
)
from cti_app.infrastructure.database.models.media_assets import MediaAssetRow
from cti_app.infrastructure.database.models.production import AnalystInvestigationRow
from cti_app.infrastructure.database.repositories.investigation import (
    SqlAlchemyInvestigationEnvelopeRepository,
)

_SHA = re.compile(r"^[0-9a-fA-F]{32,64}$", re.ASCII)
_IOCS = {
    "file": "file",
    "domain": "domain",
    "domains": "domain",
    "ip": "ip",
    "ip_address": "ip",
    "ip_addresses": "ip",
    "url": "url",
    "urls": "url",
}
_NETWORK_CAPTURE_SOURCE = "network.quarantine"


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    return str(_value(value))


def _metadata_text(value: Any) -> str:
    return str(_value(value)) if value is not None else ""


def _safe_sha(value: object) -> str | None:
    text_value = str(value).lower()
    return text_value if _SHA.fullmatch(text_value) else None


def _safe_sha256(value: object) -> str | None:
    digest = _safe_sha(value)
    return digest if digest is not None and len(digest) == 64 else None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


async def _read_canonical_blob(
    artifact_store: ProductionArtifactStore,
    blob_id: UUID,
    expected_sha256: str | None,
    *,
    max_bytes: int | None,
) -> tuple[bytes | None, str | None]:
    try:
        content = await artifact_store.read_bytes(blob_id, max_bytes=max_bytes)
    except Exception as exc:
        reason = (
            "blob_read_limit"
            if type(exc).__name__ == "BlobReadLimitExceededError"
            else "blob_unavailable"
        )
        return None, reason
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        return None, "blob_sha256_mismatch"
    return content, None


def _external_archive_policy(
    envelope: Any, subject_tlp: str | None, decisions: Sequence[Any]
) -> tuple[bool, tuple[str, ...]]:
    if envelope is None:
        return False, ("g2_envelope_unavailable",)
    disclosure = getattr(envelope, "disclosure", None)
    reasons: list[str] = []
    if not bool(getattr(disclosure, "external_llm_allowed", False)):
        reasons.append("external_llm_not_allowed")
    if g2_envelope_authorization_state(envelope, decisions).revoked:
        reasons.append("g2_envelope_revoked")
    envelope_tlp = _string(getattr(disclosure, "tlp", None))
    if envelope_tlp == TLPLevel.RED.value or subject_tlp == TLPLevel.RED.value:
        reasons.append("tlp_red")
    return not reasons, tuple(sorted(set(reasons)))


def _sample_policy(
    sample: SampleRow | None,
    qualification: str | None,
    *,
    envelope_allowed: bool,
    envelope_restrictions: Sequence[str],
) -> tuple[bool, tuple[str, ...]]:
    reasons = list(envelope_restrictions)
    if sample is None:
        return False, ("sample_not_acquired",)
    if not envelope_allowed:
        reasons.extend(envelope_restrictions)
    if not bool(sample.external_llm_allowed):
        reasons.append("sample_external_llm_not_allowed")
    if sample.license_restriction:
        reasons.append("sample_license_restricted")
    if sample.state == "rejected":
        reasons.append("sample_rejected_or_revoked")
    if qualification == "rejected":
        reasons.append("association_rejected_or_revoked")
    if sample.tlp == TLPLevel.RED.value:
        reasons.append("tlp_red")
    return not reasons, tuple(sorted(set(reasons)))


def _sample_acquisition_status(
    sample: SampleRow | None,
    investigation_created_at: datetime,
    *,
    investigation_id: UUID,
    provenance_events: Sequence[ProvenanceEventRow] = (),
    association_id: UUID | None = None,
) -> str:
    if sample is None:
        return "not_acquired"
    for event in provenance_events:
        if event.payload.get("investigation_id") == str(investigation_id) and event.event_type in {
            "sample_acquired_from_virustotal",
            "sample_imported_from_vt_sandbox_pcap",
            "sample_imported_by_operator",
        }:
            return "acquired_during_investigation"
        if (
            association_id is not None
            and event.event_type == "sample_derived_from_archive_member"
            and event.payload.get("association_id") == str(association_id)
        ):
            return "acquired_during_investigation"
    if sample.acquired_at <= investigation_created_at:
        return "already_held"
    return "acquired"


def _indicator_value(kind: str, value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    kind = _IOCS.get(kind.casefold(), "")
    candidate = value.strip()
    if not candidate:
        return None
    if kind == "file":
        digest = _safe_sha(candidate)
        if digest is None or len(digest) not in {32, 40, 64}:
            return None
        return "file", digest
    if kind == "ip":
        try:
            return "ip", str(ipaddress.ip_address(candidate))
        except ValueError:
            return None
    if kind == "domain":
        if len(candidate) > 253 or any(not part for part in candidate.rstrip(".").split(".")):
            return None
        return "domain", candidate.rstrip(".").lower()
    if kind == "url" and len(candidate) <= 2_048 and "://" in candidate:
        return "url", candidate
    return None


def _vt_indicators(
    payload: Mapping[str, Any], object_type: str, object_id: str
) -> tuple[tuple[str, str], ...]:
    rows: set[tuple[str, str]] = set()
    direct = _indicator_value(object_type, object_id)
    if direct is not None:
        rows.add(direct)
    candidate = payload.get("candidate")
    descriptors = payload.get("descriptors")
    candidates = [candidate] if isinstance(candidate, Mapping) else []
    if isinstance(descriptors, list):
        candidates.extend(item for item in descriptors if isinstance(item, Mapping))
    for item in candidates:
        indicator = _indicator_value(str(item.get("type", "")), item.get("id"))
        if indicator is not None:
            rows.add(indicator)
    return tuple(sorted(rows))


def _candidate_status(candidate: Any) -> str:
    return _metadata_text(getattr(candidate, "status", "candidate"))


def _experiment_note(experiment: Any) -> dict[str, Any]:
    return {
        "schema": "autowork.investigation-experiment-note.v1",
        "id": str(experiment.id),
        "version": experiment.version,
        "tool": experiment.tool_name,
        "tool_version": experiment.tool_version,
        "availability": _metadata_text(experiment.availability),
        "actual_outcome": _metadata_text(experiment.actual_outcome),
        "observation_coverage": _metadata_text(experiment.observation_coverage),
        "verdict": _metadata_text(experiment.verdict),
        "method_mode": _metadata_text(experiment.method_mode),
        "observation_refs": [
            {"id": str(item.observation_id), "sha256": item.sha256}
            for item in experiment.observation_refs
        ],
        "limitations": list(experiment.limitations),
    }


def _finding_note(finding: Any) -> dict[str, Any]:
    return {
        "schema": "autowork.investigation-finding-note.v1",
        "id": str(finding.id),
        "version": finding.version,
        "kind": _metadata_text(finding.kind),
        "assessment": _metadata_text(finding.assessment),
        "novelty": _metadata_text(finding.novelty) if finding.novelty is not None else None,
        "observation_refs": [str(item) for item in finding.observation_refs],
        "claim_refs": list(finding.claim_refs),
        "limitations": list(finding.limitations),
    }


def _sample_provenance_note(event: ProvenanceEventRow) -> dict[str, Any] | None:
    event_fields = {
        "sample_acquired_from_virustotal": (
            "source_service",
            "hash_family",
            "reason",
            "investigation_id",
        ),
        "sample_derived_from_archive_member": (
            "provenance",
            "derivation_id",
            "association_id",
            "parent_sample_id",
            "parent_sha256",
            "member_path",
            "member_sha256",
            "member_size",
            "reason",
        ),
        "sample_imported_from_vt_sandbox_pcap": (
            "provenance",
            "investigation_id",
            "file_hash",
            "sandbox_id",
            "capture_sha256",
            "source_service",
        ),
        "sample_imported_by_operator": (
            "investigation_id",
            "import_source",
            "import_object_id",
        ),
    }
    allowed_fields = event_fields.get(event.event_type)
    if allowed_fields is None:
        return None
    return {
        "schema": "autowork.sample-provenance-note.v1",
        "event_id": str(event.id),
        "event_type": event.event_type,
        "sample_id": str(event.aggregate_id),
        "subject_id": str(event.subject_id) if event.subject_id else None,
        "tlp": event.tlp,
        "occurred_at": event.occurred_at.isoformat(),
        "attributes": {key: event.payload[key] for key in allowed_fields if key in event.payload},
    }


async def load_investigation_annex_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    artifact_store: ProductionArtifactStore,
    *,
    include_restricted_archive: bool = False,
) -> InvestigationAnnexSnapshot:
    """Read one repeatable-read PostgreSQL snapshot and verified MinIO objects."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            source_snapshot: AuditSourceSnapshot = await load_investigation_audit_snapshot(
                session, investigation_id
            )
            opinion_figures = tuple(
                (opinion, figure)
                for opinion in source_snapshot.opinions
                for figure in getattr(opinion, "protocol_figures", ())
            )
            figure_asset_ids = {figure.asset_id for _, figure in opinion_figures}
            media_assets = (
                {
                    row.asset_id: row
                    for row in await session.scalars(
                        select(MediaAssetRow).where(MediaAssetRow.asset_id.in_(figure_asset_ids))
                    )
                }
                if figure_asset_ids
                else {}
            )
            investigation_row = await session.scalar(
                select(AnalystInvestigationRow).where(
                    AnalystInvestigationRow.id == investigation_id
                )
            )
            if investigation_row is None:
                raise LookupError("Investigation does not exist")
            subject = await session.get(SubjectRow, investigation_row.subject_id)
            subject_tlp = subject.tlp if subject is not None else None
            envelope = await SqlAlchemyInvestigationEnvelopeRepository(session).get(
                investigation_id
            )
            samples = tuple(
                await session.scalars(
                    select(SampleRow)
                    .where(SampleRow.subject_id == investigation_row.subject_id)
                    .order_by(SampleRow.id)
                )
            )
            sample_ids = tuple(sample.id for sample in samples)
            provenance_events = (
                tuple(
                    await session.scalars(
                        select(ProvenanceEventRow)
                        .where(
                            ProvenanceEventRow.aggregate_type == "sample",
                            ProvenanceEventRow.aggregate_id.in_(sample_ids),
                        )
                        .order_by(ProvenanceEventRow.occurred_at, ProvenanceEventRow.id)
                    )
                )
                if sample_ids
                else ()
            )
            blob_ids = {sample.blob_id for sample in samples}
            blob_ids.update(item.blob_id for item in source_snapshot.observations)
            blob_ids.update(row.blob_id for row in media_assets.values())
            blobs = (
                {
                    row.id: row
                    for row in await session.scalars(
                        select(BlobRow).where(BlobRow.id.in_(tuple(blob_ids)))
                    )
                }
                if blob_ids
                else {}
            )

    investigation_created_at = investigation_row.created_at
    investigation_id_text = str(investigation_id)
    envelope_allowed, envelope_restrictions = _external_archive_policy(
        envelope, subject_tlp, source_snapshot.analyst_decisions
    )
    samples_by_sha: dict[str, SampleRow] = {}
    ambiguous_sample_shas: set[str] = set()
    for sample in samples:
        blob = blobs.get(sample.blob_id)
        if blob is None:
            continue
        if blob.sha256 in samples_by_sha:
            ambiguous_sample_shas.add(blob.sha256)
        else:
            samples_by_sha[blob.sha256] = sample
    events_by_sample: defaultdict[UUID, list[ProvenanceEventRow]] = defaultdict(list)
    for event in provenance_events:
        events_by_sample[event.aggregate_id].append(event)
    associations = tuple(source_snapshot.sample_associations)
    association_by_sha = {item.sample_sha256: item for item in associations}
    captured_shas = {
        item.sha256
        for item in source_snapshot.observations
        if item.source == _NETWORK_CAPTURE_SOURCE
        and item.object_type == "network_capture"
        and item.object_id == item.sha256
    }
    assets: list[AnnexAsset] = []

    for opinion, figure in opinion_figures:
        media_asset = media_assets.get(figure.asset_id)
        restrictions: tuple[str, ...] = ()
        figure_content: bytes | None = None
        availability = "restricted"
        if media_asset is None:
            availability = "unavailable"
            restrictions = ("canonical_media_asset_manifest_absent",)
        elif (
            media_asset.sha256 != figure.asset_sha256
            or media_asset.kind != "diagram_svg"
            or media_asset.mime_type != "image/svg+xml"
        ):
            availability = "unavailable"
            restrictions = ("canonical_figure_manifest_mismatch",)
        elif not include_restricted_archive:
            restrictions = ("explicit_restricted_archive_flag_required",)
        elif not envelope_allowed:
            restrictions = envelope_restrictions or ("canonical_policy_denied",)
        else:
            figure_content, read_failure = await _read_canonical_blob(
                artifact_store,
                media_asset.blob_id,
                figure.asset_sha256,
                max_bytes=MAX_MEDIA_ASSET_READ_BYTES,
            )
            if figure_content is not None and len(figure_content) != media_asset.byte_size:
                figure_content = None
                read_failure = "canonical_figure_size_mismatch"
            if figure_content is None:
                availability = "unavailable"
                restrictions = (read_failure or "canonical_figure_unavailable",)
            else:
                availability = "present"
        assets.append(
            AnnexAsset(
                canonical_id=f"{opinion.id}:figure:{figure.asset_id}",
                type="technical_figure",
                investigation_id=investigation_id_text,
                version=opinion.version,
                sha256=figure.asset_sha256,
                source="media_assets+blobs",
                origin="analyst_opinion.protocol_figures",
                references=(
                    str(opinion.id),
                    str(figure.asset_id),
                    figure.model_sha256,
                    figure.spec_sha256,
                    figure.preview_sha256,
                    figure.pin_sha256,
                ),
                availability=availability,
                restrictions=restrictions,
                content=figure_content,
                filename=(f"opinion-{opinion.id}-v{opinion.version}-{figure.kind.value}.svg"),
                metadata={
                    "archive_export_allowed": envelope_allowed,
                    "figure_kind": figure.kind.value,
                    "media_kind": media_asset.kind if media_asset is not None else None,
                    "compiler_name": (
                        media_asset.compiler_name if media_asset is not None else None
                    ),
                    "compiler_version": (
                        media_asset.compiler_version if media_asset is not None else None
                    ),
                    "policy_version": (
                        media_asset.policy_version if media_asset is not None else None
                    ),
                    "status": _metadata_text(opinion.status),
                    "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                },
            )
        )

    # YARA candidates are append-only and the repository snapshot contains every version.
    for candidate in source_snapshot.detection_candidates:
        candidate_id = str(candidate.candidate_id)
        candidate_version_id = str(candidate.id)
        test_refs = tuple(sorted(str(item) for item in candidate.test_refs))
        parent_refs = (str(candidate.parent_version_id),) if candidate.parent_version_id else ()
        assets.append(
            AnnexAsset(
                canonical_id=candidate_version_id,
                type="yara_rule",
                investigation_id=investigation_id_text,
                version=candidate.version,
                sha256=candidate.source_sha256,
                source="detection_candidate_versions.source",
                origin="canonical_detection_candidate_versions",
                references=(candidate_id, *parent_refs, *test_refs),
                content=candidate.source.encode("utf-8"),
                metadata={
                    "candidate_id": candidate_id,
                    "candidate_status": _candidate_status(candidate),
                    "author": None,
                    "author_status": "not_recorded_in_canonical_candidate",
                    "rule_kind": "yara",
                    "test_refs": test_refs,
                    "external_llm_allowed": envelope_allowed,
                    "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                },
            )
        )

    association_qualification = {
        item.sample_sha256: _metadata_text(item.qualification) for item in associations
    }
    for sample in samples:
        blob = blobs.get(sample.blob_id)
        if blob is None:
            continue
        sample_sha = blob.sha256
        association = association_by_sha.get(sample_sha)
        if association is None:
            continue
        qualification = (
            _metadata_text(association.qualification) if association is not None else "unassociated"
        )
        is_capture = (
            sample_sha in captured_shas or blob.logical_bucket == "network-captures-quarantine"
        )
        object_type = "network_capture" if is_capture else "sample"
        archive_allowed, restrictions = _sample_policy(
            sample,
            qualification,
            envelope_allowed=envelope_allowed,
            envelope_restrictions=envelope_restrictions,
        )
        if sample_sha in ambiguous_sample_shas:
            archive_allowed = False
            restrictions = tuple(
                sorted(set(restrictions) | {"sample_hash_multiple_subject_records"})
            )
        content: bytes | None = None
        availability = "present"
        if include_restricted_archive and archive_allowed:
            content, read_failure = await _read_canonical_blob(
                artifact_store, sample.blob_id, sample_sha, max_bytes=None
            )
            if read_failure is not None:
                availability = "unavailable"
                restrictions = tuple(sorted(set(restrictions) | {read_failure}))
        elif not archive_allowed:
            availability = "restricted"
        sample_events = tuple(events_by_sample.get(sample.id, ()))
        event_refs = tuple(str(event.id) for event in sample_events)
        vt_search_refs = tuple(
            str(item.id)
            for item in source_snapshot.observations
            if item.source.casefold() == "virustotal" and item.object_id == sample_sha
        )
        capture_observation_refs = tuple(
            str(item.id)
            for item in source_snapshot.observations
            if item.source == _NETWORK_CAPTURE_SOURCE
            and item.object_type == "network_capture"
            and item.object_id == sample_sha
        )
        refs = (
            *((str(association.id),) if association is not None else (str(sample.subject_id),)),
            *event_refs,
            *vt_search_refs,
            *capture_observation_refs,
        )
        acquisition_events = tuple(sorted({event.event_type for event in sample_events}))
        assets.append(
            AnnexAsset(
                canonical_id=str(sample.id),
                type=object_type,
                investigation_id=investigation_id_text,
                sha256=sample_sha,
                source="samples+blobs",
                origin=_metadata_text(sample.origin),
                references=refs,
                availability=availability,
                restrictions=restrictions,
                content=content,
                filename=f"{sample_sha}.bin",
                metadata={
                    "archive_export_allowed": archive_allowed,
                    "external_llm_allowed": sample.external_llm_allowed,
                    "license_restriction_present": bool(sample.license_restriction),
                    "origin_kind": sample.origin_kind,
                    "qualification": qualification,
                    "sample_state": sample.state,
                    "source_service": sample.source_service,
                    "source_object_id": sample.source_object_id,
                    "acquisition_events": acquisition_events,
                    "tlp": sample.tlp,
                },
            )
        )
        for event in sample_events:
            note = _sample_provenance_note(event)
            if note is None:
                continue
            note_bytes = json.dumps(
                note, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            event_tlp_red = _string(event.tlp) == TLPLevel.RED.value
            note_archive_allowed = archive_allowed and not event_tlp_red
            note_available = include_restricted_archive and note_archive_allowed
            note_restrictions = (
                ()
                if note_archive_allowed
                else tuple(
                    sorted(
                        set(restrictions)
                        | ({"provenance_event_tlp_red"} if event_tlp_red else set())
                    )
                )
            )
            event_refs_for_note = [str(event.id), str(sample.id)]
            parent_sha = event.payload.get("parent_sha256")
            if isinstance(parent_sha, str):
                event_refs_for_note.append(parent_sha)
            assets.append(
                AnnexAsset(
                    canonical_id=str(event.id),
                    type="technical_note",
                    investigation_id=investigation_id_text,
                    sha256=hashlib.sha256(note_bytes).hexdigest(),
                    source="provenance_events",
                    origin=event.event_type,
                    references=tuple(event_refs_for_note),
                    availability="present" if note_available else "restricted",
                    restrictions=note_restrictions,
                    content=note_bytes,
                    filename=f"sample-provenance-{event.id}.json",
                    metadata={
                        "archive_export_allowed": note_archive_allowed,
                        "status": event.event_type,
                        "source_service": sample.source_service,
                        "source_object_id": sample.source_object_id,
                        "tlp": event.tlp,
                    },
                )
            )
        acquired_during = _sample_acquisition_status(
            sample,
            investigation_created_at,
            investigation_id=investigation_id,
            provenance_events=sample_events,
            association_id=association.id,
        )
        if sample_sha in ambiguous_sample_shas:
            acquired_during = "acquired_ambiguous_subject_records"
        assets.append(
            AnnexAsset(
                canonical_id=f"association:{association.id}"
                if association
                else f"sample:{sample.id}",
                type="discovered_sample",
                investigation_id=investigation_id_text,
                sha256=sample_sha,
                source="sample_association+samples",
                origin=_metadata_text(association.origin)
                if association
                else _metadata_text(sample.origin),
                references=refs,
                metadata={
                    "value": sample_sha,
                    "status": (
                        "rejected_out_of_cluster"
                        if qualification == "rejected"
                        else acquired_during
                    ),
                    "origin_kind": _metadata_text(sample.origin_kind),
                    "qualification": qualification,
                    "source_service": sample.source_service,
                    "source_object_id": sample.source_object_id,
                    "acquisition_events": acquisition_events,
                    "relation": "investigation_sample_association",
                    "pivot_criteria": "canonical_sample_association_and_acquisition_time",
                },
            )
        )

    for observation in source_snapshot.observations:
        if (
            observation.source != _NETWORK_CAPTURE_SOURCE
            or observation.object_type != "network_capture"
            or observation.object_id != observation.sha256
            or observation.sha256 in association_by_sha
        ):
            continue
        assets.append(
            AnnexAsset(
                canonical_id=str(observation.id),
                type="network_capture",
                investigation_id=investigation_id_text,
                sha256=observation.sha256,
                source="evidence_observations.blob_id",
                origin=observation.source,
                references=(str(observation.id),),
                availability="unavailable",
                restrictions=("sample_policy_metadata_unavailable",),
                metadata={"archive_export_allowed": False, "external_llm_allowed": False},
            )
        )

    # Associations retain technical relations separately from family conclusions.
    for association in associations:
        if association.sample_sha256 not in samples_by_sha:
            qualification = _metadata_text(association.qualification)
            assets.append(
                AnnexAsset(
                    canonical_id=f"association:{association.id}:unacquired",
                    type="discovered_sample",
                    investigation_id=investigation_id_text,
                    sha256=association.sample_sha256,
                    source="sample_associations.sample_sha256",
                    origin=_metadata_text(association.origin),
                    references=(str(association.id),),
                    metadata={
                        "value": association.sample_sha256,
                        "status": "rejected_out_of_cluster"
                        if qualification == "rejected"
                        else "not_acquired",
                        "origin_kind": _metadata_text(association.origin),
                        "qualification": qualification,
                        "relation": "investigation_sample_association",
                        "pivot_criteria": "canonical_sample_association_without_sample_record",
                    },
                )
            )
        for index, related_sha in enumerate(sorted(association.related_sample_sha256)):
            known_sample = samples_by_sha.get(related_sha)
            related_association = association_by_sha.get(related_sha)
            status = _sample_acquisition_status(
                known_sample,
                investigation_created_at,
                investigation_id=investigation_id,
                provenance_events=(
                    events_by_sample.get(known_sample.id, ()) if known_sample is not None else ()
                ),
                association_id=related_association.id if related_association is not None else None,
            )
            qualification = association_qualification.get(related_sha, "unknown")
            if related_sha in ambiguous_sample_shas:
                status = "acquired_ambiguous_subject_records"
            if qualification == "rejected":
                status = "rejected_out_of_cluster"
            assets.append(
                AnnexAsset(
                    canonical_id=f"{association.id}:related:{index:03d}",
                    type="discovered_sample",
                    investigation_id=investigation_id_text,
                    sha256=related_sha,
                    source="sample_association_versions.related_sample_sha256",
                    origin=_metadata_text(association.origin),
                    references=(str(association.id), association.sample_sha256),
                    metadata={
                        "value": related_sha,
                        "status": status,
                        "origin_kind": _metadata_text(association.origin),
                        "qualification": qualification,
                        "source_service": (
                            known_sample.source_service
                            if known_sample is not None and related_sha not in ambiguous_sample_shas
                            else None
                        ),
                        "source_object_id": (
                            known_sample.source_object_id
                            if known_sample is not None and related_sha not in ambiguous_sample_shas
                            else None
                        ),
                        "acquisition_events": tuple(
                            sorted(
                                {
                                    event.event_type
                                    for event in events_by_sample.get(known_sample.id, ())
                                }
                            )
                        )
                        if known_sample is not None and related_sha not in ambiguous_sample_shas
                        else (),
                        "related_to": association.sample_sha256,
                        "relation": "reported_related_to_not_family_proof",
                        "pivot_criteria": "stored_related_sample_sha256",
                    },
                )
            )

    # Read only bounded canonical JSON observations; quarantine captures remain opaque here.
    observation_payloads: dict[UUID, tuple[bytes | None, Mapping[str, Any], str | None]] = {}
    for observation in source_snapshot.observations:
        blob = blobs.get(observation.blob_id)
        if blob is None:
            observation_payloads[observation.id] = (None, {}, "blob_record_absent")
            continue
        if (
            observation.source == _NETWORK_CAPTURE_SOURCE
            and observation.object_type == "network_capture"
        ):
            observation_payloads[observation.id] = (None, {}, None)
            continue
        raw, failure = await _read_canonical_blob(
            artifact_store,
            observation.blob_id,
            observation.sha256,
            max_bytes=MAX_EVIDENCE_JSON_BYTES,
        )
        payload: Mapping[str, Any] = {}
        if raw is not None:
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                failure = "canonical_payload_not_json"
            else:
                payload = _as_mapping(decoded)
        observation_payloads[observation.id] = (raw, payload, failure)
        assets.append(
            AnnexAsset(
                canonical_id=str(observation.id),
                type="evidence_observation",
                investigation_id=investigation_id_text,
                version=observation.source_version,
                sha256=observation.sha256,
                source=observation.source,
                origin=observation.object_type,
                references=(str(observation.invocation_ref),) if observation.invocation_ref else (),
                availability="unavailable" if failure else "present",
                restrictions=(failure,) if failure else (),
            )
        )
        if (
            observation.source == "AutoWork RuleWorkbench"
            and observation.object_type == "detection_candidate_version"
            and (failure is not None or payload.get("schema") != "investigation-yara-test.v1")
        ):
            assets.append(
                AnnexAsset(
                    canonical_id=str(observation.id),
                    type="yara_test_report",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.object_type,
                    references=(observation.object_id,),
                    availability="unavailable",
                    restrictions=(failure or "canonical_yara_test_report_schema_unavailable",),
                    metadata={"rule_kind": "yara", "status": "canonical_report_unavailable"},
                )
            )
        if observation.object_type == "protocol_model" and (
            failure is not None or payload.get("schema") != "ProtocolModelV1"
        ):
            assets.append(
                AnnexAsset(
                    canonical_id=str(observation.id),
                    type="protocol_note",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin="protocol_model_observation",
                    references=(str(observation.id),),
                    availability="unavailable",
                    restrictions=(failure or "protocol_model_schema_unavailable",),
                    metadata={"archive_export_allowed": False, "status": "unavailable"},
                )
            )
        if observation.source in {
            "network.suricata_validate_rules",
            "network.suricata_replay",
        } and (failure is not None or payload.get("schema") != "SuricataObservationV1"):
            assets.append(
                AnnexAsset(
                    canonical_id=str(observation.id),
                    type="suricata_test_report",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.source_version,
                    references=(str(observation.id),),
                    availability="unavailable",
                    restrictions=(failure or "suricata_result_schema_unavailable",),
                    metadata={
                        "rule_kind": "suricata",
                        "engine_status": "unavailable",
                        "rule_syntax_validation": "unavailable",
                        "replay_status": "unavailable",
                    },
                )
            )

    # Use only typed advisor output fields for Suricata rules; raw prompts and prose are omitted.
    suricata_by_sha: dict[str, list[AnnexAsset]] = defaultdict(list)
    for observation in source_snapshot.observations:
        if observation.source != "advisor.consult" or observation.object_id != "suricata_draft":
            continue
        raw, payload, failure = observation_payloads.get(
            observation.id, (None, {}, "blob_unavailable")
        )
        output = _as_mapping(payload.get("output"))
        source = output.get("rule_source")
        if not isinstance(source, str) or not source:
            digest = _safe_sha256(output.get("rule_source_sha256"))
            if digest is not None:
                assets.append(
                    AnnexAsset(
                        canonical_id=str(observation.id),
                        type="suricata_rule",
                        investigation_id=investigation_id_text,
                        sha256=digest,
                        source="advisor_call_payload.output.rule_source",
                        origin="advisor.consult:suricata_draft",
                        references=(str(observation.id),),
                        availability="unavailable",
                        restrictions=("canonical_rule_source_unavailable",),
                        metadata={
                            "candidate_status": "experimental",
                            "rule_kind": "suricata",
                            "rule_syntax_validation": "not_tested",
                            "replay_status": "not_tested",
                        },
                    )
                )
            else:
                assets.append(
                    AnnexAsset(
                        canonical_id=str(observation.id),
                        type="suricata_rule",
                        investigation_id=investigation_id_text,
                        source="advisor_call_payload.output.rule_source",
                        origin="advisor.consult:suricata_draft",
                        references=(str(observation.id),),
                        availability="unavailable",
                        restrictions=(failure or "canonical_rule_source_not_recorded",),
                        metadata={
                            "candidate_status": "experimental",
                            "rule_kind": "suricata",
                            "rule_syntax_validation": "not_tested",
                            "replay_status": "not_tested",
                        },
                    )
                )
            continue
        content = source.encode("utf-8")
        digest = (
            _safe_sha256(output.get("rule_source_sha256")) or hashlib.sha256(content).hexdigest()
        )
        matches = hashlib.sha256(content).hexdigest() == digest
        candidate_provenance = _as_mapping(output.get("candidate_provenance"))
        fact_refs = output.get("fact_refs")
        if not isinstance(fact_refs, list):
            bridge_provenance = _as_mapping(output.get("bridge_provenance"))
            fact_refs = bridge_provenance.get("input_refs", [])
        if not isinstance(fact_refs, list):
            fact_refs = []
        candidate_version = output.get("candidate_version")
        asset = AnnexAsset(
            canonical_id=str(observation.id),
            type="suricata_rule",
            investigation_id=investigation_id_text,
            version=candidate_version if isinstance(candidate_version, str | int) else None,
            sha256=digest,
            source="advisor_call_payload.output.rule_source",
            origin="advisor.consult:suricata_draft",
            references=tuple([str(observation.id), *(str(item) for item in fact_refs)]),
            availability="present" if matches and failure is None else "unavailable",
            restrictions=()
            if matches and failure is None
            else ("rule_source_digest_unavailable_or_mismatched",),
            content=content if matches and failure is None else None,
            metadata={
                "author": output.get("author"),
                "author_status": "not_recorded_in_canonical_candidate"
                if not output.get("author")
                else "recorded",
                "candidate_id": candidate_provenance.get("candidate_id")
                or output.get("candidate_id"),
                "candidate_status": "experimental"
                if output.get("canonicalization_eligible") is not True
                else "candidate",
                "fact_refs": tuple(str(item) for item in fact_refs),
                "rule_kind": "suricata",
                "rule_syntax_validation": "not_tested",
                "replay_status": "not_tested",
                "external_llm_allowed": envelope_allowed,
                "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
            },
        )
        assets.append(asset)
        suricata_by_sha[digest].append(asset)

    for observation in source_snapshot.observations:
        raw, payload, failure = observation_payloads.get(
            observation.id, (None, {}, "blob_unavailable")
        )
        schema = payload.get("schema")
        if (
            schema == "investigation-yara-test.v1"
            and observation.source == "AutoWork RuleWorkbench"
            and observation.object_type == "detection_candidate_version"
            and raw is not None
            and failure is None
        ):
            version_ref = payload.get("version_id")
            candidate_version = payload.get("rule_version_id") or version_ref
            references = [
                str(value) for value in (candidate_version, payload.get("candidate_id")) if value
            ]
            test_refs = payload.get("test_refs", ())
            if isinstance(test_refs, list | tuple):
                references.extend(str(value) for value in test_refs)
            assets.append(
                AnnexAsset(
                    canonical_id=str(observation.id),
                    type="yara_test_report",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.object_type,
                    references=tuple(references),
                    content=raw,
                    metadata={
                        "candidate_id": payload.get("candidate_id"),
                        "candidate_status": payload.get("candidate_status"),
                        "rule_kind": "yara",
                        "status": payload.get("final_status") or payload.get("validation_status"),
                        "test_refs": tuple(str(value) for value in test_refs)
                        if isinstance(test_refs, list | tuple)
                        else (),
                        "external_llm_allowed": envelope_allowed,
                        "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                    },
                )
            )
        if (
            schema == "SuricataObservationV1"
            and observation.source in {"network.suricata_validate_rules", "network.suricata_replay"}
            and raw is not None
            and failure is None
        ):
            engine = _as_mapping(payload.get("engine"))
            rule_sha = _safe_sha256(engine.get("rule_sha256"))
            matched: Sequence[AnnexAsset] = suricata_by_sha.get(rule_sha or "", ())
            if rule_sha is not None and not matched:
                candidate_provenance = _as_mapping(payload.get("candidate_provenance"))
                unavailable_rule = AnnexAsset(
                    canonical_id=f"unavailable-rule:{observation.id}",
                    type="suricata_rule",
                    investigation_id=investigation_id_text,
                    sha256=rule_sha,
                    source="SuricataObservationV1.engine.rule_sha256",
                    origin=observation.source,
                    references=(str(observation.id),),
                    availability="unavailable",
                    restrictions=("canonical_rule_source_not_persisted",),
                    metadata={
                        "author_status": "unrecorded",
                        "candidate_status": "unknown",
                        "candidate_id": candidate_provenance.get("candidate_id"),
                        "rule_kind": "suricata",
                        "rule_syntax_validation": "not_tested",
                        "replay_status": "not_tested",
                    },
                )
                assets.append(unavailable_rule)
                suricata_by_sha[rule_sha] = [unavailable_rule]
                matched = (unavailable_rule,)
            references = [str(observation.id)]
            capture_sha = _safe_sha256(payload.get("capture_sha256"))
            capture_blob_id = payload.get("capture_blob_id")
            if capture_sha is not None:
                references.append(capture_sha)
            if isinstance(capture_blob_id, str):
                references.append(capture_blob_id)
            references.extend(item.canonical_id for item in matched)
            is_replay = observation.source.endswith("suricata_replay")
            engine_status = str(engine.get("status", "unknown")).lower()
            syntax = (
                "valid"
                if not is_replay and engine_status == "succeeded"
                else "partial"
                if not is_replay and engine_status == "partial"
                else "failed"
                if not is_replay
                else "not_assessed"
            )
            replay_status = (
                "single_capture_replay_only"
                if is_replay and engine_status in {"succeeded", "partial"}
                else "failed"
                if is_replay
                else "not_tested"
            )
            if matched:
                for rule in matched:
                    updated_metadata = dict(rule.metadata)
                    if not is_replay:
                        updated_metadata["rule_syntax_validation"] = syntax
                    else:
                        updated_metadata["replay_status"] = replay_status
                    index = next(i for i, item in enumerate(assets) if item is rule)
                    updated_rule = replace(rule, metadata=updated_metadata)
                    assets[index] = updated_rule
                    suricata_by_sha[rule_sha or ""] = [
                        updated_rule if item is rule else item
                        for item in suricata_by_sha[rule_sha or ""]
                    ]
            assets.append(
                AnnexAsset(
                    canonical_id=str(observation.id),
                    type="suricata_test_report",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.source_version,
                    references=tuple(references),
                    content=raw,
                    metadata={
                        "candidate_id": (payload.get("candidate_provenance") or {}).get(
                            "candidate_id"
                        )
                        if isinstance(payload.get("candidate_provenance"), Mapping)
                        else None,
                        "engine_status": engine_status,
                        "rule_kind": "suricata",
                        "rule_syntax_validation": syntax,
                        "replay_status": replay_status,
                        "detection_result": "single_capture_replay_only"
                        if is_replay and engine_status in {"succeeded", "partial"}
                        else "not_assessed",
                        "detection_empirically_validated": False,
                        "external_llm_allowed": envelope_allowed,
                        "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                    },
                )
            )

    # VirusTotal summary observations and sample relations are represented as typed indicators.
    for observation in source_snapshot.observations:
        raw, payload, failure = observation_payloads.get(
            observation.id, (None, {}, "blob_unavailable")
        )
        if failure or not raw or observation.source.casefold() != "virustotal":
            continue
        schema = payload.get("schema")
        if schema not in {"virustotal-investigation-summary.v1", "virustotal-object-report.v1"}:
            continue
        for index, (ioc_type, value) in enumerate(
            _vt_indicators(payload, observation.object_type, observation.object_id)
        ):
            assets.append(
                AnnexAsset(
                    canonical_id=f"{observation.id}:ioc:{index:03d}",
                    type="observed_ioc",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.object_type,
                    references=(str(observation.id),),
                    metadata={
                        "ioc_type": ioc_type,
                        "value": value,
                        "relation": "observed_in_virustotal_response",
                        "external_llm_allowed": envelope_allowed,
                        "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                    },
                )
            )
            if ioc_type == "file" and len(value) == 64:
                known = samples_by_sha.get(value)
                known_association = association_by_sha.get(value)
                known_events = events_by_sample.get(known.id, ()) if known is not None else ()
                assets.append(
                    AnnexAsset(
                        canonical_id=f"{observation.id}:discovery:{index:03d}",
                        type="discovered_sample",
                        investigation_id=investigation_id_text,
                        sha256=value,
                        source=observation.source,
                        origin=observation.object_type,
                        references=(str(observation.id),),
                        metadata={
                            "value": value,
                            "status": (
                                "acquired_ambiguous_subject_records"
                                if value in ambiguous_sample_shas
                                else _sample_acquisition_status(
                                    known,
                                    investigation_created_at,
                                    investigation_id=investigation_id,
                                    provenance_events=known_events,
                                    association_id=(
                                        known_association.id
                                        if known_association is not None
                                        else None
                                    ),
                                )
                            ),
                            "origin_kind": "virustotal_observation",
                            "qualification": association_qualification.get(value, "unknown"),
                            "source_service": (
                                known.source_service
                                if known is not None and value not in ambiguous_sample_shas
                                else None
                            ),
                            "source_object_id": (
                                known.source_object_id
                                if known is not None and value not in ambiguous_sample_shas
                                else None
                            ),
                            "acquisition_events": tuple(
                                sorted({event.event_type for event in known_events})
                            )
                            if value not in ambiguous_sample_shas
                            else (),
                            "related_to": observation.object_id,
                            "relation": "virustotal_candidate_not_family_proof",
                            "pivot_criteria": "canonical_virustotal_summary_candidate",
                        },
                    )
                )

    # Preserve protocol observations and test findings as restricted notes, never as public prose.
    for observation in source_snapshot.observations:
        raw, payload, failure = observation_payloads.get(
            observation.id, (None, {}, "blob_unavailable")
        )
        if (
            observation.object_type != "protocol_model"
            or payload.get("schema") != "ProtocolModelV1"
        ):
            continue
        summary = _as_mapping(payload.get("summary"))
        model_blob_text = payload.get("model_blob_id")
        model_sha = _safe_sha256(payload.get("model_sha256"))
        try:
            model_blob_id = UUID(str(model_blob_text))
        except (ValueError, TypeError):
            model_blob_id = None
        model_raw: bytes | None = None
        model_failure: str | None = None
        model_availability = "restricted"
        if model_blob_id is None or model_sha is None:
            model_availability = "unavailable"
            model_failure = "protocol_model_reference_invalid"
        elif not include_restricted_archive:
            model_failure = "explicit_restricted_archive_flag_required"
        elif not envelope_allowed:
            model_failure = ",".join(envelope_restrictions) or "canonical_policy_denied"
        else:
            model_raw, model_failure = await _read_canonical_blob(
                artifact_store, model_blob_id, model_sha, max_bytes=MAX_EVIDENCE_JSON_BYTES
            )
            model_availability = "present" if model_raw is not None else "unavailable"
        assets.append(
            AnnexAsset(
                canonical_id=str(model_blob_id or observation.id),
                type="protocol_note",
                investigation_id=investigation_id_text,
                version=str(payload.get("schema") or "unknown"),
                sha256=model_sha,
                source="protocol_model_blob",
                origin="canonical_evidence_observation",
                references=(str(observation.id),),
                availability=model_availability,
                restrictions=(model_failure,) if model_failure else (),
                content=model_raw,
                filename=f"protocol-{observation.id}.json",
                metadata={
                    "archive_export_allowed": envelope_allowed,
                    "status": summary.get("validation_status") or "unavailable",
                    "fact_refs": (str(observation.id),),
                    "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                },
            )
        )

    for experiment in source_snapshot.experiments:
        tool_name = str(experiment.tool_name).casefold()
        if "network" not in tool_name and "suricata" not in tool_name:
            continue
        content = json.dumps(
            _experiment_note(experiment),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assets.append(
            AnnexAsset(
                canonical_id=str(experiment.id),
                type="technical_note",
                investigation_id=investigation_id_text,
                version=experiment.version,
                source="investigation_experiments",
                origin=experiment.tool_name,
                references=tuple(str(item.observation_id) for item in experiment.observation_refs),
                sha256=hashlib.sha256(content).hexdigest(),
                availability="present" if envelope_allowed else "restricted",
                restrictions=() if envelope_allowed else envelope_restrictions,
                content=content,
                filename=f"experiment-{experiment.id}.json",
                metadata={
                    "archive_export_allowed": envelope_allowed,
                    "status": _metadata_text(experiment.verdict),
                    "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                },
            )
        )

    for finding in source_snapshot.findings:
        if _metadata_text(finding.kind) != "network_behaviour":
            continue
        content = json.dumps(
            _finding_note(finding), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assets.append(
            AnnexAsset(
                canonical_id=str(finding.id),
                type="technical_note",
                investigation_id=investigation_id_text,
                version=finding.version,
                source="investigation_finding_versions",
                origin="network_behaviour_finding",
                references=tuple(str(item) for item in finding.observation_refs),
                sha256=hashlib.sha256(content).hexdigest(),
                availability="present" if envelope_allowed else "restricted",
                restrictions=() if envelope_allowed else envelope_restrictions,
                content=content,
                filename=f"finding-{finding.id}.json",
                metadata={
                    "archive_export_allowed": envelope_allowed,
                    "status": _metadata_text(finding.assessment),
                    "tlp": _string(getattr(getattr(envelope, "disclosure", None), "tlp", None)),
                },
            )
        )

    yara_report_refs = {
        reference
        for asset in assets
        if asset.type == "yara_test_report" and asset.availability != "absent"
        for reference in asset.references
    }
    for candidate in source_snapshot.detection_candidates:
        if str(candidate.id) in yara_report_refs:
            continue
        assets.append(
            AnnexAsset(
                canonical_id=f"absent:yara-test-report:{candidate.id}",
                type="yara_test_report",
                investigation_id=investigation_id_text,
                version=candidate.version,
                source="canonical_evidence_observations",
                origin="no_test_report_recorded_for_candidate_version",
                references=(str(candidate.id), str(candidate.candidate_id)),
                availability="absent",
                metadata={
                    "candidate_id": str(candidate.candidate_id),
                    "candidate_status": _candidate_status(candidate),
                    "rule_kind": "yara",
                    "status": "not_tested_or_report_absent",
                    "test_refs": tuple(str(item) for item in candidate.test_refs),
                },
            )
        )

    suricata_report_refs = {
        reference
        for asset in assets
        if asset.type == "suricata_test_report" and asset.availability != "absent"
        for reference in asset.references
    }
    for rule in tuple(asset for asset in assets if asset.type == "suricata_rule"):
        if rule.canonical_id in suricata_report_refs:
            continue
        assets.append(
            AnnexAsset(
                canonical_id=f"absent:suricata-test-report:{rule.canonical_id}",
                type="suricata_test_report",
                investigation_id=investigation_id_text,
                version=rule.version,
                source="canonical_evidence_observations",
                origin="no_validation_or_replay_observation_for_rule",
                references=(rule.canonical_id,),
                availability="absent",
                metadata={
                    "candidate_id": rule.metadata.get("candidate_id"),
                    "engine_status": "not_tested",
                    "rule_kind": "suricata",
                    "rule_syntax_validation": "not_tested",
                    "replay_status": "not_tested",
                    "detection_result": "not_assessed",
                    "detection_empirically_validated": False,
                },
            )
        )

    return InvestigationAnnexSnapshot(investigation_id=investigation_id_text, assets=tuple(assets))
