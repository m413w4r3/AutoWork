from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_governance import (
    InvestigationActor,
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.virustotal import VirusTotalCapabilities, VirusTotalHttpError
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    InvestigationBudget,
    QualificationActorKind,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


class _HashCheckedPort:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        assert hashlib.sha256(self.payload).hexdigest() == file_hash
        self.calls += 1
        sink.write(self.payload)


class _RouteRefusedPort:
    def __init__(self) -> None:
        self.calls = 0

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        del file_hash, sink
        self.calls += 1
        raise VirusTotalHttpError(
            "synthetic download route absent",
            code="virustotal_not_found",
            status_code=404,
            route="/files/{hash}/download_url",
        )


class _PostgresEvidenceRecorder:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def record_existing_blob(self, **kwargs: Any) -> Any:
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(
                cast(ProductionArtifactStore, object()), uow.evidence_observations
            )
            observation = await recorder.record_existing_blob(**kwargs)
            await uow.commit()
            return observation


@pytest.mark.asyncio
async def test_g2_acquisition_quarantine_and_g3_decisions_persist_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    payload = b"harmless PostgreSQL acquisition fixture"
    requested_sha256 = hashlib.sha256(payload).hexdigest()
    port = _HashCheckedPort(payload)
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blob-storage"), uow_factory)
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    proposal = InvestigationEnvelopeProposal(
        investigation_id=investigation.id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 2}
        ),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=False,
        ),
    )
    try:
        approved = await envelope_service.approve(
            proposal,
            actor_id="analyst:integration",
            reason="Approve two hash-verified VT hit downloads.",
            correlation_id=f"g2:{investigation.id}",
        )
        recorder = cast(Any, _PostgresEvidenceRecorder(uow_factory))
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=recorder,
        )
        association = await hits.register_hit(
            investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )
        result = await hits.acquire_hit(
            association.id,
            purpose="Review a VT pivot under the approved G2 envelope.",
            operation_key=f"vt.download_sample:{investigation.id}:{association.id}",
        )

        assert result.status is ToolInvocationStatus.SUCCEEDED
        assert result.sample_id is not None
        assert result.association.qualification is SampleQualification.CANDIDATE
        assert port.calls == 1
        qualification_service = SampleQualificationService(uow_factory)
        confirmed = await qualification_service.qualify(
            association.id,
            SampleQualification.CONFIRMED,
            InvestigationActor("analyst:integration", QualificationActorKind.HUMAN),
            "The analyst separately reviewed the candidate evidence.",
            result.observation_refs,
            correlation_id=f"g3:{association.id}",
        )
        assert confirmed.qualification is SampleQualification.CONFIRMED

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored_envelope = await uow.investigation_envelopes.get(investigation.id)
            stored_association = await uow.sample_associations.get(association.id)
            sample = await uow.samples.get(result.sample_id)
            assert stored_envelope is not None
            assert stored_envelope.version == approved.version + 2
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
            assert stored_association is not None
            assert stored_association.qualification is SampleQualification.CONFIRMED
            assert stored_association.changes[-1].actor_kind is QualificationActorKind.HUMAN
            assert sample is not None
            assert sample.state.value == "review_candidate"
            blob = await uow.blobs.get(sample.blob_id)
            assert blob is not None
            assert blob.descriptor.sha256 == requested_sha256
            assert await catalog.read(sample.blob_id, max_bytes=1024) == payload
            observations = await uow.evidence_observations.list_for_investigation(investigation.id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            attempts = await uow.sample_acquisition_attempts.find_successful(
                investigation.id, requested_sha256
            )
            assert any(item.kind is EvidenceKind.OTHER for item in observations)
            assert {item.decision_type.value for item in decisions} == {
                "pivot_approve",
                "member_validate",
            }
            assert attempts is not None and attempts.sample_id == result.sample_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_definitive_download_refusal_is_cached_and_budget_is_resolved_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    port = _RouteRefusedPort()
    catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "refused-download-storage"), uow_factory
    )
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    requested_sha256 = "a" * 64
    try:
        await envelope_service.approve(
            InvestigationEnvelopeProposal(
                investigation_id=investigation.id,
                allowed_providers=("vt",),
                budget=InvestigationBudget(
                    limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 2}
                ),
                acquire_hits_allowed=True,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.CLEAR,
                    do_not_submit=False,
                    external_llm_allowed=False,
                ),
            ),
            actor_id="analyst:integration",
            reason="Approve bounded synthetic download failure coverage.",
            correlation_id=f"g2:download-refusal:{investigation.id}",
        )
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=cast(Any, _PostgresEvidenceRecorder(uow_factory)),
        )
        association = await hits.register_hit(
            investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )

        first = await hits.acquire_hit(
            association.id,
            purpose="Review a synthetic VT hit.",
            operation_key="synthetic:download:first",
        )
        replay = await hits.acquire_hit(
            association.id,
            purpose="Retry the same synthetic VT hit.",
            operation_key="synthetic:download:second",
        )

        assert first.status is ToolInvocationStatus.UNAVAILABLE
        assert first.error_code == "virustotal_download_route_not_found"
        assert first.budget_consumed == {
            BudgetDimension.VT_UNITS: 1,
            BudgetDimension.DOWNLOADS: 0,
        }
        assert replay.status is ToolInvocationStatus.UNAVAILABLE
        assert replay.cached is True
        assert replay.budget_consumed == {}
        assert port.calls == 1

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            latest_error = await uow.sample_acquisition_attempts.find_latest_error(
                investigation.id, requested_sha256
            )
            stored_envelope = await uow.investigation_envelopes.get(investigation.id)
            assert latest_error is not None
            assert latest_error.error_code == "virustotal_download_route_not_found"
            assert stored_envelope is not None
            assert stored_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
            assert stored_envelope.budget.reserved[BudgetDimension.VT_UNITS] == 0
            assert stored_envelope.budget.reserved[BudgetDimension.DOWNLOADS] == 0
    finally:
        await engine.dispose()


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Acquisition integration {suffix}",
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
            title="Investigation acquisition integration fixture",
            slug=f"acquisition-integration-{suffix}",
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
            budget=LoopBudget(max_vt_read_units=4, max_hits_acquired=2),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation
