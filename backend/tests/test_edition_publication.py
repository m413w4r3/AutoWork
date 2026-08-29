from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import date
from io import BytesIO
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router as publication_router
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
    PublicationAcceptanceError,
    PublicationAssemblyError,
)
from cti_app.application.edition_review import EditionReviewReadItem
from cti_app.application.jobs import DuplicateJobError
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    PublicationManifestEntryV1,
    PublicationManifestV1,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.jobs import Job
from cti_app.domain.production import (
    EditionProductionBatch,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication import BriefDocumentV1, PublicationDocumentV2
from cti_app.domain.publication_review import PublicationDecision

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_A = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT_B = UUID("33333333-3333-4333-8333-333333333333")
SUBJECT_C = UUID("33333333-3333-4333-8333-333333333334")
RUN_A = UUID("44444444-4444-4444-8444-444444444444")
RUN_B = UUID("55555555-5555-4555-8555-555555555555")
RUN_C = UUID("55555555-5555-4555-8555-555555555556")
ARTIFACT_A = UUID("66666666-6666-4666-8666-666666666666")
ARTIFACT_B = UUID("77777777-7777-4777-8777-777777777777")
ARTIFACT_C = UUID("77777777-7777-4777-8777-777777777778")
DECISION_B = UUID("88888888-8888-4888-8888-888888888888")
DECISION_C = UUID("88888888-8888-4888-8888-888888888889")


def _edition() -> Edition:
    return Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=3,
        source_profile="test",
        status=EditionStatus.REVIEW,
    )


def _run(run_id: UUID, subject_id: UUID) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=EDITION_ID,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        pipeline_generation=2,
    )


def _artifact(
    artifact_id: UUID,
    run_id: UUID,
    subject_id: UUID,
    blob_id: UUID,
    *,
    legacy: bool = False,
) -> ProductionArtifact:
    return ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.BRIEF if legacy else ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )


def _document(title: str, *, legacy: bool = False) -> BriefDocumentV1 | PublicationDocumentV2:
    document_type = BriefDocumentV1 if legacy else PublicationDocumentV2
    return document_type(
        schema_version="1" if legacy else "2",
        title=title,
        timeline=(),
        synthesis=(),
        indicators=(),
        sources=(),
        uncertainties=(),
    )


class _BlobStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        del bucket
        content = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        self.blobs[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    async def put_text(self, content: str, *, bucket: str) -> UUID:
        return await self.put_bytes(content.encode(), bucket=bucket, mime_type="text/plain")

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del bucket, mime_type
        blob_id = uuid4()
        self.blobs[blob_id] = content
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return json.loads(self.blobs[blob_id])  # type: ignore[no-any-return]

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.blobs[blob_id]
        assert len(content) <= max_bytes
        return content


class _ManifestRepo:
    def __init__(self, blobs: _BlobStore) -> None:
        self.manifest = None
        self.blob_id: UUID | None = None
        self.blobs = blobs

    async def add(self, manifest: Any, manifest_blob_id: UUID) -> None:
        self.manifest = manifest
        self.blob_id = manifest_blob_id

    async def get(self, manifest_id: UUID) -> Any:
        return self.manifest if self.manifest and self.manifest.id == manifest_id else None

    async def get_blob_id(self, manifest_id: UUID) -> UUID | None:
        return self.blob_id if self.manifest and self.manifest.id == manifest_id else None

    async def get_latest_for_edition(self, edition_id: UUID) -> Any:
        return self.manifest if self.manifest and self.manifest.edition_id == edition_id else None

    async def get_for_edition_version(self, edition_id: UUID, edition_version: int) -> Any:
        return (
            self.manifest
            if self.manifest
            and self.manifest.edition_id == edition_id
            and self.manifest.edition_version == edition_version
            else None
        )


class _Entries:
    async def append_many(self, manifest_id: UUID, entries: Any) -> None:
        del manifest_id, entries

    async def list_for_manifest(self, manifest_id: UUID) -> tuple[Any, ...]:
        del manifest_id
        return ()


class _Exclusions(_Entries):
    pass


class _ReleaseRepo:
    def __init__(self) -> None:
        self.release = None
        self.add_calls = 0

    async def add_if_absent(self, release: Any) -> bool:
        self.add_calls += 1
        if self.release is not None:
            return False
        self.release = release
        return True

    async def get_by_manifest(self, manifest_id: UUID) -> Any:
        return self.release if self.release and self.release.manifest_id == manifest_id else None

    async def get_for_edition(self, edition_id: UUID) -> Any:
        return self.release if self.release and self.release.edition_id == edition_id else None


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return await self.get(edition_id)

    async def update(self, edition: Edition, expected_version: int) -> bool:
        return edition.id == self.edition.id and expected_version + 1 == edition.version


class _Runs:
    def __init__(self, runs: dict[UUID, SubjectProductionRun]) -> None:
        self.runs = runs

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.runs.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return await self.get(run_id)


class _Artifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts
        self.current_called = False

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        del run_id, stage
        self.current_called = True
        raise AssertionError("frozen assembly must not resolve current artifacts")


class _ReadModel:
    def __init__(self, rows: list[EditionReviewReadItem]) -> None:
        self.rows = rows

    async def list_for_edition(self, edition_id: UUID) -> list[EditionReviewReadItem]:
        del edition_id
        return self.rows


class _Audit:
    async def append(self, event: Any) -> None:
        del event


class _Uow:
    def __init__(
        self,
        edition: Edition,
        rows: list[EditionReviewReadItem],
        blobs: _BlobStore,
        *,
        legacy: bool = False,
    ) -> None:
        blob_a = uuid4()
        blob_b = uuid4()
        blob_c = uuid4()
        blobs.blobs[blob_a] = json.dumps(_document("Alpha", legacy=legacy).to_json()).encode()
        blobs.blobs[blob_b] = json.dumps(_document("Bravo", legacy=legacy).to_json()).encode()
        blobs.blobs[blob_c] = json.dumps(_document("Charlie", legacy=legacy).to_json()).encode()
        self.editions = _Editions(edition)
        self.edition_production_batches = type(
            "Batches", (), {"get_latest_for_edition": self._get_batch}
        )()
        self.batch = EditionProductionBatch(
            edition_id=EDITION_ID,
            status="running",
            phase=ProductionBatchPhase.REVIEW,
        )
        self.subject_production_runs = _Runs(
            {
                RUN_A: _run(RUN_A, SUBJECT_A),
                RUN_B: _run(RUN_B, SUBJECT_B),
                RUN_C: _run(RUN_C, SUBJECT_C),
            }
        )
        self.production_artifacts = _Artifacts(
            {
                ARTIFACT_A: _artifact(ARTIFACT_A, RUN_A, SUBJECT_A, blob_a, legacy=legacy),
                ARTIFACT_B: _artifact(ARTIFACT_B, RUN_B, SUBJECT_B, blob_b, legacy=legacy),
                ARTIFACT_C: _artifact(ARTIFACT_C, RUN_C, SUBJECT_C, blob_c, legacy=legacy),
            }
        )
        self.edition_review_read_model = _ReadModel(rows)
        self.publication_manifests = _ManifestRepo(blobs)
        self.publication_manifest_entries = _Entries()
        self.publication_manifest_exclusions = _Exclusions()
        self.edition_releases = _ReleaseRepo()
        self.edition_audit = _Audit()
        self.jobs = _Jobs()

    async def _get_batch(self, edition_id: UUID) -> EditionProductionBatch | None:
        return self.batch if edition_id == EDITION_ID else None

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        return None


class _Jobs:
    def __init__(self, fail_submit: bool = False) -> None:
        self.jobs: dict[UUID, Job] = {}
        self.fail_submit = fail_submit

    async def submit(self, **kwargs: Any) -> Job:
        if self.fail_submit:
            raise RuntimeError("database unavailable")
        await asyncio.sleep(0)
        arguments = dict(kwargs)
        arguments.pop("actor_id", None)
        existing = next(
            (
                job
                for job in self.jobs.values()
                if job.idempotency_key == arguments["idempotency_key"]
            ),
            None,
        )
        if existing is not None:
            raise DuplicateJobError(existing.id)
        job = Job(**arguments)
        self.jobs[job.id] = job
        return job

    async def get(self, job_id: UUID) -> Job:
        return self.jobs[job_id]

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID, *, kind: str | None = None
    ) -> list[Job]:
        return [
            job
            for job in self.jobs.values()
            if job.aggregate_type == aggregate_type
            and job.aggregate_id == aggregate_id
            and (kind is None or job.kind == kind)
        ]

    async def retry(self, job_id: UUID, *, actor_id: str = "system") -> Job:
        del actor_id
        job = self.jobs[job_id]
        job.retry_manually()
        return job


def _job_for_manifest(
    manifest: PublicationManifestV1,
    *,
    status: str,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        kind="publication.edition.assemble",
        aggregate_type="edition",
        aggregate_id=manifest.edition_id,
        idempotency_key=f"job-{uuid4()}",
        correlation_id="test",
        input_parameters={"manifest_id": str(manifest.id)},
        max_attempts=max_attempts,
    )
    if status == "running":
        job.start()
    elif status == "failed":
        job.start()
        job.fail("pandoc_failed", "Pandoc failed", details={"private": "hidden"})
    elif status == "failed_exhausted":
        job.start()
        job.fail("pandoc_failed", "Pandoc failed", details={"private": "hidden"})
    elif status == "cancelled":
        job.request_cancellation()
    elif status == "succeeded":
        job.start()
        job.succeed("release://one")
    return job


def _job_manifest() -> PublicationManifestV1:
    return PublicationManifestV1.create(
        edition_id=EDITION_ID,
        edition_version=1,
        batch_id=uuid4(),
        created_by="analyst",
        entries=(
            PublicationManifestEntryV1(
                position=1,
                subject_id=SUBJECT_A,
                production_run_id=RUN_A,
                pipeline_generation=2,
                document_artifact_id=ARTIFACT_A,
                document_artifact_version=1,
                document_input_hash="a" * 64,
            ),
        ),
        exclusions=(),
    )


class _Dispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.calls.append(job_id)
        if self.fail:
            raise RuntimeError("redis unavailable")


@pytest.mark.asyncio
async def test_accept_freezes_order_exclusion_and_same_manifest_on_retry() -> None:
    rows = [
        EditionReviewReadItem(
            position=1,
            subject_id=SUBJECT_A,
            title="Alpha",
            run_id=RUN_A,
            pipeline_generation=2,
            run_status=SubjectProductionStatus.READY,
            document_artifact_id=ARTIFACT_A,
            document_artifact_version=1,
            document_input_hash="a" * 64,
            document_artifact_status=ProductionArtifactStatus.VERIFIED,
            error_code=None,
            error_message=None,
            effective_decision=None,
        ),
        EditionReviewReadItem(
            position=2,
            subject_id=SUBJECT_B,
            title="Bravo",
            run_id=RUN_B,
            pipeline_generation=2,
            run_status=SubjectProductionStatus.FAILED,
            document_artifact_id=None,
            document_artifact_version=None,
            document_input_hash=None,
            document_artifact_status=None,
            error_code="failed",
            error_message="failed",
            effective_decision=PublicationDecision.EXCLUDE,
            effective_decision_id=DECISION_B,
        ),
    ]
    blobs = _BlobStore()
    uow = _Uow(_edition(), rows, blobs)
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    service = EditionPublicationService(
        lambda: uow, blobs, job_service=jobs, job_dispatcher=dispatcher
    )  # type: ignore[arg-type]

    first = await service.accept(EDITION_ID, actor_id="analyst")
    second = await service.accept(EDITION_ID, actor_id="analyst")

    assert first.manifest_id == second.manifest_id
    assert [entry.subject_id for entry in first.manifest.entries] == [SUBJECT_A]
    assert first.manifest.exclusions[0].review_decision_id == DECISION_B
    assert len(jobs.jobs) == 1
    assert len(dispatcher.calls) == 2


@pytest.mark.parametrize(
    ("excluded_positions", "expected_positions"),
    (
        ((), [1, 2, 3]),
        ((1,), [2, 3]),
        ((2,), [1, 3]),
        ((3,), [1, 2]),
        ((1, 3), [2]),
    ),
)
async def test_accept_and_docx_preserve_editorial_positions_with_exclusions(
    excluded_positions: tuple[int, ...], expected_positions: list[int]
) -> None:
    subjects = (SUBJECT_A, SUBJECT_B, SUBJECT_C)
    runs = (RUN_A, RUN_B, RUN_C)
    artifacts = (ARTIFACT_A, ARTIFACT_B, ARTIFACT_C)
    titles = ("Alpha", "Bravo", "Charlie")
    decisions = (DECISION_B, DECISION_C, UUID("88888888-8888-4888-8888-888888888890"))
    rows = []
    for position, (subject_id, run_id, artifact_id, title, decision_id) in enumerate(
        zip(subjects, runs, artifacts, titles, decisions, strict=True), start=1
    ):
        excluded = position in excluded_positions
        rows.append(
            EditionReviewReadItem(
                position=position,
                subject_id=subject_id,
                title=title,
                run_id=run_id,
                pipeline_generation=2,
                run_status=(
                    SubjectProductionStatus.FAILED if excluded else SubjectProductionStatus.READY
                ),
                document_artifact_id=None if excluded else artifact_id,
                document_artifact_version=None if excluded else 1,
                document_input_hash=None if excluded else "a" * 64,
                document_artifact_status=(None if excluded else ProductionArtifactStatus.VERIFIED),
                error_code="failed" if excluded else None,
                error_message="failed" if excluded else None,
                effective_decision=PublicationDecision.EXCLUDE if excluded else None,
                effective_decision_id=decision_id if excluded else None,
            )
        )

    blobs = _BlobStore()
    uow = _Uow(_edition(), rows, blobs)
    publication = EditionPublicationService(lambda: uow, blobs)  # type: ignore[arg-type]
    accepted = await publication.accept(EDITION_ID, actor_id="analyst")

    assert [entry.position for entry in accepted.manifest.entries] == expected_positions
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]
    release = await assembly.assemble(accepted.manifest_id)
    edition_json = await blobs.read_json(release.edition_document_blob_id)
    assert [item["position"] for item in edition_json["publications"]] == expected_positions
    assert [item["document"]["title"] for item in edition_json["publications"]] == [
        title
        for position, title in enumerate(titles, start=1)
        if position not in excluded_positions
    ]

    content = await blobs.read_bytes(release.docx_blob_id, max_bytes=32 * 1024 * 1024)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        document_xml = archive.read("word/document.xml")
    for position, title in enumerate(titles, start=1):
        assert (title.encode() in document_xml) is (position not in excluded_positions)


def test_manifest_and_edition_document_allow_multiple_editorial_gaps() -> None:
    entries = tuple(
        PublicationManifestEntryV1(
            position=position,
            subject_id=uuid4(),
            production_run_id=uuid4(),
            pipeline_generation=0,
            document_artifact_id=uuid4(),
            document_artifact_version=1,
            document_input_hash="a" * 64,
        )
        for position in (7, 2, 4)
    )
    manifest = PublicationManifestV1.create(
        edition_id=EDITION_ID,
        edition_version=1,
        batch_id=uuid4(),
        created_by="analyst",
        entries=entries,
        exclusions=(),
    )
    assert [entry.position for entry in manifest.entries] == [2, 4, 7]

    document = EditionDocumentV2(
        edition={"id": str(EDITION_ID)},
        publications=tuple(
            EditionPublicationV2(
                position=entry.position,
                subject_id=entry.subject_id,
                document=_document(str(entry.position)),
            )
            for entry in entries
        ),
    )
    assert [publication.position for publication in document.ordered_publications] == [2, 4, 7]


@pytest.mark.asyncio
async def test_historical_brief_manifest_still_assembles_as_v1() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [], blobs, legacy=True)
    manifest = _job_manifest()
    manifest_blob_id, _ = await blobs.put_canonical_json(manifest.to_json(), bucket="manifests")
    await uow.publication_manifests.add(manifest, manifest_blob_id)
    uow.editions.edition.transition(EditionStatus.ASSEMBLING)

    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]
    release = await assembly.assemble(manifest.id)

    edition_document = await blobs.read_json(release.edition_document_blob_id)
    assert edition_document["schema_version"] == "1"
    assert edition_document["publications"][0]["document"]["schema_version"] == "1"


@pytest.mark.asyncio
async def test_dispatch_failure_keeps_freeze_and_retry_reuses_job() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    jobs = _Jobs()
    dispatcher = _Dispatcher(fail=True)
    service = EditionPublicationService(
        lambda: uow, blobs, job_service=jobs, job_dispatcher=dispatcher
    )  # type: ignore[arg-type]

    result = await service.accept(EDITION_ID, actor_id="analyst")

    assert result.manifest_id == uow.publication_manifests.manifest.id
    assert uow.editions.edition.status is EditionStatus.ASSEMBLING
    assert len(jobs.jobs) == 1


@pytest.mark.asyncio
async def test_accept_in_assembling_repairs_failed_job_without_new_manifest() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    jobs = uow.jobs
    dispatcher = _Dispatcher()
    service = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=jobs,
        job_dispatcher=dispatcher,
    )  # type: ignore[arg-type]

    first = await service.accept(EDITION_ID, actor_id="analyst")
    job = jobs.jobs[first.job_id]
    job.start()
    job.fail("pandoc_failed", "Pandoc failed")
    second = await service.accept(EDITION_ID, actor_id="analyst")

    assert second.manifest_id == first.manifest_id
    assert second.job_id == job.id
    assert jobs.jobs[job.id].status.value == "queued"
    assert dispatcher.calls == [job.id, job.id]


@pytest.mark.asyncio
async def test_job_creation_failure_keeps_freeze_for_a_later_retry() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    service = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=_Jobs(fail_submit=True),
        job_dispatcher=_Dispatcher(),
    )  # type: ignore[arg-type]

    result = await service.accept(EDITION_ID, actor_id="analyst")

    assert result.job_id is None
    assert not result.job_dispatched
    assert result.manifest_id == uow.publication_manifests.manifest.id
    assert uow.editions.edition.status is EditionStatus.ASSEMBLING


@pytest.mark.asyncio
async def test_empty_review_is_rejected_without_freeze() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [], blobs)
    service = EditionPublicationService(lambda: uow, blobs)  # type: ignore[arg-type]

    with pytest.raises(PublicationAcceptanceError):
        await service.accept(EDITION_ID, actor_id="analyst")
    assert uow.editions.edition.status is EditionStatus.REVIEW


@pytest.mark.asyncio
async def test_assembly_reads_manifest_artifact_id_and_publishes_real_docx() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    publication = EditionPublicationService(lambda: uow, blobs)  # type: ignore[arg-type]
    accepted = await publication.accept(EDITION_ID, actor_id="analyst")
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]

    release = await assembly.assemble(accepted.manifest_id)
    content = await blobs.read_bytes(release.docx_blob_id, max_bytes=32 * 1024 * 1024)
    edition_document = await blobs.read_json(release.edition_document_blob_id)

    assert uow.editions.edition.status is EditionStatus.PUBLISHED
    assert edition_document["schema_version"] == "2"
    assert edition_document["publications"][0]["document"]["schema_version"] == "2"
    assert content[:2] == b"PK"
    with zipfile.ZipFile(BytesIO(content)) as archive:
        document_xml = archive.read("word/document.xml")
        assert b"Alpha" in document_xml
    await assembly.assemble(accepted.manifest_id)
    assert uow.edition_releases.add_calls == 1


@pytest.mark.asyncio
async def test_assembly_rejects_an_edition_changed_after_freeze() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    publication = EditionPublicationService(lambda: uow, blobs)  # type: ignore[arg-type]
    accepted = await publication.accept(EDITION_ID, actor_id="analyst")
    uow.editions.edition.version += 1
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]

    with pytest.raises(PublicationAssemblyError, match="edition_changed_after_publication_freeze"):
        await assembly.assemble(accepted.manifest_id)

    assert uow.edition_releases.release is None
    assert uow.editions.edition.status is EditionStatus.ASSEMBLING


@pytest.mark.asyncio
async def test_assembly_failed_with_attempts_left_is_requeued_and_dispatched() -> None:
    manifest = _job_manifest()
    blobs = _BlobStore()
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    previous = _job_for_manifest(manifest, status="failed")
    jobs.jobs[previous.id] = previous
    service = EditionPublicationService(
        lambda: _Uow(_edition(), [], blobs),
        blobs,
        job_service=jobs,
        job_dispatcher=dispatcher,
    )  # type: ignore[arg-type]

    job_id, dispatched = await service._ensure_assembly_job(
        manifest, correlation_id="retry", actor_id="analyst"
    )

    assert (job_id, dispatched) == (previous.id, True)
    assert jobs.jobs[previous.id].status.value == "queued"
    assert dispatcher.calls == [previous.id]


@pytest.mark.asyncio
async def test_exhausted_or_cancelled_assembly_creates_one_successor() -> None:
    for initial_status in ("failed_exhausted", "cancelled"):
        manifest = _job_manifest()
        blobs = _BlobStore()
        jobs = _Jobs()
        dispatcher = _Dispatcher()
        previous = _job_for_manifest(
            manifest,
            status=initial_status,
            max_attempts=1,
        )
        jobs.jobs[previous.id] = previous
        service = EditionPublicationService(
            lambda current_blobs=blobs: _Uow(_edition(), [], current_blobs),
            blobs,
            job_service=jobs,
            job_dispatcher=dispatcher,
        )  # type: ignore[arg-type]

        job_id, dispatched = await service._ensure_assembly_job(
            manifest, correlation_id="retry", actor_id="analyst"
        )

        assert dispatched is True
        assert job_id != previous.id
        successor = jobs.jobs[job_id]
        assert successor.status.value == "queued"
        assert successor.idempotency_key == (
            f"publication-assemble-{manifest.id}-after-{previous.id}"
        )
        assert successor.input_parameters == {"manifest_id": str(manifest.id)}
        assert dispatcher.calls == [job_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ("running", "succeeded"))
async def test_running_or_succeeded_assembly_is_not_recreated_or_redispatched(
    initial_status: str,
) -> None:
    manifest = _job_manifest()
    blobs = _BlobStore()
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    previous = _job_for_manifest(manifest, status=initial_status)
    jobs.jobs[previous.id] = previous
    service = EditionPublicationService(
        lambda: _Uow(_edition(), [], blobs),
        blobs,
        job_service=jobs,
        job_dispatcher=dispatcher,
    )  # type: ignore[arg-type]

    job_id, dispatched = await service._ensure_assembly_job(
        manifest, correlation_id="retry", actor_id="analyst"
    )

    assert (job_id, dispatched) == (previous.id, False)
    assert len(jobs.jobs) == 1
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_concurrent_exhausted_assembly_retries_share_one_successor() -> None:
    manifest = _job_manifest()
    blobs = _BlobStore()
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    previous = _job_for_manifest(manifest, status="failed_exhausted", max_attempts=1)
    jobs.jobs[previous.id] = previous
    service = EditionPublicationService(
        lambda: _Uow(_edition(), [], blobs),
        blobs,
        job_service=jobs,
        job_dispatcher=dispatcher,
    )  # type: ignore[arg-type]

    results = await asyncio.gather(
        service._ensure_assembly_job(manifest, correlation_id="one", actor_id="analyst"),
        service._ensure_assembly_job(manifest, correlation_id="two", actor_id="analyst"),
    )

    successor_keys = [job.idempotency_key for job in jobs.jobs.values() if job.id != previous.id]
    assert len(successor_keys) == 1
    assert results[0][0] == results[1][0]
    assert len(dispatcher.calls) == 2


@pytest.mark.asyncio
async def test_release_endpoint_exposes_public_assembly_failure_state() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    jobs = uow.jobs
    service = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=jobs,
        job_dispatcher=_Dispatcher(),
    )  # type: ignore[arg-type]
    accepted = await service.accept(EDITION_ID, actor_id="analyst")
    job = jobs.jobs[next(iter(jobs.jobs))]
    job.start()
    job.fail("pandoc_failed", "Public Pandoc failure", details={"secret": "not public"})

    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = service
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/editions/{accepted.manifest.edition_id}/release")

    body = response.json()
    assert response.status_code == 200
    assert body["assembly_job_id"] == str(job.id)
    assert body["assembly_status"] == "failed"
    assert body["assembly_error_code"] == "pandoc_failed"
    assert body["assembly_error_message"] == "Public Pandoc failure"
    assert body["can_retry_assembly"] is True
    assert "error_details" not in body
