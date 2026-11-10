from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import struct
import sys
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.http_collection import UnsafeAddressError
from cti_app.application.source_media_collection import SourceMediaArchiveService, SourceMediaPolicy
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import DetectedMimeType
from cti_app.domain.discovery import SourceRole as DiscoverySourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.source_media import SourceMediaRecord

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "ops" / "m19_archive_source_media.py"
)
_SPEC = importlib.util.spec_from_file_location("m19_archive_source_media", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = script
_SPEC.loader.exec_module(script)

SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000102")
PUBLIC_IP = "93.184.216.34"


def _png(width: int, height: int, seed: int) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(
                (
                    (x * 13 + y * 7 + seed) % 256,
                    (x * 17 + y * 3 + seed * 3) % 256,
                    (x * 5 + y * 11 + seed * 17) % 256,
                )
            )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


@dataclass
class _State:
    blobs_by_id: dict[UUID, BlobRecord] = field(default_factory=dict)
    source_documents: list[SourceDocument] = field(default_factory=list)
    media: dict[UUID, SourceMediaRecord] = field(default_factory=dict)
    runs: tuple[ProductionRun, ...] = ()
    extraction_artifacts: dict[UUID, ProductionArtifact] = field(default_factory=dict)


class _Blobs:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.state.blobs_by_id.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return next(
            (
                record
                for record in self.state.blobs_by_id.values()
                if record.descriptor.logical_bucket == logical_bucket
                and record.descriptor.sha256 == sha256
            ),
            None,
        )

    async def add(self, record: BlobRecord) -> None:
        self.state.blobs_by_id.setdefault(record.id, record)

    async def add_if_absent(self, record: BlobRecord) -> BlobRecord:
        current = await self.get_by_address(
            record.descriptor.logical_bucket, record.descriptor.sha256
        )
        if current is not None:
            return current
        self.state.blobs_by_id[record.id] = record
        return record


class _SourceDocuments:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_for_subject(self, subject_id: UUID) -> tuple[SourceDocument, ...]:
        return tuple(doc for doc in self.state.source_documents if doc.subject_id == subject_id)


class _Media:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_for_subject_policy(
        self, subject_id: UUID, policy_sha256: str
    ) -> tuple[SourceMediaRecord, ...]:
        return tuple(
            record
            for record in self.state.media.values()
            if record.subject_id == subject_id and record.policy_sha256 == policy_sha256
        )

    async def add_if_absent(self, record: SourceMediaRecord) -> SourceMediaRecord:
        return self.state.media.setdefault(record.id, record)


class _Runs:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_for_subject(self, subject_id: UUID) -> tuple[ProductionRun, ...]:
        return tuple(run for run in self.state.runs if run.subject_id == subject_id)


class _Artifacts:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        if stage != ProductionArtifactStage.EXTRACTION.value:
            return None
        return self.state.extraction_artifacts.get(run_id)

    async def list_for_run(self, run_id: UUID) -> tuple[ProductionArtifact, ...]:
        artifact = self.state.extraction_artifacts.get(run_id)
        return (artifact,) if artifact is not None else ()


class _Uow:
    def __init__(self, state: _State) -> None:
        self.blobs = _Blobs(state)
        self.source_documents = _SourceDocuments(state)
        self.source_media_candidates = _Media(state)
        self.production_runs = _Runs(state)
        self.production_artifacts = _Artifacts(state)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Factory:
    def __init__(self, state: _State) -> None:
        self.state = state

    def __call__(self) -> _Uow:
        return _Uow(self.state)


class _BlobStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.objects.setdefault((logical_bucket, digest), content)
        return BlobDescriptor(digest, len(content), mime_type, logical_bucket)

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return (descriptor.logical_bucket, descriptor.sha256) in self.objects

    async def materialize(self, descriptor: BlobDescriptor, destination: Path) -> str:
        del descriptor, destination
        return "existing"

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        content = self.objects[(descriptor.logical_bucket, descriptor.sha256)]
        if len(content) > max_bytes:
            raise ValueError("blob exceeds read limit")
        return content

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.objects.pop((descriptor.logical_bucket, descriptor.sha256), None)


class _ArtifactStore:
    def __init__(self, catalog: BlobCatalogService, extraction_payload: dict[str, Any]) -> None:
        self.catalog = catalog
        self.extraction_payload = extraction_payload
        self.read_json_calls: list[UUID] = []

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        self.read_json_calls.append(blob_id)
        return self.extraction_payload

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return await self.catalog.read(blob_id, max_bytes=max_bytes)


class _OperatorRuntime(Protocol):
    artifact_store: _ArtifactStore


class _FakeSafeHttpCollector:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    async def fetch(self, url: str, *, allow_images: bool) -> Any:
        assert allow_images is True
        self.requests.append(url)
        if url not in self.responses:
            raise UnsafeAddressError("blocked by fake policy")
        body = self.responses[url]
        digest = hashlib.sha256(body).hexdigest()
        now = datetime.now(UTC)
        return type(
            "FakeCollectedResponse",
            (),
            {
                "requested_url": url,
                "final_url": url,
                "redirect_chain": (),
                "status": 200,
                "headers": {"content-type": "image/png"},
                "declared_content_type": "image/png",
                "detected_content_type": DetectedMimeType.PNG,
                "encoded_body": body,
                "decoded_body": body,
                "encoded_size": len(body),
                "encoded_sha256": digest,
                "decoded_size": len(body),
                "decoded_sha256": digest,
                "content_encoding": "identity",
                "acquired_at": now,
            },
        )()


class _NeverCollect:
    policy = SourceMediaPolicy()

    @property
    def policy_sha256(self) -> str:
        return self.policy.sha256

    async def collect(self, *_args: object, **_kwargs: object) -> tuple[SourceMediaRecord, ...]:
        raise AssertionError("dry-run called collect")


@dataclass
class _CollectionService:
    source_media_archiver: Any


def _make_runtime(
    *,
    html: str,
    responses: dict[str, bytes] | None = None,
    max_images: int | None = None,
    stop_on_policy_block: bool = False,
    has_extraction: bool = True,
    never_collect: bool = False,
) -> tuple[_OperatorRuntime, _FakeSafeHttpCollector | None, _State]:
    state = _State()
    factory = _Factory(state)
    blob_store = _BlobStore()
    html_bytes = html.encode()
    source_digest = hashlib.sha256(html_bytes).hexdigest()
    source_descriptor = BlobDescriptor(
        source_digest, len(html_bytes), "text/html", "source-archive"
    )
    source_blob = BlobRecord(descriptor=source_descriptor)
    state.blobs_by_id[source_blob.id] = source_blob
    blob_store.objects[("source-archive", source_digest)] = html_bytes
    state.source_documents.append(
        SourceDocument(
            id=DOCUMENT_ID,
            subject_id=SUBJECT_ID,
            blob_id=source_blob.id,
            original_name="source.html",
            origin="source_collection",
            acquired_at=datetime.now(UTC),
            license_restriction=None,
            tlp=TLP.CLEAR,
            do_not_submit=False,
            external_llm_allowed=False,
            detected_mime_type="text/html",
            final_url="https://site.example/article",
        )
    )
    extracted_source = ProductionSourceExtractionV1(
        source_document_id=DOCUMENT_ID,
        canonical_url="https://site.example/article",
        content_sha256=source_digest,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=DiscoverySourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=SUBJECT_ID,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version="test-policy",
        sources=(extracted_source,),
        omitted_sources=(),
        warnings=(),
    )
    run = ProductionRun(subject_id=SUBJECT_ID, edition_id=uuid4())
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    if has_extraction:
        state.runs = (run,)
        state.extraction_artifacts[run.id] = artifact

    archive_store = _ArtifactStore(
        BlobCatalogService(blob_store, factory), production_extraction_to_json(extraction)
    )
    collector = None if never_collect else _FakeSafeHttpCollector(responses or {})
    archiver: Any
    if never_collect:
        archiver = _NeverCollect()
    else:
        archiver = SourceMediaArchiveService(
            factory,
            collector,
            blob_store,
            max_images_per_subject=max_images,
            stop_on_policy_block=stop_on_policy_block,
        )
    runtime = script.OperatorRuntime(factory, archive_store, _CollectionService(archiver))
    return runtime, collector, state


def _fixture_html() -> str:
    return """<main>
      <img alt="Network chart" src="https://site.example/chart-one.png">
      <img alt="Campaign graph" src="https://site.example/chart-two.png">
      <img alt="Site logo" src="https://site.example/logo.png">
    </main>"""


def test_dry_run_never_calls_collect_and_reports_archived_observations(capsys) -> None:
    runtime, _collector, _state = _make_runtime(html=_fixture_html(), never_collect=True)

    result = asyncio.run(
        script.process_subjects([SUBJECT_ID], runtime=runtime, execute=False, delay_seconds=0)
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "mode=dry-run html_sources=1 pdf_sources=0 image_observations=3" in output
    assert "existing_records=0" in output


def test_live_archives_two_images_and_excludes_logo_offline(capsys) -> None:
    image_a = _png(220, 130, 7)
    image_b = _png(240, 140, 23)
    runtime, collector, state = _make_runtime(
        html=_fixture_html(),
        responses={
            "https://site.example/chart-one.png": image_a,
            "https://site.example/chart-two.png": image_b,
        },
        max_images=200,
        stop_on_policy_block=True,
    )

    result = asyncio.run(
        script.process_subjects([SUBJECT_ID], runtime=runtime, execute=True, delay_seconds=0)
    )

    output = capsys.readouterr().out
    assert result == 0
    assert collector is not None
    assert len(collector.requests) == 2
    assert "ACCEPTED_FOR_REVIEW:2" in output
    assert "EXCLUDED_BY_RULE:1" in output
    assert "boilerplate_pattern:1" in output
    assert len(state.media) == 3
    assert "site.example" not in output

    capsys.readouterr()
    assert (
        asyncio.run(
            script.process_subjects([SUBJECT_ID], runtime=runtime, execute=False, delay_seconds=0)
        )
        == 0
    )
    dry_output = capsys.readouterr().out
    assert "existing_records=3" in dry_output
    assert "ACCEPTED_FOR_REVIEW:2" in dry_output


def test_selects_latest_verified_extraction_artifact(capsys) -> None:
    runtime, _collector, state = _make_runtime(html=_fixture_html(), never_collect=True)
    earlier_run = state.runs[0]
    later_run = ProductionRun(
        subject_id=SUBJECT_ID,
        edition_id=uuid4(),
        run_number=earlier_run.run_number + 1,
    )
    later_artifact = ProductionArtifact(
        production_run_id=later_run.id,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="d" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    state.runs = (earlier_run, later_run)
    state.extraction_artifacts[later_run.id] = later_artifact

    assert (
        asyncio.run(
            script.process_subjects([SUBJECT_ID], runtime=runtime, execute=False, delay_seconds=0)
        )
        == 0
    )

    assert runtime.artifact_store.read_json_calls == [later_artifact.canonical_blob_id]
    assert "mode=dry-run" in capsys.readouterr().out


def test_live_archive_is_idempotent() -> None:
    runtime, collector, state = _make_runtime(
        html=_fixture_html(),
        responses={
            "https://site.example/chart-one.png": _png(220, 130, 7),
            "https://site.example/chart-two.png": _png(240, 140, 23),
        },
        max_images=200,
    )

    assert (
        asyncio.run(
            script.process_subjects([SUBJECT_ID], runtime=runtime, execute=True, delay_seconds=0)
        )
        == 0
    )
    assert (
        asyncio.run(
            script.process_subjects([SUBJECT_ID], runtime=runtime, execute=True, delay_seconds=0)
        )
        == 0
    )

    assert collector is not None
    assert len(collector.requests) == 2
    assert len(state.media) == 3


def test_image_cap_limits_collection_per_subject() -> None:
    runtime, collector, state = _make_runtime(
        html=_fixture_html(),
        responses={"https://site.example/chart-one.png": _png(220, 130, 7)},
        max_images=1,
    )

    result = asyncio.run(
        script.process_subjects([SUBJECT_ID], runtime=runtime, execute=True, delay_seconds=0)
    )

    assert result == 0
    assert collector is not None
    assert collector.requests == ["https://site.example/chart-one.png"]
    assert len(state.media) == 1


def test_execution_requires_explicit_confirmation() -> None:
    with pytest.raises(SystemExit):
        script.parse_args([str(SUBJECT_ID), "--execute"])

    args = script.parse_args(
        [str(SUBJECT_ID), "--execute", "--i-confirm-user-authorized-image-fetch"]
    )
    assert args.execute is True


def test_subject_without_verified_extraction_reports_clear_error(capsys) -> None:
    runtime, _collector, _state = _make_runtime(
        html=_fixture_html(), has_extraction=False, never_collect=True
    )

    result = asyncio.run(
        script.process_subjects([SUBJECT_ID], runtime=runtime, execute=False, delay_seconds=0)
    )

    assert result == 1
    assert f"subject {SUBJECT_ID} has no verified extraction artifact" in capsys.readouterr().out


def test_policy_block_stops_subject_collection_and_remaining_subjects(capsys) -> None:
    html = """<main>
      <img alt="Network chart" src="https://site.example/chart-one.png">
      <img alt="Blocked chart" src="https://site.example/blocked.png">
      <img alt="Later chart" src="https://site.example/chart-two.png">
    </main>"""
    runtime, collector, state = _make_runtime(
        html=html,
        responses={"https://site.example/chart-one.png": _png(220, 130, 7)},
        max_images=200,
        stop_on_policy_block=True,
    )

    result = asyncio.run(
        script.process_subjects(
            [SUBJECT_ID, uuid4()], runtime=runtime, execute=True, delay_seconds=0
        )
    )

    output = capsys.readouterr().out
    assert result == 1
    assert collector is not None
    assert collector.requests == [
        "https://site.example/chart-one.png",
        "https://site.example/blocked.png",
    ]
    assert len(state.media) == 2
    assert "policy_blocked=true" in output

    assert (
        asyncio.run(
            script.process_subjects([SUBJECT_ID], runtime=runtime, execute=True, delay_seconds=0)
        )
        == 1
    )
    assert collector.requests == [
        "https://site.example/chart-one.png",
        "https://site.example/blocked.png",
    ]
