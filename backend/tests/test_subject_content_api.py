from __future__ import annotations

import hashlib
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


class _PublicationArtifactWithoutRenderedBlob:
    def __init__(self, artifact: ProductionArtifact) -> None:
        self._artifact = artifact

    def __getattr__(self, name: str) -> Any:
        if name == "rendered_blob_id":
            raise AssertionError("PUBLICATION must not read rendered_blob_id")
        return getattr(self._artifact, name)


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
    def __init__(self, *, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.artifact_ids: list[UUID] = []

    async def render_pdf(self, artifact_id: UUID) -> object:
        self.artifact_ids.append(artifact_id)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


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
    rendered_blob_id: UUID | None = None,
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
        rendered_blob_id=rendered_blob_id,
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
    rendered_id = uuid4()
    artifact = _PublicationArtifactWithoutRenderedBlob(
        _artifact(
            run,
            ProductionArtifactStage.PUBLICATION,
            canonical_id,
            rendered_blob_id=rendered_id,
        )
    )
    payloads = _Payloads({canonical_id: _document("Current title"), rendered_id: "# Current title"})
    app = _app(
        _Uow(subject, [run], [cast(ProductionArtifact, artifact)]),
        payloads,
    )

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
    assert renderer.artifact_ids == [current_artifact.id]


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
