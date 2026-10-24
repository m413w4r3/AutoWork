"""Serve the J2 analyst-opinion browser scenario on isolated PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import signal
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import uvicorn
from fastapi import HTTPException
from sqlalchemy.engine import make_url

from cti_app.api.analyst_opinion import router as opinion_router
from cti_app.api.investigation import router as investigation_router
from cti_app.api.production import router as production_router
from cti_app.api.subjects import router as subjects_router
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
    register_publication_jobs,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseRematerializationService,
)
from cti_app.application.edition_rendering import (
    EDITION_RENDERER_MANIFEST,
    EditionRenderService,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.edition_workspace import EditionWorkspaceMaterializer
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.subjects import SubjectService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_execution import load_typst_render_bundle_snapshots
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import CandidateTopic, SourceCandidate, SourceRole
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.domain.selection import SelectionAction, SelectionDecision, SubjectDiscoveryOrigin
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.conftest import _alembic_config, _create_database, _drop_database
from tests.integration.test_edition_multibatch_publication_postgres import (
    _compose_three_runs,
)
from tests.integration.test_investigation_vertical_postgres import (
    _IOC,
    _SEED_HASH,
    _composition_factory,
    _decision,
    _HashCheckedVirusTotal,
    _settings,
    _VerticalScriptedTransport,
)
from tests.shodan_support import FakeShodanPort
from tests.workflow_emulator.driver import build_emulator_app
from tests.workflow_emulator.investigation_support import (
    DEFAULT_SAMPLE_BYTES as _S2_BYTES,
)
from tests.workflow_emulator.investigation_support import (
    DEFAULT_SAMPLE_HASH as _S2_HASH,
)
from tests.workflow_emulator.investigation_support import SyntheticSampleMaterial
from tests.workflow_emulator.test_w11_analyst_opinion_repin import _article_v5


class _QueuedBrowserDispatcher:
    """Hold real durable jobs until the browser asks the harness to execute them."""

    def __init__(self) -> None:
        self._pending: list[UUID] = []
        self._known: set[UUID] = set()

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        if job_id not in self._known:
            self._pending.append(job_id)
            self._known.add(job_id)

    def pop(self) -> UUID | None:
        if not self._pending:
            return None
        job_id = self._pending.pop(0)
        self._known.discard(job_id)
        return job_id


class _CapturedJobFailures(logging.Handler):
    """Expose JobExecutor tracebacks only through this loopback test harness."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


async def _attach_discovery_origins(
    *, uow_factory: Any, edition_id: UUID, subjects: Mapping[str, Subject]
) -> None:
    """Give the production board canonical selected-subject lineage for A/B/C."""
    created_at = datetime.now(UTC)
    ordered_subjects = tuple((label, subjects[label]) for label in ("A", "B", "C"))
    subject_ids = tuple(subject.id for _, subject in ordered_subjects)
    merge_run = DiscoveryMergeRun(
        edition_id=edition_id,
        parent_snapshot_id=None,
        intake_id=None,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        prompt_version="1",
        policy_version="1",
        blocking_version="1",
        merge_input_hash=hashlib.sha256(b"j2-browser-discovery-seed").hexdigest(),
        handle_map={},
        included_subject_ids=subject_ids,
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.VALID,
        created_at=created_at,
    )
    snapshot_subjects = tuple(
        DiscoverySubject(
            subject_id=subject.id,
            candidate=CandidateTopic(
                title=subject.title,
                summary=f"Sélection synthétique du sujet {label} pour la fixture navigateur J2.",
                novelty=f"Sujet {label} isolé pour le scénario multibatch J2.",
                technical_potential=1,
                uncertainties=(),
                relevance_reasons=("fixture navigateur J2",),
                actors=(),
                campaigns=(),
                malware=(),
                cves=(),
                victims=(),
                sectors=(),
                countries=(),
                likely_artifacts=(),
                sources=[
                    SourceCandidate(
                        url=f"https://article-{label.lower()}.j2.example.invalid/report",
                        title=f"Source synthétique {label}",
                        publisher="J2 browser fixture",
                        role=SourceRole.PRIMARY,
                        published_at=date(2026, 10, 13),
                        tlp=TLP.GREEN,
                        sensitivity="synthetic",
                        external_llm_allowed=False,
                    )
                ],
                tlp=TLP.GREEN,
                sensitivity="synthetic",
                external_llm_allowed=False,
            ),
            member_references=(),
            created_at=created_at,
        )
        for label, subject in ordered_subjects
    )
    snapshot = DiscoverySnapshot(
        edition_id=edition_id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=merge_run.id,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        subjects=snapshot_subjects,
        snapshot_hash=hashlib.sha256(
            b"j2-browser-snapshot:" + b":".join(subject_id.bytes for subject_id in subject_ids)
        ).hexdigest(),
        is_active=True,
        created_at=created_at,
    )
    identities = [
        DiscoverySubjectIdentity(
            edition_id=edition_id,
            origin_key=f"subject:{subject.id}",
            created_by_merge_run_id=merge_run.id,
            id=subject.id,
            created_at=created_at,
        )
        for _, subject in ordered_subjects
    ]

    async with uow_factory() as uow:
        await uow.discovery_merge_runs.add_if_absent(merge_run)
        await uow.discovery_snapshots.append(snapshot)
        await uow.discovery_subject_identities.add_many_if_absent(identities)
        for _, subject in ordered_subjects:
            decision = SelectionDecision(
                edition_id=edition_id,
                discovery_subject_id=subject.id,
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                action=SelectionAction.SELECT,
                subject_id=subject.id,
                actor_id="workflow-browser-seed",
                correlation_id=f"j2-selection-{subject.id}",
                idempotency_key=f"j2-selection-{subject.id}",
                occurred_at=created_at,
            )
            await uow.selection_decisions.append(decision)
            await uow.subject_discovery_origins.add(
                SubjectDiscoveryOrigin(
                    subject_id=subject.id,
                    edition_id=edition_id,
                    discovery_subject_id=subject.id,
                    selection_decision_id=decision.id,
                    selected_snapshot_id=snapshot.id,
                    selected_snapshot_version=snapshot.version,
                    created_at=created_at,
                )
            )
        await uow.commit()


def _is_loopback_host(host: str | None) -> bool:
    if host in (None, "") or host.startswith("/"):
        return True
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _scripted_responses(seed_observation_id: UUID) -> list[dict[str, Any]]:
    return [
        _decision(
            "Read the article's documented seed report first.",
            tool_call={
                "tool_name": "vt.file_report",
                "schema_version": "1",
                "arguments": {"file_hash": _SEED_HASH},
                "purpose": "Review the documented article seed.",
            },
        ),
        _decision(
            "Use VT intelligence search to find an article-absent candidate.",
            tool_call={
                "tool_name": "vt.intelligence_search",
                "schema_version": "1",
                "arguments": {
                    "query": "type:peexe",
                    "justification": "Find candidates related to the documented seed.",
                    "evidence_refs": [str(seed_observation_id)],
                },
                "purpose": "Pivot from the cited seed into VT Intelligence Search.",
            },
        ),
        _decision(
            "Acquire the article-absent candidate under the approved envelope.",
            tool_call={
                "tool_name": "vt.download_sample",
                "schema_version": "1",
                "arguments": {
                    "sha256": _S2_HASH,
                    "purpose": "Inspect S2 returned by the cited VT pivot.",
                    "evidence_refs": [str(seed_observation_id)],
                },
                "purpose": "Download harmless S2 bytes into quarantine for review.",
            },
        ),
        _decision(
            "Check the article IOC against Shodan's passive index.",
            tool_call={
                "tool_name": "shodan.host",
                "schema_version": "1",
                "arguments": {"ip": _IOC},
                "purpose": "Check the article IOC in Shodan's passive host index.",
            },
        ),
    ]


async def _attach_investigation_to_article_a(
    *,
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    edition_id: UUID,
    subject_id: UUID,
) -> dict[str, Any]:
    async with uow_factory() as uow:
        runs = await uow.production_runs.list_for_subject(subject_id)
        if len(runs) != 1:
            raise RuntimeError(f"Expected one production run for Article A, found {len(runs)}")
        run: ProductionRun = runs[0]
        prior = await uow.production_artifacts.list_for_run(run.id)
        prior_publications = [
            item for item in prior if item.stage is ProductionArtifactStage.PUBLICATION
        ]
        base_publication = max(prior_publications, key=lambda item: item.version)
        composition = await uow.edition_compositions.get_for_edition(edition_id)
        if composition is None:
            raise RuntimeError("The multibatch fixture has no initial composition")

    base_document = _article_v5(subject_id, "A")
    publication_payload = serialize_publication_document(base_document)
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(publication_payload)
    publication_blob_id = await artifact_store.put_bytes(
        publication_bytes,
        bucket="j2-browser-publication-v5",
        mime_type="application/json",
    )
    base_publication_v5 = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=base_publication.version + 1,
        input_hash=hashlib.sha256(publication_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=publication_blob_id,
    )

    synthesis_payload = {
        "title": "Article A — synthèse vérifiée de la fixture J2",
        "subject_id": str(subject_id),
        "lead": [
            {
                "text": (
                    f"No exposed services were observed for {_IOC}; "
                    f"the documented seed sample is {_SEED_HASH}."
                )
            }
        ],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload,
        bucket="j2-browser-synthesis",
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(max_cycles=12, max_vt_read_units=24, max_hits_acquired=1),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [],
        "file_indicators": [{"value": _SEED_HASH}],
        "policy": {
            "tlp": "green",
            "do_not_submit": False,
            "external_llm_allowed": True,
        },
    }
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload,
        bucket="analyst-input-packs",
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )

    seed_bytes = b"Synthetic seed evidence only; no external request is made."
    seed_blob_id = await artifact_store.put_bytes(
        seed_bytes,
        bucket="j2-browser-seed-evidence",
        mime_type="application/octet-stream",
    )
    seed_observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=seed_blob_id,
        sha256=hashlib.sha256(seed_bytes).hexdigest(),
        source="synthetic-article-seed",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id=_SEED_HASH,
        limitations=("Synthetic fixture evidence only.",),
    )

    async with uow_factory() as uow:
        await uow.production_artifacts.append(synthesis)
        await uow.production_artifacts.append(base_publication_v5)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.evidence_observations.append_if_absent(seed_observation)
        await uow.commit()

    async with uow_factory() as uow:
        blob = await uow.blobs.get(publication_blob_id)
        current = await uow.edition_compositions.get_for_edition(edition_id)
    if blob is None or current is None:
        raise RuntimeError("The Article A V5 seed did not persist")
    entries = tuple(
        replace(
            entry,
            artifact_id=base_publication_v5.id,
            artifact_version=base_publication_v5.version,
            artifact_input_hash=base_publication_v5.input_hash,
            document_sha256=blob.descriptor.sha256,
        )
        if entry.subject_id == subject_id
        else entry
        for entry in current.entries
    )
    updated_composition = await EditionCompositionService(uow_factory).replace_entries(
        edition_id,
        entries,
        expected_version=current.version,
        idempotency_key=f"j2-seed-a-v5-{uuid4()}",
        actor_id="workflow-browser-seed",
        correlation_id=f"j2-seed-a-v5-{uuid4()}",
    )
    return {
        "run_id": str(run.id),
        "investigation_id": str(investigation.id),
        "synthesis_artifact_id": str(synthesis.id),
        "synthesis_sha256": synthesis_sha256,
        "base_publication_artifact_id": str(base_publication_v5.id),
        "base_publication_version": base_publication_v5.version,
        "base_publication_sha256": hashlib.sha256(publication_bytes).hexdigest(),
        "seed_observation_id": str(seed_observation.id),
        "initial_composition_version": updated_composition.version,
    }


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    server: uvicorn.Server | None = None

    def request_termination() -> None:
        if server is None:
            shutdown_requested.set()
        else:
            server.should_exit = True

    loop.add_signal_handler(signal.SIGINT, request_termination)
    loop.add_signal_handler(signal.SIGTERM, request_termination)

    admin_dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        raise RuntimeError("TEST_POSTGRES_ADMIN_DSN is required for J2-browser-investigation")
    admin_url = make_url(admin_dsn)
    if not _is_loopback_host(admin_url.host):
        raise RuntimeError(
            "J2-browser-investigation requires TEST_POSTGRES_ADMIN_DSN to target local PostgreSQL"
        )

    state_value = os.environ.get("WORKFLOW_BROWSER_STATE")
    if not state_value:
        raise RuntimeError("WORKFLOW_BROWSER_STATE is required for J2-browser-investigation")
    state_path = Path(state_value)
    try:
        port = int(os.environ.get("WORKFLOW_BROWSER_API_PORT", "18766"))
    except ValueError as exc:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be between 1 and 65535")

    database_name = f"cti_j2_browser_{uuid4().hex}"
    database_created = False
    engine = None
    try:
        await _create_database(admin_dsn, database_name)
        database_created = True
        if shutdown_requested.is_set():
            return
        database_dsn = admin_url.set(database=database_name).render_as_string(hide_password=False)
        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(database_dsn), "head")
        if shutdown_requested.is_set():
            return

        engine = create_postgres_engine(database_dsn)
        session_factory = create_session_factory(engine)

        def uow_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(session_factory)

        with tempfile.TemporaryDirectory(prefix="workflow-browser-j2-") as blob_directory:
            edition, artifact_store, subjects, _batches = await _compose_three_runs(
                uow_factory,
                Path(blob_directory),
                production_order=("A", "B", "C"),
                composition_order=("A", "B", "C"),
            )
            await _attach_discovery_origins(
                uow_factory=uow_factory,
                edition_id=edition.id,
                subjects=subjects,
            )
            article_a = await _attach_investigation_to_article_a(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                edition_id=edition.id,
                subject_id=subjects["A"].id,
            )

            typst_binary = os.environ.get("TYPST_BINARY", "typst")
            default_template_root, default_font_root, fonts_lock = typst_bundle_paths()
            template_root = Path(os.environ.get("CHP_TYPST_ROOT", str(default_template_root)))
            font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(default_font_root)))
            load_typst_render_bundle_snapshots(
                chp_typst_root=template_root,
                font_bundle_root=font_root,
                typst_fonts_lock_path=fonts_lock,
                manifest_name=EDITION_RENDERER_MANIFEST,
            )
            render_service = EditionRenderService(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                media_asset_store=MediaAssetStore(artifact_store, uow_factory),
                renderer=EditionTypstRenderer(),
                compiler=TypstSubprocessCompiler(binary=typst_binary),
                chp_typst_root=template_root,
                font_bundle_root=font_root,
                typst_fonts_lock_path=fonts_lock,
            )
            assembly_service = EditionAssemblyService(uow_factory, artifact_store)

            initial_composition_version = int(article_a["initial_composition_version"])
            historic_accept = await EditionPublicationService(
                uow_factory,
                artifact_store,
            ).accept(
                edition.id,
                actor_id="workflow-browser-seed",
                expected_composition_version=initial_composition_version,
                correlation_id=f"j2-initial-release-{edition.id}",
            )
            historic_release = await assembly_service.assemble(historic_accept.manifest.id)
            historic_render = await render_service.render_pdf(historic_release.id)
            if historic_render.output_blob_id is None or historic_render.output_sha256 is None:
                raise RuntimeError(
                    f"J2 initial Typst render did not produce a PDF: {historic_render}"
                )
            historic_pdf = await artifact_store.read_bytes(historic_render.output_blob_id)
            if hashlib.sha256(historic_pdf).hexdigest() != historic_render.output_sha256:
                raise RuntimeError("J2 initial PDF SHA-256 does not match its persisted render")
            async with uow_factory() as uow:
                historic_manifest_blob_id = await uow.publication_manifests.get_blob_id(
                    historic_accept.manifest.id
                )
            if historic_manifest_blob_id is None:
                raise RuntimeError("J2 initial manifest blob was not persisted")
            historic_manifest_bytes = await artifact_store.read_bytes(historic_manifest_blob_id)
            historic_document_bytes = await artifact_store.read_bytes(
                historic_release.edition_document_blob_id
            )
            history = {
                "manifest_id": str(historic_accept.manifest.id),
                "manifest_sha256": hashlib.sha256(historic_manifest_bytes).hexdigest(),
                "manifest_content_sha256": historic_accept.manifest.content_sha256,
                "release_id": str(historic_release.id),
                "edition_document_sha256": historic_release.edition_document_sha256,
                "edition_document_blob_sha256": hashlib.sha256(historic_document_bytes).hexdigest(),
                "render_id": str(historic_render.id),
                "pdf_sha256": historic_render.output_sha256,
            }

            vt = _HashCheckedVirusTotal(include_candidate=True)
            shodan = FakeShodanPort()
            material = SyntheticSampleMaterial(
                seed_hash=_SEED_HASH,
                sample_bytes=_S2_BYTES,
                sample_hash=_S2_HASH,
                ip=_IOC,
            )
            transport = _VerticalScriptedTransport(
                _scripted_responses(UUID(article_a["seed_observation_id"])),
                uow_factory=uow_factory,
                investigation_id=UUID(article_a["investigation_id"]),
                material=material,
                include_external_pivot=True,
            )
            investigation_factory = _composition_factory(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                settings=_settings(providers=True, local_analysis=True),
                transport=transport,
                virustotal=vt,
                shodan=shodan,
                include_local_analysis=True,
            )
            investigation_runner = make_investigation_runner(uow_factory, investigation_factory)
            dispatcher = _QueuedBrowserDispatcher()
            registry = create_job_registry(investigation_runner=investigation_runner)
            release_workspace = EditionWorkspaceMaterializer(
                Path(blob_directory) / "release-workspaces"
            )
            rematerializer = EditionReleaseRematerializationService(
                uow_factory,
                artifact_store,
                release_workspace,
            )
            register_publication_jobs(
                registry,
                uow_factory,
                assembly_service,
                render_service,
                dispatcher,
                rematerializer,
            )
            executor = JobExecutor(uow_factory, registry)
            job_service = JobService(uow_factory, registry)

            application = build_emulator_app(uow_factory, artifact_store)
            application.include_router(subjects_router)
            application.include_router(production_router)
            application.include_router(investigation_router)
            application.include_router(opinion_router)
            application.state.subject_service = SubjectService(uow_factory)
            application.state.investigation_composition_factory = investigation_factory
            application.state.job_registry = registry
            application.state.job_service = job_service
            application.state.job_dispatcher = dispatcher
            application.state.edition_publication_service = EditionPublicationService(
                uow_factory,
                artifact_store,
                job_service=job_service,
                job_dispatcher=dispatcher,
            )
            application.state.edition_render_service = render_service
            application.state.edition_assembly_service = assembly_service
            application.state.identity_provider = LocalIdentityProvider("workflow-analyst")

            @application.get("/api/health")
            async def health() -> dict[str, str]:
                return {"status": "ok"}

            state: dict[str, Any] = {
                "scenario": "investigation-opinion-repin",
                "edition_id": str(edition.id),
                "subjects": {label: str(subjects[label].id) for label in ("A", "B", "C")},
                "database_name": database_name,
                "blob_root": str(artifact_store._catalog._store._root),
                "api_port": port,
                "candidate_sha256": _S2_HASH,
                "candidate_bytes_sha256": hashlib.sha256(_S2_BYTES).hexdigest(),
                **article_a,
                "historic_release": history,
            }
            await asyncio.to_thread(_write_state, state_path, state)

            @application.post("/__workflow_browser/run-pending-jobs")
            async def run_pending_jobs() -> dict[str, Any]:
                executed: list[dict[str, str]] = []
                failures = _CapturedJobFailures()
                jobs_logger = logging.getLogger("cti_app.application.jobs")
                jobs_logger.addHandler(failures)
                failed_job: dict[str, str | None] | None = None
                try:
                    while (job_id := dispatcher.pop()) is not None:
                        job = await executor.execute(job_id)
                        executed.append(
                            {
                                "job_id": str(job.id),
                                "kind": job.kind,
                                "status": job.status.value,
                            }
                        )
                        if job.status.value != "succeeded":
                            failed_job = {
                                "job_id": str(job.id),
                                "status": job.status.value,
                                "error_code": job.error_code,
                                "error_message": job.error_message,
                            }
                            break
                finally:
                    jobs_logger.removeHandler(failures)
                if failed_job is not None:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "code": "workflow_job_failed",
                            **failed_job,
                            "diagnostics": failures.messages,
                        },
                    )
                state["job_runs"] = [*state.get("job_runs", []), *executed]
                state["fixture_bridge_submissions"] = transport.submission_count
                state["fixture_vt_file_reports"] = vt.calls["file_report"]
                state["fixture_vt_searches"] = vt.calls["intelligence_search"]
                state["fixture_vt_downloads"] = vt.download_calls
                state["fixture_vt_downloaded_sha256s"] = list(vt.downloaded_hashes)
                state["fixture_shodan_calls"] = len(shodan.calls)
                await asyncio.to_thread(_write_state, state_path, state)
                return {
                    "executed": executed,
                    "fixtures": {
                        "bridge_submissions": transport.submission_count,
                        "vt_file_reports": vt.calls["file_report"],
                        "vt_searches": vt.calls["intelligence_search"],
                        "vt_download_calls": vt.download_calls,
                        "vt_downloaded_sha256s": list(vt.downloaded_hashes),
                        "shodan_calls": len(shodan.calls),
                    },
                }

            config = uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                log_level="info",
                access_log=False,
            )
            server = uvicorn.Server(config)

            @application.post("/__workflow_browser/shutdown")
            async def request_shutdown() -> dict[str, str]:
                server.should_exit = True
                return {"status": "shutting_down"}

            print(
                "J2-browser-investigation / backend ready: "
                f"edition={edition.id} database={database_name} port={port}",
                flush=True,
            )
            await server.serve()
    finally:
        if engine is not None:
            await engine.dispose()
        if database_created:
            await _drop_database(admin_dsn, database_name)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
