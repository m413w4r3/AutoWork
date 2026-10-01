"""PostgreSQL persistence coverage for versioned publication renders."""

import calendar
from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.domain.publication_render import (
    PublicationRender,
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


def _render(artifact_id: UUID) -> PublicationRender:
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
        format=PublicationRenderFormat.PDF,
        input_hash=sha256(uuid4().bytes).hexdigest(),
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


async def test_publication_render_repository_round_trip_and_transitions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    artifact = await _persist_publication_artifact(uow_factory)
    render = _render(artifact.id)

    async with uow_factory() as uow:
        assert await uow.publication_renders.add(render) == render
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.publication_renders.get(render.id) == render
        assert await uow.publication_renders.get_by_input_hash(render.input_hash) == render
        with pytest.raises(ValueError, match="output metadata"):
            await uow.publication_renders.mark_succeeded(
                render.id,
                output_blob_id=None,
                output_sha256=None,
                output_byte_size=None,
            )  # type: ignore[arg-type]
        succeeded = await uow.publication_renders.mark_succeeded(
            render.id,
            source_blob_id=uuid4(),
            render_data_blob_id=uuid4(),
            output_blob_id=uuid4(),
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
    statement = text(
        "INSERT INTO publication_renders ("
        "id, publication_artifact_id, renderer, renderer_version, template_version, "
        "template_sha256, compiler, compiler_version, format, input_hash, status, "
        "created_at, updated_at, output_blob_id, output_sha256, output_byte_size, error_code"
        ") VALUES ("
        ":id, :publication_artifact_id, 'typst', 'v1', 'template', :template_sha256, "
        "'typst', '0.15.1', :format, :input_hash, :status, :created_at, :updated_at, "
        ":output_blob_id, :output_sha256, :output_byte_size, :error_code)"
    )
    now = datetime.now(UTC)

    async def assert_rejected(**overrides: object) -> None:
        values: dict[str, object] = {
            "id": uuid4(),
            "publication_artifact_id": artifact.id,
            "template_sha256": "a" * 64,
            "format": "pdf",
            "input_hash": sha256(uuid4().bytes).hexdigest(),
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "output_blob_id": None,
            "output_sha256": None,
            "output_byte_size": None,
            "error_code": None,
        }
        values.update(overrides)
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(statement, values)

    try:
        await assert_rejected(format="docx")
        await assert_rejected(status="cancelled")
        await assert_rejected(input_hash="A" * 64)
        await assert_rejected(template_sha256="B" * 64)
        await assert_rejected(status="succeeded")
        await assert_rejected(status="failed")
    finally:
        await engine.dispose()
