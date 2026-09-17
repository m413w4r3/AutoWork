import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.workspace import SUBJECT_DIRECTORIES, SubjectWorkspaceMaterializer
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SourceDocument, Subject
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


@pytest.mark.asyncio
async def test_subject_workspace_materializes_logical_tree_without_using_original_names(
    tmp_path: Path,
) -> None:
    fixed_time = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    store = FilesystemBlobStore(tmp_path / "blob-store")
    descriptor = await store.put(
        BytesIO(b"#!/bin/sh\nexit 99\n"),
        logical_bucket="controlled-samples",
        mime_type="application/octet-stream",
    )
    blob = BlobRecord(descriptor=descriptor)
    subject = Subject(
        edition_id=UUID("11111111-1111-1111-1111-111111111111"),
        title="Workspace test subject",
        slug="workspace-test",
        tlp=TLP.RED,
    )
    document = SourceDocument(
        subject_id=subject.id,
        blob_id=blob.id,
        original_name="report.pdf",
        origin="manual-import",
        acquired_at=fixed_time,
        license_restriction="internal use",
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=False,
        logical_filename="2026-08-07_TLP AMBER_Rapport_Example.pdf",
    )
    sample = Sample(
        subject_id=subject.id,
        blob_id=blob.id,
        original_name="do-not-run.sh",
        origin="vendor-attachment",
        acquired_at=fixed_time,
        license_restriction="do not redistribute",
        tlp=TLP.RED,
        do_not_submit=True,
        external_llm_allowed=False,
    )
    materializer = SubjectWorkspaceMaterializer(store, clock=lambda: fixed_time)

    result = await materializer.materialize(
        subject,
        [document],
        [sample],
        {blob.id: blob},
        tmp_path / "workspaces",
    )

    assert result.source_count == 1
    assert result.sample_count == 1
    for relative_directory in SUBJECT_DIRECTORIES:
        assert (result.path / relative_directory).is_dir()
    source_path = result.path / "01_sources/original/2026-08-07_TLP AMBER_Rapport_Example.pdf"
    sample_path = result.path / "03_samples/original" / descriptor.sha256
    assert source_path.read_bytes() == b"#!/bin/sh\nexit 99\n"
    assert sample_path.read_bytes() == b"#!/bin/sh\nexit 99\n"
    assert not (result.path / "03_samples/original/do-not-run.sh").exists()
    manifest = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["canonical"] is False
    assert manifest["subject"]["edition_id"] == str(subject.edition_id)
    assert manifest["subject"]["title"] == subject.title
    assert "external_id" not in manifest["subject"]
    assert manifest["samples"][0]["do_not_submit"] is True
    assert manifest["sources"][0]["logical_filename"] == (
        "2026-08-07_TLP AMBER_Rapport_Example.pdf"
    )


@pytest.mark.asyncio
async def test_subject_workspace_is_namespaced_by_edition_for_same_slug(
    tmp_path: Path,
) -> None:
    """The same slug in two Editions must never share a filesystem projection."""
    fixed_time = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    store = FilesystemBlobStore(tmp_path / "blob-store")
    descriptor_a = await store.put(
        BytesIO(b"edition-a"),
        logical_bucket="controlled-samples",
        mime_type="application/octet-stream",
    )
    descriptor_b = await store.put(
        BytesIO(b"edition-b"),
        logical_bucket="controlled-samples",
        mime_type="application/octet-stream",
    )
    blob_a = BlobRecord(descriptor=descriptor_a)
    blob_b = BlobRecord(descriptor=descriptor_b)
    edition_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    edition_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    subject_a = Subject(
        edition_id=edition_a,
        title="Subject A",
        slug="shared-slug",
        tlp=TLP.AMBER,
    )
    subject_b = Subject(
        edition_id=edition_b,
        title="Subject B",
        slug="shared-slug",
        tlp=TLP.RED,
    )
    sample_a = Sample(
        subject_id=subject_a.id,
        blob_id=blob_a.id,
        original_name="a.bin",
        origin="vendor-attachment",
        acquired_at=fixed_time,
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=False,
    )
    sample_b = Sample(
        subject_id=subject_b.id,
        blob_id=blob_b.id,
        original_name="b.bin",
        origin="vendor-attachment",
        acquired_at=fixed_time,
        license_restriction=None,
        tlp=TLP.RED,
        do_not_submit=True,
        external_llm_allowed=False,
    )
    materializer = SubjectWorkspaceMaterializer(store, clock=lambda: fixed_time)
    workspace_root = tmp_path / "workspaces"

    result_a = await materializer.materialize(
        subject_a, [], [sample_a], {blob_a.id: blob_a}, workspace_root
    )
    result_b = await materializer.materialize(
        subject_b, [], [sample_b], {blob_b.id: blob_b}, workspace_root
    )

    root = workspace_root.resolve()
    assert result_a.path != result_b.path
    assert result_a.path == root / str(edition_a) / "shared-slug"
    assert result_b.path == root / str(edition_b) / "shared-slug"

    manifest_a = json.loads((result_a.path / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((result_b.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["subject"] == {
        "id": str(subject_a.id),
        "edition_id": str(edition_a),
        "title": "Subject A",
        "slug": "shared-slug",
        "tlp": TLP.AMBER.value,
    }
    assert manifest_b["subject"] == {
        "id": str(subject_b.id),
        "edition_id": str(edition_b),
        "title": "Subject B",
        "slug": "shared-slug",
        "tlp": TLP.RED.value,
    }

    projected_a = result_a.path / manifest_a["samples"][0]["path"]
    projected_b = result_b.path / manifest_b["samples"][0]["path"]
    assert projected_a.read_bytes() == b"edition-a"
    assert projected_b.read_bytes() == b"edition-b"
    assert projected_a.read_bytes() != projected_b.read_bytes()
    assert not (result_a.path / manifest_b["samples"][0]["path"]).exists()
    assert not (result_b.path / manifest_a["samples"][0]["path"]).exists()
