"""PostgreSQL oracles for Bridge visual corrections of enrichment elements.

F2-05 wires typed chart/diagram/caption corrections into the discussion thread:
the element context is rebuilt from pinned artifacts, Bridge's marker wire is
converted into the typed patch, the shared planner validates it, and adoption
re-proves under the run lock without any model call. Reconciliation replay of
the exact turn must produce exactly one proposal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_discussion_context import (
    EditorialDiscussionContextBuilder,
)
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
)
from cti_app.application.editorial_proposals import EditorialProposalService
from cti_app.application.editorial_threads import EditorialThreadService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
)
from cti_app.application.production_synthesis import _all_evidence_entries
from cti_app.domain.editorial_enrichment_patch import UpdateVisualCaptionPatch
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    editorial_enrichment_from_json,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from tests.editorial_visual_revision_support import ALPHA_VALUE, BETA_VALUE
from tests.integration.editorial.test_enrichment_chart_diagram_revision_postgres import (
    _ready_artifact,
)
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _adopt,
    _enrichment_block,
    _read_enrichment,
)

pytestmark = pytest.mark.integration


def _wire(
    *,
    target: str,
    action: str,
    base_hash: str,
    before_hash: str,
    after: str,
    rationale: str = "Correction Bridge synthétique.",
) -> str:
    return (
        f"@@ TARGET @@\n{target}\n"
        f"@@ ACTION @@\n{action}\n"
        f"@@ BASE_ARTIFACT_HASH @@\n{base_hash}\n"
        f"@@ BEFORE_HASH @@\n{before_hash}\n"
        f"@@ AFTER @@\n{after}\n"
        f"@@ RATIONALE @@\n{rationale}\n"
        "@@ END @@"
    )


async def _wire_inputs(
    *,
    app: Any,
    uow_factory: Any,
    seeded: Any,
    block_id: UUID,
) -> tuple[str, str, str, dict[Any, str], dict[str, Any]]:
    async with uow_factory() as uow:
        record = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
    assert record is not None
    entry = next(item for item in record.block_registry if item.block_id == block_id)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        None,
        workspace_record=record,
    )
    handles = {ref: handle for handle, ref in evidence_pack._handle_to_ref.items()}
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    return (
        f"synthesis{entry.structural_pointer}",
        record.enrichment_canonical_sha256 or "",
        entry.content_sha256,
        handles,
        payload,
    )


def _handle_for_value(handles: dict[Any, str], seeded: Any, value: str) -> str:
    entries = _all_evidence_entries(seeded.extraction)
    reference = next(ref for ref, payload in entries.items() if payload.get("value") == value)
    return handles[reference]


def _chart_after(
    *,
    chart: Any,
    seeded: Any,
    handles: dict[Any, str],
    title: str,
    labels: tuple[str, str],
) -> str:
    alpha_handle = _handle_for_value(handles, seeded, ALPHA_VALUE)
    beta_handle = _handle_for_value(handles, seeded, BETA_VALUE)
    payload = {
        "tables": [],
        "diagrams": [],
        "figures": [],
        "annotations": [],
        "resource_needs": [],
        "charts": [
            {
                "key": chart.key,
                "kind": "bar",
                "title": title,
                "caption": chart.caption,
                "placement": {
                    "kind": chart.placement.kind.value,
                    "section_index": chart.placement.section_index,
                    "anchor_block_id": (
                        str(chart.placement.anchor_block_id)
                        if chart.placement.anchor_block_id is not None
                        else None
                    ),
                },
                "purpose": {
                    "question": chart.purpose.question,
                    "available_data": chart.purpose.available_data,
                    "comprehension_gain": chart.purpose.comprehension_gain,
                    "scope": chart.purpose.scope,
                    "knowledge_limits": chart.purpose.knowledge_limits,
                    "placement_reason": chart.purpose.placement_reason,
                    "evidence_handles": [alpha_handle, beta_handle],
                },
                "points": [
                    {
                        "label": labels[0],
                        "series": chart.points[0].series,
                        "evidence_handles": [alpha_handle],
                        "value": 12,
                    },
                    {
                        "label": labels[1],
                        "series": chart.points[1].series,
                        "evidence_handles": [beta_handle],
                        "value": 18,
                    },
                ],
                "category_axis_label": chart.category_axis_label,
                "value_axis_label": chart.value_axis_label,
                "unit": chart.unit,
            }
        ],
    }
    return "Voici le patch demandé.\n```json\n" + json.dumps(payload) + "\n```"


def _diagram_after(
    *,
    diagram: Any,
    seeded: Any,
    handles: dict[Any, str],
    node_label: str,
) -> str:
    alpha_handle = _handle_for_value(handles, seeded, ALPHA_VALUE)
    beta_handle = _handle_for_value(handles, seeded, BETA_VALUE)
    payload = {
        "tables": [],
        "charts": [],
        "figures": [],
        "annotations": [],
        "resource_needs": [],
        "diagrams": [
            {
                "key": diagram.key,
                "kind": diagram.kind.value,
                "title": diagram.title,
                "caption": diagram.caption,
                "direction": diagram.direction.value,
                "profile": diagram.profile.value,
                "placement": {"kind": diagram.placement.kind.value},
                "purpose": {
                    "question": diagram.purpose.question,
                    "available_data": diagram.purpose.available_data,
                    "comprehension_gain": diagram.purpose.comprehension_gain,
                    "scope": diagram.purpose.scope,
                    "knowledge_limits": diagram.purpose.knowledge_limits,
                    "placement_reason": diagram.purpose.placement_reason,
                    "evidence_handles": [alpha_handle],
                },
                "nodes": [
                    {
                        "node_id": diagram.nodes[0].node_id,
                        "label": node_label,
                        "role": diagram.nodes[0].role.value,
                        "evidence_handles": [alpha_handle],
                    },
                    {
                        "node_id": diagram.nodes[1].node_id,
                        "label": diagram.nodes[1].label,
                        "role": diagram.nodes[1].role.value,
                        "evidence_handles": [beta_handle],
                    },
                ],
                "edges": [
                    {
                        "source_node_id": edge.source_node_id,
                        "target_node_id": edge.target_node_id,
                        "label": edge.label,
                        "relation_type": edge.relation_type.value,
                        "direction": edge.direction.value,
                        "evidence_handles": [alpha_handle],
                    }
                    for edge in diagram.edges
                ],
                "groups": [],
            }
        ],
    }
    return "```json\n" + json.dumps(payload) + "\n```"


async def _open_proposal_turn(
    *,
    app: Any,
    uow_factory: Any,
    migrated_postgres_url: str,
    workspace_service: Any,
    subject_id: UUID,
    run_id: UUID,
    occurrence_id: UUID,
    actor_id: UUID,
    instruction: str,
    raw_response: str,
) -> tuple[Any, UUID, UUID, Any]:
    """Persist the thread/send event plus the exact succeeded model turn."""
    model_conversations = ModelConversationService(uow_factory, object(), app.state.blob_store)
    thread_service = EditorialThreadService(
        uow_factory=uow_factory,
        workspace_service=workspace_service,
        artifact_store=app.state.production_artifact_store,
        model_conversations=model_conversations,
        enqueue_job=lambda _job_id: None,
    )
    await thread_service.send_proposal(
        subject_id,
        occurrence_id,
        text=instruction,
        actor_id=actor_id,
        idempotency_key=f"bridge-visual-send-{uuid4().hex}",
    )
    async with uow_factory() as uow:
        record = await uow.editorial_threads.get_current_by_occurrence(
            subject_id, run_id, occurrence_id
        )
        assert record is not None
        job = await uow.editorial_threads.latest_job_for_thread(record.thread.id)
        assert job is not None
        send_event = next(item for item in record.events if item.id == job.send_event_id)
    assert record.thread.model_conversation_id is not None

    model_run_id, turn_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            session.add(
                ModelRunRow(
                    id=model_run_id,
                    provider="fake",
                    backend="fake",
                    transport="fake",
                    model_role="drafting",
                    requested_model="synthetic-bridge",
                    prompt_template_id="editorial-discussion-proposal",
                    prompt_template_version="6",
                    authorized_input_hash="2" * 64,
                    evidence_pack_hash="3" * 64,
                    parameters={},
                    status="succeeded",
                    submission_state="result_obtained",
                    submission_attempt=1,
                    output_references=["synthetic-model-output"],
                    validation_errors=[],
                    transformations=[],
                    citation_count=0,
                    extracted_url_count=0,
                    visible_citations=[],
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ModelConversationTurnRow(
                    id=turn_id,
                    conversation_id=record.thread.model_conversation_id,
                    sequence=1,
                    parent_turn_id=None,
                    model_run_id=model_run_id,
                    input_blob_reference="synthetic-input",
                    input_sha256="4" * 64,
                    output_blob_reference="synthetic-output",
                    output_sha256=hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
                    status="succeeded",
                    external_turn_id=None,
                    idempotency_key=f"editorial-thread-message:{send_event.id}",
                    correlation_id="synthetic-correlation",
                    error_code=None,
                    error_message=None,
                    error_details=None,
                    objective="editorial_proposal",
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                )
            )
    finally:
        await engine.dispose()

    builder = EditorialDiscussionContextBuilder(
        uow_factory=uow_factory,
        workspace_service=workspace_service,
        artifact_store=app.state.production_artifact_store,
    )
    prepared = await builder.prepare_job(job.id)
    assert prepared.send_event.id == send_event.id
    return prepared, record.thread.id, model_run_id, turn_id


async def _prepare(
    *,
    proposal_service: EditorialProposalService,
    prepared: Any,
    thread_id: UUID,
    model_run_id: UUID,
    turn_id: UUID,
    raw_response: str,
) -> Any:
    assert prepared.send_event.actor_id is not None
    return await proposal_service.prepare_thread_turn(
        thread_id=thread_id,
        actor_id=prepared.send_event.actor_id,
        model_run_id=model_run_id,
        model_turn_id=turn_id,
        instruction=prepared.instruction,
        raw_response=raw_response,
        context=prepared.context,
    )


async def _enrichment_versions(uow_factory: Any, run_id: UUID) -> list[int]:
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
    return [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]


async def _thread_events(uow_factory: Any, thread_id: UUID) -> tuple[Any, list[Any]]:
    async with uow_factory() as uow:
        record = await uow.editorial_threads.get(thread_id)
    assert record is not None
    return record, list(record.events)


@pytest.mark.asyncio
async def test_bridge_element_context_offers_visual_operations_and_current_element(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        _model_gateway,
        workspace_service,
        _proposal_service,
        _adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    prepared, _thread_id, _run_id, _turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige ce graphique.",
        raw_response="@@ REFUSAL @@\ncode: revision_not_applicable\nreason: Test.\n",
    )

    assert prepared.context.proposal_operations == (
        "rewrite_chart",
        "update_visual_caption",
    )
    element_context = prepared.context.proposal_enrichment_context
    assert element_context is not None
    assert element_context["mode"] == "element"
    assert element_context["element_key"] == chart_block["element_key"]
    assert prepared.context.proposal_target_path == "synthesis/charts/0"
    current_element = element_context["current_element"]
    assert current_element["kind"] == "chart"
    assert current_element["unit"] == "endpoints"
    assert [point["value"] for point in current_element["points"]] == ["12", "18"]
    assert "evidence" not in json.dumps(current_element)

    table_block = _enrichment_block(snapshot, kind="table")
    builder = EditorialDiscussionContextBuilder(
        uow_factory=uow_factory,
        workspace_service=workspace_service,
        artifact_store=app.state.production_artifact_store,
    )
    table_context = await builder.build_manual(seeded.subject.id, UUID(table_block["block_id"]))
    assert table_context.proposal_operations == ("update_visual_caption",)


@pytest.mark.asyncio
async def test_bridge_caption_reconcile_prepares_exactly_one_proposal_and_adopts_it(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    target, base_hash, before_hash, _handles, _payload = await _wire_inputs(
        app=app,
        uow_factory=uow_factory,
        seeded=seeded,
        block_id=UUID(chart_block["block_id"]),
    )
    raw_response = _wire(
        target=target,
        action="update_visual_caption",
        base_hash=base_hash,
        before_hash=before_hash,
        after="CAPTION: Légende corrigée par Bridge.",
    )
    prepared, thread_id, model_run_id, turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige la légende du graphique.",
        raw_response=raw_response,
    )
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]

    proposal = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )
    replay = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )

    assert proposal.state is EditorialProposalState.PROPOSED
    assert replay.id == proposal.id
    assert proposal.operation == "update_visual_caption"
    assert proposal.thread_id == thread_id
    async with uow_factory() as uow:
        proposals = await uow.editorial_proposals.list_for_subject(
            seeded.subject.id,
            occurrence_id=UUID(chart_block["block_id"]),
            before=None,
            limit=10,
        )
    assert len([item for item in proposals if item.thread_id == thread_id]) == 1
    _record, events = await _thread_events(uow_factory, thread_id)
    assert len([event for event in events if event.kind is EditorialThreadEventKind.PROPOSAL]) == 1
    assert not [event for event in events if event.kind is EditorialThreadEventKind.REFUSED]
    # Prepare is non-canonical and never calls a model.
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]
    assert model_gateway.calls == []

    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert result.artifact.version == 3
    assert enrichment.charts[0].caption == "Légende corrigée par Bridge."
    assert len(enrichment.charts) == 1
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_bridge_chart_label_correction_adopts_one_chart(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    target, base_hash, before_hash, handles, payload = await _wire_inputs(
        app=app,
        uow_factory=uow_factory,
        seeded=seeded,
        block_id=UUID(chart_block["block_id"]),
    )
    base_chart = editorial_enrichment_from_json(payload).charts[0]
    raw_response = _wire(
        target=target,
        action="rewrite_chart",
        base_hash=base_hash,
        before_hash=before_hash,
        after=_chart_after(
            chart=base_chart,
            seeded=seeded,
            handles=handles,
            title="Titre corrigé par Bridge",
            labels=("Alpha (Bridge)", "Beta (Bridge)"),
        ),
    )
    prepared, thread_id, model_run_id, turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige les libellés du graphique.",
        raw_response=raw_response,
    )

    proposal = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )
    assert proposal.state is EditorialProposalState.PROPOSED
    assert proposal.operation == "rewrite_chart"
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]

    await _adopt(adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}")
    _artifact, revised_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(revised_payload)
    assert len(revised.charts) == 1
    chart = revised.charts[0]
    assert chart.key == base_chart.key
    assert chart.title == "Titre corrigé par Bridge"
    assert [point.label for point in chart.points] == ["Alpha (Bridge)", "Beta (Bridge)"]
    assert chart.placement == base_chart.placement
    assert [point.value for point in chart.points] == [point.value for point in base_chart.points]
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_bridge_chart_unproven_value_is_refused_without_canonical_change(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        _adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    target, base_hash, before_hash, handles, payload = await _wire_inputs(
        app=app,
        uow_factory=uow_factory,
        seeded=seeded,
        block_id=UUID(chart_block["block_id"]),
    )
    base_chart = editorial_enrichment_from_json(payload).charts[0]
    after = _chart_after(
        chart=base_chart,
        seeded=seeded,
        handles=handles,
        title=base_chart.title,
        labels=(base_chart.points[0].label, base_chart.points[1].label),
    ).replace('"value": 18', '"value": 99')
    raw_response = _wire(
        target=target,
        action="rewrite_chart",
        base_hash=base_hash,
        before_hash=before_hash,
        after=after,
    )
    prepared, thread_id, model_run_id, turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige la valeur.",
        raw_response=raw_response,
    )

    proposal = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert proposal.diagnostics["code"] == "bar_chart_value_not_evidenced"
    assert proposal.patch_blob_ref is None
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]
    _record, events = await _thread_events(uow_factory, thread_id)
    refused = [event for event in events if event.kind is EditorialThreadEventKind.REFUSED]
    assert len(refused) == 1
    body = await app.state.production_artifact_store.read_bytes(refused[0].payload_blob_ref)
    assert json.loads(body) == {
        "outcome": "refused",
        "code": "bar_chart_value_not_evidenced",
    }
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_bridge_diagram_label_correction_adopts_one_diagram(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    diagram_block = _enrichment_block(snapshot, kind="diagram")
    target, base_hash, before_hash, handles, payload = await _wire_inputs(
        app=app,
        uow_factory=uow_factory,
        seeded=seeded,
        block_id=UUID(diagram_block["block_id"]),
    )
    base_diagram = editorial_enrichment_from_json(payload).diagrams[0]
    raw_response = _wire(
        target=target,
        action="rewrite_diagram",
        base_hash=base_hash,
        before_hash=before_hash,
        after=_diagram_after(
            diagram=base_diagram,
            seeded=seeded,
            handles=handles,
            node_label="Chargeur",
        ),
    )
    prepared, thread_id, model_run_id, turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(diagram_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige le libellé du nœud.",
        raw_response=raw_response,
    )

    proposal = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )
    assert proposal.state is EditorialProposalState.PROPOSED
    assert proposal.operation == "rewrite_diagram"

    await _adopt(adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}")
    _artifact, revised_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(revised_payload)
    assert len(revised.diagrams) == 1
    diagram = revised.diagrams[0]
    assert diagram.key == base_diagram.key
    assert [node.label for node in diagram.nodes] == ["Chargeur", base_diagram.nodes[1].label]
    assert diagram.placement == base_diagram.placement
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_bridge_stale_visual_proposal_cannot_be_accepted(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        _model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    target, base_hash, before_hash, _handles, _payload = await _wire_inputs(
        app=app,
        uow_factory=uow_factory,
        seeded=seeded,
        block_id=UUID(chart_block["block_id"]),
    )
    raw_response = _wire(
        target=target,
        action="update_visual_caption",
        base_hash=base_hash,
        before_hash=before_hash,
        after="CAPTION: Légende Bridge ensuite obsolète.",
    )
    prepared, thread_id, model_run_id, turn_id = await _open_proposal_turn(
        app=app,
        uow_factory=uow_factory,
        migrated_postgres_url=migrated_postgres_url,
        workspace_service=workspace_service,
        subject_id=seeded.subject.id,
        run_id=seeded.run.id,
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        instruction="Corrige la légende.",
        raw_response=raw_response,
    )
    stale_proposal = await _prepare(
        proposal_service=proposal_service,
        prepared=prepared,
        thread_id=thread_id,
        model_run_id=model_run_id,
        turn_id=turn_id,
        raw_response=raw_response,
    )
    winner = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"manual-caption-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.CHART,
            element_key=chart_block["element_key"],
            caption="Légende gagnante manuelle.",
        ),
    )
    await _adopt(adoption_service, winner, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}")

    with pytest.raises(EditorialProposalAdoptionError) as stale:
        await _adopt(
            adoption_service,
            stale_proposal,
            actor_id=seeded.actor_id,
            key=f"adopt-{uuid4().hex}",
        )
    assert stale.value.code in {
        "proposal_stale",
        "proposal_base_stale",
        "proposal_state_conflict",
    }
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, stale_proposal.occurrence_id, stale_proposal.id
        )
    assert stored is not None
    assert stored.state is EditorialProposalState.STALE
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert enrichment.charts[0].caption == "Légende gagnante manuelle."
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2, 3]
