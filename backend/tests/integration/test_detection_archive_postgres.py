"""PostgreSQL proof for manifest-bound human YARA rule archival."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.detection_review_service import DetectionReviewService
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.edition_rule_archive import EditionRuleArchiveService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    augment_publication_with_opinion,
)
from cti_app.domain.classification import TLP
from cti_app.domain.edition_composition import CompositionDisposition, EditionCompositionEntry
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_document_v6 import _approved_opinion, _base_v5

pytestmark = pytest.mark.integration

YARA_SOURCE = 'rule HumanReview { strings: $a = "human-review-marker" condition: $a }'
YARA_SHA256 = hashlib.sha256(YARA_SOURCE.encode()).hexdigest()


async def _put_json(
    artifact_store: ProductionArtifactStore, payload: dict[str, object], *, bucket: str
) -> tuple[UUID, str]:
    return await artifact_store.put_canonical_json(payload, bucket=bucket)


def _test_report(version: DetectionCandidateVersion) -> dict[str, object]:
    return {
        "schema": "investigation-yara-test.v1",
        "operation": "revise",
        "candidate_id": str(version.candidate_id),
        "version_id": str(version.id),
        "proposal_source_sha256": version.source_sha256,
        "source_sha256": version.source_sha256,
        "execution_status": "succeeded",
        "engine_version": "yara test fixture",
        "candidate_status": "tested",
        "selection_bias": "synthetic fixture corpus",
        "coverage_statement": "one positive and one goodware case",
        "corpus_sha256": hashlib.sha256((version.source_sha256 + "corpus").encode()).hexdigest(),
        "corpus_sha256s_by_role": {
            "confirmed_positive": ["1" * 64],
            "out_of_cluster_candidate": [],
            "goodware": ["2" * 64],
            "holdout": [],
        },
        "roles": [
            {
                "role": "confirmed_positive",
                "corpus_sha256s": ["1" * 64],
                "matches": ["1" * 64],
                "non_matches": [],
                "non_measurable": [],
                "errors": [],
            },
            {
                "role": "goodware",
                "corpus_sha256s": ["2" * 64],
                "matches": [],
                "non_matches": ["2" * 64],
                "non_measurable": [],
                "errors": [],
            },
        ],
        "limitations": ["holdout unavailable in this synthetic fixture"],
    }


@pytest.mark.asyncio
async def test_v6_approved_rule_is_archived_for_a_new_manifest_only(
    uow_factory, tmp_path: Path
) -> None:
    edition = Edition(
        country="Detection archive integration",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
    )
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / f"detection-archive-{uuid4().hex}"), uow_factory
        )
    )
    subjects = {
        label: Subject(
            edition_id=edition.id,
            title=f"Article {label}",
            slug=f"detection-archive-{label.lower()}-{uuid4().hex}",
            tlp=TLP.GREEN,
        )
        for label in ("A", "B")
    }
    runs = {
        label: ProductionRun(
            subject_id=subjects[label].id,
            edition_id=edition.id,
            status=ProductionRunStatus.READY,
            current_stage=ProductionStage.ASSEMBLY,
            run_number=1,
            pipeline_generation=1,
        )
        for label in ("A", "B")
    }
    investigations: dict[str, AnalystInvestigation] = {}
    artifacts: dict[str, dict[str, ProductionArtifact]] = {"A": {}, "B": {}}
    publication_sha: dict[str, str] = {}

    base_template = _base_v5()
    for label in ("A", "B"):
        run = runs[label]
        subject = subjects[label]
        extraction_payload = {"items": [], "uncertainties": [], "rules": []}
        extraction_blob_id, _ = await _put_json(
            artifact_store, extraction_payload, bucket="detection-archive-extractions"
        )
        extraction = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=hashlib.sha256(f"extraction-{label}".encode()).hexdigest(),
            canonical_blob_id=extraction_blob_id,
        )
        base_document = replace(
            base_template,
            document=replace(base_template.document, subject_id=subject.id),
        )
        base_payload = serialize_publication_document(base_document)
        base_blob_id, base_sha = await _put_json(
            artifact_store, base_payload, bucket="detection-archive-publications"
        )
        base_artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=1,
            input_hash=base_sha,
            canonical_blob_id=base_blob_id,
            metadata={"input_artifacts": {"extraction_artifact_id": str(extraction.id)}},
        )
        synthesis_payload = {"schema_version": "test-synthesis.v1", "subject_id": str(subject.id)}
        synthesis_blob_id, synthesis_sha = await _put_json(
            artifact_store, synthesis_payload, bucket="detection-archive-synthesis"
        )
        synthesis = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=synthesis_sha,
            canonical_blob_id=synthesis_blob_id,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=synthesis.id,
            budget=LoopBudget(),
        )
        investigations[label] = investigation
        artifacts[label].update(
            extraction=extraction,
            base=base_artifact,
            synthesis=synthesis,
        )

        if label == "A":
            base_id = base_artifact.id
            opinion, context = _approved_opinion(base_document)
            opinion = replace(
                opinion,
                investigation_id=investigation.id,
                synthesis_artifact_id=synthesis.id,
                base_publication_artifact_id=base_id,
                content_sha256="",
            )
            context = replace(
                context,
                investigation_id=investigation.id,
                base_publication_artifact_id=base_id,
                findings=tuple(
                    replace(finding, investigation_id=investigation.id)
                    for finding in context.findings
                ),
                observations=tuple(
                    replace(observation, investigation_id=investigation.id)
                    for observation in context.observations
                ),
            )
            v6_document = augment_publication_with_opinion(base_document, opinion, context)
            v6_payload = serialize_publication_document(v6_document)
            v6_blob_id, v6_sha = await _put_json(
                artifact_store, v6_payload, bucket="detection-archive-publications"
            )
            v6_artifact = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject.id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=2,
                input_hash=v6_sha,
                canonical_blob_id=v6_blob_id,
                metadata={
                    "previous_publication_artifact_id": str(base_id),
                    "input_artifacts": {"extraction_artifact_id": str(extraction.id)},
                    "publication_augmentation": {
                        "investigation_id": str(investigation.id),
                        "opinion_id": str(opinion.id),
                        "opinion_version": opinion.version,
                        "opinion_content_sha256": opinion.content_sha256,
                        "approver_id": "opinion-approver",
                    },
                },
            )
            artifacts[label]["pinned_publication"] = v6_artifact
            publication_sha[label] = v6_sha
        else:
            artifacts[label]["pinned_publication"] = base_artifact
            publication_sha[label] = base_sha

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        for subject in subjects.values():
            await uow.subjects.add(subject)
        await uow.commit()
        for label in ("A", "B"):
            await uow.production_runs.add(runs[label])
            # B pins its unaugmented base artifact, which is also stored under "base".
            for artifact in {item.id: item for item in artifacts[label].values()}.values():
                await uow.production_artifacts.append(artifact)
            await uow.analyst_investigations.add(investigations[label])
        await uow.commit()

    versions: dict[str, DetectionCandidateVersion] = {}
    for label in ("A", "B"):
        version = DetectionCandidateVersion(
            investigation_id=investigations[label].id,
            candidate_id=uuid4(),
            source=YARA_SOURCE,
            source_sha256=YARA_SHA256,
            motif_provenance=(),
            status=DetectionCandidateStatus.TESTED,
        )
        versions[label] = version
        async with uow_factory() as uow:
            await uow.detection_candidates.append_version(version, expected_previous_id=None)
            await uow.commit()
            recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
            await recorder.record_json(
                investigation_id=investigations[label].id,
                kind=EvidenceKind.YARA_MATCH,
                payload=_test_report(version),
                source="AutoWork RuleWorkbench",
                source_version="investigation-rule-tools.v1",
                object_type="detection_candidate_version",
                object_id=str(version.id),
                limitations=("Synthetic corpus; holdout unavailable.",),
                idempotency_key=f"detection-archive-report-{edition.id}-{label}",
            )
            await uow.commit()

    entries = tuple(
        EditionCompositionEntry(
            subject_id=subjects[label].id,
            disposition=CompositionDisposition.INCLUDED,
            position=position,
            production_run_id=runs[label].id,
            pipeline_generation=runs[label].pipeline_generation,
            artifact_id=artifacts[label]["pinned_publication"].id,
            artifact_version=artifacts[label]["pinned_publication"].version,
            artifact_input_hash=artifacts[label]["pinned_publication"].input_hash,
            document_sha256=publication_sha[label],
        )
        for position, label in enumerate(("A", "B"), start=1)
    )
    composition_service = EditionCompositionService(uow_factory)
    composition = await composition_service.replace_entries(
        edition.id,
        entries,
        expected_version=0,
        idempotency_key=f"detection-archive-compose-{edition.id}-1",
        actor_id="editor",
        correlation_id=f"detection-archive-compose-{edition.id}-1",
    )
    publication_service = EditionPublicationService(uow_factory, artifact_store)
    accepted = await publication_service.accept(
        edition.id,
        actor_id="editor",
        expected_composition_version=composition.version,
        correlation_id=f"detection-archive-accept-{edition.id}-1",
    )
    assert accepted.manifest.schema_version == "2"
    # Materialize the historical release so its immutability can be asserted below.
    await EditionAssemblyService(uow_factory, artifact_store).assemble(accepted.manifest.id)
    archive_service = EditionRuleArchiveService(uow_factory, artifact_store)
    before_review = await archive_service.build(edition.id)
    assert not any(name.startswith("analyst/yara/") for name in _names(before_review.content))

    review_service = DetectionReviewService(uow_factory, artifact_store)
    await review_service.approve(
        versions["A"].id,
        YARA_SHA256,
        "human:reviewer-a",
        "Reviewed exact A source and corpus",
        "Synthetic corpus; holdout unavailable.",
        f"approve-a-{edition.id}",
    )
    await review_service.reject(
        versions["B"].id,
        YARA_SHA256,
        "human:reviewer-b",
        "Rejected the B rule after review",
        f"reject-b-{edition.id}",
    )

    async with uow_factory() as uow:
        old_manifest = await uow.publication_manifests.get(accepted.manifest.id)
        old_release = await uow.edition_releases.get_by_manifest(accepted.manifest.id)
    assert old_manifest is not None
    assert old_release is not None
    old_manifest_json = old_manifest.to_json()
    old_manifest_hash = old_manifest.content_sha256
    old_release_hash = old_release.edition_document_sha256
    after_review_old_manifest = await archive_service.build(edition.id)
    assert after_review_old_manifest.content == before_review.content

    repinned = await composition_service.replace_entries(
        edition.id,
        entries,
        expected_version=composition.version,
        idempotency_key=f"detection-archive-compose-{edition.id}-2",
        actor_id="editor",
        correlation_id=f"detection-archive-compose-{edition.id}-2",
    )
    next_publication = await publication_service.accept(
        edition.id,
        actor_id="editor",
        expected_composition_version=repinned.version,
        correlation_id=f"detection-archive-accept-{edition.id}-2",
    )
    assert next_publication.manifest.id != accepted.manifest.id
    after_repin = await archive_service.build(edition.id)
    with zipfile.ZipFile(BytesIO(after_repin.content)) as bundle:
        [analyst_path] = [name for name in bundle.namelist() if name.startswith("analyst/yara/")]
        assert bundle.read(analyst_path).decode() == YARA_SOURCE
        archive_manifest = json.loads(bundle.read("manifest.json"))
    assert len(archive_manifest["analyst_rules"]) == 1
    assert archive_manifest["analyst_rules"][0]["article"]["artifact_id"] == str(
        artifacts["A"]["pinned_publication"].id
    )

    async with uow_factory() as uow:
        reread_manifest = await uow.publication_manifests.get(accepted.manifest.id)
        reread_release = await uow.edition_releases.get_by_manifest(accepted.manifest.id)
    assert reread_manifest is not None and reread_release is not None
    assert reread_manifest.to_json() == old_manifest_json
    assert reread_manifest.content_sha256 == old_manifest_hash
    assert reread_release == old_release
    assert reread_release.edition_document_sha256 == old_release_hash


def _names(content: bytes) -> list[str]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return archive.namelist()
