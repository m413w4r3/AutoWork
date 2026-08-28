from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import get_settings
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.goodware import (
    GoodwareBaseline,
    GoodwareIndexArtifact,
    GoodwareSource,
)
from cti_app.domain.goodware_index import (
    INDEX_FILENAME,
    INDEX_FORMAT_VERSION,
    KEY_VERSION,
    MANIFEST_FILENAME,
    NON_DISCRIMINANT_PATTERN_VERSION,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    SOURCE_FORMAT,
    SUPPORTED_FEATURE_KINDS,
    GoodwareImportError,
    GoodwareMeasurementError,
    baseline_fingerprint_sha256,
    canonical_json,
    canonical_lookup_key,
    goodware_lookup_key,
    lookup_key,
    source_set_sha256,
)
from cti_app.infrastructure.goodware_artifact import (
    _MAX_MANIFEST_BYTES,
    canonical_manifest_bytes,
    ensure_ingested_descriptor,
    manifest_sha256,
    validate_artifact,
    validate_manifest,
)
from cti_app.infrastructure.goodware_index import (
    _FEATURES_SQL,
    _METADATA_SQL,
    GOODWARE_CACHE_ROOT,
    GoodwareSQLiteReader,
    expected_metadata,
    prepare_cached_index,
    verify_cached_file,
    verify_sqlite_index,
)

__all__ = [
    "GOODWARE_CACHE_ROOT",
    "INDEX_FILENAME",
    "INDEX_FORMAT_VERSION",
    "KEY_VERSION",
    "MANIFEST_FILENAME",
    "NON_DISCRIMINANT_PATTERN_VERSION",
    "NORMALIZATION_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_FORMAT",
    "SUPPORTED_FEATURE_KINDS",
    "_FEATURES_SQL",
    "_MAX_MANIFEST_BYTES",
    "_METADATA_SQL",
    "GoodwareImportError",
    "GoodwareMeasurementError",
    "GoodwareMeasurementService",
    "GoodwareSQLiteReader",
    "GoodwareService",
    "PreparedGoodwareIndex",
    "_canonical_json",
    "_expected_metadata",
    "_source_set_sha256",
    "_validate_artifact",
    "_validate_manifest",
    "_verify_cached_file",
    "_verify_sqlite_index",
    "baseline_fingerprint_sha256",
    "canonical_lookup_key",
    "goodware_lookup_key",
    "lookup_key",
]

# Compatibility for callers that used the original application-local names.
_canonical_json = canonical_json
_source_set_sha256 = source_set_sha256
_validate_artifact = validate_artifact
_validate_manifest = validate_manifest
_ensure_ingested_descriptor = ensure_ingested_descriptor
_expected_metadata = expected_metadata
_verify_cached_file = verify_cached_file
_verify_sqlite_index = verify_sqlite_index


@dataclass(frozen=True, slots=True)
class PreparedGoodwareIndex:
    """A verified Goodware index whose reads are local SQLite-only."""

    baseline_id: UUID
    baseline_fingerprint_sha256: str
    index_descriptor: BlobDescriptor
    manifest_descriptor: BlobDescriptor
    path: Path

    async def lookup(self, feature_kind: str, normalized_value: str) -> int | None:
        return await asyncio.to_thread(
            GoodwareSQLiteReader(self.path).lookup,
            feature_kind,
            normalized_value,
        )

    async def lookup_batch(
        self, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]:
        if not features:
            return {}
        return await asyncio.to_thread(GoodwareSQLiteReader(self.path).lookup_batch, features)


class GoodwareService:
    def __init__(self, blobs: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._blobs, self._uow_factory = blobs, uow_factory

    async def import_index(self, artifact_dir: Path, source_dir: Path) -> GoodwareBaseline:
        """Validate and persist one immutable v2 goodware index."""
        manifest = _validate_artifact(artifact_dir, source_dir)
        fingerprint = cast(str, manifest["baseline_fingerprint_sha256"])
        async with self._uow_factory() as uow:
            existing = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                fingerprint
            )
            if existing is not None:
                return existing

        sources: list[GoodwareSource] = []
        for source_value in cast(list[dict[str, object]], manifest["sources"]):
            filename = cast(str, source_value["filename"])
            with (source_dir / filename).open("rb") as handle:
                blob = await self._blobs.ingest(
                    handle,
                    logical_bucket="goodware-baselines",
                    mime_type="application/octet-stream",
                )
            _ensure_ingested_descriptor(
                blob.descriptor,
                expected_sha256=cast(str, source_value["sha256"]),
                expected_size=cast(int, source_value["size"]),
                label=f"source {filename}",
            )
            sources.append(
                GoodwareSource(
                    filename=filename,
                    feature_kind=cast(str, source_value["feature_kind"]),
                    sha256=cast(str, source_value["sha256"]),
                    size=cast(int, source_value["size"]),
                    blob_id=blob.id,
                )
            )

        with (artifact_dir / INDEX_FILENAME).open("rb") as handle:
            index_blob = await self._blobs.ingest(
                handle,
                logical_bucket="goodware-indexes",
                mime_type="application/vnd.sqlite3",
            )
        _ensure_ingested_descriptor(
            index_blob.descriptor,
            expected_sha256=cast(str, manifest["index_sha256"]),
            expected_size=cast(int, manifest["index_size"]),
            label=INDEX_FILENAME,
        )
        manifest_bytes = canonical_manifest_bytes(manifest)
        with (artifact_dir / MANIFEST_FILENAME).open("rb") as handle:
            manifest_blob = await self._blobs.ingest(
                handle,
                logical_bucket="goodware-index-manifests",
                mime_type="application/json",
            )
        _ensure_ingested_descriptor(
            manifest_blob.descriptor,
            expected_sha256=manifest_sha256(manifest),
            expected_size=len(manifest_bytes),
            label=MANIFEST_FILENAME,
        )

        baseline = GoodwareBaseline(
            id=uuid4(),
            baseline_fingerprint_sha256=fingerprint,
            source_set_sha256=cast(str, manifest["source_set_sha256"]),
            normalization_version=cast(str, manifest["normalization_version"]),
            record_count=cast(int, manifest["record_count"]),
            occurrence_sum=cast(int, manifest["occurrence_sum"]),
            pattern_version=NON_DISCRIMINANT_PATTERN_VERSION,
            sources=tuple(sources),
        )
        index_artifact = GoodwareIndexArtifact(
            id=uuid4(),
            baseline_id=baseline.id,
            schema_version=cast(str, manifest["schema_version"]),
            key_version=cast(str, manifest["key_version"]),
            index_format_version=cast(str, manifest["index_format_version"]),
            index_blob_id=index_blob.id,
            manifest_blob_id=manifest_blob.id,
        )

        async with self._uow_factory() as uow:
            inserted = await uow.goodware_baselines.add_if_absent(baseline)
            if not inserted:
                existing = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                    fingerprint
                )
                if existing is None:
                    raise RuntimeError("goodware baseline conflict without row")
                return existing
            await uow.goodware_baselines.add_sources(baseline.id, baseline.sources)
            await uow.goodware_baselines.add_index_artifact(index_artifact)
            await uow.commit()
            return baseline

    async def bind(self, investigation_id: UUID, baseline_id: UUID) -> None:
        async with self._uow_factory() as uow:
            inserted = await uow.investigation_goodware_baselines.add_if_absent(
                investigation_id, baseline_id
            )
            if not inserted:
                current = await uow.investigation_goodware_baselines.get(investigation_id)
                if current != baseline_id:
                    raise ValueError("investigation is already bound to another goodware baseline")
            else:
                await uow.commit()


class GoodwareMeasurementService:
    def __init__(
        self,
        store: BlobStore,
        uow_factory: UnitOfWorkFactory,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self._store = store
        self._uow_factory = uow_factory
        self._cache_root = (
            cache_root if cache_root is not None else get_settings().goodware_cache_root
        )
        self._prepared_indexes: dict[UUID | str, PreparedGoodwareIndex] = {}
        self._preparation_lock = asyncio.Lock()

    async def prepare(self, baseline_ref: UUID | str) -> PreparedGoodwareIndex:
        """Resolve, verify, and cache a baseline for local-only measurements."""
        return await self._prepare_index(baseline_ref)

    async def get_feature_occurrence(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_value: str,
    ) -> int | None:
        return await self._prepared(baseline).lookup(feature_kind, normalized_value)

    async def get_feature_occurrences(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_values: Sequence[str],
    ) -> Mapping[str, int]:
        prepared = self._prepared(baseline)
        values = tuple(dict.fromkeys(normalized_values))
        if not values:
            return {}
        return {
            value: count
            for (kind, value), count in (
                await prepared.lookup_batch(
                    [(feature_kind, value) for value in values]
                )
            ).items()
            if kind == feature_kind
        }

    async def lookup(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_value: str,
    ) -> int | None:
        return await self._prepared(baseline).lookup(feature_kind, normalized_value)

    async def lookup_batch(
        self, baseline: UUID | str, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]:
        return await self._prepared(baseline).lookup_batch(features)

    def _prepared(self, baseline_ref: UUID | str) -> PreparedGoodwareIndex:
        prepared = self._prepared_indexes.get(baseline_ref)
        if prepared is None:
            raise GoodwareMeasurementError("goodware baseline has not been prepared")
        return prepared

    async def _prepare_index(self, baseline_ref: UUID | str) -> PreparedGoodwareIndex:
        prepared = self._prepared_indexes.get(baseline_ref)
        if prepared is not None:
            return prepared

        async with self._preparation_lock:
            prepared = self._prepared_indexes.get(baseline_ref)
            if prepared is not None:
                return prepared

            baseline, artifact, index_descriptor, manifest_descriptor = await self._resolve(
                baseline_ref
            )
            prepared = self._prepared_indexes.get(baseline.id)
            if prepared is None:
                prepared = self._prepared_indexes.get(baseline.baseline_fingerprint_sha256)
            if prepared is None:
                manifest_bytes = await self._store.read(
                    manifest_descriptor, max_bytes=_MAX_MANIFEST_BYTES
                )
                try:
                    manifest = json.loads(manifest_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GoodwareMeasurementError("invalid stored goodware manifest") from exc
                try:
                    canonical = (_canonical_json(manifest) + "\n").encode("utf-8")
                except (TypeError, ValueError) as exc:
                    raise GoodwareMeasurementError(
                        "stored goodware manifest is not canonical JSON"
                    ) from exc
                if manifest_bytes != canonical:
                    raise GoodwareMeasurementError("stored goodware manifest is not canonical JSON")
                manifest = _validate_manifest(manifest)
                self._validate_runtime_metadata(
                    baseline, artifact, index_descriptor, manifest_descriptor, manifest
                )
                index_path = await self._prepare_cached_index(index_descriptor)
                await asyncio.to_thread(
                    _verify_sqlite_index,
                    index_path,
                    manifest,
                    pattern_version=baseline.pattern_version,
                )
                prepared = PreparedGoodwareIndex(
                    baseline_id=baseline.id,
                    baseline_fingerprint_sha256=baseline.baseline_fingerprint_sha256,
                    index_descriptor=index_descriptor,
                    manifest_descriptor=manifest_descriptor,
                    path=index_path,
                )

            self._prepared_indexes[prepared.baseline_id] = prepared
            self._prepared_indexes[prepared.baseline_fingerprint_sha256] = prepared
            self._prepared_indexes[baseline_ref] = prepared
            return prepared

    async def _resolve(
        self, baseline_ref: UUID | str
    ) -> tuple[GoodwareBaseline, GoodwareIndexArtifact, BlobDescriptor, BlobDescriptor]:
        async with self._uow_factory() as uow:
            if isinstance(baseline_ref, UUID):
                baseline = await uow.goodware_baselines.get(baseline_ref)
            else:
                baseline = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                    baseline_ref
                )
            if baseline is None:
                raise GoodwareMeasurementError("goodware baseline does not exist")
            artifact = await uow.goodware_baselines.get_index_artifact(
                baseline.id,
                index_format_version=INDEX_FORMAT_VERSION,
                key_version=KEY_VERSION,
            )
            if artifact is None:
                raise GoodwareMeasurementError("goodware index artifact does not exist")
            index_blob = await uow.blobs.get(artifact.index_blob_id)
            manifest_blob = await uow.blobs.get(artifact.manifest_blob_id)
            if index_blob is None or manifest_blob is None:
                raise GoodwareMeasurementError("goodware index blob metadata is missing")
            return baseline, artifact, index_blob.descriptor, manifest_blob.descriptor

    @staticmethod
    def _validate_runtime_metadata(
        baseline: GoodwareBaseline,
        artifact: GoodwareIndexArtifact,
        index_descriptor: BlobDescriptor,
        manifest_descriptor: BlobDescriptor,
        manifest: Mapping[str, object],
    ) -> None:
        if artifact.baseline_id != baseline.id:
            raise GoodwareMeasurementError("goodware index artifact is bound to another baseline")
        expected = {
            "baseline_fingerprint_sha256": baseline.baseline_fingerprint_sha256,
            "source_set_sha256": baseline.source_set_sha256,
            "normalization_version": baseline.normalization_version,
            "record_count": baseline.record_count,
            "occurrence_sum": baseline.occurrence_sum,
            "schema_version": artifact.schema_version,
            "key_version": artifact.key_version,
            "index_format_version": artifact.index_format_version,
            "index_sha256": index_descriptor.sha256,
            "index_size": index_descriptor.size,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise GoodwareMeasurementError("goodware manifest metadata does not match PostgreSQL")
        if manifest_descriptor.logical_bucket != "goodware-index-manifests":
            raise GoodwareMeasurementError("goodware manifest has an unexpected blob bucket")
        if index_descriptor.logical_bucket != "goodware-indexes":
            raise GoodwareMeasurementError("goodware index has an unexpected blob bucket")

    async def _prepare_cached_index(self, descriptor: BlobDescriptor) -> Path:
        return await prepare_cached_index(
            self._store,
            descriptor,
            self._cache_root,
            verify=_verify_cached_file,
        )
