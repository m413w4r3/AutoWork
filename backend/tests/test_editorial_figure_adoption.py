from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    EditorialFigureCatalogItem,
    EditorialFigureCatalogPage,
    FigureCatalogSource,
    archive_reference_for_catalog_item,
    assert_archive_reference_matches,
    validate_figure_insertion_limit,
)
from cti_app.domain.editorial_proposal import ManualSourceFigureSelection
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.publication_kind import PublicationKind


def _catalog_item() -> EditorialFigureCatalogItem:
    content = b"synthetic archived image"
    content_sha256 = hashlib.sha256(content).hexdigest()
    source_document_id = uuid4()
    source = "https://sources.example.test/article"
    locator = SourceFigureLocatorV1(page=2, figure_label="Figure 2")
    resolved = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source_document_id,
            sha256=content_sha256,
            source=source,
            locator=locator,
        ),
        blob_id=uuid4(),
        sha256=content_sha256,
        mime_type="image/png",
        byte_size=len(content),
        source_document_id=source_document_id,
        source=source,
        provenance="Archived source document, page 2",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="archived_blob_verified",
    )
    return EditorialFigureCatalogItem(
        figure_id=str(uuid4()),
        original_name="Synthetic source figure",
        original_caption="Archived caption",
        alt_text=None,
        source=FigureCatalogSource(str(source_document_id), "Synthetic source", source),
        page=2,
        section="Analysis",
        context_before=None,
        context_after=None,
        width=320,
        height=180,
        mime_type="image/png",
        status="available",
        selectable=True,
        exclusion_reason=None,
        already_used=False,
        _source_figure_id=resolved.figure_id,
        _source_document_id=source_document_id,
        _blob_id=resolved.blob_id,
        _sha256=content_sha256,
        _byte_size=len(content),
        _run_id=uuid4(),
        _resolved_figure=resolved,
    )


def test_multi_figure_patch_rechecks_archive_sha_mime_size_dimensions() -> None:
    item = _catalog_item()
    reference = archive_reference_for_catalog_item(item)

    assert_archive_reference_matches(reference, item)
    assert not assert_archive_reference_matches(replace(reference, sha256="f" * 64), item)
    assert not assert_archive_reference_matches(replace(reference, mime_type="image/jpeg"), item)
    assert not assert_archive_reference_matches(
        replace(reference, byte_size=reference.byte_size + 1), item
    )
    assert not assert_archive_reference_matches(replace(reference, width=640), item)
    assert not assert_archive_reference_matches(replace(reference, height=360), item)


def test_brief_blocks_more_than_three_integrated_figures() -> None:
    with pytest.raises(EditorialFigureCatalogError, match="brief_figure_limit_exceeded"):
        validate_figure_insertion_limit(
            publication_kind=PublicationKind.BRIEF,
            integrated_count=2,
            requested_count=2,
            inventory_limit=12,
        )


def test_article_respects_the_archived_inventory_figure_limit() -> None:
    with pytest.raises(
        EditorialFigureCatalogError,
        match="source_figure_inventory_limit_exceeded",
    ):
        validate_figure_insertion_limit(
            publication_kind=PublicationKind.ARTICLE,
            integrated_count=1,
            requested_count=2,
            inventory_limit=2,
        )


def test_snapshot_figure_capability_requires_inventory_and_remaining_capacity() -> None:
    from cti_app.application.editorial_workspace.service import _figure_insertion_available

    item = _catalog_item()
    page = EditorialFigureCatalogPage(
        items=(item,),
        sources=(item.source,),
        total=1,
        available_count=1,
        offset=0,
        limit=1,
    )

    assert _figure_insertion_available(
        page,
        publication_kind=PublicationKind.ARTICLE,
        production_run_id=item._run_id,
        integrated_count=0,
    )
    assert not _figure_insertion_available(
        replace(page, available_count=0),
        publication_kind=PublicationKind.ARTICLE,
        production_run_id=item._run_id,
        integrated_count=0,
    )
    assert not _figure_insertion_available(
        page,
        publication_kind=PublicationKind.BRIEF,
        production_run_id=item._run_id,
        integrated_count=3,
    )
    assert not _figure_insertion_available(
        page,
        publication_kind=PublicationKind.ARTICLE,
        production_run_id=item._run_id,
        integrated_count=item._inventory_max_figures,
    )
    assert not _figure_insertion_available(
        page,
        publication_kind=PublicationKind.ARTICLE,
        production_run_id=uuid4(),
        integrated_count=0,
    )


def test_ineligible_or_pending_figure_refused() -> None:
    item = _catalog_item()
    pending = replace(item, status="unverified", selectable=False)

    with pytest.raises(EditorialFigureCatalogError, match="editorial_figure_not_eligible"):
        archive_reference_for_catalog_item(pending)


def test_manual_figure_selection_contains_only_catalog_id_caption_and_order() -> None:
    selection = ManualSourceFigureSelection(
        figure_id=UUID("10000000-0000-4000-8000-000000000001"),
        caption="Synthetic editorial caption",
        order=0,
    )

    assert selection.figure_id == UUID("10000000-0000-4000-8000-000000000001")
    assert selection.caption == "Synthetic editorial caption"
    assert selection.order == 0
    assert not hasattr(selection, "blob_id")


@pytest.mark.asyncio
async def test_figure_factories_build_archive_dependencies_without_lifespan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from cti_app.api import editorial_workspace as editorial_workspace_api
    from cti_app.api import main as api_main
    from cti_app.application.editorial_figure_catalog import EditorialFigureCatalogService
    from cti_app.application.production_artifact_store import ProductionArtifactStore
    from cti_app.application.source_media_collection import (
        SourceMediaArchiveService,
        SourceMediaPolicy,
    )
    from cti_app.config import Settings
    from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
    from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

    settings = Settings(
        _env_file=None,
        app_env="development",
        api_bind_host="127.0.0.1",
        production_editorial_resource_search_enabled=False,
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: settings)

    # Construct the real SQLAlchemy UOW factory without opening a connection.
    engine = create_async_engine(
        "postgresql+asyncpg://figure-test:figure-test@127.0.0.1:1/autowork"
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        app = api_main.create_app()
        blob_store = FilesystemBlobStore(tmp_path / "figure-blobs")
        app.state.uow_factory = uow_factory
        app.state.blob_store = blob_store
        app.state.model_gateway = object()
        app.state.publication_render_service = object()
        assert getattr(app.state, "collection_service", None) is None
        assert getattr(app.state, "production_artifact_store", None) is None

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "app": app,
                "method": "POST",
                "path": "/api/subjects/test/editor/blocks/test/figure-proposals",
                "headers": [],
            }
        )
        manual_service = editorial_workspace_api._manual_proposal_service(request)
        adoption_service = editorial_workspace_api._proposal_adoption_service(request)
        adoption_enrichment_loader = adoption_service._inputs._enrichment_loader

        assert isinstance(app.state.production_artifact_store, ProductionArtifactStore)
        assert isinstance(manual_service._figure_catalog_service, EditorialFigureCatalogService)
        assert manual_service._figure_catalog_service._media_policy_sha256 == (
            SourceMediaPolicy().sha256
        )
        source_media_archiver = app.state.collection_service.source_media_archiver
        assert isinstance(source_media_archiver, SourceMediaArchiveService)
        assert adoption_enrichment_loader._source_media_archiver is source_media_archiver
    finally:
        await engine.dispose()


def test_figure_document_order_uses_figure_order_not_registry_position() -> None:
    from cti_app.application.editorial_workspace.registry import (
        WorkspaceSnapshotRecord,
        project_blocks,
        registry_hash,
    )
    from cti_app.domain.editorial_workspace import (
        EditorialSourceStage,
        bootstrap_block_registry,
        canonical_content_sha256,
    )
    from cti_app.domain.publication_kind import PublicationKind

    subject_id, run_id, synthesis_artifact_id, enrichment_artifact_id = (uuid4() for _ in range(4))
    synthesis = {
        "title": "Synthetic title",
        "lead": [{"block_id": str(uuid4()), "text": "Synthetic anchor paragraph."}],
        "sections": [],
    }
    synthesis_sha256 = canonical_content_sha256(synthesis)
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=synthesis,
    )
    anchor_id = next(
        entry.block_id for entry in synthesis_entries if entry.kind.value == "lead_paragraph"
    )
    enrichment = {
        "source_figures": [
            {
                "key": "figure-second",
                "caption": "Second in document order",
                "provenance": "Synthetic archived source",
                "source_url": "https://sources.example.test/article",
                "inclusion_status": "included",
                "order": 1,
                "width": 320,
                "height": 180,
                "resolved_figure": {
                    "source": "https://sources.example.test/article",
                    "sha256": "b" * 64,
                    "mime_type": "image/png",
                    "byte_size": 20,
                },
                "placement": {
                    "kind": "after_paragraph",
                    "anchor_block_id": str(anchor_id),
                },
                "locator": {"page": 1},
            },
            {
                "key": "figure-first",
                "caption": "First in document order",
                "provenance": "Synthetic archived source",
                "source_url": "https://sources.example.test/article",
                "inclusion_status": "included",
                "order": 0,
                "width": 320,
                "height": 180,
                "resolved_figure": {
                    "source": "https://sources.example.test/article",
                    "sha256": "a" * 64,
                    "mime_type": "image/png",
                    "byte_size": 10,
                },
                "placement": {
                    "kind": "after_paragraph",
                    "anchor_block_id": str(anchor_id),
                },
                "locator": {"page": 1},
            },
        ]
    }
    enrichment_sha256 = canonical_content_sha256(enrichment)
    entries = (
        *synthesis_entries,
        *bootstrap_block_registry(
            artifact_id=enrichment_artifact_id,
            canonical_sha256=enrichment_sha256,
            source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
            canonical_content=enrichment,
        ),
    )
    record = WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=1,
        synthesis_input_hash="1" * 64,
        synthesis_canonical_sha256=synthesis_sha256,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=2,
        enrichment_input_hash="2" * 64,
        enrichment_canonical_sha256=enrichment_sha256,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.ARTICLE,
        workspace_pin_hash="3" * 64,
        registry_hash=registry_hash(entries),
        block_registry=entries,
    )

    blocks, _, _ = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
        record=record,
        allow_operations=False,
    )

    assert [block["figure"]["caption"] for block in blocks if block["kind"] == "figure"] == [
        "First in document order",
        "Second in document order",
    ]
