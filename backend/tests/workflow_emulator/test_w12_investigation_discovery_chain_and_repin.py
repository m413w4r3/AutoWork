"""FULL PostgreSQL proof of discovery, chain qualification, and human repin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_rendering import EDITION_RENDERER_MANIFEST, EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_execution import load_typst_render_bundle_snapshots
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.investigation import (
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs
from tests.test_investigation_vt_tools import _page
from tests.workflow_emulator import invariants
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    article_document_v5,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    decision,
    investigation_settings,
    persist_sample_association_fixture,
    publish_opinion_http,
    qualify_sample_http,
    read_composition,
    read_manifest,
    revise_opinion_http,
    run_http_job,
    seed_investigation_for_existing_article,
    start_investigation_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


def _extract_pdf_text(pdf_bytes: bytes, temp_root: Path) -> str:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise AssertionError("W12 requires Poppler pdftotext")
    temp_root.mkdir(parents=True, exist_ok=True)
    pdf_path = temp_root / "edition.pdf"
    pdf_path.write_bytes(pdf_bytes)
    result = subprocess.run(
        [executable, str(pdf_path), "-"],
        check=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return result.stdout.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_w12_discovery_chain_claim_review_and_explicit_repin(
    workflow_driver: WorkflowDriver,
    uow_factory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W12-investigation-discovery-chain-repin")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        edition, artifact_store, subjects, batches = await driver._perform(
            "seed_independent_articles",
            lambda: _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            ),
            via="fixture",
            action_name="seed independent A/B/C runs and completed production batches",
            assertions=("three-distinct-runs", "three-distinct-production-batches"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)

        async def add_v5_publications() -> tuple[dict[str, Any], dict[str, ProductionArtifact]]:
            runs: dict[str, Any] = {}
            publications: dict[str, ProductionArtifact] = {}
            for label in ("A", "B", "C"):
                async with uow_factory() as uow:
                    subject_runs = await uow.production_runs.list_for_subject(subjects[label].id)
                if len(subject_runs) != 1:
                    raise AssertionError(f"W12 expects one independent run for {label}")
                run = subject_runs[0]
                runs[label] = run
                document = article_document_v5(subjects[label].id, label, version="W12")
                blob_id, document_sha = await artifact_store.put_canonical_json(
                    serialize_publication_document(document), bucket="w12-v5-publications"
                )
                publication = ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subjects[label].id,
                    stage=ProductionArtifactStage.PUBLICATION,
                    version=2,
                    input_hash=document_sha,
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=blob_id,
                )
                async with uow_factory() as uow:
                    await uow.production_artifacts.append(publication)
                    await uow.commit()
                publications[label] = publication
            return runs, publications

        runs, base_publications = await driver._perform(
            "seed_verified_v5_articles",
            add_v5_publications,
            via="fixture",
            action_name="persist independent V5 publications for A, B, and C",
            identities={"edition": edition.id},
            assertions=("all-publications-verified", "publication-run-identities-independent"),
        )
        invariants.require(
            len({runs[label].id for label in ("A", "B", "C")}) == 3
            and len({batches[label] for label in ("A", "B", "C")}) == 3,
            driver.scenario,
            "seed_verified_v5_articles",
            "A/B/C must remain attached to separate production runs and batches",
        )
        await driver.record_seed(
            {label: subjects[label].id for label in ("A", "B", "C")},
            mechanism="three independently seeded production batches with verified V5 artifacts",
        )
        await driver.compose(
            tuple(subjects[label].id for label in ("A", "B", "C")),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted_v1 = await driver.accept()
        release_v1 = await driver.assemble()
        composition_v1 = await read_composition(uow_factory, edition.id)
        if composition_v1 is None:
            raise AssertionError("W12 initial composition is missing")
        manifest_v1 = accepted_v1.manifest

        assert base_publications["A"].canonical_blob_id is not None
        seed = await seed_investigation_for_existing_article(
            uow_factory,
            artifact_store,
            edition=edition,
            subject=subjects["A"],
            run=runs["A"],
            publication=base_publications["A"],
            publication_bytes=await artifact_store.read_bytes(
                base_publications["A"].canonical_blob_id
            ),
            material=material,
            # The dropped payload is deliberately NOT named in the article: its hash must be
            # absent from the verified snapshot for the new-to-article demonstration (D1).
            article_claim=(
                f"C1: {material.seed_hash} is a loader and its dropped file is the final payload."
            ),
        )
        assert seed.seed_observation is not None
        await persist_sample_association_fixture(
            uow_factory,
            artifact_store,
            investigation_id=seed.investigation.id,
            subject_id=seed.subject.id,
            sample_sha256=material.seed_hash,
            origin=SampleOrigin.SEED,
        )
        await persist_sample_association_fixture(
            uow_factory,
            artifact_store,
            investigation_id=seed.investigation.id,
            subject_id=seed.subject.id,
            sample_sha256=material.sample_hash,
            origin=SampleOrigin.VT_PIVOT,
        )

        relationship_pages = [
            _page(
                (
                    {
                        "id": material.sample_hash,
                        "type": "file",
                        "attributes": {"meaningful_name": "synthetic-stage-two.bin"},
                    },
                )
            ),
            _page(
                (
                    {
                        "id": material.seed_hash,
                        "type": "file",
                        "attributes": {"meaningful_name": "synthetic-loader.bin"},
                    },
                )
            ),
        ]
        vt = HashCheckedVirusTotal(material=material, relationships=relationship_pages)

        async def final_findings(observations):
            async with uow_factory() as uow:
                invocations = await uow.tool_invocations.list_for_investigation(
                    seed.investigation.id
                )
            relation_refs = tuple(
                item.id for item in observations if item.kind is EvidenceKind.VT_RELATIONSHIP
            )
            candidate_refs = tuple(
                item.id for item in observations if item.object_id == material.sample_hash
            )
            analysis_observation_refs = {
                item.id
                for item in observations
                if item.kind is EvidenceKind.BINARY_ANALYSIS
                and item.object_id == material.sample_hash
            }
            analysis_refs = tuple(
                ref
                for invocation in invocations
                if invocation.tool_name == "sample.analyze"
                for ref in invocation.response_refs
                if ref in analysis_observation_refs
            )
            assert len(relation_refs) >= 2 and candidate_refs and analysis_refs
            cited = tuple(dict.fromkeys((*relation_refs, *analysis_refs)))
            return decision(
                "The relationship observations support the first transition; "
                "static analysis leaves the final role open.",
                finding_proposals=[
                    {
                        "kind": FindingKind.NEW_SAMPLE.value,
                        "assessment": FindingAssessment.SUPPORTED.value,
                        "statement": "The VT pivot returned a candidate absent from this article "
                        "snapshot.",
                        "observation_refs": [str(ref) for ref in candidate_refs],
                        "subject_ref": material.sample_hash,
                        "claim_refs": [],
                        "novelty": NoveltyClass.NEW_TO_ARTICLE.value,
                        "scope": "Compared only with Article A's verified snapshot.",
                        "limitations": [
                            "Absence from this article does not establish worldwide novelty."
                        ],
                    },
                    {
                        "kind": FindingKind.INFECTION_CHAIN.value,
                        "assessment": FindingAssessment.SUPPORTED.value,
                        "statement": "VT links S1 to S2 through dropped_files and parents.",
                        "observation_refs": [str(ref) for ref in relation_refs],
                        "subject_ref": material.seed_hash,
                        "claim_refs": [],
                        "novelty": None,
                        "scope": "The persisted VT relationship pages support one S1-to-S2 "
                        "transition.",
                        "limitations": [
                            "S2's execution as the final payload remains a hypothesis."
                        ],
                    },
                    {
                        "kind": FindingKind.CLAIM_VERIFICATION.value,
                        "assessment": FindingAssessment.QUALIFIED.value,
                        "statement": "C1 is partly supported by the file relation and qualified "
                        "by local static features.",
                        "observation_refs": [str(ref) for ref in cited],
                        "claim_refs": ["L1.1"],
                        "novelty": None,
                        "scope": "The relation supports the transition; static evidence does not "
                        "prove runtime payload use.",
                        "limitations": [
                            "No dynamic execution observation establishes S2's final role."
                        ],
                    },
                    {
                        "kind": FindingKind.CAPABILITY.value,
                        "assessment": FindingAssessment.SUPPORTED.value,
                        "statement": "Unreferenced Bridge output must not become a verified "
                        "finding.",
                        "observation_refs": [],
                        "claim_refs": [],
                        "novelty": None,
                        "scope": "Negative assertion for the finding guard.",
                        "limitations": [],
                    },
                ],
                next_action="finish",
            )

        responses = [
            decision(
                "Read the documented seed before pivoting.",
                tool_call={
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": material.seed_hash},
                    "purpose": "Read the verified article's S1 seed report.",
                },
            ),
            decision(
                "Search the VT index for a candidate marker.",
                tool_call={
                    "tool_name": "vt.intelligence_search",
                    "schema_version": "1",
                    "arguments": {
                        "query": 'type:peexe content:"AUTOWORK-SYNTHETIC"',
                        "justification": "Find a related candidate for the verified S1 seed.",
                        "evidence_refs": [str(seed.seed_observation.id)],
                    },
                    "purpose": "Pivot from S1 into the synthetic VT search index.",
                },
            ),
            decision(
                "Inspect dropped children of S1.",
                tool_call={
                    "tool_name": "vt.relationships",
                    "schema_version": "1",
                    "arguments": {
                        "object_type": "file",
                        "object_id": material.seed_hash,
                        "relation": "dropped_files",
                    },
                    "purpose": "Read the S1 dropped_files relationship.",
                },
            ),
            decision(
                "Check the candidate's parent relationship.",
                tool_call={
                    "tool_name": "vt.relationships",
                    "schema_version": "1",
                    "arguments": {
                        "object_type": "file",
                        "object_id": material.sample_hash,
                        "relation": "execution_parents",
                    },
                    "purpose": "Read the S2 parents relationship.",
                },
            ),
            decision(
                "Acquire the absent candidate under the approved envelope.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "arguments": {
                        "sha256": material.sample_hash,
                        "purpose": "Acquire the S2 candidate returned by the search.",
                        "evidence_refs": [str(seed.seed_observation.id)],
                    },
                    "purpose": "Download S2 to quarantine for local inspection.",
                },
            ),
        ]
        transport = ScriptedInvestigationTransport(
            responses,
            uow_factory=uow_factory,
            investigation_id=seed.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=True,
            final_response_builder=final_findings,
        )
        settings = investigation_settings(providers=True, local_analysis=True, relationships=True)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            include_local_analysis=True,
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            composition_factory=factory,
        )

        engine = create_postgres_engine(migrated_postgres_url)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                started = await driver._perform(
                    "start_investigation_http",
                    lambda: start_investigation_http(client, seed),
                    via="http",
                    action_name="POST verified Article A to the investigation API",
                    identities={"subject": seed.subject.id, "synthesis": seed.synthesis.id},
                    assertions=("verified-synthesis-identity-checked",),
                )
                investigation_id = seed.investigation.id
                envelope = await driver._perform(
                    "approve_g2_http",
                    lambda: approve_envelope_http(
                        client,
                        investigation_id,
                        providers=("vt",),
                        budget_limits={"bridge_turns": 12, "vt_units": 24, "downloads": 1},
                        allow_acquisition=True,
                        max_acquire_volume=1,
                    ),
                    via="http",
                    action_name="human HTTP approval of the G2 research envelope",
                    identities={"investigation": investigation_id},
                    assertions=("g2-is-human-and-versioned",),
                )
                invariants.require(
                    envelope.status_code == 200 and envelope.json()["approved"] is True,
                    driver.scenario,
                    "approve_g2_http",
                    f"G2 API approval failed: {envelope.text}",
                )
                await driver._perform(
                    "run_investigation_job",
                    lambda: run_http_job(
                        app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                    ),
                    via="service",
                    action_name="execute the durable investigation job with scripted VT and Bridge",
                    identities={"job": started["job_id"], "investigation": investigation_id},
                    assertions=("real-job-and-tool-dispatcher", "no-external-provider-call"),
                )
                sample_response = await driver._perform(
                    "read_candidate_http",
                    lambda: client.get(f"/api/investigations/{investigation_id}/samples"),
                    via="http",
                    action_name="read candidate qualification through the investigation API",
                    identities={"investigation": investigation_id},
                    assertions=("candidate-is-not-auto-confirmed",),
                )
                invariants.require(
                    sample_response.status_code == 200,
                    driver.scenario,
                    "read_candidate_http",
                    sample_response.text,
                )
                sample_views = sample_response.json()
                invariants.require(
                    {item["sample_sha256"] for item in sample_views}
                    == {material.seed_hash, material.sample_hash},
                    driver.scenario,
                    "read_candidate_http",
                    "the VT source seed and acquired S2 association must both be persisted",
                )
                sample_view = next(
                    item for item in sample_views if item["sample_sha256"] == material.sample_hash
                )
                invariants.require(
                    sample_view["sample_sha256"] == material.sample_hash
                    and sample_view["qualification"] == SampleQualification.CANDIDATE.value,
                    driver.scenario,
                    "read_candidate_http",
                    "automatic acquisition and analysis must leave S2 at candidate",
                )
                async with uow_factory() as uow:
                    association = await uow.sample_associations.get(UUID(sample_view["id"]))
                    observations = await uow.evidence_observations.list_for_investigation(
                        investigation_id
                    )
                    findings = await uow.investigation_findings.list_for_investigation(
                        investigation_id
                    )
                    invocations = await uow.tool_invocations.list_for_investigation(
                        investigation_id
                    )
                if association is None:
                    raise AssertionError("W12 candidate association was not persisted")
                invariants.require(
                    association.sample_sha256 == material.sample_hash
                    and association.origin is SampleOrigin.VT_PIVOT,
                    driver.scenario,
                    "read_candidate_http",
                    "S2 remains linked to its VT pivot association after acquisition",
                )
                new_sample = next(item for item in findings if item.kind is FindingKind.NEW_SAMPLE)
                chain = next(item for item in findings if item.kind is FindingKind.INFECTION_CHAIN)
                claim = next(
                    item for item in findings if item.kind is FindingKind.CLAIM_VERIFICATION
                )
                unreferenced = next(
                    item
                    for item in findings
                    if item.kind is FindingKind.CAPABILITY
                    and item.statement.startswith("Unreferenced Bridge")
                )
                relation_observations = [
                    item for item in observations if item.kind is EvidenceKind.VT_RELATIONSHIP
                ]
                analysis_refs = {
                    ref
                    for item in invocations
                    if item.tool_name == "sample.analyze"
                    for ref in item.response_refs
                }
                invariants.require(
                    new_sample.novelty is NoveltyClass.NEW_TO_ARTICLE
                    and any("worldwide novelty" in value for value in new_sample.limitations),
                    driver.scenario,
                    "persisted_findings",
                    "S2 must be article-relative new and explicitly limited against global novelty",
                )
                invariants.require(
                    all(
                        observation.object_id != material.seed_hash
                        for observation in observations
                        if observation.id in new_sample.observation_refs
                    ),
                    driver.scenario,
                    "persisted_findings",
                    "the article's S1 seed must never be classified as new",
                )
                invariants.require(
                    len(relation_observations) >= 2
                    and chain.assessment is FindingAssessment.SUPPORTED
                    and set(chain.observation_refs).issubset(
                        {item.id for item in relation_observations}
                    )
                    and any("hypothesis" in value for value in chain.limitations),
                    driver.scenario,
                    "persisted_findings",
                    "the dropped_files/parents observations must support a transition while "
                    "leaving final payload as a hypothesis",
                )
                invariants.require(
                    claim.assessment is FindingAssessment.QUALIFIED
                    and claim.claim_refs == ("L1.1",)
                    and set(claim.observation_refs).intersection(analysis_refs)
                    and set(claim.observation_refs).intersection(
                        {item.id for item in relation_observations}
                    ),
                    driver.scenario,
                    "persisted_findings",
                    "C1 must cite favorable relationship and contradictory local analysis "
                    "evidence and remain qualified",
                )
                invariants.require(
                    unreferenced.assessment is FindingAssessment.INCONCLUSIVE,
                    driver.scenario,
                    "persisted_findings",
                    "supported Bridge output without observation refs must be downgraded",
                )
                required_tools = {
                    "vt.file_report",
                    "vt.intelligence_search",
                    "vt.download_sample",
                    "sample.analyze",
                    "vt.relationships",
                }
                actual_tools = {item.tool_name for item in invocations}
                invocation_statuses = [(item.tool_name, item.status.value) for item in invocations]
                invariants.require(
                    actual_tools >= required_tools
                    and all(
                        item.status is ToolInvocationStatus.SUCCEEDED
                        # PARTIAL: optional code-feature/goodware extractors are not provisioned
                        # in the harness; the static analysis observation is still persisted.
                        or (
                            item.tool_name == "sample.analyze"
                            and item.status is ToolInvocationStatus.PARTIAL
                        )
                        for item in invocations
                    ),
                    driver.scenario,
                    "persisted_findings",
                    "the real dispatcher must persist each selected tool operation; "
                    f"missing={sorted(required_tools - actual_tools)}, "
                    f"statuses={invocation_statuses}",
                )
                g3 = await driver._perform(
                    "confirm_candidate_g3_http",
                    lambda: qualify_sample_http(
                        client,
                        investigation_id,
                        association.id,
                        expected_version=association.version,
                        qualification=SampleQualification.CONFIRMED,
                        evidence_refs=tuple(
                            item.id
                            for item in observations
                            if item.object_id == material.sample_hash
                        ),
                    ),
                    via="http",
                    action_name="human G3 qualification of S2 via the public API",
                    identities={"association": association.id},
                    assertions=("candidate-changes-only-after-human-g3",),
                )
                invariants.require(
                    g3.status_code == 200
                    and g3.json()["association"]["qualification"]
                    == SampleQualification.CONFIRMED.value,
                    driver.scenario,
                    "confirm_candidate_g3_http",
                    f"G3 confirmation failed: {g3.text}",
                )
                draft_response = await driver._perform(
                    "compose_opinion_http",
                    lambda: compose_opinion_http(client, investigation_id),
                    via="http",
                    action_name="compose the supported discovery and C1 review into an opinion",
                    identities={"investigation": investigation_id},
                    assertions=("composition-cites-persisted-findings",),
                )
                invariants.require(
                    draft_response.status_code == 200,
                    driver.scenario,
                    "compose_opinion_http",
                    draft_response.text,
                )
                draft = draft_response.json()["draft"]
                invalid = await driver._perform(
                    "reject_nonexistent_opinion_citation",
                    lambda: revise_opinion_http(
                        client,
                        UUID(draft["id"]),
                        expected_version=draft["version"],
                        blocks=(
                            {
                                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                                "text": "Uncited synthetic assertion.",
                                "finding_refs": [str(uuid4())],
                                "evidence_refs": [str(uuid4())],
                                "qualification": OpinionClaimQualification.FACT.value,
                                "certainty": 0.99,
                            },
                        ),
                    ),
                    via="http",
                    action_name="attempt revision with a nonexistent citation",
                    identities={"opinion": draft["id"]},
                    assertions=("nonexistent-citation-is-rejected-by-api",),
                )
                invariants.require(
                    invalid.status_code == 422
                    and invalid.json().get("detail", {}).get("code") == "citations_invalid",
                    driver.scenario,
                    "reject_nonexistent_opinion_citation",
                    f"nonexistent opinion citation was not rejected: {invalid.text}",
                )
                relation_refs = tuple(item.id for item in relation_observations)
                candidate_refs = tuple(
                    item.id for item in observations if item.object_id == material.sample_hash
                )
                valid_blocks = (
                    {
                        "block_type": OpinionBlockType.ADDITIONAL_SAMPLES.value,
                        "text": (
                            f"Découverte : {material.sample_hash} est absent de l'article A et "
                            "reste une découverte relative à son snapshot."
                        ),
                        "finding_refs": [str(new_sample.id)],
                        "evidence_refs": [str(ref) for ref in candidate_refs],
                        "qualification": OpinionClaimQualification.FACT.value,
                        "certainty": 0.92,
                    },
                    {
                        "block_type": OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES.value,
                        "text": (
                            "La relation dropped_files étaye la transition S1 vers S2; l'emploi de "
                            "S2 comme payload final demeure une hypothèse non prouvée."
                        ),
                        "finding_refs": [str(chain.id)],
                        "evidence_refs": [str(ref) for ref in relation_refs],
                        "qualification": OpinionClaimQualification.FACT.value,
                        "certainty": 0.86,
                    },
                    {
                        "block_type": OpinionBlockType.CLAIM_VERIFICATION.value,
                        "text": (
                            "C1 est nuancée : VT favorise le premier chaînon, tandis "
                            "que l'analyse statique ne démontre pas le rôle de payload final."
                        ),
                        "finding_refs": [str(claim.id)],
                        "evidence_refs": [str(ref) for ref in claim.observation_refs],
                        "qualification": OpinionClaimQualification.INFERENCE.value,
                        "certainty": 0.68,
                    },
                )
                revised = await driver._perform(
                    "revise_opinion_http",
                    lambda: revise_opinion_http(
                        client,
                        UUID(draft["id"]),
                        expected_version=draft["version"],
                        blocks=valid_blocks,
                        unresolved_points=(
                            "Aucun résultat dynamique n'établit l'usage final de S2.",
                        ),
                    ),
                    via="http",
                    action_name="revise the opinion with cited discovery and qualified C1 blocks",
                    identities={"opinion": draft["id"]},
                    assertions=(
                        "opinion-revision-is-cas-versioned",
                        "citations-resolve-to-real-observations",
                    ),
                )
                invariants.require(
                    revised.status_code == 200, driver.scenario, "revise_opinion_http", revised.text
                )
                revised_body = revised.json()["draft"]
                approved = await driver._perform(
                    "approve_opinion_http",
                    lambda: approve_opinion_http(
                        client,
                        UUID(revised_body["id"]),
                        version=revised_body["version"],
                        sha256=revised_body["content_sha256"],
                    ),
                    via="http",
                    action_name="approve the exact revised opinion version and content hash",
                    identities={"opinion": revised_body["id"], "version": revised_body["version"]},
                    assertions=("approved-version-and-hash-are-exact",),
                )
                invariants.require(
                    approved.status_code == 200,
                    driver.scenario,
                    "approve_opinion_http",
                    approved.text,
                )
                approved_body = approved.json()["approved"]
                published = await driver._perform(
                    "publish_opinion_http",
                    lambda: publish_opinion_http(
                        client,
                        UUID(approved_body["id"]),
                        version=approved_body["version"],
                        base_publication_sha256=approved_body["provenance"][
                            "base_publication_sha256"
                        ],
                    ),
                    via="http",
                    action_name="publish a new V6 artifact through the analyst opinion API",
                    identities={
                        "opinion": approved_body["id"],
                        "version": approved_body["version"],
                    },
                    assertions=("new-v6-publication-created", "composition-repin-remains-required"),
                )
                invariants.require(
                    published.status_code == 200
                    and published.json()["composition_repin_required"] is True,
                    driver.scenario,
                    "publish_opinion_http",
                    f"opinion publication failed: {published.text}",
                )
                publication_payload = published.json()["publication"]
                v6_id = UUID(publication_payload["artifact_id"])
                async with uow_factory() as uow:
                    stored_base = await uow.production_artifacts.get(base_publications["A"].id)
                    stored_v6 = await uow.production_artifacts.get(v6_id)
                    if stored_v6 is None or stored_v6.canonical_blob_id is None:
                        raise AssertionError("W12 V6 publication row/blob is missing")
                    blob = await uow.blobs.get(stored_v6.canonical_blob_id)
                invariants.require(
                    stored_base == base_publications["A"]
                    and stored_v6 is not None
                    and stored_v6.stage is ProductionArtifactStage.PUBLICATION
                    and stored_v6.status is ProductionArtifactStatus.VERIFIED
                    and publication_payload["previous_publication_artifact_id"]
                    == str(base_publications["A"].id),
                    driver.scenario,
                    "publish_opinion_http",
                    "V6 must be a separate verified PUBLICATION linked to the exact V5 base",
                )
                assert stored_v6 is not None and blob is not None
                v6_bytes = await artifact_store.read_bytes(stored_v6.canonical_blob_id)
                v6_document = publication_document_from_json(json.loads(v6_bytes))
                invariants.require(
                    isinstance(v6_document, PublicationDocumentV6)
                    and v6_document.rendered_opinion is not None,
                    driver.scenario,
                    "publish_opinion_http",
                    "the new artifact must contain the revised analyst opinion",
                )

                async def read_release_snapshot(release_id, manifest_id, render_id):
                    async with uow_factory() as uow:
                        release = await uow.edition_releases.get(release_id)
                        manifest = await uow.publication_manifests.get(manifest_id)
                        manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                        render = await uow.edition_renders.get(render_id)
                    if (
                        release is None
                        or manifest is None
                        or manifest_blob_id is None
                        or render is None
                    ):
                        raise AssertionError("W12 release snapshot is incomplete")
                    if render.output_blob_id is None:
                        raise AssertionError("W12 render row has no PDF blob")
                    manifest_bytes = await artifact_store.read_bytes(manifest_blob_id)
                    document_bytes = await artifact_store.read_bytes(
                        release.edition_document_blob_id
                    )
                    pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
                    invariants.require(
                        hashlib.sha256(document_bytes).hexdigest()
                        == release.edition_document_sha256
                        and hashlib.sha256(pdf_bytes).hexdigest() == render.output_sha256,
                        driver.scenario,
                        "read_release_snapshot",
                        "historical document or PDF hash differs from PostgreSQL",
                    )
                    return release, manifest, document_bytes, manifest_bytes, pdf_bytes

                typst_binary = os.environ.get("TYPST_BINARY", "typst")
                binary_path = shutil.which(typst_binary)
                default_template_root, default_font_root, fonts_lock = typst_bundle_paths()
                template_root = Path(os.environ.get("CHP_TYPST_ROOT", str(default_template_root)))
                font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(default_font_root)))
                invariants.require(
                    binary_path is not None
                    and await asyncio.to_thread(
                        lambda: (
                            template_root.is_dir() and font_root.is_dir() and fonts_lock.is_file()
                        )
                    ),
                    driver.scenario,
                    "real_typst_prerequisites",
                    "Typst, templates, fonts, and typst-fonts.lock are required",
                )
                load_typst_render_bundle_snapshots(
                    chp_typst_root=template_root,
                    font_bundle_root=font_root,
                    typst_fonts_lock_path=fonts_lock,
                    manifest_name=EDITION_RENDERER_MANIFEST,
                )
                poppler = PopplerSubprocess(temp_root=tmp_path / "poppler")
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
                pdf_inspector = PdfInspectionService(
                    __import__(
                        "cti_app.infrastructure.database.repositories.pdf_inspection",
                        fromlist=["SqlAlchemyPdfInspectionSourceRepository"],
                    ).SqlAlchemyPdfInspectionSourceRepository(create_session_factory(engine)),
                    artifact_store,
                    poppler,
                    PdfInspectionCache(tmp_path / "pdf-inspection-cache"),
                )

                async def render_release(release, suffix: str):
                    rendered = await render_service.render_pdf(release.id)
                    invariants.require(
                        rendered.status is TypstRenderStatus.SUCCEEDED
                        and rendered.output_blob_id is not None
                        and rendered.output_sha256 is not None,
                        driver.scenario,
                        f"render_{suffix}",
                        f"real Typst render failed: {rendered!r}",
                    )
                    pdf_bytes = await artifact_store.read_bytes(rendered.output_blob_id)
                    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
                    metadata = await pdf_inspector.metadata(pdf_sha)
                    diagnostic_codes = {item["code"] for item in metadata["diagnostics"]["items"]}
                    invariants.require(
                        pdf_sha == rendered.output_sha256
                        and metadata["pdf"]["page_count"] > 0
                        and metadata["diagnostics"]["status"] == "ready"
                        and "pdf_integrity_verified" in diagnostic_codes,
                        driver.scenario,
                        f"render_{suffix}",
                        "real Poppler integrity inspection failed",
                    )
                    text = await asyncio.to_thread(
                        _extract_pdf_text, pdf_bytes, tmp_path / f"pdftotext-{suffix}"
                    )
                    return rendered, pdf_bytes, text

                rendered_v1, pdf_v1, text_v1 = await driver._perform(
                    "render_v1_real_typst_poppler",
                    lambda: render_release(release_v1, "v1"),
                    via="service",
                    action_name=(
                        "render the V5-only manifest with real Typst and inspect with Poppler"
                    ),
                    identities={"release": release_v1.id, "manifest": manifest_v1.id},
                    assertions=("typst-pdf-v1-real", "poppler-v1-integrity-and-text"),
                )
                text_v1_folded = text_v1.casefold()
                invariants.require(
                    "avis de l'analyste" not in text_v1_folded
                    and all(f"Corps article {label} W12" in text_v1 for label in ("A", "B", "C")),
                    driver.scenario,
                    "render_v1_real_typst_poppler",
                    "the initial PDF must contain A/B/C and no analyst opinion",
                )
                v1_snapshot = await read_release_snapshot(
                    release_v1.id, manifest_v1.id, rendered_v1.id
                )
                release_v1_row, manifest_v1_row, document_v1, manifest_v1_bytes, pdf_v1_snapshot = (
                    v1_snapshot
                )
                composition_after_publish = await read_composition(uow_factory, edition.id)
                manifest_after_publish = await read_manifest(uow_factory, edition.id)
                invariants.require(
                    composition_after_publish == composition_v1
                    and manifest_after_publish == manifest_v1_row
                    and manifest_v1_row == manifest_v1
                    and pdf_v1_snapshot == pdf_v1,
                    driver.scenario,
                    "approved_opinion_without_repin",
                    "approving and publishing V6 must leave the composition, manifest, and PDF v1 "
                    "unchanged",
                )
                assert seed.publication.canonical_blob_id is not None
                invariants.require(
                    await artifact_store.read_bytes(seed.publication.canonical_blob_id)
                    == seed.publication_bytes,
                    driver.scenario,
                    "approved_opinion_without_repin",
                    "the original V5 blob must remain byte-identical",
                )

                composition_service = EditionCompositionService(uow_factory)

                async def human_repin_a():
                    current = await composition_service.get(edition.id)
                    if current.composition is None:
                        raise AssertionError("W12 composition is missing at repin")
                    entries = tuple(
                        replace(
                            entry,
                            artifact_id=stored_v6.id,
                            artifact_version=stored_v6.version,
                            artifact_input_hash=stored_v6.input_hash,
                            document_sha256=blob.descriptor.sha256,
                        )
                        if entry.subject_id == subjects["A"].id
                        else entry
                        for entry in current.composition.entries
                    )
                    return await composition_service.replace_entries(
                        edition.id,
                        entries,
                        expected_version=current.composition.version,
                        idempotency_key=f"w12-human-repin-{uuid4()}",
                        actor_id="human:w12-editor",
                        correlation_id=f"w12-repin:{uuid4()}",
                    )

                composition_v2 = await driver._perform(
                    "human_repin_a_v6",
                    human_repin_a,
                    via="service",
                    action_name="human EditionCompositionService CAS mutation repins A to V6",
                    identities={"edition": edition.id, "publication": stored_v6.id},
                    assertions=("explicit-human-repin", "composition-version-increments-once"),
                )
                invariants.require(
                    composition_v2.version == composition_v1.version + 1,
                    driver.scenario,
                    "human_repin_a_v6",
                    "the human repin must advance composition version once",
                )
                await driver.review(expect_can_accept=True)
                await driver.validate()
                accepted_v2 = await driver.accept()
                release_v2 = await driver.assemble()
                rendered_v2, pdf_v2, text_v2 = await driver._perform(
                    "render_v2_real_typst_poppler",
                    lambda: render_release(release_v2, "v2"),
                    via="service",
                    action_name=(
                        "render the repinned manifest with real Typst and inspect with Poppler"
                    ),
                    identities={"release": release_v2.id, "manifest": accepted_v2.manifest.id},
                    assertions=("typst-pdf-v2-real", "poppler-v2-integrity-and-opinion-text"),
                )
                current_composition = await read_composition(uow_factory, edition.id)
                if current_composition is None:
                    raise AssertionError("W12 current composition disappeared")
                current_entries = {item.subject_id: item for item in current_composition.entries}
                old_entries = {item.subject_id: item for item in composition_v1.entries}
                invariants.require(
                    current_entries[subjects["A"].id].artifact_id == stored_v6.id
                    and all(
                        current_entries[subjects[label].id] == old_entries[subjects[label].id]
                        for label in ("B", "C")
                    ),
                    driver.scenario,
                    "repin_only_a",
                    "the versioned repin must alter A while preserving B and C pins",
                )
                manifest_v2_json = accepted_v2.manifest.to_json()
                manifest_entry_v2 = {
                    str(item["subject_id"]): item for item in manifest_v2_json["entries"]
                }
                invariants.require(
                    manifest_entry_v2[str(subjects["A"].id)]["document_artifact_id"]
                    == str(stored_v6.id)
                    and all(
                        manifest_entry_v2[str(subjects[label].id)]
                        == manifest_v1.to_json()["entries"][index]
                        for index, label in ((1, "B"), (2, "C"))
                    ),
                    driver.scenario,
                    "repin_only_a",
                    "V2 manifest must retain exact B/C article entries and pin V6 for A",
                )
                normalized_v2_text = text_v2.casefold().replace(chr(0x2019), "'")
                heading = "avis de l'analyste"
                invariants.require(
                    normalized_v2_text.count(heading) == 1
                    and material.sample_hash in text_v2
                    and "C1" in text_v2
                    and "hypothèse" in normalized_v2_text
                    and "avis de l'analyste" not in text_v1.casefold(),
                    driver.scenario,
                    "render_v2_opinion_for_a_only",
                    "the real V2 PDF must render the discovery and qualified C1 opinion exactly "
                    "once",
                )
                body_a = text_v2.index("Corps article A W12")
                opinion_heading = normalized_v2_text.index(heading)
                body_b = text_v2.index("Corps article B W12")
                body_c = text_v2.index("Corps article C W12")
                invariants.require(
                    body_a < opinion_heading < body_b < body_c,
                    driver.scenario,
                    "render_v2_opinion_for_a_only",
                    "the opinion must render inside A before unchanged articles B and C",
                )
                v2_snapshot = await read_release_snapshot(
                    release_v2.id, accepted_v2.manifest.id, rendered_v2.id
                )
                assert v2_snapshot[4] == pdf_v2
                historical = await read_release_snapshot(
                    release_v1.id, manifest_v1.id, rendered_v1.id
                )
                invariants.require(
                    historical[0] == release_v1_row
                    and historical[1] == manifest_v1_row
                    and historical[2] == document_v1
                    and historical[3] == manifest_v1_bytes
                    and historical[4] == pdf_v1_snapshot,
                    driver.scenario,
                    "historical_v1_retained",
                    "the prior manifest, release, document, and Typst PDF must remain byte-stable",
                )
                await invariants.assert_manifest_entries_verified(
                    uow_factory, edition.id, scenario=driver.scenario, step="historical_manifests"
                )
        finally:
            await engine.dispose()
