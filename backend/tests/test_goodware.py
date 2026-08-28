import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application import goodware as goodware_application
from cti_app.application.goodware import GoodwareMeasurementService, GoodwareService
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.goodware import (
    Banality,
    BanalityScorer,
    BanalityThresholds,
    GoodwareBaseline,
    GoodwareIndexArtifact,
)
from cti_app.infrastructure.goodware_stage import GoodwareStageError, load_stage


def _stage(tmp_path: Path, records: bytes, source: bytes = b"database") -> tuple[Path, Path]:
    source_dir, stage_dir = tmp_path / "sources", tmp_path / "stage"
    source_dir.mkdir()
    stage_dir.mkdir()
    (source_dir / "good-strings.db").write_bytes(source)
    manifest = {
        "schema_version": "autowork-goodware-stage-v1",
        "source_format": "yargen-gzip-json-counter-v1",
        "source_set_sha256": hashlib.sha256(
            json.dumps(
                [
                    {
                        "filename": "good-strings.db",
                        "feature_kind": "string",
                        "sha256": hashlib.sha256(source).hexdigest(),
                        "size": len(source),
                    }
                ],
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "records_sha256": hashlib.sha256(records).hexdigest(),
        "record_count": 1,
        "occurrence_sum": 2,
        "sources": [
            {
                "filename": "good-strings.db",
                "feature_kind": "string",
                "sha256": hashlib.sha256(source).hexdigest(),
                "size": len(source),
            }
        ],
    }
    (stage_dir / "records.jsonl").write_bytes(records)
    (stage_dir / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
    )
    return stage_dir, source_dir


def test_stage_validates_and_streams_records(tmp_path: Path) -> None:
    records = b'{"feature_kind":"string","normalized_value":"hello","occurrence_count":2}\n'
    stage, source = _stage(tmp_path, records)
    loaded = load_stage(stage, source)
    assert next(iter(loaded.iter_features())).normalized_value == "hello"


def test_stage_rejects_path_traversal(tmp_path: Path) -> None:
    stage, source = _stage(
        tmp_path, b'{"feature_kind":"string","normalized_value":"hello","occurrence_count":2}\n'
    )
    manifest = json.loads((stage / "manifest.json").read_text())
    manifest["sources"][0]["filename"] = "../good-strings.db"
    (stage / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(GoodwareStageError):
        load_stage(stage, source)


def test_banality_thresholds_and_lookup_buckets() -> None:
    scorer = BanalityScorer(BanalityThresholds(suspicious_count=3, banal_count=5))
    assert scorer.score(None) is Banality.UNKNOWN
    assert scorer.score(1) is Banality.SPECIFIC
    assert scorer.score(3) is Banality.SUSPICIOUS_COMMON
    assert scorer.score(5) is Banality.BANAL


def test_thresholds_are_ordered() -> None:
    with pytest.raises(ValueError):
        BanalityThresholds(suspicious_count=0, banal_count=1)


class _LookupRepository:
    def __init__(
        self, baseline: GoodwareBaseline, artifact: GoodwareIndexArtifact
    ) -> None:
        self.baseline = baseline
        self.artifact = artifact
        self.calls = 0

    async def get(self, baseline_id: object) -> GoodwareBaseline | None:
        self.calls += 1
        return self.baseline if baseline_id == self.baseline.id else None

    async def get_by_baseline_fingerprint_sha256(self, fingerprint: str) -> GoodwareBaseline | None:
        self.calls += 1
        return (
            self.baseline
            if fingerprint == self.baseline.baseline_fingerprint_sha256
            else None
        )

    async def get_index_artifact(
        self, baseline_id: object, **kwargs: object
    ) -> GoodwareIndexArtifact:
        self.calls += 1
        return self.artifact


class _LookupBlobsRepository:
    def __init__(self, blobs: dict[UUID, SimpleNamespace]) -> None:
        self.blobs = blobs
        self.calls = 0

    async def get(self, blob_id: object) -> object:
        self.calls += 1
        return self.blobs[cast(UUID, blob_id)]


class _LookupUow:
    def __init__(self, goodware: _LookupRepository, blobs: _LookupBlobsRepository) -> None:
        self.goodware_baselines = goodware
        self.blobs = blobs

    async def __aenter__(self) -> "_LookupUow":
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _LookupUowFactory:
    def __init__(self, uow: _LookupUow) -> None:
        self.uow = uow
        self.calls = 0

    def __call__(self) -> _LookupUow:
        self.calls += 1
        return self.uow


class _LookupStore:
    def __init__(self, index_bytes: bytes, manifest_bytes: bytes) -> None:
        self.index_bytes = index_bytes
        self.manifest_bytes = manifest_bytes
        self.reads = 0
        self.materializations = 0

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        self.reads += 1
        return self.manifest_bytes

    async def materialize(self, descriptor: BlobDescriptor, destination: Path) -> str:
        self.materializations += 1
        await asyncio.sleep(0)
        await asyncio.to_thread(destination.write_bytes, self.index_bytes)
        return "copy"


def _lookup_fixture(tmp_path: Path) -> tuple[
    GoodwareBaseline,
    GoodwareIndexArtifact,
    dict[UUID, SimpleNamespace],
    bytes,
    bytes,
    Path,
]:
    baseline_id, artifact_id = uuid4(), uuid4()
    index_blob_id, manifest_blob_id = uuid4(), uuid4()
    source = {
        "filename": "good-strings.db",
        "feature_kind": "string",
        "sha256": "a" * 64,
        "size": 1,
        "entry_count": 1,
        "occurrence_sum": 1,
    }
    source_set = goodware_application._source_set_sha256([source])
    fingerprint = goodware_application.baseline_fingerprint_sha256(source_set)
    manifest = {
        "schema_version": goodware_application.SCHEMA_VERSION,
        "source_format": goodware_application.SOURCE_FORMAT,
        "source_set_sha256": source_set,
        "normalization_version": goodware_application.NORMALIZATION_VERSION,
        "key_version": goodware_application.KEY_VERSION,
        "index_format_version": goodware_application.INDEX_FORMAT_VERSION,
        "baseline_fingerprint_sha256": fingerprint,
        "record_count": 1,
        "occurrence_sum": 7,
        "index_sha256": "0" * 64,
        "index_size": 1,
        "sources": [source],
    }
    index_path = tmp_path / "source-index.sqlite3"
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(goodware_application._FEATURES_SQL)
        connection.execute(goodware_application._METADATA_SQL)
        connection.execute(
            "INSERT INTO features VALUES (?, ?)",
            (goodware_application.goodware_lookup_key("string", "hello"), 7),
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            goodware_application._expected_metadata(
                manifest, pattern_version=goodware_application.NON_DISCRIMINANT_PATTERN_VERSION
            ).items(),
        )
        connection.commit()
    finally:
        connection.close()
    index_bytes = index_path.read_bytes()
    manifest["index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
    manifest["index_size"] = len(index_bytes)
    manifest_bytes = (
        goodware_application._canonical_json(manifest) + "\n"
    ).encode("utf-8")
    baseline = GoodwareBaseline(
        id=baseline_id,
        baseline_fingerprint_sha256=fingerprint,
        source_set_sha256=source_set,
        normalization_version=goodware_application.NORMALIZATION_VERSION,
        record_count=1,
        occurrence_sum=7,
        pattern_version=goodware_application.NON_DISCRIMINANT_PATTERN_VERSION,
        sources=(),
    )
    artifact = GoodwareIndexArtifact(
        id=artifact_id,
        baseline_id=baseline_id,
        schema_version=goodware_application.SCHEMA_VERSION,
        key_version=goodware_application.KEY_VERSION,
        index_format_version=goodware_application.INDEX_FORMAT_VERSION,
        index_blob_id=index_blob_id,
        manifest_blob_id=manifest_blob_id,
    )
    blobs = {
        index_blob_id: SimpleNamespace(
            descriptor=BlobDescriptor(
                sha256=cast(str, manifest["index_sha256"]),
                size=cast(int, manifest["index_size"]),
                mime_type="application/vnd.sqlite3",
                logical_bucket="goodware-indexes",
            )
        ),
        manifest_blob_id: SimpleNamespace(
            descriptor=BlobDescriptor(
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size=len(manifest_bytes),
                mime_type="application/json",
                logical_bucket="goodware-index-manifests",
            )
        ),
    }
    return baseline, artifact, blobs, index_bytes, manifest_bytes, index_path


@pytest.mark.asyncio
async def test_measurement_prepares_once_and_reuses_uuid_and_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, artifact, blobs, index_bytes, manifest_bytes, _ = _lookup_fixture(tmp_path)
    repository = _LookupRepository(baseline, artifact)
    blob_repository = _LookupBlobsRepository(blobs)
    uow_factory = _LookupUowFactory(_LookupUow(repository, blob_repository))
    store = _LookupStore(index_bytes, manifest_bytes)
    verifications = {"cache": 0, "sqlite": 0}
    original_cache = goodware_application._verify_cached_file
    original_sqlite = goodware_application._verify_sqlite_index

    def verify_cache(*args: object, **kwargs: object) -> None:
        verifications["cache"] += 1
        cast(Any, original_cache)(*args, **kwargs)

    def verify_sqlite(*args: object, **kwargs: object) -> None:
        verifications["sqlite"] += 1
        cast(Any, original_sqlite)(*args, **kwargs)

    monkeypatch.setattr(goodware_application, "_verify_cached_file", verify_cache)
    monkeypatch.setattr(goodware_application, "_verify_sqlite_index", verify_sqlite)
    service = GoodwareMeasurementService(
        cast(Any, store), cast(Any, uow_factory), cache_root=tmp_path / "cache"
    )

    assert await service.lookup(baseline.id, "string", "hello") == 7
    counts = (uow_factory.calls, store.reads, store.materializations, dict(verifications))
    assert await service.lookup(baseline.baseline_fingerprint_sha256, "string", "hello") == 7
    assert (uow_factory.calls, store.reads, store.materializations, verifications) == counts


@pytest.mark.asyncio
async def test_measurement_concurrent_first_lookups_prepare_once(tmp_path: Path) -> None:
    baseline, artifact, blobs, index_bytes, manifest_bytes, _ = _lookup_fixture(tmp_path)
    repository = _LookupRepository(baseline, artifact)
    blob_repository = _LookupBlobsRepository(blobs)
    uow_factory = _LookupUowFactory(_LookupUow(repository, blob_repository))
    store = _LookupStore(index_bytes, manifest_bytes)
    service = GoodwareMeasurementService(
        cast(Any, store), cast(Any, uow_factory), cache_root=tmp_path / "cache"
    )

    assert list(
        await asyncio.gather(
            service.lookup(baseline.id, "string", "hello"),
            service.lookup(baseline.baseline_fingerprint_sha256, "string", "hello"),
        )
    ) == [7, 7]
    assert uow_factory.calls == 1
    assert store.reads == 1
    assert store.materializations == 1


@pytest.mark.asyncio
async def test_import_descriptor_mismatch_precedes_baseline_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, artifact_dir = tmp_path / "sources", tmp_path / "artifact"
    source_dir.mkdir()
    artifact_dir.mkdir()
    source_bytes = b"database"
    (source_dir / "good-strings.db").write_bytes(source_bytes)
    index_bytes = b"sqlite-index"
    (artifact_dir / goodware_application.INDEX_FILENAME).write_bytes(index_bytes)
    source = {
        "filename": "good-strings.db",
        "feature_kind": "string",
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size": len(source_bytes),
        "entry_count": 1,
        "occurrence_sum": 1,
    }
    source_set = goodware_application._source_set_sha256([source])
    manifest = {
        "schema_version": goodware_application.SCHEMA_VERSION,
        "source_format": goodware_application.SOURCE_FORMAT,
        "source_set_sha256": source_set,
        "normalization_version": goodware_application.NORMALIZATION_VERSION,
        "key_version": goodware_application.KEY_VERSION,
        "index_format_version": goodware_application.INDEX_FORMAT_VERSION,
        "baseline_fingerprint_sha256": goodware_application.baseline_fingerprint_sha256(
            source_set
        ),
        "record_count": 0,
        "occurrence_sum": 0,
        "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "index_size": len(index_bytes),
        "sources": [source],
    }
    (artifact_dir / goodware_application.MANIFEST_FILENAME).write_bytes(
        (goodware_application._canonical_json(manifest) + "\n").encode()
    )
    monkeypatch.setattr(goodware_application, "_validate_artifact", lambda *_: manifest)

    class Repository:
        add_called = False

        async def get_by_baseline_fingerprint_sha256(self, fingerprint: str) -> None:
            return None

        async def add_if_absent(self, baseline: object) -> bool:
            self.add_called = True
            return True

    class Uow:
        def __init__(self) -> None:
            self.goodware_baselines = Repository()
            self.committed = False

        async def __aenter__(self) -> "Uow":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

    class MismatchingBlobs:
        async def ingest(self, handle: object, *, logical_bucket: str, mime_type: str) -> object:
            return SimpleNamespace(
                id=uuid4(),
                descriptor=BlobDescriptor(
                    sha256="b" * 64,
                    size=1,
                    mime_type=mime_type,
                    logical_bucket=logical_bucket,
                ),
            )

    uow = Uow()
    with pytest.raises(goodware_application.GoodwareImportError, match="does not match"):
        await GoodwareService(cast(Any, MismatchingBlobs()), cast(Any, lambda: uow)).import_index(
            artifact_dir, source_dir
        )
    assert not uow.goodware_baselines.add_called
    assert not uow.committed
