from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application import editorial_proposal_adoption_inputs as adoption_inputs_module
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_workspace.registry import resolve_workspace_target
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposalOrigin
from cti_app.domain.editorial_workspace import EditorialSourceStage, bootstrap_block_registry
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
)
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.publication_kind import PublicationKind


class _ForbiddenArtifactStore:
    async def read_bytes(self, *_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("artifact storage must not be read during UOW revalidation")


class _ForbiddenGateway:
    def __getattr__(self, _name: str) -> Any:
        raise AssertionError("gateway/model services must not be used during UOW revalidation")


class _Repository:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def get(self, _key: Any) -> Any:
        return self.value

    async def get_for_update(self, _key: Any) -> Any:
        return self.value

    async def get_current_for_subject(self, _subject_id: Any) -> Any:
        return self.value

    async def get_by_run(self, _run_id: Any) -> Any:
        return self.value

    async def get_current(self, _run_id: Any, stage: str) -> Any:
        return self.value.get(stage) if isinstance(self.value, dict) else self.value


class _Uow:
    def __init__(
        self,
        *,
        document: Any,
        collection: Any,
        subject: Any,
        run: Any,
        snapshot: Any,
        artifacts: Any,
    ) -> None:
        self.subjects = _Repository(subject)
        self.source_documents = _Repository(document)
        self.source_collections = _Repository(collection)
        self.editorial_threads = _Repository(None)
        self.production_runs = _Repository(run)
        self.production_input_snapshots = _Repository(snapshot)
        self.production_artifacts = _Repository(artifacts)
        self.write_count = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _revalidation_fixture(
    *,
    tlp: TLP = TLP.AMBER,
    subject_tlp: TLP = TLP.CLEAR,
    subject_version: int = 1,
    external_llm_allowed: bool = True,
    revoked: bool = False,
) -> tuple[Any, Any, Any, Any, Any]:
    subject_id, run_id, occurrence_id = uuid4(), uuid4(), uuid4()
    source_id = uuid4()
    document = SimpleNamespace(
        id=source_id,
        subject_id=subject_id,
        source_collection_id=uuid4(),
        tlp=tlp,
        external_llm_allowed=external_llm_allowed,
        do_not_submit=False,
    )
    collection = SimpleNamespace(
        id=document.source_collection_id,
        subject_id=subject_id,
        source_tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
        sensitivity="internal",
        state=CollectionState.BLOCKED if revoked else CollectionState.COMPLETED,
        source_document_id=None,
    )
    subject = SimpleNamespace(id=subject_id, tlp=subject_tlp, version=subject_version)
    proposal = SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        occurrence_id=occurrence_id,
        snapshot_id=uuid4(),
        base_pin_hash="a" * 64,
        base_artifact_id=uuid4(),
        base_artifact_version=1,
        base_input_hash="b" * 64,
        base_canonical_sha256="c" * 64,
        base_routing_hash=None,
        thread_id=None,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        evidence_refs=(),
    )
    record = SimpleNamespace(
        routing_decision_id=None,
        routing_decision_hash=None,
        snapshot_id=proposal.snapshot_id,
        subject_id=subject_id,
        production_run_id=run_id,
        workspace_pin_hash=proposal.base_pin_hash,
        synthesis_artifact_id=proposal.base_artifact_id,
        synthesis_version=proposal.base_artifact_version,
        synthesis_canonical_sha256=proposal.base_canonical_sha256,
    )
    resolution = SimpleNamespace(
        record=record,
        entry=SimpleNamespace(block_id=proposal.occurrence_id),
        binding_state="valid",
    )
    run = SimpleNamespace(
        id=run_id, subject_id=subject_id, routing_decision_id=None, pipeline_generation=0
    )
    snapshot = SimpleNamespace(
        id=proposal.snapshot_id,
        production_run_id=run_id,
        subject_id=subject_id,
        subject_version=1,
        subject_tlp=TLP.CLEAR,
        input_hash="snapshot-input",
    )
    base_artifact = SimpleNamespace(
        id=proposal.base_artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=proposal.base_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    extraction_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="extraction-input",
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    synthesis_artifact = base_artifact
    context = SimpleNamespace(
        run=run,
        base_artifact=base_artifact,
        extraction_artifact=extraction_artifact,
        projection_artifact=None,
        synthesis_artifact=synthesis_artifact,
        stage=ProductionArtifactStage.SYNTHESIS,
        snapshot=snapshot,
        extraction=SimpleNamespace(
            subject_id=subject_id,
            sources=(SimpleNamespace(source_document_id=source_id),),
        ),
        evidence_pack=SimpleNamespace(_handle_to_ref={}),
    )
    inputs = cast(Any, object.__new__(ProductionEditorialProposalAdoptionInputs))
    inputs._prepared = {proposal.id: context}
    inputs._artifact_store = _ForbiddenArtifactStore()
    inputs._synthesis_loader = _ForbiddenGateway()
    inputs._enrichment_loader = _ForbiddenGateway()
    uow = _Uow(
        document=document,
        collection=collection,
        subject=subject,
        run=run,
        snapshot=snapshot,
        artifacts={
            ProductionArtifactStage.SYNTHESIS.value: base_artifact,
            ProductionArtifactStage.EXTRACTION.value: extraction_artifact,
            ProductionArtifactStage.RELEVANCE_PROJECTION.value: None,
        },
    )
    return inputs, proposal, resolution, uow, context


def _figure_revalidation_fixture() -> tuple[Any, Any, Any, Any, Any]:
    inputs, proposal, resolution, uow, context = _revalidation_fixture()
    synthesis_artifact = context.synthesis_artifact
    enrichment_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=proposal.production_run_id,
        subject_id=proposal.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash="figure-enrichment-input",
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    context.stage = ProductionArtifactStage.EDITORIAL_ENRICHMENT
    context.base_artifact = enrichment_artifact
    context.base_enrichment = SimpleNamespace(source_figures=())
    context.synthesis_artifact = synthesis_artifact
    context.synthesis = SimpleNamespace()
    proposal.operation = TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value
    proposal.base_artifact_id = enrichment_artifact.id
    proposal.base_artifact_version = enrichment_artifact.version
    proposal.base_canonical_sha256 = "d" * 64
    resolution.record.enrichment_artifact_id = enrichment_artifact.id
    resolution.record.enrichment_version = enrichment_artifact.version
    resolution.record.enrichment_canonical_sha256 = proposal.base_canonical_sha256
    resolution.record.publication_kind = PublicationKind.ARTICLE
    uow.production_artifacts.value[ProductionArtifactStage.EDITORIAL_ENRICHMENT.value] = (
        enrichment_artifact
    )
    uow.blobs = SimpleNamespace()
    inputs._enrichment_revision_service = SimpleNamespace(
        _enrichment=SimpleNamespace(_source_media_archiver=None, _artifact_store=object())
    )
    return inputs, proposal, resolution, uow, context


@pytest.mark.asyncio
async def test_current_red_subject_tlp_is_rechecked_in_uow_without_writes() -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()
    uow.subjects.value.tlp = TLP.RED
    uow.subjects.value.version = 2

    with pytest.raises(EditorialProposalAdoptionError, match="external_llm_tlp_blocked"):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.asyncio
async def test_current_subject_version_is_part_of_the_adoption_fence() -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture(
        subject_tlp=TLP.AMBER,
        subject_version=2,
    )

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_base_stale"):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.asyncio
async def test_access_policy_uses_current_subject_tlp_and_keeps_more_restrictive_sources() -> None:
    inputs, _proposal, _resolution, uow, context = _revalidation_fixture(
        tlp=TLP.CLEAR,
        subject_tlp=TLP.GREEN,
    )
    inputs._uow_factory = lambda: uow

    policy, subject_version = await inputs._access_policy(context.snapshot, context.extraction)

    assert policy.subject_tlp is TLP.GREEN
    assert policy.effective_tlp is TLP.GREEN
    assert subject_version == 1


@pytest.mark.asyncio
async def test_thread_origin_artifact_can_be_older_than_current_proposal_binding() -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()
    thread_id = uuid4()
    proposal.thread_id = thread_id
    uow.editorial_threads.value = SimpleNamespace(
        thread=SimpleNamespace(
            id=thread_id,
            origin=SimpleNamespace(
                subject_id=proposal.subject_id,
                production_run_id=proposal.production_run_id,
                occurrence_id=proposal.occurrence_id,
                artifact_id=uuid4(),
                artifact_version=1,
                canonical_sha256="d" * 64,
            ),
        )
    )

    await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_field", ["subject_id", "occurrence_id"])
async def test_thread_origin_scope_mismatch_is_refused(scope_field: str) -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()
    thread_id = uuid4()
    proposal.thread_id = thread_id
    origin = {
        "subject_id": proposal.subject_id,
        "production_run_id": proposal.production_run_id,
        "occurrence_id": proposal.occurrence_id,
        "artifact_id": proposal.base_artifact_id,
        "artifact_version": proposal.base_artifact_version,
        "canonical_sha256": proposal.base_canonical_sha256,
    }
    origin[scope_field] = uuid4()
    uow.editorial_threads.value = SimpleNamespace(
        thread=SimpleNamespace(id=thread_id, origin=SimpleNamespace(**origin))
    )

    with pytest.raises(EditorialProposalAdoptionError, match="thread_binding_stale"):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.asyncio
async def test_production_di_exercises_synthesis_and_enrichment_inputs_without_external_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inputs, proposal, resolution, backing_uow, context = _revalidation_fixture()
    proposal.actor_id = uuid4()
    backing_uow.subjects.value.tlp = TLP.GREEN
    context.snapshot = ProductionInputSnapshot(
        production_run_id=proposal.production_run_id,
        edition_id=uuid4(),
        subject_id=proposal.subject_id,
        subject_version=1,
        subject_title="Synthetic subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Synthetic summary.",
        actor_or_campaign="Synthetic actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        publication_language="fr",
        research_date=date(2026, 2, 1),
    )

    class _TrackedUowContext:
        def __init__(self, factory: _TrackedUowFactory) -> None:
            self.factory = factory

        async def __aenter__(self) -> _Uow:
            self.factory.open_uows += 1
            return cast(_Uow, backing_uow)

        async def __aexit__(self, *_args: object) -> None:
            self.factory.open_uows -= 1

    class _TrackedUowFactory:
        def __init__(self) -> None:
            self.open_uows = 0

        def __call__(self) -> _TrackedUowContext:
            return _TrackedUowContext(self)

    class _ExternalGuard:
        def __init__(self, factory: _TrackedUowFactory, name: str) -> None:
            self.factory = factory
            self.name = name
            self.calls = 0

        def __getattr__(self, _name: str) -> Any:
            self.calls += 1
            if self.factory.open_uows:
                raise AssertionError(f"{self.name} called while a UOW is open")
            raise AssertionError(f"unexpected {self.name} call")

    settings = Settings(_env_file=None)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: settings)
    app = api_main.create_app()
    factory = _TrackedUowFactory()
    gateway = _ExternalGuard(factory, "gateway")
    blob_store = _ExternalGuard(factory, "blob store")
    app.state.uow_factory = factory
    app.state.production_artifact_store = object()
    app.state.blob_store = blob_store
    app.state.model_gateway = gateway
    app.state.publication_render_service = object()
    service = editorial_workspace_api._proposal_adoption_service(SimpleNamespace(app=app))
    production_inputs = service._inputs
    assert isinstance(production_inputs, ProductionEditorialProposalAdoptionInputs)

    async def load_context(_proposal: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return (
            context.run,
            context.snapshot,
            context.extraction_artifact,
            None,
            context.synthesis_artifact,
        )

    monkeypatch.setattr(production_inputs, "_load_base_context", load_context)

    async def load_synthesis_extraction(*_args: Any, **_kwargs: Any) -> tuple[Any, str]:
        return context.extraction, "f" * 64

    async def load_enrichment_extraction(*_args: Any, **_kwargs: Any) -> Any:
        return context.extraction

    async def load_synthesis(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(
        production_inputs._synthesis_loader,
        "_load_extraction",
        load_synthesis_extraction,
    )
    monkeypatch.setattr(
        production_inputs._enrichment_loader,
        "_load_extraction",
        load_enrichment_extraction,
    )
    monkeypatch.setattr(production_inputs._enrichment_loader, "_load_synthesis", load_synthesis)
    monkeypatch.setattr(
        adoption_inputs_module,
        "build_synthesis_evidence_pack",
        lambda *_args, **_kwargs: SimpleNamespace(_handle_to_ref={}),
    )
    monkeypatch.setattr(
        adoption_inputs_module,
        "production_synthesis_from_json",
        lambda _payload: SimpleNamespace(),
    )

    synthesis_target = SimpleNamespace(to_json=lambda: {"kind": "title"})
    monkeypatch.setattr(
        adoption_inputs_module,
        "resolve_workspace_target",
        lambda **_kwargs: SimpleNamespace(
            legacy_target_id="synthetic-target",
            target_id="synthetic-target",
            before_hash="e" * 64,
            target=synthesis_target,
            path="/title",
        ),
    )
    proposal.operation = TargetedEditorialRevisionOperation.REWRITE_TITLE.value
    proposal.target_id = "synthetic-target"
    proposal.before_hash = "e" * 64
    synthesis_artifact = context.base_artifact

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_patch_invalid"):
        await production_inputs.synthesis_inputs(
            proposal,
            {"target": {"kind": "title"}},
            base_artifact=synthesis_artifact,
            base_payload={},
            base_bytes=b"synthetic base",
            resolution=resolution,
        )

    enrichment_target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_synthetic",
    )
    proposal.operation = TargetedEditorialRevisionOperation.REWRITE_TABLE.value
    proposal.target_id = adoption_inputs_module._target_id(enrichment_target)
    enrichment_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=proposal.production_run_id,
        subject_id=proposal.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash=proposal.base_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
        metadata={},
    )
    resolution.entry = SimpleNamespace(
        block_id=proposal.occurrence_id,
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        content_sha256=proposal.before_hash,
    )
    current_policy: dict[str, Any] = {}
    original_access_policy = production_inputs._access_policy

    async def capture_access_policy(snapshot: Any, extraction: Any) -> tuple[Any, int]:
        result = await original_access_policy(snapshot, extraction)
        current_policy["value"] = result[0]
        return result

    async def validate_input_fence(**_kwargs: Any) -> Any:
        assert _kwargs["snapshot"].subject_tlp is TLP.CLEAR
        assert current_policy["value"].subject_tlp is TLP.GREEN
        return SimpleNamespace(access_policy=current_policy["value"])

    def parse_enrichment(_payload: Any) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(production_inputs, "_access_policy", capture_access_policy)
    monkeypatch.setattr(
        production_inputs._enrichment_revision_service,
        "_validate_input_fence",
        validate_input_fence,
    )
    monkeypatch.setattr(adoption_inputs_module, "editorial_enrichment_from_json", parse_enrichment)

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_patch_invalid"):
        await production_inputs.enrichment_inputs(
            proposal,
            {"target": enrichment_target.to_json()},
            base_artifact=enrichment_artifact,
            base_payload={"schema_version": 1},
            base_bytes=b"synthetic enrichment",
            resolution=resolution,
        )

    assert factory.open_uows == 0
    assert gateway.calls == 0
    assert blob_store.calls == 0
    assert current_policy["value"].effective_tlp is TLP.AMBER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"revoked": True}, "proposal_evidence_revoked"),
        ({"tlp": TLP.RED}, "external_llm_tlp_blocked"),
        ({"external_llm_allowed": False}, "external_llm_blocked"),
    ],
)
async def test_inputs_adapter_enforces_tlp_revocation_and_external_llm_allowed_in_uow(
    kwargs: dict[str, Any], expected_code: str
) -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture(**kwargs)

    with pytest.raises(EditorialProposalAdoptionError, match=expected_code):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.asyncio
async def test_inputs_adapter_never_touches_gateway_or_blobstore() -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()

    await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert uow.write_count == 0


@pytest.mark.parametrize("caption", ("ftp://example.test/a", "mailto:user@example.test"))
def test_manual_source_figure_candidate_rejects_url_schemes_in_caption(caption: str) -> None:
    raw_figure = {
        "figure_id": "opaque-figure-id",
        "caption": caption,
        "order": 0,
        "source_document_id": str(uuid4()),
        "source_url": "https://example.test/article",
        "sha256": "a" * 64,
        "mime_type": "image/png",
        "byte_size": 10,
        "width": 10,
        "height": 10,
        "provenance": "Synthetic archived source.",
    }

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_patch_invalid"):
        adoption_inputs_module._manual_source_figure_candidate_patch(
            {"manual_source_figures": [raw_figure]},
            subject_id=uuid4(),
            run_id=uuid4(),
            catalog=(),
            extraction=SimpleNamespace(sources=()),
            base_enrichment=SimpleNamespace(source_figures=()),
            anchor_block_id=uuid4(),
            publication_kind=PublicationKind.ARTICLE,
            inventory_limit=1,
        )


def test_enrichment_target_parser_accepts_element_and_order_targets() -> None:
    element_target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.CHART,
        element_key="chart_synthetic",
    )
    assert adoption_inputs_module._enrichment_target(element_target.to_json()) == element_target

    anchor_block_id = uuid4()
    order_target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
        placement=TargetedEditorialRevisionPlacement(
            kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor_block_id,
        ),
    )

    parsed = adoption_inputs_module._enrichment_target(order_target.to_json())

    assert parsed == order_target
    assert parsed.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER
    assert parsed.element_kind is None
    assert parsed.element_key is None
    assert parsed.placement is not None
    assert parsed.placement.anchor_block_id == anchor_block_id


def _visual_adoption_probe() -> tuple[
    ProductionEditorialProposalAdoptionInputs,
    SimpleNamespace,
    SimpleNamespace,
    TargetedEditorialRevisionTarget,
]:
    subject_id, artifact_id = uuid4(), uuid4()
    base_artifact = SimpleNamespace(id=artifact_id, version=1, input_hash="1" * 64)
    proposal = SimpleNamespace(
        subject_id=subject_id,
        base_artifact_id=artifact_id,
        base_artifact_version=1,
        base_input_hash="1" * 64,
        base_canonical_sha256="2" * 64,
        actor_id=uuid4(),
        before_hash="3" * 64,
    )
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
    )
    inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=cast(Any, None),
        artifact_store=cast(Any, None),
        workspace_service=cast(Any, None),
        synthesis_loader=cast(Any, None),
        enrichment_loader=cast(Any, None),
        enrichment_revision_service=cast(Any, None),
    )
    return inputs, proposal, base_artifact, target


async def _call_visual_adoption(
    inputs: ProductionEditorialProposalAdoptionInputs,
    *,
    proposal: SimpleNamespace,
    base_artifact: SimpleNamespace,
    target: TargetedEditorialRevisionTarget,
    patch: dict[str, object],
) -> Any:
    return await inputs._visual_revision_inputs(
        proposal=proposal,
        patch=patch,
        resolution=SimpleNamespace(entry=None),
        run=cast(Any, None),
        snapshot=cast(Any, None),
        extraction=cast(Any, None),
        synthesis=cast(Any, None),
        access_policy=cast(Any, None),
        fence=cast(Any, None),
        base_artifact=base_artifact,
        base_payload={},
        base_enrichment=cast(Any, None),
        target=target,
        operation=TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION,
        projection=cast(Any, None),
    )


@pytest.mark.asyncio
async def test_visual_adoption_refuses_a_patch_for_another_element() -> None:
    """Adoption keeps refusing a prepare patch whose key is another element."""
    inputs, proposal, base_artifact, target = _visual_adoption_probe()

    with pytest.raises(EditorialProposalAdoptionError) as caught:
        await _call_visual_adoption(
            inputs,
            proposal=proposal,
            base_artifact=base_artifact,
            target=target,
            patch={
                "visual_patch": {
                    "kind": "update_visual_caption",
                    "element_kind": "table",
                    "element_key": "table_two",
                    "caption": "Synthetic caption.",
                }
            },
        )

    assert caught.value.code == "proposal_target_mismatch"


@pytest.mark.asyncio
async def test_visual_adoption_maps_an_unknown_patch_kind_to_patch_invalid() -> None:
    """An unknown wire kind is a corrupted patch, not a 'later slice'."""
    inputs, proposal, base_artifact, target = _visual_adoption_probe()

    with pytest.raises(EditorialProposalAdoptionError) as caught:
        await _call_visual_adoption(
            inputs,
            proposal=proposal,
            base_artifact=base_artifact,
            target=target,
            patch={"visual_patch": {"kind": "future_visual_patch"}},
        )

    assert caught.value.code == "proposal_patch_invalid"


@pytest.mark.parametrize(
    "raw",
    (
        {"kind": "enrichment_order"},
        {
            "kind": "enrichment_order",
            "element_kind": "table",
            "placement": {
                "kind": "after_paragraph",
                "section_id": None,
                "anchor_block_id": str(uuid4()),
            },
        },
        {
            "kind": "enrichment_order",
            "placement": {"kind": "after_lead", "section_id": None, "anchor_block_id": None},
        },
    ),
)
def test_enrichment_target_parser_rejects_invalid_order_targets(raw: dict[str, object]) -> None:
    with pytest.raises(EditorialProposalAdoptionError, match="proposal_target_mismatch"):
        adoption_inputs_module._enrichment_target(raw)


@pytest.mark.asyncio
async def test_policy_programming_error_is_not_converted_to_a_business_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()

    async def fail_with_attribute_error(*_args: Any, **_kwargs: Any) -> Any:
        raise AttributeError("injected policy programming error")

    monkeypatch.setattr(
        adoption_inputs_module, "build_synthesis_access_policy", fail_with_attribute_error
    )

    with pytest.raises(AttributeError, match="injected policy programming error"):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)


@pytest.mark.asyncio
async def test_figure_inventory_programming_error_is_not_converted_to_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, proposal, resolution, uow, _context = _figure_revalidation_fixture()

    async def fail_with_attribute_error(**_kwargs: Any) -> Any:
        raise AttributeError("injected inventory programming error")

    monkeypatch.setattr(
        adoption_inputs_module,
        "load_archived_source_figure_inventory",
        fail_with_attribute_error,
    )

    with pytest.raises(AttributeError, match="injected inventory programming error"):
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)


@pytest.mark.asyncio
async def test_inputs_adapter_accepts_v8_envelope_route_hash_without_persisted_decision() -> None:
    inputs, proposal, resolution, uow, context = _revalidation_fixture()
    base_bytes = b"synthetic canonical synthesis"
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    proposal.base_canonical_sha256 = base_sha256
    resolution.record.synthesis_canonical_sha256 = base_sha256
    resolution.record.routing_decision_id = None
    resolution.record.routing_decision_hash = "d" * 64
    proposal.base_routing_hash = "d" * 64
    resolution.record.publication_kind = PublicationKind.BRIEF
    resolution.artifact_id = proposal.base_artifact_id
    resolution.artifact_version = proposal.base_artifact_version
    resolution.canonical_sha256 = base_sha256

    async def get_artifact(_artifact_id: Any) -> Any:
        return context.base_artifact

    uow.production_artifacts.get = get_artifact

    class _UowContext:
        async def __aenter__(self) -> _Uow:
            return cast(_Uow, uow)

        async def __aexit__(self, *_args: object) -> None:
            return None

    inputs._uow_factory = lambda: _UowContext()

    await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    loaded = await inputs._load_base_context(
        proposal,
        base_artifact=context.base_artifact,
        base_bytes=base_bytes,
        resolution=resolution,
        stage=ProductionArtifactStage.SYNTHESIS,
    )

    assert loaded[0] is context.run
    assert loaded[1] is context.snapshot


@pytest.mark.asyncio
async def test_inputs_adapter_rejects_missing_adopted_routing_row() -> None:
    inputs, proposal, resolution, uow, _context = _revalidation_fixture()
    uow.production_routing_decisions = _Repository(None)
    uow.production_runs.value.routing_decision_id = uuid4()

    with pytest.raises(EditorialProposalAdoptionError) as error:
        await inputs.revalidate_evidence_policy_and_thread(uow, proposal, {}, resolution=resolution)

    assert error.value.code == "proposal_base_stale"
    assert error.value.reason == "routing:decision_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_kind", "operation"),
    [
        ("title", TargetedEditorialRevisionOperation.REWRITE_TITLE),
        ("lead_paragraph", TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH),
    ],
)
async def test_synthesis_adoption_uses_canonical_target_path_for_title_and_paragraph_blocks(
    monkeypatch: pytest.MonkeyPatch,
    block_kind: str,
    operation: TargetedEditorialRevisionOperation,
) -> None:
    inputs, proposal, resolution, _uow, context = _revalidation_fixture()
    payload = {
        "title": "[Synthetic actor] Synthetic report",
        "lead": [{"text": "Synthetic lead claim.", "evidence_refs": []}],
        "sections": [],
    }
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="a" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=payload,
    )
    entry = next(item for item in entries if item.kind.value == block_kind)
    resolution.entry = entry
    resolution.record.publication_kind = PublicationKind.BRIEF
    resolution.record.block_registry = entries
    proposal.occurrence_id = entry.block_id
    proposal.operation = operation.value
    proposal.origin = EditorialProposalOrigin.MANUAL_PATCH
    proposal.base_canonical_sha256 = "b" * 64
    proposal.before_hash = resolve_workspace_target(
        entry=entry,
        canonical_content=payload,
        operation=operation.value,
    ).before_hash
    workspace_target = resolve_workspace_target(
        entry=entry,
        canonical_content=payload,
        operation=operation.value,
    )
    proposal.target_id = workspace_target.legacy_target_id
    base_artifact = context.base_artifact
    base_artifact.metadata = {}
    base_artifact.input_hash = proposal.base_input_hash
    context.synthesis_artifact = base_artifact
    evidence_pack = SimpleNamespace(
        _handle_to_ref={}, narrative_evidence=(), technical_evidence=(), reserve_evidence=()
    )
    synthesis = SimpleNamespace()

    async def load_base_context(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return (
            context.run,
            context.snapshot,
            context.extraction_artifact,
            None,
            base_artifact,
        )

    async def load_extraction(*_args: Any, **_kwargs: Any) -> tuple[Any, str]:
        return context.extraction, "c" * 64

    async def access_policy(*_args: Any, **_kwargs: Any) -> tuple[Any, int]:
        return SimpleNamespace(effective_tlp=TLP.CLEAR, external_llm_allowed=True), 1

    async def routing(*_args: Any, **_kwargs: Any) -> None:
        return None

    captured: list[dict[str, Any]] = []

    def capture_candidate(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return SimpleNamespace(payload=payload)

    monkeypatch.setattr(inputs, "_load_base_context", load_base_context)
    monkeypatch.setattr(inputs, "_access_policy", access_policy)
    monkeypatch.setattr(inputs, "_routing", routing)
    monkeypatch.setattr(inputs, "_synthesis_lineage", lambda *_args, **_kwargs: ())
    inputs._synthesis_loader = SimpleNamespace(_load_extraction=load_extraction)
    monkeypatch.setattr(
        adoption_inputs_module,
        "build_synthesis_evidence_pack",
        lambda *_args, **_kwargs: evidence_pack,
    )
    monkeypatch.setattr(
        adoption_inputs_module,
        "production_synthesis_from_json",
        lambda _payload: synthesis,
    )
    monkeypatch.setattr(adoption_inputs_module, "build_synthesis_candidate", capture_candidate)

    patch: dict[str, object] = {
        "target": workspace_target.target.to_json(),
        "after": "[Synthetic actor] Revised report",
        "rationale": "Synthetic manual edit.",
        "evidence_refs": [],
    }
    if operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
        patch["manual_text_without_evidence"] = True

    await inputs.synthesis_inputs(
        proposal,
        patch,
        base_artifact=base_artifact,
        base_payload=payload,
        base_bytes=b"synthetic canonical synthesis",
        resolution=resolution,
    )

    assert len(captured) == 1
    canonical_target = resolve_target(payload, workspace_target.target, operation)
    wire_patch = captured[0]["patch"]
    assert wire_patch.target == canonical_target.path
    assert wire_patch.action is operation
