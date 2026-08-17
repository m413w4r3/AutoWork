import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.source_filenames import validate_logical_filename
from cti_app.domain.blobs import BlobRecord, utc_now
from cti_app.domain.entities import Sample, SourceDocument, Subject
from cti_app.domain.errors import EntityNotFoundError

SUBJECT_DIRECTORIES = (
    "00_intake",
    "01_sources/original",
    "01_sources/extracted",
    "02_evidence",
    "03_samples/original",
    "03_samples/manifests",
    "03_samples/quarantine",
    "04_hunt/queries",
    "04_hunt/raw_results",
    "04_hunt/review",
    "04_hunt/validated/samples",
    "05_analysis/triage",
    "05_analysis/unpacked",
    "05_analysis/configs",
    "05_analysis/notebooks",
    "06_pivots/graph",
    "06_pivots/clusters",
    "07_detections/yara",
    "07_detections/suricata",
    "07_detections/tests/positive",
    "07_detections/tests/negative",
    "07_detections/tests/holdout",
    "07_detections/reports",
    "08_figures",
    "09_draft",
    "10_review",
    "11_release",
)


@dataclass(frozen=True, slots=True)
class WorkspaceMaterialization:
    path: Path
    source_count: int
    sample_count: int


class SubjectWorkspaceMaterializer:
    """Builds a disposable analyst view from canonical entities and blobs."""

    def __init__(
        self,
        store: BlobStore,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._clock = clock

    async def materialize(
        self,
        subject: Subject,
        source_documents: Sequence[SourceDocument],
        samples: Sequence[Sample],
        blobs: Mapping[UUID, BlobRecord],
        workspace_root: Path,
    ) -> WorkspaceMaterialization:
        subject_path = self._prepare_directories(workspace_root, subject.slug)
        source_manifest = []
        for document in source_documents:
            raw_blob = self._require_blob(blobs, document.blob_id)
            decoded_blob = self._require_blob(blobs, document.decoded_blob_id or document.blob_id)
            logical_filename = validate_logical_filename(
                document.logical_filename or document.original_name
            )
            relative_path = Path("01_sources/original") / logical_filename
            destination = subject_path / relative_path
            if destination.parent.is_symlink() or not destination.parent.resolve().is_relative_to(
                subject_path.resolve()
            ):
                raise ValueError("Source materialization escaped the workspace")
            method = await self._store.materialize(decoded_blob.descriptor, destination)
            source_manifest.append(
                self._source_manifest_entry(document, raw_blob, decoded_blob, relative_path, method)
            )

        sample_manifest = []
        for sample in samples:
            blob = self._require_blob(blobs, sample.blob_id)
            relative_path = Path("03_samples/original") / blob.descriptor.sha256
            method = await self._store.materialize(blob.descriptor, subject_path / relative_path)
            sample_manifest.append(self._sample_manifest_entry(sample, blob, relative_path, method))

        manifest = {
            "subject": {
                "id": str(subject.id),
                "external_id": subject.external_id,
                "slug": subject.slug,
                "tlp": subject.tlp.value,
            },
            "generated_at": self._clock().isoformat(),
            "canonical": False,
            "sources": source_manifest,
            "samples": sample_manifest,
        }
        self._write_manifest(subject_path / "manifest.json", manifest)
        return WorkspaceMaterialization(
            path=subject_path,
            source_count=len(source_documents),
            sample_count=len(samples),
        )

    @staticmethod
    def _prepare_directories(workspace_root: Path, slug: str) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        root = workspace_root.resolve()
        subject_path = root / slug
        if subject_path.is_symlink():
            raise ValueError("Refusing to materialize through a symbolic link")
        subject_path.mkdir(parents=True, exist_ok=True)
        if not subject_path.resolve().is_relative_to(root):
            raise ValueError("Workspace path escaped its configured root")
        for relative_directory in SUBJECT_DIRECTORIES:
            destination = subject_path / relative_directory
            if destination.is_symlink():
                raise ValueError("Refusing to materialize through a symbolic link")
            destination.mkdir(parents=True, exist_ok=True)
        return subject_path

    @staticmethod
    def _source_manifest_entry(
        document: SourceDocument,
        raw_blob: BlobRecord,
        decoded_blob: BlobRecord,
        relative_path: Path,
        method: str,
    ) -> dict[str, object]:
        return {
            "id": str(document.id),
            "source_collection_id": (
                str(document.source_collection_id) if document.source_collection_id else None
            ),
            "source_candidate_id": (
                str(document.source_candidate_id) if document.source_candidate_id else None
            ),
            "logical_filename": document.logical_filename or document.original_name,
            "path": relative_path.as_posix(),
            "raw_blob_id": str(raw_blob.id),
            "decoded_blob_id": str(decoded_blob.id),
            "encoded_sha256": document.encoded_sha256 or raw_blob.descriptor.sha256,
            "decoded_sha256": document.decoded_sha256 or decoded_blob.descriptor.sha256,
            "encoded_size": document.encoded_size or raw_blob.descriptor.size,
            "decoded_size": document.decoded_size or decoded_blob.descriptor.size,
            "mime_type": document.detected_mime_type or decoded_blob.descriptor.mime_type,
            "requested_url": document.origin,
            "final_url": document.final_url or document.origin,
            "published_at": document.published_at.isoformat() if document.published_at else None,
            "publisher": document.publisher,
            "title": document.title,
            "tlp": document.tlp.value,
            "acquired_at": document.acquired_at.isoformat(),
            "materialization": method,
            "canonical_status": "archived",
        }

    @staticmethod
    def _sample_manifest_entry(
        asset: Sample,
        blob: BlobRecord,
        relative_path: Path,
        method: str,
    ) -> dict[str, object]:
        return {
            "id": str(asset.id),
            "blob_id": str(blob.id),
            "sha256": blob.descriptor.sha256,
            "size": blob.descriptor.size,
            "mime_type": blob.descriptor.mime_type,
            "logical_bucket": blob.descriptor.logical_bucket,
            "path": relative_path.as_posix(),
            "materialization": method,
            "original_name": asset.original_name,
            "origin": asset.origin,
            "acquired_at": asset.acquired_at.isoformat(),
            "license_restriction": asset.license_restriction,
            "tlp": asset.tlp.value,
            "do_not_submit": asset.do_not_submit,
            "external_llm_allowed": asset.external_llm_allowed,
        }

    @staticmethod
    def _require_blob(blobs: Mapping[UUID, BlobRecord], blob_id: UUID) -> BlobRecord:
        try:
            return blobs[blob_id]
        except KeyError as exc:
            raise EntityNotFoundError(f"Blob metadata {blob_id} is missing") from exc

    @staticmethod
    def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
