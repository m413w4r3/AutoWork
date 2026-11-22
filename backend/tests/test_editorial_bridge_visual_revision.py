"""Bridge visual corrections of existing enrichment elements (F2-05).

These tests are red-first contracts for the thread-side wiring: the element
projection never carries bytes or evidence text, the proposal prompt exposes
only the three visual operations, the tolerant marker parser converts Bridge's
one-element wire into the typed patch, and the shared planner is the only
semantic validator. Persistence is stubbed here; the PostgreSQL module covers
the real proposal rows, adoption and reconciliation replay.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_conversation_turns import (
    EDITORIAL_DISCUSSION_PROMPT_VERSION,
    EditorialDiscussionContext,
    EditorialDiscussionObjective,
    _editorial_discussion_data,
    _editorial_discussion_prompt,
)
from cti_app.application.editorial_discussion_context import (
    _current_element_projection,
    _supported_proposal_operations,
)
from cti_app.application.editorial_legacy_read_only import EditorialLegacyReadOnlyReason
from cti_app.application.editorial_proposals import (
    _VISUAL_MODEL_REFUSAL_CODES,
    EditorialProposalApplicationError,
    EditorialProposalService,
    _parse_thread_visual_revision,
)
from cti_app.application.editorial_synthesis.patch_validation import (
    parse_wire_patch,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_proposal import EditorialProposalOrigin, EditorialProposalState
from cti_app.domain.editorial_thread import EditorialThreadBinding
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialSourceStage,
    canonical_content_sha256,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
from tests.editorial_visual_revision_support import (
    ANCHOR,
    bar_chart,
    enrichment_with,
    flow_diagram,
    quantitative_extraction,
    quantitative_synthesis,
    visual_context,
)

_ACTOR_ID = UUID("00000000-0000-4000-8000-0000000000aa")
_MODEL_RUN_ID = UUID("00000000-0000-4000-8000-0000000000bb")
_MODEL_TURN_ID = UUID("00000000-0000-4000-8000-0000000000cc")
_SNAPSHOT_ID = UUID("00000000-0000-4000-8000-0000000000dd")
_RUN_ID = UUID("00000000-0000-4000-8000-0000000000ee")
_OCCURRENCE_ID = UUID("00000000-0000-4000-8000-0000000000ff")


def test_supported_proposal_operations_offers_only_bridge_visual_corrections() -> None:
    all_enrichment_operations = {
        "rewrite_table",
        "delete_table",
        "rewrite_chart",
        "delete_chart",
        "rewrite_diagram",
        "delete_diagram",
        "replace_source_figure",
        "delete_source_figure",
        "update_visual_caption",
        "move_enrichment_element",
    }
    assert _supported_proposal_operations(
        stage="editorial_enrichment",
        block_kind="chart",
        offered_operations=all_enrichment_operations,
        paragraph_validation_ready=False,
        enrichment_validation_ready=True,
    ) == ("rewrite_chart", "update_visual_caption")
    assert _supported_proposal_operations(
        stage="editorial_enrichment",
        block_kind="diagram",
        offered_operations=all_enrichment_operations,
        paragraph_validation_ready=False,
        enrichment_validation_ready=True,
    ) == ("rewrite_diagram", "update_visual_caption")
    assert _supported_proposal_operations(
        stage="editorial_enrichment",
        block_kind="table",
        offered_operations=all_enrichment_operations,
        paragraph_validation_ready=False,
        enrichment_validation_ready=True,
    ) == ("update_visual_caption",)
    assert _supported_proposal_operations(
        stage="editorial_enrichment",
        block_kind="figure",
        offered_operations=all_enrichment_operations,
        paragraph_validation_ready=False,
        enrichment_validation_ready=True,
    ) == ("update_visual_caption",)
    # Manual-only operations are never offered even when the registry lists them.
    for operation in (
        "move_enrichment_element",
        "reorder_enrichment_elements",
        "replace_source_figure",
        "delete_chart",
    ):
        assert operation not in _supported_proposal_operations(
            stage="editorial_enrichment",
            block_kind="chart",
            offered_operations=all_enrichment_operations,
            paragraph_validation_ready=False,
            enrichment_validation_ready=True,
        )
    assert (
        _supported_proposal_operations(
            stage="editorial_enrichment",
            block_kind="chart",
            offered_operations=all_enrichment_operations,
            paragraph_validation_ready=False,
            enrichment_validation_ready=False,
        )
        == ()
    )


def _fixture(kind: EditorialEnrichmentElementKind):
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    chart = bar_chart(refs=refs)
    diagram = flow_diagram(refs=refs)
    enrichment = enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        charts=(chart,) if kind is EditorialEnrichmentElementKind.CHART else (),
        diagrams=(diagram,) if kind is EditorialEnrichmentElementKind.DIAGRAM else (),
    )
    context = visual_context(enrichment=enrichment, extraction=extraction, synthesis=synthesis)
    payload = editorial_enrichment_to_json(enrichment)
    evidence_pack = context.evidence_pack
    assert evidence_pack is not None
    handles = {reference: handle for handle, reference in evidence_pack._handle_to_ref.items()}
    return extraction, refs, synthesis, chart, diagram, enrichment, context, payload, handles


def _entry(
    *,
    kind: EditorialBlockKind,
    pointer: str,
    payload: dict[str, object],
) -> BlockRegistryEntry:
    element = payload["charts"][0] if kind is EditorialBlockKind.CHART else payload["diagrams"][0]
    return BlockRegistryEntry(
        block_id=_OCCURRENCE_ID,
        kind=kind,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(element),
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        structural_pointer=pointer,
    )


def _base_sha() -> str:
    return "a" * 64


def _element_context(
    *,
    kind: EditorialEnrichmentElementKind,
    entry: BlockRegistryEntry,
    payload: dict[str, object],
    context,
    extraction,
    synthesis,
    handles,
    element_key: str,
) -> dict[str, object]:
    return {
        "mode": "element",
        "element_kind": kind.value,
        "element_key": element_key,
        "current_element": _current_element_projection(
            kind,
            payload["charts"][0]
            if kind is EditorialEnrichmentElementKind.CHART
            else payload["diagrams"][0],
        ),
        "target_path": f"synthesis{entry.structural_pointer}",
        "entry": entry,
        "base_content": payload,
        "base_artifact_id": uuid4(),
        "base_artifact_version": 1,
        "base_input_hash": "1" * 64,
        "base_canonical_sha256": _base_sha(),
        "base_pin_hash": "2" * 64,
        "routing_decision_hash": None,
        "production_run_id": _RUN_ID,
        "evidence_pack": context.evidence_pack,
        "extraction": extraction,
        "synthesis": synthesis,
        "figure_catalog": (),
        "visual_context": context,
    }


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, bytes] = {}

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        assert bucket == "editorial-thread-events"
        assert mime_type == "text/plain; charset=utf-8"
        blob_id = uuid4()
        self.payloads[blob_id] = content
        return blob_id


def _thread_binding() -> EditorialThreadBinding:
    return EditorialThreadBinding(
        subject_id=uuid4(),
        production_run_id=_RUN_ID,
        occurrence_id=_OCCURRENCE_ID,
        artifact_id=uuid4(),
        artifact_version=1,
        canonical_sha256="3" * 64,
    )


def _workspace_service(*, read_only_reason: object | None = None) -> SimpleNamespace:
    async def resolve_occurrence(_subject_id: UUID, _occurrence_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(read_only_reason=read_only_reason)

    return SimpleNamespace(resolve_occurrence=resolve_occurrence)


def _service_with_capture(
    monkeypatch: pytest.MonkeyPatch, *, read_only_reason: object | None = None
):
    store = _FakeArtifactStore()
    binding = _thread_binding()
    record = SimpleNamespace(thread=SimpleNamespace(origin=binding))

    class _Uow:
        editorial_threads = SimpleNamespace(get=lambda _thread_id: _async_value(record))

    @asynccontextmanager
    async def uow_factory():
        yield _Uow()

    service = EditorialProposalService(
        uow_factory=uow_factory,
        artifact_store=store,
        workspace_service=_workspace_service(read_only_reason=read_only_reason),
    )
    captured: dict[str, object] = {}

    async def fake_persist(
        preparation,
        *,
        encoded_patch,
        descriptor,
        state,
        kind,
        diagnostics,
        refusal_event=None,
    ):
        captured["preparation"] = preparation
        captured["encoded_patch"] = encoded_patch
        captured["state"] = state
        captured["diagnostics"] = diagnostics
        captured["refusal_event"] = refusal_event
        return SimpleNamespace(id=uuid4(), state=state, diagnostics=diagnostics)

    async def fake_upload(encoded_patch: bytes):
        return SimpleNamespace(sha256="d" * 64)

    async def fake_compile_chart(**values):
        return (
            {
                "asset_id": str(uuid4()),
                "sha256": "e" * 64,
                "compiler_name": "fake",
                "compiler_version": "1",
                "policy_version": "fake-policy",
            },
            values["revised_enrichment"],
        )

    async def fake_compile_diagram(_diagram):
        return {"media_type": "image/svg+xml", "media_sha256": "f" * 64}

    monkeypatch.setattr(service, "_persist_prepared_result", fake_persist)
    monkeypatch.setattr(service, "_upload_and_verify_patch", fake_upload)
    monkeypatch.setattr(service, "_compile_revised_chart", fake_compile_chart)
    monkeypatch.setattr(service, "_compile_diagram_preview", fake_compile_diagram)
    return service, captured, store, binding


def _context(
    *,
    element_context: dict[str, object],
    entry: BlockRegistryEntry,
    operations: tuple[str, ...],
) -> EditorialDiscussionContext:
    return EditorialDiscussionContext(
        section_title="Section synthétique",
        block_text="Élément synthétique",
        adjacent_paragraphs=(),
        admitted_evidence=(),
        history=(),
        evidence_pack_hash="b" * 64,
        context_sha256="c" * 64,
        effective_tlp=TLP.AMBER,
        instruction="Corrige l'élément.",
        proposal_snapshot_id=_SNAPSHOT_ID,
        proposal_target_path=str(element_context["target_path"]),
        proposal_operations=operations,
        proposal_before_hash=entry.content_sha256,
        proposal_base_sha256=_base_sha(),
        proposal_enrichment_context=element_context,
    )


def _wire(
    *,
    target: str,
    action: str,
    before_hash: str,
    after: str,
    base_hash: str = _base_sha(),
    rationale: str = "Correction synthétique.",
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


def _chart_after(
    *,
    chart,
    handles,
    title: str | None = None,
    labels: tuple[str, str] | None = None,
    beta_value: int | None = None,
) -> str:
    alpha_handle = handles[chart.points[0].evidence_refs[0]]
    beta_handle = handles[chart.points[1].evidence_refs[0]]
    point_labels = labels or (chart.points[0].label, chart.points[1].label)
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
                "title": title or chart.title,
                "caption": chart.caption,
                "placement": {
                    "kind": "after_paragraph",
                    "anchor_block_id": str(ANCHOR),
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
                        "label": point_labels[0],
                        "series": chart.points[0].series,
                        "evidence_handles": [alpha_handle],
                        "value": 12,
                    },
                    {
                        "label": point_labels[1],
                        "series": chart.points[1].series,
                        "evidence_handles": [beta_handle],
                        "value": beta_value if beta_value is not None else 18,
                    },
                ],
                "category_axis_label": chart.category_axis_label,
                "value_axis_label": chart.value_axis_label,
                "unit": chart.unit,
            }
        ],
    }
    return "Voici le patch.\n```json\n" + json.dumps(payload) + "\n```\nFin du patch."


def _diagram_after(*, diagram, handles, node_label: str) -> str:
    alpha_handle = handles[diagram.nodes[0].evidence_refs[0]]
    beta_handle = handles[diagram.nodes[1].evidence_refs[0]]
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
                "placement": {"kind": "end"},
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
                        "node_id": "loader",
                        "label": node_label,
                        "role": "unknown",
                        "evidence_handles": [alpha_handle],
                    },
                    {
                        "node_id": "payload",
                        "label": "Beta",
                        "role": "unknown",
                        "evidence_handles": [beta_handle],
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "loader",
                        "target_node_id": "payload",
                        "label": "loads",
                        "relation_type": "factual",
                        "direction": "directed",
                        "evidence_handles": [alpha_handle],
                    }
                ],
                "groups": [],
            }
        ],
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _oversized_diagram_after(*, diagram, handles, node_count: int, label: str) -> str:
    """A structurally valid diagram proposal that exceeds one display bound."""
    body = _diagram_after(diagram=diagram, handles=handles, node_label="Alpha")
    prefix = "```json\n"
    payload = json.loads(body[len(prefix) : -len("\n```")])
    alpha_handle = handles[diagram.nodes[0].evidence_refs[0]]
    payload["diagrams"][0]["nodes"] = [
        {
            "node_id": f"node_{index:03d}",
            "label": label if index == 0 else f"Node {index}",
            "role": "unknown",
            "evidence_handles": [alpha_handle],
        }
        for index in range(node_count)
    ]
    payload["diagrams"][0]["edges"] = [
        {
            "source_node_id": "node_000",
            "target_node_id": "node_001",
            "label": "loads",
            "relation_type": "factual",
            "direction": "directed",
            "evidence_handles": [alpha_handle],
        }
    ]
    return prefix + json.dumps(payload) + "\n```"


async def test_oversized_bridge_diagram_is_refused_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, _chart, diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.DIAGRAM)
    )
    entry = _entry(kind=EditorialBlockKind.DIAGRAM, pointer="/diagrams/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.DIAGRAM,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=diagram.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="rewrite_diagram",
        before_hash=entry.content_sha256,
        after=_oversized_diagram_after(
            diagram=diagram,
            handles=handles,
            node_count=65,
            label="Alpha",
        ),
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige ce diagramme.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_diagram", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["encoded_patch"] is None
    assert captured["refusal_event"].code == "enrichment_revision_patch_invalid"


async def test_bridge_diagram_label_over_the_display_bound_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, _chart, diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.DIAGRAM)
    )
    entry = _entry(kind=EditorialBlockKind.DIAGRAM, pointer="/diagrams/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.DIAGRAM,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=diagram.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="rewrite_diagram",
        before_hash=entry.content_sha256,
        after=_oversized_diagram_after(
            diagram=diagram,
            handles=handles,
            node_count=2,
            label="A" * 501,
        ),
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige ce diagramme.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_diagram", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["encoded_patch"] is None
    assert captured["refusal_event"].code == "enrichment_revision_patch_invalid"


async def test_caption_correction_is_prepared_as_a_typed_visual_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="update_visual_caption",
        before_hash=entry.content_sha256,
        after="CAPTION: Légende corrigée par Bridge.",
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige la légende.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.PROPOSED
    preparation = captured["preparation"]
    assert preparation.origin is EditorialProposalOrigin.BRIDGE_DRAFT
    assert preparation.operation is TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION
    assert preparation.occurrence_id == _OCCURRENCE_ID
    patch = json.loads(bytes(captured["encoded_patch"]))
    assert patch["operation"] == "update_visual_caption"
    assert patch["visual_patch"] == {
        "kind": "update_visual_caption",
        "element_kind": "chart",
        "element_key": chart.key,
        "caption": "Légende corrigée par Bridge.",
    }
    assert captured["state"] is EditorialProposalState.PROPOSED
    # The proposal never stores evidence text or asset bytes.
    encoded = bytes(captured["encoded_patch"]).decode("utf-8")
    assert "Synthetic quantitative fixture" not in encoded
    assert "The synthetic report records Alpha with 12 endpoints." not in encoded


async def test_read_only_context_refuses_before_any_proposal_or_thread_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, store, _binding = _service_with_capture(
        monkeypatch,
        read_only_reason=EditorialLegacyReadOnlyReason.FORMAT_NOT_EDITABLE,
    )
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="update_visual_caption",
        before_hash=entry.content_sha256,
        after="CAPTION: Légende refusée en lecture seule.",
    )

    with pytest.raises(EditorialProposalApplicationError) as caught:
        await service.prepare_thread_turn(
            thread_id=uuid4(),
            actor_id=_ACTOR_ID,
            model_run_id=_MODEL_RUN_ID,
            model_turn_id=_MODEL_TURN_ID,
            instruction="Corrige la légende.",
            raw_response=response,
            context=_context(
                element_context=element_context,
                entry=entry,
                operations=("rewrite_chart", "update_visual_caption"),
            ),
        )

    assert caught.value.code == "editorial_read_only"
    assert caught.value.reason == "format_not_editable"
    # Neither a proposal row nor a thread refusal event was persisted.
    assert captured == {}
    assert store.payloads == {}


async def test_target_witness_operation_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver witness follows the enum order, not the frozenset hash seed."""
    from cti_app.application import editorial_proposals as editorial_proposals_module

    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    real_resolver = editorial_proposals_module.resolve_enrichment_element_target
    seen: dict[str, str] = {}

    def capture(**kwargs):
        seen["operation"] = kwargs["operation"]
        return real_resolver(**kwargs)

    monkeypatch.setattr(editorial_proposals_module, "resolve_enrichment_element_target", capture)
    service, _captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="update_visual_caption",
        before_hash=entry.content_sha256,
        after="CAPTION: Légende déterministe.",
    )

    await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige la légende.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert seen["operation"] == "rewrite_chart"


async def test_chart_label_correction_keeps_one_chart_and_is_evidenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="rewrite_chart",
        before_hash=entry.content_sha256,
        after=_chart_after(
            chart=chart,
            handles=handles,
            title="Titre corrigé",
            labels=("Alpha (corrigé)", "Beta (corrigé)"),
        ),
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige les libellés.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.PROPOSED
    patch = json.loads(bytes(captured["encoded_patch"]))
    visual = patch["visual_patch"]
    assert visual["kind"] == "revise_chart"
    assert visual["element_key"] == chart.key
    assert visual["chart"]["key"] == chart.key
    assert visual["chart"]["title"] == "Titre corrigé"
    assert [point["label"] for point in visual["chart"]["points"]] == [
        "Alpha (corrigé)",
        "Beta (corrigé)",
    ]
    assert visual["chart"]["placement"]["kind"] == "after_paragraph"
    assert visual["chart"]["placement"]["anchor_block_id"] == str(ANCHOR)
    assert Decimal(visual["chart"]["points"][0]["value"]) == Decimal("12")


async def test_chart_value_without_same_record_proof_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="rewrite_chart",
        before_hash=entry.content_sha256,
        after=_chart_after(chart=chart, handles=handles, beta_value=99),
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige les valeurs.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["encoded_patch"] is None
    assert captured["diagnostics"]["code"] == "bar_chart_value_not_evidenced"
    refusal_event = captured["refusal_event"]
    assert refusal_event.code == "bar_chart_value_not_evidenced"
    body = json.loads(store.payloads[refusal_event.blob_id])
    assert body == {"outcome": "refused", "code": "bar_chart_value_not_evidenced"}


async def test_diagram_label_correction_keeps_one_diagram(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction, _refs, synthesis, _chart, diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.DIAGRAM)
    )
    entry = _entry(kind=EditorialBlockKind.DIAGRAM, pointer="/diagrams/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.DIAGRAM,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=diagram.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="rewrite_diagram",
        before_hash=entry.content_sha256,
        after=_diagram_after(diagram=diagram, handles=handles, node_label="Chargeur"),
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige le libellé du nœud.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_diagram", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.PROPOSED
    patch = json.loads(bytes(captured["encoded_patch"]))
    visual = patch["visual_patch"]
    assert visual["kind"] == "revise_diagram"
    assert visual["diagram"]["key"] == diagram.key
    assert [node["label"] for node in visual["diagram"]["nodes"]] == ["Chargeur", "Beta"]
    assert visual["diagram"]["placement"] == {"kind": "end", "section_index": None}


async def test_missing_markers_are_an_explicit_closed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige le graphique.",
        raw_response="Je ne peux pas répondre à cette demande.",
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["diagnostics"]["code"] == "revision_wire_missing_block"


async def test_model_refusal_code_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = (
        "@@ REFUSAL @@\ncode: bar_chart_insufficient_data\n"
        "reason: Preuves insuffisantes.\n@@ END @@"
    )

    parsed = _parse_thread_visual_revision(
        response,
        allowed_operations={"rewrite_chart", "update_visual_caption"},
        element_kind=EditorialEnrichmentElementKind.CHART,
        element_key=chart.key,
        before_hash=entry.content_sha256,
        target_path=f"synthesis{entry.structural_pointer}",
        base_canonical_sha256=_base_sha(),
        evidence_pack=context.evidence_pack,
    )
    assert parsed.patch is None
    assert parsed.refusal_code == "bar_chart_insufficient_data"

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige le graphique.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["diagnostics"]["code"] == "bar_chart_insufficient_data"


async def test_stale_base_hash_is_refused_with_zero_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    service, captured, _store, _binding = _service_with_capture(monkeypatch)
    response = _wire(
        target=f"synthesis{entry.structural_pointer}",
        action="update_visual_caption",
        before_hash=entry.content_sha256,
        after="CAPTION: Légende.",
        base_hash="9" * 64,
    )

    proposal = await service.prepare_thread_turn(
        thread_id=uuid4(),
        actor_id=_ACTOR_ID,
        model_run_id=_MODEL_RUN_ID,
        model_turn_id=_MODEL_TURN_ID,
        instruction="Corrige la légende.",
        raw_response=response,
        context=_context(
            element_context=element_context,
            entry=entry,
            operations=("rewrite_chart", "update_visual_caption"),
        ),
    )

    assert proposal.state is EditorialProposalState.REFUSED
    assert captured["encoded_patch"] is None
    assert captured["diagnostics"]["code"] == "proposal_base_stale"


def test_current_element_projection_is_bounded_and_carries_no_evidence() -> None:
    _extraction, _refs, _synthesis, chart, diagram, _enrichment, _context, payload, _handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    chart_projection = _current_element_projection(
        EditorialEnrichmentElementKind.CHART, payload["charts"][0]
    )
    assert chart_projection["kind"] == "chart"
    assert chart_projection["key"] == chart.key
    assert chart_projection["unit"] == "endpoints"
    assert chart_projection["points"] == [
        {"label": "Alpha", "series": "Endpoints", "value": "12", "unit": "endpoints"},
        {"label": "Beta", "series": "Endpoints", "value": "18", "unit": "endpoints"},
    ]
    serialized = json.dumps(chart_projection)
    assert "evidence" not in serialized
    assert "compiled_asset_id" not in serialized
    assert "blob" not in serialized

    diagram_projection = _current_element_projection(
        EditorialEnrichmentElementKind.DIAGRAM,
        editorial_enrichment_to_json(
            enrichment_with(
                extraction=_extraction,
                synthesis=_synthesis,
                diagrams=(diagram,),
            )
        )["diagrams"][0],
    )
    assert diagram_projection["key"] == diagram.key
    assert diagram_projection["direction"] == "left_to_right"
    assert [node["label"] for node in diagram_projection["nodes"]] == ["Alpha", "Beta"]
    assert diagram_projection["edges"][0]["source_node_id"] == "loader"
    assert diagram_projection["groups"] == []

    figure_projection = _current_element_projection(
        EditorialEnrichmentElementKind.FIGURE,
        {"key": "figure_1", "caption": "Légende intégrée."},
        figure_block={"original_caption": "Légende archivée d'origine."},
    )
    assert figure_projection == {
        "kind": "figure",
        "key": "figure_1",
        "title": None,
        "caption": "Légende intégrée.",
        "original_caption": "Légende archivée d'origine.",
    }


def test_visual_prompt_contract_exposes_current_element_and_variant_rules() -> None:
    extraction, _refs, synthesis, chart, _diagram, _enrichment, context, payload, handles = (
        _fixture(EditorialEnrichmentElementKind.CHART)
    )
    entry = _entry(kind=EditorialBlockKind.CHART, pointer="/charts/0", payload=payload)
    element_context = _element_context(
        kind=EditorialEnrichmentElementKind.CHART,
        entry=entry,
        payload=payload,
        context=context,
        extraction=extraction,
        synthesis=synthesis,
        handles=handles,
        element_key=chart.key,
    )
    discussion_context = _context(
        element_context=element_context,
        entry=entry,
        operations=("rewrite_chart", "update_visual_caption"),
    )

    assert EDITORIAL_DISCUSSION_PROMPT_VERSION == "6"
    data = _editorial_discussion_data(
        "Corrige le graphique.", discussion_context, EditorialDiscussionObjective.PROPOSAL
    )
    contract = data["proposal_contract"]
    assert contract["element"] == {"kind": "chart", "key": chart.key}
    assert contract["current_element"]["points"][0]["value"] == "12"
    assert contract["allowed_operations"] == ["rewrite_chart", "update_visual_caption"]

    prompt = _editorial_discussion_prompt(
        "Corrige le graphique.",
        discussion_context,
        objective=EditorialDiscussionObjective.PROPOSAL,
        nonce="0" * 32,
    )
    assert "rewrite_chart" in prompt
    assert "bar_chart_insufficient_data" in prompt
    assert "update_visual_caption" in prompt
    assert "current_element" in prompt
    assert "même key et le même placement" in prompt
    assert "recopié EXACTEMENT" in prompt


def test_tolerant_parser_strips_fences_and_keeps_visual_refusal_codes() -> None:
    parsed = parse_wire_patch(
        "Préambule libre.\n@@ TARGET @@\nsynthesis/charts/0\n"
        "@@ ACTION @@\nrewrite_chart\n"
        f"@@ BASE_ARTIFACT_HASH @@\n{'a' * 64}\n@@ BEFORE_HASH @@\n{'b' * 64}\n"
        '@@ AFTER @@\n```json\n{"charts": []}\n```\n@@ RATIONALE @@\nRaison.\n',
        allowed_refusal_codes=_VISUAL_MODEL_REFUSAL_CODES,
    )
    assert parsed.action is TargetedEditorialRevisionOperation.REWRITE_CHART
    assert parsed.after.startswith("```json")
    refusal = parse_wire_patch(
        "@@ REFUSAL @@\nbar_chart_insufficient_data\n@@ END @@",
        allowed_refusal_codes=_VISUAL_MODEL_REFUSAL_CODES,
    )
    assert refusal.refusal is None
    assert refusal.refusal_code == "bar_chart_insufficient_data"


def _async_value(value: object):
    async def _return() -> object:
        return value

    return _return()


@pytest.mark.asyncio
async def test_visual_correction_turn_is_prepared_once_after_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse M22 reconciliation: one visual preparation, never a blind retry."""
    from cti_app.domain.editorial_thread import (
        EditorialBridgeStatus,
        EditorialThreadJobState,
        EditorialThreadReconciliationOutcome,
    )
    from cti_app.domain.model_conversations import ConversationTurnStatus
    from tests.test_editorial_thread_message import make_service

    (
        subject_id,
        occurrence_id,
        service,
        repository,
        _store,
        _queued,
        conversations,
    ) = make_service(monkeypatch)
    actor_id = UUID("00000000-0000-0000-0000-000000000099")
    await service.send_proposal(
        subject_id,
        occurrence_id,
        text="Corrige la légende du graphique.",
        actor_id=actor_id,
        idempotency_key="visual-reconcile-send-0001",
    )
    record = repository.records[0]
    send_event = record.events[0]
    job = next(iter(repository.jobs.values()))
    repository.jobs[job.id] = replace(job, state=EditorialThreadJobState.RECONCILIATION_REQUIRED)
    repository.records[0] = replace(
        record,
        thread=replace(record.thread, bridge_status=EditorialBridgeStatus.RECONCILIATION_REQUIRED),
    )
    turn_id, model_run_id = uuid4(), uuid4()
    reply = (
        "@@ TARGET @@\nsynthesis/charts/0\n@@ ACTION @@\nupdate_visual_caption\n"
        f"@@ BASE_ARTIFACT_HASH @@\n{'a' * 64}\n@@ BEFORE_HASH @@\n{'b' * 64}\n"
        "@@ AFTER @@\nCAPTION: Légende corrigée.\n@@ RATIONALE @@\nRaison.\n@@ END @@"
    )
    conversations.gateway_calls = 0

    async def exact_turn_content(*_args, **_kwargs):
        return SimpleNamespace(
            turn=SimpleNamespace(
                id=turn_id,
                model_run_id=model_run_id,
                objective="editorial_proposal",
                status=ConversationTurnStatus.SUCCEEDED,
            ),
            output_text=reply,
        )

    async def reconciled_result(*_args, **_kwargs):
        return SimpleNamespace(
            outcome=EditorialThreadReconciliationOutcome.RECONCILED_REPLY,
            turn_id=turn_id,
            reply=reply,
        )

    monkeypatch.setattr(conversations, "turn_content_by_idempotency_key", exact_turn_content)
    monkeypatch.setattr(conversations, "_reconcile_editorial_thread_turn", reconciled_result)
    context = EditorialDiscussionContext(
        section_title="Section synthétique",
        block_text="Graphique synthétique.",
        adjacent_paragraphs=(),
        admitted_evidence=(),
        history=(),
        evidence_pack_hash="b" * 64,
        context_sha256="c" * 64,
        effective_tlp=TLP.CLEAR,
        proposal_operations=("update_visual_caption",),
        proposal_enrichment_context={"mode": "element", "element_kind": "chart"},
    )
    prepared = SimpleNamespace(
        record=repository.records[0],
        send_event=send_event,
        instruction="Corrige la légende du graphique.",
        context=context,
        objective=EditorialDiscussionObjective.PROPOSAL,
    )

    class ContextBuilder:
        async def prepare_job(self, requested_job_id):
            assert requested_job_id == job.id
            return prepared

    calls: list[tuple[UUID, UUID]] = []

    class ProposalService:
        async def prepare_thread_turn(self, **values):
            assert values["raw_response"] == reply
            calls.append((values["model_run_id"], values["model_turn_id"]))
            return SimpleNamespace(state="proposed")

    service._proposal_service = ProposalService()
    service._discussion_context_builder = ContextBuilder()

    first = await service.reconcile(
        subject_id,
        occurrence_id,
        actor_id=actor_id,
        idempotency_key="visual-reconcile-key-0001",
    )
    replay = await service.reconcile(
        subject_id,
        occurrence_id,
        actor_id=actor_id,
        idempotency_key="visual-reconcile-key-0001",
    )

    assert first.outcome is EditorialThreadReconciliationOutcome.RECONCILED_REPLY
    assert replay == first
    assert calls == [(model_run_id, turn_id)]
    assert conversations.gateway_calls == 0
    assert repository.jobs[job.id].state is EditorialThreadJobState.COMPLETE
