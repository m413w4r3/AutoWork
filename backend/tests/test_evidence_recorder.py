from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.evidence_recorder import (
    MAX_EVIDENCE_JSON_BYTES,
    ProductionEvidenceRecorder,
)
from cti_app.application.investigation_persistence import InMemoryEvidenceObservationRepository
from cti_app.application.investigation_service import ProductionArtifactObservationWriter
from cti_app.application.investigation_tools import ToolResult
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import EvidenceKind, ToolInvocationStatus


class _BlobStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}

    async def put_canonical_json(self, payload, *, bucket):
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        sha256 = hashlib.sha256(encoded).hexdigest()
        blob_id = uuid5(UUID("a73cde63-2629-40ba-9b2f-9575a7d8195e"), sha256)
        self.blobs[blob_id] = encoded
        return blob_id, sha256


@pytest.mark.asyncio
async def test_record_json_is_bounded_sanitized_and_idempotent() -> None:
    store = _BlobStore()
    repository = InMemoryEvidenceObservationRepository()
    recorder = ProductionEvidenceRecorder(store, repository)  # type: ignore[arg-type]
    investigation_id = uuid4()
    invocation_id = uuid4()
    observed_at = datetime(2026, 10, 13, tzinfo=UTC)
    attacker_token = "github_pat_" + "a" * 82
    raw_bytes = b"harmless-binary-fixture"
    byte_array = list(range(256))
    byte_array_raw = bytes(byte_array)
    large_excerpt = "Synthetic CTI excerpt " + "sample-marker " * 400
    payload = {
        "summary": f"Found the file; attacker token={attacker_token}",
        "large_excerpt": large_excerpt,
        "signed_page": "https://vt.invalid/report?X-Amz-Signature=private-value",
        "api_key": attacker_token,
        "raw_bytes": raw_bytes,
        "payload_bytes": byte_array,
        "wrapped_raw_bytes": {"value": byte_array},
        "nested": {"operator_api_token": "must-not-persist", "count": 2},
        "provider_usage": {
            "status": "available",
            "input_tokens": 321,
            "output_tokens": 54,
            "total_tokens": 375,
            "vt_api_token": "must-not-persist",
        },
    }

    first = await recorder.record_json(
        investigation_id=investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        payload=payload,
        source="virustotal.file_report",
        source_version="api-v3",
        object_type="file",
        object_id="a" * 64,
        limitations=("provider response may be partial",),
        invocation_ref=invocation_id,
        observed_at=observed_at,
        idempotency_key="vt-file-report:operation-1",
    )
    replay = await recorder.record_json(
        investigation_id=investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        payload=payload,
        source="virustotal.file_report",
        source_version="api-v3",
        object_type="file",
        object_id="a" * 64,
        limitations=("provider response may be partial",),
        invocation_ref=invocation_id,
        observed_at=observed_at + timedelta(days=1),
        idempotency_key="vt-file-report:operation-1",
    )

    assert replay == first
    assert first.observed_at == observed_at
    assert len(await repository.list_for_investigation(investigation_id)) == 1
    stored = store.blobs[first.blob_id]
    decoded = json.loads(stored)
    assert decoded["api_key"] == attacker_token
    assert decoded["summary"].endswith(attacker_token)
    assert decoded["large_excerpt"] == large_excerpt
    assert decoded["raw_bytes"] == {
        "encoding": "base64",
        "byte_length": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "content_base64": base64.b64encode(raw_bytes).decode("ascii"),
    }
    assert decoded["payload_bytes"] == {
        "encoding": "base64",
        "byte_length": len(byte_array_raw),
        "sha256": hashlib.sha256(byte_array_raw).hexdigest(),
        "content_base64": base64.b64encode(byte_array_raw).decode("ascii"),
    }
    assert decoded["wrapped_raw_bytes"]["value"] == decoded["payload_bytes"]
    for forbidden in (b"private-value", b"must-not-persist", b"X-Amz-Signature"):
        assert forbidden not in stored
    assert b"[URL omitted]" in stored
    assert b'"input_tokens":321' in stored
    assert b'"total_tokens":375' in stored

    with pytest.raises(ValueError, match="different content"):
        await recorder.record_json(
            investigation_id=investigation_id,
            kind=EvidenceKind.VT_FILE_REPORT,
            payload={"summary": "changed"},
            source="virustotal.file_report",
            source_version="api-v3",
            object_type="file",
            object_id="a" * 64,
            limitations=("provider response may be partial",),
            invocation_ref=invocation_id,
            idempotency_key="vt-file-report:operation-1",
        )


@pytest.mark.asyncio
async def test_existing_blob_and_negative_result_keep_typed_provenance() -> None:
    store = _BlobStore()
    repository = InMemoryEvidenceObservationRepository()
    recorder = ProductionEvidenceRecorder(store, repository)  # type: ignore[arg-type]
    investigation_id = uuid4()
    raw_blob_id = uuid4()
    raw_sha256 = hashlib.sha256(b"already archived inert bytes").hexdigest()
    raw = await recorder.record_existing_blob(
        investigation_id=investigation_id,
        kind=EvidenceKind.VT_RELATIONSHIP,
        blob_id=raw_blob_id,
        sha256=raw_sha256,
        source="virustotal.relationships",
        source_version="api-v3",
        object_type="file",
        object_id="b" * 64,
        limitations=("relationship page is incomplete",),
        idempotency_key="vt-relationship:page-1",
    )
    negative = await recorder.record_negative(
        investigation_id=investigation_id,
        reason="The provider returned no matching records.",
        source="shodan.search",
        source_version="api-v1",
        object_type="domain",
        object_id="example.invalid",
        limitations=("This query and corpus do not establish global absence.",),
        details={"query": "hostname:example.invalid", "result_count": 0},
        idempotency_key="shodan-search:empty-1",
    )

    assert raw.blob_id == raw_blob_id
    assert raw.sha256 == raw_sha256
    assert raw.kind is EvidenceKind.VT_RELATIONSHIP
    assert negative.kind is EvidenceKind.NEGATIVE_RESULT
    assert negative.blob_id in store.blobs
    assert len(await repository.list_for_investigation(investigation_id)) == 2


@pytest.mark.asyncio
async def test_large_advisor_observation_preserves_complete_implant_material() -> None:
    store = _BlobStore()
    repository = InMemoryEvidenceObservationRepository()
    recorder = ProductionEvidenceRecorder(store, repository, max_payload_bytes=2 * 1024 * 1024)  # type: ignore[arg-type]
    implant_material = "implant_auth_token=" + "malware-config-" * 30_000
    advisor = await recorder.record_json(
        investigation_id=uuid4(),
        kind=EvidenceKind.OTHER,
        payload={"schema": "operator-advisor-call.v1", "output": {"text": implant_material}},
        source="advisor.consult",
        source_version="advisor-consult.v1",
        object_type="advisor_call",
        object_id="draft_text",
    )
    stored_advisor = store.blobs[advisor.blob_id]
    assert MAX_EVIDENCE_JSON_BYTES > 128 * 1024
    assert len(stored_advisor) > 128 * 1024
    assert implant_material.encode() in stored_advisor

    recorder = ProductionEvidenceRecorder(store, repository)  # type: ignore[arg-type]

    investigation_id = uuid4()
    typed = await recorder.record_json(
        investigation_id=investigation_id,
        kind=EvidenceKind.YARA_MATCH,
        payload={"rule": "fixture_rule", "matched": True},
        source="yara.scan",
        source_version="1",
        object_type="file",
        object_id="d" * 64,
    )
    writer = ProductionArtifactObservationWriter(store, repository)  # type: ignore[arg-type]
    invocation = SimpleNamespace(
        investigation_id=investigation_id,
        id=uuid4(),
        operation_key="yara:scan:1",
        tool_name="yara.scan",
        schema_version="1",
    )
    result = ToolResult(
        status=ToolInvocationStatus.SUCCEEDED,
        observation_refs=(typed.id,),
        output={"summary": "same finding in a generic summary"},
    )

    assert await writer.archive(invocation, result) == ()
    assert await repository.list_for_investigation(investigation_id) == (typed,)


@pytest.mark.asyncio
async def test_capture_association_accepts_only_quarantined_and_versioned_vt_sources() -> None:
    store = _BlobStore()
    repository = InMemoryEvidenceObservationRepository()
    recorder = ProductionEvidenceRecorder(store, repository)  # type: ignore[arg-type]
    investigation_id = uuid4()
    sha256 = "e" * 64
    vt_blob_id = uuid4()
    quarantine_blob_id = uuid4()
    unrelated_blob_id = uuid4()

    await recorder.record_existing_blob(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=vt_blob_id,
        sha256=sha256,
        source="VirusTotal",
        source_version="vt-sandbox-pcap.v1",
        object_type="network_capture",
        object_id=sha256,
    )
    await recorder.record_existing_blob(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=quarantine_blob_id,
        sha256=sha256,
        source="network.quarantine",
        source_version="network-capture-acceptance.v1",
        object_type="network_capture",
        object_id=sha256,
    )
    await recorder.record_existing_blob(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=unrelated_blob_id,
        sha256=sha256,
        source="VirusTotal",
        source_version="api-v3",
        object_type="network_capture",
        object_id=sha256,
    )

    assert await recorder.is_capture_associated(
        investigation_id=investigation_id, blob_id=vt_blob_id, sha256=sha256
    )
    assert await recorder.is_capture_associated(
        investigation_id=investigation_id, blob_id=quarantine_blob_id, sha256=sha256
    )
    assert not await recorder.is_capture_associated(
        investigation_id=investigation_id, blob_id=unrelated_blob_id, sha256=sha256
    )
