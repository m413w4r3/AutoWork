"""Append immutable, bounded evidence observations to canonical blob storage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from cti_app.application.investigation_persistence import EvidenceObservationRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation

MAX_EVIDENCE_JSON_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_OBJECT_ITEMS = 256
MAX_EVIDENCE_ARRAY_ITEMS = 128
MAX_EVIDENCE_TEXT_CHARS = 4_000
_EVIDENCE_NAMESPACE = UUID("47bfec4f-9070-42d0-aef5-0eb84c8b57cd")

_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature)",
    re.I,
)
_SAFE_USAGE_COUNT_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"}
)
_BINARY_FIELD = re.compile(
    r"(?:raw[_-]?bytes|(?:file|sample|payload|content|binary)[_-]?bytes|"
    r"bytes[_-]?(?:raw|file|sample|payload)|raw[_-]?(?:binary|octets))",
    re.I,
)
_SIGNED_URL = re.compile(r"https?://[^\s\]\[()<>\"]+", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|private[_ -]?key|access[_ -]?token|client[_ -]?secret|"
    r"api[_ -]?token|authorization|credential|password|secret|signature|sig)\s*[:=]\s*[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class EvidenceRecorder(Protocol):
    """Record typed observations while keeping payload bytes in canonical storage."""

    async def record_json(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        payload: Mapping[str, Any],
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation: ...

    async def record_existing_blob(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        blob_id: UUID,
        sha256: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation: ...

    async def record_negative(
        self,
        *,
        investigation_id: UUID,
        reason: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...],
        details: Mapping[str, Any] | None = None,
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation: ...


class ProductionEvidenceRecorder:
    """Use ``ProductionArtifactStore`` for payloads and append through the evidence port."""

    def __init__(
        self,
        artifact_store: ProductionArtifactStore,
        observations: EvidenceObservationRepository,
        *,
        max_payload_bytes: int = MAX_EVIDENCE_JSON_BYTES,
    ) -> None:
        self._artifact_store = artifact_store
        self._observations = observations
        self._max_payload_bytes = max_payload_bytes

    async def record_json(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        payload: Mapping[str, Any],
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        safe_payload = (
            _preserve_advisor_json(payload)
            if source == "advisor.consult"
            else _sanitize_json(payload)
        )
        if not isinstance(safe_payload, dict):
            raise ValueError("Evidence JSON payload must be an object")
        encoded = ProductionArtifactStore.canonical_json_bytes(safe_payload)
        maximum = self._max_payload_bytes
        if len(encoded) > maximum:
            raise ValueError(
                f"Evidence JSON payload exceeds configured {maximum} byte object guard"
            )
        sha256 = hashlib.sha256(encoded).hexdigest()
        metadata = _safe_metadata(
            source=source,
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
        )
        observation_id = _observation_id(
            idempotency_key=idempotency_key,
            investigation_id=investigation_id,
            kind=kind,
            sha256=sha256,
            invocation_ref=invocation_ref,
            **metadata,
        )
        existing = await self._observations.get(observation_id)
        if existing is not None:
            _require_same_observation(
                existing,
                investigation_id=investigation_id,
                kind=kind,
                sha256=sha256,
                metadata=metadata,
                invocation_ref=invocation_ref,
            )
            return existing

        blob_id, stored_sha256 = await self._artifact_store.put_canonical_json(
            safe_payload, bucket="investigation-evidence"
        )
        if stored_sha256 != sha256:
            raise ValueError("Artifact store returned a digest that does not match its payload")
        observation = EvidenceObservation(
            id=observation_id,
            investigation_id=investigation_id,
            kind=kind,
            blob_id=blob_id,
            sha256=sha256,
            source=metadata["source"],
            source_version=metadata["source_version"],
            observed_at=observed_at or datetime.now(UTC),
            object_type=metadata["object_type"],
            object_id=metadata["object_id"],
            limitations=metadata["limitations"],
            invocation_ref=invocation_ref,
        )
        return await self._append_idempotently(observation)

    async def record_existing_blob(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        blob_id: UUID,
        sha256: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Evidence blob SHA-256 must be lowercase hexadecimal")
        metadata = _safe_metadata(
            source=source,
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
        )
        observation_id = _observation_id(
            idempotency_key=idempotency_key,
            investigation_id=investigation_id,
            kind=kind,
            sha256=sha256,
            invocation_ref=invocation_ref,
            blob_id=blob_id,
            **metadata,
        )
        existing = await self._observations.get(observation_id)
        if existing is not None:
            _require_same_observation(
                existing,
                investigation_id=investigation_id,
                kind=kind,
                sha256=sha256,
                metadata=metadata,
                invocation_ref=invocation_ref,
                blob_id=blob_id,
            )
            return existing
        observation = EvidenceObservation(
            id=observation_id,
            investigation_id=investigation_id,
            kind=kind,
            blob_id=blob_id,
            sha256=sha256,
            source=metadata["source"],
            source_version=metadata["source_version"],
            observed_at=observed_at or datetime.now(UTC),
            object_type=metadata["object_type"],
            object_id=metadata["object_id"],
            limitations=metadata["limitations"],
            invocation_ref=invocation_ref,
        )
        return await self._append_idempotently(observation)

    async def is_capture_associated(
        self, *, investigation_id: UUID, blob_id: UUID, sha256: str
    ) -> bool:
        observations = await self._observations.list_for_investigation(investigation_id)
        return any(
            observation.kind is EvidenceKind.OTHER
            and observation.blob_id == blob_id
            and observation.sha256 == sha256
            and (
                observation.source == "network.quarantine"
                or (
                    observation.source == "VirusTotal"
                    and observation.source_version == "vt-sandbox-pcap.v1"
                )
            )
            and observation.object_type == "network_capture"
            and observation.object_id == sha256
            for observation in observations
        )

    async def record_negative(
        self,
        *,
        investigation_id: UUID,
        reason: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...],
        details: Mapping[str, Any] | None = None,
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        safe_reason = sanitize_evidence_text(reason, max_chars=1_000)
        if not safe_reason:
            raise ValueError("Negative-result reason must not be empty")
        return await self.record_json(
            investigation_id=investigation_id,
            kind=EvidenceKind.NEGATIVE_RESULT,
            payload={
                "schema": "investigation-negative-result.v1",
                "reason": safe_reason,
                "details": dict(details or {}),
            },
            source=source,
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            invocation_ref=invocation_ref,
            observed_at=observed_at,
            idempotency_key=idempotency_key,
        )

    async def _append_idempotently(self, observation: EvidenceObservation) -> EvidenceObservation:
        try:
            return await self._observations.append_if_absent(observation)
        except ValueError:
            # Replays can race after both callers miss ``get``. The repository remains the
            # arbiter; return only an observation that still matches this immutable payload.
            existing = await self._observations.get(observation.id)
            if existing is None:
                raise
            _require_same_observation(
                existing,
                investigation_id=observation.investigation_id,
                kind=observation.kind,
                sha256=observation.sha256,
                metadata={
                    "source": observation.source,
                    "source_version": observation.source_version,
                    "object_type": observation.object_type,
                    "object_id": observation.object_id,
                    "limitations": observation.limitations,
                },
                invocation_ref=observation.invocation_ref,
                blob_id=observation.blob_id,
            )
            return existing


def sanitize_evidence_text(value: Any, *, max_chars: int = MAX_EVIDENCE_TEXT_CHARS) -> str:
    """Remove secret values, URLs, and NULs from a bounded untrusted string."""
    candidate = str(value).replace("\x00", "")
    candidate = _SIGNED_URL.sub("[URL omitted]", candidate)
    candidate = _SECRET_VALUE.sub(r"\1=[redacted]", candidate)
    candidate = _BEARER_VALUE.sub("Bearer [redacted]", candidate)
    return candidate[: max(0, max_chars)]


def _sanitize_json(value: Any, *, depth: int = 0) -> Any:
    """Return JSON-safe, bounded data with secret and binary fields removed."""
    if depth > 16:
        return "[maximum depth omitted]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_EVIDENCE_OBJECT_ITEMS:
                break
            safe_key = str(key)[:256]
            safe_usage_count = (
                safe_key in _SAFE_USAGE_COUNT_FIELDS
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
            )
            if (_SECRET_FIELD.search(safe_key) and not safe_usage_count) or _BINARY_FIELD.search(
                safe_key
            ):
                continue
            if safe_key.lower() in {"raw_bytes", "bytes", "octets"}:
                continue
            result[safe_key] = _sanitize_json(item, depth=depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [_sanitize_json(item, depth=depth + 1) for item in value[:MAX_EVIDENCE_ARRAY_ITEMS]]
    if isinstance(value, str):
        return sanitize_evidence_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return "[binary omitted]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return sanitize_evidence_text(value, max_chars=1_000)


def _preserve_advisor_json(value: Any, *, depth: int = 0) -> Any:
    """Keep complete, untrusted CTI advisor content intact in its MinIO artifact."""
    if depth > 64:
        raise ValueError("Advisor evidence JSON exceeds the configured nesting guard")
    if isinstance(value, Mapping):
        return {
            str(key): _preserve_advisor_json(item, depth=depth + 1) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_preserve_advisor_json(item, depth=depth + 1) for item in value]
    if isinstance(value, str | bool | int) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes | bytearray | memoryview):
        raise ValueError("Binary data must be archived in a dedicated blob")
    return str(value)


def _safe_metadata(
    *,
    source: str,
    source_version: str,
    object_type: str,
    object_id: str,
    limitations: tuple[str, ...],
) -> dict[str, Any]:
    safe = {
        "source": sanitize_evidence_text(source, max_chars=256).strip(),
        "source_version": sanitize_evidence_text(source_version, max_chars=128).strip(),
        "object_type": sanitize_evidence_text(object_type, max_chars=128).strip(),
        "object_id": sanitize_evidence_text(object_id, max_chars=1_024).strip(),
        "limitations": tuple(
            sanitize_evidence_text(item, max_chars=2_000).strip() for item in limitations[:128]
        ),
    }
    if not all(safe[key] for key in ("source", "source_version", "object_type", "object_id")):
        raise ValueError("Evidence provenance and exact object identity must not be empty")
    return safe


def _observation_id(
    *,
    idempotency_key: str | None,
    investigation_id: UUID,
    kind: EvidenceKind,
    sha256: str,
    source: str,
    source_version: str,
    object_type: str,
    object_id: str,
    limitations: tuple[str, ...],
    invocation_ref: UUID | None,
    blob_id: UUID | None = None,
) -> UUID:
    identity: dict[str, Any]
    if idempotency_key is not None:
        stable_key = sanitize_evidence_text(idempotency_key, max_chars=2_000).strip()
        if not stable_key:
            raise ValueError("Evidence idempotency key must not be empty")
        identity = {"explicit": stable_key}
    else:
        # The default key identifies one immutable content/provenance/object tuple. It excludes
        # observed_at so a retry cannot create a second observation just because time advanced.
        identity = {
            "investigation_id": str(investigation_id),
            "kind": kind.value,
            "sha256": sha256,
            "blob_id": str(blob_id) if blob_id is not None else None,
            "source": source,
            "source_version": source_version,
            "object_type": object_type,
            "object_id": object_id,
            "limitations": list(limitations),
            "invocation_ref": str(invocation_ref) if invocation_ref is not None else None,
        }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid5(_EVIDENCE_NAMESPACE, encoded)


def _require_same_observation(
    existing: EvidenceObservation,
    *,
    investigation_id: UUID,
    kind: EvidenceKind,
    sha256: str,
    metadata: Mapping[str, Any],
    invocation_ref: UUID | None,
    blob_id: UUID | None = None,
) -> None:
    if (
        existing.investigation_id != investigation_id
        or existing.kind is not kind
        or existing.sha256 != sha256
        or existing.source != metadata["source"]
        or existing.source_version != metadata["source_version"]
        or existing.object_type != metadata["object_type"]
        or existing.object_id != metadata["object_id"]
        or existing.limitations != metadata["limitations"]
        or existing.invocation_ref != invocation_ref
        or (blob_id is not None and existing.blob_id != blob_id)
    ):
        raise ValueError("Evidence idempotency key was reused with different content")


__all__ = [
    "MAX_EVIDENCE_JSON_BYTES",
    "EvidenceRecorder",
    "ProductionEvidenceRecorder",
    "_sanitize_json",
    "sanitize_evidence_text",
]
