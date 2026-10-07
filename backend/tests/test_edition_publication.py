from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router as publication_router
from cti_app.application.edition_document import edition_metadata_projection
from cti_app.application.edition_publication import (
    EDITION_ASSEMBLE_JOB_KIND,
    EDITION_RENDER_JOB_KIND,
    EditionAssembleParameters,
    EditionAssemblyService,
    EditionPublicationService,
    EditionReleaseStatus,
    PublicationAcceptanceError,
    PublicationAssemblyError,
    register_publication_jobs,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseMaterializationError,
    EditionReleaseRematerializationService,
)
from cti_app.application.edition_rendering import EditionRenderParameters, EditionRenderService
from cti_app.application.edition_review import EditionReviewReadItem, EditionReviewService
from cti_app.application.edition_workspace import EditionWorkspaceMaterializer
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobRegistry
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    EditionRelease,
    PublicationManifestEntryV1,
    PublicationManifestV1,
)
from cti_app.domain.edition_render import EditionRender, EditionRenderDisplayStatus
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionRepairAction,
    ProductionRepairIssueKind,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    RepairDecisionApplicationState,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_review import PublicationDecision
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus


def test_edition_release_only_stores_canonical_edition_document() -> None:
    assert [field.name for field in fields(EditionRelease)] == [
        "id",
        "edition_id",
        "manifest_id",
        "edition_document_blob_id",
        "edition_document_sha256",
        "created_at",
    ]


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
        state=EditionStatus.OPEN,
    )


def _run(run_id: UUID, subject_id: UUID) -> ProductionRun:
    return ProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=EDITION_ID,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        pipeline_generation=2,
    )


def _artifact(
    artifact_id: UUID,
    run_id: UUID,
    subject_id: UUID,
    blob_id: UUID,
) -> ProductionArtifact:
    return ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )


def _document(title: str) -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=SUBJECT_A,
        publication_language="fr",
        title=title,
        lead=(),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )


def _successful_edition_render(
    release_id: UUID,
    blobs: _BlobStore,
    *,
    pdf_bytes: bytes = b"%PDF-1.7\ncanonical edition pdf\n",
    created_at: datetime | None = None,
) -> EditionRender:
    output_blob_id = uuid4()
    blobs.blobs[output_blob_id] = pdf_bytes
    now = created_at or datetime.now(UTC)
    return EditionRender(
        id=uuid4(),
        edition_release_id=release_id,
        renderer="typst",
        renderer_version="edition-v2-typst-v2-semantic-annotation-coverage",
        template_version="chp-edition-v1",
        template_sha256="a" * 64,
        compiler="typst",
        compiler_version="0.15.1",
        font_bundle_version="fonts-v1",
        render_policy_version="typst-edition-v2-v1",
        format=TypstRenderFormat.PDF,
        input_hash="b" * 64,
        source_blob_id=uuid4(),
        render_data_blob_id=uuid4(),
        output_blob_id=output_blob_id,
        output_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        output_byte_size=len(pdf_bytes),
        status=TypstRenderStatus.SUCCEEDED,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _release_status(
    *,
    release: EditionRelease | None,
    render: EditionRender | None,
    render_job_status: JobStatus | None,
) -> EditionReleaseStatus:
    return EditionReleaseStatus(
        edition_id=EDITION_ID,
        edition_state=EditionStatus.OPEN,
        manifest_id=uuid4(),
        manifest_sha256="d" * 64,
        release=release,
        assembly_job_id=None,
        assembly_status=None,
        assembly_error_code=None,
        assembly_error_message=None,
        can_retry_assembly=False,
        render=render,
        render_job_id=uuid4() if render_job_status is not None else None,
        render_job_status=render_job_status,
        render_job_error_code=None,
        render_job_error_message=None,
    )


@pytest.mark.parametrize(
    ("render_status", "job_status", "expected", "can_retry"),
    [
        (None, None, EditionRenderDisplayStatus.NOT_STARTED, True),
        (None, JobStatus.QUEUED, EditionRenderDisplayStatus.QUEUED, False),
        (None, JobStatus.FAILED, EditionRenderDisplayStatus.FAILED, True),
        (TypstRenderStatus.FAILED, None, EditionRenderDisplayStatus.FAILED, True),
        (TypstRenderStatus.FAILED, JobStatus.FAILED, EditionRenderDisplayStatus.FAILED, True),
        # A retry job in flight must not be hidden behind the previous failed row.
        (TypstRenderStatus.FAILED, JobStatus.QUEUED, EditionRenderDisplayStatus.QUEUED, False),
        (TypstRenderStatus.FAILED, JobStatus.RUNNING, EditionRenderDisplayStatus.RUNNING, False),
        (TypstRenderStatus.RUNNING, None, EditionRenderDisplayStatus.RUNNING, False),
        (
            TypstRenderStatus.SUCCEEDED,
            JobStatus.QUEUED,
            EditionRenderDisplayStatus.SUCCEEDED,
            False,
        ),
    ],
)
def test_release_status_render_state_prefers_an_active_job_over_a_failed_row(
    render_status: TypstRenderStatus | None,
    job_status: JobStatus | None,
    expected: EditionRenderDisplayStatus,
    can_retry: bool,
) -> None:
    release = EditionRelease(
        edition_id=EDITION_ID,
        manifest_id=uuid4(),
        edition_document_blob_id=uuid4(),
        edition_document_sha256="e" * 64,
    )
    render: EditionRender | None = None
    if render_status is not None:
        succeeded = _successful_edition_render(release.id, _BlobStore())
        render = (
            succeeded
            if render_status is TypstRenderStatus.SUCCEEDED
            else replace(
                succeeded,
                status=render_status,
                output_blob_id=None,
                output_sha256=None,
                output_byte_size=None,
                error_code="typst_compile_failed"
                if render_status is TypstRenderStatus.FAILED
                else None,
            )
        )

    status = _release_status(release=release, render=render, render_job_status=job_status)

    assert status.render_status is expected
    assert status.can_retry_render is can_retry
    assert status.pdf_available is (render_status is TypstRenderStatus.SUCCEEDED)


def test_edition_metadata_projection_is_the_versioned_publication_metadata() -> None:
    edition = _edition()

    projection = edition_metadata_projection(edition)

    assert set(projection) == {
        "id",
        "country",
        "country_code",
        "period_start",
        "period_end",
        "tlp",
        "languages",
        "state",
        "version",
        "created_at",
        "updated_at",
    }
    assert projection["id"] == str(EDITION_ID)
    assert projection["period_start"] == "2026-08-01"
    assert projection["period_end"] == "2026-08-31"
    assert projection["tlp"] == TLP.GREEN.value
    assert projection["state"] == EditionStatus.OPEN.value
    assert projection["version"] == edition.version
    assert datetime.fromisoformat(projection["created_at"])
    assert datetime.fromisoformat(projection["updated_at"])


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

    async def read_text(self, blob_id: UUID) -> str:
        return (await self.read_bytes(blob_id, max_bytes=32 * 1024 * 1024)).decode()


class _ManifestRepo:
    def __init__(self, blobs: _BlobStore) -> None:
        self.manifests: dict[UUID, Any] = {}
        self.manifest_blobs: dict[UUID, UUID] = {}
        self.blobs = blobs

    @property
    def manifest(self) -> Any:
        return next(reversed(self.manifests.values()), None)

    @property
    def blob_id(self) -> UUID | None:
        manifest = self.manifest
        return self.manifest_blobs.get(manifest.id) if manifest is not None else None

    async def add(self, manifest: Any, manifest_blob_id: UUID) -> None:
        self.manifests[manifest.id] = manifest
        self.manifest_blobs[manifest.id] = manifest_blob_id

    async def get(self, manifest_id: UUID) -> Any:
        return self.manifests.get(manifest_id)

    async def get_blob_id(self, manifest_id: UUID) -> UUID | None:
        return self.manifest_blobs.get(manifest_id)

    async def get_latest_for_edition(self, edition_id: UUID) -> Any:
        return next(
            (
                manifest
                for manifest in reversed(tuple(self.manifests.values()))
                if manifest.edition_id == edition_id
            ),
            None,
        )

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
        self.releases: dict[UUID, Any] = {}
        self.add_calls = 0

    @property
    def release(self) -> Any:
        return next(reversed(self.releases.values()), None)

    async def add_if_absent(self, release: Any) -> bool:
        self.add_calls += 1
        if release.manifest_id in self.releases:
            return False
        self.releases[release.manifest_id] = release
        return True

    async def get_by_manifest(self, manifest_id: UUID) -> Any:
        return self.releases.get(manifest_id)

    async def get(self, release_id: UUID) -> Any:
        return next(
            (release for release in self.releases.values() if release.id == release_id),
            None,
        )

    async def get_for_edition(self, edition_id: UUID) -> Any:
        return next(
            (
                release
                for release in reversed(tuple(self.releases.values()))
                if release.edition_id == edition_id
            ),
            None,
        )


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
    def __init__(self, runs: dict[UUID, ProductionRun]) -> None:
        self.runs = runs

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.runs.get(run_id)

    async def get_for_update(self, run_id: UUID) -> ProductionRun | None:
        return await self.get(run_id)


class _Artifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts
        self.current_calls: list[tuple[UUID, str]] = []

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        # The freeze resolves the current EXTRACTION/REFERENCES to prove that
        # each included document consumed them (LOT 34).  It still selects the
        # document itself by manifest identity, never by "current".
        self.current_calls.append((run_id, stage))
        if stage == ProductionArtifactStage.PUBLICATION.value:
            return next(
                (
                    artifact
                    for artifact in self.artifacts.values()
                    if artifact.production_run_id == run_id
                ),
                None,
            )
        assert stage in {
            ProductionArtifactStage.EXTRACTION.value,
            ProductionArtifactStage.REFERENCES.value,
        }, "frozen assembly must not resolve the current document"
        return None


class _ReadModel:
    def __init__(self, rows: list[EditionReviewReadItem]) -> None:
        self.rows = rows

    async def list_for_edition(self, edition_id: UUID) -> list[EditionReviewReadItem]:
        del edition_id
        return self.rows


class _RepairIssueReader:
    def __init__(self, issue: object) -> None:
        self.issue = issue

    async def list_issue_views(
        self, _edition_id: UUID, _subject_id: UUID | None = None
    ) -> tuple[object, ...]:
        return (self.issue,)

    async def list_supplemental_source_issues(
        self, _edition_id: UUID, _subject_id: UUID | None = None
    ) -> tuple[object, ...]:
        return ()


class _Audit:
    async def append(self, event: Any) -> None:
        del event


class _EditionRenders:
    def __init__(self) -> None:
        self.renders: dict[UUID, EditionRender] = {}

    async def get_latest_for_release(self, release_id: UUID) -> EditionRender | None:
        return max(
            (render for render in self.renders.values() if render.edition_release_id == release_id),
            key=lambda render: (render.created_at, str(render.id)),
            default=None,
        )

    async def get_latest_succeeded_for_release(self, release_id: UUID) -> EditionRender | None:
        return max(
            (
                render
                for render in self.renders.values()
                if render.edition_release_id == release_id
                and render.status is TypstRenderStatus.SUCCEEDED
            ),
            key=lambda render: (render.created_at, str(render.id)),
            default=None,
        )

    async def get(self, render_id: UUID) -> EditionRender | None:
        return self.renders.get(render_id)

    def add(self, render: EditionRender) -> None:
        self.renders[render.id] = render


class _Uow:
    def __init__(
        self,
        edition: Edition,
        rows: list[EditionReviewReadItem],
        blobs: _BlobStore,
    ) -> None:
        blob_a = uuid4()
        blob_b = uuid4()
        blob_c = uuid4()
        blobs.blobs[blob_a] = json.dumps(
            publication_document_v4_to_json(_document("Alpha"))
        ).encode()
        blobs.blobs[blob_b] = json.dumps(
            publication_document_v4_to_json(_document("Bravo"))
        ).encode()
        blobs.blobs[blob_c] = json.dumps(
            publication_document_v4_to_json(_document("Charlie"))
        ).encode()
        self.editions = _Editions(edition)
        self.edition_production_batches = type(
            "Batches", (), {"get_latest_for_edition": self._get_batch}
        )()
        self.batch = EditionProductionBatch(
            edition_id=EDITION_ID,
            status="running",
            phase=ProductionBatchPhase.REVIEW,
        )
        self.production_runs = _Runs(
            {
                RUN_A: _run(RUN_A, SUBJECT_A),
                RUN_B: _run(RUN_B, SUBJECT_B),
                RUN_C: _run(RUN_C, SUBJECT_C),
            }
        )
        self.production_artifacts = _Artifacts(
            {
                ARTIFACT_A: _artifact(ARTIFACT_A, RUN_A, SUBJECT_A, blob_a),
                ARTIFACT_B: _artifact(ARTIFACT_B, RUN_B, SUBJECT_B, blob_b),
                ARTIFACT_C: _artifact(ARTIFACT_C, RUN_C, SUBJECT_C, blob_c),
            }
        )
        self.edition_review_read_model = _ReadModel(rows)
        self.publication_manifests = _ManifestRepo(blobs)
        self.publication_manifest_entries = _Entries()
        self.publication_manifest_exclusions = _Exclusions()
        self.edition_releases = _ReleaseRepo()
        self.edition_renders = _EditionRenders()
        self.edition_audit = _Audit()
        self.job_events = _Audit()
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

    async def add_if_absent(self, job: Job) -> bool:
        if any(existing.idempotency_key == job.idempotency_key for existing in self.jobs.values()):
            return False
        self.jobs[job.id] = job
        return True

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        return next(
            (job for job in self.jobs.values() if job.idempotency_key == idempotency_key),
            None,
        )

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
        job.fail("edition_assembly_failed", "Assembly failed", details={"private": "hidden"})
    elif status == "failed_exhausted":
        job.start()
        job.fail("edition_assembly_failed", "Assembly failed", details={"private": "hidden"})
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


class _PublicationJobContext:
    async def correlation_id(self) -> str:
        return "test-publication-job"

    async def report_progress(self, current: int, total: int, message: str) -> None:
        del current, total, message


class _SuccessfulRenderService:
    def __init__(self, render: EditionRender) -> None:
        self.render = render
        self.release_ids: list[UUID] = []

    async def render_pdf(self, release_id: UUID) -> EditionRender:
        self.release_ids.append(release_id)
        return self.render


class _FailingRematerializer:
    def __init__(self) -> None:
        self.calls: list[dict[str, UUID]] = []

    async def materialize(self, **kwargs: UUID) -> None:
        self.calls.append(kwargs)
        raise OSError("workspace unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_fails", [False, True])
async def test_assembly_handler_persists_release_then_queues_pdf_render(
    dispatch_fails: bool,
) -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    dispatcher = _Dispatcher(fail=dispatch_fails)
    accepted = await EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=uow.jobs,
    ).accept(EDITION_ID, actor_id="analyst")  # type: ignore[arg-type]
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]
    registry = JobRegistry()
    register_publication_jobs(
        registry,
        lambda: uow,
        assembly,
        render_service=SimpleNamespace(),  # type: ignore[arg-type]
        job_dispatcher=dispatcher,
        release_rematerializer=SimpleNamespace(),  # type: ignore[arg-type]
    )  # type: ignore[arg-type]

    result = await registry.handler(EDITION_ASSEMBLE_JOB_KIND)(
        EditionAssembleParameters(manifest_id=accepted.manifest.id),
        _PublicationJobContext(),  # type: ignore[arg-type]
    )
    release = await uow.edition_releases.get_by_manifest(accepted.manifest.id)
    render_jobs = [job for job in uow.jobs.jobs.values() if job.kind == EDITION_RENDER_JOB_KIND]

    assert release is not None
    assert [field.name for field in fields(release)] == [
        "id",
        "edition_id",
        "manifest_id",
        "edition_document_blob_id",
        "edition_document_sha256",
        "created_at",
    ]
    assert result == f"edition-release://{EDITION_ID}/{release.id}"
    assert len(render_jobs) == 1
    assert render_jobs[0].input_parameters == {"edition_release_id": str(release.id)}
    assert render_jobs[0].status.value == "queued"
    assert dispatcher.calls == [render_jobs[0].id]
    assert registry.validate(
        EDITION_RENDER_JOB_KIND,
        {"edition_release_id": str(release.id)},
    ).model_dump(mode="json") == {"edition_release_id": str(release.id)}
    assert registry.resumes_after_worker_loss(EDITION_RENDER_JOB_KIND)


@pytest.mark.asyncio
async def test_render_handler_swallows_best_effort_materialization_failure() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [], blobs)
    release_id = uuid4()
    render = _successful_edition_render(release_id, blobs)
    render_service = _SuccessfulRenderService(render)
    rematerializer = _FailingRematerializer()
    registry = JobRegistry()
    register_publication_jobs(
        registry,
        lambda: uow,
        EditionAssemblyService(lambda: uow, blobs),  # type: ignore[arg-type]
        render_service=render_service,  # type: ignore[arg-type]
        job_dispatcher=_Dispatcher(),
        release_rematerializer=rematerializer,  # type: ignore[arg-type]
    )  # type: ignore[arg-type]

    result = await registry.handler(EDITION_RENDER_JOB_KIND)(
        EditionRenderParameters(edition_release_id=release_id),
        _PublicationJobContext(),  # type: ignore[arg-type]
    )

    assert result == f"edition-render://{release_id}/{render.id}"
    assert render_service.release_ids == [release_id]
    assert rematerializer.calls == [
        {"edition_release_id": release_id, "edition_render_id": render.id}
    ]
    assert render.status is TypstRenderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_accept_freezes_order_exclusion_and_same_manifest_on_retry() -> None:
    rows = [
        EditionReviewReadItem(
            position=1,
            subject_id=SUBJECT_A,
            title="Alpha",
            run_id=RUN_A,
            pipeline_generation=2,
            run_status=ProductionRunStatus.READY,
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
            run_status=ProductionRunStatus.FAILED,
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
    assert first.edition_state is EditionStatus.OPEN
    assert uow.editions.edition.state is EditionStatus.OPEN
    assert uow.editions.edition.version == first.manifest.edition_version
    assert len(jobs.jobs) == 1
    assert len(dispatcher.calls) == 2


@pytest.mark.asyncio
async def test_completed_release_allows_a_new_snapshot_on_the_same_open_edition() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]

    first = await publication.accept(EDITION_ID, actor_id="analyst")
    await assembly.assemble(first.manifest_id)
    assert uow.editions.edition.state is EditionStatus.OPEN
    assert uow.editions.edition.version == first.manifest.edition_version

    # A released snapshot is immutable, so an explicit accept creates a new
    # snapshot even though the Edition metadata version is unchanged.
    replay = await publication.accept(EDITION_ID, actor_id="analyst")
    await assembly.assemble(replay.manifest_id)
    assert replay.manifest_id != first.manifest_id
    assert replay.manifest.edition_version == first.manifest.edition_version
    assert len(uow.publication_manifests.manifests) == 2
    assert len(uow.edition_releases.releases) == 2

    uow.editions.edition.update_metadata(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
    )
    second = await publication.accept(EDITION_ID, actor_id="analyst")
    await assembly.assemble(second.manifest_id)

    assert second.manifest_id != replay.manifest_id
    assert len(uow.publication_manifests.manifests) == 3
    assert len(uow.edition_releases.releases) == 3
    assert uow.editions.edition.state is EditionStatus.OPEN
    assert uow.editions.edition.version == second.manifest.edition_version


@pytest.mark.asyncio
async def test_metadata_change_makes_pending_snapshot_stale_and_creates_current_one() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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

    first = await publication.accept(EDITION_ID, actor_id="analyst")
    uow.editions.edition.update_metadata(
        country="Belgium",
        country_code="BE",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
    )
    second = await publication.accept(EDITION_ID, actor_id="analyst")

    assert second.manifest_id != first.manifest_id
    assert first.manifest.edition_version != uow.editions.edition.version
    assert second.manifest.edition_version == uow.editions.edition.version
    assert uow.editions.edition.state is EditionStatus.OPEN


@pytest.mark.asyncio
async def test_changed_review_input_supersedes_pending_snapshot_without_version_bump() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]

    first = await publication.accept(EDITION_ID, actor_id="analyst")
    uow.edition_review_read_model.rows[0] = replace(
        row,
        subject_id=SUBJECT_B,
        run_id=RUN_B,
        document_artifact_id=ARTIFACT_B,
    )

    with pytest.raises(PublicationAssemblyError, match="publication_inputs_changed_after_freeze"):
        await assembly.assemble(first.manifest_id)
    assert uow.edition_releases.release is None
    assert (await publication.release_status(EDITION_ID)).can_retry_assembly is False

    second = await publication.accept(EDITION_ID, actor_id="analyst")

    assert second.manifest_id != first.manifest_id
    assert second.manifest.edition_version == first.manifest.edition_version
    assert uow.editions.edition.version == first.manifest.edition_version


@pytest.mark.asyncio
async def test_archived_edition_blocks_accept_and_pending_snapshot_retry() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    uow.editions.edition.archive()

    with pytest.raises(PublicationAcceptanceError, match="edition_must_be_open"):
        await publication.accept(EDITION_ID, actor_id="analyst")
    release_status = await publication.release_status(EDITION_ID)

    assert release_status.edition_state is EditionStatus.ARCHIVED
    assert release_status.manifest_id == accepted.manifest_id
    assert release_status.can_retry_assembly is False


@pytest.mark.asyncio
async def test_accept_refuses_an_include_until_its_projection_is_materialized() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )
    decision = SimpleNamespace(id=uuid4(), action=ProductionRepairAction.INCLUDE)
    issue = SimpleNamespace(
        repair_key="1" * 64,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        effective_decision=decision,
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        is_publication_ioc=True,
    )
    blobs = _BlobStore()
    uow = _Uow(_edition(), [row], blobs)
    service = EditionPublicationService(
        lambda: uow,
        blobs,
        repair_issue_reader=_RepairIssueReader(issue),
    )  # type: ignore[arg-type]
    review = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=[issue])

    assert review.items[0].pending_rebuild_count == 1
    assert review.pending_rebuild_count == 1
    assert review.can_accept is False

    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = service
    application.state.identity_provider = LocalIdentityProvider("analyst")
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/editions/{EDITION_ID}/publication/accept")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "review_cannot_be_accepted"
    assert uow.publication_manifests.manifest is None
    assert uow.editions.edition.state is EditionStatus.OPEN


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
async def test_accept_preserves_editorial_positions_with_exclusions(
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
                run_status=(ProductionRunStatus.FAILED if excluded else ProductionRunStatus.READY),
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

    assert edition_json["schema_version"] == "2"


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
async def test_dispatch_failure_keeps_freeze_and_retry_reuses_job() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    assert uow.editions.edition.state is EditionStatus.OPEN
    assert len(jobs.jobs) == 1


@pytest.mark.asyncio
async def test_accept_in_assembling_repairs_failed_job_without_new_manifest() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    job.fail("edition_assembly_failed", "Assembly failed")
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
        run_status=ProductionRunStatus.READY,
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
    assert uow.editions.edition.state is EditionStatus.OPEN


@pytest.mark.asyncio
async def test_empty_review_is_rejected_without_freeze() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [], blobs)
    service = EditionPublicationService(lambda: uow, blobs)  # type: ignore[arg-type]

    with pytest.raises(PublicationAcceptanceError):
        await service.accept(EDITION_ID, actor_id="analyst")
    assert uow.editions.edition.state is EditionStatus.OPEN


@pytest.mark.asyncio
async def test_assembly_reads_manifest_artifact_id_and_persists_canonical_document(
    tmp_path: Path,
) -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    edition_document = await blobs.read_json(release.edition_document_blob_id)

    assert uow.editions.edition.state is EditionStatus.OPEN
    assert edition_document["schema_version"] == "2"
    assert edition_document["publications"][0]["document"]["schema_version"] == "4"
    release_path = tmp_path / "editions/2026-08_FR/release"
    assert not release_path.exists()
    await assembly.assemble(accepted.manifest_id)
    assert uow.edition_releases.add_calls == 1


@pytest.mark.asyncio
async def test_manual_target_two_articles_is_frozen_and_rematerializable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_render(*_args: Any, **_kwargs: Any) -> EditionRender:
        raise AssertionError("rematerialization must not render")

    monkeypatch.setattr(EditionRenderService, "render_pdf", reject_render)
    rows = [
        EditionReviewReadItem(
            position=position,
            subject_id=subject_id,
            title=title,
            run_id=run_id,
            pipeline_generation=2,
            run_status=ProductionRunStatus.READY,
            document_artifact_id=artifact_id,
            document_artifact_version=1,
            document_input_hash="a" * 64,
            document_artifact_status=ProductionArtifactStatus.VERIFIED,
            error_code=None,
            error_message=None,
            effective_decision=None,
        )
        for position, (subject_id, title, run_id, artifact_id) in enumerate(
            (
                (SUBJECT_A, "Article A", RUN_A, ARTIFACT_A),
                (SUBJECT_B, "Article B", RUN_B, ARTIFACT_B),
            ),
            start=1,
        )
    ]
    blobs = _BlobStore()
    uow = _Uow(_edition(), rows, blobs)
    for artifact_id, title in ((ARTIFACT_A, "Article A"), (ARTIFACT_B, "Article B")):
        blob_id = uow.production_artifacts.artifacts[artifact_id].canonical_blob_id
        assert blob_id is not None
        blobs.blobs[blob_id] = json.dumps(
            publication_document_v4_to_json(_document(title))
        ).encode()
    accepted = await EditionPublicationService(lambda: uow, blobs).accept(
        EDITION_ID, actor_id="analyst"
    )  # type: ignore[arg-type]
    assembly = EditionAssemblyService(lambda: uow, blobs)  # type: ignore[arg-type]

    release = await assembly.assemble(accepted.manifest_id)
    assert [entry.position for entry in accepted.manifest.entries] == [1, 2]
    assert [entry.subject_id for entry in accepted.manifest.entries] == [SUBJECT_A, SUBJECT_B]
    edition_payload = await blobs.read_json(release.edition_document_blob_id)
    assert [item["document"]["title"] for item in edition_payload["publications"]] == [
        "Article A",
        "Article B",
    ]
    release_path = tmp_path / "editions/2026-08_FR/release"
    assert not release_path.exists()
    manifest_blob_id = uow.publication_manifests.blob_id
    assert manifest_blob_id is not None
    canonical_bytes = {
        "publication-manifest.json": blobs.blobs[manifest_blob_id],
        "edition.json": blobs.blobs[release.edition_document_blob_id],
    }
    rematerializer = EditionReleaseRematerializationService(
        lambda: uow,
        blobs,
        EditionWorkspaceMaterializer(tmp_path / "editions"),
    )  # type: ignore[arg-type]
    with pytest.raises(
        EditionReleaseMaterializationError,
        match="edition_render_not_available",
    ):
        await rematerializer.materialize(EDITION_ID)
    assert not release_path.exists()

    render = _successful_edition_render(release.id, blobs)
    assert render.output_blob_id is not None
    uow.edition_renders.add(render)
    pdf_bytes = blobs.blobs[render.output_blob_id]
    materialized_path = await rematerializer.materialize(EDITION_ID)
    expected_bytes = {
        **canonical_bytes,
        "bulletin.pdf": blobs.blobs[render.output_blob_id],
    }
    assert materialized_path == release_path
    assert {path.name for path in release_path.iterdir()} == set(expected_bytes)
    assert {name: (release_path / name).read_bytes() for name in expected_bytes} == expected_bytes
    assert not (release_path / "edition.md").exists()
    assert not (release_path / "bulletin.docx").exists()
    manifest_id = accepted.manifest.id
    release_id = release.id
    shutil.rmtree(release_path)
    assert not release_path.exists()
    assert uow.edition_releases.release is release

    blobs.blobs[render.output_blob_id] = b"corrupted pdf"
    with pytest.raises(
        EditionReleaseMaterializationError,
        match="edition_render_output_integrity_mismatch",
    ):
        await rematerializer.materialize(EDITION_ID)
    assert not release_path.exists()
    blobs.blobs[render.output_blob_id] = pdf_bytes

    document_bytes = blobs.blobs[release.edition_document_blob_id]
    blobs.blobs[release.edition_document_blob_id] = document_bytes + b" "
    with pytest.raises(
        EditionReleaseMaterializationError,
        match="edition_document_blob_mismatch",
    ):
        await rematerializer.materialize(EDITION_ID)
    assert not release_path.exists()
    blobs.blobs[release.edition_document_blob_id] = document_bytes

    rematerialized_path = await rematerializer.materialize(
        EDITION_ID,
        edition_render_id=render.id,
    )

    assert rematerialized_path == release_path
    assert {name: (release_path / name).read_bytes() for name in expected_bytes} == expected_bytes
    assert release.id == release_id
    assert uow.edition_releases.add_calls == 1
    assert uow.publication_manifests.manifest.id == manifest_id
    assert {path.name for path in release_path.iterdir()} == set(expected_bytes)


@pytest.mark.asyncio
async def test_assembly_does_not_write_release_workspace(tmp_path: Path) -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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

    assert release.edition_id == EDITION_ID
    assert uow.editions.edition.state is EditionStatus.OPEN
    assert uow.edition_releases.release is release
    assert not (tmp_path / "editions/2026-08_FR/release").exists()


@pytest.mark.asyncio
async def test_assembly_rejects_an_edition_changed_after_freeze() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    assert uow.editions.edition.state is EditionStatus.OPEN


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
        run_status=ProductionRunStatus.READY,
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
    job.fail("edition_assembly_failed", "Public assembly failure", details={"secret": "not public"})

    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = service
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/editions/{accepted.manifest.edition_id}/release")

    body = response.json()
    assert response.status_code == 200
    assert body["edition_state"] == EditionStatus.OPEN.value
    assert "edition_status" not in body
    assert body["assembly_job_id"] == str(job.id)
    assert body["assembly_status"] == "failed"
    assert body["assembly_error_code"] == "edition_assembly_failed"
    assert body["assembly_error_message"] == "Public assembly failure"
    assert body["can_retry_assembly"] is True
    assert body["render_id"] is None
    assert body["render_status"] == "none"
    assert body["render_error_code"] is None
    assert body["render_error_message"] is None
    assert body["can_retry_render"] is False
    assert body["pdf_available"] is False
    assert "error_details" not in body


@pytest.mark.asyncio
async def test_edition_docx_download_route_is_removed() -> None:
    application = FastAPI()
    application.include_router(publication_router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/editions/{EDITION_ID}/release/docx")

    assert response.status_code == 404


def _single_article_row() -> EditionReviewReadItem:
    return EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
        document_artifact_id=ARTIFACT_A,
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )


@pytest.mark.asyncio
async def test_release_pdf_route_returns_404_409_and_verified_pdf() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [_single_article_row()], blobs)
    publication = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=uow.jobs,
    )  # type: ignore[arg-type]
    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = publication
    application.state.identity_provider = LocalIdentityProvider()
    url = f"/api/editions/{EDITION_ID}/release/pdf"
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 404

        accepted = await publication.accept(EDITION_ID, actor_id="analyst")
        assert (await client.get(url)).status_code == 404

        release = await EditionAssemblyService(lambda: uow, blobs).assemble(  # type: ignore[arg-type]
            accepted.manifest.id
        )
        unavailable = await client.get(url)
        assert unavailable.status_code == 409
        assert unavailable.json()["detail"]["code"] == "release_pdf_not_available"

        pdf = b"%PDF-release-route"
        render = _successful_edition_render(release.id, blobs, pdf_bytes=pdf)
        uow.edition_renders.add(render)
        downloaded = await client.get(url)

        assert render.output_blob_id is not None
        blobs.blobs[render.output_blob_id] = b"%PDF-tampered!!!!"
        tampered = await client.get(url)

    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content == pdf
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="bulletin-2026-08-FR.pdf"'
    )
    assert tampered.status_code == 500
    assert tampered.json()["detail"]["code"] == "release_pdf_integrity_mismatch"


@pytest.mark.asyncio
async def test_retry_render_is_idempotent_and_published_at_comes_from_render() -> None:
    row = EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_A,
        title="Alpha",
        run_id=RUN_A,
        pipeline_generation=2,
        run_status=ProductionRunStatus.READY,
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
    uow.jobs = jobs
    dispatcher = _Dispatcher()
    publication = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=jobs,
        job_dispatcher=dispatcher,
    )  # type: ignore[arg-type]
    accepted = await publication.accept(EDITION_ID, actor_id="analyst")
    release = await EditionAssemblyService(lambda: uow, blobs).assemble(  # type: ignore[arg-type]
        accepted.manifest.id
    )
    manifest_ids = set(uow.publication_manifests.manifests)
    release_ids = {item.id for item in uow.edition_releases.releases.values()}

    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = publication
    application.state.identity_provider = LocalIdentityProvider()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.post(f"/api/editions/{EDITION_ID}/release/render")
        second = await client.post(f"/api/editions/{EDITION_ID}/release/render")
        assert first.status_code == second.status_code == 200
        render_jobs = [job for job in jobs.jobs.values() if job.kind == EDITION_RENDER_JOB_KIND]
        assert render_jobs, [(job.kind, job.input_parameters) for job in jobs.jobs.values()]
        observed_status = await publication.release_status(EDITION_ID)
        assert observed_status.release is not None
        assert observed_status.release.id == release.id
        assert observed_status.render_job_status is not None, [
            (job.kind, job.aggregate_type, job.aggregate_id, job.input_parameters)
            for job in jobs.jobs.values()
        ]
        assert first.json()["render_status"] == "queued"
        assert second.json()["render_status"] == "queued"
        assert len(render_jobs) == 1
        assert render_jobs[0].input_parameters["edition_release_id"] == str(release.id)
        assert set(uow.publication_manifests.manifests) == manifest_ids
        assert {item.id for item in uow.edition_releases.releases.values()} == release_ids

        now = datetime.now(UTC)
        render = EditionRender(
            id=uuid4(),
            edition_release_id=release.id,
            renderer="typst",
            renderer_version="edition-v2-typst-v2-semantic-annotation-coverage",
            template_version="chp-edition-v1",
            template_sha256="a" * 64,
            compiler="typst",
            compiler_version="0.15.1",
            font_bundle_version="fonts-v1",
            render_policy_version="typst-edition-v2-v1",
            format=TypstRenderFormat.PDF,
            input_hash="b" * 64,
            source_blob_id=None,
            render_data_blob_id=None,
            output_blob_id=uuid4(),
            output_sha256="c" * 64,
            output_byte_size=12,
            status=TypstRenderStatus.SUCCEEDED,
            error_code=None,
            error_message=None,
            created_at=now - timedelta(minutes=2),
            updated_at=now,
        )
        uow.edition_renders.renders[render.id] = render
        dispatch_count = len(dispatcher.calls)
        published = await client.post(f"/api/editions/{EDITION_ID}/release/render")

    assert published.status_code == 200
    assert published.json()["render_id"] == str(render.id)
    assert published.json()["render_status"] == "succeeded"
    assert published.json()["pdf_available"] is True
    assert published.json()["published_at"] == now.isoformat()
    assert len(dispatcher.calls) == dispatch_count


@pytest.mark.asyncio
async def test_retry_render_returns_404_when_edition_has_no_release() -> None:
    blobs = _BlobStore()
    uow = _Uow(_edition(), [], blobs)
    publication = EditionPublicationService(
        lambda: uow,
        blobs,
        job_service=uow.jobs,
        job_dispatcher=_Dispatcher(),
    )  # type: ignore[arg-type]
    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_publication_service = publication
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/editions/{EDITION_ID}/release/render")

    assert response.status_code == 404
