from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import EvidenceKind
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_evidence_recorder_replay_and_immutability_use_postgres_repository(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    raw_blob_bytes = b"C1 harmless archived evidence fixture"
    raw_sha256 = hashlib.sha256(raw_blob_bytes).hexdigest()
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=raw_sha256,
            size=len(raw_blob_bytes),
            mime_type="application/octet-stream",
            logical_bucket="evidence-recorder-test",
        )
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.blobs.add(blob)
            recorder = ProductionEvidenceRecorder(
                cast(ProductionArtifactStore, object()), uow.evidence_observations
            )
            observed_at = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
            observation = await recorder.record_existing_blob(
                investigation_id=investigation.id,
                kind=EvidenceKind.VT_FILE_REPORT,
                blob_id=blob.id,
                sha256=raw_sha256,
                source="virustotal.file_report",
                source_version="api-v3",
                object_type="file",
                object_id=raw_sha256,
                limitations=("Provider report may be partial.",),
                observed_at=observed_at,
                idempotency_key=f"vt-file-report:{investigation.id}:{raw_sha256}",
            )
            replay = await recorder.record_existing_blob(
                investigation_id=investigation.id,
                kind=EvidenceKind.VT_FILE_REPORT,
                blob_id=blob.id,
                sha256=raw_sha256,
                source="virustotal.file_report",
                source_version="api-v3",
                object_type="file",
                object_id=raw_sha256,
                limitations=("Provider report may be partial.",),
                observed_at=observed_at + timedelta(days=1),
                idempotency_key=f"vt-file-report:{investigation.id}:{raw_sha256}",
            )
            assert replay == observation
            await uow.commit()

            persisted = await uow.evidence_observations.get(observation.id)
            assert persisted == observation
            assert await uow.evidence_observations.list_for_investigation(investigation.id) == (
                observation,
            )
            with pytest.raises(ValueError, match="immutable observation"):
                await uow.evidence_observations.append_if_absent(
                    replace(observation, source="tampered")
                )
    finally:
        await engine.dispose()


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    from tests.integration.edition_codes import reserve_edition_code

    suffix = uuid4().hex
    edition = Edition(
        country=f"Evidence recorder {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Evidence recorder integration fixture",
            slug=f"evidence-recorder-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="f" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation
