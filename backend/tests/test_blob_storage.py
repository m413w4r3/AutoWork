from io import BytesIO
from pathlib import Path

import pytest

from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


@pytest.mark.asyncio
async def test_filesystem_blob_put_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    root = Path(str(tmp_path)) / "blobs"
    store = FilesystemBlobStore(root)
    content = b"evidence bytes that must never be executed"

    first = await store.put(
        BytesIO(content), logical_bucket="source-documents", mime_type="application/pdf"
    )
    second = await store.put(
        BytesIO(content), logical_bucket="source-documents", mime_type="application/pdf"
    )

    assert first == second
    assert first.object_key == f"source-documents/{first.sha256[:2]}/{first.sha256}"
    assert await store.exists(first)
    stored_files = [path for path in root.rglob("*") if path.is_file()]
    assert stored_files == [root / first.object_key]
