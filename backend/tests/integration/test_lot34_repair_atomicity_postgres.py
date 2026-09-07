"""PostgreSQL proof that repair materialization is atomic and freeze-safe.

These tests drive the real ``ProductionRepairMaterializationService`` against
real transactions, not a hand-built reconstruction of its result.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_publication import (
    EditionPublicationService,
    PublicationAcceptanceError,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
    reference_report_to_json,
    technical_extraction_to_json,
)
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionService,
    ProductionRepairStaleError,
    build_repair_evidence_pack,
    repair_key_for_rejection,
)
from cti_app.application.production_stages import PublicationAssemblyService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionRun,
    SubjectProductionStatus,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration

# The migrated database is session-scoped: every seed owns a distinct period.
_PERIODS = itertools.count(2130)

SOURCE_URL = "https://source.example/lot34-report"
REJECTED_DOMAIN = "lot34-repaired.com"


class _PassingQA:
    """QA content is out of scope here; the transaction boundary is not."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls = 0

    async def run_qa(self, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {
            "passed": self.passed,
            "checks": {},
            "errors": [] if self.passed else ["lot34_forced_qa_failure"],
            "warnings": [],
        }


class _PausingAssembly:
    """Real assembly, held open exactly where LOT 33 committed too early."""

    def __init__(
        self,
        inner: PublicationAssemblyService,
        *,
        reached: asyncio.Event,
        release: asyncio.Event,
        error: Exception | None = None,
    ) -> None:
        self._inner = inner
        self.reached = reached
        self.release = release
        self.error = error

    async def _load_inputs(self, *args: Any) -> Any:
        return await self._inner._load_inputs(*args)

    async def assemble_publication_in_uow(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return await self._inner.assemble_publication_in_uow(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class _Fixture:
    edition: Edition
    subject: Subject
    run: SubjectProductionRun
    batch: EditionProductionBatch
    store: ProductionArtifactStore
    references: ProductionArtifact
    extraction: ProductionArtifact
    synthesis: ProductionArtifact
    publication: ProductionArtifact
    repair_key: str


def _report() -> ReferenceReport:
    return ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="LOT 34 report",
                url=SOURCE_URL,
                canonical_url=SOURCE_URL,
                publisher="Example",
                published_at=None,
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(),
    )


def _evidence_entry() -> dict[str, Any]:
    return {
        "source_id": "S1",
        "source_title": "LOT 34 report",
        "source_url": SOURCE_URL,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "reason_code": "source_evidence_missing",
        "value": REJECTED_DOMAIN,
        "value_sha256": hashlib.sha256(REJECTED_DOMAIN.encode("utf-8")).hexdigest(),
        "model_run_id": "model-run-lot34",
    }


async def _seed(uow_factory: UnitOfWorkFactory, tmp_path: Path) -> _Fixture:
    store = ProductionArtifactStore(BlobCatalogService(FilesystemBlobStore(tmp_path), uow_factory))
    period_year = next(_PERIODS)
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(period_year, 11, 1),
        period_end=date(period_year, 11, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_articles=1,
        source_profile="lot34",
        status=EditionStatus.REVIEW,
    )
    subject = Subject(
        external_id=f"LOT34-{uuid4().hex}",
        slug=f"lot34-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status="running",
        phase=ProductionBatchPhase.REVIEW,
    )
    item = EditionProductionBatchItem(
        batch_id=batch.id,
        subject_id=subject.id,
        production_run_id=run.id,
        position=1,
    )

    entry = _evidence_entry()
    evidence_id = await store.put_repair_evidence(build_repair_evidence_pack([entry]))
    references_blob = await store.put_json(
        reference_report_to_json(_report()), bucket="production-artifacts-canonical"
    )
    extraction_blob = await store.put_json(
        technical_extraction_to_json(TechnicalExtraction(items=(), rules=())),
        bucket="production-artifacts-canonical",
    )
    synthesis_blob = await store.put_text(
        "Synthèse LOT 34.", bucket="production-artifacts-rendered"
    )

    references = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="1" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob,
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="2" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob,
        metadata={
            "repair_evidence": {
                "schema_version": "1",
                "blob_id": str(evidence_id),
                "entry_count": 1,
                "index": [
                    {
                        key: entry[key]
                        for key in (
                            "source_id",
                            "source_title",
                            "source_url",
                            "proposal_kind",
                            "artifact_type",
                            "reason_code",
                            "value_sha256",
                        )
                    }
                    | {"preview": entry["value"]}
                ],
            }
        },
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="3" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        rendered_blob_id=synthesis_blob,
    )

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.edition_production_batches.add(batch)
        await uow.subject_production_runs.add(run)
        await uow.production_artifacts.append(references)
        await uow.production_artifacts.append(extraction)
        await uow.production_artifacts.append(synthesis)
        await uow.edition_production_batch_items.append_many((item,))
        await uow.commit()

    # The first document is built by the real assembly, so it carries the same
    # ``input_artifacts`` proof a production run would record.
    publication = await PublicationAssemblyService(uow_factory, store).assemble_publication(
        run.id,
        subject.id,
        "LOT 34",
        references,
        extraction,
        synthesis,
    )

    repair_key = repair_key_for_rejection(
        edition_id=edition.id,
        subject_id=subject.id,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_URL,
        artifact_type="domain",
        value=REJECTED_DOMAIN,
    )
    async with uow_factory() as uow:
        await uow.production_repair_decisions.append(
            ProductionRepairDecision(
                edition_id=edition.id,
                subject_id=subject.id,
                production_run_id=run.id,
                observed_artifact_id=extraction.id,
                observed_pipeline_generation=run.pipeline_generation,
                repair_key=repair_key,
                issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
                action=ProductionRepairAction.INCLUDE,
                actor_id="analyst",
            )
        )
        await uow.commit()

    return _Fixture(
        edition=edition,
        subject=subject,
        run=run,
        batch=batch,
        store=store,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        publication=publication,
        repair_key=repair_key,
    )


def _service(
    uow_factory: UnitOfWorkFactory,
    fixture: _Fixture,
    *,
    assembly: Any = None,
    qa: Any = None,
) -> ProductionRepairMaterializationService:
    return ProductionRepairMaterializationService(
        uow_factory,
        projection_service=ProductionRepairProjectionService(uow_factory, fixture.store),
        publication_assembly_service=(
            assembly or PublicationAssemblyService(uow_factory, fixture.store)
        ),
        qa_service=qa or _PassingQA(),
        checkpoint_service=None,
        artifact_store=fixture.store,
    )


async def _current(
    uow_factory: UnitOfWorkFactory, run_id: UUID, stage: ProductionArtifactStage
) -> ProductionArtifact | None:
    async with uow_factory() as uow:
        return await uow.production_artifacts.get_current(run_id, stage.value)


@pytest.mark.asyncio
async def test_repair_materialization_is_one_commit_with_the_new_publication(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """The real service publishes Extraction v2 and Publication v2 together."""
    fixture = await _seed(uow_factory, tmp_path)

    result = await _service(uow_factory, fixture).apply(
        edition_id=fixture.edition.id,
        subject_id=fixture.subject.id,
        actor_id="analyst",
        observed_run_id=fixture.run.id,
        observed_pipeline_generation=fixture.run.pipeline_generation,
    )

    assert result.action == "publication_reassembled"
    assert result.publication_artifact is not None
    extraction = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.EXTRACTION)
    publication = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.PUBLICATION)
    synthesis = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.SYNTHESIS)
    assert extraction is not None and extraction.version == 2
    assert publication is not None and publication.id == result.publication_artifact.id
    assert publication.version == 2
    # The Synthesis is deliberately reused: no Q4 is replayed.
    assert synthesis is not None and synthesis.id == fixture.synthesis.id
    assert publication.metadata["input_artifacts"] == {
        "references_artifact_id": str(fixture.references.id),
        "extraction_artifact_id": str(extraction.id),
        "synthesis_artifact_id": str(fixture.synthesis.id),
    }
    async with uow_factory() as uow:
        rows = await uow.production_artifacts.list_for_run(fixture.run.id)
    stale = {
        (row.stage, row.version) for row in rows if row.status is ProductionArtifactStatus.STALE
    }
    assert stale == {(ProductionArtifactStage.PUBLICATION, 1)}


@pytest.mark.asyncio
async def test_concurrent_accept_can_only_freeze_the_repaired_publication(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """The freeze waits for the repair and never manifests the old document."""
    fixture = await _seed(uow_factory, tmp_path)
    reached = asyncio.Event()
    release = asyncio.Event()
    assembly = _PausingAssembly(
        PublicationAssemblyService(uow_factory, fixture.store),
        reached=reached,
        release=release,
    )
    service = _service(uow_factory, fixture, assembly=assembly)
    publication_service = EditionPublicationService(uow_factory, fixture.store)

    async def materialize() -> Any:
        return await service.apply(
            edition_id=fixture.edition.id,
            subject_id=fixture.subject.id,
            actor_id="analyst",
            observed_run_id=fixture.run.id,
            observed_pipeline_generation=fixture.run.pipeline_generation,
        )

    async def accept() -> tuple[Any, float]:
        await reached.wait()
        started = monotonic()
        accepted = await publication_service.accept(fixture.edition.id, actor_id="analyst")
        return accepted, monotonic() - started

    materialize_task = asyncio.create_task(materialize())
    accept_task = asyncio.create_task(accept())
    await reached.wait()
    # The accept is now blocked on the Edition row lock held by the repair.
    await asyncio.sleep(0.3)
    release.set()
    result = await materialize_task
    accepted, waited = await accept_task

    assert result.publication_artifact is not None
    new_publication_id = result.publication_artifact.id
    assert new_publication_id != fixture.publication.id
    # The accept could not proceed while the repair held its fence.
    assert waited >= 0.2
    assert len(accepted.manifest.entries) == 1
    assert accepted.manifest.entries[0].document_artifact_id == new_publication_id
    assert accepted.manifest.entries[0].document_artifact_version == 2


@pytest.mark.asyncio
async def test_a_failed_repair_leaves_the_pre_repair_article_freezable(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """An Assembly failure commits nothing; the old document stays current."""
    fixture = await _seed(uow_factory, tmp_path)
    reached = asyncio.Event()
    release = asyncio.Event()
    assembly = _PausingAssembly(
        PublicationAssemblyService(uow_factory, fixture.store),
        reached=reached,
        release=release,
        error=RuntimeError("assembly exploded"),
    )
    service = _service(uow_factory, fixture, assembly=assembly)
    publication_service = EditionPublicationService(uow_factory, fixture.store)

    async def materialize() -> None:
        with pytest.raises(RuntimeError, match="assembly exploded"):
            await service.apply(
                edition_id=fixture.edition.id,
                subject_id=fixture.subject.id,
                actor_id="analyst",
                observed_run_id=fixture.run.id,
                observed_pipeline_generation=fixture.run.pipeline_generation,
            )

    async def accept() -> Any:
        await reached.wait()
        return await publication_service.accept(fixture.edition.id, actor_id="analyst")

    materialize_task = asyncio.create_task(materialize())
    accept_task = asyncio.create_task(accept())
    await reached.wait()
    await asyncio.sleep(0.2)
    release.set()
    await materialize_task
    accepted = await accept_task

    extraction = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.EXTRACTION)
    publication = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.PUBLICATION)
    assert extraction is not None and extraction.id == fixture.extraction.id
    assert publication is not None and publication.id == fixture.publication.id
    assert publication.status is ProductionArtifactStatus.VERIFIED
    assert accepted.manifest.entries[0].document_artifact_id == fixture.publication.id
    async with uow_factory() as uow:
        rows = await uow.production_artifacts.list_for_run(fixture.run.id)
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_a_failed_qa_rolls_the_whole_repair_back(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    fixture = await _seed(uow_factory, tmp_path)
    service = _service(uow_factory, fixture, qa=_PassingQA(passed=False))

    with pytest.raises(Exception, match="production_repair_qa_failed"):
        await service.apply(
            edition_id=fixture.edition.id,
            subject_id=fixture.subject.id,
            actor_id="analyst",
            observed_run_id=fixture.run.id,
            observed_pipeline_generation=fixture.run.pipeline_generation,
        )

    async with uow_factory() as uow:
        rows = await uow.production_artifacts.list_for_run(fixture.run.id)
    assert len(rows) == 4
    assert all(row.status is ProductionArtifactStatus.VERIFIED for row in rows)


@pytest.mark.asyncio
async def test_freeze_refuses_a_document_older_than_the_effective_extraction(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Defence in depth against the exact window LOT 33 could commit.

    The public projection wrapper commits the effective Extraction on its own
    — which is why the materialization path no longer uses it.  Reproducing
    that sequencing leaves a repaired Extraction current beside the previous
    document, and the freeze must refuse rather than manifest it.
    """
    fixture = await _seed(uow_factory, tmp_path)
    projection = await ProductionRepairProjectionService(
        uow_factory, fixture.store
    ).project_effective_extraction(fixture.run.id, actor_id="analyst")
    assert projection.changed

    extraction = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.EXTRACTION)
    publication = await _current(uow_factory, fixture.run.id, ProductionArtifactStage.PUBLICATION)
    # The dangerous committed state: repaired Extraction, previous document.
    assert extraction is not None and extraction.id == projection.artifact.id
    assert publication is not None and publication.id == fixture.publication.id
    assert publication.status is ProductionArtifactStatus.VERIFIED

    with pytest.raises(PublicationAcceptanceError, match="repair_materialization_incomplete"):
        await EditionPublicationService(uow_factory, fixture.store).accept(
            fixture.edition.id, actor_id="analyst"
        )


@pytest.mark.asyncio
async def test_a_concurrent_retry_makes_the_plan_stale_without_writing(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Generation N+1 refuses a materialization planned on generation N."""
    fixture = await _seed(uow_factory, tmp_path)
    observed_generation = fixture.run.pipeline_generation

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_for_update(fixture.run.id)
        assert run is not None
        run.pipeline_generation += 1
        await uow.subject_production_runs.save(run)
        await uow.commit()

    with pytest.raises(ProductionRepairStaleError):
        await _service(uow_factory, fixture).apply(
            edition_id=fixture.edition.id,
            subject_id=fixture.subject.id,
            actor_id="analyst",
            observed_run_id=fixture.run.id,
            observed_pipeline_generation=observed_generation,
        )

    async with uow_factory() as uow:
        rows = await uow.production_artifacts.list_for_run(fixture.run.id)
    assert len(rows) == 4
