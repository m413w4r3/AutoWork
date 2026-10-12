"""FULL proof: real deterministic production of A then B, followed by the official
targeted editorial-enrichment correction service. PostgreSQL, publication assembly,
Composition, Review and Acceptance are real. The ancillary subject-preview renderer
is a recording double; this scenario does not claim Typst/PDF evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.edition_review import EditionReviewService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
    editorial_enrichment_from_json,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.production import ProductionArtifactStage, ProductionRunStatus
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from tests.integration.production.support import grounded_editorial_proposal
from tests.integration.production.test_pipeline_happy_path import (
    Q1_RESPONSE,
    Q2_EDITORIAL_CORE_RESPONSE,
    Q2_SECONDARY_RESPONSE,
    _install_canonical_synthesis,
)

from . import invariants
from .driver import WorkflowDriver
from .providers import CaptureBundle

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]
EXCLUSION_REASON = "A reste exclu le temps de vérifier sa correction éditoriale."
CORRECTION_INPUT_FIXTURE_VERSION = "workflow-w5-correction-input-v1"
EDITORIAL_RESPONSE_FIXTURE_VERSION = "workflow-w5-editorial-enrichment-v1"


class _RecordingPublicationPreviewService:
    """Record the revision service's preview request without compiling a PDF."""

    def __init__(self) -> None:
        self.publication_artifact_ids: list[UUID] = []

    async def render_preview(self, publication_artifact_id: UUID) -> None:
        self.publication_artifact_ids.append(publication_artifact_id)


async def _publication_identity(
    uow_factory: UnitOfWorkFactory,
    artifact_store: Any,
    subject_id: UUID,
    *,
    scenario: str,
    step: str,
) -> tuple[UUID, str, int, UUID, int, str, str]:
    async with uow_factory() as uow:
        runs = [
            run
            for run in await uow.production_runs.list_for_subject(subject_id)
            if run.status is ProductionRunStatus.READY
        ]
        invariants.require(
            len(runs) == 1,
            scenario,
            step,
            f"expected one READY run for {subject_id}, found {len(runs)}",
        )
        run = runs[0]
        artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
    invariants.require(
        artifact is not None and artifact.canonical_blob_id is not None,
        scenario,
        step,
        f"subject {subject_id} has no canonical PUBLICATION artifact",
    )
    assert artifact is not None and artifact.canonical_blob_id is not None
    canonical = await artifact_store.read_bytes(artifact.canonical_blob_id)
    return (
        run.id,
        run.status.value,
        run.pipeline_generation,
        artifact.id,
        artifact.version,
        artifact.input_hash,
        hashlib.sha256(canonical).hexdigest(),
    )


def _ground_capture(bundle: CaptureBundle) -> CaptureBundle:
    sources: dict[str, dict[str, object]] = {}
    for url, source in bundle.sources.items():
        body = str(source["body"]).replace("ExampleMalware-A", "ExampleRAT")
        sources[url] = {
            **source,
            "body": (
                body
                + " ExampleRAT uses the documented command-and-control domain."
                + " The report states the loader persisted in memory after execution."
            ),
        }
    references = bundle.references_response.replace("ExampleMalware-A", "ExampleRAT")
    extractions = {
        url: response.replace("ExampleMalware-A", "ExampleRAT").replace(
            "Command-and-control domain in the report.",
            "ExampleRAT uses this domain for command and control.",
        )
        for url, response in bundle.extraction_responses.items()
    }
    return replace(
        bundle,
        sources=sources,
        references_response=references,
        extraction_responses=extractions,
    )


def _w5_editorial_proposal(prompt_text: str) -> str:
    """Upgrade the shared grounded fixture to the current diagram wire contract."""
    response = grounded_editorial_proposal(prompt_text)
    pack = json.loads(prompt_text)["current_evidence_pack"]
    indicator = next(
        record
        for record in pack["technical_evidence"]
        if record.get("kind") == "indicator"
        and "examplerat" in str(record.get("evidence", "")).casefold()
    )
    domain = str(indicator["value"])
    replacements = (
        (
            "DIAGRAM D001\nKEY: reported_activity",
            "DIAGRAM D001\nPROFILE: flow\nKEY: reported_activity",
        ),
        (
            "TITLE: Infrastructure de commande et de contrôle documentée\nPURPOSE:",
            "TITLE: Infrastructure de commande et de contrôle documentée\n"
            "CAPTION: Le rapport relie ExampleRAT au domaine C2 observé.\nPURPOSE:",
        ),
        (f"LABEL: {domain}", "LABEL: C2 domain"),
        (
            "RELATION_TYPE: factual\nLABEL: uses",
            "RELATION_TYPE: factual\nDIRECTION: directed\nLABEL: uses",
        ),
    )
    for original, updated in replacements:
        if original not in response:
            raise AssertionError(
                f"W5-late-correction / editorial_fixture: "
                f"{EDITORIAL_RESPONSE_FIXTURE_VERSION} lacks {original.splitlines()[0]!r}"
            )
        response = response.replace(original, updated, 1)
    return response


@pytest.mark.asyncio
async def test_w5_late_correction_requires_explicit_repin(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Use the official targeted correction service; the preview renderer is a double."""
    driver = workflow_driver
    driver.begin("W5-late-correction")
    async with driver:
        edition_id = await driver.create_edition()
        await driver.run_discovery("P1", ("A", "B"))
        await driver.apply_fusion("P1")
        selected = await driver.select_subjects("P1", ("A", "B"))

        async def ground_a_capture() -> CaptureBundle:
            return _ground_capture(driver.discovery_bundles["A"])

        driver.discovery_bundles["A"] = await driver._perform(
            "A.capture_grounding",
            ground_a_capture,
            via="fixture",
            action_name="versioned local capture edit grounding ExampleRAT and its indicator",
            identities={"subject": selected["A"]},
            assertions=("capture-bytes-unique-to-this-scenario", "correction-evidence-grounded"),
        )

        await driver.start_production(
            "A",
            selected["A"],
            editorial_enrichment_response=_w5_editorial_proposal,
        )

        async def script_grounded_correction_inputs() -> tuple[str, ...]:
            bundle = driver.discovery_bundles["A"]
            core_url, secondary_url = tuple(bundle.sources)
            core_domain = next(
                line.split(" :: ", 1)[0].removeprefix("- ").strip()
                for line in bundle.extraction_responses[core_url].splitlines()
                if "core-" in line and " :: " in line
            )
            secondary_domain = next(
                line.split(" :: ", 1)[0].removeprefix("- ").strip()
                for line in bundle.extraction_responses[secondary_url].splitlines()
                if "secondary-" in line and " :: " in line
            )
            production = driver.production_scenarios["A"]
            production.model.script.references(
                Q1_RESPONSE.replace("https://example.test/core", core_url).replace(
                    "https://example.test/secondary", secondary_url
                )
            )
            production.model.script.q2(
                source_url=core_url,
                response=Q2_EDITORIAL_CORE_RESPONSE.replace("core-c2.security-lab.io", core_domain),
            )
            production.model.script.q2(
                source_url=secondary_url,
                response=Q2_SECONDARY_RESPONSE.replace(
                    "secondary-c2.security-lab.io", secondary_domain
                ),
            )
            return (CORRECTION_INPUT_FIXTURE_VERSION, "two-grounded-publications")

        await driver._perform(
            "A.script_correction_inputs",
            script_grounded_correction_inputs,
            via="fixture",
            action_name="script versioned two-publication editorial evidence",
            identities={"subject": selected["A"]},
            assertions=(
                "unique-capture-url-bindings",
                "deterministic-local-q1-q2-responses",
                EDITORIAL_RESPONSE_FIXTURE_VERSION,
            ),
        )

        async def install_local_synthesis() -> tuple[str, ...]:
            _install_canonical_synthesis(driver.production_scenarios["A"])
            return ("canonical-synthesis-responses-use-the-local-evidence-pack",)

        await driver._perform(
            "A.script_synthesis",
            install_local_synthesis,
            via="fixture",
            action_name="install deterministic local Synthesis response",
            identities={"subject": selected["A"]},
            assertions=("no-network-model-call",),
        )
        run_a = await driver.advance_jobs_until_terminal("A")
        identity_a_v1 = await _publication_identity(
            uow_factory,
            driver.production_scenarios["A"].artifact_store,
            selected["A"],
            scenario=driver.scenario,
            step="P1.A.ready",
        )
        invariants.require(
            run_a == identity_a_v1[0],
            driver.scenario,
            "P1.A.ready",
            "P1 did not produce A's READY run through the deterministic runner",
        )

        await driver.start_production("B", selected["B"])
        await driver.advance_jobs_until_terminal("B")
        identity_b_v1 = await _publication_identity(
            uow_factory,
            driver.production_scenarios["B"].artifact_store,
            selected["B"],
            scenario=driver.scenario,
            step="P2.B.ready",
        )

        await driver.compose((selected["A"], selected["B"]))
        async with uow_factory() as uow:
            composition_v1 = await uow.edition_compositions.get_for_edition(edition_id)
            run_a_entity = await uow.production_runs.get(run_a)
            enrichment_v1 = await uow.production_artifacts.get_current(
                run_a, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            publication_v1 = await uow.production_artifacts.get_current(
                run_a, ProductionArtifactStage.PUBLICATION.value
            )
        invariants.require(
            composition_v1 is not None and run_a_entity is not None,
            driver.scenario,
            "composition_v1",
            "composition or A's READY run is missing from PostgreSQL",
        )
        invariants.require(
            enrichment_v1 is not None
            and enrichment_v1.canonical_blob_id is not None
            and publication_v1 is not None
            and publication_v1.canonical_blob_id is not None,
            driver.scenario,
            "composition_v1",
            "A's initial enrichment or PUBLICATION artifact is missing",
        )
        assert composition_v1 is not None and run_a_entity is not None
        assert enrichment_v1 is not None and enrichment_v1.canonical_blob_id is not None
        assert publication_v1 is not None and publication_v1.canonical_blob_id is not None
        entries_v1 = {entry.subject_id: entry for entry in composition_v1.entries}
        invariants.require(
            len(entries_v1) == 2
            and entries_v1[selected["A"]].artifact_id == identity_a_v1[3]
            and entries_v1[selected["A"]].artifact_version == identity_a_v1[4]
            and entries_v1[selected["B"]].artifact_id == identity_b_v1[3]
            and entries_v1[selected["B"]].artifact_version == identity_b_v1[4],
            driver.scenario,
            "composition_v1",
            "the first composition must pin A and B's original PUBLICATION artifacts",
        )

        decision_id = await driver.exclude_subject(selected["A"], reason=EXCLUSION_REASON)
        await driver.review(
            required_exclusions={selected["A"]: ("workflow-analyst", EXCLUSION_REASON)},
            expect_can_accept=True,
        )
        async with uow_factory() as uow:
            composition_with_decision = await uow.edition_compositions.get_for_edition(edition_id)
            decisions = await uow.publication_review_decisions.list_for_edition(edition_id)
        decision_v1 = next((item for item in decisions if item.id == decision_id), None)
        invariants.require(
            composition_with_decision is not None
            and decision_v1 is not None
            and decision_v1.document_artifact_id == identity_a_v1[3]
            and decision_v1.document_artifact_version == identity_a_v1[4]
            and decision_v1.production_run_id == identity_a_v1[0],
            driver.scenario,
            "decision_v1",
            "A's exclusion decision must identify the v1 run and PUBLICATION artifact",
        )
        assert composition_with_decision is not None

        await driver.include_subject(selected["A"])
        async with uow_factory() as uow:
            composition_with_v1_included = await uow.edition_compositions.get_for_edition(
                edition_id
            )
        invariants.require(
            composition_with_v1_included is not None
            and composition_with_v1_included.version == composition_with_decision.version + 1
            and any(
                entry.subject_id == selected["A"]
                and entry.disposition is CompositionDisposition.INCLUDED
                and entry.artifact_id == publication_v1.id
                and entry.artifact_version == publication_v1.version
                for entry in composition_with_v1_included.entries
            ),
            driver.scenario,
            "include_a_v1",
            "official re-inclusion must restore A on the original v1 publication pin",
        )
        assert composition_with_v1_included is not None
        composition_with_decision = composition_with_v1_included

        enrichment_store = driver.production_scenarios["A"].artifact_store
        enrichment_bytes_v1 = await enrichment_store.read_bytes(enrichment_v1.canonical_blob_id)
        publication_bytes_v1 = await enrichment_store.read_bytes(publication_v1.canonical_blob_id)
        enrichment_payload = await enrichment_store.read_json(enrichment_v1.canonical_blob_id)
        enrichment_document = editorial_enrichment_from_json(enrichment_payload)
        diagrams = enrichment_document.diagrams
        invariants.require(
            len(diagrams) == 1,
            driver.scenario,
            "correction_target",
            "the scripted editorial proposal must persist one deletable diagram; "
            f"found {len(diagrams)}; warnings={enrichment_document.warnings!r}",
        )
        diagram_key = diagrams[0].key
        enrichment_sha_v1 = hashlib.sha256(enrichment_bytes_v1).hexdigest()
        preview_service = _RecordingPublicationPreviewService()
        publication_persistence = EditorialEnrichmentService(uow_factory, enrichment_store)
        media_assets = MediaAssetStore(enrichment_store, uow_factory)
        production = driver.production_scenarios["A"]
        revision_service = ProductionEditorialEnrichmentRevisionService(
            enrichment_service=ProductionEditorialEnrichmentService(
                uow_factory=uow_factory,
                artifact_store=enrichment_store,
                model_gateway=production.model,
                editorial_enrichment_service=publication_persistence,
                media_asset_store=media_assets,
                diagram_compiler=production.diagram_compiler,
                chart_compiler=AnalyticChartRenderer(),
                resource_search_enabled=False,
            ),
            persistence_service=publication_persistence,
            publication_render_service=preview_service,  # type: ignore[arg-type]
        )
        request = TargetedEditorialRevisionRequest(
            subject_id=selected["A"],
            base_artifact_id=enrichment_v1.id,
            base_version=enrichment_v1.version,
            base_input_hash=enrichment_v1.input_hash,
            base_canonical_sha256=enrichment_sha_v1,
            before_hash=enrichment_sha_v1,
            target=TargetedEditorialRevisionTarget(
                kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
                element_kind=EditorialEnrichmentElementKind.DIAGRAM,
                element_key=diagram_key,
            ),
            operation=TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            instruction="Retirer le diagramme concerné par la correction tardive.",
            mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
            requester="workflow-analyst",
        )

        async def preview_correction() -> Any:
            preview = await revision_service.preview_targeted(request)
            invariants.require(
                preview.target_path == f"editorial_enrichment.diagram[{diagram_key}]"
                and preview.expected_model_calls == 0
                and preview.stages_invalidated == (ProductionArtifactStage.PUBLICATION.value,),
                driver.scenario,
                "correction_preview",
                "the official preview does not identify this zero-model targeted correction",
            )
            return preview

        await driver._perform(
            "correction_preview",
            preview_correction,
            via="service",
            action_name="ProductionEditorialEnrichmentRevisionService.preview_targeted",
            identities={"subject": selected["A"], "base_artifact": enrichment_v1.id},
            assertions=("official-targeted-correction-preview", "zero-model-calls-required"),
        )

        model_run_count_before = (await invariants.work_counts(uow_factory))[1]

        async def apply_correction() -> Any:
            return await revision_service.revise_targeted(request)

        async def verify_correction(result: Any) -> tuple[str, ...]:
            invariants.require(
                result.artifact is not None and result.publication_artifact is not None,
                driver.scenario,
                "correction_apply",
                "official correction did not return new enrichment and PUBLICATION artifacts",
            )
            assert result.artifact is not None and result.publication_artifact is not None
            invariants.require(
                result.artifact.canonical_blob_id is not None
                and result.publication_artifact.canonical_blob_id is not None,
                driver.scenario,
                "correction_apply",
                "corrected enrichment or PUBLICATION artifact has no canonical blob",
            )
            assert (
                result.artifact.canonical_blob_id is not None
                and result.publication_artifact.canonical_blob_id is not None
            )
            async with uow_factory() as uow:
                persisted_old = await uow.production_artifacts.get(publication_v1.id)
                persisted_new = await uow.production_artifacts.get(result.publication_artifact.id)
                current_publication = await uow.production_artifacts.get_current(
                    run_a, ProductionArtifactStage.PUBLICATION.value
                )
                history = [
                    item
                    for item in await uow.production_artifacts.list_for_run(run_a)
                    if item.stage is ProductionArtifactStage.PUBLICATION
                ]
                persisted_run = await uow.production_runs.get(run_a)
                composition_after = await uow.edition_compositions.get_for_edition(edition_id)
            new_bytes = await enrichment_store.read_bytes(
                result.publication_artifact.canonical_blob_id
            )
            new_enrichment_bytes = await enrichment_store.read_bytes(
                result.artifact.canonical_blob_id
            )
            old_bytes_after = await enrichment_store.read_bytes(publication_v1.canonical_blob_id)
            invariants.require(
                persisted_old is not None
                and persisted_old.id == publication_v1.id
                and persisted_old.version == publication_v1.version
                and persisted_old.input_hash == publication_v1.input_hash
                and persisted_old.canonical_blob_id == publication_v1.canonical_blob_id
                and hashlib.sha256(old_bytes_after).hexdigest()
                == hashlib.sha256(publication_bytes_v1).hexdigest()
                and persisted_new == result.publication_artifact
                and current_publication == result.publication_artifact
                and len(history) == 2,
                driver.scenario,
                "correction_apply",
                "the immutable v1 PUBLICATION identity and bytes must remain in the two-version "
                "history beside the new current version",
            )
            invariants.require(
                result.publication_artifact.id != publication_v1.id
                and result.publication_artifact.version > publication_v1.version
                and result.publication_artifact.input_hash != publication_v1.input_hash
                and hashlib.sha256(new_bytes).hexdigest()
                != hashlib.sha256(publication_bytes_v1).hexdigest()
                and result.artifact.version > enrichment_v1.version
                and hashlib.sha256(new_enrichment_bytes).hexdigest() != enrichment_sha_v1,
                driver.scenario,
                "correction_apply",
                "correction must create distinct publication/enrichment versions and hashes",
            )
            invariants.require(
                persisted_run is not None
                and persisted_run.id == run_a
                and persisted_run.status is ProductionRunStatus.READY
                and persisted_run.pipeline_generation == run_a_entity.pipeline_generation,
                driver.scenario,
                "correction_apply",
                "targeted correction changed A's production run or generation",
            )
            invariants.require(
                composition_after == composition_with_decision,
                driver.scenario,
                "correction_apply",
                "targeted correction implicitly changed the v1 composition pins",
            )
            invariants.require(
                await _publication_identity(
                    uow_factory,
                    driver.production_scenarios["B"].artifact_store,
                    selected["B"],
                    scenario=driver.scenario,
                    step="correction_apply.B_unchanged",
                )
                == identity_b_v1,
                driver.scenario,
                "correction_apply",
                "correcting A changed B's run, artifact identity, or canonical document hash",
            )
            invariants.require(
                (await invariants.work_counts(uow_factory))[1] == model_run_count_before,
                driver.scenario,
                "correction_apply",
                "deleting an existing diagram unexpectedly created a model run",
            )
            invariants.require(
                preview_service.publication_artifact_ids == [result.publication_artifact.id],
                driver.scenario,
                "correction_apply",
                "the correction service did not request a preview for its new artifact",
            )
            return (
                "new-publication-version-and-hash-read-from-postgresql",
                "old-publication-history-preserved",
                "run-and-generation-preserved",
                "pinned-composition-unchanged",
                "B-artifact-and-document-hash-unchanged",
                "no-model-run-created-by-deterministic-delete",
            )

        corrected = await driver._perform(
            "correction_apply",
            apply_correction,
            via="service",
            action_name="ProductionEditorialEnrichmentRevisionService.revise_targeted",
            identities={"subject": selected["A"], "base_artifact": enrichment_v1.id},
            assertions=("official-correction-channel", "publication-artifact-versioned"),
            after=verify_correction,
        )
        new_publication = corrected.publication_artifact
        assert new_publication is not None

        await driver.review(
            expected_superseded_artifacts={
                selected["A"]: (
                    publication_v1.id,
                    publication_v1.version,
                    run_a,
                    new_publication.id,
                    new_publication.version,
                )
            },
        )

        async def verify_v1_exclusion_is_not_rebound() -> tuple[str, ...]:
            async with uow_factory() as uow:
                decisions = await uow.publication_review_decisions.list_for_edition(edition_id)
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            decision = next((item for item in decisions if item.id == decision_id), None)
            review = await EditionReviewService(uow_factory).get(edition_id)
            entry = (
                next(
                    (item for item in composition.entries if item.subject_id == selected["A"]),
                    None,
                )
                if composition is not None
                else None
            )
            invariants.require(
                decision is not None
                and decision.production_run_id == run_a
                and decision.document_artifact_id == publication_v1.id
                and decision.document_artifact_version == publication_v1.version
                and new_publication.id != decision.document_artifact_id
                and entry is not None
                and entry.disposition is CompositionDisposition.INCLUDED
                and entry.artifact_id == publication_v1.id
                and not any(
                    exclusion.subject_id == selected["A"] and exclusion.applicable
                    for exclusion in review.exclusions
                ),
                driver.scenario,
                "review_v1_exclusion_not_applicable_to_v2",
                "the v1 exclusion remains bound to v1 and is absent from applicable exclusions "
                "for corrected v2",
            )
            return (
                "v1-exclusion-identity-remains-historical",
                "no-v1-exclusion-applies-to-corrected-v2",
                "included-composition-still-pins-v1-until-explicit-recomposition",
            )

        await driver._perform(
            "review_v1_exclusion_not_applicable_to_v2",
            verify_v1_exclusion_is_not_rebound,
            via="service",
            action_name="SQL and EditionReviewService read-back of v1 decision against v2",
            identities={"edition": edition_id, "subject": selected["A"], "decision": decision_id},
            assertions=("decision-v1-not-rebound-to-v2", "stale-composition-pin-remains-v1"),
        )

        manifests_before_stale_accept = await invariants.manifest_hashes(uow_factory, edition_id)

        async def reject_stale_accept() -> tuple[int, str]:
            response = await driver.client.post(
                f"/api/editions/{edition_id}/publication/accept",
                json={"expected_composition_version": composition_with_decision.version},
            )
            detail = response.json().get("detail", {})
            manifests_after = await invariants.manifest_hashes(uow_factory, edition_id)
            invariants.require(
                response.status_code == 409 and isinstance(detail.get("code"), str),
                driver.scenario,
                "accept_stale_pin",
                f"expected an HTTP 409 while A is pinned to v1, got "
                f"{response.status_code} {detail!r}",
            )
            invariants.require(
                not manifests_before_stale_accept
                and manifests_after == manifests_before_stale_accept,
                driver.scenario,
                "accept_stale_pin",
                "blocked accept changed the persisted manifest set",
            )
            return response.status_code, str(detail["code"])

        await driver._perform(
            "accept_stale_pin",
            reject_stale_accept,
            via="http",
            action_name="POST /publication/accept with A still pinned to v1",
            identities={"edition": edition_id, "subject_a": selected["A"]},
            assertions=("http-409-while-v1-is-pinned", "no-manifest-or-release-created"),
        )

        await driver.compose((selected["A"], selected["B"]))
        async with uow_factory() as uow:
            composition_v2 = await uow.edition_compositions.get_for_edition(edition_id)
        invariants.require(
            composition_v2 is not None
            and composition_v2.version == composition_with_decision.version + 1,
            driver.scenario,
            "composition_v2",
            "explicit recomposition must advance the composition version exactly once",
        )
        assert composition_v2 is not None
        entries_v2 = {entry.subject_id: entry for entry in composition_v2.entries}
        invariants.require(
            entries_v2[selected["A"]].disposition is CompositionDisposition.INCLUDED
            and entries_v2[selected["A"]].artifact_id == new_publication.id
            and entries_v2[selected["A"]].artifact_version == new_publication.version
            and entries_v2[selected["B"]].artifact_id == identity_b_v1[3]
            and entries_v2[selected["B"]].artifact_version == identity_b_v1[4],
            driver.scenario,
            "composition_v2",
            "the explicit composition must pin A v2 and leave B at its original artifact",
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted = await driver.accept()

        async def verify_manifest_v2() -> tuple[str, ...]:
            async with uow_factory() as uow:
                manifest = await uow.publication_manifests.get(accepted.manifest.id)
                entries = await uow.publication_manifest_entries.list_for_manifest(
                    accepted.manifest.id
                )
                exclusions = await uow.publication_manifest_exclusions.list_for_manifest(
                    accepted.manifest.id
                )
            by_subject = {entry.subject_id: entry for entry in entries}
            invariants.require(
                manifest is not None
                and manifest.composition_version == composition_v2.version
                and len(by_subject) == 2
                and not exclusions,
                driver.scenario,
                "accept_v2_manifest",
                "accepted manifest must freeze the v2 composition with A and B included",
            )
            invariants.require(
                by_subject[selected["A"]].document_artifact_id == new_publication.id
                and by_subject[selected["A"]].document_artifact_version == new_publication.version
                and by_subject[selected["A"]].document_input_hash == new_publication.input_hash
                and by_subject[selected["B"]].document_artifact_id == identity_b_v1[3]
                and by_subject[selected["B"]].document_artifact_version == identity_b_v1[4],
                driver.scenario,
                "accept_v2_manifest",
                "manifest must identify A's corrected PUBLICATION and unchanged B",
            )
            return (
                "manifest-v2-composition-version-read-from-postgresql",
                "manifest-pins-corrected-a-and-original-b",
                "manifest-has-no-stale-exclusion",
            )

        await driver._perform(
            "accept_v2_manifest",
            verify_manifest_v2,
            via="service",
            action_name="SQL verify accepted v2 manifest pins",
            identities={"edition": edition_id, "manifest": accepted.manifest.id},
            assertions=("manifest-points-at-a-v2", "b-unchanged"),
        )
