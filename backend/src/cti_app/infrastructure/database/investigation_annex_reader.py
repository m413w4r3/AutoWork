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

from sqlalchemy import or_, select, text
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
from cti_app.domain.investigation import DetectionCandidateVersion, TLPLevel
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
_RULE_TOOL_PREFIX = "rule."
_RULE_ATTRIBUTION_KEYS = ("author", "origin_author", "source_author", "attribution")
_RULE_ATTRIBUTION_RATIONALE_PREFIX = "rule-workbench-motif-v1:"


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


def _rule_attribution_from_rationale(candidate: DetectionCandidateVersion) -> dict[str, Any]:
    """Read the structured source attribution stored with canonical motif provenance."""
    for motif in candidate.motif_provenance:
        rationale = motif.rationale
        if not rationale.startswith(_RULE_ATTRIBUTION_RATIONALE_PREFIX):
            continue
        try:
            payload = json.loads(rationale[len(_RULE_ATTRIBUTION_RATIONALE_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attribution = _as_mapping(_as_mapping(payload).get("source_attribution"))
        if attribution:
            return dict(attribution)
    return {}


def _rule_payload_attribution(payload: Any) -> dict[str, Any]:
    """Extract only explicit rule author fields from a persisted tool result."""
    root = _as_mapping(payload)
    result = _as_mapping(root.get("result"))
    sources = (result, _as_mapping(result.get("source_attribution")))
    for source in sources:
        attribution = _as_mapping(source.get("source_attribution"))
        values = dict(attribution)
        for key in _RULE_ATTRIBUTION_KEYS:
            if key in source:
                values[key] = source[key]
        if values:
            return values
    return {}


def _rule_author(attribution: Mapping[str, Any]) -> str | None:
    for key in ("author", "source_author", "origin_author"):
        value = attribution.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _rule_origin(tool_name: str | None, attribution: Mapping[str, Any]) -> str | None:
    change_kind = attribution.get("change_kind")
    if isinstance(change_kind, str):
        if change_kind == "bridge_yara_draft":
            return "bridge"
        if change_kind.startswith("operator_"):
            return "operator"
    if tool_name == "rule.propose_bridge":
        return "bridge"
    if tool_name == "rule.operator_syntax_fix":
        return "operator"
    return "tool" if tool_name and tool_name.startswith(_RULE_TOOL_PREFIX) else None


async def _yara_version_origin_metadata(
    candidate: DetectionCandidateVersion,
    *,
    observations: Sequence[Any],
    invocations: Sequence[Any],
    artifact_store: ProductionArtifactStore,
) -> dict[str, Any]:
    """Resolve version attribution through canonical provenance, verified blobs and invocations."""
    observation_by_id = {item.id: item for item in observations}
    invocation_by_id = {item.id: item for item in invocations}
    source_observation_ids = {
        reference for motif in candidate.motif_provenance for reference in motif.observation_refs
    }
    attribution = _rule_attribution_from_rationale(candidate)
    relevant_observations = [
        observation_by_id[reference]
        for reference in source_observation_ids
        if reference in observation_by_id
    ]
    # Only explicit attribution or a tool result that identifies this exact version can
    # establish its originating invocation. Motif observations may predate the rule.
    linked_invocation_ids: set[UUID] = set()
    for key in ("bridge_invocation_id", "origin_invocation_id"):
        value = attribution.get(key)
        if isinstance(value, str):
            try:
                linked_invocation_ids.add(UUID(value))
            except ValueError:
                pass

    # A revision's new tool-result observation may not be part of inherited motif refs.
    # Match its canonical result to this exact candidate version before using its authorship.
    rule_invocations = {
        invocation.id: invocation
        for invocation in invocations
        if invocation.tool_name.startswith(_RULE_TOOL_PREFIX)
    }
    for observation in observations:
        invocation_id = observation.invocation_ref
        invocation = rule_invocations.get(invocation_id)
        if invocation is None:
            continue
        content, failure = await _read_canonical_blob(
            artifact_store,
            observation.blob_id,
            observation.sha256,
            max_bytes=MAX_EVIDENCE_JSON_BYTES,
        )
        if content is None or failure is not None:
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        result = _as_mapping(_as_mapping(payload).get("result"))
        candidate_version_id = result.get("version_id")
        if candidate_version_id == str(candidate.id):
            relevant_observations.append(observation)
            source_observation_ids.add(observation.id)
            linked_invocation_ids.add(invocation_id)
            observed_attribution = _rule_payload_attribution(payload)
            if observed_attribution:
                attribution.update(observed_attribution)

    # Source refs can carry the original Bridge attribution in the observation result.
    if not attribution:
        for observation in relevant_observations:
            content, failure = await _read_canonical_blob(
                artifact_store,
                observation.blob_id,
                observation.sha256,
                max_bytes=MAX_EVIDENCE_JSON_BYTES,
            )
            if content is None or failure is not None:
                continue
            try:
                observed_attribution = _rule_payload_attribution(json.loads(content))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if observed_attribution:
                attribution.update(observed_attribution)
                break

    invocation = next(
        (
            invocation_by_id[item]
            for item in sorted(linked_invocation_ids, key=str)
            if item in invocation_by_id
            and invocation_by_id[item].tool_name.startswith(_RULE_TOOL_PREFIX)
        ),
        None,
    )
    # Prefer the invocation that returned this exact version over an older source invocation.
    version_observation_ids = {item.id for item in relevant_observations}
    invocation = next(
        (
            item
            for item in invocations
            if item.id in linked_invocation_ids
            and item.tool_name.startswith(_RULE_TOOL_PREFIX)
            and version_observation_ids.intersection(item.response_refs)
        ),
        invocation,
    )
    tool_name = invocation.tool_name if invocation is not None else None
    author = _rule_author(attribution)
    return {
        "author": author,
        "author_status": "recorded" if author is not None else "not_recorded",
        "origin": _rule_origin(tool_name, attribution),
        "origin_tool": tool_name,
        "origin_invocation_id": str(invocation.id) if invocation is not None else None,
        "source_observation_ids": tuple(sorted(str(item) for item in source_observation_ids)),
        "parent_version": str(candidate.parent_version_id)
        if candidate.parent_version_id is not None
        else None,
    }


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


def _is_tlp_red(value: object) -> bool:
    """Stored TLP spellings differ by table ("RED" for samples, "red" for envelopes)."""
    return str(value or "").strip().casefold() == TLPLevel.RED.value


def _external_archive_policy(
    envelope: Any, subject_tlp: str | None, decisions: Sequence[Any]
) -> tuple[bool, tuple[str, ...]]:
    _, public_restrictions = _public_distribution_policy(envelope, subject_tlp, decisions)
    reasons = list(public_restrictions)
    if envelope is None:
        return False, tuple(sorted(set(reasons)))
    disclosure = getattr(envelope, "disclosure", None)
    if not bool(getattr(disclosure, "external_llm_allowed", False)):
        reasons.append("external_llm_not_allowed")
    return not reasons, tuple(sorted(set(reasons)))


def _public_distribution_policy(
    envelope: Any, subject_tlp: str | None, decisions: Sequence[Any]
) -> tuple[bool, tuple[str, ...]]:
    if envelope is None:
        return False, ("g2_envelope_unavailable",)
    reasons: list[str] = []
    if g2_envelope_authorization_state(envelope, decisions).revoked:
        reasons.append("g2_envelope_revoked")
    disclosure = getattr(envelope, "disclosure", None)
    envelope_tlp = _string(getattr(disclosure, "tlp", None))
    if _is_tlp_red(envelope_tlp) or _is_tlp_red(subject_tlp):
        reasons.append("tlp_red")
    return not reasons, tuple(sorted(set(reasons)))


def _observation_public_restrictions(
    observation: Any,
    *,
    public_distribution_restrictions: Sequence[str],
    sample_public_restrictions_by_sha: Mapping[str, set[str]],
    known_sample_shas: set[str],
) -> tuple[str, ...]:
    restrictions = set(public_distribution_restrictions)
    object_sha = _safe_sha256(observation.object_id)
    if object_sha is not None:
        restrictions.update(sample_public_restrictions_by_sha.get(object_sha, set()))
        if object_sha not in known_sample_shas:
            restrictions.add("sample_policy_metadata_unavailable")
    return tuple(sorted(restrictions))


def _observation_public_distribution_allowed(
    observation: Any,
    *,
    public_distribution_restrictions: Sequence[str],
    sample_public_restrictions_by_sha: Mapping[str, set[str]],
    known_sample_shas: set[str],
) -> bool:
    return not _observation_public_restrictions(
        observation,
        public_distribution_restrictions=public_distribution_restrictions,
        sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
        known_sample_shas=known_sample_shas,
    )


def _sample_public_restrictions(
    sample: SampleRow,
    qualification: str | None,
    *,
    subject_tlp: str | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if sample.license_restriction:
        reasons.append("sample_license_restricted")
    if sample.state == "rejected":
        reasons.append("sample_rejected_or_revoked")
    if qualification == "rejected":
        reasons.append("association_rejected_or_revoked")
    if _is_tlp_red(sample.tlp) or _is_tlp_red(subject_tlp):
        reasons.append("tlp_red")
    return tuple(sorted(set(reasons)))


def _provenance_event_public_restrictions_by_sha(
    samples_by_sha: Mapping[str, Sequence[Any]],
    events_by_sample: Mapping[UUID, Sequence[Any]],
    *,
    red_event_sample_shas: Sequence[str] = (),
) -> dict[str, set[str]]:
    """Attach RED provenance restrictions only to the hash of that held sample."""
    restrictions_by_sha: dict[str, set[str]] = {}
    for sample_sha, samples in samples_by_sha.items():
        if any(
            _is_tlp_red(event.tlp)
            for sample in samples
            for event in events_by_sample.get(sample.id, ())
        ):
            restrictions_by_sha[sample_sha] = {"provenance_event_tlp_red"}
    for sample_sha in red_event_sample_shas:
        restrictions_by_sha.setdefault(sample_sha, set()).add("provenance_event_tlp_red")
    return restrictions_by_sha


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


def _resolve_public_distribution_for_assets(
    assets: Sequence[AnnexAsset],
    *,
    public_distribution_allowed: bool,
    public_distribution_restrictions: Sequence[str],
    sample_public_restrictions_by_sha: Mapping[str, set[str]],
    known_sample_shas: set[str],
    observations: Sequence[Any],
) -> list[AnnexAsset]:
    """Resolve restrictions inherited from canonical asset and sample relations."""
    observation_by_text_id = {str(item.id): item for item in observations}
    restricted_asset_reasons_by_id = {
        asset.canonical_id: set(asset.restrictions) or {"related_asset_distribution_restricted"}
        for asset in assets
        if asset.public_distribution_allowed is False
    }
    resolved_assets: list[AnnexAsset] = []
    for asset in assets:
        asset_public_restrictions = set(public_distribution_restrictions)
        if asset.type == "discovered_sample":
            candidate_sha = _safe_sha256(asset.sha256 or asset.metadata.get("value"))
            if candidate_sha is not None:
                asset_public_restrictions.update(
                    sample_public_restrictions_by_sha.get(candidate_sha, set())
                )
            allowed = asset.public_distribution_allowed
            if asset_public_restrictions or allowed is False:
                allowed = False
            elif allowed is None:
                allowed = public_distribution_allowed
            restriction_set = set(asset.restrictions)
            if not allowed:
                restriction_set.update(asset_public_restrictions)
            resolved_assets.append(
                replace(
                    asset,
                    restrictions=tuple(sorted(restriction_set)),
                    public_distribution_allowed=allowed,
                )
            )
            continue
        related_shas = set(asset.references)
        related_observation_ids = set(asset.references)
        related_asset_ids = set(asset.references)
        for metadata_key in ("value", "related_to", "source_object_id"):
            metadata_value = asset.metadata.get(metadata_key)
            if isinstance(metadata_value, str):
                related_shas.add(metadata_value)
        for metadata_key in ("source_observation_ids", "fact_refs", "test_refs", "test_report_ids"):
            metadata_values = asset.metadata.get(metadata_key)
            if isinstance(metadata_values, Sequence) and not isinstance(
                metadata_values, str | bytes
            ):
                related_observation_ids.update(
                    value for value in metadata_values if isinstance(value, str)
                )
                related_asset_ids.update(
                    value for value in metadata_values if isinstance(value, str)
                )
        for related_asset_id in related_asset_ids:
            asset_public_restrictions.update(
                restricted_asset_reasons_by_id.get(related_asset_id, set())
            )
        for related_value in related_shas:
            related_sha = _safe_sha256(related_value)
            if related_sha is not None:
                asset_public_restrictions.update(
                    sample_public_restrictions_by_sha.get(related_sha, set())
                )
        for observation_id in related_observation_ids:
            related_observation = observation_by_text_id.get(observation_id)
            if related_observation is None:
                continue
            related_sha = _safe_sha256(related_observation.object_id)
            if related_sha is None:
                continue
            asset_public_restrictions.update(
                sample_public_restrictions_by_sha.get(related_sha, set())
            )
            if (
                related_observation.object_type in {"file", "network_capture"}
                and related_sha not in known_sample_shas
            ):
                asset_public_restrictions.add("sample_policy_metadata_unavailable")
        allowed = asset.public_distribution_allowed
        if asset_public_restrictions or allowed is False:
            allowed = False
        elif allowed is None:
            allowed = public_distribution_allowed
        restriction_set = set(asset.restrictions)
        if not allowed:
            restriction_set.update(asset_public_restrictions)
        resolved_assets.append(
            replace(
                asset,
                restrictions=tuple(sorted(restriction_set)),
                public_distribution_allowed=allowed,
            )
        )
    while True:
        restricted_asset_reasons_by_id = {
            asset.canonical_id: set(asset.restrictions) or {"related_asset_distribution_restricted"}
            for asset in resolved_assets
            if asset.public_distribution_allowed is False
        }
        newly_restricted: list[AnnexAsset] = []
        changed = False
        for asset in resolved_assets:
            if asset.public_distribution_allowed is False or asset.type == "discovered_sample":
                newly_restricted.append(asset)
                continue
            related_asset_ids = set(asset.references)
            for metadata_key in (
                "source_observation_ids",
                "fact_refs",
                "test_refs",
                "test_report_ids",
            ):
                metadata_values = asset.metadata.get(metadata_key)
                if isinstance(metadata_values, Sequence) and not isinstance(
                    metadata_values, str | bytes
                ):
                    related_asset_ids.update(
                        value for value in metadata_values if isinstance(value, str)
                    )
            inherited_restrictions = set().union(
                *(
                    restricted_asset_reasons_by_id.get(related_asset_id, set())
                    for related_asset_id in related_asset_ids
                )
            )
            if inherited_restrictions:
                resolved_restrictions = set(asset.restrictions) | inherited_restrictions
                newly_restricted.append(
                    replace(
                        asset,
                        restrictions=tuple(sorted(resolved_restrictions)),
                        public_distribution_allowed=False,
                    )
                )
                changed = True
            else:
                newly_restricted.append(asset)
        resolved_assets = newly_restricted
        if not changed:
            break
    return resolved_assets


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
            sample_blob_ids = {sample.blob_id for sample in samples}
            related_sample_shas = {
                sample_sha
                for association in source_snapshot.sample_associations
                for sample_sha in (
                    association.sample_sha256,
                    *association.related_sample_sha256,
                )
            }
            related_sample_shas.update(
                sample_sha
                for observation in source_snapshot.observations
                if (sample_sha := _safe_sha256(observation.object_id)) is not None
            )
            sample_lookup = []
            if sample_blob_ids:
                sample_lookup.append(SampleRow.blob_id.in_(tuple(sample_blob_ids)))
            if related_sample_shas:
                sample_lookup.append(BlobRow.sha256.in_(tuple(related_sample_shas)))
            samples_sharing_blobs = (
                tuple(
                    await session.scalars(
                        select(SampleRow)
                        .join(BlobRow, SampleRow.blob_id == BlobRow.id)
                        .where(or_(*sample_lookup))
                        .order_by(SampleRow.subject_id, SampleRow.id)
                    )
                )
                if sample_lookup
                else ()
            )
            restricted_sample_facts = tuple(
                await session.execute(
                    select(SampleRow, BlobRow.sha256, SubjectRow.tlp)
                    .join(BlobRow, SampleRow.blob_id == BlobRow.id)
                    .join(SubjectRow, SampleRow.subject_id == SubjectRow.id)
                    .where(
                        or_(
                            SampleRow.license_restriction.is_not(None),
                            SampleRow.state == "rejected",
                            SampleRow.tlp == TLPLevel.RED.value.upper(),
                            SubjectRow.tlp == TLPLevel.RED.value.upper(),
                            SampleRow.external_llm_allowed.is_(False),
                        )
                    )
                    .order_by(BlobRow.sha256, SampleRow.subject_id, SampleRow.id)
                )
            )
            red_provenance_sample_shas = tuple(
                await session.scalars(
                    select(BlobRow.sha256)
                    .select_from(ProvenanceEventRow)
                    .join(SampleRow, ProvenanceEventRow.aggregate_id == SampleRow.id)
                    .join(BlobRow, SampleRow.blob_id == BlobRow.id)
                    .where(
                        ProvenanceEventRow.aggregate_type == "sample",
                        ProvenanceEventRow.tlp == TLPLevel.RED.value.upper(),
                    )
                    .distinct()
                    .order_by(BlobRow.sha256)
                )
            )
            sample_ids = tuple(
                sorted({sample.id for sample in (*samples, *samples_sharing_blobs)}, key=str)
            )
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
            sample_subject_ids = {sample.subject_id for sample in samples_sharing_blobs}
            subjects_by_id = (
                {
                    row.id: row
                    for row in await session.scalars(
                        select(SubjectRow).where(SubjectRow.id.in_(tuple(sample_subject_ids)))
                    )
                }
                if sample_subject_ids
                else {}
            )
            sample_subject_tlps = {key: value.tlp for key, value in subjects_by_id.items()}
            blob_ids = set(sample_blob_ids)
            blob_ids.update(sample.blob_id for sample in samples_sharing_blobs)
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
    public_distribution_allowed, public_distribution_restrictions = _public_distribution_policy(
        envelope, subject_tlp, source_snapshot.analyst_decisions
    )
    envelope_allowed, envelope_restrictions = _external_archive_policy(
        envelope, subject_tlp, source_snapshot.analyst_decisions
    )
    samples_by_sha: dict[str, SampleRow] = {}
    ambiguous_sample_shas: set[str] = set()
    samples_by_sha_all: defaultdict[str, list[SampleRow]] = defaultdict(list)
    for sample in samples_sharing_blobs:
        blob = blobs.get(sample.blob_id)
        if blob is None:
            continue
        samples_by_sha_all[blob.sha256].append(sample)
        if len(samples_by_sha_all[blob.sha256]) > 1:
            ambiguous_sample_shas.add(blob.sha256)
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
                restrictions=tuple(
                    sorted(set(restrictions) | set(public_distribution_restrictions))
                ),
                public_distribution_allowed=public_distribution_allowed,
                archive_export_allowed=envelope_allowed,
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
        origin_metadata = await _yara_version_origin_metadata(
            candidate,
            observations=source_snapshot.observations,
            invocations=source_snapshot.invocations,
            artifact_store=artifact_store,
        )
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
                    **origin_metadata,
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
    sample_public_restrictions_by_sha: defaultdict[str, set[str]] = defaultdict(set)
    sample_archive_restrictions_by_sha: defaultdict[str, set[str]] = defaultdict(set)
    for related_sample, sample_sha, related_subject_tlp in restricted_sample_facts:
        row_restrictions = set(
            _sample_public_restrictions(
                related_sample,
                None,
                subject_tlp=related_subject_tlp,
            )
        )
        sample_public_restrictions_by_sha[sample_sha].update(row_restrictions)
        sample_archive_restrictions_by_sha[sample_sha].update(row_restrictions)
        if not bool(related_sample.external_llm_allowed):
            sample_archive_restrictions_by_sha[sample_sha].add("sample_external_llm_not_allowed")
    for sample_sha, related_samples in samples_by_sha_all.items():
        for related_sample in related_samples:
            row_restrictions = set(
                _sample_public_restrictions(
                    related_sample,
                    association_qualification.get(sample_sha),
                    subject_tlp=sample_subject_tlps.get(related_sample.subject_id),
                )
            )
            sample_public_restrictions_by_sha[sample_sha].update(row_restrictions)
            sample_archive_restrictions_by_sha[sample_sha].update(row_restrictions)
            # external_llm_allowed gates archive export, not distribution to human readers.
            if not bool(related_sample.external_llm_allowed):
                sample_archive_restrictions_by_sha[sample_sha].add(
                    "sample_external_llm_not_allowed"
                )
    for sample_sha, event_restrictions in _provenance_event_public_restrictions_by_sha(
        samples_by_sha_all,
        events_by_sample,
        red_event_sample_shas=red_provenance_sample_shas,
    ).items():
        sample_public_restrictions_by_sha[sample_sha].update(event_restrictions)
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
        public_restrictions = set(public_distribution_restrictions)
        public_restrictions.update(sample_public_restrictions_by_sha[sample_sha])
        sample_archive_restrictions = set(envelope_restrictions)
        sample_archive_restrictions.update(sample_archive_restrictions_by_sha[sample_sha])
        archive_allowed = not sample_archive_restrictions
        if sample_sha in ambiguous_sample_shas:
            archive_allowed = False
            sample_archive_restrictions.add("sample_hash_multiple_subject_records")
        sample_archive_restrictions.update(public_restrictions)
        restrictions_tuple = tuple(sorted(sample_archive_restrictions))
        content: bytes | None = None
        availability = "present"
        if include_restricted_archive and archive_allowed:
            content, read_failure = await _read_canonical_blob(
                artifact_store, sample.blob_id, sample_sha, max_bytes=None
            )
            if read_failure is not None:
                availability = "unavailable"
                restrictions_tuple = tuple(sorted(set(restrictions_tuple) | {read_failure}))
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
                restrictions=restrictions_tuple,
                public_distribution_allowed=not public_restrictions,
                archive_export_allowed=archive_allowed,
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
            event_tlp_red = _is_tlp_red(event.tlp)
            note_archive_allowed = archive_allowed and not event_tlp_red
            note_available = include_restricted_archive and note_archive_allowed
            note_restrictions = (
                ()
                if note_archive_allowed
                else tuple(
                    sorted(
                        set(sample_archive_restrictions)
                        | public_restrictions
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
                    public_distribution_allowed=not public_restrictions and not event_tlp_red,
                    archive_export_allowed=note_archive_allowed,
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
                availability="restricted" if public_restrictions else "present",
                restrictions=tuple(sorted(public_restrictions)),
                public_distribution_allowed=not public_restrictions,
                archive_export_allowed=archive_allowed,
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
                restrictions=tuple(
                    sorted(
                        set(public_distribution_restrictions)
                        | sample_public_restrictions_by_sha.get(observation.sha256, set())
                        | {"sample_policy_metadata_unavailable"}
                    )
                ),
                public_distribution_allowed=False,
                archive_export_allowed=False,
                metadata={"archive_export_allowed": False, "external_llm_allowed": False},
            )
        )

    # Associations retain technical relations separately from family conclusions.
    for association in associations:
        if association.sample_sha256 not in samples_by_sha:
            qualification = _metadata_text(association.qualification)
            public_restrictions = set(public_distribution_restrictions)
            public_restrictions.update(
                sample_public_restrictions_by_sha.get(association.sample_sha256, set())
            )
            if qualification == "rejected":
                public_restrictions.add("association_rejected_or_revoked")
            archive_restrictions = set(envelope_restrictions)
            archive_restrictions.update(
                sample_archive_restrictions_by_sha.get(association.sample_sha256, set())
            )
            if qualification == "rejected":
                archive_restrictions.add("association_rejected_or_revoked")
            assets.append(
                AnnexAsset(
                    canonical_id=f"association:{association.id}:unacquired",
                    type="discovered_sample",
                    investigation_id=investigation_id_text,
                    sha256=association.sample_sha256,
                    source="sample_associations.sample_sha256",
                    origin=_metadata_text(association.origin),
                    references=(str(association.id),),
                    availability="restricted" if public_restrictions else "present",
                    restrictions=tuple(sorted(public_restrictions | archive_restrictions)),
                    public_distribution_allowed=not public_restrictions,
                    archive_export_allowed=not archive_restrictions,
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
            related_public_restrictions = set(public_distribution_restrictions)
            related_public_restrictions.update(
                sample_public_restrictions_by_sha.get(related_sha, set())
            )
            if qualification == "rejected":
                related_public_restrictions.add("association_rejected_or_revoked")
            related_archive_restrictions = set(envelope_restrictions)
            related_archive_restrictions.update(
                sample_archive_restrictions_by_sha.get(related_sha, set())
            )
            if qualification == "rejected":
                related_archive_restrictions.add("association_rejected_or_revoked")
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
                    availability=("restricted" if related_public_restrictions else "present"),
                    restrictions=tuple(
                        sorted(related_public_restrictions | related_archive_restrictions)
                    ),
                    public_distribution_allowed=not related_public_restrictions,
                    archive_export_allowed=not related_archive_restrictions,
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
                public_distribution_allowed=_observation_public_distribution_allowed(
                    observation,
                    public_distribution_restrictions=public_distribution_restrictions,
                    sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
                    known_sample_shas=set(samples_by_sha) | set(association_by_sha),
                ),
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
                    restrictions=tuple(
                        sorted(
                            set(
                                _observation_public_restrictions(
                                    observation,
                                    public_distribution_restrictions=public_distribution_restrictions,
                                    sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
                                    known_sample_shas=set(samples_by_sha) | set(association_by_sha),
                                )
                            )
                            | {failure or "protocol_model_schema_unavailable"}
                        )
                    ),
                    public_distribution_allowed=_observation_public_distribution_allowed(
                        observation,
                        public_distribution_restrictions=public_distribution_restrictions,
                        sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
                        known_sample_shas=set(samples_by_sha) | set(association_by_sha),
                    ),
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
                    public_distribution_allowed=public_distribution_allowed,
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
            ioc_public_restrictions = set(public_distribution_restrictions)
            ioc_archive_restrictions: set[str] = set()
            observed_sample_sha = _safe_sha256(observation.object_id)
            if observed_sample_sha is not None:
                ioc_public_restrictions.update(
                    sample_public_restrictions_by_sha.get(observed_sample_sha, set())
                )
                ioc_archive_restrictions.update(
                    sample_archive_restrictions_by_sha.get(observed_sample_sha, set())
                )
            if ioc_type == "file":
                ioc_public_restrictions.update(sample_public_restrictions_by_sha.get(value, set()))
                ioc_archive_restrictions.update(
                    sample_archive_restrictions_by_sha.get(value, set())
                )
            ioc_archive_restrictions.update(envelope_restrictions)
            ioc_restrictions = ioc_public_restrictions | ioc_archive_restrictions
            assets.append(
                AnnexAsset(
                    canonical_id=f"{observation.id}:ioc:{index:03d}",
                    type="observed_ioc",
                    investigation_id=investigation_id_text,
                    sha256=observation.sha256,
                    source=observation.source,
                    origin=observation.object_type,
                    references=(str(observation.id),),
                    availability=("restricted" if ioc_public_restrictions else "present"),
                    restrictions=tuple(sorted(ioc_restrictions)),
                    public_distribution_allowed=not ioc_public_restrictions,
                    archive_export_allowed=not ioc_archive_restrictions,
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
                discovery_restrictions = set(public_distribution_restrictions)
                discovery_restrictions.update(sample_public_restrictions_by_sha.get(value, set()))
                discovery_archive_restrictions = set(envelope_restrictions)
                discovery_archive_restrictions.update(
                    sample_archive_restrictions_by_sha.get(value, set())
                )
                if association_qualification.get(value) == "rejected":
                    discovery_restrictions.add("association_rejected_or_revoked")
                    discovery_archive_restrictions.add("association_rejected_or_revoked")
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
                        availability=("restricted" if discovery_restrictions else "present"),
                        restrictions=tuple(
                            sorted(discovery_restrictions | discovery_archive_restrictions)
                        ),
                        public_distribution_allowed=not discovery_restrictions,
                        archive_export_allowed=not discovery_archive_restrictions,
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
                public_distribution_allowed=_observation_public_distribution_allowed(
                    observation,
                    public_distribution_restrictions=public_distribution_restrictions,
                    sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
                    known_sample_shas=set(samples_by_sha) | set(association_by_sha),
                ),
                archive_export_allowed=envelope_allowed,
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
                public_distribution_allowed=public_distribution_allowed,
                archive_export_allowed=envelope_allowed,
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
                public_distribution_allowed=public_distribution_allowed,
                archive_export_allowed=envelope_allowed,
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

    detection_types = {
        "yara_rule",
        "yara_test_report",
        "suricata_rule",
        "suricata_test_report",
    }
    detection_restrictions: set[str] = set()
    detection_restrictions.update(public_distribution_restrictions)
    detection_restrictions.update(envelope_restrictions)
    assets = [
        replace(
            asset,
            restrictions=tuple(sorted(set(asset.restrictions) | detection_restrictions)),
            public_distribution_allowed=public_distribution_allowed,
            archive_export_allowed=envelope_allowed,
        )
        if asset.type in detection_types and asset.availability != "absent"
        else asset
        for asset in assets
    ]

    assets = _resolve_public_distribution_for_assets(
        assets,
        public_distribution_allowed=public_distribution_allowed,
        public_distribution_restrictions=public_distribution_restrictions,
        sample_public_restrictions_by_sha=sample_public_restrictions_by_sha,
        known_sample_shas=set(samples_by_sha) | set(association_by_sha),
        observations=source_snapshot.observations,
    )

    return InvestigationAnnexSnapshot(investigation_id=investigation_id_text, assets=tuple(assets))
