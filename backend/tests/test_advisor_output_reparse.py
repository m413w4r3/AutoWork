from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.advisor_contracts import ADVISOR_SOURCE_PARSER_VERSION
from cti_app.application.investigation_advisor_tools import ReparsePersistedAdvisorOutputTool
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation, ToolInvocationStatus


class ObservationRepository:
    def __init__(self, observation: EvidenceObservation) -> None:
        self.observation = observation

    async def get(self, observation_id):
        return self.observation if observation_id == self.observation.id else None


class BlobRepository:
    def __init__(self, blob_id, sha256: str, size: int) -> None:
        self.blob_id = blob_id
        self.sha256 = sha256
        self.size = size

    async def get_by_address(self, logical_bucket: str, sha256: str):
        if logical_bucket != "model-outputs" or sha256 != self.sha256:
            return None
        return SimpleNamespace(
            id=self.blob_id,
            descriptor=SimpleNamespace(sha256=self.sha256, size=self.size),
        )


class ArtifactStore:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def read_bytes(self, blob_id, *, max_bytes: int):
        value = self.values[blob_id]
        if len(value) > max_bytes:
            raise ValueError("read exceeds test limit")
        return value


class Recorder:
    def __init__(self) -> None:
        self.calls = []
        self.observation_id = uuid4()

    async def record_json(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.observation_id)


def _fixture_raw() -> str:
    path = Path(__file__).parent / "fixtures" / "advisor_wire" / "bridge-yara-draft-347b29b2.txt"
    return path.read_text(encoding="utf-8")


def _harness(
    raw_text: str,
    *,
    consultation_type: str = "yara_draft",
    input_refs: list[dict] | None = None,
    previous_output: dict | None = None,
    reparse_context: dict | None = None,
    review_units: list[dict] | None = None,
    review_target: dict | None = None,
    reviewed_text_sha256: str | None = None,
):
    investigation_id = uuid4()
    original_id = uuid4()
    invocation_id = str(uuid4())
    measurement_ref = {"kind": "measurement", "id": "m001", "sha256": "a" * 64}
    observation_ref = {"kind": "observation", "id": str(uuid4()), "sha256": "b" * 64}
    selected_input_refs = (
        input_refs if input_refs is not None else [measurement_ref, observation_ref]
    )
    output_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    raw_blob_id = uuid4()
    bridge_provenance = {
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "input_sha256": "c" * 64,
        "input_refs": selected_input_refs,
        "output_sha256": output_sha256,
        "provider_usage": {"status": "unknown"},
    }
    output = previous_output or {
        "schema": "advisor-response.v3",
        "consultation_type": consultation_type,
        "status": "partial",
        "advisor_status": "partial",
        "usable": False,
    }
    output["output_sha256"] = output_sha256
    output["bridge_provenance"] = bridge_provenance
    persisted = {
        "schema": "operator-advisor-call.v1",
        "advisor_response_schema": "advisor-response.v3",
        "consultation_type": consultation_type,
        "advisor_status": "partial",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "idempotency_key": "original-call-key",
        "input_sha256": "c" * 64,
        "input_refs": selected_input_refs,
        "output_sha256": output_sha256,
        "provider_usage": {"status": "unknown"},
        "output": output,
    }
    if reparse_context is not None:
        persisted["reparse_context"] = reparse_context
    if review_units is not None:
        persisted["review_units"] = review_units
    if review_target is not None:
        persisted["review_target"] = review_target
    if reviewed_text_sha256 is not None:
        persisted["reviewed_text_sha256"] = reviewed_text_sha256
    persisted_bytes = json.dumps(
        persisted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    original = EvidenceObservation(
        id=original_id,
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=hashlib.sha256(persisted_bytes).hexdigest(),
        source="advisor.consult",
        source_version="advisor-consult.v1",
        observed_at=datetime.now(UTC),
        object_type="advisor_call",
        object_id=consultation_type,
    )
    recorder = Recorder()
    artifact_store = ArtifactStore(
        {original.blob_id: persisted_bytes, raw_blob_id: raw_text.encode()}
    )
    tool = ReparsePersistedAdvisorOutputTool(
        observations=ObservationRepository(original),
        blobs=BlobRepository(raw_blob_id, output_sha256, len(raw_text.encode())),
        artifact_store=artifact_store,
        recorder=recorder,
    )
    context = SimpleNamespace(investigation_id=investigation_id)
    return tool, recorder, original, context, invocation_id, output_sha256, selected_input_refs


@pytest.mark.asyncio
async def test_reparse_appends_verified_parse_without_a_bridge_transport() -> None:
    raw = _fixture_raw()
    tool, recorder, original, context, invocation_id, output_sha256, input_refs = _harness(raw)

    result = await tool.execute({"observation_id": str(original.id)}, context)

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["usable"] is True
    assert result.output["bridge_invocation_id"] == invocation_id
    assert result.output["output_sha256"] == output_sha256
    assert result.output["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION
    assert len(recorder.calls) == 1
    recorded = recorder.calls[0]
    assert recorded["source"] == "advisor.consult"
    assert recorded["source_version"] == ADVISOR_SOURCE_PARSER_VERSION
    assert recorded["idempotency_key"] == (
        f"advisor-reparse:{original.id}:{ADVISOR_SOURCE_PARSER_VERSION}"
    )
    assert recorded["payload"]["reparsed_from_observation_id"] == str(original.id)
    assert recorded["payload"]["input_refs"] == input_refs
    assert recorded["payload"]["bridge_invocation_id"] == invocation_id
    assert recorded["payload"]["bridge_turns_consumed"] == 0
    parsed_output = recorded["payload"]["output"]
    assert parsed_output["bridge_provenance"]["bridge_invocation_id"] == invocation_id
    assert parsed_output["motifs"][0]["measurement_refs"] == [
        {"measurement_id": "m001", "sha256": "a" * 64}
    ]


@pytest.mark.asyncio
async def test_reparse_refuses_minio_bytes_that_do_not_match_observation_output_sha() -> None:
    raw = _fixture_raw()
    tool, recorder, original, context, _invocation_id, _output_sha, _refs = _harness(raw)
    raw_blob_id = tool._blobs.blob_id
    tool._artifact_store.values[raw_blob_id] = b"changed after the persisted observation"

    result = await tool.execute({"observation_id": str(original.id)}, context)

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_reparse_raw_output_sha256_mismatch"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_reparse_salvages_legacy_draft_text_and_flags_prose_aliases() -> None:
    raw = """advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
UNIT: U9
S: La synthèse cite les observations R2 et R3 dans sa prose. [A1]
LIMITATION: La limite cite aussi R2 et R3 en prose.
"""
    ref = {"kind": "observation", "id": str(uuid4()), "sha256": "d" * 64}
    previous_output = {
        "schema": "advisor-response.v3",
        "consultation_type": "draft_text",
        "status": "partial",
        "advisor_status": "partial",
        "usable": False,
        "blocks": [
            {
                "unit_id": "U9",
                "block_type": "additional_samples",
                "form": "analyst_note",
                "anchor": "section:2:paragraph:0003",
                "sentences": [
                    {
                        "text": "Une phrase antérieure conservée pour reconstruire U9.",
                        "assertion_ids": ["assertion-u9"],
                    }
                ],
            }
        ],
    }
    tool, recorder, original, context, _invocation_id, _output_sha, _refs = _harness(
        raw,
        consultation_type="draft_text",
        input_refs=[ref],
        previous_output=previous_output,
    )

    result = await tool.execute({"observation_id": str(original.id)}, context)

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["usable"] is True
    assert "source_alias_in_prose" in result.output["warnings"]
    assert result.output["output"]["blocks"][0]["usable"] is True
    assert "R2 et R3" in result.output["output"]["blocks"][0]["sentences"][0]["text"]
    assert any("R2" in item and "R3" in item for item in result.output["output"]["limitations"])
    assert (
        "source_alias_in_prose" in result.output["output"]["warning_classification"]["informative"]
    )
    reparsed_payload = recorder.calls[0]["payload"]
    assert reparsed_payload["bridge_turns_consumed"] == 0
    assert "source_alias_in_prose" in reparsed_payload["output"]["warnings"]


@pytest.mark.asyncio
async def test_reparse_salvages_complete_review_units_from_partial_wire_without_bridge() -> None:
    raw = """advisor-wire.v1
TYPE: review_text
STATUS: partial
UNIT: U13
REMARK: La remarque cite R2 en prose sans créer de référence.
UNIT: U15
REMARK: La réserve de portée reste associée à cette unité.
LIMITATION: none
"""
    review_units = [
        {
            "unit_id": unit_id,
            "form": "body",
            "block_type": "key_findings",
            "anchor": None,
            "reviewed_text_sha256": digest,
        }
        for unit_id, digest in (("U13", "a" * 64), ("U15", "b" * 64))
    ]
    input_refs = [{"kind": "observation", "id": str(uuid4()), "sha256": "e" * 64}]
    tool, recorder, original, context, invocation_id, output_sha256, input_refs = _harness(
        raw,
        consultation_type="review_text",
        input_refs=input_refs,
        reparse_context={"transmitted_refs": input_refs, "units": review_units},
        review_units=review_units,
        previous_output={
            "schema": "advisor-response.v3",
            "consultation_type": "review_text",
            "status": "partial",
            "advisor_status": "partial",
            "usable": False,
            "reviews": [],
        },
    )
    result = await tool.execute({"observation_id": str(original.id)}, context)

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["usable"] is True
    assert result.output["bridge_invocation_id"] == invocation_id
    assert result.output["output_sha256"] == output_sha256
    assert [item["unit_id"] for item in result.output["output"]["reviews"]] == ["U13", "U15"]
    assert all(item["usable"] for item in result.output["output"]["reviews"])
    assert (
        "source_alias_in_prose" in result.output["output"]["warning_classification"]["informative"]
    )
    recorded = recorder.calls[0]["payload"]
    assert recorded["input_refs"] == input_refs
    assert recorded["review_units"] == review_units
    assert recorded["bridge_turns_consumed"] == 0
