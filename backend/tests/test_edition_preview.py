from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router as publication_router
from cti_app.application import edition_preview, typst_render_execution
from cti_app.application.edition_preview import (
    EditionPreviewService,
    EditionPreviewStaleError,
)
from cti_app.application.edition_review import EditionReviewReadItem
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileRequest,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import EditionDocumentV2
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_review import PublicationDecision

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_IDS = (
    UUID("22222222-2222-4222-8222-222222222222"),
    UUID("33333333-3333-4333-8333-333333333333"),
)
RUN_IDS = (
    UUID("44444444-4444-4444-8444-444444444444"),
    UUID("55555555-5555-4555-8555-555555555555"),
)
ARTIFACT_IDS = (
    UUID("66666666-6666-4666-8666-666666666666"),
    UUID("77777777-7777-4777-8777-777777777777"),
)
TEMPLATE_VERSION = "edition-preview-test-v1"
PREVIEW_HASH_A = "a" * 64


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


def _document(subject_id: UUID, title: str) -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
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


class _ArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, dict[str, Any]] = {}

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.blobs[blob_id]


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None


class _ReadModel:
    def __init__(self, rows: list[EditionReviewReadItem]) -> None:
        self.rows = rows

    async def list_for_edition(self, edition_id: UUID) -> list[EditionReviewReadItem]:
        del edition_id
        return self.rows


class _Runs:
    def __init__(self, runs: dict[UUID, ProductionRun]) -> None:
        self.runs = runs

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.runs.get(run_id)


class _Artifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        assert stage == ProductionArtifactStage.PUBLICATION.value
        return next(
            (
                artifact
                for artifact in self.artifacts.values()
                if artifact.production_run_id == run_id
            ),
            None,
        )


class _Uow:
    def __init__(
        self,
        edition: Edition,
        rows: list[EditionReviewReadItem],
        artifact_store: _ArtifactStore,
    ) -> None:
        self.editions = _Editions(edition)
        self.edition_review_read_model = _ReadModel(rows)
        runs: dict[UUID, ProductionRun] = {}
        artifacts: dict[UUID, ProductionArtifact] = {}
        for position, (subject_id, run_id, artifact_id, title) in enumerate(
            zip(SUBJECT_IDS, RUN_IDS, ARTIFACT_IDS, ("Alpha", "Bravo"), strict=True),
            start=1,
        ):
            run = ProductionRun(
                id=run_id,
                subject_id=subject_id,
                edition_id=EDITION_ID,
                status=ProductionRunStatus.READY,
                current_stage=ProductionStage.ASSEMBLY,
                pipeline_generation=2,
            )
            blob_id = uuid4()
            artifact_store.blobs[blob_id] = publication_document_v4_to_json(
                _document(subject_id, title)
            )
            artifact = ProductionArtifact(
                id=artifact_id,
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=1,
                input_hash=PREVIEW_HASH_A,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
            runs[run_id] = run
            artifacts[artifact_id] = artifact
            assert rows[position - 1].position == position
        self.production_runs = _Runs(runs)
        self.production_artifacts = _Artifacts(artifacts)
        self.publication_manifests: list[object] = []
        self.edition_releases: list[object] = []
        self.edition_renders: list[object] = []

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


def _review_rows() -> list[EditionReviewReadItem]:
    return [
        EditionReviewReadItem(
            position=position,
            subject_id=subject_id,
            title=title,
            run_id=run_id,
            pipeline_generation=2,
            run_status=ProductionRunStatus.READY,
            document_artifact_id=artifact_id,
            document_artifact_version=1,
            document_input_hash=PREVIEW_HASH_A,
            document_artifact_status=ProductionArtifactStatus.VERIFIED,
            error_code=None,
            error_message=None,
            effective_decision=PublicationDecision.INCLUDE,
        )
        for position, (subject_id, run_id, artifact_id, title) in enumerate(
            zip(SUBJECT_IDS, RUN_IDS, ARTIFACT_IDS, ("Alpha", "Bravo"), strict=True),
            start=1,
        )
    ]


def _service(
    *,
    pdf_renderer: edition_preview.EditionPreviewPdfRenderer | None = None,
) -> tuple[EditionPreviewService, _Uow, _ArtifactStore]:
    artifacts = _ArtifactStore()
    uow = _Uow(_edition(), _review_rows(), artifacts)
    service = EditionPreviewService(
        lambda: uow,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
        pdf_renderer=pdf_renderer,
    )
    return service, uow, artifacts


def test_preview_contract_is_document_only() -> None:
    assert tuple(edition_preview.EditionPreview.__dataclass_fields__) == (
        "edition_id",
        "edition_version",
        "preview_input_hash",
        "artifacts",
        "document",
        "stale",
    )


@pytest.mark.asyncio
async def test_preview_returns_edition_document_and_hash_is_stable() -> None:
    service, uow, _ = _service()

    first = await service.preview(EDITION_ID)
    second = await service.preview(EDITION_ID)

    assert isinstance(first.document, EditionDocumentV2)
    assert first.preview_input_hash == second.preview_input_hash
    assert first.stale is False
    assert [item.position for item in first.document.publications] == [1, 2]
    assert not hasattr(first, "canonical_markdown")
    assert not hasattr(first, "sanitized_html")
    assert uow.publication_manifests == []
    assert uow.edition_releases == []
    assert uow.edition_renders == []


@pytest.mark.asyncio
async def test_preview_hash_changes_and_stale_is_reported_after_article_changes() -> None:
    service, uow, artifacts = _service()
    previous = await service.preview(EDITION_ID)
    old_row = uow.edition_review_read_model.rows[0]
    old_artifact = uow.production_artifacts.artifacts[ARTIFACT_IDS[0]]
    new_artifact_id = uuid4()
    new_blob_id = uuid4()
    artifacts.blobs[new_blob_id] = publication_document_v4_to_json(
        _document(SUBJECT_IDS[0], "Replacement article")
    )
    new_artifact = replace(
        old_artifact,
        id=new_artifact_id,
        input_hash="b" * 64,
        canonical_blob_id=new_blob_id,
    )
    uow.production_artifacts.artifacts[new_artifact_id] = new_artifact
    del uow.production_artifacts.artifacts[ARTIFACT_IDS[0]]
    uow.edition_review_read_model.rows[0] = replace(
        old_row,
        document_artifact_id=new_artifact_id,
        document_input_hash="b" * 64,
    )

    current = await service.preview(
        EDITION_ID,
        previous_preview_input_hash=previous.preview_input_hash,
    )

    assert current.preview_input_hash != previous.preview_input_hash
    assert current.stale is True
    assert current.document.publications[0].document.title == "Replacement article"


class _MediaAssetStore:
    async def get(self, asset_id: UUID) -> None:
        del asset_id
        return None

    async def read(self, asset_id: UUID) -> bytes:
        del asset_id
        raise AssertionError("fixture documents do not reference media")


class _FakeCompiler:
    def __init__(self) -> None:
        self.requests: list[TypstCompileRequest] = []
        self.workspace_files: list[dict[str, bytes]] = []
        self.pdf = b"%PDF-1.7 edition-preview"

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.requests.append(request)
        self.workspace_files.append(
            {
                path.relative_to(request.workspace_root).as_posix(): path.read_bytes()
                for path in request.workspace_root.rglob("*")
                if path.is_file()
            }
        )
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.pdf,
            sha256=hashlib.sha256(self.pdf).hexdigest(),
            byte_size=len(self.pdf),
            compiler="typst",
            compiler_version="0.15.1",
        )


def _configured_preview_service(
    tmp_path: Path,
) -> tuple[EditionPreviewService, _Uow, _FakeCompiler]:
    template_root = tmp_path / "chpTypst"
    entrypoint = template_root / "RENDERER" / "edition.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"// edition template entrypoint")
    (template_root / "edition-renderer-manifest.json").write_text(
        json.dumps(
            {
                "template_version": TEMPLATE_VERSION,
                "files": ["RENDERER/edition.typ"],
            }
        ),
        encoding="utf-8",
    )
    font_root = tmp_path / "fonts"
    font_root.mkdir()
    (font_root / "preview.ttf").write_bytes(b"test-font")
    font_lock = tmp_path / "typst-fonts.lock"
    font_lock.write_text(
        json.dumps({"font_bundle_label": "edition-preview-fonts-v1", "files": ["preview.ttf"]}),
        encoding="utf-8",
    )
    compiler = _FakeCompiler()
    renderer = edition_preview.EditionPreviewPdfRenderer(
        renderer=EditionTypstRenderer(),
        media_asset_store=cast(MediaAssetStore, _MediaAssetStore()),
        compiler=compiler,  # type: ignore[arg-type]
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )
    service, uow, _ = _service(pdf_renderer=renderer)
    return service, uow, compiler


@pytest.mark.asyncio
async def test_preview_pdf_uses_edition_typst_template_and_creates_no_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_versions: list[str] = []
    original_loader = load_template_bundle

    def capture_template(*args: Any, **kwargs: Any) -> Any:
        bundle = original_loader(*args, **kwargs)
        template_versions.append(bundle.template_version)
        assert kwargs["manifest_name"] == "edition-renderer-manifest.json"
        return bundle

    monkeypatch.setattr(typst_render_execution, "load_template_bundle", capture_template)
    service, uow, compiler = _configured_preview_service(tmp_path)
    preview = await service.preview(EDITION_ID)

    result = await service.preview_pdf(
        EDITION_ID,
        expected_preview_input_hash=preview.preview_input_hash,
    )

    assert result.content == compiler.pdf
    assert result.content.startswith(b"%PDF-")
    assert result.filename == "bulletin-preview-2026-08-FR.pdf"
    assert template_versions == [TEMPLATE_VERSION]
    assert len(compiler.requests) == 1
    assert compiler.requests[0].entrypoint_relative_path == "RENDERER/edition.typ"
    assert compiler.workspace_files[0]["RENDERER/edition.typ"] == b"// edition template entrypoint"
    assert uow.publication_manifests == []
    assert uow.edition_releases == []
    assert uow.edition_renders == []


@pytest.mark.asyncio
async def test_wrong_expected_preview_hash_is_stale(tmp_path: Path) -> None:
    service, _, _ = _configured_preview_service(tmp_path)
    preview = await service.preview(EDITION_ID)

    with pytest.raises(EditionPreviewStaleError, match="edition_preview_stale"):
        await service.preview_pdf(
            EDITION_ID,
            expected_preview_input_hash="b" * 64,
        )
    assert preview.preview_input_hash != "b" * 64


@pytest.mark.asyncio
async def test_preview_api_serializes_document_and_pdf_route_fences_staleness(
    tmp_path: Path,
) -> None:
    service, uow, compiler = _configured_preview_service(tmp_path)
    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_preview_service = service
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        preview_response = await client.get(f"/api/editions/{EDITION_ID}/preview")
        preview_hash = preview_response.json()["preview_input_hash"]
        pdf_response = await client.get(
            f"/api/editions/{EDITION_ID}/preview/pdf",
            params={"preview_input_hash": preview_hash},
        )
        stale_response = await client.get(
            f"/api/editions/{EDITION_ID}/preview/pdf",
            params={"preview_input_hash": "b" * 64},
        )

    assert preview_response.status_code == 200
    preview_body = preview_response.json()
    assert "document" in preview_body
    assert "canonical_markdown" not in preview_body
    assert "sanitized_html" not in preview_body
    assert preview_body["document"]["schema_version"] == "2"
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.headers["content-disposition"] == (
        'attachment; filename="bulletin-preview-2026-08-FR.pdf"'
    )
    assert pdf_response.content == compiler.pdf
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "edition_preview_stale"
    assert len(compiler.requests) == 1
    assert uow.publication_manifests == []
    assert uow.edition_releases == []
    assert uow.edition_renders == []


class _FailingPdfRenderer:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def render(self, document: EditionDocumentV2) -> bytes:
        del document
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (edition_preview.EditionPreviewDocumentInvalidError("invalid"), 422),
        (edition_preview.EditionPreviewMediaKindMismatchError("kind"), 422),
        (edition_preview.EditionPreviewMediaIntegrityMismatchError("hash"), 422),
        (edition_preview.EditionPreviewMediaMissingError("missing"), 503),
        (edition_preview.EditionPreviewStorageFailedError("storage"), 503),
    ],
)
async def test_preview_pdf_route_maps_render_failures_to_stable_codes(
    error: edition_preview.EditionPreviewRenderError, expected_status: int
) -> None:
    service, _, _ = _service(
        pdf_renderer=_FailingPdfRenderer(error),  # type: ignore[arg-type]
    )
    preview = await service.preview(EDITION_ID)
    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_preview_service = service
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/editions/{EDITION_ID}/preview/pdf",
            params={"preview_input_hash": preview.preview_input_hash},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == error.code
