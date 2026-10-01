from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application import edition_rendering
from cti_app.application.edition_rendering import (
    EditionRenderDocumentIntegrityError,
    EditionRenderPublicationSchemaUnsupportedError,
    EditionRenderService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileFailedError,
    TypstCompileRequest,
)
from cti_app.application.typst_rendering import TypstRenderSource, TypstTemplateBundle
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    EditionRelease,
)
from cti_app.domain.edition_render import (
    EDITION_RENDER_POLICY_VERSION,
    EDITION_RENDERER,
    EDITION_RENDERER_VERSION,
    EDITION_TEMPLATE_VERSION,
    EditionRender,
    EditionRenderAcquisition,
    EditionRenderAcquisitionOutcome,
    EditionRenderFormat,
    EditionRenderStatus,
    compute_edition_render_input_hash,
    edition_render_acquisition_outcome,
    edition_render_retrying,
    invalid_succeeded_edition_render_reacquisition_outcome,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
)


def _identity() -> dict[str, object]:
    return {
        "edition_release_id": uuid4(),
        "edition_document_sha256": "1" * 64,
        "renderer": EDITION_RENDERER,
        "renderer_version": EDITION_RENDERER_VERSION,
        "template_version": EDITION_TEMPLATE_VERSION,
        "template_sha256": "2" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "chp-fonts-v1",
        "render_policy_version": EDITION_RENDER_POLICY_VERSION,
        "format": EditionRenderFormat.PDF,
    }


def test_edition_render_identity_is_deterministic_and_tracks_every_render_input() -> None:
    identity = _identity()
    expected = compute_edition_render_input_hash(**identity)  # type: ignore[arg-type]
    assert compute_edition_render_input_hash(**identity) == expected  # type: ignore[arg-type]

    mutations: tuple[tuple[str, object], ...] = (
        ("edition_release_id", uuid4()),
        ("edition_document_sha256", "3" * 64),
        ("renderer", "other-renderer"),
        ("renderer_version", "other-renderer-version"),
        ("template_version", "other-template-version"),
        ("template_sha256", "4" * 64),
        ("compiler", "other-compiler"),
        ("compiler_version", "other-compiler-version"),
        ("font_bundle_version", "other-font-bundle"),
        ("render_policy_version", "other-render-policy"),
        ("format", "png"),
    )
    for field_name, value in mutations:
        changed = {**identity, field_name: value}
        assert compute_edition_render_input_hash(**changed) != expected  # type: ignore[arg-type]


def test_job_and_runtime_identifiers_are_not_hash_inputs() -> None:
    identity = _identity()
    expected = compute_edition_render_input_hash(**identity)  # type: ignore[arg-type]
    for excluded in ("job_id", "worker_id", "timestamp", "tempdir", "request_id"):
        with pytest.raises(TypeError):
            compute_edition_render_input_hash(
                **identity,
                **{excluded: "runtime-only-value"},
            )  # type: ignore[arg-type]
    assert compute_edition_render_input_hash(**identity) == expected  # type: ignore[arg-type]


def _proposed_render(identity: dict[str, object]) -> EditionRender:
    now = datetime.now(UTC)
    return EditionRender(
        id=uuid4(),
        edition_release_id=identity["edition_release_id"],  # type: ignore[arg-type]
        renderer=str(identity["renderer"]),
        renderer_version=str(identity["renderer_version"]),
        template_version=str(identity["template_version"]),
        template_sha256=str(identity["template_sha256"]),
        compiler=str(identity["compiler"]),
        compiler_version=str(identity["compiler_version"]),
        font_bundle_version=str(identity["font_bundle_version"]),
        render_policy_version=str(identity["render_policy_version"]),
        format=EditionRenderFormat(identity["format"]),  # type: ignore[arg-type]
        input_hash=compute_edition_render_input_hash(**identity),  # type: ignore[arg-type]
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=None,
        output_sha256=None,
        output_byte_size=None,
        status=EditionRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


class _FakeEditionRenders:
    def __init__(self) -> None:
        self.renders_by_hash: dict[str, EditionRender] = {}
        self._lock = asyncio.Lock()

    async def acquire_for_render(
        self, proposed: EditionRender, *, stale_running_before: datetime
    ) -> EditionRenderAcquisition:
        async with self._lock:
            existing = self.renders_by_hash.get(proposed.input_hash)
            if existing is None:
                self.renders_by_hash[proposed.input_hash] = proposed
                return EditionRenderAcquisition(EditionRenderAcquisitionOutcome.ACQUIRED, proposed)

            outcome = edition_render_acquisition_outcome(
                existing,
                stale_running_before=stale_running_before,
            )
            if outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
                existing = edition_render_retrying(existing, now=datetime.now(UTC))
                self.renders_by_hash[proposed.input_hash] = existing
            return EditionRenderAcquisition(outcome, existing)


@pytest.mark.asyncio
async def test_fake_repository_acquisition_is_idempotent_for_one_input_hash() -> None:
    proposed = _proposed_render(_identity())
    repository = _FakeEditionRenders()
    stale_before = datetime.now(UTC) - timedelta(minutes=5)
    acquisitions = await asyncio.gather(
        *(
            repository.acquire_for_render(
                replace(proposed, id=uuid4()), stale_running_before=stale_before
            )
            for _ in range(8)
        )
    )

    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert len(repository.renders_by_hash) == 1
    owner = next(
        acquisition
        for acquisition in acquisitions
        if acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED
    )
    assert all(acquisition.render.id == owner.render.id for acquisition in acquisitions)


def _edition_document(title: str = "Frozen edition document") -> EditionDocumentV2:
    subject_id = uuid4()
    publication = PublicationDocumentV4(
        schema_version="4",
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
    return EditionDocumentV2(
        edition={
            "id": str(uuid4()),
            "country": "France",
            "country_code": "FR",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "tlp": "GREEN",
            "languages": ["fr"],
            "version": 1,
        },
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=subject_id,
                document=publication,
            ),
        ),
    )


class _RenderArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[Any, bytes] = {}

    async def read_bytes(self, blob_id: Any, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        del max_bytes
        return self.blobs[blob_id]

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> Any:
        del bucket, mime_type
        blob_id = uuid4()
        self.blobs[blob_id] = content
        return blob_id


class _ReleaseRepository:
    def __init__(self, releases: dict[Any, EditionRelease]) -> None:
        self.releases = releases

    async def get(self, release_id: Any) -> EditionRelease | None:
        return self.releases.get(release_id)


class _ServiceRenders:
    def __init__(self) -> None:
        self.renders: dict[Any, EditionRender] = {}
        self.lock = asyncio.Lock()

    async def get_by_input_hash(self, input_hash: str) -> EditionRender | None:
        return next(
            (render for render in self.renders.values() if render.input_hash == input_hash),
            None,
        )

    async def get_latest_for_release(self, release_id: Any) -> EditionRender | None:
        renders = [
            render for render in self.renders.values() if render.edition_release_id == release_id
        ]
        return max(renders, key=lambda render: (render.created_at, str(render.id)), default=None)

    async def acquire_for_render(
        self, proposed: EditionRender, *, stale_running_before: datetime
    ) -> EditionRenderAcquisition:
        async with self.lock:
            existing = await self.get_by_input_hash(proposed.input_hash)
            if existing is None:
                self.renders[proposed.id] = proposed
                return EditionRenderAcquisition(EditionRenderAcquisitionOutcome.ACQUIRED, proposed)
            outcome = edition_render_acquisition_outcome(
                existing,
                stale_running_before=stale_running_before,
            )
            if outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
                existing = edition_render_retrying(existing, now=datetime.now(UTC))
                self.renders[existing.id] = existing
            return EditionRenderAcquisition(outcome, existing)

    async def reacquire_invalid_succeeded(
        self,
        proposed: EditionRender,
        *,
        observed_output_blob_id: Any,
        observed_output_sha256: str,
    ) -> EditionRenderAcquisition:
        async with self.lock:
            existing = self.renders.get(proposed.id)
            if existing is None:
                self.renders[proposed.id] = proposed
                return EditionRenderAcquisition(
                    EditionRenderAcquisitionOutcome.ACQUIRED,
                    proposed,
                )
            outcome = invalid_succeeded_edition_render_reacquisition_outcome(
                existing,
                observed_output_blob_id=observed_output_blob_id,
                observed_output_sha256=observed_output_sha256,
            )
            if outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
                existing = edition_render_retrying(existing, now=datetime.now(UTC))
                self.renders[existing.id] = existing
            return EditionRenderAcquisition(outcome, existing)

    async def mark_succeeded(
        self,
        render_id: Any,
        *,
        output_blob_id: Any,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: Any = None,
        render_data_blob_id: Any = None,
    ) -> EditionRender:
        current = self.renders[render_id]
        succeeded = replace(
            current,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            output_blob_id=output_blob_id,
            output_sha256=output_sha256,
            output_byte_size=output_byte_size,
            status=EditionRenderStatus.SUCCEEDED,
            error_code=None,
            error_message=None,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = succeeded
        return succeeded

    async def mark_failed(
        self,
        render_id: Any,
        *,
        error_code: str,
        error_message: str | None = None,
        source_blob_id: Any = None,
        render_data_blob_id: Any = None,
    ) -> EditionRender:
        current = self.renders[render_id]
        failed = replace(
            current,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            status=EditionRenderStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = failed
        return failed


class _RenderUnitOfWork:
    def __init__(
        self,
        releases: dict[Any, EditionRelease],
        renders: _ServiceRenders,
        unexpected_accesses: list[str],
    ) -> None:
        self.edition_releases = _ReleaseRepository(releases)
        self.edition_renders = renders
        self.unexpected_accesses = unexpected_accesses

    async def __aenter__(self) -> _RenderUnitOfWork:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        self.unexpected_accesses.append(name)
        if name in {"production_artifacts", "subjects", "production_runs"}:
            raise AssertionError(f"EditionRenderService must not look up {name}")
        raise AssertionError(f"Unexpected unit-of-work access: {name}")


class _RenderUnitOfWorkFactory:
    def __init__(self, releases: dict[Any, EditionRelease], renders: _ServiceRenders) -> None:
        self.releases = releases
        self.renders = renders
        self.unexpected_accesses: list[str] = []

    def __call__(self) -> _RenderUnitOfWork:
        return _RenderUnitOfWork(self.releases, self.renders, self.unexpected_accesses)


class _NoMedia:
    async def get(self, asset_id: Any) -> None:
        raise AssertionError(f"Unexpected media lookup: {asset_id}")

    async def read(self, asset_id: Any) -> bytes:
        raise AssertionError(f"Unexpected media read: {asset_id}")


class _CapturingEditionTypstRenderer:
    def __init__(self) -> None:
        self.documents: list[EditionDocumentV2] = []
        self.templates: list[TypstTemplateBundle] = []

    def render(
        self, document: EditionDocumentV2, template_bundle: TypstTemplateBundle
    ) -> TypstRenderSource:
        self.documents.append(document)
        self.templates.append(template_bundle)
        source = next(
            file.content
            for file in template_bundle.files
            if file.relative_path == "RENDERER/edition.typ"
        )
        data = ProductionArtifactStore.canonical_json_bytes(document.to_json())
        return TypstRenderSource(
            source_bytes=source,
            source_sha256=hashlib.sha256(source).hexdigest(),
            render_data_bytes=data,
            render_data_sha256=hashlib.sha256(data).hexdigest(),
            media_refs=(),
            entrypoint_relative_path="RENDERER/edition.typ",
            render_data_relative_path="RENDERER/render-data.json",
        )


class _FakeEditionCompiler:
    def __init__(self, failures: tuple[Exception, ...] = ()) -> None:
        self.failures = list(failures)
        self.call_count = 0
        self.content = b"%PDF-edition-test"
        self.workspace_snapshots: list[dict[str, bytes]] = []

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        self.workspace_snapshots.append(
            {
                path.relative_to(request.workspace_root).as_posix(): path.read_bytes()
                for path in request.workspace_root.rglob("*")
                if path.is_file()
            }
        )
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.content,
            sha256=hashlib.sha256(self.content).hexdigest(),
            byte_size=len(self.content),
            compiler="typst",
            compiler_version="0.15.1",
        )


def _service_fixture(tmp_path: Path, document: EditionDocumentV2 | None = None) -> tuple[Any, ...]:
    canonical_document = document or _edition_document()
    document_bytes = ProductionArtifactStore.canonical_json_bytes(canonical_document.to_json())
    blob_store = _RenderArtifactStore()
    document_blob_id = uuid4()
    blob_store.blobs[document_blob_id] = document_bytes
    release = EditionRelease(
        edition_id=uuid4(),
        manifest_id=uuid4(),
        edition_document_blob_id=document_blob_id,
        edition_document_sha256=hashlib.sha256(document_bytes).hexdigest(),
    )
    renders = _ServiceRenders()
    uow_factory = _RenderUnitOfWorkFactory({release.id: release}, renders)

    template_root = tmp_path / "chpTypst"
    entrypoint = template_root / "RENDERER" / "edition.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"// edition template T1")
    (template_root / "edition-renderer-manifest.json").write_text(
        json.dumps(
            {
                "template_version": "test-edition-v1",
                "files": ["RENDERER/edition.typ"],
            }
        ),
        encoding="utf-8",
    )
    font_root = tmp_path / "fonts"
    font_path = font_root / "test-font.ttf"
    font_path.parent.mkdir(parents=True)
    font_path.write_bytes(b"test font bytes")
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text(
        json.dumps({"font_bundle_label": "test-fonts-v1", "files": ["test-font.ttf"]}),
        encoding="utf-8",
    )
    renderer = _CapturingEditionTypstRenderer()
    compiler = _FakeEditionCompiler()
    service = EditionRenderService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        artifact_store=blob_store,  # type: ignore[arg-type]
        media_asset_store=_NoMedia(),  # type: ignore[arg-type]
        renderer=renderer,  # type: ignore[arg-type]
        compiler=compiler,  # type: ignore[arg-type]
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=lock_path,
        wait_poll_interval_seconds=0.001,
        wait_timeout_seconds=1,
    )
    return (
        service,
        release,
        canonical_document,
        renders,
        blob_store,
        renderer,
        compiler,
        entrypoint,
        font_path,
    )


@pytest.mark.asyncio
async def test_edition_render_service_reuses_same_inputs_and_only_compiles_once(
    tmp_path: Path,
) -> None:
    service, release, document, renders, _store, renderer, compiler, *_ = _service_fixture(tmp_path)

    first, second = await asyncio.gather(
        service.render_pdf(release.id),
        service.render_pdf(release.id),
    )

    assert first.id == second.id
    assert first.status is EditionRenderStatus.SUCCEEDED
    assert compiler.call_count == 1
    assert renderer.documents == [document]
    assert len(renders.renders) == 1
    assert first.source_blob_id is not None
    assert first.render_data_blob_id is not None


@pytest.mark.asyncio
async def test_render_uses_the_frozen_document_after_current_publication_changes(
    tmp_path: Path,
) -> None:
    service, release, frozen_document, _renders, _store, renderer, _compiler, *_ = _service_fixture(
        tmp_path
    )
    current_document = _edition_document("New V4 publication after release")
    service._uow_factory.current_publication_document = current_document  # type: ignore[attr-defined]

    await service.render_pdf(release.id)

    assert renderer.documents == [frozen_document]
    assert renderer.documents[0] != current_document
    assert service._uow_factory.unexpected_accesses == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_template_change_creates_a_new_render_for_the_same_frozen_release(
    tmp_path: Path,
) -> None:
    service, release, document, renders, store, _renderer, compiler, entrypoint, *_ = (
        _service_fixture(tmp_path)
    )
    first = await service.render_pdf(release.id)
    first_document_bytes = store.blobs[release.edition_document_blob_id]
    compiler.content = b"%PDF-edition-template-T2"
    entrypoint.write_bytes(b"// edition template T2")

    second = await service.render_pdf(release.id)

    assert second.id != first.id
    assert first.id in renders.renders
    assert renders.renders[first.id] == first
    assert second.edition_release_id == first.edition_release_id == release.id
    assert store.blobs[release.edition_document_blob_id] == first_document_bytes
    assert service._uow_factory.releases[release.id] == release  # type: ignore[attr-defined]
    assert service._uow_factory.unexpected_accesses == []  # type: ignore[attr-defined]
    assert document == _edition_document_from_blob(first_document_bytes)
    assert first.output_sha256 != second.output_sha256
    assert compiler.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", ["font", "compiler"])
async def test_font_or_compiler_change_creates_a_new_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_input: str,
) -> None:
    service, release, _document, _renders, _store, _renderer, compiler, _entrypoint, font_path = (
        _service_fixture(tmp_path)
    )
    first = await service.render_pdf(release.id)
    if changed_input == "font":
        font_path.write_bytes(b"different font bytes")
    else:
        monkeypatch.setattr(edition_rendering, "TYPST_COMPILER_VERSION", "0.15.2")
    compiler.content = f"%PDF-edition-{changed_input}-change".encode()

    second = await service.render_pdf(release.id)

    assert second.id != first.id
    assert second.input_hash != first.input_hash
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_corrupted_cached_pdf_reacquires_the_same_render_and_rerenders(
    tmp_path: Path,
) -> None:
    service, release, _document, _renders, store, _renderer, compiler, *_ = _service_fixture(
        tmp_path
    )
    first = await service.render_pdf(release.id)
    assert first.output_blob_id is not None
    store.blobs[first.output_blob_id] = b"corrupted PDF"
    compiler.content = b"%PDF-restored-edition"

    second = await service.render_pdf(release.id)

    assert second.id == first.id
    assert second.output_sha256 == hashlib.sha256(compiler.content).hexdigest()
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_failed_compile_keeps_release_and_retry_reacquires_same_render(
    tmp_path: Path,
) -> None:
    service, release, _document, renders, _store, _renderer, compiler, *_ = _service_fixture(
        tmp_path
    )
    compiler.failures.append(TypstCompileFailedError("fixture compile failure"))

    with pytest.raises(TypstCompileFailedError):
        await service.render_pdf(release.id)
    failed = next(iter(renders.renders.values()))
    assert failed.status is EditionRenderStatus.FAILED
    assert failed.error_code == "typst_compile_failed"
    assert service._uow_factory.releases[release.id] == release  # type: ignore[attr-defined]

    succeeded = await service.render_pdf(release.id)

    assert succeeded.id == failed.id
    assert succeeded.status is EditionRenderStatus.SUCCEEDED
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_edition_document_hash_mismatch_is_stable_and_prevents_render(
    tmp_path: Path,
) -> None:
    service, release, _document, renders, *_ = _service_fixture(tmp_path)
    service._uow_factory.releases[release.id] = replace(  # type: ignore[attr-defined]
        release,
        edition_document_sha256="f" * 64,
    )

    with pytest.raises(EditionRenderDocumentIntegrityError) as error:
        await service.render_pdf(release.id)

    assert error.value.code == "edition_document_integrity_mismatch"
    assert renders.renders == {}


@pytest.mark.asyncio
async def test_non_v4_publication_uses_stable_unsupported_schema_code(
    tmp_path: Path,
) -> None:
    service, release, document, renders, store, *_ = _service_fixture(tmp_path)
    payload = document.to_json()
    payload["publications"][0]["document"]["schema_version"] = "3"
    raw = json.dumps(payload).encode()
    store.blobs[release.edition_document_blob_id] = raw
    service._uow_factory.releases[release.id] = replace(  # type: ignore[attr-defined]
        release,
        edition_document_sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(EditionRenderPublicationSchemaUnsupportedError) as error:
        await service.render_pdf(release.id)

    assert error.value.code == "edition_render_publication_schema_unsupported"
    assert renders.renders == {}


def _edition_document_from_blob(content: bytes) -> EditionDocumentV2:
    return EditionDocumentV2.from_json(json.loads(content))
