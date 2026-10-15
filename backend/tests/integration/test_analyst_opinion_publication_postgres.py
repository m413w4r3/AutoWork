from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from cti_app.api.analyst_opinion import router as analyst_opinion_router
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.domain.analyst_opinion import AnalystOpinionStatus
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    TLPLevel,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    production_synthesis_to_json,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_publication_document_v6 import _base_v5

pytestmark = pytest.mark.integration


class _PostgresArtifactStore:
    def __init__(
        self,
        uow_factory,
        blobs: dict[UUID, bytes],
    ) -> None:
        self._uow_factory = uow_factory
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.blobs[blob_id]

    async def store_stage_payloads(self, *, canonical: dict[str, object]):
        data = ProductionArtifactStore.canonical_json_bytes(canonical)
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                mime_type="application/json",
                logical_bucket="analyst-opinion-test",
            )
        )
        async with self._uow_factory() as uow:
            await uow.blobs.add(record)
            await uow.commit()
        self.blobs[record.id] = data
        return None, record.id, None


class _DraftPreviewRenderer:
    def __init__(self) -> None:
        self.rendered_synthesis: ProductionSynthesisV1 | None = None

    async def render_draft_preview(
        self,
        document,
        opinion,
        synthesis: ProductionSynthesisV1,
    ) -> CompiledTypstDocument:
        self.rendered_synthesis = synthesis
        content = b"%PDF-1.4\npostgres preview fixture\n"
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            compiler="typst",
            compiler_version="0.15.1",
        )


@pytest.mark.asyncio
async def test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    base_document = _base_v5()
    subject_id = uuid4()
    base_document = replace(
        base_document,
        document=replace(base_document.document, subject_id=subject_id),
    )
    base_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_document)
    )
    _, _, _, synthesis = _canonical_inputs()
    synthesis = replace(
        synthesis,
        subject_id=subject_id,
        title=base_document.document.title,
        publication_language=base_document.document.publication_language,
    )
    synthesis_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_synthesis_to_json(synthesis)
    )
    observation_bytes = b"harmless integration observation evidence"
    blobs: dict[UUID, bytes] = {}
    suffix = uuid4().hex
    edition = Edition(
        country=f"Analyst opinion {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        id=subject_id,
        edition_id=edition.id,
        title="Analyst opinion publication fixture",
        slug=f"analyst-opinion-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    base_blob_id = uuid4()
    synthesis_blob_id = uuid4()
    observation_blob_id = uuid4()
    base_blob = BlobRecord(
        id=base_blob_id,
        descriptor=BlobDescriptor(
            sha256=hashlib.sha256(base_bytes).hexdigest(),
            size=len(base_bytes),
            mime_type="application/json",
            logical_bucket="analyst-opinion-base",
        ),
    )
    synthesis_blob = BlobRecord(
        id=synthesis_blob_id,
        descriptor=BlobDescriptor(
            sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
            size=len(synthesis_bytes),
            mime_type="text/plain",
            logical_bucket="analyst-opinion-synthesis",
        ),
    )
    observation_blob = BlobRecord(
        id=observation_blob_id,
        descriptor=BlobDescriptor(
            sha256=hashlib.sha256(observation_bytes).hexdigest(),
            size=len(observation_bytes),
            mime_type="text/plain",
            logical_bucket="analyst-opinion-observation",
        ),
    )
    blobs.update(
        {
            base_blob_id: base_bytes,
            synthesis_blob_id: synthesis_bytes,
            observation_blob_id: observation_bytes,
        }
    )
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(synthesis_bytes).hexdigest(),
        canonical_blob_id=synthesis_blob_id,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source="fixture-static-analysis",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id=hashlib.sha256(b"harmless binary identity").hexdigest(),
        limitations=("Static observation only.",),
    )
    finding = InvestigationFinding(
        investigation_id=investigation.id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The fixture contains a second-stage loading capability.",
        observation_refs=(observation.id,),
        limitations=("Execution was not observed.",),
    )
    approval_decision = AnalystDecision(
        investigation_id=investigation.id,
        decision_type=AnalystDecisionType.PIVOT_APPROVE,
        target_type=AnalystDecisionTargetType.PIVOT,
        target_id=uuid4(),
        actor_id="editor:integration",
        reason="Local test envelope approval",
        correlation_id=f"opinion-envelope-{suffix}",
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=(),
        budget=InvestigationBudget(limits={}),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.CLEAR, do_not_submit=False),
        approval_decision_id=approval_decision.id,
        approval_actor_id="editor:integration",
    )
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            await uow.production_runs.add(run)
            await uow.blobs.add(base_blob)
            await uow.blobs.add(synthesis_blob)
            await uow.blobs.add(observation_blob)
            await uow.production_artifacts.append(synthesis_artifact)
            await uow.production_artifacts.append(base_artifact)
            await uow.analyst_investigations.add(investigation)
            await uow.evidence_observations.append_if_absent(observation)
            await uow.investigation_findings.append_version(finding, expected_previous_id=None)
            await uow.analyst_decisions.append(approval_decision)
            await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
            await uow.commit()

        store = _PostgresArtifactStore(uow_factory, blobs)
        opinion_service = AnalystOpinionService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
        )
        draft = await opinion_service.compose_from_findings(investigation.id)
        assert draft.status is AnalystOpinionStatus.DRAFT

        preview_renderer = _DraftPreviewRenderer()
        preview_app = FastAPI()
        preview_app.include_router(analyst_opinion_router)
        preview_app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
            renderer=preview_renderer,
        )

        async def canonical_state_snapshot() -> dict[str, tuple[tuple[object, ...], ...]]:
            async with engine.connect() as connection:

                async def rows(
                    statement: str,
                    parameters: dict[str, object] | None = None,
                ) -> tuple[tuple[object, ...], ...]:
                    result = await connection.execute(text(statement), parameters or {})
                    return tuple(tuple(row) for row in result.all())

                return {
                    "artifacts": await rows(
                        "SELECT * FROM production_artifacts WHERE production_run_id = :run_id "
                        "ORDER BY id",
                        {"run_id": run.id},
                    ),
                    "blobs": await rows("SELECT * FROM blobs ORDER BY id"),
                    "manifests": await rows("SELECT * FROM publication_manifests ORDER BY id"),
                    "manifest_entries": await rows(
                        "SELECT * FROM publication_manifest_entries ORDER BY manifest_id, position"
                    ),
                    "renders": await rows("SELECT * FROM publication_renders ORDER BY id"),
                    "opinions": await rows(
                        "SELECT * FROM analyst_opinion_versions WHERE opinion_id = :opinion_id "
                        "ORDER BY version",
                        {"opinion_id": draft.id},
                    ),
                    "run": await rows(
                        "SELECT * FROM production_runs WHERE id = :run_id",
                        {"run_id": run.id},
                    ),
                    "investigation": await rows(
                        "SELECT * FROM analyst_investigations WHERE id = :investigation_id",
                        {"investigation_id": investigation.id},
                    ),
                    "envelope": await rows(
                        "SELECT * FROM investigation_envelopes WHERE investigation_id = "
                        ":investigation_id",
                        {"investigation_id": investigation.id},
                    ),
                }

        state_before_preview = await canonical_state_snapshot()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=preview_app), base_url="http://test"
        ) as client:
            preview = await client.get(
                f"/api/opinions/{draft.id}/preview.pdf",
                params={
                    "version": draft.version,
                    "opinion_sha256": draft.content_sha256,
                    "base_publication_sha256": draft.base_publication_sha256,
                },
            )
        assert preview.status_code == 200
        assert preview.headers["x-opinion-status"] == "draft-unapproved"
        assert preview.headers["x-pdf-sha256"] == hashlib.sha256(preview.content).hexdigest()
        assert preview_renderer.rendered_synthesis == synthesis
        assert await canonical_state_snapshot() == state_before_preview

        approved = await opinion_service.approve(
            draft.id,
            draft.version,
            draft.content_sha256,
            "editor:integration",
            "The finding and its reservation are supported.",
            idempotency_key=f"approve-{suffix}",
        )
        augmentation = PublicationAugmentationArtifactService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
        )
        publication_service = AnalystPublicationService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
            augmentation_service=augmentation,
        )
        augmented = await publication_service.publish_augmented_article(
            approved.id,
            approved.version,
            "editor:integration",
            approved.base_publication_sha256,
        )

        async with uow_factory() as uow:
            persisted_base = await uow.production_artifacts.get(base_artifact.id)
            persisted_augmented = await uow.production_artifacts.get(augmented.id)
            versions = await uow.production_artifacts.list_for_run(run.id)
            history_items = []
            for version in range(1, approved.version + 1):
                item = await uow.analyst_opinions.get(approved.id, version=version)
                if item is not None:
                    history_items.append(item)
            opinion_history = tuple(history_items)
        assert persisted_base == base_artifact
        assert persisted_augmented == augmented
        assert persisted_augmented is not None
        assert persisted_augmented.stage is ProductionArtifactStage.PUBLICATION
        assert persisted_augmented.status is ProductionArtifactStatus.VERIFIED
        assert persisted_augmented.metadata["previous_publication_artifact_id"] == str(
            base_artifact.id
        )
        publication_versions = [
            item for item in versions if item.stage is ProductionArtifactStage.PUBLICATION
        ]
        assert len(publication_versions) == 2
        assert blobs[base_blob_id] == base_bytes
        assert len(opinion_history) == approved.version
        v6_bytes = await store.read_bytes(persisted_augmented.canonical_blob_id)
        parsed = publication_document_from_json(json.loads(v6_bytes))
        assert isinstance(parsed, PublicationDocumentV6)
        assert parsed.base_publication == base_document
        assert parsed.analyst_opinion is not None
        assert parsed.analyst_opinion.id == approved.id
        assert parsed.analyst_opinion.version == approved.version
    finally:
        await engine.dispose()
