from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from cti_app.application.investigation_audit_export import (
    AuditBundle,
    build_audit_bundle,
    write_audit_bundle,
)
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    AgendaItem,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationExecutionMode,
    InvestigationFinding,
    NoveltyClass,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
)
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
    ModelTransport,
    ModelUsage,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.investigation_audit_reader import (
    read_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.models.investigation import (
    AnalystOpinionVersionRow,
    EvidenceObservationRow,
    InvestigationFindingVersionRow,
    InvestigationWorkingStateRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.models.production import AnalystInvestigationRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PRIVATE_MARKER = "PRIVATE_SYNTHETIC_AUDIT_FIXTURE_MARKER"


@dataclass(frozen=True, slots=True)
class _InvestigationRoot:
    investigation: AnalystInvestigation
    artifact: ProductionArtifact
    conversation: ModelConversation


class _MismatchedArtifactReader:
    def __init__(self, blob_id: UUID, stored_bytes: bytes) -> None:
        self.blob_id = blob_id
        self.stored_bytes = stored_bytes
        self.read_ids: list[UUID] = []

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        assert max_bytes >= len(self.stored_bytes)
        self.read_ids.append(blob_id)
        assert blob_id == self.blob_id
        return self.stored_bytes + b" "


async def _seed_investigation_root(uow: SqlAlchemyUnitOfWork, *, label: str) -> _InvestigationRoot:
    suffix = uuid4().hex
    edition = Edition(
        country=f"L3 synthetic {label} {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title=f"L3 synthetic {label}",
        slug=f"l3-{label}-{suffix}",
        tlp=TLP.AMBER,
    )
    production_run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    artifact = ProductionArtifact(
        production_run_id=production_run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="d" * 64,
    )
    conversation = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.OPENAI_RESPONSES,
        purpose=ConversationPurpose.ANALYST_ASSISTANCE,
        title=f"L3 synthetic {label} conversation",
        edition_id=edition.id,
        subject_id=subject.id,
        status=ConversationStatus.READY,
        created_at=NOW,
        updated_at=NOW,
    )
    investigation = AnalystInvestigation(
        production_run_id=production_run.id,
        subject_id=subject.id,
        synthesis_artifact_id=artifact.id,
        budget=LoopBudget(),
        pivot_conversation_id=conversation.id,
        created_at=NOW,
        updated_at=NOW,
    )

    assert await uow.editions.add_if_absent(edition)
    await uow.subjects.add(subject)
    await uow.production_runs.add(production_run)
    await uow.production_artifacts.append(artifact)
    await uow.model_conversations.add(conversation)
    await uow.analyst_investigations.add(investigation)
    return _InvestigationRoot(investigation, artifact, conversation)


def _blob(data: bytes, *, logical_bucket: str) -> BlobRecord:
    return BlobRecord(
        descriptor=BlobDescriptor(
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mime_type="application/json",
            logical_bucket=logical_bucket,
        )
    )


def _model_run(index: int, *, now: datetime) -> ModelRun:
    return ModelRun(
        provider=ModelProvider.OPENAI,
        backend=ModelBackend.CHATGPT_BRIDGE,
        transport=ModelTransport.OPENAI_RESPONSES,
        model_role=ModelRole.RESEARCH,
        requested_model=f"synthetic-model-{index}",
        actual_model_version=f"synthetic-model-version-{index}",
        prompt_template_id="l3-audit-test",
        prompt_template_version="1",
        authorized_input_hash=("a" if index == 1 else "b") * 64,
        evidence_pack_hash=("c" if index == 1 else "e") * 64,
        parameters={"fixture": "synthetic"},
        duration_ms=25 + index,
        usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        status=ModelRunStatus.SUCCEEDED,
        submission_state=ModelSubmissionState.RESULT_OBTAINED,
        submission_attempt=1,
        bridge_request_id=f"synthetic-bridge-request-{index}",
        response_id=f"synthetic-response-{index}",
        raw_output_reference=f"blob://{uuid4()}",
        raw_output_sha256=("f" if index == 1 else "1") * 64,
        raw_output_chars=41 + index,
        normalized_output_reference=f"blob://{uuid4()}",
        normalized_output_sha256=("2" if index == 1 else "3") * 64,
        parser_stage="structured_parse",
        serializer_version="synthetic-serializer.v1",
        normalization_version="synthetic-normalizer.v1",
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )


def _turn(
    conversation_id: UUID,
    model_run_id: UUID,
    *,
    sequence: int,
    now: datetime,
    parent_turn_id: UUID | None = None,
) -> ModelConversationTurn:
    return ModelConversationTurn(
        conversation_id=conversation_id,
        sequence=sequence,
        model_run_id=model_run_id,
        input_blob_reference=f"blob://{uuid4()}",
        input_sha256=("4" if sequence == 1 else "5") * 64,
        output_blob_reference=f"blob://{uuid4()}",
        output_sha256=("6" if sequence == 1 else "7") * 64,
        status=ConversationTurnStatus.SUCCEEDED,
        idempotency_key=f"l3-synthetic-turn-{uuid4().hex}",
        parent_turn_id=parent_turn_id,
        correlation_id=f"l3-synthetic-correlation-{sequence}",
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _decoded_events(bundle: AuditBundle) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(bundle.events).splitlines() if line]


def _encode_events(rows: list[dict[str, Any]]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        for row in rows:
            stream.write(_canonical_bytes(row) + b"\n")
    return output.getvalue()


def _bundle_with_events(bundle: AuditBundle, rows: list[dict[str, Any]]) -> AuditBundle:
    return replace(bundle, events=_encode_events(rows), event_count=len(rows))


def _assert_bundle_matches_database_rows(bundle: AuditBundle, expected: dict[str, Any]) -> None:
    rows = _decoded_events(bundle)
    source_events = rows[:-1]
    snapshot_event = rows[-1]
    investigation_id = expected["investigation_id"]

    assert bundle.event_count == len(rows), "bundle event count does not match RUN_EVENTS"
    assert snapshot_event["type"] == "audit.snapshot", "audit snapshot is not the terminal event"
    assert all(row["correlation"].get("investigation_id") == investigation_id for row in rows), (
        "wrong investigation scope in RUN_EVENTS"
    )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in source_events:
        by_type.setdefault(row["type"], []).append(row)
    invocation_events = by_type.get("tool_invocation.snapshot", [])
    observation_events = by_type.get("evidence_observation.recorded", [])
    finding_events = by_type.get("finding.version_recorded", [])
    opinion_version_events = by_type.get("opinion.version_recorded", [])
    turn_events = by_type.get("model_conversation.turn", [])
    run_events = by_type.get("model_run.snapshot", [])

    assert {row["record"]["id"] for row in invocation_events} == set(expected["invocations"]), (
        "invocation IDs or counts differ from PostgreSQL"
    )
    assert {row["record"]["id"] for row in observation_events} == set(expected["observations"]), (
        "observation IDs or counts differ from PostgreSQL"
    )
    assert {(row["record"]["id"], row["record"]["version"]) for row in finding_events} == set(
        expected["findings"]
    ), "finding IDs, versions, or counts differ from PostgreSQL"
    assert {
        (row["record"]["id"], row["record"]["version"]) for row in opinion_version_events
    } == set(expected["opinions"]), "opinion IDs, versions, or counts differ from PostgreSQL"
    assert {row["record"]["id"] for row in turn_events} == set(expected["turns"]), (
        "conversation turn IDs or scope differ from PostgreSQL"
    )
    assert {row["record"]["id"] for row in run_events} == set(expected["model_runs"]), (
        "linked ModelRun IDs or scope differ from PostgreSQL"
    )

    invocation_by_id = {row["record"]["id"]: row for row in invocation_events}
    for invocation_id, source in expected["invocations"].items():
        event_row = invocation_by_id[invocation_id]
        assert event_row["record"]["version"] == source["version"]
        assert event_row["details"]["status_at_snapshot"] == source["status"]
        assert event_row["details"]["response_refs"] == source["response_refs"]

    observation_by_id = {row["record"]["id"]: row for row in observation_events}
    for observation_id, source in expected["observations"].items():
        event_row = observation_by_id[observation_id]
        assert event_row["details"]["sha256"] == source["sha256"], (
            "observation SHA-256 differs from PostgreSQL"
        )
        assert event_row["details"]["blob_ref"] == source["blob_id"]
        assert event_row["correlation"].get("invocation_id") == source["invocation_ref"]
        assert event_row["details"]["content_included"] is False

    finding_by_key = {
        (row["record"]["id"], row["record"]["version"]): row for row in finding_events
    }
    for key, source in expected["findings"].items():
        details = finding_by_key[key]["details"]
        assert details["statement_sha256"] == source["statement_sha256"]
        assert details["observation_refs"] == source["observation_refs"]
        assert details["content_included"] is False

    opinion_by_key = {
        (row["record"]["id"], row["record"]["version"]): row for row in opinion_version_events
    }
    for key, source in expected["opinions"].items():
        details = opinion_by_key[key]["details"]
        assert details["content_sha256"] == source["content_sha256"]
        assert details["status"] == source["status"]
        assert details["content_included"] is False

    opinion_block_events = {
        (row["record"]["id"], row["record"]["version"]): row
        for row in by_type.get("opinion.block_decision", [])
    }
    for key, source in expected["opinions"].items():
        block_events = opinion_block_events[key]
        assert block_events["details"]["finding_refs"] == source["finding_refs"]
        assert block_events["details"]["evidence_refs"] == source["evidence_refs"]

    state_events = by_type.get("operator_state.snapshot", [])
    assert len(state_events) == 1
    state_details = state_events[0]["details"]
    assert state_events[0]["record"]["id"] == investigation_id
    assert state_details["version"] == expected["state_version"]
    assert state_details["hypotheses"][0]["evidence_refs"] == expected["state_evidence_refs"]
    assert state_details["hypotheses"][0]["finding_refs"] == expected["state_finding_refs"]

    input_pack_events = by_type.get("input_pack.snapshot", [])
    assert len(input_pack_events) == 1
    input_pack_event = input_pack_events[0]
    assert input_pack_event["record"]["version"] == expected["investigation_version"]
    assert input_pack_event["details"]["investigation_status"] == expected["investigation_status"]
    assert input_pack_event["details"]["synthesis_artifact_id"] == expected["synthesis_artifact_id"]
    assert input_pack_event["details"]["blob_id"] == "UNKNOWN"
    assert input_pack_event["details"]["sha256"] == "UNKNOWN"

    conversation_events = by_type.get("model_conversation.snapshot", [])
    assert len(conversation_events) == 1
    assert conversation_events[0]["record"]["id"] == expected["conversation_id"]
    assert conversation_events[0]["record"]["version"] == expected["conversation_version"]
    assert (
        conversation_events[0]["details"]["head_turn_id"] == expected["conversation_head_turn_id"]
    )

    turns_by_id = {row["record"]["id"]: row for row in turn_events}
    for turn_id, source in expected["turns"].items():
        details = turns_by_id[turn_id]["details"]
        assert details["sequence"] == source["sequence"]
        assert details["model_run_id"] == source["model_run_id"]
        assert details["input_sha256"] == source["input_sha256"]
        assert details["output_sha256"] == source["output_sha256"]
        assert details["parent_turn_id"] == source["parent_turn_id"]

    runs_by_id = {row["record"]["id"]: row for row in run_events}
    for run_id, source in expected["model_runs"].items():
        details = runs_by_id[run_id]["details"]
        assert details["authorized_input_hash"] == source["authorized_input_hash"]
        assert details["evidence_pack_hash"] == source["evidence_pack_hash"]
        assert details["raw_output_sha256"] == source["raw_output_sha256"]
        assert details["normalized_output_sha256"] == source["normalized_output_sha256"]
        assert runs_by_id[run_id]["correlation"]["turn_id"] == source["turn_id"]

    advisor_observation_id = expected["advisor_observation_id"]
    advisor_event = next(
        row
        for row in by_type.get("advisor_call.metadata", [])
        if row["record"]["id"] == advisor_observation_id
    )
    advisor_details = advisor_event["details"]
    assert advisor_details["expected_sha256"] == expected["advisor_sha256"]
    assert advisor_details["read_sha256"] != expected["advisor_sha256"]
    assert advisor_details["read_status"] == "hash_mismatch"
    assert advisor_details["metadata"]["advisor_blob_available"] is False
    assert advisor_details["metadata"]["bridge_invocation_id"] == "UNKNOWN"
    assert advisor_details["bridge_turn_link"] == "UNKNOWN"
    assert advisor_details["linked_invocation_ids"] == [expected["advisor_invocation_ref"]]
    assert advisor_event["correlation"].get("invocation_id") == expected["advisor_invocation_ref"]
    assert any(
        gap["field"] == "bridge_invocation_id" and "UNKNOWN" in gap["value"]
        for gap in advisor_details["metadata"]["gaps"]
    )

    counts = Counter(row["type"] for row in rows)
    expected_counts = Counter(
        {
            "tool_invocation.snapshot": len(expected["invocations"]),
            "evidence_observation.recorded": len(expected["observations"]),
            "advisor_call.metadata": 1,
            "finding.version_recorded": len(expected["findings"]),
            "opinion.version_recorded": len(expected["opinions"]),
            "opinion.block_decision": len(expected["opinions"]),
            "input_pack.snapshot": 1,
            "model_conversation.snapshot": 1,
            "model_conversation.turn": len(expected["turns"]),
            "model_run.snapshot": len(expected["model_runs"]),
            "operator_state.snapshot": 1,
            "audit.snapshot": 1,
        }
    )
    assert counts == expected_counts, "event type counts differ from canonical PostgreSQL rows"
    assert snapshot_event["details"]["event_counts_before_snapshot"] == dict(
        sorted(Counter(row["type"] for row in source_events).items())
    )
    assert snapshot_event["details"]["event_count_before_snapshot"] == len(source_events)
    assert (
        snapshot_event["details"]["projected_source_sha256"]
        == hashlib.sha256(_canonical_bytes(source_events)).hexdigest()
    )

    def order_key(row: dict[str, Any]) -> tuple[bool, str, str, str, str]:
        return (
            row["occurred_at"] is None,
            row["occurred_at"] or "",
            row["type"],
            row["record"]["id"],
            row["event_id"],
        )

    assert source_events == sorted(source_events, key=order_key), "RUN_EVENTS order is unstable"
    for row in rows:
        event_body = {key: value for key, value in row.items() if key != "event_id"}
        assert row["event_id"] == hashlib.sha256(_canonical_bytes(event_body)).hexdigest()

    index_value = json.loads(bundle.index)
    assert index_value["investigation_id"] == investigation_id
    resources = index_value["resources"]
    resource_keys = {
        (item["kind"], item["id"], str(item.get("version") or "")) for item in resources
    }
    assert all(item["included"] is False for item in resources)
    assert {
        ("evidence_observation", observation_id, "") for observation_id in expected["observations"]
    } <= resource_keys
    assert {
        ("finding", finding_id, str(version)) for finding_id, version in expected["findings"]
    } <= resource_keys
    assert {
        ("analyst_opinion", opinion_id, str(version))
        for opinion_id, version in expected["opinions"]
    } <= resource_keys
    assert {
        ("model_conversation_turn", turn_id, "") for turn_id in expected["turns"]
    } <= resource_keys
    assert {("model_run", run_id, "") for run_id in expected["model_runs"]} <= resource_keys
    assert (
        "tool_response_reference",
        expected["missing_response_ref"],
        "",
    ) in resource_keys, "missing response reference was not retained in RUN_INDEX"
    missing_reference = next(
        item
        for item in resources
        if item["kind"] == "tool_response_reference"
        and item["id"] == expected["missing_response_ref"]
    )
    assert "target type may be UNKNOWN" in missing_reference["retrieval"]
    assert not any(row["record"]["id"] == expected["missing_response_ref"] for row in rows), (
        "missing response ref incorrectly resolved to a persisted event"
    )

    resources_by_key = {
        (item["kind"], item["id"], str(item.get("version") or "")): item for item in resources
    }
    for observation_id, source in expected["observations"].items():
        resource = resources_by_key[("evidence_observation", observation_id, "")]
        assert resource["sha256"] == source["sha256"]
        assert resource["blob_id"] == source["blob_id"]
    for (finding_id, version), source in expected["findings"].items():
        assert (
            resources_by_key[("finding", finding_id, str(version))]["content_sha256"]
            == (source["statement_sha256"])
        )
    for (opinion_id, version), source in expected["opinions"].items():
        assert (
            resources_by_key[("analyst_opinion", opinion_id, str(version))]["content_sha256"]
            == source["content_sha256"]
        )
    for turn_id, source in expected["turns"].items():
        resource = resources_by_key[("model_conversation_turn", turn_id, "")]
        assert resource["input_sha256"] == source["input_sha256"]
        assert resource["output_sha256"] == source["output_sha256"]
        assert resource["model_run_id"] == source["model_run_id"]
    for run_id, source in expected["model_runs"].items():
        resource = resources_by_key[("model_run", run_id, "")]
        assert resource["raw_output_sha256"] == source["raw_output_sha256"]
        assert resource["normalized_output_sha256"] == source["normalized_output_sha256"]
    assert not any(item["id"] in expected["foreign_ids"] for item in resources)
    assert index_value["unavailable_resources"] == [
        {
            "kind": "tool_invocation_model_run_relation",
            "id": None,
            "availability": "UNKNOWN",
            "reason": "ToolInvocation has no direct ModelRun foreign key",
        },
        {
            "kind": "pdf_and_qa_artifact",
            "id": None,
            "availability": "UNKNOWN",
            "reason": "no PDF/QA artifact link is stored on the selected investigation records",
        },
    ]

    trace_text = bundle.trace.decode("utf-8")
    expected_record_ids = {row["record"]["id"] for row in source_events}
    assert all(record_id in trace_text for record_id in expected_record_ids)
    assert PRIVATE_MARKER not in trace_text
    assert PRIVATE_MARKER not in gzip.decompress(bundle.events).decode("utf-8")
    assert PRIVATE_MARKER not in bundle.index.decode("utf-8")
    assert PRIVATE_MARKER not in bundle.manifest.decode("utf-8")

    manifest = json.loads(bundle.manifest)
    assert manifest["investigation_id"] == investigation_id
    assert manifest["counts"]["events"] == len(rows)
    assert manifest["counts"]["by_type"] == dict(sorted(counts.items()))
    assert manifest["counts"]["resources"] == len(resources)
    assert manifest["counts"]["unavailable_resources"] == len(index_value["unavailable_resources"])
    assert manifest["source_snapshot"]["versions"] == expected["source_versions"]
    assert manifest["source_snapshot"]["pivot_conversation_id"] == expected["conversation_id"]
    record_refs = manifest["source_snapshot"]["record_refs"]
    assert record_refs == [
        {
            "event_id": row["event_id"],
            "type": row["type"],
            "id": row["record"]["id"],
            "version": row["record"].get("version"),
        }
        for row in source_events
    ]
    expected_file_hashes = {
        "RUN_EVENTS.jsonl.gz": bundle.events,
        "RUN_TRACE.md": bundle.trace,
        "RUN_INDEX.json": bundle.index,
    }
    for filename, contents in expected_file_hashes.items():
        entry = manifest["files"][filename]
        assert entry["sha256"] == hashlib.sha256(contents).hexdigest()
        assert entry["bytes"] == len(contents)
    assert manifest["files"]["RUN_EVENTS.jsonl.gz"]["records"] == len(rows)
    assert manifest["files"]["RUN_INDEX.json"]["resources"] == len(resources)
    assert manifest["files"]["RUN_MANIFEST.json"]["self_sha256"] == "NOT_COMPUTED"


@pytest.mark.asyncio
async def test_postgres_audit_snapshot_projects_scoped_rows_and_resumes_without_reread(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    sql_statements: list[str] = []

    def record_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        sql_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            target = await _seed_investigation_root(uow, label="target")
            foreign = await _seed_investigation_root(uow, label="foreign")

            missing_response_ref = uuid4()
            normal_blob_bytes = b'{"fixture":"ordinary observation"}'
            advisor_blob_bytes = json.dumps(
                {
                    "schema": "operator-advisor-call.v1",
                    "consultation_type": "review_text",
                    "advisor_status": "succeeded",
                    "bridge_invocation_id": str(target.conversation.id),
                    "bridge_invocation_id_kind": "conversation_turn",
                    "idempotency_key": "synthetic-advisor-idempotency",
                    "prompt_sha256": "8" * 64,
                    "dossier_sha256": "9" * 64,
                    "output": {"content": PRIVATE_MARKER},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            normal_blob = _blob(normal_blob_bytes, logical_bucket="investigation-test")
            advisor_blob = _blob(advisor_blob_bytes, logical_bucket="investigation-test")

            normal_observation = EvidenceObservation(
                investigation_id=target.investigation.id,
                kind=EvidenceKind.OTHER,
                blob_id=normal_blob.id,
                sha256=normal_blob.descriptor.sha256,
                source="synthetic.test",
                source_version=PRIVATE_MARKER,
                observed_at=NOW + timedelta(seconds=2),
                object_type="synthetic_record",
                object_id="normal-observation",
                limitations=("synthetic fixture",),
            )
            advisor_observation = EvidenceObservation(
                investigation_id=target.investigation.id,
                kind=EvidenceKind.OTHER,
                blob_id=advisor_blob.id,
                sha256=advisor_blob.descriptor.sha256,
                source="advisor.consult",
                source_version="operator-advisor-call.v1",
                observed_at=NOW + timedelta(seconds=3),
                object_type="advisor_call",
                object_id="review_text",
                limitations=(),
            )

            first_invocation = ToolInvocation(
                investigation_id=target.investigation.id,
                operation_key=f"l3-target-operation-{uuid4().hex}",
                tool_name="synthetic.inspect",
                schema_version="v1",
                normalized_parameters={"fixture": "target"},
                purpose="synthetic audit reader fixture",
                created_at=NOW,
                updated_at=NOW,
            )
            second_invocation = ToolInvocation(
                investigation_id=target.investigation.id,
                operation_key=f"l3-target-operation-{uuid4().hex}",
                tool_name="synthetic.review",
                schema_version="v2",
                normalized_parameters={"fixture": "second"},
                purpose="synthetic second invocation",
                created_at=NOW + timedelta(seconds=6),
                updated_at=NOW + timedelta(seconds=6),
            )
            await uow.blobs.add(normal_blob)
            await uow.blobs.add(advisor_blob)
            await uow.tool_invocations.add_if_absent(first_invocation)
            running_invocation = first_invocation.transition(
                ToolInvocationStatus.RUNNING, now=NOW + timedelta(seconds=1)
            )
            await uow.tool_invocations.update_if_version(running_invocation, expected_version=1)
            completed_invocation = running_invocation.transition(
                ToolInvocationStatus.SUCCEEDED,
                response_refs=(
                    normal_observation.id,
                    advisor_observation.id,
                    missing_response_ref,
                ),
                now=NOW + timedelta(seconds=4),
            )
            await uow.tool_invocations.update_if_version(completed_invocation, expected_version=2)
            await uow.tool_invocations.add_if_absent(second_invocation)
            denied_invocation = second_invocation.transition(
                ToolInvocationStatus.POLICY_DENIED, now=NOW + timedelta(seconds=7)
            )
            await uow.tool_invocations.update_if_version(denied_invocation, expected_version=1)

            normal_observation = replace(normal_observation, invocation_ref=completed_invocation.id)
            advisor_observation = replace(
                advisor_observation, invocation_ref=completed_invocation.id
            )
            await uow.evidence_observations.append_if_absent(normal_observation)
            await uow.evidence_observations.append_if_absent(advisor_observation)

            first_finding = InvestigationFinding(
                investigation_id=target.investigation.id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.SUPPORTED,
                statement=PRIVATE_MARKER,
                observation_refs=(normal_observation.id,),
                claim_refs=("synthetic:claim:v1",),
                novelty=NoveltyClass.NEW_TO_ARTICLE,
                scope="synthetic integration fixture",
                limitations=("synthetic only",),
                created_at=NOW + timedelta(seconds=8),
            )
            second_finding = replace(
                first_finding,
                id=uuid4(),
                version=2,
                supersedes=first_finding.id,
                statement="Synthetic finding revision two.",
                observation_refs=(advisor_observation.id,),
                claim_refs=("synthetic:claim:v2",),
                created_at=NOW + timedelta(seconds=9),
            )
            await uow.investigation_findings.append_version(
                first_finding, expected_previous_id=None
            )
            await uow.investigation_findings.append_version(
                second_finding, expected_previous_id=first_finding.id
            )

            opinion = AnalystOpinionV1(
                investigation_id=target.investigation.id,
                synthesis_artifact_id=target.artifact.id,
                synthesis_sha256=target.artifact.input_hash,
                base_publication_artifact_id=uuid4(),
                base_publication_sha256="b" * 64,
                blocks=(
                    OpinionBlock(
                        block_type=OpinionBlockType.KEY_FINDINGS,
                        text=PRIVATE_MARKER,
                        finding_refs=(second_finding.id,),
                        evidence_refs=(normal_observation.id,),
                        qualification=OpinionClaimQualification.FACT,
                    ),
                ),
                author_id="synthetic-analyst",
                created_at=NOW + timedelta(seconds=10),
            )
            await uow.analyst_opinions.append_version(opinion, expected_version=0)
            approved_opinion = replace(
                opinion,
                version=2,
                status=AnalystOpinionStatus.APPROVED,
                reviewer_id="synthetic-reviewer",
                created_at=NOW + timedelta(seconds=11),
                content_sha256="",
            )
            await uow.analyst_opinions.append_version(approved_opinion, expected_version=1)

            hypothesis = Hypothesis(
                id="synthetic-hypothesis",
                statement=PRIVATE_MARKER,
                status=HypothesisStatus.SUPPORTED,
                evidence_refs=(normal_observation.id,),
                finding_refs=(second_finding.id,),
            )
            state = InvestigationWorkingState(
                investigation_id=target.investigation.id,
                agenda=InvestigationAgenda(
                    items=(
                        AgendaItem(question=PRIVATE_MARKER, potential=0.8, rank=1),
                        AgendaItem(
                            question="Synthetic second agenda question", potential=0.4, rank=2
                        ),
                    )
                ),
                hypotheses=(hypothesis,),
                execution_mode=InvestigationExecutionMode.OPERATOR_LED,
            )
            await uow.investigation_states.save_if_version(state, expected_version=None)
            state_v2 = replace(state, version=2, turn_index=1, recent_actions=(PRIVATE_MARKER,))
            await uow.investigation_states.save_if_version(state_v2, expected_version=1)

            target_run_1 = _model_run(1, now=NOW + timedelta(seconds=12))
            target_run_2 = _model_run(2, now=NOW + timedelta(seconds=14))
            target_turn_1 = _turn(
                target.conversation.id,
                target_run_1.id,
                sequence=1,
                now=NOW + timedelta(seconds=13),
            )
            target_turn_2 = _turn(
                target.conversation.id,
                target_run_2.id,
                sequence=2,
                now=NOW + timedelta(seconds=15),
                parent_turn_id=target_turn_1.id,
            )
            foreign_run = _model_run(3, now=NOW + timedelta(seconds=12))
            foreign_turn = _turn(
                foreign.conversation.id,
                foreign_run.id,
                sequence=1,
                now=NOW + timedelta(seconds=13),
            )
            await uow.model_runs.add(target_run_1)
            await uow.model_runs.add(target_run_2)
            await uow.model_runs.add(foreign_run)
            await uow.model_conversation_turns.add(target_turn_1)
            await uow.model_conversation_turns.add(target_turn_2)
            await uow.model_conversation_turns.add(foreign_turn)
            await uow.model_conversations.save(
                replace(
                    target.conversation,
                    head_turn_id=target_turn_2.id,
                    turn_count=2,
                    version=2,
                    updated_at=NOW + timedelta(seconds=16),
                    last_used_at=NOW + timedelta(seconds=16),
                )
            )
            await uow.model_conversations.save(
                replace(
                    foreign.conversation,
                    head_turn_id=foreign_turn.id,
                    turn_count=1,
                    version=2,
                    updated_at=NOW + timedelta(seconds=16),
                    last_used_at=NOW + timedelta(seconds=16),
                )
            )

            foreign_blob = _blob(b'{"fixture":"foreign"}', logical_bucket="investigation-test")
            await uow.blobs.add(foreign_blob)
            foreign_invocation = ToolInvocation(
                investigation_id=foreign.investigation.id,
                operation_key=f"l3-foreign-operation-{uuid4().hex}",
                tool_name="synthetic.foreign",
                schema_version="v1",
                normalized_parameters={"fixture": "foreign"},
                purpose="out-of-scope synthetic row",
                status=ToolInvocationStatus.POLICY_DENIED,
                created_at=NOW,
                updated_at=NOW,
            )
            foreign_observation = EvidenceObservation(
                investigation_id=foreign.investigation.id,
                kind=EvidenceKind.OTHER,
                blob_id=foreign_blob.id,
                sha256=foreign_blob.descriptor.sha256,
                source="synthetic.foreign",
                source_version="v1",
                observed_at=NOW,
                object_type="synthetic_record",
                object_id="foreign-observation",
                invocation_ref=foreign_invocation.id,
            )
            await uow.tool_invocations.add_if_absent(foreign_invocation)
            await uow.evidence_observations.append_if_absent(foreign_observation)
            await uow.commit()

        async with session_factory() as session:
            target_id = target.investigation.id
            invocation_rows = list(
                await session.scalars(
                    select(ToolInvocationRow)
                    .where(ToolInvocationRow.investigation_id == target_id)
                    .order_by(ToolInvocationRow.created_at, ToolInvocationRow.id)
                )
            )
            observation_rows = list(
                await session.scalars(
                    select(EvidenceObservationRow)
                    .where(EvidenceObservationRow.investigation_id == target_id)
                    .order_by(EvidenceObservationRow.observed_at, EvidenceObservationRow.id)
                )
            )
            finding_rows = list(
                await session.scalars(
                    select(InvestigationFindingVersionRow)
                    .where(InvestigationFindingVersionRow.investigation_id == target_id)
                    .order_by(
                        InvestigationFindingVersionRow.created_at,
                        InvestigationFindingVersionRow.id,
                    )
                )
            )
            opinion_rows = list(
                await session.scalars(
                    select(AnalystOpinionVersionRow)
                    .where(AnalystOpinionVersionRow.investigation_id == target_id)
                    .order_by(
                        AnalystOpinionVersionRow.opinion_id,
                        AnalystOpinionVersionRow.version,
                    )
                )
            )
            state_row = await session.get(InvestigationWorkingStateRow, target_id)
            investigation_row = await session.get(AnalystInvestigationRow, target_id)
            conversation_row = await session.get(ModelConversationRow, target.conversation.id)
            turn_rows = list(
                await session.scalars(
                    select(ModelConversationTurnRow)
                    .where(ModelConversationTurnRow.conversation_id == target.conversation.id)
                    .order_by(ModelConversationTurnRow.sequence)
                )
            )
            target_run_ids = [row.model_run_id for row in turn_rows]
            model_run_rows = list(
                await session.scalars(
                    select(ModelRunRow)
                    .where(ModelRunRow.id.in_(target_run_ids))
                    .order_by(ModelRunRow.id)
                )
            )
            assert state_row is not None
            assert investigation_row is not None
            assert conversation_row is not None
            assert await session.get(ToolInvocationRow, foreign_invocation.id) is not None
            assert await session.get(EvidenceObservationRow, foreign_observation.id) is not None
            assert await session.get(ModelConversationTurnRow, foreign_turn.id) is not None
            assert await session.get(ModelRunRow, foreign_run.id) is not None
            assert await session.get(EvidenceObservationRow, missing_response_ref) is None

        expected = {
            "investigation_id": str(target.investigation.id),
            "conversation_id": str(conversation_row.id),
            "source_versions": {
                "investigation_state": "investigation-state.v1",
                "investigation_experiment": "investigation-experiment.v1",
                "analyst_opinion": "analyst-opinion.v1",
                "model_conversation": "model-conversation.repository.v1",
                "advisor_call_payload": "operator-advisor-call.v1",
                "audit_projection": "autowork.investigation-audit-event.v1",
            },
            "invocations": {
                str(row.id): {
                    "version": row.version,
                    "status": row.status,
                    "response_refs": sorted(row.response_refs),
                }
                for row in invocation_rows
            },
            "observations": {
                str(row.id): {
                    "sha256": row.sha256,
                    "blob_id": str(row.blob_id),
                    "invocation_ref": str(row.invocation_ref) if row.invocation_ref else None,
                }
                for row in observation_rows
            },
            "findings": {
                (str(row.id), row.version): {
                    "statement_sha256": hashlib.sha256(row.statement.encode("utf-8")).hexdigest(),
                    "observation_refs": sorted(row.observation_ids),
                }
                for row in finding_rows
            },
            "opinions": {
                (str(row.opinion_id), row.version): {
                    "content_sha256": row.content_sha256,
                    "status": row.status,
                    "finding_refs": sorted(row.content["blocks"][0]["finding_refs"]),
                    "evidence_refs": sorted(row.content["blocks"][0]["evidence_refs"]),
                }
                for row in opinion_rows
            },
            "state_version": state_row.version,
            "state_evidence_refs": sorted(state_row.state["hypotheses"][0]["evidence_refs"]),
            "state_finding_refs": sorted(state_row.state["hypotheses"][0]["finding_refs"]),
            "investigation_version": investigation_row.version,
            "investigation_status": investigation_row.status,
            "synthesis_artifact_id": str(investigation_row.synthesis_artifact_id),
            "turns": {
                str(row.id): {
                    "sequence": row.sequence,
                    "model_run_id": str(row.model_run_id),
                    "input_sha256": row.input_sha256,
                    "output_sha256": row.output_sha256,
                    "parent_turn_id": str(row.parent_turn_id) if row.parent_turn_id else None,
                }
                for row in turn_rows
            },
            "model_runs": {
                str(row.id): {
                    "authorized_input_hash": row.authorized_input_hash,
                    "evidence_pack_hash": row.evidence_pack_hash,
                    "raw_output_sha256": row.raw_output_sha256,
                    "normalized_output_sha256": row.normalized_output_sha256,
                    "turn_id": str(
                        next(turn.id for turn in turn_rows if turn.model_run_id == row.id)
                    ),
                }
                for row in model_run_rows
            },
            "advisor_observation_id": str(advisor_observation.id),
            "advisor_sha256": advisor_blob.descriptor.sha256,
            "advisor_invocation_ref": str(
                next(
                    row.invocation_ref
                    for row in observation_rows
                    if row.id == advisor_observation.id
                )
            ),
            "conversation_version": conversation_row.version,
            "conversation_head_turn_id": str(conversation_row.head_turn_id),
            "missing_response_ref": str(missing_response_ref),
            "foreign_ids": {
                str(foreign.investigation.id),
                str(foreign.conversation.id),
                str(foreign_invocation.id),
                str(foreign_observation.id),
                str(foreign_turn.id),
                str(foreign_run.id),
            },
        }

        mismatched_store = _MismatchedArtifactReader(advisor_blob.id, advisor_blob_bytes)
        snapshot = await read_investigation_audit_snapshot(
            session_factory, target.investigation.id, mismatched_store
        )
        assert mismatched_store.read_ids == [advisor_blob.id]
        assert tuple(str(row.id) for row in snapshot.invocations) == tuple(expected["invocations"])
        assert tuple(str(row.id) for row in snapshot.observations) == tuple(
            expected["observations"]
        )
        assert tuple((str(row.id), row.version) for row in snapshot.findings) == tuple(
            expected["findings"]
        )
        assert tuple((str(row.id), row.version) for row in snapshot.opinions) == tuple(
            expected["opinions"]
        )
        assert tuple(str(row.id) for row in snapshot.conversation_turns) == tuple(expected["turns"])
        assert {str(row.id) for row in snapshot.model_runs} == set(expected["model_runs"])
        assert snapshot.hypotheses_state is not None
        assert snapshot.hypotheses_state.version == state_row.version
        assert snapshot.conversation is not None
        assert snapshot.conversation.id == target.conversation.id
        assert snapshot.conversation.version == conversation_row.version
        assert tuple(str(row.model_run_id) for row in snapshot.conversation_turns) == tuple(
            str(row.model_run_id) for row in turn_rows
        )
        assert tuple(str(row.id) for row in snapshot.model_runs) == tuple(
            str(row.model_run_id) for row in turn_rows
        )

        sql_statements.clear()
        bundle = build_audit_bundle(snapshot)
        assert sql_statements == [], "bundle construction reread PostgreSQL after snapshot capture"
        _assert_bundle_matches_database_rows(bundle, expected)

        untouched_rows = _decoded_events(bundle)
        with pytest.raises(AssertionError, match="observation IDs or counts"):
            omitted = copy.deepcopy(untouched_rows)
            omitted.remove(
                next(
                    row
                    for row in omitted
                    if row["type"] == "evidence_observation.recorded"
                    and row["record"]["id"] == str(normal_observation.id)
                )
            )
            _assert_bundle_matches_database_rows(_bundle_with_events(bundle, omitted), expected)

        with pytest.raises(AssertionError, match="wrong investigation scope"):
            wrong_scope = copy.deepcopy(untouched_rows)
            next(
                row
                for row in wrong_scope
                if row["type"] == "evidence_observation.recorded"
                and row["record"]["id"] == str(normal_observation.id)
            )["correlation"]["investigation_id"] = str(foreign.investigation.id)
            _assert_bundle_matches_database_rows(_bundle_with_events(bundle, wrong_scope), expected)

        with pytest.raises(AssertionError, match="observation SHA-256"):
            wrong_hash = copy.deepcopy(untouched_rows)
            observation_event = next(
                row
                for row in wrong_hash
                if row["type"] == "evidence_observation.recorded"
                and row["record"]["id"] == str(normal_observation.id)
            )
            observation_event["details"]["sha256"] = "0" * 64
            _assert_bundle_matches_database_rows(_bundle_with_events(bundle, wrong_hash), expected)

        assert _decoded_events(bundle) == untouched_rows, (
            "sensitivity perturbations changed source bundle"
        )
        _assert_bundle_matches_database_rows(bundle, expected)

        output_dir = tmp_path / "audit-bundle"
        written = write_audit_bundle(snapshot, output_dir, expected["investigation_id"])
        assert written.events == bundle.events
        assert written.trace == bundle.trace
        assert written.index == bundle.index
        assert written.manifest == bundle.manifest
        assert sql_statements == [], "writing a bundle from the captured snapshot reread PostgreSQL"

        resumed = write_audit_bundle(None, output_dir, expected["investigation_id"], resume=True)
        assert resumed.resumed_from_events is True
        assert resumed.event_count == bundle.event_count
        assert resumed.events == bundle.events
        assert resumed.trace == bundle.trace
        assert resumed.index == bundle.index
        assert resumed.manifest == bundle.manifest
        assert sql_statements == [], "checkpoint resume reread PostgreSQL"
        assert (output_dir / "RUN_EVENTS.jsonl.gz").read_bytes() == bundle.events
        assert (output_dir / "RUN_TRACE.md").read_bytes() == bundle.trace
        assert (output_dir / "RUN_INDEX.json").read_bytes() == bundle.index
        assert (output_dir / "RUN_MANIFEST.json").read_bytes() == bundle.manifest
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()
