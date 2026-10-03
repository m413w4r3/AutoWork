from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.subject_content import router
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.publication_rendering import (
    PublicationRenderAttempt,
    PublicationRenderDocumentInvalidError,
    PublicationRenderStorageFailedError,
)
from cti_app.application.subject_content import SubjectContentService
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SourceDocument, Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import PublicationPreviewStatus
from cti_app.domain.typst_render import TypstRenderStatus

SUBJECT_ID = uuid4()


class _Subjects:
    def __init__(self, subject: Subject | None) -> None:
        self.subject = subject

    async def get(self, subject_id: UUID) -> Subject | None:
        return self.subject if self.subject and self.subject.id == subject_id else None


class _Runs:
    def __init__(self, runs: list[ProductionRun]) -> None:
        self.runs = runs

    async def get_current_for_subject(self, subject_id: UUID) -> ProductionRun | None:
        matches = [run for run in self.runs if run.subject_id == subject_id]
        return max(matches, key=lambda run: run.created_at) if matches else None


class _Artifacts:
    def __init__(self, artifacts: list[ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            artifact
            for artifact in self.artifacts
            if artifact.production_run_id == run_id
            and artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda artifact: artifact.version) if matches else None

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.artifacts if item.id == artifact_id), None)


class _PublicationRenders:
    def __init__(self) -> None:
        self.renders: dict[UUID, object] = {}

    async def get_latest_for_artifact(self, artifact_id: UUID) -> object | None:
        matches = [
            render
            for render in self.renders.values()
            if getattr(render, "publication_artifact_id", None) == artifact_id
        ]
        return matches[-1] if matches else None

    async def get_by_input_hash(self, input_hash: str) -> object | None:
        return next(
            (
                render
                for render in self.renders.values()
                if getattr(render, "input_hash", None) == input_hash
            ),
            None,
        )


class _PublicationManifests:
    def __init__(self) -> None:
        self.manifest: object | None = None

    async def get_latest_for_edition(self, edition_id: UUID) -> object | None:
        del edition_id
        return self.manifest


class _EditionReleases:
    def __init__(self) -> None:
        self.release: object | None = None

    async def get_by_manifest(self, manifest_id: UUID) -> object | None:
        del manifest_id
        return self.release


class _Sources:
    def __init__(self, values: list[SourceDocument]) -> None:
        self.values = values

    async def list_for_subject(self, subject_id: UUID) -> list[SourceDocument]:
        return [value for value in self.values if value.subject_id == subject_id]


class _Samples:
    def __init__(self, values: list[Sample]) -> None:
        self.values = values

    async def list_for_subject(self, subject_id: UUID) -> list[Sample]:
        return [value for value in self.values if value.subject_id == subject_id]


class _Uow:
    def __init__(
        self,
        subject: Subject | None = None,
        runs: list[ProductionRun] | None = None,
        artifacts: list[ProductionArtifact] | None = None,
        sources: list[SourceDocument] | None = None,
        samples: list[Sample] | None = None,
    ) -> None:
        self.subjects = _Subjects(subject)
        self.production_runs = _Runs(runs or [])
        self.production_artifacts = _Artifacts(artifacts or [])
        self.source_documents = _Sources(sources or [])
        self.samples = _Samples(samples or [])
        self.publication_renders = _PublicationRenders()
        self.publication_manifests = _PublicationManifests()
        self.edition_releases = _EditionReleases()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Payloads:
    def __init__(self, values: dict[UUID, object]) -> None:
        self.values = values
        self.reads = 0

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        self.reads += 1
        value = self.values[blob_id]
        assert isinstance(value, dict)
        return value

    async def read_text(self, blob_id: UUID) -> str:
        self.reads += 1
        value = self.values[blob_id]
        assert isinstance(value, str)
        return value

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        value = self.values[blob_id]
        assert isinstance(value, bytes)
        assert len(value) <= max_bytes
        return value


class _ExplodingPayloads(_Payloads):
    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        raise AssertionError(f"assets must not read blob {blob_id}")

    async def read_text(self, blob_id: UUID) -> str:
        raise AssertionError(f"assets must not read blob {blob_id}")


class _PublicationRenderService:
    def __init__(
        self,
        *,
        result: object | None = None,
        error: Exception | None = None,
        preview_attempt: object | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.preview_attempt = preview_attempt
        self.artifact_ids: list[UUID] = []
        self.preview_compile_count = 0
        self._cached_preview_attempt: object | None = None

    async def render_pdf(self, artifact_id: UUID) -> object:
        self.artifact_ids.append(artifact_id)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def render_preview(self, artifact_id: UUID) -> object:
        self.artifact_ids.append(artifact_id)
        if self.error is not None:
            raise self.error
        if self._cached_preview_attempt is not None:
            return self._cached_preview_attempt
        assert self.preview_attempt is not None
        attempt_status = getattr(self.preview_attempt, "status", None)
        if attempt_status is TypstRenderStatus.SUCCEEDED:
            self.preview_compile_count += 1
            self._cached_preview_attempt = self.preview_attempt
        return self.preview_attempt


def _app(
    uow: _Uow,
    payloads: _Payloads,
    publication_render_service: _PublicationRenderService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.subject_content_service = SubjectContentService(
        cast(UnitOfWorkFactory, lambda: uow), payloads
    )
    app.state.uow_factory = lambda: uow
    app.state.production_artifact_store = payloads
    if publication_render_service is not None:
        app.state.publication_render_service = publication_render_service
    return app


@pytest.fixture
def subject() -> Subject:
    return Subject(
        edition_id=uuid4(),
        title="Test subject",
        slug="subject-one",
        tlp=TLP.AMBER,
        id=SUBJECT_ID,
    )


def _run(*, created_at: datetime | None = None, generation: int = 1) -> ProductionRun:
    return ProductionRun(
        id=uuid4(),
        subject_id=SUBJECT_ID,
        edition_id=uuid4(),
        pipeline_generation=generation,
        created_at=created_at or datetime.now(UTC),
    )


def _artifact(
    run: ProductionRun,
    stage: ProductionArtifactStage,
    canonical_blob_id: UUID,
    *,
    version: int = 1,
) -> ProductionArtifact:
    return ProductionArtifact(
        id=uuid4(),
        production_run_id=run.id,
        subject_id=SUBJECT_ID,
        stage=stage,
        version=version,
        input_hash="a" * 64,
        raw_blob_id=uuid4(),
        canonical_blob_id=canonical_blob_id,
    )


def _document(title: str) -> dict[str, Any]:
    return publication_document_v4_to_json(
        PublicationDocumentV4(
            schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
            subject_id=SUBJECT_ID,
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
    )


def _extraction(*items: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "2", "items": list(items), "uncertainties": []}


def _ioc(
    item_id: str,
    value: str,
    *,
    policy: str = "ioc_section",
    status: str = "confirmed_ioc",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "category": "artifacts",
        "value": value,
        "context": "test context",
        "artifact_type": "domain",
        "indicator_status": status,
        "display_policy": policy,
        "source_ids": ["source-1"],
        "reference_ids": [],
        "supported": True,
    }


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_content_without_production_is_stable_404(subject: Subject) -> None:
    app = _app(_Uow(subject), _Payloads({}))
    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/content")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "subject_content_not_found"


@pytest.mark.anyio
async def test_content_returns_current_artifact_without_raw_blob(subject: Subject) -> None:
    run = _run(generation=3)
    canonical_id = uuid4()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, canonical_id)
    payloads = _Payloads({canonical_id: _document("Current title")})
    app = _app(_Uow(subject, [run], [artifact]), payloads)

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/content")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subject_id"] == str(SUBJECT_ID)
    assert body["run_id"] == str(run.id)
    assert body["pipeline_generation"] == 3
    assert body["artifact_id"] == str(artifact.id)
    assert body["canonical_content"]["title"] == "Current title"
    assert "rendered_content" not in body
    assert "raw_blob" not in body


@pytest.mark.anyio
async def test_subject_publication_pdf_uses_current_verified_artifact(
    subject: Subject,
) -> None:
    previous_run = _run(created_at=datetime.now(UTC), generation=1)
    current_run = _run(created_at=datetime.now(UTC) + timedelta(seconds=1), generation=2)
    previous_artifact = _artifact(
        previous_run,
        ProductionArtifactStage.PUBLICATION,
        uuid4(),
    )
    current_artifact = _artifact(
        current_run,
        ProductionArtifactStage.PUBLICATION,
        uuid4(),
        version=2,
    )
    output_blob_id = uuid4()
    pdf_bytes = b"%PDF-1.7\npublication"
    render = SimpleNamespace(
        input_hash="e" * 64,
        output_blob_id=output_blob_id,
        output_byte_size=len(pdf_bytes),
        output_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    renderer = _PublicationRenderService(result=render)
    payloads = _Payloads({output_blob_id: pdf_bytes})
    app = _app(
        _Uow(subject, [previous_run, current_run], [previous_artifact, current_artifact]),
        payloads,
        renderer,
    )

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="publication-{SUBJECT_ID}.pdf"'
    )
    assert response.content == pdf_bytes
    assert response.headers["x-publication-render-identity"] == render.input_hash
    assert renderer.artifact_ids == [current_artifact.id]


@pytest.mark.anyio
async def test_publication_preview_reuses_the_publication_render_for_an_accepted_version(
    subject: Subject,
) -> None:
    run = _run(generation=4)
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, uuid4(), version=3)
    output_blob_id = uuid4()
    pdf_bytes = b"%PDF-1.7\naccepted article"
    render = SimpleNamespace(
        id=uuid4(),
        publication_artifact_id=artifact.id,
        input_hash="f" * 64,
        output_blob_id=output_blob_id,
        output_byte_size=len(pdf_bytes),
        output_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        status=TypstRenderStatus.SUCCEEDED,
    )
    attempt = PublicationRenderAttempt(
        status=TypstRenderStatus.SUCCEEDED,
        render=render,
    )
    renderer = _PublicationRenderService(result=render, preview_attempt=attempt)
    uow = _Uow(subject, [run], [artifact])
    manifest_id = uuid4()
    uow.publication_manifests.manifest = SimpleNamespace(
        id=manifest_id,
        edition_version=7,
        entries=[
            SimpleNamespace(
                subject_id=artifact.subject_id,
                production_run_id=artifact.production_run_id,
                pipeline_generation=run.pipeline_generation,
                document_artifact_id=artifact.id,
                document_artifact_version=artifact.version,
                document_input_hash=artifact.input_hash,
            )
        ],
    )
    uow.edition_releases.release = object()
    payloads = _Payloads({output_blob_id: pdf_bytes})
    app = _app(uow, payloads, renderer)

    async with await _client(app) as api:
        metadata = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview",
            params={"artifact_id": str(artifact.id)},
        )
        metadata_again = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview",
            params={"artifact_id": str(artifact.id)},
        )
        preview_pdf = await api.get(metadata.json()["pdf_url"])
        published_pdf = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["status"] == PublicationPreviewStatus.READY.value
    assert metadata.json()["artifact_id"] == str(artifact.id)
    assert metadata.json()["artifact_version"] == artifact.version
    assert metadata.json()["render_identity"] == render.input_hash
    assert metadata.json()["render_disposition"] == "ACCEPTED_VERSION"
    assert metadata.json()["published_edition_version"] == 7
    assert metadata_again.json()["render_identity"] == render.input_hash
    assert renderer.preview_compile_count == 1
    assert preview_pdf.status_code == 200, preview_pdf.text
    assert preview_pdf.content == pdf_bytes
    assert preview_pdf.headers["x-publication-render-identity"] == render.input_hash
    assert published_pdf.status_code == 200, published_pdf.text
    assert published_pdf.headers["x-publication-render-identity"] == render.input_hash


@pytest.mark.anyio
async def test_publication_preview_reports_in_progress_and_failed_causes(
    subject: Subject,
) -> None:
    run = _run()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, uuid4())
    running = SimpleNamespace(
        id=uuid4(),
        publication_artifact_id=artifact.id,
        input_hash="a" * 64,
        status=TypstRenderStatus.RUNNING,
    )
    failed = SimpleNamespace(
        id=uuid4(),
        publication_artifact_id=artifact.id,
        input_hash="b" * 64,
        status=TypstRenderStatus.FAILED,
        error_code="typst_compile_failed",
        error_message="Typst found an invalid layout.",
    )
    renderer = _PublicationRenderService(
        preview_attempt=PublicationRenderAttempt(
            status=TypstRenderStatus.RUNNING,
            render=running,
            error_code="publication_render_in_progress",
            error_message="The same render is still running.",
        )
    )
    app = _app(_Uow(subject, [run], [artifact]), _Payloads({}), renderer)

    async with await _client(app) as api:
        in_progress = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview",
            params={"artifact_id": str(artifact.id)},
        )
        renderer.preview_attempt = PublicationRenderAttempt(
            status=TypstRenderStatus.FAILED,
            render=failed,
            error_code="typst_compile_failed",
            error_message="Typst found an invalid layout.",
        )
        failed_response = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview",
            params={"artifact_id": str(artifact.id)},
        )

    assert in_progress.json()["status"] == PublicationPreviewStatus.IN_PROGRESS.value
    assert in_progress.json()["render_identity"] == running.input_hash
    assert failed_response.json()["status"] == PublicationPreviewStatus.FAILED.value
    assert failed_response.json()["error_code"] == "typst_compile_failed"
    assert failed_response.json()["error_message"] == "Typst found an invalid layout."


@pytest.mark.anyio
async def test_new_publication_artifact_marks_an_older_pdf_stale(subject: Subject) -> None:
    old_run = _run(created_at=datetime.now(UTC), generation=1)
    current_run = _run(created_at=datetime.now(UTC) + timedelta(seconds=1), generation=2)
    old_artifact = _artifact(old_run, ProductionArtifactStage.PUBLICATION, uuid4())
    current_artifact = _artifact(
        current_run,
        ProductionArtifactStage.PUBLICATION,
        uuid4(),
        version=2,
    )
    old_render = SimpleNamespace(
        id=uuid4(),
        publication_artifact_id=old_artifact.id,
        input_hash="c" * 64,
        status=TypstRenderStatus.SUCCEEDED,
    )
    uow = _Uow(subject, [old_run, current_run], [old_artifact, current_artifact])
    uow.publication_renders.renders[old_render.id] = old_render
    renderer = _PublicationRenderService()
    app = _app(uow, _Payloads({}), renderer)

    async with await _client(app) as api:
        response = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview",
            params={"artifact_id": str(old_artifact.id)},
        )
        old_pdf = await api.get(
            f"/api/subjects/{SUBJECT_ID}/publication/preview/{old_artifact.id}/pdf",
            params={"render_identity": old_render.input_hash},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == PublicationPreviewStatus.STALE.value
    assert response.json()["artifact_id"] == str(old_artifact.id)
    assert response.json()["artifact_version"] == old_artifact.version
    assert response.json()["render_identity"] == old_render.input_hash
    assert response.json()["current_artifact_id"] == str(current_artifact.id)
    assert response.json()["pdf_url"] is None
    assert old_pdf.status_code == 409
    assert old_pdf.json()["detail"]["code"] == "publication_preview_stale"
    assert renderer.artifact_ids == []


@pytest.mark.anyio
async def test_subject_publication_pdf_checks_output_integrity(subject: Subject) -> None:
    run = _run()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, uuid4())
    output_blob_id = uuid4()
    pdf_bytes = b"%PDF-1.7\npublication"
    render = SimpleNamespace(
        output_blob_id=output_blob_id,
        output_byte_size=len(pdf_bytes),
        output_sha256="0" * 64,
    )
    app = _app(
        _Uow(subject, [run], [artifact]),
        _Payloads({output_blob_id: pdf_bytes}),
        _PublicationRenderService(result=render),
    )

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "publication_pdf_integrity_mismatch"


@pytest.mark.anyio
async def test_unverified_publication_keeps_its_preview_but_has_no_pdf(subject: Subject) -> None:
    run = _run()
    canonical_id = uuid4()
    artifact = replace(
        _artifact(run, ProductionArtifactStage.PUBLICATION, canonical_id),
        status=ProductionArtifactStatus.NEEDS_REVIEW,
    )
    renderer = _PublicationRenderService()
    app = _app(
        _Uow(subject, [run], [artifact]),
        _Payloads({canonical_id: _document("Needs review")}),
        renderer,
    )

    async with await _client(app) as api:
        content = await api.get(f"/api/subjects/{SUBJECT_ID}/content")
        pdf = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert content.status_code == 200
    assert content.json()["status"] == "needs_review"
    assert pdf.status_code == 409
    assert pdf.json()["detail"]["code"] == "publication_not_verified"
    assert renderer.artifact_ids == []


@pytest.mark.anyio
async def test_subject_publication_pdf_is_not_available_without_publication(
    subject: Subject,
) -> None:
    app = _app(_Uow(subject, [_run()]), _Payloads({}), _PublicationRenderService())

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "publication_not_available"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            PublicationRenderDocumentInvalidError("invalid document"),
            422,
            "publication_render_document_invalid",
        ),
        (
            PublicationRenderStorageFailedError("storage unavailable"),
            503,
            "publication_render_storage_failed",
        ),
    ],
)
async def test_subject_publication_pdf_maps_render_errors(
    subject: Subject,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    run = _run()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, uuid4())
    renderer = _PublicationRenderService(error=error)
    app = _app(_Uow(subject, [run], [artifact]), _Payloads({}), renderer)

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/publication/pdf")

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert renderer.artifact_ids == [artifact.id]


@pytest.mark.anyio
async def test_content_uses_new_current_generation(subject: Subject) -> None:
    first = _run(created_at=datetime.now(UTC), generation=1)
    second = _run(created_at=datetime.now(UTC) + timedelta(seconds=1), generation=2)
    first_blob, second_blob = uuid4(), uuid4()
    artifacts = [
        _artifact(first, ProductionArtifactStage.PUBLICATION, first_blob),
        _artifact(second, ProductionArtifactStage.PUBLICATION, second_blob, version=2),
    ]
    payloads = _Payloads({first_blob: _document("Old"), second_blob: _document("New")})
    app = _app(_Uow(subject, [first, second], artifacts), payloads)

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/content")

    assert response.status_code == 200
    assert response.json()["pipeline_generation"] == 2
    assert response.json()["canonical_content"]["title"] == "New"


@pytest.mark.anyio
async def test_indicators_are_derived_from_current_extraction(subject: Subject) -> None:
    run = _run()
    extraction_blob = uuid4()
    artifact = _artifact(run, ProductionArtifactStage.EXTRACTION, extraction_blob)
    payloads = _Payloads(
        {
            extraction_blob: _extraction(
                _ioc("ioc-1", "Example.COM"),
                _ioc("ioc-2", "other.example", policy="both"),
                _ioc("contextual", "context.example", status="contextual"),
                _ioc("excluded", "excluded.example", status="excluded"),
                _ioc("hidden", "hidden.example", policy="hidden"),
            )
        }
    )
    app = _app(_Uow(subject, [run], [artifact]), payloads)

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/indicators")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": "ioc-1",
            "artifact_type": "domain",
            "display_value": "example[.]com",
            "normalized_value": "example.com",
            "indicator_status": "confirmed_ioc",
            "source_ids": ["source-1"],
        },
        {
            "id": "ioc-2",
            "artifact_type": "domain",
            "display_value": "other[.]example",
            "normalized_value": "other.example",
            "indicator_status": "confirmed_ioc",
            "source_ids": ["source-1"],
        },
    ]


@pytest.mark.anyio
async def test_indicators_without_iocs_are_empty(subject: Subject) -> None:
    run = _run()
    blob = uuid4()
    app = _app(
        _Uow(subject, [run], [_artifact(run, ProductionArtifactStage.EXTRACTION, blob)]),
        _Payloads({blob: _extraction()}),
    )
    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/indicators")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_assets_separate_sources_and_samples_without_blob_reads(subject: Subject) -> None:
    acquired = datetime.now(UTC)
    source = SourceDocument(
        id=uuid4(),
        subject_id=SUBJECT_ID,
        blob_id=uuid4(),
        original_name="report.pdf",
        origin="feed",
        acquired_at=acquired,
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=True,
        external_llm_allowed=False,
        declared_mime_type="application/pdf",
        encoded_sha256="b" * 64,
        encoded_size=42,
    )
    sample = Sample(
        id=uuid4(),
        subject_id=SUBJECT_ID,
        blob_id=uuid4(),
        original_name="sample.bin",
        origin="feed",
        acquired_at=acquired,
        license_restriction=None,
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
        expected_hash="c" * 64,
    )
    payloads = _ExplodingPayloads({})
    app = _app(_Uow(subject, sources=[source], samples=[sample]), payloads)

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/assets")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["original_name"] for item in body["sources"]] == ["report.pdf"]
    assert [item["original_name"] for item in body["samples"]] == ["sample.bin"]
    assert body["sources"][0]["sha256"] == "b" * 64
    assert body["sources"][0]["size"] == 42
    assert body["sources"][0]["mime_type"] == "application/pdf"
    assert body["samples"][0]["sha256"] == "c" * 64
    assert "blob_id" not in body["sources"][0]
    assert "url" not in body["samples"][0]
