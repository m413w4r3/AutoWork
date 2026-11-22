"""PostgreSQL oracle for the prose authority fixed at handoff creation.

Creates the investigation through the real ``AnalystPostSynthesisService`` on a
disposable database, asserts the persisted column, the typed re-ensure refusal
and the ``BEFORE UPDATE`` immutability trigger (ADR-0011 D4).
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    ProseAuthorityConflictError,
)
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProseAuthority,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


def test_handoff_persists_bridge_only_and_the_column_is_immutable(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            suffix = uuid4().hex
            edition = Edition(
                country=f"Prose authority fixture {suffix}",
                country_code=reserve_edition_code(),
                period_start=date(2026, 11, 1),
                period_end=date(2026, 11, 30),
                tlp=TLP.AMBER,
                languages=("en",),
            )
            subject = Subject(
                edition_id=edition.id,
                title="Prose authority fixture",
                slug=f"prose-authority-{suffix}",
                tlp=TLP.AMBER,
            )
            run = ProductionRun(
                subject_id=subject.id,
                edition_id=edition.id,
                research_date=date(2026, 11, 22),
            )
            synthesis = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject.id,
                stage=ProductionArtifactStage.SYNTHESIS,
                status=ProductionArtifactStatus.VERIFIED,
                version=1,
                input_hash="a" * 64,
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                assert await uow.editions.add_if_absent(edition)
                await uow.subjects.add(subject)
                await uow.production_runs.add(run)
                await uow.production_artifacts.append(synthesis)
                await uow.commit()

            service = AnalystPostSynthesisService(
                lambda: SqlAlchemyUnitOfWork(factory),
                ProductionArtifactStore(
                    BlobCatalogService(
                        FilesystemBlobStore(tmp_path / "handoff-blobs"),
                        lambda: SqlAlchemyUnitOfWork(factory),
                    )
                ),
            )
            created = await service.ensure_for_verified_synthesis(
                run=run,
                synthesis=synthesis,
                extraction_artifacts=(),
                extraction_items=(),
                policy=AnalystHandoffPolicy(tlp="amber"),
                prose_authority=ProseAuthority.BRIDGE_ONLY,
            )
            assert created is not None

            async with factory() as session:
                value = await session.scalar(
                    sa.text("SELECT prose_authority FROM analyst_investigations WHERE id = :id"),
                    {"id": created.investigation_id},
                )
                assert value == "bridge_only"
                await session.rollback()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                persisted = await uow.analyst_investigations.get(created.investigation_id)
                assert persisted is not None
                assert persisted.prose_authority is ProseAuthority.BRIDGE_ONLY
                with pytest.raises(ProseAuthorityConflictError):
                    await service.ensure_for_verified_synthesis(
                        run=run,
                        synthesis=synthesis,
                        extraction_artifacts=(),
                        extraction_items=(),
                        policy=AnalystHandoffPolicy(tlp="amber"),
                        prose_authority=ProseAuthority.LEGACY,
                        uow=uow,
                    )

            async with factory() as session:
                with pytest.raises(DBAPIError, match="immutable"):
                    await session.execute(
                        sa.text(
                            "UPDATE analyst_investigations SET prose_authority = 'legacy' "
                            "WHERE id = :id"
                        ),
                        {"id": created.investigation_id},
                    )
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
