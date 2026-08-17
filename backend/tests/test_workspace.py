import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

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
    subject = Subject(external_id="SUBJ-2026-TEST-1", slug="workspace-test", tlp=TLP.RED)
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
    assert manifest["samples"][0]["do_not_submit"] is True
    assert manifest["sources"][0]["logical_filename"] == (
        "2026-08-07_TLP AMBER_Rapport_Example.pdf"
    )
