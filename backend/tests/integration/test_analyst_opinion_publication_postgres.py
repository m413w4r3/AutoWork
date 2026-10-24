from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from cti_app.api.analyst_opinion import router as analyst_opinion_router
from cti_app.api.investigation import router as investigation_router
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OperatorOpinionToolAdapter,
    OperatorProtocolFigureAttachmentBuilder,
)
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.network_protocol_model import render_protocol_model
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.application.typst_rendering import TypstRenderer
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
    ToolInvocationStatus,
)
from cti_app.domain.network_protocol import (
    NetworkExtractionLimitsV1,
    NetworkTransport,
    StreamDirection,
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
from cti_app.domain.protocol_model import (
    AssuranceLevel,
    MessageDefinitionV1,
    ProtocolDiagramKind,
    ProtocolEvidenceRefV1,
    ProtocolModelV1,
    ProtocolStateV1,
    ProtocolTransitionV1,
    ProtocolValidationStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.edition_codes import reserve_edition_code
from tests.test_protocol_diagrams_typst import (
    _D2_BINARY,
    _REPOSITORY_ROOT,
    _TYPST_BINARY,
    _compile_pdf,
    _install_workspace,
    _typst_binary,
)
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
        self._stored_blob_ids: dict[tuple[str, str], UUID] = {}

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is not None and len(self.blobs[blob_id]) > max_bytes:
            raise ValueError("Blob exceeds the requested read limit")
        return self.blobs[blob_id]

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        value = json.loads(await self.read_bytes(blob_id))
        assert isinstance(value, dict)
        return value

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        sha256 = hashlib.sha256(content).hexdigest()
        key = (bucket, sha256)
        if existing_id := self._stored_blob_ids.get(key):
            return existing_id
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=bucket,
            )
        )
        async with self._uow_factory() as uow:
            await uow.blobs.add(record)
            await uow.commit()
        self.blobs[record.id] = content
        self._stored_blob_ids[key] = record.id
        return record.id

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


class _TypstDraftPreviewRenderer:
    def __init__(self, *, media_assets: MediaAssetStore, root: Path) -> None:
        from cti_app.application.typst_rendering import TypstRenderer, load_template_bundle

        self._media_assets = media_assets
        self._root = root
        self._renderer = TypstRenderer()
        self._templates = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
        self.rendered_opinion = None
        self.rendered_synthesis: ProductionSynthesisV1 | None = None

    async def render_draft_preview(
        self,
        document,
        opinion,
        synthesis: ProductionSynthesisV1,
        *,
        infrastructure_graph=None,
        findings=(),
        evidence_observations=(),
        base_publication_identity=None,
    ) -> CompiledTypstDocument:
        from cti_app.application.typst_rendering import TypstDraftPreviewDocument

        del base_publication_identity
        self.rendered_opinion = opinion
        self.rendered_synthesis = synthesis
        source = self._renderer.render(
            TypstDraftPreviewDocument(
                document,
                opinion,
                synthesis,
                infrastructure_graph,
                findings,
                evidence_observations,
            ),
            self._templates,
            document_label="Aperçu d'avis PostgreSQL — test",
        )
        media_bytes = {
            reference.asset_id: await self._media_assets.read(reference.asset_id)
            for reference in source.media_refs
        }
        workspace = self._root / "workspace"
        _install_workspace(
            source,
            media_bytes,
            workspace,
            entrypoint="RENDERER/publication.typ",
        )
        return cast(
            CompiledTypstDocument,
            await _compile_pdf(
                typst_binary=_typst_binary(),
                workspace_root=workspace,
                font_root=self._root / "fonts",
                entrypoint="RENDERER/publication.typ",
            ),
        )


class _OperatorClientIdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            "operator:integration-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _OperatorInvocationService:
    def __init__(self, adapter: OperatorOpinionToolAdapter, investigation_id: UUID) -> None:
        self._adapter = adapter
        self._investigation_id = investigation_id

    async def invoke(
        self,
        *,
        tool_name: str,
        schema_version: str,
        arguments: dict[str, object],
        purpose: str,
        expected_version: int,
        idempotency_key: str,
    ):
        del purpose
        result = await self._adapter.execute(
            arguments,
            ToolExecutionContext(
                investigation_id=self._investigation_id,
                envelope=None,
                budget=InvestigationBudget(limits={}),
                idempotency_key=idempotency_key,
            ),
        )
        invocation = SimpleNamespace(
            id=uuid4(),
            tool_name=tool_name,
            schema_version=schema_version,
            status=result.status,
            version=1,
            response_refs=result.observation_refs,
        )
        return SimpleNamespace(
            investigation_id=self._investigation_id,
            working_state_version=expected_version + 1,
            idempotent_replay=False,
            invocation=invocation,
            job_id=None,
            error_code=result.error_code,
            output=dict(result.output),
        )


class _OperatorCompositionFactory:
    def __init__(self, adapter: OperatorOpinionToolAdapter) -> None:
        self._adapter = adapter

    def build(self, _uow, investigation_id: UUID, *, operator_mode: bool = False):
        assert operator_mode
        invocation = _OperatorInvocationService(self._adapter, investigation_id)
        return SimpleNamespace(operator_tool_invocations=invocation, advisor_tool=None)


def _documented_protocol_model(
    *, subject_id: UUID, investigation_id: UUID, source_observation: EvidenceObservation
) -> ProtocolModelV1:
    evidence = ProtocolEvidenceRefV1(
        source=f"documented:observation:{source_observation.id}",
        documented_ref_kind="observation",
        documented_ref_id=str(source_observation.id),
        documented_source_sha256=source_observation.sha256,
    )
    status = ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED
    return ProtocolModelV1(
        schema_version="ProtocolModelV1",
        subject_id=str(subject_id),
        investigation_id=investigation_id,
        family=None,
        cluster=None,
        family_assurance=AssuranceLevel.UNCONFIRMED,
        sample_sha256s=(),
        capture_sha256s=(),
        report_ids=(),
        model_version="documented-protocol-model.v1",
        protocol="Fixture handshake",
        transport=NetworkTransport.UNKNOWN,
        endpoints=(),
        client_server_orientation="unresolved",
        infection_state=None,
        conversations=(),
        messages=(
            MessageDefinitionV1(
                message_id="hello",
                name="ClientHello",
                ordinal=1,
                directions=(StreamDirection.UNRESOLVED,),
                min_length=None,
                max_length=None,
                field_ids=(),
                validation_status=status,
                evidence=(evidence,),
            ),
        ),
        fields=(),
        transitions=(
            ProtocolTransitionV1(
                transition_id="new-to-ready",
                from_state="new",
                to_state="ready",
                message_id="hello",
                trigger="ClientHello",
                hypotheses=(),
                validation_status=status,
                evidence=(evidence,),
            ),
        ),
        function_provenance=(),
        validation_status=status,
        verification_findings=(),
        test_plan=(),
        limits=NetworkExtractionLimitsV1(),
        limitations=("Documenté, non observé.",),
        extractor_versions=(("protocol-document", "1"),),
        states=(
            ProtocolStateV1("new", "New", status, (evidence,)),
            ProtocolStateV1("ready", "Ready", status, (evidence,)),
        ),
        documentary_evidence=(evidence,),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    _D2_BINARY is None or _TYPST_BINARY is None or shutil.which("pdftotext") is None,
    reason="D2, Typst, and pdftotext are required for the real protocol preview",
)
async def test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5(
    migrated_postgres_url: str, tmp_path: Path
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
    store = _PostgresArtifactStore(uow_factory, blobs)
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
    protocol_model = _documented_protocol_model(
        subject_id=subject.id,
        investigation_id=investigation.id,
        source_observation=observation,
    )
    protocol_model_blob_id = await store.put_bytes(
        protocol_model.canonical_json_bytes(),
        bucket="network-protocol-models",
        mime_type="application/json",
    )
    protocol_render = render_protocol_model(protocol_model)
    protocol_render_blob_id = await store.put_bytes(
        protocol_render.canonical_json_bytes(),
        bucket="protocol-render-observations",
        mime_type="application/json",
    )
    protocol_model_observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=protocol_model_blob_id,
        sha256=protocol_model.canonical_sha256,
        source="network.protocol.document",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="protocol_model",
        object_id=protocol_model.canonical_sha256,
        limitations=("Documented protocol model; not a network observation.",),
    )
    protocol_render_observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=protocol_render_blob_id,
        sha256=protocol_render.canonical_sha256,
        source="network.protocol.render",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="protocol_diagram_specs",
        object_id=protocol_render.canonical_sha256,
        limitations=("Semantic protocol diagrams; no free-form D2 source was executed.",),
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
            await uow.evidence_observations.append_if_absent(protocol_model_observation)
            await uow.evidence_observations.append_if_absent(protocol_render_observation)
            await uow.investigation_findings.append_version(finding, expected_previous_id=None)
            await uow.analyst_decisions.append(approval_decision)
            await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
            await uow.commit()

        opinion_service = AnalystOpinionService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
            author_id="operator:claude-client",
        )
        operator_adapter = OperatorOpinionToolAdapter(
            service=opinion_service,
            investigations=SimpleNamespace(),
            article_snapshots=SimpleNamespace(),
            protocol_figure_builder=OperatorProtocolFigureAttachmentBuilder(
                uow_factory=uow_factory,
                artifact_store=store,  # type: ignore[arg-type]
            ),
        )
        preview_renderer = _TypstDraftPreviewRenderer(
            media_assets=MediaAssetStore(store, uow_factory),  # type: ignore[arg-type]
            root=tmp_path / "opinion-preview",
        )
        preview_app = FastAPI()
        preview_app.include_router(investigation_router)
        preview_app.include_router(analyst_opinion_router)
        preview_app.state.uow_factory = uow_factory
        preview_app.state.production_artifact_store = store
        preview_app.state.identity_provider = _OperatorClientIdentityProvider()
        preview_app.state.investigation_composition_factory = _OperatorCompositionFactory(
            operator_adapter
        )
        preview_app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
            renderer=preview_renderer,
        )
        protocol_figure_selection = {
            "protocol_model_blob_id": str(protocol_model_blob_id),
            "render_observation_id": str(protocol_render_observation.id),
            "views": [item.value for item in ProtocolDiagramKind],
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=preview_app), base_url="http://test"
        ) as client:
            composed = await client.post(
                f"/api/investigations/{investigation.id}/operator/tools/opinion.compose",
                json={
                    "schema_version": "operator-opinion.v1",
                    "arguments": {
                        "action": "compose",
                        "selection": {
                            "finding_ids": [],
                            "claim_ids": [],
                            "block_types": ["infection_chain_and_capabilities"],
                        },
                        "protocol_figures": [protocol_figure_selection],
                    },
                    "purpose": "Compose the cited draft with its rendered protocol model.",
                    "expected_version": 1,
                },
                headers={"Idempotency-Key": f"operator-opinion-compose-{suffix}"},
            )
        assert composed.status_code == 200, composed.text
        operator_result = composed.json()
        assert operator_result["invocation"]["status"] == ToolInvocationStatus.SUCCEEDED.value
        assert {
            str(protocol_model_observation.id),
            str(protocol_render_observation.id),
        }.issubset(set(operator_result["invocation"]["response_refs"]))
        draft = (await opinion_service.get_draft(investigation.id)).opinion
        assert draft is not None
        assert draft.status is AnalystOpinionStatus.DRAFT
        assert {item.kind for item in draft.protocol_figures} == set(ProtocolDiagramKind)
        assert all(
            "documenté, non observé" in item.caption.casefold() for item in draft.protocol_figures
        )
        revise_blocks = [
            {
                "block_type": item.block_type.value,
                "text": item.text,
                "finding_refs": [str(ref) for ref in item.finding_refs],
                "evidence_refs": [str(ref) for ref in item.evidence_refs],
                "qualification": item.qualification.value,
                "certainty": item.certainty,
            }
            for item in draft.blocks
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=preview_app), base_url="http://test"
        ) as client:
            revised = await client.post(
                f"/api/investigations/{investigation.id}/operator/tools/opinion.revise",
                json={
                    "schema_version": "operator-opinion.v1",
                    "arguments": {
                        "action": "revise",
                        "opinion_id": str(draft.id),
                        "expected_opinion_version": draft.version,
                        "expected_opinion_sha256": draft.content_sha256,
                        "blocks": revise_blocks,
                        "unresolved_points": list(draft.unresolved_points),
                        "protocol_figures": [protocol_figure_selection],
                    },
                    "purpose": "Retain all three protocol views on the revised draft.",
                    "expected_version": 2,
                },
                headers={"Idempotency-Key": f"operator-opinion-revise-{suffix}"},
            )
        assert revised.status_code == 200, revised.text
        assert revised.json()["invocation"]["status"] == ToolInvocationStatus.SUCCEEDED.value
        draft = (await opinion_service.get_draft(investigation.id)).opinion
        assert draft is not None and draft.version == 2
        assert {item.kind for item in draft.protocol_figures} == set(ProtocolDiagramKind)

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
        assert preview_renderer.rendered_opinion is not None
        assert {item.kind for item in preview_renderer.rendered_opinion.protocol_figures} == set(
            ProtocolDiagramKind
        )
        assert preview_renderer.rendered_synthesis == synthesis
        preview_path = tmp_path / "operator-protocol-opinion-preview.pdf"
        preview_path.write_bytes(preview.content)
        extracted = await asyncio.to_thread(
            subprocess.run,
            ("pdftotext", "-layout", str(preview_path), "-"),
            capture_output=True,
            check=True,
            timeout=30,
        )
        preview_text = " ".join(extracted.stdout.decode("utf-8", errors="replace").split())
        assert preview_text.count("Niveau de preuve") >= 3
        assert preview_text.count("ARTICLE_DOCUMENTED") >= 3
        assert preview_text.count("documenté, non observé") >= 3
        assert all(
            title in preview_text.casefold()
            for title in ("séquence réseau", "machine à états", "format de message")
        )
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
        assert {item.kind for item in parsed.analyst_opinion.protocol_figures} == set(
            ProtocolDiagramKind
        )

        render_service = PublicationRenderService(
            uow_factory=uow_factory,
            artifact_store=cast(ProductionArtifactStore, store),
            media_asset_store=MediaAssetStore(cast(ProductionArtifactStore, store), uow_factory),
            renderer=TypstRenderer(),
            compiler=TypstSubprocessCompiler(binary=_typst_binary()),
            chp_typst_root=_REPOSITORY_ROOT / "chpTypst",
            font_bundle_root=Path(
                os.environ.get("FONT_BUNDLE_ROOT") or _REPOSITORY_ROOT / "chpTypst"
            ),
            typst_fonts_lock_path=_REPOSITORY_ROOT / "infra" / "typst-fonts.lock",
            graph_diagram_compiler=D2DiagramCompiler(binary=cast(str, _D2_BINARY)),
        )
        base_render = await render_service.render_pdf(base_artifact.id)
        augmented_render = await render_service.render_pdf(persisted_augmented.id)
        base_lineage = await render_service.lineage_manifest_for_render(base_render)
        augmented_lineage = await render_service.lineage_manifest_for_render(augmented_render)

        assert base_lineage["nature"] == "base_publication"
        assert "actors" not in base_lineage
        actors = augmented_lineage["actors"]
        async with uow_factory() as uow:
            approval_decisions = await uow.analyst_decisions.list_for_investigation(
                parsed.provenance.investigation_id
            )
        approval_decision = max(
            (
                decision
                for decision in approval_decisions
                if decision.decision_type is AnalystDecisionType.NOTE_APPROVE
                and decision.target_type is AnalystDecisionTargetType.NOTE
                and decision.target_id == parsed.provenance.opinion_id
            ),
            key=lambda decision: (decision.occurred_at, decision.id),
        )
        assert approval_decision.actor_id == parsed.provenance.reviewer_id
        assert actors["reviewer"] == {
            "actor_id": parsed.provenance.reviewer_id,
            "role": "reviewer",
            "decision_label": "opinion_approved",
            "investigation_id": str(parsed.provenance.investigation_id),
            "opinion_id": str(parsed.provenance.opinion_id),
            "opinion_version": parsed.provenance.opinion_version,
            "opinion_content_sha256": parsed.provenance.opinion_content_sha256,
            "decision_id": str(approval_decision.id),
            "occurred_at": approval_decision.occurred_at.isoformat(),
            "decision_reference_status": "verified",
        }
        assert actors["approver"] == {
            "actor_id": parsed.provenance.approver_id,
            "role": "approver",
            "decision_label": "publication_augmented",
            "base_publication": {
                "artifact_id": str(base_artifact.id),
                "version": base_artifact.version,
                "sha256": parsed.provenance.base_publication_sha256,
            },
            "augmented_publication": {
                "artifact_id": str(persisted_augmented.id),
                "version": persisted_augmented.version,
                "sha256": hashlib.sha256(v6_bytes).hexdigest(),
            },
        }
        pdftotext = shutil.which("pdftotext")
        assert pdftotext is not None
        assert augmented_render.output_blob_id is not None
        augmented_pdf_path = tmp_path / "augmented-publication.pdf"
        augmented_pdf_path.write_bytes(await store.read_bytes(augmented_render.output_blob_id))
        extracted_pdf = await asyncio.to_thread(
            subprocess.run,
            (pdftotext, "-layout", str(augmented_pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=30,
        )
        extracted_pdf_text = extracted_pdf.stdout.decode("utf-8", errors="replace")
        assert parsed.provenance.reviewer_id not in extracted_pdf_text
        assert parsed.provenance.approver_id not in extracted_pdf_text
    finally:
        await engine.dispose()
