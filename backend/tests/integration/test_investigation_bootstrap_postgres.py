"""PostgreSQL oracle for M24-03A snapshot persistence.

The offline suite proves the inventory/plan/batch logic with fakes.  This test
proves the dated bootstrap snapshot really lands in the canonical
``evidence_observations`` table with its blob, and that a replayed batch returns
the stored snapshot instead of appending a second one, using the real
SQLAlchemy unit of work and a filesystem content-addressed blob store.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_article_snapshot import (
    ProductionArticleSnapshotAdapter,
)
from cti_app.application.investigation_bootstrap import (
    BOOTSTRAP_SNAPSHOT_OBJECT_TYPE,
    BOOTSTRAP_SOURCE,
    BootstrapPlanState,
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_routing import (
    ProductionRoutingDecisionV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

_SEED_SHA256 = "a" * 64
_SOURCE_DOCUMENT_ID = uuid4()
_FIXED_NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


class _NoopExecutor:
    async def execute_tool_call(self, **_kwargs: Any) -> Any:
        raise AssertionError("no provider call is allowed without G2")


@pytest.mark.asyncio
async def test_bootstrap_snapshot_persists_canonically_and_replays_postgres(
    uow_factory, tmp_path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "bootstrap-blobs"), uow_factory)
    )
    suffix = uuid4().hex
    edition = Edition(
        country=f"Bootstrap PG {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Bootstrap persistence subject",
        slug=f"bootstrap-pg-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_payload = {
        "title": "Verified synthesis with one documented sample",
        "subject_id": str(subject.id),
        "lead": [{"text": "The article cites one sample."}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="bootstrap-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )
    extraction_payload = {
        "sources": [
            {
                "indicators": [
                    {
                        "artifact_type": "hash",
                        "value": _SEED_SHA256,
                        "indicator_status": "confirmed_ioc",
                        "context": "IOC table row 1",
                        "evidence_quote": f"SHA-256 {_SEED_SHA256}",
                        "source_document_ids": [str(_SOURCE_DOCUMENT_ID)],
                    }
                ]
            }
        ]
    }
    extraction_blob_id, extraction_sha256 = await artifact_store.put_canonical_json(
        extraction_payload, bucket="bootstrap-extraction"
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=extraction_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob_id,
    )
    relevance_blob_id, relevance_sha256 = await artifact_store.put_canonical_json(
        {"items": []}, bucket="bootstrap-relevance"
    )
    relevance = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=relevance_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=relevance_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis, budget=LoopBudget()
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [
            {
                "artifact_id": str(extraction.id),
                "canonical_blob_id": str(extraction.canonical_blob_id),
                "input_hash": extraction.input_hash,
                "version": extraction.version,
            }
        ],
        "file_indicators": [],
        "policy": {
            "tlp": "GREEN",
            "do_not_submit": False,
            "external_llm_allowed": True,
        },
    }
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="analyst-input-packs"
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    decision = ProductionRoutingDecisionV1(
        decision_id=uuid4(),
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        extraction_artifact_id=extraction.id,
        extraction_artifact_hash=extraction_sha256,
        relevance_artifact_id=relevance.id,
        relevance_artifact_hash=relevance_sha256,
        input_hash=hashlib.sha256(f"routing-{suffix}".encode()).hexdigest(),
        policy_version="routing-policy-v1",
        status=RoutingStatus.DECIDED,
        publication_kind=PublicationKind.ARTICLE,
        reason_code=RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        confirmed_ioc_count=1,
        contextual_ioc_count=0,
        ambiguous_ioc_count=0,
        critical_source_diagnostics=(),
        decided_at=_FIXED_NOW,
        decision_hash=hashlib.sha256(f"decision-{suffix}".encode()).hexdigest(),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        for artifact in (synthesis, extraction, relevance):
            await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.production_routing_decisions.append_and_adopt(
            decision, expected_version=run.version
        )
        await uow.commit()

    service = InvestigationBootstrapService(
        uow_factory=uow_factory,
        article_snapshots=ProductionArticleSnapshotAdapter(uow_factory, artifact_store),
        artifact_store=artifact_store,
        capabilities=BootstrapVtCapabilities(),
        clock=lambda: _FIXED_NOW,
    )
    inventory = await service.inventory(investigation.id)
    assert inventory.pinned_synthesis_sha256 == synthesis_sha256
    assert inventory.generated_at == _FIXED_NOW
    assert [ioc.value for ioc in inventory.seeds] == [_SEED_SHA256]
    assert inventory.seeds[0].sources
    assert inventory.seeds[0].sources[0].source_document_ids == (_SOURCE_DOCUMENT_ID,)

    plan = await service.plan(investigation.id)
    assert plan.state is BootstrapPlanState.AWAITING_G2

    snapshot = await service.execute_batch(
        investigation.id,
        idempotency_key="pg-bootstrap-run",
        actor_id="analyst-pg",
        executor_factory=lambda _uow: _NoopExecutor(),
    )
    assert snapshot.plan_state is BootstrapPlanState.AWAITING_G2

    stored = await service.latest_snapshot(investigation.id)
    assert stored is not None
    assert stored.snapshot_id == snapshot.snapshot_id
    assert stored.generated_at == _FIXED_NOW

    async with uow_factory() as uow:
        observations = await uow.evidence_observations.list_for_investigation(investigation.id)
    bootstrap_observations = [
        item
        for item in observations
        if item.source == BOOTSTRAP_SOURCE and item.object_type == BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
    ]
    assert len(bootstrap_observations) == 1
    assert bootstrap_observations[0].object_id == str(snapshot.snapshot_id)
    assert bootstrap_observations[0].observed_at == _FIXED_NOW

    # The same idempotency key returns the stored snapshot without a second row.
    replayed = await service.execute_batch(
        investigation.id,
        idempotency_key="pg-bootstrap-run",
        actor_id="analyst-pg",
        executor_factory=lambda _uow: _NoopExecutor(),
    )
    assert replayed == snapshot
    async with uow_factory() as uow:
        after = await uow.evidence_observations.list_for_investigation(investigation.id)
    assert (
        len(
            [
                item
                for item in after
                if item.source == BOOTSTRAP_SOURCE
                and item.object_type == BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
            ]
        )
        == 1
    )
