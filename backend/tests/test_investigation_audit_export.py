from __future__ import annotations

import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from cti_app.application.investigation_audit_export import (
    AuditSourceSnapshot,
    build_audit_bundle,
    load_audit_events,
    write_audit_bundle,
)

_NAMESPACE = UUID("4b3870ac-c2ab-4cb3-b347-126aef96fcfe")
_INVESTIGATION_ID = "d00b6b92-bdc4-4f32-a093-5f3561213b7c"
_RAW_HASH = "1" * 64
_NORMALIZED_HASH = "2" * 64
_INPUT_HASH = "3" * 64
_OUTPUT_HASH = "4" * 64


def _id(label: str) -> str:
    return str(uuid5(_NAMESPACE, label))


def _invocation(index: int, *, status: str = "succeeded") -> SimpleNamespace:
    created = datetime(2026, 10, 23, tzinfo=UTC) + timedelta(seconds=index)
    return SimpleNamespace(
        id=_id(f"invocation-{index}"),
        investigation_id=_INVESTIGATION_ID,
        operation_key=hashlib.sha256(f"operation-{index:04d}".encode()).hexdigest(),
        tool_name="advisor.consult",
        schema_version="tool.v1",
        normalized_parameters={"question": "PRIVATE_SYNTHETIC_PROMPT_MARKER"},
        purpose="PRIVATE_SYNTHETIC_PURPOSE_MARKER",
        status=status,
        reserved_budget={"bridge_turns": 1},
        consumed_budget={"bridge_turns": 1},
        response_refs=(),
        created_at=created,
        updated_at=created + timedelta(milliseconds=40),
        version=2,
    )


def _snapshot(invocations: tuple[SimpleNamespace, ...] = ()) -> AuditSourceSnapshot:
    return AuditSourceSnapshot(
        investigation_id=_INVESTIGATION_ID,
        invocations=invocations,
        source_versions={"investigation_state": "investigation-state.v1"},
    )


def test_778_synthetic_invocations_are_deterministic_and_resume_from_checkpoint(
    tmp_path: Path,
) -> None:
    invocations = tuple(_invocation(index) for index in range(778))
    snapshot = _snapshot(invocations)

    first = build_audit_bundle(snapshot)
    second = build_audit_bundle(snapshot)
    assert first.event_count == 779  # 778 invocation records plus one snapshot record.
    assert first.events == second.events
    assert first.trace == second.trace
    assert first.index == second.index
    assert first.manifest == second.manifest

    output_dir = tmp_path / "audit"
    first_write = write_audit_bundle(snapshot, output_dir, _INVESTIGATION_ID)
    (output_dir / "RUN_TRACE.md").unlink()
    (output_dir / "RUN_INDEX.json").unlink()
    (output_dir / "RUN_MANIFEST.json").unlink()
    resumed = write_audit_bundle(None, output_dir, _INVESTIGATION_ID, resume=True)

    assert not first_write.resumed_from_events
    assert resumed.resumed_from_events
    assert (output_dir / "RUN_EVENTS.jsonl.gz").read_bytes() == first.events
    assert (output_dir / "RUN_TRACE.md").read_bytes() == first.trace
    assert (output_dir / "RUN_INDEX.json").read_bytes() == first.index
    assert (output_dir / "RUN_MANIFEST.json").read_bytes() == first.manifest
    assert len(load_audit_events(output_dir / "RUN_EVENTS.jsonl.gz", _INVESTIGATION_ID)) == 779


def test_resume_rejects_checkpoint_with_missing_event(tmp_path: Path) -> None:
    bundle = build_audit_bundle(_snapshot((_invocation(1), _invocation(2))))
    event_lines = gzip.decompress(bundle.events).splitlines()
    corrupt_checkpoint = gzip.compress(b"\n".join(event_lines[1:]) + b"\n", mtime=0)
    output_dir = tmp_path / "corrupt-audit"
    output_dir.mkdir()
    (output_dir / "RUN_EVENTS.jsonl.gz").write_bytes(corrupt_checkpoint)

    with pytest.raises(ValueError, match="snapshot"):
        write_audit_bundle(None, output_dir, _INVESTIGATION_ID, resume=True)


def test_resume_rejects_stale_event_identity_even_with_recomputed_snapshot(tmp_path: Path) -> None:
    bundle = build_audit_bundle(_snapshot((_invocation(1),)))
    rows = [json.loads(line) for line in gzip.decompress(bundle.events).splitlines()]
    rows[0]["details"]["status_at_snapshot"] = "failed"

    def canonical(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    rows[-1]["details"]["projected_source_sha256"] = hashlib.sha256(
        canonical(rows[:-1])
    ).hexdigest()
    snapshot_body = {key: value for key, value in rows[-1].items() if key != "event_id"}
    rows[-1]["event_id"] = hashlib.sha256(canonical(snapshot_body)).hexdigest()
    lines = b"".join(canonical(row) + b"\n" for row in rows)
    stale_checkpoint = gzip.compress(lines, mtime=0)
    output_dir = tmp_path / "stale-audit"
    output_dir.mkdir()
    (output_dir / "RUN_EVENTS.jsonl.gz").write_bytes(stale_checkpoint)

    with pytest.raises(ValueError, match="stale event ID"):
        write_audit_bundle(None, output_dir, _INVESTIGATION_ID, resume=True)


def test_concurrent_exports_leave_one_coherent_bundle(tmp_path: Path) -> None:
    output_dir = tmp_path / "concurrent-audit"
    snapshots = (
        _snapshot(tuple(_invocation(index) for index in range(5))),
        _snapshot(tuple(_invocation(index + 100) for index in range(13))),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(write_audit_bundle, snapshot, output_dir, _INVESTIGATION_ID)
            for snapshot in snapshots
        ]
        bundles = [future.result() for future in futures]

    assert len({bundle.events for bundle in bundles}) == 2
    manifest = json.loads((output_dir / "RUN_MANIFEST.json").read_bytes())
    files = manifest["files"]
    for filename in ("RUN_EVENTS.jsonl.gz", "RUN_TRACE.md", "RUN_INDEX.json"):
        body = (output_dir / filename).read_bytes()
        assert hashlib.sha256(body).hexdigest() == files[filename]["sha256"]
    events = load_audit_events(output_dir / "RUN_EVENTS.jsonl.gz", _INVESTIGATION_ID)
    assert manifest["counts"]["events"] == len(events)
    assert files["RUN_EVENTS.jsonl.gz"]["records"] == len(events)


def test_projection_correlates_only_persisted_ids_and_omits_private_text() -> None:
    invocation = _invocation(0)
    invocation.tool_name = "PRIVATE_SYNTHETIC_TOOL_NAME_MARKER"
    invocation.schema_version = "PRIVATE_SYNTHETIC_SCHEMA_VERSION_MARKER"
    invocation_id = invocation.id
    observation = SimpleNamespace(
        id=_id("observation"),
        kind="negative_result",
        blob_id=_id("observation-blob"),
        sha256=_OUTPUT_HASH,
        source="PRIVATE_SYNTHETIC_OBSERVATION_MARKER",
        source_version="PRIVATE_SYNTHETIC_SOURCE_VERSION_MARKER",
        observed_at=datetime(2026, 10, 23, 1, tzinfo=UTC),
        object_type="virustotal",
        object_id="PRIVATE_SYNTHETIC_SAMPLE_MARKER",
        limitations=("PRIVATE_SYNTHETIC_LIMITATION_MARKER",),
        invocation_ref=UUID(invocation_id),
    )
    advisor_observation = SimpleNamespace(
        id=_id("advisor-call-observation"),
        kind="tool_response",
        blob_id=_id("advisor-call-observation-blob"),
        sha256=_RAW_HASH,
        source="advisor_tool",
        source_version="operator-advisor-call.v1",
        observed_at=datetime(2026, 10, 23, 1, 1, tzinfo=UTC),
        object_type="advisor_call",
        object_id="review_text",
        limitations=(),
        invocation_ref=UUID(invocation_id),
    )
    candidate_test_ref = _id("candidate-test-reference")
    candidate = SimpleNamespace(
        id=_id("candidate-version"),
        candidate_id=_id("candidate"),
        version=1,
        status="candidate",
        parent_version_id=None,
        source_sha256=_NORMALIZED_HASH,
        test_refs=(UUID(candidate_test_ref),),
        motif_provenance=(
            SimpleNamespace(
                motif="PRIVATE_SYNTHETIC_YARA_MOTIF_MARKER",
                rationale="PRIVATE_SYNTHETIC_YARA_RATIONALE_MARKER",
                observation_refs=(UUID(observation.id),),
            ),
        ),
        created_at=datetime(2026, 10, 23, 1, 2, tzinfo=UTC),
    )
    sample_association = SimpleNamespace(
        id=_id("sample-association"),
        version=2,
        origin="manual",
        qualification="confirmed",
        sample_sha256=_OUTPUT_HASH,
        changes=(
            SimpleNamespace(
                from_qualification="candidate",
                to_qualification="confirmed",
                actor_kind="human",
                actor_id="PRIVATE_SYNTHETIC_SAMPLE_ACTOR_MARKER",
                reason="PRIVATE_SYNTHETIC_SAMPLE_REASON_MARKER",
                changed_at=datetime(2026, 10, 23, 1, 3, tzinfo=UTC),
            ),
        ),
    )
    experiment = SimpleNamespace(
        id=_id("experiment"),
        version=1,
        recorded_at=datetime(2026, 10, 23, 2, tzinfo=UTC),
        hypothesis_id="PRIVATE_SYNTHETIC_HYPOTHESIS_ID_MARKER",
        claim_ids=("PRIVATE_SYNTHETIC_CLAIM_ID_MARKER",),
        verdict="inconclusive",
        actual_outcome="not_observed",
        observation_coverage="limited",
        availability="limited",
        tool_name="vt.lookup",
        prediction="PRIVATE_SYNTHETIC_PREDICTION_MARKER",
        expected="PRIVATE_SYNTHETIC_EXPECTED_MARKER",
        actual="PRIVATE_SYNTHETIC_NEGATIVE_RESULT_MARKER",
        falsification_criterion="PRIVATE_SYNTHETIC_CRITERION_MARKER",
        tool_invocation_refs=(SimpleNamespace(invocation_id=UUID(invocation_id), version=2),),
        observation_refs=(
            SimpleNamespace(observation_id=UUID(observation.id), sha256=_OUTPUT_HASH),
        ),
    )
    state = SimpleNamespace(
        version=3,
        turn_index=4,
        execution_mode="operator_led",
        stop_reason=None,
        agenda=SimpleNamespace(
            items=(
                SimpleNamespace(
                    question="PRIVATE_SYNTHETIC_QUESTION_MARKER",
                    potential="PRIVATE_SYNTHETIC_POTENTIAL_MARKER",
                    rank=1,
                ),
            )
        ),
        hypotheses=(
            SimpleNamespace(
                id="PRIVATE_SYNTHETIC_HYPOTHESIS_ID_MARKER",
                status="abandoned",
                statement="PRIVATE_SYNTHETIC_HYPOTHESIS_MARKER",
                evidence_refs=(),
                finding_refs=(),
            ),
        ),
        recent_actions=("PRIVATE_SYNTHETIC_ACTION_MARKER",),
    )
    opinion = SimpleNamespace(
        id=_id("opinion"),
        version=2,
        status="draft",
        created_at=datetime(2026, 10, 23, 3, tzinfo=UTC),
        content_sha256=_RAW_HASH,
        synthesis_artifact_id=_id("synthesis-artifact"),
        synthesis_sha256=_INPUT_HASH,
        base_publication_artifact_id=_id("base-publication-artifact"),
        base_publication_sha256=_OUTPUT_HASH,
        blocks=(
            SimpleNamespace(
                text="PRIVATE_SYNTHETIC_OPINION_MARKER",
                block_type="key_findings",
                qualification="fact",
                finding_refs=(),
                evidence_refs=(UUID(observation.id),),
                bridge_draft_ref=SimpleNamespace(
                    invocation_id=invocation_id, text_sha256=_INPUT_HASH
                ),
                bridge_review_ref=None,
            ),
        ),
    )
    turn = SimpleNamespace(
        id=_id("turn"),
        conversation_id=_id("conversation"),
        sequence=1,
        model_run_id=UUID(_id("model-run")),
        parent_turn_id=None,
        status="succeeded",
        idempotency_key="a" * 64,
        correlation_id="turn-correlation-1",
        external_turn_id="PRIVATE_SYNTHETIC_EXTERNAL_TURN_ID_MARKER",
        input_sha256=_INPUT_HASH,
        output_sha256=_OUTPUT_HASH,
        input_blob_reference=f"blob://{_id('turn-input-blob')}",
        output_blob_reference=f"blob://{_id('turn-output-blob')}",
        created_at=datetime(2026, 10, 23, 4, tzinfo=UTC),
    )
    model_run = SimpleNamespace(
        id=turn.model_run_id,
        provider="openai",
        backend="chatgpt_bridge",
        transport="openai_responses",
        model_role="research",
        requested_model="PRIVATE_SYNTHETIC_TOKEN_MARKER",
        actual_model_version="model-synthetic-v1",
        prompt_template_id="template-safe-id",
        prompt_template_version="1",
        authorized_input_hash=_RAW_HASH,
        evidence_pack_hash=_NORMALIZED_HASH,
        status="succeeded",
        submission_state="result_obtained",
        submission_attempt=1,
        bridge_request_id="PRIVATE_SYNTHETIC_BRIDGE_REQUEST_ID_MARKER",
        response_id="PRIVATE_SYNTHETIC_RESPONSE_ID_MARKER",
        raw_output_sha256=_RAW_HASH,
        normalized_output_sha256=_NORMALIZED_HASH,
        raw_output_reference=f"blob://{_id('raw-output-blob')}",
        normalized_output_reference=f"blob://{_id('normalized-output-blob')}",
        parser_stage="structured_parse",
        serializer_version="serializer.v1",
        normalization_version="normalizer.v1",
        duration_ms=321,
        usage={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19, "estimated": False},
        raw_output_chars=90,
        started_at=datetime(2026, 10, 23, 4, tzinfo=UTC),
    )
    snapshot = AuditSourceSnapshot(
        investigation_id=_INVESTIGATION_ID,
        invocations=(invocation,),
        observations=(observation, advisor_observation),
        detection_candidates=(candidate,),
        sample_associations=(sample_association,),
        findings=(
            SimpleNamespace(
                id=_id("finding"),
                version=1,
                created_at=datetime(2026, 10, 23, 2, tzinfo=UTC),
                kind="claim_verification",
                assessment="supported",
                novelty=None,
                statement="PRIVATE_SYNTHETIC_FINDING_MARKER",
                scope="PRIVATE_SYNTHETIC_FINDING_SCOPE_MARKER",
                limitations=(),
                observation_refs=(UUID(observation.id),),
                claim_refs=("PRIVATE_SYNTHETIC_FINDING_CLAIM_REF_MARKER",),
                supersedes=None,
            ),
        ),
        experiments=(experiment,),
        hypotheses_state=state,
        budget_snapshot=SimpleNamespace(
            version=5,
            budget=SimpleNamespace(
                limits={"bridge_turns": 5},
                reservations=(
                    SimpleNamespace(
                        operation_key=invocation.operation_key,
                        status="committed",
                        amounts={"bridge_turns": 1},
                        committed_amounts={"bridge_turns": 1},
                    ),
                ),
            ),
        ),
        opinions=(opinion,),
        envelope=SimpleNamespace(
            version=1,
            approved_at=None,
            allowed_providers=("openai",),
            disclosure=SimpleNamespace(tlp="clear", do_not_submit=False, external_llm_allowed=True),
            approval_decision_id="PRIVATE_SYNTHETIC_APPROVAL_ID_MARKER",
            approval_actor_id=None,
        ),
        pivot_conversation_id=turn.conversation_id,
        conversation=SimpleNamespace(
            id=turn.conversation_id,
            purpose="PRIVATE_SYNTHETIC_CONVERSATION_PURPOSE_MARKER",
            provider="openai",
            transport="chatgpt_bridge",
            status="succeeded",
            head_turn_id=turn.id,
            turn_count=1,
            version=1,
            created_at=datetime(2026, 10, 23, 4, tzinfo=UTC),
        ),
        conversation_turns=(turn,),
        model_runs=(model_run,),
        source_versions={
            "audit_projection": "autowork.investigation-audit-event.v1",
            "unknown_version_key": "PRIVATE_SYNTHETIC_SOURCE_VERSION_MAP_MARKER",
        },
    )

    bundle = build_audit_bundle(snapshot)
    rows = [json.loads(line) for line in gzip.decompress(bundle.events).splitlines()]
    invocation_event = next(row for row in rows if row["type"] == "tool_invocation.snapshot")
    observation_event = next(row for row in rows if row["type"] == "evidence_observation.recorded")
    advisor_observation_event = next(
        row
        for row in rows
        if row["type"] == "evidence_observation.recorded"
        and row["record"]["id"] == advisor_observation.id
    )
    experiment_event = next(row for row in rows if row["type"] == "experiment.version_recorded")
    finding_event = next(row for row in rows if row["type"] == "finding.version_recorded")
    candidate_event = next(
        row for row in rows if row["type"] == "detection_candidate.version_recorded"
    )
    sample_association_event = next(
        row for row in rows if row["type"] == "sample_association.version_recorded"
    )
    opinion_block = next(row for row in rows if row["type"] == "opinion.block_decision")
    model_run_event = next(row for row in rows if row["type"] == "model_run.snapshot")
    turn_event = next(row for row in rows if row["type"] == "model_conversation.turn")

    assert invocation_event["correlation"] == {
        "investigation_id": _INVESTIGATION_ID,
        "invocation_id": invocation_id,
        "operation_key_sha256": hashlib.sha256(invocation.operation_key.encode()).hexdigest(),
    }
    admission = invocation_event["details"]["admission"]
    assert admission["reserved"]["amounts"] == {"bridge_turns": 1}
    assert admission["committed"]["amounts"] == {"bridge_turns": 1}
    assert admission["released"]["status_at_snapshot"] == "committed"
    assert admission["requested"]["value"] == "UNKNOWN"
    assert invocation_event["details"]["submission"]["value"] == "UNKNOWN"
    assert invocation_event["details"]["reparse"]["value"] == "UNKNOWN"
    assert invocation_event["details"]["parser_version"]["value"] == "UNKNOWN"
    assert (
        invocation_event["details"]["purpose_sha256"]
        == hashlib.sha256(b"PRIVATE_SYNTHETIC_PURPOSE_MARKER").hexdigest()
    )
    assert invocation_event["details"]["purpose_included"] is False
    assert invocation_event["details"]["tool"] == "UNKNOWN"
    assert (
        invocation_event["details"]["tool_name_sha256"]
        == hashlib.sha256(b"PRIVATE_SYNTHETIC_TOOL_NAME_MARKER").hexdigest()
    )
    assert (
        invocation_event["details"]["schema_version_sha256"]
        == hashlib.sha256(b"PRIVATE_SYNTHETIC_SCHEMA_VERSION_MARKER").hexdigest()
    )
    assert invocation_event["details"]["latency_ms"]["value"] == "UNKNOWN"
    assert invocation_event["details"]["cost_estimate"]["value"] == "UNKNOWN"
    assert (
        invocation_event["details"]["bridge_prompt_observation"]["prompt_bytes"]["value"]
        == "UNKNOWN"
    )
    assert invocation_event["details"]["bridge_run_id"]["value"] == "UNKNOWN"
    materials = invocation_event["details"]["materials"]
    assert set(materials) == {
        "dossier",
        "rendered_prompt",
        "http_payload",
        "raw_response",
        "normalized_output",
        "redacted_copy",
    }
    assert {stage["sha256"]["value"] for stage in materials.values()} == {"UNKNOWN"}
    assert all(
        measurement["value"] == "UNKNOWN" and measurement["reason"]
        for stage in materials.values()
        for measurement in stage.values()
    )
    assert {stage["omitted"]["reason"] for stage in materials.values()} == {
        "no durable dossier stage record is linked",
        "no durable rendered-prompt record is linked",
        "no observed HTTP payload record is linked",
        "no raw response record is linked",
        "no normalized output record is linked",
        "no redacted-copy record is linked",
    }
    assert invocation_event["details"]["attached_opinion_refs"] == [
        {"opinion_id": opinion.id, "version": "2"}
    ]
    assert invocation_event["details"]["attachment"]["state"] == "attached"
    assert observation_event["correlation"]["invocation_id"] == invocation_id
    assert (
        observation_event["details"]["source_version_sha256"]
        == hashlib.sha256(b"PRIVATE_SYNTHETIC_SOURCE_VERSION_MARKER").hexdigest()
    )
    assert advisor_observation_event["details"]["consultation_type"] == "review_text"
    assert advisor_observation_event["correlation"]["invocation_id"] == invocation_id
    assert experiment_event["details"]["negative_result"] is True
    hypothesis_event = next(row for row in rows if row["type"] == "operator_state.snapshot")
    assert (
        experiment_event["details"]["hypothesis_id"]
        == hypothesis_event["details"]["hypotheses"][0]["id"]
    )
    assert experiment_event["details"]["claim_ids"] == [
        f"sha256:{hashlib.sha256(b'PRIVATE_SYNTHETIC_CLAIM_ID_MARKER').hexdigest()}"
    ]
    assert finding_event["details"]["claim_refs"] == [
        f"sha256:{hashlib.sha256(b'PRIVATE_SYNTHETIC_FINDING_CLAIM_REF_MARKER').hexdigest()}"
    ]
    assert candidate_event["details"]["test_refs"] == [candidate_test_ref]
    assert candidate_event["details"]["motif_provenance"] == [
        {
            "motif_sha256": hashlib.sha256(b"PRIVATE_SYNTHETIC_YARA_MOTIF_MARKER").hexdigest(),
            "rationale_sha256": hashlib.sha256(
                b"PRIVATE_SYNTHETIC_YARA_RATIONALE_MARKER"
            ).hexdigest(),
            "observation_refs": [observation.id],
        }
    ]
    assert sample_association_event["details"]["changes"] == [
        {
            "sequence": 1,
            "from_qualification": "candidate",
            "to_qualification": "confirmed",
            "actor_kind": "human",
            "actor_id_sha256": hashlib.sha256(b"PRIVATE_SYNTHETIC_SAMPLE_ACTOR_MARKER").hexdigest(),
            "reason_sha256": hashlib.sha256(b"PRIVATE_SYNTHETIC_SAMPLE_REASON_MARKER").hexdigest(),
            "changed_at": "2026-10-23T01:03:00Z",
        }
    ]
    assert experiment_event["details"]["observation_coverage"] == "limited"
    assert opinion_block["correlation"]["invocation_id"] == invocation_id
    assert turn_event["correlation"]["turn_id"] == turn.id
    assert turn_event["correlation"]["model_run_id"] == str(turn.model_run_id)
    assert model_run_event["details"]["submission"]["state"] == "result_obtained"
    assert model_run_event["details"]["raw_output_sha256"] == _RAW_HASH
    assert model_run_event["details"]["normalized_output_sha256"] == _NORMALIZED_HASH
    assert (
        model_run_event["details"]["raw_output_sha256"]
        != model_run_event["details"]["normalized_output_sha256"]
    )
    assert model_run_event["details"]["monetary_cost"]["value"] == "UNKNOWN"
    assert model_run_event["correlation"]["turn_id"] == turn.id
    assert model_run_event["details"]["duration_ms"] == 321
    assert model_run_event["details"]["usage"]["total_tokens"] == 19
    assert (
        model_run_event["details"]["requested_model_sha256"]
        == hashlib.sha256(b"PRIVATE_SYNTHETIC_TOKEN_MARKER").hexdigest()
    )
    conversation_event = next(row for row in rows if row["type"] == "model_conversation.snapshot")
    assert conversation_event["details"]["purpose"] == "UNKNOWN"
    assert (
        turn_event["details"]["idempotency_key"]["value"] == hashlib.sha256(b"a" * 64).hexdigest()
    )

    rendered = (
        gzip.decompress(bundle.events) + bundle.trace + bundle.index + bundle.manifest
    ).decode("utf-8", errors="ignore")
    for private_marker in (
        "PRIVATE_SYNTHETIC_PROMPT_MARKER",
        "PRIVATE_SYNTHETIC_PURPOSE_MARKER",
        "PRIVATE_SYNTHETIC_OBSERVATION_MARKER",
        "PRIVATE_SYNTHETIC_SAMPLE_MARKER",
        "PRIVATE_SYNTHETIC_LIMITATION_MARKER",
        "PRIVATE_SYNTHETIC_PREDICTION_MARKER",
        "PRIVATE_SYNTHETIC_EXPECTED_MARKER",
        "PRIVATE_SYNTHETIC_NEGATIVE_RESULT_MARKER",
        "PRIVATE_SYNTHETIC_CRITERION_MARKER",
        "PRIVATE_SYNTHETIC_HYPOTHESIS_ID_MARKER",
        "PRIVATE_SYNTHETIC_CLAIM_ID_MARKER",
        "PRIVATE_SYNTHETIC_FINDING_MARKER",
        "PRIVATE_SYNTHETIC_FINDING_SCOPE_MARKER",
        "PRIVATE_SYNTHETIC_FINDING_CLAIM_REF_MARKER",
        "PRIVATE_SYNTHETIC_YARA_MOTIF_MARKER",
        "PRIVATE_SYNTHETIC_YARA_RATIONALE_MARKER",
        "PRIVATE_SYNTHETIC_SAMPLE_ACTOR_MARKER",
        "PRIVATE_SYNTHETIC_SAMPLE_REASON_MARKER",
        "PRIVATE_SYNTHETIC_EXTERNAL_TURN_ID_MARKER",
        "PRIVATE_SYNTHETIC_BRIDGE_REQUEST_ID_MARKER",
        "PRIVATE_SYNTHETIC_RESPONSE_ID_MARKER",
        "PRIVATE_SYNTHETIC_APPROVAL_ID_MARKER",
        "PRIVATE_SYNTHETIC_TOKEN_MARKER",
        "PRIVATE_SYNTHETIC_CONVERSATION_PURPOSE_MARKER",
        "PRIVATE_SYNTHETIC_TOOL_NAME_MARKER",
        "PRIVATE_SYNTHETIC_SCHEMA_VERSION_MARKER",
        "PRIVATE_SYNTHETIC_SOURCE_VERSION_MARKER",
        "PRIVATE_SYNTHETIC_SOURCE_VERSION_MAP_MARKER",
        "PRIVATE_SYNTHETIC_QUESTION_MARKER",
        "PRIVATE_SYNTHETIC_POTENTIAL_MARKER",
        "PRIVATE_SYNTHETIC_HYPOTHESIS_MARKER",
        "PRIVATE_SYNTHETIC_ACTION_MARKER",
        "PRIVATE_SYNTHETIC_OPINION_MARKER",
        "a" * 64,
        invocation.operation_key,
    ):
        assert private_marker not in rendered

    index = json.loads(bundle.index)
    manifest = json.loads(bundle.manifest)
    assert any(
        item["kind"] == "model_conversation_turn" and item["id"] == turn.id
        for item in index["resources"]
    )
    assert any(item["kind"] == "raw_output" for item in index["resources"])
    assert any(item["kind"] == "normalized_output" for item in index["resources"])
    assert any(
        item["kind"] == "detection_candidate" and item["id"] == candidate.id
        for item in index["resources"]
    )
    assert any(
        item["kind"] == "detection_candidate_test_reference" and item["id"] == candidate_test_ref
        for item in index["resources"]
    )
    assert all(
        item.get("authorization") == "existing application authorization required"
        for item in index["resources"]
    )
    assert "signed URLs" in index["policy"]
    assert manifest["counts"]["events"] == len(rows)
    assert "operation_key_sha256" in manifest["correlation_keys"]
    assert manifest["source_snapshot"]["versions"] == {
        "audit_projection": "autowork.investigation-audit-event.v1"
    }
    assert manifest["files"]["RUN_EVENTS.jsonl.gz"]["records"] == len(rows)
    assert any(
        gap["field"] == "consultation_id" and gap["value"] == "UNKNOWN" for gap in manifest["gaps"]
    )
