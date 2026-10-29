"""W18 full PostgreSQL proof for supervised first-run recovery and V5 to V6."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from cti_app.api.editions import router as editions_router
from cti_app.api.publication import router as publication_router
from cti_app.api.selection import selection_router
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_service import InvocationReconciliationOutcome
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.subject_production import SubjectProductionService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.classification import TLP
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleOrigin,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import StopReason
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionStage,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.infrastructure.database.models.investigation import InvestigationWorkingStateRow
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.production.support import ProductionScenario
from tests.shodan_support import FakeShodanPort
from tests.workflow_emulator.driver import WorkflowDriver, build_emulator_app
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_opinion_http,
    build_investigation_app,
    composition_factory,
    decision,
    investigation_settings,
    persist_sample_association_fixture,
    publish_opinion_http,
    run_http_job,
)
from tests.workflow_emulator.providers import capture_bundle

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


class _AmbiguousDownloadVirusTotal(HashCheckedVirusTotal):
    """Mock a signed CDN stream ending after partial bytes to model ambiguity."""

    def __init__(self, *, material: SyntheticSampleMaterial) -> None:
        super().__init__(material=material)
        self.download_entered = asyncio.Event()
        self.release_download = asyncio.Event()
        self.partial_bytes_written = 0

    def sample_download_response(
        self,
        request: httpx.Request,
        file_hash: str,
        sample_bytes: bytes,
    ) -> httpx.Response:
        del file_hash, sample_bytes
        payload = b"synthetic partial sample bytes"
        owner = self

        class PartialFailureStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                owner.download_entered.set()
                await owner.release_download.wait()
                yield payload
                owner.partial_bytes_written = len(payload)
                raise httpx.ReadError("synthetic connection ended after partial bytes")

            async def aclose(self) -> None:
                return None

        return httpx.Response(
            200,
            stream=PartialFailureStream(),
            headers={"content-type": "application/octet-stream"},
            request=request,
        )


def _renderer(
    *, uow_factory: Any, artifact_store: ProductionArtifactStore, binary: str
) -> tuple[PublicationRenderService, EditionRenderService, MediaAssetStore]:
    template_root, font_root, fonts_lock = typst_bundle_paths()
    media = MediaAssetStore(artifact_store, uow_factory)
    compiler = TypstSubprocessCompiler(binary=binary)
    publication = PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media,
        renderer=TypstRenderer(),
        compiler=compiler,
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=fonts_lock,
    )
    edition = EditionRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media,
        renderer=EditionTypstRenderer(),
        compiler=compiler,
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=fonts_lock,
    )
    return publication, edition, media


async def _new_combined_app(
    driver: WorkflowDriver,
    *,
    artifact_store: ProductionArtifactStore,
    composition: Any,
    typst_binary: str,
) -> tuple[FastAPI, Any, Any]:
    """Rebuild the API and worker composition while retaining canonical PG/blob state."""
    edition_app = build_emulator_app(driver.uow_factory, artifact_store)
    app, registry, dispatcher = build_investigation_app(
        uow_factory=driver.uow_factory,
        artifact_store=artifact_store,
        composition_factory=composition,
    )
    for router in (editions_router, publication_router, selection_router):
        app.include_router(router)
    for name in (
        "edition_service",
        "edition_composition_service",
        "edition_review_service",
        "edition_publication_service",
        "selection_service",
    ):
        setattr(app.state, name, getattr(edition_app.state, name))
    app.state.identity_provider = LocalIdentityProvider("human:w18-reviewer")
    publication_renderer, _edition_renderer, _media = _renderer(
        uow_factory=driver.uow_factory,
        artifact_store=artifact_store,
        binary=typst_binary,
    )
    app.state.publication_render_service = publication_renderer
    app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
        uow_factory=driver.uow_factory,
        artifact_store=artifact_store,
        renderer=publication_renderer,
    )

    old_client = driver.client
    driver.app = app
    driver.artifact_store = artifact_store
    driver.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1")
    await old_client.aclose()
    return app, registry, dispatcher


async def _working_checkpoint(uow_factory: Any, investigation_id: UUID) -> tuple[int, Any]:
    async with uow_factory() as uow:
        row = await uow._session.scalar(
            select(InvestigationWorkingStateRow).where(
                InvestigationWorkingStateRow.investigation_id == investigation_id
            )
        )
        assert row is not None
        return row.version, row.updated_at


async def _read_operator_status(
    client: httpx.AsyncClient, uow_factory: Any, investigation_id: UUID
) -> dict[str, Any]:
    before = await _working_checkpoint(uow_factory, investigation_id)
    first = await client.get(f"/api/investigations/{investigation_id}/operator-status")
    second = await client.get(f"/api/investigations/{investigation_id}/operator-status")
    assert first.status_code == second.status_code == 200, first.text
    assert first.json() == second.json()
    after = await _working_checkpoint(uow_factory, investigation_id)
    assert after == before, "operator-status must not mutate the durable working state"
    status: dict[str, Any] = first.json()
    return status


async def _checkpoint(
    *,
    label: str,
    driver: WorkflowDriver,
    production: ProductionScenario,
    discovery_batch_id: UUID,
    subject_id: UUID,
    journal: list[dict[str, Any]],
    journal_path: Path,
    production_instances: tuple[ProductionScenario, ...],
    vt: HashCheckedVirusTotal | None = None,
    shodan: FakeShodanPort | None = None,
    transport: ScriptedInvestigationTransport | None = None,
    investigation_id: UUID | None = None,
    opinion_id: UUID | None = None,
    v6_artifact_id: UUID | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    async with driver.uow_factory() as uow:
        run = await uow.production_runs.get(production.run_id)
        artifacts = tuple(await uow.production_artifacts.list_for_run(production.run_id))
        production_batch = await uow.edition_production_batches.get(production.batch_id)
        production_items = tuple(
            await uow.edition_production_batch_items.list_for_batch(production.batch_id)
        )
        discovery_batch = await uow.discovery_batches.get(discovery_batch_id)
        snapshot = await uow.discovery_snapshots.get_active(production.edition.id)
        selections = tuple(await uow.selection_decisions.list_for_edition(production.edition.id))
        collections = tuple(await uow.source_collections.list_for_subject(subject_id))
        checkpoint: dict[str, Any] = {
            "label": label,
            "edition_id": str(production.edition.id),
            "subject_id": str(subject_id),
            "discovery_batch_id": str(discovery_batch.id) if discovery_batch else None,
            "active_snapshot": (
                {"id": str(snapshot.id), "version": snapshot.version}
                if snapshot is not None
                else None
            ),
            "selection_decision_ids": [str(item.id) for item in selections],
            "production_run": (
                {
                    "id": str(run.id),
                    "status": run.status.value,
                    "stage": run.current_stage.value,
                    "version": run.version,
                    "pipeline_generation": run.pipeline_generation,
                }
                if run is not None
                else None
            ),
            "production_batch": (
                {"id": str(production_batch.id), "status": production_batch.status.value}
                if production_batch is not None
                else None
            ),
            "production_item_ids": [str(item.id) for item in production_items],
            "source_collection_ids": [str(item.id) for item in collections],
            "artifacts": [
                {
                    "id": str(item.id),
                    "stage": item.stage.value,
                    "version": item.version,
                    "input_sha256": item.input_hash,
                    "blob_id": str(item.canonical_blob_id) if item.canonical_blob_id else None,
                    "status": item.status.value,
                    "model_run_id": str(item.model_run_id) if item.model_run_id else None,
                }
                for item in sorted(
                    artifacts, key=lambda row: (row.stage.value, row.version, str(row.id))
                )
            ],
            "production_model_calls": [
                {
                    "stage": call.stage,
                    "prompt": call.request.prompt_template_id,
                    "model_run_id": str(call.model_run_id) if call.model_run_id else None,
                }
                for instance in production_instances
                for call in instance.model.calls
            ],
            "source_transport_calls": sum(
                len(instance.collection_transport.requests) for instance in production_instances
            ),
        }
        if investigation_id is not None:
            state = await uow.investigation_states.get(investigation_id)
            investigation = await uow.analyst_investigations.get(investigation_id)
            envelope = await uow.investigation_envelopes.get(investigation_id)
            invocations = tuple(await uow.tool_invocations.list_for_investigation(investigation_id))
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(investigation_id)
            )
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(investigation_id)
            )
            checkpoint["investigation"] = {
                "id": str(investigation_id),
                "version": investigation.version if investigation else None,
                "stop_reason": state.stop_reason.value if state and state.stop_reason else None,
                "state_version": state.version if state else None,
                "turn_index": state.turn_index if state else None,
                "g2_version": envelope.version if envelope else None,
                "g2_approved": envelope is not None,
                "budget_consumed": (
                    {key.value: value for key, value in envelope.budget.consumed.items()}
                    if envelope
                    else {}
                ),
                "invocations": [
                    {
                        "id": str(item.id),
                        "operation_key": item.operation_key,
                        "tool": item.tool_name,
                        "status": item.status.value,
                        "version": item.version,
                        "response_refs": [str(ref) for ref in item.response_refs],
                    }
                    for item in sorted(invocations, key=lambda row: row.operation_key)
                ],
                "observations": [
                    {
                        "id": str(item.id),
                        "sha256": item.sha256,
                        "source": item.source,
                        "object_type": item.object_type,
                        "object_id": item.object_id,
                    }
                    for item in sorted(observations, key=lambda row: str(row.id))
                ],
                "findings": [
                    {
                        "id": str(item.id),
                        "kind": item.kind.value,
                        "assessment": item.assessment.value,
                        "novelty": item.novelty.value if item.novelty else None,
                        "observation_refs": [str(ref) for ref in item.observation_refs],
                    }
                    for item in sorted(findings, key=lambda row: str(row.id))
                ],
            }
            checkpoint["fake_provider_effects"] = {
                "bridge_submissions": transport.submission_count if transport else 0,
                "bridge_attempts": transport.attempt_count if transport else 0,
                "vt_calls": dict(vt.calls) if vt else {},
                "vt_download_calls": vt.download_calls if vt else 0,
                "shodan_calls": list(shodan.calls) if shodan else [],
            }
        if opinion_id is not None:
            opinion = await uow.analyst_opinions.get(opinion_id)
            checkpoint["opinion"] = (
                {
                    "id": str(opinion.id),
                    "version": opinion.version,
                    "sha256": opinion.content_sha256,
                    "status": opinion.status.value,
                    "base_publication_sha256": opinion.base_publication_sha256,
                }
                if opinion is not None
                else None
            )
        composition = await uow.edition_compositions.get_for_edition(production.edition.id)
        checkpoint["composition"] = (
            {
                "id": str(composition.id),
                "version": composition.version,
                "entries": [
                    {
                        "subject_id": str(item.subject_id),
                        "artifact_id": str(item.artifact_id) if item.artifact_id else None,
                        "artifact_version": item.artifact_version,
                        "document_sha256": item.document_sha256,
                    }
                    for item in composition.entries
                ],
            }
            if composition is not None
            else None
        )
        manifest = await uow.publication_manifests.get_latest_manifest_for_edition(
            production.edition.id
        )
        checkpoint["latest_manifest"] = (
            {"id": str(manifest.id), "sha256": manifest.content_sha256}
            if manifest is not None
            else None
        )
        if v6_artifact_id is not None:
            v6 = await uow.production_artifacts.get(v6_artifact_id)
            checkpoint["v6_publication"] = (
                {
                    "id": str(v6.id),
                    "version": v6.version,
                    "sha256": v6.input_hash,
                    "status": v6.status.value,
                }
                if v6 is not None
                else None
            )
    if extra:
        checkpoint["extra"] = dict(extra)
    journal.append(checkpoint)
    await asyncio.to_thread(journal_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        journal_path.write_text, json.dumps(journal, ensure_ascii=False, indent=2) + "\n"
    )
    return checkpoint


async def _inspect_pdf(
    content: bytes,
    output_path: Path,
    *,
    pdftotext: str,
    pdftoppm: str,
) -> tuple[str, tuple[Path, ...]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(output_path.write_bytes, content)
    text_result = await asyncio.to_thread(
        subprocess.run,
        [pdftotext, "-layout", str(output_path), "-"],
        check=True,
        capture_output=True,
        timeout=45,
    )
    prefix = output_path.with_suffix("")
    await asyncio.to_thread(
        subprocess.run,
        [pdftoppm, "-png", "-r", "120", str(output_path), str(prefix)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    pages = tuple(sorted(output_path.parent.glob(f"{prefix.name}-*.png")))
    assert pages and all(page.stat().st_size > 1_024 for page in pages)
    return text_result.stdout.decode("utf-8", errors="replace"), pages


async def _new_article_findings(
    observations: tuple[EvidenceObservation, ...], *, material: SyntheticSampleMaterial
) -> Mapping[str, Any]:
    candidate_refs = tuple(
        item.id for item in observations if item.object_id == material.sample_hash
    )
    shodan_refs = tuple(item.id for item in observations if item.source.casefold() == "shodan")
    assert candidate_refs and shodan_refs, "final Bridge response must cite both fake providers"
    return {
        "reasoning_summary": "The local VT proxy candidate is absent from the pinned V5 article.",
        "goal": "Record one bounded article-relative discovery and the passive host observation.",
        "finding_proposals": [
            {
                "kind": FindingKind.NEW_SAMPLE.value,
                "assessment": FindingAssessment.SUPPORTED.value,
                "statement": "The synthetic VT search returned a sample absent from the article.",
                "observation_refs": [str(item) for item in candidate_refs],
                "subject_ref": material.sample_hash,
                "claim_refs": [],
                "novelty": NoveltyClass.NEW_TO_ARTICLE.value,
                "scope": "Compared only with this verified V5 article snapshot.",
                "limitations": [
                    "Article-relative novelty does not establish worldwide novelty "
                    "or family membership."
                ],
            },
            {
                "kind": FindingKind.NETWORK_BEHAVIOUR.value,
                "assessment": FindingAssessment.SUPPORTED.value,
                "statement": (
                    "The fake Shodan index returned two dated services for article IOC "
                    f"{material.ip}."
                ),
                "observation_refs": [str(item) for item in shodan_refs],
                "subject_ref": material.ip,
                "claim_refs": [],
                "scope": "Passive indexed view returned by the isolated test port.",
                "limitations": ["Indexed observations do not prove incident-time activity."],
            },
        ],
        "next_action": "continue",
    }


def _artifact_dir(tmp_path: Path) -> Path:
    """Reviewable artefacts land in W18_ARTIFACT_DIR when set, else in the test sandbox."""
    directory = Path(os.environ.get("W18_ARTIFACT_DIR") or tmp_path / "w18")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.mark.asyncio
async def test_w18_supervised_first_run_recovers_without_replaying_confirmed_effects(
    workflow_driver: WorkflowDriver,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W18-supervised-first-run-recovery")
    artifact_dir = _artifact_dir(tmp_path)
    typst_binary = os.environ.get("TYPST_BINARY", "typst")
    assert shutil.which(typst_binary), "W18 requires the real Typst compiler"
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext, "W18 requires Poppler pdftotext"
    assert pdftoppm, "W18 requires Poppler pdftoppm"
    template_root, font_root, fonts_lock = typst_bundle_paths()
    assert template_root.is_dir() and font_root.is_dir() and fonts_lock.is_file()
    if os.environ.get("TMPDIR"):
        monkeypatch.setenv("TMPDIR", os.environ["TMPDIR"])
    tempfile.tempdir = None

    sample_material = SyntheticSampleMaterial.unique()
    bundle = driver.discovery_bundles.get("A")
    assert bundle is None
    await driver.create_edition(tlp=TLP.GREEN)
    discovery = await driver.run_discovery(
        "w18-selection",
        ("A",),
        bundle_overrides={
            "A": replace(capture_bundle("A"), source_tlp=TLP.GREEN),
        },
    )
    await driver.apply_fusion("w18-selection")
    selected = await driver.select_subjects("w18-selection", ("A",))
    subject_id = selected["A"]

    bundle = driver.discovery_bundles["A"]
    sources = {url: dict(spec) for url, spec in bundle.sources.items()}
    core_url, secondary_url = tuple(sources)
    assert secondary_url in sources
    core_spec = sources[core_url]
    core_body = str(core_spec["body"])
    sources[core_url] = {
        **core_spec,
        "body": (
            core_body.replace(
                "</body>",
                " The campaign activity was reported on 2026-08-15."
                f" Entry IOC sha256 {sample_material.seed_hash} and host {sample_material.ip}."
                "</body>",
            )
        ),
    }
    secondary_spec = sources[secondary_url]
    sources[secondary_url] = {
        **secondary_spec,
        "body": str(secondary_spec["body"]).replace(
            "</body>",
            " The campaign activity was reported on 2026-08-15.</body>",
        ),
    }
    extraction = dict(bundle.extraction_responses)
    extraction[core_url] = (
        "EVENT 2026-08-15\n"
        "- The campaign activity was reported on 2026-08-15. :: "
        "The campaign activity was reported on 2026-08-15.\n\n"
        + extraction[core_url]
        + "\n\nIOC confirmed sha256\n"
        + f"- {sample_material.seed_hash} :: File hash documented in the entry report.\n"
        + "\nIOC confirmed ip\n"
        + f"- {sample_material.ip} :: Host IOC documented in the entry report."
    )
    extraction[secondary_url] = (
        "EVENT 2026-08-15\n"
        "- The campaign activity was reported on 2026-08-15. :: "
        "The campaign activity was reported on 2026-08-15.\n\n" + extraction[secondary_url]
    )
    driver.discovery_bundles["A"] = replace(
        bundle,
        sources=sources,
        extraction_responses=extraction,
    )

    await driver.start_production("A", subject_id)
    production = driver.production_scenarios["A"]
    assert production.batch_id is not None and production.run_id is not None
    original_production = production
    production_instances: list[ProductionScenario] = [production]
    while True:
        async with uow_factory() as uow:
            live_run = await uow.production_runs.get(production.run_id)
        assert live_run is not None
        if live_run.current_stage is ProductionStage.SYNTHESIS:
            break
        assert await production.runner.run_next(), "pipeline stalled before synthesis"

    checkpoint_journal: list[dict[str, Any]] = []
    checkpoint_path = artifact_dir / "checkpoints.json"
    before_failure = await _checkpoint(
        label="before-production-synthesis-failure",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
    )
    prior_stage_ids = {
        item["stage"]: item["id"]
        for item in before_failure["artifacts"]
        if item["status"] == ProductionArtifactStatus.VERIFIED.value
    }
    assert ProductionArtifactStage.EXTRACTION.value in prior_stage_ids
    assert ProductionArtifactStage.RELEVANCE_PROJECTION.value in prior_stage_ids
    async with uow_factory() as uow:
        extraction_artifact = await uow.production_artifacts.get_current(
            production.run_id, ProductionArtifactStage.EXTRACTION.value
        )
        relevance_artifact = await uow.production_artifacts.get_current(
            production.run_id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
        )
    assert extraction_artifact is not None and extraction_artifact.canonical_blob_id is not None
    assert relevance_artifact is not None and relevance_artifact.canonical_blob_id is not None
    extraction_payload = await production.artifact_store.read_json(
        extraction_artifact.canonical_blob_id
    )
    relevance_payload = await production.artifact_store.read_json(
        relevance_artifact.canonical_blob_id
    )
    assert "The campaign activity was reported" in json.dumps(
        extraction_payload, ensure_ascii=False
    ), json.dumps(extraction_payload, ensure_ascii=False)[:2_000]
    confirmed_stage_calls = {
        stage: sum(call.stage == stage for call in production.model.calls)
        for stage in ("references", "extraction", "relevance_projection")
    }
    assert confirmed_stage_calls["references"] == 1
    assert confirmed_stage_calls["extraction"] >= 1
    source_transport_calls_before_failure = len(production.collection_transport.requests)

    original_response_for = production.model.script.response_for
    fail_synthesis = True
    failed_synthesis_calls = 0

    def faulty_fixture(_script: object, request: Any) -> str | Exception:
        nonlocal failed_synthesis_calls
        if fail_synthesis and request.prompt_template_id == "production-synthesis":
            failed_synthesis_calls += 1
            return "W18 fixture fault: malformed Synthesis response for recovery testing."
        return original_response_for(request)

    production.model.script.response_for = MethodType(faulty_fixture, production.model.script)
    assert await production.runner.run_next(), "expected the synthesis job to execute"
    assert failed_synthesis_calls == 1

    production_failed_checkpoint = await _checkpoint(
        label="after-production-synthesis-failure-before-retry",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        extra={"failed_synthesis_fixture_calls": failed_synthesis_calls},
    )
    assert production_failed_checkpoint["production_run"]["id"] == str(production.run_id)
    assert (
        production_failed_checkpoint["production_run"]["stage"] == ProductionStage.SYNTHESIS.value
    )
    assert production_failed_checkpoint["production_run"]["status"] == "needs_review"

    fail_synthesis = False
    retry = await SubjectProductionService(uow_factory).retry_from_stage(
        production.run_id, ProductionStage.SYNTHESIS, force_recompute=False
    )
    assert retry.previous_status.value == "needs_review"
    assert ProductionArtifactStage.EXTRACTION.value not in retry.staled_artifacts
    assert ProductionArtifactStage.SYNTHESIS.value in retry.staled_artifacts
    production_chain = ProductionStageChain(ProductionPacingPolicy.zero())
    production_chain.bind(production.jobs, production.runner)
    retried_job = await production_chain.submit(
        run=retry.run,
        stage=ProductionStage.SYNTHESIS,
        correlation_id="w18-production-synthesis-recovery",
        actor_id="human:w18-reviewer",
    )
    assert retried_job is not None
    await production.run_until_terminal()
    assert production.run_id == original_production.run_id

    async with uow_factory() as uow:
        ready_run = await uow.production_runs.get(production.run_id)
        all_artifacts = tuple(await uow.production_artifacts.list_for_run(production.run_id))
        batch_after_recovery = await uow.edition_production_batches.get(production.batch_id)
        selections_after = tuple(
            await uow.selection_decisions.list_for_edition(production.edition.id)
        )
        active_snapshot_after = await uow.discovery_snapshots.get_active(production.edition.id)
        source_collections_after = tuple(await uow.source_collections.list_for_subject(subject_id))
    assert ready_run is not None and ready_run.status.value == "ready"
    assert batch_after_recovery is not None
    assert active_snapshot_after is not None
    assert [item.id for item in selections_after] == [
        UUID(item) for item in before_failure["selection_decision_ids"]
    ]
    assert active_snapshot_after.id == UUID(before_failure["active_snapshot"]["id"])
    assert len(source_collections_after) == len(set(item.id for item in source_collections_after))
    recovered_confirmed_stage_calls = {
        stage: sum(call.stage == stage for call in production.model.calls)
        for stage in confirmed_stage_calls
    }
    assert recovered_confirmed_stage_calls == confirmed_stage_calls
    assert len(production.collection_transport.requests) == source_transport_calls_before_failure
    assert any(item.model_run_id is not None for item in all_artifacts)
    recovered_artifact_by_stage = {
        item.stage.value: item
        for item in all_artifacts
        if item.status is ProductionArtifactStatus.VERIFIED
    }
    for stage, artifact_id in prior_stage_ids.items():
        assert recovered_artifact_by_stage[stage].id == UUID(artifact_id)
    required_stages = tuple(ProductionArtifactStage)
    assert all(stage.value in recovered_artifact_by_stage for stage in required_stages)

    production_recovered_checkpoint = await _checkpoint(
        label="after-production-recovery-new-runner",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        extra={"confirmed_stage_calls_before_failure": confirmed_stage_calls},
    )
    assert (
        production_recovered_checkpoint["selection_decision_ids"]
        == before_failure["selection_decision_ids"]
    )
    assert (
        production_recovered_checkpoint["source_collection_ids"]
        == production_failed_checkpoint["source_collection_ids"]
    )

    async with uow_factory() as uow:
        run = await uow.production_runs.get(production.run_id)
        subject = await uow.subjects.get(subject_id)
        publication = await uow.production_artifacts.get_current(
            production.run_id, ProductionArtifactStage.PUBLICATION.value
        )
        synthesis = await uow.production_artifacts.get_current(
            production.run_id, ProductionArtifactStage.SYNTHESIS.value
        )
        selection_rows = tuple(
            await uow.selection_decisions.list_for_edition(production.edition.id)
        )
    assert run is not None and subject is not None
    assert publication is not None and publication.status is ProductionArtifactStatus.VERIFIED
    assert publication.canonical_blob_id is not None
    assert synthesis is not None and synthesis.status is ProductionArtifactStatus.VERIFIED
    assert synthesis.canonical_blob_id is not None
    publication_bytes = await production.artifact_store.read_bytes(publication.canonical_blob_id)
    publication_sha256 = hashlib.sha256(publication_bytes).hexdigest()
    document = publication_document_from_json(json.loads(publication_bytes))
    assert isinstance(document, PublicationDocumentV5)
    document_payload = serialize_publication_document(document)
    assert str(subject_id) == str(document.document.subject_id)
    assert sample_material.seed_hash in json.dumps(document_payload)
    assert sample_material.ip in json.dumps(document_payload)
    assert document.document.timeline, (
        "the verified V5 must retain the extracted chronology; "
        f"relevance projection: {json.dumps(relevance_payload, ensure_ascii=False)[:2_000]}"
    )
    assert len(selection_rows) == 1

    await driver.compose(
        (subject_id,),
        provenance_batch_ids={subject_id: production.batch_id},
    )
    await driver.review(expect_can_accept=True)
    await driver.validate()
    accepted_v5 = await driver.accept()
    release_v5 = await driver.assemble()
    assert release_v5.edition_id == production.edition.id
    async with uow_factory() as uow:
        initial_composition = await uow.edition_compositions.get_for_edition(production.edition.id)
    assert initial_composition is not None
    composition_v5_version = initial_composition.version

    # The API handoff and investigation snapshot must resolve the actual, exact V5/Synthesis bytes.
    material = sample_material
    vt = _AmbiguousDownloadVirusTotal(material=material)
    shodan = FakeShodanPort()
    investigation_id_ref: dict[str, UUID] = {}

    async def final_article_response(
        observations: tuple[EvidenceObservation, ...],
    ) -> Mapping[str, Any]:
        async with uow_factory() as uow:
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(investigation_id_ref["id"])
            )
        supported = tuple(
            item
            for item in findings
            if item.assessment is FindingAssessment.SUPPORTED and item.observation_refs
        )
        if not supported:
            return await _new_article_findings(observations, material=material)
        evidence_refs = tuple(
            sorted(
                {ref for finding in supported for ref in finding.observation_refs},
                key=lambda ref: ref.hex,
            )
        )
        response = decision(
            "Draft the final fact block from the persisted VT and Shodan findings.",
            next_action="finish",
        )
        response["opinion_delta"] = {
            "blocks": [
                {
                    "block_type": OpinionBlockType.KEY_FINDINGS.value,
                    "text": (
                        "The bounded VT search returned an article-relative candidate, and the "
                        f"passive Shodan query returned two indexed services for {material.ip}."
                    ),
                    "finding_refs": [str(item.id) for item in supported],
                    "evidence_refs": [str(item) for item in evidence_refs],
                    "qualification": OpinionClaimQualification.FACT.value,
                    "certainty": 0.9,
                }
            ]
        }
        return response

    transport = ScriptedInvestigationTransport(
        [
            decision(
                "Search the fake VT proxy, attempt one approved inert acquisition, "
                "then use passive Shodan.",
                next_action="continue",
            )
        ],
        uow_factory=uow_factory,
        investigation_id=UUID(int=0),
        material=material,
        inject_analysis_at_turn=None,
        final_response_builder=final_article_response,
    )
    # The transport is rebound after the canonical investigation identity is created.
    base_settings = investigation_settings(providers=True)
    pre_factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=production.artifact_store,
        settings=base_settings,
        transport=transport,
        virustotal=vt,
        shodan=shodan,
    )
    app, registry, dispatcher = await _new_combined_app(
        driver,
        artifact_store=production.artifact_store,
        composition=pre_factory,
        typst_binary=typst_binary,
    )

    # Create an investigation on the exact durable Synthesis and V5 SHA.
    synthesis_payload = await production.artifact_store.read_json(synthesis.canonical_blob_id)
    synthesis_sha256 = hashlib.sha256(
        production.artifact_store.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    start_response = await driver.client.post(
        f"/api/subjects/{subject_id}/investigations",
        json={
            "production_run_id": str(production.run_id),
            "synthesis_artifact_id": str(synthesis.id),
            "synthesis_sha256": synthesis_sha256,
        },
    )
    assert start_response.status_code == 200, start_response.text
    investigation_id = UUID(start_response.json()["investigation"]["id"])
    investigation_id_ref["id"] = investigation_id
    transport._investigation_id = investigation_id
    approved_g2 = await driver.client.post(
        f"/api/investigations/{investigation_id}/envelope/approve",
        json={
            "allowed_providers": ["vt", "shodan"],
            "budget_limits": {"bridge_turns": 8, "vt_units": 24, "shodan_credits": 2},
            "acquire_hits_allowed": True,
            "max_acquire_volume": 1,
            # Empty fields retain the article's exact source disclosure ceiling.
            "disclosure": {},
            "reason": "Approve the bounded synthetic workflow investigation.",
            "correlation_id": f"w18-g2:{investigation_id}",
        },
        headers={"Idempotency-Key": f"w18-g2-approve-{investigation_id}"},
    )
    assert approved_g2.status_code == 200, approved_g2.text
    assert approved_g2.json()["approved"] is True
    assert approved_g2.json()["approval_actor_id"] == "human:w18-reviewer"

    async with uow_factory() as uow:
        persisted_investigation = await uow.analyst_investigations.get(investigation_id)
        input_pack = await uow.analyst_input_packs.get_for_investigation(investigation_id)
    assert persisted_investigation is not None
    assert persisted_investigation.synthesis_artifact_id == synthesis.id
    assert input_pack is not None
    article_snapshot = await pre_factory.article_snapshots.resolve(synthesis.id)
    assert article_snapshot.synthesis_sha256 == synthesis_sha256
    assert article_snapshot.base_publication_artifact_id == publication.id
    assert article_snapshot.base_publication_sha256 == publication_sha256
    assert article_snapshot.tlp == TLP.GREEN.value
    assert article_snapshot.do_not_submit is False
    assert article_snapshot.external_llm_allowed is True
    article_context = json.dumps(
        article_snapshot.article.model_dump(mode="json"), ensure_ascii=False
    )
    assert material.seed_hash in json.dumps(document_payload, ensure_ascii=False)
    assert material.sample_hash not in json.dumps(document_payload, ensure_ascii=False)
    assert material.sample_hash not in article_context

    evidence_bytes = b"W18 article-linked synthetic IOC evidence:" + material.seed_hash.encode()
    evidence_blob_id = await production.artifact_store.put_bytes(
        evidence_bytes,
        bucket="workflow-w18-article-evidence",
        mime_type="application/octet-stream",
    )
    seed_observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=evidence_blob_id,
        sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        source="synthetic-entry-article-v5",
        source_version="w18-v1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id=material.seed_hash,
        limitations=("Local synthetic entry-article evidence only.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(seed_observation)
        await uow.commit()
    association = await persist_sample_association_fixture(
        uow_factory,
        production.artifact_store,
        investigation_id=investigation_id,
        subject_id=subject_id,
        sample_sha256=material.sample_hash,
        origin=SampleOrigin.VT_PIVOT,
    )

    def tool_call(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "tool_name": name,
            "schema_version": "1",
            "purpose": f"Run one bounded synthetic {name} operation.",
            "arguments": dict(arguments),
        }

    search_call = tool_call(
        "vt.intelligence_search",
        {
            "query": 'type:peexe content:"AUTOWORK-SYNTHETIC"',
            "justification": "Look for a bounded synthetic candidate related to the article IOC.",
            "evidence_refs": [str(seed_observation.id)],
        },
    )
    download_call = tool_call(
        "vt.download_sample",
        {
            "sha256": material.sample_hash,
            "purpose": "Attempt one explicitly G2-authorized inert test download.",
            "evidence_refs": [str(seed_observation.id)],
        },
    )
    shodan_call = tool_call("shodan.host", {"ip": material.ip})
    pivot_decision = decision(
        "The candidate hash is absent from V5; inspect it once and continue "
        "with the passive host record.",
        next_action="continue",
    )
    pivot_decision["proposed_tool_calls"] = [search_call, download_call, shodan_call]
    transport._responses.clear()
    transport.enqueue(pivot_decision)

    # The exact sample association is durable before the tool batch begins.
    async with uow_factory() as uow:
        associations = tuple(await uow.sample_associations.list_for_investigation(investigation_id))
    matching_associations = [
        item for item in associations if item.sample_sha256 == material.sample_hash
    ]
    assert len(matching_associations) == 1
    assert matching_associations[0].id == association.id

    status_before_run = await _read_operator_status(driver.client, uow_factory, investigation_id)
    assert status_before_run["synthesis_sha256"] == synthesis_sha256
    assert status_before_run["base_publication_sha256"] == publication_sha256
    started = start_response.json()
    worker = asyncio.create_task(
        run_http_job(
            app,
            uow_factory,
            registry,
            dispatcher,
            UUID(started["job_id"]),
        )
    )
    try:
        await asyncio.wait_for(vt.download_entered.wait(), timeout=8)
    except TimeoutError as exc:
        if worker.done():
            worker_error = repr(worker.exception())
        else:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            worker_error = "worker did not finish before the fake VT download timeout"
        async with uow_factory() as uow:
            observed_invocations = tuple(
                await uow.tool_invocations.list_for_investigation(investigation_id)
            )
        operator_status = await driver.client.get(
            f"/api/investigations/{investigation_id}/operator-status"
        )
        invocation_summary = [(item.tool_name, item.status.value) for item in observed_invocations]
        source_policy = (
            article_snapshot.tlp,
            article_snapshot.do_not_submit,
            article_snapshot.external_llm_allowed,
        )
        raise AssertionError(
            "fake VT download was not reached: "
            f"vt_calls={vt.calls!r}, shodan_calls={shodan.calls!r}, "
            f"transport_submissions={transport.submission_count}, "
            f"invocations={invocation_summary!r}, "
            f"source_policy={source_policy!r}, "
            f"g2={operator_status.json().get('g2')!r}, "
            f"worker_error={worker_error}"
        ) from exc
    status_while_blocked = await _read_operator_status(driver.client, uow_factory, investigation_id)
    assert status_while_blocked["last_operation"]["kind"] == "bridge_turn"
    operator_invocations = {
        (item["tool_name"], item["status"]) for item in status_while_blocked["invocations"]
    }
    assert ("vt.intelligence_search", ToolInvocationStatus.SUCCEEDED.value) in operator_invocations
    assert ("vt.download_sample", ToolInvocationStatus.RUNNING.value) in operator_invocations
    blocked_working_version = status_while_blocked["working_state_version"]
    pivot_checkpoint = await _checkpoint(
        label="before-pivot-interrupt-second-tool-running",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        extra={"blocked_working_state_version": blocked_working_version},
    )
    interrupted = await driver.client.post(
        f"/api/investigations/{investigation_id}/interrupt",
        json={"expected_version": blocked_working_version},
        headers={"Idempotency-Key": f"w18-interrupt-{investigation_id}"},
    )
    assert interrupted.status_code == 200, interrupted.text
    assert interrupted.json()["investigation"]["stop_reason"] == StopReason.USER_INTERRUPT.value
    vt.release_download.set()
    await asyncio.wait_for(worker, timeout=20)
    status_after_crash = await _read_operator_status(driver.client, uow_factory, investigation_id)
    assert status_after_crash["stop_reason"] == StopReason.USER_INTERRUPT.value
    assert shodan.calls == [], "the third pivot must not be sent after the durable interrupt"
    assert vt.calls["intelligence_search"] == 1
    assert vt.download_calls == 1
    assert vt.partial_bytes_written > 0, "the fixture must fail after writing sample bytes"
    async with uow_factory() as uow:
        invocations_at_stop = tuple(
            await uow.tool_invocations.list_for_investigation(investigation_id)
        )
        observations_at_stop = tuple(
            await uow.evidence_observations.list_for_investigation(investigation_id)
        )
    search_invocation = next(
        item for item in invocations_at_stop if item.tool_name == "vt.intelligence_search"
    )
    ambiguous_invocation = next(
        item for item in invocations_at_stop if item.tool_name == "vt.download_sample"
    )
    assert search_invocation.status is ToolInvocationStatus.SUCCEEDED
    assert search_invocation.response_refs
    assert ambiguous_invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert len(ambiguous_invocation.response_refs) == 1
    assert set(ambiguous_invocation.response_refs) <= {item.id for item in observations_at_stop}
    ambiguous_observation = next(
        item for item in observations_at_stop if item.id == ambiguous_invocation.response_refs[0]
    )
    ambiguous_payload = await production.artifact_store.read_json(ambiguous_observation.blob_id)
    assert ambiguous_payload["result"] == {
        "status": ToolInvocationStatus.NEEDS_REVIEW.value,
        "outcome": "external_effect_unconfirmed",
        "adapter_output_archived": False,
    }
    assert "synthetic partial sample bytes" not in json.dumps(ambiguous_payload)
    assert not any(item.tool_name == "shodan.host" for item in invocations_at_stop)
    stopped_checkpoint = await _checkpoint(
        label="after-pivot-interrupt-second-tool-needs-review",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        extra={"ambiguous_invocation_id": str(ambiguous_invocation.id)},
    )

    # A fresh worker/API composition uses the same canonical DB and filesystem blob catalog.
    recovered_composition = composition_factory(
        uow_factory=uow_factory,
        artifact_store=production.artifact_store,
        settings=base_settings,
        transport=transport,
        virustotal=vt,
        shodan=shodan,
    )
    app, registry, dispatcher = await _new_combined_app(
        driver,
        artifact_store=production.artifact_store,
        composition=recovered_composition,
        typst_binary=typst_binary,
    )
    status_after_worker_restart = await _read_operator_status(
        driver.client, uow_factory, investigation_id
    )
    assert (
        status_after_worker_restart["last_operation"]["id"]
        == status_after_crash["last_operation"]["id"]
    )
    assert status_after_worker_restart["stop_reason"] == StopReason.USER_INTERRUPT.value
    resume_before_reconcile = await driver.client.post(
        f"/api/investigations/{investigation_id}/resume",
        json={"expected_version": status_after_worker_restart["working_state_version"]},
        headers={"Idempotency-Key": f"w18-resume-before-reconcile-{investigation_id}"},
    )
    assert resume_before_reconcile.status_code == 409, resume_before_reconcile.text
    assert resume_before_reconcile.json()["detail"]["code"] == "investigation_resume_conflict"
    assert vt.download_calls == 1, "resume must not replay an ambiguous download"
    assert shodan.calls == [], "no later external tool may run before reconciliation"
    status_after_refused_resume = await _read_operator_status(
        driver.client, uow_factory, investigation_id
    )
    assert (
        status_after_refused_resume["working_state_version"]
        == status_after_worker_restart["working_state_version"]
    )
    reconcile = await driver.client.post(
        f"/api/investigations/{investigation_id}/invocations/{ambiguous_invocation.id}/reconcile",
        headers={"Idempotency-Key": f"w18-reconcile-{ambiguous_invocation.id}"},
        json={
            "expected_version": ambiguous_invocation.version,
            "outcome": InvocationReconciliationOutcome.ABANDON.value,
            "reason": "The test proxy timed out; preserve the quota and "
            "do not resubmit this acquisition.",
            "response_refs": [],
        },
    )
    assert reconcile.status_code == 200, reconcile.text
    assert reconcile.json()["status"] == ToolInvocationStatus.FAILED.value
    status_before_resume = await _read_operator_status(driver.client, uow_factory, investigation_id)
    transport.enqueue(
        decision(
            "After reconciling the ambiguous VT acquisition, "
            "run the remaining passive Shodan pivot.",
            tool_call=shodan_call,
            next_action="continue",
        )
    )
    resumed = await driver.client.post(
        f"/api/investigations/{investigation_id}/resume",
        json={"expected_version": status_before_resume["working_state_version"]},
        headers={"Idempotency-Key": f"w18-resume-after-reconcile-{investigation_id}"},
    )
    assert resumed.status_code == 200, resumed.text
    await run_http_job(app, uow_factory, registry, dispatcher, UUID(resumed.json()["job_id"]))
    status_after_resume = await _read_operator_status(driver.client, uow_factory, investigation_id)
    assert status_after_resume["stop_reason"] == StopReason.AGENT_FINISHED.value
    assert transport.submission_count == 4, (
        "one initial pivot turn, resume-continuation, findings turn, and opinion turn"
    )
    assert transport.submission_count == len(transport._by_key)
    assert vt.calls["intelligence_search"] == 1
    assert vt.download_calls == 1, "an ambiguous acquisition must never be submitted twice"
    assert len(shodan.calls) == 1
    async with uow_factory() as uow:
        invocations = tuple(await uow.tool_invocations.list_for_investigation(investigation_id))
        observations = tuple(
            await uow.evidence_observations.list_for_investigation(investigation_id)
        )
        findings = tuple(await uow.investigation_findings.list_for_investigation(investigation_id))
        envelope = await uow.investigation_envelopes.get(investigation_id)
    assert envelope is not None
    assert not any(envelope.budget.reserved.values())
    assert sum(item.tool_name == "vt.intelligence_search" for item in invocations) == 1
    assert sum(item.tool_name == "vt.download_sample" for item in invocations) == 1
    assert sum(item.tool_name == "shodan.host" for item in invocations) == 1
    operation_keys = [item.operation_key for item in invocations]
    assert len(operation_keys) == len(set(operation_keys)), "one invocation row per operation key"
    new_sample = next(item for item in findings if item.kind is FindingKind.NEW_SAMPLE)
    assert new_sample.novelty is NoveltyClass.NEW_TO_ARTICLE
    assert set(new_sample.observation_refs) <= {item.id for item in observations}
    assert all(
        item.object_id != material.sample_hash
        for item in observations
        if item.source == "synthetic-entry-article-v5"
    )
    assert any(item.object_id == material.sample_hash for item in observations)
    resumed_checkpoint = await _checkpoint(
        label="after-pivot-worker-restart-reconcile-and-resume",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        extra={
            "reconciled_invocation_id": str(ambiguous_invocation.id),
            "operator_status_before_resume_version": status_before_resume["working_state_version"],
        },
    )
    assert pivot_checkpoint["artifacts"] == stopped_checkpoint["artifacts"]
    assert len(resumed_checkpoint["artifacts"]) == len(stopped_checkpoint["artifacts"])

    # Compose, preview, retouch, reject an old CAS, then preview only the corrected opinion.
    composed = await driver.client.post(
        f"/api/investigations/{investigation_id}/opinion/compose",
        headers={"Idempotency-Key": f"w18-compose-{investigation_id}"},
        json={"selection": {}},
    )
    assert composed.status_code == 200, composed.text
    draft = composed.json()["draft"]
    opinion_id = UUID(draft["id"])
    assert draft["provenance"]["synthesis_artifact_id"] == str(synthesis.id)
    assert draft["provenance"]["synthesis_sha256"] == synthesis_sha256
    assert draft["provenance"]["base_publication_artifact_id"] == str(publication.id)
    assert draft["provenance"]["base_publication_sha256"] == publication_sha256
    assert draft["blocks"]

    def revise_blocks(source: Mapping[str, Any], addition: str) -> list[dict[str, Any]]:
        blocks = [
            {
                "block_type": item["type"],
                "text": item["text"],
                "finding_refs": item["finding_refs"],
                "evidence_refs": item["evidence_refs"],
                "qualification": item["qualification"],
                "certainty": item["certainty"],
            }
            for item in source["blocks"]
        ]
        blocks[0]["text"] += addition
        return blocks

    async def preview_current(
        opinion_payload: Mapping[str, Any], path: Path
    ) -> tuple[str, tuple[Path, ...], bytes]:
        response = await driver.client.get(
            f"/api/opinions/{opinion_id}/preview.pdf",
            params={
                "version": opinion_payload["version"],
                "opinion_sha256": opinion_payload["content_sha256"],
                "base_publication_sha256": publication_sha256,
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Opinion-Status"] == "draft-unapproved"
        assert response.headers["X-Opinion-Version"] == str(opinion_payload["version"])
        assert response.headers["X-Opinion-Content-SHA256"] == opinion_payload["content_sha256"]
        assert response.headers["X-Base-Publication-SHA256"] == publication_sha256
        assert hashlib.sha256(response.content).hexdigest() == response.headers["X-PDF-SHA256"]
        extracted, pages = await _inspect_pdf(
            response.content, path, pdftotext=pdftotext, pdftoppm=pdftoppm
        )
        return " ".join(extracted.split()), pages, response.content

    home_output = artifact_dir
    preview_v1_text, preview_v1_pages, preview_v1_bytes = await preview_current(
        draft, home_output / "opinion-preview-v1.pdf"
    )
    preview_normalized = preview_v1_text.casefold().replace(chr(0x2019), "'")
    assert "brouillon non approuvé" in preview_normalized
    assert sample_material.seed_hash in preview_v1_text
    assert sample_material.ip in preview_v1_text
    assert "avis de l'analyste" in preview_normalized
    assert any(
        item["source"] == "synthetic-entry-article-v5"
        for item in resumed_checkpoint["investigation"]["observations"]
    )
    publication_ids_before_revision = [
        item["id"] for item in resumed_checkpoint["artifacts"] if item["stage"] == "publication"
    ]

    revision_one_blocks = revise_blocks(
        draft, " Retouche W18 v2 : indice limité au contexte observé."
    )
    revision_one = await driver.client.post(
        f"/api/opinions/{opinion_id}/revise",
        headers={"Idempotency-Key": f"w18-revise-{opinion_id}-v{draft['version']}"},
        json={
            "expected_version": draft["version"],
            "expected_sha256": draft["content_sha256"],
            "blocks": revision_one_blocks,
        },
    )
    assert revision_one.status_code == 200, revision_one.text
    revised_v2 = revision_one.json()["draft"]
    assert revised_v2["version"] == draft["version"] + 1
    stale_revision = await driver.client.post(
        f"/api/opinions/{opinion_id}/revise",
        headers={"Idempotency-Key": f"w18-stale-revise-{opinion_id}-v{draft['version']}"},
        json={
            "expected_version": draft["version"],
            "expected_sha256": draft["content_sha256"],
            "blocks": revise_blocks(draft, " This stale edit must not be applied."),
        },
    )
    assert stale_revision.status_code == 409
    current_opinion = await driver.client.get(f"/api/investigations/{investigation_id}/opinion")
    assert current_opinion.status_code == 200
    assert current_opinion.json()["draft"]["version"] == revised_v2["version"]
    assert current_opinion.json()["draft"]["content_sha256"] == revised_v2["content_sha256"]
    corrected_revision = await driver.client.post(
        f"/api/opinions/{opinion_id}/revise",
        headers={"Idempotency-Key": f"w18-revise-corrected-{opinion_id}-v{revised_v2['version']}"},
        json={
            "expected_version": revised_v2["version"],
            "expected_sha256": revised_v2["content_sha256"],
            "blocks": revise_blocks(
                revised_v2,
                " Retouche W18 v3 : hash candidat "
                f"{material.sample_hash}, découverte propre à l'article d'entrée.",
            ),
        },
    )
    assert corrected_revision.status_code == 200, corrected_revision.text
    revised_v3 = corrected_revision.json()["draft"]
    assert revised_v3["version"] == revised_v2["version"] + 1
    preview_v3_text, preview_v3_pages, preview_v3_bytes = await preview_current(
        revised_v3, home_output / "opinion-preview-v3.pdf"
    )
    preview_v3_normalized = preview_v3_text.casefold().replace(chr(0x2019), "'")
    assert "brouillon non approuvé" in preview_v3_normalized
    assert "retouche w18 v3" in preview_v3_normalized
    assert "chronologie" in preview_v3_normalized
    assert "synthèse" in preview_v3_normalized
    assert sample_material.seed_hash in preview_v3_text
    assert sample_material.sample_hash in preview_v3_text
    assert len(preview_v1_pages) >= 1 and len(preview_v3_pages) >= 1
    assert preview_v1_bytes != preview_v3_bytes
    async with uow_factory() as uow:
        artifacts_after_preview = tuple(
            await uow.production_artifacts.list_for_run(production.run_id)
        )
        composition_before_publication = await uow.edition_compositions.get_for_edition(
            production.edition.id
        )
    assert [
        str(item.id)
        for item in artifacts_after_preview
        if item.stage is ProductionArtifactStage.PUBLICATION
    ] == publication_ids_before_revision
    assert composition_before_publication is not None
    assert composition_before_publication.version == composition_v5_version

    approved_opinion = await approve_opinion_http(
        driver.client,
        opinion_id,
        version=revised_v3["version"],
        sha256=revised_v3["content_sha256"],
    )
    assert approved_opinion.status_code == 200, approved_opinion.text
    approved_payload = approved_opinion.json()["approved"]
    assert approved_payload["status"] == AnalystOpinionStatus.APPROVED.value
    published = await publish_opinion_http(
        driver.client,
        opinion_id,
        version=approved_payload["version"],
        base_publication_sha256=publication_sha256,
    )
    assert published.status_code == 200, published.text
    assert published.json()["composition_repin_required"] is True
    v6_id = UUID(published.json()["publication"]["artifact_id"])
    async with uow_factory() as uow:
        v6_artifact = await uow.production_artifacts.get(v6_id)
        assert v6_artifact is not None and v6_artifact.canonical_blob_id is not None
        v6_blob = await uow.blobs.get(v6_artifact.canonical_blob_id)
        composition_before_repin = await uow.edition_compositions.get_for_edition(
            production.edition.id
        )
        manifest_before_repin = await uow.publication_manifests.get_latest_manifest_for_edition(
            production.edition.id
        )
    assert v6_blob is not None and composition_before_repin is not None
    assert composition_before_repin.version == composition_v5_version
    assert manifest_before_repin is not None and manifest_before_repin.id == accepted_v5.manifest.id
    v6_bytes = await production.artifact_store.read_bytes(v6_artifact.canonical_blob_id)
    v6_document = publication_document_from_json(json.loads(v6_bytes))
    assert isinstance(v6_document, PublicationDocumentV6)
    assert v6_document.provenance.base_publication_artifact_id == publication.id
    assert v6_document.provenance.base_publication_sha256 == publication_sha256
    assert v6_document.provenance.opinion_id == opinion_id
    assert v6_document.provenance.opinion_version == approved_payload["version"]
    assert v6_document.provenance.opinion_content_sha256 == approved_payload["content_sha256"]
    assert v6_document.analyst_opinion is not None
    assert v6_document.rendered_opinion is not None
    assert v6_document.rendered_opinion.protocol_figures == ()
    assert v6_document.base_publication == document
    v6_published_checkpoint = await _checkpoint(
        label="publication-v6-committed-before-repin-restart-boundary",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        opinion_id=opinion_id,
        v6_artifact_id=v6_id,
        extra={
            "v5_sha256": publication_sha256,
            "preview_pdf_sha256": hashlib.sha256(preview_v3_bytes).hexdigest(),
        },
    )

    # Rebuild the API composition after the V6 commit and before the explicit repin.
    after_v6_composition = composition_factory(
        uow_factory=uow_factory,
        artifact_store=production.artifact_store,
        settings=base_settings,
        transport=transport,
        virustotal=vt,
        shodan=shodan,
    )
    app, registry, dispatcher = await _new_combined_app(
        driver,
        artifact_store=production.artifact_store,
        composition=after_v6_composition,
        typst_binary=typst_binary,
    )

    # Replay the publication request, then apply one explicit V6 repin.
    replay_publish = await publish_opinion_http(
        driver.client,
        opinion_id,
        version=approved_payload["version"],
        base_publication_sha256=publication_sha256,
    )
    assert replay_publish.status_code == 200, replay_publish.text
    assert UUID(replay_publish.json()["publication"]["artifact_id"]) == v6_id
    async with uow_factory() as uow:
        publications_after_replay = [
            item
            for item in await uow.production_artifacts.list_for_run(production.run_id)
            if item.stage is ProductionArtifactStage.PUBLICATION
        ]
    assert sum(item.id == v6_id for item in publications_after_replay) == 1
    composition_service = EditionCompositionService(uow_factory)
    repin_key = f"w18-explicit-repin-{production.edition.id}-{v6_id}"
    current_read = await composition_service.get(production.edition.id)
    assert current_read.composition is not None
    entries_v6 = tuple(
        replace(
            item,
            artifact_id=v6_artifact.id,
            artifact_version=v6_artifact.version,
            artifact_input_hash=v6_artifact.input_hash,
            document_sha256=v6_blob.descriptor.sha256,
        )
        if item.subject_id == subject_id
        else item
        for item in current_read.composition.entries
    )
    repinned = await composition_service.replace_entries(
        production.edition.id,
        entries_v6,
        expected_version=current_read.composition.version,
        idempotency_key=repin_key,
        actor_id="human:w18-reviewer",
        correlation_id=f"w18-repin:{production.edition.id}:{v6_id}",
    )
    repin_replay = await composition_service.replace_entries(
        production.edition.id,
        entries_v6,
        expected_version=current_read.composition.version,
        idempotency_key=repin_key,
        actor_id="human:w18-reviewer",
        correlation_id=f"w18-repin:{production.edition.id}:{v6_id}",
    )
    assert repinned.version == composition_v5_version + 1
    assert repin_replay.version == repinned.version
    assert repin_replay.entries == repinned.entries
    repinned_checkpoint = await _checkpoint(
        label="after-v6-repin-before-final-render-restart-boundary",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        opinion_id=opinion_id,
        v6_artifact_id=v6_id,
        extra={"repin_composition_version": repinned.version},
    )
    assert v6_published_checkpoint["composition"]["version"] == composition_v5_version
    assert repinned_checkpoint["composition"]["version"] == composition_v5_version + 1
    assert repinned_checkpoint["composition"]["entries"][0]["artifact_id"] == str(v6_id)

    after_repin_composition = composition_factory(
        uow_factory=uow_factory,
        artifact_store=production.artifact_store,
        settings=base_settings,
        transport=transport,
        virustotal=vt,
        shodan=shodan,
    )
    app, registry, dispatcher = await _new_combined_app(
        driver,
        artifact_store=production.artifact_store,
        composition=after_repin_composition,
        typst_binary=typst_binary,
    )

    await driver.review(expect_can_accept=True)
    await driver.validate()
    accepted_v6 = await driver.accept()
    release_v6 = await driver.assemble()
    # Simulate a new renderer process after repin; only the durable release and manifest are input.
    _publication_renderer_after_restart, edition_renderer_after_restart, _media_after_restart = (
        _renderer(
            uow_factory=uow_factory,
            artifact_store=production.artifact_store,
            binary=typst_binary,
        )
    )
    rendered_v6 = await edition_renderer_after_restart.render_pdf(release_v6.id)
    assert rendered_v6.output_blob_id is not None and rendered_v6.output_sha256 is not None
    final_pdf_bytes = await production.artifact_store.read_bytes(rendered_v6.output_blob_id)
    assert hashlib.sha256(final_pdf_bytes).hexdigest() == rendered_v6.output_sha256
    final_pdf_text, final_pdf_pages = await _inspect_pdf(
        final_pdf_bytes,
        home_output / "edition-v6-final.pdf",
        pdftotext=pdftotext,
        pdftoppm=pdftoppm,
    )
    final_normalized = final_pdf_text.casefold().replace(chr(0x2019), "'")
    assert "avis de l'analyste" in final_normalized
    assert "retouche w18 v3" in final_normalized
    # Long hashes in prose may wrap across lines; compare the full value without layout whitespace.
    final_compact = "".join(final_pdf_text.split())
    assert sample_material.seed_hash in final_compact
    assert sample_material.sample_hash in final_compact
    assert sample_material.ip in final_pdf_text
    assert any(
        item["source"] == "synthetic-entry-article-v5"
        for item in resumed_checkpoint["investigation"]["observations"]
    )
    assert final_pdf_pages
    async with uow_factory() as uow:
        persisted_release = await uow.edition_releases.get(release_v6.id)
        persisted_manifest = await uow.publication_manifests.get(accepted_v6.manifest.id)
        persisted_render = await uow.edition_renders.get_latest_succeeded_for_release(release_v6.id)
        final_composition = await uow.edition_compositions.get_for_edition(production.edition.id)
    assert persisted_release is not None
    assert persisted_manifest is not None
    assert persisted_render is not None and persisted_render.id == rendered_v6.id
    assert final_composition is not None and final_composition.version == repinned.version
    assert final_composition.entries[0].artifact_id == v6_id
    final_checkpoint = await _checkpoint(
        label="after-final-render-from-rebuilt-renderer",
        driver=driver,
        production=production,
        discovery_batch_id=discovery.id,
        subject_id=subject_id,
        journal=checkpoint_journal,
        journal_path=checkpoint_path,
        production_instances=tuple(production_instances),
        vt=vt,
        shodan=shodan,
        transport=transport,
        investigation_id=investigation_id,
        opinion_id=opinion_id,
        v6_artifact_id=v6_id,
        extra={
            "preview_pdf_sha256": hashlib.sha256(preview_v3_bytes).hexdigest(),
            "final_pdf_sha256": rendered_v6.output_sha256,
            "final_release_id": str(release_v6.id),
            "final_manifest_id": str(accepted_v6.manifest.id),
            "final_render_id": str(rendered_v6.id),
        },
    )
    assert final_checkpoint["v6_publication"]["id"] == str(v6_id)
    assert hashlib.sha256(preview_v3_bytes).hexdigest() != rendered_v6.output_sha256
    assert len(checkpoint_journal) >= 6
    assert {item["label"] for item in checkpoint_journal} >= {
        "before-production-synthesis-failure",
        "after-production-synthesis-failure-before-retry",
        "after-production-recovery-new-runner",
        "before-pivot-interrupt-second-tool-running",
        "after-pivot-interrupt-second-tool-needs-review",
        "after-pivot-worker-restart-reconcile-and-resume",
        "publication-v6-committed-before-repin-restart-boundary",
        "after-v6-repin-before-final-render-restart-boundary",
        "after-final-render-from-rebuilt-renderer",
    }
    assert len({item["id"] for item in final_checkpoint["artifacts"]}) == len(
        final_checkpoint["artifacts"]
    )
    assert accepted_v5.manifest.content_sha256 != accepted_v6.manifest.content_sha256
    assert publication_sha256 == v6_document.provenance.base_publication_sha256
    assert len(preview_v1_pages) == len(preview_v3_pages) >= 1
    assert len(final_pdf_pages) >= 1
    assert checkpoint_path.is_file()
