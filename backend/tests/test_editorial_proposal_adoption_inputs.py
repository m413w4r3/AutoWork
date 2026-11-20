from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposalOrigin
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus


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
        self, *, document: Any, collection: Any, run: Any, snapshot: Any, artifacts: Any
    ) -> None:
        self.source_documents = _Repository(document)
        self.source_collections = _Repository(collection)
        self.editorial_threads = _Repository(None)
        self.production_runs = _Repository(run)
        self.production_input_snapshots = _Repository(snapshot)
        self.production_artifacts = _Repository(artifacts)
        self.write_count = 0


def _revalidation_fixture(
    *, tlp: TLP = TLP.AMBER, external_llm_allowed: bool = True, revoked: bool = False
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
        run=run,
        snapshot=snapshot,
        artifacts={
            ProductionArtifactStage.SYNTHESIS.value: base_artifact,
            ProductionArtifactStage.EXTRACTION.value: extraction_artifact,
            ProductionArtifactStage.RELEVANCE_PROJECTION.value: None,
        },
    )
    return inputs, proposal, resolution, uow, context


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
