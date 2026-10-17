from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalObservation,
    VirusTotalOperation,
)
from cti_app.infrastructure.database.models.core import BlobRow
from cti_app.infrastructure.database.repositories.core import (
    SqlAlchemyVirusTotalObservationRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_vt_observation_constraints_cover_domain_enums(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    blob_id = uuid4()
    raw_sha256 = "a" * 64
    now = datetime.now(UTC)

    try:
        async with session_factory() as session:
            session.add(
                BlobRow(
                    id=blob_id,
                    sha256=raw_sha256,
                    size=1,
                    mime_type="application/json",
                    logical_bucket="virustotal-raw",
                    object_key=f"synthetic/{uuid4().hex}",
                    created_at=now,
                )
            )
            await session.flush()

            repository = SqlAlchemyVirusTotalObservationRepository(session)
            for operation in VirusTotalOperation:
                for capability in VirusTotalCapability:
                    await repository.add(
                        VirusTotalObservation(
                            operation=operation,
                            capability=capability,
                            source_identifier=f"synthetic-{operation.value}-{capability.value}",
                            safe_parameters={},
                            http_status=200,
                            blob_id=blob_id,
                            raw_sha256=raw_sha256,
                            raw_size=1,
                            observed_at=now,
                        )
                    )
            await session.commit()
    finally:
        await engine.dispose()
