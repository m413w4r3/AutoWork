"""PostgreSQL oracles for figure replacement and element removal (F2-03c).

Replacement must re-resolve the selected figure against a fresh archive read
and rewrite exactly one element in place; removal must drop one element while
keeping every archived blob, the prior artifact version and the registry
lineage. Both produce one new enrichment artifact, one pending impact and no
model run.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionResult,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.production_repair_factory import (
    create_editorial_proposal_impact_service,
)
from cti_app.domain.editorial_enrichment_patch import (
    RemoveEnrichmentElementPatch,
    ReplaceSourceFigurePatch,
)
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.editorial_workspace import EditorialTransition
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    editorial_enrichment_from_json,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _adopt,
    _configure_app,
    _enrichment_block,
    _insert_source_figure,
    _lead_anchor_placement,
    _paragraph_block,
    _read_enrichment,
    _visual_revision_services,
)
from tests.integration.editorial.test_table_insertion_postgres import (
    _ready_for_manual_table_route,
)

pytestmark = pytest.mark.integration

_FIGURE_URL_PREFIX = "https://media.example.test/"


async def _archive_rows(
    uow_factory: Any, subject_id: UUID
) -> tuple[tuple[str, str, str, str | None, str | None], ...]:
    """Archive facts a replacement/removal may never mutate."""
    async with uow_factory() as uow:
        documents = await uow.source_documents.list_for_subject(subject_id)
        rows: list[tuple[str, str, str, str | None, str | None]] = []
        for document in sorted(documents, key=lambda item: item.id.hex):
            if not (document.detected_mime_type or "").startswith("image/"):
                continue
            blob_id = document.decoded_blob_id or document.blob_id
            blob = await uow.blobs.get(blob_id)
            rows.append(
                (
                    str(document.id),
                    str(blob_id),
                    blob.descriptor.sha256 if blob is not None else "missing",
                    document.final_url,
                    document.detected_mime_type,
                )
            )
        return tuple(rows)


async def _documents(uow_factory: Any, subject_id: UUID) -> list[Any]:
    async with uow_factory() as uow:
        return list(await uow.source_documents.list_for_subject(subject_id))


def _swap_image_blobs(documents: list[Any]) -> dict[UUID, Any]:
    """Point the archived image documents at each other's bytes.

    The fresh inventory then resolves a different archived descriptor for each
    source figure, so the previously selected catalogue handle no longer
    resolves and any replacement must be refused.
    """
    images = [
        document
        for document in documents
        if str(document.detected_mime_type or "").startswith("image/")
    ]
    assert len(images) >= 2
    first, second = images[0], images[1]
    # The repository persists the decoded blob pointer; leave the encoded blob
    # untouched so the effective archived descriptor really changes identity.
    return {
        first.id: replace(first, decoded_blob_id=second.blob_id),
        second.id: replace(second, decoded_blob_id=first.blob_id),
    }


def _selectable_replacement(catalog: dict[str, Any]) -> dict[str, Any]:
    candidates = [item for item in catalog["items"] if item.get("selectable")]
    assert candidates
    return cast(dict[str, Any], candidates[0])


async def _inserted_figure_workspace(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    publication_kind: PublicationKind,
    source_figure_count: int = 2,
) -> tuple[Any, Any, Any, Any, Any, dict[str, Any]]:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory,
        base_table_placement_factory=_lead_anchor_placement,
        source_figure_count=source_figure_count,
        publication_kind=publication_kind,
    )
    workspace_service, proposal_service, adoption_service = await _visual_revision_services(
        app, uow_factory, seeded.subject.id
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        anchor = _paragraph_block(snapshot, position=1)
        await _insert_source_figure(
            client,
            subject_id=seeded.subject.id,
            anchor_id=UUID(anchor["block_id"]),
            snapshot=snapshot,
        )
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        figure = _enrichment_block(snapshot, kind="figure")
        catalog = (await client.get(f"/api/subjects/{seeded.subject.id}/editor/figures")).json()
    return (
        seeded,
        app,
        model_gateway,
        proposal_service,
        adoption_service,
        {
            "snapshot": snapshot,
            "figure": figure,
            "catalog": catalog,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected"),
    ((PublicationKind.BRIEF, "brief"), (PublicationKind.ARTICLE, "article")),
)
async def test_replace_source_figure_is_in_place_and_archive_safe(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected: str,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        proposal_service,
        adoption_service,
        view,
    ) = await _inserted_figure_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    snapshot = view["snapshot"]
    figure_block = view["figure"]
    replacement = _selectable_replacement(view["catalog"])
    assert snapshot["publication_kind"] == expected

    base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    base_enrichment = editorial_enrichment_from_json(base_payload)
    base_figure = next(
        item for item in base_enrichment.source_figures if item.key == figure_block["element_key"]
    )
    assert base_figure.resolved_figure is not None
    old_figure_id = base_figure.resolved_figure.figure_id
    old_blob_id = base_figure.resolved_figure.blob_id
    base_bytes = await app.state.production_artifact_store.read_bytes(
        base_artifact.canonical_blob_id
    )
    archive_before = await _archive_rows(uow_factory, seeded.subject.id)

    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-replace-{uuid4().hex}",
        patch=ReplaceSourceFigurePatch(
            element_key=figure_block["element_key"],
            figure_id=UUID(replacement["figure_id"]),
        ),
    )
    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )

    assert result.artifact.version == base_artifact.version + 1
    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert result.proposal.model_run_id is None
    assert result.impact.state.value == "pending"
    assert model_gateway.calls == []

    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(payload)
    assert len(revised.source_figures) == 1
    revised_figure = revised.source_figures[0]
    assert revised_figure.key == base_figure.key
    assert revised_figure.resolved_figure is not None
    assert revised_figure.resolved_figure.figure_id != old_figure_id
    assert revised_figure.resolved_figure.blob_id != old_blob_id
    assert revised_figure.caption == base_figure.caption
    assert revised_figure.placement == base_figure.placement
    assert revised_figure.order == base_figure.order
    assert revised_figure.group_order == base_figure.group_order
    assert revised.schema_version == base_enrichment.schema_version
    assert revised.figure_decisions == ()

    # The previous artifact version still references the old archived image.
    assert (
        await app.state.production_artifact_store.read_bytes(base_artifact.canonical_blob_id)
        == base_bytes
    )
    old_payload = editorial_enrichment_from_json(cast(dict[str, Any], json.loads(base_bytes)))
    assert old_payload.source_figures[0].resolved_figure is not None
    assert old_payload.source_figures[0].resolved_figure.figure_id == old_figure_id

    assert await _archive_rows(uow_factory, seeded.subject.id) == archive_before
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        snapshots = await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id)
    versions = [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert versions == [1, 2, 3]
    figure_block_id = UUID(figure_block["block_id"])
    replaced_entries = [
        entry for entry in result.snapshot.block_registry if entry.block_id == figure_block_id
    ]
    assert len(replaced_entries) == 1
    assert replaced_entries[0].transition is EditorialTransition.REWRITE
    assert result.snapshot.parent_snapshot_id is not None
    assert snapshots


@pytest.mark.asyncio
async def test_replace_rejects_a_figure_changed_between_prepare_and_accept(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        _model_gateway,
        proposal_service,
        adoption_service,
        view,
    ) = await _inserted_figure_workspace(
        uow_factory, monkeypatch, publication_kind=PublicationKind.ARTICLE
    )
    snapshot = view["snapshot"]
    figure_block = view["figure"]
    replacement = _selectable_replacement(view["catalog"])
    base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    base_bytes = await app.state.production_artifact_store.read_bytes(
        base_artifact.canonical_blob_id
    )
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-replace-revoked-{uuid4().hex}",
        patch=ReplaceSourceFigurePatch(
            element_key=figure_block["element_key"],
            figure_id=UUID(replacement["figure_id"]),
        ),
    )

    # Revoke the archive between prepare and accept: the image source
    # documents now point at each other's bytes, so every observed figure
    # digest no longer matches its archived blob.
    swapped = _swap_image_blobs(await _documents(uow_factory, seeded.subject.id))
    async with uow_factory() as uow:
        for document in await uow.source_documents.list_for_subject(seeded.subject.id):
            updated = swapped.get(document.id)
            if updated is not None:
                await uow.source_documents.save(updated)
        await uow.commit()

    with pytest.raises(EditorialProposalAdoptionError) as refused:
        await _adopt(
            adoption_service,
            proposal,
            actor_id=seeded.actor_id,
            key=f"adopt-{uuid4().hex}",
        )
    assert refused.value.code in {
        "proposal_base_stale",
        "proposal_base_unavailable",
        "editorial_enrichment_stale_base",
        "editorial_figure_not_found",
        "editorial_figure_not_eligible",
        "figure_catalog_unavailable",
    }

    artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    assert artifact.version == base_artifact.version
    assert payload == base_payload
    assert (
        await app.state.production_artifact_store.read_bytes(base_artifact.canonical_blob_id)
        == base_bytes
    )
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, proposal.occurrence_id, proposal.id
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    assert stored is not None and stored.state is not EditorialProposalState.ACCEPTED
    assert impact is None
    assert [
        item.version
        for item in artifacts
        if item.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2]


@pytest.mark.asyncio
async def test_under_lock_revalidation_refuses_a_figure_revoked_after_planning(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate is planned before the run guard; revocation must still refuse."""
    (
        seeded,
        app,
        _model_gateway,
        proposal_service,
        adoption_service,
        view,
    ) = await _inserted_figure_workspace(
        uow_factory, monkeypatch, publication_kind=PublicationKind.ARTICLE
    )
    snapshot = view["snapshot"]
    figure_block = view["figure"]
    replacement = _selectable_replacement(view["catalog"])
    base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-replace-lock-{uuid4().hex}",
        patch=ReplaceSourceFigurePatch(
            element_key=figure_block["element_key"],
            figure_id=UUID(replacement["figure_id"]),
        ),
    )
    documents_before = await _documents(uow_factory, seeded.subject.id)
    swapped = _swap_image_blobs(documents_before)

    original = ProductionEditorialProposalAdoptionInputs.revalidate_evidence_policy_and_thread

    async def revoke_then_revalidate(
        self: Any,
        uow: Any,
        proposal: Any,
        patch: Any,
        *,
        resolution: Any,
    ) -> None:
        for document in await uow.source_documents.list_for_subject(seeded.subject.id):
            updated = swapped.get(document.id)
            if updated is not None:
                await uow.source_documents.save(updated)
        await original(self, uow, proposal, patch, resolution=resolution)

    monkeypatch.setattr(
        ProductionEditorialProposalAdoptionInputs,
        "revalidate_evidence_policy_and_thread",
        revoke_then_revalidate,
    )
    with pytest.raises(EditorialProposalAdoptionError) as refused:
        await _adopt(
            adoption_service,
            proposal,
            actor_id=seeded.actor_id,
            key=f"adopt-{uuid4().hex}",
        )
    assert refused.value.code in {
        "editorial_figure_not_found",
        "editorial_figure_not_eligible",
        "figure_catalog_unavailable",
    }
    artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    assert artifact.version == base_artifact.version
    assert payload == base_payload
    async with uow_factory() as uow:
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
    # The refusal rolled the test mutation back with the acceptance UOW.
    documents_after = {
        document.id: document for document in await _documents(uow_factory, seeded.subject.id)
    }
    for document in documents_before:
        stored = documents_after[document.id]
        assert stored.blob_id == document.blob_id
        assert stored.decoded_blob_id == document.decoded_blob_id
    assert impact is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected"),
    ((PublicationKind.BRIEF, "brief"), (PublicationKind.ARTICLE, "article")),
)
async def test_remove_enrichment_element_invalidates_render_and_keeps_archive(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected: str,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        proposal_service,
        adoption_service,
        view,
    ) = await _inserted_figure_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    snapshot = view["snapshot"]
    figure_block = view["figure"]
    assert snapshot["publication_kind"] == expected
    base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    base_bytes = await app.state.production_artifact_store.read_bytes(
        base_artifact.canonical_blob_id
    )
    archive_before = await _archive_rows(uow_factory, seeded.subject.id)

    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-remove-{uuid4().hex}",
        patch=RemoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.FIGURE,
            element_key=figure_block["element_key"],
        ),
    )
    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )

    assert result.artifact.version == base_artifact.version + 1
    assert result.proposal.model_run_id is None
    assert model_gateway.calls == []
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(payload)
    assert revised.source_figures == ()
    assert revised.figure_decisions == ()
    assert revised.tables[0].key == "synthetic_table"

    # Archived media and the prior artifact version are untouched.
    assert await _archive_rows(uow_factory, seeded.subject.id) == archive_before
    assert (
        await app.state.production_artifact_store.read_bytes(base_artifact.canonical_blob_id)
        == base_bytes
    )
    assert len(editorial_enrichment_from_json(base_payload).source_figures) == 1

    # Registry lineage records the delete; the old occurrence is retired.
    figure_block_id = UUID(figure_block["block_id"])
    assert figure_block_id in result.snapshot.retired_block_ids
    assert all(entry.block_id != figure_block_id for entry in result.snapshot.block_registry)

    # The impact outbox marks the derived publication render stale.
    assert result.impact.state.value == "pending"
    impact_service = create_editorial_proposal_impact_service(
        uow_factory,
        artifact_store=app.state.production_artifact_store,
        model_gateway=cast(Any, object()),  # No worker path may call it.
    )
    processed = await impact_service.process(result.impact.impact_id)
    assert processed is not None and processed.state.value == "done"
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    publication = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.PUBLICATION
    )
    assert publication.status is ProductionArtifactStatus.STALE


@pytest.mark.asyncio
async def test_remove_vs_concurrent_replace_has_exactly_one_winner(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        _app,
        _model_gateway,
        proposal_service,
        adoption_service,
        view,
    ) = await _inserted_figure_workspace(
        uow_factory, monkeypatch, publication_kind=PublicationKind.ARTICLE
    )
    snapshot = view["snapshot"]
    figure_block = view["figure"]
    replacement = _selectable_replacement(view["catalog"])
    remove = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-remove-race-{uuid4().hex}",
        patch=RemoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.FIGURE,
            element_key=figure_block["element_key"],
        ),
    )
    replace_proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(figure_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-replace-race-{uuid4().hex}",
        patch=ReplaceSourceFigurePatch(
            element_key=figure_block["element_key"],
            figure_id=UUID(replacement["figure_id"]),
        ),
    )

    results = await asyncio.gather(
        _adopt(adoption_service, remove, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"),
        _adopt(
            adoption_service,
            replace_proposal,
            actor_id=seeded.actor_id,
            key=f"adopt-{uuid4().hex}",
        ),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, EditorialProposalAdoptionResult)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    loss = failures[0]
    assert isinstance(loss, EditorialProposalAdoptionError)
    assert loss.code in {
        "proposal_stale",
        "proposal_base_stale",
        "proposal_state_conflict",
    }
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        proposals = [
            await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id, proposal.occurrence_id, proposal.id
            )
            for proposal in (remove, replace_proposal)
        ]
    versions = [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert versions == [1, 2, 3]
    assert (
        sum(
            1
            for proposal in proposals
            if proposal is not None and proposal.state is EditorialProposalState.ACCEPTED
        )
        == 1
    )
    assert all(
        proposal is None or proposal.state is not EditorialProposalState.PROPOSED
        for proposal in proposals
    )
