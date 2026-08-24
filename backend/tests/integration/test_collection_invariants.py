from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from cti_app.domain.collection import SourceCollection
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.database.models.collection import SourceCollectionRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_collection import selected_subject

pytestmark = pytest.mark.integration


async def test_database_rejects_verified_relationship_without_qualified_evidence(
    migrated_postgres_url: str,
) -> None:
    fixture = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(fixture, ("https://one.example/report",))
    edition = next(iter(fixture.editions.values()))
    edition.country = "Invariant Test"
    edition.country_code = "IV"
    batch = next(iter(fixture.batches.values()))
    group = next(iter(fixture.groups.values()))
    source = batch.candidates[0].sources[0]
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        batch_id=batch.id,
        source_candidate_id=source.id,
        requested_url=source.canonical_url,
        proposed_role=source.role,
    )
    model_runs = [
        ModelRun(
            id=run_id,
            provider=ModelProvider.FAKE,
            model_role=ModelRole.RESEARCH,
            requested_model="fixture",
            prompt_template_id="fixture",
            prompt_template_version="1",
            authorized_input_hash="a" * 64,
            evidence_pack_hash="b" * 64,
            parameters={},
        )
        for run_id in (batch.discovery_model_run_id,)
    ]
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            for run in model_runs:
                await uow.model_runs.add(run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.editorial_groups.add(group)
            assert await uow.source_collections.add_if_absent(collection)
            await uow.commit()

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(SourceCollectionRow)
                    .where(SourceCollectionRow.id == collection.id)
                    .values(
                        relationship_status="verified",
                        relationship_evidence="model_proposal",
                    )
                )
    finally:
        await engine.dispose()
