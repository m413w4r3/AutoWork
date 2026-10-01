"""PostgreSQL persistence coverage for versioned publication renders."""

import asyncio
import calendar
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.domain.publication_render import (
    PublicationRender,
    PublicationRenderAcquisition,
    PublicationRenderAcquisitionOutcome,
    PublicationRenderFormat,
    PublicationRenderStatus,
)
from cti_app.infrastructure.database.session import create_postgres_engine

pytestmark = pytest.mark.integration


async def _persist_publication_artifact(uow_factory: UnitOfWorkFactory) -> ProductionArtifact:
    period_token = uuid4().int
    period_year = 2000 + period_token % 6000
    period_month = (period_token >> 16) % 12 + 1
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(period_year, period_month, 1),
        period_end=date(
            period_year,
            period_month,
            calendar.monthrange(period_year, period_month)[1],
        ),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Publication render persistence fixture",
        slug=f"publication-render-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=sha256(uuid4().bytes).hexdigest(),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.commit()
    return artifact


async def _persist_blob(uow_factory: UnitOfWorkFactory, content: bytes) -> BlobRecord:
    unique_content = content + uuid4().bytes
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256(unique_content).hexdigest(),
            size=len(unique_content),
            mime_type="application/octet-stream",
            logical_bucket="publication-render-repository-test",
        )
    )
    async with uow_factory() as uow:
        await uow.blobs.add(blob)
        await uow.commit()
    return blob


def _render(artifact_id: UUID, *, input_hash: str | None = None) -> PublicationRender:
    now = datetime.now(UTC)
    return PublicationRender(
        id=uuid4(),
        publication_artifact_id=artifact_id,
        renderer="typst",
        renderer_version="publication-v4-typst-v1",
        template_version="chp-article-v1",
        template_sha256="a" * 64,
        compiler="typst",
        compiler_version="0.15.1",
        font_bundle_version="test-font-bundle-v1",
        render_policy_version="test-render-policy-v1",
        format=PublicationRenderFormat.PDF,
        input_hash=input_hash or sha256(uuid4().bytes).hexdigest(),
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=None,
        output_sha256=None,
        output_byte_size=None,
        status=PublicationRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _raw_render_values(artifact_id: UUID) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "publication_artifact_id": artifact_id,
        "renderer": "typst",
        "renderer_version": "v1",
        "template_version": "template-v1",
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "fonts-v1",
        "render_policy_version": "policy-v1",
        "format": "pdf",
        "input_hash": sha256(uuid4().bytes).hexdigest(),
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": "running",
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


_RAW_RENDER_INSERT = text(
    "INSERT INTO publication_renders ("
    "id, publication_artifact_id, renderer, renderer_version, template_version, "
    "template_sha256, compiler, compiler_version, font_bundle_version, "
    "render_policy_version, format, input_hash, source_blob_id, render_data_blob_id, "
    "output_blob_id, output_sha256, output_byte_size, status, error_code, error_message, "
    "created_at, updated_at"
    ") VALUES ("
    ":id, :publication_artifact_id, :renderer, :renderer_version, :template_version, "
    ":template_sha256, :compiler, :compiler_version, :font_bundle_version, "
    ":render_policy_version, :format, :input_hash, :source_blob_id, :render_data_blob_id, "
    ":output_blob_id, :output_sha256, :output_byte_size, :status, :error_code, "
    ":error_message, :created_at, :updated_at)"
)


async def _acquire_concurrently(
    uow_factory: UnitOfWorkFactory,
    proposed: PublicationRender,
    *,
    worker_count: int = 8,
    stale_running_before: datetime | None = None,
) -> list[PublicationRenderAcquisition]:
    stale_before = stale_running_before or datetime.now(UTC) - timedelta(minutes=5)

    async def acquire(candidate: PublicationRender) -> PublicationRenderAcquisition:
        async with uow_factory() as uow:
            result = await uow.publication_renders.acquire_for_render(
                candidate,
                stale_running_before=stale_before,
            )
            await uow.commit()
            return result

    return await asyncio.gather(
        *(acquire(replace(proposed, id=uuid4())) for _ in range(worker_count))
    )


async def test_publication_render_repository_round_trip_and_transitions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    render = _render(artifact.id)
    source_blob = await _persist_blob(uow_factory, b"publication source fixture")
    render_data_blob = await _persist_blob(uow_factory, b"publication render data fixture")
    output_blob = await _persist_blob(uow_factory, b"publication PDF fixture")

    async with uow_factory() as uow:
        assert await uow.publication_renders.add(render) == render
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.publication_renders.get(render.id) == render
        assert await uow.publication_renders.get_by_input_hash(render.input_hash) == render
        assert (await uow.publication_renders.get(render.id)).font_bundle_version == (
            "test-font-bundle-v1"
        )
        assert (await uow.publication_renders.get(render.id)).render_policy_version == (
            "test-render-policy-v1"
        )
        with pytest.raises(ValueError, match="output metadata"):
            await uow.publication_renders.mark_succeeded(
                render.id,
                output_blob_id=None,
                output_sha256=None,
                output_byte_size=None,
            )  # type: ignore[arg-type]
        succeeded = await uow.publication_renders.mark_succeeded(
            render.id,
            source_blob_id=source_blob.id,
            render_data_blob_id=render_data_blob.id,
            output_blob_id=output_blob.id,
            output_sha256="c" * 64,
            output_byte_size=42,
        )
        await uow.commit()
    assert succeeded.status is PublicationRenderStatus.SUCCEEDED
    assert succeeded.output_byte_size == 42
    assert succeeded.output_blob_id is not None

    failed_render = _render(artifact.id)
    async with uow_factory() as uow:
        await uow.publication_renders.add(failed_render)
        await uow.commit()
    async with uow_factory() as uow:
        with pytest.raises(ValueError, match="error_code"):
            await uow.publication_renders.mark_failed(  # type: ignore[arg-type]
                failed_render.id, error_code=None
            )
        failed = await uow.publication_renders.mark_failed(
            failed_render.id,
            error_code="typst_compile_failed",
            error_message="compiler exited with a failure",
        )
        await uow.commit()
    assert failed.status is PublicationRenderStatus.FAILED
    assert failed.error_code == "typst_compile_failed"


async def test_publication_render_database_checks_reject_invalid_rows(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    engine = create_postgres_engine(migrated_postgres_url)

    async def assert_rejected(**overrides: object) -> None:
        values = _raw_render_values(artifact.id)
        values.update(overrides)
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(_RAW_RENDER_INSERT, values)

    async def assert_accepted(**overrides: object) -> None:
        values = _raw_render_values(artifact.id)
        values.update(overrides)
        async with engine.begin() as connection:
            await connection.execute(_RAW_RENDER_INSERT, values)

    async def assert_missing_identity_rejected(column: str) -> None:
        values = _raw_render_values(artifact.id)
        del values[column]
        columns = tuple(values)
        statement = text(
            f"INSERT INTO publication_renders ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + column for column in columns)})"
        )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(statement, values)

    try:
        await assert_rejected(format="docx")
        await assert_rejected(status="cancelled")
        await assert_rejected(input_hash="A" * 64)
        await assert_rejected(template_sha256="B" * 64)
        await assert_rejected(output_sha256="A" * 64)
        await assert_rejected(output_sha256="a" * 63)
        await assert_rejected(output_sha256="g" * 64)
        await assert_accepted(output_sha256=None)
        await assert_accepted(output_sha256="c" * 64)
        await assert_missing_identity_rejected("font_bundle_version")
        await assert_missing_identity_rejected("render_policy_version")
        await assert_rejected(status="succeeded")
        await assert_rejected(status="failed")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("blob_column", ("source_blob_id", "render_data_blob_id", "output_blob_id"))
async def test_publication_render_blob_foreign_keys_reject_missing_rows_on_insert_and_update(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    blob_column: str,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    missing_blob_id = uuid4()
    render = _render(artifact.id)
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        with pytest.raises(IntegrityError) as inserted:
            async with uow_factory() as uow:
                await uow.publication_renders.add(replace(render, **{blob_column: missing_blob_id}))
                await uow.commit()
        assert getattr(inserted.value.orig, "sqlstate", None) == "23503"

        async with uow_factory() as uow:
            await uow.publication_renders.add(render)
            await uow.commit()
        with pytest.raises(IntegrityError) as updated:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"UPDATE publication_renders SET {blob_column} = :blob_id "
                        "WHERE id = :render_id"
                    ),
                    {"blob_id": missing_blob_id, "render_id": render.id},
                )
        assert getattr(updated.value.orig, "sqlstate", None) == "23503"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("blob_column", ("source_blob_id", "render_data_blob_id", "output_blob_id"))
async def test_publication_render_blob_foreign_keys_restrict_delete(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    blob_column: str,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    descriptor = BlobDescriptor(
        sha256=sha256(f"{blob_column}-{uuid4()}".encode()).hexdigest(),
        size=1,
        mime_type="application/octet-stream",
        logical_bucket=f"render-{blob_column.removesuffix('_blob_id')}",
    )
    blob = BlobRecord(descriptor=descriptor)
    render = replace(_render(artifact.id), **{blob_column: blob.id})
    async with uow_factory() as uow:
        await uow.blobs.add(blob)
        await uow.publication_renders.add(render)
        await uow.commit()

    engine = create_postgres_engine(migrated_postgres_url)
    try:
        with pytest.raises(IntegrityError) as deleted:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM blobs WHERE id = :blob_id"), {"blob_id": blob.id}
                )
        assert getattr(deleted.value.orig, "sqlstate", None) == "23503"
    finally:
        await engine.dispose()


async def test_concurrent_acquire_for_render_inserts_one_row(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    proposed = _render(artifact.id)

    acquisitions = await _acquire_concurrently(uow_factory, proposed)

    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    async with uow_factory() as uow:
        persisted = await uow.publication_renders.get_by_input_hash(proposed.input_hash)
    assert persisted is not None
    assert persisted.status is PublicationRenderStatus.RUNNING


async def test_concurrent_acquire_retries_a_failed_render_once(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    failed = _render(artifact.id)
    async with uow_factory() as uow:
        await uow.publication_renders.add(failed)
        await uow.publication_renders.mark_failed(
            failed.id,
            error_code="typst_compile_failed",
            error_message="prior attempt failed",
        )
        await uow.commit()

    acquisitions = await _acquire_concurrently(uow_factory, failed)

    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert all(acquisition.render.id == failed.id for acquisition in acquisitions)


async def test_concurrent_acquire_takes_over_stale_running_once_but_not_fresh_rows(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    stale = replace(_render(artifact.id), updated_at=datetime.now(UTC) - timedelta(minutes=10))
    async with uow_factory() as uow:
        await uow.publication_renders.add(stale)
        await uow.commit()

    stale_acquisitions = await _acquire_concurrently(uow_factory, stale)
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.ACQUIRED
            for acquisition in stale_acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in stale_acquisitions
        )
        == len(stale_acquisitions) - 1
    )

    fresh = _render(artifact.id)
    async with uow_factory() as uow:
        await uow.publication_renders.add(fresh)
        await uow.commit()
    fresh_acquisitions = await _acquire_concurrently(uow_factory, fresh)
    assert all(
        acquisition.outcome is PublicationRenderAcquisitionOutcome.IN_PROGRESS
        for acquisition in fresh_acquisitions
    )


async def test_concurrent_invalid_succeeded_repair_has_one_owner(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    output_blob = await _persist_blob(uow_factory, b"invalid succeeded output fixture")
    succeeded = replace(
        _render(artifact.id),
        status=PublicationRenderStatus.SUCCEEDED,
        output_blob_id=output_blob.id,
        output_sha256="d" * 64,
        output_byte_size=42,
    )
    async with uow_factory() as uow:
        await uow.publication_renders.add(succeeded)
        await uow.commit()
    proposed = replace(succeeded, status=PublicationRenderStatus.RUNNING)

    async def reacquire() -> PublicationRenderAcquisition:
        async with uow_factory() as uow:
            result = await uow.publication_renders.reacquire_invalid_succeeded(
                proposed,
                observed_output_blob_id=succeeded.output_blob_id,
                observed_output_sha256=succeeded.output_sha256,
            )
            await uow.commit()
            return result

    acquisitions = await asyncio.gather(*(reacquire() for _ in range(8)))
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is PublicationRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert all(acquisition.render.id == succeeded.id for acquisition in acquisitions)
