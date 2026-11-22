"""Route oracles for the generic typed enrichment revision API.

The route, the real `EditorialProposalService` factory and the real shared
planner run here; only the pinned-artifact reader (already exercised end to end
against PostgreSQL in `tests/integration/editorial`) is replaced by a typed
in-memory preparation. Schema, headers and the closed error table are checked
against the mounted FastAPI router so a fake resolver cannot hide the real
request/response types.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_figure_catalog import opaque_figure_id
from cti_app.application.editorial_proposals import (
    EditorialProposalApplicationError,
    EditorialProposalService,
)
from cti_app.application.editorial_visual_revision import (
    VisualRevisionContext,
    VisualRevisionContextLoader,
    VisualRevisionPreparation,
)
from cti_app.application.editorial_workspace.registry import resolve_occurrence
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import EditorialFigureCatalogEntry
from cti_app.application.source_figure_inventory import SourceFigureCatalogMetadata
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalConflict,
    EditorialProposalState,
)
from cti_app.domain.editorial_workspace import (
    EditorialBlockKind,
    EditorialSourceStage,
    bootstrap_block_registry,
)
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramProfile,
    DiagramRelationDirection,
    DiagramRelationType,
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    editorial_enrichment_to_json,
    source_figure_id,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from tests.editorial_visual_revision_support import (
    anchored,
    bar_chart,
    enrichment_with,
    evidence_pack,
    flow_diagram,
    quantitative_extraction,
    quantitative_synthesis,
)
from tests.test_editorial_visual_revision import ANCHOR_A, ANCHOR_B, _context

_ENRICHMENT_INPUT_HASH = "2" * 64
_WORKSPACE_PIN_HASH = "5" * 64
_ROUTING_DECISION_HASH = "3" * 64


# ---------------------------------------------------------------------------
# Minimal canonical storage: every dependency the real service touches.
# ---------------------------------------------------------------------------


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.bytes: dict[str, bytes] = {}

    async def put(self, source: Any, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.bytes[digest] = content
        return BlobDescriptor(
            sha256=digest,
            size=len(content),
            mime_type=mime_type,
            logical_bucket=logical_bucket,
        )

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        content = self.bytes[descriptor.sha256]
        assert len(content) <= max_bytes
        return content

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.bytes.pop(descriptor.sha256, None)


class _MemoryBlobs:
    def __init__(self) -> None:
        self.by_id: dict[UUID, BlobRecord] = {}
        self.by_address: dict[tuple[str, str], BlobRecord] = {}

    async def lock_address(self, _logical_bucket: str, _digest: str) -> None:
        return None

    async def add_if_absent(self, record: BlobRecord) -> BlobRecord:
        address = (record.descriptor.logical_bucket, record.descriptor.sha256)
        stored = self.by_address.setdefault(address, record)
        self.by_id[stored.id] = stored
        return stored

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.by_id.get(blob_id)

    async def count_references(self, _blob_id: UUID) -> int:
        return 0

    async def delete(self, _blob_id: UUID) -> None:
        return None


class _MemoryProposals:
    def __init__(self) -> None:
        self.records: list[EditorialProposal] = []
        self.events: list[object] = []

    async def lock_adoption_for_run(self, _run_id: UUID) -> None:
        return None

    async def get_by_idempotency_key(
        self, subject_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> EditorialProposal | None:
        return next(
            (
                record
                for record in self.records
                if record.subject_id == subject_id
                and record.actor_id == actor_id
                and record.idempotency_key == idempotency_key
            ),
            None,
        )

    async def add_if_absent(
        self,
        record: EditorialProposal,
        *,
        event: object,
        model_run: object | None,
    ) -> tuple[EditorialProposal, bool]:
        existing = await self.get_by_idempotency_key(
            record.subject_id, record.actor_id, record.idempotency_key
        )
        if existing is not None:
            return existing, False
        self.records.append(record)
        self.events.append(event)
        return record, True

    async def get_for_occurrence(
        self, subject_id: UUID, occurrence_id: UUID, proposal_id: UUID
    ) -> EditorialProposal | None:
        return next(
            (
                record
                for record in self.records
                if record.id == proposal_id
                and record.subject_id == subject_id
                and record.occurrence_id == occurrence_id
            ),
            None,
        )

    async def transition_if_current(self, **_kwargs: object) -> tuple[None, bool]:
        return None, False


class _MemorySnapshots:
    def __init__(self, snapshot: SimpleNamespace) -> None:
        self.snapshot = snapshot

    async def get_by_id(self, snapshot_id: UUID) -> SimpleNamespace | None:
        return self.snapshot if snapshot_id == self.snapshot.snapshot_id else None

    async def latest_for_run(self, run_id: UUID) -> SimpleNamespace | None:
        return self.snapshot if run_id == self.snapshot.production_run_id else None


class _MemoryArtifacts:
    def __init__(self, artifact: ProductionArtifact) -> None:
        self.artifact = artifact

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifact if artifact_id == self.artifact.id else None


class _MemoryProductionRuns:
    async def lock_creation_for_subject(self, _subject_id: UUID) -> None:
        return None


class _MemoryUnitOfWork:
    def __init__(
        self,
        *,
        repository: _MemoryProposals,
        blobs: _MemoryBlobs,
        snapshots: _MemorySnapshots,
        artifacts: _MemoryArtifacts,
    ) -> None:
        self.editorial_proposals = repository
        self.blobs = blobs
        self.editorial_workspace_snapshots = snapshots
        self.production_artifacts = artifacts
        self.production_runs = _MemoryProductionRuns()

    async def __aenter__(self) -> _MemoryUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _MemoryArtifactStore:
    """Read-only projection of the pinned canonical payloads."""

    def __init__(self, payloads: dict[UUID, dict[str, object]]) -> None:
        self._payloads = payloads

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self._payloads[blob_id]

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = json.dumps(self._payloads[blob_id]).encode()
        assert len(content) <= max_bytes
        return content


class _NoModelGateway:
    def __getattr__(self, name: str) -> Any:
        def unexpected_call(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError(f"Visual revision must not call the model gateway: {name}")

        return unexpected_call


class _IdentityProvider:
    def __init__(self, actor_id: UUID) -> None:
        self._actor_id = actor_id

    async def current(self) -> SimpleNamespace:
        return SimpleNamespace(actor_id=str(self._actor_id))


class _RefusingLoader:
    """Typed refusal used when the occurrence is outside the pinned registry."""

    async def load(self, **_kwargs: object) -> None:
        from cti_app.application.editorial_visual_revision import VisualRevisionRejected

        raise VisualRevisionRejected(
            "proposal_base_stale", {"reason": "workspace:occurrence_resolution"}
        )


class _FakeDiagramPreviewCompiler:
    """Deterministic non-canonical preview; the real D2 compiler is PG-tested."""

    async def compile_preview(self, diagram: DiagramSpecV1) -> SimpleNamespace:
        return SimpleNamespace(
            diagram_key=diagram.key,
            media_type="image/png",
            media_sha256="d" * 64,
            media_bytes=b"synthetic-diagram-preview",
            source_sha256="e" * 64,
            compiler="synthetic-d2",
            compiler_version="1",
            compiler_policy_version="synthetic",
        )


class _MemoryMediaAssetStore:
    """Chart preview manifests without the canonical media-asset repository."""

    def __init__(self) -> None:
        self.manifests: dict[UUID, SimpleNamespace] = {}

    async def store_chart(self, compiled: Any, *, source: str) -> SimpleNamespace:
        manifest = SimpleNamespace(
            asset_id=uuid4(),
            sha256=hashlib.sha256(compiled.media_bytes).hexdigest(),
            compiler_name=compiled.compiler,
            compiler_version=compiled.compiler_version,
            policy_version=compiled.compiler_policy_version,
        )
        self.manifests[manifest.asset_id] = manifest
        return manifest

    async def get(self, asset_id: UUID) -> SimpleNamespace | None:
        return self.manifests.get(asset_id)


class _FakeVisualLoader:
    def __init__(self, preparation: VisualRevisionPreparation) -> None:
        self.preparation = preparation
        self.read_only_reason: object | None = None
        self.diagram_preview_compiler = _FakeDiagramPreviewCompiler()

    async def load(
        self, *, subject_id: UUID, snapshot_id: UUID, occurrence_id: UUID
    ) -> VisualRevisionPreparation:
        prepared = self.preparation
        assert subject_id == prepared.subject_id
        assert snapshot_id == prepared.snapshot_id
        assert occurrence_id == prepared.entry.block_id
        return prepared


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _enable_route(monkeypatch: pytest.MonkeyPatch, *, accept_enabled: bool = True) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_proposal_accept_enabled=accept_enabled,
            production_editorial_resource_search_enabled=False,
        ),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)


def _visual_stack(
    monkeypatch: pytest.MonkeyPatch,
    *,
    actor_id: UUID | None = None,
    read_only: bool = False,
    context: VisualRevisionContext | None = None,
) -> SimpleNamespace:
    actor = actor_id or uuid4()
    context = context if context is not None else _context()
    enrichment_payload = editorial_enrichment_to_json(context.base_enrichment)
    synthesis_payload = production_synthesis_to_json(context.synthesis)
    enrichment_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(enrichment_payload)
    ).hexdigest()
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    subject_id = context.extraction.subject_id
    run_id = uuid4()
    snapshot_id = uuid4()
    enrichment_artifact_id = uuid4()
    synthesis_artifact_id = uuid4()
    enrichment_blob_id = uuid4()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=synthesis_payload,
    )
    enrichment_entries = bootstrap_block_registry(
        artifact_id=enrichment_artifact_id,
        canonical_sha256=enrichment_sha256,
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        canonical_content=enrichment_payload,
    )
    table_entry = next(
        (entry for entry in enrichment_entries if entry.kind is EditorialBlockKind.TABLE),
        None,
    )
    # Reorder targets the paragraph anchor occurrence itself; pin the first
    # synthesis paragraph to the anchor identity the enrichment references.
    paragraph_entry = replace(
        next(
            entry
            for entry in synthesis_entries
            if entry.kind in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
        ),
        block_id=ANCHOR_A,
    )
    synthesis_entries = tuple(
        paragraph_entry
        if entry.kind in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
        else entry
        for entry in synthesis_entries
    )
    snapshot = SimpleNamespace(
        snapshot_id=snapshot_id,
        subject_id=subject_id,
        production_run_id=run_id,
        workspace_pin_hash=_WORKSPACE_PIN_HASH,
        routing_decision_hash=_ROUTING_DECISION_HASH,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=1,
        synthesis_input_hash="1" * 64,
        synthesis_canonical_sha256=synthesis_sha256,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=2,
        enrichment_input_hash=_ENRICHMENT_INPUT_HASH,
        enrichment_canonical_sha256=enrichment_sha256,
        block_registry=(*synthesis_entries, *enrichment_entries),
    )
    base_artifact = ProductionArtifact(
        id=enrichment_artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash=_ENRICHMENT_INPUT_HASH,
        canonical_blob_id=enrichment_blob_id,
    )

    def preparation_for(entry: Any) -> VisualRevisionPreparation:
        return VisualRevisionPreparation(
            context=context,
            base_artifact=base_artifact,
            entry=entry,
            snapshot_id=snapshot_id,
            subject_id=subject_id,
            production_run_id=run_id,
            base_canonical_sha256=enrichment_sha256,
            base_pin_hash=_WORKSPACE_PIN_HASH,
            routing_decision_hash=_ROUTING_DECISION_HASH,
        )

    loaders = {
        entry.block_id: _FakeVisualLoader(preparation_for(entry)) for entry in enrichment_entries
    }
    loaders[paragraph_entry.block_id] = _FakeVisualLoader(preparation_for(paragraph_entry))

    async def resolve(subject: UUID, occurrence_id: UUID) -> Any:
        if subject != subject_id:
            raise editorial_workspace_api.EditorialWorkspaceError("editorial_block_missing")
        resolution = resolve_occurrence(snapshot, occurrence_id)
        if read_only and resolution.entry is not None:
            from cti_app.application.editorial_legacy_read_only import (
                EditorialLegacyReadOnlyReason,
            )

            return replace(
                resolution, read_only_reason=EditorialLegacyReadOnlyReason.FORMAT_NOT_EDITABLE
            )
        return resolution

    workspace_service = SimpleNamespace(resolve_occurrence=resolve)
    repository = _MemoryProposals()
    blobs = _MemoryBlobs()
    uow = _MemoryUnitOfWork(
        repository=repository,
        blobs=blobs,
        snapshots=_MemorySnapshots(snapshot),
        artifacts=_MemoryArtifacts(base_artifact),
    )
    blob_store = _MemoryBlobStore()
    artifact_store = _MemoryArtifactStore({enrichment_blob_id: enrichment_payload})

    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = lambda: uow
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.model_gateway = _NoModelGateway()
    app.state.publication_render_service = object()
    app.state.identity_provider = _IdentityProvider(actor)

    monkeypatch.setattr(editorial_workspace_api, "_service", lambda _request: workspace_service)
    real_factory = editorial_workspace_api._manual_proposal_service
    occurrence_holder: dict[str, UUID] = {}

    def factory_with_occurrence(request: Request) -> EditorialProposalService:
        service = real_factory(request)
        # The real factory constructs the production loader from app state; the
        # pinned-artifact read itself is replaced by a typed in-memory
        # preparation keyed by the occurrence under revision. Chart compilation
        # keeps the real deterministic renderer; only its media manifest store
        # and the external D2 preview compiler are in-memory here.
        assert isinstance(service._visual_revision_loader, VisualRevisionContextLoader)
        occurrence_id = occurrence_holder["value"]
        service._visual_revision_loader = loaders.get(occurrence_id) or _RefusingLoader()
        service._diagram_preview_compiler = _FakeDiagramPreviewCompiler()
        service._media_asset_store = _MemoryMediaAssetStore()
        return service

    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", factory_with_occurrence
    )

    def block_id_for(kind: EditorialBlockKind) -> UUID:
        return next(entry.block_id for entry in enrichment_entries if entry.kind is kind)

    return SimpleNamespace(
        app=app,
        subject_id=subject_id,
        snapshot_id=snapshot_id,
        table_block_id=table_entry.block_id if table_entry is not None else None,
        paragraph_block_id=paragraph_entry.block_id,
        table_key=(
            context.base_enrichment.tables[0].key if context.base_enrichment.tables else None
        ),
        actor_id=actor,
        repository=repository,
        blob_store=blob_store,
        occurrence_holder=occurrence_holder,
        context=context,
        block_id_for=block_id_for,
    )


def _post(
    client: httpx.AsyncClient,
    subject_id: UUID,
    occurrence_id: UUID,
    body: dict[str, object],
    *,
    key: str = "visual-revision-key-0001",
) -> Any:
    return client.post(
        f"/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions",
        headers={"Idempotency-Key": key},
        json=body,
    )


def _caption_body(snapshot_id: UUID, table_key: str, caption: str) -> dict[str, object]:
    return {
        "snapshot_id": str(snapshot_id),
        "operation": "update_visual_caption",
        "element_kind": "table",
        "element_key": table_key,
        "caption": caption,
    }


def _evidence_ref_body(reference: ExtractionEvidenceRefV1) -> dict[str, str]:
    return {
        "source_document_id": str(reference.source_document_id),
        "kind": reference.kind.value,
        "evidence_key": reference.evidence_key,
    }


def _purpose_body(evidence_refs: tuple[ExtractionEvidenceRefV1, ...]) -> dict[str, object]:
    return {
        "question": "Which synthetic endpoint count is documented?",
        "available_data": "The synthetic extraction records the exact documented counts.",
        "comprehension_gain": "The comparison exposes the documented difference.",
        "scope": "Only the synthetic records cited here.",
        "evidence_refs": [_evidence_ref_body(reference) for reference in evidence_refs],
        "knowledge_limits": "No unreported value is inferred.",
        "placement_reason": "Keep the visual after its anchor paragraph.",
    }


def _context_with_chart() -> tuple[VisualRevisionContext, ChartSpecV1]:
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    chart = bar_chart(refs=refs)
    context = VisualRevisionContext(
        base_enrichment=enrichment_with(
            extraction=extraction, synthesis=synthesis, charts=(chart,)
        ),
        evidence_pack=evidence_pack(tuple(extraction_evidence_refs_v1(extraction))),
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({ANCHOR_A, ANCHOR_B}),
        production_run_id=uuid4(),
    )
    return context, chart


def _chart_body(
    snapshot_id: UUID,
    chart: ChartSpecV1,
    *,
    labels: tuple[str, str],
    second_value: str,
) -> dict[str, object]:
    first, second = chart.points
    return {
        "snapshot_id": str(snapshot_id),
        "operation": "revise_chart",
        "element_key": chart.key,
        "chart": {
            "key": chart.key,
            "kind": chart.kind.value,
            "title": "Revised documented endpoint counts",
            "caption": chart.caption,
            "category_axis_label": chart.category_axis_label,
            "value_axis_label": chart.value_axis_label,
            "unit": chart.unit,
            "points": [
                {
                    "label": labels[0],
                    "series": first.series,
                    "value": format(first.value, "f") if first.value is not None else None,
                    "evidence_refs": [
                        _evidence_ref_body(reference) for reference in first.evidence_refs
                    ],
                },
                {
                    "label": labels[1],
                    "series": second.series,
                    "value": second_value,
                    "evidence_refs": [
                        _evidence_ref_body(reference) for reference in second.evidence_refs
                    ],
                },
            ],
            "purpose": _purpose_body(
                tuple(reference for point in chart.points for reference in point.evidence_refs)
            ),
        },
    }


def _context_with_diagram(
    diagram: DiagramSpecV1 | None = None,
) -> tuple[VisualRevisionContext, DiagramSpecV1]:
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    diagram = flow_diagram(refs=refs) if diagram is None else diagram
    context = VisualRevisionContext(
        base_enrichment=enrichment_with(
            extraction=extraction, synthesis=synthesis, diagrams=(diagram,)
        ),
        evidence_pack=evidence_pack(tuple(extraction_evidence_refs_v1(extraction))),
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({ANCHOR_A, ANCHOR_B}),
        production_run_id=uuid4(),
    )
    return context, diagram


def _diagram_body(
    snapshot_id: UUID,
    diagram: DiagramSpecV1,
    *,
    title: str,
    edges: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    assert diagram.purpose is not None
    purpose_refs = diagram.purpose.evidence_refs
    return {
        "snapshot_id": str(snapshot_id),
        "operation": "revise_diagram",
        "element_key": diagram.key,
        "diagram": {
            "key": diagram.key,
            "kind": diagram.kind.value,
            "title": title,
            "caption": diagram.caption,
            "direction": diagram.direction.value,
            "profile": diagram.profile.value if diagram.profile is not None else None,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "label": node.label,
                    "role": node.role.value,
                    "evidence_refs": [
                        _evidence_ref_body(reference) for reference in node.evidence_refs
                    ],
                }
                for node in diagram.nodes
            ],
            "edges": (
                edges
                if edges is not None
                else [
                    {
                        "source_node_id": edge.source_node_id,
                        "target_node_id": edge.target_node_id,
                        "label": edge.label,
                        "direction": edge.direction.value if edge.direction is not None else None,
                        "evidence_refs": [
                            _evidence_ref_body(reference) for reference in edge.evidence_refs
                        ],
                    }
                    for edge in diagram.edges
                ]
            ),
            "groups": [],
            "purpose": _purpose_body(purpose_refs),
        },
    }


def _context_with_replaceable_figure() -> tuple[VisualRevisionContext, UUID]:
    """A base figure without a resolved archive plus one selectable catalogue entry."""
    extraction, _refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    source = extraction.sources[0]
    run_id = uuid4()
    base_figure = SourceFigureCandidateV1(
        key="figure_one",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption="Synthetic source figure caption",
        provenance="Figure 1 from the synthetic report",
        locator=SourceFigureLocatorV1(page=1),
        inclusion_status=SourceFigureInclusionStatus.PROPOSED,
        placement=anchored(),
    )
    sha256 = "f" * 64
    locator = SourceFigureLocatorV1(page=2)
    resolved = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source.source_document_id,
            sha256=sha256,
            source=source.canonical_url,
            locator=locator,
        ),
        blob_id=uuid4(),
        sha256=sha256,
        mime_type="image/png",
        byte_size=128,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance="Figure 2 from the synthetic report",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="Synthetic accepted archive figure",
    )
    context = VisualRevisionContext(
        base_enrichment=replace(
            enrichment_with(extraction=extraction, synthesis=synthesis),
            source_figures=(base_figure,),
        ),
        evidence_pack=evidence_pack(tuple(extraction_evidence_refs_v1(extraction))),
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({ANCHOR_A, ANCHOR_B}),
        figure_catalog=(
            EditorialFigureCatalogEntry(
                handle="F002",
                source_role="body",
                editorial_role="illustration",
                figure=resolved,
                metadata=SourceFigureCatalogMetadata(width=640, height=480),
            ),
        ),
        production_run_id=run_id,
    )
    public_figure_id = UUID(
        opaque_figure_id(context.base_enrichment.subject_id, run_id, resolved.figure_id)
    )
    return context, public_figure_id


# ---------------------------------------------------------------------------
# Schema and header contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    (
        {"operation": "update_visual_caption", "element_kind": "table", "element_key": "t"},
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "operation": "explode_visual",
            "element_kind": "table",
            "element_key": "t",
        },
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "operation": "update_visual_caption",
            "element_kind": "table",
            "element_key": "t",
            "caption": None,
            "source_url": "https://example.invalid/forbidden",
        },
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "operation": "update_visual_caption",
            "element_kind": "table",
            "caption": "missing key",
        },
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "operation": "update_visual_caption",
            "element_kind": "video",
            "element_key": "t",
            "caption": None,
        },
    ),
)
async def test_enrichment_revision_schema_rejects_extra_missing_or_unknown_fields(
    body: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, uuid4(), uuid4(), body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "enrichment_revision_patch_invalid"
    assert response.json()["detail"]["message"] == (
        "La révision visuelle est invalide. Vérifiez les champs, puis réessayez."
    )


@pytest.mark.asyncio
async def test_enrichment_revision_requires_a_valid_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    body = _caption_body(uuid4(), "table_one", "Synthetic caption")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        missing = await client.post(
            f"/api/subjects/{uuid4()}/editor/blocks/{uuid4()}/enrichment-revisions",
            json=body,
        )
        invalid = await _post(client, uuid4(), uuid4(), body, key="short")

    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "idempotency_key_required"
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "idempotency_key_invalid"


# ---------------------------------------------------------------------------
# Typed variants: every planner branch reachable through the real route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revise_chart_route_prepares_a_label_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, chart = _context_with_chart()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.CHART)
    stack.occurrence_holder["value"] = occurrence_id
    body = _chart_body(
        stack.snapshot_id,
        chart,
        labels=("Alpha (revised)", "Beta (revised)"),
        second_value="18",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "rewrite_chart"
    assert payload["origin"] == "manual_patch"
    assert payload["state"] == "proposed"
    visual_patch = payload["patch"]["visual_patch"]
    assert visual_patch["kind"] == "revise_chart"
    assert visual_patch["element_key"] == chart.key
    assert visual_patch["chart"]["key"] == chart.key
    assert [point["label"] for point in visual_patch["chart"]["points"]] == [
        "Alpha (revised)",
        "Beta (revised)",
    ]
    # The request carries no placement: the server-bound value is the base one.
    assert visual_patch["chart"]["placement"]["kind"] == "after_paragraph"
    preview = payload["diagnostics"]["preview"]
    assert preview["operation"] == "rewrite_chart"
    assert preview["point_count"] == 2
    assert "chart_asset" in preview
    assert len(stack.repository.records) == 1
    assert stack.repository.records[0].model_run_id is None
    assert stack.repository.records[0].state is EditorialProposalState.PROPOSED


@pytest.mark.asyncio
async def test_revise_chart_route_refuses_an_unevidenced_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, chart = _context_with_chart()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.CHART)
    stack.occurrence_holder["value"] = occurrence_id
    # The Beta FACT documents exactly "18 endpoints"; 99 is not re-proved.
    body = _chart_body(
        stack.snapshot_id,
        chart,
        labels=("Alpha", "Beta"),
        second_value="99",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "bar_chart_value_not_evidenced"
    assert response.json()["detail"]["message"] == (
        "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves "
        "actuelles. Un tableau descriptif peut être proposé à la place."
    )
    assert stack.repository.records == []


@pytest.mark.asyncio
async def test_revise_diagram_route_prepares_a_title_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, diagram = _context_with_diagram()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.DIAGRAM)
    stack.occurrence_holder["value"] = occurrence_id
    body = _diagram_body(stack.snapshot_id, diagram, title="Revised documented infection flow")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "rewrite_diagram"
    visual_patch = payload["patch"]["visual_patch"]
    assert visual_patch["kind"] == "revise_diagram"
    assert visual_patch["diagram"]["title"] == "Revised documented infection flow"
    assert visual_patch["diagram"]["placement"]["kind"] == "end"
    preview = payload["diagnostics"]["preview"]
    assert preview["operation"] == "rewrite_diagram"
    assert preview["diagram"]["media_type"] == "image/png"
    assert stack.repository.records[0].model_run_id is None


@pytest.mark.asyncio
async def test_revise_diagram_route_maps_a_structurally_invalid_body_to_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, diagram = _context_with_diagram()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.DIAGRAM)
    stack.occurrence_holder["value"] = occurrence_id
    # A non-protocol diagram requires at least one edge; the strict codec
    # refuses the body and the route reports a typed, stable 422.
    body = _diagram_body(stack.snapshot_id, diagram, title=diagram.title, edges=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "enrichment_revision_patch_invalid"
    assert response.json()["detail"]["message"] == (
        "La révision visuelle est invalide. Vérifiez les champs, puis réessayez."
    )
    assert stack.repository.records == []


@pytest.mark.asyncio
async def test_revise_diagram_route_preserves_the_protocol_relation_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protocol edge keeps its semantics instead of being forced factual."""
    _enable_route(monkeypatch)
    _extraction_value, refs = quantitative_extraction()
    protocol_diagram = replace(
        flow_diagram(refs=refs),
        profile=DiagramProfile.PROTOCOL_SEQUENCE,
        edges=(
            DiagramEdgeV1(
                "loader",
                "payload",
                "loads",
                (),
                relation_type=DiagramRelationType.PROTOCOL_FACTUAL,
                direction=DiagramRelationDirection.DIRECTED,
            ),
        ),
    )
    context, diagram = _context_with_diagram(protocol_diagram)
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.DIAGRAM)
    stack.occurrence_holder["value"] = occurrence_id
    body = _diagram_body(
        stack.snapshot_id,
        diagram,
        title="Revised protocol sequence",
        edges=[
            {
                "source_node_id": "loader",
                "target_node_id": "payload",
                "label": "loads",
                "relation_type": "protocol_factual",
                "evidence_refs": [],
            }
        ],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 200, response.text
    visual_patch = response.json()["patch"]["visual_patch"]
    assert visual_patch["diagram"]["edges"] == [
        {
            "source_node_id": "loader",
            "target_node_id": "payload",
            "label": "loads",
            "relation_type": "protocol_factual",
            "direction": "directed",
            "evidence_refs": [],
        }
    ]


@pytest.mark.asyncio
async def test_replace_source_figure_route_prepares_an_opaque_catalog_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, public_figure_id = _context_with_replaceable_figure()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.FIGURE)
    stack.occurrence_holder["value"] = occurrence_id
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "replace_source_figure",
        "element_key": "figure_one",
        "figure_id": str(public_figure_id),
        "caption": "Synthetic replacement caption",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "replace_source_figure"
    visual_patch = payload["patch"]["visual_patch"]
    assert visual_patch == {
        "kind": "replace_source_figure",
        "element_key": "figure_one",
        "figure_id": str(public_figure_id),
        "caption": "Synthetic replacement caption",
    }
    preview = payload["diagnostics"]["preview"]
    assert preview["element_kind"] == "figure"
    assert preview["figure_handle"] == "F002"
    assert preview["previous_figure_id"] is None
    assert stack.repository.records[0].model_run_id is None


@pytest.mark.asyncio
async def test_replace_source_figure_route_refuses_an_unknown_catalog_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context, _public_figure_id = _context_with_replaceable_figure()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.FIGURE)
    stack.occurrence_holder["value"] = occurrence_id
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "replace_source_figure",
        "element_key": "figure_one",
        "figure_id": str(uuid4()),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "editorial_figure_not_found"
    assert response.json()["detail"]["message"] == (
        "Cette figure n'est plus disponible dans l'inventaire."
    )
    assert stack.repository.records == []


@pytest.mark.asyncio
async def test_remove_enrichment_element_route_prepares_a_typed_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context = _context()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.TABLE)
    stack.occurrence_holder["value"] = occurrence_id
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "remove_enrichment_element",
        "element_kind": "table",
        "element_key": stack.table_key,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "delete_table"
    assert payload["patch"]["visual_patch"] == {
        "kind": "remove_enrichment_element",
        "element_kind": "table",
        "element_key": stack.table_key,
    }
    preview = payload["diagnostics"]["preview"]
    assert preview["operation"] == "delete_table"
    assert preview["element_kind"] == "table"
    assert stack.repository.records[0].model_run_id is None


@pytest.mark.asyncio
async def test_remove_enrichment_element_route_refuses_a_missing_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    context = _context()
    stack = _visual_stack(monkeypatch, context=context)
    occurrence_id = stack.block_id_for(EditorialBlockKind.TABLE)
    stack.occurrence_holder["value"] = occurrence_id
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "remove_enrichment_element",
        "element_kind": "table",
        "element_key": "table_missing",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, occurrence_id, body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "enrichment_element_not_found"
    assert stack.repository.records == []


@pytest.mark.asyncio
async def test_element_key_from_another_occurrence_is_refused_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The occurrence's element key is authoritative, not the caller's."""
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch)
    stack.occurrence_holder["value"] = stack.table_block_id
    other_key = stack.context.base_enrichment.tables[1].key
    assert other_key != stack.table_key
    body = _caption_body(stack.snapshot_id, other_key, "Nouvelle légende synthétique")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, stack.table_block_id, body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "code": "enrichment_target_mismatch",
        "message": "L'élément ciblé ne correspond pas à l'élément de cette occurrence.",
    }
    assert stack.repository.records == []


# ---------------------------------------------------------------------------
# Closed error table (direct handler call with the real message map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_status", "expected_code"),
    (
        ("enrichment_revision_patch_invalid", 422, "enrichment_revision_patch_invalid"),
        ("enrichment_element_not_found", 422, "enrichment_element_not_found"),
        ("enrichment_element_kind_mismatch", 422, "enrichment_element_kind_mismatch"),
        ("enrichment_target_mismatch", 422, "enrichment_target_mismatch"),
        ("enrichment_caption_invalid", 422, "enrichment_caption_invalid"),
        ("enrichment_move_anchor_invalid", 422, "enrichment_move_anchor_invalid"),
        ("enrichment_order_invalid", 422, "enrichment_order_invalid"),
        ("enrichment_order_group_unsupported", 422, "enrichment_order_group_unsupported"),
        ("enrichment_revision_no_change", 422, "enrichment_revision_no_change"),
        (
            "enrichment_revision_unsupported_operation",
            422,
            "enrichment_revision_unsupported_operation",
        ),
        ("bar_chart_insufficient_data", 422, "bar_chart_insufficient_data"),
        ("bar_chart_value_not_evidenced", 422, "bar_chart_value_not_evidenced"),
        ("editorial_figure_not_eligible", 422, "editorial_figure_not_eligible"),
        ("editorial_figure_not_changed", 422, "editorial_figure_not_changed"),
        ("brief_figure_limit_exceeded", 422, "brief_figure_limit_exceeded"),
        ("editorial_caption_text_violation", 422, "editorial_caption_text_violation"),
        ("enrichment_target_stale", 409, "enrichment_target_stale"),
        ("editorial_anchor_stale", 409, "editorial_anchor_stale"),
        ("proposal_base_stale", 409, "proposal_base_stale"),
        ("proposal_state_conflict", 409, "proposal_state_conflict"),
        ("editorial_snapshot_stale", 409, "editorial_snapshot_stale"),
        ("editorial_read_only", 403, "editorial_read_only"),
        ("proposal_not_found", 404, "proposal_not_found"),
        ("proposal_base_unavailable", 503, "manual_edit_unavailable"),
        ("proposal_workspace_unavailable", 503, "manual_edit_unavailable"),
        ("idempotency_key_invalid", 400, "idempotency_key_invalid"),
    ),
)
async def test_route_maps_closed_visual_revision_codes(
    code: str,
    expected_status: int,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    subject_id, occurrence_id = uuid4(), uuid4()
    reason = "workspace:visual_revision" if code == "proposal_base_stale" else None
    internal_error = EditorialProposalApplicationError(code, reason=reason)

    class ProposalService:
        async def prepare_visual_revision(self, **_kwargs: object) -> None:
            raise internal_error

    async def actor_id(_request: Request) -> UUID:
        return uuid4()

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", actor_id)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/enrichment-revisions",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": FastAPI(),
        }
    )
    payload = editorial_workspace_api.EditorialVisualCaptionRevisionRequest(
        snapshot_id=uuid4(),
        operation="update_visual_caption",
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        caption="Synthetic caption text must not be logged",
    )
    caplog.set_level(logging.WARNING, logger=editorial_workspace_api.__name__)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.prepare_enrichment_revision(
            subject_id,
            occurrence_id,
            request,
            payload,
            "visual-revision-key-0002",
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail["code"] == expected_code
    assert caught.value.detail["message"]
    assert caught.value.__cause__ is internal_error
    assert code in caplog.text
    assert str(subject_id) in caplog.text
    assert str(occurrence_id) in caplog.text
    assert "Synthetic caption text must not be logged" not in caplog.text


@pytest.mark.asyncio
async def test_route_maps_idempotency_conflict_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    class ProposalService:
        async def prepare_visual_revision(self, **_kwargs: object) -> None:
            raise EditorialProposalConflict("proposal_idempotency_conflict")

    async def actor_id(_request: Request) -> UUID:
        return uuid4()

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", actor_id)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/enrichment-revisions",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": FastAPI(),
        }
    )
    payload = editorial_workspace_api.EditorialVisualCaptionRevisionRequest(
        snapshot_id=uuid4(),
        operation="update_visual_caption",
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        caption="Synthetic caption",
    )

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.prepare_enrichment_revision(
            uuid4(), uuid4(), request, payload, "visual-revision-key-0003"
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "proposal_idempotency_conflict"
    assert caught.value.detail["message"] == ("Cette clé est déjà associée à une autre décision.")


# ---------------------------------------------------------------------------
# Happy paths through the real service factory, planner and proposal writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caption_revision_prepares_and_replays_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch)
    stack.occurrence_holder["value"] = stack.table_block_id
    body = _caption_body(stack.snapshot_id, stack.table_key, "Nouvelle légende synthétique")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        first = await _post(client, stack.subject_id, stack.table_block_id, body)
        replay = await _post(client, stack.subject_id, stack.table_block_id, body)
        conflict = await _post(
            client,
            stack.subject_id,
            stack.table_block_id,
            _caption_body(stack.snapshot_id, stack.table_key, "Autre légende"),
        )

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["operation"] == "update_visual_caption"
    assert payload["origin"] == "manual_patch"
    assert payload["state"] == "proposed"
    assert payload["thread_id"] is None
    assert payload["patch"]["visual_patch"] == {
        "kind": "update_visual_caption",
        "element_kind": "table",
        "element_key": stack.table_key,
        "caption": "Nouvelle légende synthétique",
    }
    assert payload["patch"]["schema_version"] == "editorial-enrichment-visual-patch-v1"
    assert payload["diagnostics"]["preview"]["previous_caption"] == "Caption table_one"
    assert replay.status_code == 200
    assert replay.json()["id"] == payload["id"]
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "proposal_idempotency_conflict"
    assert len(stack.repository.records) == 1
    assert stack.repository.records[0].model_run_id is None
    assert stack.repository.records[0].state is EditorialProposalState.PROPOSED


@pytest.mark.asyncio
async def test_move_revision_prepares_after_a_registered_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch)
    stack.occurrence_holder["value"] = stack.table_block_id
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "move_enrichment_element",
        "element_kind": "table",
        "element_key": stack.table_key,
        "anchor_block_id": str(ANCHOR_B),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, stack.table_block_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "move_enrichment_element"
    preview = payload["diagnostics"]["preview"]
    assert preview["from"] == {
        "kind": "after_paragraph",
        "section_index": None,
        "anchor_block_id": str(ANCHOR_A),
    }
    assert preview["to"] == {
        "kind": "after_paragraph",
        "section_index": None,
        "anchor_block_id": str(ANCHOR_B),
    }
    assert payload["patch"]["target"]["placement"] == {
        "kind": "after_paragraph",
        "anchor_block_id": str(ANCHOR_B),
        "section_id": None,
    }


@pytest.mark.asyncio
async def test_reorder_revision_prepares_on_the_anchor_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch)
    stack.occurrence_holder["value"] = stack.paragraph_block_id
    tables = stack.context.base_enrichment.tables
    body = {
        "snapshot_id": str(stack.snapshot_id),
        "operation": "reorder_enrichment_elements",
        "anchor_block_id": str(ANCHOR_A),
        "order": [
            {"kind": "table", "key": tables[1].key},
            {"kind": "table", "key": tables[0].key},
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, stack.paragraph_block_id, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["operation"] == "reorder_enrichment_elements"
    assert payload["diagnostics"]["preview"]["order"] == [
        {"kind": "table", "key": tables[1].key},
        {"kind": "table", "key": tables[0].key},
    ]
    assert payload["patch"]["target"]["kind"] == "enrichment_order"


@pytest.mark.asyncio
async def test_read_only_snapshot_refuses_the_revision_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch, read_only=True)
    stack.occurrence_holder["value"] = stack.table_block_id
    body = _caption_body(stack.snapshot_id, stack.table_key, "Nouvelle légende")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, stack.table_block_id, body)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "editorial_read_only"
    assert response.json()["detail"]["message"] == (
        "Cette version est en lecture seule : elle ne peut pas être modifiée."
    )
    assert stack.repository.records == []


@pytest.mark.asyncio
async def test_unknown_occurrence_is_refused_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_route(monkeypatch)
    stack = _visual_stack(monkeypatch)
    unknown = uuid4()
    stack.occurrence_holder["value"] = unknown
    body = _caption_body(stack.snapshot_id, stack.table_key, "Nouvelle légende")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="http://editorial.test"
    ) as client:
        response = await _post(client, stack.subject_id, unknown, body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "proposal_base_stale"
    assert stack.repository.records == []


def test_manual_proposal_service_wires_the_real_visual_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app-state factory must construct the pinned-artifact loader, not None."""
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            production_editorial_resource_search_enabled=False,
        ),
    )
    app = FastAPI()
    uow_factory = cast(Any, object())
    blob_store = cast(BlobStore, object())
    artifact_store = cast(Any, _MemoryArtifactStore({}))
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.model_gateway = _NoModelGateway()
    app.state.publication_render_service = object()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/subjects/s/editor/blocks/b/enrichment-revisions",
            "headers": [],
            "app": app,
        }
    )

    service = editorial_workspace_api._manual_proposal_service(request)

    loader = service._visual_revision_loader
    assert isinstance(loader, VisualRevisionContextLoader)
    assert loader._uow_factory is uow_factory
    assert loader._artifact_store is artifact_store
    assert loader._workspace_service is service._workspace_service
    assert loader._enrichment_loader._editorial_enrichment_service is not None
    assert loader._enrichment_revision_service._publication_render_service is (
        app.state.publication_render_service
    )


# ---------------------------------------------------------------------------
# Snapshot projection and OpenAPI surface
# ---------------------------------------------------------------------------


def _diagram_enrichment_payload() -> dict[str, object]:
    return {
        "key": "diagram_one",
        "kind": "infection_chain",
        "title": "Synthetic infection chain",
        "caption": "Synthetic diagram caption",
        "direction": "left_to_right",
        "profile": "flow",
        "nodes": [
            {"node_id": "loader", "label": "Loader", "role": "malware_tool"},
            {"node_id": "payload", "label": "Payload", "role": "data_artifact"},
        ],
        "edges": [
            {"source_node_id": "loader", "target_node_id": "payload", "label": "loads"},
        ],
        "groups": [
            {"group_id": "host", "label": "Victim host", "node_ids": ["loader", "payload"]},
        ],
        "placement": {"kind": "end", "section_index": None, "anchor_block_id": None},
    }


def test_snapshot_projection_exposes_the_bounded_diagram() -> None:
    from cti_app.application.editorial_workspace.registry import project_blocks
    from cti_app.domain.editorial_workspace import bootstrap_block_registry

    synthesis = {"title": "Synthetic report", "lead": [], "sections": []}
    enrichment = {
        "tables": [],
        "diagrams": [_diagram_enrichment_payload()],
        "charts": [],
        "source_figures": [],
    }
    entries = (
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="6" * 64,
            source_stage="synthesis",
            canonical_content=synthesis,
        ),
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="7" * 64,
            source_stage="editorial_enrichment",
            canonical_content=enrichment,
        ),
    )
    _blocks, enrichments, _outline = project_blocks(
        entries=entries, synthesis=synthesis, enrichment=enrichment
    )

    assert len(enrichments) == 1
    block = enrichments[0]
    assert block["element_kind"] == "diagram"
    assert block["element_key"] == "diagram_one"
    assert block["caption"] == "Synthetic diagram caption"
    assert block["diagram"] == {
        "key": "diagram_one",
        "kind": "infection_chain",
        "title": "Synthetic infection chain",
        "caption": "Synthetic diagram caption",
        "direction": "left_to_right",
        "profile": "flow",
        "nodes": [
            {"node_id": "loader", "label": "Loader", "role": "malware_tool"},
            {"node_id": "payload", "label": "Payload", "role": "data_artifact"},
        ],
        "edges": [
            {"source_node_id": "loader", "target_node_id": "payload", "label": "loads"},
        ],
        "groups": [
            {"group_id": "host", "label": "Victim host", "node_ids": ["loader", "payload"]},
        ],
        "anchor_block_id": None,
        "placement": "end",
    }
    view = editorial_workspace_api.EditorialWorkspaceBlock.model_validate(block)
    assert view.diagram is not None
    assert view.diagram.nodes[0].label == "Loader"
    assert view.diagram.groups[0].node_ids == ["loader", "payload"]


def test_openapi_exposes_the_generic_route_and_block_fields() -> None:
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    schema = app.openapi()

    route = schema["paths"][
        "/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions"
    ]
    assert "post" in route
    assert "Idempotency-Key" in {
        parameter["name"] for parameter in route["post"].get("parameters", [])
    }
    block_properties = schema["components"]["schemas"]["EditorialWorkspaceBlock"]["properties"]
    assert {
        "element_kind",
        "element_key",
        "order",
        "caption",
        "allowed_operations",
        "diagram",
        "chart",
        "figure",
    } <= set(block_properties)
    # A chart placed after the lead, a section or the end has no paragraph
    # anchor; the OpenAPI contract must keep the nullable field explicit.
    chart_anchor = schema["components"]["schemas"]["EditorialWorkspaceBarChart"]["properties"][
        "anchor_block_id"
    ]
    assert {"type": "null"} in chart_anchor.get("anyOf", [])
    # Request-shape bounds come from the domain constants (single source).
    from cti_app.domain.production_editorial_enrichment import (
        MAX_BAR_CHART_POINTS,
        MAX_DIAGRAM_EDGES,
        MAX_DIAGRAM_NODES,
    )

    components = schema["components"]["schemas"]
    assert (
        components["EditorialVisualChartRequest"]["properties"]["points"]["maxItems"]
        == MAX_BAR_CHART_POINTS
    )
    assert (
        components["EditorialVisualDiagramRequest"]["properties"]["nodes"]["maxItems"]
        == MAX_DIAGRAM_NODES
    )
    assert (
        components["EditorialVisualDiagramRequest"]["properties"]["edges"]["maxItems"]
        == MAX_DIAGRAM_EDGES
    )
    assert {"relation_type", "direction"} <= set(
        components["EditorialVisualDiagramEdgeRequest"]["properties"]
    )
    request_schema = route["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["discriminator"]["propertyName"] == "operation"
    serialized = json.dumps(request_schema)
    for operation in (
        "revise_chart",
        "revise_diagram",
        "update_visual_caption",
        "move_enrichment_element",
        "reorder_enrichment_elements",
        "replace_source_figure",
        "remove_enrichment_element",
    ):
        assert operation in serialized


def test_figure_and_chart_projections_carry_the_comparison_fields() -> None:
    from cti_app.application.editorial_workspace.registry import project_blocks
    from cti_app.domain.editorial_workspace import bootstrap_block_registry

    canonical_figure_id = uuid4()
    opaque_handle = uuid4()
    figure = {
        "key": "figure_one",
        "caption": "Editorial caption",
        "provenance": "Figure 1 from the synthetic report",
        "source_url": "https://example.test/report",
        "inclusion_status": "included",
        "resolved_figure": {
            "figure_id": str(canonical_figure_id),
            "sha256": "a" * 64,
            "mime_type": "image/png",
            "byte_size": 128,
            "source": "https://example.test/report",
        },
        "width": 640,
        "height": 480,
        "locator": {"page": 2, "section": "Overview"},
        "placement": {"kind": "end", "section_index": None, "anchor_block_id": None},
    }
    evidence_ref = {
        "source_document_id": str(uuid4()),
        "kind": "fact",
        "evidence_key": "c" * 64,
    }
    chart = {
        "key": "chart_one",
        "kind": "bar",
        "title": "Synthetic chart",
        "caption": "Synthetic chart caption",
        "category_axis_label": "Command",
        "value_axis_label": "Count",
        "unit": "hosts",
        "points": [
            {
                "label": "First",
                "series": "Observed",
                "value": "1",
                "evidence_refs": [evidence_ref],
            },
            {
                "label": "Second",
                "series": "Observed",
                "value": "2",
                "evidence_refs": [evidence_ref],
            },
        ],
        "placement": {"kind": "end", "section_index": None, "anchor_block_id": None},
    }
    synthesis = {"title": "Synthetic report", "lead": [], "sections": []}
    enrichment = {
        "tables": [],
        "diagrams": [],
        "charts": [chart],
        "source_figures": [figure],
    }
    entries = (
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="6" * 64,
            source_stage="synthesis",
            canonical_content=synthesis,
        ),
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="7" * 64,
            source_stage="editorial_enrichment",
            canonical_content=enrichment,
        ),
    )
    _blocks, enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
        catalog_figure_ids={canonical_figure_id: str(opaque_handle)},
        catalog_figure_captions={canonical_figure_id: "Original archived caption"},
    )

    figure_block = next(block for block in enrichments if block["kind"] == "figure")
    chart_block = next(block for block in enrichments if block["kind"] == "chart")
    assert figure_block["figure"]["catalog_figure_id"] == str(opaque_handle)
    assert figure_block["figure"]["original_caption"] == "Original archived caption"
    assert figure_block["figure"]["caption"] == "Editorial caption"
    assert figure_block["figure"]["provenance"] == "Figure 1 from the synthetic report"
    assert (figure_block["figure"]["width"], figure_block["figure"]["height"]) == (640, 480)
    assert chart_block["chart"] == {
        "key": "chart_one",
        "kind": "bar",
        "title": "Synthetic chart",
        "caption": "Synthetic chart caption",
        "legend": ["Observed"],
        "category_axis_label": "Command",
        "value_axis_label": "Count",
        "unit": "hosts",
        "points": [
            {"label": "First", "value": "1", "evidence_refs": [evidence_ref]},
            {"label": "Second", "value": "2", "evidence_refs": [evidence_ref]},
        ],
        "anchor_block_id": None,
    }
    view = editorial_workspace_api.EditorialWorkspaceBlock.model_validate(figure_block)
    assert view.figure is not None
    assert view.figure.original_caption == "Original archived caption"
