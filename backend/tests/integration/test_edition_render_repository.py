import calendar
import json
from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import EditionRelease, PublicationManifestV1
from cti_app.domain.edition_render import EditionRender
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionBatchStatus,
    ProductionRun,
)
from cti_app.domain.publication_render import PublicationRender
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus
from cti_app.infrastructure.database.session import create_postgres_engine

pytestmark = pytest.mark.integration


def _blob(bucket: str, content: bytes) -> BlobRecord:
    unique_content = content + uuid4().bytes
    return BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256(unique_content).hexdigest(),
            size=len(unique_content),
            mime_type="application/octet-stream",
            logical_bucket=bucket,
        )
    )


async def _persist_release(uow_factory: UnitOfWorkFactory) -> tuple[EditionRelease, UUID]:
    token = uuid4().int
    year = 2000 + token % 6000
    month = (token >> 16) % 12 + 1
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.QUEUED,
        idempotency_key=f"edition-render-{uuid4().hex}",
    )
    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="edition-render-test",
        entries=(),
        exclusions=(),
    )
    manifest_bytes = json.dumps(manifest.to_json(), sort_keys=True).encode()
    manifest_blob = _blob("edition-render-test-manifest", manifest_bytes)
    document_bytes = b"edition document " + uuid4().bytes
    document_blob = _blob("edition-render-test-document", document_bytes)
    release = EditionRelease(
        edition_id=edition.id,
        manifest_id=manifest.id,
        edition_document_blob_id=document_blob.id,
        edition_document_sha256=sha256(document_bytes).hexdigest(),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.edition_production_batches.add(batch)
        await uow.blobs.add(manifest_blob)
        await uow.blobs.add(document_blob)
        await uow.publication_manifests.add(manifest, manifest_blob.id)
        assert await uow.edition_releases.add_if_absent(release)
        await uow.commit()
    return release, document_blob.id


def _render(release_id: UUID, **overrides: object) -> EditionRender:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "edition_release_id": release_id,
        "renderer": "typst",
        "renderer_version": "edition-v2-typst-v1",
        "template_version": "chp-edition-v1",
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "test-font-bundle-v1",
        "render_policy_version": "typst-edition-v2-v1",
        "format": TypstRenderFormat.PDF,
        "input_hash": sha256(uuid4().bytes).hexdigest(),
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": TypstRenderStatus.RUNNING,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return EditionRender(**values)  # type: ignore[arg-type]


async def test_edition_render_repository_round_trip_and_failed_release_intact(
    uow_factory: UnitOfWorkFactory,
) -> None:
    release, _ = await _persist_release(uow_factory)
    proposed = _render(release.id)
    async with uow_factory() as uow:
        acquired = await uow.edition_renders.acquire_for_render(
            proposed, stale_running_before=datetime.now(UTC)
        )
        assert acquired.render == proposed
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.edition_renders.get(proposed.id) == proposed
        assert await uow.edition_renders.get_by_input_hash(proposed.input_hash) == proposed
        failed = await uow.edition_renders.mark_failed(
            proposed.id,
            error_code="typst_compile_failed",
            error_message="compiler exited with a failure",
        )
        persisted_release = await uow.edition_releases.get_by_manifest(release.manifest_id)
        await uow.commit()

    assert failed.status is TypstRenderStatus.FAILED
    assert failed.error_code == "typst_compile_failed"
    assert persisted_release == release

    async with uow_factory() as uow:
        retried = await uow.edition_renders.acquire_for_render(
            proposed, stale_running_before=datetime.now(UTC)
        )
        await uow.commit()
    assert retried.render.id == proposed.id
    assert retried.render.status is TypstRenderStatus.RUNNING


async def test_edition_render_blob_reachability_and_restrict_foreign_keys(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
) -> None:
    release, document_blob_id = await _persist_release(uow_factory)
    source_blob = _blob("edition-render-source", b"source")
    render_data_blob = _blob("edition-render-data", b"render data")
    output_blob = _blob("edition-render-output", b"pdf")
    proposed = _render(release.id)
    async with uow_factory() as uow:
        await uow.blobs.add(source_blob)
        await uow.blobs.add(render_data_blob)
        await uow.blobs.add(output_blob)
        await uow.edition_renders.acquire_for_render(
            proposed, stale_running_before=datetime.now(UTC)
        )
        await uow.edition_renders.mark_succeeded(
            proposed.id,
            source_blob_id=source_blob.id,
            render_data_blob_id=render_data_blob.id,
            output_blob_id=output_blob.id,
            output_sha256="c" * 64,
            output_byte_size=3,
        )
        await uow.commit()

    async with uow_factory() as uow:
        latest_success = await uow.edition_renders.get_latest_succeeded_for_release(release.id)
        assert latest_success is not None
        assert latest_success.id == proposed.id

    subject = Subject(
        edition_id=release.edition_id,
        title="Publication render reachability fixture",
        slug=f"edition-render-reachability-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=release.edition_id)
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=sha256(uuid4().bytes).hexdigest(),
    )
    now = datetime.now(UTC)
    publication_render = PublicationRender(
        id=uuid4(),
        publication_artifact_id=artifact.id,
        renderer="typst",
        renderer_version="publication-v4-typst-v1",
        template_version="chp-article-v1",
        template_sha256="d" * 64,
        compiler="typst",
        compiler_version="0.15.1",
        font_bundle_version="test-font-bundle-v1",
        render_policy_version="test-render-policy-v1",
        format=TypstRenderFormat.PDF,
        input_hash=sha256(uuid4().bytes).hexdigest(),
        source_blob_id=source_blob.id,
        render_data_blob_id=render_data_blob.id,
        output_blob_id=output_blob.id,
        output_sha256="e" * 64,
        output_byte_size=3,
        status=TypstRenderStatus.SUCCEEDED,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.publication_renders.add(publication_render)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.blobs.count_references(document_blob_id) == 1
        assert await uow.blobs.count_references(source_blob.id) == 2
        assert await uow.blobs.count_references(render_data_blob.id) == 2
        assert await uow.blobs.count_references(output_blob.id) == 2

    engine = create_postgres_engine(migrated_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM publication_renders WHERE id = :render_id"),
                {"render_id": publication_render.id},
            )
        async with uow_factory() as uow:
            assert await uow.blobs.count_references(source_blob.id) == 1
            assert await uow.blobs.count_references(render_data_blob.id) == 1
            assert await uow.blobs.count_references(output_blob.id) == 1

        for referenced_blob in (source_blob, render_data_blob, output_blob):
            with pytest.raises(IntegrityError) as deleted_blob:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DELETE FROM blobs WHERE id = :blob_id"),
                        {"blob_id": referenced_blob.id},
                    )
            assert getattr(deleted_blob.value.orig, "sqlstate", None) == "23503"

        # edition_releases is append-only: the trigger rejects the delete before
        # the edition_renders RESTRICT foreign key can fire.
        with pytest.raises(DBAPIError) as deleted_release:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM edition_releases WHERE id = :release_id"),
                    {"release_id": release.id},
                )
        assert getattr(deleted_release.value.orig, "sqlstate", None) == "55000"
    finally:
        await engine.dispose()


async def test_edition_render_database_checks_reject_invalid_rows(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
) -> None:
    release, _ = await _persist_release(uow_factory)
    render = _render(release.id)
    async with uow_factory() as uow:
        await uow.edition_renders.acquire_for_render(render, stale_running_before=datetime.now(UTC))
        await uow.commit()

    engine = create_postgres_engine(migrated_postgres_url)
    try:
        invalid_updates = (
            "format = 'html'",
            "status = 'cancelled'",
            "status = 'succeeded'",
            "status = 'failed', error_code = NULL",
            "input_hash = :invalid_hash",
        )
        for update_values in invalid_updates:
            parameters: dict[str, object] = {"render_id": render.id}
            if "invalid_hash" in update_values:
                parameters["invalid_hash"] = "A" * 64
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(f"UPDATE edition_renders SET {update_values} WHERE id = :render_id"),
                        parameters,
                    )
    finally:
        await engine.dispose()
