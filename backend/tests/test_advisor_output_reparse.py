from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.advisor_semantic_parser import (
    ADVISOR_RESPONSE_SCHEMA_V4,
    ADVISOR_SOURCE_PARSER_VERSION_V4,
)
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_REPARSE_SCHEMA_VERSION,
    ADVISOR_REPARSE_TOOL_NAME,
    ReparsePersistedAdvisorOutputTool,
    register_advisor_reparse_tool,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation, ToolInvocationStatus


class ObservationRepository:
    def __init__(self, observation: EvidenceObservation) -> None:
        self.observation = observation
        self.calls = []

    async def get(self, observation_id):
        self.calls.append(observation_id)
        return self.observation if observation_id == self.observation.id else None


class BlobRepository:
    def __init__(self, blob_id, sha256: str, size: int) -> None:
        self.blob_id = blob_id
        self.sha256 = sha256
        self.size = size
        self.calls = []

    async def get_by_address(self, logical_bucket: str, sha256: str):
        self.calls.append((logical_bucket, sha256))
        if logical_bucket != "model-outputs" or sha256 != self.sha256:
            return None
        return SimpleNamespace(
            id=self.blob_id,
            descriptor=SimpleNamespace(sha256=self.sha256, size=self.size),
        )


class ArtifactStore:
    def __init__(self, values: dict) -> None:
        self.values = values
        self.calls = []

    async def read_bytes(self, blob_id, *, max_bytes: int):
        self.calls.append(blob_id)
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


def test_registered_reparse_interface_exposes_only_explicit_v4_selection() -> None:
    registry = InMemoryToolRegistry()
    adapter = ReparsePersistedAdvisorOutputTool(
        observations=object(),
        blobs=object(),
        artifact_store=object(),
        recorder=object(),
    )

    register_advisor_reparse_tool(registry, adapter=adapter)

    registered = registry.get(ADVISOR_REPARSE_TOOL_NAME, ADVISOR_REPARSE_SCHEMA_VERSION)
    assert registered is not None
    schema = registered[0].input_schema
    assert schema["properties"]["parser_version"]["enum"] == (ADVISOR_SOURCE_PARSER_VERSION_V4,)
    assert "parser_version" not in schema["required"]


def _prompt_data(input_refs: list[dict]) -> dict:
    identifier = input_refs[0]["id"]
    digest = input_refs[0]["sha256"]
    return {
        "consultation_type": "draft_text",
        "question": "Review the observed JSON configuration.",
        "transmitted_refs": input_refs,
        "evidence": [
            {
                "evidence_id": identifier,
                "sha256": digest,
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "json",
                        "title": "Observed JSON configuration",
                        "text": '{"mode":"synthetic","label":"évidence 🧪"}',
                    }
                ],
            }
        ],
    }


def _semantic_raw() -> str:
    return json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "consultation_type": "draft_text",
            "status": "succeeded",
            "opinions": [
                {
                    "unit_id": "U1",
                    "text": "Review the observed configuration.",
                    "source_aliases": ["E1"],
                }
            ],
            "incomplete_units": [],
        },
        ensure_ascii=False,
    )


def _harness(
    raw_text: str,
    *,
    response_schema: str = "advisor-response.v3",
    stored_parser_version: str = "advisor-source-parser.v3",
    reparse_context: dict | None = None,
    settings: object | None = None,
):
    investigation_id = uuid4()
    original_id = uuid4()
    invocation_id = str(uuid4())
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    output_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    raw_blob_id = uuid4()
    bridge_provenance = {
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "input_sha256": "c" * 64,
        "input_refs": input_refs,
        "output_sha256": output_sha256,
        "provider_usage": {"status": "unknown"},
    }
    output = {
        "schema": response_schema,
        "consultation_type": "draft_text",
        "parser_version": stored_parser_version,
        "status": "succeeded",
        "usable": True,
        "output_sha256": output_sha256,
        "bridge_provenance": bridge_provenance,
    }
    persisted = {
        "schema": "operator-advisor-call.v1",
        "advisor_response_schema": response_schema,
        "consultation_type": "draft_text",
        "advisor_status": "succeeded",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "idempotency_key": "original-call-key",
        "input_sha256": "c" * 64,
        "input_refs": input_refs,
        "input_observation_refs": [],
        "input_source_refs": [],
        "output_sha256": output_sha256,
        "provider_usage": {"status": "unknown"},
        "output": output,
    }
    if reparse_context is not None:
        persisted["reparse_context"] = reparse_context
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
        object_id="draft_text",
    )
    recorder = Recorder()
    artifact_store = ArtifactStore(
        {original.blob_id: persisted_bytes, raw_blob_id: raw_text.encode("utf-8")}
    )
    tool = ReparsePersistedAdvisorOutputTool(
        observations=ObservationRepository(original),
        blobs=BlobRepository(raw_blob_id, output_sha256, len(raw_text.encode("utf-8"))),
        artifact_store=artifact_store,
        recorder=recorder,
        settings=settings,
    )
    context = SimpleNamespace(investigation_id=investigation_id)
    return tool, recorder, original, context, output_sha256, artifact_store, raw_blob_id


@pytest.mark.asyncio
async def test_legacy_v3_reparse_is_auditable_without_blob_read_or_new_observation() -> None:
    tool, recorder, original, context, output_sha256, store, raw_blob_id = _harness(
        "advisor-wire.v1\nTYPE: draft_text\nSTATUS: succeeded\nOPINION: old output\n"
    )
    original_hash = original.sha256

    result = await tool.execute({"observation_id": str(original.id)}, context)

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.error_code == "unsupported_legacy_format"
    assert result.output["status"] == "unsupported_legacy_format"
    assert result.output["output_sha256"] == output_sha256
    assert result.output["new_observation_written"] is False
    assert store.calls == [original.blob_id]
    assert recorder.calls == []
    assert original.sha256 == original_hash
    assert (
        raw_blob_id not in store.values
        or hashlib.sha256(store.values[raw_blob_id]).hexdigest() == output_sha256
    )


@pytest.mark.asyncio
async def test_explicit_v4_reparse_appends_verified_parse_without_bridge_or_egress(
    monkeypatch,
) -> None:
    raw = _semantic_raw()
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    tool, recorder, original, context, output_sha256, store, raw_blob_id = _harness(
        raw,
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        reparse_context=_prompt_data(input_refs),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reparse attempted Bridge or network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    result = await tool.execute(
        {
            "observation_id": str(original.id),
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        },
        context,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
    assert result.output["output_sha256"] == output_sha256
    assert result.output["output"]["raw_output"] == raw
    assert result.output["output"]["raw_output_bytes"] == len(raw.encode("utf-8"))
    assert store.values[raw_blob_id] == raw.encode("utf-8")
    assert hashlib.sha256(store.values[raw_blob_id]).hexdigest() == output_sha256
    assert len(recorder.calls) == 1
    recorded = recorder.calls[0]
    assert recorded["source_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
    assert recorded["payload"]["bridge_turns_consumed"] == 0
    assert recorded["payload"]["output"]["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
    assert recorded["payload"]["output"]["candidates"][0]["validated"] is False


@pytest.mark.asyncio
async def test_reparse_passes_configured_credential_values_to_pure_v4_parser() -> None:
    credential = "configured-only-reparse-token-508fb0e4"
    raw_data = json.loads(_semantic_raw())
    raw_data["opinions"][0]["text"] = f"Echoed value: {credential}"
    raw = json.dumps(raw_data, ensure_ascii=False)
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    tool, recorder, original, context, _, _, _ = _harness(
        raw,
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        reparse_context=_prompt_data(input_refs),
        settings=SimpleNamespace(openai_bridge_api_key=credential),
    )

    result = await tool.execute(
        {
            "observation_id": str(original.id),
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        },
        context,
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "refused"
    assert result.output["output"]["raw_output"] is None
    assert (
        result.output["output"]["raw_output_sha256"]
        == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    )
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_v4_reparse_refuses_raw_bytes_that_do_not_match_the_persisted_sha() -> None:
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    tool, recorder, original, context, _, store, raw_blob_id = _harness(
        _semantic_raw(),
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        reparse_context=_prompt_data(input_refs),
    )
    store.values[raw_blob_id] = b"changed after the persisted observation"

    result = await tool.execute(
        {
            "observation_id": str(original.id),
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        },
        context,
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_reparse_raw_output_sha256_mismatch"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_v4_requires_explicit_parser_selection_and_exact_reparse_context() -> None:
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    tool, recorder, original, context, _, store, raw_blob_id = _harness(
        _semantic_raw(),
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        reparse_context=_prompt_data(input_refs),
    )

    missing_selection = await tool.execute({"observation_id": str(original.id)}, context)
    assert missing_selection.error_code == "advisor_reparse_parser_version_required"
    assert recorder.calls == []
    assert raw_blob_id not in store.calls

    (
        missing_context_tool,
        missing_context_recorder,
        missing_context_original,
        missing_context,
        *_,
    ) = _harness(
        _semantic_raw(),
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
    )
    no_context = await missing_context_tool.execute(
        {
            "observation_id": str(missing_context_original.id),
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        },
        missing_context,
    )
    assert no_context.error_code == "legacy_context_unavailable"
    assert no_context.output["new_observation_written"] is False
    assert missing_context_recorder.calls == []


@pytest.mark.asyncio
async def test_invalid_v4_reparse_does_not_append_a_conclusion() -> None:
    input_refs = [{"kind": "observation", "id": "observation:synthetic-1", "sha256": "a" * 64}]
    tool, recorder, original, context, _, _, _ = _harness(
        '{"schema":"advisor-response.v3","status":"succeeded"}',
        response_schema=ADVISOR_RESPONSE_SCHEMA_V4,
        stored_parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        reparse_context=_prompt_data(input_refs),
    )

    result = await tool.execute(
        {
            "observation_id": str(original.id),
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        },
        context,
    )

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["status"] == "invalid"
    assert result.output["new_observation_written"] is False
    assert recorder.calls == []
