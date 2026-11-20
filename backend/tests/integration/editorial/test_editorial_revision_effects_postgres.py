from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
    EditorialRevisionEffectsError,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
)
from cti_app.application.editorial_workspace.registry import (
    resolve_occurrence as resolve_workspace_occurrence,
)
from cti_app.application.model_gateway import ModelExecution
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    SynthesisAccessPolicyV1,
    SynthesisAccessSourceV1,
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import (
    ProductionSynthesisRevisionService,
    TargetedEditorialRevisionConflictError,
    _RevisionContext,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.editorial_workspace import workspace_pin_hash
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelUsage,
)
from cti_app.domain.production import (
    ProductionArtifactStage,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_V3_POLICY_VERSION,
    EditorialEnrichmentV1,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_kind import PublicationKind
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _adopt_kwargs,
    _seed,
    _Seeded,
    _service,
)

pytestmark = pytest.mark.integration


class _CurrentWorkspace:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def advance(
        self,
        *,
        base_registry: tuple[Any, ...],
        canonical_sha256: str,
        retired_block_ids: tuple[UUID, ...] = (),
        **_kwargs: Any,
    ) -> Any:
        from dataclasses import replace as dataclass_replace

        from cti_app.domain.editorial_workspace import (
            AdvancedBlockRegistry,
            EditorialRevisionStatus,
            EditorialTransition,
        )

        active = tuple(
            dataclass_replace(
                entry,
                content_sha256=canonical_sha256,
                revision_status=EditorialRevisionStatus.READY,
                transition=EditorialTransition.REWRITE,
            )
            for entry in base_registry
        )
        return AdvancedBlockRegistry(
            active=active,
            retired_block_ids=frozenset(retired_block_ids),
        )

    async def snapshot_record_in_uow(self, uow: Any, subject_id: UUID) -> WorkspaceSnapshotRecord:
        record = await uow.editorial_workspace_snapshots.latest_for_run(
            await self._run_id_for_subject(uow, subject_id)
        )
        if record is None or record.subject_id != subject_id:
            raise EditorialRevisionEffectsError("editorial_snapshot_missing")
        return cast(WorkspaceSnapshotRecord, record)

    async def resolve_occurrence_in_uow(
        self, uow: Any, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        record = await self.snapshot_record_in_uow(uow, subject_id)
        return resolve_workspace_occurrence(record, occurrence_id)

    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        async with self._uow_factory() as uow:
            return await self.resolve_occurrence_in_uow(uow, subject_id, occurrence_id)

    @staticmethod
    async def _run_id_for_subject(uow: Any, subject_id: UUID) -> UUID:
        run = await uow.production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise EditorialRevisionEffectsError("editorial_run_missing")
        return cast(UUID, run.id)


class _DeterministicRevisionGateway:
    def __init__(self, uow_factory: UnitOfWorkFactory, wire: str) -> None:
        self._uow_factory = uow_factory
        self._wire = wire
        self.runs: dict[UUID, ModelRun] = {}
        self.outputs: dict[str, bytes] = {}
        self.calls = 0

    async def draft(self, request: Any, _output_schema: object | None = None) -> ModelExecution:
        self.calls += 1
        run = ModelRun(
            provider=ModelProvider.FAKE,
            model_role=ModelRole.DRAFTING,
            requested_model="synthetic-revision-gateway",
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            authorized_input_hash=hashlib.sha256(request.text.encode()).hexdigest(),
            evidence_pack_hash=request.evidence_pack_hash,
            parameters=dict(request.parameters),
            id=request.run_id or uuid4(),
        )
        raw_reference = f"synthetic-model-output://{run.id}"
        raw_bytes = self._wire.encode()
        run.raw_output_reference = raw_reference
        run.raw_output_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        run.raw_output_chars = len(self._wire)
        run.succeed(
            actual_model_version="synthetic-revision-gateway-v1",
            duration_ms=0,
            usage=ModelUsage(total_tokens=1),
            output_references=(raw_reference,),
            response_id=None,
        )
        async with self._uow_factory() as uow:
            await uow.model_runs.add(run)
            await uow.commit()
        self.runs[run.id] = run
        self.outputs[raw_reference] = raw_bytes
        return ModelExecution(run=run, output_text=self._wire)

    async def get_run(self, run_id: UUID) -> ModelRun | None:
        return self.runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        content = self.outputs[reference]
        if len(content) > max_bytes:
            raise ValueError("synthetic_output_over_limit")
        return content


def _revision_wire(seeded: _Seeded, base_sha256: str) -> str:
    title_hash = hashlib.sha256(seeded.synthesis.title.encode()).hexdigest()
    return "\n".join(
        (
            "@@TARGET@@",
            "synthesis.title",
            "@@ACTION@@",
            "rewrite_title",
            "@@BASE_ARTIFACT_HASH@@",
            base_sha256,
            "@@BEFORE_HASH@@",
            title_hash,
            "@@AFTER@@",
            "@@TITLE@@",
            "[Synthetic actor] Revised report",
            "@@END TITLE@@",
            "@@RATIONALE@@",
            "Deterministic synthetic revision.",
            "@@END@@",
        )
    )


async def _revision_service(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
) -> tuple[
    ProductionSynthesisRevisionService,
    TargetedEditorialRevisionRequest,
    Any,
    Any,
    _CurrentWorkspace,
]:
    workspace = _CurrentWorkspace(uow_factory)
    effects = EditorialRevisionEffects(workspace_service=workspace)
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    base_bytes = await artifact_store.read_bytes(seeded.base_artifact.canonical_blob_id)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    policy = SynthesisAccessPolicyV1(
        subject_tlp=TLP.AMBER,
        effective_tlp=TLP.AMBER,
        external_llm_allowed=True,
        do_not_submit=False,
        sources=tuple(
            SynthesisAccessSourceV1(
                source_document_id=source.source_document_id,
                tlp=TLP.AMBER,
                external_llm_allowed=True,
                do_not_submit=False,
            )
            for source in seeded.extraction.sources
        ),
    )
    wire = _revision_wire(seeded, base_sha256)
    gateway = _DeterministicRevisionGateway(uow_factory, wire)
    synthesis_persistence = SynthesisService(uow_factory, artifact_store)
    synthesis_service = ProductionSynthesisService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=gateway,
        synthesis_service=synthesis_persistence,
    )
    references_artifact = replace(
        seeded.base_artifact,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash="a" * 64,
    )
    extraction_artifact = replace(
        seeded.base_artifact,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=canonical_extraction_hash(seeded.extraction),
    )
    context = _RevisionContext(
        run=seeded.run,
        snapshot=seeded.production_input,
        base_artifact=seeded.base_artifact,
        base_payload=production_synthesis_to_json(seeded.synthesis),
        references_artifact=references_artifact,
        extraction_artifact=extraction_artifact,
        projection_artifact=None,
        publication_artifact=None,
        effective_repair_decisions=(),
        publication_kind=PublicationKind.ARTICLE,
    )

    async def load_context(
        _request: TargetedEditorialRevisionRequest, *, uow: Any | None = None
    ) -> _RevisionContext:
        del uow
        return context

    async def load_extraction(_run: Any, _snapshot: Any, _artifact: Any):
        return seeded.extraction, canonical_extraction_hash(seeded.extraction)

    async def load_access_policy(_snapshot: Any, _extraction: Any):
        return policy

    async def no_critical_failures(_context: Any, _extraction: Any):
        return (), ()

    synthesis_service._load_extraction = load_extraction  # type: ignore[method-assign]
    synthesis_service._load_access_policy = load_access_policy  # type: ignore[method-assign]
    revision_service = ProductionSynthesisRevisionService(
        synthesis_service=synthesis_service,
        synthesis_persistence=synthesis_persistence,
        enrichment_persistence=EditorialEnrichmentService(uow_factory, artifact_store),
        publication_render_service=SimpleNamespace(),  # type: ignore[arg-type]
        revision_effects=effects,
    )
    revision_service._load_context = load_context  # type: ignore[method-assign]
    revision_service._critical_extraction_assessment = no_critical_failures  # type: ignore[method-assign]
    request = TargetedEditorialRevisionRequest(
        subject_id=seeded.subject.id,
        base_artifact_id=seeded.base_artifact.id,
        base_version=seeded.base_artifact.version,
        base_input_hash=seeded.base_artifact.input_hash,
        base_canonical_sha256=base_sha256,
        target=TargetedEditorialRevisionTarget(kind=TargetedEditorialRevisionTargetKind.TITLE),
        operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
        instruction="Apply the deterministic synthetic title revision.",
        mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
        requester="dev-analyst",
    )
    return revision_service, request, gateway, artifact_store, workspace


async def _seed_valid_empty_enrichment(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
) -> _Seeded:
    workspace = seeded.workspace
    effects = EditorialRevisionEffects(workspace_service=workspace)
    enrichment_service = EditorialEnrichmentService(uow_factory, artifact_store)
    enrichment = EditorialEnrichmentV1(
        schema_version=3,
        subject_id=seeded.subject.id,
        production_input_hash=seeded.production_input.input_hash,
        extraction_hash=canonical_extraction_hash(seeded.extraction),
        synthesis_hash=canonical_synthesis_hash(seeded.synthesis),
        publication_language=seeded.production_input.publication_language,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V3_POLICY_VERSION,
        tables=(),
        diagrams=(),
        charts=(),
        source_figures=(),
        annotations=(),
        warnings=(),
    )
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input, seeded.extraction, seeded.synthesis, None
    )
    evidence_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    access_hash = "e" * 64
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack_hash=evidence_hash,
        access_policy_hash=access_hash,
        projection_hash=None,
        source_figure_inventory_hash=None,
    )
    payload = editorial_enrichment_to_json(enrichment)
    payload_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    async with uow_factory() as uow:
        guard = await effects.acquire_run_guard(uow, seeded.run.id)
        await effects.lock_run_row(uow, guard)
        artifact = await enrichment_service.store_editorial_enrichment_result(
            run_id=seeded.run.id,
            subject_id=seeded.subject.id,
            input_hash=input_hash,
            enrichment=enrichment,
            extraction=seeded.extraction,
            synthesis=seeded.synthesis,
            raw_result=None,
            model_run_id=None,
            evidence_pack_hash=evidence_hash,
            access_policy_hash=access_hash,
            model_policy_version="synthetic-enrichment-policy",
            routing_policy_version="synthetic-routing-policy",
            projection_hash=None,
            source_figure_inventory_hash=None,
            metadata_extra={"source_figure_inventory_hash": None},
            canonical_payload=payload,
            uow=uow,
        )
        result = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=seeded.snapshot,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            artifact=artifact,
            canonical_sha256=hashlib.sha256(payload_bytes).hexdigest(),
            canonical_content=payload,
            lineage=None,
            actor_id=seeded.actor_id,
        )
        await uow.commit()
    return replace(seeded, snapshot=result.snapshot)


def _snapshot_pins(record: WorkspaceSnapshotRecord) -> dict[str, object]:
    return {
        "subject_id": record.subject_id,
        "production_run_id": record.production_run_id,
        "synthesis_artifact_id": record.synthesis_artifact_id,
        "synthesis_version": record.synthesis_version,
        "synthesis_input_hash": record.synthesis_input_hash,
        "synthesis_canonical_sha256": record.synthesis_canonical_sha256,
        "enrichment_artifact_id": record.enrichment_artifact_id,
        "enrichment_version": record.enrichment_version,
        "enrichment_input_hash": record.enrichment_input_hash,
        "enrichment_canonical_sha256": record.enrichment_canonical_sha256,
        "publication_artifact_id": record.publication_artifact_id,
        "publication_version": record.publication_version,
        "publication_input_hash": record.publication_input_hash,
        "publication_canonical_sha256": record.publication_canonical_sha256,
        "pipeline_generation": record.pipeline_generation,
        "routing_decision_id": record.routing_decision_id,
        "routing_decision_hash": record.routing_decision_hash,
        "publication_kind": record.publication_kind,
    }


@pytest.mark.asyncio
async def test_synthesis_revise_advances_registry_and_stales_changed_pin_proposals(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=2, article_base=True)
    revision_service, request, gateway, _artifact_store, _workspace = await _revision_service(
        seeded, uow_factory
    )

    result = await revision_service.revise(request)

    assert result.artifact is not None
    assert gateway.calls == 1
    async with uow_factory() as uow:
        latest = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        assert latest is not None
        assert latest.parent_snapshot_id == seeded.snapshot.snapshot_id
        assert latest.synthesis_artifact_id == result.artifact.id
        assert latest.synthesis_version == seeded.base_artifact.version + 1
        for proposal in seeded.proposals:
            current = await uow.editorial_proposals.get_for_occurrence(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            assert current is not None and current.state is EditorialProposalState.STALE
            assert current.state_version == proposal.state_version + 1
        assert await uow.editorial_proposal_impacts.get_by_proposal(seeded.proposals[0].id) is None


@pytest.mark.asyncio
async def test_accept_races_real_synthesis_revise_with_one_winner_and_one_snapshot(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1, article_base=True)
    proposal = seeded.proposals[0]
    revision_service, request, gateway, _artifact_store, workspace = await _revision_service(
        seeded, uow_factory
    )
    seeded = replace(seeded, workspace=workspace)
    adoption_service = _service(seeded, uow_factory)

    results = await asyncio.gather(
        adoption_service.adopt(**_adopt_kwargs(seeded, proposal, f"accept-{uuid4().hex}")),
        revision_service.revise(request),
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    assert len(winners) == 1
    assert gateway.calls == 1
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(
        failures[0], (EditorialProposalAdoptionError, TargetedEditorialRevisionConflictError)
    )
    async with uow_factory() as uow:
        current_proposal = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        current_synthesis = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        latest = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        assert current_proposal is not None
        assert current_synthesis is not None
        assert latest is not None
        assert latest.parent_snapshot_id == seeded.snapshot.snapshot_id
        assert current_synthesis.id == latest.synthesis_artifact_id
        if current_proposal.state is EditorialProposalState.ACCEPTED:
            impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
            assert impact is not None and impact.accepted_snapshot_id == latest.snapshot_id
        else:
            assert current_proposal.state is EditorialProposalState.STALE
            assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None


@pytest.mark.asyncio
async def test_synthesis_revise_with_valid_enrichment_rebase_appends_one_final_snapshot(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=0, article_base=True)
    revision_service, request, gateway, artifact_store, workspace = await _revision_service(
        seeded, uow_factory
    )
    seeded = replace(seeded, workspace=workspace)
    seeded = await _seed_valid_empty_enrichment(seeded, uow_factory, artifact_store)

    async def skip_publication_assembly(**_kwargs: Any) -> None:
        return None

    revision_service._assemble_and_render = skip_publication_assembly  # type: ignore[method-assign]

    result = await revision_service.revise(request)

    assert result.artifact is not None
    assert result.revision is not None
    assert result.revision["enrichment_artifact_id"] is not None
    assert gateway.calls == 1
    assert seeded.snapshot.enrichment_artifact_id is not None
    async with uow_factory() as uow:
        latest = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        assert latest is not None
        assert latest.parent_snapshot_id == seeded.snapshot.snapshot_id
        assert latest.synthesis_artifact_id == result.artifact.id
        assert latest.enrichment_artifact_id == UUID(str(result.revision["enrichment_artifact_id"]))
        intermediate_pins = _snapshot_pins(seeded.snapshot)
        intermediate_pins.update(
            {
                "synthesis_artifact_id": result.artifact.id,
                "synthesis_version": result.artifact.version,
                "synthesis_input_hash": result.artifact.input_hash,
                "synthesis_canonical_sha256": hashlib.sha256(
                    await artifact_store.read_bytes(result.artifact.canonical_blob_id)
                ).hexdigest(),
            }
        )
        intermediate_pin_hash = workspace_pin_hash(intermediate_pins)
        assert (
            await uow.editorial_workspace_snapshots.get_by_pin(seeded.run.id, intermediate_pin_hash)
            is None
        )
