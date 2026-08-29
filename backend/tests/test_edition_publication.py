from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from io import BytesIO
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
    PublicationAcceptanceError,
)
from cti_app.application.edition_review import EditionReviewReadItem
from cti_app.application.jobs import DuplicateJobError
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.jobs import Job
from cti_app.domain.production import (
    EditionProductionBatch,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication import BriefDocumentV1
from cti_app.domain.publication_review import PublicationDecision

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_A = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT_B = UUID("33333333-3333-4333-8333-333333333333")
RUN_A = UUID("44444444-4444-4444-8444-444444444444")
RUN_B = UUID("55555555-5555-4555-8555-555555555555")
ARTIFACT_A = UUID("66666666-6666-4666-8666-666666666666")
ARTIFACT_B = UUID("77777777-7777-4777-8777-777777777777")
DECISION_B = UUID("88888888-8888-4888-8888-888888888888")


def _edition() -> Edition:
    return Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_major_articles=0,
        target_briefs=2,
        source_profile="test",
        status=EditionStatus.REVIEW,
    )


def _run(run_id: UUID, subject_id: UUID) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=EDITION_ID,
        profile=ProductionProfile.BRIEF_AUTO,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        pipeline_generation=2,
    )


def _artifact(
    artifact_id: UUID, run_id: UUID, subject_id: UUID, blob_id: UUID
) -> ProductionArtifact:
    return ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.BRIEF,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )


def _document(title: str) -> BriefDocumentV1:
    return BriefDocumentV1(
        schema_version="1",
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
        self, edition: Edition, rows: list[EditionReviewReadItem], blobs: _BlobStore
    ) -> None:
        blob_a = uuid4()
        blob_b = uuid4()
        blobs.blobs[blob_a] = json.dumps(_document("Alpha").to_json()).encode()
        blobs.blobs[blob_b] = json.dumps(_document("Bravo").to_json()).encode()
        self.editions = _Editions(edition)
        self.edition_production_batches = type(
            "Batches", (), {"get_latest_for_edition": self._get_batch}
        )()
        self.batch = EditionProductionBatch(
            edition_id=EDITION_ID,
            profile=ProductionProfile.BRIEF_AUTO,
            status="running",
            phase=ProductionBatchPhase.REVIEW,
        )
        self.subject_production_runs = _Runs(
            {RUN_A: _run(RUN_A, SUBJECT_A), RUN_B: _run(RUN_B, SUBJECT_B)}
        )
        self.production_artifacts = _Artifacts(
            {
                ARTIFACT_A: _artifact(ARTIFACT_A, RUN_A, SUBJECT_A, blob_a),
                ARTIFACT_B: _artifact(ARTIFACT_B, RUN_B, SUBJECT_B, blob_b),
            }
        )
        self.edition_review_read_model = _ReadModel(rows)
        self.publication_manifests = _ManifestRepo(blobs)
        self.publication_manifest_entries = _Entries()
        self.publication_manifest_exclusions = _Exclusions()
        self.edition_releases = _ReleaseRepo()
        self.edition_audit = _Audit()

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

    assert uow.editions.edition.status is EditionStatus.PUBLISHED
    assert content[:2] == b"PK"
    with zipfile.ZipFile(BytesIO(content)) as archive:
        document_xml = archive.read("word/document.xml")
        assert b"Alpha" in document_xml
    await assembly.assemble(accepted.manifest_id)
    assert uow.edition_releases.add_calls == 1
