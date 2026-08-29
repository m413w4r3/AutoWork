from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.subject_content import router
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.subject_content import SubjectContentService
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SourceDocument, Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)

SUBJECT_ID = uuid4()


class _Subjects:
    def __init__(self, subject: Subject | None) -> None:
        self.subject = subject

    async def get(self, subject_id: UUID) -> Subject | None:
        return self.subject if self.subject and self.subject.id == subject_id else None


class _Runs:
    def __init__(self, runs: list[SubjectProductionRun]) -> None:
        self.runs = runs

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
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
        runs: list[SubjectProductionRun] | None = None,
        artifacts: list[ProductionArtifact] | None = None,
        sources: list[SourceDocument] | None = None,
        samples: list[Sample] | None = None,
    ) -> None:
        self.subjects = _Subjects(subject)
        self.subject_production_runs = _Runs(runs or [])
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


class _ExplodingPayloads(_Payloads):
    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        raise AssertionError(f"assets must not read blob {blob_id}")

    async def read_text(self, blob_id: UUID) -> str:
        raise AssertionError(f"assets must not read blob {blob_id}")


def _app(uow: _Uow, payloads: _Payloads) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.subject_content_service = SubjectContentService(
        cast(UnitOfWorkFactory, lambda: uow), payloads
    )
    return app


@pytest.fixture
def subject() -> Subject:
    return Subject(external_id="subject-external", slug="subject-one", tlp=TLP.AMBER, id=SUBJECT_ID)


def _run(*, created_at: datetime | None = None, generation: int = 1) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=uuid4(),
        subject_id=SUBJECT_ID,
        edition_id=uuid4(),
        pipeline_generation=generation,
        created_at=created_at or datetime.now(UTC),
    )


def _artifact(
    run: SubjectProductionRun,
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
    return {
        "schema_version": "1",
        "title": title,
        "timeline": [],
        "synthesis": [],
        "indicators": [],
        "sources": [],
        "uncertainties": [],
    }


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
    artifact = _artifact(
        run,
        ProductionArtifactStage.PUBLICATION,
        canonical_id,
        rendered_blob_id=rendered_id,
    )
    payloads = _Payloads({canonical_id: _document("Current title"), rendered_id: "# Current title"})
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
    assert body["rendered_content"] == "# Current title"
    assert "raw_blob" not in body


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
