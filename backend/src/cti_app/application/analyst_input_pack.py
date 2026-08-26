"""Immutable, deterministic input for an analyst investigation.

The pack is deliberately built from structured production output.  It never
parses synthesis prose: file indicators must already be accepted by Q2.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from cti_app.domain.production import (
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)

ANALYST_INPUT_PACK_SCHEMA_VERSION = "analyst-input-pack-v1"
ANALYST_INPUT_PACK_BUCKET = "analyst-input-packs"
ANALYST_INPUT_PACK_NORMALIZATION_VERSION = "analyst-input-pack-normalization-v1"


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Return the sole byte representation used for storage and SHA-256."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class AnalystInputPackV1:
    payload: dict[str, Any]

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def _id(value: UUID | str) -> str:
    return str(value)


def _accepted_file_indicators(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only already canonical SHA-256 file indicators and their evidence.

    This is intentionally structural.  In particular, no text from the
    synthesis artifact is inspected for hash-looking strings.
    """
    accepted: list[dict[str, Any]] = []
    for item in items:
        value = item.get("value")
        status = item.get("indicator_status")
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            continue
        if status not in {"accepted", "verified", "retained"}:
            continue
        source_ids = item.get("source_ids", ())
        if not isinstance(source_ids, (list, tuple)) or not all(
            isinstance(source_id, str) for source_id in source_ids
        ):
            continue
        accepted.append(
            {
                "sha256": normalized,
                "provenance": {
                    "source_ids": sorted(set(source_ids)),
                    "extraction_item_id": item.get("id") or item.get("local_id"),
                },
            }
        )
    return sorted(
        accepted,
        key=lambda indicator: (
            indicator["sha256"],
            indicator["provenance"]["extraction_item_id"] or "",
        ),
    )


def build_analyst_input_pack_v1(
    *,
    run: SubjectProductionRun,
    investigation: AnalystInvestigation,
    synthesis: ProductionArtifact,
    extraction_artifacts: Iterable[ProductionArtifact],
    extraction_items: Iterable[dict[str, Any]],
    tlp: str | None,
    do_not_submit: bool,
    external_llm_allowed: bool,
    research_date: date,
    subject: dict[str, Any] | None = None,
) -> AnalystInputPackV1:
    """Build a fully ordered V1 pack anchored to a verified SYNTHESIS artifact."""
    if synthesis.stage is not ProductionArtifactStage.SYNTHESIS or (
        synthesis.status is not ProductionArtifactStatus.VERIFIED
    ):
        raise ValueError("Analyst input pack requires a verified SYNTHESIS artifact")
    if synthesis.production_run_id != run.id or synthesis.subject_id != run.subject_id:
        raise ValueError("SYNTHESIS artifact does not belong to the production run and subject")
    if investigation.production_run_id != run.id or investigation.subject_id != run.subject_id:
        raise ValueError("Investigation does not belong to the production run and subject")

    retained = []
    for artifact in extraction_artifacts:
        if artifact.stage is not ProductionArtifactStage.EXTRACTION:
            continue
        if artifact.status is not ProductionArtifactStatus.VERIFIED:
            continue
        retained.append(
            {
                "artifact_id": _id(artifact.id),
                "canonical_blob_id": _id(artifact.canonical_blob_id)
                if artifact.canonical_blob_id
                else None,
                "input_hash": artifact.input_hash,
                "version": artifact.version,
            }
        )

    payload = {
        "schema_version": ANALYST_INPUT_PACK_SCHEMA_VERSION,
        "normalization_version": ANALYST_INPUT_PACK_NORMALIZATION_VERSION,
        "production_run": {
            "id": _id(run.id),
            "profile": run.profile.value,
            "pipeline_generation": run.pipeline_generation,
        },
        "subject": {"id": _id(run.subject_id), **(subject or {})},
        "investigation": {
            "id": _id(investigation.id),
            "synthesis_artifact_id": _id(investigation.synthesis_artifact_id),
        },
        "synthesis_artifact": {
            "id": _id(synthesis.id),
            "canonical_blob_id": _id(synthesis.canonical_blob_id)
            if synthesis.canonical_blob_id
            else None,
            "rendered_blob_id": _id(synthesis.rendered_blob_id)
            if synthesis.rendered_blob_id
            else None,
            "input_hash": synthesis.input_hash,
            "version": synthesis.version,
        },
        "retained_extraction_artifacts": sorted(
            retained, key=lambda artifact: str(artifact["artifact_id"])
        ),
        "file_indicators": _accepted_file_indicators(extraction_items),
        "policy": {
            "tlp": tlp,
            "do_not_submit": do_not_submit,
            "external_llm_allowed": external_llm_allowed,
        },
        "research_date": research_date.isoformat(),
        "schema_versions": {
            "analyst_input_pack": ANALYST_INPUT_PACK_SCHEMA_VERSION,
            "normalization": ANALYST_INPUT_PACK_NORMALIZATION_VERSION,
        },
    }
    return AnalystInputPackV1(payload=payload)
