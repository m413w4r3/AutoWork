"""FULL proof: an approved opinion enters a frozen multibatch edition only after repin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_document import edition_metadata_projection
from cti_app.application.edition_rendering import (
    EDITION_RENDERER_MANIFEST,
    EditionRenderService,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    PublicationAugmentationArtifactService,
)
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_execution import load_typst_render_bundle_snapshots
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    PublicationManifestV2,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSourceV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.database.repositories.pdf_inspection import (
    SqlAlchemyPdfInspectionSourceRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs

from . import invariants
from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


def _render_roots_exist(template_root: Path, font_root: Path, fonts_lock: Path) -> bool:
    return template_root.is_dir() and font_root.is_dir() and fonts_lock.is_file()


def _article_v5(subject_id: UUID, label: str) -> PublicationDocumentV5:
    title = f"Article {label}"
    lead = f"Corps article {label} W11. " + (
        "This deterministic local fixture describes one independently published article. " * 24
    )
    source_document_id = uuid4()
    evidence = PublicationEvidenceRefV1(
        source_document_id=source_document_id,
        kind=PublicationEvidenceKind.FACT,
        evidence_key=hashlib.sha256(uuid4().bytes).hexdigest(),
    )
    base = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language="fr",
        title=title,
        lead=(PublicationParagraphV1(text=lead, evidence_refs=(evidence,)),),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(
            PublicationSourceV1(
                source_document_id=source_document_id,
                canonical_url=f"https://example.test/w11/{uuid4().hex}",
                title="Local workflow fixture",
                publisher="AutoWork test harness",
                published_at=None,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
            ),
        ),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )
    return PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=(
                SemanticParagraphV1(
                    anchor="title",
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, title),),
                ),
                SemanticParagraphV1(
                    anchor="lead:0001",
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, lead),),
                ),
            ),
        ),
    )


def _extract_pdf_text(pdf_bytes: bytes, temp_root: Path) -> str:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise AssertionError("W11-analyst-opinion-repin / Poppler pdftotext is required")
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
async def test_w11_analyst_opinion_requires_explicit_repin_for_multibatch_edition(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W11-analyst-opinion-repin")
    async with driver:
        typst_binary = os.environ.get("TYPST_BINARY", "typst")
        binary_path = shutil.which(typst_binary)
        default_template_root, default_font_root, fonts_lock = typst_bundle_paths()
        template_root = Path(os.environ.get("CHP_TYPST_ROOT", str(default_template_root)))
        font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(default_font_root)))

        async def check_render_prerequisites() -> tuple[str, ...]:
            invariants.require(
                binary_path is not None,
                driver.scenario,
                "typst_prerequisites",
                "real Typst 0.15.1 is required; set TYPST_BINARY or put it on PATH",
            )
            paths_exist = await asyncio.to_thread(
                _render_roots_exist,
                template_root,
                font_root,
                fonts_lock,
            )
            invariants.require(
                paths_exist,
                driver.scenario,
                "typst_prerequisites",
                "real edition templates, fonts and typst-fonts.lock are required",
            )
            template, fonts = load_typst_render_bundle_snapshots(
                chp_typst_root=template_root,
                font_bundle_root=font_root,
                typst_fonts_lock_path=fonts_lock,
                manifest_name=EDITION_RENDERER_MANIFEST,
            )
            processor = PopplerSubprocess(temp_root=tmp_path / "poppler")
            return (
                f"typst-binary:{binary_path}",
                f"template-sha256:{template.sha256}",
                f"font-files:{len(fonts.files)}",
                f"poppler-build:{processor.build}",
            )

        await driver._perform(
            "typst_prerequisites",
            check_render_prerequisites,
            via="service",
            action_name="check real Typst, template, fonts and Poppler prerequisites",
            identities={"binary": typst_binary},
            assertions=("missing-render-prerequisites-fail-explicitly",),
        )

        async def seed_independent_v5_publications() -> tuple[
            object,
            ProductionArtifactStore,
            dict[str, Subject],
            dict[str, UUID],
            dict[str, object],
            dict[str, ProductionArtifact],
            dict[str, PublicationDocumentV5],
        ]:
            edition, artifact_store, subjects, batches = await _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            )
            runs: dict[str, object] = {}
            publications: dict[str, ProductionArtifact] = {}
            documents: dict[str, PublicationDocumentV5] = {}
            for label in ("A", "B", "C"):
                async with uow_factory() as uow:
                    subject_runs = await uow.production_runs.list_for_subject(subjects[label].id)
                if len(subject_runs) != 1:
                    raise AssertionError(f"W11 expected one distinct run for article {label}")
                run = subject_runs[0]
                runs[label] = run
                document = _article_v5(subjects[label].id, label)
                documents[label] = document
                blob_id, _document_sha256 = await artifact_store.put_canonical_json(
                    serialize_publication_document(document),
                    bucket="w11-v5-publications",
                )
                publication = ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subjects[label].id,
                    stage=ProductionArtifactStage.PUBLICATION,
                    version=2,
                    input_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=blob_id,
                )
                async with uow_factory() as uow:
                    await uow.production_artifacts.append(publication)
                    await uow.commit()
                publications[label] = publication
            return edition, artifact_store, subjects, batches, runs, publications, documents

        (
            edition,
            artifact_store,
            subjects,
            batches,
            runs,
            base_artifacts,
            base_documents,
        ) = await driver._perform(
            "seed_independent_v5_publications",
            seed_independent_v5_publications,
            via="fixture",
            action_name="seed A/B/C V5 PUBLICATION artifacts in distinct production runs",
            assertions=("three-independent-ready-runs", "verified-v5-publication-blobs"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)
        run_ids = {label: runs[label].id for label in ("A", "B", "C")}
        invariants.require(
            len(set(run_ids.values())) == 3
            and len({batches[label] for label in ("A", "B", "C")}) == 3,
            driver.scenario,
            "seed_independent_v5_publications",
            "A, B and C must use separate runs and production batches",
        )
        await driver.record_seed(
            {label: subjects[label].id for label in ("A", "B", "C")},
            mechanism="_compose_three_runs plus verified V5 publication artifacts",
        )
        await driver.compose(
            tuple(subjects[label].id for label in ("A", "B", "C")),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )

        excluded_subject = Subject(
            edition_id=edition.id,
            title=f"Excluded W11 subject {uuid4().hex}",
            slug=f"w11-excluded-{uuid4().hex}",
            tlp=TLP.GREEN,
        )
        async with uow_factory() as uow:
            await uow.subjects.add(excluded_subject)
            await uow.commit()
        exclusion_decision_id = await driver.exclude_subject(
            excluded_subject.id,
            reason="Exclusion retained across an explicit analyst-opinion repin.",
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted_v1 = await driver.accept()
        release_v1 = await driver.assemble()

        engine = create_postgres_engine(migrated_postgres_url)
        source_repository = SqlAlchemyPdfInspectionSourceRepository(create_session_factory(engine))
        pdf_inspector = PdfInspectionService(
            source_repository,
            artifact_store,
            PopplerSubprocess(temp_root=tmp_path / "pdf-poppler"),
            PdfInspectionCache(tmp_path / "pdf-inspection-cache"),
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

        async def render_and_inspect(release: object, suffix: str) -> tuple[object, bytes, str]:
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
            pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            invariants.require(
                pdf_sha256 == rendered.output_sha256 and len(pdf_bytes) > 0,
                driver.scenario,
                f"render_{suffix}",
                "persisted PDF bytes do not match the render SHA-256",
            )
            inspection = await pdf_inspector.metadata(pdf_sha256)
            diagnostics = inspection["diagnostics"]
            diagnostic_codes = {item["code"] for item in diagnostics["items"]}
            invariants.require(
                inspection["pdf"]["page_count"] > 0
                and diagnostics["status"] == "ready"
                and "pdf_integrity_verified" in diagnostic_codes,
                driver.scenario,
                f"render_{suffix}",
                f"Poppler inspection failed: {diagnostics!r}",
            )
            text = await asyncio.to_thread(
                _extract_pdf_text,
                pdf_bytes,
                tmp_path / f"pdftotext-{suffix}",
            )
            return rendered, pdf_bytes, text

        try:
            rendered_v1, pdf_v1_bytes, pdf_v1_text = await driver._perform(
                "render_v1_real_typst_and_poppler",
                lambda: render_and_inspect(release_v1, "v1"),
                via="service",
                action_name="EditionRenderService + real Typst + Poppler for the V5-only release",
                identities={"release": release_v1.id, "manifest": accepted_v1.manifest.id},
                assertions=("real-typst-pdf-v1", "poppler-inspected-pdf-v1"),
            )
            invariants.require(
                "avis de l'analyste" not in pdf_v1_text.casefold()
                and all(f"Corps article {label} W11" in pdf_v1_text for label in ("A", "B", "C")),
                driver.scenario,
                "render_v1_real_typst_and_poppler",
                "V5-only PDF must retain all three articles and contain no analyst opinion",
            )

            async def read_release_snapshot(
                release_id: UUID, manifest_id: UUID, render_id: UUID
            ) -> tuple[object, bytes, bytes, bytes, str]:
                async with uow_factory() as uow:
                    release = await uow.edition_releases.get(release_id)
                    manifest = await uow.publication_manifests.get(manifest_id)
                    manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                    render = await uow.edition_renders.get(render_id)
                    manifest_blob = (
                        await uow.blobs.get(manifest_blob_id) if manifest_blob_id else None
                    )
                if (
                    release is None
                    or manifest is None
                    or manifest_blob_id is None
                    or render is None
                ):
                    raise AssertionError("W11 historical release snapshot is incomplete")
                manifest_bytes = await artifact_store.read_bytes(manifest_blob_id)
                document_bytes = await artifact_store.read_bytes(release.edition_document_blob_id)
                if hashlib.sha256(document_bytes).hexdigest() != release.edition_document_sha256:
                    raise AssertionError("W11 edition document hash does not match its release")
                if manifest_blob is None or hashlib.sha256(manifest_bytes).hexdigest() != (
                    manifest_blob.descriptor.sha256
                ):
                    raise AssertionError("W11 manifest blob hash does not match PostgreSQL")
                parsed_manifest = PublicationManifestV2.from_json(json.loads(manifest_bytes))
                if parsed_manifest.id != manifest_id or parsed_manifest.content_sha256 != (
                    manifest.content_sha256
                ):
                    raise AssertionError("W11 manifest content or identity is not canonical")
                if render.output_blob_id is None:
                    raise AssertionError("W11 release render has no PDF blob")
                pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
                if hashlib.sha256(pdf_bytes).hexdigest() != render.output_sha256:
                    raise AssertionError("W11 PDF bytes do not match persisted render SHA-256")
                pdf_text = await asyncio.to_thread(
                    _extract_pdf_text,
                    pdf_bytes,
                    tmp_path / f"pdftotext-snapshot-{release_id}",
                )
                return release, document_bytes, manifest_bytes, pdf_bytes, pdf_text

            v1_snapshot = await read_release_snapshot(
                release_v1.id, accepted_v1.manifest.id, rendered_v1.id
            )
            (
                release_v1_row,
                document_v1_bytes,
                manifest_v1_bytes,
                pdf_v1_snapshot,
                v1_text_snapshot,
            ) = v1_snapshot
            invariants.require(
                pdf_v1_bytes == pdf_v1_snapshot,
                driver.scenario,
                "render_v1_real_typst_and_poppler",
                "captured V1 PDF differs from the persisted historical PDF",
            )
            composition_service = EditionCompositionService(uow_factory)
            async with uow_factory() as uow:
                composition_v1 = await uow.edition_compositions.get_for_edition(edition.id)
            if composition_v1 is None:
                raise AssertionError("W11 initial composition is missing")
            legacy_projection = EditionDocumentV2(
                edition=edition_metadata_projection(edition),
                publications=tuple(
                    EditionPublicationV2(
                        position=index,
                        subject_id=subjects[label].id,
                        document=base_documents[label],
                    )
                    for index, label in enumerate(("A", "B", "C"), start=1)
                ),
            )
            expected_v5_document_bytes = ProductionArtifactStore.canonical_json_bytes(
                legacy_projection.to_json()
            )
            invariants.require(
                document_v1_bytes == expected_v5_document_bytes,
                driver.scenario,
                "v5_document_baseline",
                "the composed V5 document differs from the pre-H1b base-V5 projection",
            )

            async def approve_opinion_and_check_no_repin() -> tuple[
                ProductionArtifact,
                AnalystOpinionV1,
                str,
            ]:
                base_artifact = base_artifacts["A"]
                run = runs["A"]
                base_document = base_documents["A"]
                base_sha256 = publication_document_v5_sha256(base_document)
                synthesis_payload = {
                    "schema_version": "w11-synthetic-synthesis.v1",
                    "nonce": uuid4().hex,
                }
                synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
                    synthesis_payload,
                    bucket="w11-synthesis",
                )
                synthesis = ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subjects["A"].id,
                    stage=ProductionArtifactStage.SYNTHESIS,
                    version=1,
                    input_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=synthesis_blob_id,
                )
                investigation = AnalystInvestigation.from_verified_synthesis(
                    synthesis=synthesis,
                    budget=LoopBudget(),
                    id=uuid4(),
                )
                async with uow_factory() as uow:
                    await uow.production_artifacts.append(synthesis)
                    await uow.analyst_investigations.add(investigation)
                    await uow.commit()

                observation_id = uuid4()
                finding_id = uuid4()
                observation = EvidenceObservation(
                    investigation_id=investigation.id,
                    kind=EvidenceKind.CLAIM_SOURCE,
                    blob_id=uuid4(),
                    sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
                    source="workflow-fixture",
                    source_version="w11-v1",
                    observed_at=datetime.now(UTC),
                    object_type="claim",
                    object_id=f"w11-{uuid4().hex}",
                    id=observation_id,
                )
                finding = InvestigationFinding(
                    investigation_id=investigation.id,
                    kind=FindingKind.CLAIM_VERIFICATION,
                    assessment=FindingAssessment.SUPPORTED,
                    statement="The local W11 fixture supports the added analyst finding.",
                    observation_refs=(observation_id,),
                    id=finding_id,
                )
                opinion = AnalystOpinionV1(
                    id=uuid4(),
                    version=1,
                    investigation_id=investigation.id,
                    synthesis_artifact_id=synthesis.id,
                    synthesis_sha256=synthesis_sha256,
                    base_publication_artifact_id=base_artifact.id,
                    base_publication_sha256=base_sha256,
                    blocks=(
                        OpinionBlock(
                            block_type=OpinionBlockType.KEY_FINDINGS,
                            text=f"Constat W11 propre à A {uuid4().hex}.",
                            finding_refs=(finding_id,),
                            evidence_refs=(observation_id,),
                            qualification=OpinionClaimQualification.FACT,
                            certainty=0.95,
                        ),
                    ),
                    author_id="w11-author",
                    reviewer_id="w11-reviewer",
                    status=AnalystOpinionStatus.APPROVED,
                )
                citation_context = OpinionCitationContext(
                    base_publication_artifact_id=base_artifact.id,
                    base_publication_sha256=base_sha256,
                    investigation_id=investigation.id,
                    approver_id="w11-approver",
                    findings=(finding,),
                    observations=(observation,),
                    tlp_violation=False,
                    do_not_submit=False,
                )
                augmented_artifact = await PublicationAugmentationArtifactService(
                    uow_factory=uow_factory,
                    artifact_store=artifact_store,
                ).publish(
                    base_publication_artifact_id=base_artifact.id,
                    opinion=opinion,
                    citation_context=citation_context,
                )
                async with uow_factory() as uow:
                    current_composition = await uow.edition_compositions.get_for_edition(edition.id)
                    persisted_manifest = await uow.publication_manifests.get(
                        accepted_v1.manifest.id
                    )
                    persisted_release = await uow.edition_releases.get(release_v1.id)
                composition_unchanged = current_composition == composition_v1
                manifest_unchanged = persisted_manifest == accepted_v1.manifest
                release_unchanged = persisted_release == release_v1_row
                if not (composition_unchanged and manifest_unchanged and release_unchanged):
                    raise AssertionError(
                        "W11 approval changed frozen state: "
                        f"composition={composition_unchanged}, "
                        f"manifest={manifest_unchanged}, release={release_unchanged}"
                    )
                manifest_after = await artifact_store.read_bytes(
                    await _manifest_blob_id(uow_factory, accepted_v1.manifest.id)
                )
                document_after = await artifact_store.read_bytes(
                    persisted_release.edition_document_blob_id
                )
                pdf_after = await artifact_store.read_bytes(rendered_v1.output_blob_id)
                text_after = await asyncio.to_thread(
                    _extract_pdf_text,
                    pdf_after,
                    tmp_path / "pdftotext-v1-after-opinion",
                )
                invariants.require(
                    manifest_after == manifest_v1_bytes
                    and document_after == document_v1_bytes
                    and pdf_after == pdf_v1_snapshot
                    and hashlib.sha256(pdf_after).hexdigest() == rendered_v1.output_sha256
                    and text_after == v1_text_snapshot,
                    driver.scenario,
                    "opinion_without_repin",
                    "approved opinion changed the old composition, manifest, V5 document or PDF",
                )
                return augmented_artifact, opinion, base_sha256

            async def _manifest_blob_id(factory: UnitOfWorkFactory, manifest_id: UUID) -> UUID:
                async with factory() as uow:
                    blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                if blob_id is None:
                    raise AssertionError("W11 manifest blob is missing")
                return blob_id

            augmented_artifact, opinion, base_sha256 = await driver._perform(
                "approved_opinion_without_repin",
                approve_opinion_and_check_no_repin,
                via="service",
                action_name="PublicationAugmentationArtifactService.publish for pinned article A",
                identities={"base_publication": base_artifacts["A"].id},
                assertions=(
                    "approved-opinion-creates-new-publication-artifact",
                    "composition-manifest-v1-document-and-pdf-unchanged",
                ),
            )
            invariants.require(
                augmented_artifact.metadata.get("previous_publication_artifact_id")
                == str(base_artifacts["A"].id)
                and augmented_artifact.id != base_artifacts["A"].id,
                driver.scenario,
                "approved_opinion_without_repin",
                "V6 must be a new PUBLICATION linked to the exact V5 base",
            )

            async def explicit_human_repin() -> object:
                composition_read = await composition_service.get(edition.id)
                current = composition_read.composition
                if current is None:
                    raise AssertionError("W11 composition disappeared before repin")
                async with uow_factory() as uow:
                    blob = await uow.blobs.get(augmented_artifact.canonical_blob_id)
                if blob is None:
                    raise AssertionError("W11 V6 publication blob is missing")
                entries = tuple(
                    replace(
                        entry,
                        artifact_id=augmented_artifact.id,
                        artifact_version=augmented_artifact.version,
                        artifact_input_hash=augmented_artifact.input_hash,
                        document_sha256=blob.descriptor.sha256,
                    )
                    if entry.subject_id == subjects["A"].id
                    else entry
                    for entry in current.entries
                )
                return await composition_service.replace_entries(
                    edition.id,
                    entries,
                    expected_version=current.version,
                    idempotency_key=f"w11-human-repin-{uuid4()}",
                    actor_id="w11-human-editor",
                    correlation_id=f"w11-repin-{uuid4()}",
                )

            composition_v2 = await driver._perform(
                "explicit_human_repin",
                explicit_human_repin,
                via="service",
                action_name="EditionCompositionService.replace_entries with expected version CAS",
                identities={"edition": edition.id, "publication": augmented_artifact.id},
                assertions=("human-repin-uses-versioned-composition-mutation",),
            )
            invariants.require(
                composition_v2.version == composition_v1.version + 1,
                driver.scenario,
                "explicit_human_repin",
                "explicit repin must advance the composition exactly once",
            )
            await driver.review(expect_can_accept=True)
            await driver.validate()
            accepted_v2 = await driver.accept()
            release_v2 = await driver.assemble()
            rendered_v2, pdf_v2_bytes, pdf_v2_text = await driver._perform(
                "render_v2_real_typst_and_poppler",
                lambda: render_and_inspect(release_v2, "v2"),
                via="service",
                action_name="EditionRenderService + real Typst + Poppler after explicit A repin",
                identities={"release": release_v2.id, "manifest": accepted_v2.manifest.id},
                assertions=("real-typst-pdf-v2", "poppler-inspected-pdf-v2"),
            )

            async def inspect_v2_document_and_history() -> tuple[str, ...]:
                snapshot_v2 = await read_release_snapshot(
                    release_v2.id, accepted_v2.manifest.id, rendered_v2.id
                )
                _release_v2_row, document_v2_bytes, _manifest_v2_bytes, pdf_v2_snapshot, text_v2 = (
                    snapshot_v2
                )
                if pdf_v2_snapshot != pdf_v2_bytes or text_v2 != pdf_v2_text:
                    raise AssertionError("W11 v2 render and inspected PDF differ")
                v1_manifest = PublicationManifestV2.from_json(json.loads(manifest_v1_bytes))
                v2_manifest = accepted_v2.manifest
                v1_by_subject = {item.subject_id: item for item in v1_manifest.entries}
                v2_by_subject = {item.subject_id: item for item in v2_manifest.entries}
                invariants.require(
                    v2_by_subject[subjects["A"].id].document_artifact_id == augmented_artifact.id
                    and v2_by_subject[subjects["A"].id].document_artifact_version
                    == augmented_artifact.version
                    and all(
                        v1_by_subject[subjects[label].id] == v2_by_subject[subjects[label].id]
                        for label in ("B", "C")
                    )
                    and v1_manifest.exclusions == v2_manifest.exclusions
                    and any(
                        item.subject_id == excluded_subject.id
                        and item.review_decision_id == exclusion_decision_id
                        for item in v2_manifest.exclusions
                    ),
                    driver.scenario,
                    "v2_manifest_pins_and_exclusions",
                    "repin must change A only and preserve the original exclusion decision",
                )
                parsed_v1 = EditionDocumentV2.from_json(json.loads(document_v1_bytes))
                parsed_v2 = EditionDocumentV2.from_json(json.loads(document_v2_bytes))
                v1_publications = {
                    item.subject_id: item.document for item in parsed_v1.publications
                }
                v2_publications = {
                    item.subject_id: item.document for item in parsed_v2.publications
                }
                opinion_subjects = {
                    subject_id
                    for subject_id, publication in v2_publications.items()
                    if isinstance(publication, PublicationDocumentV6)
                    and publication.rendered_opinion is not None
                }
                opinion_a = v2_publications[subjects["A"].id]
                invariants.require(
                    opinion_subjects == {subjects["A"].id}
                    and isinstance(opinion_a, PublicationDocumentV6)
                    and opinion_a.base_publication == base_documents["A"]
                    and opinion_a.provenance.base_publication_artifact_id == base_artifacts["A"].id
                    and opinion_a.provenance.base_publication_sha256 == base_sha256
                    and opinion_a.provenance.opinion_id == opinion.id
                    and opinion_a.provenance.opinion_version == opinion.version
                    and opinion_a.provenance.opinion_content_sha256 == opinion.content_sha256
                    and all(
                        v1_publications[subjects[label].id] == v2_publications[subjects[label].id]
                        for label in ("B", "C")
                    ),
                    driver.scenario,
                    "v2_edition_document_opinion_provenance",
                    "frozen V2 document must carry A's V6 provenance and unchanged B/C V5 articles",
                )
                normalized_v2_text = pdf_v2_text.casefold().replace(chr(0x2019), "'")
                heading = "avis de l'analyste"
                invariants.require(
                    normalized_v2_text.count(heading) == 1
                    and "Constat W11 propre à A".casefold() in normalized_v2_text
                    and pdf_v1_text == v1_text_snapshot,
                    driver.scenario,
                    "v2_pdf_opinion_text",
                    "only the explicitly repinned A opinion should appear in the new PDF; "
                    f"heading_count={normalized_v2_text.count(heading)}, "
                    f"claim_present={'constat w11 propre à a' in normalized_v2_text}, "
                    f"text_tail={pdf_v2_text[-700:]!r}",
                )
                body_a = pdf_v2_text.index("Corps article A W11")
                opinion_heading = normalized_v2_text.index(heading)
                body_b = pdf_v2_text.index("Corps article B W11")
                body_c = pdf_v2_text.index("Corps article C W11")
                invariants.require(
                    body_a < opinion_heading < body_b < body_c,
                    driver.scenario,
                    "v2_pdf_article_order",
                    "the opinion must render inside A before the unchanged B and C articles",
                )
                historical = await read_release_snapshot(
                    release_v1.id, accepted_v1.manifest.id, rendered_v1.id
                )
                (
                    historical_release,
                    historical_doc,
                    historical_manifest,
                    historical_pdf,
                    historical_text,
                ) = historical
                invariants.require(
                    historical_release == release_v1_row
                    and historical_doc == document_v1_bytes
                    and historical_manifest == manifest_v1_bytes
                    and historical_pdf == pdf_v1_snapshot
                    and historical_text == v1_text_snapshot,
                    driver.scenario,
                    "historical_v1_retained",
                    "the old manifest, release, V5 document and PDF must remain readable "
                    "and byte-stable",
                )
                return (
                    "v2-manifest-pins-v6-for-a-only",
                    "b-and-c-v5-documents-and-pins-unchanged",
                    "exclusion-decision-retained",
                    "v2-pdf-opinion-heading-and-text-present-on-a-only",
                    "historical-v1-manifest-release-document-pdf-hashes-verified",
                )

            await driver._perform(
                "inspect_v2_document_and_history",
                inspect_v2_document_and_history,
                via="service",
                action_name="read V2 edition document and re-verify historical V1 release",
                identities={
                    "manifest_v1": accepted_v1.manifest.id,
                    "manifest_v2": accepted_v2.manifest.id,
                    "release_v1": release_v1.id,
                    "release_v2": release_v2.id,
                },
            )
        finally:
            await engine.dispose()
